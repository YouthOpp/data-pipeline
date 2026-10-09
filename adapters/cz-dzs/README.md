# DZS — European Solidarity Corps

This standalone standard-library adapter collects the current published
European Solidarity Corps (ESC) programme, 2026 grant call and directly linked
educational offers administered or advertised by the Czech National Agency,
[Dům zahraniční spolupráce (DZS)](https://www.dzs.cz/en).
The assigned source is the [ESC grant page](https://www.dzs.cz/program/evropsky-sbor-solidarity/projekty-granty).
It does not collect all DZS programmes, historical projects or the external
SALTO catalogue.

## Official access and reuse

The public programme, call, participant and [NET pages](https://www.dzs.cz/networking-activities)
provide ordinary HTML and literal linked documents without authentication.
The [DZS robots policy](https://www.dzs.cz/robots.txt) currently contains a
literal shell wrapper; it is inert data and must never be executed. Search,
facet filters, scholarship finders and private/system routes are excluded.
The [GDPR page](https://www.dzs.cz/zpracovani-osobnich-udaju) and
[statutory information](https://www.dzs.cz/povinne-zverejnovane-informace)
are not content licences or evidence of a blanket factual-reuse ban.
The adapter emits brief original factual summaries and attribution, not
publisher prose, photos, recipient lists or application data.

The eight actual delegated training links are public
[SALTO-YOUTH](https://www.salto-youth.net) detail pages, checked against its
[robots policy](https://www.salto-youth.net/robots.txt).
Its [legal notice](https://www.salto-youth.net/about/legal-notice/) welcomes
attributed circulation for formal and non-formal education, subject to
individual/third-party rights. Commercial dissemination of its texts,
information or photos requires prior written permission. This integration
uses original factual educational summaries with source URL attribution;
it does not grant commercial reuse or claim an open licence. No publisher
contact, login or application submission is performed.

The directly linked [2026 English programme guide](https://www.dzs.cz/sites/default/files/2025-11/european_solidarity_corps_guide_2026_EN.pdf)
authorises attributed EU-material reuse, excluding third-party photos.
Its English-precedence statement applies only to versions of that guide.
The English DZS website mirror is stale and is not substituted for current
Czech 2026 conditions.

## Inventory and provenance

The reviewed finite frontier contains 16 identities: four annual 2026 grant
schemes and twelve substantive educational courses. The programme parent,
its grant tabs and participant route, the [2026 call](https://www.dzs.cz/vyzva_sbor),
all eight own events advertised there, both current programme event cards,
and the seven-item ESC NET page establish this scope. The 40 guarded inputs
include those pages, original-language aliases, necessary programme/cost
PDFs, delegated training details and policies. Each successful collection
retrieves the complete guarded frontier; changes to visible HTML facts or
required document bytes fail for review rather than publishing stale facts.
HTML guards cover visible text and all anchor/link destinations, including
canonical URLs, frontier, access and material conditions;
PDF and robots guards cover exact bytes. These guards are deliberately
conservative: benign changes can require a reviewed update.

| Grant identity | Applicant and funding unit |
| --- | --- |
| Solidární projekty | At least five Czech residents aged 18–30; EUR630 per project month, not per-person income; conditional coach/exceptional costs |
| Dobrovolnické projekty | Valid lead Quality Label organisation; project budget, not a paid individual job |
| Dobrovolnické týmy v oblastech s vysokou prioritou | EACEA consortium grant, maximum EUR400,000 per project |
| Dobrovolnictví v oblasti humanitární pomoci | EACEA humanitarian consortium grant, maximum EUR650,000 per project |

National February and October rounds are deadlines within each annual
scheme, not duplicate records. Historical guides/results, participant
capacities, priority themes and budget components do not create awards.
Five one-to-one consultation appointments, two mandatory paired
On-arrival/Mid-term cohorts for already-selected volunteers and two Quality
Label accreditations are excluded. Results, inspiration stories, generic
participant directories, unrelated programmes/TCA and broad external
catalogues are not opportunity records. No forbidden filtered index is
used to expand coverage. This scope does not certify every historical ESC
event or every event published anywhere on the DZS website.

Own courses are the two Solidarity Project webinars, the ESC51 grant
webinar, TOSCA and Od nápadu k projektu. NET advertises Spaces that Empower,
the 2026 ESC MOOC, Co-create space with and for youth, Training for Mentors2,
Volunteering for ALL, Ctrl+Alt+Solidarity and Safeguarding in ESC organisations
in practice. Each NET record preserves its actual delegated detail URL and
organiser; DZS discovery and SALTO attribution remain explicit.

IDs are SHA256 prefixes derived from `cz-dzs` and a stable reviewed
programme key or actual event/detail identity. Original published Czech
or English headings are retained; research labels are not translated
record titles or invented year suffixes. Summaries are factual English
prose, at most 600 UTF-16 units. The source country CZ identifies the
publisher, not every destination. Online courses have no physical host.
All citizenship arrays remain unknown because residence, organisation
establishment and professional affiliation do not prove passport rules.

## Dates, conditions and conflicts

The 2026 guide explicitly states noon Brussels time for the published Czech
18 February and 1 October national rounds: 11:00UTC and 10:00UTC respectively.
The optional May round is not advertised by the Czech agency. EACEA closes
high-priority teams on 3 March at 17:00Brussels (16:00UTC), humanitarian
projects on 23 April at 17:00Brussels (15:00UTC). These clocks are evidenced
by the actual grant guide, not borrowed from accreditation dates.
Currently open SALTO mentor and inclusion courses state `24h UTC` on
11/21 October, normalized to 12/22 October00:00UTC. Other course deadlines
remain date-only; event dates and selection dates are not closing dates.
On a date-only closing day status is unknown, not presumed open all day.
Unstated opening/publication dates remain null. At the 9 October review,
three enrolments remain open and thirteen opportunities are expired;
status is calculated at collection time rather than hardcoded.

Material qualifiers remain in summaries: project funding is not personal
salary; residence is not citizenship; institutional Quality Label roles
matter; national travel support and fees are conditional. Od nápadu is
explicitly free with food, shared accommodation and travel. The ESC51
webinar fee is unstated. TOSCA fee applicability/amount depends on the
participant's agency; accommodation/food are covered, not universally all
expenses. The residential NET courses have their own differing fee and
travel conditions; the online MOOC is free. Volunteering for ALL accepts
newcomers who have never organised ESC volunteering and excludes ordinary
school-pupil projects. Safeguarding's broader country table conflicts with
its programme-country wording, and this uncertainty is retained.

Current Czech 2026 call and guide permit individual volunteering from two
weeks to twelve months; the older hub retains a narrower short-stay clause.
The stale English website gives older amounts and dates; current Czech
2026 and actual 2026 guide evidence supports EUR630/227 and February/October.
The high-priority guide describes 40 project participants as an aim in
principle, whereas the hub calls it a minimum; it is not a minimum per team.
The humanitarian guide says legal residence while DZS says permanent
residence. Solidarity projects primarily take place in Czechia, with
eligible bordering-region exceptions. These discrepancies are disclosed,
not converted into unsupported eligibility or benefit guarantees.

## Shared publisher pacing and durable infrastructure state

This adapter deliberately duplicates its controls locally and runs without
imports from any other adapter. It shares the DZS-family allowance with
`cz-study-in-czechia`: at least six seconds between every publisher request
start, at most ten in a rolling sixty seconds, including robots, redirects
and the single permitted same-URL HTTP503 retry after at least thirty
seconds. HTTP401/403/429 are fatal without retry. Retry-After is persisted;
invalid values block further access and future embargoes never age away.
Refusal diagnostics include the owning input and sanitized actual hop,
never query credentials, response bodies or headers.

A UID-owned private temporary state directory, flock and atomic state writes
serialize local family collectors. Both production workflows use
`youthopps-dzs-publisher-v1` concurrency without cancellation. Complete bound
repository/main workflow history selects the newest completed attempt by
chronology, including failed/cancelled runs and older-ID reruns. Only
advertised total-count drift permits at most three fresh scans; malformed,
duplicate, incomplete or overlapping history fails closed.

The exact newest attempt's `dzs-pacing-state-<attempt>` artifact is required:
bounded inert ZIP/JSON, schema, source/repository/run/attempt and digest
checks; no extraction or execution. Signed downloads strip authentication
and never log signed URLs. Restoration merges maximum embargo, blocked
state by OR and the last ten unique starts, adding a sixty-second startup
margin. An existing family cannot use the first-family bootstrap or older
successful state to recover a missing/expired newest artifact. The legacy
bootstrap is reachable only if there has never been a completed family run.
State retains only timing, never records or secrets. Runner loss, hard
cancellation or artifact expiry can prevent persistence; the next run
must fail closed and requires separately reviewed recovery.

## Bounded lifecycle and publication

The 30-minute matching workflow mints a restricted `DATA_SOURCE_TOKEN`
immediately before the sole application command. No runtime RSA/JWT/App
renewal or additional App-key credentials are needed for this short design.
Real main-thread POSIX alarms enforce a nonrenewable prepublication phase
of 900seconds, publication180seconds, failure reporting180seconds and inert
export30seconds, clipped to an absolute1290second process maximum. The first
phase includes family history/artifact, initial snapshot, robots, sockets,
full reads, backoff, parsing and validation. Socket idle timeouts alone are
not elapsed-time bounds. Slow CAS may exhaust the publication allowance
before all three attempts finish; it exits into a separate bounded failure
phase. The alarm stays armed between phases and during outcome output.
All nonexport work ends by absolute1260seconds, reserving the final30seconds
inside the original1290second bound for trusted export. Timers never reset
on helpers, retries or errors. Workflow timeout
is a last-resort cancellation, not a guarantee of artifact preservation.

Before publisher work, family state is restored and trusted. Publication
validates the complete previous own pair, then uses atomic source-scoped
Git trees/commits and nonforce CAS. A timed-out mutation is reconciled
against the exact desired own pair before any failure-metadata write;
newer/different own data is never overwritten. The first failure creates
no source folder. Later failure preserves last-good data and success
stamp, recording sanitized failure metadata when safe. Invalid/foreign
previous data or metadata cannot authorize a metadata write. Failure
reporting can itself time out and is logged as durable outcome unconfirmed.
Bounded trusted timing export runs in a finally block, including unexpected
errors; export failure does not mask the original error or promise an
artifact. Initial token lifetime remains substantially longer than the
bounded process/workflow, but API availability is never guaranteed.

Only `datas/cz-dzs/{data,metadata}.json` is published. Large existing GitHub
contents use a validated Git-blob fallback. Stable creation/first-seen and
unchanged-update timestamps survive refreshes; checked time follows the
completed successful collection. No aggregate or other source is rewritten.

Run the sole live, nonpublishing test:

```sh
python -B adapters/cz-dzs/test_adapter.py
```

It retrieves actual nonempty records, validates all16 and emits the complete
JSON on stdout; retain it only in memory. It receives no publication token
and does not write data or publish. Production begins only after maintainer
merge through the source-scoped push trigger/manual dispatch and schedule:

```sh
python -B adapters/cz-dzs/adapter.py --publish
```

Collection tests and static review do not prove production publication.
Maintainer source/site Actions and complete delivery checks remain separate.
