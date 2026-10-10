# Study in Croatia adapter

Collects the official Agency for Mobility and EU Programmes portal's bounded published scholarship and training inventory. Original English programme/call titles, short authored factual summaries and official own links are retained. Output belongs only to `datas/hr-study-in-croatia/{data,metadata}.json`.

## Reviewed inventory

Twenty public HTML inputs cover the funding/exchange/learning pages, five terminal news pages with thirty distinct cards, six material news leaves, source identity and legal/FAQ pages. Twelve distinct identities comprise five calls and seven programme overviews:

- Incoming Erasmus+, CEEPUS and bilateral scholarship frameworks, with their stated institutional/country conditions and unknown current closing/amounts.
- Croatian-heritage diaspora scholarship overview for 2025/26: EUR175 monthly for ten months, full-time public higher-education students studying in Croatia, Bosnia and Herzegovina, Montenegro, Kosovo, North Macedonia or Serbia. The 2,000 places are capacity. Heritage/living abroad does not establish a Croatian-passport whitelist. Electronic and paper documentation are both required; further selection/closing needs the official call.
- Distinct historical bilateral calls for 2023/24 and 2024/25. Public accredited institutions and study/research mobility periods are preserved, including the July/August exclusion. The 2023 closing is 31 March 2023. The 2024 wording is "before 5 April 2024"; its exclusive/inclusive cutoff is not resolved, so no inclusive UTC-day deadline is invented.
- Croatia/North Macedonia education cooperation for 29 September 2024–31 December 2028: domestic tuition conditions for North Macedonian citizens entering full degrees in Croatia, plus up to 24 annual advanced-study exchange scholarships and summer language-course scholarships. Domestic conditions do not guarantee free tuition; the nationality condition on the tuition measure is not a blanket restriction on all awards.
- Zagreb's 2026/27 BioMedMath second application round, dentistry exam windows and medicine online-exam call. Application fees are distinguished from tuition or awards. Dentistry's two mode-specific closings remain in one summary with no single invented programme-wide deadline.
- A beginner Croatian e-learning course and Rijeka's Croatian language/culture/civilisation school. Online learning has no physical host; the school's June/July programme period is not an application closing or a year-specific call.

Generic provider/course directories, six Croaticum catalogue formats, special enrolment quotas, testimonials, statistics, fairs, brochures, apps/resources and unlisted job matching are excluded. Country allocations, examination modes, institution lists and capacity do not create cloned awards. External administrator/application links are routing references; this adapter does not scrape external catalogues or submit forms.

All twelve summaries are English and fit 580 JavaScript UTF-16 units without truncation. Unknown closings and passport eligibility remain unknown. Three unambiguous dated closings are expired; nine records retain unknown availability. Historical editions do not establish a new current call. The news leaves incorrectly display today's date; their six exactly identified display-date fields are ignored, while original indexed dates, every material body/date/amount/condition and literal destination remain guarded. Timezone-less historical publication dates do not become invented UTC timestamps.

## Access and guards

The actual robots policy allows public routes. Portal identity, privacy and cookie statements were reviewed; no factual-summary permission requirement was observed there. The footer Content Disclaimer is a 200 soft404, not a valid licence or permission ban. No open licence is asserted. Use original summaries, source credit and official links; do not copy full articles, images, recipient datasets or application documents.

Each runtime freshly checks the one origin's robots. All HTTP starts, retries and redirects share `youthopps-studyincroatia-publisher-v1` pacing: at least six seconds between starts and at most ten starts in any rolling minute. Durable timestamps, future embargo and blocked state are preserved. Known refusals and unreviewed/downgrade redirects are classified before response-body reading; Retry-After is persisted first. A 503 permits one bounded same-endpoint retry after a preserved thirty-second backoff. Publisher denial never authorizes alternate access.

The material guard excludes only the reviewed random `DidYouKnowCnt` trivia sidebar, outside material content with its exact three-child shape, and the six plain `NewsOsobnaDatum` display dates. Literal links inside trivia remain guarded. Unknown counts, attributes, placement, date markup or unrelated text/link changes fail closed for fresh review. No PDF or guessed pager/API is required.

## Collection and publication

```sh
python -B adapters/hr-study-in-croatia/test_adapter.py
python -B adapters/hr-study-in-croatia/adapter.py --publish
```

The sole live test performs one real nonpublishing collection, validates all twelve records and prints complete JSON to stdout. Hold stdout in memory; do not save normalized records or provide publication credentials to tests. The matching workflow's sole application command publishes this source's own pair atomically.

Twenty HTML inputs plus one robots request require 21 nominal starts: 60-second transport reservation plus twenty six-second gaps gives a 180-second minimum allowance. The complete prepublication phase, including history/artifact restore, is capped at 900 seconds; this nominal floor is not a worst-case access guarantee. Publication and conditional failure reconciliation each have 180 seconds. Whole-process cap is 1290 seconds, nonexport work 1260 seconds, with up to thirty remaining seconds for trusted state export. POSIX main-thread absolute alarms cover reads, sleep, parse, output and cleanup; expired phases do not renew. The collector lock stays held through bounded export. The thirty-minute workflow mints its one-hour App token immediately before the command.

Full stable family history selects the exact newest completed attempt, including failed/cancelled runs, and requires its bounded digest/schema/source/run/attempt-bound inert artifact. The first-family workflow bootstrap is bound to the [reviewed public pacing handoff](https://github.com/YouthOpps/data-pipeline/issues/27#issuecomment-6095631036), which records complete absent-family history and preserved local state after both live acceptance collections. It applies only when complete history proves no family run; existing pacing state is never reset. After any completed run, including failure, the newest trusted artifact is mandatory and this marker cannot replace it. First collection failure creates no source folder. Later failures preserve last-good data and success time, with sanitized durable failure status only when safe. Guarded compare-and-swap protects siblings and newer/uncertain source pairs. Successful collection records attempt start as `last_success_at` and completion as `last_checked_at`. Before collection or writes, an existing source pair must have coherent identity, status, error, record count and aware timestamps; every record must share its metadata check time. Invalid prior pairs receive no failure metadata write.
