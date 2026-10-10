# Bosch career programme overviews

This adapter covers 18 independently reviewed student and graduate programme
identities on permitted official Bosch pages. It is a bounded programme overview,
not the worldwide job inventory. Current vacancies, enrolment and deadlines are
not established: every overview has unknown status and no application deadline.

## Coverage and identity

Global Junior Managers Program; six India programmes; Singapore Junior Managers
Program, PhD, internships and final theses; one shared students@bosch identity;
three US graduate/internship/co-op programmes; Canada's Graduate Specialist
Program; Africa's Graduate Experience Program and South African learnership
framework. The last two are named frameworks, not invented current courses.

Country JMP pages repeating generic global text are aliases. India and Singapore
have distinct entry terms. US internship and co-op sections share a page but have
different stable identities. students@bosch conditionally considers STEM students
and can invite strong interns; this does not establish a universal STEM-only rule.
Host-country information is separate from nationality eligibility. The US graduate
programme requires indefinite US work authorization and offers no future
sponsorship; citizenship is not inferred.

Known English job/country routes disallowed by robots are excluded without an
identity or alternate-language workaround. Japan's robots route returned 302 to a
404 page; no body or redirect was followed, and the programme remains excluded.
One premature prohibited jobs request during research was retained unused; its
body supplies no programme facts. Ordinary copyright reservations provide no
licence; the adapter uses short original factual summaries and direct attribution,
without copied protected prose, images or vacancy content.

## Collection and safety

The fixed finite frontier has 49 nominal physical requests: 27 full accepted
programme, parent and legal documents; six allowed-origin robots files; sixteen
excluded-country robots files only. Full visible prose, links and controls are
fingerprinted. Material, entry-condition, legal or frontier change fails the whole
collection for review instead of publishing stale recipes or a partial subset.
Every origin's fresh robots is inspected before a dependent document.

Standard-library-only operation uses normal verified TLS and inherited proxy/CA
configuration. One persistent Bosch publisher family serializes all endpoints,
robots, tests, production starts and redirects: at least six seconds between starts
and at most ten in any rolling sixty seconds. Each physical attempt, including
response closure and errors, preserves its completion plus the native interval
as the next-start boundary; a monotonic gate enforces elapsed time. The exclusive
collector preserves a sixty-second startup embargo and all stronger intervals.
Refusal and Retry-After restrictions
are durable and sticky. Legacy research inodes remain read-only and locked;
modern/legacy restrictions combine with maximum embargo/interval and logical-OR
refusal. Historical eleven-entry research state is preserved honestly and migrated
with a conservative sixty-second hold, never reset.

Collection is capped at 64 starts, 8 MiB per response, 64 MiB overall and 900
seconds. Publication is capped at 900 seconds, pacing export at 120 seconds, and
the whole job at 2520 seconds. Exhaustion fails; no truncated collection succeeds.

## Running and publication

Run the sole actual nonpublishing test with
`python -B adapters/de-bosch/test_adapter.py`. It emits the complete actual JSON
on stdout, diagnostics on stderr, and writes no normalized dataset. It requires
no publication credentials. Static/offline checks do not establish a live pass.

Production uses only `python -B adapters/de-bosch/adapter.py --publish` after merge.
It atomically publishes only `datas/de-bosch/{data,metadata}.json` in data-source.
A first failure creates no folder; later failures preserve last-good data and
successful timestamps while recording safe failure metadata when possible.
Unchanged substantive records retain their update timestamps; last-seen uses
actual collection start and last-checked uses completion. Current availability
remains unknown rather than falsely marking these programme overviews open.

The latest completed family attempt's owned inert pacing artifact is mandatory.
Missing or refused state cannot fall back to a fresh allowance. Bootstrap is
intentionally unset here: root must bind an exhaustive first-family history audit
and reviewed concrete embargo before any production release. That bootstrap is
valid only for the first family run and cannot override a refusal or stronger
interval. The workflow saves inert state after every attempt.

## Review evidence

Issue: https://github.com/YouthOpps/data-pipeline/issues/119 . Complete precode,
all 18 full-field factual recipes, full metadata/current-consumer memory proof,
country exclusions and legacy preservation were independently accepted. Source,
offline adversarial checks and two separate actual collections still require
independent validation before PR/release; production and website visibility require
post-merge evidence. No live collection or publication is claimed by this file.

Pacing artifacts are exported only after healthy durable state has been verified. The uploader requires a positive output from the current collection step, using GitHub Actions’ fresh per-step output file. A failed completion-state write cannot authorize upload of an older artifact.
