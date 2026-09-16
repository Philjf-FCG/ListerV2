"""Refresh utility: find stale wardrobe items and convert them to fresh Lister drafts."""

import json
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException

from app.db import get_connection
from app.schemas import ListingOut, PhotoRef

router = APIRouter(prefix="/refresh", tags=["refresh"])


def _row_to_listing_out(row) -> ListingOut:
    data = dict(row)
    data["photo_items"] = [PhotoRef(**p) for p in json.loads(data.pop("photo_items") or "[]")]
    data["thumbnail_urls"] = json.loads(data.pop("thumbnail_urls") or "[]")
    return ListingOut(**data)


@router.get("/stale", response_model=list[dict])
def list_stale_items(min_age_days: int = 14):
    """Return vinted_items older than min_age_days days that haven't been imported.

    These are candidates for refresh (delete old listing, relist as fresh draft).
    """
    cutoff = datetime.now() - timedelta(days=min_age_days)

    with get_connection() as conn:
        rows = conn.execute(
            """SELECT id, url, title, price, photo_urls, scraped_at
               FROM vinted_items
               WHERE imported_listing_id IS NULL
                 AND scraped_at <= ?
               ORDER BY scraped_at ASC""",
            (cutoff.isoformat(),),
        ).fetchall()

    results = []
    for row in rows:
        data = dict(row)
        if isinstance(data.get("photo_urls"), str):
            try:
                data["photo_urls"] = json.loads(data["photo_urls"])
            except (json.JSONDecodeError, TypeError):
                data["photo_urls"] = []
        results.append(data)

    return results


@router.post("/backup")
def backup_stale_items(min_age_days: int = 14):
    """Backup all stale items into the listings table as draft_pending drafts.

    This is the SAFETY NET — no Vinted changes, just creates fresh drafts in Lister
    that can be reviewed and pushed when ready.
    """
    cutoff = datetime.now() - timedelta(days=min_age_days)

    imported_ids: list[int] = []

    with get_connection() as conn:
        stale_rows = conn.execute(
            """SELECT id, url, title, price, photo_urls, scraped_at
               FROM vinted_items
               WHERE imported_listing_id IS NULL
                 AND scraped_at <= ?
               ORDER BY scraped_at ASC""",
            (cutoff.isoformat(),),
        ).fetchall()

        for row in stale_rows:
            data = dict(row)
            # Parse photo_urls from JSON string if needed
            photos_raw = data.get("photo_urls") or "[]"
            if isinstance(photos_raw, str):
                try:
                    photo_list = json.loads(photos_raw)
                except (json.JSONDecodeError, TypeError):
                    photo_list = []
            else:
                photo_list = photos_raw

            # Convert stored photo URLs to PhotoRef objects
            photo_items_json = json.dumps([
                {"source": "url", "ref": url} for url in photo_list if url
            ])
            thumbnail_urls_json = json.dumps(photo_list)

            # Create a new draft_pending listing in Lister (description will be empty — Ollama generates it on demand)
            cur = conn.execute(
                """INSERT INTO listings
                   (photo_items, thumbnail_urls, platform, title, description, price,
                    condition, tags, status, created_at, updated_at)
                  VALUES (?, ?, 'vinted', ?, '', NULL, NULL, NULL, 'draft_pending', ?, ?)""",
                (
                    photo_items_json,
                    thumbnail_urls_json,
                    data["title"],
                    datetime.now().isoformat(),  # created_at
                    datetime.now().isoformat(),  # updated_at
                ),
            )
            imported_ids.append(cur.lastrowid)

        return {
            "status": "ok",
            "backed_up_count": len(imported_ids),
            "listing_ids": imported_ids,
            "message": f"{len(imported_ids)} stale item(s) saved as draft_pending drafts. Review at /listings and push when ready.",
        }


@router.post("/delete-stale")
def delete_stale_items(min_age_days: int = 14):
    """Delete stale items from vinted_items table AFTER they've been backed up.

    WARNING: This removes the scraped reference data. Only call after /backup.
    """
    cutoff = datetime.now() - timedelta(days=min_age_days)

    with get_connection() as conn:
        # First check if there's anything to delete
        count_row = conn.execute(
            """SELECT COUNT(*) as cnt FROM vinted_items
               WHERE imported_listing_id IS NULL
                 AND scraped_at <= ?""",
            (cutoff.isoformat(),),
        ).fetchone()

        if count_row["cnt"] == 0:
            raise HTTPException(status_code=400, detail="No stale items found to delete")

        # Delete the stale items
        conn.execute(
            """DELETE FROM vinted_items
               WHERE imported_listing_id IS NULL
                 AND scraped_at <= ?""",
            (cutoff.isoformat(),),
        )

    return {
        "status": "ok",
        "deleted_count": count_row["cnt"],
        "message": f"{count_row['cnt']} stale item(s) deleted from vinted_items table.",
    }


@router.get("/summary")
def refresh_summary():
    """Quick summary of current wardrobe state and what would be affected by refresh."""
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) as cnt FROM vinted_items").fetchone()["cnt"]

        stale = conn.execute(
            """SELECT COUNT(*) as cnt FROM vinted_items
               WHERE imported_listing_id IS NULL
                 AND scraped_at <= datetime('now', '-14 days')""",
        ).fetchone()["cnt"]

        already_imported = conn.execute(
            "SELECT COUNT(*) as cnt FROM vinted_items WHERE imported_listing_id IS NOT NULL"
        ).fetchone()["cnt"]

        draft_pending = conn.execute(
            """SELECT COUNT(*) as cnt FROM listings
               WHERE status = 'draft_pending'""",
        ).fetchone()["cnt"]

        return {
            "total_vinted_items": total,
            "stale_candidates": stale,
            "already_imported_to_listings": already_imported,
            "current_draft_pending_count": draft_pending,
            "safe_to_refresh": stale > 0 and draft_pending == 0,
        }
