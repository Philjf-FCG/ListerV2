"""Application configuration loaded from environment / .env file."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Anchor all relative paths to the backend/ directory so behavior doesn't depend
# on the working directory the app happens to be launched from.
BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    ollama_base_url: str = "http://192.168.1.37:11434"
    ollama_vision_model: str = "gemma4:12b"

    local_photo_roots: str = ""
    vinted_photos_dir: str = r"C:\Users\philj\Downloads\steamdeck3-20260902\listings\photos"

    database_path: str = "./lister.db"

    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8010/auth/google/callback"

    # eBay Sell API (production). RuName is eBay's redirect identifier, not a URL -
    # its Auth Accepted URL must be configured in the eBay Developer Portal to
    # point at ebay_redirect_uri below.
    ebay_client_id: str = ""
    ebay_client_secret: str = ""
    ebay_ru_name: str = ""
    ebay_redirect_uri: str = "http://localhost:8010/auth/ebay/callback"
    ebay_marketplace_id: str = "EBAY_GB"
    # One-time seller setup: used to auto-create a merchant inventory location if
    # the seller's account doesn't have one yet (required by the Offer API).
    ebay_location_postal_code: str = ""
    ebay_location_country: str = "GB"

    extension_origin: str = "*"

    @property
    def local_photo_root_list(self) -> list[str]:
        return [p.strip() for p in self.local_photo_roots.split(";") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
