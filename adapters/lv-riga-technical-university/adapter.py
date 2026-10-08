"""Actual RTU doctoral awards, mobility grants and postdoctoral calls."""

import argparse
import base64
from datetime import date, datetime, timezone
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

SOURCE_ID = "lv-riga-technical-university"
SOURCE_URL = (
    "https://www.rtu.lv/en/studies/doctoral-studies/scholarships-and-grants"
)
WEBSITE_URL = "https://www.rtu.lv/"
LANGUAGE = "en"
PUBLISHER_COUNTRY = "LV"
PUBLISHER_TYPE = "university"
ATTRIBUTION = "Riga Technical University"
_POSTDOC = (
    "https://www.rtu.lv/en/research/post-doctoral/activity-post-doctoral-r"
    "esearch-aid"
)
_STATE = "https://www.rtu.lv/en/studies/scholarships-2"
_DOCTORAL_NOTICE = (
    "https://www.rtu.lv/en/iad/about-us-4/af-news/open/"
    "applications-are-invited-for-the-erdf-doctoral-grant-competition"
)
_MOBILITY_BASE = "https://www.rtu.lv/en/internationalization/outgoing-exchange/"
_ROBOTS = {}


class Node:
    """Retain publisher structure and text, excluding scripts and navigation."""

    def __init__(self, tag="", attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []

    def text(self):
        if self.tag in ("script", "style"):
            return ""
        parts = [x.text() if isinstance(x, Node) else x for x in self.children]
        return " ".join(" ".join(parts).split())

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def has_class(self, name):
        return name in self.attrs.get("class", "").split()


class PublisherHTML(HTMLParser):
    """Read the actual page content and complete outer funding accordions."""

    def __init__(self, document):
        super().__init__(convert_charrefs=True)
        self.root, self.complete = Node(), False
        self.stack = [self.root]
        self.feed(document)
        blocks = [
            n
            for n in self.root.walk()
            if n.has_class("uce-iframe-content-container")
        ]
        if len(blocks) != 1 or not self.complete:
            raise AdapterError(
                "Incomplete or changed publisher content", "parse"
            )
        self.content = blocks[0]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.stack[-1])
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

    def handle_endtag(self, tag):
        if tag == "html":
            self.complete = True
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, value):
        self.stack[-1].children.append(value)


def publisher_page(url):
    delay = check_robots(url)
    return PublisherHTML(fetch_source(url, min_interval=delay)).content


def required(text, *phrases):
    """Fail rather than publish stale facts after the source changes."""
    for phrase in phrases:
        if phrase.casefold() not in text.casefold():
            raise AdapterError("Funding terms changed: " + phrase, "parse")


def match(text, expression):
    found = re.search(expression, text, re.IGNORECASE)
    if found is None:
        raise AdapterError("Required funding fact is missing", "parse")
    return found


def funding_record(
    title,
    url,
    identity,
    categories,
    eligibility,
    benefits,
    summary,
    evidence,
    kind="programme-overview",
):
    record = make_record(
        title,
        url,
        categories,
        kind=kind,
        method="publisher-funding-terms",
        host_countries=[] if "mobility" in identity else ["LV"],
        evidence=["Funding terms: " + url, *evidence],
    )
    record.update(
        id=hashlib.sha256(f"{SOURCE_ID}|{identity}".encode()).hexdigest()[:24],
        source_record_id=identity,
        attribution=ATTRIBUTION,
        eligibility=eligibility,
        benefits=benefits,
        summary=summary,
    )
    return record


def outer_panels(content):
    for node in content.walk():
        if not node.has_class("collapse-panel"):
            continue
        parent, nested = node.parent, False
        while parent is not content and parent is not None:
            if parent.has_class("collapse-panel"):
                nested = True
                break
            parent = parent.parent
        if not nested:
            yield node


