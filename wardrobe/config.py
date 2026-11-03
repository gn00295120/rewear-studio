from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def default_data_dir() -> Path:
    if (ROOT / "pyproject.toml").is_file():
        return ROOT / "data"
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Rewear"
    return Path.home() / ".local/share/rewear"


def _key() -> str | None:
    value = os.environ.get("MODEL_API_KEY")
    if value:
        return value.strip() or None
    try:
        return (Path.home() / ".config/meta/model_api_key").read_text().strip() or None
    except OSError:
        return None


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("REWEAR_DATA_DIR", default_data_dir()))
    )
    ollama_url: str = field(
        default_factory=lambda: os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
    )
    ollama_model: str = field(default_factory=lambda: os.environ.get("OLLAMA_MODEL", "muse-glimmer:30b"))
    meta_url: str = field(
        default_factory=lambda: os.environ.get("META_API_URL", "https://api.meta.ai/v1").rstrip("/")
    )
    api_key: str | None = field(default_factory=_key, repr=False)
    image_search_provider: str = field(
        default_factory=lambda: os.environ.get("REWEAR_IMAGE_SEARCH", "disabled").strip()
    )
    vision_api_key: str | None = field(
        default_factory=lambda: os.environ.get("REWEAR_VISION_API_KEY", "").strip() or None,
        repr=False,
    )
    vision_project: str | None = field(
        default_factory=lambda: os.environ.get("REWEAR_VISION_PROJECT", "").strip() or None
    )
    image_search_timeout: float = 20.0
    request_timeout: float = 120.0
    reconstruction_timeout: float = 240.0
    max_upload_mb: int = 50
    max_video_seconds: int = 30
    max_items: int = 24
    job_timeout: float = 720.0
    max_queue: int = 64
    max_batch_files: int = 32
    max_batch_upload_mb: int = 256

    def __post_init__(self):
        object.__setattr__(self, "data_dir", Path(self.data_dir).expanduser().resolve())
        if self.image_search_provider not in {"disabled", "google_vision"}:
            raise ValueError("REWEAR_IMAGE_SEARCH must be 'disabled' or 'google_vision'.")
        if self.vision_project and not re.fullmatch(
            r"[a-z][a-z0-9-]{4,61}[a-z0-9]|[0-9]{6,20}", self.vision_project
        ):
            raise ValueError("REWEAR_VISION_PROJECT must be a valid Google Cloud project ID or number.")

    @property
    def image_search_enabled(self) -> bool:
        return self.image_search_provider == "google_vision"

    @property
    def image_search_configured(self) -> bool:
        # Configuration is not a successful permission or retrieval check.
        return self.image_search_enabled and bool(self.vision_api_key or self.vision_project)

    def image_search_health(self) -> dict:
        if not self.image_search_enabled:
            notice = "Currently using text search and original image matching; image-based product indexing is not enabled."
        elif not self.image_search_configured:
            notice = "Google image search is missing a project or API key configuration; text search is still available."
        else:
            notice = "Original clothing images will be sent to Google Search during lookup, then matched against products by Meta; permissions and recall need to be verified through actual searches."
        return {
            "enabled": self.image_search_enabled,
            "provider": "google_vision" if self.image_search_enabled else None,
            "configured": self.image_search_configured,
            "notice": notice,
        }
