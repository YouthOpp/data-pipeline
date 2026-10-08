"""Collect official Harno scholarship and academic mobility opportunities."""

import argparse
import base64
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
from html.parser import HTMLParser
import ipaddress
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

SOURCE_ID = "ee-harno"
SOURCE_URL = (
    "https://www.harno.ee/en/scholarships-and-grants/"
    "scholarships-studying-and-working-estonia/"
    "scholarships-international"
)
WEBSITE_URL = SOURCE_URL
LANGUAGE = "en"
PUBLISHER_COUNTRY = "EE"
PUBLISHER_TYPE = "government"
ATTRIBUTION = "Estonian Education and Youth Board (Harno)"


class Node:
    """Minimal HTML tree retaining official links and structural boundaries."""

    def __init__(self, tag="", attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []

    def text(self):
        if self.tag in ("script", "style", "noscript"):
            return ""
        parts = []
        for child in self.children:
            parts.append(child.text() if isinstance(child, Node) else child)
        return " ".join(" ".join(parts).split())

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()


class PublisherHTML(HTMLParser):
    """Small structural parser retaining the publisher's content boundaries."""

    def __init__(self, document):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(document)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in (
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        ):
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def nodes(node, tag=None, class_name=None):
    return [
        n
        for n in node.walk()
        if (tag is None or n.tag == tag)
        and (
            class_name is None or class_name in n.attrs.get("class", "").split()
        )
    ]


def one(values, description):
    if len(values) != 1:
        raise AdapterError("Expected one " + description, "parse")
    return values[0]


def page(url):
    delay = check_robots(url)
    document = fetch_source(url, min_interval=delay)
    if not re.search(
        r"</body\s*>\s*(?:</html\s*>\s*)?$", document, re.IGNORECASE
    ):
        raise AdapterError("Incomplete publisher document", "parse")
    return PublisherHTML(document).root


PREFIX = (
    "https://www.harno.ee/en/scholarships-and-grants/"
    "scholarships-studying-and-working-estonia/"
)
PAGES = {
    "entry": SOURCE_URL,
    "estophilus": PREFIX + "estophilus",
    "language": PREFIX + "scholarships-summer-and-winter",
    "expatriate": PREFIX + "scholarship-program-aimed",
    "agur": PREFIX + "ustus-agur-scholarship-doctoral",
    "special": "https://www.harno.ee/en/scholarship-students-special-needs",
    "kristjan": (
        "https://www.harno.ee/en/scholarships-and-grants/"
        "scholarships-studying-and-working-abroad/kristjan-jaak-scholarships"
    ),
    "ekkav": (
        "https://www.harno.ee/en/scholarships-and-grants/higher-education-"
        "grants/academic-studies-estonian-language-and-culture"
    ),
    "kristjan-et": "https://www.harno.ee/kristjan-jaagu-stipendium",
    "ekkav-et": (
        "https://www.harno.ee/eesti-keele-ja-kultuuri-akadeemiline-valisope"
    ),
}
CONDITIONS = {
    "entry": "f2429f93c8a4266e60dd405a28d9d809ea278a27a408bed258760cafe180701f",
    "estophilus": "dd5fdea38a5dfd88733dc9f37d04d231441e82cf081e32778e91c60fba38f2b2",
    "language": "fa8495a8984e3351cbb728c63c036586e5537b13df9c20ff06f61e0f2a17640a",
    "expatriate": "5c469f86a822a623829d0f765c1a1b7db1e37ecc468b6130a4f8d5782cb0f3c9",
    "agur": "58aa86fdc5cd40e841a7d1553fbb5173e7eb8cde5e76f7a88e7614e93c67ec36",
    "special": "d966407a5b466fd8af5adf817a0201f89206cb59866894938b1bf3b1be0049d9",
    "kristjan": "a965be80f18be2e97a876ca968b0e5616d1d2db62754ca8908b9ca3a2cf232d9",
    "ekkav": "79c95afdd9a20cef45e3e730655530795e850a02aec9529800a042ead72c3591",
    "kristjan-et": "d4575724aaa7e74fcfd02200965e8e985a7a396056f4c6d18fb15a723f52e9cc",
    "ekkav-et": "c8ede59d2f85d40d9c4f4c609d55c1d508a98bee34c20e9c19a0f8324e5016d2",
}

ELIGIBLE = (
    "AM AZ BE BR BG CA HR CZ CY EG FI FR GE DE GB GR HU IN ID IL IT KZ LV "
    "LT MX MD CN PH PL PT KR SI ES LK TH TN TR UA US"
).split()


def body(root):
    """Keep actual article facts, omitting navigation and recipient stories."""
    main = one(nodes(root, "main"), "publisher main content")
    text = main.text()
    heading = nodes(main, "h1")[0].text()
    text = text[text.index(heading) + len(heading) :]
    text = text.split("Stay tuned!")[0].split("Püsi kursis!")[0]
    text = re.split(r"(?:Last updated:|Viimati uuendatud)", text)[0]
    if "Kristjan Jaagu nimelised stipendiumid" in heading:
        before, after = text.split("2025. aasta stipendiaadid", 1)
        after = after[after.index("Õpirände stipendium") :]
        after = after.split("Enne 2024. aastat kehtivad tingimused")[-1]
        # The current short-mobility section ends before archived contracts.
        after = after.split("Kuni 2023. aastani")[0]
        text = before + " " + after
    return " ".join(text.split())


def read_pages():
    """Read the observed, bounded own-publisher funding hubs completely."""
    return {key: page(url) for key, url in PAGES.items()}


def programme(
    key,
    title,
    url,
    summary,
    evidence,
    deadline=None,
    opening=None,
    hosts=(),
    eligible=(),
    kind=None,
    rolling=False,
    category="scholarships",
):
    """Retain award identity, dates and explicit beneficiary facts."""
    record = make_record(
        title,
        url,
        [category],
        kind=kind or ("opportunity" if deadline else "programme-overview"),
        evidence=evidence,
        host_countries=list(hosts),
    )
    record["id"] = hashlib.sha256(f"{SOURCE_ID}|{key}".encode()).hexdigest()[
        :24
    ]
    record["summary"] = summary
    record["deadline"] = deadline
    record["eligible_countries"] = list(eligible)
    now = datetime.now(timezone.utc)
    if deadline:
        end = (
            datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            if "T" in deadline
            else datetime.fromisoformat(deadline).replace(tzinfo=timezone.utc)
            + timedelta(days=1)
        )
        record["status"] = (
            "expired"
            if now >= end
            else (
                "unknown"
                if opening and now.date().isoformat() < opening
                else "open"
            )
        )
    elif rolling:
        record["status"] = "open" if now.month not in (7, 8) else "unknown"
    return record


def parse_inventory(pages):
    """Normalize reviewed awards; changed source facts fail closed."""
    texts = {key: body(root) for key, root in pages.items()}
    if set(texts) != set(PAGES):
        raise AdapterError("Incomplete Harno funding inventory", "parse")
    for key, expected in CONDITIONS.items():
        digest = hashlib.sha256(texts[key].encode()).hexdigest()
        if digest != expected:
            raise AdapterError("Harno funding facts changed: " + key, "parse")
    records = []

    def add(page_key, key, title, summary, *, anchor="", facts=(), **kwargs):
        text = texts[page_key]
        evidence = []
        for fact in facts:
            match = re.search(re.escape(fact), text, re.IGNORECASE)
            if not match:
                raise AdapterError(
                    "Missing publisher evidence: " + key, "parse"
                )
            evidence.append(text[match.start() : match.end()])
        if anchor and not any(
            n.attrs.get("id") == anchor for n in pages[page_key].walk()
        ):
            raise AdapterError("Missing publisher programme anchor", "parse")
        records.append(
            programme(
                key,
                title,
                PAGES[page_key] + ("#" + anchor if anchor else ""),
                summary,
                evidence,
                **kwargs,
            )
        )

    restriction = (
        "Russian Federation or Belarus citizens are excluded from this "
        "2026/27 international programme. "
    )
    add(
        "entry",
        "summer-school-2026",
        "Estonian summer-school scholarship 2026",
        "Foreign BA/MA/PhD students enrolled abroad for at least one year; "
        "pre-register and obtain course-host confirmation. Up to EUR700 for "
        "one course and EUR25/night accommodation for at most four weeks. "
        + restriction
        + "Application closes May1 at23:59 Estonian time; "
        "2025 online-course wording is historical, not a current guarantee.",
        anchor="summerandwinter",
        deadline="2026-05-01T20:59:00Z",
        opening="2026-04-01",
        hosts=("EE",),
        facts=(
            "foreign bachelor's, master's and PhD",
            "700 EUR",
            "25 EUR",
            "Russian Federation or Belarus",
            "23:59, Estonian time",
        ),
    )
    add(
        "entry",
        "researchers-2026",
        "Estonian researcher and academic staff grant 2026",
        "Applicants must hold an academic post at a foreign higher education "
        "institution and obtain an Estonian host confirmation. Research, "
        "teaching and collaboration: EUR45/day for1–9days or EUR660/month "
        "for10days–10months. PhD students use the student scheme; postdoctoral "
        "studies are excluded. "
        + restriction
        + "May1 deadline23:59 Estonian time; presence in Estonia required.",
        anchor="researchersandacademic",
        deadline="2026-05-01T20:59:00Z",
        opening="2026-04-01",
        hosts=("EE",),
        facts=("academic position", "45 EUR", "660 EUR", "postdoctoral"),
    )
    add(
        "entry",
        "degree-exchange-2026-27",
        "Estonian degree and exchange scholarship 2026/27",
        "Citizenship restricted to39 explicitly listed countries; MA/PhD "
        "all fields, BA only Estonian language/culture. Admission confirmation "
        "required; continuing degree students full-time. PhD junior-researcher "
        "employment at an Estonian HEI excludes applicants. EUR350/month "
        "BA/MA or660 PhD; exchange over one month, up to10months; "
        "degree12months (final year10). "
        "October31 at23:59 Estonian time. Document instructions still say "
        "2025/26, conflicting with the current2026/27 call.",
        anchor="degreeandexchange",
        deadline="2026-10-31T21:59:00Z",
        opening="2026-10-01",
        hosts=("EE",),
        eligible=ELIGIBLE,
        facts=(
            "junior researcher employment contract",
            "350 euros",
            "660 euros",
            "2026/27",
            "2025/26",
            "more than one month",
        ),
    )
    add(
        "estophilus",
        "estophilus-spring-2026",
        "Estophilus research scholarship – spring 2026",
        "Foreign-national final-year BA/MA/PhD students and postdoctoral "
        "researchers at universities abroad may research Estonia-related "
        "topics in Estonia for1–5months. Estonian supervisor confirmation "
        "required; language proficiency advantageous, not required. "
        "EUR660/month living costs plus distance-based return travel "
        "EUR180–1100. Apply February15–March15,2026; stale autumn-open "
        "wording is retained as a publisher inconsistency.",
        deadline="2026-03-15",
        opening="2026-02-15",
        hosts=("EE",),
        facts=("foreign nationals", "1-5 months", "660 euros", "15 March 2026"),
    )
    add(
        "language",
        "ekkav-language-spring-2026",
        "EKKAV Estonian language summer/winter course scholarship 2026",
        "International BA/MA/PhD students enrolled abroad who study or have "
        "studied Estonian; course registration and lecturer recommendation "
        "or language certificate required. Covers participation fee, "
        "accommodation and cultural excursions; travel self-funded. Apply "
        "February15–March15,2026. The page labels the round autumn2025/26 "
        "despite these spring dates; course places are not separate awards.",
        deadline="2026-03-15",
        opening="2026-02-15",
        hosts=("EE",),
        facts=("foreign university", "travelling expenses", "March 15th 2026"),
    )
    add(
        "agur",
        "ustus-agur-2025",
        "Ustus Agur doctoral scholarship 2025",
        "Doctoral students at an Estonian university whose research concerns "
        "ICT or its application in another field. EUR5000 award; selection "
        "considers thesis topic, expected impact on Estonian society and "
        "professional record. Submit CV/publications, research description "
        "and essay by November16,2025. Decision/award dates November24/27 "
        "are not application deadlines.",
        deadline="2025-11-16",
        hosts=("EE",),
        facts=("EUR 5,000", "16 November 2025", "Estonian university"),
    )
    add(
        "special",
        "special-needs-2026-27",
        "Harno scholarship for students with special needs 2026/27",
        "Estonian higher-education students with Social Insurance Board "
        "disability confirmation; full-/part-time and participating academic "
        "leave eligible. Estonian citizenship OR supported residence/study "
        "status required, not citizenship-only. Rolling applications from "
        "September1,2026, except July/August. EUR60–510/month by disability "
        "type/severity; part-time credits EUR10–85. Foreign degree study "
        "excluded; digital consent and relevant registration "
        "evidence required.",
        rolling=True,
        hosts=("EE",),
        facts=(
            "1 September 2026",
            "except in July and August",
            "EUR 510/month",
            "residence permit",
            "foreign institution",
        ),
    )
    add(
        "kristjan-et",
        "kristjan-degree-2026",
        "Kristjan Jaak degree studies abroad scholarship 2026",
        "Estonian citizens OR long-term/permanent residents with Estonian B1 "
        "may pursue recognized foreign MA/PhD study; MA field unavailable "
        "or stronger abroad. Living/travel support and tuition up to "
        "EUR10000/year; conditional extra EUR250/month. MA up to24months, "
        "PhD48. Return to relevant Estonian employment within one year, "
        "for2/3years respectively at least half-time, or repay. Apply "
        "March1–30,2026 at23:59; timezone unspecified.",
        anchor="valisopingute_stipendium",
        deadline="2026-03-30",
        opening="2026-03-01",
        facts=(
            "30 . märts kell 23.59",
            "B1-tasemel",
            "10 000 eurot",
            "250 eurot",
            "0,5 kohaga",
        ),
    )
    for end, opening in (
        ("2026-03-02", "2026-02-01"),
        ("2026-05-31", "2026-05-01"),
        ("2026-10-31", "2026-10-01"),
    ):
        add(
            "kristjan-et",
            "kristjan-mobility-" + end,
            "Kristjan Jaak short study visit scholarship – " + end,
            "Estonian-HEI PhD students/resident physicians, or PhD students "
            "abroad on Kristjan Jaak/EUI funding; any nationality with "
            "Estonian B1 and no outstanding programme obligations. Active "
            "foreign study/research visit up to30days, beginning1–6months "
            "after the deadline. EUR120/day for1–14days or80 for15–30days, "
            "distance-based travel and event fee up to400. One application "
            "per round, at most two awards/year. Deadline "
            + end
            + " at23:59; timezone unspecified.",
            anchor="opirande_stipendium",
            deadline=end,
            opening=opening,
            facts=(
                "2026. aasta taotlusvoorud",
                "kell 23.59",
                "120 eurot",
                "80 eurot",
                "400 euro",
                "maksimaalselt kaks",
            ),
        )
    add(
        "ekkav-et",
        "ekkav-guest-lectures-spring-2026",
        "EKKAV short guest lecture funding – spring 2026",
        "Foreign higher-education institutions offering curricular Estonian "
        "language/culture studies may request short guest-lecture funding. "
        "Normally the local Estonian lecturer prepares the application and "
        "hosts the guest. Guideline-rate lecture fee plus evidenced travel "
        "reimbursement by unit prices; amounts not stated on this page. "
        "Apply February15–March15,2026; lectures must follow "
        "confirmed results.",
        anchor="kulalisloeng",
        deadline="2026-03-15",
        opening="2026-02-15",
        kind="institutional-grant",
        category="grants",
        facts=(
            "Külalisloengute rahastust",
            "õppekaval põhinev",
            "sõidukulud",
            "15. märtsini 2026",
        ),
    )
    add(
        "ekkav-et",
        "ekkav-estonia-lectures-spring-2026",
        "EKKAV Estonia-themed lecture funding – spring 2026",
        "Higher-educated Estonian citizens OR foreign nationals with suitable "
        "education/experience may lecture at a foreign HEI without curricular "
        "Estonian language/culture teaching. Foreign applicants require "
        "Estonian B1; Estonian application documents. Guideline-rate lecture "
        "fee, no travel reimbursement; exact amount unstated. Apply "
        "February15–March15,2026; lectures must follow confirmed results.",
        anchor="eesti-teemaline",
        deadline="2026-03-15",
        opening="2026-02-15",
        facts=(
            "Eesti kodanik või välismaalane",
            "B1 tasemel",
            "sõidukulusid ei kompenseerita",
            "15. märtsini 2026",
        ),
    )
    validate_records(records)
    return records


def collect():
    """Return the complete validated real Harno opportunity collection."""
    return parse_inventory(read_pages())


CATEGORIES = (
    "scholarships",
    "internships",
    "volunteering",
    "fellowships",
    "training",
    "competitions",
    "grants",
    "jobs",
    "other",
)
_GITHUB = "https://api.github.com/repos/YouthOpps/data-source"
_USER_AGENT = "YouthOpp/1.0 (+https://github.com/YouthOpps/data-pipeline)"


class AdapterError(Exception):
    def __init__(self, message, stage="parse", status=None):
        super().__init__(message)
        self.stage, self.status = stage, status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, url):
        return None


