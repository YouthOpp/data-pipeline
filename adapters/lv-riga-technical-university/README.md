# Riga Technical University funding opportunities

This source collects RTU's substantive published doctoral awards, mobility
funding routes, Latvian state scholarship information and postdoctoral call.
The publisher is Riga Technical University; facts retain its attribution and
canonical URLs. Collection does not infer Latvian citizenship from the host.

## Inventory and eligibility

The doctoral [funding page](https://www.rtu.lv/en/studies/doctoral-studies/scholarships-and-grants)
has six funding sections and a separate FAQ. The parser checks the complete
reviewed section inventory and rejects missing, duplicate or unfamiliar
sections. The FAQ is evidence, not an award. Tuition discounts exclude
state-funded students and RTU employees. The EUR 140 monthly scholarship has
state-funded study, academic progress and study-start/statutory conditions.
RTU doctoral grants and Recovery and Resilience Mechanism grants explicitly
have no announced application round; they remain genuine programmes with
unknown application status and no deadline. RRM funding is EUR 1292 monthly
including employer social security at 0.5 FTE for at least 12 months, plus up
to EUR 500 monthly research expenses.

The doctoral Erasmus umbrella is represented by four actual linked funding
routes rather than an extra duplicate: European study, traineeship, staff
mobility and partner-country study. European study requires average 6.75;
traineeship and partner-country study require 6.25. European study and
traineeship specify bachelor semester three/master semester two; partner
country study does not specify the master semester-two condition. European
study lasts 2–12 months, partner-country study 3–12 months. Student funding
only partly covers costs. Trainees must secure a host; academic leave excludes
the Erasmus scholarship, whereas employer payment above EUR 600 monthly
excludes only Latvian state co-financing. Staff funding covers at most five
working days and excludes payment for travel days without work. Recurring
month/day windows have no stated year, so do not become invented deadlines.

The ERDF doctoral fourth selection is one actual call, checked against its
canonical notice. Cohort numbers, available places, historical FAQs, mirror
notices and regulations for another selection do not create extra records.
Its deadline is 14 October 2026, source clock 16:59 without a stated timezone.
The record uses `2026-10-14` and preserves the clock separately and in its
summary. No Riga offset is invented. A same-day date-only deadline has unknown
status; the website's end-of-UTC-day expiration convention does not establish
a publisher closing instant.

The postdoctoral fifth selection closes 29 October 2026 at 23:59 EET, preserved
as `2026-10-29T23:59:00+02:00`. Its 26 October access-request milestone is
separate evidence, not the application deadline. Eligibility includes Latvian
or foreign researchers with a doctorate obtained within ten years and RIS3
research. The published support includes up to EUR 130419 over 24 months.

The Latvian state scholarship page names academic year 2026/27 but also retains
2025 application text. It remains a programme with no exact deadline and
unknown status. Bilateral/reciprocal-country eligibility is described without
inventing an exhaustive country list. This reviewed inventory currently yields
11 identities, but collection discovers and validates source sections instead
of asserting a fixed record count. Programme identities and published call
selection identifiers produce stable source-specific IDs.

## Access and reuse

Public English HTML is read with an identifying YouthOpp user agent and normal
TLS verification. Live robots rules are honored, including ordinary CRLF
syntax, redirected paths and any stricter crawl delay. Application platforms,
search, disallowed application pages and the disallowed Latvian privacy redirect
are not collected. No login, application submission, email decoding or protected
PDF retrieval is needed.

RTU's published intellectual-property management policy concerns creation and
use of works in employment, study and cooperation relationships. It does not
state a blanket prohibition on factual public-page collection, and does not
grant a blanket open-content licence. Records use attributed facts and short
original summaries, without reproducing images or entire publisher prose.
Research evidence is cached outside the repository in
`/tmp/youthopps-source-triage/16`; that cache is not a runtime dependency.

Requests, redirects and bounded transient retries share a source-specific
cross-process lock and durable pacing state outside the repository: at least
six seconds between starts and at most ten starts per rolling minute. Robots
and Retry-After requirements can impose longer waits. Parser or policy changes
fail the collection rather than publish a partial result.

## Run and publication

```sh
python -B adapters/lv-riga-technical-university/test_adapter.py
python -B adapters/lv-riga-technical-university/adapter.py
```

The single live test emits the complete JSON record array on stdout and test
diagnostics on stderr. It needs no publication credentials and writes no output
files. The adapter's ordinary command also collects without publication.

The matching workflow runs only in the upstream repository on main, after
changes to this adapter or its workflow, by manual dispatch, and every six hours
at minute 49. Its narrowly scoped GitHub App token targets only `data-source`.
Publication uses the adapter's `--publish` command and shared publication
concurrency. A single atomic commit updates only this source's data and metadata.
A first failed collection creates no source folder; later failures preserve
last good data and its durable success timestamp while recording failure
metadata. The success timestamp advances only after successful publication of
validated nonempty records. Local tests do not publish or run Actions.

## Production access diagnostics

The initial automatic and manual hosted-runner collections returned HTTP 403
before any source folder was published. Timing indicates the first publisher
request, but the original error did not identify its endpoint. Identical
identifying requests succeed from this cloud environment; an IP-based access
restriction has not been established. Robots requests now advertise plain text,
matching the publisher response, while HTML requests retain their ordinary HTML
Accept header. HTTP failures identify only the public HTTPS host and path,
excluding queries and user information. This change supplies actionable hosted
failure evidence; it does not prove that the hosted access refusal is resolved.
The identifying user agent, TLS verification, robots enforcement and no-retry
policy for HTTP 403 remain unchanged.
