# data/normalized

This folder stores **normalized Opportunity records** parsed from raw feeds.

## Structure

```
data/normalized/
  opportunities/
    YYYY-MM-DD.jsonl   # one JSON Lines file per day; each line is one Opportunity record
```

## Data model

Each line is a JSON object with the following fields:

| Field         | Type            | Required | Description                                      |
|---------------|-----------------|----------|--------------------------------------------------|
| id            | string          | ✅        | Deterministic SHA-1 hash of `source + "|" + url` |
| title         | string          | ✅        |                                                  |
| url           | string          | ✅        |                                                  |
| source        | string          | ✅        | e.g. `"opportunitiesforyouth"`                   |
| source_url    | string          | ✅        | The feed URL                                     |
| published_at  | string (ISO8601)| nullable |                                                  |
| summary       | string          | nullable |                                                  |
| tags          | array of strings| optional |                                                  |
| location      | string          | optional |                                                  |
| deadline      | string (ISO8601)| optional |                                                  |
| language      | string          | optional |                                                  |
| created_at    | string (ISO8601)| ✅        | When the record was first written                |
| updated_at    | string (ISO8601)| ✅        | When the record was last updated/merged          |

## Notes

- Daily files are never deleted — full history is preserved.
- The `data/latest/` folder contains the de-duplicated merged output for the website.
