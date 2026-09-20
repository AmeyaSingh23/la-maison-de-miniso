from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

import httpx
from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.db.models.clothing_item import ClothingItem
from app.schemas.outfit import OutfitWeatherContext

logger = logging.getLogger(__name__)
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


class OutfitSuggestion(BaseModel):
    name: str = Field(max_length=120)
    rationale: str = Field(max_length=400)
    item_ids: list[UUID] = Field(min_length=1, max_length=8)


def _retry_after(response: httpx.Response, default: int = 60) -> int:
    try:
        return min(300, max(1, int(response.headers.get("retry-after", default))))
    except (TypeError, ValueError):
        return default


class GroqOutfitGenerator:
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.groq_api_key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Outfit suggestions are not configured on the server.",
            )
        self._api_key = settings.groq_api_key
        self._model = settings.groq_outfit_model

    async def generate(
        self,
        items: list[ClothingItem],
        *,
        occasion: str | None,
        style_notes: str | None,
        weather_context: OutfitWeatherContext | None,
    ) -> OutfitSuggestion:
        inventory = [
            {
                "id": str(item.id),
                "name": item.item_name,
                "category": item.category,
                "custom_category": item.custom_category,
                "color": item.color,
                "pattern": item.pattern,
                "fabric": item.fabric,
                "tags": item.tags,
            }
            for item in items
        ]
        prompt = f"""Create one wearable outfit using only IDs from the inventory JSON below.
Return a JSON object with a concise name, concise rationale, and 1-8 distinct item_ids.
Prefer a coherent primary garment combination (top+bottom, dress, or uniform) and add shoes/accessories only when available and complementary.
Never invent garments, IDs, brands, weather facts, or missing items.
Occasion: {occasion or 'not specified'}
Style notes: {style_notes or 'not specified'}
Weather context: {json.dumps(weather_context.model_dump(mode='json'), separators=(',', ':')) if weather_context else 'not available; do not assume weather conditions'}
Inventory: {json.dumps(inventory, separators=(',', ':'))}"""
        payload = {
            "model": self._model,
            "temperature": 0.35,
            "max_tokens": 500,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": "You are a wardrobe stylist. Return valid JSON only, with no markdown.",
                },
                {"role": "user", "content": prompt},
            ],
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        timeout = httpx.Timeout(30.0, connect=8.0)
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.post(_GROQ_URL, json=payload, headers=headers)
                if response.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
                    retry_after = _retry_after(response)
                    logger.warning("Outfit suggestion rate-limited; retry_after=%s", retry_after)
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail="Outfit suggestions are busy right now.",
                        headers={"Retry-After": str(retry_after)},
                    )
                if response.status_code in {408, 500, 502, 503, 504}:
                    if attempt == 0:
                        await asyncio.sleep(1.0)
                        continue
                    logger.warning("Outfit suggestion service error: status=%s", response.status_code)
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail="Outfit suggestions are temporarily unavailable.",
                    )
                if response.status_code >= 400:
                    logger.error("Outfit suggestion configuration error: status=%s", response.status_code)
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Outfit suggestions are temporarily unavailable.",
                    )
                data = response.json()
                choices = data.get("choices") if isinstance(data, dict) else None
                content = choices[0]["message"].get("content") if isinstance(choices, list) and choices else None
                return OutfitSuggestion.model_validate_json(content)
            except HTTPException:
                raise
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
                if attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
                logger.warning("Outfit suggestion request failed: %s", type(error).__name__)
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Outfit suggestions are temporarily unavailable.",
                ) from error
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Outfit suggestions are temporarily unavailable.",
        )
