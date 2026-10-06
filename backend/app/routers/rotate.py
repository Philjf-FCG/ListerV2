"""Wardrobe rotation: refresh a few of the oldest live Vinted listings.

Flow (nothing here touches Vinted - the extension scans, and the user publishes/deletes):
  1. POST /rotate/scan    - extension posts the full live wardrobe (after auto-scrolling).
  2. POST /rotate/plan    - take the oldest POOL_SIZE by REAL upload date (from the Vinted
                            data export), randomly pick PICK_COUNT, back up each item's photos
                            as JPEG + original.json, and create a draft_pending Vinted listing
                            with freshly written copy. Drafts then go through the normal
                            review -> extension fill flow, and the user clicks Publish.
  3. GET  /rotate/status  - what's in the pool and which rotations are in progress.
  4. POST /rotate/{id}/deleted - user confirms they deleted the original on Vinted.
"""
import json
import logging
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from PIL import Image

from app.config import BACKEND_DIR
from app.db import get_connection
from app.ollama_client import generate_listing_copy
from app.photo_sources import local
from app.routers import sync
from app.schemas import VintedItemIn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rotate", tags=["rotate"])

POOL_SIZE = 20
PICK_COUNT = 5
SCAN_MAX_AGE = timedelta(hours=24)
_BACKUP_DIR = BACKEND_DIR / "rotation_backups"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_days(listed_at: str | None) -> int | None:
    if not listed_at:
        return None
    try:
        return (_now() - datetime.fromisoformat(listed_at)).days
    except ValueError:
        return None


def _live_items(conn) -> tuple[list[dict], str | None]:
    """Items present in the most recent full wardrobe scan (one per Vinted id), joined
    with the export's real upload date/details. Returns (items, scan_at)."""
    scan_at = conn.execute("SELECT MAX(last_seen_at) AS m FROM vinted_items").fetchone()["m"]
    if not scan_at:
        return [], None
    rows = conn.execute(
        "SELECT * FROM vinted_items WHERE last_seen_at = ? AND dismissed = 0 ORDER BY id", (scan_at,)
    ).fetchall()
    export = {r["vinted_id"]: dict(r) for r in conn.execute("SELECT * FROM vinted_export_items").fetchall()}

    seen: set[str] = set()
    items: list[dict] = []
    for row in rows:
        data = dict(row)
        vinted_id = sync._vinted_item_id(data["url"])
        if not vinted_id or vinted_id in seen:
            continue
        seen.add(vinted_id)
        try:
            data["photo_urls"] = json.loads(data.get("photo_urls") or "[]")
        except (json.JSONDecodeError, TypeError):
            data["photo_urls"] = []
        meta = export.get(vinted_id, {})
        data.update(
            vinted_id=vinted_id,
            listed_at=meta.get("listed_at"),
            description=meta.get("description"),
            brand=meta.get("brand"),
            size=meta.get("size"),
            condition=meta.get("condition"),
            colour=meta.get("colour"),
        )
        items.append(data)
    return items, scan_at


def _oldest_pool(live: list[dict], pool_size: int) -> tuple[list[dict], int]:
    """Oldest items by real upload date. Items with no date in the export are newer than
    the export itself (anything live before it was taken would be in it), so they can't
    belong in the oldest pool - they're just counted so the UI can mention them."""
    dated = sorted((i for i in live if i["listed_at"]), key=lambda i: i["listed_at"])
    return dated[:pool_size], len(live) - len(dated)


