# data/latest

This folder contains the **final, de-duplicated Opportunity dataset** that the website consumes.

## Files

| File                  | Description                                                         |
|-----------------------|---------------------------------------------------------------------|
| `opportunities.json`  | JSON array of all unique Opportunities, sorted by `published_at` desc (nulls last) |
| `opportunities.jsonl` | Same records in JSON Lines format (one record per line)             |

## Update cadence

These files are regenerated on every pipeline run (every 6 hours via GitHub Actions
and on every `workflow_dispatch`).

## Usage

Point your website / frontend at:

```
data/latest/opportunities.json
```

You can fetch it directly from the raw GitHub URL:

```
https://raw.githubusercontent.com/YouthOpp/data-pipeline/main/data/latest/opportunities.json
```
