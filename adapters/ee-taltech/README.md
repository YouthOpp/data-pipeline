# TalTech Development Fund scholarships

Source `ee-taltech` collects the complete [official Autumn 2026 scholarship call](https://taltech.ee/arengufond/stipendiumid/2026-sugis) published by the Tallinn University of Technology Development Fund. Python 3.12 or later and the standard library are sufficient. The [English scholarship directory](https://taltech.ee/en/scholarships) is a publisher reference, not an opportunity record or an authoritative current competition calendar.

## Access and reuse evidence

On 8 October 2026 the official seasonal page and [robots policy](https://taltech.ee/robots.txt) both returned HTTP 200 without login or an access challenge. Robots permit public pages but disallow `/api/*`. The complete award descriptions are already present in the server-rendered HTML, so the adapter does not access the disallowed API or execute/download application code. Original official statute PDFs are retained as links; they are not fetched. Application forms, student systems and applicant information are not accessed.

The linked [privacy policy](https://taltech.ee/en/privacy-policy) describes the university's processing of personal data and does not prohibit this reviewed public award collection route. No separate public reuse license or automated-collection prohibition was found in the reviewed official navigation, footer and policy. This does not assert unrestricted redistribution rights. Records retain the source ID, original statute URL and all supporting publisher anchors; metadata supplies the human-readable attribution.

Requests identify YouthOpp, respect robots on fetched pages and redirects, and honor Retry-After. Publisher starts are at least six seconds apart and at most ten per rolling minute. Local overlapping runs use locked pacing state outside the checkout. Production runs share serialized publication concurrency; do not overlap a local run with production on another machine.

## Complete inventory and deduplication

The live inventory reviewed on 8 October 2026 contains 92 genuine scholarship cards and six FAQ cards. It covers the special competition, all-study-level awards, economics, IT, science, engineering subject groups, the Maritime Academy, and lecturer/researcher awards. There is no additional pagination: the complete accordion content is present in the fetched HTML.

The 92 award cards produce 81 distinct records. Nine repeated-statute groups have identical complete award descriptions after only Unicode, whitespace and punctuation-spacing normalization: the Marine Engineers Union, IT College, Swedbank, VKG bachelor's, Foreign Intelligence Service bachelor's, Wise, VKG master's, Foreign Intelligence Service master's and TREV-2. These account for eleven repeated cards. Their full descriptions explicitly preserve the shared allocation and eligibility across faculties; for example, Swedbank's one shared call describes both faculty allocations. Every duplicate publisher anchor is retained as evidence and every section label as a tag. Matching statute links alone do not justify merging: conflicting descriptions, deadlines, categories or nationality eligibility fail collection.

Different bachelor's/master's statutes remain distinct. The two Metrosert awards also remain distinct, including the explicitly named internship award. Secondary older PDF links, FAQ/application-document examples, homepage/directory metadata and general event/news descriptions are excluded. The English Development Fund overview still described the expired Spring 2026 competition when reviewed; the assigned official Estonian Autumn 2026 page takes precedence. Other general university aid programmes belong to different application routes and are outside this seasonal call's inventory.

## Identity, deadlines and eligibility

Stable IDs derive from source ID, the publisher's academic-season heading and the normalized original statute URL. Original URLs remain unchanged; source evidence points to the actual HTML accordion headings. Empty collections, duplicate IDs, unsafe URLs, invalid dates/countries/categories and ambiguous repeated awards fail validation.

The main competition explicitly accepts applications from 30 September through 19 October 2026. Its 73 distinct regular awards use 19 October as their date-only deadline, interpreted through the end of that Tallinn calendar day and serialized in UTC. Peep Sürje has an explicit individual 12 October deadline, which overrides the calendar. The other seven special-competition awards lack a reliable individual deadline, so their deadlines remain null and their status unknown. Their mention of `30.09–20.10` concerns exclusion from the main competition's counting rules; it is not treated as their application deadline. Dated records become expired after their deadline rather than remaining perpetually open.

All records are scholarships; the Metrosert internship scholarship also carries the internships category. Estonia is the study/host country. Estonian nationality is recorded only where the award body explicitly requires it; university enrollment, local study or the publisher's country do not imply citizenship eligibility. Full academic, allocation and application conditions remain available through the original statute and publisher links. Publication dates are not invented from unrelated page dates; collection freshness timestamps are UTC.

## Run and live test

From the repository root:

```sh
python -B adapters/ee-taltech/adapter.py
python -B adapters/ee-taltech/test_adapter.py
```

The folder also works independently outside the repository. Its sole live test retrieves real nonempty data, validates every record and emits the complete result as one JSON document on stdout, with diagnostics on stderr. It receives no publication credentials and writes no output files. The implementation live test validated all 81 records in memory, including unique IDs/URLs, all 92 award-card coverage, duplicate provenance and special deadlines.

## Publication

The matching `fetch-ee-taltech.yml` Action runs automatically on main pushes affecting this adapter folder or its own workflow, including maintainer merges. Manual dispatch and a six-hour schedule (`37 0/6 * * *`, UTC) are available. The repository/main guard, global publication serialization, read-only pipeline permissions and the existing GitHub App's data-source-only contents-write token scope are retained. The sole application command is:

```sh
python -B adapters/ee-taltech/adapter.py --publish
```

Publication updates only `YouthOpps/data-source/datas/ee-taltech/{data,metadata}.json`, atomically and with conflict-safe reference updates. Successful publication of validated nonempty records advances the durable success timestamp. Later failures preserve last-good data and its success timestamp and record sanitized failure metadata when possible; failure before the first success creates no source folder. Tests never publish. Production publication requires maintainer merge and was not invoked during implementation.
