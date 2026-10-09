"""Collect genuine official Study in Denmark funding frameworks."""

import argparse
import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import gzip
import hashlib
from html.parser import HTMLParser
import io
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

SOURCE_ID = "dk-study-in-denmark"
SOURCE_URL = "https://studyindenmark.dk/study-options/scholarships"
WEBSITE_URL = "https://studyindenmark.dk/"
LANGUAGE = "en"
PUBLISHER_COUNTRY = "DK"
PUBLISHER_TYPE = "government"
ATTRIBUTION = (
    "Study in Denmark — Danish Ministry of Science, Higher Education "
    "and Digital Affairs"
)


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


def view(root):
    return one(
        [n for n in root.walk() if n.attrs.get("id") == "view"],
        "complete public Study in Denmark article",
    )


def normalize_state_json(raw):
    """Replace unquoted JS undefined; preserve actual text inside strings."""
    parts, quoted, escaped, index = [], False, False, 0
    while index < len(raw):
        char = raw[index]
        if quoted:
            parts.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            index += 1
            continue
        if char == '"':
            quoted = True
        if raw.startswith("undefined", index):
            before = raw[index - 1] if index else ""
            end = index + len("undefined")
            after = raw[end] if end < len(raw) else ""
            if before == ":" and after in (",", "}"):
                parts.append("null")
                index = end
                continue
        parts.append(char)
        index += 1
    return "".join(parts)


def public_state(root):
    """Read actual public SSR state without requesting internal endpoints."""
    scripts = [
        "".join(c for c in n.children if isinstance(c, str))
        for n in nodes(root, "script")
    ]
    matches = [
        re.fullmatch(r"window\.__data=(.*);", s, re.DOTALL)
        for s in scripts
        if s.startswith("window.__data=")
    ]
    match = one(matches, "public SSR inventory state")
    if not match:
        raise AdapterError("Missing complete public inventory state", "parse")
    raw = normalize_state_json(match.group(1))
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as error:
        raise AdapterError(
            "Invalid public SSR inventory state", "parse"
        ) from error


def source_facts(key, root):
    """Retain every material condition; omit unrelated call-to-action counts."""
    if (
        "Danish Ministry of Science, Higher Education and Digital Affairs"
        not in root.text()
    ):
        raise AdapterError("Publisher attribution changed", "parse")
    policy_links = [
        n
        for n in nodes(root, "a")
        if re.search(r"copyright|terms|reuse|licen[sc]e", n.text(), re.I)
    ]
    if policy_links:
        raise AdapterError(
            "New linked publisher policy requires review", "access"
        )
    state = public_state(root)
    data = state["content"]["data"]
    if data.get("rights") not in (None, ""):
        raise AdapterError(
            "New publisher rights statement requires review", "access"
        )
    if key in ("home", "study-options", "institutions", "news", "events"):
        items = data.get("items", [])
        if not isinstance(items, list):
            raise AdapterError(
                "Missing complete source navigation inventory", "parse"
            )
        if key == "news" and data.get("items_total") != len(items):
            raise AdapterError("Incomplete actual news inventory", "parse")
        inventory = sorted(
            (
                item.get("@type"),
                item.get("@id"),
                item.get("title"),
                item.get("description"),
            )
            for item in items
            if item.get("@type") != "Image"
        )
        if key == "news":
            totals = [
                value.get("total")
                for value in state["querystringsearch"]["subrequests"].values()
            ]
            if totals != [len(inventory)]:
                raise AdapterError("News inventory total mismatch", "parse")
        if key == "events":
            totals = [
                v.get("total")
                for v in state["querystringsearch"]["subrequests"].values()
            ]
            if totals != [0]:
                raise AdapterError(
                    "New current events require award review", "parse"
                )
        return json.dumps([data.get("title"), inventory], ensure_ascii=False)
    text = view(root).text()
    if key != "privacy":
        if "Find Your Study Programme" not in text:
            raise AdapterError(
                "Changed article boundary requires review", "parse"
            )
        text = text.split("Find Your Study Programme", 1)[0].strip()
    return text


