"""SQLite persistence layer. Kept intentionally simple (stdlib sqlite3, no ORM)."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import BACKEND_DIR, get_settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_items TEXT NOT NULL,       -- JSON list of {"source": "local"|"google_photos", "ref": "..."}
    thumbnail_urls TEXT,             -- JSON list of thumbnail URLs, same order as photo_items
    platform TEXT NOT NULL,          -- 'vinted' or 'ebay'
    title TEXT,
    description TEXT,
    price TEXT,
    condition TEXT,
    tags TEXT,
    status TEXT NOT NULL DEFAULT 'draft_pending',
        -- draft_pending -> reviewed_ready -> posted_as_draft -> failed
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS vinted_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,      -- Vinted item URL, used as the natural dedupe key
    title TEXT NOT NULL,
    price TEXT,
    photo_url TEXT,                -- public Vinted CDN URL, fetchable without auth
    imported_listing_id INTEGER,   -- set once turned into a Lister listing via /sync/import
    scraped_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _db_path() -> str:
    settings = get_settings()
    path = Path(settings.database_path)
    if not path.is_absolute():
        path = BACKEND_DIR / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def _migrate_legacy_single_photo_columns(conn: sqlite3.Connection) -> None:
    """Older versions of this app stored one photo per listing in source/photo_ref/
    thumbnail_ref NOT NULL columns. Adding the new columns isn't enough - those old
    NOT NULL constraints stick around and reject new-style inserts - so rebuild the
    table from scratch and copy the data across."""
    import json

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(listings)").fetchall()}
    if "source" not in columns or "photo_ref" not in columns:
        return  # already migrated, or a fresh DB that never had the legacy columns

    conn.execute(
        """CREATE TABLE listings_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            photo_items TEXT NOT NULL,
            thumbnail_urls TEXT,
            platform TEXT NOT NULL,
            title TEXT,
            description TEXT,
            price TEXT,
            condition TEXT,
            tags TEXT,
            status TEXT NOT NULL DEFAULT 'draft_pending',
            error TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    for row in conn.execute("SELECT * FROM listings").fetchall():
        data = dict(row)
        if data.get("photo_items"):
            photo_items = data["photo_items"]
            thumbnail_urls = data.get("thumbnail_urls") or "[]"
        else:
            photo_items = json.dumps([{"source": data["source"], "ref": data["photo_ref"]}])
            thumb = data.get("thumbnail_ref")
            thumbnail_urls = json.dumps([f"/photos/thumbnail?path={thumb}"] if thumb else [])
        conn.execute(
            """INSERT INTO listings_new
                (id, photo_items, thumbnail_urls, platform, title, description, price, condition,
                 tags, status, error, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data["id"], photo_items, thumbnail_urls, data["platform"], data.get("title"),
                data.get("description"), data.get("price"), data.get("condition"), data.get("tags"),
                data["status"], data.get("error"), data["created_at"], data["updated_at"],
            ),
        )
    conn.execute("DROP TABLE listings")
    conn.execute("ALTER TABLE listings_new RENAME TO listings")


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(_SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(listings)").fetchall()}
        if "photo_items" not in columns:
            conn.execute("ALTER TABLE listings ADD COLUMN photo_items TEXT")
        if "thumbnail_urls" not in columns:
            conn.execute("ALTER TABLE listings ADD COLUMN thumbnail_urls TEXT")
        _migrate_legacy_single_photo_columns(conn)


@contextmanager
def get_connection():
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
