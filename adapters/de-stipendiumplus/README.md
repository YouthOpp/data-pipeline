# StipendiumPlus

This standalone standard-library adapter collects eight genuine funding or
training identities from StipendiumPlus's own substantive public pages. The
publisher is the Arbeitsgemeinschaft der Begabtenförderungswerke der
Bundesrepublik Deutschland; its thirteen members administer distinct procedures.
The source is not thirteen cloned provider awards or a central application.

| Identity | Source-backed host projection | Application deadline/status |
| --- | --- | --- |
| Student scholarships | Unspecified; overseas support exists | Null / unknown |
| Doctoral scholarships | Germany, with qualified overseas exceptions | Null / unknown |
| Summer academy 2027 | Bad Staffelstein, Germany | Null / unknown |
| Historical summer academy 2025 | Heidelberg, Germany | Null / unknown |
| CHIN-KoBe | Known modules in Germany, China and Taiwan; nonexclusive | Null / unknown |
| Nie wieder!? | Unspecified venue | Null / unknown |
| Aufstiegsstipendium | Unspecified | Null / unknown |
| Apprentice scholarships/educational support | Unspecified; overseas possibilities | Null / unknown |

Student funding includes €300 monthly independent of income and conditional
maintenance up to €855 monthly considering parental and own income. Recognized
HEI enrollment, formal admission, performance, engagement and member values are
preserved. No Abitur or political-party/church membership is universally required.
Conditional insurance, family/childcare, nonrepayable/taxfree treatment and foreign
study support remain explicit in record evidence. The amounts are per-recipient
benefits, not consortium budgets or guaranteed combined income for every person.

Doctoral €1,750 monthly includes €100 research support, rather than adding it.
Any citizenship is eligible for a doctorate at a German HEI. Reasoned overseas
exceptions require German citizenship **or** an Abitur obtained in Germany;
cotutelle is generally possible. That condition is not a DE-only overall
citizenship whitelist. Record evidence retains degree-delay exceptions, medical
and second-doctorate limits, supervision/engagement, member procedures, side-work
alternatives, child/maternity extensions, prior-support accounting, and exclusions
of double funding, purely educational funding or an additional degree. The normal
3.5-year and child-related 4.5-year maximums are conditional, not blanket promises.

The upcoming academy's 5–10 September 2027 and historical academy's 17–22 August
2025 are **event dates**, not application deadlines or proof of open registration.
The historical edition has its own actually linked dated/located public leaf,
funded-member audience and eleven concrete curriculum groups. Its title and
record evidence clearly identify it as historical. There is no evergreen academy
clone, guessed annual edition, capacity-based award or working-group clone.

CHIN-KoBe is for funded members of the thirteen works and SBB; especially suitable
fields are not exclusive admission rules. Its country projection represents known
programme modules, not every session's complete venue set. Nie wieder's scholar
seminars are distinct from public evenings. Aufstiegsstipendium is a separately
named federal first-degree scholarship for vocationally qualified professionals
with several years of experience; its own substantive paragraph shares the
student-benefits URL. No ordinary student/doctoral rates or nationality/host rules
are borrowed. Apprentice support is a genuine common cohort for people in
state-recognized vocational training with engagement, with member-dependent
scholarships or cooperation routes and mentoring/workshops/exchange. Missing
common amounts or a central application do not erase this real framework.

## Access and bounded source completeness

The adapter reads twenty-one complete own HTML pages: the home, imprint, all five
actual audience/member navigation hubs, student and doctoral benefits/eligibility/
application/FAQ leaves, academy parent and its actually linked 2027/2025 editions,
CHIN-KoBe, Nie wieder and the doctoral representation initiative. This finite
frontier identifies all actual own shared funding/training programmes without an
external SBB/ELES/CHIN-KoBe catalogue, thirteen member-provider crawl, guessed
archive years or sitemap-wide discovery. Member-fit advice, values, scholar
portraits, schools' advice/materials/ambassador outreach and the elected doctoral
representation initiative are not separate opportunities. The trainee page's
substantive framework and repeated Aufstiegsstipendium paragraph are handled
separately and deduplicated.

Material article facts, complete own-route inventories, canonical public source
URLs and policy navigation are reviewed fingerprints. Missing or changed required
pages, new routes or rights links fail closed for review; collection never
silently publishes a partial subset. Both apex and `www` links count as the
same publisher frontier; an unreviewed `www` navigation alias fails closed
rather than silently disappearing or changing canonical record URLs.
Images, scripts, styles, PDF media and
personal contact presentation are not opportunity facts. They are neither fetched
as alternative access nor copied into records.

The public robots policy disallows `/wp-admin/`, with an AJAX exception; the
adapter uses ordinary permitted public HTML rather than an invented AJAX/private
endpoint. Normal identifying User-Agent, inherited proxy and verified TLS remain
in use. Shared publisher-family locks enforce starts at least six seconds apart
and at most ten in a rolling minute across endpoints, redirects, retries and
concurrent local runs, including `www` aliases. Retry-After is honored. Production
runs are also serialized by the common publication Action group; separate machines
must not overlap outside that scheduling arrangement.

The imprint protects copyrighted works and requires written consent for uses
beyond copyright limits; it permits copies only for private noncommercial use.
This is not an open licence or a stated blanket ban on independently authored
facts. Output contains original English factual summaries, attribution and source
links, with necessary numeric eligibility/event facts. No publisher descriptions,
images, raw HTML, PDFs, personal contact details or applicant information are
published. Changed access or rights evidence requires review.

## Execution and publication

Run the sole actual live nonpublishing collection test with:

```sh
python -B adapters/de-stipendiumplus/test_adapter.py
```

It validates nonempty real records and emits the complete JSON on stdout, with
diagnostics on stderr. It receives no publication credentials and creates no
record output files. The two Python files run when copied to an independent
folder. Source notes are confined to this folder; the repository root README is
unchanged.

The main-only source Action executes only:

```sh
python -B adapters/de-stipendiumplus/adapter.py --publish
```

Atomic non-forced publication touches only
`datas/de-stipendiumplus/{data,metadata}.json`. First failure creates no source
folder. Later failure preserves last-good data and success time, with failure
metadata when possible. A sibling-source conflict may be rebased; a newer update
to this source is never overwritten. Live tests and static checks do not certify
production or live-site appearance; the separate authorized maintainer performs
merge, source/site Actions and complete published-record verification.
