# Opportunity Desk

Collect the complete current [official RSS feed](https://opportunitydesk.org/feed/)
window, preserving each genuine individual announcement's original article URL,
title, publication timestamp and publisher taxonomy. This is a feed-scoped
inventory, not a crawl of the entire archive or every programme linked from an
article. The feed currently exposes ten recent posts and short descriptions,
without `content:encoded`; its window changes as the publisher adds posts.

## Access and attribution

The public [robots policy](https://opportunitydesk.org/robots.txt) permits the
reviewed feed and article routes. Collection rechecks the current policy and
checks every redirected endpoint before following it. No login, JavaScript,
CAPTCHA or restricted endpoint is used. The publisher's
[disclaimer](https://opportunitydesk.org/disclaimer/) describes a free information
resource, warns that its inventory is nonexhaustive and directs readers to the
original provider for authoritative current information. That statement is not
an express copyright licence. This adapter publishes attributed factual titles,
links, taxonomy and short factual deadline labels, without copying article prose,
images or full descriptions. The [contact page](https://opportunitydesk.org/contact/)
is the publisher's route for rights inquiries. No permission request or contact
submission was made as part of implementation.

## Inventory and quality decisions

Every RSS item is inspected against its public article body, specifically the
`post-content` element; menus, related posts and scripts are excluded. Feed tags
provide classification, and actual application/eligibility/deadline content is
required before an item becomes an opportunity. Unsupported taxonomy or changed
article markup fails collection rather than publishing guessed records.

Editorial taxonomy (`Our Blog`, `Blog`, `General Tips`, `How-To`, `Success Stories`,
`Young Person of the Month`) is excluded. Multi-opportunity roundups are also
excluded: plural opportunity inventory in the title must be corroborated by
multiple numbered article entries. The October 7, 2026 scholarship roundup
contains 27 separate numbered calls and is not one scholarship. Its children are
outside this feed's individual-announcement scope; the adapter does not expand
that roundup or claim complete historical coverage. Exclusions are logged to
stderr. A feed consisting only of exclusions is a failed empty collection.

Awards and contests map to `competitions`; the actual Global Teacher Prize and
Global Schools Prize articles corroborate their `Awards` taxonomy with prize,
eligibility and application sections. Training, scholarships, fellowships,
internships, volunteering, grants and jobs use the corresponding exact taxonomy.
A genuine conference/ambassador call with only `Conferences` taxonomy uses the
existing `other` category with `kind=opportunity` and classified provenance, not
an unknown kind. The reviewed Hamburg Sustainability Conference Youth Ambassador
article has explicit applications, eligibility, funded participation and a
deadline. It may fall outside the moving feed window in a later run.

Classification evidence is the actual article URL, not unattached tag text.
Identifiers remain the first 24 SHA-256 hexadecimal characters of
`opportunitydesk|<original article URL>`. Tag ordering, award quantities and feed
position do not affect identity. Feed duplicates or unexpected article hosts
fail collection. Separate programmes bundled in one genuine application call
remain one announcement, such as Stanford CISAC's fellowship tracks.

Country eligibility uses explicit, completely recognized citizenship lists in
article eligibility text. Hosts use narrowly supported physical-location text:
Washington, DC internships, a required NYC fellowship stay, and an explicit
Denmark host-institution stay. Work
authorization does not imply citizenship; Danish institutional association does
not imply mandatory Danish citizenship. Names, continent tags and worldwide
visibility do not establish eligibility or hosts. Other unsupported country
fields remain empty/null. RSS publication dates retain their explicit timezone. Article
calendar-only deadlines and rolling/ongoing statements are preserved as short
factual summary labels. Explicit application-closing dates with a clock and
GMT/UTC zone become precise `deadline` timestamps. Explicit ET submission clocks
use America/New_York with a matching dated deadline header; DST transitions are
handled by the regional timezone, and ambiguous/nonexistent clocks are rejected. Their status follows that instant; conflicting instants
remain unresolved. Calendar-only or unzoned dates retain null deadlines.
Otherwise status
remains unknown rather than guessing a closing instant or guaranteeing that an
aggregator's open wording is current.

## Run and publication

```sh
python -B adapters/opportunitydesk/test_adapter.py
python -B adapters/opportunitydesk/adapter.py --publish
```

The single live test retrieves and validates genuine nonempty records and prints
one complete JSON list to stdout; diagnostics go to stderr. It has no mocks,
fixtures, output files or publication token. Publication is for authorized
production automation only, using `DATA_SOURCE_TOKEN`.

Publisher requests, robots, articles, redirects and retries share at least six
seconds between starts and at most ten starts in any rolling minute. A local
file lock serializes collections across processes, and a separate locked
standard-library temporary budget persists starts and `Retry-After` backoff
across processes. Budget state contains no credentials or collected records.
The matching Action serializes publication with other source Actions; overlapping
remote/local research runs must still be coordinated under the publisher allowance.

A successful publication atomically replaces only
`datas/opportunitydesk/data.json` and `metadata.json`, using a Git tree/commit and
non-forced ref update. Conflicts retry only when this source is unchanged. Prior
last-good records and success timestamps survive collection or publication
failure; failure metadata is saved when safe. First-run failure creates no source
folder. Failure to durably record metadata is reported explicitly in logs.

The matching workflow runs on changes to this adapter or its own workflow on
`main` and every six hours (UTC), preserves manual dispatch and the repository/main guard, and executes
only this adapter's publication command. No production Action is used as an
implementation test.
