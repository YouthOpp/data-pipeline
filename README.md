# data-pipeline

Automated data pipeline that fetches opportunity RSS/Atom feeds, normalizes the
items into a consistent JSON dataset, and publishes the result to
`data/latest/opportunities.json` for the YouthOpp website.

The pipeline runs every 6 hours via GitHub Actions and commits any new data
back to this repository automatically.

---

## Folder structure

```
data-pipeline/
  data/
    sources/
      sources.json          ← list of feed sources (edit this to add new feeds)
    raw/
      README.md
      <source>/
        YYYY-MM-DD.xml      ← daily raw feed snapshot
        latest.xml          ← overwritten each run
    normalized/
      README.md
      opportunities/
        YYYY-MM-DD.jsonl    ← normalized records for that day (JSON Lines)
    latest/
      README.md
      opportunities.json    ← ✅ WEBSITE READS THIS (JSON array, deduped, sorted)
      opportunities.jsonl   ← same data in JSON Lines format
  scripts/
    utils.py                ← shared helpers
    fetch_rss.py            ← downloads raw feeds
    normalize.py            ← parses XML → normalized records
    dedupe_merge.py         ← merges all days → final dataset
  .github/
    workflows/
      pipeline.yml          ← GitHub Actions workflow
  requirements.txt
  .gitignore
  README.md
```

---

## Running locally

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Download raw feeds
python scripts/fetch_rss.py

# 3. Normalize feed entries
python scripts/normalize.py

# 4. Build the deduplicated latest dataset
python scripts/dedupe_merge.py
```

After running, you will find:

- `data/raw/opportunitiesforyouth/<today>.xml` and `latest.xml`
- `data/normalized/opportunities/<today>.jsonl`
- `data/latest/opportunities.json` and `opportunities.jsonl`

---

## Adding a new feed

1. Open `data/sources/sources.json`.
2. Add a new entry following this template:

```json
{
  "source": "my-source-name",
  "source_url": "https://example.com/feed/",
  "enabled": true,
  "language": "en",
  "default_tags": ["youth", "opportunity"]
}
```

3. Re-run the three scripts (or push to trigger GitHub Actions).

- `source` must be a URL-safe slug (lowercase, hyphens only).
- Set `"enabled": false` to temporarily pause a feed without removing it.

---

## Where the website reads data

Point your website / frontend at:

```
data/latest/opportunities.json
```

Raw GitHub URL:

```
https://raw.githubusercontent.com/YouthOpp/data-pipeline/main/data/latest/opportunities.json
```

---

## How duplicate records are handled

Each record is assigned a **deterministic ID** computed as the SHA-1 hash of
`"<source>|<url>"`. When multiple pipeline runs capture the same opportunity,
`dedupe_merge.py` keeps only the most recently seen version (last-write wins)
so the `latest` files never contain duplicates.

---

## Troubleshooting

| Problem | Likely cause | Fix |
|---|---|---|
| `data/raw/.../YYYY-MM-DD.xml` is missing | Feed was unreachable at that time | Wait for the next run or trigger `workflow_dispatch` |
| `published_at` is `null` for some entries | Feed does not include a date | Safe to ignore; records sort to the end |
| No records in `opportunities.jsonl` | Feed returned 0 entries or bad XML | Check the raw XML file manually |
| GitHub Actions fails with "HTTP error" | Source site is temporarily down | The workflow exits with code 1; retry via `workflow_dispatch` |

---

## Data model

Each Opportunity record contains:

| Field | Type | Required | Description |
|---|---|---|---|
| `id` | string | ✅ | SHA-1 of `source + "\|" + url` |
| `title` | string | ✅ | |
| `url` | string | ✅ | |
| `source` | string | ✅ | e.g. `"opportunitiesforyouth"` |
| `source_url` | string | ✅ | Feed URL |
| `published_at` | ISO 8601 / null | | |
| `summary` | string / null | | Plain-text excerpt |
| `tags` | string array | | Feed categories + `default_tags` |
| `location` | string / null | | |
| `deadline` | ISO 8601 / null | | |
| `language` | string / null | | e.g. `"en"` |
| `created_at` | ISO 8601 | ✅ | When the record was first written |
| `updated_at` | ISO 8601 | ✅ | When the record was last updated |