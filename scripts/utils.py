"""Shared helper utilities for the data pipeline."""

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Iterator


def sha1_id(source: str, url: str) -> str:
    """Return a deterministic SHA-1 hex digest ID for a given source + url pair."""
    raw = f"{source}|{url}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with timezone info."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_str() -> str:
    """Return today's UTC date as YYYY-MM-DD."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def safe_mkdir(path: str) -> None:
    """Create a directory (and parents) if it does not already exist."""
    os.makedirs(path, exist_ok=True)


def read_jsonl(path: str) -> Iterator[dict[str, Any]]:
    """Yield parsed JSON objects from a JSON Lines file, skipping blank lines."""
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def write_jsonl(records: list[dict[str, Any]], path: str) -> None:
    """Write a list of dicts to a JSON Lines file (one JSON object per line)."""
    parent = os.path.dirname(path)
    if parent:
        safe_mkdir(parent)
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_json(data: Any, path: str) -> None:
    """Write *data* to a JSON file with 2-space indentation."""
    parent = os.path.dirname(path)
    if parent:
        safe_mkdir(parent)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def parse_date_to_iso(value: Any) -> str | None:
    """
    Try to parse *value* (a time.struct_time tuple, a string, or None) into an
    ISO 8601 UTC string (YYYY-MM-DDTHH:MM:SSZ).

    Returns ``None`` if the value cannot be parsed.
    """
    if value is None:
        return None

    # feedparser returns a 9-tuple (time.struct_time) for parsed dates
    if isinstance(value, (list, tuple)) and len(value) >= 6:
        try:
            dt = datetime(*value[:6], tzinfo=timezone.utc)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            return None

    if isinstance(value, str) and value.strip():
        try:
            from dateutil import parser as dateutil_parser  # type: ignore

            dt = dateutil_parser.parse(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            return None

    return None