def sitemap_facts(document, *, funding_only=True):
    """Strictly validate complete public XML before selecting source routes."""
    try:
        root = ET.fromstring(document)
    except ET.ParseError as error:
        raise AdapterError("Malformed source sitemap XML", "parse") from error
    document_type = root.tag.rsplit("}", 1)[-1]
    if document_type.lower() in (
        "error",
        "challenge",
        "captcha",
        "accessdenied",
        "forbidden",
    ):
        raise AdapterError(
            "Publisher returned a refusal/challenge document", "access"
        )
    expected = "urlset" if funding_only else "sitemapindex"
    if document_type != expected:
        raise AdapterError("Unexpected source sitemap document type", "parse")
    urls = [
        e.text for e in root.iter() if e.tag.endswith("}loc") or e.tag == "loc"
    ]
    if not urls:
        raise AdapterError("Source sitemap has zero locations", "parse")
    invalid, classes = 0, []
    for url in urls:
        if not isinstance(url, str):
            invalid += 1
            if len(classes) < 3:
                classes.append("non-string:missing-host")
            continue
        try:
            parsed = urllib.parse.urlsplit(url)
            valid = (
                url.startswith(WEBSITE_URL)
                and parsed.scheme == "https"
                and parsed.netloc == "studyindenmark.dk"
                and not parsed.query
                and not parsed.fragment
                and not re.search(r"\s", url)
            )
            scheme = (
                parsed.scheme
                if parsed.scheme in ("https", "http")
                else "other-scheme"
            )
            host = (
                "expected-host"
                if parsed.hostname == "studyindenmark.dk"
                else "other-host" if parsed.hostname else "missing-host"
            )
        except ValueError:
            valid, scheme, host = False, "invalid-scheme", "invalid-host"
        if not valid:
            invalid += 1
            if len(classes) < 3:
                classes.append(scheme + ":" + host)
    if invalid:
        raise AdapterError(
            f"Invalid sitemap locations: total={len(urls)}, invalid={invalid}, "
            + "classes="
            + ",".join(classes),
            "parse",
        )
    if len(urls) != len(set(urls)):
        raise AdapterError("Duplicate source sitemap route", "parse")
    if not funding_only:
        return sorted(urls)
    return sorted(
        u
        for u in urls
        if "/portal/" not in u
        and not re.search(r"\.(?:jpe?g|png|gif|webp|pdf)$", u, re.I)
        and re.search(
            r"scholar|phd|grant|stipend|fund|terms|legal|copyright|privacy",
            u,
            re.I,
        )
    )


def read_sitemap(url):
    interval = check_robots(url)
    status, _, body = request_bytes(url, interval=interval)
    if status != 200:
        raise AdapterError(
            f"Publisher sitemap returned HTTP {status}", "fetch", status
        )
    if body.startswith(b"\x1f\x8b"):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as stream:
                body = stream.read(5_000_001)
        except (OSError, EOFError) as error:
            raise AdapterError(
                "Malformed compressed sitemap", "parse"
            ) from error
    if len(body) > 5_000_000:
        raise AdapterError("Expanded sitemap exceeds 5 MB", "parse")
    if re.search(rb"<(?:!doctype\s+html|html\b)", body, re.I):
        raise AdapterError(
            "Publisher returned HTML/challenge instead of XML", "access"
        )
    return body


def read_child_inventory():
    """Allow one delayed same-endpoint recovery for invalid XML metadata."""
    for attempt in range(2):
        try:
            facts = sitemap_facts(read_sitemap(WEBSITE_URL + "sitemap1.xml.gz"))
        except AdapterError as error:
            if error.stage != "parse" or attempt:
                raise
            print(
                "Invalid child sitemap metadata; one paced recovery: "
                + safe_error(error),
                file=sys.stderr,
            )
            time.sleep(30)
            continue
        # Valid changed policy/funding is substantive change, never retry it.
        if facts != REVIEWED_FUNDING_POLICY_ROUTES:
            raise AdapterError(
                "Publisher funding/policy route inventory changed", "parse"
            )
        return


