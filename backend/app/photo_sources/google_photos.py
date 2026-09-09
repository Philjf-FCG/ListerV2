"""Google Photos ingestion via the Photos Picker API (the only API that still
grants read access to a user's library since the 2025 Library API scope changes).

Flow:
  1. GET /auth/google/login          -> redirects user to Google consent screen
  2. GET /auth/google/callback       -> exchanges code for tokens, stores locally
  3. POST /photos/google/session     -> creates a Picker session, returns pickerUri
                                          for the user to open and pick photos
  4. GET  /photos/google/session/{id}-> poll until photos have been picked
  5. GET  /photos/google/items/{id}  -> list picked media items as PhotoItems
"""
import json
import time as time_mod
from datetime import datetime
from pathlib import Path

import httpx
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from app.config import BACKEND_DIR, get_settings
from app.schemas import PhotoItem

_SCOPES = ["https://www.googleapis.com/auth/photospicker.mediaitems.readonly"]
_TOKEN_FILE = BACKEND_DIR / "google_token.json"
_PICKER_BASE = "https://photospicker.googleapis.com/v1"

# Google enforces PKCE for loopback redirect URIs; the verifier generated during
# /auth/google/login must be reused in /auth/google/callback, so stash it here
# keyed by the OAuth `state` value (fine for a single-user local app).
_PENDING_CODE_VERIFIERS: dict[str, str] = {}

# The Picker API only supports mediaItems.list (no mediaItems.get-by-id), so cache
# each item's baseUrl/mimeType here when listed - baseUrls are valid for ~60 minutes.
_MEDIA_ITEM_CACHE: dict[str, dict] = {}


def _client_config() -> dict:
    settings = get_settings()
    return {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.google_redirect_uri],
        }
    }


def build_auth_url() -> str:
    settings = get_settings()
    flow = Flow.from_client_config(_client_config(), scopes=_SCOPES, redirect_uri=settings.google_redirect_uri)
    auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")
    _PENDING_CODE_VERIFIERS[state] = flow.code_verifier
    return auth_url


def exchange_code(code: str, state: str | None = None) -> None:
    settings = get_settings()
    flow = Flow.from_client_config(_client_config(), scopes=_SCOPES, redirect_uri=settings.google_redirect_uri)
    code_verifier = _PENDING_CODE_VERIFIERS.pop(state, None) if state else None
    if code_verifier:
        flow.code_verifier = code_verifier
    flow.fetch_token(code=code)
    creds = flow.credentials
    _TOKEN_FILE.write_text(creds.to_json())


def _load_credentials() -> Credentials | None:
    if not _TOKEN_FILE.exists():
        return None
    data = json.loads(_TOKEN_FILE.read_text())
    return Credentials.from_authorized_user_info(data, _SCOPES)


def is_connected() -> bool:
    return _load_credentials() is not None


def _auth_header() -> dict:
    creds = _load_credentials()
    if creds is None:
        raise RuntimeError("Google Photos is not connected yet; visit /auth/google/login first")
    if creds.expired and creds.refresh_token:
        import google.auth.transport.requests

        creds.refresh(google.auth.transport.requests.Request())
        _TOKEN_FILE.write_text(creds.to_json())
    return {"Authorization": f"Bearer {creds.token}"}


def _raise_with_google_detail(resp: httpx.Response) -> None:
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = resp.text
        raise RuntimeError(f"Google Photos API error {resp.status_code}: {detail}") from exc


def create_picker_session() -> dict:
    with httpx.Client() as client:
        resp = client.post(f"{_PICKER_BASE}/sessions", headers=_auth_header())
        _raise_with_google_detail(resp)
        return resp.json()


def get_picker_session(session_id: str) -> dict:
    with httpx.Client() as client:
        resp = client.get(f"{_PICKER_BASE}/sessions/{session_id}", headers=_auth_header())
        _raise_with_google_detail(resp)
        return resp.json()


def wait_for_selection(session_id: str, timeout_seconds: int = 300) -> bool:
    """Polls the session until the user finishes picking, or timeout. Returns True if photos were picked."""
    deadline = time_mod.time() + timeout_seconds
    while time_mod.time() < deadline:
        session = get_picker_session(session_id)
        if session.get("mediaItemsSet"):
            return True
        time_mod.sleep(2)
    return False


def list_picked_items(session_id: str) -> list[PhotoItem]:
    items: list[PhotoItem] = []
    page_token = None
    with httpx.Client() as client:
        while True:
            params = {"sessionId": session_id}
            if page_token:
                params["pageToken"] = page_token
            resp = client.get(f"{_PICKER_BASE}/mediaItems", headers=_auth_header(), params=params)
            _raise_with_google_detail(resp)
            data = resp.json()
            for media_item in data.get("mediaItems", []):
                media_file = media_item.get("mediaFile", {})
                create_time = media_item.get("createTime")
                taken_at = datetime.fromisoformat(create_time.replace("Z", "+00:00")) if create_time else None
                _MEDIA_ITEM_CACHE[media_item["id"]] = {
                    "baseUrl": media_file.get("baseUrl"),
                    "mimeType": media_file.get("mimeType", "image/jpeg"),
                }
                items.append(
                    PhotoItem(
                        source="google_photos",
                        ref=media_item["id"],
                        thumbnail_url=f"/photos/google/thumbnail/{media_item['id']}",
                        taken_at=taken_at,
                    )
                )
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    return items


def _cached_media_file(media_item_id: str) -> dict:
    cached = _MEDIA_ITEM_CACHE.get(media_item_id)
    if not cached or not cached.get("baseUrl"):
        raise RuntimeError(
            "This photo's Google Photos session has expired or the backend was restarted - "
            "go back and re-scan Google Photos to pick it again."
        )
    return cached


def get_cached_mime_type(media_item_id: str) -> str:
    cached = _MEDIA_ITEM_CACHE.get(media_item_id)
    return (cached or {}).get("mimeType", "image/jpeg")


def get_cached_base_url(media_item_id: str) -> str | None:
    cached = _MEDIA_ITEM_CACHE.get(media_item_id)
    if cached and cached.get("baseUrl"):
        return f"{cached['baseUrl']}=w1600-h1600"
    return None


def download_media_bytes(media_item_id: str) -> bytes:
    """Download the actual bytes for a previously-listed media item."""
    base_url = _cached_media_file(media_item_id)["baseUrl"]
    with httpx.Client() as client:
        image_resp = client.get(f"{base_url}=d", headers=_auth_header())
        image_resp.raise_for_status()
        return image_resp.content


def fetch_thumbnail(media_item_id: str, size: int = 512) -> tuple[bytes, str]:
    """Fetch a small preview image for the UI. Google's baseUrl requires the same
    OAuth bearer token as the API itself, so a plain <img src> can't load it directly -
    the backend must fetch it and stream the bytes back to the browser."""
    cached = _cached_media_file(media_item_id)
    with httpx.Client() as client:
        image_resp = client.get(f"{cached['baseUrl']}=w{size}-h{size}", headers=_auth_header())
        image_resp.raise_for_status()
        return image_resp.content, cached["mimeType"]
