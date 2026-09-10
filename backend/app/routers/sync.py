"""Compares the user's live Vinted listings (scraped by the extension, since Vinted
has no API) against their live eBay listings (via eBay's Trading API), and lets
the user import anything missing on eBay straight into Lister's normal review
pipeline - it becomes a regular draft_pending listing, same as photo-sourced ones,
so it still goes through Ollama copy generation and human review before posting.
"""
import difflib
import io
import json
import re
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from PIL import Image

from app import ebay_client
from app.config import get_settings
from app.db import get_connection
from app.ollama_client import generate_listing_copy
from app.photo_sources import local
from app.schemas import VintedItemIn, VintedItemOut

router = APIRouter(prefix="/sync", tags=["sync"])

_MATCH_THRESHOLD = 0.6


def get_local_vinted_photos(vinted_url: str | None) -> list[Path]:
    """Finds all photos for a Vinted listing in the local directory structure."""
    if not vinted_url:
        return []
    m = re.search(r"/items/(\d+)", vinted_url)
    if not m:
        return []
    item_id = m.group(1)
    settings = get_settings()
    if not settings.vinted_photos_dir:
        return []
    folder = Path(settings.vinted_photos_dir) / item_id
    if not folder.exists() or not folder.is_dir():
        return []

    valid_exts = {".webp", ".jpg", ".jpeg", ".png", ".heic"}
    photos = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in valid_exts]
    return sorted(photos, key=lambda p: p.name)


def relink_all_vinted_photos() -> int:
    """Updates existing imported listings to use their full set of local photos."""
    updated = 0
    with get_connection() as conn:
        vinted_rows = conn.execute(
            "SELECT * FROM vinted_items WHERE imported_listing_id IS NOT NULL"
        ).fetchall()
        for v in vinted_rows:
            listing_id = v["imported_listing_id"]
            local_photos = get_local_vinted_photos(v["url"])
            if not local_photos:
                continue

            photo_items_data = [{"source": "local", "ref": str(p)} for p in local_photos]
            thumbnail_urls_data = [
                f"/photos/thumbnail?path={local.ensure_thumbnail(p).name}" for p in local_photos
            ]

            conn.execute(
                """UPDATE listings
                   SET photo_items = ?, thumbnail_urls = ?, updated_at = datetime('now')
                   WHERE id = ?""",
                (json.dumps(photo_items_data), json.dumps(thumbnail_urls_data), listing_id),
            )
            updated += 1
    return updated


def _normalize(title: str) -> str:
    return " ".join(title.lower().split())


def _best_match_ratio(title: str, candidates: list[str]) -> float:
    norm = _normalize(title)
    if not candidates:
        return 0.0
    return max(difflib.SequenceMatcher(None, norm, _normalize(c)).ratio() for c in candidates)


@router.post("/vinted/items")
def upsert_vinted_items(items: list[VintedItemIn]):
    """Extension posts whatever it scraped from the Vinted listings page here."""
    # Debug: Log items received
    import json as _json
    with open("backend/vinted_scrape_debug.json", "w") as f:
        _json.dump([dict(item) for item in items], f, indent=2)
    
    with get_connection() as conn:
        for item in items:
            # Handle both photo_url (single, backward compat) and photo_urls (array)
            photo_data = None
            if item.photo_urls is not None:
                # Only save non-empty arrays
                if len(item.photo_urls) > 0:
                    photo_data = json.dumps(item.photo_urls)
            elif item.photo_url is not None:
                # Backward compatibility: convert single photo_url to array format
                photo_data = json.dumps([item.photo_url])

            # Fallback: try to extract price from title if not provided
            price = item.price
            if not price:
                import re as _re
                price_match = _re.search(r'[£$€]\s?(\d+[.,]?\d*)', item.title)
                if price_match:
                    price = price_match.group(1)

            conn.execute(
                """INSERT INTO vinted_items (url, title, price, photo_urls)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(url) DO UPDATE SET title = excluded.title,
                        price = excluded.price, photo_urls = excluded.photo_urls""",
                (item.url, item.title, price, photo_data),
            )
    return {"status": "ok", "count": len(items)}


