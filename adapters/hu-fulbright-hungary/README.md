# Fulbright Hungary

Source: `hu-fulbright-hungary`, published by the Hungarian-American Fulbright Commission in Hungary. This standalone adapter uses Python 3.12 or later and the standard library only.

## Official access and attribution

Collection reads the public [award homepage](https://fulbright.hu/), [short-term grants](https://fulbright.hu/special-short-term-grants/) and [U.S. awards](https://fulbright.hu/fulbright-awards/for-us-citizens/), plus linked current Hungarian and joint university award pages. The [robots policy](https://fulbright.hu/robots.txt) reviewed on 8 October 2026 permits access with an empty `Disallow` rule. The official navigation and footer expose no separate terms or reuse policy; ordinary copyright is present. This supports the reviewed public collection route, not a claim of unrestricted redistribution rights. Records preserve publisher attribution, original links and classification evidence. Application forms and external award websites are linked, never crawled or submitted.

The adapter identifies itself as YouthOpp, honors robots and Retry-After, and starts at most ten requests per rolling minute with at least six seconds between requests. Redirect destinations are checked against robots. Local overlapping runs share locked pacing state outside the checkout; production publication is serialized by the workflow. Do not overlap local collection with a production run on another machine.

## Opportunity inventory and exclusions

The reviewed live inventory contains ten genuine records:

- Three active Hungarian calls: lecturer, researcher and Foreign Language Teaching Assistant (FLTA).
- Two joint lecturer awards at the University of Texas at Austin and Indiana University, explicitly matching the current parent call's academic cycle and application route.
- Four rolling grants: conference travel for Hungarian alumni, Specialist hosting, Intercountry Lecturing hosting and Outreach Lecturing Fund travel. Hosting grants are classified as institutional grants.
- One dated U.S. Student Program call for postgraduate study in Hungary.

The collector checks the entire homepage award section and short-term grant sections. It excludes expired deadlines, future announcements, recipient lists and explicitly discontinued programmes. The undated Pittsburgh description has no verified current academic cycle. MTA support supplements an existing research award rather than providing a separate application call. The institutional hosting questionnaire has an expired deadline; its two rolling programmes duplicate the collected Specialist and Intercountry grants. Other U.S. navigation entries are undated programme descriptions without current calls. New unsupported short-term deadlines fail explicitly rather than silently producing an incomplete collection.

## Identity, dates and eligibility

Identifiers derive from source ID and original URL; dated calls also include the published academic cycle, so recurring awards retain distinct identities across cycles. Every record contains the source ID, source URL and evidence links; metadata supplies the human-readable publisher attribution. Empty collections, duplicate IDs, invalid URLs, dates, categories or countries fail validation.

Hungarian citizenship is recorded only when the official current award detail explicitly requires it. Joint lecturer eligibility and deadline come from their verified shared parent application route. Institutional grants do not imply individual nationality eligibility. Host countries distinguish U.S. study/travel from institutional hosting in Hungary.

Exact Hungarian deadlines use the end of the publisher's Budapest calendar day and are serialized in UTC. Joint award deadlines inherit the current lecturer call's deadline. Rolling applications have no invented deadline. The U.S. Student Program's approximate April-to-mid-October application window remains in its summary; its exact deadline is null, boundary-month status is unknown, and the call is excluded outside the published months. Publication and modification timestamps are not inferred from unrelated dates; collection freshness uses UTC timestamps.

## Run and test

From the repository root:

```sh
python -B adapters/hu-fulbright-hungary/adapter.py
python -B adapters/hu-fulbright-hungary/test_adapter.py
```

The sole live test performs real non-publishing collection, validates every record and emits the complete JSON result on stdout with diagnostics on stderr. It receives no publication token and writes no output files. The folder can run independently when copied outside the repository.

On 8 October 2026, author and independent live tests validated all ten records in memory, including unique identities and URLs, publisher evidence, countries and dates. Later collection may legitimately change as calls open or expire.

## Publication

The matching `fetch-hu-fulbright-hungary.yml` workflow runs automatically on pushes to `main` affecting this adapter folder or its own workflow, including maintainer merges. Manual dispatch and a six-hour schedule (`29 0/6 * * *`, UTC) remain available. Repository/main guards, shared publication concurrency and the existing GitHub App's data-source-only contents-write token scope remain enforced.

The production command is exclusively:

```sh
python -B adapters/hu-fulbright-hungary/adapter.py --publish
```

Only `YouthOpps/data-source/datas/hu-fulbright-hungary/{data,metadata}.json` are published, atomically and with conflict-safe reference updates. Successful publication of validated nonempty records advances the durable success timestamp. Later failures preserve last-good data and its success timestamp while recording sanitized failure metadata when possible; a failure before first success creates no source folder. Tests never publish. Production publication was reviewed statically and was not invoked during implementation or QA.
