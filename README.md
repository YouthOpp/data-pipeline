# data-pipeline

Automated pipeline for fetching, normalizing, and merging youth opportunity data from RSS feeds.

## Project Structure

```
data-pipeline/
  data/
    sources/
      sources.json          # RSS feed source configurations
    raw/
      <YYYY-MM-DD>/
        <source-id>.xml     # Raw RSS/XML fetched each day
    normalized/
      opportunities/
        <YYYY-MM-DD>.jsonl  # Normalized records per day
    latest/
      opportunities.json    # Deduplicated JSON array (latest)
      opportunities.jsonl   # Deduplicated JSONL (latest)
  scripts/
    fetch_rss.js            # Fetches RSS feeds → data/raw/
    normalize.js            # Parses XML → data/normalized/
    dedupe_merge.js         # Merges & dedupes → data/latest/
  .github/
    workflows/
      rss.yml               # Daily GitHub Actions workflow
  package.json
  README.md
```

## Getting Started

### Prerequisites

- Node.js >= 18

### Add a Source

Edit `data/sources/sources.json` and add an entry:

```json
{
  "id": "my-source",
  "name": "My Opportunity Feed",
  "url": "https://example.com/feed.rss",
  "type": "rss",
  "enabled": true
}
```

### Run the Pipeline Manually

```bash
npm run pipeline
```

Or run each step individually:

```bash
node scripts/fetch_rss.js      # Fetch raw XML
node scripts/normalize.js      # Normalize to JSONL
node scripts/dedupe_merge.js   # Merge & deduplicate
```

## Automated Workflow

The GitHub Actions workflow (`.github/workflows/rss.yml`) runs daily at 06:00 UTC and automatically commits updated data files to the repository.