_OPENER = urllib.request.build_opener(NoRedirect)
_ROBOTS = {}
_PUBLISHER_NEXT_AT = 0
_TRANSIENT = (429, 502, 503, 504)


def utc_now():
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def safe_error(error):
    text = str(error)
    text = re.sub(
        r'https?://[^\s<>"\']+',
        lambda match: urllib.parse.urlunsplit(
            (
                urllib.parse.urlsplit(match[0]).scheme,
                urllib.parse.urlsplit(match[0]).hostname or "",
                urllib.parse.urlsplit(match[0]).path,
                "",
                "",
            )
        ),
        text,
    )
    text = re.sub(r"(?i)\b(?:bearer|basic)\s+\S+", "[redacted]", text)
    text = re.sub(
        r"(?i)\b(token|password|secret|api[_-]?key|authorization)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        text,
    )
    return " ".join(text.split())[:500]


def pace(url, interval=6):
    """Lock shared publisher pacing state across overlapping local processes.

    State lives outside the checkout. The advisory lock stays held until the
    response and any Retry-After are recorded, so parallel runs cannot race.
    Production uses a serialized Action; separate machines must not overlap.
    """
    host = (
        (urllib.parse.urlsplit(url).hostname or "").lower().removeprefix("www.")
    )
    interval = max(interval, 12 if host == "harno.ee" else 6)
    directory = os.path.join(
        tempfile.gettempdir(), SOURCE_ID + "-pacing-" + str(os.getuid())
    )
    os.makedirs(directory, mode=0o700, exist_ok=True)
    information = os.lstat(directory)
    if (
        not os.path.isdir(directory)
        or os.path.islink(directory)
        or information.st_uid != os.getuid()
        or information.st_mode & 0o077
    ):
        raise AdapterError("Unsafe publisher pacing directory", "access")
    publisher = (
        "harno.ee" if host == "harno.ee" or host.endswith(".harno.ee") else host
    )
    key = hashlib.sha256(publisher.encode()).hexdigest()
    descriptor = os.open(
        os.path.join(directory, key),
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    state = os.fdopen(descriptor, "r+", encoding="utf-8")
    try:
        fcntl.flock(state.fileno(), fcntl.LOCK_EX)
        raw = state.read()
        budget = (
            json.loads(raw)
            if raw
            else {"starts": [], "interval": 6, "until": 0}
        )
        budget["interval"] = max(budget["interval"], interval, 6)
        while True:
            now = time.time()
            budget["starts"] = [
                start for start in budget["starts"] if now - start < 60
            ]
            ready = max(
                budget["until"],
                (
                    budget["starts"][-1] + budget["interval"]
                    if budget["starts"]
                    else now
                ),
                (
                    budget["starts"][0] + 60
                    if len(budget["starts"]) >= 10
                    else now
                ),
            )
            if ready <= now:
                budget["starts"].append(now)
                save_budget(state, budget)
                return state, budget
            time.sleep(min(ready - now, 60))
    except Exception:
        state.close()
        raise


def save_budget(state, budget):
    state.seek(0)
    json.dump(budget, state)
    state.truncate()
    state.flush()
    os.fsync(state.fileno())


def request_bytes(url, *, interval=6, method="GET", headers=None, payload=None):
    global _PUBLISHER_NEXT_AT
    state, budget = pace(url, interval)
    publisher = (urllib.parse.urlsplit(url).hostname or "").removeprefix(
        "www."
    ) == "harno.ee"
    try:
        request = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers={"User-Agent": _USER_AGENT, **(headers or {})},
        )
        try:
            response = _OPENER.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            retry = response.headers.get("Retry-After")
            if retry:
                try:
                    seconds = (
                        float(retry)
                        if re.fullmatch(r"\d+(?:\.\d+)?", retry)
                        else parsedate_to_datetime(retry).timestamp()
                        - time.time()
                    )
                    budget["until"] = max(
                        budget["until"], time.time() + max(0, seconds)
                    )
                    save_budget(state, budget)
                except (ValueError, TypeError, OverflowError):
                    pass
            if publisher and response.status in _TRANSIENT:
                budget["until"] = max(budget["until"], time.time() + 60)
                save_budget(state, budget)
            try:
                body = response.read(5_000_001)
                if len(body) > 5_000_000:
                    raise AdapterError("Response exceeds 5 MB", "fetch")
                return response.status, response.headers, body
            finally:
                if publisher:
                    _PUBLISHER_NEXT_AT = max(
                        _PUBLISHER_NEXT_AT, time.time() + 120, budget["until"]
                    )
    finally:
        state.close()


