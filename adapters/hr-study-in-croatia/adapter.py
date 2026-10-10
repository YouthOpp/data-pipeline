"""Collect the reviewed finite Study in Croatia programme and call frontier."""

import argparse
import base64
import contextlib
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
import signal
import ssl
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

SOURCE_ID = "hr-study-in-croatia"
SOURCE_URL = "https://www.studyincroatia.hr/"
WEBSITE_URL = "https://www.studyincroatia.hr/"
LANGUAGE = "en"
PUBLISHER_COUNTRY = "HR"
PUBLISHER_TYPE = "national-education-agency"
ATTRIBUTION = (
    "Study in Croatia https://www.studyincroatia.hr/; "
    "Agency for Mobility and EU Programmes and the named programme administrators"
)

_GITHUB = "https://api.github.com/repos/YouthOpps/data-source"
_WORKFLOW_GITHUB = "https://api.github.com/repos/YouthOpps/data-pipeline"
_USER_AGENT = (
    "YouthOpps/1.0 (+https://github.com/YouthOpps/data-pipeline; "
    "public opportunity metadata)"
)
CATEGORIES = {
    "scholarships", "grants", "internships", "jobs", "training", "fellowships"
}
FAMILY = "youthopps-studyincroatia-publisher-v1"
FAMILY_SOURCES = {"hr-study-in-croatia"}
COLLECTION_TIMEOUT = 900
RUN_TIMEOUT = 1290
EXPORT_TIMEOUT = 30
_STATE_SCHEMA = 1
_ARTIFACT_NAME = "studyincroatia-pacing-state"
_STATE_PATH = _STATE_LOCK = None
_COLLECTION_LOCK_FD = None
_COLLECTION_STARTED = None
_RESTORED = _PUBLISHING = _STATE_TRUSTED = False
_ROBOTS = {}
_RUN_END = _PHASE_END = None
_PHASE_NAME = None


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
            class_name is None
            or class_name in n.attrs.get("class", "").split()
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


class PhaseExpired(BaseException):
    """A non-renewable whole-phase deadline, never an ordinary HTTP error."""


def check_deadline():
    if _PHASE_END is None or _RUN_END is None:
        raise AdapterError("Bounded execution context required", "access")
    if time.monotonic() >= min(_PHASE_END, _RUN_END):
        raise PhaseExpired()


def request_timeout(publisher=False):
    check_deadline()
    remaining = min(_PHASE_END, _RUN_END) - time.monotonic()
    if remaining <= 0:
        raise PhaseExpired()
    return min(60 if publisher else 15, remaining)


def alarm_expired(signum, frame):
    raise PhaseExpired()


