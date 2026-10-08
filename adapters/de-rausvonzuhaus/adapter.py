"""Public Rausvonzuhaus vacancies with attributed factual card metadata."""

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

SOURCE_ID = "de-rausvonzuhaus"
SOURCE_URL = "https://www.rausvonzuhaus.de/lastminute"
WEBSITE_URL = "https://www.rausvonzuhaus.de/"
LANGUAGE = "de"
PUBLISHER_COUNTRY = "DE"
PUBLISHER_TYPE = "youth-information-service"
ATTRIBUTION = "rausvonzuhaus.de / Eurodesk Deutschland (IJAB)"
_COUNTRIES = dict(
    zip(
        "Frankreich|USA|Deutschland|Südkorea|Island|Österreich|Togo|Ecuador|"
        "Indien|Uganda|Tansania|Thailand|Kambodscha|Kolumbien|Serbien|Georgien|"
        "Peru|Ghana|Südafrika|Ruanda|Großbritannien|Australien|Irland|Norwegen|"
        "Belgien|Kanada|Spanien|Rumänien|Costa Rica|Portugal|Estland|Finnland|"
        "Neuseeland|Polen|Schweiz|Kirgisistan|Dänemark|Namibia|Botswana|Vietnam|"
        "Israel|Fidschi|Dominikanische Republik|Malawi|Tschechien|Chile|"
        "Griechenland|Niederlande|Nepal|Kenia|Philippinen|Indonesien|Litauen|"
        "Italien|Kasachstan|Albanien|Brasilien|Bolivien|Mosambik|Kosovo|"
        "Bosnien und Herzegowina".split("|"),
        "FR US DE KR IS AT TG EC IN UG TZ TH KH CO RS GE PE GH ZA RW GB AU IE "
        "NO BE CA ES RO CR PT EE FI NZ PL CH KG DK NA BW VN IL FJ DO MW CZ CL "
        "GR NL NP KE PH ID LT IT KZ AL BR BO MZ XK BA".split(),
    )
)


class VacancyCards(HTMLParser):
    """Capture factual card elements, omitting images and copyright credits."""

    def __init__(self, document):
        super().__init__(convert_charrefs=True)
        self.cards = []
        self.card = None
        self.capture = None
        self.document_complete = False
        self.feed(document)
        self.close()
        if (
            self.card is not None
            or not self.cards
            or not self.document_complete
        ):
            raise AdapterError("Incomplete or empty vacancy listing", "parse")

    def handle_starttag(self, tag, attributes):
        attributes = dict(attributes)
        classes = attributes.get("class", "").split()
        if tag == "a" and "lmm-item" in classes:
            if self.card is not None:
                raise AdapterError("Nested vacancy card", "parse")
            self.card = {
                "url": attributes.get("href", ""),
                "title": " ".join(attributes.get("data-title", "").split()),
                "paragraphs": [],
                "programme": "",
            }
        if self.card is not None:
            if tag == "p":
                self.capture = (tag, "paragraphs", [])
            elif tag == "span" and "text-meta-head" in classes:
                self.capture = (tag, "programme", [])

    def handle_data(self, value):
        if self.capture is not None:
            self.capture[2].append(value)

    def handle_endtag(self, tag):
        if tag == "html":
            self.document_complete = True
        if self.capture is not None and tag == self.capture[0]:
            _, field, parts = self.capture
            value = " ".join(" ".join(parts).split())
            if field == "paragraphs":
                self.card[field].append(value)
            else:
                self.card[field] = value
            self.capture = None
        if tag == "a" and self.card is not None:
            self.cards.append(self.card)
            self.card = None
            self.capture = None


def calendar_date(value):
    """Parse an explicit publisher calendar date without inventing a clock."""
    try:
        return datetime.strptime(value, "%d.%m.%Y").date()
    except ValueError as error:
        raise AdapterError("Invalid vacancy calendar date", "parse") from error


