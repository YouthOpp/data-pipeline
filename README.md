# YouthOpps data-pipeline

Source adapters and scheduled collection for [YouthOpps](https://youthopps.org).

## Repositories

- [data-pipeline](https://github.com/YouthOpps/data-pipeline): source definitions, adapters, and collection workflows.
- [data-source](https://github.com/YouthOpps/data-source): source JSON snapshots and the unified `catalog.json`.
- [youthopps.github.io](https://github.com/YouthOpps/youthopps.github.io): static website using `data-source` as a Git submodule.

## Collection

Each configured source has one workflow named `.github/workflows/fetch-<source-id>.yml` with a matching `fetch-<source-id>` display name. Runs are scheduled every six hours and can be started manually. Each workflow fetches and validates its own source, writing only `data-source/sources/<source-id>/` and `data-source/catalog.json` as pretty-printed JSON in a single data-source commit when content changes. Collection runs share a concurrency group to serialize publication.

Failed or empty collections do not publish. The website checks data-source hourly using its own `check new data` workflow, commits a changed submodule pointer to its own main branch, and Cloudflare deploys website commits. Pipeline workflows do not access or commit to the website repository.

## Development

Node.js 22:

```sh
npm ci --ignore-scripts
npm run validate
npm test
```

For one source, check out `YouthOpps/data-source` into the `data-source/` subfolder and run `node scripts/publish-source.js <source-id> data-source`.

Preserve source provenance and never infer eligibility, deadlines, or source permissions without evidence.

## Support

Use [GitHub Discussions](https://github.com/orgs/YouthOpps/discussions) for discussions and [Issues](https://github.com/YouthOpps/data-pipeline/issues) for actionable bugs. Private contact: contact@youthopps.org.
