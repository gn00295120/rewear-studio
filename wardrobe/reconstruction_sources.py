"""Server-owned provenance for bounded, confirmed-group reconstruction inputs.

The manager enforces account/group membership. These models bind those approved
items to named local references; no URL is fetched and no client path is accepted.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SourceID = Literal["cutout", "context", "extra_1", "extra_2"]
SOURCE_IDS = ("cutout", "context", "extra_1", "extra_2")
Identifier = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
SHA256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
MediaURL = Annotated[str, Field(min_length=1, max_length=2048)]


class ReferenceSource(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: SourceID
    item_id: Identifier
    job_id: Identifier
    role: Literal["target_locator", "original_context"]
    media_url: MediaURL
    source_url: MediaURL | None = None
    original_url: MediaURL | None = None
    file_sha256: SHA256
    reference_sha256: SHA256 | None = None

    @model_validator(mode="after")
    def local_original(self):
        for url in (self.media_url, self.source_url, self.original_url):
            if url is None:
                continue
            prefix = f"/media/{self.job_id}/"
            relative = url.removeprefix(prefix)
            if (
                not url.startswith(prefix)
                or any(character in url for character in ("\\", "%", "?", "#"))
                or any(ord(character) < 32 or ord(character) == 127 for character in url)
                or any(part in {"", ".", "..", "reconstructions", "shopping"} for part in relative.split("/"))
            ):
                raise ValueError("Reference URLs must name original media in their own job")
        if Path(self.media_url).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise ValueError("Reference must be a supported original image")
        if self.role != ("target_locator" if self.id == "cutout" else "original_context"):
            raise ValueError("Reference role disagrees with its source ID")
        return self


class ReferenceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    version: Literal[1] = 1
    target_item_id: Identifier
    group_id: Identifier | None = None
    membership_sha256: SHA256 | None = None
    primary_source: Literal["context", "cutout"]
    sources: list[ReferenceSource] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def source_bindings(self):
        by_id = {source.id: source for source in self.sources}
        if len(by_id) != len(self.sources) or "cutout" not in by_id:
            raise ValueError("References require one unique primary cutout")
        if self.primary_source != ("context" if "context" in by_id else "cutout"):
            raise ValueError("Primary source must be the primary photograph when available")
        primary = by_id["cutout"]
        for name in ("cutout", "context"):
            source = by_id.get(name)
            if source and (source.item_id != self.target_item_id or source.job_id != primary.job_id):
                raise ValueError("Primary references must belong to the target item and job")
        extras = [source for source in self.sources if source.id.startswith("extra_")]
        if "extra_2" in by_id and "extra_1" not in by_id:
            raise ValueError("Additional reference IDs must be contiguous")
        if len({source.item_id for source in extras}) != len(extras) or any(
            source.item_id == self.target_item_id for source in extras
        ):
            raise ValueError("Additional references must be distinct other items")
        if (self.group_id is None) != (self.membership_sha256 is None) or (extras and self.group_id is None):
            raise ValueError("Additional references require a confirmed-group snapshot")
        if [source.id for source in self.sources] != [name for name in SOURCE_IDS if name in by_id]:
            raise ValueError("Reference manifest must use canonical source order")
        return self


@dataclass(frozen=True)
class ReconstructionSourceBundle:
    manifest: ReferenceManifest
    paths: Mapping[SourceID, Path]

    def __post_init__(self):
        if not isinstance(self.manifest, ReferenceManifest):
            raise ValueError("Source bundle requires a validated reference manifest")
        manifest = ReferenceManifest.model_validate(self.manifest.model_dump())
        if set(self.paths) != {source.id for source in manifest.sources}:
            raise ValueError("Source paths must exactly cover the reference manifest")
        if any(not isinstance(path, Path) for path in self.paths.values()):
            raise ValueError("Source bundle paths must be server-owned Path values")
        object.__setattr__(self, "manifest", manifest)
        object.__setattr__(self, "paths", MappingProxyType(dict(self.paths)))
