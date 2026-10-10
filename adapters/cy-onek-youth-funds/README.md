# ONEK youth funding opportunities

This standalone adapter collects genuine funding rounds advertised by the
[Youth Board of Cyprus (ONEK)](https://youthfunds.onek.org.cy/en/), starting from
its [Youth Initiatives programme](https://youthfunds.onek.org.cy/en/services/youth-initiatives/).
It also covers Green Volunteering, Youth and the World, and one genuine historical
funding information session. It does not treat programme menus as opportunities.

## Access and rights

Research on 10 October 2026 used normal HTTPS verification and an identifying
YouthOpps user agent. The actual funding site's robots policy permits required
public routes; the WordPress administration exclusion does not cover them.
The publisher's linked homepage and actual privacy policy were checked separately.
Copyright is reserved; no open licence is claimed. Original factual summaries,
official titles, attribution and canonical links are used, with no logos, protected
full prose, images, applicant information or recipient lists. Guide requirements
for approval of beneficiaries' logo use do not authorize logo republication here.

The application portal is linked for applicants; its public root/robots routes
returned an Angular shell. Collection does not log in, submit applications, fetch
private portal APIs or follow applicant/bank forms. Linked public guides are read
as material evidence, not assumed to be current call-specific amounts.

## Inventory, identity and exclusions

The reviewed frontier has 42 material inputs: 36 HTML pages, four public PDF
routes and two public event-listing JSON responses. Two origins' robots checks
bring the ordinary physical request count to 44. The Greek and English World
guide URLs return identical 2024 PDF bytes; they are evidence aliases, not awards.

Fourteen semantic identities comprise four Youth Initiatives rounds, five Green
Volunteering rounds, four Youth and the World rounds and the June 2024 information
session. They retain literal English publisher titles and links. Stable IDs hash
the source slug and actual programme/edition/round identity, so multiple genuine
rounds sharing a programme URL remain distinct. Joint Green 2027 A1/A2 is one
jointly titled call with two implementation tracks, not duplicate awards.

Two actual news pages contain 17 items and exhaust the advertised frontier.
Seven call notices and two extension notices provide current/historical rounds.
Beneficiary/results/evaluation news is excluded. The presentation invitation and
subsequent report describe one event; the software-launch report is retrospective.
Funding-use categories, age groups, grant bands and capacity are not records.

The public calendar's actual frontend supplies a current nonce and public
`/en/em-ajax/get_listings/` POST controls. Page 1 contains ten exact event URLs;
page 2 explicitly returns no events. All ten leaves contain an explicit Greek
test description, without substantive genuine applicant information; they are
excluded. Collection rechecks both actual listing responses and every leaf.
Changed facts, new genuine entries, changed filters or a nonterminal frontier
fail closed for review rather than publish a silent partial inventory.

Material fingerprints retain complete main content, substantive links, exact
footer text/links and pagination controls. Encoded contact masks are bound to
their decoded target hashes, never exported. Only the exact validated public
event-view counter and current nonce tolerate rotation; funding facts and dates
remain guarded. PDF bytes are bound exactly.

## Dates, eligibility and benefits

Application deadlines come from actual published rounds. Activity dates and event
dates never become application deadlines; the historical session's actual RSVP
deadline is retained. Closing dates with no stated timezone remain date-only,
including the old Green notices with unzoned clocks. On a date-only closing day,
exact status is unknown. Published opening dates keep future rounds unknown until
the application window starts, irrespective of generic Open badges.

Current Greek Green tables explicitly identify 2027 and resolve the stale English
heading. The 2024 Green guide reverses the current numbering of innovative and
multi-action projects; older amounts are not mapped onto current numbered actions.
The guides' age cutoffs differ from the current FAQ/HTML. World lawful permanent
Cyprus residence over six months is not a citizenship restriction. Confirming
current caps, age conditions and permitted activity remains necessary.

Youth Initiatives explicitly hosts activities in Cyprus; the information session
has an actual Cyprus venue. Green destination countries and international World
destinations are left unspecified. All passport eligibility arrays are empty.
Summaries explain eligible applicants and benefits without invented amounts or
nationality exclusions. English source titles and authored summaries are labelled
`language=en` and `summary_language=en`; Greek evidence is not an extra record.

## Standalone commands and publication

```sh
python -B adapters/cy-onek-youth-funds/test_adapter.py
python -B adapters/cy-onek-youth-funds/adapter.py
python -B adapters/cy-onek-youth-funds/adapter.py --publish
```

Python standard library only. The sole live test performs one complete collection,
validates fourteen nonempty records, and emits their complete JSON to stdout.
Tests receive no publication credentials and write no collected dataset files.

`--publish` requires `DATA_SOURCE_TOKEN` scoped to `YouthOpps/data-source`, plus
workflow identity/token for trusted publisher pacing handoff. It atomically writes
only `datas/cy-onek-youth-funds/{data,metadata}.json`, with bounded conflict-safe
retries and readback. First-run failures create no source folder. Later failures
preserve validated last-good data/success time and attempt a failure metadata write.
Invalid or ambiguous prior pairs fail closed before collection or writes.

The persistent `youthopps-onek-publisher-v1` allowance serializes overlapping
collectors across both origins, with at least six seconds between starts and at
most ten starts per rolling minute. Redirects/retries/robots share the allowance.
Header-first Retry-After and access refusals persist embargo/block before bodies.
Unreviewed routes, redirects, robots restrictions and challenges stop collection.
Local state is never reset; production restores the newest trusted completed
family attempt, including failures and cancellations, without older fallback.
The reviewed [first-family bootstrap marker](https://github.com/YouthOpps/data-pipeline/issues/52#issuecomment-6095335317)
binds the absent-family history review and earliest production start. Once any
family run exists, its newest completed-attempt artifact is mandatory; the marker
does not permit replacing missing artifacts, embargoes or blocked state.

The prepublication absolute limit is 900 seconds, global 1,290, operation alarm
1,260, publication 180, failure reporting 180 and trusted state export 30.
Publisher/API sockets are bounded to 60/15 seconds and the Action job to 30 minutes.
The conservative 44-start production pacing floor is 318 seconds. A one-hour
publication App credential is created immediately before collection; the global
deadline leaves ample credential margin. Collector locks remain held through the
bounded final trusted export. The workflow runs on matching main pushes, its
schedule and manual dispatch; production execution belongs to the maintainer.
