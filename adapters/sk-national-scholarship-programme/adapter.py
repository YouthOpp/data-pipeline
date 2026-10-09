"""Collect official National Scholarship Programme mobility awards."""

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

SOURCE_ID = "sk-national-scholarship-programme"
SOURCE_URL = "https://www.scholarships.sk/en/main/o-programe"
WEBSITE_URL = (
    "https://www.scholarships.sk/en/main/programme-terms-and-conditions/"
)
LANGUAGE = "en"
PUBLISHER_COUNTRY = "SK"
PUBLISHER_TYPE = "nonprofit"
ATTRIBUTION = "SAIA, n. o. – www.saia.sk"


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


PAGES = {
    "about": SOURCE_URL,
    "home": "https://www.scholarships.sk/en",
    "terms": WEBSITE_URL,
    "incoming": (
        "https://www.scholarships.sk/en/main/programme-terms-and-conditions/"
        "foreign-applicants"
    ),
    "outgoing": (
        "https://www.scholarships.sk/en/main/programme-terms-and-conditions/"
        "applicants-from-slovakia-2023"
    ),
    "call": (
        "https://www.scholarships.sk/en/news/"
        "call-sum-sem-acad-yr-26/27-apply-now"
    ),
    "outgoing-detail": (
        "https://www.stipendia.sk/sk/main/podmienky-pre-predkladanie-ziadosti/"
        "uchadzaci-zo-slovenska/"
    ),
    "outgoing-rates": (
        "https://www.stipendia.sk/sk/main/podmienky-pre-predkladanie-ziadosti/"
        "uchadzaci-zo-slovenska/vyska-stipendia/"
    ),
    "slovak-home": "https://www.stipendia.sk/sk/",
    "outgoing-call": (
        "https://www.stipendia.sk/sk/aktuality/" "vyzva-ls-ar-26/27-otvorena"
    ),
    "legal": (
        "https://www.saia.sk/en/main/about-us-main/"
        "legal-information-and-data-protection/"
    ),
}
TERMS_PDF = (
    "https://www.saia.sk/_user/documents/SAIA/pravne-info/"
    "VZP-SAIA-weby2018-SK.pdf"
)
CONDITIONS = {
    "about": "5b18d8ed26c3a175025571cc4377e8a26c63d98abf40331ac35d6a81ebeed0e2",
    "home": "226e794e4b96e6e602d3614dd855ddd2aa2091f9267ef2f3465a143e161a8ba9",
    "terms": "4d9401831c800cd2b2316d6bb44cfc58dba2848f0f60dc43eb3671ad24dcb2fa",
    "incoming": "548adc0641f83da5e2dfcbecb1cb576d10c41223e8cb6081815563b86244ae7c",
    "outgoing": "6511ca9fdb6a5d90a026c9ff462d8359466bfe64c64a50383175249f840b23ba",
    "call": "d1c42a6e310c67867cd7128900d7324e29a0ad93a99aee987e68e66f9cbc9256",
    "outgoing-detail": "08a279bb22d6b27a4fc15f443c26e0e12bd289a6a9b01b35fbcfad35a46fe68f",
    "outgoing-rates": "21259996c085d5421b853bf6f332c734b95ea9a7780e41c7a0830c0f8367c82c",
    "slovak-home": "ee62390ddd153a60bc1daf540953ac615bd1a7f6091d23357f6f1e7f8bc600b8",
    "outgoing-call": "ccdb2736ab17038b28943181dd9251e6280c346ed83301353d419ccb64790c9c",
    "legal": "62d8dbf97292692b0313261c796b72d88558752f810be76e10f4bdb6a191fdb9",
}
TERMS_HASH = "79426e09e2fa247e7a3e7dcfee669ad298b9eace24576c0e198582a90a8bb826"

DEADLINE = "2026-10-31T15:00:00Z"


def content(root):
    """Select the actual article, excluding menus and personal testimonials."""
    return one(nodes(root, "div", "content-sm"), "NSP factual article")


def source_facts(key, root):
    if key in ("home", "slovak-home"):
        target = PAGES["call" if key == "home" else "outgoing-call"]
        base = one(nodes(root, "base"), "publisher base URL").attrs.get("href")
        link = one(
            [
                n
                for n in nodes(root, "a")
                if urllib.parse.urljoin(base, n.attrs.get("href", "")) == target
            ],
            "current dated call link",
        )
        return link.text() + " " + target
    if key == "legal":
        return one(
            [n for n in root.walk() if n.attrs.get("id") == "contentcol"],
            "SAIA terms article",
        ).text()
    return content(root).text()


