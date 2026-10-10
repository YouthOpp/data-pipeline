"""Collect individual public Opportunity Desk RSS opportunities."""

import argparse
import base64
import contextlib
import datetime as dt
from datetime import datetime, timezone
import email.utils
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import html
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
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

SOURCE_ID = "opportunitydesk"
SOURCE_URL = "https://opportunitydesk.org/feed/"
WEBSITE_URL = "https://opportunitydesk.org/"
LANGUAGE = "en"
PUBLISHER_COUNTRY = None
PUBLISHER_TYPE = "aggregator"
ATTRIBUTION = "Opportunity Desk"

_TAG_RULES = (
    ("scholarships", r"\b(?:scholarships?|burs)\b"),
    ("internships", r"\binternships?\b"),
    ("volunteering", r"\bvolunteer(?:ing|s)?\b"),
    ("fellowships", r"\bfellowships?\b"),
    ("training", r"\b(?:training|courses?|workshops?)\b"),
    ("competitions", r"\b(?:competitions?|contests?|awards?)\b"),
    ("grants", r"\bgrants?\b"),
    ("jobs", r"\b(?:jobs?|careers?)\b"),
)


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
_ROBOTS_CACHE = {}


def _feed_text(value):
    return " ".join(html.unescape(re.sub(r"<[^>]*>", " ", value or "")).split())


def _published(value):
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return None
        return (
            parsed.astimezone(dt.timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    except (ValueError, TypeError, OverflowError):
        return None


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


def budget_path(host):
    key = hashlib.sha256((SOURCE_ID + "|" + host).encode()).hexdigest()[:24]
    return os.path.join(tempfile.gettempdir(), "youthopps-" + key + ".budget")


@contextlib.contextmanager
def budget_state(host):
    """Keep request starts and backoff shared across local processes."""
    with open(budget_path(host), "a+", encoding="utf-8") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        try:
            file.seek(0)
            text = file.read()
            state = (
                json.loads(text)
                if text
                else {"starts": [], "interval": 6, "until": 0}
            )
            yield state
            file.seek(0)
            file.truncate()
            json.dump(state, file)
            file.flush()
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def pace(url, interval=6):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    host = host.removeprefix("www.")
    while True:
        with budget_state(host) as state:
            now = time.time()
            starts = [start for start in state["starts"] if now - start < 60]
            state["starts"] = starts
            state["interval"] = max(6, interval, state["interval"])
            ready = max(
                state["until"],
                starts[-1] + state["interval"] if starts else now,
                starts[0] + 60 if len(starts) >= 10 else now,
            )
            if ready <= now:
                state["starts"].append(now)
                return {"host": host}
            wait = ready - now
        time.sleep(wait)


def request_bytes(url, *, interval=6, method="GET", headers=None, payload=None):
    budget = pace(url, interval)
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
                    else parsedate_to_datetime(retry).timestamp() - time.time()
                )
                with budget_state(budget["host"]) as state:
                    state["until"] = max(
                        state["until"], time.time() + max(0, seconds)
                    )
            except (ValueError, TypeError, OverflowError):
                pass
        body = response.read(5_000_001)
        if len(body) > 5_000_000:
            raise AdapterError("Response exceeds 5 MB", "fetch")
        return response.status, response.headers, body


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
    robots_url = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, "/robots.txt", "", "")
    )
    if robots_url not in _ROBOTS_CACHE:
        try:
            _ROBOTS_CACHE[robots_url] = fetch_source(robots_url)
        except AdapterError as error:
            if error.status == 404:
                _ROBOTS_CACHE[robots_url] = ""
            else:
                raise
    text = _ROBOTS_CACHE[robots_url]
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


_COUNTRY_NAMES = {
    "Burundi": "BI",
    "Central African Republic": "CF",
    "Democratic Republic of the Congo": "CD",
    "Djibouti": "DJ",
    "Eritrea": "ER",
    "Ethiopia": "ET",
    "Kenya": "KE",
    "Republic of the Congo": "CG",
    "Rwanda": "RW",
    "Somalia": "SO",
    "South Sudan": "SS",
    "Sudan": "SD",
    "Tanzania": "TZ",
    "Uganda": "UG",
    "Denmark": "DK",
    "United States": "US",
}


