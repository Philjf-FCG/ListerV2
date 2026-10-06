"""Tests for wardrobe rotation (app/routers/rotate.py) and the export-date plumbing.

Everything runs against a throwaway SQLite file with Ollama and photo lookups stubbed,
so it never touches the real lister.db or the network.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import app.db as db
from app.routers import rotate, sync
from app.vinted_export_parser import parse_export_date, parse_vinted_export

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
FIRST_ID = 9_000_000_000


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", lambda: str(tmp_path / "test.db"))
    db.init_db()
    monkeypatch.setattr(rotate, "_BACKUP_DIR", tmp_path / "backups")

    def fake_photos(url):
        vinted_id = sync._vinted_item_id(url)
        folder = tmp_path / "src" / vinted_id
        folder.mkdir(parents=True, exist_ok=True)
        photo = folder / "a.webp"
        Image.new("RGB", (40, 40), "red").save(photo, "WEBP")
        return [photo]

    copy_calls: list[dict] = []

    def fake_copy(**kwargs):
        copy_calls.append(kwargs)
        return {"title": "Fresh title", "description": "fresh", "tags": "t", "suggested_price_gbp": "9"}

    monkeypatch.setattr(sync, "get_local_vinted_photos", fake_photos)
    monkeypatch.setattr(rotate.local, "ensure_thumbnail", lambda p: Path(f"thumb_{p.parent.name}.jpg"))
    monkeypatch.setattr(rotate, "generate_listing_copy", fake_copy)

    app = FastAPI()
    app.include_router(rotate.router)
    app.include_router(sync.router)
    return TestClient(app), copy_calls


def _scan(client, n=30):
    items = [
        {"url": f"https://www.vinted.co.uk/items/{FIRST_ID + i}-slug", "title": f"item {i}", "price": "5"}
        for i in range(n)
    ]
    assert client.post("/rotate/scan", json=items).status_code == 200


def _add_export_dates(n_dated=28):
    with db.get_connection() as conn:
        for i in range(n_dated):
            conn.execute(
                "INSERT INTO vinted_export_items VALUES (?,?,?,?,?,?,?)",
                (str(FIRST_ID + i), (BASE + timedelta(days=i)).isoformat(), f"desc {i}", "B", "M", "Very good", "Red"),
            )


def test_parse_export_date_converts_to_utc():
    assert parse_export_date("2026-08-28 20:30:14 +0100") == "2026-08-28T19:30:14+00:00"
    assert parse_export_date("not a date") is None


def test_status_ranks_by_real_upload_date_and_ignores_stale_scans(env):
    client, _ = env
    _scan(client)
    _add_export_dates()
    with db.get_connection() as conn:  # an item from an old scan must not count as live
        conn.execute(
            "INSERT INTO vinted_items (url, title, last_seen_at) VALUES "
            "('https://www.vinted.co.uk/items/9000000099-x', 'gone', '2020-01-01T00:00:00+00:00')"
        )
    status = client.get("/rotate/status").json()
    assert status["live_count"] == 30
    assert status["undated_count"] == 2
    assert [o["vinted_id"] for o in status["oldest"]] == [str(FIRST_ID + i) for i in range(20)]


def test_plan_picks_five_of_the_oldest_twenty_and_creates_drafts(env):
    client, copy_calls = env
    _scan(client)
    _add_export_dates()
    result = client.post("/rotate/plan?seed=1").json()

    assert len(result["picked"]) == 5
    assert all(int(p["vinted_id"]) < FIRST_ID + 20 for p in result["picked"])
    assert all("Previous description" in c["item_hint"] and "Brand: B" in c["item_hint"] for c in copy_calls)

    backup = rotate._BACKUP_DIR / result["picked"][0]["vinted_id"]
    assert Image.open(backup / "photo_0.jpg").format == "JPEG"
    assert json.loads((backup / "original.json").read_text())["brand"] == "B"

    with db.get_connection() as conn:
        rows = conn.execute("SELECT * FROM listings").fetchall()
    assert len(rows) == 5
    assert all(r["status"] == "draft_pending" and r["platform"] == "vinted" for r in rows)
    assert all(r["price"] == "5" and r["source_vinted_item_id"] for r in rows)  # real price kept


def test_plan_is_deterministic_for_a_seed(env):
    client, _ = env
    _scan(client)
    _add_export_dates()
    first = [p["vinted_id"] for p in client.post("/rotate/plan?seed=7").json()["picked"]]
    with db.get_connection() as conn:
        conn.execute("DELETE FROM listings")
    second = [p["vinted_id"] for p in client.post("/rotate/plan?seed=7").json()["picked"]]
    assert first == second


def test_second_plan_blocked_until_originals_confirmed_deleted(env):
    client, _ = env
    _scan(client)
    _add_export_dates()
    picked = client.post("/rotate/plan?seed=1").json()["picked"]
    assert client.post("/rotate/plan").status_code == 409

    first = picked[0]["vinted_item_id"]
    assert client.post(f"/rotate/{first}/deleted").status_code == 200
    assert client.post(f"/rotate/{first}/deleted").status_code == 404  # can't confirm twice

    status = client.get("/rotate/status").json()
    assert len(status["open_rotations"]) == 4
    assert status["live_count"] == 29  # the deleted original is no longer live

    # Forcing another round never re-picks anything still in progress
    in_progress = {r["vinted_item_id"] for r in status["open_rotations"]}
    again = {p["vinted_item_id"] for p in client.post("/rotate/plan?force=true&seed=2").json()["picked"]}
    assert len(again) == 5 and not (again & in_progress) and first not in again


def test_plan_refuses_a_stale_scan(env):
    client, _ = env
    _scan(client)
    _add_export_dates()
    with db.get_connection() as conn:
        conn.execute("UPDATE vinted_items SET last_seen_at = '2020-01-01T00:00:00+00:00'")
    assert client.post("/rotate/plan").status_code == 409


def test_plan_without_any_export_dates_explains_why(env):
    client, _ = env
    _scan(client)
    response = client.post("/rotate/plan")
    assert response.status_code == 400
    assert "export" in response.json()["detail"]


def test_item_without_photos_is_skipped_not_fatal(env, monkeypatch):
    client, _ = env
    _scan(client)
    _add_export_dates()
    monkeypatch.setattr(sync, "get_local_vinted_photos", lambda url: [])
    result = client.post("/rotate/plan").json()
    assert result["picked"] == []
    assert result["skipped"] and all(s["reason"] == "no photos available" for s in result["skipped"])


EXPORT_HTML = """
<html><body>
<div class="cell"><div class="cell-header">Listings</div></div>
<div class="cell" itemscope>
  <div class="cell-header" itemprop="title">Test jacket</div>
  <span itemprop="description">A jacket.</span>
  <span itemprop="brand">Acme</span><span itemprop="size">L</span>
  <span itemprop="status">Very good</span><span itemprop="color">Blue</span>
  <span itemprop="created_at">2026-08-28 20:30:14 +0100</span>
  <span itemprop="order_value">7.0 GBP</span>
  <div class="cell-images"><div><a href="photos/9000000001/p.webp"><img itemprop="item_photo"></a></div></div>