def fetch_source(url, min_interval=6):
    original = urllib.parse.urlsplit(url)

    def checked(value):
        parsed = urllib.parse.urlsplit(value)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or host != (original.hostname or "").lower().removeprefix("www.")
            or parsed.port != original.port
        ):
            raise AdapterError(
                "Source redirect leaves the reviewed HTTPS host", "fetch"
            )
        if host == "localhost" or host.endswith(".localhost"):
            raise AdapterError("Public HTTPS source required", "fetch")
        try:
            if not ipaddress.ip_address(host).is_global:
                raise AdapterError("Public HTTPS source required", "fetch")
        except ValueError:
            pass

    for attempt in range(2):
        current = url
        try:
            for hop in range(4):
                checked(current)
                status, headers, body = request_bytes(
                    current, interval=min_interval
                )
                if status in (301, 302, 303, 307, 308):
                    if hop == 3 or not headers.get("Location"):
                        raise AdapterError(
                            "Invalid or excessive source redirects", "fetch"
                        )
                    current = urllib.parse.urljoin(current, headers["Location"])
                    checked(current)
                    if original.path != "/robots.txt":
                        min_interval = max(min_interval, check_robots(current))
                    continue
                if status in _TRANSIENT and attempt == 0:
                    break
                if status != 200:
                    raise AdapterError(
                        f"Publisher returned HTTP {status} for "
                        + urllib.parse.urlunsplit(
                            (
                                urllib.parse.urlsplit(current).scheme,
                                urllib.parse.urlsplit(current).hostname or "",
                                urllib.parse.urlsplit(current).path,
                                "",
                                "",
                            )
                        ),
                        "fetch",
                        status,
                    )
                return body.decode(headers.get_content_charset() or "utf-8")
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if attempt:
                raise AdapterError(
                    "Publisher connection failed: " + safe_error(error), "fetch"
                ) from error
    raise AdapterError("Publisher could not be read", "fetch")


