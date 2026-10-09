# Wallonie-Bruxelles International

This independently runnable, standard-library adapter collects WBI's own
published funding, training and paid-placement facts. It does not collect
applications, images, external provider catalogues, account pages or search.

Run the sole non-publishing live test with Python 3.12:

```sh
python -B test_adapter.py
```

The complete actual JSON goes to stdout; diagnostics go to stderr. Publication
is limited to the matching authorized main-branch Action and source folder.

## Reviewed inventory and identity

The education catalogue has two pages (10 and 5 cards). Four research taxonomy
indices supplement it, giving 18 distinct own service leaves. Three advice or
networking services are excluded because they offer no own award. The resulting
21 genuine identities comprise 15 programmes, plus two additional WORLD award
types, incoming/outgoing Mobility Fund routes, an additional Excellence IN type,
the separately named Québec tuition exemption and the current SEE internship.

WORLD doctoral, postdoctoral and short awards have distinct published durations,
renewals and lifetime entitlements despite sharing the same actual URL and form.
Excellence IN long/short awards similarly have distinct award entitlements.
Mobility Fund IN/OUT are institutional grants: FWB institutions submit and receive
funds; incoming foreign staff are beneficiaries, not direct applicants. The
outgoing permanent FWB employment requirement is not a citizenship restriction
on incoming beneficiaries. PHC Tournesol is also institutional cooperation.

The company and Louisiana articles supplement their service pages; they do not
create duplicate awards. The named SEE internship differs from the CIRC pilot
postdoctoral scholarship. Country variants, three Hague places, programme
capacity, yearly rates, language aliases and external directories are not
separate opportunities. The obsolete language alias returns 404; the substantive
current own language leaf is the reviewed source.

## Eligibility, destinations and benefits

FWB affiliation, qualifications and residence are not Belgian citizenship.
Only the separately named Québec fee exemption has explicit Belgian eligibility.
Host countries come from actual programme destinations. WORLD, EXPLORT and the
nonexhaustive international-organisation programme have no artificial complete
country whitelist. Company internships are abroad: WBI says EU27 priority but
includes Belgium, so the known projection contains the other 26 countries; the
source inconsistency and unspecified conditional border destinations remain in
record evidence. Destination countries never become citizenship eligibility.

WORLD's €2,120/month and CIRC's €3,000/month are mobility scholarships, not salary.
WORLD's conditional registration/training limit is €835. WORLD parenthood
extensions are 15/12 months under the stated circumstances; CIRC separately uses
one year per childbirth/adoption. Excellence IN permits pending PhD completion
with timely proof; its July/December proof dates are not application deadlines.
PHC supports up to €2,000/year for two years with published expense conditions.
Hague applicants must have completed some study at an FWB-funded institution.

The Louisiana source conflicts on one-year residence (FWB versus Belgium),
preserved in evidence. Quoted salaries belong explicitly to 2026–27; they are
not presented as a verified 2027–28 pay schedule. Company support is monthly,
with 50% travel support capped at €500. Its current call's research/doctoral
exceptions are preserved rather than inferred to apply to all programmes.

## Dates and change protection

Three actual dated calls close November 1, November 13 and December 31, 2026.
Their raw 23h59/midnight wording lacks a time zone. Deadlines therefore remain
calendar dates: open before that date, unknown on the date, expired after it.
The consumer's date-only sorting convention does not establish a publisher UTC
closing instant. Hague's January 31, 2026 call is retained as expired.

The other 17 records have no actual call year and remain unknown with null
normalized deadlines. Annual day/month windows, even those specifying UTC+1 or
UTC+2, do not justify inventing a year or closing clock. Event registration,
interview dates and pending-degree proof dates are not award deadlines.

Every required catalogue page, leaf, current article and legal article must be
complete and match the reviewed factual fingerprint. Changes fail closed for
review; no source-year rollover or partial inventory is silently published.
Only the publisher's transient `Last edited il y a N minutes/heures/jours` UI
text is removed before comparison; surrounding substantive text remains.

## Permitted access and publication

[WBI's legal notice](https://www.wbi.be/fr/mentions-legales-0) permits attributed
textual/numerical information for noncommercial, nonadvertising use. This is not
an unrestricted open licence. Own English factual summaries and source links
are published with WBI attribution; images, framing and full prose are excluded.
Commercial/advertising use or other protected elements require prior permission
from `multimedia[@]wbi.be` under the actual notice. No publisher contact is sent.

Robots is checked before each endpoint, including redirects. All WBI endpoints
share one cross-process lock and rolling allowance: at least six seconds between
starts and at most ten starts per rolling 60 seconds, including retries and
redirects. Retry-After is honored. The identifying user agent, normal TLS and
session proxy remain enabled. Remote publishers must not overlap locally run
WBI tests; the publication Action uses the global serialized source group.

Atomic, non-forced commits update only this source's data/metadata pair. First
failure creates no source folder. Later failure preserves the last good data and
success timestamp, recording sanitized failure metadata when safe. Concurrent
sibling commits can be rebased; newer data from this same source is never
force-overwritten.
