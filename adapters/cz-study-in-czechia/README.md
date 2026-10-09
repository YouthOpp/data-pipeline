# Study in Czechia

This standalone adapter publishes original English factual summaries of
scholarships, training and internships advertised by Study in Czechia/DZS.
Its reviewed catalogue contains 282 candidate IDs, partitioned into 269
opportunities, twelve nomination-channel aliases and one excluded offer.
University quotas, course options and annual call rows do not create extra
records. The excluded Latvian staff offer (ID 28) has no substantive offer
conditions after reviewing its English detail, selected call and Czech detail.

The finite catalogue includes 60 incoming candidates, 222 outgoing candidates
and zero results under the separately published compatriot controls. Genuine
compatriot programmes discovered through the incoming catalogue are retained.
The ordinary server-rendered pagination repeated its first page; the adapter
uses the exact anonymous navigation endpoints advertised by the publisher.
It validates all thirty page states, scope controls, advertised totals, current
page numbers, terminal navigation and the complete unique ID union.

The source uses nonunique canonical tags which omit `id` and `callId`. Original
programme query URLs remain the record links and stable identity evidence;
canonical tags are guarded separately. Stable identifiers use the programme
representative, allowing the selected annual call to change without changing
an opportunity's identity. Every nomination channel retains its specific
conditions and calendar. Sending-university affiliation, language requirements
and application-country fields are distinct from passport requirements.
Positive country arrays describe explicitly established facts and can be
nonexclusive; the factual evidence explains alternative affiliation routes.
Host countries describe supported physical locations, not service-market areas.

## Reviewed material and limits

The runtime checks 282 English programme pages, 280 selected call pages, 126
necessary public Czech programme details, thirteen policy/discovery pages,
thirty catalogue states and thirteen public PDF/XLSX documents. Six robots
policies are checked by transport: 750 logical inputs before redirects. Current,
nearest future or latest historical calls are selected according to the actual
source calendar; annual rows remain provenance, rather than additional records.
The required documents include government Guidelines/FAQ/eligible fields,
Barrande, Vietnam, historical Korean guidance, DAAD and Bavaria material.
Document editions and conflicts with later portal rows remain explicit.