def check_robots(url):
    parsed = urllib.parse.urlsplit(url)
    origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    if origin not in _ROBOTS:
        try:
            _ROBOTS[origin] = fetch_source(origin + "/robots.txt")
        except AdapterError as error:
            if error.status == 404:
                _ROBOTS[origin] = ""
            else:
                raise
    text = _ROBOTS[origin]
    groups, group = [], {"agents": [], "rules": [], "delay": 0}
    for line in text.splitlines():
        key, separator, value = line.split("#", 1)[0].strip().partition(":")
        if not separator:
            continue
        key, value = key.lower().strip(), value.strip()
        if key == "user-agent":
            if group["rules"] or group["delay"]:
                groups.append(group)
                group = {"agents": [], "rules": [], "delay": 0}
            group["agents"].append(value.lower())
        elif key in ("allow", "disallow"):
            group["rules"].append((key, value))
        elif key == "crawl-delay":
            try:
                group["delay"] = float(value)
                if not 0 <= group["delay"] <= 60:
                    raise ValueError()
            except ValueError:
                raise AdapterError("Unsupported robots crawl delay", "access")
    groups.append(group)
    directives = [
        line.split("#", 1)[0].strip().lower()
        for line in text.splitlines()
        if line.split("#", 1)[0].strip()
    ]
    if (
        directives
        and not any(g["agents"] for g in groups)
        and not all(line.startswith("sitemap:") for line in directives)
    ):
        raise AdapterError("Robots policy is unavailable or invalid", "access")
    specific = [g for g in groups if "youthopp" in g["agents"]]
    selected = specific or [g for g in groups if "*" in g["agents"]]
    path = parsed.path + ("?" + parsed.query if parsed.query else "")
    matches = []
    for g in selected:
        for directive, pattern in g["rules"]:
            if not pattern:
                continue
            expression = (
                "^"
                + ".*".join(
                    re.escape(part)
                    for part in pattern.removesuffix("$").split("*")
                )
                + ("$" if pattern.endswith("$") else "")
            )
            if re.search(expression, path):
                matches.append(
                    (
                        len(pattern.replace("*", "").removesuffix("$")),
                        directive == "allow",
                    )
                )
    if matches and not max(matches)[1]:
        raise AdapterError(
            "Robots policy disallows this source endpoint", "access"
        )
    return max([6] + [g["delay"] for g in selected])