def check_terms_policy():
    """Verify the reviewed binding public terms, without publishing the PDF."""
    delay = check_robots(TERMS_PDF)
    status, _, document = request_bytes(TERMS_PDF, interval=delay)
    if status != 200 or hashlib.sha256(document).hexdigest() != TERMS_HASH:
        raise AdapterError(
            "SAIA binding terms changed or unavailable", "access"
        )


def read_pages():
    """Read the complete two-direction programme and current call evidence."""
    pages = {key: page(url) for key, url in PAGES.items()}
    check_terms_policy()
    return pages


def add_track(
    records,
    pages,
    direction,
    track,
    anchor,
    title,
    summary,
    facts,
    detail,
    extra=(),
):
    """Retain one actual applicant category within the source-proven round."""
    key = "incoming" if direction == "incoming" else "outgoing-detail"
    root = content(pages[key])
    text = root.text()
    if not any(n.attrs.get("id") == anchor for n in root.walk()):
        raise AdapterError("Missing actual applicant category anchor", "parse")
    evidence = []
    for fact in facts:
        match = re.search(re.escape(fact), text, re.IGNORECASE)
        if not match:
            raise AdapterError("Missing NSP category fact: " + track, "parse")
        evidence.append(text[match.start() : match.end()])
    evidence.extend(detail)
    evidence.extend(extra)
    evidence.extend(
        [
            "Actual 2026/27 call: "
            + PAGES["call" if direction == "incoming" else "outgoing-call"],
            "Deadline 31 October 2026 at 16:00 CET (fixed UTC+01); "
            "application deadline is distinct from document delivery "
            "milestones.",
            "Source attribution: " + ATTRIBUTION + "; retrieval time is "
            "last_checked_at (UTC), also retained in source metadata.",
        ]
    )
    record = make_record(
        title,
        PAGES[key] + "#" + anchor,
        ["scholarships"],
        host_countries=["SK"] if direction == "incoming" else [],
        evidence=evidence,
    )
    record["id"] = hashlib.sha256(
        f"{SOURCE_ID}|{direction}|{track}|2026-10-31".encode()
    ).hexdigest()[:24]
    record["summary"] = summary
    record["deadline"] = DEADLINE
    record["status"] = (
        "expired"
        if datetime.now(timezone.utc)
        >= datetime.fromisoformat(DEADLINE.replace("Z", "+00:00"))
        else "open"
    )
    records.append(record)


def outgoing_funding(pages):
    """Keep country-specific amount cells with their real applicant columns."""
    table = one(
        nodes(content(pages["outgoing-rates"]), "table"),
        "outgoing monthly funding table",
    )
    rows = []
    for row in nodes(table, "tr"):
        cells = [
            n.text()
            for n in row.children
            if isinstance(n, Node) and n.tag in ("td", "th")
        ]
        if len(cells) == 4:
            if not all(re.fullmatch(r"[\d ]+ EUR", x) for x in cells[1:]):
                raise AdapterError(
                    "Invalid outgoing monthly funding row", "parse"
                )
            rows.append(cells)
    if len(rows) != 69 or len({r[0] for r in rows}) != 69:
        raise AdapterError(
            "Changed outgoing monthly funding inventory", "parse"
        )
    return [
        [
            "Published monthly rate (" + row[0] + "): " + row[column]
            for row in rows
        ]
        for column in (1, 2, 3)
    ]


