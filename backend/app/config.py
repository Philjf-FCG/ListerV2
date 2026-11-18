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
    vinted_photos_dir: str = ""

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

    # When set, every request (except /healthz and CORS preflights) must carry HTTP Basic
    # credentials. Leave unset for a localhost-only dev setup; set it whenever the app is
    # reachable from the network, because the app holds eBay/Google tokens.
    access_user: str = "lister"
    access_password: str = ""

    @property
    def local_photo_root_list(self) -> list[str]:
        return [p.strip() for p in self.local_photo_roots.split(";") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


def default_vinted_export_index() -> str:
    """Where the Vinted data export's listings/index.html lives: next to VINTED_PHOTOS_DIR
    (<export>/listings/photos) when that's configured, else the original Windows location."""
    photos_dir = get_settings().vinted_photos_dir
    if photos_dir:
        return str(Path(photos_dir).parent / "index.html")
    return r"c:\vinted\data\listings\index.html"


def file_uri_to_path(uri: str) -> Path:
    """Turn the file:// URIs this app stores for local photos into a real path on any OS.
    Handles file:///C:/x, file://C:\\x (the export parser's Windows form) and
    file:///srv/x (Linux, where the leading slash must be kept)."""
    rest = uri[len("file://"):] if uri.lower().startswith("file://") else uri
    if len(rest) >= 3 and rest[0] == "/" and rest[1].isalpha() and rest[2] == ":":
        rest = rest[1:]  # "/C:/x" -> "C:/x"
    return Path(rest)
