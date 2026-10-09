# Fulbright Austria

This standalone standard-library adapter collects 45 reviewed awards and
teaching opportunities from Fulbright Austria. Copy `adapter.py` and
`test_adapter.py` together; no shared package, fixtures or third-party parser is
required. Source-specific documentation belongs in this folder.

The inventory covers 67 official HTML pages, three public PDFs and one public
application-instructions ZIP. It includes both study/scholar directions, the
Austrian FLTA programme, salaried US teaching-assistant employment, Schuman
programmes, institutional Specialist/Intercountry projects, a separately funded
Arts Specialist track and the American Studies prize. The two EU Schuman briefs
are one programme; Expert has one identity with two published application
rounds. Recipient counts, universities, course alternatives and donation targets
are not extra opportunities. Fundraising pages corroborate existing programmes.

USTA uses `jobs`, since participants are salaried school employees and explicitly
not Fulbright grantees. Institutional requests distinguish the applicant host
from the visiting specialist/scholar. Citizenship is projected only where the
publisher states it: six incoming student records leave citizenship unspecified;
the Diplomatic Academy award states a US/Austrian dual-citizenship exception.
Known hosts are nonexclusive; WU additionally requires an unspecified
Central-European partner visit. No European destination list is invented for the
US-to-EU Schuman programme or the thesis prize.

Amounts retain monthly, weekly, daily and one-time units. IFK Senior states
€5,000 for four months without specifying monthly frequency. Prize funding is
one €1,000 award or two €750 awards, according to the panel. Expert covers the
required host health-plan and visa application costs, while sponsorship and
institutional fees are excluded. Its normalized cutoff advances from November
1, 2026 to April 1, 2027 after the first round; both travel windows remain explicit.

Date-only deadlines contain no inferred clock. The US student pages state
17:00 ET without defining the timezone, so October 7, 2026 remains date-only.
Schuman's explicit December noon CET converts using UTC+1. Future openings are
not currently open applications. A date-only closing day has unknown status;
expired calls and explicitly inactive 2027–28 awards remain visible. USTA
orientation duration/attendance and degree-document dates conflict between
sections and older guidance. Austrian student funding also has inconsistent
header, roadmap, instruction-year and previous-grant rules; these are retained.

The actual imprint permits attributed excerpts, except multimedia/software,
and prohibits framing. Summaries and evidence are independently written public
facts with publisher attribution and source links. No photographs, audio,
applicant data, biographies or full protected prose are published. No open
licence is claimed. The privacy policy and legal footer are required evidence.
Application portals, private forms, donation/payment operations, recipient
contact data and external provider catalogues are not requested.

All material pages and exact canonical URLs, own routes including queries,
external dependencies, policies and footer text are checked against reviewed
facts. New funding, eligibility, calendar, policy or document versions fail
closed. The complete visible main content is
hashed, so even benign biography/contact/editorial changes require review;
this conservative limit avoids missing material conditions inside presentation
blocks. Those personal details are never included in normalized records. Three PDFs and the ZIP require complete bounded bytes and the reviewed
SHA-256; production does not run PDF extraction tools or parse blank forms.

Publisher request starts are at least six seconds apart, with no more than ten
in any rolling 60-second window, across endpoints, aliases, redirects and
retries. Robots is checked per origin, Retry-After is honored, TLS/proxy settings
are inherited, and an identifying YouthOpp user agent is used. Actual access
refusals are fatal; a public form widget is not an access challenge. Redirects
must stay on the reviewed HTTPS publisher family and reviewed public routes.

Fresh research initially received one HTTP 503 for the outgoing scholar hub.
One later explicitly authorized exact retry returned a complete HTTP 200 page.
The first body's bytes and SHA were preserved; its original headers/timing were
not captured and the cause remains unknown. The final accepted inventory uses
the complete subsequent official response. No production publication is claimed
by these research or offline checks.

Run the only committed test from this folder:

```sh
python3 -B -m unittest test_adapter.py
```

It performs one complete live, non-publishing collection, validates all 45
records and prints the complete JSON collection to stdout. Use an in-memory
consumer/verifier rather than saving normalized record JSON files.

The filtered main-branch workflow runs on source changes, schedule or manual
dispatch. Publication requires `DATA_SOURCE_TOKEN`, writes only this source's
records/metadata pair atomically to `YouthOpps/data-source`, preserves previous
good data on failures, and uses compare-and-swap retries that preserve sibling
sources and reject a newer same-source snapshot. All publishing workflows share
the repository's global serialization group. Repository maintainers handle
merge, source publication and website Actions after independent acceptance.