def parse_inventory(pages):
    """Normalize all six genuine tracks; changed required facts fail closed."""
    if set(pages) != set(PAGES):
        raise AdapterError("Incomplete two-way NSP inventory", "parse")
    for key, root in pages.items():
        if (
            hashlib.sha256(source_facts(key, root).encode()).hexdigest()
            != CONDITIONS[key]
        ):
            raise AdapterError("NSP source facts changed: " + key, "parse")
    records = []
    incoming_common = [
        "Citizens of any country except Slovakia; no invented positive "
        "citizenship whitelist. Full-degree students in Slovakia excluded.",
        "Exclude applicants who spent at least 15 months in Slovakia during "
        "the prior 36 months and overlapping publicly financed scholarship "
        "stays. Reapplication requires at least two years since last NSP "
        "award/receipt; exceptional force-majeure cases may be considered.",
        "Apply online; original admission/invitation must reach SAIA by noon "
        "on the third Slovak working day after the application deadline. "
        "This secondary delivery requirement is not the application deadline.",
        "Non-EU/EEA/Swiss scholars staying over 90 days who need a temporary "
        "residence permit may receive up to EUR250 reimbursement for the "
        "required medical examination/certificate, with original invoice.",
    ]
    add_track(
        records,
        pages,
        "incoming",
        "student",
        "elapp_stud",
        "NSP incoming university student scholarship – October 2026",
        "Students enrolled outside Slovakia: MA level or at least 2.5 years "
        "completed in the same/similar degree; accepted Slovak academic "
        "mobility, not PhD or full-degree study in Slovakia. EUR620/month "
        "plus optional travel EUR0–1500. Current stay starts February1–April1 "
        "2027, ends by August31; general longer durations may not fit. "
        "Non-Slovak citizenship; 15/36-month residence and public-funding "
        "exclusions, two-year repeat rule. Apply October31,2026 at16:00 CET.",
        ("at least 2.5 years", "620 EUR", "except the citizens of Slovakia"),
        incoming_common
        + [
            "General student durations: one/two full semesters (4–5/9–10 "
            "months) or one–three full trimesters (3–4/6–7/9–10 months); "
            "the current call's February–August 2027 window still governs.",
            "Travel is requested with the scholarship and paid at the end; "
            "distance-based EUR0/50/100/250/350/500/750/1100/1500 tiers "
            "are one component, not separate grants.",
        ],
    )
    add_track(
        records,
        pages,
        "incoming",
        "phd",
        "elapp_phdstud",
        "NSP incoming PhD mobility scholarship – October 2026",
        "PhD study/training outside Slovakia; accepted by a Slovak HEI or "
        "eligible doctoral research institution for 1–10-month academic "
        "mobility. EUR1025.50/month plus optional travel EUR0–1500. Current "
        "stay starts February1–August31,2027, ends by November30. Non-Slovak "
        "citizenship; full-degree study in Slovakia, 15/36-month prior "
        "residence and overlapping public awards excluded; two-year repeat "
        "rule. Apply October31,2026 at16:00 CET.",
        (
            "scientific training takes place outside Slovakia",
            "1025,50 EUR",
            "1 – 10 months",
        ),
        incoming_common
        + [
            "PhD travel grant requested with the scholarship and paid at "
            "the end; distance-based EUR0–1500. Host must be eligible to "
            "carry out a doctoral programme, not simply any institution.",
        ],
    )
    add_track(
        records,
        pages,
        "incoming",
        "academic-artist",
        "elapp_research",
        "NSP incoming teacher/researcher/artist scholarship – October 2026",
        "Invited by certified Slovak non-business research/education host "
        "for 1–10 months. Monthly EUR1025.50 no PhD/under4 years, EUR1370 "
        "PhD/under10 or1470 PhD/over10; equality boundaries unstated. "
        "No PhD/over4 "
        "normally excluded; special over4–7-year cases possible. Non-Slovak "
        "citizenship; prior15/36-month residence/public-award exclusions "
        "and two-year repeat rule. Start February1–August31,2027; finish "
        "November30. Apply October31,2026 at16:00 CET.",
        (
            "valid certificate of eligibility",
            "not a business company",
            "more than 4 years",
            "not more than 7 years",
            "maximum of 6 years",
        ),
        incoming_common
        + [
            "Monthly tiers: no PhD and less than four years' experience "
            "EUR1025.50; PhD and less than ten years EUR1370; PhD and more "
            "than ten years EUR1470. Exactly four/ten year boundaries are "
            "not specified in the table, so no unsupported rate is selected.",
            "No-PhD candidates with more than four years are normally "
            "excluded; administrator/selection-committee discretion for "
            "more than four but at most seven years, at the lowest rate. "
            "PhD studies count as work experience, capped at six years "
            "when calculating the monthly funding tier.",
            "The published student/PhD travel allowance does not apply "
            "to the incoming teacher/researcher/artist category.",
        ],
    )
    funding = outgoing_funding(pages)
    outgoing_common = [
        "Eligible: Slovak citizens irrespective of permanent residence; "
        "EU/EEA/Swiss citizens with residence rights/permanent residence; "
        "third-country nationals with permanent/long-term residence in "
        "Slovakia. These are alternative citizenship/residence routes, "
        "not a Slovak-only citizenship whitelist.",
        "Foreign destinations are generally worldwide; Russia stays are "
        "currently prohibited. Country-specific funding rates are benefit "
        "options, not a closed host-country whitelist or proof of citizenship.",
        "Country-specific monthly support covers living expenses, not "
        "tuition; travel grant EUR0–1500 requested together with the "
        "scholarship and paid with the final installment. No tier clones.",
        "Full degree study abroad and overlapping publicly financed "
        "stays are excluded. Repeat applications require at least two "
        "years since last NSP award/receipt, with exceptional force-majeure "
        "consideration. Return obligations and host acceptance apply.",
        "Current call dated 2026 explicitly covers applicants resident "
        "in Slovakia. Student starts February1–April1,2027, completion "
        "by August31; other listed academics start February1–August31, "
        "completion by November30. General duration maxima do not "
        "override the current round's dates.",
    ]
    for index, track, anchor, title, summary, facts, detail in (
        (
            0,
            "student",
            "opuch_stud",
            "student",
            "MA or fourth-year integrated-degree students at Slovak "
            "HEIs: foreign "
            "study "
            "(one semester/trimester to one academic year) or thesis-related "
            "research/artistic stay (3–6 months). Complete BA/six semesters "
            "before departure and return to finish the Slovak degree. "
            "External students require full-time home-institution employment. "
            "Country-specific living support plus travel; "
            "residence/citizenship "
            "alternatives, not Slovak-only. Russia excluded. Current dates "
            "constrain general maxima. Deadline October31,2026 at16:00 CET.",
            ("diplomovej práce", "6 semestrov", "na plný pracovný úväzok"),
            [
                "Final-year BA applicants must enroll in the Slovak "
                "second-level "
                "degree before departure. External/part-time-format applicants "
                "must also be full-time employees of an eligible home "
                "institution "
                "and return to it after the stay."
            ],
        ),
        (
            1,
            "phd",
            "opuch_dokt",
            "PhD",
            "PhD students at Slovak HEIs, including eligible external "
            "education institutions, may undertake foreign study/research/"
            "artistic mobility for1–10months related to their doctorate. "
            "External-format students require full-time eligible home "
            "employment and return. Country-specific living support, not "
            "tuition, plus travel EUR0–1500; citizenship/residence "
            "alternatives "
            "apply. Russia excluded; current2027 dates constrain duration. "
            "Apply October31,2026 at16:00 CET.",
            ("doktorandi", "1 – 10 mesiacov", "na plný pracovný úväzok"),
            [
                "The foreign study/research/artistic stay must relate to the "
                "doctoral programme and its focus. External-format students "
                "must be full-time employees of an eligible home institution "
                "and return to it."
            ],
        ),
        (
            2,
            "postdoc",
            "opuch_vysk",
            "postdoctoral",
            "Full-time teachers/researchers at certified Slovak non-business "
            "institutions, PhD awarded at most10years ago; exceptional cases "
            ">10–13years possible. Foreign research stay2–6months for career "
            "development, with required home return. Country-specific living "
            "support, not tuition, plus travel EUR0–1500. "
            "Citizenship/residence "
            "alternatives, not Slovak-only; Russia excluded. Current2027 dates "
            "constrain duration. Apply October31,2026 at16:00 CET.",
            ("postdoktorandi", "nie je obchodnou spoločnosťou", "13 rokov"),
            [
                "PhD awarded no more than ten years before the deadline; "
                "administrator/selection-committee discretion may admit more "
                "than ten but no more than thirteen years. Full-time eligible "
                "home employment and return to the home institution required."
            ],
        ),
    ):
        add_track(
            records,
            pages,
            "outgoing",
            track,
            anchor,
            "NSP outgoing " + title + " scholarship – October 2026",
            summary,
            facts,
            outgoing_common + detail,
            funding[index],
        )
    validate_records(records)
    return records


