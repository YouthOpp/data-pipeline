# YouthOpps data-pipeline

Source adapters and scheduled collection for [YouthOpps](https://youthopps.org).

## Repositories

- [data-pipeline](https://github.com/YouthOpps/data-pipeline): source configuration, trusted adapters, and GitHub Actions.
- [data-source](https://github.com/YouthOpps/data-source): source-specific JSON snapshots and the derived `catalog.json`.
- [youthopps.github.io](https://github.com/YouthOpps/youthopps.github.io): static website, country/category filtering, and pinned catalog builds.

## Collection contract

Each enabled source has **one workflow** named `.github/workflows/source-<id>.yml`. A run selects **one source** from `data/sources/sources.json`, executes its reviewed adapter, validates the canonical records, and updates only `data-source/sources/<id>/` plus the derived `catalog.json` in **one commit**. A shared concurrency group prevents simultaneous publication. Scheduled workflows are staggered four minutes apart; each runs every six hours.

Failed or empty collections fail without publishing; the previous source snapshot remains intact. The website uses a commit-pinned catalog from `data-source` and updates at most once every six hours. Disabled or access-restricted sources have no scheduled workflows and are omitted from the published catalog.

The current selection comes from the latest successful source-level results, excluding sources with collection failures. Inactive sources remain in the registry for research and re-verification, not automatic collection.

## Development

Requires Node.js 22. Run `npm ci --ignore-scripts`, `npm run validate`, and `npm test`. To locally execute a single adapter, check out `YouthOpps/data-source` into the `data-source/` subfolder and run `node scripts/publish-source.js <source-id> data-source`. This updates local JSON but does not commit or push.

All data must be based on the original factual title, URL, and reviewed metadata. Never infer applicant eligibility or an open application window from publisher location. Keep source permission and robots restrictions enforced.

## Contributions

Use [GitHub Discussions](https://github.com/orgs/YouthOpps/discussions) first for general questions and [Issues](https://github.com/YouthOpps/data-pipeline/issues) for actionable source problems. Email contact@youthopps.org when GitHub is unsuitable.
