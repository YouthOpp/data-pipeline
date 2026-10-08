"""Current University of Tartu exchange calls and ENLIGHT courses.

Public ut.ee content and robots.txt are the reviewed access route; no login is
needed for collection. Applying uses UT credentials in SoleMOVE, which this
adapter never accesses. The open-calls table gates current inventory; upcoming
competitions and the evergreen scholarship directory are not opportunities.
Partner call years come from explicit dated headings. ENLIGHT's abbreviated
November deadline is anchored to the article's publication year, never today's
year. A date-only deadline remains valid through the publisher's Tallinn day.
Individual ENLIGHT course links are preserved without crawling external sites.
"""

import argparse
import base64
from datetime import datetime, timezone
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

SOURCE_ID = "ee-university-of-tartu"
SOURCE_URL = "https://ut.ee/en/open-calls"
WEBSITE_URL = "https://ut.ee/en/scholarships-and-other-stipends"
LANGUAGE = "en"
PUBLISHER_COUNTRY = "EE"
PUBLISHER_TYPE = "university"
ATTRIBUTION = "University of Tartu"
_PARTNERS_URL = "https://ut.ee/en/competitions-ut-partner-universities"
_ENLIGHT_URL = (
    "https://ut.ee/en/news/enlight-short-term-blended-mobility-courses"
)
_COUNTRIES = {"Georgia": "GE", "Japan": "JP", "South Korea": "KR", "USA": "US"}


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
    """Read only the publisher's content block, excluding navigation/footer."""

    def __init__(self, document):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(document)
        blocks = [
            n
            for n in self.root.walk()
            if n.attrs.get("id") == "block-utheme-content"
        ]
        if len(blocks) != 1:
            raise AdapterError("Publisher content block changed", "parse")
        self.content = blocks[0]

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


def publisher_page(url, delay):
    """Check every fetched content path against the current robots policy."""
    delay = max(delay, check_robots(url))
    return PublisherHTML(fetch_source(url, min_interval=delay)).content


