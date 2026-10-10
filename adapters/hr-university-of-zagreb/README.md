# University of Zagreb public opportunities

This standalone standard-library adapter collects a bounded University of Zagreb public opportunity inventory from the [assigned scholarship entry](https://www.unizg.hr/studiji-i-studiranje/upisi-stipendije-priznavanja/stipendije/), own mobility notices, educational programme tables and [Rectorate vacancies](https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/). It does not claim all-time or all-faculty coverage.

The reviewed inventory has398 genuine identities: five University scholarship categories,50 student mobility tracks,52 staff mobility tracks, one institutional UNIC seed call,228 educational catalogue programmes,32 vacancies and30 ancillary scholarship/fellowship/training notices. Identical seats and funding rows do not create records. Two proved punctuation/title aliases in the educational tables merge; distinct faculty/field/host allocations and genuinely different activity windows remain separate. The mandatory FPZG two-university trip is one allocation, not two independent offers. Original Croatian/English titles are retained.

## Finite scope and exclusions

The adapter checks215 unique official HTTPS inputs plus fresh same-origin robots. The finite indices were exhausted:146 student calls/eight pages,129 staff calls/seven pages,388 summer notices/20pages,245 student funding notices/13pages and96 staff funding notices/five pages. The275 primary cards and729 ancillary cards are accounted before selecting substantive opportunities.

Primary scope is programmes actually promoted by current own scholarship/mobility hubs and their latest actual editions. Ancillary scope is own advertised2026/2027 or2027–28 editions, including older publications explicitly describing current/future activity or an undated substantive framework. Prior one-off notices are historical archive exclusions, not asserted cancellations or deprecations. The advertised UNIC2023 institutional call is honestly closed; the closed2013–17 research archive is excluded.

| Boundary | Treatment |
| --- | --- |
| Capacity-closed former short-PhD call, replaced Georgia deadline, older scholarship editions | Excluded with actual current-call/edition evidence |
| Mainz student/staff aliases, Azrieli duplicate announcements | One genuine identity per actual participant track |
| Historical programmes absent current hub promotion | Historical archive exclusions; no invented legal replacement |
| External generic catalogues, faculty directories, referral-only cards | Excluded; no delegated catalogue substituted |
| Current CEEPUS/interfaculty bilateral labels and AMPEU referral | Accounted but insufficient own substantive call facts; no guessed route |
| Foundation mission audiences and honorary recognitions | Excluded without substantive applicant/benefit opportunity facts |
| Advertised CIRTT training route | Actual linked route returned404; no guessed alternate route |
| Recipient results, private applicant/household documents, forms | Not collected |

Catalogue courses are programme overviews with unknown current enrolment and no application deadline. A concrete own short-course title/topic and published event edition can support a limited overview; event dates are never registration deadlines. Osnabrück, Rennes, VIA and the two AI weeks remain grouped notices rather than unsupported option clones.

## Official access and reuse

[Robots](https://www.unizg.hr/robots.txt) permits selected routes and excludes three unrelated literal paths. Robots, exact reviewed source routes, normal TLS/CA verification and inherited proxy settings are enforced. No browser impersonation, TLS/cipher changes, login, forms, search/facet discovery or denial bypass is used.

The University's [public-information rights page](https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/pravo-na-pristup-informacijama/) describes Article31 public-body information reuse and possible published justified conditions. It is not interpreted as a blanket copyright license or a universal requirement for prior written permission. The adapter publishes original concise factual summaries, attribution and canonical links. It does not copy full prose, images or personal recipient/application records. The [privacy information](https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/sluzbenici-za-zastitu-osobnih-podataka/) is part of the reviewed frontier. External administrators are credited where their own University notice substantiates an opportunity; their application/catalogue routes are not fetched.

## Facts, dates and identifiers

IDs derive the canonical own URL and actual programme/participant/faculty/field/edition identity, never table position. Canonical URLs may legitimately be shared by distinct categories or tracks. Literal original programme titles are preserved. Course aliases keep dual published provenance and conflicting fees rather than inventing editions.

Date-only deadlines remain date-only; published unzoned noon/midnight/17:00 stays in the summary without invented UTC. First academic-round institutional handoff dates are not individual deadlines; no d-specific closing is borrowed from a/b/c. Rectorate notices preserve actual NN publication and8-,15- or30-day rules, with unknown absolute closing because legal counting is unverified. Document issue dates are not publication proof. Unknown availability stays unknown.

Material source caveats are deliberately visible:

- University scholarships are EUR2325/year in10 monthly instalments, not EUR2325monthly. The ECTS table's2026 year conflicts with general/scoring2025 clauses; own results corroborate Jan16,2026 date-only closing.
- Short PhD's own2026 paragraph conflicts with corroborated2027 closing. Third-country study PDFs omit FER in one eligibility list although the appendix and own HTML include it; activity endJuly2 versusJuly5 remains unresolved.
- EU traineeship650/700 and third-country traineeship700 monthly figures already include the150 supplement. The third-country441.44 income cutoff conflicts with its stated85%base. Study support500/550 is a different mechanism.
- Integral same-edition KA131 instructions list Lucerne144/day while its allocation lists152. KA171 instructions themselves list180 and190, with allocations190. Generic travel figures also differ from allocations. Exact conflicting awards require publisher clarification; no latest-file precedence is invented.
- Incoming staff follows the explicit current EU-plus-associated-country company/research nonteaching scope; a broader FAQ does not expand it. Five funded activity days plus two travel days are not60 fully funded days.
- Academic reimbursements and institutional BIP/UNIC budgets are not unconditional personal cash awards. Mainz participants pay lodging/insurance despite daily support.
- Course prices are listed catalogue values, not guaranteed current fees or universally per-person charges. Per-module, per-ECTS, group and separate examination charges remain qualified. Nuclear/radiological programme competencies mistakenly describe bee-sampling; its limited overview explicitly retains that mismatch, without inventing a curriculum. Malformed HKO“65” is not repaired to6.5.
- BOSE lists1000/year and12000total inconsistently; its exact personal amount is unknown. Zurich support is CHF1500/semester additional to2200SEMP. Older Azrieli Fall2025 amounts are not imported into current tracks.

Affiliation/residence is not passport eligibility. Only explicit nationality rules populate country restrictions; University/source country does not establish citizenship or physical host. Unknown physical venues remain empty; wholly online programmes have no physical host. All summaries fit600UTF16 units with material qualifications intact, without truncation.

## Run and publication

```sh
python -B adapters/hr-university-of-zagreb/test_adapter.py
python -B adapters/hr-university-of-zagreb/adapter.py --publish
```

The test contains exactly one real nonpublishing collection, validates every record and emits the complete JSON document on stdout. Keep stdout in memory; do not save normalized collected records. Production uses `DATA_SOURCE_TOKEN` restricted to the data-source repository and read-only workflow metadata credentials. The matching workflow's sole application command publishes atomically to this source's own data/metadata pair; unrelated and newer source pairs are protected.

Publisher family `youthopps-unizg-publisher-v1` shares a collector lock and durable pacing state: at least six seconds between starts and no more than10 rolling60seconds. Existing timestamps, blocked state and future embargo are preserved across starts and workflow attempts. Full stable run history selects the exact latest completed family attempt and restores only a bounded, digest/schema/source/run/attempt-bound inert artifact. Bootstrap is allowed only when reviewed complete family history proves none; missing bootstrap fails closed. Retry-After/503 controls do not reset the budget.

Prepublication has a2100second absolute phase cap; publication and conditional failure reconciliation each have180seconds. The whole process is bounded2490seconds, with nonexport work capped2460 and up to30remaining seconds reserved for trusted timing export. Main-thread POSIX alarms cover reads, sleeps, parsing, phase gaps and output; socket timeouts alone are not the wallclock bound. Expired phases are not renewed. Export errors do not mask earlier errors, and collector locks release afterward. Hard process termination cannot guarantee an artifact. The50minute workflow mints its initial one-hour App token immediately before the command; no renewal/crypto subsystem is included.

HTML guards bind reviewed semantic text and literal anchor/canonical/frontier destinations. PDFs are exact byte-digest guarded, including reviewed scanned material, without runtime external PDF tools. Missing/changed facts, aliases or links fail closed for fresh review rather than publishing a partial inventory. Live, independent and production acceptance are separate gates.

The lifelong-programmes and research-closed index guards normalize only their two reviewed literal `Top` scroll-link destinations. The unique plain `a.cd-top` control must retain its exact attributes and text; all other text, links, programme destinations and PDF guards remain unchanged.

An unknown destination on either strictly identified `Top` control still stops collection. A bounded diagnostic reports the input key, the canonical material fingerprint and a hash of the literal destination. Only previously reviewed relative paths and the numeric public `IDX_Spectacle` parameter can appear in the route field; other destinations are redacted. The material fingerprint covers the existing visible-text and literal-link guard with the same scroll sentinel; it is not a raw-body or all-attribute equality claim. This diagnostic does not accept new aliases or fix hosted transport.
