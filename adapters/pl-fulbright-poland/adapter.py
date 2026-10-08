"""Collect Fulbright Poland programme calls and institutional exchanges."""

import argparse
import base64
import contextlib
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
from zoneinfo import ZoneInfo

SOURCE_ID = "pl-fulbright-poland"
SOURCE_URL = "https://fulbright.edu.pl/junior-research/"
WEBSITE_URL = "https://fulbright.edu.pl/"
LANGUAGE = "pl"
PUBLISHER_COUNTRY = "PL"
PUBLISHER_TYPE = "scholarship-commission"
ATTRIBUTION = "Polish-U.S. Fulbright Commission (CC BY-NC 4.0)"
OUTGOING_URL = WEBSITE_URL + "stypendia-do-usa/"
INCOMING_URL = WEBSITE_URL + "amerykanie-w-polsce/"
LICENSE_URL = "https://creativecommons.org/licenses/by-nc/4.0/"


class Node:
    """Minimal HTML tree retaining official links and structural boundaries."""

    def __init__(self, tag="", attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []

    def text(self):
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
    root = PublisherHTML(fetch_source(url, min_interval=delay)).root
    if "CC BY-NC 4.0" not in root.text():
        raise AdapterError("Publisher content licence changed", "access")
    return root


_MONTHS = {
    "stycznia": 1,
    "lutego": 2,
    "marca": 3,
    "kwietnia": 4,
    "maja": 5,
    "czerwca": 6,
    "lipca": 7,
    "sierpnia": 8,
    "września": 9,
    "października": 10,
    "listopada": 11,
    "grudnia": 12,
}
_LOCAL_PATHS = {
    "graduate-student",
    "notre-dame-llm",
    "junior-research",
    "research-stanford",
    "polish-scholar",
    "polish-studies",
    "stem-impact",
    "sir",
    "schuman-award",
    "biolab",
    "topminds",
    "eta",
    "specialist",
    "inter-country",
}
_EU_COUNTRIES = [
    "AT",
    "BE",
    "BG",
    "HR",
    "CY",
    "CZ",
    "DK",
    "EE",
    "FI",
    "FR",
    "DE",
    "GR",
    "HU",
    "IE",
    "IT",
    "LV",
    "LT",
    "LU",
    "MT",
    "NL",
    "PL",
    "PT",
    "RO",
    "SK",
    "SI",
    "ES",
    "SE",
]


def content(root):
    return one(nodes(root, class_name="entry-content"), "programme content")


def sections(node):
    """Retain distinct publisher heading sections without menu/sidebar text."""
    result = []
    active = None
    for child in node.children:
        if not isinstance(child, Node):
            continue
        if child.tag in ("h2", "h3"):
            active = [child, []]
            result.append(active)
        elif active:
            active[1].append(child)
    return result


def closing_date(text):
    """Require an explicit closing day, Polish month and year."""
    match = re.search(
        r"\bdo (\d{1,2}) (" + "|".join(_MONTHS) + r") (20\d{2})\b",
        text,
        re.I,
    )
    if not match:
        return None
    day, month, year = match.groups()
    date = datetime(int(year), _MONTHS[month.lower()], int(day))
    clock = re.search(
        r"(?:godziny|godz\.)\s*(\d{1,2}):(\d{2}) czasu polskiego",
        text,
        re.I,
    )
    if clock:
        local = date.replace(
            hour=int(clock.group(1)),
            minute=int(clock.group(2)),
            tzinfo=ZoneInfo("Europe/Warsaw"),
        )
        return (
            local.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    return date.date().isoformat()


def programme_deadline(body):
    # Current opening/closure notices take priority over old FAQ/calendar text.
    paragraphs = [n.text() for n in nodes(body, "p") if n.text()]
    for text in paragraphs[:3]:
        if re.search(r"wniosk|zgłoszen|[Nn]abór", text):
            result = closing_date(text)
            if result:
                return result
    for heading, children in sections(body):
        if not heading.text().startswith("Termin składania wniosków"):
            continue
        for child in children:
            text = child.text()
            if re.search(r"wniosk|[Nn]abór|rekrut", text):
                result = closing_date(text)
                if result:
                    return result
    return None


def status_for(deadline, text):
    if re.search(r"Nabór (?:został zakończony|jest zamknięty)", text):
        return "expired"
    if deadline:
        today = datetime.now(ZoneInfo("Europe/Warsaw")).date().isoformat()
        if len(deadline) == 10:
            # A calendar date is not a fabricated end-of-day timestamp.
            if deadline < today:
                return "expired"
            if deadline == today:
                return "unknown"
        elif deadline <= utc_now():
            return "expired"
        if re.search(r"Nabór jest otwarty|[Nn]abór.*(?:trwa|przyjm)", text):
            return "open"
    return "unknown"


def local_programme(root, path):
    body = content(root)
    title = one(nodes(root, "h1"), "programme title").text()
    text = body.text()
    url = WEBSITE_URL + path + "/"
    institutional = path in ("eta", "specialist", "inter-country", "sir")
    categories = ["grants"] if institutional else ["scholarships"]
    if path == "biolab":
        categories = ["internships"]
    elif path == "topminds":
        categories = ["training"]
    record = make_record(
        title,
        url,
        categories,
        kind="institutional-grant" if institutional else "opportunity",
        method="publisher-programme-content",
        evidence=[url],
    )
    if path == "topminds":
        record["kind"] = "programme-overview"
    record["deadline"] = programme_deadline(body)
    record["status"] = status_for(record["deadline"], text)
    # Only positive requirements populate citizenship, never residency or names.
    if path not in ("biolab", "sir") and re.search(
        r"polskim obywatelstw|polskie obywatelstwo|polski[emgo]+ obywatelstw",
        text,
        re.I,
    ):
        record["eligible_countries"] = ["PL"]
    if path == "sir":
        record["host_countries"] = ["US"]
    elif institutional and re.search(r"polskich|w Polsce|terytorium RP", text):
        record["host_countries"] = ["PL"]
    elif path == "topminds":
        record["language"] = "en"
    elif re.search(r"\bUSA\b|Stanach Zjednoczonych", text):
        record["host_countries"] = ["US"]
    summary = (
        "Official programme; individual degree, affiliation and "
        "application conditions apply."
    )
    if institutional:
        summary = (
            "Institutional application for hosting an academic visitor in "
            "Poland; this is not an individual student application."
        )
    if path == "sir":
        summary = (
            "A US university submits the institutional request, not an "
            "individual. Polish nominees require Polish citizenship; "
            "nominee recruitment occurs only if the administrator requests "
            "candidates. No current individual nomination call is identified."
        )
    elif path == "biolab":
        summary = (
            "One research internship across four US institutions. Polish "
            "citizenship is not required; US citizenship or permanent "
            "residency disqualifies applicants. Active master's/doctoral "
            "status in a Polish institution must be maintained; non-Polish "
            "applicants must also have completed a bachelor's in Poland."
        )
    elif path == "topminds":
        summary = (
            "Mentoring programme for doctoral candidates and final-year "
            "master's students in Polish institutions. The published "
            "closing day has no explicit year (October 29, 16:00 CEST)."
        )
    if record["eligible_countries"] == ["PL"]:
        summary += " Polish citizenship is required."
    if re.search(
        r"(?:brak|nie masz) podwójnego obywatelstwa polsko-amerykańskiego",
        text,
        re.I,
    ):
        summary += (
            " Dual Polish-US citizenship and US permanent residency are "
            "excluded."
        )
    for heading, children in sections(body):
        if not heading.text().startswith("Długość"):
            continue
        duration = " ".join(n.text() for n in children)
        interval = re.search(r"od (\d+) do (\d+) (miesięcy|tygodni)", duration)
        single = re.search(r"okres (\d+) miesięcy", duration)
        if interval:
            unit = "months" if interval.group(3) == "miesięcy" else "weeks"
            summary += (
                f" Published duration: {interval.group(1)}–"
                f"{interval.group(2)} {unit}."
            )
        elif single:
            summary += f" Published duration: {single.group(1)} months."
        elif "Jeden rok akademicki" in duration:
            summary += " Published duration: one academic year."
        elif "pierwszy rok studiów" in duration:
            summary += " Initial funding is for the first study year."
        break
    if record["deadline"]:
        summary += " Published closing date: " + record["deadline"] + "."
    elif path in ("sir", "inter-country"):
        summary += (
            " No complete dated closing instant is published in the "
            "reviewed programme section."
        )
    record["summary"] = summary
    return record


_NATO_COUNTRIES_EXCLUDING_US = [
    "AL",
    "BE",
    "BG",
    "CA",
    "HR",
    "CZ",
    "DK",
    "EE",
    "FI",
    "FR",
    "DE",
    "GR",
    "HU",
    "IS",
    "IT",
    "LV",
    "LT",
    "LU",
    "ME",
    "NL",
    "MK",
    "NO",
    "PL",
    "PT",
    "RO",
    "SK",
    "SI",
    "ES",
    "SE",
    "TR",
    "GB",
]


def schuman_variants(root):
    """Preserve five named variants with their separate qualifications."""
    body = content(root)
    text = body.text()
    if not re.search(r"państw członkowskich NATO", text):
        raise AdapterError("Schuman variant eligibility changed", "parse")
    records = []
    for heading, children in sections(body):
        title = heading.text()
        if heading.tag != "h3" or not title.startswith("Fulbright"):
            continue
        anchor = heading.attrs.get("id")
        if not anchor:
            raise AdapterError("Missing Schuman variant identity", "parse")
        variant = " ".join(n.text() for n in children)
        url = WEBSITE_URL + "schuman-award/#" + anchor
        record = make_record(
            title,
            url,
            ["scholarships"],
            kind="programme-overview",
            method="publisher-named-schuman-variant",
            evidence=[url],
        )
        record["eligible_countries"] = sorted(_EU_COUNTRIES)
        record["host_countries"] = ["US"]
        if "NATO Security" in title:
            record["eligible_countries"] = sorted(_NATO_COUNTRIES_EXCLUDING_US)
            summary = (
                "NATO citizens with a PhD undertake three months of "
                "NATO-related research/teaching at a US academic "
                "institution."
            )
        elif title == "Fulbright Schuman Doctoral Research Award":
            summary = (
                "EU citizens with at least a bachelor's degree undertake "
                "doctoral study/research in the US. Projects must address "
                "EU-US relations or EU policy and at least two EU member "
                "states."
            )
        elif "Post-Doctoral" in title:
            summary = (
                "EU citizens with a PhD undertake US research/teaching "
                "addressing EU-US relations or EU policy and at least two "
                "EU member states."
            )
        elif "International Educator" in title:
            summary = (
                "EU citizens with at least a bachelor's degree and "
                "international-education employment at a European "
                "university conduct US research; a US affiliation letter is "
                "required."
            )
        elif "Innovation" in title:
            record["host_countries"] = []
            summary = (
                "EU citizens, with or without a PhD, research the "
                "policy-technology interface and EU-US relations. This "
                "variant states research in one or two EU member states, "
                "without naming specific host countries."
            )
        else:
            raise AdapterError("Unreviewed Schuman variant", "parse")
        if not variant:
            raise AdapterError("Missing substantive Schuman variant", "parse")
        record["summary"] = summary + (
            " US citizenship or US permanent residency disqualifies "
            "applicants. The annual December 1 deadline has no explicit "
            "year."
        )
        records.append(record)
    if len(records) != 5:
        raise AdapterError("Schuman named variant inventory changed", "parse")
    return records


def incoming_programmes(root):
    body = content(root)
    records = []
    for heading, children in sections(body):
        title = heading.text()
        if not title.startswith("Fulbright"):
            continue
        text = " ".join(n.text() for n in children)
        links = [a for child in children for a in nodes(child, "a")]
        anchor = heading.attrs.get("id")
        if not anchor:
            raise AdapterError("Missing incoming programme anchor", "parse")
        url = INCOMING_URL + "#" + anchor
        record = make_record(
            title,
            url,
            ["scholarships"],
            kind="programme-overview",
            method="publisher-incoming-programme",
            evidence=[url],
        )
        if re.search(
            r"obywatel[ie].*Stanów Zjednoczonych|obywatelstwem amerykańskim",
            text,
        ):
            record["eligible_countries"] = ["US"]
        if re.search(r"w Polsce|do Polski", text):
            record["host_countries"] = ["PL"]
        record["summary"] = (
            "Published individual exchange programme. Qualifications and "
            "institutional invitation requirements depend on the programme; "
            "no dated deadline is stated here."
        )
        if "U.S. Scholar" in title:
            record["summary"] = (
                "US citizens with a PhD or terminal degree undertake 3–10 "
                "months of teaching/research in Poland. The annual "
                "February–September calendar has no exact current year."
            )
        elif "U.S. Student" in title:
            record["summary"] = (
                "US citizens with a bachelor's degree before the grant, "
                "but before a PhD, undertake 9–10 months of research in "
                "Poland. A Polish host-institution invitation letter is "
                "required. No dated application round is stated here."
            )
        elif "English Teaching Assistant" in title:
            record["summary"] = (
                "US citizens with a bachelor's degree, before a PhD, teach "
                "English in Poland for nine months. No invitation letter "
                "is required; the Commission allocates host universities. "
                "The institutional host application is a separate call."
            )
        if "Specialist" in title:
            record["summary"] = (
                "American specialist visits to invited Polish institutions for "
                "14–42 days. Host-institution application is a separate call. "
                "No dated individual deadline is stated here."
            )
        if "Schuman" in title:
            record["summary"] = (
                "US-citizen research in the European Union; the reverse "
                "EU-citizen US research call has its own programme page. No "
                "complete dated deadline or specific host-country list is "
                "stated here."
            )
        records.append(record)
        if "U.S. Scholar" in title:
            for link in links:
                target = link.attrs.get("href", "")
                if (
                    not target.startswith(
                        "https://fulbrightscholars.org/award/"
                    )
                    or not link.text()
                ):
                    continue
                partner = make_record(
                    link.text().removesuffix(" NEW"),
                    target,
                    ["scholarships"],
                    kind="programme-overview",
                    host_countries=["PL"],
                    method="publisher-named-partner-programme",
                    evidence=[url, target],
                )
                partner["eligible_countries"] = record["eligible_countries"]
                partner["summary"] = (
                    "Named Polish partner award in the US Scholar programme; "
                    "US "
                    "citizenship and a PhD/terminal degree apply; 3–10 months "
                    "of teaching/research. No dated "
                    "deadline is stated by this publisher section."
                )
                records.append(partner)
    if len(records) < 5:
        raise AdapterError("Incoming programme inventory changed", "parse")
    return records


@contextlib.contextmanager
def collection_lock():
    with open(
        os.path.join(tempfile.gettempdir(), SOURCE_ID + ".lock"), "a"
    ) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def collect():
    with collection_lock():
        outgoing = page(OUTGOING_URL)
        incoming = page(INCOMING_URL)
        # Discover the publisher's actual programme navigation/footer URLs,
        # not quantities of award places or unrelated news/application systems.
        paths = set()
        for root in (outgoing, incoming):
            for link in nodes(root, "a"):
                url = urllib.parse.urljoin(
                    WEBSITE_URL, link.attrs.get("href", "")
                )
                parsed = urllib.parse.urlsplit(url)
                path = parsed.path.strip("/")
                if (
                    parsed.hostname in ("topminds.pl", "www.topminds.pl")
                    and link.text().casefold() == "program topminds"
                ):
                    # The navigation links the official programme site; its
                    # substantive Commission explanation has this own route.
                    paths.add("topminds")
                if (
                    parsed.hostname == "fulbright.edu.pl"
                    and path in _LOCAL_PATHS
                ):
                    paths.add(path)
        if paths != _LOCAL_PATHS:
            raise AdapterError(
                "Programme navigation inventory changed", "parse"
            )
        records = []
        for path in sorted(paths):
            root = page(WEBSITE_URL + path + "/")
            if path == "schuman-award":
                records.extend(schuman_variants(root))
            else:
                records.append(local_programme(root, path))
        records.extend(incoming_programmes(incoming))
        validate_records(records)
        print(
            f"Fulbright Poland: {len(paths)} own programme pages, "
            f"{len(records)} total programme/call records",
            file=sys.stderr,
        )
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
    publisher = SOURCE_ID if host == "fulbright.edu.pl" else host
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
                        f"Publisher returned HTTP {status}", "fetch", status
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
    result["license"] = "CC BY-NC 4.0"
    result["license_url"] = LICENSE_URL
    result["changes"] = (
        "Factual selection and normalization; YouthOpps-written summaries."
    )
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