def article_countries(article):
    """Separate explicit citizenship lists from evidenced host locations."""
    eligible = []
    hosts = []
    location = None
    for block in article.blocks:
        match = re.search(
            r"citizens of one of the following countries:\s*(.+?)(?:[.—]|$)",
            block,
            re.I,
        )
        if match:
            # Consume the complete list, longest names first. Never substring
            # match Sudan inside South Sudan or Congo inside its two republics.
            names = re.split(r",\s*|\s+and\s+", match.group(1).strip())
            codes = [_COUNTRY_NAMES.get(name.strip()) for name in names]
            if names and all(codes):
                eligible.extend(codes)
    text = article.text
    if any(
        re.search(r"hosting institution|host institution", block, re.I)
        and re.search(r"period in Denmark", block, re.I)
        for block in article.blocks
    ):
        hosts.append("DK")
    if re.search(r"internships?", text, re.I) and re.search(
        r"in Washington, DC", text
    ):
        hosts.append("US")
        location = "Washington, DC, United States"
    if re.search(
        r"Fellows must be located in the NYC Metropolitan area or find "
        r"housing in the NYC Metropolitan area for the duration",
        text,
        re.I,
    ):
        hosts.append("US")
        location = "NYC Metropolitan area, United States"
    return sorted(set(eligible)), sorted(set(hosts)), location