def date_deadline(value, year=None):
    """Resolve a publisher date, with an explicit contextual year when needed."""
    pattern = r"(\d{1,2}) ([A-Za-z]+)(?: (\d{4}))?"
    match = re.fullmatch(pattern, value.strip())
    if not match or not (match[3] or year):
        raise AdapterError("Deadline lacks a supported calendar year", "parse")
    date = datetime.strptime(
        f"{match[1]} {match[2]} {match[3] or year}", "%d %B %Y"
    )
    return (
        date.replace(
            hour=23,
            minute=59,
            second=59,
            microsecond=999000,
            tzinfo=ZoneInfo("Europe/Tallinn"),
        )
        .astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def current_rows(content):
    """Select the current-call table, rejecting unhandled inventory changes."""
    section = ""
    tables = []
    for node in content.walk():
        if node.tag == "h3":
            section = node.text()
        elif node.tag == "table" and section == "Competitions currently open":
            tables.append(node)
    if len(tables) != 1:
        raise AdapterError("Current competitions table changed", "parse")
    rows = []
    for row in tables[0].walk():
        if row.tag != "tr":
            continue
        cells = [
            n for n in row.children if isinstance(n, Node) and n.tag == "td"
        ]
        if not cells:
            continue
        if len(cells) != 4:
            raise AdapterError("Current competition row changed", "parse")
        links = [
            urllib.parse.urljoin(SOURCE_URL, n.attrs.get("href", ""))
            for n in cells[0].walk()
            if n.tag == "a"
        ]
        if len(links) != 1 or links[0] not in (_PARTNERS_URL, _ENLIGHT_URL):
            raise AdapterError(
                "New current call requires source review", "parse"
            )
        rows.append((links[0], cells))
    if not rows or len({url for url, _ in rows}) != len(rows):
        raise AdapterError("Empty or duplicate current call rows", "parse")
    return rows


def finish_record(record, identity, deadline, summary):
    record["id"] = hashlib.sha256(
        f"{SOURCE_ID}|{identity}".encode()
    ).hexdigest()[:24]
    record["deadline"] = deadline
    record["summary"] = (
        summary
        if len(summary) <= 600
        else summary[:596].rsplit(" ", 1)[0] + "..."
    )
    record["status"] = "expired" if deadline < utc_now() else "open"
    return record


def partner_records(content, cells):
    """Expand the current exchange call into explicitly dated universities."""
    nodes = list(content.walk())
    by_id = {n.attrs["id"]: n for n in nodes if n.attrs.get("id")}
    country = None
    records = []
    for node in nodes:
        if node.tag in ("h2", "h3") and node.text() in _COUNTRIES:
            country = _COUNTRIES[node.text()]
        if node.tag != "button":
            continue
        titles = [
            n.text()
            for n in node.walk()
            if "collapsible__title" in n.attrs.get("class", "").split()
        ]
        if len(titles) != 1:
            continue
        match = re.fullmatch(
            r"(.+) \(application deadline (\d{1,2} [A-Za-z]+ \d{4})\)",
            titles[0],
        )
        if not match:
            continue
        # The current parent listing, not an old accordion, decides inclusion.
        if not match[2].startswith(cells[3].text() + " "):
            continue
        anchor = node.attrs.get("aria-controls")
        body = by_id.get(anchor)
        if not body or not country:
            raise AdapterError(
                "Dated exchange call has no detail or country", "parse"
            )
        title, deadline = match[1], date_deadline(match[2])
        url = _PARTNERS_URL + "#" + anchor
        record = make_record(
            "Exchange studies: " + title,
            url,
            ["training"],
            tags=["exchange", "tuition-waiver"],
            host_countries=[country],
            method="tartu-current-call-and-dated-partner",
            evidence=[SOURCE_URL, url],
        )
        summary = "University of Tartu students only. " + body.text()
        records.append(
            finish_record(
                record, f"exchange|{title}|{match[2][-4:]}", deadline, summary
            )
        )
    # Every named destination in the parent call must be represented.
    names = re.split(
        r",\s*| and ", next(n.text() for n in cells[0].walk() if n.tag == "a")
    )
    for name in names:
        key = (
            name.lower().replace("-", " ").removesuffix(" universities").strip()
        )
        tokens = key.split()
        if not any(
            all(
                token in r["title"].lower().replace("-", " ").split()
                for token in tokens
            )
            for r in records
        ):
            raise AdapterError(
                "Current exchange destination is missing: " + name, "parse"
            )
    if len(records) != len(names):
        raise AdapterError(
            "Exchange detail inventory differs from current call", "parse"
        )
    return records


def enlight_records(content, cells):
    """Expand the publisher's current course list, preserving course UUID URLs."""
    dates = [
        n.text()
        for n in content.walk()
        if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", n.text())
    ]
    dates = set(dates)
    if len(dates) != 1:
        raise AdapterError("ENLIGHT article publication date changed", "parse")
    published_date = datetime.strptime(dates.pop(), "%d.%m.%Y")
    deadline = date_deadline(cells[3].text(), published_date.year)
    if deadline[:10] < published_date.date().isoformat():
        raise AdapterError("ENLIGHT deadline precedes publication", "parse")
    if "The application deadline is " + cells[3].text() not in content.text():
        raise AdapterError(
            "ENLIGHT parent and article deadlines disagree", "parse"
        )
    host = ""
    records = []
    links_seen = set()
    for node in content.walk():
        if node.tag == "h2":
            host = node.text()
        if node.tag != "h3":
            continue
        links = [n for n in node.walk() if n.tag == "a"]
        if not links:
            continue
        if len(links) != 1:
            raise AdapterError("Course heading has ambiguous links", "parse")
        url = links[0].attrs.get("href", "")
        if (
            not re.fullmatch(
                r"https://courses\.enlight-eu\.org/joint-courses/[0-9a-f-]{36}",
                url,
            )
            or not host
        ):
            raise AdapterError("Unexpected ENLIGHT course URL or host", "parse")
        if url in links_seen:
            raise AdapterError("Duplicate ENLIGHT course", "parse")
        links_seen.add(url)
        record = make_record(
            links[0].text(),
            url,
            ["training"],
            tags=["ENLIGHT", "Erasmus+", "blended-mobility"],
            method="tartu-current-enlight-course-list",
            evidence=[SOURCE_URL, _ENLIGHT_URL, url],
        )
        nodes = list(content.walk())
        position = nodes.index(node)
        details = []
        for sibling in nodes[position + 1 :]:
            if sibling.tag in ("h2", "h3"):
                break
            if sibling.tag == "p":
                detail = sibling.text()
                if detail.startswith(("Level:", "Entry requirements:")):
                    details.append(detail.split("Departmental Coordinator:")[0])
        if not details:
            raise AdapterError("Course eligibility details missing", "parse")
        summary = (
            f"University of Tartu students only. Host: {host}. "
            + " ".join(details)
            + " Erasmus+ mobility grant; apply through UT SoleMOVE."
        )
        records.append(finish_record(record, url, deadline, summary))
    all_course_links = [
        n
        for n in content.walk()
        if n.tag == "a"
        and "courses.enlight-eu.org/joint-courses/" in n.attrs.get("href", "")
    ]
    if not records or len(all_course_links) != len(records):
        raise AdapterError("ENLIGHT course coverage incomplete", "parse")
    return records


def collect():
    """Collect only current calls using serialized, paced public requests."""
    content = publisher_page(SOURCE_URL, 6)
    records = []
    for url, cells in current_rows(content):
        details = publisher_page(url, 6)
        if url == _PARTNERS_URL:
            records.extend(partner_records(details, cells))
        else:
            records.extend(enlight_records(details, cells))
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
_USER_AGENT = "YouthOpp/1.0 (+https://github.com/YouthOpps/data-pipeline)"


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
    try:
        text = fetch_source(
            urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, "/robots.txt", "", "")
            )
        )
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
                    "Source changed concurrently; newer remote data was not overwritten",
                    "publish",
                )
            snapshot = latest
            if attempt == 2:
                raise AdapterError(
                    "Publication failed; durable remote outcome could not be updated",
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
        help="Write only this source to YouthOpps/data-source using DATA_SOURCE_TOKEN",
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
                    "Publication failed; durable failure status could not be recorded. Remote data was not force-overwritten."
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
                    "message": "Run could not complete; durable status unavailable",
                    "error": safe_error(error),
                    "failure_stage": getattr(error, "stage", "publish"),
                },
                ensure_ascii=False,
            )
        )
        sys.exit(1)