def collect():
    """Collect all genuine current two-direction NSP scholarship tracks."""
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
    directory = os.path.join(
        tempfile.gettempdir(), "saia-publisher-pacing-" + str(os.getuid())
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
        "scholarships.sk"
        if host in ("scholarships.sk", "stipendia.sk", "saia.sk")
        or host.endswith(".saia.sk")
        else host
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
    state, budget = pace(url, interval)
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    request_headers = {"User-Agent": _USER_AGENT, **(headers or {})}
    if parsed.path == "/robots.txt" and (
        host in ("scholarships.sk", "stipendia.sk", "saia.sk")
        or host.endswith(".saia.sk")
    ):
        request_headers["Accept"] = "text/plain, */*;q=0.1"
    try:
        request = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers=request_headers,
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
            body = response.read(5_000_001)
            if len(body) > 5_000_000:
                raise AdapterError("Response exceeds 5 MB", "fetch")
            return response.status, response.headers, body
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
    """Validate all real records against the source and consumer contracts."""
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


def metadata(attempt, previous, records, error=None, checked_at=None):
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
            previous.get("last_checked_at")
            if error
            else checked_at
            or max(
                (record["last_checked_at"] for record in records),
                default=attempt,
            )
        ),
        "record_count": len(records),
        "message": (
            "Collection failed; last-good data preserved"
            if error
            else (
                f"Collected {len(records)} validated two-way NSP "
                f"scholarship tracks"
            )
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
        records = collect()
        checked_at = utc_now()
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
                last_checked_at=checked_at,
            )
        records.sort(key=lambda record: record["id"])
        records.sort(
            key=lambda record: record["published_at"] or "", reverse=True
        )
        validate_records(records)
    except Exception as error:
        failure, records = error, old
    outcome = metadata(
        attempt,
        previous,
        records,
        failure,
        checked_at=None if failure else checked_at,
    )
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