def make_record(
    title,
    url,
    categories,
    kind="opportunity",
    published_at=None,
    tags=None,
    host_countries=None,
    method="editorial-review",
    evidence=None,
):
    now = utc_now()
    return {
        "id": hashlib.sha256(f"{SOURCE_ID}|{url}".encode()).hexdigest()[:24],
        "title": title,
        "url": url,
        "source": SOURCE_ID,
        "source_url": SOURCE_URL,
        "published_at": published_at,
        "summary": "",
        "tags": tags or [],
        "location": None,
        "deadline": None,
        "language": LANGUAGE,
        "category": categories[0],
        "categories": categories,
        "kind": kind,
        "host_countries": host_countries or [],
        "eligible_countries": [],
        "publisher_country": PUBLISHER_COUNTRY,
        "created_at": now,
        "updated_at": now,
        "first_seen_at": now,
        "last_seen_at": now,
        "last_checked_at": now,
        "status": "unknown",
        "classification": {
            "method": method,
            "status": "unknown" if categories == ["other"] else "classified",
            "evidence": evidence or [],
        },
    }


def validate_records(records):
    """Validate every collected or persisted record against the contract."""
    if not isinstance(records, list) or not records:
        raise AdapterError(
            "Empty or invalid opportunity collection", "validate"
        )
    ids = set()
    for record in records:
        if (
            not isinstance(record, dict)
            or record.get("source") != SOURCE_ID
            or record.get("source_url") != SOURCE_URL
        ):
            raise AdapterError("Record source identity mismatch", "validate")
        for field in (
            "id",
            "title",
            "url",
            "created_at",
            "updated_at",
            "first_seen_at",
            "last_seen_at",
            "last_checked_at",
        ):
            if (
                not isinstance(record.get(field), str)
                or not record[field].strip()
            ):
                raise AdapterError("Missing record field: " + field, "validate")
        if record["id"] in ids:
            raise AdapterError("Duplicate opportunity identifier", "validate")
        ids.add(record["id"])
        url = urllib.parse.urlsplit(record["url"])
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
        ):
            raise AdapterError("Unsafe opportunity URL", "validate")
        for field in (
            "created_at",
            "updated_at",
            "first_seen_at",
            "last_seen_at",
            "last_checked_at",
            "published_at",
            "deadline",
        ):
            value = record.get(field)
            if field not in record:
                raise AdapterError("Missing record field: " + field, "validate")
            if value is not None:
                try:
                    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                        if field != "deadline":
                            raise ValueError()
                        datetime.strptime(value, "%Y-%m-%d")
                    elif (
                        datetime.fromisoformat(
                            value.replace("Z", "+00:00")
                        ).tzinfo
                        is None
                    ):
                        raise ValueError()
                except (ValueError, TypeError, AttributeError):
                    raise AdapterError(
                        "Invalid record timestamp: " + field, "validate"
                    )
        for field in ("tags", "host_countries", "eligible_countries"):
            values = record.get(field)
            if not isinstance(values, list) or any(
                not isinstance(value, str) for value in values
            ):
                raise AdapterError("Invalid record list: " + field, "validate")
            if field != "tags" and any(
                not re.fullmatch("[A-Z]{2}", value) for value in values
            ):
                raise AdapterError("Invalid country code", "validate")
        categories = record.get("categories")
        if (
            not isinstance(categories, list)
            or not categories
            or any(value not in CATEGORIES for value in categories)
            or len(set(categories)) != len(categories)
            or record.get("category") != categories[0]
        ):
            raise AdapterError("Invalid opportunity categories", "validate")
        if record.get("kind") not in (
            "opportunity",
            "programme-overview",
            "institutional-grant",
            "unknown",
        ) or record.get("status") not in ("open", "expired", "unknown"):
            raise AdapterError("Invalid opportunity kind or status", "validate")
        summary = record.get("summary")
        if not isinstance(summary, str) or re.search("<[^>]+>", summary):
            raise AdapterError("Invalid plain summary", "validate")
        length = len(summary.encode("utf-16-le")) // 2
        if length > 600:
            raise AdapterError("Invalid plain summary", "validate")
        for field in ("location", "language", "publisher_country"):
            if (
                field not in record
                or record[field] is not None
                and not isinstance(record[field], str)
            ):
                raise AdapterError("Invalid record field: " + field, "validate")
        classification = record.get("classification")
        if (
            not isinstance(classification, dict)
            or not isinstance(classification.get("method"), str)
            or classification.get("status") not in ("classified", "unknown")
            or not isinstance(classification.get("evidence"), list)
            or any(
                not isinstance(value, str)
                for value in classification["evidence"]
            )
        ):
            raise AdapterError("Invalid classification evidence", "validate")


