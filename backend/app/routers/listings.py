import json
import mimetypes
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.db import get_connection
from app.ollama_client import generate_listing_copy
from app.photo_sources import google_photos, local
from app.schemas import GenerateRequest, ListingOut, ListingUpdate, PhotoRef

router = APIRouter(prefix="/listings", tags=["listings"])


def _load_image_and_thumbnail(item: PhotoRef) -> tuple[bytes, str | None]:
    if item.source == "local":
        path = Path(item.ref)
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"Local photo not found: {item.ref}")
        thumb = local.ensure_thumbnail(path)
        return path.read_bytes(), f"/photos/thumbnail?path={thumb.name}"
    if item.source == "google_photos":
        return google_photos.download_media_bytes(item.ref), f"/photos/google/thumbnail/{item.ref}"
    if item.source == "url":
        try:
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                resp = client.get(item.ref)
                resp.raise_for_status()
                return resp.content, item.ref  # public URL, browser can load it directly
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Couldn't fetch photo URL: {exc}") from exc
    raise HTTPException(status_code=400, detail=f"Unknown source: {item.source}")


def _row_to_listing_out(row) -> ListingOut:
    data = dict(row)
    data["photo_items"] = [PhotoRef(**p) for p in json.loads(data.pop("photo_items") or "[]")]
    data["thumbnail_urls"] = json.loads(data.pop("thumbnail_urls") or "[]")
    return ListingOut(**data)


@router.post("/generate", response_model=list[ListingOut])
def generate(req: GenerateRequest):
    if not req.items:
        raise HTTPException(status_code=400, detail="At least one photo is required")

    images: list[bytes] = []
    thumbnail_urls: list[str] = []
    for item in req.items:
        image_bytes, thumbnail_url = _load_image_and_thumbnail(item)
        images.append(image_bytes)
        if thumbnail_url:
            thumbnail_urls.append(thumbnail_url)

    photo_items_json = json.dumps([item.model_dump() for item in req.items])
    thumbnail_urls_json = json.dumps(thumbnail_urls)

    created: list[ListingOut] = []
    with get_connection() as conn:
        for platform in req.platforms:
            try:
                copy = generate_listing_copy(
                    images=images,
                    platform=platform,
                    item_hint=req.item_hint,
                    condition_hint=req.condition,
                    price_hint=req.price_hint,
                )
                cur = conn.execute(
                    """INSERT INTO listings
                        (photo_items, thumbnail_urls, platform, title, description, price, condition, tags, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft_pending')""",
                    (
                        photo_items_json,
                        thumbnail_urls_json,
                        platform,
                        copy.get("title"),
                        copy.get("description"),
                        copy.get("suggested_price_gbp"),
                        copy.get("condition"),
                        copy.get("tags"),
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - surfaced as a failed row, not a 500
                cur = conn.execute(
                    """INSERT INTO listings
                        (photo_items, thumbnail_urls, platform, status, error)
                       VALUES (?, ?, ?, 'failed', ?)""",
                    (photo_items_json, thumbnail_urls_json, platform, str(exc)),
                )
            row = conn.execute("SELECT * FROM listings WHERE id = ?", (cur.lastrowid,)).fetchone()
            created.append(_row_to_listing_out(row))
    return created


@router.get("", response_model=list[ListingOut])
def list_listings(status: str | None = None, platform: str | None = None):
    query = "SELECT * FROM listings WHERE 1=1"
    params: list[str] = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if platform:
        query += " AND platform = ?"
        params.append(platform)
    query += " ORDER BY created_at DESC"
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_listing_out(r) for r in rows]


@router.get("/{listing_id}", response_model=ListingOut)
def get_listing(listing_id: int):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Listing not found")
    return _row_to_listing_out(row)


@router.patch("/{listing_id}", response_model=ListingOut)
def update_listing(listing_id: int, update: ListingUpdate):
    fields = {k: v for k, v in update.model_dump().items() if v is not None}
    if not fields:
        return get_listing(listing_id)
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    with get_connection() as conn:
        conn.execute(
            f"UPDATE listings SET {set_clause}, updated_at = datetime('now') WHERE id = ?",
            (*fields.values(), listing_id),
        )
        row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Listing not found")
    return _row_to_listing_out(row)


@router.delete("/{listing_id}")
def delete_listing(listing_id: int):
    with get_connection() as conn:
        conn.execute("DELETE FROM listings WHERE id = ?", (listing_id,))
    return {"status": "deleted"}


@router.get("/{listing_id}/photos/{index}")
def get_listing_photo(listing_id: int, index: int):
    """Full-resolution photo bytes for a listing, used by the browser extension to
    actually upload images to Vinted/eBay (not just fill in the text fields)."""
    with get_connection() as conn:
        row = conn.execute("SELECT photo_items FROM listings WHERE id = ?", (listing_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Listing not found")
    items = json.loads(row["photo_items"] or "[]")
    if index < 0 or index >= len(items):
        raise HTTPException(status_code=404, detail="Photo index out of range")
    item = PhotoRef(**items[index])

    if item.source == "local":
        path = Path(item.ref)
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"Local photo not found: {item.ref}")
        mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
        return Response(content=path.read_bytes(), media_type=mime)

    if item.source == "url":
        try:
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                resp = client.get(item.ref)
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Couldn't fetch photo URL: {exc}") from exc
        mime = resp.headers.get("content-type", "image/jpeg")
        return Response(content=resp.content, media_type=mime)

    try:
        content = google_photos.download_media_bytes(item.ref)
        mime = google_photos.get_cached_mime_type(item.ref)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return Response(content=content, media_type=mime)
