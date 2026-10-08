"""University of Vienna's public Non-EU exchange-place inventory.

The programme page publishes eligibility and conditional scholarship funding.
Its regional Mobility Online forms publish specific bilateral exchange places,
including unavailable allocations; these are retained with an unknown status.
All rows are rendered in HTML: DataTables pagination is entirely client-side.
Partner-specific URLs use public form parameters, not session-encrypted detail
links. Identifiers include academic year, programme, institution and study
field.
Reviewed Ottawa allocations always use explicit study-cycle/duration indicators
(even when only one allocation remains) as a semantic
agreement discriminator; changing place quantities never participate in
identity.
No nationality restriction is inferred from enrolment at the University of
Vienna.
The imprint permits attributed noncommercial copies; only factual fields are
normalised here. Sources: https://international.univie.ac.at/en/imprint and the
programme/mobility endpoints below. No authentication or JS execution is needed.
"""

import argparse
import base64
from collections import Counter, deque
import contextlib
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
from urllib import robotparser

SOURCE_ID = "at-university-of-vienna"
SOURCE_URL = (
    "https://international.univie.ac.at/en/"
    "study-abroad-with-erasmus-and-co/non-eu-student-exchange-program"
)
WEBSITE_URL = "https://international.univie.ac.at/en/"
LANGUAGE = "en"
PUBLISHER_COUNTRY = "AT"
PUBLISHER_TYPE = "university"
ATTRIBUTION = "Source: University of Vienna"
PORTAL = "https://mobility.univie.ac.at/mobility/MobilitySearchServlet"
COUNTRIES = {
    "Argentina": "AR",
    "Australia": "AU",
    "Brazil": "BR",
    "Canada": "CA",
    "Chile": "CL",
    "China": "CN",
    "Colombia": "CO",
    "Costa Rica": "CR",
    "India": "IN",
    "Japan": "JP",
    "Lebanon": "LB",
    "Mexico": "MX",
    "Peru": "PE",
    "Singapore": "SG",
    "South Africa": "ZA",
    "South Korea": "KR",
    "Taiwan": "TW",
    "Tanzania": "TZ",
    "Thailand": "TH",
    "USA": "US",
    "United States": "US",
    "United States of America": "US",
    "Uruguay": "UY",
}


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
_BUDGETS = {}


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
    host = (
        (urllib.parse.urlsplit(url).hostname or "").lower().removeprefix("www.")
    )
    if host in ("international.univie.ac.at", "mobility.univie.ac.at"):
        host = SOURCE_ID
    budget = _BUDGETS.setdefault(
        host, {"starts": deque(), "interval": 6, "until": 0}
    )
    budget["interval"] = max(budget["interval"], interval, 6)
    while True:
        now = time.monotonic()
        while budget["starts"] and now - budget["starts"][0] >= 60:
            budget["starts"].popleft()
        ready = max(
            budget["until"],
            (
                budget["starts"][-1] + budget["interval"]
                if budget["starts"]
                else now
            ),
            budget["starts"][0] + 60 if len(budget["starts"]) >= 10 else now,
        )
        if ready <= now:
            budget["starts"].append(now)
            return budget
        time.sleep(ready - now)


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
                budget["until"] = max(
                    budget["until"], time.monotonic() + max(0, seconds)
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
    try:
        text = fetch_source(robots_url)
    except AdapterError as error:
        if error.status == 404:
            return 6
        raise
    policy = robotparser.RobotFileParser()
    policy.parse(text.splitlines())
    if not policy.can_fetch(_USER_AGENT, url):
        raise AdapterError("Robots policy disallows source endpoint", "access")
    delay = policy.crawl_delay(_USER_AGENT) or 6
    rate = policy.request_rate(_USER_AGENT)
    if rate:
        delay = max(delay, rate.seconds / rate.requests)
    return max(6, delay)


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
    if error:
        result["failure_stage"] = getattr(error, "stage", "parse")
    return result


class InventoryHTML(HTMLParser):
    """Extract public form options and all rows of the publisher's inventory."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.options = {}
        self.select = None
        self.option = None
        self.option_text = []
        self.inventory = False
        self.in_body = False
        self.cells = None
        self.cell = None
        self.rows = []
        self.headers = []
        self.flags = []
        self.text = []
        self.skip = 0
        self.row_flags = []
        self.details = []
        self.row_details = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
        if tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"])
            if self.inventory:
                self.row_details.append(attributes["href"])
        if tag == "select":
            self.select = attributes.get("name")
            self.options.setdefault(self.select, {})
        if tag == "option" and self.select:
            self.option = attributes.get("value")
            self.option_text = []
        if (
            tag == "table"
            and "datatable" in attributes.get("class", "").split()
        ):
            self.inventory = True
        if self.inventory and tag == "tbody":
            self.in_body = True
        if self.inventory and tag == "tr":
            self.cells = []
            self.row_flags = []
            self.row_details = []
        if self.inventory and tag in ("td", "th"):
            self.cell = []
        if self.inventory and tag == "i" and attributes.get("title"):
            self.row_flags.append(attributes["title"])
        if tag == "br":
            self.handle_data(" ")

    def handle_data(self, data):
        if self.skip:
            return
        self.text.append(data)
        if self.option is not None:
            self.option_text.append(data)
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        if tag == "option" and self.option is not None:
            self.options[self.select][self.option] = " ".join(
                "".join(self.option_text).split()
            )
            self.option = None
        if tag == "select":
            self.select = None
        if self.inventory and tag in ("td", "th") and self.cell is not None:
            self.cells.append(" ".join("".join(self.cell).split()))
            self.cell = None
        if self.inventory and tag == "tr" and self.cells:
            if not self.headers:
                self.headers = self.cells
            elif self.in_body:
                self.rows.append(self.cells)
                self.flags.append(self.row_flags)
                self.details.append(self.row_details)
            self.cells = None
        if tag == "tbody":
            self.in_body = False
        if tag == "table" and self.inventory:
            self.inventory = False


class AgreementHTML(HTMLParser):
    """Read explicit qualification checkboxes in public agreement details."""

    LABELS = {
        "First cycle/Bachelor/Undergraduate": "Bachelor",
        "Second cycle/Master/Postgraduate": "Master",
        "Third cycle/Phd/Doctoral": "Doctoral",
        "Jahresplätze": "annual",
        "Semesterplätze": "semester",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.mode = None
        self.parts = []
        self.images = []
        self.pending = None
        self.values = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "td":
            css = attributes.get("class", "").split()
            self.mode = (
                "label"
                if "colLabel" in css
                else "value" if "dispCol" in css else None
            )
            self.parts = []
            self.images = []
        if tag == "img" and self.mode == "value":
            self.images.append(
                urllib.parse.urlsplit(attributes.get("src", "")).path
            )

    def handle_data(self, data):
        if self.mode:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag != "td":
            return
        if self.mode == "label":
            label = " ".join("".join(self.parts).split())
            self.pending = self.LABELS.get(label)
        elif self.mode == "value" and self.pending:
            if (
                len(self.images) != 1
                or self.images[0]
                not in (
                    "/mobility/images/haken3.gif",
                    "/mobility/images/cancel.gif",
                )
                or self.pending in self.values
            ):
                raise AdapterError(
                    "Ambiguous agreement qualification indicator"
                )
            self.values[self.pending] = self.images[0].endswith("/haken3.gif")
            self.pending = None
        self.mode = None


def agreement_signature(links, delay):
    details = [urllib.parse.urljoin(PORTAL, link) for link in links]
    if len(details) != 1:
        raise AdapterError(
            "Expected one public bilateral agreement detail link"
        )
    url = details[0]
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "mobility.univie.ac.at"
        or parsed.path != "/mobility/DispSearchDetailServlet"
    ):
        raise AdapterError("Unexpected public agreement endpoint", "access")
    page = AgreementHTML()
    page.feed(fetch_source(url, min_interval=delay))
    page.close()
    if set(page.values) != set(AgreementHTML.LABELS.values()):
        raise AdapterError(
            "Agreement study-cycle or duration indicators missing"
        )
    signature = "|".join(
        key + "=" + str(page.values[key]) for key in sorted(page.values)
    )
    description = "; ".join(key for key, value in page.values.items() if value)
    if not description:
        raise AdapterError(
            "Agreement has no explicit supported study cycle/duration"
        )
    return signature, description


def parse_page(url, delay):
    page = InventoryHTML()
    page.feed(fetch_source(url, min_interval=delay))
    page.close()
    return page


@contextlib.contextmanager
def collection_lock():
    """Serialize same-source runs, including a pacing gap between processes."""
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


def collect_inventory():
    delay = check_robots(SOURCE_URL)
    programme = parse_page(SOURCE_URL, delay)
    text = " ".join(" ".join(programme.text).split())
    if "Non-EU Student Exchange Program" not in text or "students" not in text:
        raise AdapterError("Programme identity or eligibility changed")
    links = sorted(
        {url for url in programme.links if url.startswith(PORTAL + "?")}
    )
    if len(links) != 4:
        raise AdapterError(
            "Expected four official regional exchange inventories"
        )
    records = []
    portal_delay = check_robots(links[0])
    for url in links:
        page = parse_page(url, portal_delay)
        years = page.options.get("studj_id", {})
        valid_years = {
            key: value
            for key, value in years.items()
            if re.fullmatch(r"20\d{2}/20\d{2}", value)
        }
        if not valid_years:
            raise AdapterError("No published academic years in inventory")
        year_id = max(valid_years, key=valid_years.get)
        year = valid_years[year_id]
        source_query = dict(
            urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)
        )
        if source_query.get("studj_id") != year_id:
            source_query["studj_id"] = year_id
            page = parse_page(
                PORTAL + "?" + urllib.parse.urlencode(source_query),
                portal_delay,
            )
        homes = page.options.get("inst_id_heim", {})
        if len(homes) != 1 or next(iter(homes.values())) != "Universität Wien":
            raise AdapterError("University of Vienna form identity changed")
        partners = {
            name: key
            for key, name in page.options.get("inst_id_partner", {}).items()
            if key not in ("0", "-1")
        }
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        query.update(
            studj_id=year_id,
            inst_id_heim=next(iter(homes)),
            readNew="0",
            kz_search="A",
            org_id="31",
            lcd_id="0",
            inst_id_partner="0",
        )
        result_url = PORTAL + "?" + urllib.parse.urlencode(query)
        if not robot_allowed(result_url):
            raise AdapterError("Unexpected public form destination", "access")
        listing = parse_page(result_url, portal_delay)
        expected = [
            "",
            "",
            "Home institution",
            "Host country",
            "Partner institution",
            "Study field",
            "Program",
            "Teaching language",
            "Total number",
            "Qty. free spots",
            "Coordinator",
        ]
        if listing.headers != expected:
            raise AdapterError(
                "Exchange table schema changed: " + str(listing.headers)
            )
        inventory_text = " ".join(" ".join(page.text).split())
        count = re.search(r"(\d+) Exchange possibilities in", inventory_text)
        if count is None or int(count.group(1)) != len(listing.rows):
            raise AdapterError(
                "Regional inventory count differs from full table"
            )
        duplicate_keys = Counter((row[4], row[5]) for row in listing.rows)
        for cells, flags, detail_links in zip(
            listing.rows, listing.flags, listing.details
        ):
            if (
                len(cells) != len(expected)
                or cells[2] != "University of Vienna"
            ):
                raise AdapterError("Malformed or foreign exchange allocation")
            country, partner, field, programme_name = cells[3:7]
            if country not in COUNTRIES or partner not in partners:
                raise AdapterError(
                    "Unmapped country or partner: " + country + " / " + partner
                )
            if not all(re.fullmatch(r"-?\d+", value) for value in cells[8:10]):
                raise AdapterError("Exchange-place quantities changed")
            partner_query = {**query, "inst_id_partner": partners[partner]}
            partner_url = PORTAL + "?" + urllib.parse.urlencode(partner_query)
            record = make_record(
                partner + " — " + programme_name + " (" + year + ")",
                partner_url,
                ["training"],
                tags=["student-exchange"],
                host_countries=[COUNTRIES[country]],
                method="publisher-listing",
                evidence=[SOURCE_URL, result_url],
            )
            # URLs identify a partner; distinct study-field allocations
            # retain distinct IDs.
            identity = "|".join(
                [
                    SOURCE_ID,
                    query["aust_prog_id"],
                    year_id,
                    partners[partner],
                    field,
                ]
            )
            qualification = ""
            if partner == "University of Ottawa":
                signature, qualification = agreement_signature(
                    detail_links, portal_delay
                )
                identity += "|" + signature
                record["title"] += " — " + qualification
            elif duplicate_keys[(partner, field)] > 1:
                raise AdapterError(
                    "New ambiguous partner allocations require identity review"
                )
            record["id"] = hashlib.sha256(identity.encode()).hexdigest()[:24]
            record["summary"] = (
                "University of Vienna outgoing student exchange. "
                f"Academic year: {year}; "
                f'study field: {field or "not specified"}; '
                f"total places: {cells[8]}; free places: {cells[9]}; "
                f'teaching language: {cells[7] or "not specified"}.'
            )
            if qualification:
                record["summary"] += (
                    " Study cycles/duration: " + qualification + "."
                )
            record["location"] = country
            record["attribution"] = ATTRIBUTION
            record["status"] = (
                "open"
                if "Application currently possible" in flags
                and int(cells[9]) > 0
                else "unknown"
            )
            records.append(record)
    validate_records(records)
    return records


def robot_allowed(url):
    """Keep forms on the reviewed endpoint; check robots once per host."""
    return url.startswith(PORTAL + "?")


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
                    "Publication failed; durable failure status "
                    "could not be recorded. "
                    "Remote data was not force-overwritten."
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