def article_deadline(article):
    """Read only explicit closing instants with an unambiguous UTC zone."""
    pattern = (
        r"\b(?:applications?.{0,100}?will close|applications? close|"
        r"deadline(?: is)?) at\s+(\d{1,2})(?:[.:](\d{2}))?\s*"
        r"(am|pm)?\s+(GMT|UTC)\s+on\s+"
        r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\b"
    )
    values = set()
    for match in re.finditer(pattern, article.text, re.I):
        hour, minute, meridiem, _, day, month, year = match.groups()
        hour = int(hour)
        if meridiem:
            if not 1 <= hour <= 12:
                continue
            hour = hour % 12 + (12 if meridiem.lower() == "pm" else 0)
        try:
            date = dt.datetime.strptime(
                f"{day} {month} {year}", "%d %B %Y"
            ).replace(
                hour=hour, minute=int(minute or 0), tzinfo=dt.timezone.utc
            )
        except ValueError:
            continue
        values.add(
            date.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
    headers = [
        block for block in article.blocks if block.startswith("Deadline:")
    ]
    header_dates = []
    for block in headers:
        match = re.fullmatch(
            r"Deadline:\s*([A-Za-z]+) (\d{1,2}), (\d{4})", block
        )
        if match:
            try:
                header_dates.append(
                    dt.datetime.strptime(" ".join(match.groups()), "%B %d %Y")
                )
            except ValueError:
                pass
    for match in re.finditer(
        r"Applications must be submitted by ([A-Za-z]+) (\d{1,2}) "
        r"at (\d{1,2}):(\d{2})(AM|PM) ET\b",
        article.text,
        re.I,
    ):
        month, day, hour, minute, meridiem = match.groups()
        if not 1 <= int(hour) <= 12:
            continue
        for header in header_dates:
            if header.strftime("%B").lower() != month.lower():
                continue
            if header.day != int(day):
                continue
            try:
                local = header.replace(
                    hour=int(hour) % 12
                    + (12 if meridiem.lower() == "pm" else 0),
                    minute=int(minute),
                    tzinfo=ZoneInfo("America/New_York"),
                )
                # Reject ambiguous or missing local clocks at DST transitions.
                if local.replace(fold=1).utcoffset() != local.utcoffset():
                    continue
                utc = local.astimezone(dt.timezone.utc)
                if utc.astimezone(local.tzinfo) != local:
                    continue
                values.add(
                    utc.isoformat(timespec="milliseconds").replace(
                        "+00:00", "Z"
                    )
                )
            except ValueError:
                continue
    # Conflicting instants require source review, rather than choosing a date.
    return values.pop() if len(values) == 1 else None


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
                    if (
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


class Article(HTMLParser):
    """Read only the publisher's article, excluding menus and related posts."""

    def __init__(self, document):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.skip = 0
        self.parts = []
        self.blocks = []
        self.block = None
        self.links = []
        self.feed(document)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        attributes = dict(attrs)
        if tag == "div":
            if self.depth:
                self.depth += 1
            elif "post-content" in attributes.get("class", "").split():
                self.depth = 1
        if not self.depth:
            return
        if tag in ("p", "h2", "h3", "h4", "li"):
            self.block = []
        if tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"])

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        if not self.depth:
            return
        if tag in ("p", "h2", "h3", "h4", "li") and self.block is not None:
            self.blocks.append(" ".join(" ".join(self.block).split()))
            self.block = None
        if tag == "div":
            self.depth -= 1

    def handle_data(self, value):
        if self.depth and not self.skip:
            self.parts.append(value)
            if self.block is not None:
                self.block.append(value)

    @property
    def text(self):
        return " ".join(" ".join(self.parts).split())


@contextlib.contextmanager
def collection_lock():
    """Serialize publisher runs and retain pacing gaps across processes."""
    path = os.path.join(tempfile.gettempdir(), SOURCE_ID + ".lock")
    with open(path, "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            time.sleep(6)
            yield
        finally:
            time.sleep(6)
            fcntl.flock(lock, fcntl.LOCK_UN)


def collect():
    with collection_lock():
        return collect_inventory()


def material_programme_categories(article):
    """Classify a narrowly evidenced training programme with finalist grants."""
    headings = {}
    for name in ("benefits", "eligibility", "application"):
        positions = [
            i
            for i, block in enumerate(article.blocks)
            if block.casefold() == name
        ]
        if len(positions) != 1:
            return []
        headings[name] = positions[0]
    if not (
        headings["benefits"] < headings["eligibility"] < headings["application"]
    ):
        return []
    eligibility = article.blocks[
        headings["eligibility"] + 1 : headings["application"]
    ]
    application = article.blocks[headings["application"] + 1 :]
    if not any(
        re.search(r"\b(?:eligible|eligibility|applicants)\b", block, re.I)
        for block in eligibility
    ) or not any(re.search(r"\bapply\b", block, re.I) for block in application):
        return []
    benefits = article.blocks[
        headings["benefits"] + 1 : headings["eligibility"]
    ]
    courses = [
        block
        for block in benefits
        if re.fullmatch(
            r"Courses: Free access to (?:a range of )?online "
            r"self-paced courses",
            block,
            re.I,
        )
    ]
    workshops = [
        block
        for block in benefits
        if re.fullmatch(r"Talks: Expert talks & workshops on .+", block, re.I)
    ]
    grants = [
        block
        for block in benefits
        if re.fullmatch(
            r"Grant: Top [1-9][0-9]* finalists win one of "
            r"(?:[1-9][0-9]*|one|two|three) \$[1-9][0-9,]* grants",
            block,
            re.I,
        )
    ]
    if len(courses) == len(workshops) == len(grants) == 1:
        return ["training", "grants"]
    return []


def material_conference_grants(article):
    """Recognize the UN fund's conditional representative travel support."""
    blocks = article.blocks
    headings = {}
    for name in (
        "Benefits",
        "Eligibility",
        "Selection Criteria",
        "Application",
    ):
        positions = [i for i, block in enumerate(blocks) if block == name]
        if len(positions) != 1:
            return []
        headings[name] = positions[0]
    if not (
        headings["Benefits"]
        < headings["Eligibility"]
        < headings["Selection Criteria"]
        < headings["Application"]
    ):
        return []
    benefits = blocks[headings["Benefits"] + 1 : headings["Eligibility"]]
    eligibility = blocks[
        headings["Eligibility"] + 1 : headings["Selection Criteria"]
    ]
    selection = blocks[
        headings["Selection Criteria"] + 1 : headings["Application"]
    ]
    application = blocks[headings["Application"] + 1 :]
    sessions = blocks[: headings["Benefits"]]
    funding = [
        block
        for block in benefits
        if re.fullmatch(
            r"The UN Voluntary Fund for Indigenous Peoples will provide, "
            r"in accordance with United Nations rules and procedures, "
            r"a travel arrangement as well as a stipend to cover travel, "
            r"accommodation, and related expenses for selected Indigenous "
            r"representatives attending the abovementioned processes\.",
            block,
        )
    ]
    requirements = (
        any(
            re.search(
                r"session of the UN Permanent Forum on Indigenous Issues "
                r"\(UNPFII\)",
                block,
            )
            for block in sessions
        ),
        any(
            re.search(
                r"session of the Expert Mechanism on the Rights of Indigenous "
                r"Peoples \(EMRIP\)",
                block,
            )
            for block in sessions
        ),
        "Applications are open to Indigenous individuals and "
        "representatives of Indigenous Peoples’ organizations who:"
        in eligibility,
        "Are actively engaged in the promotion and protection of the "
        "rights of Indigenous Peoples;" in eligibility,
        "Require financial support to cover travel, accommodation and "
        "related expenses." in eligibility,
        any(
            re.fullmatch(
                r"Representation: Priority will be given to Indigenous "
                r"representatives who have a clear mandate from their "
                r"communities or organizations\.",
                block,
            )
            for block in selection
        ),
        "Interested candidates must complete the application form "
        "available at:" in application,
    )
    if len(benefits) == len(funding) == 1 and all(requirements):
        return ["grants"]
    return []


def collect_inventory():
    _ROBOTS_CACHE.clear()
    delay = check_robots(SOURCE_URL)
    document = ET.fromstring(fetch_source(SOURCE_URL, min_interval=delay))
    items = document.findall("./channel/item")
    if not items:
        raise AdapterError("Empty or invalid RSS feed")
    records = []
    seen = set()
    for item in items:
        title = _feed_text(item.findtext("title"))
        url = (item.findtext("link") or "").strip()
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "opportunitydesk.org":
            raise AdapterError("Unexpected RSS article host")
        if not title or url in seen:
            raise AdapterError("Missing title or duplicate RSS article")
        seen.add(url)
        tags = list(
            dict.fromkeys(
                _feed_text(node.text)
                for node in item.findall("category")
                if _feed_text(node.text)
            )
        )
        article = Article(fetch_source(url, min_interval=check_robots(url)))
        if not article.text:
            raise AdapterError("Publisher article structure changed")
        numbered = [
            block for block in article.blocks if re.match(r"^\d+[.)]\s+", block)
        ]
        editorial = {
            "our blog",
            "blog",
            "general tips",
            "how-to",
            "success stories",
            "young person of the month",
        }
        if editorial.intersection(tag.casefold() for tag in tags):
            print("Excluded editorial article: " + url, file=sys.stderr)
            continue
        if len(numbered) >= 2 and re.search(
            r"\b(?:opportunities|scholarships|internships|jobs)\b", title, re.I
        ):
            print("Excluded multi-opportunity roundup: " + url, file=sys.stderr)
            continue
        # Classification uses exact publisher taxonomy and article evidence,
        # never an opportunity-shaped title alone.
        categories = [
            category
            for category, pattern in _TAG_RULES
            if any(re.fullmatch(pattern, tag, re.I) for tag in tags)
        ]
        text = article.text
        if not re.search(
            r"\b(?:eligibility|eligible|application|apply|"
            r"applicants|deadline)\b",
            text,
            re.I,
        ):
            raise AdapterError("Unresolved non-opportunity RSS article: " + url)
        if "Conferences" in tags and not categories:
            categories = material_conference_grants(article)
            if not categories and re.search(
                r"\b(?:conference|ambassador|participants)\b", text, re.I
            ):
                categories = ["other"]
        if not categories:
            categories = material_programme_categories(article)
        if not categories:
            raise AdapterError("Unreviewed publisher opportunity taxonomy")
        record = make_record(
            title,
            url,
            categories,
            kind="opportunity",
            published_at=_published(item.findtext("pubDate")),
            tags=tags,
            method="publisher-taxonomy-and-article-v2",
            evidence=[url],
        )
        eligible, hosts, location = article_countries(article)
        record["eligible_countries"] = eligible
        record["host_countries"] = hosts
        record["location"] = location
        record["deadline"] = article_deadline(article)
        if record["deadline"]:
            record["status"] = (
                "open" if record["deadline"] > utc_now() else "expired"
            )
        deadlines = [
            block for block in article.blocks if block.startswith("Deadline:")
        ]
        if deadlines:
            record["summary"] = "Publisher-stated " + deadlines[0][:500]
        if categories == ["other"]:
            record["classification"]["status"] = "classified"
        records.append(record)
    validate_records(records)
    return records


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
                    "Source changed concurrently; newer remote data "
                    "was not overwritten",
                    "publish",
                )
            snapshot = latest
            if attempt == 2:
                raise AdapterError(
                    "Publication failed; durable remote outcome "
                    "could not be updated",
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
        help="Write only this source to YouthOpps/data-source "
        "using DATA_SOURCE_TOKEN",
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
                    "message": "Run could not complete; "
                    "durable status unavailable",
                    "error": safe_error(error),
                    "failure_stage": getattr(error, "stage", "publish"),
                },
                ensure_ascii=False,
            )
        )
        sys.exit(1)
