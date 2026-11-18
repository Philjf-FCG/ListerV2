"""Tests for the pieces that make the app deployable on another machine/OS:
path migration, file:// handling, export-path defaults and the access password."""
import base64
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.db as db
from app import config
from app.main import app
from migrate_paths import convert, migrate, parse_mappings

MAPS = parse_mappings(
    [r"C:\Vinted\Data=/srv/lister/phil/vinted", r"C:\DevProjects\lister2\backend=/srv/lister/phil/backend"]
)


def test_convert_handles_every_form_the_app_stores():
    assert convert(r"C:\Vinted\Data\listings\photos\1\a.webp", MAPS) == "/srv/lister/phil/vinted/listings/photos/1/a.webp"
    assert convert("c:/vinted/data/listings/photos/1/a.webp", MAPS) == "/srv/lister/phil/vinted/listings/photos/1/a.webp"  # case + slashes
    assert convert(r"file://C:\Vinted\Data\listings/photos/1/a.webp", MAPS) == "file:///srv/lister/phil/vinted/listings/photos/1/a.webp"
    assert convert("file:///C:/Vinted/Data/x.webp", MAPS) == "file:///srv/lister/phil/vinted/x.webp"
    assert convert(r"C:\DevProjects\lister2\backend\rotation_backups\9\photo_0.jpg", MAPS) == "/srv/lister/phil/backend/rotation_backups/9/photo_0.jpg"


def test_convert_respects_directory_boundaries_and_ignores_unmapped():
    assert convert(r"C:\Vinted\DataExtra\x.jpg", MAPS) is None  # not inside C:\Vinted\Data
    assert convert(r"D:\Other\x.jpg", MAPS) is None


def test_bad_mapping_is_rejected():
    with pytest.raises(ValueError):
        parse_mappings(["no-equals-sign"])


def _make_db(tmp_path) -> str:
    path = str(tmp_path / "t.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE listings (id INTEGER PRIMARY KEY, photo_items TEXT);"
        "CREATE TABLE vinted_items (id INTEGER PRIMARY KEY, photo_urls TEXT);"
    )
    conn.execute("INSERT INTO listings VALUES (1, ?)", (json.dumps([
        {"source": "local", "ref": r"C:\Vinted\Data\listings\photos\1\a.webp"},
        {"source": "google_photos", "ref": "AbC123"},
        {"source": "url", "ref": "https://cdn.example/x.jpg"},
        {"source": "local", "ref": r"D:\elsewhere\b.jpg"},
    ]),))
    conn.execute("INSERT INTO vinted_items VALUES (1, ?)", (json.dumps(
        [r"file://C:\Vinted\Data\listings/photos/1/a.webp", "https://cdn.example/y.jpg"]),))
    conn.commit()
    conn.close()
    return path


def test_migrate_rewrites_only_local_paths_and_reports_unmapped(tmp_path):
    path = _make_db(tmp_path)
    report = migrate(path, MAPS)
    assert report["listing_refs_changed"] == 1 and report["vinted_urls_changed"] == 1
    assert report["unmapped"] == [r"listings#1: D:\elsewhere\b.jpg"]
    conn = sqlite3.connect(path)
    refs = [i["ref"] for i in json.loads(conn.execute("SELECT photo_items FROM listings").fetchone()[0])]
    urls = json.loads(conn.execute("SELECT photo_urls FROM vinted_items").fetchone()[0])
    assert refs[0] == "/srv/lister/phil/vinted/listings/photos/1/a.webp"
    assert refs[1:3] == ["AbC123", "https://cdn.example/x.jpg"]  # untouched
    assert urls == ["file:///srv/lister/phil/vinted/listings/photos/1/a.webp", "https://cdn.example/y.jpg"]


def test_dry_run_changes_nothing_and_migration_is_idempotent(tmp_path):
    path = _make_db(tmp_path)
    before = sqlite3.connect(path).execute("SELECT photo_items FROM listings").fetchone()[0]
    assert migrate(path, MAPS, dry_run=True)["listing_refs_changed"] == 1
    assert sqlite3.connect(path).execute("SELECT photo_items FROM listings").fetchone()[0] == before
    migrate(path, MAPS)
    assert migrate(path, MAPS)["listing_refs_changed"] == 0  # second run finds nothing left to do


def test_file_uri_to_path_keeps_the_leading_slash_on_linux_paths():
    assert config.file_uri_to_path("file:///srv/lister/a.webp") == Path("/srv/lister/a.webp")
    assert config.file_uri_to_path("file:///C:/Vinted/a.webp") == Path("C:/Vinted/a.webp")
    assert config.file_uri_to_path(r"file://C:\Vinted\a.webp") == Path(r"C:\Vinted\a.webp")
    assert config.file_uri_to_path("/plain/path.jpg") == Path("/plain/path.jpg")


def test_default_export_index_follows_vinted_photos_dir(monkeypatch):
    settings = config.get_settings()
    monkeypatch.setattr(settings, "vinted_photos_dir", "/srv/lister/phil/vinted/listings/photos")
    assert Path(config.default_vinted_export_index()) == Path("/srv/lister/phil/vinted/listings/index.html")
    monkeypatch.setattr(settings, "vinted_photos_dir", "")
    assert config.default_vinted_export_index().lower().endswith(r"listings\index.html")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_db_path", lambda: str(tmp_path / "t.db"))
    db.init_db()
    return TestClient(app)


def _basic(user: str, password: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_no_password_configured_means_open_for_local_dev(client):
    assert client.get("/listings").status_code == 200


def test_password_gate(client, monkeypatch):
    settings = config.get_settings()
    monkeypatch.setattr(settings, "access_password", "s3cret")
    monkeypatch.setattr(settings, "access_user", "lister")

    denied = client.get("/listings")
    assert denied.status_code == 401 and "Basic" in denied.headers["www-authenticate"]
    assert client.get("/listings", headers=_basic("lister", "wrong")).status_code == 401
    assert client.get("/listings", headers=_basic("someone", "s3cret")).status_code == 401
    assert client.get("/listings", headers={"Authorization": "Basic !!!notbase64"}).status_code == 401
    assert client.get("/listings", headers=_basic("lister", "s3cret")).status_code == 200
    assert client.get("/healthz").status_code == 200  # health check stays open


def test_cors_preflight_and_401_carry_cors_headers_for_the_extension(client, monkeypatch):
    monkeypatch.setattr(config.get_settings(), "access_password", "s3cret")
    origin = {"Origin": "chrome-extension://abc"}
    pre = client.options("/listings", headers={**origin, "Access-Control-Request-Method": "GET",
                                                "Access-Control-Request-Headers": "authorization"})
    assert pre.status_code == 200 and "authorization" in pre.headers["access-control-allow-headers"].lower()
    denied = client.get("/listings", headers=origin)
    assert denied.status_code == 401 and denied.headers["access-control-allow-origin"] == "*"
