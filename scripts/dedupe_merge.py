"""
dedupe_merge.py — Combine all normalized JSONL files, deduplicate by `id`
(last-write wins), sort by published_at descending (nulls last), and write
the final dataset to data/latest/.

Usage:
    python scripts/dedupe_merge.py
"""

import glob
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)

sys.path.insert(0, SCRIPT_DIR)
from utils import now_iso, read_jsonl, write_json, write_jsonl  # noqa: E402

NORM_DIR = os.path.join(REPO_ROOT, "data", "normalized", "opportunities")
LATEST_DIR = os.path.join(REPO_ROOT, "data", "latest")
LATEST_JSON = os.path.join(LATEST_DIR, "opportunities.json")
LATEST_JSONL = os.path.join(LATEST_DIR, "opportunities.jsonl")


def _sort_key(record: dict):
    """Sort key: published_at ascending means we put nulls last when reversed."""
    pub = record.get("published_at")
    # Use a very old date for nulls so they sort after real dates (when desc)
    return pub if pub else "0000-00-00T00:00:00Z"


def main() -> None:
    # Collect all .jsonl files sorted by name (chronological)
    pattern = os.path.join(NORM_DIR, "*.jsonl")
    jsonl_files = sorted(glob.glob(pattern))

    print(f"[dedupe_merge] Found {len(jsonl_files)} normalized file(s).")

    # To extend: change `jsonl_files` to only the last N days:
    #   from datetime import datetime, timedelta, timezone
    #   cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    #   jsonl_files = [f for f in jsonl_files if os.path.basename(f) >= cutoff + ".jsonl"]

    merged: dict[str, dict] = {}
    total_read = 0
    for path in jsonl_files:
        try:
            for record in read_jsonl(path):
                record_id = record.get("id")
                if record_id:
                    merged[record_id] = record  # last-write wins
                    total_read += 1
        except OSError as exc:
            print(f"  ⚠ Could not read {path}: {exc}", file=sys.stderr)

    print(f"[dedupe_merge] Read {total_read} raw record(s), {len(merged)} unique after dedup.")

    # Sort: published_at descending, nulls last
    records = sorted(merged.values(), key=_sort_key, reverse=True)

    write_json(records, LATEST_JSON)
    write_jsonl(records, LATEST_JSONL)

    now = now_iso()
    print(f"[dedupe_merge] Wrote {len(records)} record(s) → {LATEST_JSON}")
    print(f"[dedupe_merge] Wrote {len(records)} record(s) → {LATEST_JSONL}")
    print(f"[dedupe_merge] Done at {now}.")


if __name__ == "__main__":
    main()