PAGES = {
    "entry": "https://studyindenmark.dk/study-options/scholarships",
    "home": "https://studyindenmark.dk/",
    "study-options": "https://studyindenmark.dk/study-options",
    "institutions": (
        "https://studyindenmark.dk/study-options/higher-education-ins"
        "titutions"
    ),
    "privacy": "https://studyindenmark.dk/privacy",
    "news": "https://studyindenmark.dk/news",
    "events": "https://studyindenmark.dk/events",
    "tuition": (
        "https://studyindenmark.dk/study-options/tuition-fees-and-sch"
        "olarships"
    ),
    "application": "https://studyindenmark.dk/study-options/how-to-apply",
    "study-types": "https://studyindenmark.dk/study-options/what-can-i-study",
    "phd": (
        "https://studyindenmark.dk/study-options/what-can-i-study/phd"
        "-and-research"
    ),
    "masters": (
        "https://studyindenmark.dk/study-options/what-can-i-study/mas"
        "ters-programmes"
    ),
    "exchange": (
        "https://studyindenmark.dk/study-options/what-can-i-study/exc"
        "hange-programmes-and-summer-schools"
    ),
    "summer": (
        "https://studyindenmark.dk/study-options/what-can-i-study/sum"
        "mer-schools"
    ),
}

CONDITIONS = {
    "entry": "0e0e939d53a0107ce2ecd1621dd703b04d6c99be32c66750c3d6051fedf3a604",
    "home": "2154077b0f205614615336939fbe58b312dd46dbbc5697b79727e525fd772c0a",
    "study-options": "1ac07442182f4e086aece5427c5f8f27f929d506bdb91c7ed28f8d74c333f151",
    "institutions": "198334006f31921de75bc8c7e401d4e3beabb5a19603a2f18dd124620fee1b8a",
    "privacy": "8eee2ddc905d7aaaebf0bb2a11246b7ea74106f2aa0f499075a0926789cef0ea",
    "news": "6b7eb74dd4909c00b911fc0df9975792432f179a22f9f06814926178cce7a378",
    "events": "1b0b5107f236abc5523e4be8e2a571ed9597c73a03bd0c728b94f3cea0d2fa08",
    "tuition": "af3b10592236d7cb3c1456fe071c533fdca739f5c1535387fa407222f61c2253",
    "application": "e935d90f388c6a3c1a6d423ec7a1407d6dcb132dfc7f6943b91cc93ca12821d9",
    "study-types": "13e94bb6180d0cd602f8082dbd91e438e20d9bb76df3d4ae7bce2f36e49eac9d",
    "phd": "479e1e4a17f35d589cb09e975fe385b0abf8fad746df9465f1acc244120f9dec",
    "masters": "6cb135e7d7b630f52d1760a069e5784f64b040eb259f06cc1b6085405c5a1ce1",
    "exchange": "539be0c965f42bcd7d2067a4338cdbfbb4c92f9e8194b1560de22561ffa92665",
    "summer": "e8971bc0bd3734862a80c3d15102daa7d2a9ce1a75cec724c8f8f60337849268",
}

REVIEWED_FUNDING_POLICY_ROUTES = [
    "https://studyindenmark.dk/privacy",
    "https://studyindenmark.dk/study-options/scholarships",
    "https://studyindenmark.dk/study-options/tuition-fees-and-scholarships",
    "https://studyindenmark.dk/study-options/what-can-i-study/phd-and-research",
]