def github(method, path, payload=None, missing_ok=False):
    token = os.environ.get("DATA_SOURCE_TOKEN")
    if not token:
        raise AdapterError(
            "DATA_SOURCE_TOKEN is required for publication", "publish"
        )
    headers = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    try:
        status, _, body = request_bytes(
            _GITHUB + path,
            method=method,
            headers=headers,
            payload=(
                json.dumps(payload).encode() if payload is not None else None
            ),
        )
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise AdapterError(
            "GitHub connection failed; publication is unconfirmed", "publish"
        ) from error
    if status == 404 and missing_ok:
        return None
    if status not in (200, 201):
        raise AdapterError(
            f"GitHub publication API returned HTTP {status}", "publish", status
        )
    return json.loads(body)


def read_snapshot():
    head = github("GET", "/git/ref/heads/main")["object"]["sha"]
    tree = github("GET", "/git/commits/" + head)["tree"]["sha"]
    files = {}
    for name in ("data.json", "metadata.json"):
        file = github(
            "GET",
            f"/contents/datas/{SOURCE_ID}/{name}?ref={head}",
            missing_ok=True,
        )
        if file is not None and file.get("encoding") != "base64":
            raise AdapterError(
                "Unsupported existing source file encoding", "publish"
            )
        files[name] = (
            None
            if file is None
            else base64.b64decode(file["content"]).decode("utf-8")
        )
    if (files["data.json"] is None) != (files["metadata.json"] is None):
        raise AdapterError(
            "Incomplete remote source snapshot; no files changed", "publish"
        )
    return {"head": head, "tree": tree, "files": files}