def doctoral_records(content):
    records, seen = [], set()
    for panel in outer_panels(content):
        titles = [n.text() for n in panel.walk() if n.tag == "h6"]
        if not titles:
            raise AdapterError("Funding section has no title", "parse")
        title, text = titles[0], panel.text()
        if title in seen:
            raise AdapterError("Duplicate funding section", "parse")
        seen.add(title)
        if title == "Frequently Asked Questions (FAQ)":
            continue
        if title == "Erasmus+ Scholarship for Mobility":
            required(text, "doctoral students", "5–30 days", "2–12 months")
            continue  # Represent the four distinct funded application routes.
        if title == "Tuition Fee Discounts":
            required(text, "self-funded", "not applicable", "RTU employees")
            eligibility = [
                (
                    "Self-funded doctoral students; state-funded places "
                    "and RTU employees excluded."
                ),
                (
                    "Publication and conference criteria depend on "
                    "doctoral year; supervisor and academic approvals "
                    "required."
                ),
            ]
            benefits = ["Full or partial tuition discount, per academic year."]
            summary = (
                "Annual full or partial doctoral tuition discounts for "
                "self-funded students meeting publication, conference and "
                "approval requirements; state-funded places and RTU "
                "employees are excluded. Apply to the faculty dean or "
                "institute/academy director. No exact deadline is "
                "published."
            )
            identity, categories = "doctoral-tuition-discount", ["scholarships"]
        elif title == "Monthly Scholarship":
            required(
                text,
                "state-funded",
                "no academic debts",
                "first ten working days",
                "Section 47(1)",
            )
            amount = match(
                text, r"monthly scholarship amount.*?is ([\d.,]+) EUR per month"
            )[1]
            eligibility = [
                (
                    "State-funded doctoral students without academic "
                    "debts and with required examinations completed."
                ),
                (
                    "Study start before 1 September 2024, or the stated "
                    "Section 47(1) exceptions for later entrants."
                ),
            ]
            benefits = [f"EUR {amount} per month."]
            summary = (
                f"Monthly doctoral scholarship of EUR {amount} for "
                f"qualifying state-funded students without academic "
                f"debts. Later entrants need the stated Higher Education "
                f"Law exceptions. Apply during the first ten working days "
                f"of each semester; no exact calendar deadline is "
                f"supplied."
            )
            identity, categories = "doctoral-monthly-scholarship", [
                "scholarships"
            ]
        elif title == "RTU Doctoral Grants":
            required(text, "not been announced", "doctoral student's salary")
            amount = match(
                text, r"approximately ([\d, ]+) EUR per academic year"
            )[1].strip()
            eligibility = [
                "Doctoral students; competition conditions vary by year.",
                (
                    "The publisher explicitly says the grant application "
                    "has not been announced."
                ),
            ]
            salary = match(text, r"~([\d, ]+) EUR must be used")[1].strip()
            research = match(text, r"~([\d, ]+) EUR can be used")[1].strip()
            benefits = [
                f"Approximately EUR {amount} per academic year.",
                (
                    f"Approximately EUR {salary} salary/social security "
                    f"and EUR {research} research/publicity allocation."
                ),
            ]
            summary = (
                f"RTU doctoral grant programme providing approximately "
                f"EUR {amount} annually for salary and research/publicity "
                f"costs. The publisher says applications have not been "
                f"announced; annual competition conditions vary. This is "
                f"a genuine funding programme, not a current open call."
            )
            identity, categories = "rtu-doctoral-grants", ["grants"]
        elif title == "Recovery and Resilience Mechanism (RRM) Grants":
            required(
                text,
                "not been announced",
                "0.5",
                "at least 12 months",
                "5.2.1.1.i.0/2/24/I/CFLA/003",
            )
            amount = match(
                content.text(), r"ANM doctoral grant funds, (\d+) EUR"
            )[1]
            eligibility = [
                "Doctoral grant employment for at least 12 months at 0.5 FTE.",
                (
                    "Applications are explicitly not announced; old FAQ "
                    "dates are not current deadlines."
                ),
            ]
            benefits = [
                (
                    f"EUR {amount} per month for 0.5 FTE including "
                    f"employer social security."
                ),
                "Up to EUR 500 per month for research expenses.",
            ]
            required(text, "500 EUR per month")
            summary = (
                f"Recovery/Resilience doctoral programme: EUR {amount} "
                f"monthly including employer social security for at least "
                f"12 months at 0.5 FTE, plus up to EUR 500 monthly "
                f"research expenses. Applications are not announced; "
                f"historical FAQ dates do not establish a new call."
            )
            identity, categories = "rrm-5.2.1.1.i.0-2-24-I-CFLA-003", ["grants"]
        elif title.startswith(
            "RTU Doctoral Grants for Supporting Scientific Excellence"
        ):
            records.append(erdf_record(title, text))
            continue
        else:
            raise AdapterError("Unreviewed funding section: " + title, "parse")
        records.append(
            funding_record(
                title,
                SOURCE_URL,
                identity,
                categories,
                eligibility,
                benefits,
                summary,
                [title],
            )
        )
    expected = {
        "Tuition Fee Discounts",
        "Monthly Scholarship",
        "RTU Doctoral Grants",
        "Erasmus+ Scholarship for Mobility",
        "Recovery and Resilience Mechanism (RRM) Grants",
        "Frequently Asked Questions (FAQ)",
        "RTU Doctoral Grants for Supporting Scientific Excellence in Smart "
        "Specialization Areas (1.1.1.8/1/24/I/007)",
    }
    if seen != expected:
        raise AdapterError("Incomplete doctoral funding inventory", "parse")
    return records