def arm_alarm(deadline):
    """Keep the absolute alarm armed even outside named phases."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PhaseExpired()
    signal.setitimer(signal.ITIMER_REAL, remaining)


@contextlib.contextmanager
def execution():
    global _RUN_END, _COLLECTION_LOCK_FD
    if _RUN_END is not None:
        raise AdapterError("Nested execution context refused", "access")
    previous = signal.getsignal(signal.SIGALRM)
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise AdapterError("Existing process alarm refused", "access")
    signal.signal(signal.SIGALRM, alarm_expired)
    _RUN_END = time.monotonic() + RUN_TIMEOUT
    arm_alarm(_RUN_END - EXPORT_TIMEOUT)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        _RUN_END = None
        if _COLLECTION_LOCK_FD is not None:
            try:
                os.close(_COLLECTION_LOCK_FD)
            except OSError:
                pass
            _COLLECTION_LOCK_FD = None


@contextlib.contextmanager
def phase(name, seconds):
    global _PHASE_END, _PHASE_NAME
    if _PHASE_END is not None or _RUN_END is None:
        raise AdapterError("Invalid phase transition", "access")
    _PHASE_NAME = name
    limit = _RUN_END if name == "export" else _RUN_END - EXPORT_TIMEOUT
    _PHASE_END = min(time.monotonic() + seconds, limit)
    try:
        check_deadline()
        arm_alarm(_PHASE_END)
        yield
        check_deadline()
    finally:
        _PHASE_END = _PHASE_NAME = None
        arm_alarm(limit)


_OPENER = urllib.request.build_opener(NoRedirect)


def numeric(value):
    try:
        return (
            type(value) in (int, float) and math.isfinite(value) and value >= 0
        )
    except OverflowError:
        return False


def validate_budget(state):
    """Validate inert state without age-expiring a future publisher embargo."""
    if not isinstance(state, dict) or set(state) != {
        "schema",
        "family",
        "observed_at",
        "not_before",
        "starts",
        "blocked",
    }:
        raise AdapterError("Invalid publisher pacing state", "access")
    if (
        type(state["schema"]) is not int
        or state["schema"] != _STATE_SCHEMA
        or state["family"] != FAMILY
        or not numeric(state["observed_at"])
        or not numeric(state["not_before"])
        or type(state["blocked"]) is not bool
        or not isinstance(state["starts"], list)
        or len(state["starts"]) > 10
        or any(not numeric(t) for t in state["starts"])
        or state["starts"] != sorted(state["starts"])
        or len(set(state["starts"])) != len(state["starts"])
        or any(t > state["observed_at"] for t in state["starts"])
    ):
        raise AdapterError("Invalid publisher pacing history", "access")
    if time.time() + 1 < state["observed_at"]:
        raise AdapterError("Publisher pacing clock moved backwards", "access")
    return state


def empty_budget(now):
    return {
        "schema": _STATE_SCHEMA,
        "family": FAMILY,
        "observed_at": now,
        "not_before": now,
        "starts": [],
        "blocked": False,
    }


def budget_file():
    global _STATE_PATH, _STATE_LOCK
    if _STATE_PATH:
        return
    directory = os.path.join(
        tempfile.gettempdir(), FAMILY + ("-") + str(os.getuid())
    )
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
    if (
        info.st_uid != os.getuid()
        or not stat.S_ISREG(info.st_mode)
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
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
        if (
            info.st_uid != os.getuid()
            or not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise AdapterError("Unsafe publisher pacing state owner", "access")
        raw = file.read(16385)
    if len(raw) > 16384:
        raise AdapterError("Oversized publisher pacing state", "access")
    try:
        return validate_budget(json.loads(raw))
    except (ValueError, TypeError):
        raise AdapterError(
            "Corrupt publisher pacing state", "access"
        ) from None


def save_budget(state):
    validate_budget(state)
    fd, name = tempfile.mkstemp(
        prefix=("pending-"), dir=os.path.dirname(_STATE_PATH)
    )
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
    check_deadline()
    with locked_budget():
        state = load_budget()
        if state["blocked"]:
            raise AdapterError(
                ("Publisher backoff requires reviewed recovery"), ("access")
            )
        now = time.time()
        starts = [t for t in state["starts"] if t > now - 60]
        target = max(now, state["not_before"])
        if starts:
            target = max(target, starts[-1] + 6)
        if len(starts) == 10:
            target = max(target, starts[0] + 60)
        if target + 120 > time.time() + (_PHASE_END - time.monotonic()):
            raise AdapterError(
                "Publisher backoff exceeds phase budget", "access"
            )
        if _COLLECTION_STARTED is not None and (
            target + 120 > _COLLECTION_STARTED + COLLECTION_TIMEOUT
        ):
            raise AdapterError(
                ("Publisher backoff exceeds collection budget"), ("access")
            )
        if target > now:
            time.sleep(target - now)
        now = time.time()
        if now + 0.001 < target:
            raise AdapterError(
                ("Publisher pacing clock moved backwards"), ("access")
            )
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


def publisher_transport_error(error, url):
    """Describe a publisher transport failure without logging exception text."""
    cause = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(cause, ssl.SSLCertVerificationError):
        category = "TLS certificate verification failure"
    elif isinstance(cause, ssl.SSLError):
        category = "TLS handshake/read failure"
    elif isinstance(cause, TimeoutError):
        category = "Network timeout"
    elif isinstance(cause, ConnectionError):
        category = "Connection failure"
    else:
        category = "Network transport failure"
    parsed = urllib.parse.urlsplit(url)
    hop = urllib.parse.urlunsplit(
        (parsed.scheme, parsed.hostname or "", parsed.path, "", "")
    )
    return AdapterError(
        "Publisher transport failure at " + hop + " (" + category + ")",
        "fetch",
    )


def request_bytes(
    url,
    method="GET",
    headers=None,
    payload=None,
    publisher=False,
    byte_limit=30000000,
):
    """Bound response bytes; redirects are explicit and credential-safe."""
    headers = {"User-Agent": _USER_AGENT, **(headers or {})}
    current = url
    for redirect in range(6):
        check_deadline()
        if publisher:
            public_url(current)
            pace()
        request = urllib.request.Request(
            current, data=payload, headers=headers, method=method
        )
        try:
            response = _OPENER.open(
                request, timeout=request_timeout(publisher)
            )
        except urllib.error.HTTPError as error:
            response = error
        except (urllib.error.URLError, OSError) as error:
            if publisher:
                raise publisher_transport_error(error, current) from None
            raise
        with response:
            status = response.status
            response_headers = response.headers
            retry = response.headers.get("Retry-After")
            if publisher:
                record_retry_after(retry)
            if publisher and status in (401, 403, 429):
                refused = urllib.parse.urlsplit(current)
                provenance = urllib.parse.urlunsplit(
                    (refused.scheme, refused.hostname or "",
                     refused.path, "", "")
                )
                raise AdapterError(
                    f"Publisher refused access: HTTP {status}: " + provenance,
                    "access",
                    status,
                )
            location = response.headers.get("Location")
            is_redirect = status in (301, 302, 303, 307, 308)
            if is_redirect and publisher:
                if not location or redirect == 5:
                    raise AdapterError("Invalid redirect chain", "access")
                destination = urllib.parse.urljoin(current, location)
                public_url(destination)
                if (urllib.parse.urlsplit(current).scheme == "https"
                        and urllib.parse.urlsplit(destination).scheme != "https"):
                    raise AdapterError(
                        "Publisher redirect transport downgrade", "access"
                    )
            if is_redirect:
                body = b""
            else:
                declared = response.headers.get("Content-Length")
                if declared and (
                    not declared.isdigit() or int(declared) > byte_limit
                ):
                    raise AdapterError(
                        ("Oversized or invalid response length"), ("fetch")
                    )
                try:
                    body = response.read(byte_limit + 1)
                except (urllib.error.URLError, OSError) as error:
                    if publisher:
                        raise publisher_transport_error(error, current) from None
                    raise
                if (
                    len(body) > byte_limit
                    or declared
                    and len(body) != int(declared)
                ):
                    raise AdapterError("Incomplete or oversized response", "fetch")
                encoding = response.headers.get("Content-Encoding", "identity")
                if encoding != "identity":
                    raise AdapterError(
                        ("Unsupported response content encoding"), ("fetch")
                    )
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
                    raise AdapterError(
                        ("Publisher redirect transport downgrade"), ("access")
                    )
                check_robots(destination)
            else:
                if new.scheme != "https" or new.username or new.password:
                    raise AdapterError(
                        ("Unsafe authenticated redirect"), ("publish")
                    )
                if new.netloc != old.netloc:
                    headers = {
                        key: value
                        for key, value in headers.items()
                        if key.lower() != "authorization"
                    }
                if urllib.parse.urlsplit(url).hostname == "api.github.com":
                    raise AdapterError(
                        ("Unexpected GitHub API redirect"), ("publish")
                    )
            current = destination
            continue
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
            raise AdapterError(
                ("Publisher robots policy unavailable"), ("access"), status
            )
    parser = _ROBOTS[origin]
    if parser is not None and not parser.can_fetch(_USER_AGENT, url):
        raise AdapterError(
            "Publisher robots excludes required input", "access"
        )
    if parser is not None:
        delay = parser.crawl_delay(_USER_AGENT)
        if delay and delay > 6:
            with locked_budget():
                state = load_budget()
                if state["starts"]:
                    state[("not_before")] = max(
                        state[("not_before")], state[("starts")][-1] + delay
                    )
                    save_budget(state)
        rate = parser.request_rate(_USER_AGENT)
        if rate and rate.seconds / rate.requests > 6:
            raise AdapterError(
                ("Stricter publisher request-rate requires review"), ("access")
            )


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
        key = next(
            (key for key, info in INPUTS.items() if info["url"] == url),
            "unmapped-public-input",
        )
        parsed = urllib.parse.urlsplit(reviewed)
        provenance = parsed.scheme + "://" + parsed.netloc + parsed.path
        raise AdapterError(
            f"Required publisher input {key} returned HTTP {status}: "
            + provenance,
            "fetch",
            status,
        )


def workflow_api(path):
    """Read authenticated workflow metadata without publication permissions."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise AdapterError("Workflow state credentials missing", "access")
    try:
        status, _, body = request_bytes(
            _WORKFLOW_GITHUB + path,
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json",
            },
        )
        if status != 200:
            raise ValueError("Workflow API status")
        return json.loads(body)
    except Exception:
        raise AdapterError(
            ("Workflow pacing state unavailable"), ("access")
        ) from None


def current_run_identity():
    if (
        os.environ.get("GITHUB_REPOSITORY") != "YouthOpps/data-pipeline"
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
    ):
        raise AdapterError("Unexpected workflow repository", "access")
    values = [
        os.environ.get(key, "")
        for key in (("GITHUB_RUN_ID"), ("GITHUB_RUN_ATTEMPT"))
    ]
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
        if (
            not source
            or type(identity) is not int
            or identity <= 0
            or type(attempt) is not int
            or attempt <= 0
            or run["status"] != "completed"
            or run["repository"]["full_name"] != "YouthOpps/data-pipeline"
            or run[("head_repository")][("full_name")]
            != ("YouthOpps/data-pipeline")
            or run["head_branch"] != "main"
            or expected
            and (identity, attempt, source) != expected
        ):
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
        raise AdapterError(
            ("Untrusted completed family attempt"), ("access")
        ) from None


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
                    "Workflow inventory remained unstable", "access"
                ) from None


