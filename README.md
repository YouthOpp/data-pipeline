# YouthOpps data-pipeline

Six independent source adapters for [YouthOpps](https://youthopps.org). Each adapter is a small standalone project with no shared runtime code or external packages.

```text
adapters/
  at-oead-ernst-mach/
  bg-feba-alumni/
  de-fulbright-germany/
  ee-university-of-tartu/
  opportunitydesk/
  us-nasa-internships/
    adapter.py
    test_adapter.py
.github/workflows/
  fetch-<source-id>.yml
```

Every source folder contains exactly `adapter.py` and `test_adapter.py`. Only these six implemented sources are installed. Add another source when its adapter is developed; do not create placeholder source folders.

## Run one adapter

Python 3.12 or later, standard library only. No package installation is needed. Commands work from the repository root; an adapter can also run independently from its own folder.

```sh
python -B adapters/opportunitydesk/adapter.py
```

This retrieves the real source, parses and validates its records, then reports the outcome without publishing. Source URLs, parsing, request limits, record validation and GitHub publication all live in that adapter's own file. Publisher requests, retries and redirects are paced at least six seconds apart, at most ten per rolling minute per target, with stricter robots delays and Retry-After respected.

For authorized publication, provide `DATA_SOURCE_TOKEN` through the environment and run:

```sh
python -B adapters/opportunitydesk/adapter.py --publish
```

The token must have contents write access to `YouthOpps/data-source`. Do not put it in source code or command arguments. Publication updates only `datas/<source-id>/data.json` and `metadata.json` on that repository's `main` branch, together in one Git commit through the GitHub API. Non-force reference updates and source snapshot comparisons prevent overwriting concurrent changes. No local data-source checkout is required.

A successful nonempty collection records `status: "success"` and advances the last-success timestamp. Fetch, parsing, validation or publication failure records `status: "fail"`, a sanitized error, failure stage and UTC attempt time, while preserving last-good data and its success timestamp. If durable error reporting is unavailable, the run fails explicitly. A source with no previous successful data does not create a published folder on failure. No catalog or aggregate source index is produced.

## Test one adapter

Each folder has exactly one live, non-publishing test. It checks whether that source can be retrieved and produces valid nonempty records:

```sh
python -B adapters/opportunitydesk/test_adapter.py
```

An unavailable or changed publisher fails the test. Tests do not write local or remote data and are never invoked by Actions. There are no shared tests, fixtures, scripts, schemas, source registries or package manifests.

## GitHub Actions

Each of the six files in `.github/workflows/` runs only its matching adapter with `--publish`. It obtains `DATA_SOURCE_TOKEN` using the existing `WEBSITE_APP_ID` variable and `WEBSITE_APP_PRIVATE_KEY` secret for the data-source GitHub App installation. Runs are limited to this repository's `main` branch and share a publication concurrency group.

The existing six-hour schedules for OeAD, FEBA and Fulbright Germany are preserved. Opportunity Desk, NASA and University of Tartu can be started manually. Actions do not install project dependencies or invoke test files.

The workflow folder is the sole code-layout exception to the independent adapter folders: [GitHub discovers workflows in `.github/workflows/`](https://docs.github.com/en/actions/concepts/workflows-and-actions/workflows).

Before publishing a data-layout migration, coordinate consumers to use `datas/<source-id>/{data,metadata}.json`. Website ingestion and the website's data-source pin belong to its own project.
