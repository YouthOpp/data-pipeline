# YouthOpp data pipeline

Open AI agent: built and maintained through transparent AI-assisted development.

Canonical repositories: [YouthOpp/data-pipeline](https://github.com/YouthOpp/data-pipeline), [YouthOpp/youthopp.github.io](https://github.com/YouthOpp/youthopp.github.io) and [YouthOpp/.github](https://github.com/YouthOpp/.github).

A community-contributed source adapter pipeline for YouthOpp, a nonprofit-minded, open-source opportunity index. Students and young graduates can discover original opportunities without browsing many separate publishers. Original publishers remain the authority for deadlines, eligibility and applications.

## Run

Requires Node.js 22 or later. `npm ci --ignore-scripts`, `npm run validate`, `npm test`, then `npm run pipeline`. The build produces `dist/catalog.json` and `dist/collection-report.json`; `npm run contributors` produces `dist/contributors.json`. These outputs are never automatically committed into Git history.

To preserve prior records locally, set `PREVIOUS_CATALOG=/path/to/catalog.json`. Scheduled Actions restore the last successful public release before collecting new feeds. A failed or empty source preserves its last good records and their verification timestamps. Total source failure blocks publication. Partial failures are visible in the report and source metadata. A missing record in a bounded RSS feed is not treated as closed; only explicit deadlines produce open/expired status. Otherwise availability is unknown. Retained records may be old: the website must display dates and unknown status, not promise all opportunities remain available.

## Public data contract

`catalog.json` contains `schema_version: 1`, `generated_at`, `opportunities` and `sources`. Each opportunity keeps original article `url`, feed `source_url`, publisher `source` slug, original-language title and an empty summary (no publisher prose), publication and collection dates. Category is derived from publisher-provided tags or explicit reviewed programme selection. Location, deadline, countries and eligibility remain unknown until explicit evidence is available. Source entries include publisher `website_url`, last attempt, last successful check and errors.

## Publication

The trusted `main` branch in [YouthOpp/data-pipeline](https://github.com/YouthOpp/data-pipeline) publishes releases; the default-branch schedule runs every six hours, and manual dispatch can refresh it. A versioned release (`catalog-<run-id>-<attempt>`) stores an auditable data snapshot; `catalog-latest` serves the most recent successful catalog. Frontend builds use the committed catalog-release.json pointer to pin and verify an immutable manifest and its catalog/contributor assets; catalog-latest is used only before the first website pointer exists. The pipeline restores verified state and retains the newest 30 published versioned snapshots, preserving the current release and latest pointer. See [Catalog operations](https://youthopps.org/docs/pipeline-operations/) for publication ordering, recovery and retention. No paid backend, API key or database is required. After successful release publication, the workflow commits a small immutable release pointer to website main using a short-lived GitHub App installation token scoped to the website repository (Actions variable `WEBSITE_APP_ID` and secret `WEBSITE_APP_PRIVATE_KEY`); Cloudflare's existing Git integration then rebuilds the site. No generated dataset is committed to the frontend. See [Cloudflare setup](https://youthopps.org/docs/cloudflare-pages/).

PR checks use read-only permissions, no secrets, fixture tests, and no remote collection. Collection and publishing execute only trusted default-branch code. Public RSS is capped at 5 MB and 25 seconds, HTTPS-only with bounded same-host redirects; unrelated hosts are rejected. GitHub limits still apply.

## Sources and contribution

See the [adapter contribution guide](https://youthopps.org/docs/pipeline-adapters/). Every registered source has an explicit enabled switch; collection also respects documented policy and access gates. The current configuration enables 110 source definitions: reviewed feeds, HTML programme/listing adapters, and the public Campus Bourses API. Enabled means collection may be attempted; it does not assert a successful fetch, an open application, eligibility or rights to publisher prose. Source-health reports preserve successful, error and blocked outcomes. Link-metadata adapters publish only the reviewed factual title and original link; existing specialized adapters likewise retain no article prose. Portugal leaves original publication null when absent; modification is never publication. Exact-page adapters make one request to the reviewed page; publisher country does not imply applicant eligibility or destination. Feed or page access does not imply ownership of publisher content. YouthOpp publishes factual source titles and original links only; source inclusion can be paused or removed through the [documented issue/PR process](https://youthopps.org/docs/source-removal/). No institution endorsement or charitable registration is implied.

See the [scoped country evidence renewal](https://youthopps.org/docs/country-renewal-2026-10-05/) for dated research and application/access limits.

The OeAD exact notice requires visible `© OeAD` copyright credit from the source `attribution` field on the source directory, associated record detail and every associated catalogue, home-page and category listing row. Its date-only publication and declared deadline are research/title evidence; catalogue timestamps and application state stay unknown. See the [OeAD access review](https://youthopps.org/docs/oead-access-review-2026-10-05/).

## Categorical model

See the [data model](https://youthopps.org/docs/data-model/) for the shared directory taxonomy, record/source classification, geography, migration and integrity-covered ID indexes.

## Authoritative technical documentation

The rendered [YouthOpp Docs](https://youthopps.org/docs/) are the authoritative prose documentation for this pipeline. Start with [architecture](https://youthopps.org/docs/architecture/), the [adapter and catalog contract](https://youthopps.org/docs/adapter-contract/), [data quality](https://youthopps.org/docs/data-quality/) and [source research](https://youthopps.org/docs/source-research/). This repository retains executable contracts—schemas, tests and sanitized fixtures—and machine-readable evidence, rather than a second independent copy of the same technical prose. Dated fork run and release URLs inside research provenance remain unchanged as historical evidence; production collection, contributor scoring and publication use the original YouthOpp repositories.

## Repository boundary

This repository owns adapters, collection scripts and Actions, source research/registry, canonical opportunity data and contributor history. The website only downloads the integrity-verified release and presents it. Tracked `data/sources/` contains maintained source configuration and research; generated datasets live in releases rather than dated raw/normalized/latest Git trees. Only the active ESM collection path remains. The removed legacy datasets were not used by either active workflow or the release consumer; they are not migrated into the canonical index because their full article content and missing classification/verification fields would invent unsupported records. Prior Git history and existing release snapshots remain available. Sanitized extraction fixtures and active validation are retained.

`contributors.json` records one point per attributable non-merge authored commit, deduplicated across public repository histories. Bots and explicitly AI-authored commits are excluded; identity gaps and collection errors remain visible. The same immutable catalog manifest covers this auxiliary dataset.




## Opportunity extraction and acceptance

`opportunity-html` extracts factual title/link records from configured publisher listings or exact programmes selected from the dated research registry. A programme overview does not assert an open application call. Generic directory pages are not counted as opportunities. Title destination phrases add explicit country evidence; publisher geography remains separate from host and applicant countries.

Every publication runs `scripts/verify-catalog.js`, persists per-source coverage in `collection-report.json`, and writes an Actions summary. After upload, `scripts/verify-published.js` verifies downloaded release assets, their hashes and actual source coverage before the website is notified. `REQUIRE_ALL_SOURCES=1` makes incomplete coverage fail; ordinary partial publication preserves last-good records and reports every incomplete source. Issues track publisher-specific access and extraction failures.

Source coverage is a separate required workflow job (`complete-source-coverage`). It deliberately fails while any enabled source lacks fresh classified records, even when the partial catalog is published successfully. Publication, source completeness, and website deployment are distinct checks. See issue #6 for coverage work and #7 for publisher permission dependencies. Connection failures receive one bounded retry; HTTP access denials are retained as errors. Reviewed external programme links are emitted only when their exact title and URL still occur in the live source page. Listing extraction no longer silently stops at 50 records; more than 500 requires a dedicated paginated adapter and fails explicitly.


The `campus-bourses` adapter reads the official public English catalogue API, verifies its declared count against every returned programme and rejects duplicate or malformed IDs. It publishes programme titles and original frontend routes, with no copied summaries. API update dates and search-country filters do not become publication dates, application deadlines, destinations or applicant eligibility. Reviewed-only programme links and explicit record exclusions prevent generic tuition guidance from reappearing as an opportunity in retained snapshots.