def card_record(card, today):
    """Normalize a genuine vacancy, excluding inconsistent dates."""
    url = urllib.parse.urljoin(SOURCE_URL, card["url"])
    parsed = urllib.parse.urlsplit(url)
    identity = re.fullmatch(r"/lastminute/detail/(\d+)", parsed.path)
    paragraphs = card["paragraphs"]
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.rausvonzuhaus.de"
        or parsed.query
        or parsed.fragment
        or identity is None
        or len(paragraphs) not in (5, 6)
        or not card["title"]
        or paragraphs[0] != card["title"]
        or not card["programme"]
    ):
        raise AdapterError("Vacancy card structure changed", "parse")
    age = re.fullmatch(r"(\d+)-(\d+) Jahre", paragraphs[1])
    period = re.fullmatch(
        r"(\d{2}\.\d{2}\.\d{4}) bis (\d{2}\.\d{2}\.\d{4})",
        paragraphs[3],
    )
    closing = re.fullmatch(
        r"Bewerbungsschluss: (\d{2}\.\d{2}\.\d{4})", paragraphs[4]
    )
    if age is None or period is None or closing is None:
        raise AdapterError("Unsupported vacancy age or date format", "parse")
    minimum, maximum = map(int, age.groups())
    if not 0 <= minimum <= maximum <= 120:
        raise AdapterError("Invalid vacancy age range", "parse")
    start, end = map(calendar_date, period.groups())
    deadline = calendar_date(closing[1])
    if start > end or deadline > end:
        return None
    programme = card["programme"]
    if "freiwilligendienst" in programme.casefold() or programme in (
        "weltwärts",
        "Workcamp",
        "Europäisches Solidaritätskorps (ESK)",
    ):
        categories = ["volunteering"]
    elif programme in (
        "Jugendbegegnung",
        "Langfristiger individueller Schüleraustausch",
    ):
        categories = ["training"]
    elif programme == "Praktikum für Studierende":
        categories = ["internships"]
    else:
        raise AdapterError(
            "Unsupported vacancy programme: " + programme, "parse"
        )
    country = paragraphs[2].split(",", 1)[0].strip()
    if country not in _COUNTRIES:
        raise AdapterError(
            "Unknown publisher host country: " + country, "parse"
        )
    evidence = [
        "Public vacancy card: " + url,
        "Programme: " + programme,
        "Age: " + paragraphs[1],
        "Publisher host label: " + paragraphs[2],
        "Project dates: " + paragraphs[3],
        paragraphs[4] + "; closing clock and time zone unspecified",
    ]
    record = make_record(
        card["title"],
        url,
        categories,
        tags=[programme],
        host_countries=[_COUNTRIES[country]],
        method="publisher-vacancy-card",
        evidence=evidence,
    )
    record.update(
        id=hashlib.sha256(f"{SOURCE_ID}|{identity[1]}".encode()).hexdigest()[
            :24
        ],
        source_record_id=identity[1],
        attribution=ATTRIBUTION,
        location=paragraphs[2],
        deadline=deadline.isoformat(),
        deadline_precision="date",
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        age_min=minimum,
        age_max=maximum,
        status=(
            "open"
            if deadline > today
            else "expired" if deadline < today else "unknown"
        ),
        summary=(
            f"{programme}; {paragraphs[1]}; {paragraphs[2]}. "
            f"Projekt: {paragraphs[3]}. {paragraphs[4]}. "
            "Nationalitätsvoraussetzungen, Kosten und Leistungen sind "
            "in der Angebotskarte nicht angegeben. Quelle: rausvonzuhaus.de."
        ),
    )
    return record


def collect():
    """Read the complete normal HTML list with paced, robots-checked access."""
    delay = check_robots(SOURCE_URL)
    document = fetch_source(SOURCE_URL, min_interval=delay)
    cards = VacancyCards(document).cards
    today = datetime.now(timezone.utc).date()
    records, identities = [], set()
    for card in cards:
        if card["url"] in identities:
            raise AdapterError("Duplicate publisher vacancy card", "parse")
        identities.add(card["url"])
        record = card_record(card, today)
        if record is not None:
            records.append(record)
    print(
        f"Publisher cards: {len(cards)}; retained: {len(records)}; "
        f"inconsistent dates: {len(cards) - len(records)}",
        file=sys.stderr,
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
                        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"
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
                        f"Publisher returned HTTP {status}", "fetch", status
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
    ids, urls = set(), set()
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
        if record["url"] in urls:
            raise AdapterError("Duplicate opportunity URL", "validate")
        urls.add(record["url"])
        identity = record.get("source_record_id", "")
        if (
            not isinstance(identity, str)
            or not re.fullmatch(r"\d+", identity)
            or record["url"] != SOURCE_URL + "/detail/" + identity
            or record["attribution"] != ATTRIBUTION
            or record.get("deadline_precision") != "date"
            or record["eligible_countries"] != []
        ):
            raise AdapterError("Invalid vacancy provenance", "validate")
        dates = []
        for field in ("start_date", "end_date", "deadline"):
            value = record.get(field)
            try:
                if not isinstance(value, str) or not re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}", value
                ):
                    raise ValueError()
                dates.append(date.fromisoformat(value))
            except ValueError as error:
                raise AdapterError(
                    "Invalid vacancy date", "validate"
                ) from error
        if dates[0] > dates[1] or dates[2] > dates[1]:
            raise AdapterError("Inconsistent vacancy dates", "validate")
        minimum, maximum = record.get("age_min"), record.get("age_max")
        if (
            type(minimum) is not int
            or type(maximum) is not int
            or not 0 <= minimum <= maximum <= 120
        ):
            raise AdapterError("Invalid vacancy age range", "validate")
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