def erdf_record(title, text):
    required(
        text,
        "1.1.1.8/1/24/I/007",
        "RTU cooperation partners",
        "RIS3",
        "additional employment of at least 30%",
    )
    selection = match(text, r"announced (\w+) selection competition")[1]
    closing = match(
        text, r"submitted by ([A-Za-z]+ \d{1,2}, \d{4}) at (\d{2}[:.]\d{2})"
    )
    day = datetime.strptime(closing[1], "%B %d, %Y").date()
    clock = closing[2].replace(".", ":")
    datetime.strptime(clock, "%H:%M")
    amount = match(text, r"grant up to (\d+) EUR per month")[1]
    months = match(text, r"Grant duration: (\d+) - (\d+) months")
    record = funding_record(
        title + " — " + selection + " selection",
        _DOCTORAL_NOTICE,
        "erdf-1.1.1.8-1-24-I-007-" + selection,
        ["grants"],
        [
            (
                "Doctoral students at RTU, Liepāja/Rezekne academies or "
                "cooperation partners; RIS3 research."
            ),
            (
                "50% employment plus at least 30% additional employment "
                "at RTU or a project partner."
            ),
        ],
        [
            (
                f"Up to EUR {amount} monthly: EUR 1300 salary/social "
                f"security and EUR 500 research."
            ),
            f"Duration {months[1]}–{months[2]} months.",
        ],
        (
            f"ERDF doctoral {selection} selection for "
            f"RTU/academy/cooperation-partner doctoral researchers in "
            f"RIS3 areas. Up to EUR {amount} monthly for "
            f"{months[1]}–{months[2]} months. Application deadline "
            f"{day.isoformat()} at {clock}; source time zone is "
            f"unspecified. Capacity and project statistics are not "
            f"separate awards."
        ),
        [
            "Project: 1.1.1.8/1/24/I/007",
            "Selection: " + selection,
            "Application deadline: " + closing[0],
        ],
        kind="opportunity",
    )
    required(text, "1300 EUR", "500 EUR")
    record.update(
        deadline=day.isoformat(),
        deadline_precision="date",
        deadline_local_time=clock,
        deadline_timezone=None,
    )
    today = datetime.now(timezone.utc).date()
    record["status"] = (
        "open" if day > today else "expired" if day < today else "unknown"
    )
    return record


