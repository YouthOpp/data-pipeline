# Rausvonzuhaus vacancies

This adapter reads the actual public [Last Minute Markt](https://www.rausvonzuhaus.de/lastminute) vacancy cards published by Eurodesk Deutschland / IJAB. The FAQ and other directory pages are not records. Every result links to its original normal `/lastminute/detail/<numeric-id>` page. The numeric publisher ID supplies the stable source identity; title, dates or position in the list do not determine that identity.

## Access and reuse

Reviewed on 8 October 2026: the public [robots policy](https://www.rausvonzuhaus.de/robots.txt) permits the normal listing and detail routes, but disallows `/typo3/` and paths containing `hide_page_frame`. Collection uses the normal listing only, without embedding queries, login, applicant data or external application requests. Requests identify YouthOpp and send an ordinary HTML Accept header. Transient HTTP failures receive at most one paced retry; Retry-After also applies to that retry. Robots is checked on each run and on content redirects. A complete HTML document and nonempty, valid card inventory are required.

The publisher's [imprint](https://www.rausvonzuhaus.de/impressum) states:

> Soweit im Einzelfall nicht anders geregelt und soweit nicht fremde Rechte betroffen sind, ist die Verbreitung der Inhalte von www.rausvonzuhaus.de über die embedding Funktionen oder in Teilen davon in elektronischer und gedruckter Form für die Arbeitsfelder Internationale Jugendarbeit und Jugendinformation unter der Voraussetzung erwünscht, dass die Quelle (www.rausvonzuhaus.de) genannt wird.

It prohibits commercial distribution without prior written IJAB permission. YouthOpps' intended noncommercial youth information use matches the stated purpose. Records provide attributed partial factual metadata and our own concise summary; they do not reproduce photographs, image credits or full third-party descriptions. Every record and source metadata attribute `rausvonzuhaus.de / Eurodesk Deutschland (IJAB)`. The official contact for questions is `rausvonzuhaus@eurodesk.eu`, with IJAB contact `info@ijab.de` on the imprint.

One robots request and one listing request normally provide the entire inventory. Requests, retries and redirects are separated by at least six seconds and limited to ten starts per rolling minute. The publisher's stricter robots delay and Retry-After are honored. A lock and durable pacing state in the operating system's temporary directory serialize overlapping local processes. Production Actions share serialized publication concurrency; separate machines must not run this source simultaneously.

## Inventory and fields

All cards are present in the public HTML; no pagination or external detail enrichment is necessary for the supported fields. Parse the `lmm-item` card boundaries and actual title, programme badge and factual paragraphs. On 8 October 2026, the inspected listing contained 477 distinct numeric IDs; 471 had internally consistent dates: 462 volunteering, seven youth/school exchanges classified as training, and two student internships. Counts are discovered on every run, not hardcoded acceptance targets.

Programme badges include IJFD, weltwärts, workcamps, French-German and other volunteering, European Solidarity Corps, youth encounters, student internships and school exchanges. The adapter retains the publisher's German programme label in tags and classification evidence. Age bounds, publisher host label, project dates and application deadline come directly from each card. Two-letter host codes are a mapping of the explicit country label, including the commonly used `XK` for Kosovo. A host country does not establish citizenship eligibility: `eligible_countries` remains empty. Costs and benefits are unspecified in the list and are not guessed; a reviewed detail example requires a €3,200 contribution, so inclusion does not imply free participation.

The publisher sometimes has a title/location disagreement, for example a Trento title beside a Germany host label. Preserve and attribute the structured label; do not infer a replacement host country from a place name in a title. Each record's classification evidence preserves the actual publisher host label for review. Titles are normalized for whitespace; image copyright credits are excluded.

## Dates and exclusions

Explicit `DD.MM.YYYY` dates become ISO calendar dates. `deadline` is `YYYY-MM-DD` with `deadline_precision: date`, and `start_date`/`end_date` preserve the project interval. The publisher gives no deadline clock or time zone. No closing instant is invented. The website interprets date-only deadlines through the end of the UTC date as a consumer convention, not a publisher statement. Future deadline days on a published vacancy are `open`, past deadline days are `expired`, and the same UTC calendar day is conservatively `unknown` because the closing clock is unspecified. Valid expired opportunities are retained; an ended project alone is not an exclusion.

Exclude internally contradictory ranges or an application deadline after the stated project end, and report the number to stderr. Do not repair a year by inference. Six inspected cards require this exclusion:

| Publisher ID | Project start | Project end | Application deadline |
|---|---|---|---|
| 2527 | 2025-09-01 | 2026-08-31 | 2026-10-10 |
| 7418 | 2026-09-01 | 2026-08-31 | 2026-10-15 |
| 2521 | 2025-09-01 | 2026-08-31 | 2026-10-26 |
| 3226 | 2025-08-15 | 2026-08-15 | 2027-02-12 |
| 6995 | 2026-09-01 | 2027-03-01 | 2027-05-31 |
| 7660 | 2027-03-01 | 2027-09-01 | 2027-10-31 |

Every deadline exceeds its stated project end; 7418 additionally reverses start and end. These are excluded for contradiction, rather than merely for being old. If a source corrects the dates, the card is included automatically. New unsupported card structures, age/date formats, programme labels or country labels fail collection rather than silently discarding part of the inventory. Duplicate IDs or URLs and incomplete HTML also fail validation.

## Run and publication

From the data-pipeline repository:

```sh
python -B adapters/de-rausvonzuhaus/adapter.py
python -B adapters/de-rausvonzuhaus/test_adapter.py
```

The single live test collects actual nonempty records, validates them, and emits the complete JSON document on stdout. Diagnostics go to stderr. It has no publication credentials and writes no output files. The folder operates independently using Python's standard library; its test imports only its own adapter in addition to standard modules.

The matching `fetch-de-rausvonzuhaus.yml` runs after changes to its own adapter/workflow are merged to `main`, supports manual dispatch and runs every six hours at minute 41. A repository/main guard, minimal permissions, data-source-only GitHub App token and global publication concurrency constrain the Action. Its sole application command uses `adapter.py --publish`; tests and formatting never run in production.

Production starts after maintainer merge. It atomically publishes only `datas/de-rausvonzuhaus/data.json` and `metadata.json` using a conflict-safe Git commit. First-run failure creates no source folder. After a successful publication, later failure preserves last-good data and the last successful timestamp and records sanitized failure metadata when possible. The success timestamp advances only after a validated, nonempty result is successfully published. Failed metadata publication is reported rather than claimed to have been saved. This adapter does not alter any other source or create a catalog.
