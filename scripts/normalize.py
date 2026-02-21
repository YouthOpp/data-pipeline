"""
normalize.py — Parse today's raw XML feed for each enabled source and write
normalized Opportunity records to data/normalized/opportunities/<today>.jsonl.

Usage:
    python scripts/normalize.py
"""

import html
import json
import os
import sys

import feedparser  # type: ignore

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

sys.path.insert(0, SCRIPT_DIR)
from utils import (  # noqa: E402
    now_iso,
    parse_date_to_iso,
    safe_mkdir,
    sha1_id,
    today_str,
    write_jsonl,
)

SOURCES_FILE = os.path.join(REPO_ROOT, "data", "sources", "sources.json")
RAW_DIR = os.path.join(REPO_ROOT, "data", "raw")
NORM_DIR = os.path.join(REPO_ROOT, "data", "normalized", "opportunities")


def load_sources() -> list[dict]:
    with open(SOURCES_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def find_raw_file(source: str, today: str) -> str | None:
    """Return the path to today's raw XML, falling back to latest.xml."""
    source_dir = os.path.join(RAW_DIR, source)
    daily = os.path.join(source_dir, f"{today}.xml")
    if os.path.exists(daily):
        return daily
    latest = os.path.join(source_dir, "latest.xml")
    if os.path.exists(latest):
        print(f"  ⚠ No daily file for {today}, falling back to latest.xml")
        return latest
    return None


def clean_html(text: str | None) -> str | None:
    """Strip HTML tags from *text* and unescape HTML entities."""
    if not text:
        return None
    import re

    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return " ".join(text.split()) or None


def parse_entry(entry: object, cfg: dict, now: str) -> dict:
    """Convert a feedparser entry into a normalized Opportunity dict."""
    source = cfg["source"]
    url = getattr(entry, "link", "") or ""
    title = getattr(entry, "title", "") or ""

    # Summary: prefer summary, fallback to description content
    summary_raw = getattr(entry, "summary", None) or getattr(
        entry, "description", None
    )
    # Some feeds store content in entry.content list
    if not summary_raw:
        content_list = getattr(entry, "content", [])
        if content_list:
            summary_raw = content_list[0].get("value", "")
    summary = clean_html(summary_raw)

    published_at = parse_date_to_iso(getattr(entry, "published_parsed", None))
    if published_at is None:
        published_at = parse_date_to_iso(getattr(entry, "updated_parsed", None))

    tags = list(cfg.get("default_tags", []))
    # Merge feed-level tags/categories
    for tag_obj in getattr(entry, "tags", []):
        term = getattr(tag_obj, "term", None)
        if term and term not in tags:
            tags.append(term)

    return {
        "id": sha1_id(source, url),
        "title": title,
        "url": url,
        "source": source,
        "source_url": cfg["source_url"],
        "published_at": published_at,
        "summary": summary,
        "tags": tags,
        "location": None,
        "deadline": None,
        "language": cfg.get("language"),
        "created_at": now,
        "updated_at": now,
    }


def normalize_source(cfg: dict, today: str, now: str) -> list[dict]:
    """Parse the raw feed for *cfg* and return a list of Opportunity dicts."""
    source = cfg["source"]
    raw_path = find_raw_file(source, today)
    if raw_path is None:
        print(f"  ✗ No raw file found for source '{source}' — skipping.")
        return []

    print(f"  → Parsing {raw_path} …")
    feed = feedparser.parse(raw_path)

    if feed.bozo and not feed.entries:
        print(f"  ✗ Feed parse error: {feed.bozo_exception}")
        return []

    records = []
    for entry in feed.entries:
        try:
            record = parse_entry(entry, cfg, now)
            if record["url"]:  # skip entries without a URL
                records.append(record)
        except Exception as exc:  # noqa: BLE001
            entry_url = getattr(entry, "link", "<no url>")
            entry_title = getattr(entry, "title", "<no title>")
            print(f"  ⚠ Skipping entry '{entry_title}' ({entry_url}) due to error: {exc}")

    print(f"  ✓ Parsed {len(records)} record(s) from {source}")
    return records


def main() -> None:
    sources = load_sources()
    enabled = [s for s in sources if s.get("enabled", False)]
    today = today_str()
    now = now_iso()

    print(f"[normalize] Date: {today}  |  {len(enabled)} enabled source(s)")

    all_records: list[dict] = []
    for cfg in enabled:
        print(f"\n[{cfg['source']}]")
        records = normalize_source(cfg, today, now)
        all_records.extend(records)

    if all_records:
        safe_mkdir(NORM_DIR)
        out_path = os.path.join(NORM_DIR, f"{today}.jsonl")
        write_jsonl(all_records, out_path)
        print(f"\n[normalize] Wrote {len(all_records)} record(s) → {out_path}")
    else:
        print("\n[normalize] No records to write.")


if __name__ == "__main__":
    main()