PROFILES = [
    {
        "track": "government",
        "key": "entry",
        "title": "Danish government scholarships for non-EU/EEA students",
        "categories": ["scholarships"],
        "summary": "Limited scholarships selected by Danish universities for "
        "highly qualified full-degree students: full/partial "
        "tuition waiver and/or living-cost grant. Requires "
        "citizenship outside EU/EEA/Switzerland, full-degree "
        "enrolment and a time-limited Danish education residence "
        "permit. No universal dated call or fixed amount stated.",
        "evidence": "Excluded: artistic higher-education admission; legal "
        "claim to Danish-citizen rights; specified "
        "admission-time Aliens Act §9c(1) permit as child of "
        "foreign parent holding §9m permit and citizenship "
        "outside EU/EEA; SU grant eligibility. The narrow "
        "child/parent clause is not a ban on all residents. "
        "Intro mentions Switzerland ambiguously; explicit "
        "eligibility bullet excludes it. Negative citizenship "
        "condition is preserved without inventing an exhaustive "
        "positive whitelist. Each university administers limited "
        "annual allocations and selects recipients; allocations "
        "are not distinct awards.",
        "hosts": ["DK"],
        "kind": "programme-overview",
    },
    {
        "track": "nordplus",
        "key": "entry",
        "title": "Nordplus higher education mobility scholarships",
        "categories": ["scholarships"],
        "summary": "Students enrolled at a Nordic or Baltic higher education "
        "institution may study in another Nordic or Baltic "
        "country as part of their degree. Contact the home "
        "university or national educational agency. Funding "
        "amount and dated application call are not specified.",
        "evidence": "Affiliation is not citizenship. Published destination "
        "region is Nordic/Baltic, without an exhaustive named "
        "country list; neither DK-only hosting nor a complete "
        "national whitelist is inferred. One framework, not a "
        "record per country.",
        "hosts": [],
        "kind": "programme-overview",
    },
    {
        "track": "erasmus",
        "key": "entry",
        "title": "Erasmus higher education exchange scholarships",
        "categories": ["scholarships"],
        "summary": "Study abroad for two to twelve months as part of higher "
        "education in the home country. Own source describes "
        "students from EU/EEA and Switzerland. The own exchange "
        "page confirms incoming study in Denmark, with prior "
        "enrolment at a higher education institution in the "
        "student’s country of residence.",
        "evidence": "Known Danish hosting: "
        "https://studyindenmark.dk/study-options/what-can-i-study/exc"
        "hange-programmes-and-summer-schools "
        ". Denmark is not the scheme’s only worldwide "
        "destination; source scholarship paragraph says abroad "
        "more broadly. Students from a region is not an "
        "unconditional citizenship whitelist. Consult home "
        "institution/national agency for precise eligibility and "
        "application procedure; no universal dated call or fixed "
        "grant amount. Do not turn bilateral exchange advice "
        "into unnamed award clones.",
        "hosts": ["DK"],
        "kind": "programme-overview",
    },
    {
        "track": "erasmus-mundus",
        "key": "entry",
        "title": "Erasmus Mundus / Joint Master Degree scholarships",
        "categories": ["scholarships"],
        "summary": "Scholarships for specific joint master’s courses offered "
        "by a Danish institution and another European university "
        "or college. Own source includes EU/EEA and non-EU/EEA "
        "students. Contact the individual master’s course for "
        "funding conditions and application procedures; no "
        "universal dated call or amount.",
        "evidence": "Danish institution establishes a known DK host; the "
        "other European partner country is unspecified, so DK is "
        "not presented as an exhaustive joint host list. Course "
        "catalogue links do not create synthetic current course "
        "awards.",
        "hosts": ["DK"],
        "kind": "programme-overview",
    },
    {
        "track": "fulbright",
        "key": "entry",
        "title": "Fulbright Denmark study and research grants",
        "categories": ["scholarships", "fellowships"],
        "summary": "An American scholar or postgraduate student at master’s "
        "or PhD level may apply for an academic year of study "
        "and/or research in Denmark. Visit Fulbright Denmark for "
        "selection and application procedures. Own overview gives "
        "no current dated call or award amount.",
        "evidence": "American is the publisher’s exact eligibility "
        "descriptor; this overview does not define a "
        "passport/citizenship or residence test, so no "
        "unsupported positive citizenship list is invented. This "
        "own substantive framework overview does not replicate "
        "the external provider’s individual call catalogue.",
        "hosts": ["DK"],
        "kind": "programme-overview",
    },
    {
        "track": "su",
        "key": "entry",
        "title": "Danish State Educational Support (SU)",
        "categories": ["scholarships"],
        "summary": "Educational support is generally awarded to Danish "
        "residents. International students may apply for equal "
        "status under Danish rules or EU law; individual "
        "conditions apply. No fixed award amount, universal "
        "application deadline or unconditional grant entitlement "
        "is stated.",
        "evidence": "Residence and equal-status conditions are not DK-only "
        "citizenship. Do not assume every foreign student "
        "qualifies, and do not merge SU with the separate "
        "government scholarship that excludes SU-eligible "
        "applicants.",
        "hosts": ["DK"],
        "kind": "programme-overview",
    },
    {
        "track": "industrial-phd",
        "key": "phd",
        "title": "Industrial PhD grant",
        "categories": ["grants", "jobs"],
        "summary": "Commercial research with employment by a private company "
        "and university PhD enrolment. Find a company and "
        "university supervisor; the company applies to Innovation "
        "Fund Denmark for the grant on the candidate’s behalf. "
        "After funding success, apply for Graduate School "
        "enrolment. Salary follows the company agreement; no "
        "grant amount or dated call stated.",
        "evidence": "The private company is grant applicant; employed PhD "
        "candidate is beneficiary, not direct grant applicant. "
        "Own Danish university context establishes DK host. "
        "General PhD admission normally requires a relevant "
        "recognised master’s and good English, "
        "institution-dependent; generic 3+5/4+4 routes are not "
        "unconditional eligibility for this grant. DKK32,567 "
        "average University of Copenhagen faculty-funded salary "
        "and DKK50,000 general tuition fee are not this grant’s "
        "benefits/costs. No university job-bank, degree or "
        "third-party award clones.",
        "hosts": ["DK"],
        "kind": "institutional-grant",
    },
]