def postdoc_record(content):
    text = content.text()
    required(
        text,
        "Latvian or foreign researchers",
        "doctoral degree not more than 10 years",
    )
    closing = match(
        text,
        (
            "deadline to prepare an application.*?is ([A-Za-z]+ \\d{1,2}, "
            "\\d{4}) (\\d{1,2}:\\d{2} [AP]M) \\(EET\\)"
        ),
    )
    local = datetime.strptime(
        closing[1] + " " + closing[2], "%B %d, %Y %I:%M %p"
    )
    deadline = local.isoformat(timespec="seconds") + "+02:00"
    access = match(
        text,
        (
            "deadline for the request access.*?is ([A-Za-z]+ \\d{1,2}, "
            "\\d{4}) (\\d{1,2}:\\d{2} [AP]M) \\(EET\\)"
        ),
    )
    round_number = match(text, r"regulations of the (\d+)th call")[1]
    cap = match(text, r"funding for a research application is EUR ([\d ]+)")[
        1
    ].strip()
    salary = match(text, r"gross salary EUR ([\d ]+) per month")[1].strip()
    required(text, "EUR 1000 per month", "710 euros", "24 months")
    record = funding_record(
        "RTU postdoctoral research grant — selection " + round_number,
        _POSTDOC,
        "postdoctoral-1.1.1.9-selection-" + round_number,
        ["fellowships", "grants"],
        [
            (
                "Latvian or foreign researchers with a doctoral degree "
                "obtained no more than 10 years before the selection "
                "deadline."
            ),
            (
                "Research in the stated RTU priority/RIS3 areas; "
                "application prepared in Latvian in POSTDOC."
            ),
        ],
        [
            f"Maximum EUR {cap} for up to 24 months.",
            (
                f"Gross monthly salary EUR {salary}, including taxes; EUR "
                f"1000 monthly unit costs and possible EUR 710 relocation "
                f"payment."
            ),
        ],
        (
            f"RTU postdoctoral selection {round_number}: Latvian or "
            f"foreign researchers within ten years of their doctorate; "
            f"priority research areas. Up to EUR {cap} for 24 months, "
            f"salary EUR {salary} monthly plus unit costs and possible "
            f"relocation support. Application deadline {closing[1]} "
            f"{closing[2]} EET; the earlier access-request milestone is "
            f"not the application deadline."
        ),
        ["Application: " + closing[0], "System-access request: " + access[0]],
        kind="opportunity",
    )
    record.update(
        deadline=deadline,
        deadline_timezone="EET",
        deadline_precision="minute",
        access_request_deadline_source=access[0],
    )
    record["status"] = (
        "open"
        if datetime.fromisoformat(deadline) > datetime.now(timezone.utc)
        else "expired"
    )
    return record


def state_record(content):
    text = content.text()
    required(
        text,
        "acceptance letter",
        "signed an agreement with Latvia",
        "completed at least one academic year",
    )
    year = match(text, r"for the (\d{4}/\d{4}) academic year")[1]
    return funding_record(
        "Latvian State Scholarship via RTU — " + year,
        _STATE,
        "latvian-state-scholarship-" + year,
        ["scholarships"],
        [
            (
                "Foreign students, researchers or teaching staff from the "
                "publisher-linked bilateral/reciprocal eligible "
                "countries; no complete country list is provided on this "
                "RTU page."
            ),
            (
                "Bachelor/first-level applicants need at least one "
                "completed academic year; RTU acceptance letter and "
                "stated admission process required."
            ),
        ],
        [
            (
                "Latvian state funding for study, research or "
                "summer-school participation at Latvian higher education "
                "institutions."
            )
        ],
        (
            f"Latvian State Scholarship {year} through RTU for qualifying "
            f"foreign applicants from bilateral/reciprocal countries. RTU "
            f"acceptance documentation is required. The page also "
            f"contains stale 2025 application text; no exact current "
            f"deadline or country eligibility list is inferred."
        ),
        [
            "Academic year: " + year,
            (
                "RTU acceptance letter required; older February/March "
                "2025 wording is not a current deadline."
            ),
        ],
    )


_MOBILITY = {
    "study-mobility-programme-countries": (
        "RTU Erasmus+ Europe study scholarship",
        "mobility-study-europe",
        "6.75",
        "February 20 and September 20",
        ["scholarships"],
    ),
    "traineeship-mobility-programme-countries": (
        "RTU Erasmus+ Europe traineeship scholarship",
        "mobility-traineeship-europe",
        "6.25",
        "March 31 and October 31",
        ["scholarships", "internships"],
    ),
    "staff-mobility-europe": (
        "RTU Erasmus+ staff teaching/training grant",
        "mobility-staff-europe",
        None,
        None,
        ["grants", "training"],
    ),
    "erasmus-mobility-to-partner-countries": (
        "RTU Erasmus+ World study scholarship",
        "mobility-study-world",
        "6.25",
        "February 25 and September 25",
        ["scholarships"],
    ),
}


