# IBM early-career programme information

This adapter covers five substantive programmes advertised by the official [IBM careers site](https://www.ibm.com/careers): internships, apprenticeships, co-op, the Entry-Level Program and the Sales Accelerator Program. It publishes original factual summaries and the literal English programme-page titles. These are programme overviews with unknown current availability and no application deadline; they do not represent a complete worldwide vacancy inventory.

## Programme boundaries

- Internships: degree-pursuing students, real project work, mentoring and learning resources. The supporting co-op comparison describes 12-week summer internships; this is not a universal duration for all global internships. Employee testimonials do not establish current host countries or applicant eligibility.
- Apprenticeships: full-time earn-and-learn for people without a four-year bachelor's degree in the field pursued who have domain knowledge. No numerical pay, universal programme duration or country-specific admission rules are asserted.
- Co-op: 16 weeks, full-time and on-site at an IBM office, for enrolled college/university students in software or sales. A subsequent permanent role depends on performance and an offer.
- Entry-Level Program: global structured onboarding, training, certifications, hands-on work and mentoring, for recent graduates, career switchers and nontraditional backgrounds. The less-than-one-year experience description is typical, not a universal hard cutoff. Named occupational areas are not separate invented vacancies.
- Sales Accelerator: fresh graduates, two tracks and a two-year growth plan. The Sales track explicitly includes an assignment in Valencia, Spain, or Dublin, Ireland; these physical destinations do not imply citizenship eligibility or the location of every programme role. Client Engineering and Sales remain one advertised programme, not cloned vacancy calls.

The US publisher country identifies IBM, not a passport requirement. All nationality eligibility lists remain empty. Actual location, pay and selection conditions depend on individual vacancies that this source does not collect.

## Finite official frontier and access

Ten literal own-site HTML inputs cover the careers homepage, its entry-level and internship hubs, all four advertised programme cards, the internship explanatory article, the distinct Sales Accelerator child of the entry-level guide, and the linked legal pages. Each input guards complete visible text, literal links, canonical references and public form/frontier controls. No form is submitted. There is no programme-index pagination on this bounded informational frontier. Careers advice, employee stories, generic occupational descriptions, learning directories, talent-network registration and job alerts are excluded.

The public vacancy-search page rendered an empty client-side shell. Its declared API's robots route redirected to the homepage, so no usable API policy or complete vacancy pagination was established; the API was not called. This is a limitation of the separate vacancy-inventory route, not a claim that all IBM access is prohibited. No guessed API, private applicant route or external ATS substitution is used.

The actual [IBM robots policy](https://www.ibm.com/robots.txt) permits the reviewed careers and legal paths. The [terms](https://www.ibm.com/legal/terms), reached through the actual careers footer, expressly permit robots-compliant crawling. The reviewed scope is noncommercial original minimal facts and official links; this is not a blanket licence to reproduce creative content, images, visitor information or submitted tools.

## Timing, failure and publication

The preserved `youthopps-ibm-careers-publisher-v1` family coordinates all publisher requests, robots, retries and redirects: at least six seconds between starts and at most ten starts in any rolling minute. Existing embargoes, refused-access state and timing history are retained. A sixty-second startup embargo applies after restoration under the exclusive collector lock. Each physical attempt, including failure and response closure, persists its completion plus the stricter native interval. A monotonic gate protects the actual opener from reservation and filesystem timing variation. Ten HTML inputs plus one fresh robots request have an eleven-start nominal floor of 120 seconds. Hard bounds are 100 physical starts, 8 MiB per response and 64 MiB across publisher response bodies. Known HTTP 401/403/429 refusals persist Retry-After and block state before body reading; unreviewed redirects stop before following their destination.

True POSIX alarms enforce a single 1,290-second run: prepublication at most 900 seconds, publication and conditional failure handling at most 180 seconds each, with the final 30 seconds reserved for trusted timing export. Nonexport work is clipped at the original 1,260-second deadline. Socket timeouts do not replace the wall-clock bound. Failure reconciliation protects a newer atomic data/metadata pair. A hard process kill cannot guarantee artifact export.

The workflow has one application command, `python -B adapters/ibm/adapter.py --publish`, a thirty-minute job limit, one restricted data-source App token minted immediately before collection and a queued family concurrency group. The workflow includes the concrete first-family bootstrap recorded in the [reviewed history and pacing handoff](https://github.com/YouthOpps/data-pipeline/issues/120#issuecomment-6101430692); it applies only when exhaustive history finds no previously completed family attempt and cannot override a refusal or stronger interval. Once family history exists, the newest completed attempt's validated inert artifact is mandatory, including after failure; a missing artifact never permits fresh bootstrap or an older fallback.

Publication changes only `datas/ibm/data.json` and `datas/ibm/metadata.json` atomically. Before first success, failure creates no source folder. Later failure preserves the validated last-good data and its success/check timestamps when safe metadata publication is possible. Source metadata credits IBM and clearly describes the programme-information scope.

New records use attempt START for creation, first-seen and last-seen, and successful END for last-checked. Material changes advance updated-at to END; unchanged records retain their earlier updated-at and original creation/first-seen values. Tests never receive publication credentials or publish.

## Validation status

The sole live test is `python -B adapters/ibm/test_adapter.py`. It must emit the complete actual five-record JSON only on stdout; test diagnostics go to stderr. Initial implementation is subject to independent offline review and separately coordinated author and independent live acceptance. Production and website delivery remain separate maintainer gates.

Timing artifacts are uploaded only after this run validates and durably finalizes a fresh healthy envelope and signals readiness through its fresh step output. A failed publisher completion-state save cannot export prior state; owned stale artifacts are invalidated before any new export.
