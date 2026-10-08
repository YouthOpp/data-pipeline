# Fulbright Poland

Collect the Polish-U.S. Fulbright Commission's current programme inventory from
[programmes to the USA](https://fulbright.edu.pl/stypendia-do-usa/) and
[Americans in Poland](https://fulbright.edu.pl/amerykanie-w-polsce/), including
the linked own-publisher programme pages and named partner awards. The assigned
[Junior Research source](https://fulbright.edu.pl/junior-research/) remains the
record source identity. This is programme/call coverage, not a news archive or a
worldwide Fulbright catalogue.

## Access, licence and attribution

The public [robots policy](https://fulbright.edu.pl/robots.txt) permits these
routes; `/wp-admin/` remains excluded. Each live collection checks robots and
each redirect before following it. No application portal, authentication,
CAPTCHA, PDF, form submission or restricted route is needed.

The publisher footer states that, unless otherwise marked, website contents
are available under [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/).
Collection checks that the licence statement remains present. Attribute the
Polish-U.S. Fulbright Commission, link the original programme and licence, and
use the output noncommercially under that licence. This adapter supplies factual
metadata and its own short summaries. It does not reproduce full explanatory
articles, images or third-party content. Linked US Scholar partner titles and
programme facts come from the Commission's own inventory; the external partner
pages are original destination links, not an externally crawled dataset or a
claim that their full content shares this licence. No permission request was
submitted. Published source metadata includes `license`, `license_url`, original
Commission attribution, and `changes` identifying factual selection,
normalization and YouthOpps-written summaries, so recipients receive the
licence and modification indication alongside the source records.

## Complete programme inventory and distinctions

The reviewed inventory contains 29 genuine programme/call records:

- Fourteen own-publisher detail pages (18 records after the five named
  Schuman variants replace their parent overview): Graduate Student, Notre Dame LL.M,
  Junior Research, Stanford Research, Polish Scholar, Polish Studies, STEM
  Impact, Scholar-in-Residence, outgoing Schuman, BioLAB, TopMinds, and the
  institutional ETA, Specialist and Inter-Country calls.
- Five incoming individual programme sections: US Scholar, US Student, English
  Teaching Assistant, Specialist and US-citizen Schuman.
- Six explicitly named US Scholar partner awards at Polish host institutions.
  These are real separately named programmes, not clones of funded places.

The general US Scholar programme remains distinct from its named partner
awards, as published by the Commission. Incoming individual ETA/Specialist
opportunities and Polish host-institution applications have different applicants
and original section/detail links. Institutional ETA, Specialist, Inter-Country and Scholar-in-Residence use
`kind=institutional-grant`; they are not direct student applications. A US
university submits Scholar-in-Residence requests; Polish nominee recruitment
occurs conditionally if the administrator asks the Commission for candidates.
No current individual nomination call is claimed. Incoming undated programmes,
named partner programmes, TopMinds and the five Schuman variants use
`kind=programme-overview`; current dated rounds retain `kind=opportunity`. BioLAB is one
internship programme across four host institutions. Counts of scholarships,
visits and host places never produce additional records.

Discovery uses the two official inventories' actual linked programme paths.
Programme URLs are normalized to the reviewed canonical own detail paths;
`graduate-student-award` navigation aliases do not produce duplicates. The
reviewed Program TopMinds navigation links topminds.pl; its substantive local
Commission explanation is requested through `/topminds/`, which redirects to
the historical canonical `/topminds-2025/`, without external crawling. Its
programme information is retained as an overview; no 2026 recruitment round is
invented.
Each
programme page's current title and conditions are read independently. There is
no programme-list pagination on these inventories. News, previous results,
webinars, alumni stories, fundraising, application systems, language duplicates
and the global US awards search are excluded. Incoming programme and six partner
facts are already substantive on the Commission's page, so no external global
catalogue crawl is performed. Unknown dates do not remove real programmes.

As researched on 8 October 2026, six calls are open, four are closed, and nineteen
have unresolved yearless/undated calendars. These are observations, not stored
records or fixed expected status counts. All programme data is retrieved live.
The public outgoing overview still says STEM closes 31 October, while the actual
2027–28 STEM programme page explicitly says **30 October 2026 at 15:00 Polish
time**. The current detailed programme notice is authoritative; the adapter does
not copy the stale overview date. Graduate/LL.M/Polish Scholar remain genuine
2027–28 expired calls; planned 2028–29 recruitment is not invented as a new call.

## Dates, eligibility and identity

Explicit Polish closing day/month/year is preserved as `YYYY-MM-DD` when no
clock is given, such as the institutional Specialist deadline, 8 January 2027.
A stated clock with “Polish time” uses `Europe/Warsaw` for that actual date:
13 October 2026 at 15:00 becomes 13:00 UTC, while 30 October at 15:00 becomes
14:00 UTC after the seasonal offset changes. BioLAB's 1 February 2027 at 15:00
also becomes 14:00 UTC. No midnight or end-of-day cutoff is invented. Complete
current notices take precedence over older FAQ/calendar text. Date-only records
are unresolved on the closing day; explicit closed notices remain expired.

Yearless annual windows in Schuman and Inter-Country and the yearless TopMinds
October deadline retain null deadlines and unknown status. A linked historical
edition is not treated as a dated current call. Scholar-in-Residence has real
programme/eligibility facts but delegates dates to its administrator; its dated
status remains unknown.

Polish or US citizenship requirements are separated from physical host country,
institutional affiliation, residence and work/visa authorization. Polish
citizenship is required where the detail actually says so; dual Polish-US
citizenship and US permanent residence exclusions are preserved in the summary.
BioLAB explicitly does not require Polish citizenship, but excludes US citizens
and US permanent residents. Applicants must maintain active Polish
master's/doctoral student status; non-Polish applicants must additionally have
completed a bachelor's degree in Poland. Institutional host calls
do not gain a citizenship restriction. Four named outgoing Schuman variants require EU citizenship, represented by
the 27 EU country codes. The separate NATO Security Studies variant requires
NATO citizenship and a PhD; US citizenship/dual-US citizenship and US permanent
residency are excluded. Its eligibility therefore lists 31 NATO countries
without the US. Each actual named variant has its original heading anchor.
Innovation explicitly describes research in one or two EU states, so no
specific host-country list is invented; US-citizen Schuman research in
the EU has no specific host-country list, so none is invented. TopMinds' Polish
institution/student description does not establish a citizenship restriction.

Stable identifiers use the first 24 SHA-256 hexadecimal characters of
`pl-fulbright-poland|<programme URL>`. US Scholar programme facts preserve PhD/terminal degree and 3–10 months. US
Student requires a bachelor's degree before the grant, before PhD, and a host
invitation letter; ETA requires a bachelor's, before PhD, and explicitly needs
no invitation letter.

Incoming sections use stable original
publisher anchors; partner awards retain their original individually linked
URLs. Award capacity, order, deadlines and academic-cycle title changes do not
change identity. Source URLs in classification evidence provide provenance.

## Test, schedule and publication

```sh
python -B adapters/pl-fulbright-poland/test_adapter.py
python -B adapters/pl-fulbright-poland/adapter.py --publish
```

The only test performs real non-publishing collection, validates all records,
and prints the complete JSON list to stdout; diagnostics go to stderr. It uses
no mocks, fixtures, output files or publication credentials. The matching
workflow publishes on its own adapter/workflow changes merged to `main`, manual
dispatch, and every six hours (UTC), with repository/main guards and a source
App token. Its sole application command executes this adapter with `--publish`.
Production Actions are not implementation tests.

Robots, pages, redirects and retries share at least six seconds between request
starts, at most ten starts per rolling minute, and persistent `Retry-After`
backoff. File locks serialize overlapping local collections and pacing state
across processes. Separate machines/production runs must still coordinate the
publisher allowance; Actions serialize publication globally.

Success atomically replaces only
`datas/pl-fulbright-poland/{data,metadata}.json`. Later failures preserve last-good
records and the success timestamp, recording failure metadata when safe.
First-run failure creates no published source folder. Failure to persist status
is explicitly reported. The source README is local; root README is unchanged.
