# data/raw

This folder stores **raw feed snapshots** downloaded from each source.

## Structure

```
data/raw/
  <source>/          # one subfolder per source (matches "source" key in sources.json)
    YYYY-MM-DD.xml   # daily snapshot of the raw feed
    latest.xml       # overwritten each run with the most recent download
```

## Notes

- Files are **never deleted automatically** — full history is preserved in Git.
- `latest.xml` is always the most recent successful download for that source.
- To add a new source, add an entry to `data/sources/sources.json` and re-run
  `python scripts/fetch_rss.py`.
