from __future__ import annotations

import json
import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Category = Literal[
    "top", "bottom", "dress", "outerwear", "onesie", "sleepwear", "shoes", "accessory", "other"
]
Condition = Literal["new_with_tags", "like_new", "good", "fair", "poor", "unknown"]
ShortText = Annotated[str, Field(max_length=200)]
Provider = Literal["local", "meta"]
Mode = Literal["single", "rack", "outfit"]


class Garment(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    category: Category
    subtype: ShortText | None
    brand: ShortText | None
    size_label: ShortText | None
    colors: list[ShortText] = Field(max_length=20)
    pattern: ShortText | None
    material_guess: ShortText | None
    condition: Condition
    defects: list[ShortText] = Field(max_length=30)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    needs_review: bool

    @model_validator(mode="after")
    def require_review(self):
        if self.confidence < 0.7 or self.condition == "unknown" or self.defects:
            self.needs_review = True
        return self


class ItemPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    record: dict | None = None
    reviewed: bool | None = None


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    prompt: str = Field(min_length=10, max_length=2000)
    provider: Provider = "local"
    mode: Mode = "single"
    concept: str = Field(default="garment", min_length=1, max_length=100)


class MergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    item_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(min_length=2, max_length=128)
    primary_item_id: str = Field(min_length=1, max_length=64)


class DiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    market: Literal["TW", "global"] = "TW"


class ReconstructionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reference_item_ids: list[Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]] = Field(
        default_factory=list, max_length=2
    )
    mode: Literal["preview", "verified"] = "preview"

    @model_validator(mode="after")
    def distinct_sources(self):
        if len(set(self.reference_item_ids)) != len(self.reference_item_ids):
            raise ValueError("請選擇不同的參考照片。")
        return self


class ReconstructionSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    candidate_id: Annotated[str, Field(pattern=r"^[1-3]$")]


def person_box(value) -> list[float] | None:
    """A user-selected rectangle in the EXIF-normalized image, never face identity."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            raise ValueError("人物範圍格式不正確。") from None
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("人物範圍需要四個座標。")
    if any(
        isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) or not 0 <= n <= 1
        for n in value
    ):
        raise ValueError("人物範圍座標必須介於 0 和 1。")
    left, top, right, bottom = value
    if right <= left or bottom <= top:
        raise ValueError("請框選有效的人物範圍。")
    return [float(n) for n in value]


def unknown_garment() -> dict:
    return Garment(
        category="other",
        subtype=None,
        brand=None,
        size_label=None,
        colors=[],
        pattern=None,
        material_guess=None,
        condition="unknown",
        defects=[],
        confidence=0.0,
        needs_review=True,
    ).model_dump()