def mobility_record(path, content):
    title, identity, grade, window, categories = _MOBILITY[path]
    text = content.text()
    if grade is None:
        required(
            text,
            "5 (five) working days",
            "March",
            "June",
            "September",
            "December",
            "grant is not paid for travel days",
        )
        eligibility = [
            (
                "RTU teaching or administrative staff; teaching/training "
                "mobility, not a conference-only trip."
            ),
            (
                "Annual application months: March, June, September and "
                "December; no exact deadline is stated."
            ),
        ]
        benefits = [
            (
                "Travel support and subsistence grant for at most five "
                "working days; no grant for nonworking travel days."
            )
        ]
        summary = (
            "RTU Erasmus+ teaching/training mobility grant for eligible "
            "staff, with travel support and up to five working days of "
            "funded activity. Conference-only training is unsupported; "
            "nonworking travel days are not funded. Apply in March, June, "
            "September or December; no exact current deadline is stated."
        )
    else:
        required(text, grade, "academic debts", window, "partly cover")
        world = identity.endswith("world")
        eligibility = [
            (
                f"RTU students without academic debts; weighted average "
                f"at least {grade} and the stated language/mobility "
                f"objective requirements."
            ),
            (
                "At least bachelor semester three; master's students."
                if world
                else (
                    "At least bachelor semester three or master's "
                    "semester two."
                )
            ),
        ]
        benefits = [
            (
                "Mobility scholarship partly covers extra overseas "
                "living/mobility costs; additional funding may be needed."
            )
        ]
        if "traineeship" in identity:
            required(
                text,
                "academic leave",
                "600",
                "Only then the scholarship can be provided",
            )
            eligibility += [
                (
                    "Academic leave excludes the Erasmus scholarship; a "
                    "receiving host must be secured for payment."
                ),
                (
                    "Employer pay above EUR 600/month excludes Latvian "
                    "state co-financing only, not the whole Erasmus grant."
                ),
            ]
            benefits += [
                (
                    "Student traineeship duration: minimum two months, up "
                    "to twelve months at one study level; doctoral short "
                    "mobility has separate published terms."
                )
            ]
            summary = (
                f"RTU Europe traineeship scholarship: average at least "
                f"{grade}, bachelor semester three or master semester "
                f"two, no academic debts or academic leave, language "
                f"requirements and receiving host. Costs are partly "
                f"funded. Recurring deadlines: {window}, without a "
                f"supplied year. Employer pay over EUR 600 excludes "
                f"Latvian co-financing only."
            )
        else:
            benefits += [
                (
                    "Study duration: three to twelve months."
                    if world
                    else (
                        "Study duration: two to twelve months; doctoral "
                        "short mobility is separately described."
                    )
                )
            ]
            summary = (
                f"RTU Erasmus+ {'World' if world else 'Europe'} study "
                f"scholarship: average at least {grade}, bachelor "
                f"semester three or "
                f"{'master enrolment' if world else 'master semester two'}, "
                f"no academic debts, stated language and study "
                f"requirements. Funding covers part of extra mobility "
                f"costs. Recurring deadlines: {window}; no calendar year "
                f"or exact current opening is invented."
            )
    return funding_record(
        title,
        _MOBILITY_BASE + path,
        identity,
        categories,
        eligibility,
        benefits,
        summary,
        [
            "Application window: "
            + (window or "March/June/September/December"),
            "Distinct publisher application and funding route.",
        ],
    )


