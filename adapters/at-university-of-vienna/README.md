# University of Vienna Non-EU exchange places

Source: [University of Vienna International Office](https://international.univie.ac.at/en/study-abroad-with-erasmus-and-co/non-eu-student-exchange-program).

The adapter collects real outgoing exchange allocations from the four public Mobility Online inventories linked by the programme page: Africa/Asia; Australia/Canada/USA; Latin America; and remaining places. It selects each inventory's latest published academic year and checks the full HTML table against the publisher's advertised allocation count. DataTables pagination is client-side; every row is present in the response.

Records preserve the publisher's institution, programme, host country, study field, teaching languages, total places, free places and application-availability indicator. Explicitly available allocations with positive free places are `open`; other allocations have `unknown` status. Negative free-place counts are retained as published. Exchange study is classified as `training`. Scholarships depend on ranking and budget, so the adapter makes no unconditional funding promise or inferred nationality restriction. Recurring date-only programme deadlines do not establish a dated deadline for an individual allocation; `deadline` remains null.

## Access and attribution

Only public read-only HTTPS GET endpoints are used. No login, application submission, external downloads or production execution is required for collection. The programme's robots policy permits these paths; Mobility Online returned 404 for robots.txt during research. The adapter checks robots policy at runtime and fails on denied access. Both publisher hosts share one request allowance: at least six seconds between starts and at most ten requests per rolling minute. A process lock serializes overlapping runs.

The [official imprint](https://international.univie.ac.at/en/imprint) permits attributed noncommercial copying. Records and publication metadata identify `Source: University of Vienna`. Summaries contain factual allocation fields, not copied explanatory articles.

## Stable identity

Identifiers include the source, programme, academic year, partner institution and study field. University of Ottawa has two allocations whose listing fields are otherwise identical. The public agreement details distinguish Bachelor-only from Bachelor/Master allocations through explicit check/cross indicators. All Ottawa allocations always include their study-cycle and duration signature in the identifier, even when only one remains. Place quantities, row order and encrypted detail-link parameters never participate in identity. New ambiguous non-Ottawa allocations fail pending identity review.

Record links use durable public partner-filter form URLs. Transient encrypted agreement links are fetched only to read Ottawa qualification indicators and are not persisted in record identifiers or URLs.

## Run and validate

Python 3.12 or later, standard library only, on a POSIX platform supporting `fcntl` locks:

```sh
python -B adapters/at-university-of-vienna/test_adapter.py
```

The single live test collects genuine records, validates the complete result and emits one JSON document on stdout. Diagnostics go to stderr. It never publishes or writes opportunity data files. The three-file source folder also runs independently when copied outside the repository.

During research the four inventories contained 31, 23, 4 and 32 allocations respectively. These counts are evidence from the reviewed inventory, not hardcoded acceptance limits; runtime checks use the publisher's current counts.

## Publication

The matching `fetch-at-university-of-vienna` Action runs automatically on changes to this adapter folder or its matching workflow on `main`, and also supports manual dispatch. Its sole application command runs this adapter with `--publish`; it does not invoke tests or install dependencies.

Publication writes only `YouthOpps/data-source/datas/at-university-of-vienna/{data,metadata}.json` atomically. First-run failure creates no published source folder. Later failure preserves last-good data and its success timestamp and records failure metadata when possible. Implementation and live testing do not trigger production publication; the automatic Action runs only after maintainer merge.