def publish_files(snapshot, files):
    expected = snapshot["files"].copy()
    desired = {**expected, **files}
    for attempt in range(3):
        candidate = None
        try:
            tree = github(
                "POST",
                "/git/trees",
                {
                    "base_tree": snapshot["tree"],
                    "tree": [
                        {
                            "path": f"datas/{SOURCE_ID}/{name}",
                            "mode": "100644",
                            "type": "blob",
                            "content": text,
                        }
                        for name, text in files.items()
                    ],
                },
            )["sha"]
            candidate = github(
                "POST",
                "/git/commits",
                {
                    "message": f"Update {SOURCE_ID} collection outcome",
                    "tree": tree,
                    "parents": [snapshot["head"]],
                },
            )["sha"]
            github(
                "PATCH",
                "/git/refs/heads/main",
                {"sha": candidate, "force": False},
            )
            return
        except AdapterError:
            latest = read_snapshot()
            if candidate and (
                latest["head"] == candidate or latest["files"] == desired
            ):
                return
            if latest["files"] != expected:
                raise AdapterError(
                    "Source changed concurrently; "
                    "newer remote data was not overwritten",
                    "publish",
                )
            snapshot = latest
            if attempt == 2:
                raise AdapterError(
                    "Publication failed; "
                    "durable remote outcome could not be updated",
                    "publish",
                )


def wait_publisher_cooldown(previous):
    """Carry publisher cooldown between separate serialized Action runners."""
    global _PUBLISHER_NEXT_AT
    if not previous:
        return
    value = previous.get("publisher_next_request_at")
    if value is None:
        # Old attempt/success clocks mark collection start, not the last request.
        ready = time.time() + 120
    else:
        try:
            if not isinstance(value, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}" r"(?:\.\d{1,6})?Z", value
            ):
                raise ValueError()
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
                raise ValueError()
            ready = moment.timestamp()
        except (ValueError, TypeError, OverflowError):
            raise AdapterError("Invalid publisher cooldown timestamp", "access")
    _PUBLISHER_NEXT_AT = max(_PUBLISHER_NEXT_AT, ready)
    while ready > time.time():
        time.sleep(min(ready - time.time(), 60))