@router.get("/vinted/items", response_model=list[VintedItemOut])
def list_vinted_items(check_ebay: bool = True, skip_ebay_check: bool = False):
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM vinted_items ORDER BY scraped_at DESC").fetchall()

    ebay_titles: list[str] = []
    if check_ebay and rows and not skip_ebay_check and ebay_client.is_connected():
        try:
            ebay_titles = ebay_client.get_active_listing_titles()
        except RuntimeError:
            # eBay API error - continue without checking (items will show as not on eBay)
            ebay_titles = []

    results = []
    for row in rows:
        data = dict(row)
        # Parse JSON strings to Python lists
        if isinstance(data.get("photo_urls"), str):
            try:
                data["photo_urls"] = json.loads(data["photo_urls"])
            except (json.JSONDecodeError, TypeError):
                data["photo_urls"] = None
        
        on_ebay = bool(data["imported_listing_id"]) or (
            bool(ebay_titles) and _best_match_ratio(data["title"], ebay_titles) >= _MATCH_THRESHOLD
        )
        results.append(VintedItemOut(**data, on_ebay=on_ebay))
    return results


@router.post("/vinted/relink-photos")
def relink_photos_endpoint():
    count = relink_all_vinted_photos()
    return {"status": "ok", "relinked_listings": count}


@router.post("/vinted/import/{vinted_item_id}")
def import_vinted_item(vinted_item_id: int, item_hint: str | None = None):
    """Runs the Vinted photos (all local photos if present, else scraped thumbnail)
    through Ollama copy generator and creates a draft_pending eBay listing."""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM vinted_items WHERE id = ?", (vinted_item_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Vinted item not found")

    local_photos = get_local_vinted_photos(row["url"])
    if local_photos:
        images: list[bytes] = []
        photo_items_data = []
        thumbnail_urls_data = []
        for p in local_photos:
            photo_items_data.append({"source": "local", "ref": str(p)})
            thumb = local.ensure_thumbnail(p)
            thumbnail_urls_data.append(f"/photos/thumbnail?path={thumb.name}")
            with Image.open(p) as img:
                img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=90)
                images.append(buf.getvalue())
    else:
        # Fallback: if no local photos, use photo_urls from database (array format)
        photo_data_str = row["photo_urls"]
        if not photo_data_str:
            raise HTTPException(status_code=400, detail="This item has no photos to work from")
        
        import json
        try:
            photo_urls = json.loads(photo_data_str)
        except (json.JSONDecodeError, TypeError):
            raise HTTPException(status_code=500, detail=f"Invalid photo_urls data: {photo_data_str}")
        
        if not photo_urls:
            raise HTTPException(status_code=400, detail="This item has no photos to work from")
        
        # Download all photos from the URL array
        images = []
        photo_items_data = []
        thumbnail_urls_data = []
        for url in photo_urls:
            try:
                with httpx.Client(timeout=30, follow_redirects=True) as client:
                    photo_resp = client.get(url)
                    photo_resp.raise_for_status()
                    image_bytes = photo_resp.content
                images.append(image_bytes)
                photo_items_data.append({"source": "url", "ref": url})
                thumbnail_urls_data.append(url)
            except httpx.HTTPError as exc:
                # Skip failed photos but continue with others
                print(f"Warning: Could not fetch photo {url}: {exc}")
        
        if not images:
            raise HTTPException(status_code=400, detail="Could not download any photos from Vinted")

    hint = item_hint or f"Originally listed on Vinted as: {row['title']}"
    try:
        copy = generate_listing_copy(images=images, platform="ebay", item_hint=hint, price_hint=row["price"])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Ollama generation failed: {exc}") from exc

    # Use Ollama's suggested price or fall back to Vinted price (ensure non-None)
    listing_price = copy.get("suggested_price_gbp") or row["price"] or ""

    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO listings
                (photo_items, thumbnail_urls, platform, title, description, price, condition, tags, status)
               VALUES (?, ?, 'ebay', ?, ?, ?, ?, ?, 'draft_pending')""",
            (
                json.dumps(photo_items_data),
                json.dumps(thumbnail_urls_data),
                copy.get("title"),
                copy.get("description"),
                listing_price,
                copy.get("condition"),
                copy.get("tags"),
            ),
        )
        listing_id = cur.lastrowid
        conn.execute("UPDATE vinted_items SET imported_listing_id = ? WHERE id = ?", (listing_id, vinted_item_id))
    return {"status": "imported", "listing_id": listing_id}
