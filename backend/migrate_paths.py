"""Rewrite Windows file paths stored in a Lister database so it works on another machine.

The database records local photos as plain paths (listings.photo_items, source "local") and as
file:// URIs (vinted_items.photo_urls from the Vinted export importer). Moving the app to
Linux leaves every one of those pointing at C:\\... so photos 404. This remaps them.

  python migrate_paths.py --db lister.db \\
      --map "C:\\Vinted\\Data=/srv/lister/phil/vinted" \\
      --map "C:\\DevProjects\\lister2\\backend=/srv/lister/phil/backend" [--dry-run]

Matching is case-insensitive and ignores slash direction. Anything that still looks like a
Windows path afterwards is reported so nothing is silently left broken.
"""
import argparse
import json
import re
import sqlite3
import sys

_WINDOWS_PATH = re.compile(r"^(file://)?/?[A-Za-z]:[\\/]")


def _norm(path: str) -> str:
    return path.replace("\\", "/").rstrip("/")


def parse_mappings(specs: list[str]) -> list[tuple[str, str]]:
    mappings = []
    for spec in specs:
        src, sep, dst = spec.partition("=")
        if not sep or not src or not dst:
            raise ValueError(f"bad --map {spec!r}: expected SRC=DST")
        mappings.append((_norm(src), _norm(dst)))
    # longest source first so a nested prefix wins over its parent
    return sorted(mappings, key=lambda m: len(m[0]), reverse=True)


def convert(value: str, mappings: list[tuple[str, str]]) -> str | None:
    """Return the remapped value, or None if no mapping applies."""
    is_uri = value.lower().startswith("file://")
    path = value[len("file://"):] if is_uri else value
    path = _norm(path)
    if re.match(r"^/[A-Za-z]:/", path):
        path = path[1:]
    for src, dst in mappings:
        if path.lower() == src.lower() or path.lower().startswith(src.lower() + "/"):
            new_path = dst + path[len(src):]
            return f"file://{new_path}" if is_uri else new_path
    return None


def migrate(db_path: str, mappings: list[tuple[str, str]], dry_run: bool = False) -> dict:
    report = {"listing_refs_changed": 0, "vinted_urls_changed": 0, "unmapped": []}
    conn = sqlite3.connect(db_path)
    try:
        for row_id, raw in conn.execute("SELECT id, photo_items FROM listings").fetchall():
            items = json.loads(raw or "[]")
            changed = False
            for item in items:
                ref = item.get("ref", "")
                if item.get("source") == "local" or ref.lower().startswith("file://"):
                    new = convert(ref, mappings)
                    if new is not None and new != ref:
                        item["ref"], changed = new, True
                        report["listing_refs_changed"] += 1
                    elif new is None and _WINDOWS_PATH.match(ref):
                        report["unmapped"].append(f"listings#{row_id}: {ref}")
            if changed and not dry_run:
                conn.execute("UPDATE listings SET photo_items = ? WHERE id = ?", (json.dumps(items), row_id))

        for row_id, raw in conn.execute("SELECT id, photo_urls FROM vinted_items WHERE photo_urls IS NOT NULL").fetchall():
            urls = json.loads(raw or "[]")
            changed = False
            for i, url in enumerate(urls):
                if not url.lower().startswith("file://"):
                    continue  # http(s) CDN URLs are left alone
                new = convert(url, mappings)
                if new is not None and new != url:
                    urls[i], changed = new, True
                    report["vinted_urls_changed"] += 1
                elif new is None and _WINDOWS_PATH.match(url):
                    report["unmapped"].append(f"vinted_items#{row_id}: {url}")
            if changed and not dry_run:
                conn.execute("UPDATE vinted_items SET photo_urls = ? WHERE id = ?", (json.dumps(urls), row_id))
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True)
    parser.add_argument("--map", action="append", default=[], dest="maps", metavar="SRC=DST")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    report = migrate(args.db, parse_mappings(args.maps), args.dry_run)
    print(f"{'(dry run) ' if args.dry_run else ''}listing photo refs rewritten: {report['listing_refs_changed']}")
    print(f"{'(dry run) ' if args.dry_run else ''}vinted_items photo URLs rewritten: {report['vinted_urls_changed']}")
    for line in report["unmapped"]:
        print("UNMAPPED:", line)
    return 1 if report["unmapped"] else 0


if __name__ == "__main__":
    sys.exit(main())