def metadata(attempt, previous, records, error=None):
    result = {
        "source": SOURCE_ID,
        "source_url": SOURCE_URL,
        "website_url": WEBSITE_URL,
        "language": LANGUAGE,
        "publisher_country": PUBLISHER_COUNTRY,
        "publisher_type": PUBLISHER_TYPE,
        "status": "fail" if error else "success",
        "last_attempt_at": attempt,
        "last_success_at": (
            previous.get("last_success_at") if error else attempt
        ),
        "last_checked_at": (
            previous.get("last_checked_at") if error else attempt
        ),
        "record_count": len(records),
        "publisher_next_request_at": (
            datetime.fromtimestamp(
                _PUBLISHER_NEXT_AT or time.time() + 120, timezone.utc
            )
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        ),
        "message": (
            "Collection failed; last-good data preserved"
            if error
            else (f"Collected {len(records)} validated Harno funding records")
        ),
        "error": safe_error(error) if error else None,
    }
    if ATTRIBUTION:
        result["attribution"] = ATTRIBUTION
    if error:
        result["failure_stage"] = getattr(error, "stage", "parse")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--publish",
        action="store_true",
        help=(
            "Write only this source to YouthOpps/data-source "
            "using DATA_SOURCE_TOKEN"
        ),
    )
    args = parser.parse_args()
    snapshot = read_snapshot() if args.publish else None
    old = (
        json.loads(snapshot["files"]["data.json"])
        if snapshot and snapshot["files"]["data.json"] is not None
        else []
    )
    previous = (
        json.loads(snapshot["files"]["metadata.json"])
        if snapshot and snapshot["files"]["metadata.json"] is not None
        else {}
    )
    if snapshot and snapshot["files"]["data.json"] is not None:
        validate_records(old)
        if previous.get("source") != SOURCE_ID:
            raise AdapterError(
                "Remote metadata source mismatch; no files changed", "publish"
            )
    attempt, failure = utc_now(), None
    try:
        if args.publish:
            wait_publisher_cooldown(previous)
        records = collect()
        validate_records(records)
        prior = {record["id"]: record for record in old}
        temporal = {
            "created_at",
            "updated_at",
            "first_seen_at",
            "last_seen_at",
            "last_checked_at",
        }
        for record in records:
            before = prior.get(record["id"], {})
            same = {
                key: value
                for key, value in before.items()
                if key not in temporal
            } == {
                key: value
                for key, value in record.items()
                if key not in temporal
            }
            record.update(
                created_at=before.get("created_at", attempt),
                first_seen_at=before.get("first_seen_at", attempt),
                updated_at=(
                    before.get("updated_at", attempt) if same else attempt
                ),
                last_seen_at=attempt,
                last_checked_at=attempt,
            )
        records.sort(key=lambda record: record["id"])
        records.sort(
            key=lambda record: record["published_at"] or "", reverse=True
        )
        validate_records(records)
    except Exception as error:
        failure, records = error, old
    outcome = metadata(attempt, previous, records, failure)
    if args.publish and (not failure or old):
        files = {
            "metadata.json": json.dumps(outcome, ensure_ascii=False, indent=2)
            + "\n"
        }
        if not failure:
            files["data.json"] = (
                json.dumps(records, ensure_ascii=False, indent=2) + "\n"
            )
        try:
            publish_files(snapshot, files)
        except Exception as error:
            failure = error
            outcome = metadata(
                attempt,
                previous,
                old,
                AdapterError(
                    "Publication failed: " + safe_error(error), "publish"
                ),
            )
            try:
                latest = read_snapshot()
                if not old or latest["files"] != snapshot["files"]:
                    raise AdapterError(
                        "No safe previous snapshot for failure reporting",
                        "publish",
                    )
                publish_files(
                    latest,
                    {
                        "metadata.json": json.dumps(
                            outcome, ensure_ascii=False, indent=2
                        )
                        + "\n"
                    },
                )
            except Exception:
                outcome["message"] = (
                    "Publication failed; durable failure status could not be "
                    "recorded. Remote data was not force-overwritten."
                )
    elif args.publish:
        outcome["message"] = (
            "First collection failed; no published source folder created"
        )
    print(json.dumps(outcome, ensure_ascii=False))
    return 1 if failure else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(
            json.dumps(
                {
                    "source": SOURCE_ID,
                    "status": "fail",
                    "last_attempt_at": utc_now(),
                    "message": (
                        "Run could not complete; " "durable status unavailable"
                    ),
                    "error": safe_error(error),
                    "failure_stage": getattr(error, "stage", "publish"),
                },
                ensure_ascii=False,
            )
        )
        sys.exit(1)
