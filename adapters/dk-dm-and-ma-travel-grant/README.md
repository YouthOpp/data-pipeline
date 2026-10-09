# DM student funding and member training

Source issue [#57](https://github.com/YouthOpps/data-pipeline/issues/57), official [English travel grant](https://dm.dk/students/membership/travel-grant/). This standalone standard-library adapter publishes five separately supported member programmes: the spring 2027 travel scholarship, student-event Activity Fund, free student-member classes, free online-course access, and SLING/ULING/NAKU mentoring. Independently written English summaries and factual evidence retain original programme links and DM attribution. No images, applicant data, mentor lists or protected long-form prose are published.

The finite reviewed frontier consists of 26 complete public pages: English/Danish student and membership/benefit hubs, grants hub, bilingual travel/sponsorship/classes, professional/financial benefits, online access, specific mentoring, current-event discovery, public reimbursement guidance and actual footer policies. Home/student landings provide programme/rights discovery without freezing changing general-news or upcoming-event presentation. This describes the actual member entitlements; it does not claim coverage of every dated event in DM's JavaScript calendar, every general union service or an external provider catalogue.

## Source facts and boundaries

Travel funding is up to DKK 8,000 per person; the English 2026 annual DKK 150,000 fund and twice-yearly DKK 75,000 allocations are budgets. Membership and relevant bachelor/master enrolment are required, not citizenship. Optional approved domestic/foreign trips are supported; mandatory, teaching-replacement and already university-funded activities are excluded. Danish dates the next round November 1, 2026–February 1, 2027, while English states the opening and recurring yearless calendars. Closing time/zone is unknown; the published deadline remains date-only and current state unknown, including its cutoff day. Only after that date can it be marked expired; a future-opening notice does not become a current-open claim. Danish experience-sharing assessment and English optional photo wording are both retained.

Activity Fund/Sponsoratpulje supports student-group event expenses through a responsible DM-member applicant. Danish requires university-enrolled participants, a full budget and application at least one month before the event; that relative lead time supplies no fixed deadline. Its award limit is unspecified; a DKK 7,500 testimonial is not a rate. English party/Friday-bar exclusion and broader Danish social examples remain explicitly scoped. Reimbursement is normally after the event; actual receipt and CVR-dependent administrative guidance is preserved without accessing financial forms.

Classes have explicitly published Aalborg/Aarhus/Odense/Copenhagen locations; their known host is DK. Travel, sponsorship, online access and mentoring have no complete country whitelist, so host lists remain empty. All eligible-country lists remain empty: neither publisher location nor membership proves citizenship. Online introductory DM/MA wording and the specific DM-member access paragraph are both retained; no mandatory dual membership is invented. External onlinekurser.dk registration is a participant condition, never an operation performed by the collector. Specific mentoring has three eligible subject areas, typical individually agreed meetings and no guaranteed match, published price, stipend or fixed cutoff; the stale English general mentor's half-year restriction does not apply by inference.

Membership registration redirects toward blivmedlem.dm.dk and the old general mentor page returns404. Neither is a runtime input; the separately linked current specific mentoring programme is supported by its own public page. Consumer discounts/banking/insurance, political activity, career advice, recipient stories, promotional random prize draws and operational reimbursement forms are excluded. No external MA/provider/mentor-directory, application/login or form submission is performed, and no public PDF is necessary for these five facts.

## Access and changing content

Fresh robots.txt returned404 on October 9, 2026; this absence is not a licence or a guarantee of future access. Every run checks current robots and normal HTTPS responses. Actual privacy/cookie policies concern data handling; membership purchase terms are expressly scoped to membership payment, and competition terms to promotional draws. No general automation or independently worded factual-use prohibition was found in those reviewed policies. New policy dependencies or changed restrictions require review; no open-licence claim is made.

Publisher request starts are spaced by at least six seconds, with at most ten starts in every rolling 60-second window across apex/www, robots, endpoints, redirects and retries. A shared locked local budget honours Retry-After; production Actions serialize all source publication. HTTPS redirects are limited to the same apex/www publisher family. Refusal/challenge, incomplete HTML, changed material conditions, missing required pages, changed original-URL metadata, new own programme/PDF/policy links or new legal statements fail closed. DM currently publishes og:url without rel=canonical; both observed format and exact original path are checked.

Only reviewed page-specific photo credits and one sponsorship testimonial are omitted from substantive fingerprints; new material eligibility/legal text in those locations is rejected. Programme conditions, full privacy, visible footer and legal anchor identities remain guarded. Complete public application HTML may contain a reCAPTCHA field; its presence is not an access challenge and the collector never operates that form. During research a broad substring helper falsely flagged that actual widget; the retained complete 200 response was parsed without another request after correcting the helper. No access refusal or bypass is claimed.

## Run and publication

Run from this folder with Python 3:

```sh
python3 -B adapter.py
python3 -B test_adapter.py
```

The sole test performs a complete real nonpublishing collection and writes the complete JSON to stdout. Validation covers every record, dates, countries, categories, stable identities and a 600 UTF-16-unit summary limit. Live verification should parse stdout only in memory and compare the complete accepted facts and the actual unchanged website consumer; do not save complete record dumps.

Publication uses `DATA_SOURCE_TOKEN` and `python3 -B adapter.py --publish`. The workflow triggers on its own main-branch files, schedule and manual dispatch. Data and metadata are committed atomically only to this source's folder in YouthOpps/data-source. Initial failures create no partial source folder; later failures preserve last-good data and success/check timestamps. Non-forced compare-and-swap publication safely retries unrelated sibling updates and refuses to overwrite a newer update of this source. Production/source/site Actions and final delivery verification are maintained separately.
