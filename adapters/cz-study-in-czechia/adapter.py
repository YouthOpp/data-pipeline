"""Collect reviewed Study in Czechia scholarships and placements.

Official finite catalogue controls, full material guards and original factual
projections are source-local. Protected source documents and personal details
are never redistributed as records. Tempus material is excluded pending consent.
"""

import argparse
import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
from html.parser import HTMLParser
import io
import ipaddress
import json
import math
import os
import re
import sys
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

SOURCE_ID = "cz-study-in-czechia"
SOURCE_URL = "https://studyin.gov.cz/scholarships/"
WEBSITE_URL = "https://studyin.gov.cz/"
LANGUAGE = "en"
PUBLISHER_COUNTRY = "CZ"
PUBLISHER_TYPE = "government"
ATTRIBUTION = (
    "Study in Czechia — Czech National Agency for International Education "
    "and Research (DZS); official delegated programme conditions"
)
_GITHUB = "https://api.github.com/repos/YouthOpps/data-source"
_WORKFLOW_GITHUB = "https://api.github.com/repos/YouthOpps/data-pipeline"
_USER_AGENT = (
    "YouthOpps/1.0 (+https://github.com/YouthOpps/data-pipeline; "
    "public opportunity metadata)"
)
CATEGORIES = {
    "scholarships", "internships", "volunteering", "training", "jobs",
    "competitions", "grants", "fellowships", "other",
}
FAMILY = "youthopps-dzs-publisher-v1"
FAMILY_SOURCES = {"cz-study-in-czechia", "cz-dzs"}
COLLECTION_TIMEOUT = 10800

class Node:
    """Minimal HTML tree retaining official links and structural boundaries."""

    def __init__(self, tag="", attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []

    def text(self):
        if self.tag in ("script", "style"):
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

class AdapterError(Exception):
    def __init__(self, message, stage="parse", status=None):
        super().__init__(message)
        self.stage, self.status = stage, status

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, url):
        return None

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

_OPENER = urllib.request.build_opener(NoRedirect)
_ROBOTS = {}
_RESTORED = False
_COLLECTION_STARTED = None

_STATE_SCHEMA = 1
_STATE_PATH = None
_STATE_LOCK = None
_PUBLISHING = False
_STATE_TRUSTED = False
_COLLECTION_LOCK_FD = None
_ARTIFACT_NAME = "dzs-pacing-state"


def numeric(value):
    try:
        return (
            type(value) in (int, float)
            and math.isfinite(value) and value >= 0
        )
    except OverflowError:
        return False


def validate_budget(state):
    """Validate inert state without age-expiring a future publisher embargo."""
    if not isinstance(state, dict) or set(state) != {
        "schema", "family", "observed_at", "not_before", "starts", "blocked",
    }:
        raise AdapterError("Invalid publisher pacing state", "access")
    if (type(state["schema"]) is not int or state["schema"] != _STATE_SCHEMA
            or state["family"] != FAMILY
            or not numeric(state["observed_at"])
            or not numeric(state["not_before"])
            or type(state["blocked"]) is not bool
            or not isinstance(state["starts"], list)
            or len(state["starts"]) > 10
            or any(not numeric(t) for t in state["starts"])
            or state["starts"] != sorted(state["starts"])
            or len(set(state["starts"])) != len(state["starts"])
            or any(t > state["observed_at"] for t in state["starts"])):
        raise AdapterError("Invalid publisher pacing history", "access")
    if time.time() + 1 < state["observed_at"]:
        raise AdapterError("Publisher pacing clock moved backwards", "access")
    return state


def empty_budget(now):
    return {
        "schema": _STATE_SCHEMA, "family": FAMILY, "observed_at": now,
        "not_before": now, "starts": [], "blocked": False,
    }


def budget_file():
    global _STATE_PATH, _STATE_LOCK
    if _STATE_PATH:
        return
    directory = os.path.join(tempfile.gettempdir(), FAMILY + (
        "-"
    ) + str(os.getuid()))
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise AdapterError("Unsafe publisher pacing directory", "access")
    if info.st_mode & 0o077:
        raise AdapterError("Unsafe publisher pacing permissions", "access")
    _STATE_PATH = os.path.join(directory, "state.json")
    _STATE_LOCK = os.path.join(directory, "state.lock")


def locked_budget():
    budget_file()
    descriptor = os.open(
        _STATE_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
    )
    info = os.fstat(descriptor)
    if (info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077 or info.st_nlink != 1):
        os.close(descriptor)
        raise AdapterError("Unsafe publisher pacing lock", "access")
    file = os.fdopen(descriptor, "a+")
    fcntl.flock(file, fcntl.LOCK_EX)
    return file


def load_budget():
    try:
        fd = os.open(_STATE_PATH, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return empty_budget(time.time())
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if (info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise AdapterError("Unsafe publisher pacing state owner", "access")
        raw = file.read(16385)
    if len(raw) > 16384:
        raise AdapterError("Oversized publisher pacing state", "access")
    try:
        return validate_budget(json.loads(raw))
    except (ValueError, TypeError):
        raise AdapterError("Corrupt publisher pacing state", "access") from None


def save_budget(state):
    validate_budget(state)
    fd, name = tempfile.mkstemp(prefix=(
        "pending-"
    ), dir=os.path.dirname(_STATE_PATH))
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(state, file, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, _STATE_PATH)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def pace():
    (
        "Serialize every publisher request start across the complete "
        "origin set."
    )
    with locked_budget():
        state = load_budget()
        if state["blocked"]:
            raise AdapterError((
                "Publisher backoff requires reviewed recovery"
            ), (
                "access"
            ))
        now = time.time()
        starts = [t for t in state["starts"] if t > now - 60]
        target = max(now, state["not_before"])
        if starts:
            target = max(target, starts[-1] + 6)
        if len(starts) == 10:
            target = max(target, starts[0] + 60)
        if _COLLECTION_STARTED is not None and (
            target + 120 > _COLLECTION_STARTED + COLLECTION_TIMEOUT
        ):
            raise AdapterError((
                "Publisher backoff exceeds collection budget"
            ), (
                "access"
            ))
        if target > now:
            time.sleep(target - now)
        now = time.time()
        if now + 0.001 < target:
            raise AdapterError((
                "Publisher pacing clock moved backwards"
            ), (
                "access"
            ))
        state["starts"] = [t for t in starts if t > now - 60] + [now]
        state["observed_at"] = now
        save_budget(state)


def record_retry_after(value):
    """Persist the embargo before reporting any refusal or retrieval failure."""
    if value is None:
        return
    with locked_budget():
        state = load_budget()
        now = time.time()
        try:
            if re.fullmatch(r"[0-9]+", value.strip()):
                until = now + int(value)
            else:
                parsed = parsedate_to_datetime(value)
                if parsed.utcoffset() is None:
                    raise ValueError("Missing timezone")
                until = parsed.timestamp()
            if not numeric(until):
                raise ValueError("Invalid embargo")
            state["not_before"] = max(state["not_before"], until)
        except Exception:
            state["blocked"] = True
        state["observed_at"] = now
        save_budget(state)


def request_bytes(url, method="GET", headers=None, payload=None,
                  publisher=False, byte_limit=30000000):
    """Bound response bytes; redirects are explicit and credential-safe."""
    headers = {"User-Agent": _USER_AGENT, **(headers or {})}
    current = url
    for redirect in range(6):
        if publisher:
            public_url(current)
            pace()
        request = urllib.request.Request(
            current, data=payload, headers=headers, method=method
        )
        try:
            response = _OPENER.open(request, timeout=60)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            status = response.status
            response_headers = response.headers
            retry = response.headers.get("Retry-After")
            if publisher:
                record_retry_after(retry)
            declared = response.headers.get("Content-Length")
            if declared and (
                not declared.isdigit() or int(declared) > byte_limit
            ):
                raise AdapterError((
                    "Oversized or invalid response length"
                ), (
                    "fetch"
                ))
            body = response.read(byte_limit + 1)
            if (len(body) > byte_limit
                    or declared and len(body) != int(declared)):
                raise AdapterError("Incomplete or oversized response", "fetch")
            encoding = response.headers.get("Content-Encoding", "identity")
            if encoding != "identity":
                raise AdapterError((
                    "Unsupported response content encoding"
                ), (
                    "fetch"
                ))
            location = response.headers.get("Location")
        if status in (301, 302, 303, 307, 308):
            if not location or redirect == 5:
                raise AdapterError("Invalid redirect chain", "access")
            destination = urllib.parse.urljoin(current, location)
            old, new = (
                urllib.parse.urlsplit(current),
                urllib.parse.urlsplit(destination),
            )
            if publisher:
                public_url(destination)
                if old.scheme == "https" and new.scheme != "https":
                    raise AdapterError((
                        "Publisher redirect transport downgrade"
                    ), (
                        "access"
                    ))
                check_robots(destination)
            else:
                if new.scheme != "https" or new.username or new.password:
                    raise AdapterError((
                        "Unsafe authenticated redirect"
                    ), (
                        "publish"
                    ))
                if new.netloc != old.netloc:
                    headers = {key: value for key, value in headers.items()
                               if key.lower() != "authorization"}
                if urllib.parse.urlsplit(url).hostname == "api.github.com":
                    raise AdapterError((
                        "Unexpected GitHub API redirect"
                    ), (
                        "publish"
                    ))
            current = destination
            continue
        if publisher and status in (401, 403, 429):
            raise AdapterError((
                f"Publisher refused access: HTTP {status}"
            ), (
                "access"
            ), status)
        return status, response_headers, body
    raise AdapterError("Unresolved redirect", "access")


def check_robots(url):
    import urllib.robotparser
    parsed = urllib.parse.urlsplit(public_url(url))
    origin = parsed.scheme + "://" + parsed.netloc
    if parsed.path == "/robots.txt":
        return
    if origin not in _ROBOTS:
        status, _, body = request_bytes(origin + "/robots.txt", publisher=True)
        if status == 404:
            _ROBOTS[origin] = None
        elif status == 200:
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(body.decode("utf-8", "strict").splitlines())
            _ROBOTS[origin] = parser
        else:
            raise AdapterError((
                "Publisher robots policy unavailable"
            ), (
                "access"
            ), status)
    parser = _ROBOTS[origin]
    if parser is not None and not parser.can_fetch(_USER_AGENT, url):
        raise AdapterError("Publisher robots excludes required input", "access")
    if parser is not None:
        delay = parser.crawl_delay(_USER_AGENT)
        if delay and delay > 6:
            with locked_budget():
                state = load_budget()
                if state["starts"]:
                    state[(
                        "not_before"
                    )] = max(state[(
                        "not_before"
                    )], state[(
                        "starts"
                    )][-1] + delay)
                    save_budget(state)
        rate = parser.request_rate(_USER_AGENT)
        if rate and rate.seconds / rate.requests > 6:
            raise AdapterError((
                "Stricter publisher request-rate requires review"
            ), (
                "access"
            ))


def fetch_public(url):
    reviewed = public_url(url)
    check_robots(reviewed)
    for attempt in range(2):
        status, headers, body = request_bytes(reviewed, publisher=True)
        if status == 200:
            return status, headers, body
        if status == 503 and attempt == 0:
            with locked_budget():
                state = load_budget()
                now = time.time()
                state["not_before"] = max(state["not_before"], now + 30)
                state["observed_at"] = now
                save_budget(state)
            continue
        key = next((key for key, info in INPUTS.items()
                    if info["url"] == url), "unmapped-public-input")
        parsed = urllib.parse.urlsplit(reviewed)
        provenance = parsed.scheme + "://" + parsed.netloc + parsed.path
        raise AdapterError(
            f"Required publisher input {key} returned HTTP {status}: "
            + provenance, "fetch", status)


def workflow_api(path):
    """Read authenticated workflow metadata without publication permissions."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise AdapterError("Workflow state credentials missing", "access")
    try:
        status, _, body = request_bytes(
            _WORKFLOW_GITHUB + path,
            headers={"Authorization": "Bearer " + token,
                     "Accept": "application/vnd.github+json"},
        )
        if status != 200:
            raise ValueError("Workflow API status")
        return json.loads(body)
    except Exception:
        raise AdapterError((
            "Workflow pacing state unavailable"
        ), (
            "access"
        )) from None


def current_run_identity():
    if (os.environ.get("GITHUB_REPOSITORY") != "YouthOpps/data-pipeline"
            or os.environ.get("GITHUB_REF") != "refs/heads/main"):
        raise AdapterError("Unexpected workflow repository", "access")
    values = [os.environ.get(key, "") for key in ((
        "GITHUB_RUN_ID"
    ), (
        "GITHUB_RUN_ATTEMPT"
    ))]
    if any(not re.fullmatch(r"[1-9][0-9]*", value) for value in values):
        raise AdapterError("Invalid workflow run identity", "access")
    return tuple(map(int, values))


def family_source(run):
    path = run.get("path", "")
    for source in FAMILY_SOURCES:
        if path == ".github/workflows/fetch-" + source + ".yml":
            return source
    return None


def completed_identity(run, expected=None):
    """Bind a completed attempt and its chronological publisher interval."""
    try:
        source = family_source(run)
        identity, attempt = run["id"], run["run_attempt"]
        if (not source or type(identity) is not int or identity <= 0
                or type(attempt) is not int or attempt <= 0
                or run["status"] != "completed"
                or run["repository"]["full_name"] != "YouthOpps/data-pipeline"
                or run[(
                    "head_repository"
                )][(
                    "full_name"
                )] != (
                    "YouthOpps/data-pipeline"
                )
                or run["head_branch"] != "main"
                or expected and (identity, attempt, source) != expected):
            raise ValueError("Untrusted completed attempt")
        times = []
        for field in ("run_started_at", "updated_at"):
            value = datetime.fromisoformat(run[field].replace("Z", "+00:00"))
            if value.utcoffset() is None:
                raise ValueError("Missing attempt timezone")
            times.append(value.timestamp())
        began, finished = times
        if not 0 <= began <= finished <= time.time() + 1:
            raise ValueError("Invalid attempt chronology")
        return finished, began, identity, attempt, source
    except Exception:
        raise AdapterError((
            "Untrusted completed family attempt"
        ), (
            "access"
        )) from None


class InventoryRace(AdapterError):
    """A changed advertised total permits a bounded fresh history scan."""


def latest_family_run():
    """Restart only count-drift races, without reusing partial candidates."""
    for attempt in range(3):
        try:
            return scan_family_history()
        except InventoryRace:
            if attempt == 2:
                raise AdapterError(
                    "Workflow inventory remained unstable", "access") from None


def scan_family_history():
    """Select newest completed chronology, including older-ID rerun attempts."""
    current_id, current_attempt = current_run_identity()
    candidates = []
    total, scanned = None, 0
    seen_ids = set()
    for page in range(1, 101):
        result = workflow_api(f"/actions/runs?per_page=100&page={page}")
        count, runs = result.get("total_count"), result.get("workflow_runs")
        if (type(count) is not int or count < 0
                or not isinstance(runs, list) or len(runs) > 100):
            raise AdapterError("Invalid workflow run inventory", "access")
        if total is None:
            total = count
        elif total != count:
            raise InventoryRace((
                "Workflow inventory changed during restore"
            ), (
                "access"
            ))
        scanned += len(runs)
        for run in runs:
            if not isinstance(run, dict):
                raise AdapterError("Invalid workflow run entry", "access")
            run_id = run.get("id")
            if (type(run_id) is not int or run_id <= 0
                    or run_id in seen_ids):
                raise AdapterError(
                    "Invalid or duplicate workflow run identity", "access")
            seen_ids.add(run_id)
            source = family_source(run)
            if not source:
                continue
            if (run.get((
                "repository"
            ), {}).get((
                "full_name"
            )) != (
                "YouthOpps/data-pipeline"
            )
                    or run.get("head_repository", {}).get("full_name")
                    != (
                        "YouthOpps/data-pipeline"
                    ) or run.get((
                        "head_branch"
                    )) != (
                        "main"
                    )):
                raise AdapterError((
                    "Workflow repository or branch mismatch"
                ), (
                    "access"
                ))
            identity, attempt = run.get("id"), run.get("run_attempt")
            if type(identity) is not int or type(attempt) is not int:
                raise AdapterError("Invalid family run identity", "access")
            if identity == current_id:
                if attempt != current_attempt:
                    raise AdapterError((
                        "Current workflow attempt mismatch"
                    ), (
                        "access"
                    ))
                if current_attempt > 1:
                    prior = workflow_api((
                        f"/actions/runs/{identity}/attempts/"
                        f"{current_attempt - 1}"
                    ))
                    candidates.append(completed_identity(
                        prior, (identity, current_attempt - 1, source)))
                continue
            if run.get((
                "status"
            )) in ((
                "queued"
            ), (
                "waiting"
            ), (
                "pending"
            ), (
                "requested"
            )):
                continue
            if run.get("status") != "completed":
                raise AdapterError("Another family run is active", "access")
            candidates.append(completed_identity(run))
        if scanned >= total:
            if scanned != total:
                raise AdapterError((
                    "Workflow inventory count mismatch"
                ), (
                    "access"
                ))
            if not candidates:
                return None
            ordered = sorted(candidates)
            if len(ordered) > 1 and ordered[-1][0] == ordered[-2][0]:
                raise AdapterError((
                    "Ambiguous family attempt chronology"
                ), (
                    "access"
                ))
            return ordered[-1][2:]
        if len(runs) != 100:
            raise AdapterError("Incomplete workflow inventory", "access")
    raise AdapterError("Workflow inventory exceeds reviewed bound", "access")


def download_inert_artifact(identity, artifact_id, digest=None):
    (
        "Download a bounded authenticated artifact, stripping "
        "cross-origin auth."
    )
    token = os.environ.get("GITHUB_TOKEN", "")
    url = _WORKFLOW_GITHUB + f"/actions/artifacts/{artifact_id}/zip"
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "User-Agent": _USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
    try:
        try:
            response = _OPENER.open(request, timeout=60)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            if response.status != 302:
                raise ValueError("Expected artifact redirect")
            destination = response.headers.get("Location", "")
            response.read(1001)
        parsed = urllib.parse.urlsplit(destination)
        if (parsed.scheme != "https" or not parsed.hostname
                or parsed.username or parsed.password or parsed.port):
            raise ValueError("Unsafe artifact URL")
        if ("." not in parsed.hostname
                or parsed.hostname.endswith(((
                    ".local"
                ), (
                    ".internal"
                ), (
                    ".localhost"
                )))):
            raise ValueError("Private artifact hostname")
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            pass
        else:
            raise ValueError("Literal artifact address")
        # Signed URL is never stored or included in exceptions; no auth header.
        status, _, body = request_bytes(destination, byte_limit=32768)
        if status != 200 or len(body) > 32768:
            raise ValueError("Invalid artifact response")
        if digest is not None:
            if (not isinstance(digest, str)
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                    or digest[7:] != hashlib.sha256(body).hexdigest()):
                raise ValueError("Artifact digest mismatch")
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            entries = archive.infolist()
            if len(entries) != 1 or entries[0].filename != "pacing-state.json":
                raise ValueError("Unexpected artifact members")
            entry = entries[0]
            if entry.file_size > 16384 or entry.flag_bits & 1:
                raise ValueError("Oversized or encrypted state")
            if entry.external_attr >> 16 & 0o170000 == 0o120000:
                raise ValueError("State symlink")
            raw = archive.read(entry)
        envelope = json.loads(raw)
        if not isinstance(envelope, dict) or set(envelope) != {
            "schema", "repository", "run_id", "run_attempt", "source", "state",
        }:
            raise ValueError("Invalid artifact envelope")
        run_id, attempt, source = identity
        if (type(envelope["schema"]) is not int or envelope["schema"] != 1
                or envelope["repository"] != "YouthOpps/data-pipeline"
                or type(envelope["run_id"]) is not int
                or type(envelope["run_attempt"]) is not int
                or envelope["run_id"] != run_id
                or envelope["run_attempt"] != attempt
                or envelope["source"] != source):
            raise ValueError("Artifact ownership mismatch")
        return validate_budget(envelope["state"])
    except Exception:
        raise AdapterError((
            "Invalid or unavailable family pacing artifact"
        ), (
            "access"
        )) from None


def restore_family_artifact():
    global _RESTORED, _STATE_TRUSTED
    if _RESTORED:
        return
    latest = latest_family_run()
    if latest is None:
        raw = os.environ.get("DZS_PACING_BOOTSTRAP", "")
        try:
            bootstrap = json.loads(raw)
            if (set(bootstrap) != {"schema", "family", "not_before", "evidence"}
                    or type(bootstrap["schema"]) is not int
                    or bootstrap["schema"] != 1 or bootstrap["family"] != FAMILY
                    or not numeric(bootstrap["not_before"])
                    or not isinstance(bootstrap["evidence"], str)
                    or not re.fullmatch((
                        "https://github\\.com/YouthOpps/data-pipeline/(?:is"
                        "sues|pull)/[0-9]+(?:#issuecomment-[0-9]+)?"
                    ), bootstrap[(
                        "evidence"
                    )])):
                raise ValueError("Invalid bootstrap")
            incoming = empty_budget(time.time())
            incoming[(
                "not_before"
            )] = max(time.time() + 60, bootstrap[(
                "not_before"
            )])
        except Exception:
            raise AdapterError((
                "Reviewed first-family bootstrap required"
            ), (
                "access"
            )) from None
    else:
        run_id, attempt, source = latest
        expected = _ARTIFACT_NAME + "-" + str(attempt)
        result = workflow_api(f"/actions/runs/{run_id}/artifacts?per_page=100")
        artifacts = result.get("artifacts")
        if (not isinstance(artifacts, list) or result.get((
            "total_count"
        )) != len(artifacts)
                or len(artifacts) > 100):
            raise AdapterError("Incomplete family artifact inventory", "access")
        matches = [a for a in artifacts if a.get("name") == expected]
        if len(matches) != 1 or matches[0].get("expired") is not False:
            raise AdapterError((
                "Newest family pacing artifact missing or expired"
            ), (
                "access"
            ))
        artifact = matches[0]
        if (type(artifact.get("id")) is not int or artifact["id"] <= 0
                or artifact.get("workflow_run", {}).get("id") != run_id
                or not numeric(artifact.get("size_in_bytes"))
                or artifact["size_in_bytes"] > 32768):
            raise AdapterError("Invalid family artifact binding", "access")
        incoming = download_inert_artifact(latest, artifact[(
            "id"
        )], artifact.get((
            "digest"
        )))
    with locked_budget():
        local = load_budget()
        now = time.time()
        combined = empty_budget(now)
        combined[(
            "not_before"
        )] = max(local[(
            "not_before"
        )], incoming[(
            "not_before"
        )], now + 60)
        combined["blocked"] = local["blocked"] or incoming["blocked"]
        combined[(
            "starts"
        )] = sorted(set(local[(
            "starts"
        )] + incoming[(
            "starts"
        )]))[-10:]
        save_budget(combined)
    _RESTORED, _STATE_TRUSTED = True, True


def export_family_artifact():
    """Best-effort infrastructure state; never contains records or secrets."""
    path = os.environ.get("DZS_PACING_ARTIFACT_PATH")
    if not path or not _STATE_TRUSTED:
        return
    run_id, attempt = current_run_identity()
    with locked_budget():
        state = load_budget()
    envelope = {
        "schema": 1, "repository": "YouthOpps/data-pipeline",
        "run_id": run_id, "run_attempt": attempt, "source": SOURCE_ID,
        "state": state,
    }
    configured = os.environ.get("RUNNER_TEMP", "")
    if not configured or not os.path.isabs(configured):
        raise AdapterError((
            "Explicit runner temporary directory required"
        ), (
            "access"
        ))
    root = os.path.realpath(configured)
    if os.path.dirname(os.path.abspath(path)) != root:
        raise AdapterError("Unsafe pacing artifact path", "access")
    fd = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(fd, "w") as file:
        json.dump(envelope, file, separators=(",", ":"))


def prepare_collection():
    global _COLLECTION_STARTED, _STATE_TRUSTED, _COLLECTION_LOCK_FD
    _COLLECTION_STARTED = time.time()
    budget_file()
    if _COLLECTION_LOCK_FD is None:
        path = os.path.join(os.path.dirname(_STATE_PATH), "collection.lock")
        descriptor = os.open(
            path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            if os.fstat(descriptor).st_uid != os.getuid():
                raise OSError("Wrong lock owner")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(descriptor)
            raise AdapterError((
                "Another local family collector is active"
            ), (
                "access"
            )) from None
        _COLLECTION_LOCK_FD = descriptor
    if os.environ.get("GITHUB_ACTIONS") == "true":
        restore_family_artifact()
    _STATE_TRUSTED = True


def public_url(value):
    """Validate a literal reviewed source URL, retaining meaningful queries."""
    parsed = urllib.parse.urlsplit(value)
    allowed = {
        "studyin.gov.cz", "studujnavs.gov.cz", "www.dzs.cz",
        "msmt.gov.cz", "www.btha.cz", "static.daad.de",
    }
    if (parsed.hostname not in allowed or parsed.username or parsed.password
            or parsed.port or parsed.scheme not in ("http", "https")):
        raise AdapterError("Unreviewed public source origin", "access")
    if parsed.scheme == "http" and parsed.hostname != "static.daad.de":
        raise AdapterError("Unreviewed public source transport", "access")
    if re.search((
        "/(?:ucet|login|register|account|prihlasit|registrovat)(?:/|$)"
    ),
                 parsed.path, re.I):
        raise AdapterError("Private application route is excluded", "access")
    return urllib.parse.urlunsplit(parsed._replace(fragment=""))


def document_root(text, complete=True):
    if complete and not re.search(
            r"</body\s*>\s*</html\s*>\s*(?:<!--[\s\S]*?-->\s*)*$",
            text, re.I):
        raise AdapterError("Incomplete public HTML", "parse")
    root = PublisherHTML(text).root
    title = " ".join(n.text() for n in nodes(root, "title")).lower()
    if re.match(r"just a moment|access denied|attention required|"
                r"checking your browser|verify you are human", title):
        raise AdapterError("Publisher access challenge", "access")
    return root


def source_facts(key, root):
    (
        "Hash complete visible material and linked provenance, never "
        "publish it."
    )
    url = INPUTS[key]["url"]
    links = []
    canonicals = []
    for node in nodes(root):
        if node.tag == "link" and node.attrs.get("rel") == "canonical":
            canonicals.append(node.attrs.get("href", ""))
        if node.tag not in ("a", "form"):
            continue
        target = node.attrs.get("href" if node.tag == "a" else "action", "")
        ajax = node.attrs.get("data-ajax", "")
        values = []
        for value in (target, ajax):
            parsed = urllib.parse.urlsplit(urllib.parse.urljoin(url, value))
            if parsed.username or parsed.password:
                raise AdapterError("Unsafe public provenance link", "parse")
            values.append(urllib.parse.urlunsplit(parsed))
        links.append((node.tag, node.text(), *values))
    # All visible body/footer/noscript text is retained; no personal-text
    # exclusions or keyword guesses can hide a newly inserted condition.
    fields = sorted((n.attrs.get("name"), n.attrs.get("value", ""))
                    for n in nodes(root, "input")
                    if n.attrs.get("name") in {"isOutgoing", "compatriots"})
    facts = {"text": root.text(), "links": sorted(set(links)),
             "canonical": canonicals, "scope_fields": fields}
    if INPUTS[key].get("catalogue"):
        facts["catalogue"] = catalogue_facts(key, root)
    return json.dumps(facts, ensure_ascii=False, sort_keys=True)


def catalogue_facts(key, root):
    info = INPUTS[key]
    scope, number = info["scope"], info["page"]
    details = []
    for node in nodes(root, "a"):
        target = urllib.parse.urlsplit(urllib.parse.urljoin(
            info["url"], node.attrs.get("href", "")))
        if target.path == "/en/scholarships/scholarship-detail/":
            query = urllib.parse.parse_qs(target.query)
            if set(query) != {"id"} or len(query["id"]) != 1:
                raise AdapterError((
                    "Invalid catalogue programme identity"
                ), (
                    "parse"
                ))
            value = query["id"][0]
            if not re.fullmatch(r"[1-9]\d*", value):
                raise AdapterError("Invalid catalogue programme ID", "parse")
            details.append(int(value))
    if len(set(details)) != len(details):
        raise AdapterError("Repeated catalogue programme", "parse")
    if scope == "compatriots":
        if (details or "No scholarships found for the given criteria"
                not in root.text() or sorted({
                    (n.attrs.get("name"), n.attrs.get("value"))
                    for n in nodes(root, "input")
                    if n.attrs.get("name") in {"isOutgoing", "compatriots"}})
                != [("compatriots", "true"), ("isOutgoing", "true")]):
            raise AdapterError("Invalid compatriot empty scope", "parse")
        return {"scope": scope, "ids": [], "total": 0, "page": 1}
    current = [n.text() for n in nodes(root)
               if n.attrs.get("aria-current") == "page"
               and re.fullmatch(r"[1-9][0-9]*", n.text())]
    totals = re.findall(r"Results found:\s*(\d+)", root.text())
    if current != [str(number)] or totals != [str(info["total"])]:
        raise AdapterError("Catalogue current page/total changed", "parse")
    expected_count = min(10, info["total"] - (number - 1) * 10)
    if len(details) != expected_count:
        raise AdapterError("Incomplete catalogue page", "parse")
    pages = set()
    for node in nodes(root, "a"):
        ajax = node.attrs.get("data-ajax", "")
        parsed = urllib.parse.urlsplit(ajax)
        if parsed.path != "/ajax/scholarshipsearch/switchpage":
            continue
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        if (query.get((
            "isOutgoing"
        )) != [(
            "true"
        ) if scope == (
            "outgoing"
        ) else (
            "false"
        )]
                or query.get("compatriots") != ["false"]
                or query.get("pageSize") != ["10"]
                or query.get("language") != ["en"]):
            raise AdapterError("Catalogue navigation scope changed", "parse")
        try:
            pages.add(int(query["page"][0]))
        except (ValueError, KeyError, IndexError):
            raise AdapterError("Malformed catalogue pager", "parse")
    last = (info["total"] + 9) // 10
    if ((number < last and number + 1 not in pages)
            or (number == last and any(n > last for n in pages))):
        raise AdapterError("Catalogue terminal proof failed", "parse")
    return {"scope": scope, "ids": details, "total": info["total"],
            "page": number, "last": last}


def validate_asset(key, body):
    asset = INPUTS[key]
    if hashlib.sha256(body).hexdigest() != asset["sha256"]:
        raise AdapterError("Material document changed: " + key, "parse")
    if asset["format"] == "pdf":
        if not body.startswith(b"%PDF-") or b"%%EOF" not in body[-2048:]:
            raise AdapterError("Incomplete material PDF: " + key, "parse")
    else:
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                if ("[Content_Types].xml" not in archive.namelist()
                        or sum(n.file_size for n in archive.infolist())
                        > 30_000_000
                        or archive.testzip() is not None):
                    raise ValueError()
        except (ValueError, zipfile.BadZipFile, RuntimeError):
            raise AdapterError("Invalid material spreadsheet: " + key, "parse")
    return asset["sha256"]


def read_input(key):
    info = INPUTS[key]
    status, headers, body = fetch_public(info["url"])
    if info["format"] in ("pdf", "xlsx"):
        return validate_asset(key, body)
    try:
        text = body.decode(headers.get_content_charset() or "utf-8")
    except (UnicodeError, LookupError):
        raise AdapterError("Invalid publisher text encoding: " + key, "parse")
    if info["format"] == "json":
        try:
            envelope = json.loads(text)
            if (not isinstance(envelope, dict)
                    or set(envelope) != {"version", "data"}
                    or type(envelope["version"]) is not int
                    or envelope["version"] != 1
                    or not isinstance(envelope["data"], list)
                    or len(envelope["data"]) != 1):
                raise ValueError()
            item = envelope["data"][0]
            if (not isinstance(item, dict)
                    or set(item) != {"content", "targetBlock", "type"}
                    or item["targetBlock"] != "#scholarshipSearchListContainer"
                    or item["type"] != "html"
                    or not isinstance(item["content"], str)):
                raise ValueError()
            root = document_root(item["content"], complete=False)
        except (ValueError, TypeError, KeyError):
            raise AdapterError("Incomplete public catalogue response", "parse")
    else:
        root = document_root(text)
    return source_facts(key, root)


def read_pages():
    prepare_collection()
    pages = {}
    for key in INPUTS:
        value = read_input(key)
        actual = (value if INPUTS[key]["format"] in ("pdf", "xlsx")
                  else hashlib.sha256(value.encode()).hexdigest())
        if actual != CONDITIONS[key]:
            raise AdapterError("Reviewed source facts/frontier changed: " + key,
                               "parse")
        pages[key] = value
    return pages


def parse_inventory(pages):
    if set(pages) != set(INPUTS):
        raise AdapterError("Incomplete whole-source evidence", "parse")
    union = set()
    scopes = {"incoming": set(), "outgoing": set(), "compatriots": set()}
    for key, value in pages.items():
        info = INPUTS[key]
        actual = (value if info["format"] in ("pdf", "xlsx")
                  else hashlib.sha256(value.encode()).hexdigest())
        if actual != CONDITIONS[key]:
            raise AdapterError("Changed whole-source material: " + key, "parse")
        if info.get("catalogue"):
            facts = json.loads(value)["catalogue"]
            ids = set(facts["ids"])
            if union.intersection(ids):
                raise AdapterError((
                    "Repeated candidate across catalogue pages"
                ), (
                    "parse"
                ))
            union.update(ids)
            scopes[facts["scope"]].update(ids)
    if (union != set(PROGRAMME_IDS) or len(scopes["incoming"]) != 60
            or len(scopes["outgoing"]) != 222 or scopes["compatriots"]):
        raise AdapterError("Whole-catalogue terminal union changed", "parse")
    records = []
    today = utc_now()[:10]
    for profile in PROFILES:
        record = make_record(profile["title"], profile["url"],
                             profile["categories"], kind=profile["kind"],
                             host_countries=profile["hosts"],
                             evidence=[ATTRIBUTION, *profile["evidence"]])
        record["id"] = hashlib.sha256(
            (SOURCE_ID + "|programme-" + str(profile["programme"])).encode()
        ).hexdigest()[:24]
        record["summary"] = profile["summary"]
        record["eligible_countries"] = profile["eligible"]
        record["deadline"] = profile["deadline"]
        deadline, opening = profile["deadline"], profile.get("opening")
        status = "unknown"
        if not profile.get("uncertain") and deadline:
            if today > deadline:
                status = "expired"
            elif today < deadline and opening and today >= opening:
                status = "open"
        record["status"] = status
        records.append(record)
    validate_records(records)
    if len(records) != 269:
        raise AdapterError("Incomplete reviewed identity partition", "validate")
    return records


def collect():
    return parse_inventory(read_pages())


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

_APP_TOKEN = None
_APP_EXPIRY = 0
_APP_CONFIGURATION = None


class DER:
    """Strict definite-length DER reader for unencrypted RSA private keys."""

    def __init__(self, data):
        self.data, self.offset = data, 0

    def read(self, tag):
        if self.offset + 2 > len(self.data):
            raise ValueError("Incomplete DER")
        actual, length = self.data[self.offset:self.offset + 2]
        self.offset += 2
        if actual != tag:
            raise ValueError("Unexpected DER tag")
        if length & 128:
            count = length & 127
            if not 1 <= count <= 4:
                raise ValueError("Invalid DER length")
            raw = self.data[self.offset:self.offset + count]
            if len(raw) != count or raw[0] == 0:
                raise ValueError("Nonminimal DER length")
            self.offset += count
            length = int.from_bytes(raw, "big")
            if length < 128:
                raise ValueError("Nonminimal DER length")
        end = self.offset + length
        if end > len(self.data):
            raise ValueError("Incomplete DER value")
        result = self.data[self.offset:end]
        self.offset = end
        return result

    def integer(self):
        raw = self.read(2)
        if not raw or raw[0] & 128:
            raise ValueError("Negative or empty DER integer")
        if len(raw) > 1 and raw[0] == 0 and not raw[1] & 128:
            raise ValueError("Nonminimal DER integer")
        return int.from_bytes(raw, "big")

    def finish(self):
        if self.offset != len(self.data):
            raise ValueError("Trailing DER content")


def rsa_key(pem):
    """Validate a complete PKCS#1/PKCS#8 two-prime RSA private key."""
    try:
        if not isinstance(pem, str) or len(pem) > 20000:
            raise ValueError("Invalid PEM size")
        match = re.fullmatch(
            r"\s*-----BEGIN (RSA PRIVATE KEY|PRIVATE KEY)-----\r?\n"
            r"([A-Za-z0-9+/=\r\n]+)-----END \1-----\s*", pem,
        )
        if not match:
            raise ValueError("Unsupported PEM")
        encoded = match[2].replace("\r", "").replace("\n", "")
        raw = base64.b64decode(encoded, validate=True)
        if base64.b64encode(raw).decode() != encoded:
            raise ValueError("Noncanonical PEM encoding")
        outer = DER(raw)
        sequence = DER(outer.read(48))
        outer.finish()
        if match[1] == "PRIVATE KEY":
            if sequence.integer() != 0:
                raise ValueError("Unsupported PKCS#8 version")
            algorithm = DER(sequence.read(48))
            if algorithm.read(6) != bytes.fromhex("2a864886f70d010101"):
                raise ValueError("Not RSA")
            if algorithm.read(5) != b"":
                raise ValueError("Invalid RSA parameters")
            algorithm.finish()
            inner = DER(sequence.read(4))
            sequence.finish()
            sequence = DER(inner.read(48))
            inner.finish()
        values = [sequence.integer() for _ in range(9)]
        sequence.finish()
        version, n, e, d, p, q, dp, dq, inverse = values
        if (
            version != 0 or not 2048 <= n.bit_length() <= 8192
            or e < 3 or e % 2 == 0 or e >= n
            or min(d, p, q, dp, dq, inverse) <= 0
            or d >= n or inverse >= p
            or p == q or p % 2 == 0 or q % 2 == 0 or p * q != n
            or e * d % math.lcm(p - 1, q - 1) != 1
            or dp != d % (p - 1) or dq != d % (q - 1)
            or inverse * q % p != 1
        ):
            raise ValueError("Inconsistent RSA key")
        return n, e, d
    except Exception:
        raise AdapterError((
            "Invalid GitHub App signing key"
        ), (
            "publish"
        )) from None


def app_jwt(now):
    app_id = os.environ.get("WEBSITE_APP_ID", "")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", app_id):
        raise AdapterError("Invalid GitHub App identifier", "publish")
    n, e, d = rsa_key(os.environ.get("WEBSITE_APP_PRIVATE_KEY", ""))
    def encode(value):
        return base64.urlsafe_b64encode(value).rstrip(b"=")
    header = encode(b'{"alg":"RS256","typ":"JWT"}')
    payload = encode(json.dumps({
        "iat": int(now) - 60, "exp": int(now) + 540, "iss": app_id,
    }, separators=(",", ":")).encode())
    message = header + b"." + payload
    digest = bytes.fromhex("3031300d060960864801650304020105000420")
    digest += hashlib.sha256(message).digest()
    size = (n.bit_length() + 7) // 8
    padded = b"\x00\x01" + b"\xff" * (size - len(digest) - 3)
    padded += b"\x00" + digest
    signature = pow(int.from_bytes(padded, "big"), d, n)
    if pow(signature, e, n) != int.from_bytes(padded, "big"):
        raise AdapterError("Invalid RSA signing operation", "publish")
    return (message + b"." + encode(signature.to_bytes(size, "big"))).decode()


def app_api(method, path, jwt, payload=None):
    """Use only the authenticated GitHub origin; never expose credentials."""
    try:
        status, _, body = request_bytes(
            "https://api.github.com" + path, method=method,
            headers={
                "Authorization": "Bearer " + jwt,
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2026-03-10",
            }, payload=(json.dumps(payload).encode()
                        if payload is not None else None),
        )
        if status not in (200, 201):
            raise ValueError("App API failure")
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError("Invalid App response")
        return result
    except Exception:
        raise AdapterError((
            "GitHub App authentication failed"
        ), (
            "publish"
        )) from None


def publication_token():
    """Renew a repository-scoped installation token before expiry/CAS."""
    global _APP_TOKEN, _APP_EXPIRY, _APP_CONFIGURATION
    if not os.environ.get("WEBSITE_APP_ID"):
        token = os.environ.get("DATA_SOURCE_TOKEN")
        if not token:
            raise AdapterError("Publication credentials missing", "publish")
        return token
    configuration = hashlib.sha256((
        os.environ.get("WEBSITE_APP_ID", "") + "\0"
        + os.environ.get("WEBSITE_APP_INSTALLATION_ID", "") + "\0"
        + os.environ.get("WEBSITE_APP_PRIVATE_KEY", "")
    ).encode()).digest()
    if _APP_CONFIGURATION != configuration:
        _APP_TOKEN, _APP_EXPIRY = None, 0
        _APP_CONFIGURATION = configuration
    now = time.time()
    if _APP_TOKEN and now + 300 < _APP_EXPIRY <= now + 3700:
        return _APP_TOKEN
    jwt = app_jwt(now)
    installation = app_api(
        "GET", "/repos/YouthOpps/data-source/installation", jwt,
    )
    identity = installation.get("id")
    expected = os.environ.get("WEBSITE_APP_INSTALLATION_ID", "")
    account = installation.get("account", {})
    if (
        type(identity) is not int or identity <= 0
        or str(identity) != expected
        or not isinstance(account, dict)
        or installation.get("app_id") != int(os.environ["WEBSITE_APP_ID"])
        or account.get("login") != "YouthOpps"
        or account.get("type") != "Organization"
    ):
        raise AdapterError("Unexpected GitHub App installation", "publish")
    result = app_api((
        "POST"
    ), (
        f"/app/installations/{identity}/access_tokens"
    ), jwt, {
        "repositories": ["data-source"], "permissions": {"contents": "write"},
    })
    repositories = result.get("repositories")
    permissions = result.get("permissions", {})
    try:
        expiry = datetime.fromisoformat(
            result["expires_at"].replace("Z", "+00:00")
        )
        if expiry.utcoffset() is None:
            raise ValueError("Missing token timezone")
        expiry = expiry.timestamp()
    except Exception:
        raise AdapterError((
            "Invalid installation token expiry"
        ), (
            "publish"
        )) from None
    now = time.time()
    if (
        not isinstance(result.get("token"), str)
        or not re.fullmatch(r"[!-~]{1,4096}", result["token"])
        or not now + 300 < expiry <= now + 3700
        or not isinstance(repositories, list) or len(repositories) != 1
        or not isinstance(repositories[0], dict)
        or repositories[0].get("full_name") != "YouthOpps/data-source"
        or not isinstance(permissions, dict)
        or permissions.get("contents") != "write"
        or any(key not in ("contents", "metadata")
               or key == "metadata" and value != "read"
               for key, value in permissions.items())
    ):
        raise AdapterError("Unexpected installation token scope", "publish")
    _APP_TOKEN, _APP_EXPIRY = result["token"], expiry
    return _APP_TOKEN


def github(method, path, payload=None, missing_ok=False):
    token = publication_token()
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
        if file is not None and file.get("encoding") == "none":
            sha = file.get("sha", "")
            if not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise AdapterError("Invalid existing blob identity", "publish")
            file = github("GET", "/git/blobs/" + sha)
            if file.get("sha") != sha:
                raise AdapterError("Existing blob identity mismatch", "publish")
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
        "name": "Study in Czechia",
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
                f"Collected {len(records)} validated Study in Czechia "
                "funding programmes and placements"
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



# Reviewed finite source inputs.
INPUTS = {
    'programme-1': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=1'
        ),
        'format': 'html',
    },
    'programme-10': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=10'
        ),
        'format': 'html',
    },
    'programme-100': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=100'
        ),
        'format': 'html',
    },
    'programme-101': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=101'
        ),
        'format': 'html',
    },
    'programme-102': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=102'
        ),
        'format': 'html',
    },
    'programme-103': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=103'
        ),
        'format': 'html',
    },
    'programme-104': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=104'
        ),
        'format': 'html',
    },
    'programme-105': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=105'
        ),
        'format': 'html',
    },
    'programme-106': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=106'
        ),
        'format': 'html',
    },
    'programme-107': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=107'
        ),
        'format': 'html',
    },
    'programme-108': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=108'
        ),
        'format': 'html',
    },
    'programme-109': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=109'
        ),
        'format': 'html',
    },
    'programme-11': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=11'
        ),
        'format': 'html',
    },
    'programme-111': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=111'
        ),
        'format': 'html',
    },
    'programme-114': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=114'
        ),
        'format': 'html',
    },
    'programme-115': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=115'
        ),
        'format': 'html',
    },
    'programme-116': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=116'
        ),
        'format': 'html',
    },
    'programme-117': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=117'
        ),
        'format': 'html',
    },
    'programme-118': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=118'
        ),
        'format': 'html',
    },
    'programme-12': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=12'
        ),
        'format': 'html',
    },
    'programme-120': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=120'
        ),
        'format': 'html',
    },
    'programme-122': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=122'
        ),
        'format': 'html',
    },
    'programme-124': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=124'
        ),
        'format': 'html',
    },
    'programme-13': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=13'
        ),
        'format': 'html',
    },
    'programme-130': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=130'
        ),
        'format': 'html',
    },
    'programme-131': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=131'
        ),
        'format': 'html',
    },
    'programme-134': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=134'
        ),
        'format': 'html',
    },
    'programme-136': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=136'
        ),
        'format': 'html',
    },
    'programme-137': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=137'
        ),
        'format': 'html',
    },
    'programme-138': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=138'
        ),
        'format': 'html',
    },
    'programme-139': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=139'
        ),
        'format': 'html',
    },
    'programme-14': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=14'
        ),
        'format': 'html',
    },
    'programme-142': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=142'
        ),
        'format': 'html',
    },
    'programme-144': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=144'
        ),
        'format': 'html',
    },
    'programme-145': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=145'
        ),
        'format': 'html',
    },
    'programme-146': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=146'
        ),
        'format': 'html',
    },
    'programme-148': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=148'
        ),
        'format': 'html',
    },
    'programme-149': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=149'
        ),
        'format': 'html',
    },
    'programme-15': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=15'
        ),
        'format': 'html',
    },
    'programme-150': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=150'
        ),
        'format': 'html',
    },
    'programme-151': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=151'
        ),
        'format': 'html',
    },
    'programme-152': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=152'
        ),
        'format': 'html',
    },
    'programme-153': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=153'
        ),
        'format': 'html',
    },
    'programme-154': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=154'
        ),
        'format': 'html',
    },
    'programme-155': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=155'
        ),
        'format': 'html',
    },
    'programme-157': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=157'
        ),
        'format': 'html',
    },
    'programme-159': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=159'
        ),
        'format': 'html',
    },
    'programme-16': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=16'
        ),
        'format': 'html',
    },
    'programme-160': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=160'
        ),
        'format': 'html',
    },
    'programme-163': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=163'
        ),
        'format': 'html',
    },
    'programme-164': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=164'
        ),
        'format': 'html',
    },
    'programme-165': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=165'
        ),
        'format': 'html',
    },
    'programme-166': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=166'
        ),
        'format': 'html',
    },
    'programme-167': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=167'
        ),
        'format': 'html',
    },
    'programme-168': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=168'
        ),
        'format': 'html',
    },
    'programme-169': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=169'
        ),
        'format': 'html',
    },
    'programme-17': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=17'
        ),
        'format': 'html',
    },
    'programme-170': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=170'
        ),
        'format': 'html',
    },
    'programme-171': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=171'
        ),
        'format': 'html',
    },
    'programme-172': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=172'
        ),
        'format': 'html',
    },
    'programme-173': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=173'
        ),
        'format': 'html',
    },
    'programme-174': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=174'
        ),
        'format': 'html',
    },
    'programme-175': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=175'
        ),
        'format': 'html',
    },
    'programme-176': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=176'
        ),
        'format': 'html',
    },
    'programme-177': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=177'
        ),
        'format': 'html',
    },
    'programme-178': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=178'
        ),
        'format': 'html',
    },
    'programme-179': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=179'
        ),
        'format': 'html',
    },
    'programme-18': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=18'
        ),
        'format': 'html',
    },
    'programme-180': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=180'
        ),
        'format': 'html',
    },
    'programme-181': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=181'
        ),
        'format': 'html',
    },
    'programme-182': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=182'
        ),
        'format': 'html',
    },
    'programme-183': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=183'
        ),
        'format': 'html',
    },
    'programme-184': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=184'
        ),
        'format': 'html',
    },
    'programme-185': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=185'
        ),
        'format': 'html',
    },
    'programme-186': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=186'
        ),
        'format': 'html',
    },
    'programme-187': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=187'
        ),
        'format': 'html',
    },
    'programme-188': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=188'
        ),
        'format': 'html',
    },
    'programme-189': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=189'
        ),
        'format': 'html',
    },
    'programme-19': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=19'
        ),
        'format': 'html',
    },
    'programme-190': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=190'
        ),
        'format': 'html',
    },
    'programme-191': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=191'
        ),
        'format': 'html',
    },
    'programme-192': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=192'
        ),
        'format': 'html',
    },
    'programme-193': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=193'
        ),
        'format': 'html',
    },
    'programme-194': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=194'
        ),
        'format': 'html',
    },
    'programme-195': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=195'
        ),
        'format': 'html',
    },
    'programme-196': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=196'
        ),
        'format': 'html',
    },
    'programme-197': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=197'
        ),
        'format': 'html',
    },
    'programme-198': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=198'
        ),
        'format': 'html',
    },
    'programme-199': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=199'
        ),
        'format': 'html',
    },
    'programme-2': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=2'
        ),
        'format': 'html',
    },
    'programme-20': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=20'
        ),
        'format': 'html',
    },
    'programme-200': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=200'
        ),
        'format': 'html',
    },
    'programme-201': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=201'
        ),
        'format': 'html',
    },
    'programme-202': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=202'
        ),
        'format': 'html',
    },
    'programme-203': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=203'
        ),
        'format': 'html',
    },
    'programme-204': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=204'
        ),
        'format': 'html',
    },
    'programme-205': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=205'
        ),
        'format': 'html',
    },
    'programme-206': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=206'
        ),
        'format': 'html',
    },
    'programme-207': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=207'
        ),
        'format': 'html',
    },
    'programme-208': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=208'
        ),
        'format': 'html',
    },
    'programme-209': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=209'
        ),
        'format': 'html',
    },
    'programme-21': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=21'
        ),
        'format': 'html',
    },
    'programme-210': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=210'
        ),
        'format': 'html',
    },
    'programme-211': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=211'
        ),
        'format': 'html',
    },
    'programme-212': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=212'
        ),
        'format': 'html',
    },
    'programme-213': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=213'
        ),
        'format': 'html',
    },
    'programme-214': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=214'
        ),
        'format': 'html',
    },
    'programme-215': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=215'
        ),
        'format': 'html',
    },
    'programme-216': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=216'
        ),
        'format': 'html',
    },
    'programme-217': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=217'
        ),
        'format': 'html',
    },
    'programme-218': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=218'
        ),
        'format': 'html',
    },
    'programme-219': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=219'
        ),
        'format': 'html',
    },
    'programme-22': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=22'
        ),
        'format': 'html',
    },
    'programme-220': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=220'
        ),
        'format': 'html',
    },
    'programme-221': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=221'
        ),
        'format': 'html',
    },
    'programme-222': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=222'
        ),
        'format': 'html',
    },
    'programme-223': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=223'
        ),
        'format': 'html',
    },
    'programme-224': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=224'
        ),
        'format': 'html',
    },
    'programme-225': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=225'
        ),
        'format': 'html',
    },
    'programme-226': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=226'
        ),
        'format': 'html',
    },
    'programme-227': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=227'
        ),
        'format': 'html',
    },
    'programme-228': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=228'
        ),
        'format': 'html',
    },
    'programme-229': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=229'
        ),
        'format': 'html',
    },
    'programme-23': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=23'
        ),
        'format': 'html',
    },
    'programme-230': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=230'
        ),
        'format': 'html',
    },
    'programme-231': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=231'
        ),
        'format': 'html',
    },
    'programme-232': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=232'
        ),
        'format': 'html',
    },
    'programme-233': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=233'
        ),
        'format': 'html',
    },
    'programme-234': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=234'
        ),
        'format': 'html',
    },
    'programme-235': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=235'
        ),
        'format': 'html',
    },
    'programme-236': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=236'
        ),
        'format': 'html',
    },
    'programme-237': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=237'
        ),
        'format': 'html',
    },
    'programme-238': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=238'
        ),
        'format': 'html',
    },
    'programme-239': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=239'
        ),
        'format': 'html',
    },
    'programme-24': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=24'
        ),
        'format': 'html',
    },
    'programme-240': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=240'
        ),
        'format': 'html',
    },
    'programme-241': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=241'
        ),
        'format': 'html',
    },
    'programme-242': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=242'
        ),
        'format': 'html',
    },
    'programme-243': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=243'
        ),
        'format': 'html',
    },
    'programme-244': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=244'
        ),
        'format': 'html',
    },
    'programme-245': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=245'
        ),
        'format': 'html',
    },
    'programme-246': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=246'
        ),
        'format': 'html',
    },
    'programme-247': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=247'
        ),
        'format': 'html',
    },
    'programme-248': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=248'
        ),
        'format': 'html',
    },
    'programme-249': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=249'
        ),
        'format': 'html',
    },
    'programme-25': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=25'
        ),
        'format': 'html',
    },
    'programme-250': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=250'
        ),
        'format': 'html',
    },
    'programme-251': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=251'
        ),
        'format': 'html',
    },
    'programme-252': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=252'
        ),
        'format': 'html',
    },
    'programme-253': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=253'
        ),
        'format': 'html',
    },
    'programme-254': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=254'
        ),
        'format': 'html',
    },
    'programme-255': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=255'
        ),
        'format': 'html',
    },
    'programme-256': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=256'
        ),
        'format': 'html',
    },
    'programme-257': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=257'
        ),
        'format': 'html',
    },
    'programme-258': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=258'
        ),
        'format': 'html',
    },
    'programme-259': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=259'
        ),
        'format': 'html',
    },
    'programme-26': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=26'
        ),
        'format': 'html',
    },
    'programme-260': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=260'
        ),
        'format': 'html',
    },
    'programme-261': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=261'
        ),
        'format': 'html',
    },
    'programme-263': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=263'
        ),
        'format': 'html',
    },
    'programme-264': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=264'
        ),
        'format': 'html',
    },
    'programme-265': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=265'
        ),
        'format': 'html',
    },
    'programme-266': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=266'
        ),
        'format': 'html',
    },
    'programme-267': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=267'
        ),
        'format': 'html',
    },
    'programme-268': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=268'
        ),
        'format': 'html',
    },
    'programme-269': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=269'
        ),
        'format': 'html',
    },
    'programme-27': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=27'
        ),
        'format': 'html',
    },
    'programme-270': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=270'
        ),
        'format': 'html',
    },
    'programme-271': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=271'
        ),
        'format': 'html',
    },
    'programme-273': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=273'
        ),
        'format': 'html',
    },
    'programme-274': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=274'
        ),
        'format': 'html',
    },
    'programme-275': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=275'
        ),
        'format': 'html',
    },
    'programme-276': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=276'
        ),
        'format': 'html',
    },
    'programme-277': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=277'
        ),
        'format': 'html',
    },
    'programme-278': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=278'
        ),
        'format': 'html',
    },
    'programme-279': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=279'
        ),
        'format': 'html',
    },
    'programme-28': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=28'
        ),
        'format': 'html',
    },
    'programme-280': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=280'
        ),
        'format': 'html',
    },
    'programme-281': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=281'
        ),
        'format': 'html',
    },
    'programme-282': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=282'
        ),
        'format': 'html',
    },
    'programme-283': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=283'
        ),
        'format': 'html',
    },
    'programme-284': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=284'
        ),
        'format': 'html',
    },
    'programme-285': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=285'
        ),
        'format': 'html',
    },
    'programme-286': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=286'
        ),
        'format': 'html',
    },
    'programme-287': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=287'
        ),
        'format': 'html',
    },
    'programme-288': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=288'
        ),
        'format': 'html',
    },
    'programme-289': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=289'
        ),
        'format': 'html',
    },
    'programme-29': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=29'
        ),
        'format': 'html',
    },
    'programme-290': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=290'
        ),
        'format': 'html',
    },
    'programme-291': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=291'
        ),
        'format': 'html',
    },
    'programme-292': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=292'
        ),
        'format': 'html',
    },
    'programme-293': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=293'
        ),
        'format': 'html',
    },
    'programme-294': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=294'
        ),
        'format': 'html',
    },
    'programme-295': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=295'
        ),
        'format': 'html',
    },
    'programme-296': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=296'
        ),
        'format': 'html',
    },
    'programme-297': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=297'
        ),
        'format': 'html',
    },
    'programme-298': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=298'
        ),
        'format': 'html',
    },
    'programme-299': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=299'
        ),
        'format': 'html',
    },
    'programme-3': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=3'
        ),
        'format': 'html',
    },
    'programme-30': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=30'
        ),
        'format': 'html',
    },
    'programme-300': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=300'
        ),
        'format': 'html',
    },
    'programme-301': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=301'
        ),
        'format': 'html',
    },
    'programme-31': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=31'
        ),
        'format': 'html',
    },
    'programme-32': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=32'
        ),
        'format': 'html',
    },
    'programme-33': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=33'
        ),
        'format': 'html',
    },
    'programme-332': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=332'
        ),
        'format': 'html',
    },
    'programme-333': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=333'
        ),
        'format': 'html',
    },
    'programme-334': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=334'
        ),
        'format': 'html',
    },
    'programme-335': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=335'
        ),
        'format': 'html',
    },
    'programme-337': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=337'
        ),
        'format': 'html',
    },
    'programme-339': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=339'
        ),
        'format': 'html',
    },
    'programme-34': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=34'
        ),
        'format': 'html',
    },
    'programme-340': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=340'
        ),
        'format': 'html',
    },
    'programme-341': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=341'
        ),
        'format': 'html',
    },
    'programme-342': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=342'
        ),
        'format': 'html',
    },
    'programme-343': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=343'
        ),
        'format': 'html',
    },
    'programme-344': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=344'
        ),
        'format': 'html',
    },
    'programme-35': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=35'
        ),
        'format': 'html',
    },
    'programme-4': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=4'
        ),
        'format': 'html',
    },
    'programme-40': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=40'
        ),
        'format': 'html',
    },
    'programme-41': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=41'
        ),
        'format': 'html',
    },
    'programme-42': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=42'
        ),
        'format': 'html',
    },
    'programme-43': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=43'
        ),
        'format': 'html',
    },
    'programme-45': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=45'
        ),
        'format': 'html',
    },
    'programme-46': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=46'
        ),
        'format': 'html',
    },
    'programme-47': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=47'
        ),
        'format': 'html',
    },
    'programme-48': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=48'
        ),
        'format': 'html',
    },
    'programme-49': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=49'
        ),
        'format': 'html',
    },
    'programme-5': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=5'
        ),
        'format': 'html',
    },
    'programme-50': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=50'
        ),
        'format': 'html',
    },
    'programme-51': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=51'
        ),
        'format': 'html',
    },
    'programme-52': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=52'
        ),
        'format': 'html',
    },
    'programme-53': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=53'
        ),
        'format': 'html',
    },
    'programme-54': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=54'
        ),
        'format': 'html',
    },
    'programme-55': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=55'
        ),
        'format': 'html',
    },
    'programme-56': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=56'
        ),
        'format': 'html',
    },
    'programme-57': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=57'
        ),
        'format': 'html',
    },
    'programme-59': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=59'
        ),
        'format': 'html',
    },
    'programme-6': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=6'
        ),
        'format': 'html',
    },
    'programme-60': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=60'
        ),
        'format': 'html',
    },
    'programme-61': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=61'
        ),
        'format': 'html',
    },
    'programme-62': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=62'
        ),
        'format': 'html',
    },
    'programme-63': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=63'
        ),
        'format': 'html',
    },
    'programme-64': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=64'
        ),
        'format': 'html',
    },
    'programme-65': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=65'
        ),
        'format': 'html',
    },
    'programme-66': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=66'
        ),
        'format': 'html',
    },
    'programme-67': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=67'
        ),
        'format': 'html',
    },
    'programme-68': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=68'
        ),
        'format': 'html',
    },
    'programme-69': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=69'
        ),
        'format': 'html',
    },
    'programme-7': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=7'
        ),
        'format': 'html',
    },
    'programme-70': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=70'
        ),
        'format': 'html',
    },
    'programme-71': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=71'
        ),
        'format': 'html',
    },
    'programme-72': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=72'
        ),
        'format': 'html',
    },
    'programme-73': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=73'
        ),
        'format': 'html',
    },
    'programme-74': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=74'
        ),
        'format': 'html',
    },
    'programme-75': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=75'
        ),
        'format': 'html',
    },
    'programme-76': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=76'
        ),
        'format': 'html',
    },
    'programme-77': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=77'
        ),
        'format': 'html',
    },
    'programme-78': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=78'
        ),
        'format': 'html',
    },
    'programme-79': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=79'
        ),
        'format': 'html',
    },
    'programme-8': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=8'
        ),
        'format': 'html',
    },
    'programme-80': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=80'
        ),
        'format': 'html',
    },
    'programme-81': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=81'
        ),
        'format': 'html',
    },
    'programme-82': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=82'
        ),
        'format': 'html',
    },
    'programme-83': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=83'
        ),
        'format': 'html',
    },
    'programme-84': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=84'
        ),
        'format': 'html',
    },
    'programme-85': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=85'
        ),
        'format': 'html',
    },
    'programme-86': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=86'
        ),
        'format': 'html',
    },
    'programme-87': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=87'
        ),
        'format': 'html',
    },
    'programme-88': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=88'
        ),
        'format': 'html',
    },
    'programme-89': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=89'
        ),
        'format': 'html',
    },
    'programme-9': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=9'
        ),
        'format': 'html',
    },
    'programme-90': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=90'
        ),
        'format': 'html',
    },
    'programme-91': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=91'
        ),
        'format': 'html',
    },
    'programme-92': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=92'
        ),
        'format': 'html',
    },
    'programme-93': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=93'
        ),
        'format': 'html',
    },
    'programme-94': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=94'
        ),
        'format': 'html',
    },
    'programme-95': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=95'
        ),
        'format': 'html',
    },
    'programme-96': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=96'
        ),
        'format': 'html',
    },
    'programme-97': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=97'
        ),
        'format': 'html',
    },
    'programme-98': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=98'
        ),
        'format': 'html',
    },
    'programme-99': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=99'
        ),
        'format': 'html',
    },
    'sibling-178': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=178'
        ),
        'format': 'html',
    },
    'sibling-179': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=179'
        ),
        'format': 'html',
    },
    'sibling-180': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=180'
        ),
        'format': 'html',
    },
    'sibling-181': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=181'
        ),
        'format': 'html',
    },
    'sibling-182': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=182'
        ),
        'format': 'html',
    },
    'sibling-183': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=183'
        ),
        'format': 'html',
    },
    'sibling-184': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=184'
        ),
        'format': 'html',
    },
    'sibling-185': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=185'
        ),
        'format': 'html',
    },
    'sibling-186': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=186'
        ),
        'format': 'html',
    },
    'sibling-187': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=187'
        ),
        'format': 'html',
    },
    'sibling-188': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=188'
        ),
        'format': 'html',
    },
    'sibling-189': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=189'
        ),
        'format': 'html',
    },
    'sibling-190': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=190'
        ),
        'format': 'html',
    },
    'sibling-191': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=191'
        ),
        'format': 'html',
    },
    'sibling-192': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=192'
        ),
        'format': 'html',
    },
    'sibling-193': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=193'
        ),
        'format': 'html',
    },
    'sibling-194': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=194'
        ),
        'format': 'html',
    },
    'sibling-195': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=195'
        ),
        'format': 'html',
    },
    'sibling-196': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=196'
        ),
        'format': 'html',
    },
    'sibling-197': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=197'
        ),
        'format': 'html',
    },
    'sibling-198': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=198'
        ),
        'format': 'html',
    },
    'sibling-199': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=199'
        ),
        'format': 'html',
    },
    'sibling-200': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=200'
        ),
        'format': 'html',
    },
    'sibling-201': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=201'
        ),
        'format': 'html',
    },
    'sibling-202': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=202'
        ),
        'format': 'html',
    },
    'sibling-203': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=203'
        ),
        'format': 'html',
    },
    'sibling-204': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=204'
        ),
        'format': 'html',
    },
    'sibling-205': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=205'
        ),
        'format': 'html',
    },
    'sibling-206': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=206'
        ),
        'format': 'html',
    },
    'sibling-207': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=207'
        ),
        'format': 'html',
    },
    'sibling-208': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=208'
        ),
        'format': 'html',
    },
    'sibling-209': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=209'
        ),
        'format': 'html',
    },
    'sibling-210': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=210'
        ),
        'format': 'html',
    },
    'sibling-211': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=211'
        ),
        'format': 'html',
    },
    'sibling-212': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=212'
        ),
        'format': 'html',
    },
    'sibling-213': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=213'
        ),
        'format': 'html',
    },
    'sibling-214': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=214'
        ),
        'format': 'html',
    },
    'sibling-215': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=215'
        ),
        'format': 'html',
    },
    'sibling-216': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=216'
        ),
        'format': 'html',
    },
    'sibling-217': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=217'
        ),
        'format': 'html',
    },
    'sibling-218': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=218'
        ),
        'format': 'html',
    },
    'sibling-219': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=219'
        ),
        'format': 'html',
    },
    'sibling-220': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=220'
        ),
        'format': 'html',
    },
    'sibling-221': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=221'
        ),
        'format': 'html',
    },
    'sibling-222': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=222'
        ),
        'format': 'html',
    },
    'sibling-223': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=223'
        ),
        'format': 'html',
    },
    'sibling-224': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=224'
        ),
        'format': 'html',
    },
    'sibling-225': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=225'
        ),
        'format': 'html',
    },
    'sibling-226': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=226'
        ),
        'format': 'html',
    },
    'sibling-227': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=227'
        ),
        'format': 'html',
    },
    'sibling-228': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=228'
        ),
        'format': 'html',
    },
    'sibling-229': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=229'
        ),
        'format': 'html',
    },
    'sibling-230': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=230'
        ),
        'format': 'html',
    },
    'sibling-231': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=231'
        ),
        'format': 'html',
    },
    'sibling-232': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=232'
        ),
        'format': 'html',
    },
    'sibling-233': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=233'
        ),
        'format': 'html',
    },
    'sibling-234': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=234'
        ),
        'format': 'html',
    },
    'sibling-235': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=235'
        ),
        'format': 'html',
    },
    'sibling-236': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=236'
        ),
        'format': 'html',
    },
    'sibling-237': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=237'
        ),
        'format': 'html',
    },
    'sibling-238': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=238'
        ),
        'format': 'html',
    },
    'sibling-239': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=239'
        ),
        'format': 'html',
    },
    'sibling-240': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=240'
        ),
        'format': 'html',
    },
    'sibling-241': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=241'
        ),
        'format': 'html',
    },
    'sibling-242': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=242'
        ),
        'format': 'html',
    },
    'sibling-243': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=243'
        ),
        'format': 'html',
    },
    'sibling-244': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=244'
        ),
        'format': 'html',
    },
    'sibling-245': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=245'
        ),
        'format': 'html',
    },
    'sibling-246': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=246'
        ),
        'format': 'html',
    },
    'sibling-247': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=247'
        ),
        'format': 'html',
    },
    'sibling-248': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=248'
        ),
        'format': 'html',
    },
    'sibling-249': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=249'
        ),
        'format': 'html',
    },
    'sibling-250': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=250'
        ),
        'format': 'html',
    },
    'sibling-251': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=251'
        ),
        'format': 'html',
    },
    'sibling-252': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=252'
        ),
        'format': 'html',
    },
    'sibling-253': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=253'
        ),
        'format': 'html',
    },
    'sibling-254': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=254'
        ),
        'format': 'html',
    },
    'sibling-255': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=255'
        ),
        'format': 'html',
    },
    'sibling-256': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=256'
        ),
        'format': 'html',
    },
    'sibling-257': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=257'
        ),
        'format': 'html',
    },
    'sibling-258': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=258'
        ),
        'format': 'html',
    },
    'sibling-259': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=259'
        ),
        'format': 'html',
    },
    'sibling-260': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=260'
        ),
        'format': 'html',
    },
    'sibling-261': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=261'
        ),
        'format': 'html',
    },
    'sibling-263': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=263'
        ),
        'format': 'html',
    },
    'sibling-264': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=264'
        ),
        'format': 'html',
    },
    'sibling-265': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=265'
        ),
        'format': 'html',
    },
    'sibling-266': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=266'
        ),
        'format': 'html',
    },
    'sibling-267': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=267'
        ),
        'format': 'html',
    },
    'sibling-268': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=268'
        ),
        'format': 'html',
    },
    'sibling-269': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=269'
        ),
        'format': 'html',
    },
    'sibling-270': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=270'
        ),
        'format': 'html',
    },
    'sibling-271': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=271'
        ),
        'format': 'html',
    },
    'sibling-273': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=273'
        ),
        'format': 'html',
    },
    'sibling-274': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=274'
        ),
        'format': 'html',
    },
    'sibling-275': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=275'
        ),
        'format': 'html',
    },
    'sibling-276': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=276'
        ),
        'format': 'html',
    },
    'sibling-277': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=277'
        ),
        'format': 'html',
    },
    'sibling-278': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=278'
        ),
        'format': 'html',
    },
    'sibling-279': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=279'
        ),
        'format': 'html',
    },
    'sibling-28': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=28'
        ),
        'format': 'html',
    },
    'sibling-280': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=280'
        ),
        'format': 'html',
    },
    'sibling-281': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=281'
        ),
        'format': 'html',
    },
    'sibling-282': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=282'
        ),
        'format': 'html',
    },
    'sibling-283': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=283'
        ),
        'format': 'html',
    },
    'sibling-284': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=284'
        ),
        'format': 'html',
    },
    'sibling-285': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=285'
        ),
        'format': 'html',
    },
    'sibling-286': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=286'
        ),
        'format': 'html',
    },
    'sibling-287': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=287'
        ),
        'format': 'html',
    },
    'sibling-288': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=288'
        ),
        'format': 'html',
    },
    'sibling-289': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=289'
        ),
        'format': 'html',
    },
    'sibling-290': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=290'
        ),
        'format': 'html',
    },
    'sibling-291': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=291'
        ),
        'format': 'html',
    },
    'sibling-292': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=292'
        ),
        'format': 'html',
    },
    'sibling-293': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=293'
        ),
        'format': 'html',
    },
    'sibling-294': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=294'
        ),
        'format': 'html',
    },
    'sibling-295': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=295'
        ),
        'format': 'html',
    },
    'sibling-296': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=296'
        ),
        'format': 'html',
    },
    'sibling-297': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=297'
        ),
        'format': 'html',
    },
    'sibling-298': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=298'
        ),
        'format': 'html',
    },
    'sibling-299': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=299'
        ),
        'format': 'html',
    },
    'sibling-300': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=300'
        ),
        'format': 'html',
    },
    'sibling-301': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=301'
        ),
        'format': 'html',
    },
    'sibling-332': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=332'
        ),
        'format': 'html',
    },
    'sibling-87': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=87'
        ),
        'format': 'html',
    },
    'sibling-95': {
        'url': (
            'https://studujnavs.gov.cz/prehled-stipendii/detail-stipend'
            'ia?id=95'
        ),
        'format': 'html',
    },
    'call-1-468': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=1&callId=468'
        ),
        'format': 'html',
    },
    'call-2-226': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=2&callId=226'
        ),
        'format': 'html',
    },
    'call-3-467': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=3&callId=467'
        ),
        'format': 'html',
    },
    'call-4-469': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=4&callId=469'
        ),
        'format': 'html',
    },
    'call-5-470': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=5&callId=470'
        ),
        'format': 'html',
    },
    'call-6-471': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=6&callId=471'
        ),
        'format': 'html',
    },
    'call-7-452': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=7&callId=452'
        ),
        'format': 'html',
    },
    'call-8-453': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=8&callId=453'
        ),
        'format': 'html',
    },
    'call-9-454': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=9&callId=454'
        ),
        'format': 'html',
    },
    'call-10-474': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=10&callId=474'
        ),
        'format': 'html',
    },
    'call-11-475': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=11&callId=475'
        ),
        'format': 'html',
    },
    'call-12-476': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=12&callId=476'
        ),
        'format': 'html',
    },
    'call-13-477': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=13&callId=477'
        ),
        'format': 'html',
    },
    'call-14-14': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=14&callId=14'
        ),
        'format': 'html',
    },
    'call-15-15': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=15&callId=15'
        ),
        'format': 'html',
    },
    'call-16-16': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=16&callId=16'
        ),
        'format': 'html',
    },
    'call-17-17': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=17&callId=17'
        ),
        'format': 'html',
    },
    'call-18-478': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=18&callId=478'
        ),
        'format': 'html',
    },
    'call-19-182': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=19&callId=182'
        ),
        'format': 'html',
    },
    'call-20-480': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=20&callId=480'
        ),
        'format': 'html',
    },
    'call-21-472': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=21&callId=472'
        ),
        'format': 'html',
    },
    'call-22-473': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=22&callId=473'
        ),
        'format': 'html',
    },
    'call-23-445': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=23&callId=445'
        ),
        'format': 'html',
    },
    'call-24-447': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=24&callId=447'
        ),
        'format': 'html',
    },
    'call-25-446': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=25&callId=446'
        ),
        'format': 'html',
    },
    'call-26-431': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=26&callId=431'
        ),
        'format': 'html',
    },
    'call-27-413': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=27&callId=413'
        ),
        'format': 'html',
    },
    'call-28-28': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=28&callId=28'
        ),
        'format': 'html',
    },
    'call-29-481': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=29&callId=481'
        ),
        'format': 'html',
    },
    'call-30-482': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=30&callId=482'
        ),
        'format': 'html',
    },
    'call-31-513': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=31&callId=513'
        ),
        'format': 'html',
    },
    'call-32-483': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=32&callId=483'
        ),
        'format': 'html',
    },
    'call-33-484': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=33&callId=484'
        ),
        'format': 'html',
    },
    'call-34-485': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=34&callId=485'
        ),
        'format': 'html',
    },
    'call-35-486': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=35&callId=486'
        ),
        'format': 'html',
    },
    'call-40-487': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=40&callId=487'
        ),
        'format': 'html',
    },
    'call-41-488': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=41&callId=488'
        ),
        'format': 'html',
    },
    'call-42-489': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=42&callId=489'
        ),
        'format': 'html',
    },
    'call-43-490': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=43&callId=490'
        ),
        'format': 'html',
    },
    'call-45-463': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=45&callId=463'
        ),
        'format': 'html',
    },
    'call-46-46': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=46&callId=46'
        ),
        'format': 'html',
    },
    'call-47-434': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=47&callId=434'
        ),
        'format': 'html',
    },
    'call-48-187': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=48&callId=187'
        ),
        'format': 'html',
    },
    'call-49-462': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=49&callId=462'
        ),
        'format': 'html',
    },
    'call-50-461': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=50&callId=461'
        ),
        'format': 'html',
    },
    'call-51-190': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=51&callId=190'
        ),
        'format': 'html',
    },
    'call-52-191': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=52&callId=191'
        ),
        'format': 'html',
    },
    'call-53-450': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=53&callId=450'
        ),
        'format': 'html',
    },
    'call-54-193': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=54&callId=193'
        ),
        'format': 'html',
    },
    'call-55-194': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=55&callId=194'
        ),
        'format': 'html',
    },
    'call-56-451': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=56&callId=451'
        ),
        'format': 'html',
    },
    'call-57-196': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=57&callId=196'
        ),
        'format': 'html',
    },
    'call-59-449': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=59&callId=449'
        ),
        'format': 'html',
    },
    'call-60-411': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=60&callId=411'
        ),
        'format': 'html',
    },
    'call-61-420': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=61&callId=420'
        ),
        'format': 'html',
    },
    'call-62-491': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=62&callId=491'
        ),
        'format': 'html',
    },
    'call-63-240': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=63&callId=240'
        ),
        'format': 'html',
    },
    'call-64-492': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=64&callId=492'
        ),
        'format': 'html',
    },
    'call-65-493': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=65&callId=493'
        ),
        'format': 'html',
    },
    'call-66-502': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=66&callId=502'
        ),
        'format': 'html',
    },
    'call-67-500': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=67&callId=500'
        ),
        'format': 'html',
    },
    'call-68-501': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=68&callId=501'
        ),
        'format': 'html',
    },
    'call-69-227': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=69&callId=227'
        ),
        'format': 'html',
    },
    'call-70-503': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=70&callId=503'
        ),
        'format': 'html',
    },
    'call-71-504': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=71&callId=504'
        ),
        'format': 'html',
    },
    'call-72-505': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=72&callId=505'
        ),
        'format': 'html',
    },
    'call-73-506': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=73&callId=506'
        ),
        'format': 'html',
    },
    'call-74-507': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=74&callId=507'
        ),
        'format': 'html',
    },
    'call-75-496': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=75&callId=496'
        ),
        'format': 'html',
    },
    'call-76-497': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=76&callId=497'
        ),
        'format': 'html',
    },
    'call-77-494': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=77&callId=494'
        ),
        'format': 'html',
    },
    'call-78-512': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=78&callId=512'
        ),
        'format': 'html',
    },
    'call-79-495': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=79&callId=495'
        ),
        'format': 'html',
    },
    'call-80-508': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=80&callId=508'
        ),
        'format': 'html',
    },
    'call-81-509': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=81&callId=509'
        ),
        'format': 'html',
    },
    'call-82-510': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=82&callId=510'
        ),
        'format': 'html',
    },
    'call-83-511': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=83&callId=511'
        ),
        'format': 'html',
    },
    'call-84-459': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=84&callId=459'
        ),
        'format': 'html',
    },
    'call-85-238': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=85&callId=238'
        ),
        'format': 'html',
    },
    'call-86-239': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=86&callId=239'
        ),
        'format': 'html',
    },
    'call-87-515': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=87&callId=515'
        ),
        'format': 'html',
    },
    'call-88-88': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=88&callId=88'
        ),
        'format': 'html',
    },
    'call-89-516': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=89&callId=516'
        ),
        'format': 'html',
    },
    'call-90-90': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=90&callId=90'
        ),
        'format': 'html',
    },
    'call-91-517': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=91&callId=517'
        ),
        'format': 'html',
    },
    'call-92-92': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=92&callId=92'
        ),
        'format': 'html',
    },
    'call-93-93': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=93&callId=93'
        ),
        'format': 'html',
    },
    'call-94-94': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=94&callId=94'
        ),
        'format': 'html',
    },
    'call-95-95': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=95&callId=95'
        ),
        'format': 'html',
    },
    'call-96-96': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=96&callId=96'
        ),
        'format': 'html',
    },
    'call-97-97': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=97&callId=97'
        ),
        'format': 'html',
    },
    'call-98-245': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=98&callId=245'
        ),
        'format': 'html',
    },
    'call-99-246': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=99&callId=246'
        ),
        'format': 'html',
    },
    'call-100-247': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=100&callId=247'
        ),
        'format': 'html',
    },
    'call-101-248': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=101&callId=248'
        ),
        'format': 'html',
    },
    'call-102-404': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=102&callId=404'
        ),
        'format': 'html',
    },
    'call-103-405': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=103&callId=405'
        ),
        'format': 'html',
    },
    'call-104-406': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=104&callId=406'
        ),
        'format': 'html',
    },
    'call-105-416': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=105&callId=416'
        ),
        'format': 'html',
    },
    'call-106-417': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=106&callId=417'
        ),
        'format': 'html',
    },
    'call-107-439': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=107&callId=439'
        ),
        'format': 'html',
    },
    'call-108-428': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=108&callId=428'
        ),
        'format': 'html',
    },
    'call-109-109': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=109&callId=109'
        ),
        'format': 'html',
    },
    'call-111-111': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=111&callId=111'
        ),
        'format': 'html',
    },
    'call-114-414': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=114&callId=414'
        ),
        'format': 'html',
    },
    'call-115-436': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=115&callId=436'
        ),
        'format': 'html',
    },
    'call-116-432': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=116&callId=432'
        ),
        'format': 'html',
    },
    'call-117-433': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=117&callId=433'
        ),
        'format': 'html',
    },
    'call-118-118': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=118&callId=118'
        ),
        'format': 'html',
    },
    'call-120-409': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=120&callId=409'
        ),
        'format': 'html',
    },
    'call-122-122': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=122&callId=122'
        ),
        'format': 'html',
    },
    'call-124-124': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=124&callId=124'
        ),
        'format': 'html',
    },
    'call-130-130': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=130&callId=130'
        ),
        'format': 'html',
    },
    'call-131-430': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=131&callId=430'
        ),
        'format': 'html',
    },
    'call-134-427': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=134&callId=427'
        ),
        'format': 'html',
    },
    'call-136-437': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=136&callId=437'
        ),
        'format': 'html',
    },
    'call-137-435': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=137&callId=435'
        ),
        'format': 'html',
    },
    'call-138-410': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=138&callId=410'
        ),
        'format': 'html',
    },
    'call-139-139': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=139&callId=139'
        ),
        'format': 'html',
    },
    'call-142-142': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=142&callId=142'
        ),
        'format': 'html',
    },
    'call-144-425': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=144&callId=425'
        ),
        'format': 'html',
    },
    'call-145-426': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=145&callId=426'
        ),
        'format': 'html',
    },
    'call-146-407': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=146&callId=407'
        ),
        'format': 'html',
    },
    'call-148-444': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=148&callId=444'
        ),
        'format': 'html',
    },
    'call-149-443': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=149&callId=443'
        ),
        'format': 'html',
    },
    'call-150-429': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=150&callId=429'
        ),
        'format': 'html',
    },
    'call-151-421': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=151&callId=421'
        ),
        'format': 'html',
    },
    'call-152-422': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=152&callId=422'
        ),
        'format': 'html',
    },
    'call-153-442': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=153&callId=442'
        ),
        'format': 'html',
    },
    'call-154-460': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=154&callId=460'
        ),
        'format': 'html',
    },
    'call-155-408': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=155&callId=408'
        ),
        'format': 'html',
    },
    'call-157-412': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=157&callId=412'
        ),
        'format': 'html',
    },
    'call-159-159': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=159&callId=159'
        ),
        'format': 'html',
    },
    'call-160-160': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=160&callId=160'
        ),
        'format': 'html',
    },
    'call-163-163': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=163&callId=163'
        ),
        'format': 'html',
    },
    'call-164-243': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=164&callId=243'
        ),
        'format': 'html',
    },
    'call-165-438': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=165&callId=438'
        ),
        'format': 'html',
    },
    'call-166-423': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=166&callId=423'
        ),
        'format': 'html',
    },
    'call-167-167': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=167&callId=167'
        ),
        'format': 'html',
    },
    'call-168-169': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=168&callId=169'
        ),
        'format': 'html',
    },
    'call-169-168': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=169&callId=168'
        ),
        'format': 'html',
    },
    'call-170-170': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=170&callId=170'
        ),
        'format': 'html',
    },
    'call-171-498': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=171&callId=498'
        ),
        'format': 'html',
    },
    'call-172-514': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=172&callId=514'
        ),
        'format': 'html',
    },
    'call-173-173': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=173&callId=173'
        ),
        'format': 'html',
    },
    'call-174-440': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=174&callId=440'
        ),
        'format': 'html',
    },
    'call-175-175': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=175&callId=175'
        ),
        'format': 'html',
    },
    'call-176-466': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=176&callId=466'
        ),
        'format': 'html',
    },
    'call-177-499': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=177&callId=499'
        ),
        'format': 'html',
    },
    'call-178-250': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=178&callId=250'
        ),
        'format': 'html',
    },
    'call-179-251': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=179&callId=251'
        ),
        'format': 'html',
    },
    'call-180-252': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=180&callId=252'
        ),
        'format': 'html',
    },
    'call-181-257': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=181&callId=257'
        ),
        'format': 'html',
    },
    'call-182-254': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=182&callId=254'
        ),
        'format': 'html',
    },
    'call-183-255': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=183&callId=255'
        ),
        'format': 'html',
    },
    'call-184-258': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=184&callId=258'
        ),
        'format': 'html',
    },
    'call-185-256': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=185&callId=256'
        ),
        'format': 'html',
    },
    'call-186-259': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=186&callId=259'
        ),
        'format': 'html',
    },
    'call-187-260': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=187&callId=260'
        ),
        'format': 'html',
    },
    'call-188-261': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=188&callId=261'
        ),
        'format': 'html',
    },
    'call-189-262': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=189&callId=262'
        ),
        'format': 'html',
    },
    'call-190-263': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=190&callId=263'
        ),
        'format': 'html',
    },
    'call-191-264': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=191&callId=264'
        ),
        'format': 'html',
    },
    'call-192-267': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=192&callId=267'
        ),
        'format': 'html',
    },
    'call-193-265': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=193&callId=265'
        ),
        'format': 'html',
    },
    'call-194-266': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=194&callId=266'
        ),
        'format': 'html',
    },
    'call-195-268': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=195&callId=268'
        ),
        'format': 'html',
    },
    'call-196-269': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=196&callId=269'
        ),
        'format': 'html',
    },
    'call-197-270': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=197&callId=270'
        ),
        'format': 'html',
    },
    'call-198-271': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=198&callId=271'
        ),
        'format': 'html',
    },
    'call-199-272': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=199&callId=272'
        ),
        'format': 'html',
    },
    'call-200-273': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=200&callId=273'
        ),
        'format': 'html',
    },
    'call-201-275': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=201&callId=275'
        ),
        'format': 'html',
    },
    'call-202-274': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=202&callId=274'
        ),
        'format': 'html',
    },
    'call-203-276': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=203&callId=276'
        ),
        'format': 'html',
    },
    'call-204-277': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=204&callId=277'
        ),
        'format': 'html',
    },
    'call-205-278': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=205&callId=278'
        ),
        'format': 'html',
    },
    'call-206-279': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=206&callId=279'
        ),
        'format': 'html',
    },
    'call-207-280': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=207&callId=280'
        ),
        'format': 'html',
    },
    'call-209-281': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=209&callId=281'
        ),
        'format': 'html',
    },
    'call-210-283': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=210&callId=283'
        ),
        'format': 'html',
    },
    'call-211-282': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=211&callId=282'
        ),
        'format': 'html',
    },
    'call-212-284': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=212&callId=284'
        ),
        'format': 'html',
    },
    'call-213-285': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=213&callId=285'
        ),
        'format': 'html',
    },
    'call-214-286': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=214&callId=286'
        ),
        'format': 'html',
    },
    'call-215-287': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=215&callId=287'
        ),
        'format': 'html',
    },
    'call-216-288': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=216&callId=288'
        ),
        'format': 'html',
    },
    'call-217-289': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=217&callId=289'
        ),
        'format': 'html',
    },
    'call-218-290': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=218&callId=290'
        ),
        'format': 'html',
    },
    'call-219-291': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=219&callId=291'
        ),
        'format': 'html',
    },
    'call-220-292': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=220&callId=292'
        ),
        'format': 'html',
    },
    'call-221-293': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=221&callId=293'
        ),
        'format': 'html',
    },
    'call-222-294': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=222&callId=294'
        ),
        'format': 'html',
    },
    'call-223-295': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=223&callId=295'
        ),
        'format': 'html',
    },
    'call-224-296': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=224&callId=296'
        ),
        'format': 'html',
    },
    'call-225-297': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=225&callId=297'
        ),
        'format': 'html',
    },
    'call-226-298': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=226&callId=298'
        ),
        'format': 'html',
    },
    'call-227-299': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=227&callId=299'
        ),
        'format': 'html',
    },
    'call-228-300': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=228&callId=300'
        ),
        'format': 'html',
    },
    'call-229-301': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=229&callId=301'
        ),
        'format': 'html',
    },
    'call-230-302': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=230&callId=302'
        ),
        'format': 'html',
    },
    'call-231-303': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=231&callId=303'
        ),
        'format': 'html',
    },
    'call-232-304': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=232&callId=304'
        ),
        'format': 'html',
    },
    'call-233-305': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=233&callId=305'
        ),
        'format': 'html',
    },
    'call-234-306': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=234&callId=306'
        ),
        'format': 'html',
    },
    'call-235-307': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=235&callId=307'
        ),
        'format': 'html',
    },
    'call-236-308': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=236&callId=308'
        ),
        'format': 'html',
    },
    'call-237-309': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=237&callId=309'
        ),
        'format': 'html',
    },
    'call-238-310': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=238&callId=310'
        ),
        'format': 'html',
    },
    'call-239-311': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=239&callId=311'
        ),
        'format': 'html',
    },
    'call-240-312': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=240&callId=312'
        ),
        'format': 'html',
    },
    'call-241-313': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=241&callId=313'
        ),
        'format': 'html',
    },
    'call-242-314': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=242&callId=314'
        ),
        'format': 'html',
    },
    'call-243-315': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=243&callId=315'
        ),
        'format': 'html',
    },
    'call-244-316': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=244&callId=316'
        ),
        'format': 'html',
    },
    'call-245-317': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=245&callId=317'
        ),
        'format': 'html',
    },
    'call-246-318': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=246&callId=318'
        ),
        'format': 'html',
    },
    'call-247-319': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=247&callId=319'
        ),
        'format': 'html',
    },
    'call-248-320': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=248&callId=320'
        ),
        'format': 'html',
    },
    'call-249-321': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=249&callId=321'
        ),
        'format': 'html',
    },
    'call-250-322': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=250&callId=322'
        ),
        'format': 'html',
    },
    'call-251-323': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=251&callId=323'
        ),
        'format': 'html',
    },
    'call-252-324': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=252&callId=324'
        ),
        'format': 'html',
    },
    'call-253-325': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=253&callId=325'
        ),
        'format': 'html',
    },
    'call-254-326': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=254&callId=326'
        ),
        'format': 'html',
    },
    'call-255-327': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=255&callId=327'
        ),
        'format': 'html',
    },
    'call-256-328': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=256&callId=328'
        ),
        'format': 'html',
    },
    'call-257-329': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=257&callId=329'
        ),
        'format': 'html',
    },
    'call-258-330': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=258&callId=330'
        ),
        'format': 'html',
    },
    'call-259-331': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=259&callId=331'
        ),
        'format': 'html',
    },
    'call-260-332': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=260&callId=332'
        ),
        'format': 'html',
    },
    'call-261-333': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=261&callId=333'
        ),
        'format': 'html',
    },
    'call-263-335': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=263&callId=335'
        ),
        'format': 'html',
    },
    'call-264-336': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=264&callId=336'
        ),
        'format': 'html',
    },
    'call-265-337': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=265&callId=337'
        ),
        'format': 'html',
    },
    'call-266-338': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=266&callId=338'
        ),
        'format': 'html',
    },
    'call-267-339': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=267&callId=339'
        ),
        'format': 'html',
    },
    'call-268-340': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=268&callId=340'
        ),
        'format': 'html',
    },
    'call-269-341': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=269&callId=341'
        ),
        'format': 'html',
    },
    'call-270-342': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=270&callId=342'
        ),
        'format': 'html',
    },
    'call-271-343': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=271&callId=343'
        ),
        'format': 'html',
    },
    'call-273-345': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=273&callId=345'
        ),
        'format': 'html',
    },
    'call-274-346': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=274&callId=346'
        ),
        'format': 'html',
    },
    'call-275-347': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=275&callId=347'
        ),
        'format': 'html',
    },
    'call-276-348': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=276&callId=348'
        ),
        'format': 'html',
    },
    'call-277-349': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=277&callId=349'
        ),
        'format': 'html',
    },
    'call-278-350': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=278&callId=350'
        ),
        'format': 'html',
    },
    'call-279-351': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=279&callId=351'
        ),
        'format': 'html',
    },
    'call-280-352': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=280&callId=352'
        ),
        'format': 'html',
    },
    'call-281-353': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=281&callId=353'
        ),
        'format': 'html',
    },
    'call-282-354': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=282&callId=354'
        ),
        'format': 'html',
    },
    'call-283-355': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=283&callId=355'
        ),
        'format': 'html',
    },
    'call-284-356': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=284&callId=356'
        ),
        'format': 'html',
    },
    'call-285-357': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=285&callId=357'
        ),
        'format': 'html',
    },
    'call-286-358': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=286&callId=358'
        ),
        'format': 'html',
    },
    'call-287-359': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=287&callId=359'
        ),
        'format': 'html',
    },
    'call-288-360': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=288&callId=360'
        ),
        'format': 'html',
    },
    'call-289-361': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=289&callId=361'
        ),
        'format': 'html',
    },
    'call-290-362': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=290&callId=362'
        ),
        'format': 'html',
    },
    'call-291-363': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=291&callId=363'
        ),
        'format': 'html',
    },
    'call-292-364': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=292&callId=364'
        ),
        'format': 'html',
    },
    'call-293-365': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=293&callId=365'
        ),
        'format': 'html',
    },
    'call-294-366': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=294&callId=366'
        ),
        'format': 'html',
    },
    'call-295-367': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=295&callId=367'
        ),
        'format': 'html',
    },
    'call-296-368': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=296&callId=368'
        ),
        'format': 'html',
    },
    'call-297-369': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=297&callId=369'
        ),
        'format': 'html',
    },
    'call-298-370': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=298&callId=370'
        ),
        'format': 'html',
    },
    'call-299-371': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=299&callId=371'
        ),
        'format': 'html',
    },
    'call-300-372': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=300&callId=372'
        ),
        'format': 'html',
    },
    'call-301-373': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=301&callId=373'
        ),
        'format': 'html',
    },
    'call-333-415': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=333&callId=415'
        ),
        'format': 'html',
    },
    'call-334-418': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=334&callId=418'
        ),
        'format': 'html',
    },
    'call-335-419': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=335&callId=419'
        ),
        'format': 'html',
    },
    'call-337-129': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=337&callId=129'
        ),
        'format': 'html',
    },
    'call-339-455': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=339&callId=455'
        ),
        'format': 'html',
    },
    'call-340-456': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=340&callId=456'
        ),
        'format': 'html',
    },
    'call-341-457': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=341&callId=457'
        ),
        'format': 'html',
    },
    'call-342-458': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=342&callId=458'
        ),
        'format': 'html',
    },
    'call-343-464': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=343&callId=464'
        ),
        'format': 'html',
    },
    'call-344-465': {
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            'call-detail/?id=344&callId=465'
        ),
        'format': 'html',
    },
    'policy-0': {
        'url': 'https://studyin.gov.cz/',
        'format': 'html',
    },
    'policy-1': {
        'url': 'https://studyin.gov.cz/cookies/',
        'format': 'html',
    },
    'policy-2': {
        'url': (
            'https://studyin.gov.cz/plan-your-studies/scholarships-and-'
            'finances/'
        ),
        'format': 'html',
    },
    'policy-3': {
        'url': 'https://www.dzs.cz/en/processing-personal-data',
        'format': 'html',
    },
    'policy-4': {
        'url': 'https://studujnavs.gov.cz/cookies',
        'format': 'html',
    },
    'policy-5': {
        'url': 'https://www.dzs.cz/zpracovani-osobnich-udaju',
        'format': 'html',
    },
    'policy-6': {
        'url': 'https://msmt.gov.cz/',
        'format': 'html',
    },
    'policy-7': {
        'url': 'https://msmt.gov.cz/ministerstvo/o-webu',
        'format': 'html',
    },
    'policy-8': {
        'url': (
            'https://msmt.gov.cz/ministerstvo/o-webu/zasady-pouzivani-s'
            'ouboru-cookies'
        ),
        'format': 'html',
    },
    'policy-9': {
        'url': 'https://www.btha.cz/de/',
        'format': 'html',
    },
    'policy-10': {
        'url': 'https://www.btha.cz/de/impressum',
        'format': 'html',
    },
    'policy-11': {
        'url': 'https://www.btha.cz/de/datenschutzerklaerung',
        'format': 'html',
    },
    'policy-12': {
        'url': 'https://www.btha.cz/de/haftungsausschluss',
        'format': 'html',
    },
    'catalogue-incoming-1': {
        'url': 'https://studyin.gov.cz/scholarships/?outgoing=false',
        'format': 'html',
        'catalogue': True,
        'scope': 'incoming',
        'page': 1,
        'total': 60,
    },
    'catalogue-incoming-2': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=false&compatriots=false&pa'
            'ge=2'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'incoming',
        'page': 2,
        'total': 60,
    },
    'catalogue-incoming-3': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=false&compatriots=false&pa'
            'ge=3'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'incoming',
        'page': 3,
        'total': 60,
    },
    'catalogue-incoming-4': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=false&compatriots=false&pa'
            'ge=4'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'incoming',
        'page': 4,
        'total': 60,
    },
    'catalogue-incoming-5': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=false&compatriots=false&pa'
            'ge=5'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'incoming',
        'page': 5,
        'total': 60,
    },
    'catalogue-incoming-6': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=false&compatriots=false&pa'
            'ge=6'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'incoming',
        'page': 6,
        'total': 60,
    },
    'catalogue-outgoing-1': {
        'url': 'https://studyin.gov.cz/scholarships/?outgoing=true',
        'format': 'html',
        'catalogue': True,
        'scope': 'outgoing',
        'page': 1,
        'total': 222,
    },
    'catalogue-outgoing-2': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=true&compatriots=false&pag'
            'e=2'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'outgoing',
        'page': 2,
        'total': 222,
    },
    'catalogue-outgoing-3': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=true&compatriots=false&pag'
            'e=3'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'outgoing',
        'page': 3,
        'total': 222,
    },
    'catalogue-outgoing-4': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=true&compatriots=false&pag'
            'e=4'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'outgoing',
        'page': 4,
        'total': 222,
    },
    'catalogue-outgoing-5': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=true&compatriots=false&pag'
            'e=5'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'outgoing',
        'page': 5,
        'total': 222,
    },
    'catalogue-outgoing-6': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=true&compatriots=false&pag'
            'e=6'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'outgoing',
        'page': 6,
        'total': 222,
    },
    'catalogue-outgoing-7': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=true&compatriots=false&pag'
            'e=7'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'outgoing',
        'page': 7,
        'total': 222,
    },
    'catalogue-outgoing-8': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=true&compatriots=false&pag'
            'e=8'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'outgoing',
        'page': 8,
        'total': 222,
    },
    'catalogue-outgoing-9': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=true&compatriots=false&pag'
            'e=9'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'outgoing',
        'page': 9,
        'total': 222,
    },
    'catalogue-outgoing-10': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=true&compatriots=false&pag'
            'e=10'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'outgoing',
        'page': 10,
        'total': 222,
    },
    'catalogue-outgoing-11': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=true&compatriots=false&pag'
            'e=11'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'outgoing',
        'page': 11,
        'total': 222,
    },
    'catalogue-outgoing-12': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=true&compatriots=false&pag'
            'e=12'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'outgoing',
        'page': 12,
        'total': 222,
    },
    'catalogue-outgoing-13': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=true&compatriots=false&pag'
            'e=13'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'outgoing',
        'page': 13,
        'total': 222,
    },
    'catalogue-outgoing-14': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=true&compatriots=false&pag'
            'e=14'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'outgoing',
        'page': 14,
        'total': 222,
    },
    'catalogue-outgoing-15': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=true&compatriots=false&pag'
            'e=15'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'outgoing',
        'page': 15,
        'total': 222,
    },
    'catalogue-outgoing-16': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=true&compatriots=false&pag'
            'e=16'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'outgoing',
        'page': 16,
        'total': 222,
    },
    'catalogue-outgoing-17': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=true&compatriots=false&pag'
            'e=17'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'outgoing',
        'page': 17,
        'total': 222,
    },
    'catalogue-outgoing-18': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=true&compatriots=false&pag'
            'e=18'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'outgoing',
        'page': 18,
        'total': 222,
    },
    'catalogue-outgoing-19': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=true&compatriots=false&pag'
            'e=19'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'outgoing',
        'page': 19,
        'total': 222,
    },
    'catalogue-outgoing-20': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=true&compatriots=false&pag'
            'e=20'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'outgoing',
        'page': 20,
        'total': 222,
    },
    'catalogue-outgoing-21': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=true&compatriots=false&pag'
            'e=21'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'outgoing',
        'page': 21,
        'total': 222,
    },
    'catalogue-outgoing-22': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=true&compatriots=false&pag'
            'e=22'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'outgoing',
        'page': 22,
        'total': 222,
    },
    'catalogue-outgoing-23': {
        'url': (
            'https://studyin.gov.cz/ajax/scholarshipsearch/switchpage?p'
            'ageSize=10&language=en&search=&isCallOpen=&isCallFuture=&i'
            'sScholarshipCovered=&isOutgoing=true&compatriots=false&pag'
            'e=23'
        ),
        'format': 'json',
        'catalogue': True,
        'scope': 'outgoing',
        'page': 23,
        'total': 222,
    },
    'catalogue-compatriots-1': {
        'url': 'https://studyin.gov.cz/scholarships/?compatriots=true',
        'format': 'html',
        'catalogue': True,
        'scope': 'compatriots',
        'page': 1,
        'total': 0,
    },
    'document-d1cc0983a316': {
        'url': (
            'https://msmt.gov.cz/media/wp-content/uploads/2026/08/Selec'
            'ted_areas_of_study_for_programmes_offered_in_Czech_2026-1.'
            'xlsx'
        ),
        'format': 'xlsx',
        'sha256': (
            '823dd6d23f8d265ab61cd87d965f52300e0b967f49375dacaea4fd95b5'
            '2625e1'
        ),
    },
    'document-02273471f86e': {
        'url': (
            'https://msmt.gov.cz/media/wp-content/uploads/2026/08/Guide'
            'lines_2026-1.pdf'
        ),
        'format': 'pdf',
        'sha256': (
            '287ddc83668a3f8f6b8e47be9cb19fad641a05e69cbb58fc76d687a210'
            '8d2fc3'
        ),
    },
    'document-6b56bd1c25a8': {
        'url': (
            'https://msmt.gov.cz/media/wp-content/uploads/2026/08/FAQ_2'
            '026.pdf'
        ),
        'format': 'pdf',
        'sha256': (
            '7b1eadb1c07f784e40a169ca8035ccc86ce48030ecaec599980a1b52bf'
            '99c71e'
        ),
    },
    'document-c1c59a621ca2': {
        'url': (
            'https://msmt.gov.cz/media/wp-content/uploads/2026/08/Selec'
            'ted_Master_s_programmes_offered_in_English_2026-1-1.xlsx'
        ),
        'format': 'xlsx',
        'sha256': (
            '8c3f6f9e4b96d4c09148dd9bde515ed9cdc23d40cfdff6adfa9e75f767'
            '41f099'
        ),
    },
    'document-c90ee4d8f41f': {
        'url': (
            'https://msmt.gov.cz/media/wp-content/uploads/2026/08/Selec'
            'ted_Doctoral_programmes_offered_in_English_2026-1-1.xlsx'
        ),
        'format': 'xlsx',
        'sha256': (
            '2f341a1410faa2184d8f669c107369af3f66f597dd07894aa16b527392'
            'ef8139'
        ),
    },
    'document-dddbf0187368': {
        'url': (
            'https://www.dzs.cz/sites/default/files/2025-06/Podm%C3%ADn'
            'ky%20pro%C2%A0ud%C4%9Blov%C3%A1n%C3%AD%20stipendi%C3%AD%20'
            'Ministerstva%20%C5%A1kolstv%C3%AD%20a%20vzd%C4%9Bl%C3%A1v%'
            'C3%A1n%C3%AD%20Vietnamsk%C3%A9%20socialistick%C3%A9%20repu'
            'bliky%20z%C2%A026.3.2025.pdf'
        ),
        'format': 'pdf',
        'sha256': (
            '86967f5b7e0f90b6a527868a5533387823358d999cc3405476f0bc1485'
            '13d058'
        ),
    },
    'document-2bfa4b35bc26': {
        'url': (
            'https://www.dzs.cz/sites/default/files/2025-10/CT-BFP%2020'
            '26%20-%20Guide%20for%20applicants.pdf'
        ),
        'format': 'pdf',
        'sha256': (
            'ef0fcea51ea6f9d9785001a162b2fb7fac3eb6dc3fd880337e6b7b6ce2'
            'dd9864'
        ),
    },
    'document-a822bc320810': {
        'url': (
            'https://www.dzs.cz/sites/default/files/2025-10/ST-BFP%2020'
            '26%20-%20Guide%20for%20applicants.pdf'
        ),
        'format': 'pdf',
        'sha256': (
            'ffcadb2d14f77eea0993b31e8f5b293b21c32fcb7cec23fc5874ed57ff'
            'f039e3'
        ),
    },
    'document-59d56122d1c1': {
        'url': (
            'https://www.dzs.cz/sites/default/files/2025-02/2025%20GKS-'
            'G%20Application%20Guidelines%20%28English%29.pdf'
        ),
        'format': 'pdf',
        'sha256': (
            '0c2c8485213cb3edf5db70107205af80cc439bcec3c6c02bb84977969d'
            'fe7a39'
        ),
    },
    'document-20f2b7744c73': {
        'url': (
            'https://www.dzs.cz/sites/default/files/2024-03/%EC%B2%A8%E'
            'B%B6%80%201.%202024%20Korean%20Goverment%20Scholarship%20G'
            'uidelines%200318.pdf'
        ),
        'format': 'pdf',
        'sha256': (
            '8646aba0a618c88d185159f77ce110cade5ea782314d4cad6320208389'
            '8c68b3'
        ),
    },
    'document-4806bff37922': {
        'url': (
            'http://static.daad.de/media/daad_de/word-excel-nicht-barri'
            'erefrei/in-deutschland-studieren-forschen-lehren/daad_hsk_'
            'kursanbieterliste.xlsx'
        ),
        'format': 'xlsx',
        'sha256': (
            '2c0ea5ddcc343dbfdb43b3cc1723be4c8743747723be474dd2105af06a'
            '327b34'
        ),
    },
    'document-3a2289b7ba89': {
        'url': (
            'https://static.daad.de/media/daad_de/pdfs_nicht_barrierefr'
            'ei/im-ausland-studieren-forschen-lehren/daad_reisekostenzu'
            'schuesse_stipendiaten.pdf'
        ),
        'format': 'pdf',
        'sha256': (
            'defdba5a17dea2c4e34da549718cf4e302575c80d3a3c7cdc56c5ad047'
            '2475f1'
        ),
    },
    'document-080f5ea25650': {
        'url': (
            'https://www.btha.cz/images/BAYHOST-BTHA%20JStip%20Ausschre'
            'ibung%20EA%202027-28%20DE.pdf'
        ),
        'format': 'pdf',
        'sha256': (
            '64cc7fabbee72d38048f363d986278a9b9d852b3b70032f77529527666'
            '24aa91'
        ),
    },
}

CONDITIONS = {
    'programme-1': (
        'f9ab0394b2d392da56b78669b2852045845177aa2cf5b33ec0f9ae9383546c'
        '49'
    ),
    'programme-10': (
        '2a7741f3ae19776fa7808cd833986813d74d7edae51fdc88e24a58de3bf963'
        'bb'
    ),
    'programme-100': (
        'bc6b2a1094f947db30fd12f0692ee24f5bacb0569246006bbdcf865d5ce94c'
        'ce'
    ),
    'programme-101': (
        'f9aaf5fd465496dcb96aa65d174bc10602b1a6380ebae7731552d1d1918a9f'
        '66'
    ),
    'programme-102': (
        'feb4b9005b4244b5d1df049da0295d81f62ccf33b74beea38b06dfad714a24'
        '89'
    ),
    'programme-103': (
        '75ef2bd4490688d82ca0764b77359235c85338bad9be67de16ce587f107165'
        '3b'
    ),
    'programme-104': (
        '5b608257ea4443b50211787c6eeebdac49db8dcfd6dcfc34fc1ed17517fda7'
        '41'
    ),
    'programme-105': (
        '31418a24d50fd0a0391f1f0038bc563c88386d54265c88cf6d430d58e94e15'
        'ad'
    ),
    'programme-106': (
        '1ab1ea9e51caf84601a6457a9636e8e8f040de9fe1d46c18220599fa5f93a1'
        'c4'
    ),
    'programme-107': (
        'eb633089e74e5a4229534f8a1201b7244613018f23d40ff142ca07d5a77581'
        'a0'
    ),
    'programme-108': (
        '6dbd8b9baa3d690905ba6308dbfd01767d82dcd39925f0c047c24f4e91e146'
        '6f'
    ),
    'programme-109': (
        '6d604ae15571e2e4a427abe4b0526ee2ce79c1a3ee5af8d1aaeb80e5fd0c49'
        '4f'
    ),
    'programme-11': (
        '044a254c5f0392954187d738d95b2f6e15af43df2f0ee61d20755d80075166'
        '66'
    ),
    'programme-111': (
        'fdb3b4bf45a1f85130b7a496b3e3aa518b96ca4e1e28fb4b8c4e6681c19ae4'
        'c4'
    ),
    'programme-114': (
        'a8f9c04c46124e0c8590458c940569457b266a09b07dc27b5a43be633cd572'
        'bf'
    ),
    'programme-115': (
        'b80efb931eafedae74ddbcb8a2adcdacc45f3982bf2448b91899058d2ab114'
        '75'
    ),
    'programme-116': (
        '96b20c299e75e483943d9e4ec1314fee1ccea1af8d1e8b97fae6e01aac2967'
        '04'
    ),
    'programme-117': (
        'c2a41855bb33c5e852e5c87693b4d973a9d2ce5b06db0325b6123ff6021188'
        '46'
    ),
    'programme-118': (
        '141e5d7cc658eeafb8bc181922d8c8acee2c3527dfde6f402bfc61abdd6c52'
        'cc'
    ),
    'programme-12': (
        '0f4b034efa47e81cc0819990a5104c94c6ca9fd96cebc49ea0917807d19eae'
        '8f'
    ),
    'programme-120': (
        '03a62c017622e1e0331e106a8a14b08b03ad1ea75fbabe189242c286205353'
        'e2'
    ),
    'programme-122': (
        '067c653678f202074aba2af74ff7411594131146a294cbc970789ed666924b'
        '7b'
    ),
    'programme-124': (
        'b8e2886ba35eb2b506614a30bb20d3db0915105b9e53c41f438a680926563b'
        'b5'
    ),
    'programme-13': (
        'a1d69ad698046a34398f14dd463f4af4f8af3a4abef4bae83f2bd67a09823a'
        '05'
    ),
    'programme-130': (
        '7e45b1aecb0c1ce6f5ee8fe4064d1b2af5db4d90b8df124d43bc82f24a95cd'
        '69'
    ),
    'programme-131': (
        'c8fc98c7ab6071ede87ff22fce5ede2988ec72791e58e831bf1d01085eff84'
        'e2'
    ),
    'programme-134': (
        'c1a19d45b1a581f6130b91eac58bc7116343d79ae8e6d6d3427b7152d4b381'
        'd0'
    ),
    'programme-136': (
        '9a40a3d40cd274b72727ecf3c6cc48999bec7e5fbb0583632a32f5e621d227'
        'f5'
    ),
    'programme-137': (
        '957a0bc920dfa4df0e3f8525a234c4f8a25231f449dc46413c3e78f003aefc'
        'fe'
    ),
    'programme-138': (
        '7d51cdbf209e32feec8f65fa38c45b9ff836bc0925e15c51422602501f914e'
        '59'
    ),
    'programme-139': (
        'f5a3e305cfd85482293aa89b436f7c356df0e9f6298b604af95f090fdec6f0'
        'b6'
    ),
    'programme-14': (
        '8d0cad7eaa80ec026192d705befe953c04f8992b1ff659e0ffa0f76f4874bb'
        '5a'
    ),
    'programme-142': (
        'f08a73ea3036c53777b34cf6b6bb1b8d9c77b5fdb1d6f02f7d82138addac01'
        'fd'
    ),
    'programme-144': (
        '9c015bc61a2607ddc6307b41c491fa65728f8dec45553a94971da8a60f857d'
        '90'
    ),
    'programme-145': (
        '834c70879541ae69405f3063f137b7c55615ddb118bd0d4841e501e9241479'
        '84'
    ),
    'programme-146': (
        '772da49253e36e57bd2939fb6808cc806b4bff67948d5d92451de3c35626e0'
        '06'
    ),
    'programme-148': (
        '2b81e97655d95a5eed3b1dbfe60e6b234236fab69ebb8ed2e0e30654f1980b'
        '5d'
    ),
    'programme-149': (
        '142f70c09e690bcc1e275020656492ddda076f21ee5859472b06b862ca1c2f'
        '7d'
    ),
    'programme-15': (
        'b87ae01a1db5056d47d0a934b2d53babd59eecf95438ff4c56d0053e034135'
        'f3'
    ),
    'programme-150': (
        '29d0a230c7c3141cf57cd0c729420f10439dafc8d17f9124fa91d59bb8dca7'
        '4f'
    ),
    'programme-151': (
        '3f61837c72f26f581fee380617cff746163eb7ee86794224c39e1a9a34e4da'
        '9d'
    ),
    'programme-152': (
        '99605a3246f6940c423ebc98e453df31db8d4b0caa7dbe4895fb5344aa7f33'
        'd6'
    ),
    'programme-153': (
        'fe7cdfbe3372654963b749c19288dd75687dfb9f20552d458baf8942d71a97'
        '6f'
    ),
    'programme-154': (
        '69665cc8957419cf56d0b6e72eac22f42bc1950bde7b78bf0e3861f8471c77'
        '88'
    ),
    'programme-155': (
        '9a3791935cdb85d7392ca1ab8e7e0d09757315323d50ee698437ee726549fd'
        '5d'
    ),
    'programme-157': (
        'b8d5348231f96aaf6def92a4fbdfe7a633ea580341332cfc262d20e2650e87'
        '93'
    ),
    'programme-159': (
        '895bb2a97275725e9dfb542a863b080f205f4883c639f37b85d9547d2ea860'
        'be'
    ),
    'programme-16': (
        'eecb21721b1da454eecf27738389f1866b6f50f295e4b7d3dcee27e22b4c20'
        '3e'
    ),
    'programme-160': (
        '3c9d99fad21a01fa48cfd7971f8471383520f5c59b2b793d89372d328b02a8'
        'cd'
    ),
    'programme-163': (
        'b935c66629e1e41906fee3cc3abe94b589f24cbe8f57318075de317112abef'
        '37'
    ),
    'programme-164': (
        '1a186c2bbec49700adb2e6dc6dde5791c30d7a78ccc854a568829b11a72a4c'
        'c9'
    ),
    'programme-165': (
        '9e1bde2cdfcc935f308555d58be98875a3f01079aa437305bdbcf8cd03b302'
        '05'
    ),
    'programme-166': (
        '6323126c234bb04084e83fe01d52f3a0da579f94a701b54aa030887997ecaf'
        '24'
    ),
    'programme-167': (
        'ddc1fb3dddbd74fdd18664fd1e776391271e12c9e47bf811ff7faa4665f33b'
        '55'
    ),
    'programme-168': (
        'b40c1da068a2132848b77100fb62bb8e8c2115f01d3a3f9c799b4ce0bd101c'
        'b3'
    ),
    'programme-169': (
        'f7b246af5fda95334a7619bbe642688674ca3f320f176bea252b1474e4f311'
        'c3'
    ),
    'programme-17': (
        '21ef077118aa782f18e5269215f230a1bfd4fcf62bf0547d88a1f23f0ba25d'
        '7f'
    ),
    'programme-170': (
        'cb81b38f3ee1950da9b4473a8c119065747c51f93dc4f00f281aa019147014'
        'c6'
    ),
    'programme-171': (
        '2ac4e6bacc558df52d35209098e584519d42095d113ae665c42cdefdc026c1'
        '24'
    ),
    'programme-172': (
        '785eb085cfa9d18260596c1c127b7cf977c91f6df8b0ce7fc59c5a0adc6acd'
        'c1'
    ),
    'programme-173': (
        'dd64515bbff4d499b156ffee3e5f2dc794c7ecceda2941930141501b69b6fa'
        '6c'
    ),
    'programme-174': (
        'c8cdc1c74682c450083298d59b19178578554d9fa0cf3ee95ba0ece0a0c5d6'
        '3f'
    ),
    'programme-175': (
        'b161399a5b956ba47ba8e41e69a9bf26c6582b370bd8b118b8f8f1002b25af'
        '3e'
    ),
    'programme-176': (
        'dd65ce11be887f25c706bd14ca15a548ceee7861f2e2481956ac8fefec0b4b'
        'df'
    ),
    'programme-177': (
        '0645ff6adc8bdfe3943bd0332482a6cab1a889428f67f887c3e7ed2699b766'
        '9b'
    ),
    'programme-178': (
        '41ad226b961ba66c669f7bd9bfdc14ee10302b49fdc626ae2a723dd06055ce'
        'd4'
    ),
    'programme-179': (
        '0e4a76b2b576628142e3bf1be92b1e5a970372ab79b16ec05708e23bf935af'
        '17'
    ),
    'programme-18': (
        '529fad5e61e2add7b088182e4c25b1cd0c26da494c5a8e282498dba9d2bdd1'
        '04'
    ),
    'programme-180': (
        'b209bd5cdbbaf05f87ec221ff8d6895941e2d53e4ea282e48ecc6c6822cf8e'
        '10'
    ),
    'programme-181': (
        '4f86d3ef82311267730cfba20e8795d7b147c11b4d9eef230e5df772ddc9f6'
        '1e'
    ),
    'programme-182': (
        '9527bcf8d5d65424d8273bb80262cde418490d0ffd357bd7845ca4c095277e'
        'ec'
    ),
    'programme-183': (
        '0d4123c61df2aa6dec9659c1c43bb766624e32baf3c639427872e96ff9a16d'
        '2e'
    ),
    'programme-184': (
        'ca0440c4fbaad799a66dac14fd15988d60e7df3041b358363df1d47fb644c5'
        '71'
    ),
    'programme-185': (
        '1f535e29cf734ea2342180bd6888c9962b2bd91a7fb5d9e03607825c9a4ef6'
        'a3'
    ),
    'programme-186': (
        '2b9329ef2888b9dc3ef1a6bea59f912faa9cc90d245bc89ac5f3ed0d785ac9'
        '18'
    ),
    'programme-187': (
        '1d648c75b88d6735ab72ca11d966b14f98aaa6e0b26fd360af9bf96feb5ce1'
        '15'
    ),
    'programme-188': (
        '00e6def81ec3c11888bb6f44047f72cc28b31533b40182ed60341bda2b2928'
        'f9'
    ),
    'programme-189': (
        '26fd90c49b418a29bae42d98981cb96c2e27c2449c5c18073fd2c4e10864b4'
        'd9'
    ),
    'programme-19': (
        '07887ff76e47c0e5a39c011c2a52e5a7b2382fd34e303b6f6b24a7f27746c2'
        'fc'
    ),
    'programme-190': (
        '0a3524e634c05b51cc747ab7839c8ef6408c2382c354b99d5db604e71f489d'
        'aa'
    ),
    'programme-191': (
        'd5565b66e0e4fbbe1d14206690c1c86f909fcaaa4cfcda7e50e9db9a5b0e5b'
        'b2'
    ),
    'programme-192': (
        '0aabfc64d00ad08bb094b47e7dcd9dee8545ef4afbe7f5cc91241c7daf4917'
        '46'
    ),
    'programme-193': (
        'c68373f5083f30ec1bac70ceb598b2b3b9f5242b3a269b5618cf07da59a34b'
        '2d'
    ),
    'programme-194': (
        '72f0d2d634b7547b2fd08a20873963c2892afb0bdb600362ed5ccd918b1fde'
        '19'
    ),
    'programme-195': (
        'd644151fca57d2c791f139f3e2bf3b8a786824615483629c09d3c6313388b0'
        'cc'
    ),
    'programme-196': (
        'c95b70c890e9fae57aff40f16f52e104504c2be83cdb7f8a1bfa9845c9dc3c'
        '11'
    ),
    'programme-197': (
        '1eeef2964d81e4b41b7f6f7fc8d4cb1e24c2ca714b95961dee66b8fe44cef8'
        'f1'
    ),
    'programme-198': (
        'd5af6b17461998b3587314302863da3d2fc8e408bb9d479cae81d66c385f2c'
        'bb'
    ),
    'programme-199': (
        'bc981fd16f00825de92a298589237911de78ee04005436b938f9842b0c2c8d'
        'be'
    ),
    'programme-2': (
        '9f3c209dc9da1069d5c59731105520ab17d1de20c175ba8825d276bd5db0ad'
        '30'
    ),
    'programme-20': (
        '2e631ff6334ecb0d0c24ffc0d646d4a02a1ab34cfa4e86e5207806b15e7fe6'
        '0a'
    ),
    'programme-200': (
        'f1a9f866b4726af2c37895f5ec57c12263119fc80921d2b1c2f5f29b1f9d6e'
        'c4'
    ),
    'programme-201': (
        '6430b67738b8cb6a423ea19d44eb1998a251cc11114268d68f28774c429588'
        '11'
    ),
    'programme-202': (
        '08ed91a874607aed4199a32c7b92607d91e095e962f7129f07b8e822c85930'
        'e9'
    ),
    'programme-203': (
        '96f8099879369744da92fdf2d5925f7fc93e7db07a5629f96309b5b3a84eca'
        'b0'
    ),
    'programme-204': (
        '719f567aa4e569428a8f87fde8d67f63f891f114f1d1b0792031109578caaa'
        '93'
    ),
    'programme-205': (
        '73a77e7faa129d97e9fa38efb132271e61a397094d64bf71013e472fb11519'
        '8c'
    ),
    'programme-206': (
        '5c424e45e3a328a62a2eb4c64d93f95864e69d52f938c6a820b2c12e91484c'
        'f3'
    ),
    'programme-207': (
        '60461839aef8dfd23df2d97954b93ec6640fc8fe3f7b34f3fc0d1b9a31c57e'
        'dc'
    ),
    'programme-208': (
        '554395f3cb8b468eaee8cc65bd6f5ce3cc598a925b8041fa285c27dcf40108'
        '29'
    ),
    'programme-209': (
        'cc10ceb28fa577fdfdfd300678ed086d95459ce7341729402b38326cb92f15'
        '8a'
    ),
    'programme-21': (
        '62134209a68ed28abda0d30924ea28f310d265ef57516149eb8dd656e14cdd'
        'f6'
    ),
    'programme-210': (
        '78b094b828a2f7d19f83a7e88a7b3803c370fc9a685e0bdd9b6e004f764069'
        '6b'
    ),
    'programme-211': (
        '572a33ff6a8b21175b18a539b84795094503cb891f495f7185c6916495d423'
        'a0'
    ),
    'programme-212': (
        '6f828451f16e122bf6f62f492b0680596a658ec7f4e340ae2b7620970f0c45'
        '7b'
    ),
    'programme-213': (
        'a052b34591bf50163d673ee584de6aef870fdac71e6b41a32e338a6cd5fe0b'
        '39'
    ),
    'programme-214': (
        '7adc6d5d949a462e40678d171f481b141f049a5299d97f5ca09a0788c9e761'
        '76'
    ),
    'programme-215': (
        '55a0620575e0c9ed871d05adc2bc7a82f537625f98d47a083c41100dcd826a'
        '15'
    ),
    'programme-216': (
        '765758081e47803364be466d00fe98a3913b011eba4271281af98c9dc1c6d8'
        'f6'
    ),
    'programme-217': (
        '6fe567ece6a8d433217b13061f6144b0203f5bff2bef2be83b0da7406770ba'
        '8f'
    ),
    'programme-218': (
        '2600b7151e0721150cb9f8914a676b402b5107c81b5aade0c4ab360896a2e8'
        '16'
    ),
    'programme-219': (
        '89bd90cb016ad9e1707da7d9ad7e3ce39ec9591232d3ff160ef5753bd4fe9c'
        'f8'
    ),
    'programme-22': (
        '9e93e69b880158dd0f52051f65e7c74865aebdf4ccc12bb104e046d525bb29'
        '0b'
    ),
    'programme-220': (
        'd4b0d33e03a45d6e681b5db9cb1c77cbc90dbc6a1e3f847333ac0626c063f0'
        '9e'
    ),
    'programme-221': (
        '9f5579f2e59a80f5a3ca323d73150d32d79bf30178d2f2bb5883928624dd6b'
        '07'
    ),
    'programme-222': (
        'c990c4fecb03a3c7a7da6b0d35ac779234ee9d1d2ff42ada797e451b67bc8a'
        '79'
    ),
    'programme-223': (
        '1d351c4ca3d22b1df8dfcf9d6e7a6ee84f6a7a0549a45be8e9005a67e066a8'
        '5c'
    ),
    'programme-224': (
        '9313c49c3a4174b50c6b3aa6a2b4650644cae214d6b97ce440a25e41c951e0'
        'c1'
    ),
    'programme-225': (
        '16f6726a2214c0e631130886a7e75f058eb95820639324f1b70f2c102466fd'
        'c0'
    ),
    'programme-226': (
        '5cb120707c60c9c00bc91f0a73709d262c2be69b6f5424e3cea5dccf1c4b5d'
        'ab'
    ),
    'programme-227': (
        '687f1d46ff38b0cb34917e10da7742d24e0c49a8273dc759d320bc5d6896be'
        '10'
    ),
    'programme-228': (
        '5942f31faac566ea1a3f87213200362fdcd9d66c131df126270b99aae3e6b8'
        'c2'
    ),
    'programme-229': (
        '3432e64b394aa2fae6e7118ad8cd0afbc05c756ebb96ab260b2c52ffd0d128'
        'b8'
    ),
    'programme-23': (
        'cbd0d6177923a79d7fcade93b6de5eeda9e5809d42eecec02fe0e1d31bcc14'
        '2b'
    ),
    'programme-230': (
        '4b9207e8d3820d129665c945479ccae66c7f833b2b1be31c11671059e08cb9'
        'bb'
    ),
    'programme-231': (
        'd3c73532b1dd61b4c34ddbeaaf0efe2b3794ce4c6be0a8dad1f09d8ce8daf1'
        '0e'
    ),
    'programme-232': (
        '51950e48f559184d128e970b83fc154d209513099c523586a530413d6ad5b9'
        '4e'
    ),
    'programme-233': (
        '7e567ef2722321201742b448d74098b01df4ad0e2b20da09d921d5436734f2'
        'e7'
    ),
    'programme-234': (
        'cd7474811b1b1438bf407015e266c3584612761a6a13fa3eca95b91d061560'
        '69'
    ),
    'programme-235': (
        '9cb43ea9de510805ad30cbab263252ef039b34a48a2f76005c9a6cda70455f'
        '70'
    ),
    'programme-236': (
        'd5c4840bfbdc28bafd59dd03636e26b10ae4aab721c9ecc62a48216af8c494'
        '65'
    ),
    'programme-237': (
        'c9d79d79bea8d956f04f96f93f61e90af2cb1c90a8d05460ff217e6a2f43df'
        '96'
    ),
    'programme-238': (
        '9744647237f764067767087c25ccdb082670c2be31b0fc61f8fe5fae891c0c'
        '3f'
    ),
    'programme-239': (
        '8f0164351865bd92efb68c2f479d28753b37bbc830ec32e7250610f43c2e25'
        '9f'
    ),
    'programme-24': (
        'b8c2f07841fc34944de2dc1ff5821715a263dab20fffec5d5536365e7b7a57'
        '54'
    ),
    'programme-240': (
        '2b16fece538600a48096b87f0e7fd5ca269d272e785c3251fbcb628365dacc'
        '64'
    ),
    'programme-241': (
        'b45421220b8a5438235c15ae30d005bcf83ae95b4848429d1ace68e678db7e'
        '52'
    ),
    'programme-242': (
        'bbb964ce418e9bc27b0852e1e0826fc5e020c4bc16bd4f731391e76a94755f'
        'b4'
    ),
    'programme-243': (
        'c3ede1209b5170caa27816d39465a3bfde08e0855ac64cf612d7b284dd71e7'
        'b8'
    ),
    'programme-244': (
        'd6eb7f65ad9f98decf124a43e5815f46fd0cf14ce804b81a3ac1a695a028ee'
        '16'
    ),
    'programme-245': (
        '6ada7005e74b747da7de5311dd99a05523f08b027472eb362c54edfb21e771'
        '6c'
    ),
    'programme-246': (
        '71148075c07c10119dc40949b0a2cfbba87910e53a5b846da954dd55445ed6'
        'aa'
    ),
    'programme-247': (
        '37e8018b146ef45a3079d209085751652167402fdd338115a9472eedb309a7'
        '29'
    ),
    'programme-248': (
        '38aec08583e9e88fda5b0db9728d4be529c5d34ae98c4991b1940023e9b6ff'
        'ba'
    ),
    'programme-249': (
        'bd9f1d1d51db77404c03c93321540b2fe5307f55ef961e2e3ba696f8bba9b9'
        '4a'
    ),
    'programme-25': (
        '7269492bcde474c80bbfa80aebb4c2f5eae7c4bdccb74fa1e6ba4bf4beaccc'
        '6b'
    ),
    'programme-250': (
        '6f91510d7f193f1ba7b13d7dad47de768c7b480a146605c3732de9bc668d05'
        'b0'
    ),
    'programme-251': (
        '8430d1577c4714ce96c3d9af1ecb42f859803ab87487f51e00e38cf63fa3d6'
        'f5'
    ),
    'programme-252': (
        'a6e894b8b5e5bfa3f10d9ca118ea94019f6ca21eff4b901e8826268826a2da'
        'ab'
    ),
    'programme-253': (
        '60f43d468c3f0009564e85eca4527b6ebe599b73e772f1b539ea3becb7250d'
        'b3'
    ),
    'programme-254': (
        '12e1f2e04ede13db1672ff6a279d268e2d5a25000dfae9542b8c402705486d'
        '7f'
    ),
    'programme-255': (
        'f5573ff3cd398f38ebed14105756d56f58af8ebd9627e7286a078d7e6fb86d'
        '48'
    ),
    'programme-256': (
        'd72161e5394ff8ee5ea5db08431f59f208702de9500c7482bcc8a50c7ccdf8'
        '98'
    ),
    'programme-257': (
        'c6a3fde94e4919ac65aa204b918c424f732de51e074a5e86247a00c7bd9717'
        'b1'
    ),
    'programme-258': (
        'ee102e8abd837b428e70d6d0a410a61b0ff8b0834e357e455518573a76e9a2'
        'eb'
    ),
    'programme-259': (
        'aee3841ffeb0a93881d1b2bddfb690a44c9783fc7aae7491c33fa5e0bd388f'
        'b0'
    ),
    'programme-26': (
        'be4d938677aeef6ccf57bfc0f03690e62d1e78464c414d98d1e873fde6d441'
        'f9'
    ),
    'programme-260': (
        'c0287e4337a1f4fd3f26eab4cc8340240d54efc8dfc36a829d1a3a09f91476'
        '1d'
    ),
    'programme-261': (
        'e15d84ee6d997e83f0bad668932ae6a04b715032781412ff5a0408caf139c1'
        'bf'
    ),
    'programme-263': (
        '29ba0f87f6399fb4b3a9c42025ac5662ae4751e657e13caec59308baf2ee24'
        '06'
    ),
    'programme-264': (
        'b753473050545c59a5a21801af344e9ff9c6add2a4ccdee5871e3637a94b22'
        'b4'
    ),
    'programme-265': (
        'dd17b258f0006609594435cf5dad4558e1d2433f4b510e01dca7492c7015b0'
        '72'
    ),
    'programme-266': (
        '6734d2cccd99e9c632cbfe3b003e84b4a4bebddaaff10a51283dff99a2ebbc'
        'b9'
    ),
    'programme-267': (
        '8fa0025049bf24903b4feffc9eb32241dcda6dc62903234f83b7d2d760e807'
        '2e'
    ),
    'programme-268': (
        '136fe3974c026603ced21c6064205ca8012d73ed52ec67aca8ff79605a615a'
        'e2'
    ),
    'programme-269': (
        'da10a8b32dc4878f666962d33797dc29280448f31215809133bcfbd186f5be'
        '4c'
    ),
    'programme-27': (
        'dcaeaa12efb3d57818ae680809eaf9028c56418f212f7e0709155650c7ce34'
        '5d'
    ),
    'programme-270': (
        'bf3fa6d81351025b6c982c1f0627e5dad9192a5dc4e841128beea7cf16f073'
        'e8'
    ),
    'programme-271': (
        'a3e86e683d5f67ea013f8f93f0790229f5f268a376bc3fa41a24f568c8b9ec'
        '3a'
    ),
    'programme-273': (
        'bdfa5cded81ed6e8eeda226650001a289200b3053f0c47de738a975fc7a3b5'
        '3c'
    ),
    'programme-274': (
        '7d7248d484cd90a83c177dcbdf075ccc3b54b9a3c136a47c750a3b637a5a8b'
        '20'
    ),
    'programme-275': (
        '0367f1d75bbdb6bdfd9d6835d15ff56f0235c9f034d1ad1b27850da4eec3a2'
        '57'
    ),
    'programme-276': (
        'ecd570b457ad7df4581b4056f4819b7a59aa8ddbacb042815cc38fc86c753a'
        '61'
    ),
    'programme-277': (
        '39682242b61eaffcb19e0d029f21e903a39b5cc2672303eb2c76a3fcfacf5d'
        '07'
    ),
    'programme-278': (
        '3941ada5c7cb06d186c64cf90214113a6f1286d5526aeb22005fc4cd460f0d'
        '95'
    ),
    'programme-279': (
        '99f1d16f5e7202ee57c8d376816d4abe74afe7216a5cf441216c1a28cb04ed'
        '61'
    ),
    'programme-28': (
        '329ac6ba48abf4355705076c340c755d5088158d4709a0d789c0f27632a455'
        'd9'
    ),
    'programme-280': (
        '078a6681fc192a74419200d1488317ef51de9d7ce73b1f13eaa6d5914df8e3'
        'bd'
    ),
    'programme-281': (
        '2f49193abb7c69bf8d3987e0689edd3d7a72d9c1896cb81efdb526f498b311'
        '45'
    ),
    'programme-282': (
        'e2ba874b9d7c519cd47e1ff2246d03f4cc92cc3552a98b51f266f32533d93c'
        'ae'
    ),
    'programme-283': (
        'b3545236e8f482ec9323418ec1b2dbf7fabacc263ab68e4d6d298c6fa05863'
        '41'
    ),
    'programme-284': (
        '00fb77b2aca2841834de0758db17c71a683e94909f8f20779039fc752f2323'
        '50'
    ),
    'programme-285': (
        '3856253ff5444ccec571807508b65a4be3bdc08e0663d6183ade45d71fde81'
        '31'
    ),
    'programme-286': (
        'e4472f2b9070ee66c2eb7e3b6bf020b866a5b4ad0d9cc8e002c44fe4ecb5cf'
        '03'
    ),
    'programme-287': (
        '885191294237ef021db9b5a7a50fe3496fd370ff5e58c998e2405f3e3e478c'
        '8c'
    ),
    'programme-288': (
        '8dc3f1fdec9b2e2ae7049097455b8d689e7f1af3b37d9c31923f2b412b517c'
        'cf'
    ),
    'programme-289': (
        '104c6727117eff8652a329b8942c3f715abf24cc2f439d7184e668c9916cb0'
        '50'
    ),
    'programme-29': (
        '70759418708245f52c7ed93e04c77aad6ed1f7e911ca2f644abe86282a283d'
        '27'
    ),
    'programme-290': (
        '16f575ecc6b67febbfbc12c982a238c1f73248e0dec81025999221854a1510'
        '04'
    ),
    'programme-291': (
        'a2ab60e72de5a18d3a3a7b0c7ba2d0ab5fce4d7e32075b20ee783178f65ac9'
        'e9'
    ),
    'programme-292': (
        '6e703eb9e79d0ee1690752d0440d0e0106395fe958a9f429b9f3f8f8656114'
        'bc'
    ),
    'programme-293': (
        'fe566b9c81dbb23af7ca0e4660150a1d71c9c0077c1ec24355f3905d696c3c'
        '11'
    ),
    'programme-294': (
        '6c16143010886baf2803ceaae771b3ee06559d5408a00b9ab38688bff8b885'
        '52'
    ),
    'programme-295': (
        'd16a8ff97380719fc0665c2f67e2e8698df20cccef8355c6aedf1641337343'
        'f3'
    ),
    'programme-296': (
        '4977a4424e2a65a492a020932b4158940cf88e734493c641cd757c1171e6b1'
        'db'
    ),
    'programme-297': (
        'bcc459e4844f7e1754609eac1405e91538205237b4b94a2a10ac23509f04af'
        'a7'
    ),
    'programme-298': (
        '25bd397e4d4a7bf54e889583d9eb5ed720fee4c3a34dc88fdb331a3779a86b'
        'f6'
    ),
    'programme-299': (
        '91e173bab04b709c8072041d9dc62509348c4077a406f676834e45c3dac393'
        'e3'
    ),
    'programme-3': (
        'f3dd876a8c00751e95c7fb50c829d2efeae6603e20af09b63ff84fc033c789'
        '0e'
    ),
    'programme-30': (
        '8ca0295be7ed91644324ca2f51da5418c6cf97373b3848328cc810224210e9'
        '91'
    ),
    'programme-300': (
        '78c4575d22f21d1543801ead7aa48393d6888abd562a0e5fdf1d273bb5d916'
        '2f'
    ),
    'programme-301': (
        '8acca44612e6b461cda40bfff33f016ad71a921ad9b72f1a6a7e18b32f8034'
        '1f'
    ),
    'programme-31': (
        'ad6c047c74ff64205ac7df95dfcbaa537afbbec0feb2d8e815e69553dca8a0'
        'fe'
    ),
    'programme-32': (
        'dd65ecc21b328fa4e2d4cb3506a52e977e3c02a238f8a121e9fba1caf8813f'
        'd2'
    ),
    'programme-33': (
        '2d89d0c2549acf24d08ea37c9bd6963dda78982094c21a902e57ab64bc75da'
        'd7'
    ),
    'programme-332': (
        '568088fd68ac02ab84f7a7ff205b9b0355b170864efde1d2793a48cd7a0f15'
        '21'
    ),
    'programme-333': (
        '85297a9a5f3314da0ddc18870518791f535154e6b4e8a8e86e99e652fb46a1'
        '22'
    ),
    'programme-334': (
        '498bdbbe6093170822d7f1a18c1031d1e43c45b4a398bb05c511e4c747d996'
        '06'
    ),
    'programme-335': (
        'bcf21d0466c943161b7a67af97f5d0cf8f0e237ac069e3e7c237cd6e9d1336'
        '4c'
    ),
    'programme-337': (
        '9c402751e8b4f396e4c282fbbc4460c86b425d9b063579012cb0b06f05253b'
        'e8'
    ),
    'programme-339': (
        'c578f60eb7746dbb5efd724d785d1588bf87381111e785e00ff9bde885fbc1'
        '11'
    ),
    'programme-34': (
        '83399c7ef85d8926c289c13c81cae6d7fecd9912af65b5065de9d64efd4a85'
        '0b'
    ),
    'programme-340': (
        '772e48fbe9b6f689c0aa159a86899c0d72f8c01a88c2f87b66604c56c80672'
        'b8'
    ),
    'programme-341': (
        '89443da58208fcc3093af5480e91f0b6f01268c40d93fd4a148c1575ab9fe8'
        '34'
    ),
    'programme-342': (
        '5bb5eaa2dd0f9ad1b6a765c4e967dc2514b93972db706d196d83f4c963e5e2'
        '25'
    ),
    'programme-343': (
        '0dd00e2cc047dc034e63ede05f20b77d63d73b9fce7a3245f953fbe17983ab'
        '27'
    ),
    'programme-344': (
        '66a819329f6635b14be83c785b0792d38bd06646d0ccd0a2eac8fe6d4dce52'
        'fd'
    ),
    'programme-35': (
        '20b328773a134455642ddc87f10b9fc6dbc465c60d6275826664580546c0c2'
        '7c'
    ),
    'programme-4': (
        'ece2c3493835f681260885431506065a55139e308f15e9205e094716f42ee6'
        'ed'
    ),
    'programme-40': (
        '683e1dbda2de101ca30c55ad46d0c6f59c5df94a518f3592dddfd1f0073799'
        '09'
    ),
    'programme-41': (
        'c3bf406b167c0160255612f7d884ad658ed80f147dbdfc72c3e8ccb3c00428'
        'f8'
    ),
    'programme-42': (
        '4d29e70307e702480732d643e3c7ef0a7ffddc202a90206ef40d86490f549c'
        '2c'
    ),
    'programme-43': (
        '6f23dc3bc9c295dd8b43e0b2f2284f642c9e49166dcdfabce4a064dc498cc4'
        '06'
    ),
    'programme-45': (
        'ba5b1bcce917db78d7c0ca0764892fd82f7fa83cf3916996a0c1b0fb3b95dc'
        '99'
    ),
    'programme-46': (
        '9ba712a76a891845ff4caffe7040badae891f13e2e05f6fd27fda40ab0f436'
        'a7'
    ),
    'programme-47': (
        'be40f1248ff609106ad02af2da4bae81f2529d5af53f523167c44b7e2b9347'
        '76'
    ),
    'programme-48': (
        '1351206ffb613ca18151b125114b6d9471305f3795eac1166152e53d47d3a5'
        '5c'
    ),
    'programme-49': (
        '0710c5dcc4543e29b2f4ee0be6f09eee6bbc9e1e68785b6754f928b13d09eb'
        'de'
    ),
    'programme-5': (
        '8ec35cb92f327c6da4cc759beb739bdeaa8f6cc072ecf6aa0c8e1e10b79b3d'
        'd9'
    ),
    'programme-50': (
        'fb96b85db622ffbb591d780b783613768cfd7afab68a04f15cfd3a62813b04'
        '83'
    ),
    'programme-51': (
        '846e171e7844f8c8d0b0e7832e16a4148938940fb3c77d862076ca03fa14d6'
        '4e'
    ),
    'programme-52': (
        'cd7637b4ba5175a3c5891641d1cc58dd88d83575f433173bccb3d8f575fdf7'
        '52'
    ),
    'programme-53': (
        '6895422bd7b188f2be0912d2214c7c49b9063458252cd8bbdf5bac5c574942'
        '77'
    ),
    'programme-54': (
        '2f903538a5e847c9fd5846c6e6d41d88c4522d291eeea9d4671bac88a6cc9a'
        '93'
    ),
    'programme-55': (
        'd90d7249aeda0e23f2a1aadc9bd71fcf547a904f1d1171c1609409036fa339'
        'af'
    ),
    'programme-56': (
        'cb1946fa95d1f049939a7591b5c7628a7875c41ff009cdbc2fc16ae6fd9e8a'
        '94'
    ),
    'programme-57': (
        'dd85ed8df0a6e4dff156d82f19f0174b124c975e1afa33bbe830fc3b7fb3e2'
        '9a'
    ),
    'programme-59': (
        'a9b8a0f04863b8feb5e454306f571d228981e87961f8855e43ad7337cf1d15'
        'e8'
    ),
    'programme-6': (
        '3d59715637018902f8cadf48b0019f2fdb86e89cd35252934a916cea3f59e3'
        'e3'
    ),
    'programme-60': (
        'a801a91b716dcb08fd574d3d79871afa794d984abbfc1f57f8ff6d21555c53'
        'b9'
    ),
    'programme-61': (
        'dd074f139df0d3b8f7b3f61ee1c35737b2d49ce7288f842ebe844407c3cf41'
        '36'
    ),
    'programme-62': (
        '657a2935bd999d37837c068df6154f4f5d770e7386da1a4783a1e5a6fa0b0c'
        'f4'
    ),
    'programme-63': (
        'bee256fbec4b9aabaf43fc1203756964b53f12bfb1ccf44b7b655234f5e6f3'
        '0c'
    ),
    'programme-64': (
        'ff20b1ce7e09c84f640323b59acefa4ba79ab240c904157bbba60ebbcfd908'
        'e0'
    ),
    'programme-65': (
        '20dcb67eaf402901a1ddd980ea3f3e43730c5c866a300ac6764f30735019a7'
        '54'
    ),
    'programme-66': (
        '62869eb2bc5c29e59b1645cf782bdb6dcf8decd1272178f252b132a6e90207'
        'e4'
    ),
    'programme-67': (
        '0e109008d57ec4d15b4caaed96eab14eec217a6e519981dad2c140fd713209'
        '09'
    ),
    'programme-68': (
        '843c80349962a774f80539fa44816e8575ba63ab584401e761d86b00600e7e'
        'b9'
    ),
    'programme-69': (
        '13bb8adc84291507608e9cd773af2574d2b94f6f60ec2fee3295d42e0d0d72'
        '60'
    ),
    'programme-7': (
        'b3d729370148f42331cef010daac8a67fb297422fa813a5806ae9d2962262c'
        '4b'
    ),
    'programme-70': (
        '56f8c771fa44eee281b3fca69bf7442aeaa647023b151efa3ed49779a262e1'
        'bd'
    ),
    'programme-71': (
        '82b3196b0b8c13fc0ceb27310fda53970621ec935ad74979a7cd10f7c8c420'
        'fa'
    ),
    'programme-72': (
        'f8db290d14dbfbc9768a010462e590e37762d27f81f241df45ca7ae79e226e'
        '99'
    ),
    'programme-73': (
        '3c0a160bde3674564b38c0ed10810cd39b9cb5c4e773fce94612e07fae5747'
        '89'
    ),
    'programme-74': (
        '476a439404e45ce9e611abfec9a07224b3ffca73b556054aeaeae013f498de'
        '92'
    ),
    'programme-75': (
        '178c79eb6f22170cbd5d81eecf7e6062c69c191d0df065471f1c3c9cab741a'
        'b3'
    ),
    'programme-76': (
        '75b9b393fcafcf1b57568c24bb6e2ad289359d67295d57b2a78fce93b61dfa'
        '65'
    ),
    'programme-77': (
        '4dc279b9f3cbcd22e841c74a381ad81c5b28e9d49f75eb7b92392e707f3418'
        'fa'
    ),
    'programme-78': (
        '636fb305e7295f3a85ea2aba84b38795aba279e04a44ed60164118d0f17a55'
        '2c'
    ),
    'programme-79': (
        'baaac1a13a6a471fe3edf48a8ec9c57c3c71ee9438dd99a0bb45c1962eb22a'
        '75'
    ),
    'programme-8': (
        'dfb2ccb06e4b3b39e5b49812dbb89d607c7dea75b6713bbad265accfd3f395'
        'd8'
    ),
    'programme-80': (
        'fb29120950b3929c33f90b300097bc94cd293dafd7a17ecbceaa9a85da76d6'
        '8a'
    ),
    'programme-81': (
        '79eedfb9183d380c03cda1cb358ed8292af9abf5a55ca65f6d3f8e265a3658'
        'e2'
    ),
    'programme-82': (
        '359dcd5cfdf307e791a7d3b8541eda27476ca3abf27b9d9d6dae16229b308d'
        '42'
    ),
    'programme-83': (
        '9b9dc3b372682b3d61a1d477c208f222cc40174706e42e51138675a3b4fc46'
        '89'
    ),
    'programme-84': (
        '7e1e6618037110f06a17e0b7b3499d6c1250abf07d7c5177fa3714cb57f37b'
        'a8'
    ),
    'programme-85': (
        '6a7b0a82c2974b9c31ff3bf0acfb8334e54cf537427f95d30c51836e597b6e'
        '10'
    ),
    'programme-86': (
        '080f506c2cb9b12471e5b64f8726b716eaef096920c04f5d48b642d21b9e06'
        'c3'
    ),
    'programme-87': (
        '956b27d3fa2c29d67e1d5858359466abf5c2548ad075a107601a691435c2b4'
        '61'
    ),
    'programme-88': (
        'f6900ec46911550b9e24251059e76d09f21b50d35777a8010b97bdf7a5123d'
        '15'
    ),
    'programme-89': (
        'ac02e5edfd1c76d7ef03afe900873b962ca15d8198cb09e56be8fadb1c94be'
        '96'
    ),
    'programme-9': (
        '757fb3727b75ac5b9f34021e65383c23c0248f0bae40786887f55ab74ee4fc'
        'cc'
    ),
    'programme-90': (
        '8c26c5410d74f281aa23cca7d8fc5aed1cf75255660a3f06c25bb8fb54cb47'
        '7c'
    ),
    'programme-91': (
        '539c015e3f8269bd76b957b2c03a73768f9cb7d505cf3879ad89145a534f5e'
        '88'
    ),
    'programme-92': (
        '24dc561e0a208c3470a73a4f2c9ce587a6413f5bb5233f92db353b420d3aa9'
        'c8'
    ),
    'programme-93': (
        '182bfeafe56d7a6415e685dc55572f57a5d3b4cfb85c3a37962c89f2a2c19f'
        'c3'
    ),
    'programme-94': (
        'ef6c03b83d7c4b6ca6128673d678feaa4da8a38ebc6fbe939f41e250f6d4e3'
        '8b'
    ),
    'programme-95': (
        '4aeb5878045b4be729cd6fa1fe00a04ba8dbf7f90a52ac578e592af6b2004e'
        '57'
    ),
    'programme-96': (
        '4c3a015fe63f799c0c4a5b2f37342f03f393297bd3b090c92840d2668ca4f1'
        '58'
    ),
    'programme-97': (
        '2f9eb8adad5be5f785cb66d7b3bdb5ce00ee3e4c777cf19da29b495660dd7a'
        'f2'
    ),
    'programme-98': (
        'aa23b38deb5cd5e19a54985fca4e0d9335cebe295cad52e62f21da19dc124f'
        'c1'
    ),
    'programme-99': (
        'c806a6922b194e7b2169b69f7f8b0c9ce33db36052fbed202ec50317993d95'
        '71'
    ),
    'sibling-178': (
        '3dae61ec9e9b44d28b9661ecf52cfca52d0ee3e27a24e6de187d8e8922fbd8'
        'a3'
    ),
    'sibling-179': (
        '37f04e53729b9738da1693f5edb9ce6efa3e7321bd63f7c7c02c99422d6778'
        '7d'
    ),
    'sibling-180': (
        'fbc3f6c029d19b632b1271f8d9fee1e8c34b6a51ec2a42a2efb1910ec376ae'
        '4a'
    ),
    'sibling-181': (
        'cd8082aa5b03b831b44ec62199440329e18d243ce3b4ceee9042f38f9142da'
        '7f'
    ),
    'sibling-182': (
        '883bc948ec2c3d160c9397b064cf395b2995a32f2b575ba7e12e7463bc8301'
        'f7'
    ),
    'sibling-183': (
        '0aa0d2128da1dfc766912cbbe230df2438859b33dafe24f4b04ef3077988b3'
        '8b'
    ),
    'sibling-184': (
        'af6af32142b918253aa5e74d5afa365ac600422884115d56eddcb43f135275'
        'c6'
    ),
    'sibling-185': (
        '9ddf37bcdd5c3daa3384435283cecc7abc3f7656c66555ff7fc4f9baecb769'
        'bb'
    ),
    'sibling-186': (
        '4bae46abc86cfc645262faeb0857c9341254d5c02e3edd20c7a66f93c9c693'
        '98'
    ),
    'sibling-187': (
        '74a96e348a39650eaa3e1cf33eb407c551b35f0221b041817775a9cebdeb90'
        'a3'
    ),
    'sibling-188': (
        '2be85573ec641a80ee7acae3844bff522602117e6f49e474f90233f02c58aa'
        '71'
    ),
    'sibling-189': (
        'fb6be33675c303d15dc2b9af74a691f1e468228d0a30404f4e0741237a46c4'
        'a0'
    ),
    'sibling-190': (
        '1c94d7234952df2fe8ee8c3fb13498654f82e3d427c08f006554953ea26b35'
        'e3'
    ),
    'sibling-191': (
        'b246475e363ecaa0f3ce873976dbdf43e362aa5306cc441bf7292605fcf770'
        '77'
    ),
    'sibling-192': (
        'f0f3ff766f28c03b5f7f6af558e01ca3c82ae99ce926c76cc8dcec32d424e7'
        '0a'
    ),
    'sibling-193': (
        '87c7b71068ea98ed13f9659bff5a040ee3f96390ade5d8e350d48be5757f93'
        '87'
    ),
    'sibling-194': (
        '58394bd1a8466005e88b6ab2ffb88eddbc8a5cac79df0579df2b23875908aa'
        '87'
    ),
    'sibling-195': (
        '1fe379a0cb2d2fe42c847b740c66bcfc9cca9abab5bf1bdb51cac43d45f3ec'
        'fd'
    ),
    'sibling-196': (
        '435126e7abf81cb7204f2d86afa8dc5f59170015d23fd07f652af6353676ff'
        'a6'
    ),
    'sibling-197': (
        'ddb552686cea9cb2190aadb8a876bf03318ad0435fce8ac8797bbcd88fcc0e'
        '3e'
    ),
    'sibling-198': (
        'fe62d6a7a04aa7730708c403a7af03fd5ec40d9bbf2c33d0148a74a2f37737'
        'c9'
    ),
    'sibling-199': (
        '267ee6b84a65341bdc2cd50863b594338820508332d242244af623965bc868'
        '11'
    ),
    'sibling-200': (
        'abd904e906d228e4877d480acb256366b23d45af0b344ec161556899ecb35d'
        '2d'
    ),
    'sibling-201': (
        '8773c097ba20acbd075d2205919dfb30b66748858e5f2dcb4d59dabb439e78'
        'ee'
    ),
    'sibling-202': (
        'c49ac1420089ab1f78788bc9f0eaf72235db759d745bdedd2553a32672515d'
        '5a'
    ),
    'sibling-203': (
        '18bfedff7e48e2620b563fcf12c2826b589ae74d32b04c458f51ff7374f231'
        'f0'
    ),
    'sibling-204': (
        '49bd13b86c236fc3062821cd4a842d21cf3495cdf1f8aefbb5ad1ca5c3432d'
        '71'
    ),
    'sibling-205': (
        'eef5adb101a54c78980fa871f74afde697433d28472f401d939f22157fe12b'
        'f4'
    ),
    'sibling-206': (
        'f46675269e0ebdf8662e6827b78e025c022ed23949d9ddab931d2b8b097e75'
        '86'
    ),
    'sibling-207': (
        '58a59adea313f4a57b83159276f1fdc6e41f3caa3b6c3508a0ee2e13b60339'
        '49'
    ),
    'sibling-208': (
        '94f95c25ad04c174d22611e584a8f8eb67503927b1baf8ef737ded2e8112d6'
        '87'
    ),
    'sibling-209': (
        '2b950c0e3614104ea292fe65fe05aa22749e599a44594beaba0b55dc416199'
        '37'
    ),
    'sibling-210': (
        'e45e2bb9de496346b8e439caef9f731557feac4fa8c90cc8d8d16c315ac422'
        '34'
    ),
    'sibling-211': (
        '39a0b4c7918a6e76d8284c1908943b7a0768b86645772091172a908751d383'
        'e9'
    ),
    'sibling-212': (
        '7bdaa46ee9b2e0055090b27d3754106bed2923400a5cbbe526eeb6b4a8088d'
        '97'
    ),
    'sibling-213': (
        'd7fa00bd08a8a6250d188bc2b5a2b68928caaf8bd5820d87674737bf398fe5'
        '18'
    ),
    'sibling-214': (
        'd95ed99ddc281d1ee282e324ee79a95806e3751312d991c1b6003f589c059a'
        'c9'
    ),
    'sibling-215': (
        'e709dbb4102d1e1a4ed79fd18a44aacd409d20ae112c03f5a5b62fb9c1ba9a'
        '47'
    ),
    'sibling-216': (
        '0316acccc6e8b6a2eab10bb4a47dfb30245b7bb68026f7d30ce00c416a9439'
        '82'
    ),
    'sibling-217': (
        'c5ff0503833040ac3379373c1bc8191d0e8364f132dd7bfa884236dd873e05'
        '06'
    ),
    'sibling-218': (
        '1f5e74e27a9e25677bb9fb3373daa274c4f7c163a8146b7794c25e03ddb692'
        'db'
    ),
    'sibling-219': (
        'db8340c522b07cca864948f81201b031b7e89e17a7fdad474298aa392eccb3'
        'a5'
    ),
    'sibling-220': (
        '9e65e41c44d8b66e95b5c1930b4a7f026d5a441d50a46d035be1fed9dabb5c'
        '35'
    ),
    'sibling-221': (
        '6d58e784e5931a9b9da466020584f8fa0bc01acf7452eb8a3620c97509cb11'
        'ac'
    ),
    'sibling-222': (
        '36ad62605e30b08316a64aaacb4aecd11103f8f96ffc4826e23229b83c649a'
        '57'
    ),
    'sibling-223': (
        '4339f4275d1960276d656d7fc92ecb9fa8dda1c2d095b52ba0c64ea9a02b43'
        '78'
    ),
    'sibling-224': (
        '9104d6708dd0eb7da63974748c735de126ea94f882ac54b1b2e652e1279301'
        'd0'
    ),
    'sibling-225': (
        '123d41fac5d3749b9c59b6a04841a207a5224610b819bc4856b93c0b553447'
        'bf'
    ),
    'sibling-226': (
        '7ca8e044ff92917ffdc00bdb43bf13a49a70a89a023c372c3c7cbdbed199b0'
        'ce'
    ),
    'sibling-227': (
        '2709e17ef825da4ec22004a6185ed7c435346c31f29f4d0546ebd3825d5d2d'
        '35'
    ),
    'sibling-228': (
        'e753d69655320849b2f6650d18ee8ecdc25007ef5f25c148be1b9c5584aac3'
        '1e'
    ),
    'sibling-229': (
        '4aeb927250002cb76e938600d6a2d246460849ca8b1053d557360db379af24'
        'f1'
    ),
    'sibling-230': (
        '3c1d07f4f592a7182806ee533fd6ee7b579998435731439b29eeaf4840cd3f'
        '49'
    ),
    'sibling-231': (
        '0b6dcbedf6836e97614361e25a6415afdfec471eec6a4968b95ebbbed1b845'
        '21'
    ),
    'sibling-232': (
        '93c28ef5a872b77315fe99114f30963c1c7b1bb5fd3b8859d7cd60be6839ff'
        'af'
    ),
    'sibling-233': (
        '3dbd66e0082f4a5f36034dba6a3ae55f5125857d2b321bb1fc9879a1222987'
        '50'
    ),
    'sibling-234': (
        '466fffe8fedfb6007e28ad39baa2516cac39a8fbed815b5874f1cd1c0cce07'
        '2d'
    ),
    'sibling-235': (
        'a004cde2ec94ee6e4a5ab21b6d0564c08c30ff360f50f22707a979a0b68f0d'
        '18'
    ),
    'sibling-236': (
        '230a9dd15177d834661b21bff8d87cbb73921fdc714141a0bae3793dac0117'
        '92'
    ),
    'sibling-237': (
        'b2ebb9675c932370e564adf6a04a2029d8580808b79d05c95c7ba59d925305'
        'bd'
    ),
    'sibling-238': (
        '86b3494e9e1ec5e42d2f68f748989de9a2a396a7aa348c995cb1017ea40074'
        'd2'
    ),
    'sibling-239': (
        '71b24cd2a88be17d874999d0aaec15b3a6fbe0bcddedf65dac178d3a3cb5fa'
        '78'
    ),
    'sibling-240': (
        '5a58e061127d7d4e5e94645d667166f05075da06d294a1dd600a661c5120a7'
        'b6'
    ),
    'sibling-241': (
        'cb4cdae27878c1be6474387e673b25e56adb891e6ceacbd53826a84b5d2fed'
        '14'
    ),
    'sibling-242': (
        '19a32f1173a8312a93485b99dd645955880d182797e5835d0b1f1994c4881a'
        '18'
    ),
    'sibling-243': (
        'eb7b442ba5250d46b2bc4e9ec961a0db7c2e491488db3ef57ca34f1525d715'
        '3f'
    ),
    'sibling-244': (
        '70ece85870d77e92af15bd008512fb0939cd538b68afc40e249d231863f96b'
        '8e'
    ),
    'sibling-245': (
        '392c19b599fc4b08a8b58c2128c275abcbbeb19b1fdf4314ff864774c7fdd4'
        'c3'
    ),
    'sibling-246': (
        '39a8e5a0f39a84141d6b24731be1d6879b4629a228225090f3c22c064528a5'
        '58'
    ),
    'sibling-247': (
        '955cc825296823acaf29a3bf76cd75f81b40a671dc4587fb97af1a1716ce00'
        'a5'
    ),
    'sibling-248': (
        '8242a57d1b3d4d19a8245b05ca551d5fb665827aa63895037401b264d30258'
        '55'
    ),
    'sibling-249': (
        '81be3d4ccd9a91b4fc8050c2a0ee94c3aae9fcbb0c636bd1ef56814352e220'
        '0e'
    ),
    'sibling-250': (
        '06b962991bed8f3a30b70c05403cf50334988a2d077a51945dcf5d03623715'
        '83'
    ),
    'sibling-251': (
        '58f238688f9aba9d4ea53ffd6c89e4ca84cd4a155d3afc679449a5572c8c23'
        '14'
    ),
    'sibling-252': (
        'b4ee38b84e11f5c412b711ba047a8899ca90be3b62584b837aa245fbe109cf'
        'a6'
    ),
    'sibling-253': (
        '0c5cc305b8808881b215a7615b141378061ea82f6d0a6fbd5352ad37242a46'
        'ca'
    ),
    'sibling-254': (
        '86637c5b70b99683cef05d2a691d8caaa47a10ea3ac1525f1f3c1d9a58288a'
        'd5'
    ),
    'sibling-255': (
        '36331e36fc9228904ef4ffadd6280f602fb631f034668f2a57c16a64f67947'
        '74'
    ),
    'sibling-256': (
        '7c79dcd51d9d93122db44841b9376dbe30eb42e4b474e1b97c51036222f2e4'
        'dc'
    ),
    'sibling-257': (
        'df4ff4072e29784b9b052cd3e6bcd53bc87536e0f1d7d1fb6bc01241bb478a'
        '9e'
    ),
    'sibling-258': (
        '9633fa69d294bf26c77a195dd54b0f980d438674b9518909175fe27e19ad5a'
        'c1'
    ),
    'sibling-259': (
        'dfa4b3ada3c3ec6cfc883be47adbf7a5c282bf008b7d0c86fc5661fc716862'
        '4f'
    ),
    'sibling-260': (
        '8451524f91babbbd3aa78e516dc8d797616d110ec557a0980671932fa036fc'
        '7e'
    ),
    'sibling-261': (
        'c5a22f3943ea38d2565b77c814e20a968947e418df4beb5f04ef583d32b454'
        'f2'
    ),
    'sibling-263': (
        '82922eadc75a37da43cb9d5991ec775ff9aeb6bd99f5a4323e4414608e4a9f'
        '91'
    ),
    'sibling-264': (
        '3d4e7470d5ca6e2f9ec5b0d6a369bbcfb723dd5705bf185768cdefebd39203'
        '8b'
    ),
    'sibling-265': (
        'f4446b835a750b984a5626aa61fa51df5ce00663948025003714d540c5b389'
        'b7'
    ),
    'sibling-266': (
        'a01b4b85c1855184100d6e650cbd31676da93a63cdd2df424a9f7f57313db9'
        '1e'
    ),
    'sibling-267': (
        '9ea08896dd93e5fe06173df7c1dd0d68213c5c3d4fc8a58a31b71cfb3ee118'
        'cb'
    ),
    'sibling-268': (
        '33138f3ae48ccb5fd2f8b8ee77675740c046d0c927ebcedea94acba906fdae'
        'ba'
    ),
    'sibling-269': (
        'f65037edb4ce0990045c8cf80a8e8a9314f6daefd89466681a96298ad5ecc2'
        '36'
    ),
    'sibling-270': (
        '40d41eef8931521f564412f34aa0da9eceb319e6b1d864477f943fed9c5f53'
        'bd'
    ),
    'sibling-271': (
        'ac0d7e46414ca8b1a3edae3ed2cb9fad628101fff3cbfc1c1e30b63b8f30ea'
        'c6'
    ),
    'sibling-273': (
        '1dfd19d0ace79bafbc7792052cf463e500986a06694f6c7e4c914101227657'
        'b5'
    ),
    'sibling-274': (
        '085cc9b3a49ed603b04bd40b051dec161cf052e107b39942c2efbe0827161e'
        '33'
    ),
    'sibling-275': (
        'dd5513fafcccdc24c1fbfe25a5a70cc141abec75e2b9442c7fbdb2c264c2b4'
        'f4'
    ),
    'sibling-276': (
        '160f0ef6802bf55ecc9ddb14fd649fccefe2b0a01c59e9e7e7e4a2ea26b807'
        'd8'
    ),
    'sibling-277': (
        'c5157f4ce9984af0279c8d8ac2e7834d21a0ebcab2ee11c5a64daf9c20a316'
        'b0'
    ),
    'sibling-278': (
        '708bbb86268155de73ff471aa81886a351961860c81196000fb048230b39a8'
        'de'
    ),
    'sibling-279': (
        'f379017b496dc94fa2119af8dd38509f26f0a46add5a77c84eac8ec8149a33'
        '59'
    ),
    'sibling-28': (
        'b56d0dc4010dbaef3988cfb49d5d08e54a09d3f21c8a38f430e4ead787a306'
        'a1'
    ),
    'sibling-280': (
        'fc2b718f57f995ab85aef17382578ae114b758324b5c176a55d70bfbe3f1cd'
        'ef'
    ),
    'sibling-281': (
        '82f57d0b937b4caad37bacf733bbc043e75cba582c1bcd539461359962f4f5'
        'ee'
    ),
    'sibling-282': (
        'e8230d8f23d3affe64895d94464a9490eed652df7ff120de23e3c795cc0411'
        '0d'
    ),
    'sibling-283': (
        'ff58e30d8e9a0881fb6428a2a5d42b6242c101edddcf939791bcf4290cb8b5'
        '03'
    ),
    'sibling-284': (
        'd65fecbd45d8b792c015b5d143650127f357b9de27d21d4eeb37eb7674c7ce'
        '6b'
    ),
    'sibling-285': (
        '273eacb740d98d3c57657963102763014826de9f3c21b29ac0c1e7ee4e5d51'
        '21'
    ),
    'sibling-286': (
        'dfe385f775833acc4e4071fccd33db792908d28846418a49c5c4f116b93310'
        'b2'
    ),
    'sibling-287': (
        '92891300755e4edb77a6b1a90f4b866fe3ed2b3ff11b81d42e2966d04992ad'
        '9f'
    ),
    'sibling-288': (
        '362a9598c430dd21e17777aaef9587a73902482219ee7b6df4cc40a4278cb8'
        'da'
    ),
    'sibling-289': (
        '27e99441febc1ae587227114a6bab3e1c969fd2d304c33c51f8935396a97ee'
        'f3'
    ),
    'sibling-290': (
        '0b5486ed0d3015c38c340907d8f5a1fda919a836077511a61b0b43be4b62ea'
        '11'
    ),
    'sibling-291': (
        'fd27e26089c7774dfe18f0052e7cb65b56f3ff029a7b741657e731901531a9'
        'f5'
    ),
    'sibling-292': (
        '8ac8c5110e8045aeaa702e825c9f985c16ed52552147a27c009d976c060b1f'
        'e0'
    ),
    'sibling-293': (
        'f2bdd71ad8b68be0e7f8f4b0e671ea0d8a7109098937a5a90446a4311ff1e2'
        '96'
    ),
    'sibling-294': (
        '71f0d6375d57fc6865b640c5034ce85ae28c7d7cf087706973f4dfa96e363a'
        '45'
    ),
    'sibling-295': (
        '6bba5da1169476c3018329a1937af78ea1a7ab39c4ac5b369fa07025c35793'
        '0b'
    ),
    'sibling-296': (
        '3ce22bc9525b6b52adddbc9f405bbc7ab6d369e927ca29455d600a4a6a45ec'
        '6b'
    ),
    'sibling-297': (
        '489915c3339b5f8c09e142cfa32a52427d07b50fc5b26d0d7129ad2554a08c'
        '8e'
    ),
    'sibling-298': (
        '0f09b41e65662680d0ce8f520893e90ea7e5c422e55e8cdff8fd72b4b965fd'
        'eb'
    ),
    'sibling-299': (
        '9428a521e861b4ad6f61bc58faae555124b5f548a8d786daef036688f09759'
        '50'
    ),
    'sibling-300': (
        '6f4315af29231e50d12edcf112ba2d74242eb7c557c393365413b72786e522'
        'b2'
    ),
    'sibling-301': (
        '87b64f117b1f9a6bdf07c1c04781c1ed5d300a9729e7bfb36765e1299e8d8d'
        '8e'
    ),
    'sibling-332': (
        'c3ee864d2d16ac600e85c214f03fd2c9b970a6ae16ce5efd5354f81166c79d'
        '20'
    ),
    'sibling-87': (
        'edfbd43223cc8b295e7192c0c9771d4c139761efa4ebd3222b001ec49af87d'
        'd0'
    ),
    'sibling-95': (
        '71ed4b485db562ec7e6039bfe13aa6aca95efa137e45a37a591c096cc0d9bb'
        'f4'
    ),
    'call-1-468': (
        'b3e3434dfd49171c22460842ca3f4999de490d394b6543f80821f4d9d5789c'
        '7c'
    ),
    'call-2-226': (
        '4847bbe27376ad52c32e29aee1f55e63066f6b3f1373fb565e0887b5662fc8'
        'd4'
    ),
    'call-3-467': (
        '243495e745baa9947759756740442dffe57329ecb64f97f771bbb9519e5120'
        '11'
    ),
    'call-4-469': (
        'bc1a957d8d3d62174c5e364d63ea5249ae3b19ddd2675720de7a75d1de9deb'
        '95'
    ),
    'call-5-470': (
        'c2c80059bf28ee184daa78417869ffa1346c5b924d94daabd9c91481448703'
        'cf'
    ),
    'call-6-471': (
        '5cf5668bee6b06c1779aa38a48bc24405881b2a3f38b8838cde82d3ae6fc44'
        '31'
    ),
    'call-7-452': (
        '16bea6b01e7302e99c9dd358bb5d7a7c353cae4adb38eb44b46d99133b3bb9'
        '53'
    ),
    'call-8-453': (
        '93003cfc741380a0487601e1d1261e4e8a2a0f7f9b222c22512f4fb7f60b86'
        'f4'
    ),
    'call-9-454': (
        'cc87a64b901e507ae538ac6e06ad0a3b33f387c26b2534e42d153c8160f593'
        '0a'
    ),
    'call-10-474': (
        'd210dd1fff72a268735bd10a21744889d99a26d79c9ad8df672c33ed2a96b4'
        '32'
    ),
    'call-11-475': (
        '70d68728b48930bf216d0387516de8387e62777f8c4e46dec8f1f7c7304386'
        'bf'
    ),
    'call-12-476': (
        '3c6a9dec8b7dbce99088735b24cd36c2b3ebbc433b5a6e2636694c78b8f118'
        'be'
    ),
    'call-13-477': (
        'bffb609686d13579cea676ba43da3672dd4bda7ff8fc13ce3fc328476d9a39'
        'c2'
    ),
    'call-14-14': (
        'a0198b9a761ab79535f9da6cb708b1d519f405d7cbf025efb5398488d1df8d'
        'a6'
    ),
    'call-15-15': (
        'f29f0a990f667f4b61e6dc5375eb847416822d04107d50ae27029f2e6c6878'
        '2b'
    ),
    'call-16-16': (
        '2614330eb8cdb8052da551009794962f8e7349c4e7259b7f55afb43ad43774'
        '45'
    ),
    'call-17-17': (
        'd6918429426e60d16ed2cb531cfab40ca6dc3978fe624d024ec624d9b9b808'
        '0b'
    ),
    'call-18-478': (
        'e7e4c1377215fec020fafe24dd9638f92f516468312026936718042ba72ad3'
        '42'
    ),
    'call-19-182': (
        '1f8c6c0dd870f20d41e9e8c005aaaddb527a93b54760f57ec662bd0ceb9a5e'
        '51'
    ),
    'call-20-480': (
        '511231f46e72d7af4ee455aef5fe9b7dde4b2d22812306fd1e8ebf2aff460a'
        '07'
    ),
    'call-21-472': (
        'f1a4914f409997e4973d5937d017b8502fb77070808ff55393f0dda9042e10'
        '4c'
    ),
    'call-22-473': (
        '753019193dcf4f8a4fba8b89bf3aa8941b43090df996ed164eb7aebb5a2a19'
        '88'
    ),
    'call-23-445': (
        'e06cda5f4b76cd3ddc9da2664cd2019e8bc7da5e73bf16891d3a6d5dc886fa'
        '7e'
    ),
    'call-24-447': (
        '0d68e87a887411f8ba72a22a81990079b3e3d61998f4f57c8c2179b7f4c6c0'
        'a5'
    ),
    'call-25-446': (
        '17e8bcc022171a842414fc0b9de5cb2242110d177079d0a887282f4619969a'
        'c2'
    ),
    'call-26-431': (
        '21e5e22ecc4954c66eef302a1b923764b11360f5e7e2a45ea41e394aac8c81'
        '52'
    ),
    'call-27-413': (
        '6f894bef9e41fe15c4a71655672900f575ea436cf9a419915935ebc6da7abc'
        'ab'
    ),
    'call-28-28': (
        'd00da1b164d619060cf455055b5404df656ca060df2ac674d65ede21374be3'
        'c9'
    ),
    'call-29-481': (
        '346bd3b7bbe308b9fc9c8681efa003987b644c14e16ae0ddb5ba3870357462'
        'e2'
    ),
    'call-30-482': (
        '9c7435b4015f0e01d1df1177eb2b9493b080990634ad14c1e6f6c49d5ac5ae'
        'f0'
    ),
    'call-31-513': (
        '084a398982e54e458263f0a1e57fced0e0242b7351f6dd1d41928db9d51066'
        'dc'
    ),
    'call-32-483': (
        '2a684e8693a118dcdaec6fee929ad7999576a91f29a7f00a01d42035f2a37a'
        '60'
    ),
    'call-33-484': (
        'ff529b418f7b5be3c4a5c25dae788d39187ef2d19e440c2f83391cb04788b9'
        'b5'
    ),
    'call-34-485': (
        '9374f1fc6f564c77b46cbc164c0542cf744382935c820c316038de587bae5c'
        '2a'
    ),
    'call-35-486': (
        'caf64a48dd9f9a9fc514eecadd2f4bde750d7fca09277ccd0656eeee9ac27c'
        '77'
    ),
    'call-40-487': (
        'dca5c91dd45918475c95dc285496d5cd4194fdda8373e8c6ffad49a768326f'
        '8e'
    ),
    'call-41-488': (
        '82a7b0e2c27b004edceb19214eaf1abbfe8ace82628dd02c965cdfa96bba76'
        '89'
    ),
    'call-42-489': (
        'afda7656f63d9504f83fb99f435ed692478a598f6bac3bccc686b8d6bf8e76'
        '51'
    ),
    'call-43-490': (
        '71e87f264b275831e679534c9c9aff03429ad8eebdf143cb96e0f5320aaf17'
        '69'
    ),
    'call-45-463': (
        '52d5f2be932c7283d07b979ed22a5626a821a8731a4fbf7c1e6526ea14cc85'
        '7a'
    ),
    'call-46-46': (
        'a6675747bf830fc0913782e5d799b8d79f44291ad128623c1426cf4c13f7a2'
        '40'
    ),
    'call-47-434': (
        '613f892f0af1f7636d3ca3fc14012987bec639819812b765b2cfe222ab7e49'
        'f3'
    ),
    'call-48-187': (
        '7e57ea71212c816ce5ef2d3654353e8de75de9fafae451d891034251b1e645'
        '2e'
    ),
    'call-49-462': (
        '651af1961b13e33dd134ea648a2a749ec3dca0390fb3765fbe661694cbe146'
        '4e'
    ),
    'call-50-461': (
        'cbce585472cadb376ece0cef953cfe29e238581645c47f9547ace583af227a'
        '55'
    ),
    'call-51-190': (
        'c3b9ec0670bba6a9c4be8470c01666581bd0ff48f8847788cd31a6afc05dfd'
        'ab'
    ),
    'call-52-191': (
        '046f2bd9571b70dcd3bb9811a35b6a2ad7af19a1e1b1dc529d9a1ec2c188ce'
        '3a'
    ),
    'call-53-450': (
        '322cbb082c9ac0760aa3090dfa14923b9333305b66fa7a7ad38d9252efa081'
        '59'
    ),
    'call-54-193': (
        'ce9cdb7988c455520d7b92dad1b9890a78abece1a7bbb24bf8f1e78a11518d'
        '2f'
    ),
    'call-55-194': (
        'a84c00300b1299e35f79e0fe5735b5b88218a3999bcfeeec02f20b1f126654'
        '76'
    ),
    'call-56-451': (
        '0971c3309db04c66e1de4a6af1f8e7bebb1d51017c7a354f463db8783b01b6'
        '8d'
    ),
    'call-57-196': (
        'a266740dced69f8fce669eb7db6630987308ca2e0151d57b869c984ac901bc'
        '40'
    ),
    'call-59-449': (
        'cb58e3ce7565dde79250b23ca44343b1982888a73153125d79ca97183e0c14'
        '58'
    ),
    'call-60-411': (
        'b6fc840479bf3ee7e96e78ed7487f4f6d4129b4a44e67085f9e51d1f29d87a'
        '74'
    ),
    'call-61-420': (
        'da40c7eef34ada241e0a7b1921ae494a038341a68ade11de8fea24ab7c8c5c'
        'c7'
    ),
    'call-62-491': (
        '4faa227cebe5524c2c635d444d9a475e50917d1fcb12f89172a200f9d09348'
        'ee'
    ),
    'call-63-240': (
        '97845479a3cc657dfbbbadd00b0ef2222428e1344d1ff813b3f57785c8493e'
        '3e'
    ),
    'call-64-492': (
        'fec55994f9c469fad0d7608687b0d10f3d6681748bdd4fcb7825a7a517b70e'
        '28'
    ),
    'call-65-493': (
        'c6fa0dba71879cc788be4a6bb298bb44d10132f31f96b1f96fecbcfc3cca38'
        '80'
    ),
    'call-66-502': (
        '352c98307c97ce440f584c43dd9474153050c0893a0f1614e99d81b1dbb13c'
        'd5'
    ),
    'call-67-500': (
        'f32fe93fe19d40f7d188a92554168f42aea50bc6496ff2bcaa991e13477568'
        '81'
    ),
    'call-68-501': (
        'b7a1c70b8341b32cbdb27d5243c235eda8f9ff79a731d6ee29ca6bda0e65e7'
        '8a'
    ),
    'call-69-227': (
        '8495c09f29f0a12aadb6bf3947fc32807f613f853756cb5f6f5f08e9010347'
        'f5'
    ),
    'call-70-503': (
        '3c9538d04017dbf8d889068d5ae80b17eb7ce978bfa124eaa21149f90365ad'
        '44'
    ),
    'call-71-504': (
        'e7988735e8e00ea840957e973ec60383f497843234bf95d7d01fb5f890f477'
        '52'
    ),
    'call-72-505': (
        '2e95af5c4a3ac73fe45985cfe84fd23d5adeafdba3749e01dc695815d056fa'
        '2c'
    ),
    'call-73-506': (
        '538efc25b6e49b233ddc59729fc48bb410638e60a364c9225ae07deaaebc46'
        '1f'
    ),
    'call-74-507': (
        'a4dac87d117c2f0b66b27b4a38d34ca36c745e422b4c719252050918c4a06b'
        '5b'
    ),
    'call-75-496': (
        '468ec11a8fd22d0a7422b8ff3ab30cac7f4691b93e6d2843abbe1fc87d329e'
        '5f'
    ),
    'call-76-497': (
        '6a55fc6f668575af16754d8e8eb92c1b020bd2ee375b93b2b889b033f81951'
        '3d'
    ),
    'call-77-494': (
        'a7759189765ce30b6f346d4417445239ec97e038ccd6174e066edc567f261b'
        'db'
    ),
    'call-78-512': (
        '1b0405a947cd139409f2fcb454a12b85157a918fe61fccde0d506f7fd380d0'
        'bf'
    ),
    'call-79-495': (
        'e1febb017d33e3246011b970252e4429e4e7655bf03cc805c4b4d9f3ff3cde'
        '89'
    ),
    'call-80-508': (
        '08739c5053ed1e3a2427a9827c17447d09350fb40126230e3a25c1f34c1112'
        '6b'
    ),
    'call-81-509': (
        '2650f6a87a70ce7e8d2b3368e044609b8a55079e1414315a86d9c9b5d904cc'
        '57'
    ),
    'call-82-510': (
        'dd220e34d2b0d7be254dbe6bbe4b612203a8f87b9720e64352ced57fcf2bc5'
        '57'
    ),
    'call-83-511': (
        'ae293bea936d79e150686dcbae76693e3636b59873c96ac15e0c08e9ee2b2c'
        'a2'
    ),
    'call-84-459': (
        '57b4828b6c7854a4618bbfa6be7932dea7bd05e1efc90a90ed5f29ea08f406'
        '50'
    ),
    'call-85-238': (
        'ececcebedc61992a656346fe0455a70d3f13844dc66817ffb7fec85167c34d'
        'b9'
    ),
    'call-86-239': (
        '728ad05ab219976d9b8972822628f539df8544bbd18e5a21e7caed9bb5aa15'
        '4d'
    ),
    'call-87-515': (
        '842b51fe2025cd5fc93bf2baa7cfed652ee0a85a2ed4fe4c32e16890f8228f'
        '5c'
    ),
    'call-88-88': (
        '62064f26d99d288d36c561bce54f9c47f59d1276f817e9dae5d3cd313aeadc'
        'e8'
    ),
    'call-89-516': (
        '2a18a6778489595ea05421cb3f5d3ca3813b4814678ace2672850591562bf2'
        'c3'
    ),
    'call-90-90': (
        '3681f781f711c922b4911c1c4ea12c87d94abb209a2084c6a188cf013845d8'
        '92'
    ),
    'call-91-517': (
        '2eeee42dc778599d0239312023edcf228c42a981651e00c891c027af799824'
        '7f'
    ),
    'call-92-92': (
        '307d4d5790110a904a1f2670be9bdf27452a2742cf4158adda3ba74dfcd2a8'
        'a6'
    ),
    'call-93-93': (
        'fc0c8ad2a4e9202d616dc26ff4c422a6c9a520ca818fd55f4f40cf6eafffdf'
        'a0'
    ),
    'call-94-94': (
        '405616db6f81da75a1926518f8b855f0a02c970bde3515200946e7096aab90'
        'd9'
    ),
    'call-95-95': (
        'a2ef85b6c8c47488648efc624ffd18b4c298aa95f105ea125af4e7ef18815e'
        '62'
    ),
    'call-96-96': (
        '4c9c6a7f9ac6ec8537d7d01c75157f189233b7098a1b7d450c421fbe8cac3c'
        'd3'
    ),
    'call-97-97': (
        'a70391aef49abde527198fc002975f09162e456ac1821e31d23f641f69e368'
        'e9'
    ),
    'call-98-245': (
        '4f9c19a07073c976118674a081c192a53c54c56119780dc5e3afb1311e6398'
        '54'
    ),
    'call-99-246': (
        'fdebfc6610fc992b16e4027eea1ef66662cf1fea273a1810125b826f1d4083'
        'd3'
    ),
    'call-100-247': (
        'cdfe8a9eb6a138f82108cb71aba5deeeeb077faf3988de20c158a025e7de47'
        '8a'
    ),
    'call-101-248': (
        '48a6a38a9cdf87ca7418ca036dc9228188aa9754785a343d910bbed085ee42'
        '26'
    ),
    'call-102-404': (
        '9831b82cc6b4e35758c71f330dbdeab0aa7f1f3ae487345c28535f4074395c'
        '87'
    ),
    'call-103-405': (
        '2664d524ccbadc56a0ec6e2ca74aa34d3680428ae9781fe4d8e2489c5370ac'
        '61'
    ),
    'call-104-406': (
        '82f565b6bbc6a5c9c1ddc560fe4bacb15441049a628b60418e9306e2749a77'
        'd1'
    ),
    'call-105-416': (
        '8d56ecc04d2dba58642211e6467396ebc42e1396ffff87893b239610b2ca27'
        '58'
    ),
    'call-106-417': (
        '7bc9ee10ecdc3a04a3a52e2562fa179deacd096e105b6d723bf8ff9327c594'
        '57'
    ),
    'call-107-439': (
        '8e074c3ebf4fe23ae3cb6bed816c406d7c231d00fe8a14aae70a3df4f0fbec'
        '66'
    ),
    'call-108-428': (
        '8ef99a5af3dd5415ad3cdaca713b04d800ec2d648bf7a2cbb9004511e1a433'
        '53'
    ),
    'call-109-109': (
        '83a020a9aea4cee5d42f950da3165cdef9c6e86ae084602004cb8d8bdde37a'
        'cc'
    ),
    'call-111-111': (
        '10c9f4114ab48659aea34ac2d7e4a332c8c2251b87756ae73c95ac128859b0'
        '0f'
    ),
    'call-114-414': (
        '6edd7b51da12ffd92cb861f9f1b38b0160c5f98272e27a536ccdf148378ba4'
        '68'
    ),
    'call-115-436': (
        '4357a4eb7152ece819c21fbe487bca15e154d658ff42e8dce4eecbd1e8832e'
        '4b'
    ),
    'call-116-432': (
        '175c237ce25160c7e259feafe5c75c966e3b996ebc1133b1b52b3e7822c93d'
        '5f'
    ),
    'call-117-433': (
        'fb51eb402fad7561b75a316e8a755252b4fd929823a90237e9231a191d7b8b'
        'a0'
    ),
    'call-118-118': (
        '342f4e05025ea3be3fe4d62fce2561661cb794dd67a8b3c2c4773d8fa76902'
        '7e'
    ),
    'call-120-409': (
        'ea77c322eb723434ef0966ed395c3cfc31a35aabdb3b78c5bc6cb9ad9d5729'
        'eb'
    ),
    'call-122-122': (
        '44f635525a7a449887e744ab1289eece019ed776aa689d6a73a6325bf00066'
        '50'
    ),
    'call-124-124': (
        'ed779a01cc722df6cf9eec20fef1b5fec282956d0637af1ed3adcbb842345e'
        'a1'
    ),
    'call-130-130': (
        '2eeb8d4d0d3ce55cfeba011be903000ed6992fb85405154cc6a19128aa1a61'
        'af'
    ),
    'call-131-430': (
        '9ff683f60053b9240e8d45cbffb172e6a1e1df7ae65d31771c31d0b77405f6'
        '41'
    ),
    'call-134-427': (
        '441e987be366d7ca522b394d38890e47dded47fc9a3f6c9412306011a3dad7'
        '23'
    ),
    'call-136-437': (
        'c0b31a8cb0eece4ba9689d48ed70b147c8decb63a296f86318b4400f8d8402'
        'e1'
    ),
    'call-137-435': (
        'c3d2bdd5937ad7e54cf5c1b77bcc8a39d9bfa8a49d1ff5a77fea880af4c504'
        '62'
    ),
    'call-138-410': (
        '7801a1d14fc40d3cf881648bb5262b580f1243f62802e22737f40f8d8f83bb'
        '86'
    ),
    'call-139-139': (
        '732e203657b566297adc3995e036067d148b085573af5edeca83780371a360'
        '78'
    ),
    'call-142-142': (
        '4e9ffa03b1d917a9f44a794314685bc3ff542f071862d8b5b46692d5c2cc80'
        '64'
    ),
    'call-144-425': (
        '43a4a3763906c66587fe52223cf8f3ca9c193d304eb1a2ed4069962c135a01'
        'f6'
    ),
    'call-145-426': (
        '52bbfbd0c6250dc17bfbf43eda3219c1b0e90f44bc88efe2dbb4b3b1561552'
        '00'
    ),
    'call-146-407': (
        '53cf10e9d4d5bc25a4b0176b076e857c2e30767ae5cca8af7d3fc49155adca'
        '33'
    ),
    'call-148-444': (
        '62d6ea194b4b262aa05e9f6ee8fa8be86cfc8cae1ed5eee64c841d00b85b30'
        'fb'
    ),
    'call-149-443': (
        '5e1fd095b9bc7ce0e37aea8573e32a638ad50dec26e52b9021f924aa64d28d'
        '87'
    ),
    'call-150-429': (
        '06892bd080e514d21440bf318c04df24e1fd632666188437d9d3b13a9ac6d3'
        '01'
    ),
    'call-151-421': (
        '892d565bf4ab12a06c4a03d8d2dde0cb4fcdc03eda66135503e3632ef888fa'
        '2b'
    ),
    'call-152-422': (
        '289883d73013e96aaf9d67b486e706d710888370e44bcc4609b5c4227329ba'
        'ff'
    ),
    'call-153-442': (
        '23e3dfa9d43480cb8a447f419fca287e6b7151c45bbb8a928f1679e91d5980'
        '7d'
    ),
    'call-154-460': (
        'a10ea8f3d22a4532508450e0cc629f5cbbaaac5141f85d58522ee1fdc59451'
        '75'
    ),
    'call-155-408': (
        '82ce26c0eb66cfdf41f1fba9ad46bde598579a1544701ceb3a2dbc76757c15'
        'd2'
    ),
    'call-157-412': (
        '043e1d16f330370fe953c637a9cbd8a5f6316315fa1d9c00f1b9118ee49915'
        '53'
    ),
    'call-159-159': (
        '74260cb6f9bbbaba9edda234601ed389d7b726b6da3b80a62df2dbb8662214'
        'a1'
    ),
    'call-160-160': (
        '565fc248eccb6d8577a36cda3cd045abe066bba60ae6f03de085e042dac8d0'
        '92'
    ),
    'call-163-163': (
        '96c12267b1a5d594c15f256a972528cd169b7d68ca5e6259d666a31d9b20f7'
        '5a'
    ),
    'call-164-243': (
        'd9b85b25df8a7b308bad2c196bfb19cadbfcd8252def41979da96e7c6f47a6'
        'e3'
    ),
    'call-165-438': (
        'c6e1dce5e7c650ee5d3d85c771167f0582f0d94685d30dafe5a3502f3a7946'
        'af'
    ),
    'call-166-423': (
        '43560b0e7e61a9a62509f1f19196ec19c9cdbe0c937a06d296bc287f21a3cd'
        '0d'
    ),
    'call-167-167': (
        '068d55e7eb6445eb0e6aa647a153175bedb13338f790e3879a04278d0295be'
        '14'
    ),
    'call-168-169': (
        'a234945761f311570ce74a251fa868720b1e9f44b6f1f722af2d9c8912f23b'
        '97'
    ),
    'call-169-168': (
        '750bdc5f49e4aeae34b587f728d536441b67d8ab341d608646edba2157e737'
        'f9'
    ),
    'call-170-170': (
        '3c38d39858cbb0fd8bf46953d24917862124d23213379764dd2a1440fbd8ba'
        'ee'
    ),
    'call-171-498': (
        '6f4684fbe6b14cc12c7addff949f47609a98a512026bf80c0e7e5d5cb043ca'
        '40'
    ),
    'call-172-514': (
        '2e97d73bb21452d9fd85f2d5e5099923fac127e7b7c2a995340d5949e1bffb'
        'ea'
    ),
    'call-173-173': (
        '065dc9995fa5a954c81afa55aa4a936bcfa033ffdcfa87d210e9c3146ee2ba'
        'e5'
    ),
    'call-174-440': (
        'e6101fff8e930aa0dd976a34384e9351dbe7f23112b87fea122f6e4702d329'
        '65'
    ),
    'call-175-175': (
        '68b556d4dd2536b5c38d2e9d16c0b4d99731b46687d309e55f9263ffbedda4'
        '5f'
    ),
    'call-176-466': (
        'dd8aa7ecf3629cb811806d9e71b753f9ebcdec21c2aeb14fea7df1a8ecd1a9'
        '20'
    ),
    'call-177-499': (
        '8abac079f95adc352446ef80a93cde565bbb51b8df4a3e9cecc48465b8bf78'
        '6e'
    ),
    'call-178-250': (
        '2402984d9a6cfcdb66157cf8383b94d0757deb2e56bebc75b583f9ea6fc61d'
        '53'
    ),
    'call-179-251': (
        '671d020608f6bfefbe527674ff3d09f0cc1e6071cc91205ffb0f18b01e9c1d'
        '98'
    ),
    'call-180-252': (
        'aa9089beecc73e569206575f2c26fe6b4faab81191d09fa22b4bf8649e36af'
        'bc'
    ),
    'call-181-257': (
        'c1a004bd883d13c4820f6298062a37adbbb0a74e4526c46ad099a4aba64848'
        '98'
    ),
    'call-182-254': (
        '16bbe580ca0a506df9a78d89f6b92b8b7d59f389493bb40cae125a8ce31028'
        'c7'
    ),
    'call-183-255': (
        '28dd7e14a05be1762a386abddcb26eb81a542f5f091dd1590fe036278cd838'
        '80'
    ),
    'call-184-258': (
        '9156c936a57ba20b4dc44fb98ac662d843546dc1687e295d8b17423f7f9fa3'
        '91'
    ),
    'call-185-256': (
        'fbda139158ba9eb4b0db0e4ae294a07efc39fa2b34bcf3866d2f3d140d9600'
        '61'
    ),
    'call-186-259': (
        '2228107a6fb247967fc191dd399b1904899aa1b216f7b7807da9ec305b98c9'
        '58'
    ),
    'call-187-260': (
        '0cc9e54e87fce88f6c57145b62cbcd47ac01970c1693bf18e4955f8dd38a7b'
        '01'
    ),
    'call-188-261': (
        '9d29b04366f43b3ac74fd1bc44097b81c5c19de588653b71969c7960eb596d'
        'f6'
    ),
    'call-189-262': (
        '4747019bce54679315432f559c0f5c0ec20b7e9843320141cd497c6987d4c7'
        '67'
    ),
    'call-190-263': (
        'f739a04ca1c92463fef64ed87f24ecd1129c0b564116cc48ae2cab66b13561'
        '70'
    ),
    'call-191-264': (
        '4a1b261eb24ad231381e6f1d18f350d4387fca6d7440a7c62d69f040162957'
        'ce'
    ),
    'call-192-267': (
        'be09bee9fcf987e5076ef2722d0811a8b81e7af669355991fdb011d8c82503'
        '0a'
    ),
    'call-193-265': (
        'fe47f32d1af8ad6c9eec38cd6f29879d1d3b8d4951a6d8a8573d3829992d25'
        '9a'
    ),
    'call-194-266': (
        '6d12790af74aa9086b4bf7d92076749350b600dc089c228f2c8f152f5e34c2'
        'cb'
    ),
    'call-195-268': (
        'd71b7a336451454f315b5abd00e3280add28cbfbac23081d784286e4b90c27'
        'bf'
    ),
    'call-196-269': (
        'd9a930125f1cdc0bb9ac7cc9b32b57dd0697ad7434e7c6dd7162dd42ada948'
        'c4'
    ),
    'call-197-270': (
        '0cf9af42282f37f3ca8f8d0a6683f0051c04a1cf80945e90c14e350eb27267'
        '8f'
    ),
    'call-198-271': (
        'ec46679541c8c321c152f0229d25d880c4af1461dd310f17a8a73cb2e6cf1a'
        '6c'
    ),
    'call-199-272': (
        'e246bc9ee120835762b56ce88689c9664d028478dc9cbb7cf3389ab29793d4'
        '5c'
    ),
    'call-200-273': (
        '787680f3ce96ae03aad1612206ba5954e80605d8944c113475362f30487946'
        'c5'
    ),
    'call-201-275': (
        '2b243190294b6844127df4ab64e69266449a7ff9b78ee9eda2186d61b96daa'
        'd4'
    ),
    'call-202-274': (
        'ca35b25dec2b7dc2c166573476d7c3a40e07ed30d40f8c9fabcaf47887f5f5'
        '43'
    ),
    'call-203-276': (
        '558d452d7bdc0c210b78bb756ba2ecc4266c90222d3251933667f5f03216ba'
        'a6'
    ),
    'call-204-277': (
        '105d13053888919788fbd711adc9c686560361e876967e706e7a2784f27bcc'
        'ab'
    ),
    'call-205-278': (
        '295d135881cd81b5985104b3fe91a4f07182f2eec1c47b744615b7ae2efc0e'
        '20'
    ),
    'call-206-279': (
        '5b27e2ee9f84328f0a765acaf65d75a5e02442acfe2f4f1f8e7dc5afade504'
        '4e'
    ),
    'call-207-280': (
        'bd03831fd8eec370132148cecc6a28d0cda14588e77ffb6fd847628ce9c116'
        '44'
    ),
    'call-209-281': (
        '4c11fa799093b84e5f7b3d35ac40b1ca8e016c768be332b37e15c6358e6bfd'
        'ae'
    ),
    'call-210-283': (
        'd60a75bd09b465ab9bad04637d4c31b6a042022bbf02b4ad40e3ef14f6fcd0'
        '22'
    ),
    'call-211-282': (
        '6f9c600ba5209cc25888df787541ec808c458c602d5fcfadefe98b10ec3299'
        'cf'
    ),
    'call-212-284': (
        '285b0de01620dcb8534432444d5effe93921c8ff2894b34a34e57f853777f1'
        'c5'
    ),
    'call-213-285': (
        '178acafc72465a720d66109b6959230cd76abf29370219869d0799c7c36d91'
        'cb'
    ),
    'call-214-286': (
        'ac61aee1935b815a42dfaf1a1e8c0803043b34436912566cb16d0157dec567'
        '63'
    ),
    'call-215-287': (
        'b03409e8ff5ec905330e72b914634aba0572dd5705e36bfa35cfb2d326edee'
        'd7'
    ),
    'call-216-288': (
        '9544981f24ec2cc5b734a272a893f1070159cc5eed07c1b8a68906a50927f5'
        '78'
    ),
    'call-217-289': (
        '2648ec8bac5a322f798923ce3ca5862837a10719981dbe4244d13de14984b8'
        '66'
    ),
    'call-218-290': (
        '06111b89ea6f8e0feafd281f885a53b8c5c7ef4939b11d5a2d32f1f1c7ec94'
        '41'
    ),
    'call-219-291': (
        '41afb3d688937903ef96a38a3346e8c8fffd0e837d626cf82ce48a38c3a3dc'
        '39'
    ),
    'call-220-292': (
        '61de0eb239126246902aa910cb4d6a295b8c9b9e2f6036511a7242758a9bef'
        '65'
    ),
    'call-221-293': (
        '49ad3eb5f4d25685fb6827a71da7ebda6b7578083f1ba24d85f6952be532a3'
        '5a'
    ),
    'call-222-294': (
        '34bf8d1a3dab5eac8d2b93f77bd2fa83b46d52c11e37dc9b28e271f03f601a'
        '14'
    ),
    'call-223-295': (
        '80d73bd7f7d4c0be12b36056262a2eb2526a81f1634d51f4c50595852c785d'
        '33'
    ),
    'call-224-296': (
        '61468fd9c07f3ae3be09adad9ac5e8d2c4001b4049f77f3b910675db635d5f'
        '27'
    ),
    'call-225-297': (
        '011501015d9bd348c5c6ad700eac1753ece6b618102713d6d0abd6b41b8640'
        'dd'
    ),
    'call-226-298': (
        '2333e1a236b40c1646cce3c10da362e6c5e9def07ca72f8569bf96c7982534'
        '98'
    ),
    'call-227-299': (
        '77b4583d1cb5c13f6c711f29ee2423e38d6209865153f19010bb23fdc589ec'
        'ba'
    ),
    'call-228-300': (
        'eb8ee14f96458375b67ecf0b9ad5c4489d0619816a514ad829bfa098993e84'
        'b3'
    ),
    'call-229-301': (
        '9f374cabf0f08d4be85b8ae996674d65ca6b971d07426eec68036f9c8a4257'
        '59'
    ),
    'call-230-302': (
        'd78862dd8a39509df060d49a17c59ac9b09391f06619e5be4c8c4f4c76afa7'
        '3f'
    ),
    'call-231-303': (
        '73e215798c46fd9a80c571edd4cb430a729172c146e826f2077475e0ead13e'
        '6f'
    ),
    'call-232-304': (
        'ac518e93b86f440abd4482e208e3481f45b2e2e73e50bab9597652f0d7bb52'
        '21'
    ),
    'call-233-305': (
        '9308bf26f9ac83e8cd9fe2a59ad8bf9d23c69b38c03b51f1600f55898817a7'
        'e7'
    ),
    'call-234-306': (
        'e2474ea08ce89ae96abcff538738472f8e2cd546b195e5bd6892ae8ae78ee4'
        'f9'
    ),
    'call-235-307': (
        '3a033491b44beeb5858d324499955cfcc84500f99abd894c80e888d6b794b7'
        '41'
    ),
    'call-236-308': (
        'cdf14e07de81bfc57f4673f3bd43999a30bea546ef5487860f285f2671e3ef'
        '07'
    ),
    'call-237-309': (
        '7b561ed7dbe010c790cd0e8ba328fcb4831a0b22b630412903ad12cfadbf6d'
        '19'
    ),
    'call-238-310': (
        '7befa450403b35a6259fbd642b68831d5a81d18663cd4902b5617bdd3949c0'
        'c9'
    ),
    'call-239-311': (
        'c9de726e0ca13d749e3fbdae1cc466fa5993d05f433173182176b21493b329'
        '77'
    ),
    'call-240-312': (
        '757598d58c05ac88f672c362de1c067f21333b1cc90b24dd2e4ffaf3c4392e'
        '8a'
    ),
    'call-241-313': (
        '8030c27bb4ac7fc4b77db6649398284950816b750086fe3bf34dde559566e6'
        '37'
    ),
    'call-242-314': (
        'd51151d45667300b797724dd5e63a9d45e0dcc0b0c5ace0516a254365173f3'
        '12'
    ),
    'call-243-315': (
        'c607d944420ac4b3f854dba3aaf262fdb373585159b915a2aa3bd6b221d230'
        'ad'
    ),
    'call-244-316': (
        '78ba43fcc30e411bbac13a6ad124b66048cc00cd03340eb8d10a67fd8a7d89'
        '92'
    ),
    'call-245-317': (
        'f9d52fe93faa8a1fb1e45def83669054329b81c7ea4388d1a18fb64fce190f'
        'cb'
    ),
    'call-246-318': (
        '49a8cb4fa01dd5b0ea8f816028eafbc4fcb594138480ecceafb482cc500130'
        '17'
    ),
    'call-247-319': (
        '3458a110f18c89ccf362256b6bb9b46cd5d0f93fbfd0f0e93ce08e6984c920'
        '1e'
    ),
    'call-248-320': (
        'ffa31caed2ddf670dc0bcd34eee0d1e5ccd127856128111182b7e9b76367f7'
        'f2'
    ),
    'call-249-321': (
        '6f06a84426c841b5fe444be2e6ec45805346f9ab53aa874b6d1923f0ae33c5'
        '6b'
    ),
    'call-250-322': (
        '5db5ee6bc43b3c7cded4469269f8605d62a153ad8e3c317e9c11e0f8483e55'
        'f0'
    ),
    'call-251-323': (
        'ad6bc700f55f40f367d837abb69824d178ecebf559787793d797d828d9f352'
        '50'
    ),
    'call-252-324': (
        '6ee1534a1225827b5306e9b335d8be5f813c53ac16ff2b900972a520c47f91'
        'aa'
    ),
    'call-253-325': (
        '89b1375bf4b8a3f956537369d409661a322d16a8ceb645fadd3116cdb229df'
        '3f'
    ),
    'call-254-326': (
        '6b652e7c8d559e24e35602cab5dffdb968140e7e7f12a485365c42582caab3'
        'c3'
    ),
    'call-255-327': (
        '28529f3d70935c5d94ae2eca673ceb57e1730dca6d191fbfa49d430245c476'
        '19'
    ),
    'call-256-328': (
        '1b4897365b214a120e87d8851466b4f24715eceab716ec7323d80ca732c072'
        'a2'
    ),
    'call-257-329': (
        'afb64f4517b8d89e3cd2c4ecb3deb4b3f78db9e8612bd9325aaa5243df3ece'
        'da'
    ),
    'call-258-330': (
        'b7cd8bb91dda51aa0502257fb2482114aae225a9dd355056e68055006ec61a'
        '5c'
    ),
    'call-259-331': (
        '8d8c2fdaaaa53b35c77f5923e06ed8063f9f72f7f4366cb5f9b5fdbe5f55bf'
        '55'
    ),
    'call-260-332': (
        '62f0943aa3734c671c1c7a8e6e5a010033bb88cfc3e964cfa408c1928b3f0e'
        '24'
    ),
    'call-261-333': (
        '11c7ba7f7e54a6bb266c5ad9f38b1b86a2e971c84280f14db8f89d52a51fe2'
        '4a'
    ),
    'call-263-335': (
        '1f8b6200422e19e3cd2116e268de81b0db5c182330c28267934d5ad19b9ced'
        '06'
    ),
    'call-264-336': (
        '2e6a5cad6bb3d98ec5ed0f207c6d1bb803a16457f692ff3f9433822e3d213a'
        'b2'
    ),
    'call-265-337': (
        '149b90b1244118932ac00f657990b04d7244804881aa7a3ea4efdd67e27da2'
        '13'
    ),
    'call-266-338': (
        'c300bcd52d6bd4b282253bf571b50d67d6ea10d3d2a3dcd09d96e9c9508c90'
        '62'
    ),
    'call-267-339': (
        '3b143f921b906182faa326278bbfd97cdaab9c471ab95d3cddeba38d73f29b'
        '23'
    ),
    'call-268-340': (
        '439713d99693b4470c03f60c4a7328db13ade88faf58b8f7e1e533c9bb4747'
        '56'
    ),
    'call-269-341': (
        '3b55b89cecb2a008a43d93f15309dcc19f001891f614b301e321f1a50a0249'
        '85'
    ),
    'call-270-342': (
        'f0a675d43f80566342c78964e30f41455d61c0c6a091f1f364405bb3d2f111'
        '8e'
    ),
    'call-271-343': (
        '9e39dd9cdb5564322427efc98ab7e58f8e7c2e0410055a2d325fc06b696e2e'
        '39'
    ),
    'call-273-345': (
        '42825fcc9d7e9951bbe212062109a8f17feb6c0a6e2932a45cafd0e8e20e26'
        '30'
    ),
    'call-274-346': (
        'ffc45f9846b1ba1950b6988c131af0eaacadaa82fc0e1a0cf2af7414db9eb5'
        '29'
    ),
    'call-275-347': (
        '20cd801b14c69f4fbc7593538203a74c41c05c8f4132698df6f7198b820323'
        'f2'
    ),
    'call-276-348': (
        '87d9b5a9afb5b7986fe916fce0278dc2ab7c65b8ff69c193961b11cbb60aa6'
        '7b'
    ),
    'call-277-349': (
        '0843af99606f8c9f5acda02563a170d52284cf584b479732e2c18caee1b05f'
        '6a'
    ),
    'call-278-350': (
        '3d657486874229401264c7f9b379ddb9f155ad2f8e9384c6b04ac31289c6a7'
        '0b'
    ),
    'call-279-351': (
        '4df55f9408a09768f2e2f0148ace71ce203c70da2ceeecef2f1a9a5926f3ff'
        '25'
    ),
    'call-280-352': (
        'f588e3c0f42677feeff9f7be7445cbe3427c1291e79fba96cb2bab2f2dc68a'
        'f5'
    ),
    'call-281-353': (
        '373ead036f49c9c9337168062747dc4e7af712e48e58556ffc21ab8cc301fd'
        'd5'
    ),
    'call-282-354': (
        'bb3863347383a311ae0d776779fcc8c47279c6368e8ae8fbdc61eb0e48a181'
        'e4'
    ),
    'call-283-355': (
        'b88b5c75edf4a4a1c870acfa121f38589db40c6f2ffefa585f39f09ce01872'
        'af'
    ),
    'call-284-356': (
        '98fa6b8d652d41ea101bc6fa7ba45aa0d2ecc21d81dd0803f5918c7abf6873'
        '7b'
    ),
    'call-285-357': (
        'd04d165ee41b7a64c82651fcc6cd425839ebb56fa7346a78d8c4a46b6919f3'
        '01'
    ),
    'call-286-358': (
        '4ee894e20d9b0f185ba2aea80ddcaef406dff99c1cd89f21fd32b70bfd9fae'
        'a2'
    ),
    'call-287-359': (
        '6415b93c91a422e5d9c3ce4b135747ea674070817fb2937f3f65fb00aa57a4'
        '04'
    ),
    'call-288-360': (
        '2b5a617a32dd1ebc4772e7e206761792b0893112069b457631c81f5152befa'
        '9a'
    ),
    'call-289-361': (
        '49a92683ffff8435d6c10052ee6a5ea9b24af3e1dc75f473f81f132bea2809'
        '11'
    ),
    'call-290-362': (
        'f2e8994385d268d63a395aee5f9723b25201da385c87525ad6d4dba13add12'
        'e2'
    ),
    'call-291-363': (
        'd26b649b0df137e18fde04c09c73c832a176a51f883bacbda47561c520344f'
        'dd'
    ),
    'call-292-364': (
        'bf6f9756d808d793f9eceb124ef4512d70183fb508da55deae22bca88651c4'
        '53'
    ),
    'call-293-365': (
        'a74e84ec163b28874b55c5fd76bdf2807fdda07bd3e84203a47613a4998d66'
        'bc'
    ),
    'call-294-366': (
        'a7f5803d7306da795e6199af4546654bdf261c460e64e3be05c1e6dab874b5'
        'a7'
    ),
    'call-295-367': (
        '7be54f8ac123007949d20f120d0b5ded8addf9dd2ca957aa6e5aae83deed50'
        'cf'
    ),
    'call-296-368': (
        '5dad307a2060424016cd7e300cd84077ad9ae9866a8e4624555b4eba0d37c8'
        'c3'
    ),
    'call-297-369': (
        'b5b2972de8bb4066822a866e7cb4675c7ac574f1d9c92148cfefd509e1c35b'
        'a9'
    ),
    'call-298-370': (
        '65f892f4ffc43ff6f0ffd562c41402039cbf10f9df1ddbc14dffd740448e4b'
        'f5'
    ),
    'call-299-371': (
        '7a6be42445cbb9b309d3eb7343607ded1ce406d40fdb1dac22f4e6aba07ae8'
        '41'
    ),
    'call-300-372': (
        '0225a5a73c15b00a4f99a7c573d615eeee107b5188617153df8ebfd55477f4'
        '1a'
    ),
    'call-301-373': (
        '43d27457221d3961c9d7f2c810fc0c419cbc13b1a08f12dfe36cc4c40cbaa5'
        'aa'
    ),
    'call-333-415': (
        '3e71b3b54e7057134452de72ddddea07b1c10a9a8280ffa191eb08ad6dcdf6'
        '05'
    ),
    'call-334-418': (
        '329a881e8084b1b0a63603a879bb56559fe3b72cb2ac4e9880efb3559dc956'
        'e8'
    ),
    'call-335-419': (
        '07887ef5d6b7b216bcc7e3f5e24b02edb0528183bd24799d94aec6c3fa0024'
        'd5'
    ),
    'call-337-129': (
        '5c522959924c89f59787b2882b932abc3a511627abfa729ad5b6153d3c4831'
        'a7'
    ),
    'call-339-455': (
        '7dc6db65b95f09538379a9b2621152751fadb08cc12ef999552c12d25253a0'
        '4c'
    ),
    'call-340-456': (
        '59149fceafe12a35c3502f9c612ba824d60aa8b7e60977a2d234b6142b2cd6'
        'c9'
    ),
    'call-341-457': (
        'f45e35f6e4e83def9da91f703320897779e31c1e3bcceca91a31a2c3217bba'
        'b8'
    ),
    'call-342-458': (
        '1d318a01236d2e9f6ca7aeda3e553f680d49b76a98ec46e5c2127364b8ce9f'
        '82'
    ),
    'call-343-464': (
        '3a9a01ce4018cc864a83bd0a668b06e46f41b2036b0d2d50018a2baecd4238'
        '79'
    ),
    'call-344-465': (
        '9e13b0aeacbda4ba51ad7c8107c622ab76c83edf69f531882f818e5c5c8fcf'
        '03'
    ),
    'policy-0': (
        '06040c03a8e0c54bb5c1c5f2dc2799aeba75be23a05bc6089aed5b638fe2ee'
        '8e'
    ),
    'policy-1': (
        'd07628b7c6b3071135e0e0f3500caf5df4740ec050e685a21d28620083768e'
        '91'
    ),
    'policy-2': (
        'e0039a4114d8f38654e264fb46974f95531ea596ffc479da37b1641fe5545f'
        '85'
    ),
    'policy-3': (
        '6ac8b9a8ab365878547545b86c19c89bd58737d3b4442fc7a780ab23e12dab'
        '11'
    ),
    'policy-4': (
        '24ae97413862d7587bb140b50418eb92cfda94fb8f96a2e91b6237a51184e9'
        '63'
    ),
    'policy-5': (
        'd5422ab3050517de711ad6c68fe52a1e46bc5356b4f250540914279ba5cf06'
        '5b'
    ),
    'policy-6': (
        '7106d0d594f234501035f29e1bdc2ad54c2cbd8c2b8fdfbf4c64990e395268'
        '7e'
    ),
    'policy-7': (
        '81054f4ccb4f0cd15d9fe3ee9fd181977743cb27d52e5870115f65e412ba83'
        'f7'
    ),
    'policy-8': (
        '8e12e0ce65ffeb0d19173db1b56d466964faf6062f94268e1300dd6865d681'
        '23'
    ),
    'policy-9': (
        'c9309f2107859dfce9b04a4831e726266aeb2dbd293d0095894ca3a5a1b63d'
        '3a'
    ),
    'policy-10': (
        '4085a9904a933c8ea5396710353888733c505a90bd85f5b47fb3e3edee4147'
        '51'
    ),
    'policy-11': (
        'ff46096ab65f8e5a87d9e9188fe9551277f8f685aa86b139aec9d25d75353d'
        'b3'
    ),
    'policy-12': (
        'db0f625be4cb6439490da1c2dbcc7c29335566245482de261a27e72d953d84'
        '8d'
    ),
    'catalogue-incoming-1': (
        '7c64dea5ac1ba6f3efd5f205641f83a0f84537fe96cdf879f2bc3f100ad520'
        '9c'
    ),
    'catalogue-incoming-2': (
        '541d17d94f64dffe384e5087968a8406951329efe405fddee97460931f11af'
        '82'
    ),
    'catalogue-incoming-3': (
        '2ab28dc919608d098573b1189137e8a820eb4dba0e38e071789da4ba98979b'
        '96'
    ),
    'catalogue-incoming-4': (
        'ce7a50ec43d075bc862b6a941a1b86d27309ca9f19e7c1276f1e0e0fb87d81'
        'f0'
    ),
    'catalogue-incoming-5': (
        '47d97ca521534a45ec3e69926f8d3597aa2dcf1e7a86e4178cdd33274f16e2'
        '94'
    ),
    'catalogue-incoming-6': (
        'b37505a47151c35abc6916be14110a347b7f754a6952c41a3a1d659a833edc'
        '34'
    ),
    'catalogue-outgoing-1': (
        '7e3a123fed38d5064bacac8e2d71a4917ab104b64b8906d08c238a24cdaecf'
        '26'
    ),
    'catalogue-outgoing-2': (
        '5e64370013be533277d6b819406b53d1b51b4857d98e8eb0cca170484e05bc'
        '58'
    ),
    'catalogue-outgoing-3': (
        'e903f9614f2c9f70a83f2bbdac47fc733c6352c7817dc8d47ac414855c017b'
        'b6'
    ),
    'catalogue-outgoing-4': (
        '09a97a18bd3d2ff0414e0e0aa591ed0e00c0e09cb994e23c09c9e74a10d599'
        'dc'
    ),
    'catalogue-outgoing-5': (
        '28bc1631ffb8c8939904d2d0d601fc0ebe32e532ab884a53d968647a29a357'
        '2b'
    ),
    'catalogue-outgoing-6': (
        '166897dc5cfd0d30aeed7292ba2ee8408333846b034c23551d0d375f4d9c81'
        'a1'
    ),
    'catalogue-outgoing-7': (
        '19906b4b8c8814f8b9e83c8fb82c8b9fefc763333b87b6ea6210617bdc4085'
        '45'
    ),
    'catalogue-outgoing-8': (
        '8a1e60e2893d8e3fc5bea405fe1c94a061fd3ae075d4854d962eef84f7e6a5'
        'a7'
    ),
    'catalogue-outgoing-9': (
        'f4c88928302ab15b3634431e4956187b24f5b0072e943193a82f8b156d97d8'
        'c0'
    ),
    'catalogue-outgoing-10': (
        '7b6c25df9de074a2ee3215925211bdb3789529671555efb561e16554b2de59'
        '6a'
    ),
    'catalogue-outgoing-11': (
        'dce317bae568331bedfb21ba951b998ac7239b20ccf9b5d235a70d6512dc20'
        '77'
    ),
    'catalogue-outgoing-12': (
        'd3cef70b50e6cd803d98a9e0833d3951a86eaa3276a94d2684d26b28756612'
        '2e'
    ),
    'catalogue-outgoing-13': (
        '8d772c996f3bb9abdf3bd1a7608bc2c492503e5f939e54451870a07caec3b6'
        'bd'
    ),
    'catalogue-outgoing-14': (
        'b33ebdc57347b1b16cf41576fc0e0ef83de0508916d003f173fe3fbbbd6732'
        'b5'
    ),
    'catalogue-outgoing-15': (
        'c40d6b478514c7617817c882dc8cd5294e90ec173337e3655b86310a6821c2'
        '5b'
    ),
    'catalogue-outgoing-16': (
        'b8c3c3766dd1cfe8802c6c16000d8435c1e47bc35bc7434ed8cfd7eaa119ed'
        'aa'
    ),
    'catalogue-outgoing-17': (
        '72d7750ea49f80f6fbbe22b1807909c8ddf082f26f7aafc6648cb82e14a152'
        '7f'
    ),
    'catalogue-outgoing-18': (
        '3ebad35a5b5443b29ffe0e4e136fd9c06e2ea2e9e6d96266e99cc4d207de02'
        '96'
    ),
    'catalogue-outgoing-19': (
        '3721175586c62a971eba73fdf5e0bdddc22f755fe12db5b14f354313b94557'
        '9b'
    ),
    'catalogue-outgoing-20': (
        'b0c9554268df5c3e28aa64c85f40eb78570d4b70a88c589592589c3406376e'
        'd7'
    ),
    'catalogue-outgoing-21': (
        '04cb750af679e85d2330387b5d3c40cb1770a04c01ba0d10a32a930ee55897'
        '28'
    ),
    'catalogue-outgoing-22': (
        '2528e7b1c6e7fe24a30ba36d40efc130ac649acaa756e9ece224d744c5d7b8'
        'f7'
    ),
    'catalogue-outgoing-23': (
        '96e5640541a8e0ad16e1f305260c7b8a6563af6d85f9745b0db643bc31cc6d'
        'b9'
    ),
    'catalogue-compatriots-1': (
        '73da5f0d41d8c20ee62d9a9800b1378df1cfc02922fde802ab5ab9a866d874'
        '5f'
    ),
    'document-d1cc0983a316': (
        '823dd6d23f8d265ab61cd87d965f52300e0b967f49375dacaea4fd95b52625'
        'e1'
    ),
    'document-02273471f86e': (
        '287ddc83668a3f8f6b8e47be9cb19fad641a05e69cbb58fc76d687a2108d2f'
        'c3'
    ),
    'document-6b56bd1c25a8': (
        '7b1eadb1c07f784e40a169ca8035ccc86ce48030ecaec599980a1b52bf99c7'
        '1e'
    ),
    'document-c1c59a621ca2': (
        '8c3f6f9e4b96d4c09148dd9bde515ed9cdc23d40cfdff6adfa9e75f76741f0'
        '99'
    ),
    'document-c90ee4d8f41f': (
        '2f341a1410faa2184d8f669c107369af3f66f597dd07894aa16b527392ef81'
        '39'
    ),
    'document-dddbf0187368': (
        '86967f5b7e0f90b6a527868a5533387823358d999cc3405476f0bc148513d0'
        '58'
    ),
    'document-2bfa4b35bc26': (
        'ef0fcea51ea6f9d9785001a162b2fb7fac3eb6dc3fd880337e6b7b6ce2dd98'
        '64'
    ),
    'document-a822bc320810': (
        'ffcadb2d14f77eea0993b31e8f5b293b21c32fcb7cec23fc5874ed57fff039'
        'e3'
    ),
    'document-59d56122d1c1': (
        '0c2c8485213cb3edf5db70107205af80cc439bcec3c6c02bb84977969dfe7a'
        '39'
    ),
    'document-20f2b7744c73': (
        '8646aba0a618c88d185159f77ce110cade5ea782314d4cad63202083898c68'
        'b3'
    ),
    'document-4806bff37922': (
        '2c0ea5ddcc343dbfdb43b3cc1723be4c8743747723be474dd2105af06a327b'
        '34'
    ),
    'document-3a2289b7ba89': (
        'defdba5a17dea2c4e34da549718cf4e302575c80d3a3c7cdc56c5ad0472475'
        'f1'
    ),
    'document-080f5ea25650': (
        '64cc7fabbee72d38048f363d986278a9b9d852b3b70032f7752952766624aa'
        '91'
    ),
}

PROGRAMME_IDS = [
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    9,
    10,
    11,
    12,
    13,
    14,
    15,
    16,
    17,
    18,
    19,
    20,
    21,
    22,
    23,
    24,
    25,
    26,
    27,
    28,
    29,
    30,
    31,
    32,
    33,
    34,
    35,
    40,
    41,
    42,
    43,
    45,
    46,
    47,
    48,
    49,
    50,
    51,
    52,
    53,
    54,
    55,
    56,
    57,
    59,
    60,
    61,
    62,
    63,
    64,
    65,
    66,
    67,
    68,
    69,
    70,
    71,
    72,
    73,
    74,
    75,
    76,
    77,
    78,
    79,
    80,
    81,
    82,
    83,
    84,
    85,
    86,
    87,
    88,
    89,
    90,
    91,
    92,
    93,
    94,
    95,
    96,
    97,
    98,
    99,
    100,
    101,
    102,
    103,
    104,
    105,
    106,
    107,
    108,
    109,
    111,
    114,
    115,
    116,
    117,
    118,
    120,
    122,
    124,
    130,
    131,
    134,
    136,
    137,
    138,
    139,
    142,
    144,
    145,
    146,
    148,
    149,
    150,
    151,
    152,
    153,
    154,
    155,
    157,
    159,
    160,
    163,
    164,
    165,
    166,
    167,
    168,
    169,
    170,
    171,
    172,
    173,
    174,
    175,
    176,
    177,
    178,
    179,
    180,
    181,
    182,
    183,
    184,
    185,
    186,
    187,
    188,
    189,
    190,
    191,
    192,
    193,
    194,
    195,
    196,
    197,
    198,
    199,
    200,
    201,
    202,
    203,
    204,
    205,
    206,
    207,
    208,
    209,
    210,
    211,
    212,
    213,
    214,
    215,
    216,
    217,
    218,
    219,
    220,
    221,
    222,
    223,
    224,
    225,
    226,
    227,
    228,
    229,
    230,
    231,
    232,
    233,
    234,
    235,
    236,
    237,
    238,
    239,
    240,
    241,
    242,
    243,
    244,
    245,
    246,
    247,
    248,
    249,
    250,
    251,
    252,
    253,
    254,
    255,
    256,
    257,
    258,
    259,
    260,
    261,
    263,
    264,
    265,
    266,
    267,
    268,
    269,
    270,
    271,
    273,
    274,
    275,
    276,
    277,
    278,
    279,
    280,
    281,
    282,
    283,
    284,
    285,
    286,
    287,
    288,
    289,
    290,
    291,
    292,
    293,
    294,
    295,
    296,
    297,
    298,
    299,
    300,
    301,
    332,
    333,
    334,
    335,
    337,
    339,
    340,
    341,
    342,
    343,
    344,
]



# Original factual programme projections.
PROFILES = [
    {
        'programme': 1,
        'title': (
            'Albania: Summer courses in Albanian language for '
            'university students and teachers'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=1'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['AL'],
        'eligible': [],
        'deadline': '2027-02-19',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Albanian language summer school for university students '
            'and teachers: one month, with course activities, '
            'accommodation and meals provided in kind. No cash stipend '
            'is stated. The source lists institutional travel '
            'reimbursement.'
        ),
        'evidence': [
            (
                'The published September 1–30 course lasts one month. '
                'An English motivation letter and academic transcript '
                'are required. Travel reimbursement is listed '
                'separately from in-kind support; its application '
                'conditions remain those of the published offer.'
            ),
            (
                'Programme 1, published call 468: application opening '
                '2026-10-01; closing 2027-02-19. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 2,
        'title': (
            "Albania: Study stays for students in Bachelor's, "
            "Master's, and Doctoral programmes"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=2'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['AL'],
        'eligible': [],
        'deadline': '2027-02-19',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Study in Albania for BA, MA and doctoral students, '
            'lasting 2–9 months. Monthly support is CZK 12,000 for '
            'BA/MA students and CZK 13,000 for doctoral students; '
            'travel reimbursement is listed. Host acceptance follows '
            'Czech nomination.'
        ),
        'evidence': [
            (
                'Applicants select a university suitable for their '
                'field and submit an English motivation letter, study '
                'plan and academic transcript. Czech nomination is '
                'followed by a host acceptance letter; the foreign '
                'partner makes the final admission decision.'
            ),
            (
                'Programme 2, published call 226: application opening '
                '2026-10-01; closing 2027-02-19. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 3,
        'title': (
            'Albania: Research and teaching stays for university '
            'teachers'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=3'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['AL'],
        'eligible': [],
        'deadline': '2027-02-19',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Research or teaching in Albania for academic staff '
            'affiliated with a university in Czechia: up to one month, '
            'with CZK 13,000 monthly support and listed travel '
            'reimbursement. University affiliation is distinct from '
            'citizenship.'
        ),
        'evidence': [
            (
                'The academic-staff offer requires a motivation letter '
                'and research plan. Sending-university affiliation is '
                'published; no Czech passport requirement is '
                'established by that affiliation.'
            ),
            (
                'Programme 3, published call 467: application opening '
                '2026-10-01; closing 2027-02-19. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 4,
        'title': (
            'Bulgaria: Summer courses in Bulgarian language for '
            'university students and teachers of philology'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=4'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['BG'],
        'eligible': [],
        'deadline': '2027-02-19',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Bulgarian summer language courses for up to three weeks. '
            'Course activities, accommodation and meals are provided '
            'in kind, with no cash stipend; the source lists travel '
            'reimbursement. Two universities are host choices within '
            'the same offer.'
        ),
        'evidence': [
            (
                'Host universities are course options, subject to the '
                'nomination and application requirements of the '
                'published call.'
            ),
            (
                'Programme 4, published call 469: application opening '
                '2026-10-01; closing 2027-02-19. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 5,
        'title': (
            "Bulgaria: Study stays for students in Bachelor's, "
            "Master's, and Doctoral programmes"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=5'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['BG'],
        'eligible': [],
        'deadline': '2027-02-19',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Study in Bulgaria for BA, MA and doctoral students: 2–9 '
            'months. Published monthly support is CZK 12,000 for BA/MA '
            'and CZK 13,000 for doctoral students. Travel '
            'reimbursement is listed; host acceptance follows '
            'nomination.'
        ),
        'evidence': [
            (
                'The application requires the published motivation, '
                'study-plan and academic records. A foreign acceptance '
                'letter is obtained after nomination; nomination does '
                'not guarantee host admission.'
            ),
            (
                'Programme 5, published call 470: application opening '
                '2026-10-01; closing 2027-02-19. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 6,
        'title': (
            'Bulgaria: Research and teaching stays for university '
            'teachers'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=6'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['BG'],
        'eligible': [],
        'deadline': '2027-02-19',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Academic-staff research or teaching in Bulgaria for 1–3 '
            'months, for staff affiliated with a university in '
            'Czechia. The host institution pays support; amount and '
            'frequency are unspecified. Travel reimbursement is listed.'
        ),
        'evidence': [
            (
                'A motivation letter and research plan are required. A '
                'host invitation is recommended rather than an initial '
                'mandatory attachment.'
            ),
            (
                'Programme 6, published call 471: application opening '
                '2026-10-01; closing 2027-02-19. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 7,
        'title': (
            'China: Summer courses in Chinese language for teachers of '
            'Chinese at Masaryk University'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=7'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['CN'],
        'eligible': [],
        'deadline': '2026-07-10',
        'opening': '2026-07-01',
        'uncertain': False,
        'summary': (
            'Chinese-language teacher training in Shanghai, August '
            '2–15, 2026. Two-week course activities and accommodation '
            'are provided in kind; no cash stipend is stated, and '
            'travel reimbursement is listed. Masaryk, Charles and '
            'Palacký universities have separate nomination channels.'
        ),
        'evidence': [
            (
                'All three channels describe the same offline '
                'Chinese-language course. Charles University '
                'additionally requires a Czech or English motivation '
                'letter. The respective institutional quotas and '
                'application calls remain channel-specific.'
            ),
            (
                'Programme 7, published call 452: application opening '
                '2026-07-01; closing 2026-07-10. Closing dates have no '
                'verified timezone.'
            ),
            (
                'Programme 8, published call 453: application opening '
                '2026-07-01; closing 2026-07-10. Closing dates have no '
                'verified timezone.'
            ),
            (
                'Nomination channel: https://studyin.gov.cz/en/scholars'
                'hips/scholarship-detail/?id=8'
            ),
            (
                'Programme 9, published call 454: application opening '
                '2026-07-01; closing 2026-07-10. Closing dates have no '
                'verified timezone.'
            ),
            (
                'Nomination channel: https://studyin.gov.cz/en/scholars'
                'hips/scholarship-detail/?id=9'
            ),
        ],
    },
    {
        'programme': 10,
        'title': (
            'China: Study stays for sinology students of Masaryk '
            'University'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=10'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CN'],
        'eligible': [],
        'deadline': '2026-12-01',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Study in China for 5–10 months, through university '
            'nomination or an open channel. Monthly support is CZK '
            '12,000 for BA/MA and CZK 13,000 for doctoral students; '
            'travel reimbursement is listed. Application documents '
            'differ by channel.'
        ),
        'evidence': [
            (
                'Masaryk, Charles and Palacký university channels and '
                'the open channel are retained. Charles requires '
                'additional motivation and academic-performance '
                'documentation, with the MA transcript for doctoral '
                'applicants. The open channel requires a CV, study '
                'plan and transcript; host acceptance and HSK evidence '
                'are optional listed attachments.'
            ),
            (
                'Programme 10, published call 474: application opening '
                '2026-10-01; closing 2026-12-01. Closing dates have no '
                'verified timezone.'
            ),
            (
                'Programme 11, published call 475: application opening '
                '2026-10-01; closing 2026-12-01. Closing dates have no '
                'verified timezone.'
            ),
            (
                'Nomination channel: https://studyin.gov.cz/en/scholars'
                'hips/scholarship-detail/?id=11'
            ),
            (
                'Programme 12, published call 476: application opening '
                '2026-10-01; closing 2026-12-01. Closing dates have no '
                'verified timezone.'
            ),
            (
                'Nomination channel: https://studyin.gov.cz/en/scholars'
                'hips/scholarship-detail/?id=12'
            ),
            (
                'Programme 13, published call 477: application opening '
                '2026-10-01; closing 2026-12-01. Closing dates have no '
                'verified timezone.'
            ),
            (
                'Nomination channel: https://studyin.gov.cz/en/scholars'
                'hips/scholarship-detail/?id=13'
            ),
        ],
    },
    {
        'programme': 14,
        'title': (
            'Egypt: Study stays for students of Arabic language in '
            "Bachelor's and Master's study programmes at Charles "
            'University'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=14'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['EG'],
        'eligible': [],
        'deadline': '2024-12-08',
        'opening': '2024-10-21',
        'uncertain': False,
        'summary': (
            'Five-month Arabic study in Egypt for BA/MA students '
            'through university nomination channels. Published support '
            'is EGP 550 per month plus accommodation; a possible EGP '
            '250 accommodation contribution has no verified payment '
            'frequency. Travel reimbursement is listed.'
        ),
        'evidence': [
            (
                'Charles University and the University of West Bohemia '
                'describe the same funded course, with separate '
                'nomination channels and published historical calls. '
                'The accommodation contribution’s payment frequency is '
                'unspecified.'
            ),
            (
                'Programme 14, published call 14: application opening '
                '2024-10-21; closing 2024-12-08. Closing dates have no '
                'verified timezone.'
            ),
            (
                'Programme 15, published call 15: application opening '
                '2024-10-21; closing 2024-12-08. Closing dates have no '
                'verified timezone.'
            ),
            (
                'Nomination channel: https://studyin.gov.cz/en/scholars'
                'hips/scholarship-detail/?id=15'
            ),
        ],
    },
    {
        'programme': 16,
        'title': (
            'Egypt: Study stays for university teachers and scholars'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=16'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['EG'],
        'eligible': [],
        'deadline': '2024-12-08',
        'opening': '2024-10-21',
        'uncertain': False,
        'summary': (
            'Academic-staff research or teaching in Egypt for 1–5 '
            'months, with EGP 800 monthly support and accommodation. A '
            'possible EGP 400 accommodation contribution has '
            'unspecified frequency; travel reimbursement is listed.'
        ),
        'evidence': [
            (
                'Two signed references, a diploma, CV, motivation '
                'letter and study plan are required. The published '
                'call code OUT/EG/STU conflicts with the '
                'academic-staff audience stated in the programme.'
            ),
            (
                'Programme 16, published call 16: application opening '
                '2024-10-21; closing 2024-12-08. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 17,
        'title': 'Egypt: Study stays for students in doctoral programmes',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=17'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['EG'],
        'eligible': [],
        'deadline': '2024-12-08',
        'opening': '2024-10-21',
        'uncertain': False,
        'summary': (
            'Ten-month doctoral study in Egypt, with EGP 800 monthly '
            'support and accommodation. A possible EGP 400 '
            'accommodation contribution has unspecified frequency. The '
            'narrative and payment table differ on which country funds '
            'the offer.'
        ),
        'evidence': [
            (
                'Two signed references and a student-status '
                'certificate are required. The narrative identifies '
                'Egyptian funding while the table assigns payment to '
                'the sending country; both claims are retained.'
            ),
            (
                'Programme 17, published call 17: application opening '
                '2024-10-21; closing 2024-12-08. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 18,
        'title': (
            'Georgia: Language and subject-specific summer courses for '
            'university students and teachers'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=18'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['GE'],
        'eligible': [],
        'deadline': '2027-01-15',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Georgian summer courses lasting 1–4 weeks. The narrative '
            'describes in-kind support without a cash stipend, while '
            'the table lists GEL 250 with no payment frequency. The '
            'older narrative and the dated portal call also refer to '
            'different years.'
        ),
        'evidence': [
            (
                'The narrative still awaits 2025 course details, '
                'whereas the selected call closes January 15, 2027. '
                'Neither a monthly cash payment nor updated event '
                'dates are established by this conflict.'
            ),
            (
                'Programme 18, published call 478: application opening '
                '2026-10-01; closing 2027-01-15. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 19,
        'title': (
            "Georgia: Study stays for students in Bachelor's, Master's "
            'and Doctoral programmes'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=19'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['GE'],
        'eligible': [],
        'deadline': '2026-01-16',
        'opening': '2025-10-01',
        'uncertain': False,
        'summary': (
            'Study in Georgia for BA, MA and doctoral students, '
            'lasting 2–9 months with GEL 800 monthly support and '
            'listed travel reimbursement. The narrative includes '
            'several degree levels while structured entries repeat '
            'bachelor-level study.'
        ),
        'evidence': [
            (
                'The degree-level discrepancy is retained rather than '
                'restricting all applicants to the repeated table '
                'entry. The published selected application period is '
                'historical.'
            ),
            (
                'Programme 19, published call 182: application opening '
                '2025-10-01; closing 2026-01-16. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 20,
        'title': (
            'Georgia: Research and teaching stays for university '
            'teachers'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=20'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['GE'],
        'eligible': [],
        'deadline': '2027-01-29',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Research or teaching in Georgia for academic staff at a '
            'university in Czechia, for up to one month. Published '
            'support is GEL 800 monthly, with travel reimbursement '
            'listed. Host acceptance follows nomination.'
        ),
        'evidence': [
            (
                'University affiliation is the published '
                'academic-staff condition; it does not establish a '
                'Czech citizenship requirement.'
            ),
            (
                'Programme 20, published call 480: application opening '
                '2026-10-01; closing 2027-01-29. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 21,
        'title': (
            'Croatia: Summer courses in Croatian language for students '
            'and teachers of Masaryk University'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=21'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['HR'],
        'eligible': [],
        'deadline': '2027-02-19',
        'opening': '2027-10-01',
        'uncertain': True,
        'summary': (
            'Two-week Croatian summer language course for '
            'Croatian/Slavic students or qualified teachers: B1 '
            'minimum, B2 preferred, with course, meals and '
            'accommodation in kind and no travel reimbursement. '
            'Masaryk and Charles nomination channels have conflicting '
            'application-opening dates.'
        ),
        'evidence': [
            (
                'Masaryk call 472 opens October 1, 2027 but closes '
                'February 19, 2027, so its end precedes its start. '
                'Charles call 473 opens October 1, 2026 and closes '
                'February 19, 2027. The conflict is not repaired by '
                'transferring the Charles opening to the Masaryk '
                'channel.'
            ),
            (
                'Programme 21, published call 472: application opening '
                '2027-10-01; closing 2027-02-19. Closing dates have no '
                'verified timezone.'
            ),
            (
                'Programme 22, published call 473: application opening '
                '2026-10-01; closing 2027-02-19. Closing dates have no '
                'verified timezone.'
            ),
            (
                'Nomination channel: https://studyin.gov.cz/en/scholars'
                'hips/scholarship-detail/?id=22'
            ),
        ],
    },
    {
        'programme': 23,
        'title': (
            'Japan: Scholarships for completing entire university '
            'studies for high school graduates and first-year '
            'university students (Undergraduate Students Programme)'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=23'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['JP'],
        'eligible': ['CZ'],
        'deadline': '2026-06-01',
        'opening': '2026-04-27',
        'uncertain': False,
        'summary': (
            'MEXT undergraduate study in Japan for Czech citizens: 5–7 '
            'years with JPY 117,000 monthly support and round-trip '
            'travel. The source has conflicting birth-date thresholds. '
            'Portal closing and paper delivery are separate: June 1, '
            '2026, with paper delivery by 16:30 (timezone unstated).'
        ),
        'evidence': [
            (
                'The table requires birth after April 1, 2002 while '
                'the narrative states April 2, 2001. The later '
                'programme start is an event date, not an application '
                'deadline. The paper-delivery clock does not supply a '
                'verified online-closing timezone.'
            ),
            (
                'Programme 23, published call 445: application opening '
                '2026-04-27; closing 2026-06-01. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 24,
        'title': (
            'Japan: Scholarships for completing entire higher '
            'vocational study programmes (Specialized Training College '
            'Students Programme)'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=24'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['JP'],
        'eligible': ['CZ'],
        'deadline': '2026-06-01',
        'opening': '2026-04-27',
        'uncertain': False,
        'summary': (
            'MEXT vocational study in Japan for Czech citizens: up to '
            'three years, with JPY 117,000 monthly support and '
            'round-trip travel. The narrative April 2026 start differs '
            'from the 2026/27 call. Paper delivery is June 1, 2026 by '
            '16:30, with no stated timezone.'
        ),
        'evidence': [
            (
                'The published birth-date threshold is April 2002. The '
                'portal provides a date-only June 1, 2026 closing, '
                'separate from the paper-delivery clock.'
            ),
            (
                'Programme 24, published call 447: application opening '
                '2026-04-27; closing 2026-06-01. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 25,
        'title': (
            "Japan: Study stays for university graduates of Bachelor's "
            "programmes, and students and graduates of Master's and "
            'Doctoral programmes (Research Students Programme)'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=25'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['JP'],
        'eligible': ['CZ'],
        'deadline': '2026-06-01',
        'opening': '2026-04-27',
        'uncertain': False,
        'summary': (
            'MEXT postgraduate study in Japan for Czech citizens, '
            'lasting 18–24 months. Monthly amounts are JPY 143,000 for '
            'preparation, 144,000 for MA and 145,000 for doctoral '
            'study. Paper delivery is June 1, 2026 by 16:30; timezone '
            'is unspecified.'
        ),
        'evidence': [
            (
                'The birth-date threshold after 1992 conflicts with '
                'the narrative age limit of 30. The online closing is '
                'date-only, and the paper-delivery time is a distinct '
                'requirement.'
            ),
            (
                'Programme 25, published call 446: application opening '
                '2026-04-27; closing 2026-06-01. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 26,
        'title': (
            'South Korea: Summer course in Korean language for '
            "students in Bachelor's, Master's, and Doctoral Programmes"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=26'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['KR'],
        'eligible': ['CZ'],
        'deadline': '2026-04-20',
        'opening': '2026-03-24',
        'uncertain': False,
        'summary': (
            'Korean summer courses for Czech citizens, lasting 2–4 '
            'weeks with in-kind support and listed travel '
            'reimbursement. The portal closes April 20, 2026, while '
            'the linked course guidance is a historical 2024 edition; '
            'its benefits and dates do not establish a new 2026 offer.'
        ),
        'evidence': [
            (
                'The 2024 guide requires Czech citizenship and Czech '
                'university affiliation and states historical April '
                '1/17 application dates. Its GPA condition is 80% or '
                'the top 20%. Food support is KRW 7,000 per meal, '
                'three meals daily, only when meals are not provided; '
                'the online-course benefit covers tuition only.'
            ),
            (
                'Programme 26, published call 431: application opening '
                '2026-03-24; closing 2026-04-20. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 27,
        'title': (
            "South Korea: Study stays for students in Master's and "
            "Doctoral programmes and graduates of Bachelor's and "
            "Master's programmes"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=27'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['KR'],
        'eligible': ['CZ'],
        'deadline': '2026-02-24',
        'opening': '2026-02-02',
        'uncertain': False,
        'summary': (
            'Graduate GKS study in Korea: the own portal lists 3–4 '
            'years and KRW 1,380,000 monthly, while required guidance '
            'is the 2025 edition with different annual allowances and '
            'age dates. Czech selected participants receive no '
            'round-trip airfare under that guide.'
        ),
        'evidence': [
            (
                'The 2025 guide requires both applicant and parents to '
                'lack Korean citizenship, including Korean dual '
                'nationality; ordinarily applicants are under 40, with '
                'a specified ODA academic exception. Academic '
                'performance must be at least 80%, the top 20%, or a '
                'listed equivalent GPA. Its annual allowances are KRW '
                '14,160,000 for language, 15,720,000 for degree study '
                'and 19,800,000 for research, not a derived monthly '
                'amount. The own 2026 portal and historical 2025 guide '
                'are different editions. Language certificates are '
                'optional at submission subject to university '
                'requirements; TOPIK 3 is required for progression, '
                'with stated exemptions and potentially higher '
                'university requirements. Tuition limits and language '
                'support vary by track; research has no degree tuition '
                'or language-training benefit. Withdrawal and '
                'university-change restrictions apply.'
            ),
            (
                'Programme 27, published call 413: application opening '
                '2026-02-02; closing 2026-02-24. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 29,
        'title': (
            'Latvia: Study stays for university students in '
            "Bachelor's, Master's, and Doctoral Programmes"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=29'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['LV'],
        'eligible': [],
        'deadline': '2027-04-01',
        'opening': '2026-11-01',
        'uncertain': False,
        'summary': (
            'Study in Latvia for 2–10 months, with EUR 500 monthly for '
            'BA/MA and EUR 700 for doctoral students. Repeat support '
            'is limited to twice at the same degree level; travel '
            'reimbursement is listed. The published application period '
            'is November 1, 2026–April 1, 2027.'
        ),
        'evidence': [
            (
                'The country-of-application entry is not a citizenship '
                'requirement. The opening date is separate from the '
                'closing date.'
            ),
            (
                'Programme 29, published call 481: application opening '
                '2026-11-01; closing 2027-04-01. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 30,
        'title': (
            'Latvia: Subject- and field-specific summer courses in '
            'English for university students and teachers'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=30'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['LV'],
        'eligible': [],
        'deadline': '2027-04-01',
        'opening': '2026-11-01',
        'uncertain': False,
        'summary': (
            'Latvian summer courses lasting 1–4 weeks, with course '
            'support in kind and no cash stipend; travel reimbursement '
            'is listed. The narrative organizer period is yearless '
            'February 1–April 1, while the portal gives November 1, '
            '2026–April 1, 2027.'
        ),
        'evidence': [
            (
                'Direct organizer applications and the portal '
                'nomination period are separate stages. Individual '
                'universities are course choices within the funding '
                'framework.'
            ),
            (
                'Programme 30, published call 482: application opening '
                '2026-11-01; closing 2027-04-01. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 31,
        'title': (
            'Hungary: Summer courses in Hungarian language for '
            'university students and teachers'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=31'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['HU'],
        'eligible': [],
        'deadline': '2027-01-29',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Hungarian summer courses for up to one month, with '
            'in-kind support, no cash stipend and listed travel '
            'reimbursement. Czech nomination is followed by host '
            'registration through Tempus; the portal closes January '
            '29, 2027.'
        ),
        'evidence': [
            (
                'A Czech nomination does not itself complete the '
                'separate host-registration requirement. Host '
                'registration and its documents are a separate stage '
                'governed by the host’s instructions.'
            ),
            (
                'Programme 31, published call 513: application opening '
                '2026-10-01; closing 2027-01-29. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 32,
        'title': (
            'Hungary: Study stays for university students in doctoral '
            'programmes'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=32'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['HU'],
        'eligible': [],
        'deadline': '2027-01-22',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Doctoral study in Hungary for up to nine months, with CZK '
            '13,000 monthly support and listed travel reimbursement. '
            'Motivation, study plan and academic transcript are '
            'required; host acceptance is obtained for the second '
            'application phase.'
        ),
        'evidence': [
            (
                'Nomination and final host acceptance are separate '
                'steps, with the published portal period retained as '
                'the application calendar.'
            ),
            (
                'Programme 32, published call 483: application opening '
                '2026-10-01; closing 2027-01-22. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 33,
        'title': (
            "Hungary: Study stays for students in Bachelor's and "
            "Master's programmes"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=33'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['HU'],
        'eligible': [],
        'deadline': '2027-01-22',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Five-month study in Hungary for BA/MA students, with CZK '
            '12,000 monthly support and listed travel reimbursement. '
            'Motivation, study plan and transcript are required; host '
            'acceptance follows nomination.'
        ),
        'evidence': [
            (
                'The second application stage requires host '
                'acceptance. The doctoral offer has different '
                'qualifications, duration and funding.'
            ),
            (
                'Programme 33, published call 484: application opening '
                '2026-10-01; closing 2027-01-22. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 34,
        'title': (
            'Hungary: 1–2 week research stays for university teachers'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=34'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['HU'],
        'eligible': [],
        'deadline': '2027-01-22',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'One- to two-week academic research in Hungary for staff '
            'at a university in Czechia. A one-time host payment is '
            'listed, with amount unspecified; travel reimbursement is '
            'listed.'
        ),
        'evidence': [
            (
                'Sending-university employment or academic affiliation '
                'is distinct from citizenship. The payment frequency '
                'is one-time rather than monthly.'
            ),
            (
                'Programme 34, published call 485: application opening '
                '2026-10-01; closing 2027-01-22. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 35,
        'title': (
            'Hungary: 1–3 month research stays for university teachers'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=35'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['HU'],
        'eligible': [],
        'deadline': '2027-01-22',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Academic research in Hungary for university staff '
            'affiliated in Czechia, lasting 1–3 months. The host '
            'country pays support, with amount and frequency '
            'unspecified; travel reimbursement is listed.'
        ),
        'evidence': [
            (
                'The offer describes an academic-staff research stay, '
                'separate from the shorter one- to two-week research '
                'offer.'
            ),
            (
                'Programme 35, published call 486: application opening '
                '2026-10-01; closing 2027-01-22. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 40,
        'title': (
            'Mexico: Study and research stays for students in '
            "Bachelor's, Master's, and Doctoral programmes"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=40'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['MX'],
        'eligible': [],
        'deadline': '2027-04-16',
        'opening': '2027-01-01',
        'uncertain': False,
        'summary': (
            'Study scholarships in Mexico for up to 12 months. Host '
            'funding is stated without a published amount; travel is '
            'not reimbursed. Czech public-university students use '
            'Czech nomination followed by SIGCA; the published portal '
            'period is January 1–April 16, 2027.'
        ),
        'evidence': [
            (
                'Other applicants follow a route outside the Czech '
                'portal. Spanish-language and minimum-age requirements '
                'apply as published; the minimum age is 18. Nomination '
                'and final provider acceptance are separate.'
            ),
            (
                'Programme 40, published call 487: application opening '
                '2027-01-01; closing 2027-04-16. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 41,
        'title': (
            'Mongolia: Summer courses for university students in '
            'Mongolian studies'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=41'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['MN'],
        'eligible': [],
        'deadline': '2027-01-29',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Mongolian summer courses for up to one month, with course '
            'support in kind, no cash stipend and listed travel '
            'reimbursement. Mongolian A2 is required; named '
            'disciplinary interests do not form an exclusive field '
            'restriction.'
        ),
        'evidence': [
            (
                'The published course is a language-training offer, '
                'with disciplinary focus distinguished from mandatory '
                'language eligibility.'
            ),
            (
                'Programme 41, published call 488: application opening '
                '2026-10-01; closing 2027-01-29. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 42,
        'title': (
            'Mongolia: Study and research stays for university teachers'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=42'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['MN'],
        'eligible': [],
        'deadline': '2027-01-29',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Academic-staff stays in Mongolia for up to one month, '
            'with accommodation, a meal allowance and pocket money at '
            'unspecified national rates. Travel reimbursement is '
            'listed, and host acceptance is required.'
        ),
        'evidence': [
            (
                'Applicants are staff affiliated with a university in '
                'Czechia. The source does not establish a fixed '
                'pocket-money amount or a Czech passport requirement.'
            ),
            (
                'Programme 42, published call 489: application opening '
                '2026-10-01; closing 2027-01-29. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 43,
        'title': (
            "Mongolia: Study stays for students in Bachelor's, "
            "Master's, and Doctoral programmes"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=43'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['MN'],
        'eligible': [],
        'deadline': '2027-01-29',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Study in Mongolia for BA/MA students, lasting 2–9 months. '
            'Monthly payment follows national legislation, with amount '
            'unspecified; travel reimbursement is listed. The call '
            'title says 2027/2029 while its academic-year field says '
            '2027/2028.'
        ),
        'evidence': [
            (
                'The selected portal closing is January 29, 2027. The '
                'title includes doctoral students, while the '
                'description names BA/MA students. Both this audience '
                'discrepancy and the conflicting academic-year labels '
                'are retained.'
            ),
            (
                'Programme 43, published call 490: application opening '
                '2026-10-01; closing 2027-01-29. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 45,
        'title': (
            'Germany: BTHA Scholarships – postgradual study stays in '
            'Bavaria'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=45'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        (
            "eligible"
        ): [(
            "BG"
        ), (
            "HR"
        ), (
            "CZ"
        ), (
            "HU"
        ), (
            "PL"
        ), (
            "RO"
        ), (
            "RU"
        ), (
            "RS"
        ), (
            "SK"
        ), (
            "UA"
        )],
        'deadline': '2026-12-01',
        'opening': '2026-09-01',
        'uncertain': False,
        'summary': (
            'Postgraduate study or doctoral research in Bavaria. The '
            '2027/28 guide expects EUR 992 monthly, with conditional '
            'family support; applicants must hold a listed citizenship '
            'and reside in their home country. Ordinary age limits are '
            'under 30 for MA or under 35 for PhD, with justified '
            'exceptions.'
        ),
        'evidence': [
            (
                'The explicit citizenship list is Bulgaria, Croatia, '
                'Czechia, Hungary, Poland, Romania, Russia, Serbia, '
                'Slovakia and Ukraine. The Czech travel-refund channel '
                'additionally concerns Czech-university students or '
                'graduates. The guide requires strong academic '
                'results, financial need and appropriate language '
                'proficiency, normally C1; residence and study in '
                'Bavaria are required during support. Expected EUR 992 '
                'is paid in 12 monthly installments; requested EUR 160 '
                'monthly family support may depend on budget-year '
                'timing. Support normally lasts one year and may be '
                'renewed twice; a justified Bavarian doctoral '
                'exception allows a further one or two semesters. '
                'Home-university doctoral research is supported for '
                'one year. With agreement, additional income averaging '
                'at most EUR 800 gross monthly and another scholarship '
                'below EUR 450 monthly are permitted. The provider '
                'supplies no travel grant; this differs from the '
                'separate Czech-university reimbursement route. '
                'Pending degrees are due by July 31, 2027 and doctoral '
                'host-supervisor acceptance is required. The '
                'application closes December 1, 2026, date-only.'
            ),
            (
                'Programme 45, published call 463: application opening '
                '2026-09-01; closing 2026-12-01. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 46,
        'title': (
            'Germany: Seminar for German teachers in Buchenbach '
            '(Baden-Württemberg)'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=46'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2023-09-12',
        'opening': '2023-08-14',
        'uncertain': False,
        'summary': (
            'One-week professional training for German-speaking '
            'teachers. The published funding amount is unspecified; '
            'travel reimbursement is listed. The retained application '
            'closing is historical: September 12, 2023.'
        ),
        'evidence': [
            (
                'The teacher-training offer is retained as a '
                'historical programme with its published application '
                'route; no new opening or cash amount is inferred.'
            ),
            (
                'Programme 46, published call 46: application opening '
                '2023-08-14; closing 2023-09-12. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 47,
        'title': (
            'Germany: Seminar for German teachers in Meißen (Saxony)'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=47'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2026-04-30',
        'opening': '2026-03-20',
        'uncertain': False,
        'summary': (
            'Twelve-day teacher seminar in Meissen, with course, meals '
            'and accommodation provided and no cash stipend. DZS '
            'group-car travel is covered. A professional CV in German '
            'is required; the published closing is April 30, 2026.'
        ),
        'evidence': [
            (
                'The offer addresses German-speaking teachers or '
                'relevant employees. Travel support describes the '
                'organized group journey, rather than unrestricted '
                'individual reimbursement.'
            ),
            (
                'Programme 47, published call 434: application opening '
                '2026-03-20; closing 2026-04-30. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 48,
        'title': (
            'Germany: DAAD Scholarships – Summer courses for '
            'university students'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=48'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2025-10-30',
        'opening': '2025-09-01',
        'uncertain': False,
        'summary': (
            'German summer courses for students, lasting 3–4 weeks. '
            'Amount and funding entitlement are unspecified on the own '
            'offer. Its selected closing is October 30, 2025, while '
            'the required course list contains 2027 options; that list '
            'does not establish a new scholarship call.'
        ),
        'evidence': [
            (
                'The own source lists travel reimbursement. The course '
                'list includes language, subject, date and provider '
                'choices; advertised extra services are not '
                'automatically included in the scholarship. The list '
                'states that funding benefits are those in the award '
                'call.'
            ),
            (
                'Programme 48, published call 187: application opening '
                '2025-09-01; closing 2025-10-30. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 49,
        'title': (
            'Germany: DAAD Scholarships – Postgraduate studies in '
            'architecture, urbanism, monument care, and related '
            'disciplines'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=49'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2026-09-24',
        'opening': '2026-08-20',
        'uncertain': False,
        'summary': (
            'DAAD graduate study in the published engineering/arts '
            'fields, lasting 10–24 months with EUR 992 monthly '
            'support. Travel reimbursement is listed; the closing is '
            'September 24, 2026.'
        ),
        'evidence': [
            (
                'The linked travel table effective January 1, 2026 '
                'states EUR 211 for a Czech outward-and-return journey '
                'or EUR 106 one-way, subject to the relevant award '
                'conditions; EUR 211 is not payable for each direction.'
            ),
            (
                'Programme 49, published call 462: application opening '
                '2026-08-20; closing 2026-09-24. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 50,
        'title': (
            'Germany: DAAD Scholarships – Postgraduate studies in music'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=50'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2026-09-24',
        'opening': '2026-08-20',
        'uncertain': False,
        'summary': (
            'DAAD arts graduate study for 10–24 months. The own '
            'payment table states EUR 992 one-time, unlike monthly '
            'amounts in other DAAD offers. Travel reimbursement is '
            'listed; the published closing is September 24, 2026.'
        ),
        'evidence': [
            (
                'The own one-time frequency is retained rather than '
                'replaced by another award’s monthly rate. The linked '
                'travel table states EUR 211 round trip or EUR 106 '
                'one-way for the Czech row, subject to this award’s '
                'travel rules.'
            ),
            (
                'Programme 50, published call 461: application opening '
                '2026-08-20; closing 2026-09-24. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 51,
        'title': (
            'Germany: DAAD Scholarships – Postgraduate studies in '
            'performing arts'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=51'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2025-11-10',
        'opening': '2025-09-01',
        'uncertain': False,
        'summary': (
            'DAAD graduate study lasting 10–24 months, with EUR 992 '
            'monthly support and listed travel reimbursement. The '
            'retained published closing is November 10, 2025.'
        ),
        'evidence': [
            (
                'Qualification and subject conditions remain those of '
                'the named offer; the historical closing is not rolled '
                'into a new annual call.'
            ),
            (
                'Programme 51, published call 190: application opening '
                '2025-09-01; closing 2025-11-10. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 52,
        'title': (
            'Germany: DAAD Scholarships – Postgraduate studies in fine '
            'arts, design, visual communication, film'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=52'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2025-11-10',
        'opening': '2025-09-01',
        'uncertain': False,
        'summary': (
            'DAAD arts graduate study with EUR 992 monthly support. '
            'Travel support is unspecified. The duration is '
            'unspecified in the own offer; the published closing is '
            'November 10, 2025.'
        ),
        'evidence': [
            (
                'No duration is transferred from another DAAD award.'
            ),
            (
                'Programme 52, published call 191: application opening '
                '2025-09-01; closing 2025-11-10. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 53,
        'title': (
            'Germany: DAAD Scholarships – Study visits for academics '
            'in arts and architecture'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=53'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2026-07-31',
        'opening': '2026-05-01',
        'uncertain': False,
        'summary': (
            'DAAD academic or teacher stays for 1–3 months. Host '
            'monthly funding is stated without an amount; travel '
            'reimbursement is listed. The published closing is July '
            '31, 2026, distinct from the February 2027 start.'
        ),
        'evidence': [
            (
                'The programme start is an event date rather than a '
                'later application deadline.'
            ),
            (
                'Programme 53, published call 450: application opening '
                '2026-05-01; closing 2026-07-31. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 54,
        'title': (
            'Germany: DAAD Scholarships – Master studies in all '
            'academic disciplines'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=54'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2025-11-17',
        'opening': '2025-09-01',
        'uncertain': False,
        'summary': (
            'DAAD study for 10–24 months, with EUR 992 monthly support '
            'and listed travel reimbursement. The own qualification '
            'field is blank; the published closing is November 17, '
            '2025.'
        ),
        'evidence': [
            (
                'The blank qualification field does not establish an '
                'unrestricted application entitlement; the original '
                'offer supplies the application route.'
            ),
            (
                'Programme 54, published call 193: application opening '
                '2025-09-01; closing 2025-11-17. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 55,
        'title': (
            'Germany: DAAD Scholarships – Double degree programmes for '
            'doctoral students'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=55'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2025-11-17',
        'opening': '2025-09-01',
        'uncertain': False,
        'summary': (
            'DAAD research support for 7–24 months, with EUR 1,300 '
            'monthly and listed travel reimbursement. The published '
            'closing is November 17, 2025.'
        ),
        'evidence': [
            (
                'The duration and monthly amount are those of this '
                'named offer, without transferring another DAAD '
                'award’s rates.'
            ),
            (
                'Programme 55, published call 194: application opening '
                '2025-09-01; closing 2025-11-17. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 56,
        'title': (
            'Germany: DAAD Scholarships – Research grants for doctoral '
            'students'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=56'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2026-07-31',
        'opening': None,
        'uncertain': False,
        'summary': (
            'DAAD student research for 2–12 months, with EUR 1,400 '
            'monthly and listed travel reimbursement. The closing is '
            'July 31, 2026 and the opening is unspecified. The '
            'selected call title says February 2027 while its body '
            'says February 2026 and later.'
        ),
        'evidence': [
            (
                'Both conflicting start-year claims are retained. A '
                'missing opening is not replaced with an assumed May '
                'date.'
            ),
            (
                'Programme 56, published call 451: application opening '
                'unspecified; closing 2026-07-31. Closing dates have '
                'no verified timezone.'
            ),
        ],
    },
    {
        'programme': 57,
        'title': (
            'Germany: DAAD Scholarships – Research grants for '
            'post-docs in all academic disciplines'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=57'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2025-11-17',
        'opening': '2025-09-01',
        'uncertain': False,
        'summary': (
            'DAAD graduate research for 2–6 months, with EUR 1,400 '
            'monthly support and listed travel reimbursement. The '
            'published closing is November 17, 2025.'
        ),
        'evidence': [
            (
                'The offer’s degree and research qualifications apply '
                'through its original application route.'
            ),
            (
                'Programme 57, published call 196: application opening '
                '2025-09-01; closing 2025-11-17. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 59,
        'title': (
            'Germany: DAAD Scholarships – Re-invitation programme for '
            'former scholarship holders (Alumni)'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=59'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2026-07-31',
        'opening': '2026-05-01',
        'uncertain': False,
        'summary': (
            'DAAD research stays for graduates, teachers, students or '
            'employees, lasting 1–3 months. Host payment is stated '
            'without a verified amount or frequency. The closing is '
            'July 31, 2026; the February 2027 start is separate.'
        ),
        'evidence': [
            (
                'A programme start does not replace the earlier '
                'application closing.'
            ),
            (
                'Programme 59, published call 449: application opening '
                '2026-05-01; closing 2026-07-31. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 60,
        'title': (
            'Germany: Seminar for German teachers in Dillingen an der '
            'Donau (Bavaria)'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=60'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2026-01-22',
        'opening': '2026-01-14',
        'uncertain': False,
        'summary': (
            'Five-day professional training for German-speaking '
            'teachers. The source does not state a cash amount; travel '
            'reimbursement is listed. The published closing is January '
            '22, 2026.'
        ),
        'evidence': [
            (
                'A not-applicable attachment entry is not proof that '
                'the programme has no participant requirements.'
            ),
            (
                'Programme 60, published call 411: application opening '
                '2026-01-14; closing 2026-01-22. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 61,
        'title': (
            'Germany: Observation internships for teachers at schools '
            'in Bavaria'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=61'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2026-03-23',
        'opening': '2026-03-02',
        'uncertain': False,
        'summary': (
            'Two-week school observation in Bavaria for teachers at '
            'Czech primary, secondary or vocational schools with '
            'German. EUR 375 is a one-time partial subsidy; '
            'participants fund travel and subsistence, and host '
            'insurance is not provided.'
        ),
        'evidence': [
            (
                'School-director approval and the signed German StMUK '
                'form are required. The published closing is March 23, '
                '2026. This partial subsidy does not promise full '
                'reimbursement of the stay.'
            ),
            (
                'Programme 61, published call 420: application opening '
                '2026-03-02; closing 2026-03-23. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 62,
        'title': (
            "Peru: Study and research stays for students in Master's "
            'and Doctoral programmes at the Czech University of Life '
            'Sciences Prague studying forestry or agriculture with a '
            'special focus on agroforestry'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=62'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['PE'],
        'eligible': [],
        'deadline': '2026-11-20',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Study at Ucayali in Peru for MA/PhD students at the Czech '
            'University of Life Sciences in relevant forestry or '
            'agricultural fields: 3–9 months, PEN 2,000 monthly, '
            'without travel reimbursement. The call’s DE code '
            'conflicts with the actual Peruvian host.'
        ),
        'evidence': [
            (
                'Spanish CV with photo, motivation, study plan, two '
                'signed home-university references in Spanish, '
                'transcript and Spanish diploma translation are '
                'required. The published closing is November 20, 2026.'
            ),
            (
                'Programme 62, published call 491: application opening '
                '2026-10-01; closing 2026-11-20. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 63,
        'title': (
            'Poland: Summer courses in Polish language and culture for '
            'university students and teachers'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=63'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['PL'],
        'eligible': [],
        'deadline': '2026-01-16',
        'opening': '2025-11-01',
        'uncertain': False,
        'summary': (
            'Three-week Polish language courses for students and '
            'teachers of Polish or Polish-focused Central European '
            'studies. In-kind course support is stated without a cash '
            'stipend, and travel reimbursement is listed. The '
            'programme offers onsite and online course options.'
        ),
        'evidence': [
            (
                'Five onsite and three online variants are choices '
                'within the scholarship framework. The programme table '
                'says three weeks, but the selected call lists a July '
                '6–August 2, 2026 course at John Paul II Catholic '
                'University, alongside three-week courses; the actual '
                'option duration differs from the table. The published '
                'closing is January 16, 2026.'
            ),
            (
                'Programme 63, published call 240: application opening '
                '2025-11-01; closing 2026-01-16. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 64,
        'title': 'Poland: Study and research stays for academic staff',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=64'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['PL'],
        'eligible': [],
        'deadline': '2027-02-12',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Academic-staff research in Poland for 1–7 months, with '
            'CZK 13,000 monthly support and listed travel '
            'reimbursement. A two-page research plan and second-stage '
            'host acceptance through NAWA are required.'
        ),
        'evidence': [
            (
                'Applicants are affiliated with a university in '
                'Czechia. The October 1 event start is separate from '
                'the February 12, 2027 portal closing.'
            ),
            (
                'Programme 64, published call 492: application opening '
                '2026-10-01; closing 2027-02-12. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 65,
        'title': (
            'Poland: Study and research stays for students in '
            "Bachelor's, Master's, and Doctoral programmes"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=65'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['PL'],
        'eligible': [],
        'deadline': '2027-02-12',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Study in Poland for BA, MA and doctoral students, lasting '
            '2–10 months. Monthly support is CZK 12,000 for BA/MA or '
            'CZK 13,000 for doctoral study; travel reimbursement is '
            'listed. The published closing is February 12, 2027.'
        ),
        'evidence': [
            (
                'The quota is 80 funded months, including 40 reserved '
                'for Polish studies, rather than 80 individual awards. '
                'The older call’s academic-year field conflicts with '
                'its title.'
            ),
            (
                'Programme 65, published call 493: application opening '
                '2026-10-01; closing 2027-02-12. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 66,
        'title': (
            'Romania: Summer courses in Romanian language for '
            'university students and teachers'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=66'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['RO'],
        'eligible': [],
        'deadline': '2027-10-19',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'One-month Romanian summer courses, with course '
            'activities, accommodation and meals provided in kind and '
            'no cash stipend. Travel reimbursement is listed. The '
            'published closing is October 19, 2027.'
        ),
        'evidence': [
            (
                'The October 2027 closing is retained as published, '
                'without changing it to match neighboring programmes.'
            ),
            (
                'Programme 66, published call 502: application opening '
                '2026-10-01; closing 2027-10-19. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 67,
        'title': (
            'Romania: Study and teaching stays for university teachers'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=67'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['RO'],
        'eligible': [],
        'deadline': '2027-02-19',
        'opening': '2025-10-01',
        'uncertain': False,
        'summary': (
            'Academic-staff stays in Romania for up to one month, with '
            'CZK 13,000 monthly support and listed travel '
            'reimbursement. The call opens October 1, 2025 and closes '
            'February 19, 2027.'
        ),
        'evidence': [
            (
                'The published opening year is retained, rather than '
                'transferred from neighboring 2026 openings. '
                'Sending-university affiliation does not establish '
                'citizenship.'
            ),
            (
                'Programme 67, published call 500: application opening '
                '2025-10-01; closing 2027-02-19. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 68,
        'title': (
            "Romania: Study stays for students in Bachelor's, "
            "Master's, and Doctoral programmes"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=68'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['RO'],
        'eligible': [],
        'deadline': '2027-02-19',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Study in Romania for BA, MA and doctoral students, '
            'lasting 2–9 months. Monthly support is CZK 12,000 for '
            'BA/MA and CZK 13,000 for doctoral students; travel '
            'reimbursement is listed.'
        ),
        'evidence': [
            (
                'The published portal closing is February 19, 2027. '
                'Degree-specific amounts are not a universal flat '
                'payment.'
            ),
            (
                'Programme 68, published call 501: application opening '
                '2026-10-01; closing 2027-02-19. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 69,
        'title': (
            'Greece: Study and teaching stays for university teachers'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=69'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['GR'],
        'eligible': [],
        'deadline': '2026-01-14',
        'opening': '2025-11-01',
        'uncertain': False,
        'summary': (
            'Academic-staff visits to Greece for 5–7 days, with up to '
            'EUR 110 per day for at most seven days. Payment is one '
            'retrospective installment after return and the required '
            'post-return documents; travel reimbursement is listed. '
            'Host nomination must precede the stay by at least two '
            'months.'
        ),
        'evidence': [
            (
                'Applicants must work at a Czech public university. A '
                'host invitation, CV with publications and research '
                'plan are required. The portal closing is January 14, '
                '2026; support is not promised as an advance payment.'
            ),
            (
                'Programme 69, published call 227: application opening '
                '2025-11-01; closing 2026-01-14. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 70,
        'title': (
            'North Macedonia: Summer courses in Macedonian language '
            'for students and teachers of Masaryk University'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=70'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['MK'],
        'eligible': [],
        'deadline': '2027-02-19',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Three-week summer course in Ohrid, North Macedonia, '
            'through Masaryk or Charles university nomination '
            'channels. Course support is provided in kind without a '
            'cash stipend, and travel reimbursement is listed.'
        ),
        'evidence': [
            (
                'The Charles channel’s selected call code says MUNI '
                'while its programme and historical channel identify '
                'Charles University. This code discrepancy does not '
                'replace the actual channel affiliation. Both '
                'published closings are February 19, 2027.'
            ),
            (
                'Programme 70, published call 503: application opening '
                '2026-10-01; closing 2027-02-19. Closing dates have no '
                'verified timezone.'
            ),
            (
                'Programme 71, published call 504: application opening '
                '2026-10-01; closing 2027-02-19. Closing dates have no '
                'verified timezone.'
            ),
            (
                'Nomination channel: https://studyin.gov.cz/en/scholars'
                'hips/scholarship-detail/?id=71'
            ),
        ],
    },
    {
        'programme': 72,
        'title': (
            'North Macedonia: Study and research stays for university '
            'teachers and researchers'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=72'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['MK'],
        'eligible': [],
        'deadline': '2027-02-19',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Academic-staff stays in North Macedonia for 1–10 days, '
            'with accommodation, meals and approximately EUR 70 pocket '
            'money per month as literally stated. Travel reimbursement '
            'is listed; no daily EUR 70 rate is established.'
        ),
        'evidence': [
            (
                'The monthly pocket-money unit is retained despite the '
                'shorter stay duration; the source does not guarantee '
                'payment of a full month’s amount. The closing is '
                'February 19, 2027.'
            ),
            (
                'Programme 72, published call 505: application opening '
                '2026-10-01; closing 2027-02-19. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 73,
        'title': (
            "North Macedonia: Study stays for students in Bachelor's "
            "and Master's programmes"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=73'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['MK'],
        'eligible': [],
        'deadline': '2027-02-19',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Study in North Macedonia for BA/MA students, lasting 2–10 '
            'months with accommodation, meals and approximately EUR 70 '
            'monthly pocket money. Travel reimbursement is listed.'
        ),
        'evidence': [
            'The published closing is February 19, 2027.',
            (
                'Programme 73, published call 506: application opening '
                '2026-10-01; closing 2027-02-19. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 74,
        'title': (
            'North Macedonia: Study stays for students in Doctoral '
            'programmes'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=74'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['MK'],
        'eligible': [],
        'deadline': '2027-02-19',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Doctoral study in North Macedonia for 2–10 months, with '
            'accommodation, meals and approximately EUR 70 monthly '
            'pocket money. Travel reimbursement is listed.'
        ),
        'evidence': [
            (
                'The doctoral audience is stated separately from the '
                'BA/MA offer. The published closing is February 19, '
                '2027.'
            ),
            (
                'Programme 74, published call 507: application opening '
                '2026-10-01; closing 2027-02-19. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 75,
        'title': (
            'Slovakia: Summer courses in Slovak language and culture '
            'for students and teachers of Masaryk University'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=75'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['SK'],
        'eligible': [],
        'deadline': '2027-02-12',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Slovak SAS summer courses for up to one month through '
            'Masaryk or Charles nomination channels, with in-kind '
            'support, no cash stipend and listed travel reimbursement. '
            'Czech nomination is followed by a registration-code step.'
        ),
        'evidence': [
            (
                'The published portal closing is February 12, 2027. '
                'April/end-May organizer steps are stated without a '
                'year and remain separate from the dated portal '
                'closing.'
            ),
            (
                'Programme 75, published call 496: application opening '
                '2026-10-01; closing 2027-02-12. Closing dates have no '
                'verified timezone.'
            ),
            (
                'Programme 76, published call 497: application opening '
                '2026-10-01; closing 2027-02-12. Closing dates have no '
                'verified timezone.'
            ),
            (
                'Nomination channel: https://studyin.gov.cz/en/scholars'
                'hips/scholarship-detail/?id=76'
            ),
        ],
    },
    {
        'programme': 77,
        'title': (
            'Slovakia: Stays for university teachers and researchers'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=77'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['SK'],
        'eligible': [],
        'deadline': '2027-04-30',
        'opening': '2026-11-01',
        'uncertain': False,
        'summary': (
            'Two-week academic or employee stays in Slovakia, with '
            'accommodation, board and an unspecified allowance but no '
            'cash scholarship. Travel reimbursement is listed. The '
            'published application period is November 1, 2026–April '
            '30, 2027.'
        ),
        'evidence': [
            (
                'Two paper application sets must be posted by the '
                'closing; this is a posting requirement rather than a '
                'verified receipt deadline.'
            ),
            (
                'Programme 77, published call 494: application opening '
                '2026-11-01; closing 2027-04-30. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 78,
        'title': (
            'Slovakia: Study stays for university students studying in '
            'Doctoral programmes'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=78'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['SK'],
        'eligible': [],
        'deadline': '2027-02-12',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Doctoral study in Slovakia for 3–10 months, with EUR '
            '1,025 monthly support and listed travel reimbursement. '
            'Online submission and two paper application sets are '
            'required.'
        ),
        'evidence': [
            'The published closing is February 12, 2027.',
            (
                'Programme 78, published call 512: application opening '
                '2026-10-01; closing 2027-02-12. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 79,
        'title': (
            'Slovakia: Study stays for university students studying in '
            "Master's programmes"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=79'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['SK'],
        'eligible': [],
        'deadline': '2027-02-12',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Master’s study in Slovakia for 3–10 months, with EUR 620 '
            'monthly support and listed travel reimbursement. Online '
            'submission and two paper application sets are required.'
        ),
        'evidence': [
            'The published closing is February 12, 2027.',
            (
                'Programme 79, published call 495: application opening '
                '2026-10-01; closing 2027-02-12. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 80,
        'title': (
            'Slovenia: Summer seminar on Slovenian language for '
            'students and teachers of Masaryk University'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=80'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['SI'],
        'eligible': [],
        'deadline': '2027-02-19',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Two-week Slovenian summer seminar in Ljubljana through '
            'Masaryk or Charles nomination channels. Basic Slovenian '
            'and a language test and online interview are required; '
            'support includes course activities, meals and '
            'accommodation in kind, with no cash or travel '
            'reimbursement.'
        ),
        'evidence': [
            (
                'A motivation letter and transcript are required. Both '
                'published channel calls close February 19, 2027. The '
                'seminar’s language test is not transferred to the '
                'separately advertised summer school.'
            ),
            (
                'Programme 80, published call 508: application opening '
                '2026-10-01; closing 2027-02-19. Closing dates have no '
                'verified timezone.'
            ),
            (
                'Programme 81, published call 509: application opening '
                '2026-10-01; closing 2027-02-19. Closing dates have no '
                'verified timezone.'
            ),
            (
                'Nomination channel: https://studyin.gov.cz/en/scholars'
                'hips/scholarship-detail/?id=81'
            ),
        ],
    },
    {
        'programme': 82,
        'title': (
            'Slovenia: Summer school of Slovenian language for '
            'students and teachers of Masaryk University'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=82'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['SI'],
        'eligible': [],
        'deadline': '2027-02-19',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Two-week Slovenian language school in Ljubljana for '
            'students and teachers nominated by Masaryk or Charles '
            'University. Funding covers classes, activities, '
            'accommodation and meals, with no cash allowance or travel '
            'reimbursement. Both nomination channels list 19 February '
            '2027 as the application deadline.'
        ),
        'evidence': [
            (
                'Masaryk and Charles University each nominate '
                'participants through their rectorate; selected '
                'candidates must also register with the Slovenian '
                'host. No seminar-specific language-test requirement '
                'is stated for this summer-school offer.'
            ),
            (
                'Programme 82, published call 510: application opening '
                '2026-10-01; closing 2027-02-19. Closing dates have no '
                'verified timezone.'
            ),
            (
                'Programme 83, published call 511: application opening '
                '2026-10-01; closing 2027-02-19. Closing dates have no '
                'verified timezone.'
            ),
            (
                'Nomination channel: https://studyin.gov.cz/en/scholars'
                'hips/scholarship-detail/?id=83'
            ),
        ],
    },
    {
        'programme': 84,
        'title': (
            'Switzerland: Stays for doctoral students in all academic '
            'fields – Swiss Government Excellence Scholarships '
            'Programme'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=84'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CH'],
        'eligible': [],
        'deadline': '2026-11-16',
        'opening': '2026-08-20',
        'uncertain': False,
        'summary': (
            'Swiss doctoral research scholarship for graduates and '
            'students, for up to 12 months, with CHF 2450 monthly and '
            'no travel reimbursement. The portal specifies birth after '
            '31 December 1990. Applications for 2027/28 close on 16 '
            'November 2026 through GO ESKAS, with a research plan and '
            'Swiss supervisor support.'
        ),
        'evidence': [
            (
                'GO ESKAS requires a complete CV, motivation letter of '
                'up to two pages, FCS research proposal of up to five '
                'pages, Swiss supervisor support and supervisor CV of '
                'up to two pages, and two academic referees. Degree '
                'records need certified translations unless in '
                'English, French, Italian or German. Graduate-school '
                'admission confirmation and Swiss residence evidence '
                'apply only to the specified applicant groups. Email '
                'and paper applications are not accepted.'
            ),
            (
                'Passport identification pages are required; '
                'applicants with dual citizenship provide both '
                'passports.'
            ),
            (
                'Programme 84, published call 459: application opening '
                '2026-08-20; closing 2026-11-16. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 85,
        'title': (
            'Switzerland: Study stays in MA programmes for fine art '
            'students – Swiss Government Excellence Scholarships '
            'programme)'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=85'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CH'],
        'eligible': [],
        'deadline': '2025-11-17',
        'opening': '2025-09-01',
        'uncertain': False,
        'summary': (
            'Swiss fine-art master’s study scholarship: 12 months, '
            'with a listed extension of up to 21 months, CHF 1920 '
            'monthly and no travel reimbursement. Birth after 31 '
            'December 1990 is specified. Applications closed on 17 '
            'November 2025; the call title says 2025/26 while its '
            'academic-year field says 2026/27.'
        ),
        'evidence': [
            (
                'The listed stay lasts twelve months and the separate '
                'extension field allows up to twenty-one months; no '
                'amount or eligibility is borrowed from the doctoral '
                'scholarship.'
            ),
            (
                'Programme 85, published call 238: application opening '
                '2025-09-01; closing 2025-11-17. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 86,
        'title': (
            'Ukraine: Scholarships for completing full university '
            "studies in Bachelor's, Master’s, and Doctoral programmes"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=86'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['UA'],
        'eligible': ['CZ'],
        'deadline': '2026-03-15',
        'opening': '2025-11-01',
        'uncertain': False,
        'summary': (
            'Full bachelor’s, master’s or doctoral study in Ukraine '
            'for Czech citizens, including an initial '
            'Ukrainian-language preparation year. Minimum duration is '
            'two years. Monthly support is at least UAH 2000 for '
            'bachelor’s/master’s students or UAH 7500 for doctoral '
            'students; travel reimbursement is available. Online and '
            'Ukrainian-translated paper documents are required. '
            'Deadline: 15 March 2026.'
        ),
        'evidence': [
            (
                'The paper dossier includes the application form, '
                'highest qualification with diploma supplement and '
                'transcript, passport identification pages and '
                'Ukrainian translations. Doctoral applicants add a '
                'research-topic description or proposal of 500–1000 '
                'words. Successful bachelor’s and master’s candidates '
                'start by 30 September, doctoral candidates by 31 '
                'October; these are study-start limits rather than '
                'application deadlines.'
            ),
            (
                'Programme 86, published call 239: application opening '
                '2025-11-01; closing 2026-03-15. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 87,
        'title': (
            'AKTION: Scholarships for participation in summer colleges '
            'for university students from Czechia'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=87'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-10-15',
        'opening': '2026-01-01',
        'uncertain': False,
        'summary': (
            'AKTION language colleges in České Budějovice and '
            'Poděbrady bring together Czech and Austrian university '
            'students. The Czech public-university cohort requires '
            'German B1–C1, prioritises first-time participants and '
            'lists a 15 October 2026 deadline. The Austrian cohort’s '
            'published call closed on 1 June 2025. Applications go to '
            'the organising university; amounts, housing and duration '
            'are unspecified.'
        ),
        'evidence': [
            (
                'Students apply directly to the organising university. '
                'Repeat participation is allowed, but first-time '
                'AKTION language-school participants have priority in '
                'the Czech cohort. The Austrian cohort has a separate '
                '1 March–1 June 2025 calendar; Czech-cohort German '
                'B1–C1 and public-university conditions do not '
                'establish an identical Austrian recruitment rule.'
            ),
            (
                'Programme 87, published call 515: application opening '
                '2026-01-01; closing 2026-10-15. Closing dates have no '
                'verified timezone.'
            ),
            (
                'Programme 95, published call 95: application opening '
                '2025-03-01; closing 2025-06-01. Closing dates have no '
                'verified timezone.'
            ),
            (
                'Nomination channel: https://studyin.gov.cz/en/scholars'
                'hips/scholarship-detail/?id=95'
            ),
        ],
    },
    {
        'programme': 88,
        'title': (
            'AKTION: Semester and short-term scholarships for '
            'university students from Czechia in Austria'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=88'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['AT'],
        'eligible': [],
        'deadline': '2026-10-15',
        'opening': '2026-07-15',
        'uncertain': False,
        'summary': (
            'AKTION study in Austria for master’s students affiliated '
            'with universities in Czechia: one to five months, without '
            'extension. Required documents include two Czech academic '
            'recommendations, Austrian supervisor approval, '
            'registration, diploma supplement and transcript. Funding '
            'amount is unspecified. Application deadline: 15 October '
            '2026.'
        ),
        'evidence': [
            (
                'Programme 88, published call 88: application opening '
                '2026-07-15; closing 2026-10-15. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 89,
        'title': (
            'AKTION: 1–3 month research and teaching stays in Austria '
            'for academic staff from Czechia'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=89'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['AT'],
        'eligible': [],
        'deadline': '2026-10-15',
        'opening': '2026-03-16',
        'uncertain': False,
        'summary': (
            'AKTION research or teaching in Austria for academic staff '
            'from universities in Czechia: one to three months, '
            'without extension. A diploma supplement, two Czech '
            'academic recommendations, registration and Austrian '
            'supervisor approval are required. The funding amount is '
            'not stated. Applications close on 15 October 2026.'
        ),
        'evidence': [
            (
                'Programme 89, published call 516: application opening '
                '2026-03-16; closing 2026-10-15. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 90,
        'title': 'AKTION: One-month stays in Austria for academic staff',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=90'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['AT'],
        'eligible': [],
        'deadline': '2026-10-15',
        'opening': '2026-07-15',
        'uncertain': False,
        'summary': (
            'AKTION one-month academic stay in Austria, without '
            'extension. Applicants need their home department head’s '
            'recommendation, registration and Austrian academic '
            'supervisor approval; reports from earlier AKTION stays '
            'are optional. The source does not state a funding amount. '
            'Deadline: 15 October 2026.'
        ),
        'evidence': [
            (
                'Programme 90, published call 90: application opening '
                '2026-07-15; closing 2026-10-15. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 91,
        'title': (
            'AKTION: Habilitation scholarships for academic staff from '
            'Czechia'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=91'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['AT'],
        'eligible': [],
        'deadline': '2027-03-15',
        'opening': '2027-01-01',
        'uncertain': False,
        'summary': (
            'AKTION habilitation support for academic staff from '
            'Czechia: one to five months in Austria, without '
            'extension. Applicants must be within ten years of their '
            'doctorate and have feasible academic goals. Home '
            'leadership recommendation, publication list and Austrian '
            'supervisor approval are required. Applications run from 1 '
            'January to 15 March 2027; the amount is unspecified.'
        ),
        'evidence': [
            (
                'Programme 91, published call 517: application opening '
                '2027-01-01; closing 2027-03-15. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 92,
        'title': (
            'AKTION: Semester and short-term scholarships for '
            'university students from Austria in Czechia'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=92'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-10-15',
        'opening': '2026-07-15',
        'uncertain': False,
        'summary': (
            'AKTION study in Czechia for students from Austrian '
            'universities: one to five months, without extension. Two '
            'Austrian academic recommendations, Czech supervisor '
            'approval and academic records are required. The amount is '
            'not stated. Applications close on 15 October 2026.'
        ),
        'evidence': [
            (
                'Programme 92, published call 92: application opening '
                '2026-07-15; closing 2026-10-15. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 93,
        'title': (
            'AKTION: 1–3 month research and teaching stays in Czechia '
            'for academic staff from Austria'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=93'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-10-15',
        'opening': '2026-07-15',
        'uncertain': False,
        'summary': (
            'AKTION research and teaching visits to Czechia for '
            'academic staff from Austrian universities: one to three '
            'months, without extension. A diploma supplement, two '
            'Austrian academic recommendations and Czech supervisor '
            'approval are required. Funding amount is unspecified. '
            'Applications close on 15 October 2026.'
        ),
        'evidence': [
            (
                'Programme 93, published call 93: application opening '
                '2026-07-15; closing 2026-10-15. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 94,
        'title': (
            'AKTION: Scholarships for participation in Summer Schools '
            'of Slavonic Studies in Czechia for university students '
            'and academic staff from Austria'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=94'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2024-06-01',
        'opening': '2024-03-01',
        'uncertain': False,
        'summary': (
            'AKTION funding to attend Summer Schools of Slavonic '
            'Studies in Czechia for students and academic staff from '
            'Austria. The published application period was 1 March–1 '
            'June 2024. This entry does not specify an award amount or '
            'duration.'
        ),
        'evidence': [
            (
                'Programme 94, published call 94: application opening '
                '2024-03-01; closing 2024-06-01. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 96,
        'title': 'AKTION: One-month stays in Czechia for academic staff',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=96'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-10-15',
        'opening': '2026-07-15',
        'uncertain': False,
        'summary': (
            'AKTION one-month academic visit to Czechia, without '
            'extension. Home department leadership recommendation and '
            'Czech supervisor approval are required; reports of '
            'earlier AKTION stays are optional. The funding amount is '
            'unspecified. Deadline: 15 October 2026.'
        ),
        'evidence': [
            (
                'Programme 96, published call 96: application opening '
                '2026-07-15; closing 2026-10-15. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 97,
        'title': (
            'AKTION: Habilitation scholarships for academic staff from '
            'Austria'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=97'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-03-15',
        'opening': '2026-01-01',
        'uncertain': False,
        'summary': (
            'Habilitation stay in Czechia for academic staff from '
            'Austria: one to five months. The portal lists CZK 27000 '
            'but does not specify payment frequency. Applications '
            'closed on 15 March 2026. The programme is labelled AKTION '
            'while the call title refers to bilateral-agreement '
            'ministry scholarships.'
        ),
        'evidence': [
            (
                'Programme 97, published call 97: application opening '
                '2026-01-01; closing 2026-03-15. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 98,
        'title': (
            'Barrande Fellowship Programme: Short-term stays /host '
            'country: France/'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=98'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['FR'],
        'eligible': [],
        'deadline': '2026-01-29',
        'opening': '2025-10-31',
        'uncertain': False,
        'summary': (
            'Barrande short doctoral research visit to France for '
            'students at Czech universities: one to three consecutive '
            'months. Funding is EUR 1704 monthly or CZK 40000 monthly '
            'under the stated sending-university route. Doctoral '
            'enrolment and both supervisors’ approval are required. '
            'Travel and accommodation are self-funded. Deadline: 29 '
            'January 2026.'
        ),
        'evidence': [
            (
                'All scientific fields are eligible; environment, '
                'energy, mathematics and IT are encouraged rather than '
                'exclusive fields. Both supervisors must approve '
                'English for the stay. Applications require an English '
                'CV, cover letter, project, academic transcript and '
                'signed recommendations or acceptance from both '
                'supervisors. The selection interview is twenty '
                'minutes with a seventy-percent threshold, without a '
                'guaranteed award. French housing assistance may be '
                'available but is not guaranteed.'
            ),
            (
                'Applicants must be affiliated with a Czech '
                'higher-education institution, and French citizens are '
                'excluded from the France-bound scholarship.'
            ),
            (
                'Programme 98, published call 245: application opening '
                '2025-10-31; closing 2026-01-29. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 99,
        'title': (
            'Barrande Fellowship Programme: doctoral degree study '
            'under the joint supervision /host country: France/'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=99'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['FR'],
        'eligible': [],
        'deadline': '2026-01-29',
        'opening': '2025-10-31',
        'uncertain': False,
        'summary': (
            'Barrande cotutelle doctorate in France for applicants '
            'affiliated with Czech universities. Funding is EUR 1770 '
            'monthly for five months per year, for up to three years, '
            'with fifteen funded months in total. A master’s degree by '
            'the first stay, enrolment at both universities and a '
            'signed cotutelle agreement are required. Travel and '
            'housing are self-funded. Deadline: 29 January 2026.'
        ),
        'evidence': [
            (
                'All scientific fields are eligible; environment, '
                'energy, mathematics and IT are encouraged rather than '
                'exclusive fields. Both supervisors must approve '
                'English for the stay. Applications require an English '
                'CV, cover letter, project, academic transcript and '
                'signed recommendations or acceptance from both '
                'supervisors. The selection interview is twenty '
                'minutes with a seventy-percent threshold, without a '
                'guaranteed award. French housing assistance may be '
                'available but is not guaranteed.'
            ),
            (
                'Second-year master’s students may apply, but must '
                'obtain the degree before the first funded stay and '
                'satisfy doctoral enrolment at both institutions and '
                'the signed cotutelle agreement. Five funded months '
                'per year over at most three years does not mean '
                'fifteen uninterrupted months.'
            ),
            (
                'Applicants must be affiliated with a Czech '
                'higher-education institution, and French citizens are '
                'excluded from the France-bound scholarship.'
            ),
            (
                'Programme 99, published call 246: application opening '
                '2025-10-31; closing 2026-01-29. Closing dates have no '
                'verified timezone.'
            ),
        ],
    },
    {
        'programme': 100,
        'title': (
            'Barrande Fellowship Programme: Short-term stays /host '
            'country: Czechia/'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=100'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-01-29',
        'opening': '2025-10-31',
        'uncertain': False,
        'summary': (
            'Barrande short doctoral research visit to Czechia for '
            'students affiliated with French universities: one to '
            'three consecutive months, with CZK 35000 monthly. '
            'Doctoral enrolment and both supervisors’ approval are '
            'required. Hosts are Czech public universities or '
            'affiliated research centres. Travel and accommodation are '
            'self-funded. Deadline: 29 January 2026.'
        ),
        'evidence': [
            (
                'All scientific fields are eligible; environment, '
                'energy, mathematics and IT are encouraged rather than '
                'exclusive fields. Both supervisors must approve '
                'English for the stay. Applications require an English '
                'CV, cover letter, project, academic transcript and '
                'signed recommendations or acceptance from both '
                'supervisors. The selection interview is twenty '
                'minutes with a seventy-percent threshold, without a '
                'guaranteed award.'
            ),
            (
                'Applicants must be affiliated with a French '
                'higher-education institution. The Czech host must be '
                'a public university or, for the short stay, an '
                'affiliated research centre; affiliation is not a '
                'general nationality requirement.'
            ),
            (
                'Programme 100, published call 247: application '
                'opening 2025-10-31; closing 2026-01-29. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 101,
        'title': (
            'Barrande Fellowship Programme: doctoral degree study '
            'under the joint supervision /host country: Czechia/'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=101'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-01-29',
        'opening': '2025-10-31',
        'uncertain': False,
        'summary': (
            'Barrande cotutelle doctorate in Czechia for applicants '
            'affiliated with French universities: CZK 35000 monthly '
            'for five months per year, up to three years and fifteen '
            'funded months in total. A master’s degree by the first '
            'stay, dual enrolment and signed cotutelle agreement are '
            'required. Travel and housing are self-funded. Deadline: '
            '29 January 2026.'
        ),
        'evidence': [
            (
                'All scientific fields are eligible; environment, '
                'energy, mathematics and IT are encouraged rather than '
                'exclusive fields. Both supervisors must approve '
                'English for the stay. Applications require an English '
                'CV, cover letter, project, academic transcript and '
                'signed recommendations or acceptance from both '
                'supervisors. The selection interview is twenty '
                'minutes with a seventy-percent threshold, without a '
                'guaranteed award.'
            ),
            (
                'Second-year master’s students may apply, but must '
                'obtain the degree before the first funded stay and '
                'satisfy doctoral enrolment at both institutions and '
                'the signed cotutelle agreement. Five funded months '
                'per year over at most three years does not mean '
                'fifteen uninterrupted months.'
            ),
            (
                'Applicants must be affiliated with a French '
                'higher-education institution. The Czech host must be '
                'a public university; affiliation is not a general '
                'nationality requirement.'
            ),
            (
                'Programme 101, published call 248: application '
                'opening 2025-10-31; closing 2026-01-29. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 102,
        'title': 'ČEJKA: Czech language course for compatriots',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=102'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-04-02',
        'opening': '2026-02-05',
        'uncertain': False,
        'summary': (
            'ČEJKA four-week Czech-language course in Poděbrady for '
            'adults aged 18 or over who promote Czech culture abroad; '
            'association membership is not required. Tuition, meals '
            'and accommodation are covered. Transport support is CZK '
            '2000 for European countries or CZK 5000 for non-European '
            'countries. Deadline: 2 April 2026.'
        ),
        'evidence': [
            (
                'Programme 102, published call 404: application '
                'opening 2026-02-05; closing 2026-04-02. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 103,
        'title': 'ČEJKA: Course in teaching Czech as a foreign language',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=103'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-04-22',
        'opening': '2026-02-05',
        'uncertain': False,
        'summary': (
            'ČEJKA two-week training in teaching Czech in Prague for '
            'adults with good or very good Czech; association '
            'membership is not required. Course, meals and '
            'accommodation are covered, with conditional travel and '
            'personal support. Deadline: 22 April 2026. The call is '
            'labelled 2026 but prints 24 August–4 September 2025, '
            'differing from the programme’s 25 August–5 September 2025.'
        ),
        'evidence': [
            (
                'Programme 103, published call 405: application '
                'opening 2026-02-05; closing 2026-04-22. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 104,
        'title': (
            'ČEJKA: Study stays for compatriots at public universities '
            'in Czechia'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=104'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-04-01',
        'opening': '2026-02-05',
        'uncertain': False,
        'summary': (
            'ČEJKA Czech-studies stay at a Czech public university for '
            'adults aged 18 or over: one or two semesters, with Czech '
            'A2 checked online. Support is CZK 12000 monthly plus '
            'transport of CZK 2000 for European countries or CZK 5000 '
            'elsewhere. Deadline: 1 April 2026.'
        ),
        'evidence': [
            (
                'Programme 104, published call 406: application '
                'opening 2026-02-05; closing 2026-04-01. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 105,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            "Education, Youth and Sports for Students of Bachelor's, "
            "Master's and Doctoral Programmes from Albania"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=105'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-04-17',
        'opening': '2026-04-01',
        'uncertain': False,
        'summary': (
            'Study or research in Czechia for bachelor’s, master’s and '
            'doctoral students at Albanian public universities: two to '
            'nine months. Albanian ministry nomination is required '
            'before the Czech application. Tuition is waived, but the '
            'sending-country scholarship amount and frequency are '
            'unspecified and MEYS does not pay travel. Deadline: 17 '
            'April 2026.'
        ),
        'evidence': [
            (
                'Doctoral applicants must submit a Czech host '
                'invitation and a publication list, despite the '
                'generic attachment table displaying optional labels.'
            ),
            (
                'Admitted non-EU/EEA applicants must arrange '
                'comprehensive medical insurance for the full stay, '
                'including medically required repatriation.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 105, published call 416: application '
                'opening 2026-04-01; closing 2026-04-17. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 106,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            'Education, Youth and Sports for Academic Staff from '
            'Albania'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=106'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-04-17',
        'opening': '2026-04-01',
        'uncertain': False,
        'summary': (
            'Study, research or teaching at a Czech public university '
            'for academic or scientific staff from Albanian public '
            'universities or scientific institutions: up to one month. '
            'Albanian ministry nomination and a host invitation are '
            'required. Support amount and frequency are unspecified; '
            'MEYS does not reimburse travel. Deadline: 17 April 2026.'
        ),
        'evidence': [
            (
                'Admitted non-EU/EEA applicants must arrange '
                'comprehensive medical insurance for the full stay, '
                'including medically required repatriation.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 106, published call 417: application '
                'opening 2026-04-01; closing 2026-04-17. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 107,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            'Education, Youth and Sports for Academic Staff from '
            'Bulgaria'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=107'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-04-17',
        'opening': '2026-03-23',
        'uncertain': False,
        'summary': (
            'Academic stay in Czechia for staff from Bulgarian public '
            'universities: one to three months, with prior nomination. '
            'The source states monthly CZK 12000/CZK 13000 '
            'degree-based funding despite the staff audience. Tuition '
            'is waived; MEYS does not reimburse travel. Deadline: 17 '
            'April 2026.'
        ),
        'evidence': [
            (
                'The monthly degree bands are CZK 12000 without a '
                'master’s-equivalent qualification and CZK 13000 with '
                'one, subject to the stated exception for enrolment in '
                'another bachelor’s or master’s programme. They are '
                'qualification-based bands rather than an '
                'unconditional doctoral rate.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 107, published call 439: application '
                'opening 2026-03-23; closing 2026-04-17. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 108,
        'title': 'Summer Schools of Slavonic Studies',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=108'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-08-31',
        'opening': '2026-03-17',
        'uncertain': False,
        'summary': (
            'Summer Schools of Slavonic Studies in Czechia: 21–30 days '
            'for adults aged 18 or over nominated through their '
            'designated country or territory authority. Tuition, '
            'materials, activities, meals and accommodation are '
            'covered, but international travel, insurance and visa '
            'costs are not. The portal closes on 31 August 2026; 31 '
            'March is an agency nomination stage, not a universal '
            'applicant deadline.'
        ),
        'evidence': [
            (
                'Participants attend the school allocated through '
                'their nomination channel. Country and territory '
                'allocations include regional channels and do not '
                'establish a universal citizenship requirement. Six '
                'listed host school/location entries are course '
                'options within the same framework.'
            ),
            (
                'Admitted non-EU/EEA applicants must arrange '
                'comprehensive medical insurance for the full stay, '
                'including medically required repatriation.'
            ),
            (
                'Programme 108, published call 428: application '
                'opening 2026-03-17; closing 2026-08-31. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 109,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            "Education, Youth and Sports for Students of Bachelor's, "
            "Master's and Doctoral Programmes from China"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=109'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-03-15',
        'opening': '2026-02-15',
        'uncertain': False,
        'summary': (
            'Study or research in Czechia for bachelor’s, master’s and '
            'doctoral students at Chinese public universities: five to '
            'ten months. Prior China Scholarship Council nomination is '
            'required. Tuition is waived; the sending-country support '
            'amount and payment frequency are unspecified. MEYS does '
            'not reimburse travel. Deadline: 15 March 2026.'
        ),
        'evidence': [
            (
                'Admitted non-EU/EEA applicants must arrange '
                'comprehensive medical insurance for the full stay, '
                'including medically required repatriation.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 109, published call 109: application '
                'opening 2026-02-15; closing 2026-03-15. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 111,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            "Education, Youth and Sports for Students of Bachelor's "
            "and Master's Programmes from Egypt"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=111'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': ['EG'],
        'deadline': '2026-04-15',
        'opening': '2026-03-16',
        'uncertain': False,
        'summary': (
            'Five-month study in Czechia for bachelor’s/master’s '
            'students affiliated with Egyptian universities, including '
            'Egyptian citizens studying at Czech public universities. '
            'Prior nomination through the Czech embassy in Cairo is '
            'required. Funding is CZK 12000 monthly; MEYS does not '
            'reimburse travel. Deadline: 15 April 2026.'
        ),
        'evidence': [
            (
                'Admitted non-EU/EEA applicants must arrange '
                'comprehensive medical insurance for the full stay, '
                'including medically required repatriation.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 111, published call 111: application '
                'opening 2026-03-16; closing 2026-04-15. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 114,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            "Education, Youth and Sports for Students of Bachelor's "
            "and Master's Programmes from France"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=114'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': ['FR'],
        'deadline': '2026-06-26',
        'opening': '2026-06-14',
        'uncertain': False,
        'summary': (
            'Study in Czechia for bachelor’s/master’s students from '
            'French public universities, including French citizens '
            'studying at Czech public universities: two to ten months, '
            'with CZK 12000 monthly. The host decides admission; MEYS '
            'does not reimburse travel. Deadline: 26 June 2026.'
        ),
        'evidence': [
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 114, published call 414: application '
                'opening 2026-06-14; closing 2026-06-26. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 115,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            'Education, Youth and Sports for Students of Doctoral '
            'Programmes from Mongolia'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=115'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-10-23',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Doctoral study or research in Czechia for students from '
            'Mongolian public universities: two to nine months, '
            'normally CZK 13000 monthly, with a degree-based exception '
            'for other study levels. Prior nomination and '
            'English/Czech language proof are required. Applications '
            'run 1–23 October 2026; the nominating authority must send '
            'nominations at least three months before the stay.'
        ),
        'evidence': [
            (
                'Czech or English proficiency evidence and a '
                'publication list are required. The Czech host '
                'invitation is optional. Nominations by the Mongolian '
                'authority must arrive at least three months before '
                'the placement, separately from the 23 October 2026 '
                'portal deadline.'
            ),
            (
                'Admitted non-EU/EEA applicants must arrange '
                'comprehensive medical insurance for the full stay, '
                'including medically required repatriation.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 115, published call 436: application '
                'opening 2026-10-01; closing 2026-10-23. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 116,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            "Education, Youth and Sports for Students of Bachelor's, "
            "Master's and Doctoral Programmes from Georgia"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=116'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-04-10',
        'opening': '2026-03-18',
        'uncertain': False,
        'summary': (
            'Study or research in Czechia for bachelor’s, master’s and '
            'doctoral students at Georgian public universities: two to '
            'nine months. Funding is CZK 12000/CZK 13000 monthly '
            'according to degree level. Prior nomination is required; '
            'doctoral applicants need an invitation and publications. '
            'MEYS does not reimburse travel. Deadline: 10 April 2026.'
        ),
        'evidence': [
            (
                'The monthly degree bands are CZK 12000 without a '
                'master’s-equivalent qualification and CZK 13000 with '
                'one, subject to the stated exception for enrolment in '
                'another bachelor’s or master’s programme. They are '
                'qualification-based bands rather than an '
                'unconditional doctoral rate.'
            ),
            (
                'Admitted non-EU/EEA applicants must arrange '
                'comprehensive medical insurance for the full stay, '
                'including medically required repatriation.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 116, published call 432: application '
                'opening 2026-03-18; closing 2026-04-10. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 117,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            'Education, Youth and Sports for Academic Staff from '
            'Georgia'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=117'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-04-10',
        'opening': '2026-03-18',
        'uncertain': False,
        'summary': (
            'Study, research or lecturing in Czechia for academic '
            'staff from Georgian public universities: up to one month. '
            'Funding is CZK 933 per day for food and personal costs, '
            'paid as a single sum, plus directly paid accommodation. '
            'Prior nomination and host acceptance are required. MEYS '
            'does not reimburse travel. Deadline: 10 April 2026.'
        ),
        'evidence': [
            (
                'Admitted non-EU/EEA applicants must arrange '
                'comprehensive medical insurance for the full stay, '
                'including medically required repatriation.'
            ),
            (
                'Programme 117, published call 433: application '
                'opening 2026-03-18; closing 2026-04-10. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 118,
        'title': (
            'Bilateral Agreements: Ministry of Education Scholarships '
            "for Students of Bachelor's, Master's and Doctoral Study "
            'Programmes from India'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=118'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': ['IN'],
        'deadline': '2026-03-31',
        'opening': '2026-03-10',
        'uncertain': False,
        'summary': (
            'Study or research in Czechia for students from Indian '
            'public universities, including Indian citizens at Czech '
            'public universities: up to ten months. Monthly funding is '
            'CZK 12000/CZK 13000 by degree level. Prior nomination '
            'through the Czech embassy in Delhi is required. MEYS does '
            'not reimburse travel. Deadline: 31 March 2026.'
        ),
        'evidence': [
            (
                'The monthly degree bands are CZK 12000 without a '
                'master’s-equivalent qualification and CZK 13000 with '
                'one, subject to the stated exception for enrolment in '
                'another bachelor’s or master’s programme. They are '
                'qualification-based bands rather than an '
                'unconditional doctoral rate.'
            ),
            (
                'Admitted non-EU/EEA applicants must arrange '
                'comprehensive medical insurance for the full stay, '
                'including medically required repatriation.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 118, published call 118: application '
                'opening 2026-03-10; closing 2026-03-31. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 120,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            "Education, Youth and Sports for Students of Bachelor's, "
            "Master's and Doctoral Programmes from Italy"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=120'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': ['IT'],
        'deadline': '2026-02-27',
        'opening': '2026-01-15',
        'uncertain': False,
        'summary': (
            'Study or research in Czechia for students at Italian '
            'universities, including Italian citizens at Czech public '
            'universities: three to six months, with CZK 12000/CZK '
            '13000 monthly by degree level. Apply online before the '
            'Rome embassy’s assessment; prior external nomination is '
            'not required. MEYS does not reimburse travel. Deadline: '
            '27 February 2026.'
        ),
        'evidence': [
            (
                'The monthly degree bands are CZK 12000 without a '
                'master’s-equivalent qualification and CZK 13000 with '
                'one, subject to the stated exception for enrolment in '
                'another bachelor’s or master’s programme. They are '
                'qualification-based bands rather than an '
                'unconditional doctoral rate.'
            ),
            (
                'Admitted non-EU/EEA applicants must arrange '
                'comprehensive medical insurance for the full stay, '
                'including medically required repatriation.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 120, published call 409: application '
                'opening 2026-01-15; closing 2026-02-27. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 122,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            'Education, Youth and Sports for students and fresh '
            'graduates from Japan'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=122'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': ['JP'],
        'deadline': '2026-03-31',
        'opening': '2026-03-10',
        'uncertain': False,
        'summary': (
            'Study or research in Czechia for students or recent '
            'graduates from Japan, including Japanese citizens at '
            'Czech public universities: nine to twenty-four months. '
            'Monthly support is CZK 12000/CZK 13000 by degree level. '
            'Prior Tokyo embassy nomination is required. MEYS does not '
            'reimburse travel. Deadline: 31 March 2026.'
        ),
        'evidence': [
            (
                'The monthly degree bands are CZK 12000 without a '
                'master’s-equivalent qualification and CZK 13000 with '
                'one, subject to the stated exception for enrolment in '
                'another bachelor’s or master’s programme. They are '
                'qualification-based bands rather than an '
                'unconditional doctoral rate.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 122, published call 122: application '
                'opening 2026-03-10; closing 2026-03-31. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 124,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            "Education, Youth and Sports for Students of Bachelor's, "
            "Master's and Doctoral Programmes from the Republic of "
            'Korea'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=124'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': ['KR'],
        'deadline': '2026-03-15',
        'opening': '2026-01-02',
        'uncertain': False,
        'summary': (
            'Study or research in Czechia for students or recent '
            'bachelor’s/master’s graduates from Korean universities, '
            'including Korean citizens at Czech public universities: '
            'nine to twenty-four months. Funding is CZK 12000/CZK '
            '13000 monthly by degree level. The Seoul embassy assesses '
            'the online application without a preliminary nomination '
            'requirement. MEYS does not reimburse travel. Deadline: 15 '
            'March 2026.'
        ),
        'evidence': [
            (
                'The monthly degree bands are CZK 12000 without a '
                'master’s-equivalent qualification and CZK 13000 with '
                'one, subject to the stated exception for enrolment in '
                'another bachelor’s or master’s programme. They are '
                'qualification-based bands rather than an '
                'unconditional doctoral rate.'
            ),
            (
                'Admitted non-EU/EEA applicants must arrange '
                'comprehensive medical insurance for the full stay, '
                'including medically required repatriation.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 124, published call 124: application '
                'opening 2026-01-02; closing 2026-03-15. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 130,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            "Education, Youth and Sports for Students of Bachelor's "
            "and Master's Programmes from Hungary"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=130'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': ['HU'],
        'deadline': '2026-04-17',
        'opening': '2026-03-17',
        'uncertain': False,
        'summary': (
            'Five-month bachelor’s/master’s study in Czechia for '
            'students from Hungarian universities, including Hungarian '
            'citizens at Czech public universities. Prior Tempus '
            'Public Foundation nomination is required. The '
            'sending-country scholarship amount and payment frequency '
            'are unspecified; MEYS does not pay travel. Deadline: 17 '
            'April 2026.'
        ),
        'evidence': [
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 130, published call 130: application '
                'opening 2026-03-17; closing 2026-04-17. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 131,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            'Education, Youth and Sports for Academic Staff from '
            'Hungary (1-3 months)'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=131'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-04-17',
        'opening': '2026-03-17',
        'uncertain': False,
        'summary': (
            'Academic study, research or teaching in Czechia for staff '
            'from Hungarian public universities: one to three months, '
            'with CZK 13000 monthly. Prior Tempus Public Foundation '
            'nomination and host invitation are required. MEYS does '
            'not reimburse travel. Deadline: 17 April 2026.'
        ),
        'evidence': [
            (
                'Admitted non-EU/EEA applicants must arrange '
                'comprehensive medical insurance for the full stay, '
                'including medically required repatriation.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 131, published call 430: application '
                'opening 2026-03-17; closing 2026-04-17. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 134,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            "Education, Youth and Sports for Students of Bachelor's, "
            "Master's and Doctoral Programmes from Mexico"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=134'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': ['MX'],
        'deadline': '2026-04-17',
        'opening': '2026-03-13',
        'uncertain': False,
        'summary': (
            'Study or research in Czechia for students from Mexican '
            'universities, including Mexican citizens at Czech public '
            'universities: three to ten months. Funding is CZK '
            '12000/CZK 13000 monthly by degree level. Prior Mexico '
            'City embassy nomination is required; MEYS does not pay '
            'travel. Deadline: 17 April 2026.'
        ),
        'evidence': [
            (
                'The monthly degree bands are CZK 12000 without a '
                'master’s-equivalent qualification and CZK 13000 with '
                'one, subject to the stated exception for enrolment in '
                'another bachelor’s or master’s programme. They are '
                'qualification-based bands rather than an '
                'unconditional doctoral rate.'
            ),
            (
                'Admitted non-EU/EEA applicants must arrange '
                'comprehensive medical insurance for the full stay, '
                'including medically required repatriation.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 134, published call 427: application '
                'opening 2026-03-13; closing 2026-04-17. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 136,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            "Education, Youth and Sports for Students of Bachelor's "
            "and Master's Programmes from Mongolia"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=136'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-10-23',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Bachelor’s/master’s study or research in Czechia for '
            'students from Mongolian public universities: two to nine '
            'months, with CZK 12000 monthly. Prior nomination and '
            'English/Czech language proof are required; a host '
            'invitation is optional. Applications run 1–23 October '
            '2026. MEYS does not reimburse travel.'
        ),
        'evidence': [
            (
                'Admitted non-EU/EEA applicants must arrange '
                'comprehensive medical insurance for the full stay, '
                'including medically required repatriation.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 136, published call 437: application '
                'opening 2026-10-01; closing 2026-10-23. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 137,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            'Education, Youth and Sports for Academic Staff from '
            'Mongolia'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=137'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-04-10',
        'opening': '2026-03-18',
        'uncertain': False,
        'summary': (
            'Academic study, research or teaching in Czechia for staff '
            'from Mongolian public universities: up to one month. CZK '
            '933 daily for food and personal costs is paid as one sum, '
            'with accommodation paid separately. Prior nomination and '
            'host acceptance are required; MEYS does not reimburse '
            'travel. Deadline: 10 April 2026.'
        ),
        'evidence': [
            (
                'Admitted non-EU/EEA applicants must arrange '
                'comprehensive medical insurance for the full stay, '
                'including medically required repatriation.'
            ),
            (
                'Programme 137, published call 435: application '
                'opening 2026-03-18; closing 2026-04-10. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 138,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            "Education, Youth and Sports for Students of Bachelor's, "
            "Master's and Doctoral Programmes from Germany"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=138'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': ['DE'],
        'deadline': '2026-03-01',
        'opening': '2026-01-19',
        'uncertain': False,
        'summary': (
            'Study or research in Czechia for students from German '
            'universities, including German citizens at Czech public '
            'universities: three to nine months. Monthly support is '
            'CZK 12000/CZK 13000 by degree level; MEYS does not '
            'reimburse travel. Deadline: 1 March 2026.'
        ),
        'evidence': [
            (
                'The monthly degree bands are CZK 12000 without a '
                'master’s-equivalent qualification and CZK 13000 with '
                'one, subject to the stated exception for enrolment in '
                'another bachelor’s or master’s programme. They are '
                'qualification-based bands rather than an '
                'unconditional doctoral rate.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 138, published call 410: application '
                'opening 2026-01-19; closing 2026-03-01. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 139,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            "Education, Youth and Sports for Students of Bachelor's, "
            "Master's and Doctoral Programmes from Germany (Bavaria)"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=139'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': ['DE'],
        'deadline': '2026-08-16',
        'opening': '2026-07-24',
        'uncertain': False,
        'summary': (
            'Study or research in Czechia for students from Bavarian '
            'universities, including German citizens at Czech public '
            'universities: up to ten months. Prior BTHA nomination is '
            'required. Funding is CZK 12000/CZK 13000 monthly by '
            'degree level; MEYS does not pay travel. Deadline: 16 '
            'August 2026.'
        ),
        'evidence': [
            (
                'The monthly degree bands are CZK 12000 without a '
                'master’s-equivalent qualification and CZK 13000 with '
                'one, subject to the stated exception for enrolment in '
                'another bachelor’s or master’s programme. They are '
                'qualification-based bands rather than an '
                'unconditional doctoral rate.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 139, published call 139: application '
                'opening 2026-07-24; closing 2026-08-16. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 142,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            "Education, Youth and Sports for Students of Bachelor's, "
            "Master's and Doctoral Programmes from Peru (Universidad "
            'Nacional de Ucayali)'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=142'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': ['PE'],
        'deadline': '2026-04-15',
        'opening': '2026-03-01',
        'uncertain': False,
        'summary': (
            'Forestry or agriculture study at the Czech University of '
            'Life Sciences Prague for students from Ucayali, Peru, '
            'including Peruvian citizens at that Czech university: '
            'three to nine months. Prior embassy nomination is '
            'required. Funding is CZK 12000/CZK 13000 monthly by '
            'degree level; MEYS does not pay travel. Deadline: 15 '
            'April 2026.'
        ),
        'evidence': [
            (
                'The monthly degree bands are CZK 12000 without a '
                'master’s-equivalent qualification and CZK 13000 with '
                'one, subject to the stated exception for enrolment in '
                'another bachelor’s or master’s programme. They are '
                'qualification-based bands rather than an '
                'unconditional doctoral rate.'
            ),
            (
                'Admitted non-EU/EEA applicants must arrange '
                'comprehensive medical insurance for the full stay, '
                'including medically required repatriation.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 142, published call 142: application '
                'opening 2026-03-01; closing 2026-04-15. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 144,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            "Education, Youth and Sports for Students of Bachelor's, "
            "Master's and Doctoral Programmes from Poland"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=144'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': ['PL'],
        'deadline': '2026-04-17',
        'opening': '2026-03-13',
        'uncertain': False,
        'summary': (
            'Study or research in Czechia for students from Polish '
            'universities, including Polish citizens at Czech public '
            'universities: two to ten months. Prior NAWA nomination is '
            'required. The sending-country scholarship amount and '
            'frequency are unspecified; MEYS does not pay travel. '
            'Deadline: 17 April 2026.'
        ),
        'evidence': [
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 144, published call 425: application '
                'opening 2026-03-13; closing 2026-04-17. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 145,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            'Education, Youth and Sports for Academic Staff from Poland'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=145'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-04-17',
        'opening': '2026-03-13',
        'uncertain': False,
        'summary': (
            'Academic study, research or teaching in Czechia for staff '
            'from Polish public universities: one to seven months. '
            'Prior NAWA nomination is required; NAWA-funded support '
            'amount and payment frequency are unspecified. MEYS does '
            'not reimburse travel. Deadline: 17 April 2026.'
        ),
        'evidence': [
            (
                'Admitted non-EU/EEA applicants must arrange '
                'comprehensive medical insurance for the full stay, '
                'including medically required repatriation.'
            ),
            (
                'Programme 145, published call 426: application '
                'opening 2026-03-13; closing 2026-04-17. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 146,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            "Education, Youth and Sports for Students of Bachelor's, "
            "Master's and Doctoral Programmes from Portugal"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=146'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': ['PT'],
        'deadline': '2026-04-17',
        'opening': '2026-01-13',
        'uncertain': False,
        'summary': (
            'Study or research in Czechia for students from Portuguese '
            'public universities, including Portuguese citizens at '
            'Czech public universities: up to ten months. Monthly '
            'support is CZK 12000/CZK 13000 by degree level. '
            'Recommendations and transcripts are optional in the '
            'attachment table. MEYS does not pay travel. Deadline: 17 '
            'April 2026.'
        ),
        'evidence': [
            (
                'The monthly degree bands are CZK 12000 without a '
                'master’s-equivalent qualification and CZK 13000 with '
                'one, subject to the stated exception for enrolment in '
                'another bachelor’s or master’s programme. They are '
                'qualification-based bands rather than an '
                'unconditional doctoral rate.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 146, published call 407: application '
                'opening 2026-01-13; closing 2026-04-17. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 148,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            "Education, Youth and Sports for Students of Bachelor's, "
            "Master's and Doctoral Programmes from Romania"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=148'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-04-20',
        'opening': '2026-04-01',
        'uncertain': False,
        'summary': (
            'Study or research in Czechia for students from Romanian '
            'public universities: two to nine months, following '
            'nomination. The sending-country support amount and '
            'frequency are unspecified; MEYS does not pay travel. '
            'Applications close on 20 April 2026. The call title says '
            '2025/26 while its academic-year field says 2026/27.'
        ),
        'evidence': [
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 148, published call 444: application '
                'opening 2026-04-01; closing 2026-04-20. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 149,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            'Education, Youth and Sports for Academic Staff from '
            'Romania'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=149'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-04-20',
        'opening': '2026-04-01',
        'uncertain': False,
        'summary': (
            'Academic study, research or teaching in Czechia for staff '
            'from Romanian public universities: up to one month, with '
            'prior nomination. Support amount and frequency are '
            'unspecified, with dormitory accommodation covered by MEYS '
            'and no MEYS travel reimbursement. Deadline: 20 April '
            '2026. The call title says 2025/26 while its academic-year '
            'field says 2026/27.'
        ),
        'evidence': [
            (
                'MEYS covers student-dormitory accommodation. The '
                'availability of student catering does not establish '
                'free meals.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 149, published call 443: application '
                'opening 2026-04-01; closing 2026-04-20. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 150,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            'Education, Youth and Sports for Students of Doctoral '
            'Programmes from Hungary'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=150'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-04-17',
        'opening': '2026-03-17',
        'uncertain': False,
        'summary': (
            'Doctoral study or research in Czechia for students from '
            'Hungarian public universities: three to nine months, with '
            'CZK 13000 monthly. Prior Tempus Public Foundation '
            'nomination is required. This entry does not state the '
            'separate Hungarian-citizen-at-Czech-university '
            'alternative. MEYS does not pay travel. Deadline: 17 April '
            '2026.'
        ),
        'evidence': [
            'Scholarship holders are exempt from tuition fees.',
            (
                'The CZK 13,000 monthly rate requires a '
                'master’s-equivalent qualification, subject to the '
                'source’s exception for enrolment in another '
                'bachelor’s or master’s programme.'
            ),
            (
                'Programme 150, published call 429: application '
                'opening 2026-03-17; closing 2026-04-17. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 151,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            "Education, Youth and Sports for Students of Bachelor's "
            "and Master's Programmes from the Republic of North "
            'Macedonia'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=151'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-04-17',
        'opening': '2026-04-01',
        'uncertain': False,
        'summary': (
            'Bachelor’s/master’s study or research in Czechia for '
            'students from North Macedonian public universities: two '
            'to ten months, following ministry nomination. Monthly '
            'support is CZK 12000/CZK 13000 according to degree level. '
            'Travel is covered by the sending country rather than '
            'MEYS. Deadline: 17 April 2026.'
        ),
        'evidence': [
            (
                'The monthly degree bands are CZK 12000 without a '
                'master’s-equivalent qualification and CZK 13000 with '
                'one, subject to the stated exception for enrolment in '
                'another bachelor’s or master’s programme. They are '
                'qualification-based bands rather than an '
                'unconditional doctoral rate.'
            ),
            (
                'Admitted non-EU/EEA applicants must arrange '
                'comprehensive medical insurance for the full stay, '
                'including medically required repatriation.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 151, published call 421: application '
                'opening 2026-04-01; closing 2026-04-17. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 152,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            'Education, Youth and Sports for Academic Staff from the '
            'Republic of North Macedonia'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=152'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-04-17',
        'opening': '2026-04-01',
        'uncertain': False,
        'summary': (
            'Visits to Czechia for professors, university teachers, '
            'academics or experts giving lectures, consultations, '
            'curriculum work or conference participation: up to ten '
            'days, with prior nomination. The receiving party provides '
            'accommodation, board and pocket money; amounts are '
            'unspecified. MEYS does not reimburse travel. Closing: '
            'April 17, 2026.'
        ),
        'evidence': [
            (
                'Admitted non-EU/EEA applicants must arrange '
                'comprehensive medical insurance for the full stay, '
                'including medically required repatriation.'
            ),
            (
                'The audience includes experts as well as university '
                'staff. The table states daily payment without a cash '
                'rate. Nomination and the listed academic documents, '
                'recommendations and host invitation remain required.'
            ),
            (
                'Programme 152, published call 422: application '
                'opening 2026-04-01; closing 2026-04-17. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 153,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            "Education, Youth and Sports for Students of Master's and "
            'Doctoral Programmes from Slovakia'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=153'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': ['SK'],
        'deadline': '2026-04-17',
        'opening': '2026-04-01',
        'uncertain': False,
        'summary': (
            'Master’s/doctoral study or research in Czechia for '
            'students from Slovak public universities, including '
            'Slovak citizens at Czech public universities: three to '
            'ten months. Prior SAIA nomination is required. Funding is '
            'CZK 12000/CZK 13000 monthly by degree level; MEYS does '
            'not pay travel. Deadline: 17 April 2026.'
        ),
        'evidence': [
            (
                'The monthly degree bands are CZK 12000 without a '
                'master’s-equivalent qualification and CZK 13000 with '
                'one, subject to the stated exception for enrolment in '
                'another bachelor’s or master’s programme. They are '
                'qualification-based bands rather than an '
                'unconditional doctoral rate.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 153, published call 442: application '
                'opening 2026-04-01; closing 2026-04-17. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 154,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            'Education, Youth and Sports for Academic Staff from '
            'Slovakia (max. 14 days)'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=154'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2026-09-03',
        'uncertain': False,
        'summary': (
            'Academic visits to Czechia for staff employed at Slovak '
            'public universities: up to fourteen days, with paid '
            'accommodation for up to fourteen nights. Prior Slovak '
            'nomination and host invitation are required. The stated '
            'CZK 939 daily conflicts with CZK 283 pocket money plus '
            'CZK 706 board, totalling CZK 989; the breakfast-adjusted '
            'amount is CZK 812.50. Dates must be notified two months '
            'ahead. Portal deadline: 30 June 2027.'
        ),
        'evidence': [
            (
                'The Slovak selection instructions and required '
                'ministry nomination coexist with a stray Tempus '
                'Public Foundation sentence. The source does not '
                'reimburse travel. The source states CZK 939 daily '
                'although its listed components total CZK 989; this '
                'discrepancy is unresolved rather than silently '
                'corrected.'
            ),
            (
                'Admitted non-EU/EEA applicants must arrange '
                'comprehensive medical insurance for the full stay, '
                'including medically required repatriation.'
            ),
            (
                'Programme 154, published call 460: application '
                'opening 2026-09-03; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 155,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            "Education, Youth and Sports for Students of Master's and "
            'Doctoral Programmes from Spain'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=155'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': ['ES'],
        'deadline': '2026-04-17',
        'opening': '2026-01-13',
        'uncertain': False,
        'summary': (
            'Master’s/doctoral study or research in Czechia for '
            'students from Spanish public universities, including '
            'Spanish citizens at Czech public universities: three to '
            'nine months. Monthly support is CZK 12000/CZK 13000 by '
            'degree level; MEYS does not reimburse travel. Deadline: '
            '17 April 2026.'
        ),
        'evidence': [
            (
                'The monthly degree bands are CZK 12000 without a '
                'master’s-equivalent qualification and CZK 13000 with '
                'one, subject to the stated exception for enrolment in '
                'another bachelor’s or master’s programme. They are '
                'qualification-based bands rather than an '
                'unconditional doctoral rate.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 155, published call 408: application '
                'opening 2026-01-13; closing 2026-04-17. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 157,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            "Education, Youth and Sports for Students of Master's and "
            'Doctoral Programmes from Switzerland'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=157'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': ['CH'],
        'deadline': '2026-03-31',
        'opening': '2026-02-16',
        'uncertain': False,
        'summary': (
            'Master’s/doctoral study or research in Czechia for '
            'students from Swiss universities, including Swiss '
            'citizens at Czech public universities. Funding is CZK '
            '12000/CZK 13000 monthly by degree level; duration is '
            'unspecified. Language proof is required, and MEYS does '
            'not reimburse travel. Deadline: 31 March 2026.'
        ),
        'evidence': [
            (
                'The monthly degree bands are CZK 12000 without a '
                'master’s-equivalent qualification and CZK 13000 with '
                'one, subject to the stated exception for enrolment in '
                'another bachelor’s or master’s programme. They are '
                'qualification-based bands rather than an '
                'unconditional doctoral rate.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 157, published call 412: application '
                'opening 2026-02-16; closing 2026-03-31. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 159,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            'Education, Youth and Sports for Students and Fresh '
            'Graduates from Taiwan'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=159'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': ['TW'],
        'deadline': '2026-04-09',
        'opening': '2026-03-10',
        'uncertain': False,
        'summary': (
            'Study or research in Czechia for students or recent '
            'graduates from Taiwan, including Taiwanese citizens at '
            'Czech public universities: five to twenty months. Prior '
            'Taiwanese nomination is required. Funding is CZK '
            '12000/CZK 13000 monthly by degree level; MEYS does not '
            'pay travel. Deadline: 9 April 2026.'
        ),
        'evidence': [
            (
                'The monthly degree bands are CZK 12000 without a '
                'master’s-equivalent qualification and CZK 13000 with '
                'one, subject to the stated exception for enrolment in '
                'another bachelor’s or master’s programme. They are '
                'qualification-based bands rather than an '
                'unconditional doctoral rate.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 159, published call 159: application '
                'opening 2026-03-10; closing 2026-04-09. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 160,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            'Education, Youth and Sports for Academic Staff from Greece'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=160'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2025-08-20',
        'opening': '2025-08-05',
        'uncertain': False,
        'summary': (
            'Academic visits to Czechia for staff from Greek '
            'universities, specifically in calendar year 2025: up to '
            'seven days. CZK 989 per day is paid as a single sum, with '
            'hotel accommodation for up to six nights. Official Greek '
            'nomination and a host invitation are required; dates need '
            'six weeks’ notice. MEYS does not pay travel. Deadline: 20 '
            'August 2025.'
        ),
        'evidence': [
            (
                'Programme 160, published call 160: application '
                'opening 2025-08-05; closing 2025-08-20. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 163,
        'title': (
            'ČEJKA: Study stays for students of Czech studies at '
            'public universities in Czechia'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=163'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-04-01',
        'opening': '2026-02-05',
        'uncertain': False,
        'summary': (
            'ČEJKA study at eligible Czech universities for students '
            'taught abroad by a lecturer sent under the Czech-language '
            'support programme, aged at least 18. Funding is CZK 12000 '
            'monthly plus CZK 2000 transport from European countries '
            'or CZK 5000 elsewhere. A programme lecturer’s declaration '
            'and medical certificate are required. Deadline: 1 April '
            '2026.'
        ),
        'evidence': [
            (
                'Programme 163, published call 163: application '
                'opening 2026-02-05; closing 2026-04-01. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 164,
        'title': (
            'Germany: BTHA scholarships – sommer schools and language '
            'courses at Bavarian universities'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=164'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2026-03-10',
        'opening': '2026-01-01',
        'uncertain': False,
        'summary': (
            'BTHA funding for summer schools and German-language '
            'courses at Bavarian universities for students, teachers '
            'and employees. Amount and duration are not specified, and '
            'travel costs are not reimbursed. The published '
            'application period is 1 January–10 March 2026.'
        ),
        'evidence': [
            (
                'Programme 164, published call 243: application '
                'opening 2026-01-01; closing 2026-03-10. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 165,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            "Education, Youth and Sports for Students of Bachelor's, "
            "Master's and Doctoral Programmes from Bulgaria"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=165'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-04-17',
        'opening': '2026-03-23',
        'uncertain': False,
        'summary': (
            'Study or research in Czechia for students from Bulgarian '
            'public universities: two to nine months, with prior '
            'nomination. Doctoral applicants require a host invitation '
            'and publications. Sending-country support amount and '
            'frequency are unspecified; MEYS does not pay travel. '
            'Deadline: 17 April 2026. The call title says 2025/26 but '
            'its academic-year field says 2026/27.'
        ),
        'evidence': [
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 165, published call 438: application '
                'opening 2026-03-23; closing 2026-04-17. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 166,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            'Education, Youth and Sports for Students of Doctoral '
            'Programmes from the Republic of North Macedonia'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=166'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-04-17',
        'opening': '2026-04-01',
        'uncertain': False,
        'summary': (
            'Doctoral study or research in Czechia for students from '
            'North Macedonian public universities: two to ten months, '
            'with ministry nomination, publications and host '
            'invitation required. The source gives CZK 12000/CZK 13000 '
            'monthly degree bands despite the doctoral audience. '
            'Travel is funded by the sending country, not MEYS. '
            'Deadline: 17 April 2026.'
        ),
        'evidence': [
            (
                'The monthly degree bands are CZK 12000 without a '
                'master’s-equivalent qualification and CZK 13000 with '
                'one, subject to the stated exception for enrolment in '
                'another bachelor’s or master’s programme. They are '
                'qualification-based bands rather than an '
                'unconditional doctoral rate.'
            ),
            (
                'Admitted non-EU/EEA applicants must arrange '
                'comprehensive medical insurance for the full stay, '
                'including medically required repatriation.'
            ),
            'Scholarship holders are exempt from tuition fees.',
            (
                'Programme 166, published call 423: application '
                'opening 2026-04-01; closing 2026-04-17. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 167,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            'Education, Youth and Sports for Academic Staff from '
            'Hungary (max. 14 days)'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=167'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2025-04-30',
        'opening': '2025-03-15',
        'uncertain': False,
        'summary': (
            'Academic visits to Czechia for staff at Hungarian public '
            'universities: up to fourteen days and fourteen funded '
            'accommodation nights, following Tempus nomination. CZK '
            '939 daily conflicts with CZK 283 pocket money plus CZK '
            '706 board, totalling CZK 989; breakfast reduces the '
            'listed amount to CZK 812.50. Dates need two months’ '
            'notice. Deadline: 30 April 2025; the call’s quota is '
            'labelled both days and months.'
        ),
        'evidence': [
            (
                'The call labels its capacity 85 days but also prints '
                'a total quota of 85 months. Those quota statements do '
                'not alter the individual maximum fourteen-day stay. '
                'MEYS does not reimburse travel.'
            ),
            (
                'Admitted non-EU/EEA applicants must arrange '
                'comprehensive medical insurance for the full stay, '
                'including medically required repatriation.'
            ),
            (
                'Programme 167, published call 167: application '
                'opening 2025-03-15; closing 2025-04-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 168,
        'title': (
            'Estonia: 1–9 day research and teaching stays for academic '
            'staff'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=168'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['EE'],
        'eligible': [],
        'deadline': '2025-04-16',
        'opening': '2025-03-17',
        'uncertain': False,
        'summary': (
            'Academic stay in Estonia for university staff: one to '
            'nine days, with EUR 45 daily and no travel reimbursement. '
            'The published application period is 17 March–16 April '
            '2025.'
        ),
        'evidence': [
            (
                'Programme 168, published call 169: application '
                'opening 2025-03-17; closing 2025-04-16. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 169,
        'title': (
            'Estonia: Subject- and field-specific summer and winter '
            'courses (incl. language courses) for university students'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=169'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['EE'],
        'eligible': [],
        'deadline': '2025-04-16',
        'opening': '2025-03-17',
        'uncertain': False,
        'summary': (
            'Summer courses in Estonia for university students: one to '
            'four weeks. Host-country funding is indicated, but its '
            'amount and payment frequency are unspecified. Travel is '
            'not reimbursed. Applications ran from 17 March to 16 '
            'April 2025.'
        ),
        'evidence': [
            (
                'Programme 169, published call 168: application '
                'opening 2025-03-17; closing 2025-04-16. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 170,
        'title': (
            'Estonia: Research and teaching stays for academic staff '
            'up to 10 months'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=170'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['EE'],
        'eligible': [],
        'deadline': '2025-04-16',
        'opening': '2025-03-17',
        'uncertain': False,
        'summary': (
            'Academic research stay in Estonia for university '
            'employees: up to ten months, with EUR 660 monthly and no '
            'travel reimbursement. Applications ran from 17 March to '
            '16 April 2025.'
        ),
        'evidence': [
            (
                'Programme 170, published call 170: application '
                'opening 2025-03-17; closing 2025-04-16. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 171,
        'title': 'Vietnam: Study stays for university students',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=171'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['VN'],
        'eligible': [],
        'deadline': '2027-01-29',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Study in Vietnam for students at Czech public '
            'universities: two to ten months, with VND 4820000 monthly '
            'plus VND 3580000 one-time support. State-university '
            'tuition is covered; accommodation and meals follow '
            'Vietnamese-student conditions. Vietnamese preparation is '
            'required if language skills are insufficient, with '
            'documented exemptions. Sending-university travel '
            'reimbursement is available. Nomination deadline: 29 '
            'January 2027; a second dossier stage follows.'
        ),
        'evidence': [
            (
                'After Czech nomination, the Vietnamese partner '
                'decides the award. The second-stage dossier must be '
                'in English or Vietnamese, with certified translations '
                'where needed, a medical certificate issued no more '
                'than six months before application and passport '
                'validity for the stay or at least one year after '
                'arrival. The partner deadline is not specified in the '
                'selected call. The 26 March 2025 guidance prints 15 '
                'July without a year; it does not establish a '
                'year-qualified closing date.'
            ),
            (
                'Vietnamese B2 or documented prior education in '
                'Vietnamese exempts the otherwise required one-year '
                'preparation. Doctoral dossiers need a research '
                'proposal and two academic recommendations. The '
                'selected call’s sixty-month allocation is a shared '
                'budget, not sixty individual awards. Accommodation '
                'and meals on Vietnamese-student terms are not a '
                'promise of universally free housing and food.'
            ),
            (
                'Programme 171, published call 498: application '
                'opening 2026-10-01; closing 2027-01-29. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 172,
        'title': (
            'Vietnam: Summer courses of Vietnamese language and '
            'culture for university students'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=172'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['VN'],
        'eligible': [],
        'deadline': '2027-01-29',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Vietnamese language and culture course in Da Nang for '
            'students at Czech public universities: one to four weeks, '
            'with no Vietnamese prerequisite. Course, excursions, '
            'accommodation and meals are covered, with no cash stipend '
            'and sending-university travel reimbursement on request. '
            'Czech nomination closes on 29 January 2027, followed by a '
            'Vietnamese dossier stage; the call lists 1 June 2027 as '
            'its event date.'
        ),
        'evidence': [
            (
                'After Czech nomination, the Vietnamese partner '
                'decides the award. The second-stage dossier must be '
                'in English or Vietnamese, with certified translations '
                'where needed, a medical certificate issued no more '
                'than six months before application and passport '
                'validity for the stay or at least one year after '
                'arrival. The partner deadline is not specified in the '
                'selected call. The 26 March 2025 guidance prints 15 '
                'July without a year; it does not establish a '
                'year-qualified closing date.'
            ),
            (
                'Programme 172, published call 514: application '
                'opening 2026-10-01; closing 2027-01-29. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 173,
        'title': (
            'Vietnam: Completion of entire post-graduate study '
            'programmes'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=173'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['VN'],
        'eligible': [],
        'deadline': '2026-01-31',
        'opening': '2025-10-01',
        'uncertain': False,
        'summary': (
            'Full postgraduate study at a Vietnamese public '
            'university, with a language-preparation year if needed. '
            'Monthly support is VND 2900000 during preparation and VND '
            '4110000 during degree study, plus VND 4480000 one-time '
            'support; travel is not reimbursed. The title says '
            'postgraduate study while structured fields list '
            'bachelor’s level. Applications close on 31 January 2026; '
            'calls are biennial.'
        ),
        'evidence': [
            (
                'During degree study, support is VND 4110000 monthly; '
                'preparatory language study has the separate VND '
                '2900000 monthly rate. Tuition is covered, and '
                'dormitory accommodation and meals follow '
                'Vietnamese-student conditions. The selected call '
                'explicitly states that calls are published every '
                'other year.'
            ),
            (
                'Programme 173, published call 173: application '
                'opening 2025-10-01; closing 2026-01-31. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 174,
        'title': (
            'Italy: Study stays for masters and doctoral students, '
            'students of art, research projects, and courses of Italian'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=174'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['IT'],
        'eligible': [],
        'deadline': '2026-03-26',
        'opening': '2026-03-01',
        'uncertain': False,
        'summary': (
            'Italian funding for master’s/doctoral study, arts study, '
            'research projects or Italian-language courses. The entry '
            'does not state funding amounts or duration. Applications '
            'for 2026/27 ran from 1 to 26 March 2026.'
        ),
        'evidence': [
            (
                'Programme 174, published call 440: application '
                'opening 2026-03-01; closing 2026-03-26. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 175,
        'title': (
            "South Korea: Study stays for students of Bachelor's "
            'programmes and high school graduates'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=175'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['KR'],
        'eligible': [],
        'deadline': '2025-10-17',
        'opening': '2025-09-15',
        'uncertain': False,
        'summary': (
            'Undergraduate study in South Korea for bachelor’s '
            'students and school graduates: five to seven years, with '
            'KRW 1140000 monthly and no travel reimbursement. The '
            'portal specifies birth after 1 March 2001. Applications '
            'ran from 15 September to 17 October 2025.'
        ),
        'evidence': [
            (
                'Programme 175, published call 175: application '
                'opening 2025-09-15; closing 2025-10-17. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 176,
        'title': (
            'Germany: Observation internships at schools in Hamburg '
            'for teachers from Prague elementary schools'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=176'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2026-10-18',
        'opening': '2026-09-26',
        'uncertain': False,
        'summary': (
            'Four-day school-observation placement in Hamburg, 29 '
            'November–2 December 2026, only for teachers employed at '
            'Prague elementary schools. German is required, with a '
            'German CV, motivation letter of up to two A4 pages and '
            'signed attachment. Travel is covered; cash support is '
            'unspecified. Deadline: 18 October 2026.'
        ),
        'evidence': [
            (
                'Programme 176, published call 466: application '
                'opening 2026-09-26; closing 2026-10-18. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 177,
        'title': (
            'Vietnam: Study and teaching stays for university teachers'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=177'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['VN'],
        'eligible': [],
        'deadline': '2027-01-29',
        'opening': '2026-10-01',
        'uncertain': False,
        'summary': (
            'Study or teaching in Vietnam for teachers at Czech public '
            'universities: two to four weeks, with VND 4110000 '
            'monthly, VND 3580000 one-time support, free dormitory and '
            'sending-university travel reimbursement. Czech nomination '
            'closes on 29 January 2027, followed by a Vietnamese '
            'dossier stage. The source also gives 30 June 2026 for '
            'late host acceptance, conflicting with the 2027/28 round.'
        ),
        'evidence': [
            (
                'After Czech nomination, the Vietnamese partner '
                'decides the award. The second-stage dossier must be '
                'in English or Vietnamese, with certified translations '
                'where needed, a medical certificate issued no more '
                'than six months before application and passport '
                'validity for the stay or at least one year after '
                'arrival. The partner deadline is not specified in the '
                'selected call. The 26 March 2025 guidance prints 15 '
                'July without a year; it does not establish a '
                'year-qualified closing date.'
            ),
            (
                'The selected call shares two total months among '
                'several nominees, rather than guaranteeing two months '
                'per teacher. The printed late host-acceptance date of '
                '30 June 2026 is earlier than the 1 October 2026–29 '
                'January 2027 application period and remains an '
                'unresolved source conflict.'
            ),
            (
                'Programme 177, published call 499: application '
                'opening 2026-10-01; closing 2027-01-29. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 178,
        'title': 'Erasmus+ Traineeship, Embassy Yerevan, Armenia',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=178'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['AM'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-10-31',
        'uncertain': True,
        'summary': (
            'Yerevan embassy: political, economic and human-rights '
            'research, development and cultural work for 2–4 months. '
            'Completed BA, post-Soviet knowledge, English B2 and '
            'Russian B1+ required. No free housing; approval usually '
            '6–8 weeks. Czech citizens only. Host interest: 20 January '
            '2026; late host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Yerevan embassy: political, economic and human-rights '
                'research, development and cultural work for 2–4 '
                'months. Completed BA, post-Soviet knowledge, English '
                'B2 and Russian B1+ required. No free housing; '
                'approval usually 6–8 weeks.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=178; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 178, published call 250: application '
                'opening 2025-10-31; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 179,
        'title': 'Erasmus+ Traineeship, Embassy Algiers, Algeria',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=179'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['DZ'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-10-31',
        'uncertain': True,
        'summary': (
            'Algiers embassy: press monitoring, Czech/French web '
            'articles and events for 2–3 months within '
            'September–November 2026, adjustable by agreement. French '
            'B2/C1 required; free housing offered. Czech citizens '
            'only. Host interest: 20 January 2026; late host agreement '
            'possible. Portal: 30 June 2027; availability unknown. '
            'University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Algiers embassy: press monitoring, Czech/French web '
                'articles and events for 2–3 months within '
                'September–November 2026, adjustable by agreement. '
                'French B2/C1 required; free housing offered.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=179; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 179, published call 251: application '
                'opening 2025-10-31; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 180,
        'title': (
            'Erasmus+ Traineeship, Czech School Without Borders '
            'Brussels (Ecole tchèque sans frontières Bruxelles, asbl), '
            'Belgium'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=180'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['BE'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-10-31',
        'uncertain': True,
        'summary': (
            'Brussels Czech school: preschool/classroom teaching, '
            'literacy, workshops and library administration for at '
            'least five months, September–January or February–June. '
            'Native-level Czech required; teaching background '
            'preferred. Paid housing can be arranged. Host interest: '
            '20 January 2026; late host agreement possible. Portal: 30 '
            'June 2027; availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Brussels Czech school: preschool/classroom teaching, '
                'literacy, workshops and library administration for at '
                'least five months, September–January or '
                'February–June. Native-level Czech required; teaching '
                'background preferred. Paid housing can be arranged.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'For local communication, English, French or Dutch at '
                'A2–B1 is recommended. The host requests a Czech CV '
                'and motivation letter and prefers an online '
                'interview; educational background is preferred rather '
                'than an exclusive degree requirement.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=180; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 180, published call 252: application '
                'opening 2025-10-31; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 181,
        'title': 'Erasmus+ Traineeship, Embassy Baku, Azerbaijan',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=181'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['AZ'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-13',
        'uncertain': True,
        'summary': (
            'Baku embassy: political/security research, social media '
            'and events for at most two months three weeks; up to six '
            'months only with a parallel ADA university agreement. '
            'English and Russian required. No free housing; local '
            'phone/WhatsApp availability needed. Czech citizens only. '
            'Host interest: 20 January 2026; late host agreement '
            'possible. Portal: 30 June 2027; availability unknown. '
            'University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Baku embassy: political/security research, social '
                'media and events for at most two months three weeks; '
                'up to six months only with a parallel ADA university '
                'agreement. English and Russian required. No free '
                'housing; local phone/WhatsApp availability needed.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The ordinary Baku stay is capped at two months and '
                'three weeks; only a parallel placement at ADA under '
                'an inter-university agreement permits up to six '
                'months. The host expects a local SIM or ongoing '
                'WhatsApp availability and offers no own office.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=181; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 181, published call 257: application '
                'opening 2025-11-13; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 182,
        'title': (
            'Erasmus+ Traineeship, Permanent Representation of the '
            'Czech Republic to the EU in Brussels, Belgium'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=182'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['BE'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-13',
        'uncertain': True,
        'summary': (
            'Brussels EU representation: support Council working '
            'groups, negotiations and briefing/event preparation for '
            '3–6 months. English B2 and MS Office required; advanced '
            'relevant-field students targeted without exclusivity. '
            'French advantageous. No free housing. Czech citizens '
            'only. Host interest: 20 January 2026; late host agreement '
            'possible. Portal: 30 June 2027; availability unknown. '
            'University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Brussels EU representation: support Council working '
                'groups, negotiations and briefing/event preparation '
                'for 3–6 months. English B2 and MS Office required; '
                'advanced relevant-field students targeted without '
                'exclusivity. French advantageous. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=182; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 182, published call 254: application '
                'opening 2025-11-13; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 183,
        'title': (
            'Erasmus+ Traineeship, Czech House Buenos Aires, Argentina'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=183'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['AR'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-13',
        'uncertain': True,
        'summary': (
            'Buenos Aires Czech House: adult Czech teaching, community '
            'culture and administration for 6–9 months. Spanish B1–B2 '
            'required; music/dance experience welcomed. No free '
            'housing. A monthly host contribution may be negotiated, '
            'with no amount guaranteed. Host interest: 20 January '
            '2026; late host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Buenos Aires Czech House: adult Czech teaching, '
                'community culture and administration for 6–9 months. '
                'Spanish B1–B2 required; music/dance experience '
                'welcomed. No free housing. A monthly host '
                'contribution may be negotiated, with no amount '
                'guaranteed.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=183; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 183, published call 255: application '
                'opening 2025-11-13; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 184,
        'title': 'Erasmus+ Traineeship, Embassy Brussels, Belgium',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=184'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['BE'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-13',
        'uncertain': True,
        'summary': (
            'Brussels embassy: Belgian political/economic analysis, '
            'consular administration and web/social content for at '
            'least three months in October–December, year unspecified. '
            'English required; French advantageous. No free housing. '
            'Czech citizens only. Host interest: 20 January 2026; late '
            'host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Brussels embassy: Belgian political/economic '
                'analysis, consular administration and web/social '
                'content for at least three months in '
                'October–December, year unspecified. English required; '
                'French advantageous. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=184; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 184, published call 258: application '
                'opening 2025-11-13; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 185,
        'title': (
            'Erasmus+ Traineeship, J. A. Comenius Czech Primary School '
            '(Češka osnovna škola J.A.Komenskog), Daruvar, Croatia'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=185'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['HR'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-13',
        'uncertain': True,
        'summary': (
            'Daruvar J. A. Komensky primary school: Czech teaching, '
            'theatre/dance/puppetry and language competitions for 2–6 '
            'months in January–June 2026, now past. Music/arts '
            'experience welcomed. No free housing. Host interest: 20 '
            'January 2026; late host agreement possible. Portal: 30 '
            'June 2027; availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Daruvar J. A. Komensky primary school: Czech '
                'teaching, theatre/dance/puppetry and language '
                'competitions for 2–6 months in January–June 2026, now '
                'past. Music/arts experience welcomed. No free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=185; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 185, published call 256: application '
                'opening 2025-11-13; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 186,
        'title': (
            'Erasmus+ Traineeship, Consulate General Sao Paulo, Brazil'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=186'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['BR'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-13',
        'uncertain': True,
        'summary': (
            'Sao Paulo consulate: Portuguese translation/interpreting, '
            'official-meeting support and administration for 3–6 '
            'months. Portuguese required, with an additional '
            'Portuguese motivation letter of at most one A4 page. No '
            'free housing. Czech citizens only. Host interest: 20 '
            'January 2026; late host agreement possible. Portal: 30 '
            'June 2027; availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Sao Paulo consulate: Portuguese '
                'translation/interpreting, official-meeting support '
                'and administration for 3–6 months. Portuguese '
                'required, with an additional Portuguese motivation '
                'letter of at most one A4 page. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=186; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 186, published call 259: application '
                'opening 2025-11-13; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 187,
        'title': 'Erasmus+ Traineeship, Embassy Brasília, Brazil',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=187'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['BR'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Brasilia embassy: political/economic research, public '
            'diplomacy, media and administration, with optional '
            'visa/consular work. Two 5–6-month stays or three '
            'four-month stays; Portuguese required. Discounted campus '
            'housing may be available; it is not free. Czech citizens '
            'only. Host interest: 20 January 2026; late host agreement '
            'possible. Portal: 30 June 2027; availability unknown. '
            'University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Brasilia embassy: political/economic research, public '
                'diplomacy, media and administration, with optional '
                'visa/consular work. Two 5–6-month stays or three '
                'four-month stays; Portuguese required. Discounted '
                'campus housing may be available; it is not free.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=187; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 187, published call 260: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 188,
        'title': 'Erasmus+ Traineeship, Embassy Sofia, Bulgaria',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=188'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['BG'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Sofia embassy: political/economic/human-rights research, '
            'diplomatic briefing, events and web/social content for '
            '2–6 months. Web/social experience advantageous. No free '
            'housing. Czech citizens only. Host interest: 20 January '
            '2026; late host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Sofia embassy: political/economic/human-rights '
                'research, diplomatic briefing, events and web/social '
                'content for 2–6 months. Web/social experience '
                'advantageous. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=188; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 188, published call 261: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 189,
        'title': 'Erasmus+ Traineeship, Embassy Podgorica, Montenegro',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=189'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['ME'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Podgorica embassy: political/economic analysis, '
            'public-diplomacy events and reporting for 3–6 months '
            'between September 2026 and June 2027. Balkan languages '
            'advantageous. Some tasks are remote because of limited '
            'office space. No free housing. Czech citizens only. Host '
            'interest: 20 January 2026; late host agreement possible. '
            'Portal: 30 June 2027; availability unknown. University '
            'Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Podgorica embassy: political/economic analysis, '
                'public-diplomacy events and reporting for 3–6 months '
                'between September 2026 and June 2027. Balkan '
                'languages advantageous. Some tasks are remote because '
                'of limited office space. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=189; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 189, published call 262: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 190,
        'title': 'Erasmus+ Traineeship, Embassy Santiago de Chile, Chile',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=190'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['CL'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Santiago embassy: political, economic, cultural and '
            'consular work for 2–4 months, with stays throughout the '
            'year. Spanish required. No free housing. Czech citizens '
            'only. Host interest: 20 January 2026; late host agreement '
            'possible. Portal: 30 June 2027; availability unknown. '
            'University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Santiago embassy: political, economic, cultural and '
                'consular work for 2–4 months, with stays throughout '
                'the year. Spanish required. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=190; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 190, published call 263: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 191,
        'title': 'Erasmus+ Traineeship, Embassy Zagreb, Croatia',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=191'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['HR'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Zagreb embassy: political/economic/cultural work, '
            'meetings, media monitoring and web/social content for 3–5 '
            'months. Croatian and computer skills required. No free '
            'housing. Czech citizens only. Host interest: 20 January '
            '2026; late host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Zagreb embassy: political/economic/cultural work, '
                'meetings, media monitoring and web/social content for '
                '3–5 months. Croatian and computer skills required. No '
                'free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=191; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 191, published call 264: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 192,
        'title': (
            'Erasmus+ Traineeship, Czech School in Copenhagen / '
            'vKodani.cz, Denmark'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=192'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['DK'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Copenhagen Czech school: individual-family teaching, '
            'self-prepared materials and community events for 3–6 '
            'months outside summer; no regular classes currently run. '
            'Danish-school visits depend on agreement. Not knowing '
            'Danish is considered advantageous. No free housing. Host '
            'interest: 20 January 2026; late host agreement possible. '
            'Portal: 30 June 2027; availability unknown. University '
            'Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Copenhagen Czech school: individual-family teaching, '
                'self-prepared materials and community events for 3–6 '
                'months outside summer; no regular classes currently '
                'run. Danish-school visits depend on agreement. Not '
                'knowing Danish is considered advantageous. No free '
                'housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=192; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 192, published call 267: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 193,
        'title': 'Erasmus+ Traineeship, Consulate General Hong Kong',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=193'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['HK'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Hong Kong consulate: cultural, trade and political '
            'diplomacy for six or twelve months. Active involvement in '
            'events and social media expected. No free housing. Czech '
            'citizens only. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Hong Kong consulate: cultural, trade and political '
                'diplomacy for six or twelve months. Active '
                'involvement in events and social media expected. No '
                'free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=193; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 193, published call 265: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 194,
        'title': 'Erasmus+ Traineeship, Embassy Copenhagen, Denmark',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=194'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['DK'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Copenhagen embassy: political/economic analysis, '
            'EU-related meetings and public diplomacy. The page gives '
            '4–5 months but also September–December 2026/January–June '
            '2027 windows. Danish advantageous; Denmark-specific '
            'motivation requested. No free housing. Czech citizens '
            'only. Host interest: 20 January 2026; late host agreement '
            'possible. Portal: 30 June 2027; availability unknown. '
            'University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Copenhagen embassy: political/economic analysis, '
                'EU-related meetings and public diplomacy. The page '
                'gives 4–5 months but also September–December '
                '2026/January–June 2027 windows. Danish advantageous; '
                'Denmark-specific motivation requested. No free '
                'housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The host asks for specific motivation about Denmark '
                'and this placement rather than repeating CV facts; '
                'the stated 4–5-month duration and the wider '
                'advertised calendar windows are retained without '
                'repairing them.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=194; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 194, published call 266: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 195,
        'title': 'Erasmus+ Traineeship, Embassy Cairo, Egypt',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=195'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['EG'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Cairo embassy: political/economic research on Egypt, '
            'Sudan and Eritrea, events and media. At least three '
            'months within 15 May–31 August or 1 September–15 December '
            '2026. English B2 required. Housing offered without rent, '
            'but trainees pay minor fees and utilities. Czech citizens '
            'only. Host interest: 20 January 2026; late host agreement '
            'possible. Portal: 30 June 2027; availability unknown. '
            'University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Cairo embassy: political/economic research on Egypt, '
                'Sudan and Eritrea, events and media. At least three '
                'months within 15 May–31 August or 1 September–15 '
                'December 2026. English B2 required. Housing offered '
                'without rent, but trainees pay minor fees and '
                'utilities.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=195; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 195, published call 268: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 196,
        'title': 'Erasmus+ Traineeship, Bohemica Tbilisi, Georgia',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=196'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['GE'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Tbilisi Bohemica: native-Czech co-teaching, '
            "children's-library activities, oral-history interviews "
            'and community/cultural work. Published stay 1 November '
            '2025–30 June 2026 is past. Teaching, child-work and '
            'organisational skills expected. No free housing. Host '
            'interest: 20 January 2026; late host agreement possible. '
            'Portal: 30 June 2027; availability unknown. University '
            'Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Tbilisi Bohemica: native-Czech co-teaching, '
                "children's-library activities, oral-history "
                'interviews and community/cultural work. Published '
                'stay 1 November 2025–30 June 2026 is past. Teaching, '
                'child-work and organisational skills expected. No '
                'free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=196; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 196, published call 269: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 197,
        'title': 'Erasmus+ Traineeship, Embassy Tallinn, Estonia',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=197'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['EE'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Tallinn embassy: research, export support, meetings and '
            'cultural events for six months, January–June or '
            'July–December 2026. English C1 required; Czech motivation '
            'letter requested. No free housing. Czech citizens only. '
            'Host interest: 20 January 2026; late host agreement '
            'possible. Portal: 30 June 2027; availability unknown. '
            'University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Tallinn embassy: research, export support, meetings '
                'and cultural events for six months, January–June or '
                'July–December 2026. English C1 required; Czech '
                'motivation letter requested. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=197; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 197, published call 270: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 198,
        'title': (
            'Erasmus+ Traineeship, Czech School Milan / Association of '
            'Compatriots and Friends of the Czech Republic in Milan, '
            'Italy'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=198'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['IT'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Milan Czech school: differentiated teaching support, '
            'projects, adult/online lessons and web/social work. '
            'Windows February–May 2026, October 2026–January 2027 and '
            'February–May 2027. Basic Italian required. No free '
            'housing. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Milan Czech school: differentiated teaching support, '
                'projects, adult/online lessons and web/social work. '
                'Windows February–May 2026, October 2026–January 2027 '
                'and February–May 2027. Basic Italian required. No '
                'free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=198; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 198, published call 271: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 199,
        'title': (
            'Erasmus+ Traineeship, Jaroslav Hašek Grammar School, '
            'Huluboaia (Holuboje), Cahul District, Moldova'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=199'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['MD'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            "Huluboaia Czech school: children's/adult Czech lessons, "
            'materials, folklore and community activities. Start 15 '
            'October, with year and duration unspecified. Russian or '
            'Romanian required. Free housing offered. Host interest: '
            '20 January 2026; late host agreement possible. Portal: 30 '
            'June 2027; availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                "Huluboaia Czech school: children's/adult Czech "
                'lessons, materials, folklore and community '
                'activities. Start 15 October, with year and duration '
                'unspecified. Russian or Romanian required. Free '
                'housing offered.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=199; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 199, published call 272: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 200,
        'title': 'Erasmus+ Traineeship, Embassy Addis Ababa, Ethiopia',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=200'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['ET'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Addis Ababa embassy: political/economic work for 2–3 '
            'months with free housing. The source requires completed '
            'BA, post-Soviet knowledge, English B2 and Russian B1+; '
            'these unusual location-related conditions remain as '
            'published. Czech citizens only. Host interest: 20 January '
            '2026; late host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Addis Ababa embassy: political/economic work for 2–3 '
                'months with free housing. The source requires '
                'completed BA, post-Soviet knowledge, English B2 and '
                'Russian B1+; these unusual location-related '
                'conditions remain as published.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=200; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 200, published call 273: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 201,
        'title': (
            'Erasmus+ Traineeship, Czech School Without Borders '
            '(Rhein-Main / czentrum gUG), Frankfurt am Main, Germany'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=201'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Frankfurt Rhein-Main school: Czech/preschool teaching and '
            'organisational, library and marketing work, 35–40 hours '
            'weekly. Ideally August 2026–June 2027; a semester is '
            'possible. German A1 or communicative English plus '
            'computer/social skills required. No free housing. Host '
            'interest: 20 January 2026; late host agreement possible. '
            'Portal: 30 June 2027; availability unknown. University '
            'Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Frankfurt Rhein-Main school: Czech/preschool teaching '
                'and organisational, library and marketing work, 35–40 '
                'hours weekly. Ideally August 2026–June 2027; a '
                'semester is possible. German A1 or communicative '
                'English plus computer/social skills required. No free '
                'housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'Computer, social-media, Microsoft Word and PowerPoint '
                'skills are required. The host offers preschool and '
                'school teaching plus administration, logistics, '
                'accounting, library and marketing tasks; published '
                'pupil numbers do not create separate awards.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=201; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 201, published call 275: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 202,
        'title': 'Erasmus+ Traineeship, Embassy Helsinki, Finland',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=202'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['FI'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Helsinki embassy: political/economic research, export '
            'support, meetings and administration for six months, '
            'July–December 2026. Finnish welcomed, not required; CV '
            'and motivation letter in English. No free housing. Czech '
            'citizens only. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Helsinki embassy: political/economic research, export '
                'support, meetings and administration for six months, '
                'July–December 2026. Finnish welcomed, not required; '
                'CV and motivation letter in English. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=202; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 202, published call 274: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 203,
        'title': 'Erasmus+ Traineeship, Embassy Paris, France',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=203'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['FR'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Paris embassy: public diplomacy, economic analysis or '
            'political research, events and media work for 3–12 '
            'months. French required. No free housing. Czech citizens '
            'only. Host interest: 20 January 2026; late host agreement '
            'possible. Portal: 30 June 2027; availability unknown. '
            'University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Paris embassy: public diplomacy, economic analysis or '
                'political research, events and media work for 3–12 '
                'months. French required. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=203; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 203, published call 276: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 204,
        'title': (
            'Erasmus+ Traineeship, Czech School Without Borders '
            '(Tschechische Schule ohne Grenzen e. V.), Munich, Germany'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=204'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Munich Czech school: teaching, library, events and '
            'publicity for at least a semester within February '
            '2025–July 2026, now past. German basics advantageous; '
            'teamwork and autonomy expected. Family lodging may be '
            'arranged for low rent and utilities; no free housing. '
            'Host interest: 20 January 2026; late host agreement '
            'possible. Portal: 30 June 2027; availability unknown. '
            'University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Munich Czech school: teaching, library, events and '
                'publicity for at least a semester within February '
                '2025–July 2026, now past. German basics advantageous; '
                'teamwork and autonomy expected. Family lodging may be '
                'arranged for low rent and utilities; no free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=204; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 204, published call 277: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 205,
        'title': 'Erasmus+ Traineeship, Embassy Accra, Ghana',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=205'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['GH'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Accra embassy: political, economic, development, press '
            'and consular work for 3–6 months. French advantageous. '
            'Embassy lodging is only a conditional possibility for a '
            'nominal fee, not free housing. Czech citizens only. Host '
            'interest: 20 January 2026; late host agreement possible. '
            'Portal: 30 June 2027; availability unknown. University '
            'Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Accra embassy: political, economic, development, '
                'press and consular work for 3–6 months. French '
                'advantageous. Embassy lodging is only a conditional '
                'possibility for a nominal fee, not free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=205; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 205, published call 278: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 206,
        'title': 'Erasmus+ Traineeship, Embassy Tbilisi, Georgia',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=206'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['GE'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Tbilisi embassy: political/economic/public-diplomacy '
            'research, reporting, meetings and some consular work for '
            '3–6 months in autumn 2026 or spring–summer 2027. Russian '
            'advantageous, not required. No free housing. Czech '
            'citizens only. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Tbilisi embassy: political/economic/public-diplomacy '
                'research, reporting, meetings and some consular work '
                'for 3–6 months in autumn 2026 or spring–summer 2027. '
                'Russian advantageous, not required. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=206; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 206, published call 279: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 207,
        'title': 'Erasmus+ Traineeship, Embassy Rome, Italy',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=207'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['IT'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Rome embassy: Malta briefings and event support for 3–4 '
            'months from July 2026. Remote participation is possible, '
            'so physical travel to Italy is not guaranteed; own '
            'computer required. Italian and Italian motivation '
            'welcomed. No free housing. Czech citizens only. Host '
            'interest: 20 January 2026; late host agreement possible. '
            'Portal: 30 June 2027; availability unknown. University '
            'Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Rome embassy: Malta briefings and event support for '
                '3–4 months from July 2026. Remote participation is '
                'possible, so physical travel to Italy is not '
                'guaranteed; own computer required. Italian and '
                'Italian motivation welcomed. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=207; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 207, published call 280: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 208,
        'title': 'Erasmus+ Traineeship, Embassy Tokyo, Japan',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=208'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['JP'],
        'eligible': ['CZ'],
        'deadline': None,
        'opening': None,
        'uncertain': True,
        'summary': (
            'Tokyo embassy: analysis and event work across embassy '
            'sections for three months in 2026/27. Japanese '
            'recommended. No free housing; approval usually 6–8 weeks. '
            'No portal call is published for this otherwise '
            'substantive placement. Czech citizens only. Host '
            'interest: 20 January 2026, with late agreement possible; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Tokyo embassy: analysis and event work across embassy '
                'sections for three months in 2026/27. Japanese '
                'recommended. No free housing; approval usually 6–8 '
                'weeks. No portal call is published for this otherwise '
                'substantive placement.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The English programme publishes no selected portal '
                'call; the linked Czech leaf supplies the actual '
                '2026/27 three-month offer. No portal deadline is '
                'inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=208; this query-sensitive URL preserves the '
                'source identity.'
            ),
        ],
    },
    {
        'programme': 209,
        'title': 'Erasmus+ Traineeship, Embassy Pretoria, South Africa',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=209'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['ZA'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Pretoria embassy: political/economic reports, events, '
            'social media and operational/consular support for at most '
            'three months. Portuguese or French recommended. No free '
            'housing. Czech citizens only. Host interest: 20 January '
            '2026; late host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Pretoria embassy: political/economic reports, events, '
                'social media and operational/consular support for at '
                'most three months. Portuguese or French recommended. '
                'No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=209; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 209, published call 281: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 210,
        'title': (
            'Erasmus+ Traineeship, Czech School in Regensburg '
            '(Tschechische Schule in Regensburg e. V.), Germany'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=210'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Regensburg Czech school: preschool/school teaching, '
            'association administration, events and creative community '
            'projects. Ideally mid-September–late July, year '
            'unspecified. Pedagogical grounding or experience '
            'expected; a pedagogy degree is not mandatory. No free '
            'housing. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Regensburg Czech school: preschool/school teaching, '
                'association administration, events and creative '
                'community projects. Ideally mid-September–late July, '
                'year unspecified. Pedagogical grounding or experience '
                'expected; a pedagogy degree is not mandatory. No free '
                'housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=210; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 210, published call 283: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 211,
        'title': 'Erasmus+ Traineeship, Embassy Seoul, South Korea',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=211'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['KR'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Seoul embassy: political/economic reports, translation, '
            'social content and events for at least four months within '
            'June–December 2026. Korean advantageous. Initially only '
            'CV and motivation letter requested. No free housing. '
            'Czech citizens only. Host interest: 20 January 2026; late '
            'host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Seoul embassy: political/economic reports, '
                'translation, social content and events for at least '
                'four months within June–December 2026. Korean '
                'advantageous. Initially only CV and motivation letter '
                'requested. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=211; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 211, published call 282: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 212,
        'title': (
            'Erasmus+ Traineeship, Czech School Porto / Club of Czechs '
            'and Slovaks in Portugal, Portugal'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=212'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['PT'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Porto Czech school: weekly Czech lessons, teaching '
            'materials and weekend community activities for 2–10 '
            'months. Affinity with young children expected; relevant '
            'teaching fields preferred. No free housing. Host '
            'interest: 20 January 2026; late host agreement possible. '
            'Portal: 30 June 2027; availability unknown. University '
            'Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Porto Czech school: weekly Czech lessons, teaching '
                'materials and weekend community activities for 2–10 '
                'months. Affinity with young children expected; '
                'relevant teaching fields preferred. No free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=212; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 212, published call 284: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 213,
        'title': (
            'Erasmus+ Traineeship, Komenský School Association '
            'Realgymnasium, Vienna, Austria'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=213'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['AT'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Vienna Komensky school: after-school administration, '
            'afternoon lessons, projects and excursions from September '
            '2026 to June 2027. German B2 and proof of study required. '
            'No free housing. Host interest: 20 January 2026; late '
            'host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Vienna Komensky school: after-school administration, '
                'afternoon lessons, projects and excursions from '
                'September 2026 to June 2027. German B2 and proof of '
                'study required. No free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=213; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 213, published call 285: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 214,
        'title': (
            'Erasmus+ Traineeship, Czech Compatriot Association in '
            'Athens / Czech School in Athens, Greece'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=214'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['GR'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Athens Czech school/compatriot association: teaching, '
            'materials, library, events and social content. Longer '
            'September–December, January–June or September–June stays '
            'preferred; years unspecified. Communicative English '
            'required. Housing costs about EUR 400/month. Host '
            'interest: 20 January 2026; late host agreement possible. '
            'Portal: 30 June 2027; availability unknown. University '
            'Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Athens Czech school/compatriot association: teaching, '
                'materials, library, events and social content. Longer '
                'September–December, January–June or September–June '
                'stays preferred; years unspecified. Communicative '
                'English required. Housing costs about EUR 400/month.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=214; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 214, published call 286: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 215,
        'title': (
            'Erasmus+ Traineeship, Czech Association in Greece (Czech '
            'Association Greece), Athens, Greece'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=215'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['GR'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Athens Czech Association: online teaching, materials, '
            'podcasts and community events for at least four months, '
            'January–June 2026 or late August 2026–February 2027. At '
            'least third-year or follow-on MA student; Czech, English '
            'A2 and computer skills required. Housing help, not free '
            'accommodation. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Athens Czech Association: online teaching, materials, '
                'podcasts and community events for at least four '
                'months, January–June 2026 or late August '
                '2026–February 2027. At least third-year or follow-on '
                'MA student; Czech, English A2 and computer skills '
                'required. Housing help, not free accommodation.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'An online interview is part of selection. Child-work '
                'experience, music and singing are preferred rather '
                'than mandatory; the school helps arrange housing but '
                'does not offer it free.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=215; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 215, published call 287: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 216,
        'title': (
            'Erasmus+ Traineeship, Greek-Czech Friendship Association, '
            'Thessaloniki, Greece'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=216'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['GR'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Thessaloniki association: Czech teaching, workshops, '
            'library, events and social content for six months from 15 '
            'January 2026, now past. English B2 and Czech '
            'CV/motivation required. No free housing. Host interest: '
            '20 January 2026; late host agreement possible. Portal: 30 '
            'June 2027; availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Thessaloniki association: Czech teaching, workshops, '
                'library, events and social content for six months '
                'from 15 January 2026, now past. English B2 and Czech '
                'CV/motivation required. No free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=216; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 216, published call 288: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 217,
        'title': (
            'Erasmus+ Traineeship, Greek-Czech Friendship Association, '
            'Thessaloniki, Greece'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=217'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['SE'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'The English title says Thessaloniki, but the linked Czech '
            "material describes Stockholm's Cestina hrou/STSF school: "
            'Sunday Czech teaching and possible local-school '
            'cooperation, mid-January–mid-June 2026, now past. Nordic '
            'language advantageous. No free housing; title/location '
            'conflict retained. Host interest: 20 January 2026; late '
            'host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'The English title says Thessaloniki, but the linked '
                "Czech material describes Stockholm's Cestina "
                'hrou/STSF school: Sunday Czech teaching and possible '
                'local-school cooperation, mid-January–mid-June 2026, '
                'now past. Nordic language advantageous. No free '
                'housing; title/location conflict retained.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original English programme title names '
                'Thessaloniki, Greece, while the exact linked Czech ID '
                '217 leaf describes Stockholm, Sweden, and '
                'STSF/Cestina hrou. This identity remains separate '
                'from the actual Thessaloniki placement 216, and the '
                'disagreement is disclosed rather than silently '
                'corrected.'
            ),
            (
                'Swedish or another Nordic language is advantageous, '
                'not mandatory. Young-child work, communication and '
                'creativity are expected; possible '
                'university/local-school cooperation is not guaranteed.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=217; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 217, published call 289: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 218,
        'title': (
            'Erasmus+ Traineeship, Czech School Without Borders '
            'Zurich, Switzerland'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=218'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['CH'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Zurich Czech school: primary/preschool teaching, '
            'workshops, materials and social content, 23 August '
            '2026–31 January 2027 or 22 February–7 July 2027. German '
            'B1+ advantageous. No free housing. Host interest: 20 '
            'January 2026; late host agreement possible. Portal: 30 '
            'June 2027; availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Zurich Czech school: primary/preschool teaching, '
                'workshops, materials and social content, 23 August '
                '2026–31 January 2027 or 22 February–7 July 2027. '
                'German B1+ advantageous. No free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=218; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 218, published call 290: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 219,
        'title': (
            'Erasmus+ Traineeship, Czech School Without Borders '
            'Geneva, Switzerland'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=219'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['CH'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Geneva Czech school: Czech curriculum teaching, school '
            'journalism, events, online support and administration, '
            'mid-September–late January or February–late June; years '
            'unspecified. Native-level Czech required; French/computer '
            'skills advantageous. No free housing. Host interest: 20 '
            'January 2026; late host agreement possible. Portal: 30 '
            'June 2027; availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Geneva Czech school: Czech curriculum teaching, '
                'school journalism, events, online support and '
                'administration, mid-September–late January or '
                'February–late June; years unspecified. Native-level '
                'Czech required; French/computer skills advantageous. '
                'No free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=219; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 219, published call 291: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 220,
        'title': 'Erasmus+ Traineeship, Okénko, London, United Kingdom',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=220'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['GB'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'London Okenko school: teaching support, parent '
            'communication, materials, administration and library work '
            'for 6–12 months. IT skills required. No free housing. A '
            'visa is required; arrangements need checking with the UK '
            'authorities. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'London Okenko school: teaching support, parent '
                'communication, materials, administration and library '
                'work for 6–12 months. IT skills required. No free '
                'housing. A visa is required; arrangements need '
                'checking with the UK authorities.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=220; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 220, published call 292: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 221,
        'title': 'Erasmus+ Traineeship, Czech Centre Brussels, Belgium',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=221'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['BE'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Brussels Czech Centre: cultural production, director '
            'assistance or communications for 4–9 months. Excellent '
            'English required; French/German advantageous. No free '
            'housing. The three role types are parts of this placement '
            'framework. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Brussels Czech Centre: cultural production, director '
                'assistance or communications for 4–9 months. '
                'Excellent English required; French/German '
                'advantageous. No free housing. The three role types '
                'are parts of this placement framework.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=221; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 221, published call 293: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 222,
        'title': 'Erasmus+ Traineeship, Czech Centre Sofia, Bulgaria',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=222'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['BG'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Sofia Czech Centre: PR, web/social content, cultural '
            'projects and events for 3–6 months. Excellent Czech, '
            'English B2 and Facebook/Instagram experience required; '
            'photography and Bulgarian/another Slavic language '
            'advantageous. No free housing. Host interest: 20 January '
            '2026; late host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Sofia Czech Centre: PR, web/social content, cultural '
                'projects and events for 3–6 months. Excellent Czech, '
                'English B2 and Facebook/Instagram experience '
                'required; photography and Bulgarian/another Slavic '
                'language advantageous. No free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=222; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 222, published call 294: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 223,
        'title': 'Erasmus+ Traineeship, Czech Centre Cairo, Egypt',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=223'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['EG'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Cairo Czech Centre: translation/PR, '
            'graphics/documentation or production coordination for 2–6 '
            'months. Excellent Czech/English required; Arabic '
            'translation is role-specific, not a universal '
            'requirement. Housing may be arranged for a nominal price, '
            'not free. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Cairo Czech Centre: translation/PR, '
                'graphics/documentation or production coordination for '
                '2–6 months. Excellent Czech/English required; Arabic '
                'translation is role-specific, not a universal '
                'requirement. Housing may be arranged for a nominal '
                'price, not free.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=223; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 223, published call 295: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 224,
        'title': 'Erasmus+ Traineeship, Czech Centre Paris, France',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=224'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['FR'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Paris Czech Centre: cultural administration or '
            'graphics/media work for 3–6 months. The general language '
            'field requires French B2, while the graphics role calls '
            'basic French advantageous; this conflict remains '
            'unresolved. No free housing. Host interest: 20 January '
            '2026; late host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Paris Czech Centre: cultural administration or '
                'graphics/media work for 3–6 months. The general '
                'language field requires French B2, while the graphics '
                'role calls basic French advantageous; this conflict '
                'remains unresolved. No free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=224; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 224, published call 296: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 225,
        'title': 'Erasmus+ Traineeship, Czech Centre Tbilisi, Georgia',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=225'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['GE'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Tbilisi Czech Centre: PR, graphics, web/social content, '
            'events, administration and Czech–English translation for '
            '3–6 months. Advanced Czech and English B2 required. No '
            'free housing. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Tbilisi Czech Centre: PR, graphics, web/social '
                'content, events, administration and Czech–English '
                'translation for 3–6 months. Advanced Czech and '
                'English B2 required. No free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=225; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 225, published call 297: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 226,
        'title': 'Erasmus+ Traineeship, Czech Centre Milan, Italy',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=226'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['IT'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Milan Czech Centre: PR, social/web content, Italian media '
            'monitoring, events and Italian–Czech translation for 3–6 '
            'months. Advanced Czech and Italian B2 required. No free '
            'housing. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Milan Czech Centre: PR, social/web content, Italian '
                'media monitoring, events and Italian–Czech '
                'translation for 3–6 months. Advanced Czech and '
                'Italian B2 required. No free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=226; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 226, published call 298: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 227,
        'title': 'Erasmus+ Traineeship, Czech Centre Rome, Italy',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=227'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['IT'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Rome Czech Centre: web/social content, Italian media '
            'monitoring, events, administration and Italian–Czech '
            'translation for 3–6 months. Italian B2 required. No free '
            'housing. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Rome Czech Centre: web/social content, Italian media '
                'monitoring, events, administration and Italian–Czech '
                'translation for 3–6 months. Italian B2 required. No '
                'free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=227; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 227, published call 299: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 228,
        'title': 'Erasmus+ Traineeship, Czech Centre Tel Aviv, Israel',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=228'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['IL'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Tel Aviv Czech Centre/Czech House Jerusalem: cultural '
            'events, English–Czech translation, PR and administration '
            'for 2–3 months, with activities also in Jerusalem. '
            'Excellent Czech/English required. No free housing. Host '
            'interest: 20 January 2026; late host agreement possible. '
            'Portal: 30 June 2027; availability unknown. University '
            'Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Tel Aviv Czech Centre/Czech House Jerusalem: cultural '
                'events, English–Czech translation, PR and '
                'administration for 2–3 months, with activities also '
                'in Jerusalem. Excellent Czech/English required. No '
                'free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=228; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 228, published call 300: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 229,
        'title': 'Erasmus+ Traineeship, Czech Centre Tokyo, Japan',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=229'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['JP'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Tokyo Czech Centre: PR, media/social/web content, '
            'reception, events and Japanese–Czech translation for 3–6 '
            'months. Advanced Czech and Japanese B2/N2 required. No '
            'free housing. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Tokyo Czech Centre: PR, media/social/web content, '
                'reception, events and Japanese–Czech translation for '
                '3–6 months. Advanced Czech and Japanese B2/N2 '
                'required. No free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=229; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 229, published call 301: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 230,
        'title': 'Erasmus+ Traineeship, Czech Centre Seoul, South Korea',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=230'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['KR'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Seoul Czech Centre: cultural projects, graphics/posters, '
            'media and social content for 3–6 months. Excellent Czech '
            'grammar plus advanced Korean OR graphic-design experience '
            'required; Korean translation only for Korean-speaking '
            'trainees. No free housing. Published details do not '
            'confirm a host deadline or grant entitlement. Portal '
            'closes 30 June 2027; availability remains unknown.'
        ),
        'evidence': [
            (
                'Seoul Czech Centre: cultural projects, '
                'graphics/posters, media and social content for 3–6 '
                'months. Excellent Czech grammar plus advanced Korean '
                'OR graphic-design experience required; Korean '
                'translation only for Korean-speaking trainees. No '
                'free housing. Published details do not confirm a host '
                'deadline or grant entitlement. Portal closes 30 June '
                '2027; availability remains unknown.'
            ),
            (
                'The application and funding section of this specific '
                'Czech leaf is empty. The English portal call closes '
                'on 30 June 2027, but that alone establishes neither a '
                'host-interest deadline nor a funding entitlement or '
                'open availability.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=230; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 230, published call 302: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 231,
        'title': 'Erasmus+ Traineeship, Czech Centre Budapest, Hungary',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=231'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['HU'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Budapest Czech Centre: graphics, PR/marketing, social '
            'content, events and administration for 4–9 months. '
            'Excellent Czech required. An extra housing/fare '
            'contribution is offered, but no amount or free '
            'accommodation is promised. Host interest: 20 January '
            '2026; late host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Budapest Czech Centre: graphics, PR/marketing, social '
                'content, events and administration for 4–9 months. '
                'Excellent Czech required. An extra housing/fare '
                'contribution is offered, but no amount or free '
                'accommodation is promised.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=231; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 231, published call 303: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 232,
        'title': 'Erasmus+ Traineeship, Czech Centre Berlin, Germany',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=232'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Berlin Czech Centre: translation/PR/cultural programme '
            'work or graphics/photo/video/audio documentation for 5–9 '
            'months. German B2/C1 required. No free housing; Chemnitz '
            '2025 is a historical example, not a new event call. Host '
            'interest: 20 January 2026; late host agreement possible. '
            'Portal: 30 June 2027; availability unknown. University '
            'Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Berlin Czech Centre: translation/PR/cultural '
                'programme work or graphics/photo/video/audio '
                'documentation for 5–9 months. German B2/C1 required. '
                'No free housing; Chemnitz 2025 is a historical '
                'example, not a new event call.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=232; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 232, published call 304: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 233,
        'title': 'Erasmus+ Traineeship, Czech Centre Munich, Germany',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=233'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Munich Czech Centre: translation/PR/cultural work or '
            'graphics/photo/video production for 3–6 months. '
            'Czech/German B1/B2, computer skills and reliability '
            'expected; social-media experience advantageous. No free '
            'housing. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Munich Czech Centre: translation/PR/cultural work or '
                'graphics/photo/video production for 3–6 months. '
                'Czech/German B1/B2, computer skills and reliability '
                'expected; social-media experience advantageous. No '
                'free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=233; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 233, published call 305: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 234,
        'title': 'Erasmus+ Traineeship, Czech Centre Warsaw, Poland',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=234'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['PL'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Warsaw Czech Centre: cultural events, publicity and PR '
            'for 3–6 months. Good Czech and Polish required. No free '
            'housing. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Warsaw Czech Centre: cultural events, publicity and '
                'PR for 3–6 months. Good Czech and Polish required. No '
                'free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=234; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 234, published call 306: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 235,
        'title': 'Erasmus+ Traineeship, Czech Centre Vienna, Austria',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=235'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['AT'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Vienna Czech Centre: translation, web/social content, '
            'projects and administration for 4–6 months. Czech/German '
            'B2 and good English required. Prior graphics experience '
            'is not required; social experience advantageous. No free '
            'housing. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Vienna Czech Centre: translation, web/social content, '
                'projects and administration for 4–6 months. '
                'Czech/German B2 and good English required. Prior '
                'graphics experience is not required; social '
                'experience advantageous. No free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=235; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 235, published call 307: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 236,
        'title': 'Erasmus+ Traineeship, Czech Centre Bucharest, Romania',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=236'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['RO'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Bucharest Czech Centre: research, communication, PR, '
            'event documentation and creative cultural work in '
            'February–June or September–December, years unspecified. '
            'Excellent Czech plus English OR Romanian required. Free '
            'housing offered. Host interest: 20 January 2026; late '
            'host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Bucharest Czech Centre: research, communication, PR, '
                'event documentation and creative cultural work in '
                'February–June or September–December, years '
                'unspecified. Excellent Czech plus English OR Romanian '
                'required. Free housing offered.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=236; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 236, published call 308: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 237,
        'title': (
            'Erasmus+ Traineeship, Czech Centre Bratislava, Slovakia'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=237'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['SK'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Bratislava Czech Centre: PR, marketing, graphics, '
            'administration and production. January–March, April–June '
            'and September–December stays need at least three months; '
            'July–August permits one month, conflicting with the '
            'shared 2–12-month rule. Free housing offered. Host '
            'interest: 20 January 2026; late host agreement possible. '
            'Portal: 30 June 2027; availability unknown. University '
            'Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Bratislava Czech Centre: PR, marketing, graphics, '
                'administration and production. January–March, '
                'April–June and September–December stays need at least '
                'three months; July–August permits one month, '
                'conflicting with the shared 2–12-month rule. Free '
                'housing offered.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=237; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 237, published call 309: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 238,
        'title': 'Erasmus+ Traineeship, Czech Centre Madrid, Spain',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=238'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['ES'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Madrid Czech Centre: events, Spanish-partner '
            'communication, web/newsletters, library, translation and '
            'documentation for 3–6 months. Excellent Czech and '
            'spoken/written Spanish B2 required. No free housing. Host '
            'interest: 20 January 2026; late host agreement possible. '
            'Portal: 30 June 2027; availability unknown. University '
            'Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Madrid Czech Centre: events, Spanish-partner '
                'communication, web/newsletters, library, translation '
                'and documentation for 3–6 months. Excellent Czech and '
                'spoken/written Spanish B2 required. No free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=238; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 238, published call 310: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 239,
        'title': 'Erasmus+ Traineeship, Czech Centre Belgrade, Serbia',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=239'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['RS'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Belgrade Czech Centre: project production or '
            'graphics/photo/video work, 30 hours weekly. At least '
            'three months, preferably four; September–December, '
            'January–April or May–August, years unspecified. Excellent '
            'Czech and good English; Serbian advantageous. No free '
            'housing. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Belgrade Czech Centre: project production or '
                'graphics/photo/video work, 30 hours weekly. At least '
                'three months, preferably four; September–December, '
                'January–April or May–August, years unspecified. '
                'Excellent Czech and good English; Serbian '
                'advantageous. No free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The role descriptions state at least three months and '
                'prefer four; the separate stay field lists four-month '
                'windows. The 30-hour weekly schedule applies to both '
                'production and graphics roles.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=239; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 239, published call 311: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 240,
        'title': 'Erasmus+ Traineeship, Czech Centre Stockholm, Sweden',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=240'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['SE'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Stockholm Czech Centre: marketing, translation, PR, '
            'graphics/photo documentation and cultural production for '
            '4–6 months. English B2/C1 required. No free housing; '
            'distinct from the Stockholm school placement. Host '
            'interest: 20 January 2026; late host agreement possible. '
            'Portal: 30 June 2027; availability unknown. University '
            'Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Stockholm Czech Centre: marketing, translation, PR, '
                'graphics/photo documentation and cultural production '
                'for 4–6 months. English B2/C1 required. No free '
                'housing; distinct from the Stockholm school placement.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=240; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 240, published call 312: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 241,
        'title': 'Erasmus+ Traineeship, Czech Centre New York, USA',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=241'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['US'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'New York Czech Centre: cultural-event '
            'production/communications or library cataloguing, Tritius '
            'and research work for a three-month quarter. Excellent '
            'English and text/audio/video/social computer skills '
            'required. No free housing. Host interest: 20 January '
            '2026; late host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'New York Czech Centre: cultural-event '
                'production/communications or library cataloguing, '
                'Tritius and research work for a three-month quarter. '
                'Excellent English and text/audio/video/social '
                'computer skills required. No free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=241; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 241, published call 313: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 242,
        'title': (
            'Erasmus+ Traineeship, Czech Centre London, United Kingdom'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=242'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['GB'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'London Czech Centre: cultural events, administration, '
            'research and English–Czech translation for 3–6 months. '
            'Very good Czech/English required. No free housing. UK '
            'visa and third-country Erasmus arrangements require '
            'individual university/UK-authority coordination. Host '
            'interest: 20 January 2026; late host agreement possible. '
            'Portal: 30 June 2027; availability unknown. University '
            'Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'London Czech Centre: cultural events, administration, '
                'research and English–Czech translation for 3–6 '
                'months. Very good Czech/English required. No free '
                'housing. UK visa and third-country Erasmus '
                'arrangements require individual '
                'university/UK-authority coordination.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=242; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 242, published call 314: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 243,
        'title': 'Erasmus+ Traineeship, Czech Centre Hanoi, Vietnam',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=243'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['VN'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-14',
        'uncertain': True,
        'summary': (
            'Hanoi Czech Centre: PR/translation/social content, '
            'graphics/documentation or programme production for 3–6 '
            'months. Excellent Czech/English required; Vietnamese '
            'translation is role-specific, not a universal '
            'prerequisite. No free housing. Host interest: 20 January '
            '2026; late host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Hanoi Czech Centre: PR/translation/social content, '
                'graphics/documentation or programme production for '
                '3–6 months. Excellent Czech/English required; '
                'Vietnamese translation is role-specific, not a '
                'universal prerequisite. No free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=243; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 243, published call 315: application '
                'opening 2025-11-14; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 244,
        'title': 'Erasmus+ Traineeship, Embassy Ottawa, Canada',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=244'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['CA'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-20',
        'uncertain': True,
        'summary': (
            'Ottawa embassy: trade/economic research, export projects, '
            'events and social content for 3–5 months. French basics '
            'recommended, not compulsory; motivation interview '
            'required. No free housing. Czech citizens only. Host '
            'interest: 20 January 2026; late host agreement possible. '
            'Portal: 30 June 2027; availability unknown. University '
            'Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Ottawa embassy: trade/economic research, export '
                'projects, events and social content for 3–5 months. '
                'French basics recommended, not compulsory; motivation '
                'interview required. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=244; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 244, published call 316: application '
                'opening 2025-11-20; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 245,
        'title': 'Erasmus+ Traineeship, Embassy Nairobi, Kenya',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=245'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['KE'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-20',
        'uncertain': True,
        'summary': (
            'Nairobi embassy: political/economic analysis, cultural '
            'events and administration within summer 2026–summer 2027; '
            'individual duration unspecified. University students with '
            'completed BA, active Czech and advanced English required. '
            'No free housing. Czech citizens only. Host interest: 20 '
            'January 2026; late host agreement possible. Portal: 30 '
            'June 2027; availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Nairobi embassy: political/economic analysis, '
                'cultural events and administration within summer '
                '2026–summer 2027; individual duration unspecified. '
                'University students with completed BA, active Czech '
                'and advanced English required. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=245; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 245, published call 317: application '
                'opening 2025-11-20; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 246,
        'title': 'Erasmus+ Traineeship, Embassy Bogota, Colombia',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=246'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['CO'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-20',
        'uncertain': True,
        'summary': (
            'Bogota embassy: political/economic reporting, EU '
            'coordination, human rights and events for 4–5 months in '
            'January–June or September–December, years unspecified. '
            'Spanish B2 and Spanish motivation required; ID-copy '
            'consent, recent criminal record and study proof needed. '
            'No free housing. Czech citizens only. Host interest: 20 '
            'January 2026; late host agreement possible. Portal: 30 '
            'June 2027; availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Bogota embassy: political/economic reporting, EU '
                'coordination, human rights and events for 4–5 months '
                'in January–June or September–December, years '
                'unspecified. Spanish B2 and Spanish motivation '
                'required; ID-copy consent, recent criminal record and '
                'study proof needed. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=246; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 246, published call 318: application '
                'opening 2025-11-20; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 247,
        'title': 'Erasmus+ Traineeship, Embassy Nicosia, Cyprus',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=247'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['CY'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-20',
        'uncertain': True,
        'summary': (
            'Nicosia embassy: political/economic media research, '
            'meetings, events, export support and social content for '
            'six months, 1 July–31 December 2026. Category B driving '
            'licence advantageous. No free housing; citizenship '
            'eligibility is unspecified. Host interest: 20 January '
            '2026; late host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Nicosia embassy: political/economic media research, '
                'meetings, events, export support and social content '
                'for six months, 1 July–31 December 2026. Category B '
                'driving licence advantageous. No free housing; '
                'citizenship eligibility is unspecified.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=247; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 247, published call 319: application '
                'opening 2025-11-20; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 248,
        'title': 'Erasmus+ Traineeship, Embassy Vilnius, Lithuania',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=248'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['LT'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-20',
        'uncertain': True,
        'summary': (
            'Vilnius embassy: political/economic news, web/social '
            'content and events for 2–4 months in September–December '
            '2026 or stated 2027 intervals through August. '
            'Russian/Lithuanian and web-editing experience '
            'advantageous. No free housing. Czech citizens only. Host '
            'interest: 20 January 2026; late host agreement possible. '
            'Portal: 30 June 2027; availability unknown. University '
            'Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Vilnius embassy: political/economic news, web/social '
                'content and events for 2–4 months in '
                'September–December 2026 or stated 2027 intervals '
                'through August. Russian/Lithuanian and web-editing '
                'experience advantageous. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=248; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 248, published call 320: application '
                'opening 2025-11-20; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 249,
        'title': 'Erasmus+ Traineeship, Embassy Luxembourg, Luxembourg',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=249'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['LU'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-20',
        'uncertain': True,
        'summary': (
            'Luxembourg embassy: diplomatic operations, events, media '
            'and social content for three-month quarters in 2027. '
            'French, possibly German, and Canva/organisation skills '
            'expected. Free embassy-room housing offered. One interval '
            "ends on invalid '31 September 2027'; no corrected date is "
            'inferred. Czech citizens only. Host interest: 20 January '
            '2026; late host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Luxembourg embassy: diplomatic operations, events, '
                'media and social content for three-month quarters in '
                '2027. French, possibly German, and Canva/organisation '
                'skills expected. Free embassy-room housing offered. '
                "One interval ends on invalid '31 September 2027'; no "
                'corrected date is inferred.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=249; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 249, published call 321: application '
                'opening 2025-11-20; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 250,
        'title': 'Erasmus+ Traineeship, Embassy Budapest, Hungary',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=250'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['HU'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-20',
        'uncertain': True,
        'summary': (
            'Budapest embassy: political/economic/cultural/protocol '
            'research and events for six months, stated half-year '
            'windows January 2026–June 2027. Czech/Slovak, English and '
            'computer skills required; Hungarian advantageous. Czech '
            'and English motivation letters needed. No free housing. '
            'Czech citizens only. Host interest: 20 January 2026; late '
            'host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Budapest embassy: political/economic/cultural/protocol'
                ' research and events for six months, stated half-year '
                'windows January 2026–June 2027. Czech/Slovak, English '
                'and computer skills required; Hungarian advantageous. '
                'Czech and English motivation letters needed. No free '
                'housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'Czech and English motivation letters are required, '
                'with a Hungarian version if that language is known. '
                'Only selected applicants are notified for further '
                'steps at least two months before the stay. Graphics '
                'experience is welcomed, not required.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=250; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 250, published call 322: application '
                'opening 2025-11-20; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 251,
        'title': 'Erasmus+ Traineeship, Embassy Ulaanbaatar, Mongolia',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=251'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['MN'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-20',
        'uncertain': True,
        'summary': (
            'Ulaanbaatar embassy: administration, research, social '
            'content and cultural events in May–June or '
            'October–November 2026. English motivation letter '
            'required. No free housing. Czech citizens only. Host '
            'interest: 20 January 2026; late host agreement possible. '
            'Portal: 30 June 2027; availability unknown. University '
            'Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Ulaanbaatar embassy: administration, research, social '
                'content and cultural events in May–June or '
                'October–November 2026. English motivation letter '
                'required. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=251; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 251, published call 323: application '
                'opening 2025-11-20; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 252,
        'title': 'Erasmus+ Traineeship, Embassy Berlin, Germany',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=252'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-20',
        'uncertain': True,
        'summary': (
            'Berlin embassy: political/cultural/protocol or '
            'trade-economic research, media, visits and social content '
            'for at least three months. German ideally B2; German '
            'motivation letter required. No free housing. Czech '
            'citizens only. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Berlin embassy: political/cultural/protocol or '
                'trade-economic research, media, visits and social '
                'content for at least three months. German ideally B2; '
                'German motivation letter required. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=252; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 252, published call 324: application '
                'opening 2025-11-20; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 253,
        'title': (
            'Erasmus+ Traineeship, Consulate General Düsseldorf, '
            'Germany'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=253'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-20',
        'uncertain': True,
        'summary': (
            'Dusseldorf consulate: administration, research, citizen '
            'assistance and trade/cultural events for 3–5 months from '
            'May or September 2026; July/August disfavoured. German B2 '
            'and MS Office required; German motivation letter needed. '
            'Approval takes 2–3 months. No free housing. Czech '
            'citizens only. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Dusseldorf consulate: administration, research, '
                'citizen assistance and trade/cultural events for 3–5 '
                'months from May or September 2026; July/August '
                'disfavoured. German B2 and MS Office required; German '
                'motivation letter needed. Approval takes 2–3 months. '
                'No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The source-specific approval period is two to three '
                'months, longer than the generic MFA six-to-eight-week '
                'statement. German must support spoken, written and '
                'telephone work; a German motivation letter is '
                'requested.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=253; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 253, published call 325: application '
                'opening 2025-11-20; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 254,
        'title': 'Erasmus+ Traineeship, Embassy Oslo, Norway',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=254'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['NO'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-20',
        'uncertain': True,
        'summary': (
            'Oslo embassy: political, economic and cultural diplomatic '
            'practice for three months in a summer or winter semester, '
            'year unspecified. Norwegian advantageous. No free '
            'housing. Czech citizens only. Host interest: 20 January '
            '2026; late host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Oslo embassy: political, economic and cultural '
                'diplomatic practice for three months in a summer or '
                'winter semester, year unspecified. Norwegian '
                'advantageous. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=254; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 254, published call 326: application '
                'opening 2025-11-20; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 255,
        'title': 'Erasmus+ Traineeship, Embassy Warsaw, Poland',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=255'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['PL'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-20',
        'uncertain': True,
        'summary': (
            'Warsaw embassy: Polish political research, press and '
            'social content for five months, September 2026–January '
            '2027 or February–June 2027. Advanced Polish required. '
            'Discounted housing may be available, not free. Czech '
            'citizens only. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Warsaw embassy: Polish political research, press and '
                'social content for five months, September '
                '2026–January 2027 or February–June 2027. Advanced '
                'Polish required. Discounted housing may be available, '
                'not free.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=255; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 255, published call 327: application '
                'opening 2025-11-20; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 256,
        'title': 'Erasmus+ Traineeship, Embassy Lisbon, Portugal',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=256'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['PT'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-20',
        'uncertain': True,
        'summary': (
            'Lisbon embassy: political/economic work for 2–3 months '
            'from January 2027. Portuguese required. Free housing '
            'depends on unspecified conditions, so it is not '
            'guaranteed. Czech citizens only. Host interest: 20 '
            'January 2026; late host agreement possible. Portal: 30 '
            'June 2027; availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Lisbon embassy: political/economic work for 2–3 '
                'months from January 2027. Portuguese required. Free '
                'housing depends on unspecified conditions, so it is '
                'not guaranteed.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=256; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 256, published call 328: application '
                'opening 2025-11-20; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 257,
        'title': 'Erasmus+ Traineeship, Embassy Vienna, Austria',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=257'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['AT'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-20',
        'uncertain': True,
        'summary': (
            'Vienna embassy: political/economic/consular research, '
            'media and events in summer 2026 or stated semester '
            'windows. German B2/C1/C2 required, with application, '
            'legal-capacity declaration, criminal record and study '
            'proof. Housing costs EUR 4/day. Czech citizens only. Host '
            'interest: 20 January 2026; late host agreement possible. '
            'Portal: 30 June 2027; availability unknown. University '
            'Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Vienna embassy: political/economic/consular research, '
                'media and events in summer 2026 or stated semester '
                'windows. German B2/C1/C2 required, with application, '
                'legal-capacity declaration, criminal record and study '
                'proof. Housing costs EUR 4/day.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=257; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 257, published call 329: application '
                'opening 2025-11-20; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 258,
        'title': (
            'Erasmus+ Traineeship, Permanent Mission of the Czech '
            'Republic to the UN, OSCE and other International '
            'Organizations in Vienna, Austria'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=258'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['AT'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-20',
        'uncertain': True,
        'summary': (
            'Vienna international mission: OSCE/UN meetings, reporting '
            'and research for 4–6 months, September–December 2026 or '
            'February–July 2027. English C1, analysis and office '
            'skills required. Own funds must cover costs including '
            'travel; housing depends on capacity, with no entitlement. '
            'Czech citizens only. Host interest: 20 January 2026; late '
            'host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Vienna international mission: OSCE/UN meetings, '
                'reporting and research for 4–6 months, '
                'September–December 2026 or February–July 2027. '
                'English C1, analysis and office skills required. Own '
                'funds must cover costs including travel; housing '
                'depends on capacity, with no entitlement.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'An English CV and an English motivation letter of at '
                'most one standard page are requested. Analytical '
                'skills, office applications, deep research and '
                'occasional time flexibility are required. Acceptance '
                'and accommodation are not entitlements; all costs '
                'including travel remain the trainee’s responsibility, '
                'independently of any home-university grant decision.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=258; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 258, published call 330: application '
                'opening 2025-11-20; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 259,
        'title': 'Erasmus+ Traineeship, Embassy Bucharest, Romania',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=259'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['RO'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-20',
        'uncertain': True,
        'summary': (
            'Bucharest embassy: political/economic reports, economic '
            'diplomacy and social content for 2–4 months. Romanian '
            'advantageous. Embassy housing may be available for a fee, '
            'not free. Czech citizens only. Host interest: 20 January '
            '2026; late host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Bucharest embassy: political/economic reports, '
                'economic diplomacy and social content for 2–4 months. '
                'Romanian advantageous. Embassy housing may be '
                'available for a fee, not free.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=259; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 259, published call 331: application '
                'opening 2025-11-20; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 260,
        'title': 'Erasmus+ Traineeship, Embassy Dakar, Senegal',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=260'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['SN'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-20',
        'uncertain': True,
        'summary': (
            'Dakar embassy: political/economic regional research, '
            'media, events and administration for 2–12 months. '
            'Research covers several countries; the host is Senegal. '
            'French and French motivation letter required. No free '
            'housing. Czech citizens only. Host interest: 20 January '
            '2026; late host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Dakar embassy: political/economic regional research, '
                'media, events and administration for 2–12 months. '
                'Research covers several countries; the host is '
                'Senegal. French and French motivation letter '
                'required. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=260; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 260, published call 332: application '
                'opening 2025-11-20; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 261,
        'title': 'Erasmus+ Traineeship, Embassy Skopje, North Macedonia',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=261'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['MK'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-20',
        'uncertain': True,
        'summary': (
            'Skopje embassy: political/media reports, public '
            'diplomacy, social content and other sections for four '
            'months, 1 September–20 December 2026. A western-Balkan '
            'language advantageous. No free housing. Czech citizens '
            'only. Host interest: 20 January 2026; late host agreement '
            'possible. Portal: 30 June 2027; availability unknown. '
            'University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Skopje embassy: political/media reports, public '
                'diplomacy, social content and other sections for four '
                'months, 1 September–20 December 2026. A '
                'western-Balkan language advantageous. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=261; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 261, published call 333: application '
                'opening 2025-11-20; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 263,
        'title': 'Erasmus+ Traineeship, Embassy Madrid, Spain',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=263'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['ES'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-21',
        'uncertain': True,
        'summary': (
            'Madrid embassy: political/economic reports, meetings, '
            'diplomatic visits, projects and web/social content for '
            '3–6 months. Spanish B2+ required. No free housing. Czech '
            'citizens only. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Madrid embassy: political/economic reports, meetings, '
                'diplomatic visits, projects and web/social content '
                'for 3–6 months. Spanish B2+ required. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=263; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 263, published call 335: application '
                'opening 2025-11-21; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 264,
        'title': (
            'Erasmus+ Traineeship, Embassy Abu Dhabi, United Arab '
            'Emirates'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=264'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['AE'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-21',
        'uncertain': True,
        'summary': (
            'Abu Dhabi embassy: political, trade and administrative '
            'work for 2–12 months. Start depends on embassy expansion, '
            'expected in early 2026 but not confirmed completed. '
            'Arabic advantageous, not required; regional interest and '
            'English motivation requested. No free housing. Czech '
            'citizens only. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Abu Dhabi embassy: political, trade and '
                'administrative work for 2–12 months. Start depends on '
                'embassy expansion, expected in early 2026 but not '
                'confirmed completed. Arabic advantageous, not '
                'required; regional interest and English motivation '
                'requested. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'Expansion of the Abu Dhabi embassy was expected in '
                'the first half of 2026, but the leaf does not '
                'establish that it has finished. The start remains '
                'conditional on completion, not automatically current '
                'because that expected period has passed.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=264; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 264, published call 336: application '
                'opening 2025-11-21; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 265,
        'title': 'Erasmus+ Traineeship, Embassy Belgrade, Serbia',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=265'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['RS'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-21',
        'uncertain': True,
        'summary': (
            'Belgrade embassy: trade-counsellor support, community and '
            'cultural projects for 3–5 months. Serbian and '
            'photo/video/graphics skills advantageous; motivation '
            'should specify desired embassy work. No free housing. '
            'Czech citizens only. Host interest: 20 January 2026; late '
            'host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Belgrade embassy: trade-counsellor support, community '
                'and cultural projects for 3–5 months. Serbian and '
                'photo/video/graphics skills advantageous; motivation '
                'should specify desired embassy work. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=265; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 265, published call 337: application '
                'opening 2025-11-21; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 266,
        'title': 'Erasmus+ Traineeship, Embassy Bern, Switzerland',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=266'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['CH'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-21',
        'uncertain': True,
        'summary': (
            'Bern embassy: German media monitoring, Swiss '
            'political/trade research, consular support and community '
            'events for two months. German required. Embassy-site '
            'lodging costs CHF 10/night. Czech citizens only. Host '
            'interest: 20 January 2026; late host agreement possible. '
            'Portal: 30 June 2027; availability unknown. University '
            'Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Bern embassy: German media monitoring, Swiss '
                'political/trade research, consular support and '
                'community events for two months. German required. '
                'Embassy-site lodging costs CHF 10/night.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=266; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 266, published call 338: application '
                'opening 2025-11-21; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 267,
        'title': (
            'Erasmus+ Traineeship, Permanent Mission of the Czech '
            'Republic to the UN, specialized UN agencies and other '
            'International Organizations in Geneva, Switzerland'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=267'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['CH'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-21',
        'uncertain': True,
        'summary': (
            'Geneva UN mission: human rights, humanitarian/health, '
            'security, disarmament and cyber/AI work, 2 February–17 '
            'July 2026, now past. French advantageous; English '
            'motivation and video interview needed. No free housing. '
            'Host interest closed 5 December 2025, distinct from '
            'portal 30 June 2027; availability unknown. '
            'Home-university Erasmus funding is conditional.'
        ),
        'evidence': [
            (
                'Geneva UN mission: human rights, humanitarian/health, '
                'security, disarmament and cyber/AI work, 2 '
                'February–17 July 2026, now past. French advantageous; '
                'English motivation and video interview needed. No '
                'free housing. Host interest closed 5 December 2025, '
                'distinct from portal 30 June 2027; availability '
                'unknown. Home-university Erasmus funding is '
                'conditional.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The Geneva UN mission repeats a host-interest '
                'deadline of 5 December 2025 and a 2 February–17 July '
                '2026 stay. Both are historical; neither is replaced '
                'by the portal closing date or the separate generic '
                'host-interest guidance.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=267; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 267, published call 339: application '
                'opening 2025-11-21; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 268,
        'title': 'Erasmus+ Traineeship, Embassy Bangkok, Thailand',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=268'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['TH'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-21',
        'uncertain': True,
        'summary': (
            'Bangkok embassy: Thailand/Laos reports, meetings and '
            'events for three months from October 2026; host Thailand. '
            'Office/organisation skills, own laptop and WhatsApp '
            'needed. Residence visa required; stays over three months '
            'need extension. No free housing. Czech citizens only. '
            'Host interest: 20 January 2026; late host agreement '
            'possible. Portal: 30 June 2027; availability unknown. '
            'University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Bangkok embassy: Thailand/Laos reports, meetings and '
                'events for three months from October 2026; host '
                'Thailand. Office/organisation skills, own laptop and '
                'WhatsApp needed. Residence visa required; stays over '
                'three months need extension. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=268; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 268, published call 340: application '
                'opening 2025-11-21; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 269,
        'title': 'Erasmus+ Traineeship, Embassy Tunis, Tunisia',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=269'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['TN'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-21',
        'uncertain': True,
        'summary': (
            'Tunis embassy: political/economic and public-diplomacy '
            'work for 2–3 months. English AND French required; Arabic '
            'advantageous. Discounted embassy housing may be '
            'available, not free. Czech citizens only. Host interest: '
            '20 January 2026; late host agreement possible. Portal: 30 '
            'June 2027; availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Tunis embassy: political/economic and '
                'public-diplomacy work for 2–3 months. English AND '
                'French required; Arabic advantageous. Discounted '
                'embassy housing may be available, not free.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=269; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 269, published call 341: application '
                'opening 2025-11-21; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 270,
        'title': (
            'Erasmus+ Traineeship, Czech Association in Greece, Rhodes'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=270'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['GR'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-27',
        'uncertain': True,
        'summary': (
            'Rhodes association: Czech-school teaching, library, camp '
            'and community work in September–April or shorter semester '
            'windows, years unspecified. Primary-teacher studies '
            'required. Housing costs EUR 360–400/month including '
            'utilities; free bicycle/e-bike offered. Application '
            'timing is by host agreement, not a stated January '
            'deadline. Portal closes 30 June 2027; availability '
            'unknown. Home-university Erasmus funding is conditional.'
        ),
        'evidence': [
            (
                'Rhodes association: Czech-school teaching, library, '
                'camp and community work in September–April or shorter '
                'semester windows, years unspecified. Primary-teacher '
                'studies required. Housing costs EUR 360–400/month '
                'including utilities; free bicycle/e-bike offered. '
                'Application timing is by host agreement, not a stated '
                'January deadline. Portal closes 30 June 2027; '
                'availability unknown. Home-university Erasmus funding '
                'is conditional.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'This leaf has a different Czech-school/association '
                'application framework: host contracting and timing '
                'are by agreement; no 20 January deadline is stated. '
                'The EUR 360–400 monthly housing figure is a cost '
                'including utilities, while use of a bicycle or e-bike '
                'is free.'
            ),
            (
                'Primary-teacher studies are required. Guitar and '
                'singing are advantageous; autonomy, communication, '
                'organisation, assertiveness, reliability, initiative, '
                'teamwork and a positive relationship with children '
                'are expected. The stated seasonal windows have no '
                'year, and no year is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=270; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 270, published call 342: application '
                'opening 2025-11-27; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 271,
        'title': (
            'Erasmus+ Traineeship, Consulate General Istanbul, Turkey'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=271'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['TR'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-21',
        'uncertain': True,
        'summary': (
            'Istanbul consulate: political/economic reports, cultural '
            'events and visa/consular work for 3–4 months, 1 '
            'October–31 December 2026 or 1 March–30 June 2027. Turkish '
            'advantageous, not required. No free housing. Czech '
            'citizens only. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Istanbul consulate: political/economic reports, '
                'cultural events and visa/consular work for 3–4 '
                'months, 1 October–31 December 2026 or 1 March–30 June '
                '2027. Turkish advantageous, not required. No free '
                'housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=271; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 271, published call 343: application '
                'opening 2025-11-21; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 273,
        'title': 'Erasmus+ Traineeship, Consulate General Chicago, USA',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=273'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['US'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-21',
        'uncertain': True,
        'summary': (
            'Chicago consulate: consular, economic and '
            'public-diplomacy work for three months. English B2 '
            'required; MA students preferred, not exclusively '
            'eligible. No free housing. Czech citizens only. Host '
            'interest: 20 January 2026; late host agreement possible. '
            'Portal: 30 June 2027; availability unknown. University '
            'Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Chicago consulate: consular, economic and '
                'public-diplomacy work for three months. English B2 '
                'required; MA students preferred, not exclusively '
                'eligible. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=273; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 273, published call 345: application '
                'opening 2025-11-21; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 274,
        'title': 'Erasmus+ Traineeship, Consulate General New York, USA',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=274'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['US'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-21',
        'uncertain': True,
        'summary': (
            'New York consulate: economic/events or consular/visa work '
            'for three months in March–May, June–August or '
            'September–November, years unspecified. Economics/law '
            'studies advantageous by role; web/social experience '
            'expected. No full-stay housing; exceptional first-day '
            'accommodation possible. Czech citizens only. Host '
            'interest: 20 January 2026; late host agreement possible. '
            'Portal: 30 June 2027; availability unknown. University '
            'Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'New York consulate: economic/events or consular/visa '
                'work for three months in March–May, June–August or '
                'September–November, years unspecified. Economics/law '
                'studies advantageous by role; web/social experience '
                'expected. No full-stay housing; exceptional first-day '
                'accommodation possible.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'Economics studies are advantageous for the '
                'economic/events role and law studies for the '
                'consular/visa role. The short motivation letter '
                'should specify start availability; availability and '
                'capacity vary by role and season.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=274; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 274, published call 346: application '
                'opening 2025-11-21; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 275,
        'title': (
            'Erasmus+ Traineeship, Consulate General Los Angeles, USA'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=275'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['US'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-21',
        'uncertain': True,
        'summary': (
            'Los Angeles consulate: consular/visa/records, web, '
            'economic and public-event work for 2–3 months outside '
            'July/August. Law/economics studies advantageous. No free '
            'housing. Czech citizens only. Host interest: 20 January '
            '2026; late host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Los Angeles consulate: consular/visa/records, web, '
                'economic and public-event work for 2–3 months outside '
                'July/August. Law/economics studies advantageous. No '
                'free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=275; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 275, published call 347: application '
                'opening 2025-11-21; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 276,
        'title': (
            'Erasmus+ Traineeship, Permanent Mission of the Czech '
            'Republic to the UN in New York, USA'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=276'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['US'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-21',
        'uncertain': True,
        'summary': (
            'New York UN mission: UN committees, political cooperation '
            'and receptions for six months, 1 February–24 July 2026, '
            'now past. Overseas experience advantageous. Housing is '
            'currently not free; a future offer depends on unconfirmed '
            'building renovation. Czech citizens only. Host interest: '
            '20 January 2026; late host agreement possible. Portal: 30 '
            'June 2027; availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'New York UN mission: UN committees, political '
                'cooperation and receptions for six months, 1 '
                'February–24 July 2026, now past. Overseas experience '
                'advantageous. Housing is currently not free; a future '
                'offer depends on unconfirmed building renovation.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The source describes future free housing only after '
                'renovation of the mission building. It does not '
                'establish that renovation is complete, so current '
                'free accommodation is not promised.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=276; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 276, published call 348: application '
                'opening 2025-11-21; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 277,
        'title': 'Erasmus+ Traineeship, Embassy Washington, USA',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=277'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['US'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-21',
        'uncertain': True,
        'summary': (
            'Washington embassy: political/economic/cultural research, '
            'visits, meetings and public diplomacy. Published duration '
            '2–5 months also requires at least ten weeks. English C1, '
            'excellent written Czech, communication and autonomy '
            'required; MA students preferred. No free housing. Czech '
            'citizens only. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Washington embassy: political/economic/cultural '
                'research, visits, meetings and public diplomacy. '
                'Published duration 2–5 months also requires at least '
                'ten weeks. English C1, excellent written Czech, '
                'communication and autonomy required; MA students '
                'preferred. No free housing.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The source gives a 2–5-month range together with a '
                'ten-week minimum. Neither clause is silently replaced '
                'by a rounded duration; master’s students are '
                'preferred rather than exclusively eligible.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=277; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 277, published call 349: application '
                'opening 2025-11-21; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 278,
        'title': 'Erasmus+ Traineeship, Embassy Hanoi, Vietnam',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=278'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['VN'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-21',
        'uncertain': True,
        'summary': (
            'Hanoi embassy: political/economic analysis, meetings and '
            'web/social content for 3–6 months. English B2, '
            'international-relations/trade knowledge and regional '
            'interest expected; Czech CV/motivation needed. Trainees '
            'pay travel, housing and meals despite the conditional HEI '
            'grant route. Czech citizens only. Host interest: 20 '
            'January 2026; late host agreement possible. Portal: 30 '
            'June 2027; availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'Hanoi embassy: political/economic analysis, meetings '
                'and web/social content for 3–6 months. English B2, '
                'international-relations/trade knowledge and regional '
                'interest expected; Czech CV/motivation needed. '
                'Trainees pay travel, housing and meals despite the '
                'conditional HEI grant route.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=278; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 278, published call 350: application '
                'opening 2025-11-21; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 279,
        'title': 'Erasmus+ Traineeship, Embassy Lusaka, Zambia',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=279'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['ZM'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-21',
        'uncertain': True,
        'summary': (
            'Lusaka embassy: development-project preparation, '
            'implementation/evaluation and trade cooperation for at '
            'most three months. English and development/trade interest '
            'expected. Housing costs USD 400–600, frequency '
            'unspecified; longer stays need a trainee-paid work '
            'permit, about USD 600/year. Czech citizens only. Host '
            'interest: 20 January 2026; late host agreement possible. '
            'Portal: 30 June 2027; availability unknown. University '
            'Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'Lusaka embassy: development-project preparation, '
                'implementation/evaluation and trade cooperation for '
                'at most three months. English and development/trade '
                'interest expected. Housing costs USD 400–600, '
                'frequency unspecified; longer stays need a '
                'trainee-paid work permit, about USD 600/year.'
            ),
            'Only Czech citizens may take this placement.',
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The USD 400–600 housing amount has no stated '
                'frequency. Stays above three months require a work '
                'permit at an approximate USD 600 annual cost borne by '
                'the trainee; neither figure is a scholarship payment.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=279; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 279, published call 351: application '
                'opening 2025-11-21; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 280,
        'title': 'Erasmus+ Traineeship, CzechInvest',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=280'
        ),
        'categories': ['internships'],
        'kind': 'programme-overview',
        'hosts': [],
        'eligible': [],
        'deadline': None,
        'opening': '2025-11-21',
        'uncertain': True,
        'summary': (
            'CzechInvest offers individually agreed Erasmus '
            'internships for university students/recent graduates '
            'under the 2–12-month framework. Specific host, duties, '
            'passport eligibility, matching and payment are not '
            'confirmed. The host-interest stage closed 20 January '
            '2026, with late agreement possible; portal closes 30 June '
            '2027. This limited programme overview does not establish '
            'an open placement. Home-university Erasmus funding is '
            'conditional.'
        ),
        'evidence': [
            (
                'CzechInvest offers individually agreed Erasmus '
                'internships for university students/recent graduates '
                'under the 2–12-month framework. Specific host, '
                'duties, passport eligibility, matching and payment '
                'are not confirmed. The host-interest stage closed 20 '
                'January 2026, with late agreement possible; portal '
                'closes 30 June 2027. This limited programme overview '
                'does not establish an open placement. Home-university '
                'Erasmus funding is conditional.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The limited own-source offer is individually agreed; '
                'no specific destination, duties, citizenship '
                'condition, guaranteed matching or payment is '
                'established. It is a programme overview, not a fully '
                'specified vacancy.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=280; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 280, published call 352: application '
                'opening 2025-11-21; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 281,
        'title': (
            'Erasmus+ Traineeship, CzechTourism Japan & Taiwan, Tokyo, '
            'Japan'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=281'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['JP'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-24',
        'uncertain': True,
        'summary': (
            'CzechTourism Tokyo: tourism strategy, events and online '
            'content for Japanese/Taiwanese markets; demonstrated host '
            'Japan, not Taiwan. Stays 3–6 months in March–November, '
            'year unspecified. Japanese or Chinese advantageous. No '
            'free housing; visa required over three months. Host '
            'interest: 20 January 2026; late host agreement possible. '
            'Portal: 30 June 2027; availability unknown. University '
            'Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'CzechTourism Tokyo: tourism strategy, events and '
                'online content for Japanese/Taiwanese markets; '
                'demonstrated host Japan, not Taiwan. Stays 3–6 months '
                'in March–November, year unspecified. Japanese or '
                'Chinese advantageous. No free housing; visa required '
                'over three months.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The office is in Tokyo, Japan, with responsibility '
                'for Japanese and Taiwanese tourism markets. Market '
                'coverage does not establish a second physical host '
                'country.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=281; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 281, published call 353: application '
                'opening 2025-11-24; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 282,
        'title': 'Erasmus+ Traineeship, CzechTourism Germany, Berlin',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=282'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-24',
        'uncertain': True,
        'summary': (
            'CzechTourism Berlin: product analysis, social content and '
            'office work for 2–5 months. German ideally B2; Czech '
            'content can be translated by the office. No free housing. '
            'Host interest: 20 January 2026; late host agreement '
            'possible. Portal: 30 June 2027; availability unknown. '
            'University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'CzechTourism Berlin: product analysis, social content '
                'and office work for 2–5 months. German ideally B2; '
                'Czech content can be translated by the office. No '
                'free housing.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=282; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 282, published call 354: application '
                'opening 2025-11-24; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 283,
        'title': (
            'Erasmus+ Traineeship, CzechTourism Austria & Switzerland, '
            'Vienna, Austria'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=283'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['AT'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-24',
        'uncertain': True,
        'summary': (
            'CzechTourism Vienna: tourism inquiries, social/influencer '
            'work, trips, events and research for 2–12 months '
            'year-round; Swiss-market work does not prove host '
            'Switzerland. German B2 and restricted-level security '
            'clearance arranged two months ahead required. Free '
            'housing only by agreement and capacity. Host interest: 20 '
            'January 2026; late host agreement possible. Portal: 30 '
            'June 2027; availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'CzechTourism Vienna: tourism inquiries, '
                'social/influencer work, trips, events and research '
                'for 2–12 months year-round; Swiss-market work does '
                'not prove host Switzerland. German B2 and '
                'restricted-level security clearance arranged two '
                'months ahead required. Free housing only by agreement '
                'and capacity.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'The office is in Vienna, Austria, with responsibility '
                'for the Swiss market. Switzerland is not demonstrated '
                'as a physical placement destination. Restricted-level '
                'formal security clearance must be arranged through '
                'the office two months before the stay; free housing '
                'remains conditional on agreement and capacity.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=283; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 283, published call 355: application '
                'opening 2025-11-24; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 284,
        'title': (
            'Erasmus+ Traineeship, CzechTrade Belgium and Luxembourg'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=284'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['BE'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-24',
        'uncertain': True,
        'summary': (
            'CzechTrade Belgium/Luxembourg office: business missions, '
            'research and partner communication in Belgium for four '
            'months February–May 2026 or three months '
            'September–November 2026. English C1 required. No free '
            'housing. University–student–agency agreement, MFA '
            'screening and CV-data consent required. Host interest: 20 '
            'January 2026; late host agreement possible. Portal: 30 '
            'June 2027; availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'CzechTrade Belgium/Luxembourg office: business '
                'missions, research and partner communication in '
                'Belgium for four months February–May 2026 or three '
                'months September–November 2026. English C1 required. '
                'No free housing. University–student–agency agreement, '
                'MFA screening and CV-data consent required.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'CzechTrade requires a three-party agreement between '
                'the university, student and agency, MFA '
                'security-department screening that can take up to '
                'eight weeks, and consent to CV personal-data '
                'processing for recruitment. Czech citizenship is '
                'explicitly required only where stated, including the '
                'Chicago, San Francisco and Austin student offers.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=284; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 284, published call 356: application '
                'opening 2025-11-24; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 285,
        'title': 'Erasmus+ Traineeship, CzechTrade Chile',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=285'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['CL'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-24',
        'uncertain': True,
        'summary': (
            'CzechTrade Chile: partner/market/sector research, news '
            'and business missions. At least three months, ideally '
            '4–5, in September–December or March–June, years '
            'unspecified. Spanish B2+ required. No free housing. '
            'Tripartite university agreement, MFA screening and '
            'CV-data consent required. Host interest: 20 January 2026; '
            'late host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'CzechTrade Chile: partner/market/sector research, '
                'news and business missions. At least three months, '
                'ideally 4–5, in September–December or March–June, '
                'years unspecified. Spanish B2+ required. No free '
                'housing. Tripartite university agreement, MFA '
                'screening and CV-data consent required.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'CzechTrade requires a three-party agreement between '
                'the university, student and agency, MFA '
                'security-department screening that can take up to '
                'eight weeks, and consent to CV personal-data '
                'processing for recruitment. Czech citizenship is '
                'explicitly required only where stated, including the '
                'Chicago, San Francisco and Austin student offers.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=285; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 285, published call 357: application '
                'opening 2025-11-24; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 286,
        'title': 'Erasmus+ Traineeship, CzechTrade Israel',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=286'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['IL'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-24',
        'uncertain': True,
        'summary': (
            'CzechTrade Israel: projects, business missions, analysis '
            'and client administration for 3–6 months. Communication, '
            'reliability and organisation expected; Czech motivation '
            'letter needed. No free housing. Tripartite university '
            'agreement, MFA screening and CV-data consent required. '
            'Host interest: 20 January 2026; late host agreement '
            'possible. Portal: 30 June 2027; availability unknown. '
            'University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'CzechTrade Israel: projects, business missions, '
                'analysis and client administration for 3–6 months. '
                'Communication, reliability and organisation expected; '
                'Czech motivation letter needed. No free housing. '
                'Tripartite university agreement, MFA screening and '
                'CV-data consent required.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'CzechTrade requires a three-party agreement between '
                'the university, student and agency, MFA '
                'security-department screening that can take up to '
                'eight weeks, and consent to CV personal-data '
                'processing for recruitment. Czech citizenship is '
                'explicitly required only where stated, including the '
                'Chicago, San Francisco and Austin student offers.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=286; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 286, published call 358: application '
                'opening 2025-11-24; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 287,
        'title': 'Erasmus+ Traineeship, CzechTrade South Korea',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=287'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['KR'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-24',
        'uncertain': True,
        'summary': (
            'CzechTrade Korea: Korean-partner communication for 3–4 '
            'months in autumn 2026 or spring 2027. Korean required, '
            'level unspecified. No free housing. Tripartite university '
            'agreement, MFA screening and CV-data consent required. '
            'Host interest: 20 January 2026; late host agreement '
            'possible. Portal: 30 June 2027; availability unknown. '
            'University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'CzechTrade Korea: Korean-partner communication for '
                '3–4 months in autumn 2026 or spring 2027. Korean '
                'required, level unspecified. No free housing. '
                'Tripartite university agreement, MFA screening and '
                'CV-data consent required.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'CzechTrade requires a three-party agreement between '
                'the university, student and agency, MFA '
                'security-department screening that can take up to '
                'eight weeks, and consent to CV personal-data '
                'processing for recruitment. Czech citizenship is '
                'explicitly required only where stated, including the '
                'Chicago, San Francisco and Austin student offers.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=287; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 287, published call 359: application '
                'opening 2025-11-24; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 288,
        'title': 'Erasmus+ Traineeship, CzechTrade Colombia',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=288'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['CO'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-24',
        'uncertain': True,
        'summary': (
            'CzechTrade Colombia: market/partner research, meetings, '
            'databases, Spanish–English translation and promotion for '
            '3–4 months in stated quarterly windows, years '
            'unspecified. Spanish B2+ and Spanish motivation required. '
            'No free housing. Tripartite agreement, MFA screening and '
            'CV-data consent required. Host interest: 20 January 2026; '
            'late host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'CzechTrade Colombia: market/partner research, '
                'meetings, databases, Spanish–English translation and '
                'promotion for 3–4 months in stated quarterly windows, '
                'years unspecified. Spanish B2+ and Spanish motivation '
                'required. No free housing. Tripartite agreement, MFA '
                'screening and CV-data consent required.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'CzechTrade requires a three-party agreement between '
                'the university, student and agency, MFA '
                'security-department screening that can take up to '
                'eight weeks, and consent to CV personal-data '
                'processing for recruitment. Czech citizenship is '
                'explicitly required only where stated, including the '
                'Chicago, San Francisco and Austin student offers.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=288; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 288, published call 360: application '
                'opening 2025-11-24; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 289,
        'title': 'Erasmus+ Traineeship, CzechTrade Hungary',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=289'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['HU'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-24',
        'uncertain': True,
        'summary': (
            'CzechTrade Hungary: client/partner communication, events, '
            'administration and web work during 2026, individual '
            'duration unspecified. Hungarian advantageous. No free '
            'housing. Tripartite university agreement, MFA screening '
            'and CV-data consent required. Host interest: 20 January '
            '2026; late host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'CzechTrade Hungary: client/partner communication, '
                'events, administration and web work during 2026, '
                'individual duration unspecified. Hungarian '
                'advantageous. No free housing. Tripartite university '
                'agreement, MFA screening and CV-data consent required.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'CzechTrade requires a three-party agreement between '
                'the university, student and agency, MFA '
                'security-department screening that can take up to '
                'eight weeks, and consent to CV personal-data '
                'processing for recruitment. Czech citizenship is '
                'explicitly required only where stated, including the '
                'Chicago, San Francisco and Austin student offers.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=289; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 289, published call 361: application '
                'opening 2025-11-24; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 290,
        'title': 'Erasmus+ Traineeship, CzechTrade Morocco',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=290'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['MA'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-24',
        'uncertain': True,
        'summary': (
            'CzechTrade Rabat: trade missions, fairs, partner/market '
            'research and office administration in March–June 2026, '
            'now past. French required. No free housing. Tripartite '
            'university agreement, MFA screening and CV-data consent '
            'required. Host interest: 20 January 2026; late host '
            'agreement possible. Portal: 30 June 2027; availability '
            'unknown. University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'CzechTrade Rabat: trade missions, fairs, '
                'partner/market research and office administration in '
                'March–June 2026, now past. French required. No free '
                'housing. Tripartite university agreement, MFA '
                'screening and CV-data consent required.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'CzechTrade requires a three-party agreement between '
                'the university, student and agency, MFA '
                'security-department screening that can take up to '
                'eight weeks, and consent to CV personal-data '
                'processing for recruitment. Czech citizenship is '
                'explicitly required only where stated, including the '
                'Chicago, San Francisco and Austin student offers.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=290; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 290, published call 362: application '
                'opening 2025-11-24; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 291,
        'title': 'Erasmus+ Traineeship, CzechTrade Mexico',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=291'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['MX'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-24',
        'uncertain': True,
        'summary': (
            'CzechTrade Mexico: Czech-company expansion, fairs and '
            'business visits for 3–6 months. Spanish B2/C1 required. '
            'No free housing. Tripartite university agreement, MFA '
            'screening and CV-data consent required. Host interest: 20 '
            'January 2026; late host agreement possible. Portal: 30 '
            'June 2027; availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'CzechTrade Mexico: Czech-company expansion, fairs and '
                'business visits for 3–6 months. Spanish B2/C1 '
                'required. No free housing. Tripartite university '
                'agreement, MFA screening and CV-data consent required.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'CzechTrade requires a three-party agreement between '
                'the university, student and agency, MFA '
                'security-department screening that can take up to '
                'eight weeks, and consent to CV personal-data '
                'processing for recruitment. Czech citizenship is '
                'explicitly required only where stated, including the '
                'Chicago, San Francisco and Austin student offers.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=291; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 291, published call 363: application '
                'opening 2025-11-24; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 292,
        'title': (
            'Erasmus+ Traineeship, CzechTrade Central America and the '
            'Caribbean, Mexico'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=292'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['MX'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-24',
        'uncertain': True,
        'summary': (
            'CzechTrade Central America/Caribbean office: '
            'market/partner research, databases, fairs and client '
            'meetings; demonstrated host Mexico. Stays 3–4 months from '
            'late August/early September–December, year unspecified. '
            'Spanish B1 required. No free housing. Tripartite '
            'agreement, MFA screening and CV-data consent required. '
            'Host interest: 20 January 2026; late host agreement '
            'possible. Portal: 30 June 2027; availability unknown. '
            'University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'CzechTrade Central America/Caribbean office: '
                'market/partner research, databases, fairs and client '
                'meetings; demonstrated host Mexico. Stays 3–4 months '
                'from late August/early September–December, year '
                'unspecified. Spanish B1 required. No free housing. '
                'Tripartite agreement, MFA screening and CV-data '
                'consent required.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'CzechTrade requires a three-party agreement between '
                'the university, student and agency, MFA '
                'security-department screening that can take up to '
                'eight weeks, and consent to CV personal-data '
                'processing for recruitment. Czech citizenship is '
                'explicitly required only where stated, including the '
                'Chicago, San Francisco and Austin student offers.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=292; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 292, published call 364: application '
                'opening 2025-11-24; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 293,
        'title': 'Erasmus+ Traineeship, CzechTrade Germany, Düsseldorf',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=293'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['DE'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-24',
        'uncertain': True,
        'summary': (
            'CzechTrade Dusseldorf: firm contacts, market analysis, '
            'LinkedIn/social/web content, fairs and missions for 3–5 '
            'months during 2026. English and German B1 required. No '
            'free housing. Tripartite university agreement, MFA '
            'screening and CV-data consent required. Host interest: 20 '
            'January 2026; late host agreement possible. Portal: 30 '
            'June 2027; availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'CzechTrade Dusseldorf: firm contacts, market '
                'analysis, LinkedIn/social/web content, fairs and '
                'missions for 3–5 months during 2026. English and '
                'German B1 required. No free housing. Tripartite '
                'university agreement, MFA screening and CV-data '
                'consent required.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'CzechTrade requires a three-party agreement between '
                'the university, student and agency, MFA '
                'security-department screening that can take up to '
                'eight weeks, and consent to CV personal-data '
                'processing for recruitment. Czech citizenship is '
                'explicitly required only where stated, including the '
                'Chicago, San Francisco and Austin student offers.'
            ),
            (
                'English is required and German is specified at B1; no '
                'English CEFR level is inferred from the German '
                'requirement.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=293; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 293, published call 365: application '
                'opening 2025-11-24; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 294,
        'title': 'Erasmus+ Traineeship, CzechTrade Peru',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=294'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['PE'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-24',
        'uncertain': True,
        'summary': (
            'CzechTrade Peru: office operations, '
            'commercial-opportunity research, databases and missions '
            'for 3–12 months, ideally March–June or September–December '
            '2026. Spanish B2 and Spanish motivation required. No free '
            'housing. Tripartite agreement, MFA screening and CV-data '
            'consent required. Host interest: 20 January 2026; late '
            'host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'CzechTrade Peru: office operations, '
                'commercial-opportunity research, databases and '
                'missions for 3–12 months, ideally March–June or '
                'September–December 2026. Spanish B2 and Spanish '
                'motivation required. No free housing. Tripartite '
                'agreement, MFA screening and CV-data consent required.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'CzechTrade requires a three-party agreement between '
                'the university, student and agency, MFA '
                'security-department screening that can take up to '
                'eight weeks, and consent to CV personal-data '
                'processing for recruitment. Czech citizenship is '
                'explicitly required only where stated, including the '
                'Chicago, San Francisco and Austin student offers.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=294; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 294, published call 366: application '
                'opening 2025-11-24; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 295,
        'title': 'Erasmus+ Traineeship, CzechTrade Austria',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=295'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['AT'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-24',
        'uncertain': True,
        'summary': (
            'CzechTrade Austria: market/sector research, firm '
            'contacts, fairs, missions and marketing for six months '
            'January–June 2027. German ideally B1. No free housing. '
            'Tripartite university agreement, MFA screening and '
            'CV-data consent required. Host interest: 20 January 2026; '
            'late host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'CzechTrade Austria: market/sector research, firm '
                'contacts, fairs, missions and marketing for six '
                'months January–June 2027. German ideally B1. No free '
                'housing. Tripartite university agreement, MFA '
                'screening and CV-data consent required.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'CzechTrade requires a three-party agreement between '
                'the university, student and agency, MFA '
                'security-department screening that can take up to '
                'eight weeks, and consent to CV personal-data '
                'processing for recruitment. Czech citizenship is '
                'explicitly required only where stated, including the '
                'Chicago, San Francisco and Austin student offers.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=295; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 295, published call 367: application '
                'opening 2025-11-24; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 296,
        'title': 'Erasmus+ Traineeship, CzechTrade Romania',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=296'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['RO'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-24',
        'uncertain': True,
        'summary': (
            'CzechTrade Romania: market/partner research, trade '
            'fairs/missions and media monitoring for three months by '
            'agreement in September–December or February–June, years '
            'unspecified. Romanian/another Romance language '
            'advantageous. No free housing. Tripartite agreement, MFA '
            'screening and CV-data consent required. Host interest: 20 '
            'January 2026; late host agreement possible. Portal: 30 '
            'June 2027; availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'CzechTrade Romania: market/partner research, trade '
                'fairs/missions and media monitoring for three months '
                'by agreement in September–December or February–June, '
                'years unspecified. Romanian/another Romance language '
                'advantageous. No free housing. Tripartite agreement, '
                'MFA screening and CV-data consent required.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'CzechTrade requires a three-party agreement between '
                'the university, student and agency, MFA '
                'security-department screening that can take up to '
                'eight weeks, and consent to CV personal-data '
                'processing for recruitment. Czech citizenship is '
                'explicitly required only where stated, including the '
                'Chicago, San Francisco and Austin student offers.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=296; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 296, published call 368: application '
                'opening 2025-11-24; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 297,
        'title': 'Erasmus+ Traineeship, CzechTrade Saudi Arabia',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=297'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['SA'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-24',
        'uncertain': True,
        'summary': (
            'CzechTrade Saudi Arabia: assist the office director for '
            'at least three months. English required; Arabic '
            'advantageous only. No free housing. Tripartite university '
            'agreement, MFA screening and CV-data consent required. '
            'Host interest: 20 January 2026; late host agreement '
            'possible. Portal: 30 June 2027; availability unknown. '
            'University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'CzechTrade Saudi Arabia: assist the office director '
                'for at least three months. English required; Arabic '
                'advantageous only. No free housing. Tripartite '
                'university agreement, MFA screening and CV-data '
                'consent required.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'CzechTrade requires a three-party agreement between '
                'the university, student and agency, MFA '
                'security-department screening that can take up to '
                'eight weeks, and consent to CV personal-data '
                'processing for recruitment. Czech citizenship is '
                'explicitly required only where stated, including the '
                'Chicago, San Francisco and Austin student offers.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=297; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 297, published call 369: application '
                'opening 2025-11-24; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 298,
        'title': 'Erasmus+ Traineeship, CzechTrade Spain',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=298'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['ES'],
        'eligible': [],
        'deadline': '2027-06-30',
        'opening': '2025-11-24',
        'uncertain': True,
        'summary': (
            'CzechTrade Spain: business-opportunity research, '
            'databases, phone work, company lists and market news for '
            'five months February–June 2027. Spanish required. No free '
            'housing. Tripartite university agreement, MFA screening '
            'and CV-data consent required. Host interest: 20 January '
            '2026; late host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'CzechTrade Spain: business-opportunity research, '
                'databases, phone work, company lists and market news '
                'for five months February–June 2027. Spanish required. '
                'No free housing. Tripartite university agreement, MFA '
                'screening and CV-data consent required.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'CzechTrade requires a three-party agreement between '
                'the university, student and agency, MFA '
                'security-department screening that can take up to '
                'eight weeks, and consent to CV personal-data '
                'processing for recruitment. Czech citizenship is '
                'explicitly required only where stated, including the '
                'Chicago, San Francisco and Austin student offers.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=298; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 298, published call 370: application '
                'opening 2025-11-24; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 299,
        'title': 'Erasmus+ Traineeship, CzechTrade USA, Chicago',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=299'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['US'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-24',
        'uncertain': True,
        'summary': (
            'CzechTrade Chicago: exporter research, partner outreach, '
            'missions/fairs and PR/LinkedIn for 3–6 months. Only '
            'Czech-citizen students may apply. No free housing. '
            'Tripartite university agreement, MFA screening and '
            'CV-data consent required. Host interest: 20 January 2026; '
            'late host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'CzechTrade Chicago: exporter research, partner '
                'outreach, missions/fairs and PR/LinkedIn for 3–6 '
                'months. Only Czech-citizen students may apply. No '
                'free housing. Tripartite university agreement, MFA '
                'screening and CV-data consent required.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'CzechTrade requires a three-party agreement between '
                'the university, student and agency, MFA '
                'security-department screening that can take up to '
                'eight weeks, and consent to CV personal-data '
                'processing for recruitment. Czech citizenship is '
                'explicitly required only where stated, including the '
                'Chicago, San Francisco and Austin student offers.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=299; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 299, published call 371: application '
                'opening 2025-11-24; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 300,
        'title': 'Erasmus+ Traineeship, CzechTrade USA, San Francisco',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=300'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['US'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-24',
        'uncertain': True,
        'summary': (
            'CzechTrade San Francisco: director support, analysis, '
            'projects, client outreach and some events for 3–5 months '
            'by agreement. Only Czech-citizen students; excellent '
            'English/Czech and office skills required. No free '
            'housing. Tripartite agreement, MFA screening and CV-data '
            'consent required. Host interest: 20 January 2026; late '
            'host agreement possible. Portal: 30 June 2027; '
            'availability unknown. University Erasmus grant '
            'conditional.'
        ),
        'evidence': [
            (
                'CzechTrade San Francisco: director support, analysis, '
                'projects, client outreach and some events for 3–5 '
                'months by agreement. Only Czech-citizen students; '
                'excellent English/Czech and office skills required. '
                'No free housing. Tripartite agreement, MFA screening '
                'and CV-data consent required.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'CzechTrade requires a three-party agreement between '
                'the university, student and agency, MFA '
                'security-department screening that can take up to '
                'eight weeks, and consent to CV personal-data '
                'processing for recruitment. Czech citizenship is '
                'explicitly required only where stated, including the '
                'Chicago, San Francisco and Austin student offers.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=300; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 300, published call 372: application '
                'opening 2025-11-24; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 301,
        'title': 'Erasmus+ Traineeship, CzechTrade USA, Austin',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=301'
        ),
        'categories': ['internships'],
        'kind': 'opportunity',
        'hosts': ['US'],
        'eligible': ['CZ'],
        'deadline': '2027-06-30',
        'opening': '2025-11-24',
        'uncertain': True,
        'summary': (
            'CzechTrade Austin: business research, meetings and '
            'missions for four months, 1 February–30 May 2026, now '
            'past. Only Czech-citizen students; strong English, MS '
            'Office and autonomy required. No free housing. Tripartite '
            'agreement, MFA screening and CV-data consent required. '
            'Host interest: 20 January 2026; late host agreement '
            'possible. Portal: 30 June 2027; availability unknown. '
            'University Erasmus grant conditional.'
        ),
        'evidence': [
            (
                'CzechTrade Austin: business research, meetings and '
                'missions for four months, 1 February–30 May 2026, now '
                'past. Only Czech-citizen students; strong English, MS '
                'Office and autonomy required. No free housing. '
                'Tripartite agreement, MFA screening and CV-data '
                'consent required.'
            ),
            (
                'The sending institution must be a university. The '
                'published Erasmus framework covers students at all '
                'study levels and recent graduates, with a general '
                '2–12-month range; specific host durations and '
                'conditions remain controlling. The home university '
                'decides grant eligibility under its own rules, and '
                'host acceptance does not guarantee a stipend or '
                'amount.'
            ),
            (
                'The Czech material asks applicants to send a CV and '
                'any additional host documents directly to the host by '
                '20 January 2026. Applications after that date are '
                'possible by host agreement. Applicants also need '
                'home-university selection and host acceptance; the '
                'published portal end date is a separate stage and '
                'does not demonstrate current host recruitment.'
            ),
            (
                'The selected English portal call closes on 30 June '
                '2027. Its dates are retained separately from the '
                'actual host-interest deadline, host stay dates, '
                'conditional acceptance and any historical placement '
                'window; no date-only timezone is inferred.'
            ),
            (
                'CzechTrade requires a three-party agreement between '
                'the university, student and agency, MFA '
                'security-department screening that can take up to '
                'eight weeks, and consent to CV personal-data '
                'processing for recruitment. Czech citizenship is '
                'explicitly required only where stated, including the '
                'Chicago, San Francisco and Austin student offers.'
            ),
            (
                'The original public programme is '
                'https://studyin.gov.cz/en/scholarships/scholarship-det'
                'ail/?id=301; this query-sensitive URL preserves the '
                'source identity.'
            ),
            (
                'Programme 301, published call 373: application '
                'opening 2025-11-24; closing 2027-06-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 332,
        'title': (
            'Hungary: Scholarships for completing full-degree '
            "university studies in Bachelor's, Master’s, and Doctoral "
            'programmes'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=332'
        ),
        'categories': ['scholarships'],
        'kind': 'programme-overview',
        'hosts': ['HU'],
        'eligible': [],
        'deadline': '2026-02-19',
        'opening': None,
        'uncertain': False,
        'summary': (
            'Historical full-degree scholarships in Hungary for 24–48 '
            'months. Own sources list HUF 56,600 monthly for BA/MA and '
            'doctoral rates of HUF 140,600 then 180,000. The closing '
            'is February 19, 2026. Complete provider eligibility is '
            'unverified because its required manual needs written '
            'permission for this use.'
        ),
        'evidence': [
            (
                'This limited programme overview uses independently '
                'published DZS/Studujnavs facts only. The listed '
                'doctoral monthly amounts apply to years 1–2 and 3–4 '
                'respectively. Degree and language documentation, '
                'required dossier and host pre-acceptance requirements '
                'follow the own published overview. Its literal 23:00 '
                'closing has no stated timezone, so only the closing '
                'date is normalized. The Tempus manual is neither '
                'retrieved nor used; citizenship eligibility remains '
                'unspecified. The original links provide the direct '
                'application route.'
            ),
        ],
    },
    {
        'programme': 333,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            'Education, Youth and Sports for Students of Doctoral '
            'Programmes from Egypt'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=333'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': ['EG'],
        'deadline': '2026-04-15',
        'opening': '2026-03-16',
        'uncertain': False,
        'summary': (
            'Doctoral study in Czechia for 1–10 months, with CZK '
            '13,000 monthly support and a stated degree-band '
            'exception. Egyptian university affiliation is a route; '
            'Egyptian citizens at Czech universities are an explicit '
            'alternative. Prior Cairo nomination and host invitation '
            'are required.'
        ),
        'evidence': [
            (
                'The own portal closing is April 15, 2026. The '
                'positive Egyptian citizenship projection is '
                'nonexclusive: it describes the explicit alternative '
                'for citizens studying at a Czech university, not a '
                'replacement for the broader sending-university '
                'affiliation route. CZK 12,000 monthly applies without '
                'a master’s-equivalent qualification and CZK 13,000 to '
                'master’s holders, except when enrolled in another '
                'bachelor’s or master’s programme.'
            ),
            (
                'Programme 333, published call 415: application '
                'opening 2026-03-16; closing 2026-04-15. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 334,
        'title': 'Taiwan: Taiwan Scholarship Program',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=334'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['TW'],
        'eligible': [],
        'deadline': '2026-04-01',
        'opening': '2026-02-13',
        'uncertain': False,
        'summary': (
            'Full-degree study in Taiwan for 1–4 years, with TWD '
            '20,000 monthly support. The published portal closing is '
            'April 1, 2026. Degree, admission and application '
            'conditions remain those of the named award.'
        ),
        'evidence': [
            (
                'The source does not establish a universal citizenship '
                'restriction from its country-of-application entry.'
            ),
            (
                'Programme 334, published call 418: application '
                'opening 2026-02-13; closing 2026-04-01. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 335,
        'title': 'Taiwan: Huayu Enrichment Scholarship Program',
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=335'
        ),
        'categories': ['training'],
        'kind': 'opportunity',
        'hosts': ['TW'],
        'eligible': [],
        'deadline': '2026-04-01',
        'opening': '2026-02-13',
        'uncertain': False,
        'summary': (
            'Huayu Mandarin study in Taiwan for adults aged at least '
            '18, lasting 3–12 months with TWD 28,000 monthly support. '
            'The published portal closing is April 1, 2026.'
        ),
        'evidence': [
            (
                'The language-training offer is separate from '
                'full-degree funding. No travel entitlement is '
                'inferred from a missing reimbursement entry.'
            ),
            (
                'Programme 335, published call 419: application '
                'opening 2026-02-13; closing 2026-04-01. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 337,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            "Education, Youth and Sports for Students of Bachelor's, "
            "Master's and Doctoral Programmes from Latvia"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=337'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': ['LV'],
        'deadline': '2026-04-17',
        'opening': '2026-04-02',
        'uncertain': False,
        'summary': (
            'Study in Czechia for BA, MA and doctoral students '
            'affiliated with Latvian universities, lasting 2–10 '
            'months. Monthly support is CZK 12,000 or 13,000 according '
            'to completed qualification, with an exception for another '
            'BA/MA enrolment. Latvian citizens at Czech universities '
            'are an explicit alternative; Latvian-agency nomination is '
            'required.'
        ),
        'evidence': [
            (
                'The positive Latvian citizenship projection is '
                'nonexclusive because the sending-university '
                'affiliation route also exists. The selected call '
                'closes April 17, 2026. CZK 12,000 applies without a '
                'master’s-equivalent qualification and CZK 13,000 to '
                'master’s holders unless enrolled in another '
                'bachelor’s or master’s programme.'
            ),
            (
                'Programme 337, published call 129: application '
                'opening 2026-04-02; closing 2026-04-17. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 339,
        'title': (
            'Scholarships within the Framework of the Foreign '
            'Development Cooperation Programme - Study Programme in '
            'English Language'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=339'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [
            'BA',
            'GE',
            'UA',
            'BY',
            'ET',
            'GA',
            'MR',
            'ZM',
            'BD',
            'KH',
            'PS',
            'GT',
            'HN',
        ],
        'deadline': '2026-09-30',
        'opening': '2026-08-01',
        'uncertain': False,
        'summary': (
            'Czech government funding for English-medium follow-up '
            'MA/PhD applicants from 13 stated nationalities who are '
            'not already enrolled in Czechia. Monthly rates are CZK '
            '16,000 for MA and 24,960 for PhD, with living costs paid '
            'from the stipend. Published closing: September 30, 2026.'
        ),
        'evidence': [
            (
                'The stated nationality list is Bosnia and '
                'Herzegovina, Georgia, Ukraine, Belarus, Ethiopia, '
                'Gabon, Mauritania, Zambia, Bangladesh, Cambodia, '
                'Palestine, Guatemala and Honduras. Existing Czech '
                'enrolment excludes this variant. English proficiency '
                'and admission/test requirements apply.'
            ),
            (
                'The required government Guidelines and FAQ concern '
                'academic year 2027/28. Prior master’s graduates '
                'cannot obtain this support for the same or a lower '
                'degree level. Czech or EU citizens and Czech '
                'permanent residents are excluded; the own offer '
                'states birth after September 1, 1991 and before '
                'August 31, 2008; language and degree prerequisites '
                'also apply. Belarusian applicants must meet the '
                'stated democratic-opposition condition.'
            ),
            (
                'Monthly support is CZK 16,000 for master’s study or '
                'CZK 24,960 for doctoral study where that degree is '
                'actually offered. Housing, food and local transport '
                'are paid from the stipend rather than supplied free. '
                'International travel is borne by the recipient or '
                'sender. Medical support has cost-sharing and '
                'standard-care limits and excludes pregnancy, delivery '
                'and childcare; the initial medical examination and '
                'health conditions apply.'
            ),
            (
                'Support covers the standard study period. Changing '
                'the university, programme or language is restricted, '
                'and return home is required. A Czech B2 certificate '
                'plus admission can allow the preparatory year to be '
                'skipped; it is not universally mandatory. The degree, '
                'field and language options are application choices '
                'within the published award type.'
            ),
            (
                'The guidelines state a general-prerequisite test for '
                'Czech-medium variants, whereas the own English/test '
                'descriptions differ in places. The online closing is '
                'September 30, 2026; later enrolment or paper-document '
                'requirements are separate stages.'
            ),
            (
                'Programme 339, published call 455: application '
                'opening 2026-08-01; closing 2026-09-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 340,
        'title': (
            'Scholarships within the Framework of the Foreign '
            'Development Cooperation Programme - Study Programmes in '
            'Czech Language'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=340'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': ['BA', 'GE', 'UA', 'BY'],
        'deadline': '2026-09-30',
        'opening': '2026-08-01',
        'uncertain': False,
        'summary': (
            'Czech government funding for Czech-medium follow-up '
            'master’s study for four stated nationalities, for '
            'applicants not already enrolled in Czechia. Support is '
            'CZK 16,000 monthly. Czech B2 and admission can permit '
            'skipping preparation; the own page and guidelines differ '
            'on required tests.'
        ),
        'evidence': [
            (
                'The stated nationalities are Bosnia and Herzegovina, '
                'Georgia, Ukraine and Belarus. This named variant '
                'offers follow-up master’s study; a general doctoral '
                'benefit elsewhere does not establish a doctoral offer '
                'here. The own page describes English and general '
                'tests while the guidelines assign a '
                'general-prerequisite test to Czech-medium variants. '
                'The online closing is September 30, 2026.'
            ),
            (
                'The required government Guidelines and FAQ concern '
                'academic year 2027/28. Prior master’s graduates '
                'cannot obtain this support for the same or a lower '
                'degree level. Czech or EU citizens and Czech '
                'permanent residents are excluded; the own offer '
                'states birth after September 1, 1991 and before '
                'August 31, 2008; language and degree prerequisites '
                'also apply. Belarusian applicants must meet the '
                'stated democratic-opposition condition.'
            ),
            (
                'Monthly support is CZK 16,000 for master’s study or '
                'CZK 24,960 for doctoral study where that degree is '
                'actually offered. Housing, food and local transport '
                'are paid from the stipend rather than supplied free. '
                'International travel is borne by the recipient or '
                'sender. Medical support has cost-sharing and '
                'standard-care limits and excludes pregnancy, delivery '
                'and childcare; the initial medical examination and '
                'health conditions apply.'
            ),
            (
                'Support covers the standard study period. Changing '
                'the university, programme or language is restricted, '
                'and return home is required. A Czech B2 certificate '
                'plus admission can allow the preparatory year to be '
                'skipped; it is not universally mandatory. The degree, '
                'field and language options are application choices '
                'within the published award type.'
            ),
            (
                'The guidelines state a general-prerequisite test for '
                'Czech-medium variants, whereas the own English/test '
                'descriptions differ in places. The online closing is '
                'September 30, 2026; later enrolment or paper-document '
                'requirements are separate stages.'
            ),
            (
                'Programme 340, published call 456: application '
                'opening 2026-08-01; closing 2026-09-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 341,
        'title': (
            'Scholarships within the Framework of the Foreign '
            'Development Cooperation Programme - Study Programmes in '
            'English Language for Students already studying in the '
            'Czech Republic'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=341'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': ['ET', 'GA', 'MR', 'ZM', 'BD', 'KH', 'PS', 'GT', 'HN'],
        'deadline': '2026-09-30',
        'opening': '2026-08-01',
        'uncertain': False,
        'summary': (
            'Czech government funding for students already enrolled '
            'full-time in accredited Czech study. Its narrative lists '
            'nine nationalities, but its application-country table '
            'lists 13; the text also says Czech-medium despite the '
            'English title/tests. Monthly MA/PhD rates are CZK '
            '16,000/24,960.'
        ),
        'evidence': [
            (
                'The nine stated nationalities are Ethiopia, Gabon, '
                'Mauritania, Zambia, Bangladesh, Cambodia, Palestine, '
                'Guatemala and Honduras. The larger table and the '
                'language mismatch are retained as source conflicts, '
                'without adding the four other nationalities to this '
                'projection. These applicants receive lower assessment '
                'priority than foreign applicants. A signed printed '
                'application and enrolment confirmation are due '
                'December 20, 2026, separately from the September 30 '
                'online closing; no interview is required.'
            ),
            (
                'The required government Guidelines and FAQ concern '
                'academic year 2027/28. Prior master’s graduates '
                'cannot obtain this support for the same or a lower '
                'degree level. Czech or EU citizens and Czech '
                'permanent residents are excluded; the own offer '
                'states birth after September 1, 1991 and before '
                'August 31, 2008; language and degree prerequisites '
                'also apply. Belarusian applicants must meet the '
                'stated democratic-opposition condition.'
            ),
            (
                'Monthly support is CZK 16,000 for master’s study or '
                'CZK 24,960 for doctoral study where that degree is '
                'actually offered. Housing, food and local transport '
                'are paid from the stipend rather than supplied free. '
                'International travel is borne by the recipient or '
                'sender. Medical support has cost-sharing and '
                'standard-care limits and excludes pregnancy, delivery '
                'and childcare; the initial medical examination and '
                'health conditions apply.'
            ),
            (
                'Support covers the standard study period. Changing '
                'the university, programme or language is restricted, '
                'and return home is required. A Czech B2 certificate '
                'plus admission can allow the preparatory year to be '
                'skipped; it is not universally mandatory. The degree, '
                'field and language options are application choices '
                'within the published award type.'
            ),
            (
                'The guidelines state a general-prerequisite test for '
                'Czech-medium variants, whereas the own English/test '
                'descriptions differ in places. The online closing is '
                'September 30, 2026; later enrolment or paper-document '
                'requirements are separate stages.'
            ),
            (
                'Programme 341, published call 457: application '
                'opening 2026-08-01; closing 2026-09-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 342,
        'title': (
            'Scholarships within the Framework of the Foreign '
            'Development Cooperation Programme - Study Programmes in '
            'Czech Language for Students already studying in the Czech '
            'Republic'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=342'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': ['BA', 'GE', 'UA', 'BY'],
        'deadline': '2026-09-30',
        'opening': '2026-08-01',
        'uncertain': False,
        'summary': (
            'Czech government funding for Czech-medium follow-up '
            'master’s students already enrolled in Czechia, from four '
            'stated nationalities. Monthly support is CZK 16,000. '
            'Existing students have lower assessment priority; printed '
            'signed documents are due December 20, 2026, separately '
            'from the September 30 portal closing.'
        ),
        'evidence': [
            (
                'The four nationalities are Bosnia and Herzegovina, '
                'Georgia, Ukraine and Belarus. This enrolled variant '
                'does not include a preparatory year; its actual '
                'master’s audience is not expanded by generic doctoral '
                'funding descriptions. A signed printed application '
                'and enrolment confirmation are required by December '
                '20, 2026, without an interview.'
            ),
            (
                'The required government Guidelines and FAQ concern '
                'academic year 2027/28. Prior master’s graduates '
                'cannot obtain this support for the same or a lower '
                'degree level. Czech or EU citizens and Czech '
                'permanent residents are excluded; the own offer '
                'states birth after September 1, 1991 and before '
                'August 31, 2008; language and degree prerequisites '
                'also apply. Belarusian applicants must meet the '
                'stated democratic-opposition condition.'
            ),
            (
                'Monthly support is CZK 16,000 for master’s study or '
                'CZK 24,960 for doctoral study where that degree is '
                'actually offered. Housing, food and local transport '
                'are paid from the stipend rather than supplied free. '
                'International travel is borne by the recipient or '
                'sender. Medical support has cost-sharing and '
                'standard-care limits and excludes pregnancy, delivery '
                'and childcare; the initial medical examination and '
                'health conditions apply.'
            ),
            (
                'Support covers the standard study period. Changing '
                'the university, programme or language is restricted, '
                'and return home is required. A Czech B2 certificate '
                'plus admission can allow the preparatory year to be '
                'skipped; it is not universally mandatory. The degree, '
                'field and language options are application choices '
                'within the published award type.'
            ),
            (
                'The guidelines state a general-prerequisite test for '
                'Czech-medium variants, whereas the own English/test '
                'descriptions differ in places. The online closing is '
                'September 30, 2026; later enrolment or paper-document '
                'requirements are separate stages.'
            ),
            (
                'Programme 342, published call 458: application '
                'opening 2026-08-01; closing 2026-09-30. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 343,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            "Education, Youth and Sports for Students of Bachelor's, "
            "Master's and Doctoral Programmes from Vietnam"
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=343'
        ),
        'categories': ['scholarships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-10-02',
        'opening': '2026-09-16',
        'uncertain': False,
        'summary': (
            'Study in Czechia for students affiliated with Vietnamese '
            'public universities, lasting 2–10 months. Monthly support '
            'is CZK 12,000 or 13,000 according to completed '
            'qualification, with a further BA/MA-enrolment exception. '
            'Prior Vietnamese nomination and language proof are '
            'required; postgraduate documents depend on the degree.'
        ),
        'evidence': [
            (
                'The selected call closes October 2, 2026. Host '
                'invitation and publications are conditional '
                'postgraduate requirements despite the table’s '
                'optional entries. University affiliation is not a '
                'Vietnamese citizenship requirement. CZK 12,000 '
                'applies without a master’s-equivalent qualification '
                'and CZK 13,000 to master’s holders unless enrolled in '
                'another bachelor’s or master’s programme.'
            ),
            (
                'Programme 343, published call 464: application '
                'opening 2026-09-16; closing 2026-10-02. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
    {
        'programme': 344,
        'title': (
            'Bilateral Agreements: Scholarships of the Ministry of '
            'Education, Youth and Sports for Academic Staff from '
            'Vietnam'
        ),
        'url': (
            'https://studyin.gov.cz/en/scholarships/scholarship-detail/'
            '?id=344'
        ),
        'categories': ['fellowships'],
        'kind': 'opportunity',
        'hosts': ['CZ'],
        'eligible': [],
        'deadline': '2026-10-02',
        'opening': '2026-09-16',
        'uncertain': False,
        'summary': (
            'Academic/research staff employed at Vietnamese public '
            'universities can visit Czechia for up to one month, with '
            'CZK 851 per day for personal and meal costs, paid as a '
            'lump sum, plus paid accommodation. The published portal '
            'closing is October 2, 2026.'
        ),
        'evidence': [
            (
                'The daily amount is CZK 851 for this Vietnamese '
                'academic-staff offer; other incoming awards have '
                'different daily rates. Sending-institution '
                'affiliation is distinct from citizenship.'
            ),
            (
                'Programme 344, published call 465: application '
                'opening 2026-09-16; closing 2026-10-02. Closing dates '
                'have no verified timezone.'
            ),
        ],
    },
]

if __name__ == "__main__":
    try:
        try:
            result = main()
        finally:
            export_family_artifact()
        sys.exit(result)
    except Exception as error:
        print(json.dumps({
            "source": SOURCE_ID,
            "status": "fail",
            "last_attempt_at": utc_now(),
            "message": "Run could not complete; durable status unavailable",
            "error": safe_error(error),
            "failure_stage": getattr(error, "stage", "publish"),
        }, ensure_ascii=False))
        sys.exit(1)