def scan_family_history():
    """Select newest completed chronology, including older-ID rerun attempts."""
    current_id, current_attempt = current_run_identity()
    candidates = []
    total, scanned = None, 0
    seen_ids = set()
    for page in range(1, 101):
        result = workflow_api(f"/actions/runs?per_page=100&page={page}")
        count, runs = result.get("total_count"), result.get("workflow_runs")
        if (
            type(count) is not int
            or count < 0
            or not isinstance(runs, list)
            or len(runs) > 100
        ):
            raise AdapterError("Invalid workflow run inventory", "access")
        if total is None:
            total = count
        elif total != count:
            raise InventoryRace(
                ("Workflow inventory changed during restore"), ("access")
            )
        scanned += len(runs)
        for run in runs:
            if not isinstance(run, dict):
                raise AdapterError("Invalid workflow run entry", "access")
            run_id = run.get("id")
            if type(run_id) is not int or run_id <= 0 or run_id in seen_ids:
                raise AdapterError(
                    "Invalid or duplicate workflow run identity", "access"
                )
            seen_ids.add(run_id)
            source = family_source(run)
            if not source:
                continue
            if (
                run.get(("repository"), {}).get(("full_name"))
                != ("YouthOpps/data-pipeline")
                or run.get("head_repository", {}).get("full_name")
                != ("YouthOpps/data-pipeline")
                or run.get(("head_branch")) != ("main")
            ):
                raise AdapterError(
                    ("Workflow repository or branch mismatch"), ("access")
                )
            identity, attempt = run.get("id"), run.get("run_attempt")
            if type(identity) is not int or type(attempt) is not int:
                raise AdapterError("Invalid family run identity", "access")
            if identity == current_id:
                if attempt != current_attempt:
                    raise AdapterError(
                        ("Current workflow attempt mismatch"), ("access")
                    )
                if current_attempt > 1:
                    prior = workflow_api(
                        (
                            f"/actions/runs/{identity}/attempts/"
                            f"{current_attempt - 1}"
                        )
                    )
                    candidates.append(
                        completed_identity(
                            prior, (identity, current_attempt - 1, source)
                        )
                    )
                continue
            if run.get(("status")) in (
                ("queued"),
                ("waiting"),
                ("pending"),
                ("requested"),
            ):
                continue
            if run.get("status") != "completed":
                raise AdapterError("Another family run is active", "access")
            candidates.append(completed_identity(run))
        if scanned >= total:
            if scanned != total:
                raise AdapterError(
                    ("Workflow inventory count mismatch"), ("access")
                )
            if not candidates:
                return None
            ordered = sorted(candidates)
            if len(ordered) > 1 and ordered[-1][0] == ordered[-2][0]:
                raise AdapterError(
                    ("Ambiguous family attempt chronology"), ("access")
                )
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
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + token,
            "User-Agent": _USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        try:
            response = _OPENER.open(request, timeout=request_timeout())
        except urllib.error.HTTPError as error:
            response = error
        with response:
            if response.status != 302:
                raise ValueError("Expected artifact redirect")
            destination = response.headers.get("Location", "")
            response.read(1001)
        parsed = urllib.parse.urlsplit(destination)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port
        ):
            raise ValueError("Unsafe artifact URL")
        if "." not in parsed.hostname or parsed.hostname.endswith(
            ((".local"), (".internal"), (".localhost"))
        ):
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
            if (
                not isinstance(digest, str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                or digest[7:] != hashlib.sha256(body).hexdigest()
            ):
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
            "schema",
            "repository",
            "run_id",
            "run_attempt",
            "source",
            "state",
        }:
            raise ValueError("Invalid artifact envelope")
        run_id, attempt, source = identity
        if (
            type(envelope["schema"]) is not int
            or envelope["schema"] != 1
            or envelope["repository"] != "YouthOpps/data-pipeline"
            or type(envelope["run_id"]) is not int
            or type(envelope["run_attempt"]) is not int
            or envelope["run_id"] != run_id
            or envelope["run_attempt"] != attempt
            or envelope["source"] != source
        ):
            raise ValueError("Artifact ownership mismatch")
        return validate_budget(envelope["state"])
    except Exception:
        raise AdapterError(
            ("Invalid or unavailable family pacing artifact"), ("access")
        ) from None


def restore_family_artifact():
    global _RESTORED, _STATE_TRUSTED
    if _RESTORED:
        return
    latest = latest_family_run()
    if latest is None:
        raw = os.environ.get("STUDYINCROATIA_PACING_BOOTSTRAP", "")
        try:
            bootstrap = json.loads(raw)
            if (
                set(bootstrap)
                != {"schema", "family", "not_before", "evidence"}
                or type(bootstrap["schema"]) is not int
                or bootstrap["schema"] != 1
                or bootstrap["family"] != FAMILY
                or not numeric(bootstrap["not_before"])
                or not isinstance(bootstrap["evidence"], str)
                or not re.fullmatch(
                    (
                        "https://github\\.com/YouthOpps/data-pipeline/(?:is"
                        "sues|pull)/[0-9]+(?:#issuecomment-[0-9]+)?"
                    ),
                    bootstrap[("evidence")],
                )
            ):
                raise ValueError("Invalid bootstrap")
            incoming = empty_budget(time.time())
            incoming[("not_before")] = max(
                time.time() + 60, bootstrap[("not_before")]
            )
        except Exception:
            raise AdapterError(
                ("Reviewed first-family bootstrap required"), ("access")
            ) from None
    else:
        run_id, attempt, source = latest
        expected = _ARTIFACT_NAME + "-" + str(attempt)
        result = workflow_api(f"/actions/runs/{run_id}/artifacts?per_page=100")
        artifacts = result.get("artifacts")
        if (
            not isinstance(artifacts, list)
            or result.get(("total_count")) != len(artifacts)
            or len(artifacts) > 100
        ):
            raise AdapterError(
                "Incomplete family artifact inventory", "access"
            )
        matches = [a for a in artifacts if a.get("name") == expected]
        if len(matches) != 1 or matches[0].get("expired") is not False:
            raise AdapterError(
                ("Newest family pacing artifact missing or expired"),
                ("access"),
            )
        artifact = matches[0]
        if (
            type(artifact.get("id")) is not int
            or artifact["id"] <= 0
            or artifact.get("workflow_run", {}).get("id") != run_id
            or not numeric(artifact.get("size_in_bytes"))
            or artifact["size_in_bytes"] > 32768
        ):
            raise AdapterError("Invalid family artifact binding", "access")
        incoming = download_inert_artifact(
            latest, artifact[("id")], artifact.get(("digest"))
        )
    with locked_budget():
        local = load_budget()
        now = time.time()
        combined = empty_budget(now)
        combined[("not_before")] = max(
            local[("not_before")], incoming[("not_before")], now + 60
        )
        combined["blocked"] = local["blocked"] or incoming["blocked"]
        combined[("starts")] = sorted(
            set(local[("starts")] + incoming[("starts")])
        )[-10:]
        save_budget(combined)
    _RESTORED, _STATE_TRUSTED = True, True


