# data-pipeline

Automated data pipeline that fetches opportunity feeds from multiple sources,
normalizes items into a consistent JSON dataset, classifies them by
date/source/category, and publishes the result to an external data repository.

Each source runs as a **separate pipeline** with staggered schedules to avoid
GitHub rate limiting. Raw feed data is treated as temporary and is **not**
persisted in Git — only the normalized, classified output is stored.

---

## Architecture

```
                          ┌─────────────────────┐
                          │   Source Workflows   │
                          │  (staggered crons)   │
                          └──┬──────┬──────┬─────┘
                             │      │      │
                    ┌────────┘      │      └────────┐
                    ▼               ▼               ▼
           source-A.yml     source-B.yml     source-C.yml
           (every 6h @:00)  (every 6h @:15)  (every 6h @:30)
                    │               │               │
                    ▼               ▼               ▼
              fetch → normalize → classify (per source)
                    │               │               │
                    └───────┬───────┘───────┬───────┘
                            ▼               ▼
                    data/classified/    data/latest/
                            │               │
                            ▼               ▼
                    ┌──────────────────────────┐
                    │  Merge & Sync Workflow    │
                    │  (daily — pushes to       │
                    │   external data repo)     │
                    └──────────────────────────┘
```

---

## Folder structure

```
data-pipeline/
  data/
    sources/
      sources.json              ← list of feed sources (edit this to add new feeds)
    classified/                 ← normalized + classified output
      by-source/
        <source>/
          YYYY-MM-DD.jsonl      ← records from this source on this date
      by-category/
        <category>/
          YYYY-MM-DD.jsonl      ← records in this category on this date
      by-date/
        YYYY-MM-DD/
          <source>.jsonl        ← records from each source on this date
    latest/
      opportunities.json        ← ✅ final merged dataset (JSON array, deduped, sorted)
      opportunities.jsonl       ← same data in JSON Lines format
  scripts/
    utils.js                    ← shared helpers (legacy)
    pipeline_utils.js           ← shared helpers + classification logic
    classify.js                 ← re-classifies historical normalized data
    dedupe_merge.js             ← merges classified data → final dataset
    push_data.sh                ← pushes data to external data repo
    fetch_rss.js                ← legacy: downloads all raw feeds
    normalize.js                ← legacy: parses all raw XML → normalized records
    sources/
      opportunitiesforyouth.js  ← source-specific pipeline (fetch → normalize → classify)
  .github/
    workflows/
      source-opportunitiesforyouth.yml  ← per-source workflow (staggered cron)
      pipeline.yml                      ← merge & sync workflow (daily)
  package.json
  .gitignore
  README.md
```

---

## Requirements

- **Node.js** 16.x or higher
- **npm** 7.x or higher

Dependencies (installed via `npm install`):
- `axios` - HTTP client for downloading feeds
- `rss-parser` - RSS/Atom feed parser

---

## Running locally

```bash
# 1. Install dependencies
npm install

# 2. Run a specific source pipeline (fetch + normalize + classify)
npm run source:opportunitiesforyouth

# 3. Build the deduplicated latest dataset
npm run merge

# Or run the full legacy pipeline:
npm run pipeline
```

After running a source pipeline, you will find:

- `data/classified/by-source/<source>/<today>.jsonl`
- `data/classified/by-category/<category>/<today>.jsonl`
- `data/classified/by-date/<today>/<source>.jsonl`
- `data/latest/opportunities.json` and `opportunities.jsonl`

---

## Adding a new source

1. Create a new source script at `scripts/sources/<source-name>.js`.
   Use `scripts/sources/opportunitiesforyouth.js` as a template.

2. Add the source to `data/sources/sources.json`:

```json
{
  "source": "my-source-name",
  "source_url": "https://example.com/feed/",
  "enabled": true,
  "language": "en",
  "default_tags": ["youth", "opportunity"],
  "categories": ["education", "fellowship"]
}
```

3. Create a GitHub Actions workflow at `.github/workflows/source-<source-name>.yml`.
   **Stagger the cron schedule** so it doesn't overlap with existing sources:

```yaml
on:
  schedule:
    # Offset by 1 hour from previous source
    - cron: "0 1,7,13,19 * * *"
```

4. Add an npm script in `package.json`:

```json
"source:<source-name>": "node scripts/sources/<source-name>.js"
```

5. Push to trigger the new workflow, or run locally:

```bash
npm run source:<source-name>
```

---

## Pushing data to an external repository

The pipeline is designed to push normalized data to a separate data repository
(e.g., `YouthOpp/data`). To enable this:

1. Create a GitHub personal access token (PAT) with `repo` scope.
2. Add it as a repository secret named `DATA_REPO_URL`:
   ```
   https://x-access-token:<PAT>@github.com/YouthOpp/data.git
   ```
3. The per-source and merge workflows will automatically push classified and
   latest data to the external repo.

---

## Classification

Data is automatically classified into three dimensions:

| Dimension     | Path                                          | Description                          |
|---------------|-----------------------------------------------|--------------------------------------|
| **By source** | `data/classified/by-source/<source>/`         | All records from a given source      |
| **By category** | `data/classified/by-category/<category>/`   | Records matching a tag/category      |
| **By date**   | `data/classified/by-date/YYYY-MM-DD/`         | All records for a given date         |

Categories are derived from each record's `tags` array. Records without tags
are placed in the `uncategorized` bucket.

---

## How duplicate records are handled

Each record is assigned a **deterministic ID** computed as the SHA-1 hash of
`"<source>|<url>"`. When multiple pipeline runs capture the same opportunity,
`dedupe_merge.js` keeps only the most recently seen version (last-write wins)
so the `latest` files never contain duplicates.

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

---

## Troubleshooting

| Problem | Likely cause | Fix |
|---|---|---|
| Source workflow fails with "HTTP error" | Source site is temporarily down | Wait for next scheduled run or trigger `workflow_dispatch` |
| `published_at` is `null` for some entries | Feed does not include a date | Safe to ignore; records sort to the end |
| No records in classified output | Feed returned 0 entries or bad XML | Check source script logs |
| `push_data.sh` fails | `DATA_REPO_URL` secret not configured | See "Pushing data to an external repository" section |