def collect():
    """Discover funding sections and distinct linked application routes."""
    _ROBOTS.clear()
    content = publisher_page(SOURCE_URL)
    records = doctoral_records(content)
    notice = publisher_page(_DOCTORAL_NOTICE).text()
    notice_closing = match(
        notice,
        r"submitted by ([A-Za-z]+ \d{1,2}, \d{4}) at (\d{2}[:.]\d{2})",
    )
    notice_day = datetime.strptime(notice_closing[1], "%B %d, %Y").date()
    erdf = [record for record in records if record["url"] == _DOCTORAL_NOTICE]
    if (
        len(erdf) != 1
        or erdf[0]["deadline"] != notice_day.isoformat()
        or erdf[0]["deadline_local_time"] != notice_closing[2].replace(".", ":")
    ):
        raise AdapterError("Doctoral call notices disagree", "parse")
    records.append(postdoc_record(publisher_page(_POSTDOC)))
    records.append(state_record(publisher_page(_STATE)))
    for path in _MOBILITY:
        records.append(
            mobility_record(path, publisher_page(_MOBILITY_BASE + path))
        )
    validate_records(records)
    return records


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
_USER_AGENT = "YouthOpp/1.0 (+https://youthopps.org/contact/)"


class AdapterError(Exception):
    def __init__(self, message, stage="parse", status=None):
        super().__init__(message)
        self.stage, self.status = stage, status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, url):
        return None


_OPENER = urllib.request.build_opener(NoRedirect)


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
    key = hashlib.sha256(host.encode()).hexdigest()
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
                    current,
                    interval=min_interval,
                    headers={
                        "Accept": (
                            "text/plain,*/*;q=0.8"
                            if urllib.parse.urlsplit(current).path
                            == "/robots.txt"
                            else (
                                "text/html,application/xhtml+xml;q=0.9,"
                                "*/*;q=0.8"
                            )
                        )
                    },
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
                        f"Publisher returned HTTP {status} at "
                        + urllib.parse.urlunsplit(
                            (
                                "https",
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
        except AdapterError as error:
            if attempt or error.status not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if attempt:
                raise AdapterError(
                    "Publisher connection failed: " + safe_error(error), "fetch"
                ) from error
    raise AdapterError("Publisher could not be read", "fetch")


def check_robots(url):
    parsed = urllib.parse.urlsplit(url)
    origin = (parsed.scheme, parsed.netloc)
    try:
        text = _ROBOTS.get(origin)
        if text is None:
            text = fetch_source(
                urllib.parse.urlunsplit(
                    (parsed.scheme, parsed.netloc, "/robots.txt", "", "")
                )
            )
            _ROBOTS[origin] = text
    except AdapterError as error:
        if error.status == 404:
            return 6
        raise
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
        if (
            record.get("attribution") != ATTRIBUTION
            or record.get("eligible_countries") != []
            or not record.get("source_record_id")
            or not record.get("eligibility")
            or not record.get("benefits")
        ):
            raise AdapterError(
                "Missing funding evidence or attribution", "validate"
            )
        for field in ("eligibility", "benefits"):
            if not isinstance(record[field], list) or any(
                not isinstance(value, str) or not value
                for value in record[field]
            ):
                raise AdapterError("Invalid funding evidence", "validate")
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
                    if field == "deadline" and re.fullmatch(
                        r"\d{4}-\d{2}-\d{2}", value
                    ):
                        date.fromisoformat(value)
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
        if (
            not isinstance(record.get("summary"), str)
            or len(record["summary"]) > 600
            or re.search("<[^>]+>", record["summary"])
        ):
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
                    (
                        "Source changed concurrently; newer remote data "
                        "was not overwritten"
                    ),
                    "publish",
                )
            snapshot = latest
            if attempt == 2:
                raise AdapterError(
                    (
                        "Publication failed; durable remote outcome could "
                        "not be updated"
                    ),
                    "publish",
                )


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
        "message": (
            "Collection failed; last-good data preserved"
            if error
            else f"Collected and validated {len(records)} records"
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
            "Write only this source to YouthOpps/data-source using "
            "DATA_SOURCE_TOKEN"
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
                    "Publication failed; durable failure status could "
                    "not be recorded. Remote data was not "
                    "force-overwritten."
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
                    ("message"): (
                        "Run could not complete; durable status " "unavailable"
                    ),
                    "error": safe_error(error),
                    "failure_stage": getattr(error, "stage", "publish"),
                },
                ensure_ascii=False,
            )
        )
        sys.exit(1)