def export_family_artifact():
    """Best-effort infrastructure state; never contains records or secrets."""
    path = os.environ.get("STUDYINCROATIA_PACING_ARTIFACT_PATH")
    if not path or not _STATE_TRUSTED:
        return
    run_id, attempt = current_run_identity()
    with locked_budget():
        state = load_budget()
    envelope = {
        "schema": 1,
        "repository": "YouthOpps/data-pipeline",
        "run_id": run_id,
        "run_attempt": attempt,
        "source": SOURCE_ID,
        "state": state,
    }
    configured = os.environ.get("RUNNER_TEMP", "")
    if not configured or not os.path.isabs(configured):
        raise AdapterError(
            ("Explicit runner temporary directory required"), ("access")
        )
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
            raise AdapterError(
                ("Another local family collector is active"), ("access")
            ) from None
        _COLLECTION_LOCK_FD = descriptor
    if os.environ.get("GITHUB_ACTIONS") == "true":
        restore_family_artifact()
    _STATE_TRUSTED = True


def document_root(text, complete=True):
    if complete and not re.search(
        r"</body\s*>\s*</html\s*>\s*(?:<!--[\s\S]*?-->\s*)*$", text, re.I
    ):
        raise AdapterError("Incomplete public HTML", "parse")
    root = PublisherHTML(text).root
    title = " ".join(n.text() for n in nodes(root, "title")).lower()
    if re.match(
        r"just a moment|access denied|attention required|"
        r"checking your browser|verify you are human",
        title,
    ):
        raise AdapterError("Publisher access challenge", "access")
    return root


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
                raise AdapterError(
                    "Missing record field: " + field, "validate"
                )
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
                raise AdapterError(
                    "Missing record field: " + field, "validate"
                )
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
            raise AdapterError(
                "Invalid opportunity kind or status", "validate"
            )
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
                raise AdapterError(
                    "Invalid record field: " + field, "validate"
                )
        if "summary_language" in record and (
            not isinstance(record["summary_language"], str)
            or not re.fullmatch(
                r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*",
                record["summary_language"],
            )
        ):
            raise AdapterError("Invalid summary language", "validate")
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
    check_deadline()
    token = os.environ.get("DATA_SOURCE_TOKEN", "")
    if not token:
        raise AdapterError("Publication credentials missing", "publish")
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
            "GitHub connection failed; outcome unconfirmed", "publish"
        ) from error
    if status == 404 and missing_ok:
        return None
    if status not in (200, 201):
        raise AdapterError(
            f"GitHub publication HTTP {status}", "publish", status
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
                raise AdapterError(
                    "Existing blob identity mismatch", "publish"
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
        check_deadline()
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


def public_url(value):
    parsed = urllib.parse.urlsplit(value)
    clean = urllib.parse.urlunsplit(parsed._replace(fragment=""))
    allowed = {info["url"] for info in INPUTS.values()} | {
        "https://www.studyincroatia.hr/robots.txt"
    }
    if (
        clean not in allowed
        or parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port
    ):
        raise AdapterError("Unreviewed public source route", "access")
    return clean


def html_fingerprint(body, key=None):
    """Guard all material prose and literal links, excluding reviewed chrome."""
    root = document_root(body.decode("utf-8", "strict"))
    # Retain literal destinations even inside the reviewed trivia sidebar.
    links = sorted({
        (node.tag, node.attrs.get("rel", ""), node.attrs["href"])
        for tag in ("a", "link") for node in nodes(root, tag)
        if node.attrs.get("href")
    })
    sidebars = nodes(root, "div", "DidYouKnowCnt")
    expected = SIDEBAR_COUNTS[key]
    if len(sidebars) != expected or any(
        node.attrs != {"class": "DidYouKnowCnt"} for node in sidebars
    ):
        raise AdapterError("Changed reviewed sidebar structure: " + key, "parse")
    for node in sidebars:
        if any(node in list(main.walk()) for main in root.walk()
               if main.attrs.get("id") == "main"):
            raise AdapterError("Trivia sidebar moved into material content", "parse")
        children = [child for child in node.children if isinstance(child, Node)]
        classes = ("DidYouKnowNaslov", "DidYouKnowSlika", "DidYouKnowTekst")
        if (len(children) != 3
                or any(child.tag != "div" or child.attrs != {"class": value}
                       for child, value in zip(children, classes))
                or children[0].text() != "Did You Know"):
            raise AdapterError("Changed reviewed trivia sidebar shape", "parse")
        node.children = ["reviewed-nonmaterial-random-sidebar"]
    dates = nodes(root, "div", "NewsOsobnaDatum")
    if key in NEWS_LEAF_KEYS:
        if len(dates) != 1 or dates[0].attrs != {"class": "NewsOsobnaDatum"}:
            raise AdapterError("Changed news display-date structure: " + key, "parse")
        node = dates[0]
        if len(node.children) != 1 or not isinstance(node.children[0], str):
            raise AdapterError("Changed plain news display date: " + key, "parse")
        try:
            datetime.strptime(node.text(), "%d %B %Y")
        except ValueError:
            raise AdapterError("Invalid news display date: " + key, "parse") from None
        node.children = ["reviewed-dynamic-news-display-date"]
    elif dates:
        raise AdapterError("Unexpected news display date: " + key, "parse")
    facts = json.dumps({"text": root.text(), "links": links},
                       ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(facts.encode()).hexdigest()


def read_input(key):
    info = INPUTS[key]
    try:
        status, headers, body = fetch_public(info["url"])
    except AdapterError as error:
        raise AdapterError(
            "Required publisher input " + key + ": " + safe_error(error),
            error.stage,
            error.status,
        ) from None
    if info["format"] == "html":
        digest = html_fingerprint(body, key)
    else:
        if info["format"] == "pdf" and not body.startswith(b"%PDF-"):
            raise AdapterError("Invalid material document: " + key, "parse")
        digest = hashlib.sha256(body).hexdigest()
    if digest != info["sha256"]:
        raise AdapterError(
            "Reviewed source facts/frontier changed: " + key, "parse"
        )
    return digest


def read_pages():
    """Exhaust the twenty reviewed own inputs once in their declared order."""
    return {key: read_input(key) for key in INPUTS}


def parse_inventory(pages):
    if set(pages) != set(INPUTS) or any(
        pages[key] != info["sha256"] for key, info in INPUTS.items()
    ):
        raise AdapterError("Incomplete reviewed programme frontier", "parse")
    records = []
    now = datetime.now(timezone.utc)
    for profile in PROFILES:
        record = make_record(
            profile["title"],
            profile["url"],
            [profile["category"]],
            kind=profile["kind"],
            host_countries=profile["hosts"],
            evidence=[ATTRIBUTION, profile["proof"]],
        )
        record["id"] = hashlib.sha256(
            (SOURCE_ID + "|" + profile["key"]).encode()
        ).hexdigest()[:24]
        record["summary"] = profile["summary"]
        record["summary_language"] = "en"
        record["deadline"] = profile["deadline"]
        record["language"] = profile.get("language", LANGUAGE)
        record["eligible_countries"] = profile.get("eligible_countries", [])
        deadline = profile["deadline"]
        if deadline is not None:
            record["status"] = (
                "expired"
                if now.date().isoformat() > deadline
                else "open" if now.date().isoformat() < deadline else "unknown"
            )
        records.append(record)
    validate_records(records)
    if len(records) != 12:
        raise AdapterError(
            "Incomplete reviewed identity partition", "validate"
        )
    return records


def collect():
    if _PHASE_END is not None:
        check_deadline()
        return parse_inventory(read_pages())
    with execution():
        with phase("prepublication", 900):
            prepare_collection()
            return parse_inventory(read_pages())


def metadata(attempt, previous, records, error=None, checked_at=None):
    outcome = {
        "source": SOURCE_ID,
        "name": "Study in Croatia — bounded scholarships and training programmes",
        "source_url": SOURCE_URL,
        "website_url": WEBSITE_URL,
        "language": LANGUAGE,
        "publisher_country": PUBLISHER_COUNTRY,
        "publisher_type": PUBLISHER_TYPE,
        "attribution": ATTRIBUTION,
        "status": "fail" if error else "success",
        "last_attempt_at": attempt,
        "last_success_at": (
            previous.get("last_success_at") if error else attempt
        ),
        "last_checked_at": (
            previous.get("last_checked_at") if error else checked_at
        ),
        "record_count": len(records),
        "message": (
            "Collection failed; last-good data preserved"
            if error
            else f"Collected {len(records)} reviewed Study in Croatia opportunities"
        ),
        "error": safe_error(error) if error else None,
    }
    if error:
        outcome["failure_stage"] = getattr(error, "stage", "parse")
    return outcome


def validate_prior_metadata(previous, records):
    """Accept failure writes only after the complete owned last-good pair."""
    if (
        not isinstance(previous, dict)
        or previous.get("source") != SOURCE_ID
        or previous.get("source_url") != SOURCE_URL
        or previous.get("website_url") != WEBSITE_URL
        or not isinstance(previous.get("message"), str)
        or previous.get("status") not in {"success", "fail"}
        or type(previous.get("record_count")) is not int
        or previous["record_count"] != len(records)
        or previous.get("language") != LANGUAGE
        or previous.get("publisher_country") != PUBLISHER_COUNTRY
        or (previous.get("status") == "success" and previous.get("error") is not None)
        or (previous.get("status") == "fail" and (
            not isinstance(previous.get("error"), str)
            or not previous["error"].strip()
            or not isinstance(previous.get("failure_stage"), str)
            or not previous["failure_stage"].strip()
        ))
    ):
        raise AdapterError(
            "Invalid previous source metadata contract", "publish"
        )
    timestamps = {}
    try:
        for field in ("last_attempt_at", "last_success_at", "last_checked_at"):
            value = previous.get(field)
            if not isinstance(value, str):
                raise ValueError()
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError()
            timestamps[field] = parsed
        if timestamps["last_success_at"] > timestamps["last_checked_at"]:
            raise ValueError()
        if previous["status"] == "success":
            if timestamps["last_attempt_at"] != timestamps["last_success_at"]:
                raise ValueError()
        elif timestamps["last_attempt_at"] < timestamps["last_checked_at"]:
            raise ValueError()
        for record in records:
            parsed = datetime.fromisoformat(
                record["last_checked_at"].replace("Z", "+00:00")
            )
            if parsed.tzinfo is None or parsed != timestamps["last_checked_at"]:
                raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise AdapterError(
            "Invalid previous metadata timestamps", "publish"
        ) from None



def phase_error(name):
    return AdapterError(
        "Whole " + name + " phase deadline exceeded",
        "publish" if name == "publication" else "access",
    )


def serialize(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def fresh_records(records, old, attempt, checked):
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
        same = {k: v for k, v in before.items() if k not in temporal} == {
            k: v for k, v in record.items() if k not in temporal
        }
        record.update(
            created_at=before.get("created_at", attempt),
            first_seen_at=before.get("first_seen_at", attempt),
            updated_at=before.get("updated_at", attempt) if same else attempt,
            last_seen_at=attempt,
            last_checked_at=checked,
        )
    records.sort(key=lambda record: record["id"])
    validate_records(records)


def run_body(publishing):
    attempt = utc_now()
    snapshot = None
    old, previous, records, desired = [], {}, [], None
    failure = None
    try:
        with phase("prepublication", 900):
            prepare_collection()
            if publishing:
                candidate = read_snapshot()
                if candidate["files"]["data.json"] is not None:
                    candidate_records = json.loads(
                        candidate["files"]["data.json"]
                    )
                    candidate_metadata = json.loads(
                        candidate["files"]["metadata.json"]
                    )
                    validate_records(candidate_records)
                    validate_prior_metadata(
                        candidate_metadata, candidate_records
                    )
                    old, previous = candidate_records, candidate_metadata
                snapshot = candidate
            records = collect()
            checked = utc_now()
            fresh_records(records, old, attempt, checked)
            outcome = metadata(attempt, previous, records, checked_at=checked)
            desired = {
                "data.json": serialize(records),
                "metadata.json": serialize(outcome),
            }
    except PhaseExpired:
        failure = phase_error("prepublication")
    except Exception as error:
        failure = error
    if failure is None and publishing:
        try:
            with phase("publication", 180):
                publish_files(snapshot, desired)
        except PhaseExpired:
            failure = phase_error("publication")
        except Exception as error:
            failure = error
    if failure is not None:
        outcome = metadata(attempt, previous, old, failure)
        if publishing:
            try:
                with phase("failure", 180):
                    if snapshot is None:
                        raise AdapterError(
                            "No validated previous source pair", "publish"
                        )
                    latest = read_snapshot()
                    if desired is not None and latest["files"] == desired:
                        outcome = json.loads(desired["metadata.json"])
                        failure = None
                    elif old and latest["files"] == snapshot["files"]:
                        publish_files(
                            latest,
                            {
                                "metadata.json": serialize(outcome),
                            },
                        )
                    else:
                        raise AdapterError(
                            "No safe unchanged last-good pair", "publish"
                        )
            except (Exception, PhaseExpired):
                outcome["message"] = (
                    "Run failed; durable outcome unconfirmed. "
                    "No newer source pair was overwritten."
                )
        elif not old:
            outcome["message"] = "Collection failed; no publication attempted"
    arm_alarm(_RUN_END - EXPORT_TIMEOUT)
    print(json.dumps(outcome, ensure_ascii=False))
    return 1 if failure is not None else 0


def run_lifecycle(publishing):
    result = None
    try:
        result = run_body(publishing)
    except PhaseExpired:
        result = 1
    finally:
        try:
            with phase("export", EXPORT_TIMEOUT):
                export_family_artifact()
        except (Exception, PhaseExpired):
            try:
                arm_alarm(_RUN_END)
                print(
                    "Timing artifact export failed; next run fail closed",
                    file=sys.stderr,
                )
            except (Exception, PhaseExpired):
                pass
            if result == 0:
                result = 1
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    try:
        with execution():
            return run_lifecycle(args.publish)
    except PhaseExpired:
        return 1


NEWS_LEAF_KEYS = {'bilateral-2023',
 'bilateral-2024',
 'biomedical',
 'cooperation-macedonia',
 'dental',
 'medical'}
SIDEBAR_COUNTS = {'about': 1,
 'bilateral-2023': 0,
 'bilateral-2024': 0,
 'biomedical': 0,
 'cookies': 1,
 'cooperation-macedonia': 0,
 'dental': 0,
 'disclaimer': 1,
 'exchange': 1,
 'faq': 1,
 'funding': 1,
 'home': 0,
 'learning': 1,
 'medical': 0,
 'news': 0,
 'news-page1': 0,
 'news-page2': 0,
 'news-page3': 0,
 'news-page4': 0,
 'privacy': 1}
INPUTS = {'about': {'url': 'https://www.studyincroatia.hr/about-us/about-us-39/',
           'format': 'html',
           'sha256': 'fe41cc3fb79d8f15ad0e49be5730116a75d1a182d4211a1b20bd1f3218e0b0da'},
 'bilateral-2023': {'url': 'https://www.studyincroatia.hr/news/scholarships-of-the-republic-of-croatia-call-for-applications-for-the-academic-year-2023-2024/',
                    'format': 'html',
                    'sha256': 'cd9e868f9a21906aa3b529588a1ecd260331808717a3a2d7536e0eb122148546'},
 'bilateral-2024': {'url': 'https://www.studyincroatia.hr/news/scholarships-of-the-republic-of-croatia-call-for-applications-for-the-academic-year-2024-2025/',
                    'format': 'html',
                    'sha256': '1d81ea32dc3f463167b7d9fd97bbc583ff68df25ad2d5fee09ba6742c4fa27e8'},
 'biomedical': {'url': 'https://www.studyincroatia.hr/news/applications-for-the-international-graduate-biomedical-mathematics-study-programme-in-english-now-open/',
                'format': 'html',
                'sha256': '7c3ad9ada8ef0c063ab27bbdddfc6edb0cc3f19b239b81816131577df4b06767'},
 'cookies': {'url': 'https://www.studyincroatia.hr/about-us/cookie-statement/',
             'format': 'html',
             'sha256': 'e29ab9a047a40f4051a1f8006b1b41c1abcb0d2c87f13c06398f8eb66ff12d58'},
 'cooperation-macedonia': {'url': 'https://www.studyincroatia.hr/news/cooperation-programme-between-croatia-and-north-macedonia/',
                           'format': 'html',
                           'sha256': 'f655f863f8efb37d4fff7d62eb050e6c97ea0e693791e9cd796e6285ceeca3e8'},
 'dental': {'url': 'https://www.studyincroatia.hr/news/entrance-exam-applications-for-the-dental-study-programme-in-english-now-open/',
            'format': 'html',
            'sha256': '59410b2ea8b304525f8ed66fa19685f4ef91c8e5e904fe381ccdfe0ca0a7a36b'},
 'disclaimer': {'url': 'https://www.studyincroatia.hr/about-us/content-disclaimer/',
                'format': 'html',
                'sha256': '8c4ee86a588117054604fff9e515866581a0c62d4281556e5ec1e7cd32fbfc60'},
 'exchange': {'url': 'https://www.studyincroatia.hr/study-in-croatia/exchange-programmes/',
              'format': 'html',
              'sha256': '8252530f4909bfe2974e0a8ef1fad134b3b76fd4cf1cb63dfc6addfb442bb860'},
 'faq': {'url': 'https://www.studyincroatia.hr/faq/',
         'format': 'html',
         'sha256': 'd1de8c6a59282e581aa6a1d7d78562101d5fd26a73a8634862d6be377ff72ca2'},
 'funding': {'url': 'https://www.studyincroatia.hr/study-in-croatia/tuition-fees-and-scholarships/',
             'format': 'html',
             'sha256': '5526628839e0e35ef7d1fd433361192d868e54fc235b47eae8e69f35e73e612f'},
 'home': {'url': 'https://www.studyincroatia.hr/',
          'format': 'html',
          'sha256': 'ca4f7b76129d96c4ee91d295cfd46246d65fb3ebfe62b683080a0d18f1ea1cbc'},
 'learning': {'url': 'https://www.studyincroatia.hr/live-and-work-in-croatia/learning-croatian/',
              'format': 'html',
              'sha256': '44e073a7e8ee3d0e8ec63af64f7050158096ed9e2d7846e8f832e775e47c6c9a'},
 'medical': {'url': 'https://www.studyincroatia.hr/news/university-of-zagreb-school-of-medicine-opens-applications-for-medical-studies-in-english/',
             'format': 'html',
             'sha256': 'cdd3143a50f9e30d9eef7c9b26518afacc38fd641e7a5ec3affd8843d6dd43a4'},
 'news-page1': {'url': 'https://www.studyincroatia.hr/news/?page=1',
                'format': 'html',
                'sha256': '6875c65ef74d983e7533bf18d8033d7d8ddd681edd8e75e66717ccf4e6bfc238'},
 'news-page2': {'url': 'https://www.studyincroatia.hr/news/?page=2',
                'format': 'html',
                'sha256': 'a098670e40ddb483b4a992e62c942d17b77d73bea36b6b5cede374805632e6ab'},
 'news-page3': {'url': 'https://www.studyincroatia.hr/news/?page=3',
                'format': 'html',
                'sha256': '18390286d89806c8fa45a483eca5927c80b8c2c9699e08921242520035b1dcd5'},
 'news-page4': {'url': 'https://www.studyincroatia.hr/news/?page=4',
                'format': 'html',
                'sha256': 'd25c18d42622c8c1235ad3a44d39602f442be1169facf1882c4c91c2f77ade15'},
 'news': {'url': 'https://www.studyincroatia.hr/news/',
          'format': 'html',
          'sha256': 'd74144f5edc2300d78d6dd3703406da1e30aaf8d2cf786db8b6ff0494bbdb62f'},
 'privacy': {'url': 'https://www.studyincroatia.hr/about-us/privacy-policy-statement/',
             'format': 'html',
             'sha256': 'e98d25caa2dcbf64b58bbcde9dbde23eb1bab93806522b36903a66e8349a82e7'}}
PROFILES = [{'key': 'https://www.studyincroatia.hr/study-in-croatia/exchange-programmes/|Erasmus-incoming-standing',
  'title': 'ERASMUS+',
  'url': 'https://www.studyincroatia.hr/study-in-croatia/exchange-programmes/',
  'kind': 'programme-overview',
  'category': 'scholarships',
  'hosts': ['HR'],
  'summary': 'Incoming Erasmus+ study or traineeship mobility for students '
             'enrolled at a higher education institution abroad. Study stays '
             'last one or two semesters in Croatia; virtual and blended '
             'mobility are also supported. A signed sending/receiving '
             'institutional agreement is required. Check the home Erasmus '
             'coordinator or international office for an eligible agreement. '
             'Grant amounts, application closing and other selection '
             'conditions are not provided.',
  'deadline': None,
  'proof': 'Own reviewed exchange programme/call; '},
 {'key': 'https://www.studyincroatia.hr/study-in-croatia/exchange-programmes/|CEEPUS-incoming-standing',
  'title': 'CEEPUS',
  'url': 'https://www.studyincroatia.hr/study-in-croatia/exchange-programmes/',
  'kind': 'programme-overview',
  'category': 'scholarships',
  'hosts': ['HR'],
  'summary': 'Student exchange scholarships through CEEPUS networks or '
             'freemover mobility when a home/host university is outside a '
             'network. Networks require at least three universities in three '
             'countries. Participating countries: Albania, Austria, Bosnia '
             'and Herzegovina, Bulgaria, Croatia, North Macedonia, Moldova, '
             'Montenegro, Poland, Kosovo, Romania, Serbia, Slovakia, '
             'Slovenia, Czechia and Hungary. The source limits applicants to '
             'students from these countries; a passport rule is not '
             'specified. Amount and closing are not provided.',
  'deadline': None,
  'proof': 'Own reviewed exchange programme/call; '},
 {'key': 'https://www.studyincroatia.hr/study-in-croatia/exchange-programmes/|bilateral-incoming-standing',
  'title': 'BILATERAL SCHOLARSHIPS',
  'url': 'https://www.studyincroatia.hr/study-in-croatia/exchange-programmes/',
  'kind': 'programme-overview',
  'category': 'scholarships',
  'hosts': ['HR'],
  'summary': 'Standing Croatian Government bilateral scholarship framework '
             'for incoming students, teachers and researchers undertaking '
             'study or research in Croatia. Scholarship numbers and types '
             'vary by the bilateral agreement with each country. This '
             'overview does not specify a current application round, grant '
             'amount, exact country eligibility or closing date. Check the '
             'administering Agency for Mobility and EU Programmes for the '
             'applicable call.',
  'deadline': None,
  'proof': 'Own reviewed exchange programme/call; '},
 {'key': 'https://www.studyincroatia.hr/study-in-croatia/tuition-fees-and-scholarships/|diaspora-scholarships-2025-26',
  'title': 'SCHOLARSHIPS TO UNIVERSITY STUDENTS WHO ARE CROATIANS LIVING '
           'ABROAD',
  'url': 'https://www.studyincroatia.hr/study-in-croatia/tuition-fees-and-scholarships/',
  'kind': 'programme-overview',
  'category': 'scholarships',
  'hosts': ['HR', 'BA', 'ME', 'XK', 'MK', 'RS'],
  'summary': '2025/26 scholarship overview for Croatians living abroad who '
             'study full-time at public higher education institutions in '
             'Croatia, Bosnia and Herzegovina, Montenegro, Kosovo, North '
             'Macedonia or Serbia. EUR175 per month for ten months, once for '
             'one academic year. The 2,000 places are programme capacity. '
             'Applications require electronic documentation and a paper '
             'application with mandatory documents. Croatian '
             'heritage/residence abroad is stated; passport eligibility, the '
             'closing and further selection conditions require the official '
             'call.',
  'deadline': None,
  'proof': 'Own reviewed funding programme/call; Keep deadline null; '
           'academic-year2025/26 programme overview, not invented2026/27 '
           'call. Allocations300HR/1500BA/30ME/5XK/15MK/150RS; '
           'within300HR:200BAresident/60minority/40diaspora. No '
           'country-capacity clones; special enrolment quota later on same '
           'page is NOT a scholarship.'},
 {'key': 'https://www.studyincroatia.hr/news/scholarships-of-the-republic-of-croatia-call-for-applications-for-the-academic-year-2024-2025/|bilateral-2024-25',
  'title': 'Scholarships of the Republic of Croatia - Call for Applications '
           'for the academic year 2024/2025',
  'url': 'https://www.studyincroatia.hr/news/scholarships-of-the-republic-of-croatia-call-for-applications-for-the-academic-year-2024-2025/',
  'kind': 'opportunity',
  'category': 'scholarships',
  'hosts': ['HR'],
  'summary': 'Historical 2024/25 bilateral call for foreign higher-education '
             'students, teachers and research fellows to study, teach or '
             'research in any field at an accredited public Croatian '
             'higher-education or research institution agreed in advance. '
             'Mobilities run 1 October 2024–30 September 2025, excluding July '
             'and August. Applications through the Agency system were '
             'required before 5 April 2024; no time or timezone is stated. '
             'Grant amounts and further eligibility are not supplied in this '
             'notice.',
  'deadline': None,
  'proof': 'Own reviewed bilateral-2024 programme/call; Literal '
           'before5April2024 is preserved in summary. Keep deadlineNULL '
           'because exact exclusive/inclusive cutoff and clock are not '
           'established; website treats date-only deadlines inclusive through '
           'UTCendofday, which would overstate this wording. No shift '
           'toApril4 or inventedclock. Own more-information link points '
           'external2023/24 article; do not borrow mismatched-edition '
           'conditions.'},
 {'key': 'https://www.studyincroatia.hr/news/scholarships-of-the-republic-of-croatia-call-for-applications-for-the-academic-year-2023-2024/|bilateral-2023-24',
  'title': 'Scholarships of the Republic of Croatia - Call for Applications '
           'for the academic year 2023/2024',
  'url': 'https://www.studyincroatia.hr/news/scholarships-of-the-republic-of-croatia-call-for-applications-for-the-academic-year-2023-2024/',
  'kind': 'opportunity',
  'category': 'scholarships',
  'hosts': ['HR'],
  'summary': 'Historical 2023/24 scholarship call for foreign '
             'higher-education students, teachers and research fellows '
             'undertaking study, teaching or research in any field at an '
             'accredited public Croatian higher-education or research '
             'institution agreed in advance. Mobility period: 1 October '
             '2023–30 September 2024, excluding July and August. Application '
             'deadline was 31 March 2023, with no time or timezone stated. '
             'Grant amounts and additional eligibility conditions are not '
             'supplied in this notice.',
  'deadline': '2023-03-31',
  'proof': 'Own reviewed bilateral-2023 programme/call; '},
 {'key': 'https://www.studyincroatia.hr/news/cooperation-programme-between-croatia-and-north-macedonia/|cooperation-hr-mk-2024-28',
  'title': 'Cooperation Programme between Croatia and North Macedonia',
  'url': 'https://www.studyincroatia.hr/news/cooperation-programme-between-croatia-and-north-macedonia/',
  'kind': 'programme-overview',
  'category': 'scholarships',
  'hosts': ['HR', 'MK'],
  'summary': 'Education cooperation valid 29 September 2024–31 December 2028. '
             'North Macedonian citizens enrolling in a full degree in Croatia '
             'receive the same tuition-fee conditions as domestic students; '
             'this does not guarantee zero tuition. The programme also '
             'exchanges up to 24 advanced-study scholarships annually plus '
             'summer language-course scholarships. Awards, application dates '
             'and detailed scholarship eligibility are not stated. The North '
             'Macedonian nationality condition concerns the tuition measure, '
             'not every exchange award.',
  'deadline': None,
  'proof': 'Own reviewed cooperation-macedonia programme/call; '},
 {'key': 'https://www.studyincroatia.hr/news/applications-for-the-international-graduate-biomedical-mathematics-study-programme-in-english-now-open/|biomedmath-2026-27-second-round',
  'title': 'Applications for the International Graduate Biomedical '
           'Mathematics Study Programme in English Now Open!',
  'url': 'https://www.studyincroatia.hr/news/applications-for-the-international-graduate-biomedical-mathematics-study-programme-in-english-now-open/',
  'kind': 'opportunity',
  'category': 'training',
  'hosts': ['HR'],
  'summary': 'Second 2026/27 application round for Zagreb Faculty of '
             'Science’s English-taught Biomedical Mathematics Master’s closed '
             '30 June 2026. Requires signed application, passport pages, '
             'secondary diploma, Bachelor’s degree if available, transcripts, '
             'CV and motivation letter in English, plus proof of EUR35 '
             'application-fee payment. Non-native English speakers need at '
             'least B2 proficiency. Selected candidates have an English '
             'online interview focused mainly on mathematics background. '
             'Tuition and full academic eligibility are not stated.',
  'deadline': '2026-06-30',
  'proof': 'Own reviewed biomedical programme/call; '},
 {'key': 'https://www.studyincroatia.hr/news/entrance-exam-applications-for-the-dental-study-programme-in-english-now-open/|dental-2026-27-exam-windows',
  'title': 'Entrance Exam Applications for the Dental Study Programme in '
           'English Now Open',
  'url': 'https://www.studyincroatia.hr/news/entrance-exam-applications-for-the-dental-study-programme-in-english-now-open/',
  'kind': 'opportunity',
  'category': 'training',
  'hosts': ['HR'],
  'summary': 'Zagreb Dental Medicine’s English-taught programme, 2026/27 '
             'entrance-exam notice. Applications and payment proof were due '
             '19 April for the in-person Zagreb exam on 25 April, or 26 April '
             'for the online exam on 12 May for distant-country candidates. '
             'These announced 2026 windows have passed; no later round is '
             'established. Exam format and syllabus follow Zagreb Medicine. '
             'Payment amount, tuition and full admissions requirements are '
             'not stated. Two mode-specific closings are retained without a '
             'single programme-wide deadline.',
  'deadline': None,
  'proof': 'Own reviewed dental programme/call; 2026 comes from actual2026/27 '
           'intake and original3April2026 indexed publication, not '
           'executionyear. Single identity: physical/online exams are two '
           'routes of samecall, not duplicated calls. deadlineNULL/unknown; '
           'summarypreservesboth actualclosings/examdates.'},
 {'key': 'https://www.studyincroatia.hr/news/university-of-zagreb-school-of-medicine-opens-applications-for-medical-studies-in-english/|medicine-2026-27-online-exam',
  'title': 'University of Zagreb School of Medicine Opens Applications for '
           'Medical Studies in English',
  'url': 'https://www.studyincroatia.hr/news/university-of-zagreb-school-of-medicine-opens-applications-for-medical-studies-in-english/',
  'kind': 'opportunity',
  'category': 'training',
  'hosts': ['HR'],
  'summary': '2026/27 call for international applicants to enter Zagreb '
             'Medicine’s first year of integrated undergraduate and graduate '
             'studies taught in English. Applications for the 12 May 2026 '
             'online entrance exam closed 26 April 2026. The exam covers '
             'high-school biology, physics and chemistry, with 120 questions: '
             '40 per subject. The notice does not establish later exam '
             'rounds, tuition, application fees or the full admissions '
             'conditions; consult the institution’s requirements.',
  'deadline': '2026-04-26',
  'proof': 'Own reviewed medical programme/call; '},
 {'key': 'https://www.studyincroatia.hr/live-and-work-in-croatia/learning-croatian/|croatian-beginner-elearning-standing',
  'title': 'e-learning course of the Croatian language',
  'url': 'https://www.studyincroatia.hr/live-and-work-in-croatia/learning-croatian/',
  'kind': 'programme-overview',
  'category': 'training',
  'hosts': [],
  'summary': 'Beginner Croatian e-learning course offered jointly by the '
             'University of Zagreb, Croatian Heritage Foundation and '
             'University Computing Centre. Intended for people with no '
             'previous Croatian or only very basic knowledge. This is an '
             'online learning programme; the source does not provide a '
             'current enrolment round, price, teaching duration, certificate '
             'conditions or application closing. Follow the linked university '
             'course information for current terms.',
  'deadline': None,
  'proof': 'Own reviewed learning programme/call; '},
 {'key': 'https://www.studyincroatia.hr/live-and-work-in-croatia/learning-croatian/|rijeka-croatian-school-standing',
  'title': 'School of Croatian Language, Culture and Civilisation',
  'url': 'https://www.studyincroatia.hr/live-and-work-in-croatia/learning-croatian/',
  'kind': 'programme-overview',
  'category': 'training',
  'hosts': ['HR'],
  'summary': 'Croatian language, culture and civilisation school associated '
             'with the Rijeka School of Croatian Studies at the University of '
             'Rijeka’s Faculty of Humanities and Social Sciences. The school '
             'takes place at the end of June or beginning of July; no '
             'year-specific edition or application closing is given. Fees, '
             'entry requirements, teaching duration, exact venue and current '
             'enrolment availability are not specified; check the school’s '
             'information.',
  'deadline': None,
  'proof': 'Own reviewed learning programme/call; PhysicalCroatiacountry '
           'fromschool contextRijekaUniversity/Croatia, exactvenueunknown; '
           'programmeperiodNOTclosing.'}]

if __name__ == "__main__":
    sys.exit(main())
