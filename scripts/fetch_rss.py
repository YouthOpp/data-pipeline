"""
fetch_rss.py — Download RSS/Atom feeds listed in data/sources/sources.json
and save raw snapshots under data/raw/<source>/.

Usage:
    python scripts/fetch_rss.py
"""

import json
import os
import sys

import requests

# ---------------------------------------------------------------------------
# Resolve repo root relative to this script so the script works from any CWD
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

sys.path.insert(0, SCRIPT_DIR)
from utils import safe_mkdir, today_str  # noqa: E402

SOURCES_FILE = os.path.join(REPO_ROOT, "data", "sources", "sources.json")
RAW_DIR = os.path.join(REPO_ROOT, "data", "raw")

REQUEST_TIMEOUT = 30  # seconds


def load_sources() -> list[dict]:
    with open(SOURCES_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def fetch_feed(source_cfg: dict) -> bytes:
    """Download the feed and return raw bytes."""
    url = source_cfg["source_url"]
    print(f"  → Fetching {url} …")
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.content


def save_raw(source: str, content: bytes) -> tuple[str, str]:
    """
    Save *content* to:
      data/raw/<source>/<today>.xml
      data/raw/<source>/latest.xml

    Returns (daily_path, latest_path).
    """
    dest_dir = os.path.join(RAW_DIR, source)
    safe_mkdir(dest_dir)

    today = today_str()
    daily_path = os.path.join(dest_dir, f"{today}.xml")
    latest_path = os.path.join(dest_dir, "latest.xml")

    for path in (daily_path, latest_path):
        with open(path, "wb") as fh:
            fh.write(content)

    return daily_path, latest_path


def main() -> None:
    sources = load_sources()
    enabled = [s for s in sources if s.get("enabled", False)]

    print(f"[fetch_rss] {len(enabled)} enabled source(s) found.")

    success, failed = 0, 0
    for cfg in enabled:
        source = cfg["source"]
        print(f"\n[{source}]")
        try:
            content = fetch_feed(cfg)
            daily, latest = save_raw(source, content)
            size_kb = len(content) / 1024
            print(f"  ✓ Saved {size_kb:.1f} KB → {daily}")
            print(f"  ✓ Updated {latest}")
            success += 1
        except requests.RequestException as exc:
            print(f"  ✗ HTTP error: {exc}", file=sys.stderr)
            failed += 1
        except OSError as exc:
            print(f"  ✗ File error: {exc}", file=sys.stderr)
            failed += 1

    print(f"\n[fetch_rss] Done. {success} succeeded, {failed} failed.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