def _open_rotations(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT l.id AS listing_id, l.status AS listing_status, l.title AS new_title,
                  v.id AS vinted_item_id, v.url, v.title AS original_title, v.rotated_at
           FROM listings l JOIN vinted_items v ON v.id = l.source_vinted_item_id
           WHERE l.status != 'failed' ORDER BY l.id DESC"""
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["delete_ready"] = d["listing_status"] in ("reviewed_ready", "posted_as_draft")
        out.append(d)
    return out


def _summarise(item: dict) -> dict:
    return {
        "vinted_item_id": item["id"],
        "vinted_id": item["vinted_id"],
        "title": item["title"],
        "url": item["url"],
        "listed_at": item["listed_at"],
        "age_days": _age_days(item["listed_at"]),
    }


def _backup_item(item: dict) -> list[Path]:
    """Saves JPEG copies of every photo plus original.json, independent of Vinted, so the
    original listing can be recreated from scratch even after it's deleted."""
    sources = sync.get_local_vinted_photos(item["url"])
    if not sources:
        http_urls = [u for u in item["photo_urls"] if u.lower().startswith("http")]
        if http_urls:
            sync.cache_vinted_photos(item["vinted_id"], http_urls)
            sources = sync.get_local_vinted_photos(item["url"])

    folder = _BACKUP_DIR / item["vinted_id"]
    folder.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for i, src in enumerate(sources):
        dest = folder / f"photo_{i}.jpg"
        try:
            with Image.open(src) as img:
                img.convert("RGB").save(dest, "JPEG", quality=92)
            saved.append(dest)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Couldn't back up photo %s: %s", src, exc)

    details = {k: item.get(k) for k in (
        "url", "title", "price", "description", "brand", "size", "condition", "colour", "listed_at"
    )}
    details.update(photos=[p.name for p in saved], backed_up_at=_now().isoformat(timespec="seconds"))
    (folder / "original.json").write_text(json.dumps(details, indent=2, ensure_ascii=False), encoding="utf-8")
    return saved


def _copy_hint(item: dict) -> str:
    facts = ", ".join(
        f"{label}: {item[key]}"
        for label, key in (("Brand", "brand"), ("Size", "size"), ("Colour", "colour"), ("Condition", "condition"))
        if item.get(key)
    )
    hint = "This item is already on sale on Vinted and is being relisted."
    if facts:
        hint += f" Known facts - {facts}."
    if item.get("description"):
        hint += (
            "\nPrevious description, for factual details only (measurements, inclusions, flaws - "
            "keep every flaw disclosed). Write fresh copy rather than editing it:\n"
            + item["description"]
        )
    return hint


def _create_draft(item: dict, photos: list[Path]) -> tuple[int, str | None]:
    copy = generate_listing_copy(
        images=[str(p) for p in photos],
        platform="vinted",
        item_hint=_copy_hint(item),
        condition_hint=item.get("condition"),
        price_hint=item.get("price"),
    )
    # The real current price is authoritative; only fall back to the model's guess if unknown.
    price = item.get("price") or copy.get("suggested_price_gbp")
    condition = item.get("condition") or copy.get("condition")
    photo_items = [{"source": "local", "ref": str(p)} for p in photos]
    thumbs = [f"/photos/thumbnail?path={local.ensure_thumbnail(p).name}" for p in photos]
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO listings
                   (photo_items, thumbnail_urls, platform, title, description, price, condition,
                    tags, status, source_vinted_item_id)
               VALUES (?, ?, 'vinted', ?, ?, ?, ?, ?, 'draft_pending', ?)""",
            (json.dumps(photo_items), json.dumps(thumbs), copy.get("title"), copy.get("description"),
             price, condition, copy.get("tags"), item["id"]),
        )
        return cur.lastrowid, copy.get("title")


@router.post("/scan")
def scan(items: list[VintedItemIn]):
    """Full live wardrobe from the extension. Every item gets the same scan timestamp, so
    "currently live" means "present in the latest scan" - sold/deleted items drop out."""
    if not items:
        raise HTTPException(status_code=400, detail="Scan contained no items")
    scan_at = _now().isoformat(timespec="seconds")
    sync.upsert_scraped_items(items, scan_at=scan_at, cache_photos=False)
    return {"status": "ok", "count": len(items), "scan_at": scan_at}


@router.post("/import-dates")
def import_dates(export_path: str = r"c:\vinted\data\listings\index.html"):
    """Loads real upload dates/details from the Vinted data export into vinted_export_items
    only. Unlike /sync/import-vinted-export this never touches vinted_items, so it can't
    disturb eBay import links or scan state, and it's safe to re-run."""
    from app.vinted_export_parser import parse_vinted_export

    if not Path(export_path).exists():
        raise HTTPException(status_code=404, detail=f"Export file not found: {export_path}")
    try:
        items = parse_vinted_export(export_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to parse export: {exc}") from exc

    stored = 0
    with get_connection() as conn:
        conn.execute("DELETE FROM vinted_export_items")  # derived data - rebuilt from the export each time
        for item in items:
            vinted_id = sync._vinted_item_id(item.url)
            if not vinted_id:
                continue
            conn.execute(
                """INSERT OR REPLACE INTO vinted_export_items
                       (vinted_id, listed_at, description, brand, size, condition, colour)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (vinted_id, item.listed_at, item.description, item.brand, item.size,
                 item.condition, item.colour),
            )
            stored += 1
        live, _ = _live_items(conn)
    dated = sum(1 for i in live if i["listed_at"])
    return {"status": "ok", "stored": stored, "live_items": len(live), "live_items_with_dates": dated}


@router.get("/status")
def status():
    with get_connection() as conn:
        live, scan_at = _live_items(conn)
        open_rotations = [r for r in _open_rotations(conn) if not r["rotated_at"]]
    pool, undated = _oldest_pool(live, POOL_SIZE)
    return {
        "scan_at": scan_at,
        "live_count": len(live),
        "undated_count": undated,
        "oldest": [_summarise(i) for i in pool],
        "open_rotations": open_rotations,
    }


@router.post("/plan")
def plan(count: int = PICK_COUNT, pool: int = POOL_SIZE, force: bool = False, seed: int | None = None):
    if not 1 <= count <= pool <= 50:
        raise HTTPException(status_code=400, detail="Need 1 <= count <= pool <= 50")

    with get_connection() as conn:
        live, scan_at = _live_items(conn)
        open_rotations = [r for r in _open_rotations(conn) if not r["rotated_at"]]

    if not scan_at or _now() - datetime.fromisoformat(scan_at) > SCAN_MAX_AGE:
        raise HTTPException(status_code=409, detail="Wardrobe scan is missing or older than 24h - scan it again first")
    if open_rotations and not force:
        raise HTTPException(
            status_code=409,
            detail=f"{len(open_rotations)} rotation(s) still in progress - finish deleting those originals first",
        )

    pool_items, undated = _oldest_pool(live, pool)
    if not pool_items:
        raise HTTPException(
            status_code=400,
            detail="No upload dates available for the live items - import your Vinted data export first",
        )

    in_progress = {r["vinted_item_id"] for r in open_rotations}
    eligible = [i for i in pool_items if i["id"] not in in_progress]
    random.Random(seed).shuffle(eligible)

    picked: list[dict] = []
    skipped: list[dict] = []
    for item in eligible:
        if len(picked) >= count:
            break
        try:
            photos = _backup_item(item)
            if not photos:
                skipped.append({**_summarise(item), "reason": "no photos available"})
                continue
            listing_id, new_title = _create_draft(item, photos)
        except Exception as exc:  # noqa: BLE001 - one bad item shouldn't sink the batch
            logger.warning("Rotation draft failed for %s: %s", item["url"], exc)
            skipped.append({**_summarise(item), "reason": str(exc)})
            continue
        picked.append({**_summarise(item), "listing_id": listing_id, "new_title": new_title})

    return {
        "picked": picked,
        "skipped": skipped,
        "pool_size": len(pool_items),
        "undated_items": undated,
        "message": f"{len(picked)} draft(s) created from the oldest {len(pool_items)} items. "
        "Review them in Lister, publish the new listings on Vinted, then delete the originals.",
    }


@router.post("/{vinted_item_id}/deleted")
def confirm_deleted(vinted_item_id: int):
    """The user says they've deleted the original on Vinted. Lister never does this itself."""
    with get_connection() as conn:
        match = [r for r in _open_rotations(conn) if r["vinted_item_id"] == vinted_item_id and not r["rotated_at"]]
        if not match:
            raise HTTPException(status_code=404, detail="No rotation in progress for that item")
        conn.execute(
            "UPDATE vinted_items SET rotated_at = ?, dismissed = 1 WHERE id = ?",
            (_now().isoformat(timespec="seconds"), vinted_item_id),
        )
    return {"status": "ok"}