The Hungarian full-degree offer (332) is a limited programme overview using
independently published DZS/Studujnavs facts. Complete provider eligibility is
unverified. Its linked Tempus manual is excluded because the provider's
[usage terms](https://tka.hu/hasznalati-feltetelek) require written permission
for the described material use on another website. Permission can be requested
through the verified official contact `info@tpf.hu`, describing automated
factual collection and public delivery. No request has been sent. See the
[permission and scope note](https://github.com/YouthOpps/data-pipeline/issues/55#issuecomment-6079886467).
No Tempus document, portal or policy is a runtime input.

CzechInvest (280) is an individually agreed internship framework with conditional
home-university Erasmus support; it does not promise a particular placement,
host country or stipend. Other placements retain their specific duties,
language/degree/citizenship requirements and housing costs. A future portal
closing does not override a historical host interest date or stay period;
late agreements depend on the actual host. Date-only closings remain date-only,
and their closing day has unknown status. Unverified clock zones are not guessed.

## Integrity and access

All required HTML visible text, canonical identities and advertised links,
including meaningful queries and external material links, are conservatively
fingerprinted. Scripts and styles are excluded; visible `noscript` text is
retained. New eligibility, funding, calendar, programme or legal content fails
closed. Personal/editorial text is only checked locally for integrity and is
never included in published records. Because the guard is conservative, even a
benign public biography or presentation change can require a fresh review.
Required PDF/XLSX inputs must match the exact reviewed SHA-256 and pass complete
format/size checks. They are not uploaded or redistributed by the adapter.
No third-party parser is needed in production.

Requests use an identifying user agent, normal TLS validation and the inherited
proxy/CA configuration. All publisher and delegated-material origins share one
budget: at least six seconds between starts and at most ten starts in each
rolling sixty-second window. Redirects and robots requests consume that same
budget. Refusals, challenges and unavailable required policies fail closed;
embedded public application fields are not submitted. Published anonymous
catalogue navigation is read without account operations. No private API,
registration, application submission, contact operation or external catalogue
is used. Privacy policies are not treated as an open licence. Output contains
factual summaries, source attribution and original links, without copied full
prose, images or personal contact details.

## Execution and durable pacing

The script uses only the Python standard library and can run independently:

```sh
python3 -B adapter.py
python3 -B adapter.py --publish
python3 -B test_adapter.py
```

The sole unittest performs one complete nonpublishing collection and emits the
full validated JSON to stdout. It does not write an output dataset or receive
publication credentials. A complete collection has a baseline above 75 minutes;
the workflow and live verification allow 180 minutes, subject to measured runs.

Required public inputs receive one retry only after HTTP 503, using the same
published URL, identifying agent, TLS/proxy and robots policy. The retry waits
at least thirty seconds and honors any longer persisted `Retry-After`; a
blocked state or insufficient remaining collection budget refuses access.
Other statuses, including 403 and 429, and transport/parse errors are not
retried. A final HTTP refusal identifies the input key and public origin/path
without response bodies or query strings. The first actual nonpublishing run
failed after 3726.598 seconds with HTTP 503; its cause remains unknown and it
did not produce a validated whole-source result.

The workflow serializes the DZS publisher family with cancellation disabled.
It does not block unrelated publishers behind this long collection. Local
processes share an owned, locked temporary family budget and reject overlapping
collectors. Local state alone does not survive a fresh GitHub runner.

For GitHub Actions, the adapter restores the newest completed family attempt,
including failed or cancelled attempts and older run IDs that were rerun later.
It validates repository/main/workflow/attempt ownership and chronological state.
History pagination attempts at most three complete scans, restarting from
page one only if the advertised total changes. Partial candidates are discarded.
Positive integer run IDs must be unique across all pages, including unrelated
runs. Duplicate IDs, malformed/oversized pages, API errors and untrusted
bindings fail immediately. This bounded response to concurrent run creation
is not transactional snapshot isolation; repeated inventory changes fail
closed. Artifact selection and recovery rules are unchanged.

Only one bounded inert JSON member is accepted from the authenticated artifact;
no paths are extracted and no downloaded code is executed. Signed download
URLs are redacted and receive no GitHub authorization. A future `Retry-After`
embargo is never discarded because the state is old. If the job cannot wait,
it fails before publisher access and preserves the embargo for upload.

The infrastructure upload is best effort. Cancellation or runner loss can
prevent it. Missing, expired, corrupt or mismatched expected state fails closed;
the adapter never falls back to an older successful artifact. Recovery then
requires maintainer review rather than a silent pacing reset. The existing code
has no later recovery override: resumption requires a separately reviewed
recovery change bound to that exact preceding attempt. Initial bootstrap cannot
replace this missing state, and an older artifact cannot substitute for it. Artifact retention
is ninety days; an idle gap beyond retention also requires reviewed recovery.
First-family bootstrap is accepted only when complete authenticated history
proves there is no preceding completed family run. The maintainer supplies the
nonsecret `DZS_PACING_BOOTSTRAP` JSON with schema `1`, the exact family name,
a bounded `not_before` timestamp and an issue/PR evidence link. It cannot replace
missing state from a later run. Initial bootstrap will be set by the maintainer
after final live verification; this adapter does not mutate repository settings.

Publication uses the existing GitHub App credentials in memory and an
installation ID from the repository-scoped token action. A standard-library
RS256 signer renews the data-source-only contents-write token before expiry;
private keys and tokens are never written or logged. The initial source snapshot
is retained across collection to prevent overwriting a newer own-source result.
The data/metadata pair is committed atomically using non-forced compare-and-swap.
Sibling-source changes can be rebased safely; newer own-source changes are
preserved. A first failure creates no source folder, and later failures retain
last-good data and successful timestamps.

Implementation and offline verification do not establish live publisher access,
production publication or website delivery. Those stages require their separate
full collection and post-merge checks. Research and scope evidence is recorded
in the [issue milestone](https://github.com/YouthOpps/data-pipeline/issues/55#issuecomment-6080296712).