def read_pages():
    pages = {key: page(url) for key, url in PAGES.items()}
    index = sitemap_facts(
        read_sitemap(WEBSITE_URL + "sitemap.xml.gz"), funding_only=False
    )
    if index != [WEBSITE_URL + "sitemap1.xml.gz"]:
        raise AdapterError("Publisher sitemap index changed", "parse")
    read_child_inventory()
    return pages


def parse_inventory(pages):
    if set(pages) != set(PAGES):
        raise AdapterError("Incomplete source evidence", "parse")
    for key, root in pages.items():
        digest = hashlib.sha256(source_facts(key, root).encode()).hexdigest()
        if digest != CONDITIONS[key]:
            raise AdapterError(
                "Source facts/inventory changed: " + key, "parse"
            )
    records = []
    for profile in PROFILES:
        record = make_record(
            profile["title"],
            PAGES[profile["key"]],
            profile["categories"],
            kind=profile["kind"],
            host_countries=profile["hosts"],
            evidence=[ATTRIBUTION, profile["evidence"]],
        )
        record["id"] = hashlib.sha256(
            (SOURCE_ID + "|" + profile["track"]).encode()
        ).hexdigest()[:24]
        record["summary"] = profile["summary"]
        records.append(record)
    validate_records(records)
    return records


def collect():
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
        tempfile.gettempdir(), "dk-study-in-denmark-pacing-" + str(os.getuid())
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
    publisher = "studyindenmark.dk" if host == "studyindenmark.dk" else host
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
    request_headers = {"User-Agent": _USER_AGENT, **(headers or {})}
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
                f"Collected {len(records)} validated Study in Denmark "
                f"funded opportunities"
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