</div>
</body></html>
"""


def test_export_parser_reads_real_upload_date_and_details(tmp_path):
    export = tmp_path / "index.html"
    export.write_text(EXPORT_HTML, encoding="utf-8")
    item = parse_vinted_export(export)[0]
    assert item.listed_at == "2026-08-28T19:30:14+00:00"
    assert (item.brand, item.size, item.condition, item.colour) == ("Acme", "L", "Very good", "Blue")


def test_export_parser_keeps_all_digits_of_11_digit_item_ids(tmp_path):
    export = tmp_path / "index.html"
    export.write_text(EXPORT_HTML.replace("9000000001", "10030496030"), encoding="utf-8")
    assert parse_vinted_export(export)[0].url == "https://www.vinted.co.uk/items/10030496030"


def test_import_dates_replaces_stale_rows(env, tmp_path):
    client, _ = env
    with db.get_connection() as conn:
        conn.execute("INSERT INTO vinted_export_items (vinted_id, listed_at) VALUES ('1003049603', 'stale')")
    export = tmp_path / "index.html"
    export.write_text(EXPORT_HTML, encoding="utf-8")
    client.post("/rotate/import-dates", params={"export_path": str(export)})
    with db.get_connection() as conn:
        ids = [r[0] for r in conn.execute("SELECT vinted_id FROM vinted_export_items")]
    assert ids == ["9000000001"]


def test_import_dates_only_touches_the_dates_table(env, tmp_path):
    client, _ = env
    url = "https://www.vinted.co.uk/items/9000000001-jacket"
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO vinted_items (url, title, imported_listing_id, last_seen_at) VALUES (?, 'x', 42, 'SCAN')",
            (url,),
        )
    export = tmp_path / "index.html"
    export.write_text(EXPORT_HTML, encoding="utf-8")

    result = client.post("/rotate/import-dates", params={"export_path": str(export)}).json()
    assert result == {"status": "ok", "stored": 1, "live_items": 1, "live_items_with_dates": 1}

    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM vinted_items WHERE url = ?", (url,)).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM vinted_items").fetchone()[0]
    assert row["imported_listing_id"] == 42 and row["last_seen_at"] == "SCAN" and count == 1
    assert client.post("/rotate/import-dates", params={"export_path": str(tmp_path / "nope.html")}).status_code == 404


def test_export_import_stores_dates_and_keeps_scan_state(env, tmp_path):
    client, _ = env
    url = "https://www.vinted.co.uk/items/9000000001"
    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO vinted_items (url, title, last_seen_at, rotated_at) VALUES (?, 'x', 'SCAN', 'ROT')", (url,)
        )
    export = tmp_path / "index.html"
    export.write_text(EXPORT_HTML, encoding="utf-8")

    assert client.post("/sync/import-vinted-export", params={"export_path": str(export)}).status_code == 200

    with db.get_connection() as conn:
        meta = conn.execute("SELECT * FROM vinted_export_items WHERE vinted_id = '9000000001'").fetchone()
        row = conn.execute("SELECT * FROM vinted_items WHERE url = ?", (url,)).fetchone()
    assert meta["listed_at"] == "2026-08-28T19:30:14+00:00" and meta["brand"] == "Acme"
    assert row["last_seen_at"] == "SCAN" and row["rotated_at"] == "ROT"  # re-import must not wipe these
