"""Collect the bounded reviewed University of Zagreb public opportunity frontier."""

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

SOURCE_ID = "hr-university-of-zagreb"
SOURCE_URL = "https://www.unizg.hr/studiji-i-studiranje/upisi-stipendije-priznavanja/stipendije/"
WEBSITE_URL = "https://www.unizg.hr/"
LANGUAGE = "hr"
PUBLISHER_COUNTRY = "HR"
PUBLISHER_TYPE = "university"
ATTRIBUTION = "Sveučilište u Zagrebu https://www.unizg.hr/ and the named programme administrators"

_GITHUB = "https://api.github.com/repos/YouthOpps/data-source"
_WORKFLOW_GITHUB = "https://api.github.com/repos/YouthOpps/data-pipeline"
_USER_AGENT = (
    "YouthOpps/1.0 (+https://github.com/YouthOpps/data-pipeline; "
    "public opportunity metadata)"
)
CATEGORIES = {"scholarships", "grants", "internships", "jobs", "training", "fellowships"}
FAMILY = "youthopps-unizg-publisher-v1"
FAMILY_SOURCES = {"hr-university-of-zagreb"}
COLLECTION_TIMEOUT = 2100
RUN_TIMEOUT = 2490
EXPORT_TIMEOUT = 30
_STATE_SCHEMA = 1
_ARTIFACT_NAME = "unizg-pacing-state"
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
        if publisher and status in (401, 403, 429):
            refused = urllib.parse.urlsplit(current)
            provenance = urllib.parse.urlunsplit(
                (refused.scheme, refused.hostname or "", refused.path, "", "")
            )
            raise AdapterError(
                f"Publisher refused access: HTTP {status}: " + provenance,
                "access",
                status,
            )
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
        raw = os.environ.get("UNIZG_PACING_BOOTSTRAP", "")
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
    path = os.environ.get("UNIZG_PACING_ARTIFACT_PATH")
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
    allowed = {info["url"] for info in INPUTS.values()} | {"https://www.unizg.hr/robots.txt"}
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
    root = document_root(body.decode("utf-8", "strict"))
    scroll = None
    aliases = {
        "lifelong-programmes": {
            "/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/arhiva/arhiva-ljetne-skole/#0",
            "/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/#0",
        },
        "research-closed": {
            "/istrazivanje/istrazivanje-i-inovacije/financiranje-istrazivanja/zatvoreni-natjecaji/?IDX_Spectacle=59035#0",
            "/istrazivanje/istrazivanje-i-inovacije/financiranje-istrazivanja/zatvoreni-natjecaji/#0",
        },
    }
    if key in aliases:
        scroll = one(
            nodes(root, "a", "cd-top"), key + " reviewed Top scroll control"
        )
        if (
            set(scroll.attrs) != {"href", "class"}
            or scroll.attrs["class"] != "cd-top"
            or scroll.children != ["Top"]
        ):
            raise AdapterError(
                key + ": changed Top structure attrs_exact="
                + str(set(scroll.attrs) == {"href", "class"})
                + " class_exact=" + str(scroll.attrs.get("class") == "cd-top")
                + " plain_Top=" + str(scroll.children == ["Top"]),
                "parse",
            )
    links = sorted(
        {
            (
                node.tag,
                node.attrs.get("rel", ""),
                "reviewed-scroll:" + key if node is scroll else node.attrs["href"],
            )
            for tag in ("a", "link")
            for node in nodes(root, tag)
            if node is scroll or node.attrs.get("href")
        }
    )
    facts = json.dumps(
        {"text": root.text(), "links": links},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(facts.encode()).hexdigest()
    if scroll is not None and scroll.attrs["href"] not in aliases[key]:
        href = scroll.attrs["href"]
        href_digest = hashlib.sha256(href.encode("utf-8", "surrogatepass")).hexdigest()
        route = "[redacted]"
        # This grammar exposes only public own-site routes, never arbitrary
        # URL components. It diagnoses destinations without accepting/following them.
        if (
            len(href) <= 512
            and not re.search(r"[\x00-\x20\x7f\\]", href)
            and (href.startswith("/") and not href.startswith("//")
                 or href.startswith("https://www.unizg.hr/"))
        ):
            try:
                parts = urllib.parse.urlsplit(href)
                path = parts.path
                segments = path.split("/")
                query = (
                    "?" + parts.query
                    if re.fullmatch(r"IDX_Spectacle=[0-9]{1,20}", parts.query)
                    else "?[redacted]" if parts.query else ""
                )
                fragment = (
                    "#0" if parts.fragment == "0"
                    else "#[redacted]" if parts.fragment else ""
                )
                public = path + query + fragment
                if (
                    parts.scheme in ("", "https")
                    and parts.netloc in ("", "www.unizg.hr")
                    and path.startswith(("/o-sveucilistu/", "/istrazivanje/"))
                    and re.fullmatch(r"/[a-z0-9._/-]+", path)
                    and "//" not in path
                    and not any(segment in (".", "..") for segment in segments)
                    and not any(
                        re.search(r"auth|session|token|secret|password|credential|login|passwd", segment)
                        or len(segment) > 64
                        or re.fullmatch(r"[a-f0-9]{32,}", segment)
                        for segment in segments
                    )
                    and len(public) <= 300
                ):
                    route = public
            except ValueError:
                pass
        diagnostic = {
            "key": key,
            "material_sha256": digest,
            "literal_href_sha256": href_digest,
            "public_route": route,
        }
        try:
            message = json.dumps(diagnostic, separators=(",", ":"))
            if len(message.encode("utf-8")) <= 1024:
                print(message, file=sys.stderr, flush=True)
        except (OSError, ValueError):
            pass
        raise AdapterError(
            key + " material_sha256=" + digest + ": unreviewed Top scroll destination",
            "parse",
        )
    return digest


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
    first = ("lifelong-programmes", "research-closed")
    fetched = {key: read_input(key) for key in first}
    for key in INPUTS:
        if key not in fetched:
            fetched[key] = read_input(key)
    return {key: fetched[key] for key in INPUTS}


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
    if len(records) != 398:
        raise AdapterError(
            "Incomplete reviewed identity partition", "validate"
        )
    return records


def collect():
    if _PHASE_END is not None:
        check_deadline()
        return parse_inventory(read_pages())
    with execution():
        with phase("prepublication", 2100):
            prepare_collection()
            return parse_inventory(read_pages())


def metadata(attempt, previous, records, error=None, checked_at=None):
    outcome = {
        "source": SOURCE_ID,
        "name": "University of Zagreb — bounded public calls and educational catalogue",
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
            else f"Collected {len(records)} reviewed University of Zagreb opportunities"
        ),
        "error": safe_error(error) if error else None,
    }
    if error:
        outcome["failure_stage"] = getattr(error, "stage", "parse")
    return outcome


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
        with phase("prepublication", 2100):
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
                    if (
                        not isinstance(candidate_metadata, dict)
                        or candidate_metadata.get("source") != SOURCE_ID
                    ):
                        raise AdapterError(
                            "Remote metadata source mismatch", "publish"
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


INPUTS = {'academic2026-first-call': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Sveucilisnog_osoblja/Bilaterala/2026/1_-_Prvi_krug/Natjecaj_za_akademsku_mobilnost_2026_prvi_krug_final.pdf',
                             'format': 'pdf',
                             'sha256': '4aca46aacb48cfc762674efd7e5825a5d49f5f357fb38e241ee99ef1460ebe9d'},
 'academic2026-first-round': {'url': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-akademsku-mobilnost-u-2026-godini-prvi-krug/',
                              'format': 'html',
                              'sha256': '6d096626131acaa686b60ed7e3b218eb0505cbc28b63b9139a9815d0eb26014c'},
 'ancillary-ambiguous-38': {'url': 'https://www.unizg.hr/nc/vijest/article/azrieli-international-visiting-phd-fellowship-stipendije-1/',
                            'format': 'html',
                            'sha256': '9d0bc08ea1848cbeb306b411da6c0f4f8d5be3e981c24037471b609ccb3ab3e2'},
 'ancillary-ambiguous-39': {'url': 'https://www.unizg.hr/nc/vijest/article/sveuciliste-u-zuerichu-stipendija/',
                            'format': 'html',
                            'sha256': 'b20174f201e7a3cb34953ef5646ecee2ddc8876e806ba297d309bf2a3af7422d'},
 'ancillary-ambiguous-40': {'url': 'https://www.unizg.hr/nc/vijest/article/azrieli-international-visiting-phd-fellowship-stipendije/',
                            'format': 'html',
                            'sha256': '74d3e1263507b0fb4cab17a21d3f861a484c0750df749590ed98caae6e464259'},
 'ancillary-ambiguous-41': {'url': 'https://www.unizg.hr/nc/vijest/article/azrieli-international-postdoctoral-i-visiting-phd-fellowship-stipendije/',
                            'format': 'html',
                            'sha256': '2e5cfd25948319d7d06b9b4252742fa57b1b34fa93f9242a593095412d61ca41'},
 'ancillary-current-1': {'url': 'https://www.unizg.hr/nc/vijest/article/bayhost-stipendije-u-bavarskoj-za-ak-god-20272028/',
                         'format': 'html',
                         'sha256': 'e47c3393ef52fa9f7e318e676a902c5aedf83ccc6b4a47bd8a9ff7d0af8e234d'},
 'ancillary-current-10': {'url': 'https://www.unizg.hr/nc/vijest/article/azrieli-international-postdoctoral-fellowship-stipendije-202627/',
                          'format': 'html',
                          'sha256': '4e206f12a1211187dfaa584587463a9fb1c667ed9a154f2b1f7bc4a4bb9a1e38'},
 'ancillary-current-11': {'url': 'https://www.unizg.hr/nc/vijest/article/stipendije-za-ucenje-islandskog-jezika-u-ak-god-20252026/',
                          'format': 'html',
                          'sha256': '41da69601ab90a1bfa821a4093bad03f1a022053af5a715d7a02b1d0192cac89'},
 'ancillary-current-12': {'url': 'https://www.unizg.hr/nc/vijest/article/stipendija-za-studij-na-georgia-institute-of-technology-ak-god-20252026/',
                          'format': 'html',
                          'sha256': 'f0a6ec7f893514516d7558caaa12f4af2c1c272fd03f655efc2ffc0e39e8a829'},
 'ancillary-current-13': {'url': 'https://www.unizg.hr/nc/vijest/article/mofa-taiwan-fellowship/',
                          'format': 'html',
                          'sha256': 'a4d00a64efaab75ebbe3e38042da82b6e323f6f775a2c022b1eb78fe74fed393'},
 'ancillary-current-14': {'url': 'https://www.unizg.hr/nc/vijest/article/universite-dete-francophone-en-relations-internationales-oif-bukurest-srpanj-2026-pot/',
                          'format': 'html',
                          'sha256': '72021785ff2988e9df3ba250221470d73a046090ecca91f76618e3b2d49d786a'},
 'ancillary-current-15': {'url': 'https://www.unizg.hr/nc/vijest/article/secoias-summer-school-2026-security-and-communication-in-aerospace-embedded-systems-francuska/',
                          'format': 'html',
                          'sha256': '70ef6fcbcac57eb2afefb09653fcf774349fcc26226430ab69f2ce43cc3ab051'},
 'ancillary-current-16': {'url': 'https://www.unizg.hr/nc/vijest/article/minato-international-summer-school-on-micro-and-nano-fabrication-toulouse-francuska/',
                          'format': 'html',
                          'sha256': '1322e75e191905bbac9525e56116e6f9a2deb94f8144788ed0ae25cc31b15da1'},
 'ancillary-current-17': {'url': 'https://www.unizg.hr/nc/vijest/article/summer-schools-at-hm-njemacka/',
                          'format': 'html',
                          'sha256': 'ff8230b64ae9ef3b21033013f59fbff3827b2d81a04d04d394b9593df1e191a8'},
 'ancillary-current-18': {'url': 'https://www.unizg.hr/nc/vijest/article/universidade-da-corunas-international-summer-school-iss/',
                          'format': 'html',
                          'sha256': 'cf9a3de0640b66524f2efcfa0337875c86b9492675e8471407bc0262fd31af30'},
 'ancillary-current-19': {'url': 'https://www.unizg.hr/nc/vijest/article/summer-school-universite-de-lorraine-francuska/',
                          'format': 'html',
                          'sha256': '5cb1e490bd1c4f8071652c1dbe187e5e79c0c47c91b4a06098a697b2c9e8ae01'},
 'ancillary-current-2': {'url': 'https://www.unizg.hr/nc/vijest/article/taiwan-scholarships-2026-stipendije-za-redoviti-studij-i-tecaj-mandarinskog-kineskog-na-tajvanu/',
                         'format': 'html',
                         'sha256': '7c8a4f71fa39a18490840e3022880c18d8aaf66225c902908c0b07cd256143ab'},
 'ancillary-current-20': {'url': 'https://www.unizg.hr/nc/vijest/article/ljubljana-summer-school-2026/',
                          'format': 'html',
                          'sha256': '447a38fa1a1a4194254f36359587881d52bd5088af8ee1721f8311a3e67800b0'},
 'ancillary-current-21': {'url': 'https://www.unizg.hr/nc/vijest/article/ljetna-skola-the-european-system-of-human-rights-protection-council-of-europe-eu-osce-njem/',
                          'format': 'html',
                          'sha256': '0f8f318979c0215f24a0535c325d287970aa48e911efaca70078c2bcca363f29'},
 'ancillary-current-22': {'url': 'https://www.unizg.hr/nc/vijest/article/international-summer-school-oth-regensburg-njemacka/',
                          'format': 'html',
                          'sha256': 'a317a028ae819d53456cc5b55a4d565648814d9e6e192ad95339d41e29f47414'},
 'ancillary-current-23': {'url': 'https://www.unizg.hr/nc/vijest/article/udk-berlin-summer-university-programme-2026/',
                          'format': 'html',
                          'sha256': '7dbfe31dd5a8e74604a583acb79c3cb0bca34b5968484afb927f06d674d929ab'},
 'ancillary-current-24': {'url': 'https://www.unizg.hr/nc/vijest/article/universidad-loyolas-summer-programme-in-spanish-language-culture/',
                          'format': 'html',
                          'sha256': '37382ef49d9f8f0de0d93daa324dcd8af10f9057000a5855cc4c965c71f93ae3'},
 'ancillary-current-25': {'url': 'https://www.unizg.hr/nc/vijest/article/antwerp-summer-university-2026/',
                          'format': 'html',
                          'sha256': 'def76640831ca0fb34b90f6f57e8d71acafca87d2a6c812b5368c6d4aa34d82b'},
 'ancillary-current-26': {'url': 'https://www.unizg.hr/nc/vijest/article/catolica-porto-business-school-international-summer-school-programmes/',
                          'format': 'html',
                          'sha256': 'b48bb387216749ed6225c4daba155b223b2c5f77930d245bd1b293a587858ece'},
 'ancillary-current-27': {'url': 'https://www.unizg.hr/nc/vijest/article/international-summer-university-isu-osnabrueck-campus-njemacka/',
                          'format': 'html',
                          'sha256': 'd722c51967d377690716ba3f1814335dca659534eb9ff6436c6d8ea88dfc37bc'},
 'ancillary-current-28': {'url': 'https://www.unizg.hr/nc/vijest/article/rennes-school-of-business-francuska/',
                          'format': 'html',
                          'sha256': '391a509060cc3a061532cef8c874b679ce3f6bec3837882f021ad3b978d4082d'},
 'ancillary-current-29': {'url': 'https://www.unizg.hr/nc/vijest/article/via-summer-school-danska/',
                          'format': 'html',
                          'sha256': 'e16e64d21938da6087e4c56d0d87c7eb24b48f71c1dca7747e24e843321ca7c0'},
 'ancillary-current-3': {'url': 'https://www.unizg.hr/nc/vijest/article/you-and-europe-program-stipendija-za-europske-studente-diplomskih-studija-u-njemackoj/',
                         'format': 'html',
                         'sha256': 'abf2897440ceab9ad5a46fba29602232edc5a85006f3130f5aba338085e5c0b1'},
 'ancillary-current-30': {'url': 'https://www.unizg.hr/nc/vijest/article/bsb-summer-school-2026-francuska/',
                          'format': 'html',
                          'sha256': '2640484ca59652844c878227264bd997eed20633c3e34e6ceadefec3aa029c4a'},
 'ancillary-current-31': {'url': 'https://www.unizg.hr/nc/vijest/article/the-hague-summer-school-2026/',
                          'format': 'html',
                          'sha256': 'd75f3a9cd78581ce947d4785d1e096585319352a83861e037c316111677c155f'},
 'ancillary-current-32': {'url': 'https://www.unizg.hr/nc/vijest/article/lidd-design-summer-camp/',
                          'format': 'html',
                          'sha256': '7575caed92db54aefb883cd899b809e49671e8dc19f639777e772d93e0e950a0'},
 'ancillary-current-33': {'url': 'https://www.unizg.hr/nc/vijest/article/european-summer-program-2026-lille-francuska/',
                          'format': 'html',
                          'sha256': '2813dc6e368181e056353a219e10759d1aa3346e34899fc1a22b4d539385711d'},
 'ancillary-current-34': {'url': 'https://www.unizg.hr/nc/vijest/article/ljetna-skola-for-apac-international-austrian-eu-summer-school-2026-austrija/',
                          'format': 'html',
                          'sha256': '8c19861d6369ad822301507ec595fb2df7fbedd2c1ac73dda6ff5e5a3b04467d'},
 'ancillary-current-35': {'url': 'https://www.unizg.hr/nc/vijest/article/universite-cote-dazur-graduate-school-of-economics-and-management-francuska/',
                          'format': 'html',
                          'sha256': '6769b5bfb42d81a22556c2740c6b90778aa3c64ee9d6344ad67d92589644648d'},
 'ancillary-current-36': {'url': 'https://www.unizg.hr/nc/vijest/article/bayhost-sipendije-za-tecajeve-njemackog-jezika-u-bavarskoj-2026/',
                          'format': 'html',
                          'sha256': 'afbd92224a6f83f5584609f9f1910a5c99a1ba24fd97de373e7415b3cb637807'},
 'ancillary-current-37': {'url': 'https://www.unizg.hr/nc/vijest/article/ljetna-skola-u-rio-de-janeiru/',
                          'format': 'html',
                          'sha256': '42655e748af308ef046c071349f4d0e4a22bba89d6aaf74f5a901d3fb044e741'},
 'ancillary-current-4': {'url': 'https://www.unizg.hr/nc/vijest/article/hardiman-doktorske-stipendije-na-university-of-galway-2026/',
                         'format': 'html',
                         'sha256': '53ea9cf1ff1454aa81438c02554b41c9f644d87b5aa82e6ba8d0b1a4ef4101af'},
 'ancillary-current-5': {'url': 'https://www.unizg.hr/nc/vijest/article/bayhost-stipendije-u-bavarskoj-za-ak-god-20262027/',
                         'format': 'html',
                         'sha256': 'd205bc3c827b8b5231fdc7aa227dcd32e68c46520bffe5e4d3763f6db5adc9bc'},
 'ancillary-current-6': {'url': 'https://www.unizg.hr/nc/vijest/article/stipendija-za-studij-na-georgia-institute-of-technology-ak-god-20262027-1/',
                         'format': 'html',
                         'sha256': '0d0261853ed5c158c3606cbadc9a8a38788d478f160e60f148416fb998aaca50'},
 'ancillary-current-7': {'url': 'https://www.unizg.hr/nc/vijest/article/stipendije-za-ucenje-islandskog-jezika-u-ak-god-20262027/',
                         'format': 'html',
                         'sha256': '5de4b10d6c353501f7a3858ec45329104da6bfaa4c5abc1c16e79eb01759d843'},
 'ancillary-current-8': {'url': 'https://www.unizg.hr/nc/vijest/article/bose-program-stipendiranja-ak-god-202627/',
                         'format': 'html',
                         'sha256': '22c4322dba5b9f827d84cd79c8703cf3f5b2f5b771247b99438aa31208979daa'},
 'ancillary-current-9': {'url': 'https://www.unizg.hr/nc/vijest/article/stipendija-za-studij-na-georgia-institute-of-technology-ak-god-20262027/',
                         'format': 'html',
                         'sha256': '71575acea9c593bbe09826b2406297efff3481317aa7c7832cfabae0c3a3f26e'},
 'course-catalogue': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/',
                      'format': 'html',
                      'sha256': 'cae6e0940f813fe941e99ed60474953744a694d425622a644ace4a2916259ebd'},
 'course-table-0': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/farmaceutsko-biokemijski-fakultet/',
                    'format': 'html',
                    'sha256': '1bf0c819749c55cc20ab17e21bb27f021df9f996068f1361faeb70b0b9bf576d'},
 'course-table-10': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/',
                     'format': 'html',
                     'sha256': 'c7769475c061eeb01bd623925b6ca86872c0c19e0a06459f42250fbeff784654'},
 'course-table-11': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/katolicki-bogoslovni-fakultet/',
                     'format': 'html',
                     'sha256': 'f6a1a71e04a359c8c0b0c2cc36de8121ed324d00c46daed6aa4559dcdde582cf'},
 'course-table-12': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/kinezioloski-fakultet/',
                     'format': 'html',
                     'sha256': '5ff20e08ee35f2b493322681fd6386f5ece3cedd9eed8d306244a56b6d97d005'},
 'course-table-13': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/pravni-fakultet/',
                     'format': 'html',
                     'sha256': 'e516e26bebfc1df03a45bc96ac0c4f115d66219bc090abe55155a738c8c1f04a'},
 'course-table-14': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/sveucilisni-centar-protestantske-teologije-matija-vlacic-ilirik/',
                     'format': 'html',
                     'sha256': 'be426fe1c60b71f4ba8dbee2579e2c934cd962b266f339ad6421cb34e86023f0'},
 'course-table-15': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/',
                     'format': 'html',
                     'sha256': 'cfbd6134f7ba8f21da1bfad45a1e316ae5b2737922a83d65a48aa56240497e07'},
 'course-table-16': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/prirodoslovno-matematicki-fakultet/',
                     'format': 'html',
                     'sha256': '555d2ad0754d5c26c5bf4a226d6c1b98c6f33478e6e6ab4165a2ed476060c956'},
 'course-table-17': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/',
                     'format': 'html',
                     'sha256': '9c5c4950d44ee9864fc12da0879af54b016ad95ecd69bcfb6d5949463f672ced'},
 'course-table-18': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-kemijskog-inzenjerstva-i-tehnologije/',
                     'format': 'html',
                     'sha256': 'ea0917d8cd28c06a946421ac05f975eff1e00af23857c232d9d6c2e378d17533'},
 'course-table-19': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/',
                     'format': 'html',
                     'sha256': '0784d1d51b0b2d4554748ca45b9496f5328329e986683a0a81d87b1d2daca8a3'},
 'course-table-2': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
                    'format': 'html',
                    'sha256': 'baef5a62d38577989dfa4e9d5e45c0d08e129a9908c7d6605f2a1ad575d31714'},
 'course-table-20': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/',
                     'format': 'html',
                     'sha256': '471181c1a993bb9f8d611a93e14878349084d9bad1052b4a9183e77efff2e966'},
 'course-table-21': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/gradevinski-fakultet/',
                     'format': 'html',
                     'sha256': '56fd40383d1cd6986138ea17d7f14047a1eecde25abd5c3414106153415d43ac'},
 'course-table-22': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/graficki-fakultet/',
                     'format': 'html',
                     'sha256': '8905b1e5f30d372cab8796f46210edc121c407667c56a674c28d4d4163902413'},
 'course-table-23': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/rudarsko-geolosko-naftni-fakultet/',
                     'format': 'html',
                     'sha256': 'f39327dd8b80c3f2c1d5402848526abe06348a77e2e3343e592bfcb9153ce466'},
 'course-table-24': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/tekstilno-tehnoloski-fakultet/',
                     'format': 'html',
                     'sha256': 'c62d264812a0bfe58accfe8d93b146be35ee7a5d4a7d5047badfb9039951a3a3'},
 'course-table-3': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
                    'format': 'html',
                    'sha256': '4068b1216370c2a253a7375fdb7134e6008a92d4d1c0f87de21d7375afd13e12'},
 'course-table-4': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/agronomski-fakultet/',
                    'format': 'html',
                    'sha256': '7836cc21a52d62542fdf46222da222c4f43074728d5bb8e3c250b9a4068f03f9'},
 'course-table-5': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-sumarstva-i-drvne-tehnologije/',
                    'format': 'html',
                    'sha256': 'd2c7120d92e0404fca31a946389bb04b0e744e25a4627720745b3266e5d68127'},
 'course-table-6': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
                    'format': 'html',
                    'sha256': 'c40327e9d0495d0c24c4580423abdfb3d6d3dd445f1df318b0f9d0e42205683d'},
 'course-table-7': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/ekonomski-fakultet/',
                    'format': 'html',
                    'sha256': 'cfb4dd8209f1c0fa83d96a6fdc594836abc031c1cb30aa789a0eb060c0982018'},
 'course-table-8': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-hrvatskih-studija/',
                    'format': 'html',
                    'sha256': '1d8b9aee3da21059613b5571fa46206040d4a62e13893b489af76134f3d41311'},
 'course-table-9': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-organizacije-i-informatike/',
                    'format': 'html',
                    'sha256': '697181e6de70a19d046cea9db0d22f0da8ece5cc489c16aef19cfe087a22080a'},
 'course-table-last1': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/muzicka-akademija/',
                        'format': 'html',
                        'sha256': 'd2f9c3623311457691f1d799394a84d81114e14b2941dd15d6100ed91e1e8ddc'},
 'doctoral': {'url': 'https://www.unizg.hr/suradnja/medunarodna-razmjena/razmjena-studenata/kratka-doktorska-mobilnost/',
              'format': 'html',
              'sha256': '4ac677a5d93246d93a56741ddae4a979daadf5a185b66011ad0e8db10c494b27'},
 'foreign': {'url': 'https://www.unizg.hr/studiji-i-studiranje/upisi-stipendije-priznavanja/strani-studenti/',
             'format': 'html',
             'sha256': 'be936ea9979b5c764e559d12d2576a80079de5d6947460e23029823ba8b1d008'},
 'foundation': {'url': 'https://www.unizg.hr/suradnja/alumni-i-zaklada-sveucilista/zaklada-sveucilista-u-zagrebu/',
                'format': 'html',
                'sha256': 'd95196b41e20085d4ec6e371b28252067ccddad14302378c04d4740b0ddc3cfb'},
 'funding': {'url': 'https://www.unizg.hr/istrazivanje/istrazivanje-i-inovacije/financiranje-istrazivanja/',
             'format': 'html',
             'sha256': 'e82532e1153046f1b0643238c946cedeccaa0cf557a5ae5acd5375fd09d44f21'},
 'general-calls': {'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/',
                   'format': 'html',
                   'sha256': '325e7f1984520fb52ede27a3c11b33579d9dd5ebb776f91173d69d91b8c2d007'},
 'hr-eu': {'url': 'https://www.unizg.hr/studiji-i-studiranje/upisi-stipendije-priznavanja/hrvatski-i-eu-studenti/',
           'format': 'html',
           'sha256': '36cbdec715f65207846fa309a4cd0ae6b22331311fc540b90776511332b12aac'},
 'hub': {'url': 'https://www.unizg.hr/studiji-i-studiranje/upisi-stipendije-priznavanja/stipendije/',
         'format': 'html',
         'sha256': 'e0bb42a84489595c9b947ce6c5fcdd28a7e39f9d85efe404161b338b320093ea'},
 'lifelong-programmes': {'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/',
                         'format': 'html',
                         'sha256': 'e3f27c20c6b9d7492157159dd465519b34ec9a40178c029750b5691e91448de9'},
 'lifelong': {'url': 'https://www.unizg.hr/studiji-i-studiranje/cjelozivotno-obrazovanje-i-usavrsavanje/',
              'format': 'html',
              'sha256': '32396a18ea652989f89977296dbddcbb02d338f99739df849af2fb297c8a4db0'},
 'material-1': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Erasmus_short/31012027/Natjecaj_kratka_PhD_31_01_2027.pdf',
                'format': 'pdf',
                'sha256': '2208bbb78c9d03995118d5ee90752004739535dd9dcf697b060ea0b66db1d4f1'},
 'material-10': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Erasmus_SMS/2026_27/Natjecaj_SMS_2026_27_zadnje.pdf',
                 'format': 'pdf',
                 'sha256': '8c2e6604b27b5dbf039fcbe5c6490072ee0564e66e90bd263952e36ef6223c4c'},
 'material-11': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Erasmus_SMS/2026_27/Potpora_za_putne_troskove_2026_.pdf',
                 'format': 'pdf',
                 'sha256': '507ebe29ee56a02b61f5410a2aeef579d0b0470238cb730d2ca860d6a83c7943'},
 'material-12': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/01_Natjecaj_Macquarie_University_2026_2027_zimski.pdf',
                 'format': 'pdf',
                 'sha256': '1eec667168986c6318837067394b7b31d85fcb7c10534e1a9275acae8b4d801d'},
 'material-13': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/02_Natjecaj_Macquarie_University_2026_2027_ljetni.pdf',
                 'format': 'pdf',
                 'sha256': '40f8616e61e51f6a1922d94015b6fd1a850c9844ec42a62d9a4df754e5b06bd1'},
 'material-14': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/05_Natjecaj_Universidade_Federal_do_Parana_2026_2027_zimski.pdf',
                 'format': 'pdf',
                 'sha256': 'd7781bb14cbe9126fd2b15e69a853a69548ce39b41d6e99e30c0cd39061fd40e'},
 'material-15': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/06_Natjecaj_Universidade_Federal_do_Parana_2026_2027_ljetni.pdf',
                 'format': 'pdf',
                 'sha256': 'ea81f5f7a2c4eee54f37b60ea4d619d0d3eb5203468cfb81757ebd7712c7ddcd'},
 'material-16': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/07_Natjecaj_Pontificia_Universidad_Catolica_de_Chile_2026_2027.pdf',
                 'format': 'pdf',
                 'sha256': 'e867946381e6de93828eae76f15d93bee565cf37b5c67331bd7f061854ba3949'},
 'material-17': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/08_Natjecaj_Universidad_de_Chile_2026_2027_zimski.pdf',
                 'format': 'pdf',
                 'sha256': '7cf0e3fb0eec482656a72eb2c8e93332f1eb4c77c7f930b11f8890cb37c1ea6a'},
 'material-18': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/09_Natjecaj_Universidad_de_Chile_2026_2027_ljetni.pdf',
                 'format': 'pdf',
                 'sha256': 'b0f1210ce6d701751cdbeb78629b373d839ab88bf9a871dad8936db99a973d9e'},
 'material-19': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/10_Natjecaj_Chuo_University_2026_2027_zimski.pdf',
                 'format': 'pdf',
                 'sha256': 'e484046915f518c566c5f9145f413daa31b76a2bcdb33ea1ee772d21cec81484'},
 'material-2': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Erasmus_short/31012027/PhD_Troskovi_zivota_i_puta_2025.pdf',
                'format': 'pdf',
                'sha256': 'f620ca7e414efdcbc07029316f3bc76d885edb90af0097418abeaeac70d3520b'},
 'material-20': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/11_Natjecaj_Chuo_University_2026_2027_ljetni.pdf',
                 'format': 'pdf',
                 'sha256': '9a651e4858c2c595283aa12fb6ab513f6baf5043669b9037705687bfbf342422'},
 'material-21': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/12_Natjecaj_Kanagawa_University_2026_2027_zimski.pdf',
                 'format': 'pdf',
                 'sha256': '418212b2d462dfb7f5c6be101e0b0f667a7d722d454d67c5b165bf633d4dd2e6'},
 'material-22': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/13_Natjecaj_Kanagawa_University_2026_2027_ljetni.pdf',
                 'format': 'pdf',
                 'sha256': '2a0428016af94348780b3c179424228259f688c1171d02763a7fd4f3b7c2d783'},
 'material-23': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/14_Natjecaj_Sophia_University_2026_2027_zimski.pdf',
                 'format': 'pdf',
                 'sha256': '4d3481707197bb273ab56e3012afaca7635c278509e0db6bca6d69944d09a2e7'},
 'material-24': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/15_Natjecaj_Sophia_University_2026_2027_ljetni.pdf',
                 'format': 'pdf',
                 'sha256': 'ec93d43d9387727bd747f8d2c6555a3c942ce4dc9e7569045853c90536391e70'},
 'material-25': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/16_Natjecaj_Chonnam_National_University_2026_2027_zimski.pdf',
                 'format': 'pdf',
                 'sha256': 'e11bc7d1b518ac011a9a68ffe09fc2fc8c7e7d4eaad79d3fdf1039114d35462e'},
 'material-26': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/17_Natjecaj_Chonnam_National_University_2026_2027_ljetni.pdf',
                 'format': 'pdf',
                 'sha256': 'b2fd81c30d55a8053b81c784213f807ba44ef1603d392e46ada46d332cc6befc'},
 'material-27': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/18_Natjecaj_Hanyang_University_2026_2027_zimski.pdf',
                 'format': 'pdf',
                 'sha256': 'b1f0b44985dfa5935669d2c41e3ff90d3201768dce79f50b7d0ee34164d2b969'},
 'material-28': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/19_Natjecaj_Hanyang_University_2026_2027_ljetni.pdf',
                 'format': 'pdf',
                 'sha256': 'aaa63b949c33b5c63ca31816af75abffa0818214b67acdb531e1e4329cfddce9'},
 'material-29': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/20_Natjecaj_City_University_of_Hong_Kong_2026_2027_zimski.pdf',
                 'format': 'pdf',
                 'sha256': 'a93d3e0a70cab5b2a1dcb1836a58e67c827c7c59f9b412c7967e6964d8d5d5a8'},
 'material-3': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Erasmus_SMP/SMP_2026-27/Natjecaj_SMP_E__1_krug-18052026__.pdf',
                'format': 'pdf',
                'sha256': 'c5cbf11613c16c3358627a60bb89409725dff351e1a676ec271772ae7afe676d'},
 'material-30': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/21_Natjecaj_City_University_of_Hong_Kong_2026_2027_ljetni.pdf',
                 'format': 'pdf',
                 'sha256': '2906e90d16d2f5af1783df85bb65e5f2f0a146338a13e6aa29b3139ede9ee77d'},
 'material-31': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/29_Natjecaj_NCKU_2026_2027_zimski.pdf',
                 'format': 'pdf',
                 'sha256': '5e14fd70820b2754b5b5807b667da53aae8862adcf2c2155e2273347f98210ea'},
 'material-32': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/30_Natjecaj_NCKU_2026_2027_ljetni.pdf',
                 'format': 'pdf',
                 'sha256': 'ac66e0a9388a433abac13764e1fe42a79c32c30399f89651e4dcb2eb14ce18fd'},
 'material-33': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/31_Natjecaj_NSYSU_2026_2027_zimski.pdf',
                 'format': 'pdf',
                 'sha256': 'f666c5f290735519b34ca31bc993eb149fee1479af579f9d91c3325b5a49ca0b'},
 'material-34': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/32_Natjecaj_NSYSU_2026_2027_ljetni.pdf',
                 'format': 'pdf',
                 'sha256': '7d1e8021ae37e2073e7a72a03dc33ee0003a51afc7df9b955cbae18942f8868e'},
 'material-35': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/22_Natjecaj_UDEG_2026_2027_zimski.pdf',
                 'format': 'pdf',
                 'sha256': '854d908b77d08b12de0c706b5054e3437f20ab9f121b3ad5a5e8e8957a794bbb'},
 'material-36': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/23_Natjecaj_UDEG_2026_2027_ljetni.pdf',
                 'format': 'pdf',
                 'sha256': 'df11fd8c782d3ec86995ae6da6dff50757e848b907eaf13e4291c50ab3564f96'},
 'material-37': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/25_Natjecaj_University_of_New_Mexico_2026_2027_zimski.pdf',
                 'format': 'pdf',
                 'sha256': 'bfcd0297dbaf605ee68bc6bb3e4441629cd244b1c0530b1f790f375266d273dd'},
 'material-38': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/26_Natjecaj_University_of_New_Mexico_2026_2027_ljetni.pdf',
                 'format': 'pdf',
                 'sha256': '41855350e2fe356c1e1296e74ab82675e13491f937bb9c7a56388229b180342b'},
 'material-39': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/27_Natjecaj_National_University_of_Singapore_2026_2027_zimski.pdf',
                 'format': 'pdf',
                 'sha256': '85e0a57ea53d23cbf7902523a8443b53f7d79a1adf31966b0ab7184e12113a3a'},
 'material-4': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Erasmus_SMP/SMP_2026-27/Potpora_za_putne_troskove_SMP_2026-27.pdf',
                'format': 'pdf',
                'sha256': '14da0186b521cfee3cec49efdb314ff81285ca6925e4a850455514bd8b9d88f6'},
 'material-40': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/28_Natjecaj_National_University_of_Singapore_2026_2027_ljetni.pdf',
                 'format': 'pdf',
                 'sha256': '775bed9d59aeba76594d100cce94ab165b3d0d3ac77d6af430036095619d8f6c'},
 'material-41': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/33_Natjecaj_Universidad_Catolica_del_Uruguay_2026_2027_zimski.pdf',
                 'format': 'pdf',
                 'sha256': '5a64b296796b673b8dce9df9b3c1a58c3f8462a7af4770ce70ff640a0d0c7812'},
 'material-42': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/34_Natjecaj_Universidad_Catolica_del_Uruguay_2026_2027_ljetni.pdf',
                 'format': 'pdf',
                 'sha256': '5462f362299a7498879fee9f4befd1dce574e92afdc8d5aa5615e75b31f7448a'},
 'material-43': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/DAAD/JGU_Mainz/2_Natjecaj_SUZG_Mainz__DAAD_2026.pdf',
                 'format': 'pdf',
                 'sha256': '2514bd97fc2068742d707312d6889a39bbf905d611a5d4ea70edb31f8a4225d4'},
 'material-44': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Erasmus_SMS/ERASMUS_PARTNER/KA131-INTER-OPENING-STAFF/KA131-25-INTOPENING_STAFF/Natjecaj-Osoblje-trecezemlje-KA131-2025.pdf',
                 'format': 'pdf',
                 'sha256': '300db17b0398fa214d14d48ade006300d50f5bb3b498ea13f0510a135abd9582'},
 'material-45': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Erasmus_SMS/ERASMUS_PARTNER/KA131-INTER-OPENING-STAFF/KA131-25-INTOPENING_STAFF/UputezaPrijavu-trecezemlje-KA131-2025.pdf',
                 'format': 'pdf',
                 'sha256': 'a32b014ad974bf470d5c3b11892bffa3a019ebdc8fa04a288c1476efbfc98b30'},
 'material-46': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Erasmus_SMS/ERASMUS_PARTNER/KA131-INTER-OPENING-STAFF/KA131-25-INTOPENING_STAFF/Tablica-Partneri-3Zemlje-25_01.pdf',
                 'format': 'pdf',
                 'sha256': '1ee8d055a1d5d95d0cd105fdd522254f771a48db07560ab7b9f76e97c26b2d5d'},
 'material-47': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Erasmus__BIP/Natjecaj_BIP_7.pdf',
                 'format': 'pdf',
                 'sha256': '73be0c82f19444a7f743b20dc0365492b30174caf70344d1698d43f082049dd2'},
 'material-48': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Erasmus__BIP/2._Upute_uz_Natjecaj_sa_smjernicama_za_pripremu_kombiniranih_intenzivnih_programa__7_.pdf',
                 'format': 'pdf',
                 'sha256': '3d24604d01729465dab92bd101e10cf5e5c0a3fb74957bbf41fbde106f30de9c'},
 'material-49': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Erasmus__BIP/6._Tablica_iznosa_za_zivotne_troskove_i_tablica_iznosa_za_putne_troskove___7_.pdf',
                 'format': 'pdf',
                 'sha256': '6fa5546db9ade97fa7a608b5757a62eced7116b729fca7caeb8f76ee5c8a6295'},
 'material-5': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Erasmus_SMP/SMP_2026-27/Razdioba_dana_mobilnosti_za_natjecaj_.pdf',
                'format': 'pdf',
                'sha256': '3504d18f1393ae7f293ad78fe07768a552e5335e54968b53ef8f637ab4a19cfc'},
 'material-50': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Sveucilisnog_osoblja/Erasmus_plus/Erasmus_plus_2026-27/Natjecaj_Eplus_STT_STA_2026_2027_.pdf',
                 'format': 'pdf',
                 'sha256': 'f5784f35eca67de3196e0540bd5ffc5fbe1d2b27edb48044dffcd49c07459959'},
 'material-51': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Sveucilisnog_osoblja/Erasmus_plus/Erasmus_plus_2026-27/1_UPUTE_uz_natjecaj_2026-27.pdf',
                 'format': 'pdf',
                 'sha256': '39c9b93d4f18155d4d1626da2946d45bc6630cb99efcb21b19a92f1fc7e98552'},
 'material-52': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Sveucilisnog_osoblja/Erasmus_plus/Erasmus_plus_2026-27/5_Nacionalni_iznosi_projekt_2026.pdf',
                 'format': 'pdf',
                 'sha256': '4372973cef7a7fee50539f88f462a7ea9960014d71d1188e2c7bf2020ad46354'},
 'material-53': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Sveucilisnog_osoblja/Erasmus_plus/Erasmus_plus_2026-27/9_Prijava_na_natjecaj_FAQ.pdf',
                 'format': 'pdf',
                 'sha256': '9bb90a6860e5c554232dfebab710c2876dfde8d91e4857da27e82856534da9fe'},
 'material-54': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Erasmus_SMS/ERASMUS_PARTNER/KA171-2024/Natjecaj-KA171-2024-trecezemlje.pdf',
                 'format': 'pdf',
                 'sha256': '881a99cbf7aee7316f16abf6f08a6e4d53a8157dfc63d05baffce549efa033d3'},
 'material-55': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Erasmus_SMS/ERASMUS_PARTNER/KA171-2024/UputezaPrijavu-KA171-2024-trecezemlje.pdf',
                 'format': 'pdf',
                 'sha256': 'ad833f8dcdb1ec7629ae0e9ba4d7f216236d1be0da1b130b13ad76b2ea35acb5'},
 'material-56': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Erasmus_SMS/ERASMUS_PARTNER/KA171-2024/1Natjecaj-PopisPartnera.pdf',
                 'format': 'pdf',
                 'sha256': '2ee9580924a8ff73a0e60c7a051283c0fc165bd8affbfee63f0b1d9dda9c048b'},
 'material-57': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Sveucilisnog_osoblja/Bilaterala/2026/2_-_Drugi_krug/Natjecaj_za_akademsku_mobilnost_2026_drugi_krug_final.pdf',
                 'format': 'pdf',
                 'sha256': 'f3da55227b137cd87abc6ec4e00c7d12786fd6efe95007266a66454232cde915'},
 'material-6': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Erasmus_SMS/ERASMUS_PARTNER/KA131-INTER-OPENING-SMS/KA131-2025-SMP/2025-KA131-IntOpening-SMPNatjecaj-3zemlje.pdf',
                'format': 'pdf',
                'sha256': 'b858f9298277dddd3b9c7d077647f1daf785b1ba8f762a478f29b06a666a1989'},
 'material-7': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Erasmus_SMS/ERASMUS_PARTNER/KA131-INTER-OPENING-SMS/KA131-2025-SMP/3Zemlje-SMP.pdf',
                'format': 'pdf',
                'sha256': '5b1d640c30914de7ed4eead4c4c51f8992ccd98d75344341b7bd094f9990a976'},
 'material-8': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Erasmus_SMS/ERASMUS_PARTNER/KA131-INTER-OPENING-SMS/KA131-2025-SMS/2025-KA131-Natjecaj_SMS_IntOpening.pdf',
                'format': 'pdf',
                'sha256': 'c52f3219d9414513a7026c45bde3859feaf114b12ead0743e1e92939af4179f2'},
 'material-9': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Erasmus_SMS/ERASMUS_PARTNER/KA131-INTER-OPENING-SMS/KA131-2025-SMS/2025-KA131-IntOpening-PregledPartnera.pdf',
                'format': 'pdf',
                'sha256': '0442bb600583f6100a06e291818a8109e7f8f14b7a2c46ba4f46d89b15291f82'},
 'mobility': {'url': 'https://www.unizg.hr/suradnja/medunarodna-razmjena/natjecaji/',
              'format': 'html',
              'sha256': '132e7cf7a0049b9c00244881948e8775404c8fb065a42c14f0bc88db219cd715'},
 'own-awards': {'url': 'https://www.unizg.hr/istrazivanje/istrazivanje-i-inovacije/nagrade-za-posebna-postignuca/',
                'format': 'html',
                'sha256': 'a1c2bcdefc68b5a3358eb4e931290239a239d3f72740b5ebbae95b714d29435a'},
 'privacy': {'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/sluzbenici-za-zastitu-osobnih-podataka/',
             'format': 'html',
             'sha256': 'af6788f8bbde6972bf7034054314e934561578ea47981a6614918b4aa9513a5d'},
 'rectorate': {'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/',
               'format': 'html',
               'sha256': '2ca45d229b2df5dc34e7529978710aff2c76b2e701039a0497d5c58c9472fad3'},
 'research-closed': {'url': 'https://www.unizg.hr/istrazivanje/istrazivanje-i-inovacije/financiranje-istrazivanja/zatvoreni-natjecaji/',
                     'format': 'html',
                     'sha256': '71800e6db5019cdd2a6ce104760900f5cdf88ecd8d6aaf40e3d091ac79dc79be'},
 'research-open': {'url': 'https://www.unizg.hr/istrazivanje/istrazivanje-i-inovacije/financiranje-istrazivanja/otvoreni-natjecaji/',
                   'format': 'html',
                   'sha256': '370e85d8896d6aea826f2ad5100e1228a1c51a14c5682dc4cc47f7c8c973c2cf'},
 'rights': {'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/pravo-na-pristup-informacijama/',
            'format': 'html',
            'sha256': '2485086b57fdc463846c98a21e0019924c42169ec8e04bf5d66cd15a622b88fb'},
 'scholarship-call': {'url': 'https://www.unizg.hr/fileadmin/rektorat/Studiji_studiranje/Stipendije/2025_2026/FINAL_Natjecaj_za_dodjelu_stipendija_Sveucilsta_u_Zagrebu.pdf',
                      'format': 'pdf',
                      'sha256': '6a98e46ed0d6109be846756735e1bfc0ffb7b15764f4e45475fe56b698473f36'},
 'staff-calls-p2': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-sveucilisnih-djelatnika/natjecaji-za-djelatnike/p/1/',
                    'format': 'html',
                    'sha256': 'd393be1ce1938281b13166aea68edc6671512bf0c5a0c116a98cd8b5d2e21648'},
 'staff-calls-p3': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-sveucilisnih-djelatnika/natjecaji-za-djelatnike/p/2/',
                    'format': 'html',
                    'sha256': '37e051ff2c68d915d95a86db4622d9f945191705f1c16df1d8c4505595ab72b4'},
 'staff-calls-p4': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-sveucilisnih-djelatnika/natjecaji-za-djelatnike/p/3/',
                    'format': 'html',
                    'sha256': '1b3bcf11f7ba38dfeb1a466a653376a061d5efdc221f34f92cbf1d4f7ceba32e'},
 'staff-calls-p5': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-sveucilisnih-djelatnika/natjecaji-za-djelatnike/p/4/',
                    'format': 'html',
                    'sha256': 'e4c8c8f9f497b23d48fe0d7b61f64bf2857484ac0c35b40ab269f0c0112922cd'},
 'staff-calls-p6': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-sveucilisnih-djelatnika/natjecaji-za-djelatnike/p/5/',
                    'format': 'html',
                    'sha256': '7df001c1ca5d8507a96d131a968ad96e0c1b1c5d01371b662aebf7f892487c7e'},
 'staff-calls-p7': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-sveucilisnih-djelatnika/natjecaji-za-djelatnike/p/6/',
                    'format': 'html',
                    'sha256': 'c6d2ecdf536d885fd05932c785473fc15227ad6cf465d947aa287f66623fdf72'},
 'staff-calls': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-sveucilisnih-djelatnika/natjecaji-za-djelatnike/',
                 'format': 'html',
                 'sha256': 'd99d3cff72bfaed00ec0bffbe419fb38657e0948e08d0787724eedd325f4be67'},
 'staff-latest-1': {'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
                    'format': 'html',
                    'sha256': '177388439bf10c004b3fffbd236839f6f0c7fd598b4f74f94ee6b9c7b1fd57d3'},
 'staff-latest-2': {'url': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-kombinirane-intenzivne-programe-u-okviru-erasmus-programa-kljucne-aktivnosti-1-unutar-1/',
                    'format': 'html',
                    'sha256': '71757ab693e4e41d00fc594b07d0ef3dc408b596d0c56569d0c4521c2a16ff34'},
 'staff-latest-3': {'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-natjecaj-za-mobilnost-nastavnog-i-nenastavnog-osoblja-u-ak-god-20262027-drzave-cl/',
                    'format': 'html',
                    'sha256': '9cb09df18644db786ced0415704d4c596c2d4602a67b46e8f5ed49ec8af11145'},
 'staff-latest-4': {'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka171-mobilnost-osoblja-za-trece-zemlje-koje-nisu-pridruzene-programu-razdoblje-mobiln/',
                    'format': 'html',
                    'sha256': 'd78d508357be2c825f37d2a59f0ff2c8137bbd72e24880bf2ca647b8dd2f4e23'},
 'staff-latest-5': {'url': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-akademsku-mobilnost-u-2026-godini-drugi-krug/',
                    'format': 'html',
                    'sha256': 'fdb180579b35fac4ad67b60cd779f06d258b228ea7ea86662bd237579d945d26'},
 'staff-latest-6': {'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-4-krug-medunarodno-otvaranje-mobilnost-za-trece-zemlje-koje-nisu-pridruzene-pr/',
                    'format': 'html',
                    'sha256': '6b651e04f213713ed8ec4ddec8666ab9dca9bc200564541adf6dd9c79b8cbd5b'},
 'staff-latest-7': {'url': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-stipendiju-u-svrhu-kratkog-istrazivackog-boravka-na-sveucilistu-johannes-gutenberg-2/',
                    'format': 'html',
                    'sha256': 'daed01f774831e5ca5e902efb377fc25a4a8ae5158dd65330f5799e9d8a83336'},
 'staff-other-p2': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-sveucilisnih-djelatnika/ostale-mogucnosti-stipendiranja/p/1/',
                    'format': 'html',
                    'sha256': '7b1af5c1d143b4c1f8d098e412f13edc84cbb35fc4b95edc091147c2abf48315'},
 'staff-other-p3': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-sveucilisnih-djelatnika/ostale-mogucnosti-stipendiranja/p/2/',
                    'format': 'html',
                    'sha256': 'a725bcbfb02736c3efe7e864ef70bb7ad1b228996dacff9e717ead2b4e6adba9'},
 'staff-other-p4': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-sveucilisnih-djelatnika/ostale-mogucnosti-stipendiranja/p/3/',
                    'format': 'html',
                    'sha256': 'c3019362d67ce92deb3e0107d618163a56159a1a03dba647ce9dbb31e8e8186f'},
 'staff-other-p5': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-sveucilisnih-djelatnika/ostale-mogucnosti-stipendiranja/p/4/',
                    'format': 'html',
                    'sha256': '589691efa214a45062f177166b0771d1b4d7fae8e3404ffbec9d0fd1c9cc3b26'},
 'staff-other': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-sveucilisnih-djelatnika/ostale-mogucnosti-stipendiranja/',
                 'format': 'html',
                 'sha256': '6e70ab6bc0bad32f0327210962e30614dc5de3925e84bdd0b76ff027f53e1e27'},
 'student-calls-p2': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/natjecaji-za-studente/p/1/',
                      'format': 'html',
                      'sha256': '3e4eb5c6c71cb9281eb0bfe15f6f3d229b78c65bc19e40a73faa59c2c07dc12f'},
 'student-calls-p3': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/natjecaji-za-studente/p/2/',
                      'format': 'html',
                      'sha256': 'aae5abfc832cd23528ad63c988d1fb53657e541bd73cd2d1a28609079b0425ab'},
 'student-calls-p4': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/natjecaji-za-studente/p/3/',
                      'format': 'html',
                      'sha256': '721a12f4de6464badf29786c16643f2e1871b08248519ea572e3c763e65eb3c0'},
 'student-calls-p5': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/natjecaji-za-studente/p/4/',
                      'format': 'html',
                      'sha256': 'e0c7bbbe9a458ac90f03d35dd69eb29a08e4f9ac057ce584eba7492c92a9cf95'},
 'student-calls-p6': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/natjecaji-za-studente/p/5/',
                      'format': 'html',
                      'sha256': 'd40803be8cc35643e4b7b81a30f4169587fda4f63aebb28bc5d2187aa2295fd7'},
 'student-calls-p7': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/natjecaji-za-studente/p/6/',
                      'format': 'html',
                      'sha256': '721b4803f1993289ce9e9895b932e06a0c5e6c51b85e8295a4cdb8705b0e2204'},
 'student-calls-p8': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/natjecaji-za-studente/p/7/',
                      'format': 'html',
                      'sha256': '9920c2a1d00f4140dc3a5138832b2117dcab73c8626b076e0b6a3503469bb272'},
 'student-calls': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/natjecaji-za-studente/',
                   'format': 'html',
                   'sha256': '269921d9f866fafc5bfb8aa7b3b8909b3023038211fc04f99e1c5030d307226a'},
 'student-latest-1': {'url': 'https://www.unizg.hr/nc/vijest/article/novo-erasmus-natjecaj-za-kratku-mobilnost-doktorskih-studenata-za-razdoblje-16112026-3006202/',
                      'format': 'html',
                      'sha256': '930b4038e3b937a94a00d073fd39b857f70613fa6bfe6f07f1c160eba59d309a'},
 'student-latest-2': {'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-natjecaj-za-kratku-mobilnost-doktorskih-studenata-za-razdoblje-15062026-30062027/',
                      'format': 'html',
                      'sha256': 'db3de4ff59ff7ee4b70ca6d5cc603321e0755ca2a1ae264915e546bd9aee367d'},
 'student-latest-3': {'url': 'https://www.unizg.hr/nc/vijest/article/1-krug-natjecaja-za-mobilnost-studenata-ka131-erasmus-strucna-praksa-ak-god-202627/',
                      'format': 'html',
                      'sha256': '20b0e14e06129ab2c609dc0f52702e205c276a7e285cb4d6c05f77dbe29ac055'},
 'student-latest-4': {'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-strucnu-praksu-medunarodno-otvaranje-trece-z-1/',
                      'format': 'html',
                      'sha256': '69d674b059917b84a1617a42e35c90b215f8094fe8b2f6c4b445c8bf504f141a'},
 'student-latest-5': {'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/',
                      'format': 'html',
                      'sha256': 'b48db0f9b28cdf38a2f565a7eaef65a1f4a98218337a965f356615ce22ad56b3'},
 'student-latest-6': {'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-natjecaj-za-mobilnost-studenata-za-studijski-boravak-drzave-clanice-eu-a-i-trece-zem-1/',
                      'format': 'html',
                      'sha256': '8b7952796e7f1297e31992d278068a777d99acfd7b4106cd265a86187690aaf4'},
 'student-latest-7': {'url': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-stipendiju-za-studentsku-razmjenu-u-ak-god-20262027-bilateralna-medusveucilis/',
                      'format': 'html',
                      'sha256': '73ea400542490be7bd2d92c0baed54a4cf02b24df0fc8731a9f1ea83b3204e4f'},
 'student-latest-8': {'url': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-stipendiju-u-svrhu-kratkog-istrazivackog-boravka-na-sveucilistu-johannes-gutenberg-3/',
                      'format': 'html',
                      'sha256': '31e8b95dddb681b3b8e32832032d3bd50d20ab70785dc9ba6dbb1dc05c408051'},
 'student-other-p10': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/ostale-mogucnosti-stipendiranja/p/9/',
                       'format': 'html',
                       'sha256': '1393db44c93eb228930aaf44f27c6fb4d5ce6ed1e674672fa1cbbecdae188840'},
 'student-other-p11': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/ostale-mogucnosti-stipendiranja/p/10/',
                       'format': 'html',
                       'sha256': 'c61cc35766499db1e79361aec1a0d9b8f3afc38b4efcb51956d829c7c7e81175'},
 'student-other-p12': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/ostale-mogucnosti-stipendiranja/p/11/',
                       'format': 'html',
                       'sha256': '357272559b011ab7ca483c2ba95e00d882cc7c3f9ee2cfdb14e3ee0c83f3cc97'},
 'student-other-p13': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/ostale-mogucnosti-stipendiranja/p/12/',
                       'format': 'html',
                       'sha256': 'b1250479e8937f067c44f8da2c5da2b7d4da4fc4f891bd8115d7952940b80f9c'},
 'student-other-p2': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/ostale-mogucnosti-stipendiranja/p/1/',
                      'format': 'html',
                      'sha256': '80696e352fc04395c4e9484dd566c99c3d0a6bae1796bfc62bce19a9081f379e'},
 'student-other-p3': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/ostale-mogucnosti-stipendiranja/p/2/',
                      'format': 'html',
                      'sha256': 'e948bee46400934310c71570b84c12fe7d0778c6a8cbaf9d2ed21a8a8453cc7b'},
 'student-other-p4': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/ostale-mogucnosti-stipendiranja/p/3/',
                      'format': 'html',
                      'sha256': '2349da265bdf12030cdc37397bf5d59b662436f3310f18cdeab1c13f89ee28fb'},
 'student-other-p5': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/ostale-mogucnosti-stipendiranja/p/4/',
                      'format': 'html',
                      'sha256': '23797754f2f760d3bfe611e86170cb838bfe8c5d3a3902ac98a875796f8d664e'},
 'student-other-p6': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/ostale-mogucnosti-stipendiranja/p/5/',
                      'format': 'html',
                      'sha256': '7e729fe7e95be41940028aa0256a6721fb7f783389018bea806b5ad56c4c23fb'},
 'student-other-p7': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/ostale-mogucnosti-stipendiranja/p/6/',
                      'format': 'html',
                      'sha256': '3ef4de12619ca5537255caca82dad1a7994fe5e72bceb6e0811b5f01b0b2f17b'},
 'student-other-p8': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/ostale-mogucnosti-stipendiranja/p/7/',
                      'format': 'html',
                      'sha256': '2ddabe2063f1b8823aeb8705ee51202f1b6c0a3ddb15a4d844af3e9cb4297f6c'},
 'student-other-p9': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/ostale-mogucnosti-stipendiranja/p/8/',
                      'format': 'html',
                      'sha256': 'fa0d1e7a15de733e5ad63cb6aa779f4985bcc070a278f341f2919128ee1b2339'},
 'student-other': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/ostale-mogucnosti-stipendiranja/',
                   'format': 'html',
                   'sha256': '00d2dc0d2fa67327ad66e5bd86e99a13548b24a3d4099ba440694726b3ccc50d'},
 'study-own-framework-0': {'url': 'https://www.unizg.hr/suradnja/medunarodna-razmjena/razmjena-studenata/studijski-boravak/erasmus/',
                           'format': 'html',
                           'sha256': '9d6ccfff206e7867e28ac2702d6736b6ee5c76a361e879540843ac9a2a863679'},
 'study-own-framework-1': {'url': 'https://www.unizg.hr/suradnja/medunarodna-razmjena/razmjena-studenata/studijski-boravak/bilateralna-razmjena/',
                           'format': 'html',
                           'sha256': 'd0f02dc7301f0fce73a4ba0e4b7c335d795b3c63d8333766d1a43516eefe2d7b'},
 'study': {'url': 'https://www.unizg.hr/suradnja/medunarodna-razmjena/razmjena-studenata/studijski-boravak/',
           'format': 'html',
           'sha256': 'eb6dc4e06977dd8fd3683322bdc1ec26cf0af0e5e98584aed0f1568112015041'},
 'summer-schools-p10': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/medunarodne-ljetne-skole/p/9/',
                        'format': 'html',
                        'sha256': 'b78a0302762f0368c27cf67f569cc74077c24c18eaab3164b16c4548a24409fc'},
 'summer-schools-p11': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/medunarodne-ljetne-skole/p/10/',
                        'format': 'html',
                        'sha256': 'e08603049b7768e7b2b676c30bb7baaab55b86ea79f5564b20526320ad46899c'},
 'summer-schools-p12': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/medunarodne-ljetne-skole/p/11/',
                        'format': 'html',
                        'sha256': '63bdea15a04bf23248d8dd4a2d4d868be1915d7d1ef4f64c064abde98fc3b229'},
 'summer-schools-p13': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/medunarodne-ljetne-skole/p/12/',
                        'format': 'html',
                        'sha256': '3e03f34f779b3078f66838ef6b30e099f75750bbb3c9ae399e9e5eb66577bfec'},
 'summer-schools-p14': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/medunarodne-ljetne-skole/p/13/',
                        'format': 'html',
                        'sha256': 'e1a6887d6b4fd2235ae7938cb2566640a74df4e8e34878d312eb05cebe66f681'},
 'summer-schools-p15': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/medunarodne-ljetne-skole/p/14/',
                        'format': 'html',
                        'sha256': 'a254429c8c4e7561c59cfa24e5c9507f0c6ca6e1389a54b24e3f7b8cc9b189e1'},
 'summer-schools-p16': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/medunarodne-ljetne-skole/p/15/',
                        'format': 'html',
                        'sha256': 'dfc9d2e2b17ea3c566fff989299cd4b7e1307b303a074c838b8152007d39b005'},
 'summer-schools-p17': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/medunarodne-ljetne-skole/p/16/',
                        'format': 'html',
                        'sha256': '05c6204ff37fa857d6e56e2c825353e81a8fd014badf542ca2c741c16555dc7b'},
 'summer-schools-p18': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/medunarodne-ljetne-skole/p/17/',
                        'format': 'html',
                        'sha256': '27484d4dfc586066827c8fbf0277ccbfe3c6c328c3f43f56966080b6ad76da9c'},
 'summer-schools-p19': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/medunarodne-ljetne-skole/p/18/',
                        'format': 'html',
                        'sha256': '952d910ce439fecffb85fa0d4da5d607f82292a2689358c6c53ca0476524268e'},
 'summer-schools-p2': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/medunarodne-ljetne-skole/p/1/',
                       'format': 'html',
                       'sha256': 'bf73e5296962e29b25a59cfb485d5f0113631cf40008548d0f60d647b2f662f1'},
 'summer-schools-p20': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/medunarodne-ljetne-skole/p/19/',
                        'format': 'html',
                        'sha256': '4cd4c3561ad417fafd62361eb4c710453066fdbd70e919db1903e9a6114c564c'},
 'summer-schools-p3': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/medunarodne-ljetne-skole/p/2/',
                       'format': 'html',
                       'sha256': 'cff4ac89489ff0697eb242c0bf39acb223b8ccbf2aabb13f4a2e7659537b83b2'},
 'summer-schools-p4': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/medunarodne-ljetne-skole/p/3/',
                       'format': 'html',
                       'sha256': 'd6fee8868b9ec0734a8e319ef5a78937ef71dd3904111c7b72b24757c63de5d0'},
 'summer-schools-p5': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/medunarodne-ljetne-skole/p/4/',
                       'format': 'html',
                       'sha256': '2740993aa9256751a5021da8d212fa8c49918cf21fa834600f95b5709a2c076d'},
 'summer-schools-p6': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/medunarodne-ljetne-skole/p/5/',
                       'format': 'html',
                       'sha256': 'ed20456f744327f06b713d86ea9d45b717948eedab90bc301866beff1d5fb158'},
 'summer-schools-p7': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/medunarodne-ljetne-skole/p/6/',
                       'format': 'html',
                       'sha256': 'd5dcd75e9037896b0b836e37ded23c456530a2a5b4ca705575bc5dea2affef62'},
 'summer-schools-p8': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/medunarodne-ljetne-skole/p/7/',
                       'format': 'html',
                       'sha256': 'e1f5a2b6818e700ecc69993589ae1a7b92c008231ee4fc098a70bf309ca3c094'},
 'summer-schools-p9': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/medunarodne-ljetne-skole/p/8/',
                       'format': 'html',
                       'sha256': 'a8868354fa726c42b9b9c9c47402ff018789c1b04cd5a9683d9a3fbeaaed9fc8'},
 'summer-schools': {'url': 'https://www.unizg.hr/nc/suradnja/medunarodna-razmjena/razmjena-studenata/medunarodne-ljetne-skole/',
                    'format': 'html',
                    'sha256': '9fd557c43070eb47917b24284204917c6ae796d3f9ba58c22aa772bb96c39240'},
 'traineeship': {'url': 'https://www.unizg.hr/suradnja/medunarodna-razmjena/razmjena-studenata/strucna-praksa/',
                 'format': 'html',
                 'sha256': '5d7e209aef2271cd98df4ad63fef258e062560c2b48ed2d27926dc59501030c1'}}

PROFILES = [{'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/farmaceutsko-biokemijski-fakultet/|GxP '
         'konferencija',
  'title': 'GxP konferencija',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/farmaceutsko-biokemijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 14 sati tijekom 2 dana (6 '
             'predavanja + 6 radionica); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 350. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/farmaceutsko-biokemijski-fakultet/|Tečaj '
         'za mentore na Stručnom osposobljavanju studenata farmacije',
  'title': 'Tečaj za mentore na Stručnom osposobljavanju studenata farmacije',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/farmaceutsko-biokemijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 5 sati nastave + 1 sat '
             'samostalnog rada; Jezik: hrvatski; Izvođenje: mješovito; Katalog EUR: '
             '70. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/|Nastavničke '
         'kompetencije za suvremenu visokoškolsku nastavu',
  'title': 'Nastavničke kompetencije za suvremenu visokoškolsku nastavu',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 365 sati nastave + 535 sati '
             'samostalnog rada; Jezik: hrvatski; Izvođenje: mješovito; Katalog EUR: '
             '3200 Eura. Published HKO  (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/|Temeljne '
         'andragoške kompetencije',
  'title': 'Temeljne andragoške kompetencije',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 90 sati (36 sati nastave + 54 '
             'samostalnog rada polaznika); Jezik: hrvatski; Izvođenje: mješovito; '
             'Katalog EUR: 550 Eura. Published HKO  (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/|Dopunska '
         'pedagoško-psihološko-didaktičko-metodička izobrazba nastavnika',
  'title': 'Dopunska pedagoško-psihološko-didaktičko-metodička izobrazba nastavnika',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: Dva semestra, 192 sata '
             'kontaktne nastave + nastavna praksa; Jezik: hrvatski; Izvođenje: '
             'mješovito; klasično (uživo); Katalog EUR: 1.140 (ZS ak. god. 24/25). '
             'Published HKO 6; 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/|Osnaživanje '
         'temeljnih nastavničkih kompetencija visokoškolskih nastavnika – OSMISLI',
  'title': 'Osnaživanje temeljnih nastavničkih kompetencija visokoškolskih nastavnika '
           '– OSMISLI',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: UKUPNO = 60 sati kontakt '
             'nastave + 10 sati mentoriranoga rada + samostalni rad + e-učenje, '
             'Opterećenje polaznika = oko 120 sati; Jezik: hrvatski; Izvođenje: '
             'klasično (uživo); mješovito; Katalog EUR: Za otvorene grupe: 610,52 za '
             'polaznika. Za sastavnice (in-house ponuda): na upit - ovisno o broju '
             'polaznika. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/|Predavanja '
         'usmjerena na osnaživanje temeljnih znanja iz pripreme nastave u visokom '
         'školstvu',
  'title': 'Predavanja usmjerena na osnaživanje temeljnih znanja iz pripreme nastave u '
           'visokom školstvu',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: Po dogovoru, 90 min po '
             'predavanju; Jezik: hrvatski; Izvođenje: klasično (uživo); Katalog EUR: '
             'Na upit. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/|Radioničke '
         'edukacije iz temeljnih nastavničkih kompetencija u visokom školstvu',
  'title': 'Radioničke edukacije iz temeljnih nastavničkih kompetencija u visokom '
           'školstvu',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: Okvirno se radi o '
             'jednodnevnim edukacijama, no moguće ih je prilagoditi potrebama '
             'naručitelja.; Jezik: hrvatski; Izvođenje: klasično (uživo); Katalog EUR: '
             'Na upit. Ovisno o potrebama naručitelja (broj polaznika, trajanje itd.). '
             'Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/|Program '
         'učenja engleskog općeg jezika (razine A1, A2, B1, B2, C1 i C2)',
  'title': 'Program učenja engleskog općeg jezika (razine A1, A2, B1, B2, C1 i C2)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 2240 sati (1120 sati nastave '
             '+ otprilike 1120 samostalni rad polaznika za postizanje razine znanja '
             'C2); Jezik: engleski; Izvođenje: klasično (uživo); mješovito; Katalog '
             'EUR: Cijena jednog modula u trajanju od 70 nastavnih sati je 340. '
             'Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/|Program '
         'učenja njemačkog općeg jezika (razine A1, A2, B1, B2, C1 i C2)',
  'title': 'Program učenja njemačkog općeg jezika (razine A1, A2, B1, B2, C1 i C2)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 1120 sati nastave da bi '
             'polaznik stekao znanje na C2 razini prema ZEROJ-u + najmanje 1120 sati '
             'samostalnog rada; Jezik: njemački; Izvođenje: klasično (uživo); Katalog '
             'EUR: 340 1 modul od 70 sati. Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/|Program '
         'učenja francuskog općeg jezika (razine A1, A2, B1, B2, C1 i C2)',
  'title': 'Program učenja francuskog općeg jezika (razine A1, A2, B1, B2, C1 i C2)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 1120 sati nastave da bi '
             'polaznik stekao znanje na C2 razini prema ZEROJ-u + najmanje 1120 sati '
             'samostalnog rada; Jezik: francuski; Izvođenje: klasično (uživo); '
             'mješovito; Katalog EUR: Cijena jednog modula u trajanju od 70 nastavnih '
             'sati je 340. Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/|Program '
         'učenja španjolskog općeg jezika (razine A1, A2, B1, B2, C1 i C2)',
  'title': 'Program učenja španjolskog općeg jezika (razine A1, A2, B1, B2, C1 i C2)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 1120 sati nastave da bi '
             'polaznik stekao znanje na C2 razini prema ZEROJ-u + najmanje 1120 sati '
             'samostalnog rada; Jezik: španjolski; Izvođenje: klasično (uživo); '
             'mješovito; Katalog EUR: Cijena jednog modula u trajanju od 70 nastavnih '
             'sati je 340. Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/|Program '
         'učenja talijanskog općeg jezika (razine A1, A2, B1, B2, C1 i C2)',
  'title': 'Program učenja talijanskog općeg jezika (razine A1, A2, B1, B2, C1 i C2)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 1120 sati nastave da bi '
             'polaznik stekao znanje na C2 razini prema ZEROJ-u + najmanje 1120 sati '
             'samostalnog rada; Jezik: talijanski; Izvođenje: klasično (uživo); '
             'mješovito; Katalog EUR: Cijena jednog modula u trajanju od 70 nastavnih '
             'sati je 340. Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/|Program '
         'učenja ruskog općeg jezika (razine A1, A2, B1, B2, C1 i C2)',
  'title': 'Program učenja ruskog općeg jezika (razine A1, A2, B1, B2, C1 i C2)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 1120 sati nastave da bi '
             'polaznik stekao znanje na C2 razini prema ZEROJ-u + najmanje 1120 sati '
             'samostalnog rada; Jezik: ruski; Izvođenje: klasično (uživo); mješovito; '
             'Katalog EUR: Cijena jednog modula u trajanju od 70 nastavnih sati je '
             '340. Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/|Program '
         'učenja nizozemskog općeg jezika (razine A1, A2, B1, B2, C1 i C2)',
  'title': 'Program učenja nizozemskog općeg jezika (razine A1, A2, B1, B2, C1 i C2)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 1120 sati nastave da bi '
             'polaznik stekao znanje na C2 razini prema ZEROJ-u + najmanje 1120 sati '
             'samostalnog rada; Jezik: nizozemski; Izvođenje: klasično (uživo); '
             'mješovito; Katalog EUR: Cijena jednog modula u trajanju od 70 nastavnih '
             'sati je 340. Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/|Program '
         'učenja slovenskog općeg jezika (razine A1, A2, B1, B2, C1 i C2)',
  'title': 'Program učenja slovenskog općeg jezika (razine A1, A2, B1, B2, C1 i C2)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 1120 sati nastave da bi '
             'polaznik stekao znanje na C2 razini prema ZEROJ-u + najmanje 1120 sati '
             'samostalnog rada; Jezik: slovenski; Izvođenje: klasično (uživo); '
             'mješovito; Katalog EUR: Cijena jednog modula u trajanju od 70 nastavnih '
             'sati je 340. Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/|Hrvatski '
         'kao drugi i strani jezik (tzv. semestralni tečaj)',
  'title': 'Hrvatski kao drugi i strani jezik (tzv. semestralni tečaj)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 225 sati nastave; Jezik: '
             'hrvatski; Izvođenje: klasično (uživo); Katalog EUR: 850. Published HKO '
             '5; 6; 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/|Croaticumove '
         'škole hrvatskoga jezika i kulture',
  'title': 'Croaticumove škole hrvatskoga jezika i kulture',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 60 sati nastave + 15 sati '
             'terenske nastave u kulturnim institucijama + 40 sati samostalnoga rada '
             'studenata; Jezik: hrvatski; Izvođenje: klasično (uživo); Katalog EUR: '
             '450. Published HKO 5; 6; 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/|Upravljanje '
         'znanstveno-istraživačkim projektima (Horizon Europe/Obzor Europa) za jačanje '
         'obrazovanja',
  'title': 'Upravljanje znanstveno-istraživačkim projektima (Horizon Europe/Obzor '
           'Europa) za jačanje obrazovanja',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/filozofski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 30 sati nastave + 25 sati '
             'samostalnog rada; Jezik: hrvatski; Izvođenje: mješovito; Katalog EUR: '
             '510 (EUR). Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/katolicki-bogoslovni-fakultet/|Program '
         'cjeloživotnog obrazovanja "Teološka kultura"',
  'title': 'Program cjeloživotnog obrazovanja "Teološka kultura"',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/katolicki-bogoslovni-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 720 sati (240 sati nastave '
             '(120 prva godina + 120 druga godina) + 480 sati samostalnog rada); '
             'Jezik: hrvatski; Izvođenje: klasično (uživo); Katalog EUR: 670 po '
             'godini, ukupno 1.340. Published HKO 6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/katolicki-bogoslovni-fakultet/|Program '
         'cjeloživotnog obrazovanja "Formacija odgojitelja svećeničkih i redovničkih '
         'kandidata"',
  'title': 'Program cjeloživotnog obrazovanja "Formacija odgojitelja svećeničkih i '
           'redovničkih kandidata"',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/katolicki-bogoslovni-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 500 sati (128 sati nastave + '
             '372 sata samostalnog rada); Jezik: hrvatski; Izvođenje: klasično '
             '(uživo); Katalog EUR: 670. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/katolicki-bogoslovni-fakultet/|Program '
         'cjeloživotnog obrazovanja "Teološko-katehetsko doškolovanje za vjerski odgoj '
         'djece predškolske dobi"',
  'title': 'Program cjeloživotnog obrazovanja "Teološko-katehetsko doškolovanje za '
           'vjerski odgoj djece predškolske dobi"',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/katolicki-bogoslovni-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 330 sati (110 sati nastave + '
             '220 sati samostalnog rada); Jezik: hrvatski; Izvođenje: klasično '
             '(uživo); Katalog EUR: 670. Published HKO 6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/katolicki-bogoslovni-fakultet/|Program '
         'za stjecanje nastavničkih kompetencija. '
         'Pedagoško-psihološko-didaktičko-metodička izobrazba',
  'title': 'Program za stjecanje nastavničkih kompetencija. '
           'Pedagoško-psihološko-didaktičko-metodička izobrazba',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/katolicki-bogoslovni-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 480 sati nastave + 850 '
             'samostalni rad polaznika; Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 670. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/katolicki-bogoslovni-fakultet/|Liturgijska '
         'glazbena kultura',
  'title': 'Liturgijska glazbena kultura',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/katolicki-bogoslovni-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 240 sati (120 + 120) nastave '
             '+ 600 sati individualnog rada; Jezik: hrvatski; Izvođenje: klasično '
             '(uživo); Katalog EUR: 800. Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/kinezioloski-fakultet/|Program '
         'obrazovanja za stjecanje mikrokvalifikacije Trener atletike 1. razine',
  'title': 'Program obrazovanja za stjecanje mikrokvalifikacije Trener atletike 1. '
           'razine',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/kinezioloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 200 sati (170 sati nastave + '
             '30 sati samostalnog rada); Jezik: hrvatski; Izvođenje: mješovito; '
             'Katalog EUR: 1.200. Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/kinezioloski-fakultet/|Program '
         'obrazovanja za stjecanje mikrokvalifikacija Trener tenisa 1. razine',
  'title': 'Program obrazovanja za stjecanje mikrokvalifikacija Trener tenisa 1. '
           'razine',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/kinezioloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 200 (170 sati nastave + 30 '
             'sati samostalnog rada); Jezik: hrvatski; Izvođenje: mješovito; Katalog '
             'EUR: 1.200. Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/kinezioloski-fakultet/|Program '
         'obrazovanja za stjecanje mikrokvalifikacije Trener košarke 1. razine',
  'title': 'Program obrazovanja za stjecanje mikrokvalifikacije Trener košarke 1. '
           'razine',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/kinezioloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 200 sati (170 sati nastave + '
             '30 sati samostalnog rada); Jezik: hrvatski; Izvođenje: mješovito; '
             'Katalog EUR: 1.200. Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/kinezioloski-fakultet/|Program '
         'obrazovanja za stjecanje mikrokvalifikacija Trener odbojke 1. razine',
  'title': 'Program obrazovanja za stjecanje mikrokvalifikacija Trener odbojke 1. '
           'razine',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/kinezioloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 200 sati (170 sati + 30 sati '
             'samostalnog rada); Jezik: hrvatski; Izvođenje: mješovito; Katalog EUR: '
             '1.200. Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/kinezioloski-fakultet/|Program '
         'obrazovanja za stjecanje mikrokvalifikacije Trener hrvanja 1. razine',
  'title': 'Program obrazovanja za stjecanje mikrokvalifikacije Trener hrvanja 1. '
           'razine',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/kinezioloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 200 sati (170 sati + 30 sati '
             'samostalnog rada); Jezik: hrvatski; Izvođenje: mješovito; Katalog EUR: '
             '1.200. Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/kinezioloski-fakultet/|Program '
         'obrazovanja za stjecanje mikrokvalifikacije Trener ritmičke gimnastike 1. '
         'razine',
  'title': 'Program obrazovanja za stjecanje mikrokvalifikacije Trener ritmičke '
           'gimnastike 1. razine',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/kinezioloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 200 sati (170 sati nastave + '
             '30 sati samostalnog rada); Jezik: hrvatski; Izvođenje: Mješovito; '
             'Katalog EUR: 1.200. Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/pravni-fakultet/|Praktična '
         'primjena Opće uredbe o zaštiti podataka',
  'title': 'Praktična primjena Opće uredbe o zaštiti podataka',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/pravni-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 20 sati nastave; Jezik: '
             'hrvatski; Izvođenje: klasično (uživo); na daljinu; Katalog EUR: 450. '
             'Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/pravni-fakultet/|Aktualnosti '
         'u sustavu prostornog uređenja i gradnje',
  'title': 'Aktualnosti u sustavu prostornog uređenja i gradnje',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/pravni-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 16 sati; Jezik: hrvatski; '
             'Izvođenje: mješovito; Katalog EUR: 400. Published HKO 7 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/pravni-fakultet/|Ljetna '
         'škola "Socijalni rad i razvoj zajednice"',
  'title': 'Ljetna škola "Socijalni rad i razvoj zajednice"',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/pravni-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 60 sati (20 sati nastave + 20 '
             'sati vježbe + 20 sati samostalni rad); Jezik: hrvatski; Izvođenje: '
             'mješovito; Katalog EUR: Od 50 - 100 Eur ovisno o trajanju i formatu. '
             'Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/pravni-fakultet/|Zagovaranje '
         'prava nacionalnih manjina u Republici Hrvatskoj',
  'title': 'Zagovaranje prava nacionalnih manjina u Republici Hrvatskoj',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/pravni-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 48 sati; Jezik: hrvatski; '
             'Izvođenje: mješovito; Katalog EUR: 500. Published HKO 5 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/pravni-fakultet/|Upravljanje '
         'i digitalne vještine u socijalnim poduzećima za radnu integraciju',
  'title': 'Upravljanje i digitalne vještine u socijalnim poduzećima za radnu '
           'integraciju',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/pravni-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 84,5 sati (51 sat vođenog '
             'procesa učenja, nastave, 33,5 sata samostalnog rada polaznika); Jezik: '
             'hrvatski; Izvođenje: mješovito; Katalog EUR: 550. Published HKO 5 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/pravni-fakultet/|Antikorupcijske '
         'radionice: Program usavršavanja u području suzbijanja korupcije',
  'title': 'Antikorupcijske radionice: Program usavršavanja u području suzbijanja '
           'korupcije',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/pravni-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 13 radionica po 5 nastavnih '
             'sati; Jezik: hrvatski; Izvođenje: klasično (uživo); mješovito; Katalog '
             'EUR: 80 Eur + PDV po radionici / 135 eur + PDV za modul s 2 radionice /. '
             'Published HKO 6;7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/sveucilisni-centar-protestantske-teologije-matija-vlacic-ilirik/|Razlikovni '
         'program za upis na diplomski studij Protestantska teologija',
  'title': 'Razlikovni program za upis na diplomski studij Protestantska teologija',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/sveucilisni-centar-protestantske-teologije-matija-vlacic-ilirik/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: Određuje se individualno, '
             'prema dokumentaciji o prethodno završenom studijskom programu '
             'polaznika.; Jezik: hrvatski; Izvođenje: klasično (uživo); Katalog EUR: '
             'Cijena razlikovnog programa određuje se broju ECTS bodova koje polaznik '
             'upisuje, a cijena ECTS boda iznosi 15,93 EUR.. Published HKO 6 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/|Program '
         'za prekvalifikaciju ili dokvalifikaciju učitelja u svrhu stjecanja '
         'kvalifikacije odgojitelja',
  'title': 'Program za prekvalifikaciju ili dokvalifikaciju učitelja u svrhu stjecanja '
           'kvalifikacije odgojitelja',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Listed fee to be determined by the '
             'faculty funding decision. Jezik: hrvatski; izvođenje: klasično (uživo). '
             'Published HKO 65 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/|Kreativni '
         'program glazba-jezik-drama',
  'title': 'Kreativni program glazba-jezik-drama',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Two equivalent catalogue entries list '
             'conflicting EUR200 and199.08; current price unknown.20h; Croatian, in '
             'person; published HKO5 is not an admission qualification. Published HKO '
             '5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/|Pedagoško-psihološko '
         'obrazovanje za nastavnike',
  'title': 'Pedagoško-psihološko obrazovanje za nastavnike',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 205 sati nastave + 1595 sati '
             'samostalnog rada; Jezik: hrvatski; Izvođenje: klasično (uživo); Katalog '
             'EUR: 955, 60 (uz cijenu programa plaćaju se i troškovi upisa 27 eura te '
             'indeks i upisni materijal 20 eura). Published HKO 7 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/|Program '
         'stjecanja pedagoških kompetencija za strukovne učitelje, suradnike u nastavi '
         'i mentore',
  'title': 'Program stjecanja pedagoških kompetencija za strukovne učitelje, suradnike '
           'u nastavi i mentore',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 205 sati nastave + 1595 sati '
             'samostalnog rada; Jezik: hrvatski; Izvođenje: klasično (uživo); Katalog '
             'EUR: 955,60 (uz to se plaćaju troškovi upisa 27 eura i upisni materijal '
             '20 eura). Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/|Poseban '
         'program kinezioloških aktivnosti',
  'title': 'Poseban program kinezioloških aktivnosti',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 50 sati (10 sati predavanja + '
             '40 sati vježbi); Jezik: hrvatski; Izvođenje: klasično (uživo); Katalog '
             'EUR: 398,17. Published HKO 6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/|Refleksivna '
         'praksa u odgoju i obrazovanju',
  'title': 'Refleksivna praksa u odgoju i obrazovanju',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 6 sati nastave; Jezik: '
             'hrvatski; Izvođenje: klasično (uživo); Katalog EUR: 53,04. Published HKO '
             '6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/|Filozofija, '
         'feminizam i odgojiteljska profesija: modeli izgradnje pluralnog društva',
  'title': 'Filozofija, feminizam i odgojiteljska profesija: modeli izgradnje '
           'pluralnog društva',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 12 sati nastave; Jezik: '
             'hrvatski; Izvođenje: klasično (uživo); Katalog EUR: 199,08. Published '
             'HKO 6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/|Poticanje '
         'pripovjednih sposobnosti djece',
  'title': 'Poticanje pripovjednih sposobnosti djece',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: Npr. 60 sati (20 sati nastave '
             '+ 40 sati samostalnog rada); Jezik: hrvatski; Izvođenje: klasično '
             '(uživo); Katalog EUR: 132. Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/|Sinkretički '
         'i multimodalni pristup glazbenom odgoju i obrazovanju djece rane i '
         'predškolske dobi',
  'title': 'Sinkretički i multimodalni pristup glazbenom odgoju i obrazovanju djece '
           'rane i predškolske dobi',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 50 sati (40 sati nastave i 10 '
             'sati samostalnog rada); Jezik: hrvatski; Izvođenje: mješovito; Katalog '
             'EUR: 465. Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/|Osnove '
         'kazališne pismenosti (Kako gledati predstavu za djecu?)',
  'title': 'Osnove kazališne pismenosti (Kako gledati predstavu za djecu?)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 9 sati; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 86. Published HKO 5 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/|Europski '
         'kurikulum za razvoj otpornosti djece predškolske i školske dobi - RESCUR',
  'title': 'Europski kurikulum za razvoj otpornosti djece predškolske i školske dobi - '
           'RESCUR',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 18 sati nastave; Jezik: '
             'hrvatski; Izvođenje: klasično (uživo); Katalog EUR: 199,08. Published '
             'HKO 6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/|I '
         'nkluzivni pristup i podrška djeci s teškoćama u razvoju u ranom i '
         'predškolskom odgoju i obrazovanju',
  'title': 'I nkluzivni pristup i podrška djeci s teškoćama u razvoju u ranom i '
           'predškolskom odgoju i obrazovanju',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/uciteljski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 25 sati (20 sati nastave + 5 '
             'sati samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 238,90. Published HKO 6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/prirodoslovno-matematicki-fakultet/|LabAnim '
         '- Tečaj za osposobljavanje osoba koje rade s pokusnim životinjama i '
         'životinjama za proizvodnju bioloških pripravaka',
  'title': 'LabAnim - Tečaj za osposobljavanje osoba koje rade s pokusnim životinjama '
           'i životinjama za proizvodnju bioloških pripravaka',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/prirodoslovno-matematicki-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Listed fees: technicians EUR491.07; '
             'investigators EUR637.07; already-certified technicians investigator '
             'track EUR318.53. Jezik: hrvatski; izvođenje: klasično (uživo). Published '
             'HKO 6; 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/|Postkvantna '
         'kriptografija: razumijevanje prijetnji i rješenja',
  'title': 'Postkvantna kriptografija: razumijevanje prijetnji i rješenja',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 6 sati; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 350,00 EUR + PDV. Published '
             'HKO 6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/|Sposobnosti, '
         'prednosti i mane velikih jezičnih modela',
  'title': 'Sposobnosti, prednosti i mane velikih jezičnih modela',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 16 sati; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo) ;na daljinu; mješovito;; Katalog EUR: '
             '1.000,00 EUR + PDV. Published HKO 6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/|Od '
         'modela do algoritama: matematičke osnove za informacijske tehnologije',
  'title': 'Od modela do algoritama: matematičke osnove za informacijske tehnologije',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 30 sati; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 900 EUR+PDV. Published HKO 6 '
             '(not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/|AI '
         'Bootcamp: Rukovoditeljsko izdanje / AI Bootcamp: Executive Edition',
  'title': 'AI Bootcamp: Rukovoditeljsko izdanje / AI Bootcamp: Executive Edition',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 20 sati (16h nastave+4 sata '
             'praktičnog rada); Jezik: Hrvatski i engleski; Izvođenje: klasično '
             '(uživo); Katalog EUR: 1.200+ PDV. Published HKO 6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/|Kako '
         'razviti i monetizirati baterijski sustav',
  'title': 'Kako razviti i monetizirati baterijski sustav',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 6; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 400 EUR po polazniku. '
             'Published HKO 6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/|AI '
         'Bootcamp - Osnove umjetne inteligencije (eng. AI Bootcamp - Foundations of '
         'AI)',
  'title': 'AI Bootcamp - Osnove umjetne inteligencije (eng. AI Bootcamp - Foundations '
           'of AI)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 40 sati (20 sati nastave + 20 '
             'sati zajedničkog praktičnog rada); Jezik: hrvatski, engleski; Izvođenje: '
             'klasično; Katalog EUR: 1.200 EUR + PDV. Published HKO 7 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/|Osposobljavanje '
         'za rad daljinski upravljanim ronilicama (ROV)',
  'title': 'Osposobljavanje za rad daljinski upravljanim ronilicama (ROV)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 24 sata (18 sati nastave + 6 '
             'sati samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 520 EUR po polazniku. Published HKO 6 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/|AIoTwin',
  'title': 'AIoTwin',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 38 sati (18 sati nastave + 20 '
             'samostalnog rada polaznika); Jezik: engleski; Izvođenje: klasično '
             '(uživo); Katalog EUR: Cijena sudjelovanja je pokrivena iz budžeta '
             'projekta. Polaznici sami pokrivaju vlastiti trošak putovanja i '
             'smještaja.. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/|Technical '
         'Leadership Program (TLP)',
  'title': 'Technical Leadership Program (TLP)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 240 sati (114 sati nastave + '
             '96 sati samostalnog rada); Jezik: engleski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 5.000 EUR + PDV. Published HKO 6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/|Upravljanje '
         'rizikom',
  'title': 'Upravljanje rizikom',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 45 sati (20 sati nastave + 25 '
             'samostalnog rada polaznika); Jezik: hrvatski; Izvođenje: mješovito; '
             'Katalog EUR: 700,00 € + PDV. Published HKO nije primjenjivo (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/|Duboko '
         'podržano učenje',
  'title': 'Duboko podržano učenje',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 40 sati (40 sati nastave); '
             'Jezik: hrvatski; Izvođenje: klasično (uživo); mješovito; Katalog EUR: '
             '1.000,00 € + PDV. Published HKO nije primjenjivo (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/|Umjetna '
         'inteligencija – kako računala „misle“?',
  'title': 'Umjetna inteligencija – kako računala „misle“?',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 18 sati (18 sati nastave); '
             'Jezik: hrvatski; Izvođenje: klasično (uživo); mješovito; Katalog EUR: '
             '500 € + PDV. Published HKO nije primjenjivo (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/|Analitika '
         'u financijama',
  'title': 'Analitika u financijama',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 24 sata (24 sata nastave); '
             'Jezik: hrvatski; Izvođenje: klasično (uživo); mješovito; Katalog EUR: '
             '800 € + PDV. Published HKO nije primjenjivo (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/|Statističko '
         'zaključivanje i analiza podataka',
  'title': 'Statističko zaključivanje i analiza podataka',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 32 sata (32 sata nastave); '
             'Jezik: hrvatski; Izvođenje: klasično (uživo); mješovito; Katalog EUR: '
             '900 € + PDV. Published HKO nije primjenjivo (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/|AI '
         'Bootcamp: Matematička optimizacija (eng. AI Bootcamp: Mathematical '
         'Optimization)',
  'title': 'AI Bootcamp: Matematička optimizacija (eng. AI Bootcamp: Mathematical '
           'Optimization)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 40 sati (16 sati nastave + 24 '
             'samostalnog rada polaznika); Jezik: hrvatski; Izvođenje: klasično '
             '(uživo); Katalog EUR: 1.200 EUR + PDV. Published HKO nije primjenjivo '
             '(not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/|AI '
         'Bootcamp: Predviđanje vremenskih nizova (eng. AI Bootcamp: Time-Series '
         'Forecasting)',
  'title': 'AI Bootcamp: Predviđanje vremenskih nizova (eng. AI Bootcamp: Time-Series '
           'Forecasting)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 40 sati (12 sati nastave + 28 '
             'samostalnog rada polaznika); Jezik: hrvatski; Izvođenje: klasično '
             '(uživo); Katalog EUR: 1.200 EUR + PDV. Published HKO nije primjenjivo '
             '(not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/|Implementacija '
         'direktive NIS2',
  'title': 'Implementacija direktive NIS2',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-elektrotehnike-i-racunarstva/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 15 (15 sati nastave); Jezik: '
             'hrvatski; Izvođenje: klasično (uživo); Katalog EUR: Cijena na upit '
             '(Cijena se određuje ovisno o veličini grupe te prilagodbi programa prema '
             'specifičnostima poduzeća naručitelja). Published HKO nije primjenjivo '
             '(not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-kemijskog-inzenjerstva-i-tehnologije/|Razlikovni '
         'programi za upis na diplomske studije Fakulteta kemijskog inženjerstva i '
         'tehnologije',
  'title': 'Razlikovni programi za upis na diplomske studije Fakulteta kemijskog '
           'inženjerstva i tehnologije',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-kemijskog-inzenjerstva-i-tehnologije/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Individual programme480–1800h (about '
             'half teaching/half independent work); listed EUR60 entry-condition '
             'check+30 enrolment+25/ECTS. Jezik: hrvatski; izvođenje: klasično '
             '(uživo). Published HKO 6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/|Osposobljavanje '
         'i usavršavanje revizora cestovne sigurnosti',
  'title': 'Osposobljavanje i usavršavanje revizora cestovne sigurnosti',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 36 sati; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 700. Published HKO 6 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/|Stručni '
         'seminar o izvođenju, održavanju i kontroli oznaka na kolniku',
  'title': 'Stručni seminar o izvođenju, održavanju i kontroli oznaka na kolniku',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 24 sata (16 sati teorijske '
             'nastave + 8 sati praktičnog dijela); Jezik: hrvatski; Izvođenje: '
             'klasično (uživo); Katalog EUR: Troškovi upisa i pohađanja cjelokupnog '
             'Seminara: • 1.000 (bez PDV-a) za članove Via Vite • 1.200 (bez PDV-a) '
             'ostali. Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/|Osposobljavanje '
         'za izbjegavanje nepravilnog položaja aviona i vađenje iz njega',
  'title': 'Osposobljavanje za izbjegavanje nepravilnog položaja aviona i vađenje iz '
           'njega',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 5 sati nastave i 3 sata '
             'letačkog osposobljavanja na avionu; Jezik: hrvatski; Izvođenje: klasično '
             '(uživo); na daljinu; mješovito; Katalog EUR: 1.245 bez PDV. Published '
             'HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/|Osposobljavanje '
         'za ovlaštenje instruktora letenja na avionu',
  'title': 'Osposobljavanje za ovlaštenje instruktora letenja na avionu',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 141 sati nastave i vježbi + '
             '30 sati letačkog osposobljavanja na avionu i simulatoru; Jezik: '
             'hrvatski; Izvođenje: klasično (uživo); na daljinu; mješovito; Katalog '
             'EUR: Od 8.641 do 15.390 bez PDV, ovisno o broju polaznika i tipu aviona '
             'na kojem se provodi praktični dio školovanja. Published HKO 5; 6 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/|Integrirano '
         'ATP(A) osposobljavanje',
  'title': 'Integrirano ATP(A) osposobljavanje',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 1004 sata nastave i vježbi + '
             '200 sati letačkog osposobljavanja (avion i simulator letenja); Jezik: '
             'engleski; Izvođenje: klasično (uživo); na daljinu; mješovito; Katalog '
             'EUR: 71.357,50 odnosno 58.248,10 bez PDV za studente prijediplomskog '
             'studija aeronautike Fakulteta prometnih znanosti. Published HKO 5; 6 '
             '(not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/|Osposobljavanje '
         'za suradnju višečlane posade aviona',
  'title': 'Osposobljavanje za suradnju višečlane posade aviona',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 25 sati nastave i 20 sati '
             'letačkog osposobljavanja na simulatoru; Jezik: hrvatski nastava se može '
             'izvoditi i na engleskom jeziku; Izvođenje: klasično (uživo); na daljinu; '
             'mješovito; Katalog EUR: Od 2.125 do 4.875 bez PDV, ovisno o broju '
             'polaznika. Published HKO 5; 6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/|Osposobljavanje '
         'za ovlaštenje za instrumentalno letenje na dvomotornom avionu',
  'title': 'Osposobljavanje za ovlaštenje za instrumentalno letenje na dvomotornom '
           'avionu',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 5 sati letačkog '
             'osposobljavanja na avionu i simulatoru; Jezik: hrvatski nastava se može '
             'izvoditi i na engleskom jeziku; Izvođenje: klasično (uživo); na daljinu; '
             'mješovito; Katalog EUR: 1.730 bez PDV. Published HKO 5; 6 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/|Osposobljavanje '
         'za ovlaštenje vizualno letenje na dvomotornom klipnom avionu',
  'title': 'Osposobljavanje za ovlaštenje vizualno letenje na dvomotornom klipnom '
           'avionu',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 7 sati nastave i 6 sati '
             'letačkog osposobljavanja na avionu; Jezik: hrvatski nastava se može '
             'izvoditi i na engleskom jeziku; Izvođenje: klasično (uživo); na daljinu; '
             'mješovito; Katalog EUR: 3.230 bez PDV. Published HKO 5; 6 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/|Osposobljavanje '
         'za dozvolu privatnog pilota aviona',
  'title': 'Osposobljavanje za dozvolu privatnog pilota aviona',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 100 sati nastave i vježbi + '
             '45 sati letačkog osposobljavanja na avionu; Jezik: hrvatski nastava se '
             'može izvoditi i na engleskom jeziku; Izvođenje: klasično (uživo); na '
             'daljinu; mješovito; Katalog EUR: Od 12.345 do 15.495 bez PDV, ovisno o '
             'tipu aviona na kojem se provodi praktični dio školovanja. Published HKO '
             '5; 6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/|Osposobljavanje '
         'za ovlaštenje za instrumentalno letenje na jednomotornom avionu – praktično '
         'osposobljavanje',
  'title': 'Osposobljavanje za ovlaštenje za instrumentalno letenje na jednomotornom '
           'avionu – praktično osposobljavanje',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 50 sati letačkog '
             'osposobljavanja na avionu i simulatoru; Jezik: hrvatski nastava se može '
             'izvoditi i na engleskom jeziku; Izvođenje: klasično (uživo); Katalog '
             'EUR: 10.825 bez PDV. Published HKO 5; 6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/|Ljetna '
         'škola „Road Safety Summer School“',
  'title': 'Ljetna škola „Road Safety Summer School“',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Listed early fees EUR600 master/700 '
             'postgraduate/900 other; standard700/800/1000 respectively. Jezik: '
             'engleski; izvođenje: klasično (uživo). Published HKO 6 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/|Plan '
         'i program osnovnog osposobljavanja kontrolora zračnog prometa – tečaj (engl. '
         'Basic ATCO Training Plan and Program – Training Course)',
  'title': 'Plan i program osnovnog osposobljavanja kontrolora zračnog prometa – tečaj '
           '(engl. Basic ATCO Training Plan and Program – Training Course)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: Program traje 444 sata (414 '
             'sati nastave + 30 sati praktičnih vježbi na simulatoru za kontrolu '
             'zračnog prometa); Jezik: Program se može izvoditi na hrvatskom ili '
             'engleskom jeziku; Izvođenje: klasično (uživo); Katalog EUR: Ovisi o '
             'veličini grupe polaznika te je li financiran od naručitelja – za '
             'optimalan broj od 12 kandidata okvirna cijena je 87.750. Published HKO 5 '
             '(not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/|Osposobljavanje '
         'za instruktora na uređaju za vježbanje (Synthetic Training Device Instructor '
         'Training (STDI) Course)',
  'title': 'Osposobljavanje za instruktora na uređaju za vježbanje (Synthetic Training '
           'Device Instructor Training (STDI) Course)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 36 sati (25 sati nastave, 5 '
             'sati samostalnog rada i 6 sati praktičnih vježbi na simulatoru za '
             'kontrolu zračnog prometa); Jezik: engleski; Izvođenje: klasično (uživo); '
             'mješovito; Katalog EUR: Cijena ovisi o veličini grupe polaznika te je li '
             'financiran od naručitelja – po kandidatu cijena iznosi 850. Published '
             'HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/|Osposobljavanje '
         'za ocjenjivača (Assessor Training Course)',
  'title': 'Osposobljavanje za ocjenjivača (Assessor Training Course)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-prometnih-znanosti/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 22 sata (21 sat nastave 1 sat '
             'praktičnih vježbi ocjenjivanja na simulatoru za kontrolu zračnog '
             'prometa); Jezik: engleski; Izvođenje: klasično (uživo); mješovito; '
             'Katalog EUR: Ovisi o veličini grupe polaznika te je li financiran od '
             'naručitelja – po kandidatu cijena iznosi 580. Published HKO 5 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Model '
         'Management 1',
  'title': 'Model Management 1',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 10 sati (3 sata nastave + 7 '
             'sati samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 350. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Kompletna '
         'imedijatna sanacija atrofične donje čeljusti SHORT implantatima '
         'fiksnoprotetskim radom',
  'title': 'Kompletna imedijatna sanacija atrofične donje čeljusti SHORT implantatima '
           'fiksnoprotetskim radom',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 11 sati (5 sati nastave i '
             'demonstracija + 6 sati samostalnog rada); Jezik: hrvatski; Izvođenje: '
             'klasično (uživo); Katalog EUR: 350. Published HKO 7 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Terapija '
         'periimplantatnih bolesti',
  'title': 'Terapija periimplantatnih bolesti',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 13 sati (7 sati nastave + 6 '
             'sati samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 450. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Kako '
         'uspješno provesti izbjeljivanje na vitalnim i avitalnim zubima',
  'title': 'Kako uspješno provesti izbjeljivanje na vitalnim i avitalnim zubima',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 8 sati (4 sata nastave + 4 '
             'sata samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 150. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Tips '
         '& Tricks oralne kirurgije',
  'title': 'Tips & Tricks oralne kirurgije',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 12 sati (6 sati nastave + 6 '
             'sati samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 300. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Od '
         'reparacije do regeneracije – što je novo u endodonciji?',
  'title': 'Od reparacije do regeneracije – što je novo u endodonciji?',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 12 sati (4 sati nastave + 8 '
             'sati samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 300. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Simpozij: '
         'Sigurnost prije svega. Nesvakidašnji izazovi u ordinaciji - ukratko što '
         'učiniti?',
  'title': 'Simpozij: Sigurnost prije svega. Nesvakidašnji izazovi u ordinaciji - '
           'ukratko što učiniti?',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 6 sati (nastava); Jezik: '
             'hrvatski; Izvođenje: klasično (uživo); Katalog EUR: 150. Published HKO '
             '5;7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Izrada '
         'pokrovne proteze na implantatima u mandibuli',
  'title': 'Izrada pokrovne proteze na implantatima u mandibuli',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 11 sati (3 sata nastave +8 '
             'sati samostalnoog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 390. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Inicijalna-nekirurška '
         'parodontna terapija',
  'title': 'Inicijalna-nekirurška parodontna terapija',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 10 sati (4 sata nastave + 6 '
             'sati samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 390. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|K '
         'ako postići stabilnost u ortodonciji? Retencija i recidiv',
  'title': 'K ako postići stabilnost u ortodonciji? Retencija i recidiv',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 10 sati (5 sati nastave + 5 '
             'sati samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 300. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Planiranje '
         'fiksnoprotetske terapije i dijagnostički pristup u estetskoj zoni',
  'title': 'Planiranje fiksnoprotetske terapije i dijagnostički pristup u estetskoj '
           'zoni',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 7.5 sati (3.5 sati nastave i '
             '4 sata samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 270. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Jednoposjetno '
         'endodontsko liječenje zuba i postendodontska opskrba zuba',
  'title': 'Jednoposjetno endodontsko liječenje zuba i postendodontska opskrba zuba',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 9 sati (3 sata nastave + 6 '
             'sati samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 230. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|11. '
         'Međunarodni kongres Stomatološkog fakulteta Sveučilišta u Zagrebu',
  'title': '11. Međunarodni kongres Stomatološkog fakulteta Sveučilišta u Zagrebu',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 17 sati (predavanja i '
             'posteri); Jezik: hrvatski; Izvođenje: klasično (uživo); Katalog EUR: '
             '250. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Terapija '
         'bruksizma udlagama i alternative',
  'title': 'Terapija bruksizma udlagama i alternative',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 8.5 sati (2 sata nastava + '
             '6.5 sati samstalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 200. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Otisci '
         'od A/butmenta do Z/uba',
  'title': 'Otisci od A/butmenta do Z/uba',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 8 sati (4 sata nastave + 6 '
             'sati samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 300. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Mali '
         'pacijent veliki izazov',
  'title': 'Mali pacijent veliki izazov',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 10 sati (5 sata nastave + 5 '
             'sati samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 200. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Klinička '
         'endodoncija – predvidljiva, jednostavna i učinkovita',
  'title': 'Klinička endodoncija – predvidljiva, jednostavna i učinkovita',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 10 sati (5 sati nastave + 5 '
             'sati samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 290. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Napredne '
         'tehnike brušenja u fiksnoj protetici',
  'title': 'Napredne tehnike brušenja u fiksnoj protetici',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 11 sati (4 sata nastave + 7 '
             'sati samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 600. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Dizajn '
         'okluzije u 90 minuta',
  'title': 'Dizajn okluzije u 90 minuta',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 2 sata (nastava); Jezik: '
             'hrvatski; Izvođenje: klasično (uživo); Katalog EUR: 100. Published HKO 7 '
             '(not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Dizajn '
         'okluzije 2 - napredni koraci',
  'title': 'Dizajn okluzije 2 - napredni koraci',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 4 sata (1 sata nastave + 3 '
             'sata samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 200. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Klinička '
         'endodoncija 2: Revizija endodontskog punjenja',
  'title': 'Klinička endodoncija 2: Revizija endodontskog punjenja',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 9 sati (4 sata nasttave + 5 '
             'sati samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 290. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Novosti '
         'u dječjoj stomatologiji- od znanosti do klinike',
  'title': 'Novosti u dječjoj stomatologiji- od znanosti do klinike',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 12 sati (6 sati nastave i 6 '
             'sati samostalnoog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 150. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Klinički '
         'izazovi u endodonciji',
  'title': 'Klinički izazovi u endodonciji',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 9 sati (5 sati nastave + 4 '
             'sata samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 200. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Stabiliziraj '
         'taraumu, spasi zub!',
  'title': 'Stabiliziraj taraumu, spasi zub!',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 10 (5 sati nastave + 5 sati '
             'samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); Katalog '
             'EUR: 200. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Kako '
         'spriječiti prigovore i tužbe pacijenata u dentalnoj medicini?',
  'title': 'Kako spriječiti prigovore i tužbe pacijenata u dentalnoj medicini?',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 5 sati (nastava); Jezik: '
             'hrvatski; Izvođenje: klasično (uživo); Katalog EUR: 100. Published HKO 7 '
             '(not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|Pacijenti '
         's temporomandibularnim poremećajima i opstruktivnom apnejom u spavanju – '
         'kakvu udlagu napraviti?',
  'title': 'Pacijenti s temporomandibularnim poremećajima i opstruktivnom apnejom u '
           'spavanju – kakvu udlagu napraviti?',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 8 sati (4 sata nastave + 4 '
             'sata samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 230. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|3,2,1 '
         'Kreni! Brušenje u fiksnoprotetskoj terapiji – multidisciplinarni pristup',
  'title': '3,2,1 Kreni! Brušenje u fiksnoprotetskoj terapiji – multidisciplinarni '
           'pristup',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 12 sati (6 sati nastave + 6 '
             'sati samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 500. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/|BOPT '
         'biološki orjentirane tehnike preparacije',
  'title': 'BOPT biološki orjentirane tehnike preparacije',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/stomatoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 10 sati (4 sata nastave + 6 '
             'sati samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 190. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/|Seminar: '
         'Mjerenje tlaka – osnove mjerenja i umjeravanja',
  'title': 'Seminar: Mjerenje tlaka – osnove mjerenja i umjeravanja',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 16 (10 sati nastave + 6 sati '
             'rada u laboratoriju); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 400 + PDV. Published HKO 6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/|Summer '
         'school on energy planning of 100% renewable energy systems',
  'title': 'Summer school on energy planning of 100% renewable energy systems',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 75 (30 sati nastave + 45 sati '
             'samostalnog rada); Jezik: engleski; Izvođenje: klasično (uživo); Katalog '
             'EUR: 450. Published HKO 6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/|Konstruiranje '
         'pomoću računala',
  'title': 'Konstruiranje pomoću računala',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 30 sati (15 sati nastave + 15 '
             'sati samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 1.500. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/|Metode '
         'kreativnosti i inženjerskog rješavanja problema',
  'title': 'Metode kreativnosti i inženjerskog rješavanja problema',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 30 sati (15 sati nastave + 15 '
             'sati samostalnog rada); Jezik: engleski; Izvođenje: klasično (uživo); na '
             'daljinu; Katalog EUR: 1.500. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/|Metode '
         'umjetne inteligencije u konstruiranju',
  'title': 'Metode umjetne inteligencije u konstruiranju',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 30 sati (15 sati nastave + 15 '
             'sati samostalnog rada); Jezik: engleski; Izvođenje: klasično (uživo); na '
             'daljinu; Katalog EUR: 1.500. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/|NUMAP-FOAM '
         '(Numerical Modelling of Coupled Problems in Applied Physics with OpenFOAM)',
  'title': 'NUMAP-FOAM (Numerical Modelling of Coupled Problems in Applied Physics '
           'with OpenFOAM)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 15 sati predavanja + 60 sati '
             'vježbi + 40 sati samostalnog rada; Jezik: engleski; Izvođenje: klasično '
             '(uživo); Katalog EUR: 2.500. Published HKO 6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/|Upotreba '
         'virtualne stvarnosti u konstruiranju',
  'title': 'Upotreba virtualne stvarnosti u konstruiranju',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 30 sati (20 sati nastave + 10 '
             'sati samostalnog rada); Jezik: engleski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 1.500. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/|Gospodarenje '
         'radnim tvarima u nepokretnim i mobilnim rashladnim i klimatizacijskim '
         'uređajima te dizalicama topline',
  'title': 'Gospodarenje radnim tvarima u nepokretnim i mobilnim rashladnim i '
           'klimatizacijskim uređajima te dizalicama topline',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Category-dependent '
             'theoretical/practical hours; listed stationary I/II/III/IV fees '
             'EUR870/680/270/460; vehicle MAC330; upskilling210. Jezik: hrvatski; '
             'izvođenje: klasično (uživo). Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/|Kvalifikacija '
         'za međunarodnog inženjera za zavarivanje (IWE)',
  'title': 'Kvalifikacija za međunarodnog inženjera za zavarivanje (IWE)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 386 sati predavanja i 60 sati '
             'praktičnih vježbi; Jezik: hrvatskI; Izvođenje: mješovito; Katalog EUR: '
             '4.980 + PDV. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/|Kvalifikacija '
         'za međunarodnog tehničara za zavarivanje (IWT)',
  'title': 'Kvalifikacija za međunarodnog tehničara za zavarivanje (IWT)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 280 sati predavanja i 60 sati '
             'praktičnih vježbi; Jezik: hrvatski; Izvođenje: mješovito; Katalog EUR: '
             '4.460 + PDV. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/|Kvalifikacija '
         'za međunarodnog specijalista za zavarivanje (IWS)',
  'title': 'Kvalifikacija za međunarodnog specijalista za zavarivanje (IWS)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 162 sata predavanja i 60 sati '
             'praktičnih vježbi; Jezik: hrvatski; Izvođenje: mješovito; Katalog EUR: '
             '3.980 + PDV. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/|Erasumus '
         'Mundus Joint Master Degree Programme SUSTAINABLE SHIP AND SHIPPING 4.0 (SEAS '
         '4.0)',
  'title': 'Erasumus Mundus Joint Master Degree Programme SUSTAINABLE SHIP AND '
           'SHIPPING 4.0 (SEAS 4.0)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-strojarstva-i-brodogradnje/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 3 semestra; Jezik: engleski; '
             'Izvođenje: klasično (uživo); mješovito; Katalog EUR: 13.500 za polaznike '
             'izvan EU i 6.750 za polaznike iz EU. Published HKO 7 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/gradevinski-fakultet/|IPMA '
         'trening i certificiranje voditelja projekata u skladu sa Zakonom o poslovima '
         'i djelatnostima prostornog uređenja i gradnje (napredni modul)',
  'title': 'IPMA trening i certificiranje voditelja projekata u skladu sa Zakonom o '
           'poslovima i djelatnostima prostornog uređenja i gradnje (napredni modul)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/gradevinski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Listed training EUR804.64, plus '
             'examination EUR357+25%VAT payable separately to the association. Jezik: '
             'hrvatski; izvođenje: klasično (uživo); na daljinu. Published HKO nije '
             'primjenjivo (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/gradevinski-fakultet/|IPMA '
         'trening i certificiranje voditelja projekata u skladu sa Zakonom o poslovima '
         'i djelatnostima prostornog uređenja i gradnje (osnovni modul)',
  'title': 'IPMA trening i certificiranje voditelja projekata u skladu sa Zakonom o '
           'poslovima i djelatnostima prostornog uređenja i gradnje (osnovni modul)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/gradevinski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Listed training EUR207.38, plus '
             'examination EUR357+25%VAT payable separately to the association. Jezik: '
             'hrvatski; izvođenje: klasično (uživo); na daljinu. Published HKO nije '
             'primjenjivo (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/graficki-fakultet/|Analogna '
         'fotografija',
  'title': 'Analogna fotografija',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/graficki-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 6h; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 50. Published HKO nije '
             'primjenjivo (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/graficki-fakultet/|Stručni '
         'tečaj za operatere na digitalnim tiskarskim strojevima',
  'title': 'Stručni tečaj za operatere na digitalnim tiskarskim strojevima',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/graficki-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 16 h ( 8h teorijske nastave i '
             '8 h praktične nastave); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 530. Published HKO nije primjenjivo (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/rudarsko-geolosko-naftni-fakultet/|Ljetna '
         'škola naftnog rudarstva / Petroleum Engineering Summer School',
  'title': 'Ljetna škola naftnog rudarstva / Petroleum Engineering Summer School',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/rudarsko-geolosko-naftni-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 30-40 sati, ovisno o '
             'radionici; Jezik: engleski; Izvođenje: klasično (uživo); sa stjecanjem '
             'ECTS bodova; mješovito; Katalog EUR: 1.400 + PDV (VAT). Published HKO 7 '
             '(not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/rudarsko-geolosko-naftni-fakultet/|Međunarodna '
         'škola rudarstva u Dubrovniku: Primjena inovacija',
  'title': 'Međunarodna škola rudarstva u Dubrovniku: Primjena inovacija',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/rudarsko-geolosko-naftni-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: Trajanje programa ukupno (16 '
             'sati nastave + 4 sati samostalnog rada); Jezik: engleski; Izvođenje: '
             'mješovitO; Katalog EUR: 400 – 500 (+PDV) za sudjelovanje uživo i 100 '
             '(total) za sudjelovanje online. Published HKO 6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/rudarsko-geolosko-naftni-fakultet/|Temeljni '
         'seminar iz protueksplozijske zaštite električkih i neelektričkih uređaja i '
         'instalacija',
  'title': 'Temeljni seminar iz protueksplozijske zaštite električkih i neelektričkih '
           'uređaja i instalacija',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/rudarsko-geolosko-naftni-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 34 sata nastave; Jezik: '
             'hrvatski; Izvođenje: klasično (uživo); Katalog EUR: 900. Published HKO 6 '
             '(not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/rudarsko-geolosko-naftni-fakultet/|Webinar '
         'iz protueksplozijske zaštite električkih i neelektričkih uređaja i '
         'instalacija (obnavljanje znanja)',
  'title': 'Webinar iz protueksplozijske zaštite električkih i neelektričkih uređaja i '
           'instalacija (obnavljanje znanja)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/rudarsko-geolosko-naftni-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 15 sati nastave; Jezik: '
             'hrvatski; Izvođenje: na daljinu; Katalog EUR: 380. Published HKO 6 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/tekstilno-tehnoloski-fakultet/|Kreativno '
         'tkanje kao put stvaranja',
  'title': 'Kreativno tkanje kao put stvaranja',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/tekstilno-tehnoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 40 sati (12 sati nastave + 28 '
             'mentoriranog samostalnog rada polaznika); Jezik: hrvatski; Izvođenje: '
             'klasično (uživo); Katalog EUR: 300. Published HKO 5 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/tekstilno-tehnoloski-fakultet/|Razlikovni '
         'program - program za stjecanje uvjeta za upis na sveučilišne diplomske '
         'studije TEKSTILNA TEHNOLOGIJA I INŽENJERSTVO (TTI) i TEKSTILNI I MODNI '
         'DIZAJN (TMD)',
  'title': 'Razlikovni program - program za stjecanje uvjeta za upis na sveučilišne '
           'diplomske studije TEKSTILNA TEHNOLOGIJA I INŽENJERSTVO (TTI) i TEKSTILNI I '
           'MODNI DIZAJN (TMD)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/tekstilno-tehnoloski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Individual bridging subjects/hours; '
             'listed fee EUR18.58 per assigned ECTS, not a fixed whole-programme '
             'price. Jezik: hrvatski; izvođenje: mješovito;klasično (uživo);sa '
             'stjecanjem ECTS bodova. Published HKO 6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Liječenje '
         'rana i amputacije - serija tečaja : 1. Liječenje rana – osnovni tečaj 2. '
         'Liječenje rana – napredni tečaj 3. Amputacije ekstremiteta prsta i repa',
  'title': 'Liječenje rana i amputacije - serija tečaja : 1. Liječenje rana – osnovni '
           'tečaj 2. Liječenje rana – napredni tečaj 3. Amputacije ekstremiteta prsta '
           'i repa',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 8-10 sati; Jezik: hrvatski; '
             'Izvođenje: klasično; Katalog EUR: 600-900 eura. Published HKO 7 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Uloga '
         'veterinara u slučaju nuklearne i radiološke nesreće',
  'title': 'Uloga veterinara u slučaju nuklearne i radiološke nesreće',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Published nuclear/radiological title '
             'conflicts with copied bee-sampling competencies: detailed curriculum '
             'unknown. Listed14h (5 teaching+9 self-study; additional '
             'exercise/workshop breakdown ambiguous), EUR200. Jezik: hrvatski; '
             'izvođenje: klasično (uživo). Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Službeno '
         'uzorkovanje na pčelinjaku',
  'title': 'Službeno uzorkovanje na pčelinjaku',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Published duration “20” has no unit; do '
             'not infer20hours. Listed fee below is a catalogue entry. Jezik: '
             'hrvatski; izvođenje: klasično (uživo). Cijena(EUR): 200. Published HKO 7 '
             '(not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Prepoznavanje '
         'bolesti pčela',
  'title': 'Prepoznavanje bolesti pčela',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 20; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); bez stjecanja ECTS bodova; sa stjecanjem '
             'ECTS bodova; Katalog EUR: 400. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Etinioza-prepoznavanje '
         'i kontrolne mjere',
  'title': 'Etinioza-prepoznavanje i kontrolne mjere',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 6; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 200. Published HKO 7 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Bolesti '
         'pčela',
  'title': 'Bolesti pčela',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 22 sata; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 400. Published HKO 7 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Tečaj '
         'za osposobljavanje osoba koje rade s pokusnim životinjama i životinjama za '
         'proizvodnju bioloških pripravaka',
  'title': 'Tečaj za osposobljavanje osoba koje rade s pokusnim životinjama i '
           'životinjama za proizvodnju bioloških pripravaka',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 65 sati (52 sata predavanja i '
             '13 sati praktična nastava); Jezik: hrvatski; Izvođenje: klasično '
             '(uživo); na daljinu; Katalog EUR: 435. Published HKO 7 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Terapia '
         'laserom- jesmo li na istoj valnoj duljini?',
  'title': 'Terapia laserom- jesmo li na istoj valnoj duljini?',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 4 sata ( 2,5 sata nastave i '
             '1,5 sat praktični rad); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 150. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Praktična '
         'veterinarska stomatologija',
  'title': 'Praktična veterinarska stomatologija',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 8 sati (2 sata nastave + 6 '
             'sati samostalnog rada polaznika); Jezik: hrvatski; Izvođenje: klasično '
             '(uživo); Katalog EUR: 500. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|TPLO '
         'tečaj',
  'title': 'TPLO tečaj',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 8 sati (4 sata nastave + 4 '
             'sata samostalnog rada polaznika); Jezik: hrvatski; Izvođenje: klasično '
             '(uživo); Katalog EUR: 1.250. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Pozicije '
         'snimanja za uzgojne preglede pasa (Tehnike snimanja za radiološke uzgojne '
         'preglede pasa)',
  'title': 'Pozicije snimanja za uzgojne preglede pasa (Tehnike snimanja za radiološke '
           'uzgojne preglede pasa)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 5 sati (3h teorijski dio + 2h '
             'praktični dio); Jezik: hrvatski; Izvođenje: klasično (uživo); na '
             'daljinu; Katalog EUR: 200. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Veterinarsko '
         'javno zdravstvo',
  'title': 'Veterinarsko javno zdravstvo',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 76 sati teorijske nastave i '
             '49 sati praktične nastave; Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 50-200. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Program '
         'osposobljavanja osoba odgovornih za uzgoj i brigu o životinjama u uzgoju '
         'kućnih ljubimaca namijenjenih prodaji',
  'title': 'Program osposobljavanja osoba odgovornih za uzgoj i brigu o životinjama u '
           'uzgoju kućnih ljubimaca namijenjenih prodaji',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 6 sati; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); na daljinu; Katalog EUR: 30-80. Published '
             'HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Zaštita '
         'zdravlja toplovodnih riba u uzgoju',
  'title': 'Zaštita zdravlja toplovodnih riba u uzgoju',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 5 sati (4 sata nastave + 1 '
             'sat samostalnog rada polaznika); Jezik: hrvatski; Izvođenje: klasično '
             '(uživo); Katalog EUR: 120 + PDV. Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Zaštita '
         'zdravlja slatkovodnih riba',
  'title': 'Zaštita zdravlja slatkovodnih riba',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 14 sati (8 sati nastave + 6 '
             'sati samostalnog rada polaznika); Jezik: hrvatski; Izvođenje: klasično '
             '(uživo); Katalog EUR: 400 + PDV. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Tečaj '
         'za veterinarske higijeničare i dezinfektore',
  'title': 'Tečaj za veterinarske higijeničare i dezinfektore',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 5,75 sati nastave + 1 sat '
             'praktično; Jezik: hrvatski; Izvođenje: klasično (uživo); Katalog EUR: '
             '150-200. Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Primijenjena '
         'dezinfekcija, dezinsekcija i deratizacija',
  'title': 'Primijenjena dezinfekcija, dezinsekcija i deratizacija',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 17,5 sati nastave + 3,66 sati '
             'praktična primjena + 1 sat provjera znanja; Jezik: hrvatski; Izvođenje: '
             'klasično (uživo); na daljinu; Katalog EUR: 450-500. Published HKO 7 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Ponašanje '
         'i dobrobit farmskih životinja',
  'title': 'Ponašanje i dobrobit farmskih životinja',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 7,25 sati nastave + 0,5 sati '
             'provjera znanja; Jezik: hrvatski; Izvođenje: klasično (uživo); na '
             'daljinu; Katalog EUR: 132. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|The '
         'role of veterinarians in the wildlife protection',
  'title': 'The role of veterinarians in the wildlife protection',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 3 sata predavanja; Jezik: '
             'hrvatski engleski ako je veći broj stranih prijava; Izvođenje: na '
             'daljinu; Katalog EUR: 0-150. Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Marketing '
         'u veterinarskoj praksi',
  'title': 'Marketing u veterinarskoj praksi',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 5 sati nastave +2 samostalnog '
             'rada; Jezik: hrvatski; Izvođenje: klasično (uživo); Katalog EUR: 150. '
             'Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Poslovno '
         'upravljanje u veterinarskoj praksi',
  'title': 'Poslovno upravljanje u veterinarskoj praksi',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 7 sati (5 sati nastave +2 '
             'samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); Katalog '
             'EUR: 150. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Uloga '
         'veterinara u povećanju profitabilnosti farme',
  'title': 'Uloga veterinara u povećanju profitabilnosti farme',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 6 sati (4 sati nastave +2 '
             'samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo); Katalog '
             'EUR: 150. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Primjena '
         'plinske kromatografije u analizi masnokiselinskog sastava bioloških uzoraka',
  'title': 'Primjena plinske kromatografije u analizi masnokiselinskog sastava '
           'bioloških uzoraka',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 10 sati (4 sati nastave +6 '
             'samostalnog rada polaznika); Jezik: Hrvatski, engleski; Izvođenje: '
             'Mješovito (na daljinu i u sastavnici). Published HKO 7 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Endoskopska '
         'pretraga probavnog i dišnog sustava pasa i mačaka',
  'title': 'Endoskopska pretraga probavnog i dišnog sustava pasa i mačaka',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 10 sati (4 sati predavanja i '
             '6 sati praktičnog rada uz nadzor); Jezik: Hrvatski, engleski; Izvođenje: '
             'Klasično i/ili mješovito. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Ultrazvučna '
         'pretraga abdomena pasa i mačaka',
  'title': 'Ultrazvučna pretraga abdomena pasa i mačaka',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 8 sati (4 sata predavanja i 4 '
             'sata praktičnog rada uz nadzor); Jezik: Hrvatski, engleski; Izvođenje: '
             'Klasično i/ili mješovito. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Klamidioza '
         'ptica – značaj, širenje, uzorkovanje i dijagnostika bolesti',
  'title': 'Klamidioza ptica – značaj, širenje, uzorkovanje i dijagnostika bolesti',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 14 sati (6 sati nastave (4 '
             'sata predavanja i 2 sata vježbi) + 8 sati samostalnog rada); Jezik: '
             'Hrvatski; Izvođenje: Klasično, uživo. Published HKO 7 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Zarazne '
         'bolesti ptica kućnih ljubimaca – značaj, širenje, uzorkovanje i dijagnostika '
         'bolesti',
  'title': 'Zarazne bolesti ptica kućnih ljubimaca – značaj, širenje, uzorkovanje i '
           'dijagnostika bolesti',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 16 sati (8 sati nastave (5 '
             'sati predavanja i 3 sata vježbi) + 8 sati samostalnog rada); Jezik: '
             'Hrvatski; Izvođenje: Klasično, uživo. Published HKO 7 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Osnove '
         'profitabilnosti proizvodnje mlijeka na farmama',
  'title': 'Osnove profitabilnosti proizvodnje mlijeka na farmama',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 34 (24 sati nastave +10 '
             'samostalnog rada); Jezik: Hrvatski; Izvođenje: Klasično, na daljinu. '
             'Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Procjena '
         'troškova bolesti i strategije upravljanja cijenama',
  'title': 'Procjena troškova bolesti i strategije upravljanja cijenama',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 34 (24 sati nastave +10 '
             'samostalnog rada); Jezik: Hrvatski; Izvođenje: Klasično i na daljinu. '
             'Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Programiranje '
         'zdravstvene zaštite životinja i procjena troškova',
  'title': 'Programiranje zdravstvene zaštite životinja i procjena troškova',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 50 (15 sati nastave +35 '
             'samostalnog rada); Jezik: Hrvatski; Izvođenje: Klasično i na daljinu, na '
             'sastavnici. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Upravljanje '
         'rasplođivanjem konja',
  'title': 'Upravljanje rasplođivanjem konja',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 60 h (15 h predavanja, 15 h '
             'praktični rad i 30 h samostalni rad na daljinu uz nadzor); Jezik: '
             'Hrvatski/engleski; Izvođenje: Klasično, na sastavnici, izvan sastavnice. '
             'Published HKO  (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Upravljanje '
         'rasplođivanjem konja i napredni pristup rasplođivanju smanjeno plodnih '
         'kopitara',
  'title': 'Upravljanje rasplođivanjem konja i napredni pristup rasplođivanju smanjeno '
           'plodnih kopitara',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 60 h (15 h predavanja, 15 h '
             'praktični rad i 30 h samostalni rad na daljinu uz nadzor); Jezik: '
             'Hrvatski/engleski; Izvođenje: Klasično, na sastavnici, izvan sastavnice. '
             'Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Endoskopska '
         'pretraga probavnog i dišnog sustava konja',
  'title': 'Endoskopska pretraga probavnog i dišnog sustava konja',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 10 sati (4 sati predavanja i '
             '6 sati praktičnog rada uz nadzor); Jezik: Hrvatski, engleski; Izvođenje: '
             'Klasično i/ili mješovito. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Ultrazvučna '
         'pretraga abdomena konja',
  'title': 'Ultrazvučna pretraga abdomena konja',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 8 sati (4 sata predavanja i 4 '
             'sata praktičnog rada uz nadzor); Jezik: Hrvatski, engleski; Izvođenje: '
             'Klasično i/ili mješovito. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Osnove '
         'elektrokardiografske dijagnostike u pasa i mačaka',
  'title': 'Osnove elektrokardiografske dijagnostike u pasa i mačaka',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 10 sati (6 sati predavanja i '
             '4 sati praktičnog rada uz nadzor); Jezik: Hrvatski, engleski; Izvođenje: '
             'Klasično i/ili mješovito. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Mikrobiologija '
         'hrane u službenim kontrolama objekata koji posluju s hranom životinjskog '
         'podrijetla',
  'title': 'Mikrobiologija hrane u službenim kontrolama objekata koji posluju s hranom '
           'životinjskog podrijetla',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 7 sati (4 h predavanja i 3 h '
             'problemskih zadataka); Jezik: Hrvatski; Izvođenje: Klasično, na '
             'sastavnici i izvan sastavnice. Published HKO  (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Preduvjetni '
         'programi i HACCP sustav u objektima koji posluju s hranom životinjskog '
         'podrijetla',
  'title': 'Preduvjetni programi i HACCP sustav u objektima koji posluju s hranom '
           'životinjskog podrijetla',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 8 sati (5 h predavanja i 3 h '
             'problemskih zadataka); Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'Katalog EUR: 250. Published HKO nije primjenjivo (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/|Osnovne '
         'ortopedske operacije',
  'title': 'Osnovne ortopedske operacije',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/veterinarski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 6 sati (2 sata predavanja + 4 '
             'sata radionica/samostalni rad polaznika); Jezik: hrvatski; Izvođenje: '
             'klasično (uživo); Katalog EUR: 150-250. Published HKO nije primjenjivo '
             '(not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/agronomski-fakultet/|Specijalističko '
         'usavršavanje analitičara za utvrđivanje botaničkog podrijetla meda',
  'title': 'Specijalističko usavršavanje analitičara za utvrđivanje botaničkog '
           'podrijetla meda',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/agronomski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 66 sati (22 sata teoretske i '
             '44 sata praktične nastave); Jezik: hrvatski; Izvođenje: klasično '
             '(uživo); Katalog EUR: Po jednom polazniku 862 EUR, a slučaju da se radi '
             'o instituciji do tri polaznika 1.078 EUR. Published HKO 5 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/agronomski-fakultet/|Program '
         'osposobljavanja ocjenjivača',
  'title': 'Program osposobljavanja ocjenjivača',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/agronomski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 30 sati; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 600. Published HKO 5 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-sumarstva-i-drvne-tehnologije/|Osnove '
         'poznavanja drva',
  'title': 'Osnove poznavanja drva',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-sumarstva-i-drvne-tehnologije/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 7 sati nastave + 53 sata '
             'samostalnog rada; Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'mješovito; Katalog EUR: 373,75 (uključen PDV). Published HKO 5 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-sumarstva-i-drvne-tehnologije/|ISPM '
         '15 – DMP',
  'title': 'ISPM 15 – DMP',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-sumarstva-i-drvne-tehnologije/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 10 sati nastave + 50 sati '
             'samostalnog rada; Jezik: hrvatski; Izvođenje: klasično (uživo); '
             'mješovito; Katalog EUR: 460 (uključen PDV). Published HKO 5 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-sumarstva-i-drvne-tehnologije/|Stručno '
         'usavršavanje članova Komore inženjera šumarstva i drvne tehnologije',
  'title': 'Stručno usavršavanje članova Komore inženjera šumarstva i drvne '
           'tehnologije',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-sumarstva-i-drvne-tehnologije/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 1 sat po predavanju; Jezik: '
             'hrvatski; Izvođenje: klasično (uživo); mješovito; Katalog EUR: Nema. '
             'Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Rad '
         's korisnicima u otporu: odnos, motivacija i promjena',
  'title': 'Rad s korisnicima u otporu: odnos, motivacija i promjena',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 15 sati nastave; Jezik: '
             'hrvatski; Izvođenje: klasično (uživo);na daljinu;; Katalog EUR: 300 Eura '
             '(s PDV-om). Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Od '
         'potrebe do provedbe – planiranje psihoedukativnih grupnih programa za djecu '
         'i mlade s problemima u ponašanju',
  'title': 'Od potrebe do provedbe – planiranje psihoedukativnih grupnih programa za '
           'djecu i mlade s problemima u ponašanju',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 15 sati nastave; Jezik: '
             'hrvatski; Izvođenje: klasično (uživo);na daljinu;; Katalog EUR: 300 Eura '
             '(s PDV-om). Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Europski '
         'prevencijsku kurikulum: trening za ključne ljude',
  'title': 'Europski prevencijsku kurikulum: trening za ključne ljude',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 20 sati nastave; Jezik: '
             'hrvatski; Izvođenje: klasično (uživo); Katalog EUR: 5000. Published HKO '
             '7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Jezično '
         'govorni razvoj djece predškolske dobi',
  'title': 'Jezično govorni razvoj djece predškolske dobi',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 10 sati nastave; Jezik: '
             'hrvatski; Izvođenje: klasično (uživo); na daljinu; Katalog EUR: 300. '
             'Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Kako/kada '
         'čitati djetetu: Prevencija problema u čitanju i pisanju',
  'title': 'Kako/kada čitati djetetu: Prevencija problema u čitanju i pisanju',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 6 sati; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 190. Published HKO 5 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Primjena '
         'korpusa i umjetne inteligencije u kliničkom radu logopeda',
  'title': 'Primjena korpusa i umjetne inteligencije u kliničkom radu logopeda',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 6 sati; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 190. Published HKO 7 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Procjena '
         'i poticanje razvoja slušanja, jezika i govora kod djece s oštećenjem sluha – '
         'modul 1',
  'title': 'Procjena i poticanje razvoja slušanja, jezika i govora kod djece s '
           'oštećenjem sluha – modul 1',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 6 sati; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 190. Published HKO 7 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Selektivni '
         'mutizam u dječjoj dobi - procjena i podrška',
  'title': 'Selektivni mutizam u dječjoj dobi - procjena i podrška',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 10,5 sati (8,5 sati nastave+2 '
             'sata samostalnog rada); Jezik: hrvatski; Izvođenje: klasično (uživo);na '
             'daljinu; Katalog EUR: 265. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Selektivni '
         'mutizam - edukacija za učitelje',
  'title': 'Selektivni mutizam - edukacija za učitelje',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 6 (5 sati nastava + 1 sat '
             'samostalni rad); Jezik: hrvatski; Izvođenje: na daljinu; Katalog EUR: '
             '100. Published HKO 6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Poticanje '
         'socijalne komunikacije kod djece s razvojnim odstupanjima',
  'title': 'Poticanje socijalne komunikacije kod djece s razvojnim odstupanjima',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 24 (20 sati '
             'nastave+4samostalni rad); Jezik: hrvatski; Izvođenje: klasično '
             '(uživo);na daljinu; Katalog EUR: 340. Published HKO 7 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Autizam '
         'u adolescenciji i odrasloj dobi',
  'title': 'Autizam u adolescenciji i odrasloj dobi',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 12 sati (10+2); Jezik: '
             'hrvatski; Izvođenje: klasično (uživo);na daljinu; Katalog EUR: 280. '
             'Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Individualizirani '
         'postupci za učenike sa specifičnim poremećajem učenja - elementi '
         'jednostavnog jezika u nastavničkom radu',
  'title': 'Individualizirani postupci za učenike sa specifičnim poremećajem učenja - '
           'elementi jednostavnog jezika u nastavničkom radu',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 5 sati nastave; Jezik: '
             'hrvatski; Izvođenje: klasično (uživo); Katalog EUR: 170. Published HKO 6 '
             '(not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Razvojna '
         'procjena djece dobi 0-6 godina',
  'title': 'Razvojna procjena djece dobi 0-6 godina',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 30 sati; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo);na daljinu; Katalog EUR: 600. Published HKO '
             '7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Razvojni '
         'profili djece predškolske dobi: opažanje i analiza',
  'title': 'Razvojni profili djece predškolske dobi: opažanje i analiza',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 20 sati (16 nastava + 4 '
             'samostalni rad); Jezik: hrvatski; Izvođenje: klasično (uživo);na '
             'daljinu; Katalog EUR: 365. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Procjena '
         'i terapija čitanja i pisanja u srednjoškolskoj i odrasloj dobi',
  'title': 'Procjena i terapija čitanja i pisanja u srednjoškolskoj i odrasloj dobi',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 6; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 170. Published HKO 7 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Pišem '
         'ti – pismo u logopedskoj procjeni i terapiji: vještina pisanja u školskoj '
         'dobi',
  'title': 'Pišem ti – pismo u logopedskoj procjeni i terapiji: vještina pisanja u '
           'školskoj dobi',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 6; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 170. Published HKO 7 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Matematika '
         'u školskoj dobi – kako obilježja urednog i odstupajućeg razvoja ugraditi u '
         'procjenu i terapiju',
  'title': 'Matematika u školskoj dobi – kako obilježja urednog i odstupajućeg razvoja '
           'ugraditi u procjenu i terapiju',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 6; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 150. Published HKO 7 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Matematika '
         'u predškolskoj dobi – kako obilježja urednog i odstupajućeg razvoja ugraditi '
         'u procjenu i terapiju',
  'title': 'Matematika u predškolskoj dobi – kako obilježja urednog i odstupajućeg '
           'razvoja ugraditi u procjenu i terapiju',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 6; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 150. Published HKO 7 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Edukacija '
         'za implementaciju programa prevencije kockanja mladih „TKO ZAPRAVO '
         'POBJEĐUJE?“',
  'title': 'Edukacija za implementaciju programa prevencije kockanja mladih „TKO '
           'ZAPRAVO POBJEĐUJE?“',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Listed EUR5500 excluding VAT for a '
             'GROUP of30; group funding or individual charges depend on participant '
             'numbers. Jezik: hrvatski; izvođenje: klasično (uživo). Published HKO 6;7 '
             '(not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Edukacija '
         'za implementaciju programa prevencije ponašajnih ovisnosti i rizičnih '
         'ponašanja u virtualnom okruženju „ALATI ZA MODERNO DOBA“',
  'title': 'Edukacija za implementaciju programa prevencije ponašajnih ovisnosti i '
           'rizičnih ponašanja u virtualnom okruženju „ALATI ZA MODERNO DOBA“',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Listed EUR5500 excluding VAT; group '
             'funding or individual charges depend on participant numbers. Fee unit is '
             'not specified. Jezik: hrvatski; izvođenje: klasično (uživo). Published '
             'HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Psihološki '
         'aspekti ADHD-a i podrška u školi',
  'title': 'Psihološki aspekti ADHD-a i podrška u školi',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 10.5 sati nastave; Jezik: '
             'hrvatski; Izvođenje: klasično (uživo); Katalog EUR: 260. Published HKO 5 '
             '(not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Ekspresivne '
         'art terapije u edukaciji i rehabilitaciji - Modul 1',
  'title': 'Ekspresivne art terapije u edukaciji i rehabilitaciji - Modul 1',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 24 sata nastave; Jezik: '
             'hrvatski; Izvođenje: klasično (uživo); Katalog EUR: 400. Published HKO 5 '
             '(not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Ekspresivne '
         'art terapije u edukaciji i rehabilitaciji - Modul 2',
  'title': 'Ekspresivne art terapije u edukaciji i rehabilitaciji - Modul 2',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 24; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 400. Published HKO 6 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Jednostavna '
         'metodologija u društvenim znanostima - 1. modul - online',
  'title': 'Jednostavna metodologija u društvenim znanostima - 1. modul - online',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 17 sati i 15 minuta; Jezik: '
             'hrvatski; Izvođenje: na daljinu; Katalog EUR: 350. Published HKO 6 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Jednostavna '
         'statistika u društvenim znanostima - 2. modul',
  'title': 'Jednostavna statistika u društvenim znanostima - 2. modul',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 17 sati i 15 minuta; Jezik: '
             'hrvatski; Izvođenje: na daljinu; Katalog EUR: 350. Published HKO 6 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|T '
         'e rapija poremećaja glasa kod djece',
  'title': 'T e rapija poremećaja glasa kod djece',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 6 (4 sata nastave+2 sata '
             'samostalnog rada polaznika); Jezik: hrvatski; Izvođenje: klasično '
             '(uživo);na daljinu; Katalog EUR: 150. Published HKO 7 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|Interdisciplinarni '
         'pristup tretmanu poremećaja glasa kod djece',
  'title': 'Interdisciplinarni pristup tretmanu poremećaja glasa kod djece',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 5 sati i 15 minuta nastave; '
             'Jezik: hrvatski; Izvođenje: klasično (uživo); Katalog EUR: 180. '
             'Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|SENZORNA '
         'INTEGRACIJA - MODUL I',
  'title': 'SENZORNA INTEGRACIJA - MODUL I',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 14; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo);; Katalog EUR: 270. Published HKO 7 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|SENZORNA '
         'INTEGRACIJA - MODUL 2, Procjena i izrada individualiziranih programa '
         'poticanja senzorne integracije',
  'title': 'SENZORNA INTEGRACIJA - MODUL 2, Procjena i izrada individualiziranih '
           'programa poticanja senzorne integracije',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 20; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 335. Published HKO 7 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|SENZORNA '
         'INTEGRACIJA - MODUL 3, Pisanje nalaza i mišljenja, strategije poticanja i '
         'prilagodbe, preporuke za provedbu programa i savjetovanje',
  'title': 'SENZORNA INTEGRACIJA - MODUL 3, Pisanje nalaza i mišljenja, strategije '
           'poticanja i prilagodbe, preporuke za provedbu programa i savjetovanje',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 20; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 335. Published HKO 7 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|SENZORNA '
         'INTEGRACIJA - MODUL 4, SUPERVIZIJA',
  'title': 'SENZORNA INTEGRACIJA - MODUL 4, SUPERVIZIJA',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 20; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 335. Published HKO 7 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|SENZORNA '
         'INTEGRACIJA –STRUČNI MODUL 2, Senzorno integrativno poticanje kroz '
         'svakodnevne aktivnosti i oblikovanje okružja',
  'title': 'SENZORNA INTEGRACIJA –STRUČNI MODUL 2, Senzorno integrativno poticanje '
           'kroz svakodnevne aktivnosti i oblikovanje okružja',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 12; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 295. Published HKO 6 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/|SENZORNA '
         'INTEGRACIJA –STRUČNI MODUL 3, Supervizija',
  'title': 'SENZORNA INTEGRACIJA –STRUČNI MODUL 3, Supervizija',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/edukacijsko-rehabilitacijski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 14; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 310. Published HKO 7 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/ekonomski-fakultet/|FEB-UNIDU '
         'Summer Business Academy',
  'title': 'FEB-UNIDU Summer Business Academy',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/ekonomski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 150 sati (30 sati nastave + '
             '120 sati samostalnog rada); Jezik: engleski; Izvođenje: mješovito; '
             'Katalog EUR: 1.100. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/ekonomski-fakultet/|Digitalna '
         'transformacija poslovanja',
  'title': 'Digitalna transformacija poslovanja',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/ekonomski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 36 sati; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 2.000. Published HKO 6 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/ekonomski-fakultet/|FBA '
         'PODUZETNIŠTVO U EKONOMIJI ZNANJA (Fundamentals of Business Administration - '
         'Entrepreneurship in the knowledge economy)',
  'title': 'FBA PODUZETNIŠTVO U EKONOMIJI ZNANJA (Fundamentals of Business '
           'Administration - Entrepreneurship in the knowledge economy)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/ekonomski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: Ukupno 120 sati (od toga 20 '
             'uvodnih sati, 90 stručnih sati, te 10 praktičnih sati mentorskog rada); '
             'Jezik: hrvatski; Izvođenje: klasično (uživo); mješovito; Katalog EUR: '
             '1.600 EUR po polazniku. Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/ekonomski-fakultet/|Korporativno '
         'upravljanje za članove nadzornih i upravnih odbora',
  'title': 'Korporativno upravljanje za članove nadzornih i upravnih odbora',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/ekonomski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 40 sati nastave; Jezik: '
             'hrvatski; Izvođenje: klasično (uživo). Published HKO 6 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/ekonomski-fakultet/|Osposobljavanje '
         'internih auditora za kvalitetu i okoliš',
  'title': 'Osposobljavanje internih auditora za kvalitetu i okoliš',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/ekonomski-fakultet/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 18 sati nastave; Jezik: '
             'hrvatski; Izvođenje: klasično (uživo); na daljinu; Katalog EUR: 700. '
             'Published HKO nije primjenjivo (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-hrvatskih-studija/|Program '
         'za stjecanje temeljnih andragoških kompetencija',
  'title': 'Program za stjecanje temeljnih andragoških kompetencija',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-hrvatskih-studija/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 75 sati (32 sata nastave + 43 '
             'samostalnoga rada polaznika).; Jezik: hrvatski; Izvođenje: klasično '
             '(uživo); Katalog EUR: 480. Published HKO  (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-hrvatskih-studija/|Program '
         'stjecanja pedagoških kompetencija za strukovne učitelje i suradnike u '
         'nastavi',
  'title': 'Program stjecanja pedagoških kompetencija za strukovne učitelje i '
           'suradnike u nastavi',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-hrvatskih-studija/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 2100 sati (225 sati '
             'neposredne nastave i 1875 sati samostalnog rada polaznika). Nastava se '
             'održava 2 semestra; Jezik: hrvatski; Izvođenje: klasično (uživo). '
             'Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-hrvatskih-studija/|Međunarodna '
         'zimska i ljetna škola "Klimatske promjene i sigurnosni izazovi"',
  'title': 'Međunarodna zimska i ljetna škola "Klimatske promjene i sigurnosni '
           'izazovi"',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-hrvatskih-studija/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 180 ( nastava 58 sati + '
             'samostalan rad 122 sata ); Jezik: Hrvatski / i / ili / engleski; '
             'Izvođenje: Klasično + na daljinu. Published HKO 6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-hrvatskih-studija/|Pedagoško-psihološko-metodičko-didaktička '
         'naobrazba nastavnika',
  'title': 'Pedagoško-psihološko-metodičko-didaktička naobrazba nastavnika',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-hrvatskih-studija/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: Dva semestra, 358 sati '
             'nastave; Jezik: hrvatski; Izvođenje: klasično (uživo); Katalog EUR: '
             '955,60 (školarina) i 33,18 (upisnina). Published HKO 7 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-hrvatskih-studija/|Tečaj '
         'grčkog jezika',
  'title': 'Tečaj grčkog jezika',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-hrvatskih-studija/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 90 sati (30 sati seminara i '
             '30 sati vježbi + 30 sati samostalnog rada); Jezik: grčki; Izvođenje: '
             'klasično (uživo); Katalog EUR: Besplatan. Published HKO 5 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-hrvatskih-studija/|Hrvatski '
         'za neizvorne govornike 2',
  'title': 'Hrvatski za neizvorne govornike 2',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-hrvatskih-studija/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 15 sati predavanja, 30 sati '
             'vježbi i 15 sati seminara; Jezik: engleski; Izvođenje: klasično (uživo); '
             'Katalog EUR: Program je besplatan. Published HKO 5 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-hrvatskih-studija/|Hrvatski '
         'za neizvorne govornike 1',
  'title': 'Hrvatski za neizvorne govornike 1',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-hrvatskih-studija/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 15 sati predavanja, 30 sati '
             'vježbi i 15 sati seminara; Jezik: engleski; Izvođenje: klasično (uživo); '
             'Katalog EUR: Program je besplatan. Published HKO 5 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-hrvatskih-studija/|Mrežni '
         'jezični modul Nauči hrvatski!',
  'title': 'Mrežni jezični modul Nauči hrvatski!',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-hrvatskih-studija/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 30 sati samostalnoga rada; '
             'Jezik: engleski; Izvođenje: na daljinu; Katalog EUR: Besplatan. '
             'Published HKO 5 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-hrvatskih-studija/|Međunarodna '
         'ljetna škola latinskoga jezika i kulture',
  'title': 'Međunarodna ljetna škola latinskoga jezika i kulture',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-hrvatskih-studija/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 50 sati; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: Besplatno. Published HKO 6 '
             '(not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-organizacije-i-informatike/|Razlikovna '
         'godina diplomskih studija',
  'title': 'Razlikovna godina diplomskih studija',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-organizacije-i-informatike/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: Ovisno o broju razlikovnih '
             'kolegija i njihovom opsegu danom u ECTS-ima; Jezik: hrvatski; Izvođenje: '
             'klasično (uživo); Katalog EUR: Sukladno broju ECTS bodova propisanih '
             'razlikovnih kolegija.. Published HKO 6 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-organizacije-i-informatike/|Pedagoško-psihološko-didaktičko-metodičko '
         'obrazovanje nastavnika',
  'title': 'Pedagoško-psihološko-didaktičko-metodičko obrazovanje nastavnika',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-organizacije-i-informatike/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 1650 sati (355 sati nastave + '
             '1295 sati samostalnog rada); Jezik: hrvatski; Izvođenje: mješovito; '
             'Katalog EUR: 1.000. Published HKO 7 (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-organizacije-i-informatike/|Program '
         'stručnog usavršavanja u području javne nabave',
  'title': 'Program stručnog usavršavanja u području javne nabave',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-organizacije-i-informatike/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 8 sati; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 180. Published HKO 5 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-organizacije-i-informatike/|Specijalistički '
         'program izobrazbe u području javne nabave',
  'title': 'Specijalistički program izobrazbe u području javne nabave',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-organizacije-i-informatike/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 50 sati; Jezik: hrvatski; '
             'Izvođenje: klasično (uživo); Katalog EUR: 530. Published HKO 5 (not '
             'admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-organizacije-i-informatike/|Radionica '
         'za mentore na doktorskom studiju',
  'title': 'Radionica za mentore na doktorskom studiju',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/fakultet-organizacije-i-informatike/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Trajanje: 14 sati; Jezik: hrvatski; '
             'Izvođenje: mješovito; Katalog EUR: 400. Published HKO 7 (not admission '
             'level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/muzicka-akademija/|Program '
         'umjetničkog usavršavanja (elektronička glazba i izvođački studiji)',
  'title': 'Program umjetničkog usavršavanja (elektronička glazba i izvođački studiji)',
  'url': 'https://www.unizg.hr/o-sveucilistu/sveuciliste-jucer-danas-sutra/osiguravanje-kvalitete/cjelozivotno-obrazovanje/baza-programa-cjelozivotnog-obrazovanja/pregled-programa-czo-po-sastavnicama/muzicka-akademija/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': [],
  'summary': 'Educational catalogue overview; enrolment, entry requirements and '
             'current dates/prices unverified. Two tracks:40 electronic or32 '
             'instrumental/vocal/conducting mentored hours+about160 independent hours. '
             'Listed EUR1500–2300 by track/accompaniment, plus EUR67 entrance and140 '
             'final examination. Jezik: hrvatski; izvođenje: klasično (uživo). '
             'Published HKO  (not admission level).',
  'deadline': None,
  'proof': 'Own educational table; original programme block; published catalogue '
           'values and source conflicts retained.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2739',
  'title': 'voditelj ustrojstvene jedinice II., interni naziv: voditelj ispostave za '
           'knjigovodstveno-računovodstvene poslove u Središnjem sveučilišnom uredu za '
           'poslovanje , neodređeno vrijeme u punom radnom vremenu, jedan (1) '
           'izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2739',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Economics bachelor HKO6;10years relevant experience, accounting '
             'software, IT and English. Closing: 8 days from Narodne novine '
             'publication; legal counting/current availability unverified, so no '
             'absolute deadline. Salary unknown. NN br. 107., od 23. rujna 2026.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2739; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2738',
  'title': 'voditelj ustrojbene jedinice 3, interni naziv: rukovoditelj Odjela za '
           'financijsko-računovodstveno poslovanje Sveučilišta u Zagrebu u Središnjem '
           'sveučilišnom uredu za poslovanje , na određeno vrijeme (zamjena za '
           'privremeno nenazočnu zaposlenicu) u punom radnom vremenu, jedan (1) '
           'izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2738',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Economics accounting/finance master HKO7.1;5years experience, foreign '
             'language and excellent IT. Closing: 8 days from Narodne novine '
             'publication; legal counting/current availability unverified, so no '
             'absolute deadline. Salary unknown. NN br. 107, od 23. rujna 2026.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2738; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2737',
  'title': 'suradnik , interni naziv: stručni suradnik u Središnjem sveučilišnom uredu '
           'za poslovanje , na određeno vrijeme (zamjena za privremeno nenazočnu '
           'zaposlenicu) u punom radnom vremenu, jedan (1) izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2737',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Economics master HKO7.1;1year relevant experience, English and excellent '
             'IT; budget-accounting experience preferred. Closing: 8 days from Narodne '
             'novine publication; legal counting/current availability unverified, so '
             'no absolute deadline. Salary unknown. NN br. 107, od 23. rujna 2026.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2737; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2735',
  'title': 'viši savjetnik 2 , interni naziv: viši stručni savjetnik za studije i '
           'studente u Središnjem uredu za studije i upravljanje kvalitetom '
           'Sveučilišta u Zagrebu, na neodređeno vrijeme u punom radnom vremenu, jedan '
           '(1) izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2735',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Master HKO7.1;4years relevant experience, foreign language and IT. '
             'Closing: 8 days from Narodne novine publication; legal counting/current '
             'availability unverified, so no absolute deadline. Salary unknown. NN br. '
             '101, od 11. rujna 2026.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2735; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2734',
  'title': 'suradnik, interni naziv: projektni administrator na projektu UNIC, za rad '
           'na projektu Europsko sveučilište postindustrijskih gradova – UNIC u Uredu '
           'za EU projekte, u punom radnom vremenu, na određeno vrijeme do 30. rujna '
           '2027., jedan (1) izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2734',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Social/interdisciplinary-social master HKO7.1;1year administration, '
             'spoken/written English and MSOffice; EU-project training preferred. '
             'Closing: 8 days from Narodne novine publication; legal counting/current '
             'availability unverified, so no absolute deadline. Salary unknown. NN br. '
             '81, od 24. srpnja 2026.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2734; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2733',
  'title': 'viši savjetnik 2 u Središnjem sveučilišnom uredu za međunarodnu i '
           'međuinstitucijsku suradnju, na neodređeno vrijeme, u punom radnom vremenu, '
           '(2) izvršitelja (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2733',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Master HKO7.1;4years experience including1 similar, excellent English '
             'and IT; Erasmus experience preferred. EU-project funded. Closing: 8 days '
             'from Narodne novine publication; legal counting/current availability '
             'unverified, so no absolute deadline. Salary unknown. NN br. 72 od 8. '
             'srpnja 2026.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2733; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2728',
  'title': 'voditelj ustrojstvene jedinice 3 u Centru za istraživanje, inovacije i '
           'transfer tehnologije Sveučilišta u Zagrebu, na neodređeno vrijeme u punom '
           'radnom vremenu, jedan (1) izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2728',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Master HKO7.1;5years research/innovation/technology-transfer or '
             'commercialisation, team/project management, Croatian/English and '
             'excellent IT; doctorate preferred. Closing: 8 days from Narodne novine '
             'publication; legal counting/current availability unverified, so no '
             'absolute deadline. Salary unknown. NN broj 52/26 od 20.5.2026.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2728; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2727',
  'title': 'savjetnik u Centru za istraživanje, razvoj i transfer tehnologije (CIRIT) '
           'Sveučilišta u Zagrebu, na neodređeno vrijeme u punom radnom vremenu, jedan '
           '(1) izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2727',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Master HKO7.1;3years experience, active English and IT. Closing: 8 days '
             'from Narodne novine publication; legal counting/current availability '
             'unverified, so no absolute deadline. Salary unknown. NN, br. 49/26, od '
             '13. svibnja 2026.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2727; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2713',
  'title': 'radnik III. vrste, interni naziv: portir, na određeno vrijeme, do povratka '
           'na rad privremeno odsutnog zaposlenika, u punom radnom vremenu, jedan (1) '
           'izvršitelj (m/ž) .',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2713',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Secondary HKO4.1/4.2, relevant experience; temporary replacement.12/24 '
             'and12/48 shifts; employer reference preferred. Closing: 8 days from '
             'Narodne novine publication; legal counting/current availability '
             'unverified, so no absolute deadline. Salary unknown. NN br. 39/26 od '
             '15.4.2026.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2713; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2703',
  'title': 'voditelj ustrojstvene jedinice 2, interni naziv: koordinator za suradnju s '
           'knjižnicama Sveučilišta u Zagrebu, na neodređeno vrijeme u punom radnom '
           'vremenu, jedan (1) izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2703',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Master HKO7.1, library-adviser and research-associate titles;10years '
             'relevant experience including4management; English and IT. Closing: 8 '
             'days from Narodne novine publication; legal counting/current '
             'availability unverified, so no absolute deadline. Salary unknown. NN br. '
             '19, od 25. veljače 2026.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2703; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2701',
  'title': 'viši savjetnik 2, interni naziv: viši stručni savjetnik za upravljanje '
           'kvalitetom, na određeno vrijeme (do 30. rujna 2026.), s punim radnim '
           'vremenom, jedan (1) izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2701',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Social/humanities master HKO7.1;4years relevant experience, English, '
             'another foreign language and IT. Closing: 8 days from Narodne novine '
             'publication; legal counting/current availability unverified, so no '
             'absolute deadline. Salary unknown. NN br. 15, od 13. veljače 2026.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2701; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2699',
  'title': 'voditelj ustrojstvene jedinice 2, interni naziv: voditelj Ureda za pravne '
           'poslove , u punom radnom vremenu, na neodređeno vrijeme, jedan (1) '
           'izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2699',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Law master HKO7.1;10years relevant higher-education experience, English, '
             'IT and organisational/team skills. Closing: 8 days from Narodne novine '
             'publication; legal counting/current availability unverified, so no '
             'absolute deadline. Salary unknown. NN br. 12/2026, od 4. veljače 2026.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2699; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2698',
  'title': 'suradnik, interni naziv: stručni suradnik u Uredu rektora , u punom radnom '
           'vremenu, na neodređeno vrijeme, jedan (1) izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2698',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Humanities master HKO7.1;1year experience, excellent English and another '
             'foreign language, IT and communication/teamwork. Closing: 8 days from '
             'Narodne novine publication; legal counting/current availability '
             'unverified, so no absolute deadline. Salary unknown. NN br. 12/2026, od '
             '4. veljače 2026.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2698; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2697',
  'title': 'voditelj ustrojstvene jedinice 2, interni naziv: voditelj Središnjeg '
           'sveučilišnog ureda za multimedijsku produkciju i komunikaciju Sveučilišta '
           'u Zagrebu , na neodređeno vrijeme, s punim radnim vremenom, jedan (1) '
           'izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2697',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Humanities master HKO7.1;5years relevant experience, active English, '
             'excellent Croatian, communication/IT and higher-education experience. '
             'Closing: 8 days from Narodne novine publication; legal counting/current '
             'availability unverified, so no absolute deadline. Salary unknown. NN br. '
             '12/2026, od 4. veljače 2026.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2697; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2696',
  'title': 'voditelj ustrojstvene jedinice 4, interni naziv: rukovoditelj pododsjeka '
           'za obračun plaća, na neodređeno vrijeme, s punim radnim vremenom, jedan '
           '(1) izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2696',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Economics accounting/finance master HKO7.1;3years experience, foreign '
             'language and excellent IT. Closing: 8 days from Narodne novine '
             'publication; legal counting/current availability unverified, so no '
             'absolute deadline. Salary unknown. NN br. 12/2026, od 4. veljače 2026.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2696; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2692',
  'title': 'viši savjetnik, interni naziv: viši stručni savjetnik za transfer '
           'tehnologije, na neodređeno vrijeme u punom radnom vremenu, jedan (1) '
           'izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2692',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Master HKO7.1;4years relevant experience, excellent English and IT; '
             'doctorate/current doctoral study desirable. Closing: 8 days from Narodne '
             'novine publication; legal counting/current availability unverified, so '
             'no absolute deadline. Salary unknown. NN br. 8 /26, od 23. siječnja '
             '2026.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2692; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2691',
  'title': 'suradnik, interni naziv: stručni suradnik u Uredu za akademsko priznavanje '
           'inozemnih visokoškolskih kvalifikacija, na određeno vrijeme do povratka na '
           'rad privremeno nenazočne zaposlenice, u punom radnom vremenu, jedan (1) '
           'izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2691',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Social-science master HKO7.1;1year relevant experience, foreign language '
             'and IT. Closing: 8 days from Narodne novine publication; legal '
             'counting/current availability unverified, so no absolute deadline. '
             'Salary unknown. NN br 8/26, od 23. siječnja 2026. Zagreb, 12. siječnja '
             '2026.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2691; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2690',
  'title': 'radnik III. vrste, interni naziv: portir, na neodređeno vrijeme u punom '
           'radnom vremenu, jedan (1) izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2690',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Secondary HKO4.1/4.2;1year same/related experience; permanent full-time '
             'porter,12/24 and12/48 shifts. Closing: 8 days from Narodne novine '
             'publication; legal counting/current availability unverified, so no '
             'absolute deadline. Salary unknown. NN br. 8/26, od 23. siječnja 2026.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2690; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2686',
  'title': 'voditelj ustrojstvene jedinice 4 u Središnjem uredu za poslovanje '
           'Sveučilišta u Zagrebu , neodređeno vrijeme u punom radnom vremenu, jedan '
           '(1) izvršitelj (m/ž). Poslovi koji se obavljaju na ovom radnom mjestu po '
           'složenosti, odgovornostima i opsegu odgovaraju poslovima na razini '
           'voditelja računovodstva.',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2686',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Economics accounting/finance master HKO7.1;4years experience, foreign '
             'language and excellent IT. Closing: 15 days from Narodne novine '
             'publication; legal counting/current availability unverified, so no '
             'absolute deadline. Salary unknown. NN, br. 4/26, od 14. 1. 2026.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2686; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2682',
  'title': 'asistent (doktorand), na projektu Hrvatske zaklade za znanost „Ex vivo/in '
           'vitro model za procjenu učinkovitosti onkolitičke viroterapije (MOVE)“ '
           '(IP-2024-05-4740) https://www.oncovirlab.hr/ex-vivo-ucinkovitost/, na '
           'određeno vrijeme do 6 godina, u punom radnom vremenu u Centru za '
           'istraživanje i prijenos znanja u biotehnologiji Sveučilišta u Zagrebu, '
           'jedan (1) izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2682',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Relevant natural/biomedical/biotechnical master; GPA STRICTLY>3.5, '
             'biology doctoral enrolment;6-year contract. Non-native Croatian B2 and '
             'foreign-qualification recognition apply; English/research experience '
             'preferred. Closing: 30 days from Narodne novine publication; legal '
             'counting/current availability unverified, so no absolute deadline. '
             'Salary unknown. NN broj 154/2025 od 19.12.2025.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2682; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2679',
  'title': 'savjetnik , interni naziv: stručni savjetnik za poslove nabave u '
           'Središnjem uredu za razvoj, investicije i prostorno planiranje, neodređeno '
           'vrijeme u punom radnom vremenu, jedan (1) izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2679',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Master HKO7.1;3years relevant experience, foreign language and IT; '
             'public-procurement experience preferred. Closing: 8 days from Narodne '
             'novine publication; legal counting/current availability unverified, so '
             'no absolute deadline. Salary unknown. NN broj 153/2025 od 17. prosinca '
             '2025.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2679; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2678',
  'title': 'voditelj ustrojstvene jedinice III. vrste, interni naziv: tajnica , '
           'neodređeno vrijeme u punom radnom vremenu, jedan (1) izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2678',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Secondary HKO4.1/4.2;same/related experience and IT. Closing: 8 days '
             'from Narodne novine publication; legal counting/current availability '
             'unverified, so no absolute deadline. Salary unknown. NN broj 153/2025 od '
             '17. prosinca 2025.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2678; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2670',
  'title': 'savjetnik za financijsko-računovodstvene poslove u Središnjem sveučilišnom '
           'uredu za međunarodnu i međuinstitucijsku suradnju Sveučilišta u Zagrebu, '
           'na određeno vrijeme do dvije godine, s punim radnim vremenom, jedan '
           'izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2670',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Master in social/humanities/technical/natural-mathematical field;3years '
             'experience, spreadsheets/databases and English; finance/project '
             'experience preferred. Closing: 8 days from Narodne novine publication; '
             'legal counting/current availability unverified, so no absolute deadline. '
             'Salary unknown. NN 141/25 od 19.11.2025.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2670; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2669',
  'title': 'voditelj ustrojstvene jedinice 4 , na neodređeno vrijeme u punom radnom '
           'vremenu, jedan (1) izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2669',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Master HKO7.1;4years legal experience, passed bar examination, active '
             'English and IT. Closing: 8 days from Narodne novine publication; legal '
             'counting/current availability unverified, so no absolute deadline. '
             'Salary unknown. NN br. 139/25, od 12. 11. 2025.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2669; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2665',
  'title': '– docent, u znanstvenom području: humanističke znanosti , znanstvenom '
           'polju: 6.02 teologija, na neodređeno vrijeme, s punim radnim vremenom, u '
           'Centru za protestantsku teologiju Matija Vlačić Ilirik Sveučilišta u '
           'Zagrebu, jedan izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2665',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Theology assistant-professor appointment; statutory/University '
             'academic-election criteria and foreign-qualification recognition apply; '
             'foreign nationals must provide Croatian C2 proof. Closing: 30 days from '
             'Narodne novine publication; legal counting/current availability '
             'unverified, so no absolute deadline. Salary unknown. NN br. 136/2025 od '
             '5.11.2025.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2665; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2664',
  'title': 'voditelj ustrojstvene jedinice III. vrste , interni naziv: portir/domar, '
           'na neodređeno vrijeme, u punom radnom vremenu, jedan (1) izvršitelj (m/ž)',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2664',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Secondary education;1year same/related experience; permanent full-time '
             'porter/caretaker. Closing: 8 days from Narodne novine publication; legal '
             'counting/current availability unverified, so no absolute deadline. '
             'Salary unknown. NN br. 136/2025 od 5.11.2025.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2664; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2649',
  'title': 'specijalist za reviziju, interni naziv: viši unutarnji revizor u Uredu za '
           'unutarnju reviziju Sveučilišta u Zagrebu, na neodređeno vrijeme, s punim '
           'radnim vremenom, jedan (1) izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2649',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Economics/law master;state examination, internal-auditor authorisation '
             'or conditional1–2year qualification timetable;4years relevant '
             'experience, foreign language and IT. Closing: 8 days from Narodne novine '
             'publication; legal counting/current availability unverified, so no '
             'absolute deadline. Salary unknown. NN 118/2025 od 5.9.2025.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2649; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2648',
  'title': 'viši savjetnik 2, interni naziv: viši stručni savjetnik za projekte u '
           'Središnjem uredu za razvoj, investicije i prostorno planiranje, na '
           'neodređeno vrijeme, s punim radnim vremenom, jedan (1) izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2648',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Master HKO7.1;4years relevant experience, English and IT; '
             'procurement/business-information-system experience preferred. Closing: 8 '
             'days from Narodne novine publication; legal counting/current '
             'availability unverified, so no absolute deadline. Salary unknown. NN '
             '118/2025 od 5.9.2025.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2648; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2647',
  'title': 'tehnički suradnik, interni naziv: tehničar za audiovizualnu opremu, na '
           'određeno vrijeme do 3 godine, s punim radnim vremenom, jedan (1) '
           'izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2647',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Secondary/general-high-school education;MORE THAN5years relevant '
             'experience, English and IT;fixed term up to3years. Closing: 8 days from '
             'Narodne novine publication; legal counting/current availability '
             'unverified, so no absolute deadline. Salary unknown. NN 118/2025 od '
             '5.9.2025.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2647; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2646',
  'title': 'suradnik, interni naziv: stručni suradnik, na određeno vrijeme do povratka '
           'privremeno nenazočnog zaposlenika, s punim radnim vremenom, u kadrovskoj '
           'službi, jedan (1) izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2646',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Master HKO7.1;1year higher-education experience, active English and IT; '
             'temporary HR-service replacement. Closing: 8 days from Narodne novine '
             'publication; legal counting/current availability unverified, so no '
             'absolute deadline. Salary unknown. NN 118/2025 od 5.9.2025.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2646; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2644',
  'title': 'viši savjetnik 2, interni naziv: viši stručni savjetnik za obavljanje '
           'poslova Studentskog zbora, na neodređeno vrijeme, s punim radnim vremenom, '
           'jedan (1) izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2644',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Master HKO7.1;5years relevant experience, foreign language and IT. '
             'Closing: 8 days from Narodne novine publication; legal counting/current '
             'availability unverified, so no absolute deadline. Salary unknown. NN '
             '118/2025 od 5.9.2025.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2644; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2642',
  'title': 'suradnik, interni naziv: stručni suradnik za međunarodnu suradnju u '
           'Središnjem sveučilišnom uredu za međunarodnu i međuinstitucionalnu '
           'suradnju Sveučilišta u Zagrebu , na neodređeno vrijeme, s punim radnim '
           'vremenom, jedan (1) izvršitelj (m/ž).',
  'url': 'https://www.unizg.hr/o-sveucilistu/dokumenti-i-javnost-informacija/natjecaji/rektorat-natjecaji/#c2642',
  'category': 'jobs',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Economics master HKO7.1;1year relevant experience, excellent English and '
             'IT. Closing: 8 days from Narodne novine publication; legal '
             'counting/current availability unverified, so no absolute deadline. '
             'Salary unknown. NN br. 104/2025. od 18.7.2025.',
  'deadline': None,
  'proof': 'Own Rectorate notice c2642; actual role/CLASA/NN trigger; latest-role '
           'scope, not inferred legal replacement.'},
 {'key': 'https://www.unizg.hr/studiji-i-studiranje/upisi-stipendije-priznavanja/stipendije/|category-A',
  'title': 'STIPENDIJE ZA IZVRSNOST',
  'url': 'https://www.unizg.hr/studiji-i-studiranje/upisi-stipendije-priznavanja/stipendije/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': [],
  'summary': '2025/26 full-time Zagreb students; EUR2325/year (10×232.50), one '
             'category, no other public scholarship except Erasmus+. Top10% and '
             'GPA≥4;55ECTS per year, not first bachelor/integrated year. Closing '
             'Jan16,2026 date-only, own results corroborate late2025 typo; ECTS '
             'table2026 conflicts with general/scoring2025. Affiliation, not passport.',
  'deadline': '2026-01-16',
  'proof': 'Original category A; complete call and own results; conflicting dates '
           'retained.'},
 {'key': 'https://www.unizg.hr/studiji-i-studiranje/upisi-stipendije-priznavanja/stipendije/|category-B',
  'title': 'STIPENDIJE ZA IZVRSNE STUDENTE S OSTVARENIM POSEBNIM ZNANSTVENIM I '
           'STRUČNIM POSTIGNUĆIMA',
  'url': 'https://www.unizg.hr/studiji-i-studiranje/upisi-stipendije-priznavanja/stipendije/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': [],
  'summary': '2025/26 full-time Zagreb students; EUR2325/year (10×232.50), one '
             'category, no other public scholarship except Erasmus+. GPA≥4/55ECTS per '
             'year and documented exceptional scientific or professional work; not '
             'first bachelor/integrated year. Closing Jan16,2026 date-only, own '
             'results corroborate late2025 typo; ECTS table2026 conflicts with '
             'general/scoring2025. Affiliation, not passport.',
  'deadline': '2026-01-16',
  'proof': 'Original category B; complete call and own results; conflicting dates '
           'retained.'},
 {'key': 'https://www.unizg.hr/studiji-i-studiranje/upisi-stipendije-priznavanja/stipendije/|category-C',
  'title': 'STIPENDIJE ZA IZVRSNE STUDENTE S OSTVARENIM POSEBNIM POSTIGNUĆIMA U '
           'UMJETNIČKOM PODRUČJU',
  'url': 'https://www.unizg.hr/studiji-i-studiranje/upisi-stipendije-priznavanja/stipendije/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': [],
  'summary': '2025/26 full-time Zagreb students; EUR2325/year (10×232.50), one '
             'category, no other public scholarship except Erasmus+. GPA≥4/55ECTS per '
             'year; three art academies, Design study and Institute for Church Music; '
             'artistic awards/recognition required; not first bachelor/integrated '
             'year. Closing Jan16,2026 date-only, own results corroborate late2025 '
             'typo; ECTS table2026 conflicts with general/scoring2025. Affiliation, '
             'not passport.',
  'deadline': '2026-01-16',
  'proof': 'Original category C; complete call and own results; conflicting dates '
           'retained.'},
 {'key': 'https://www.unizg.hr/studiji-i-studiranje/upisi-stipendije-priznavanja/stipendije/|category-D',
  'title': 'STIPENDIJE PODZASTUPLJENIM I RANJIVIM SKUPINAMA STUDENATA',
  'url': 'https://www.unizg.hr/studiji-i-studiranje/upisi-stipendije-priznavanja/stipendije/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': [],
  'summary': '2025/26 full-time Zagreb students; EUR2325/year (10×232.50), one '
             'category, no other public scholarship except Erasmus+. GPA≥3/35ECTS per '
             'year; first-year school GPA≥3.5. Household minimum social benefit OR≥60% '
             'bodily disability/functional grade≥2 OR children/alternative care. '
             'Closing Jan16,2026 date-only, own results corroborate late2025 typo; '
             'ECTS table2026 conflicts with general/scoring2025. Affiliation, not '
             'passport.',
  'deadline': '2026-01-16',
  'proof': 'Original category D; complete call and own results; conflicting dates '
           'retained.'},
 {'key': 'https://www.unizg.hr/studiji-i-studiranje/upisi-stipendije-priznavanja/stipendije/|category-E',
  'title': 'STIPENDIJE ZA IZVRSNE STUDENTE SPORTAŠE',
  'url': 'https://www.unizg.hr/studiji-i-studiranje/upisi-stipendije-priznavanja/stipendije/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': [],
  'summary': '2025/26 full-time Zagreb students; EUR2325/year (10×232.50), one '
             'category, no other public scholarship except Erasmus+. Categorised '
             'Olympic/Paralympic/Deaflympic athletes; GPA≥4/55ECTS per year, not first '
             'bachelor/integrated year. Closing Jan16,2026 date-only, own results '
             'corroborate late2025 typo; ECTS table2026 conflicts with '
             'general/scoring2025. Affiliation, not passport.',
  'deadline': '2026-01-16',
  'proof': 'Original category E; complete call and own results; conflicting dates '
           'retained.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/novo-erasmus-natjecaj-za-kratku-mobilnost-doktorskih-studenata-za-razdoblje-16112026-3006202/|SMS',
  'title': 'NOVO: Erasmus+ Natječaj za kratku mobilnost doktorskih studenata za '
           'razdoblje 16.11.2026.-30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/novo-erasmus-natjecaj-za-kratku-mobilnost-doktorskih-studenata-za-razdoblje-16112026-3006202/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': [],
  'summary': 'Doctoral Zagreb students;5–30 physical days Nov16,2026–Jun30,2027. Study '
             'agreement and B2; partner HEI. EUR79/day1–14,56/day15–30; conditional '
             'travel and100/150 fewer-opportunity supplement. No UK/CH/home or '
             'study-residence destination.12months/half-programme cumulative cap; '
             'sequential, not concurrent. Jan31,2027 midnight unzoned conflicts with '
             'one HTML2026 clause; PDF corroborates2027. November starts need '
             'mid-October application.60places/wholeEUR130000 may close earlier.',
  'deadline': '2027-01-31',
  'proof': 'Original own notice student-latest-1; reviewed call and its integral '
           'literal materials; track SMS.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/novo-erasmus-natjecaj-za-kratku-mobilnost-doktorskih-studenata-za-razdoblje-16112026-3006202/|SMT',
  'title': 'NOVO: Erasmus+ Natječaj za kratku mobilnost doktorskih studenata za '
           'razdoblje 16.11.2026.-30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/novo-erasmus-natjecaj-za-kratku-mobilnost-doktorskih-studenata-za-razdoblje-16112026-3006202/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': [],
  'summary': 'Doctoral Zagreb students;5–30 physical days Nov16,2026–Jun30,2027. '
             'Traineeship B1; graduates apply/selected/LA before finishing and '
             'complete within1year AND Jun30,2027; France graduate traineeship '
             'excluded. EUR79/day1–14,56/day15–30; conditional travel and100/150 '
             'fewer-opportunity supplement. No UK/CH/home or study-residence '
             'destination.12months/half-programme cumulative cap; sequential, not '
             'concurrent. Jan31,2027 midnight unzoned conflicts with one HTML2026 '
             'clause; PDF corroborates2027. November starts need mid-October '
             'application.60places/wholeEUR130000 may close earlier.',
  'deadline': '2027-01-31',
  'proof': 'Original own notice student-latest-1; reviewed call and its integral '
           'literal materials; track SMT.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/1-krug-natjecaja-za-mobilnost-studenata-ka131-erasmus-strucna-praksa-ak-god-202627/|SMT',
  'title': '1. KRUG Natječaja za mobilnost studenata - KA131, Erasmus+ stručna praksa, '
           'ak. god. 2026./27.',
  'url': 'https://www.unizg.hr/nc/vijest/article/1-krug-natjecaja-za-mobilnost-studenata-ka131-erasmus-strucna-praksa-ak-god-202627/',
  'category': 'internships',
  'kind': 'opportunity',
  'hosts': [],
  'summary': 'Zagreb all-level full/part-time students, B1; graduates '
             'apply/selected/LA before graduation, complete within1year; France '
             'graduate exclusion.2months minimum;650/700EUR per month ALREADY '
             'includes150 traineeship extra, conditional250 and travel. Home fees '
             'continue; no UK/CH/home/residence/EU-body host or doubleEUfunding. '
             'Starts Sep14,2026–Jul30,2027; finish Sep30.12/24level cap. '
             'June15/July17/Sept4 processing windows, final Sept4noon '
             'unzoned;160capacity may close earlier.',
  'deadline': '2026-09-04',
  'proof': 'Original own notice student-latest-3; reviewed call and its integral '
           'literal materials; track SMT.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-strucnu-praksu-medunarodno-otvaranje-trece-z-1/|SMT',
  'title': 'Erasmus+ KA131 natječaj za mobilnost studenata za stručnu praksu: '
           'međunarodno otvaranje, treće zemlje',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-strucnu-praksu-medunarodno-otvaranje-trece-z-1/',
  'category': 'internships',
  'kind': 'opportunity',
  'hosts': [],
  'summary': 'Zagreb all-level full/part-time students, B1/approved host/coordinator; '
             'graduates apply and signLA before graduation; France '
             'exception.2–10months May25,2026–Jun21,2027.700EUR/month ALREADY '
             'includes150extra, conditional250/travel. No home/residence host, '
             'doubleEUfunding; work-permit rules apply. Jun15,2026 noon unzoned has '
             'erroneous weekday;2025/26 footer conflicts with2026/27 body. Own441.44 '
             'income cutoff contradicts85%base; eligibility requires clarification.',
  'deadline': '2026-06-15',
  'proof': 'Original own notice student-latest-4; reviewed call and its integral '
           'literal materials; track SMT.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-natjecaj-za-mobilnost-studenata-za-studijski-boravak-drzave-clanice-eu-a-i-trece-zem-1/|SMS',
  'title': 'Erasmus+ natječaj za mobilnost studenata za studijski boravak - države '
           'članice EU-a i treće zemlje pridružene programu ak. god. 2026./27. (zimski '
           'i ljetni semestar)',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-natjecaj-za-mobilnost-studenata-za-studijski-boravak-drzave-clanice-eu-a-i-trece-zem-1/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': [],
  'summary': 'Zagreb students/faculty host agreement and full-time study at host even '
             'if part-time at home; B2, military partner B1/passport only '
             'conditionally. First master semester excluded.2–12months '
             'Jun1,2026–Sep30,2027.500/550EUR/month, conditional250/travel/zero-grant; '
             'host tuition waived, home fees continue, no residence '
             'destination/doubleEUfunding. Up to3 ranked host choices in ONE '
             'application, not3 awards. Mar5,2026 noon unzoned.',
  'deadline': '2026-03-05',
  'proof': 'Original own notice student-latest-6; reviewed call and its integral '
           'literal materials; track SMS.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/|AGR|Universidad '
         'de la Frontera|MA/PhD agriculture/forestry',
  'title': 'Erasmus+ KA131 natječaj za mobilnost studenata za studijski boravak: '
           'međunarodno otvaranje – treće zemlje: AGR, EFZG, FER, FFZG, FHS, FSB, '
           '2026./27.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['CL'],
  'summary': 'AGR Zagreb MA/PhD agriculture/forestry; Universidad de la Frontera, '
             '4months/2places. March–July2027. B2, enrolled throughout; residence '
             'excluded. EUR700/month+conditional travel/250support; host waiver/home '
             'fees continue. Apr20,2026 noon unzoned vs laterPDF2025. PDF/appendix '
             'endJul2,2027 vsHTMLJul5 unresolved.',
  'deadline': '2026-04-20',
  'proof': 'Original own notice student-latest-5; reviewed call and its integral '
           'literal materials; track AGR|Universidad de la Frontera|MA/PhD '
           'agriculture/forestry.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/|EFZG|Shanghai '
         'University of International Business and Economics|BA/MA business0410',
  'title': 'Erasmus+ KA131 natječaj za mobilnost studenata za studijski boravak: '
           'međunarodno otvaranje – treće zemlje: AGR, EFZG, FER, FFZG, FHS, FSB, '
           '2026./27.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['CN'],
  'summary': 'EFZG Zagreb BA/MA business0410; Shanghai University of International '
             'Business and Economics, 5months/2places. winter2026/27 ONLY. B2, '
             'enrolled throughout; residence excluded. EUR700/month+conditional '
             'travel/250support; host waiver/home fees continue. Apr20,2026 noon '
             'unzoned vs laterPDF2025. PDF/appendix endJul2,2027 vsHTMLJul5 '
             'unresolved.',
  'deadline': '2026-04-20',
  'proof': 'Original own notice student-latest-5; reviewed call and its integral '
           'literal materials; track EFZG|Shanghai University of International '
           'Business and Economics|BA/MA business0410.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/|EFZG|Lucerne '
         'University of Applied Sciences and Arts|BA/MA business0410',
  'title': 'Erasmus+ KA131 natječaj za mobilnost studenata za studijski boravak: '
           'međunarodno otvaranje – treće zemlje: AGR, EFZG, FER, FFZG, FHS, FSB, '
           '2026./27.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['CH'],
  'summary': 'EFZG Zagreb BA/MA business0410; Lucerne University of Applied Sciences '
             'and Arts, 5months/2places. winter OR summer2026/27. B2, enrolled '
             'throughout; residence excluded. EUR550/month+conditional '
             'travel/250support; host waiver/home fees continue. Apr20,2026 noon '
             'unzoned vs laterPDF2025. PDF/appendix endJul2,2027 vsHTMLJul5 '
             'unresolved.',
  'deadline': '2026-04-20',
  'proof': 'Original own notice student-latest-5; reviewed call and its integral '
           'literal materials; track EFZG|Lucerne University of Applied Sciences and '
           'Arts|BA/MA business0410.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/|EFZG|University '
         'of the West of England|BA business0410',
  'title': 'Erasmus+ KA131 natječaj za mobilnost studenata za studijski boravak: '
           'međunarodno otvaranje – treće zemlje: AGR, EFZG, FER, FFZG, FHS, FSB, '
           '2026./27.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['GB'],
  'summary': 'EFZG Zagreb BA business0410; University of the West of England, '
             '5months/4places. spring2027 ONLY. B2, enrolled throughout; residence '
             'excluded. EUR550/month+conditional travel/250support; host waiver/home '
             'fees continue. Apr20,2026 noon unzoned vs laterPDF2025. PDF/appendix '
             'endJul2,2027 vsHTMLJul5 unresolved.',
  'deadline': '2026-04-20',
  'proof': 'Original own notice student-latest-5; reviewed call and its integral '
           'literal materials; track EFZG|University of the West of England|BA '
           'business0410.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/|EFZG|Manchester '
         'Metropolitan University|BA/MA business0410',
  'title': 'Erasmus+ KA131 natječaj za mobilnost studenata za studijski boravak: '
           'međunarodno otvaranje – treće zemlje: AGR, EFZG, FER, FFZG, FHS, FSB, '
           '2026./27.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['GB'],
  'summary': 'EFZG Zagreb BA/MA business0410; Manchester Metropolitan University, '
             '5months/4places. spring2027 ONLY. B2, enrolled throughout; residence '
             'excluded. EUR550/month+conditional travel/250support; host waiver/home '
             'fees continue. Apr20,2026 noon unzoned vs laterPDF2025. PDF/appendix '
             'endJul2,2027 vsHTMLJul5 unresolved.',
  'deadline': '2026-04-20',
  'proof': 'Original own notice student-latest-5; reviewed call and its integral '
           'literal materials; track EFZG|Manchester Metropolitan University|BA/MA '
           'business0410.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/|FER|Instituto '
         'Tecnológico y de Estudios Superiores de Monterrey|BA/MA '
         'computing/electrical0612/0613/0713/0714',
  'title': 'Erasmus+ KA131 natječaj za mobilnost studenata za studijski boravak: '
           'međunarodno otvaranje – treće zemlje: AGR, EFZG, FER, FFZG, FHS, FSB, '
           '2026./27.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['MX'],
  'summary': 'FER Zagreb BA/MA computing/electrical0612/0613/0713/0714; Instituto '
             'Tecnológico y de Estudios Superiores de Monterrey, 5months/1places. '
             'spring2027 ONLY. B2, enrolled throughout; residence excluded. '
             'EUR700/month+conditional travel/250support; host waiver/home fees '
             'continue. Apr20,2026 noon unzoned vs laterPDF2025. PDF/appendix '
             'endJul2,2027 vsHTMLJul5 unresolved. FER omitted inPDFbody; appendix+HTML '
             'confirm eligibility.',
  'deadline': '2026-04-20',
  'proof': 'Original own notice student-latest-5; reviewed call and its integral '
           'literal materials; track FER|Instituto Tecnológico y de Estudios '
           'Superiores de Monterrey|BA/MA computing/electrical0612/0613/0713/0714.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/|FHS|Sveučilište '
         'u Mostaru|all levels history/Croatian/psychology/journalism/information',
  'title': 'Erasmus+ KA131 natječaj za mobilnost studenata za studijski boravak: '
           'međunarodno otvaranje – treće zemlje: AGR, EFZG, FER, FFZG, FHS, FSB, '
           '2026./27.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['BA'],
  'summary': 'FHS Zagreb all levels '
             'history/Croatian/psychology/journalism/information; Sveučilište u '
             'Mostaru, 5months/4places. winter2026/27; MA/PhD research-only timing '
             'flexible. B2, enrolled throughout; residence excluded. '
             'EUR700/month+conditional travel/250support; host waiver/home fees '
             'continue. Apr20,2026 noon unzoned vs laterPDF2025. PDF/appendix '
             'endJul2,2027 vsHTMLJul5 unresolved.',
  'deadline': '2026-04-20',
  'proof': 'Original own notice student-latest-5; reviewed call and its integral '
           'literal materials; track FHS|Sveučilište u Mostaru|all levels '
           'history/Croatian/psychology/journalism/information.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/|FSB|Universidade '
         'de São Paulo - EESC|MA/PhD engineering',
  'title': 'Erasmus+ KA131 natječaj za mobilnost studenata za studijski boravak: '
           'međunarodno otvaranje – treće zemlje: AGR, EFZG, FER, FFZG, FHS, FSB, '
           '2026./27.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['BR'],
  'summary': 'FSB Zagreb MA/PhD engineering; Universidade de São Paulo - EESC, '
             '5months/2places. Aug–Dec2026 ONLY. B2, enrolled throughout; residence '
             'excluded. EUR700/month+conditional travel/250support; host waiver/home '
             'fees continue. Apr20,2026 noon unzoned vs laterPDF2025. PDF/appendix '
             'endJul2,2027 vsHTMLJul5 unresolved.',
  'deadline': '2026-04-20',
  'proof': 'Original own notice student-latest-5; reviewed call and its integral '
           'literal materials; track FSB|Universidade de São Paulo - EESC|MA/PhD '
           'engineering.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/|FSB|Pontificia '
         'Universidad Católica de Valparaíso|MA/PhD engineering',
  'title': 'Erasmus+ KA131 natječaj za mobilnost studenata za studijski boravak: '
           'međunarodno otvaranje – treće zemlje: AGR, EFZG, FER, FFZG, FHS, FSB, '
           '2026./27.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['CL'],
  'summary': 'FSB Zagreb MA/PhD engineering; Pontificia Universidad Católica de '
             'Valparaíso, 5months/2places. Aug–Dec2026; research-only timing flexible. '
             'B2, enrolled throughout; residence excluded. EUR700/month+conditional '
             'travel/250support; host waiver/home fees continue. Apr20,2026 noon '
             'unzoned vs laterPDF2025. PDF/appendix endJul2,2027 vsHTMLJul5 '
             'unresolved.',
  'deadline': '2026-04-20',
  'proof': 'Original own notice student-latest-5; reviewed call and its integral '
           'literal materials; track FSB|Pontificia Universidad Católica de '
           'Valparaíso|MA/PhD engineering.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/|FSB|Instituto '
         'Tecnológico y de Estudios Superiores de Monterrey|BA/MA engineering',
  'title': 'Erasmus+ KA131 natječaj za mobilnost studenata za studijski boravak: '
           'međunarodno otvaranje – treće zemlje: AGR, EFZG, FER, FFZG, FHS, FSB, '
           '2026./27.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['MX'],
  'summary': 'FSB Zagreb BA/MA engineering; Instituto Tecnológico y de Estudios '
             'Superiores de Monterrey, 5months/1places. spring2027 ONLY. B2, enrolled '
             'throughout; residence excluded. EUR700/month+conditional '
             'travel/250support; host waiver/home fees continue. Apr20,2026 noon '
             'unzoned vs laterPDF2025. PDF/appendix endJul2,2027 vsHTMLJul5 '
             'unresolved.',
  'deadline': '2026-04-20',
  'proof': 'Original own notice student-latest-5; reviewed call and its integral '
           'literal materials; track FSB|Instituto Tecnológico y de Estudios '
           'Superiores de Monterrey|BA/MA engineering.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/|FFZG|Univerzitet '
         'u Sarajevu|all levels Turkish language/literature',
  'title': 'Erasmus+ KA131 natječaj za mobilnost studenata za studijski boravak: '
           'međunarodno otvaranje – treće zemlje: AGR, EFZG, FER, FFZG, FHS, FSB, '
           '2026./27.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['BA'],
  'summary': 'FFZG Zagreb all levels Turkish language/literature; Univerzitet u '
             'Sarajevu, 5months/2places. winter2026/27; research-only timing flexible. '
             'B2, enrolled throughout; residence excluded. EUR700/month+conditional '
             'travel/250support; host waiver/home fees continue. Apr20,2026 noon '
             'unzoned vs laterPDF2025. PDF/appendix endJul2,2027 vsHTMLJul5 '
             'unresolved.',
  'deadline': '2026-04-20',
  'proof': 'Original own notice student-latest-5; reviewed call and its integral '
           'literal materials; track FFZG|Univerzitet u Sarajevu|all levels Turkish '
           'language/literature.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/|FFZG|Universidade '
         'de São Paulo|MA/PhD Portuguese language/literature',
  'title': 'Erasmus+ KA131 natječaj za mobilnost studenata za studijski boravak: '
           'međunarodno otvaranje – treće zemlje: AGR, EFZG, FER, FFZG, FHS, FSB, '
           '2026./27.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-natjecaj-za-mobilnost-studenata-za-studijski-boravak-medunarodno-otvaranje-tre-1/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['BR'],
  'summary': 'FFZG Zagreb MA/PhD Portuguese language/literature; Universidade de São '
             'Paulo, 5months/2places. Aug–Dec2026 ONLY. B2, enrolled throughout; '
             'residence excluded. EUR700/month+conditional travel/250support; host '
             'waiver/home fees continue. Apr20,2026 noon unzoned vs laterPDF2025. '
             'PDF/appendix endJul2,2027 vsHTMLJul5 unresolved.',
  'deadline': '2026-04-20',
  'proof': 'Original own notice student-latest-5; reviewed call and its integral '
           'literal materials; track FFZG|Universidade de São Paulo|MA/PhD Portuguese '
           'language/literature.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/01_Natjecaj_Macquarie_University_2026_2027_zimski.pdf',
  'title': 'Natječaj za stipendiju – Macquarie University (Australija), za zimski '
           'semestar akademske godine 2026./2027. (srpanj – studeni)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/01_Natjecaj_Macquarie_University_2026_2027_zimski.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['AU'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA/MA≥2semesters, English. EUR800/month+EUR1000travel refund and host '
             'tuition waiver; home tuition continues if applicable. Housing assistance '
             'is not free lodging; host final acceptance required. Indicative semester '
             'dates follow host calendar. Up to3 bilateral applications with '
             'priorities, not3guaranteed awards.',
  'deadline': '2026-02-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/02_Natjecaj_Macquarie_University_2026_2027_ljetni.pdf',
  'title': 'Natječaj za stipendiju – Macquarie University (Australija), za ljetni '
           'semestar akademske godine 2026./2027. veljača – lipanj)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/02_Natjecaj_Macquarie_University_2026_2027_ljetni.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['AU'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA/MA≥2semesters, English. EUR800/month+EUR1000travel refund and host '
             'tuition waiver; home tuition continues if applicable. Housing assistance '
             'is not free lodging; host final acceptance required. Indicative semester '
             'dates follow host calendar. Up to3 bilateral applications with '
             'priorities, not3guaranteed awards.',
  'deadline': '2026-03-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/05_Natjecaj_Universidade_Federal_do_Parana_2026_2027_zimski.pdf',
  'title': 'Natječaj za stipendiju – Universidade Federal do Paraná (Brazil), za '
           'zimski semestar akademske godine 2026./2027. (kolovoz – studeni)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/05_Natjecaj_Universidade_Federal_do_Parana_2026_2027_zimski.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['BR'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA/MA≥2semesters, English AND Portuguese. EUR640/month+EUR1000travel '
             'refund and host tuition waiver; home tuition continues if applicable. '
             'Housing assistance is not free lodging; host final acceptance required. '
             'Indicative semester dates follow host calendar. Up to3 bilateral '
             'applications with priorities, not3guaranteed awards.',
  'deadline': '2026-02-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/06_Natjecaj_Universidade_Federal_do_Parana_2026_2027_ljetni.pdf',
  'title': 'Natječaj za stipendiju – Universidade Federal do Paraná (Brazil), za '
           'ljetni semestar akademske godine 2026./2027. (ožujak – lipanj)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/06_Natjecaj_Universidade_Federal_do_Parana_2026_2027_ljetni.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['BR'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA/MA≥2semesters, English AND Portuguese. EUR640/month+EUR1000travel '
             'refund and host tuition waiver; home tuition continues if applicable. '
             'Housing assistance is not free lodging; host final acceptance required. '
             'Indicative semester dates follow host calendar. Up to3 bilateral '
             'applications with priorities, not3guaranteed awards.',
  'deadline': '2026-03-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/07_Natjecaj_Pontificia_Universidad_Catolica_de_Chile_2026_2027.pdf',
  'title': 'Natječaj za stipendiju – Pontificia Universidad Católica de Chile (Čile), '
           'za zimski ili ljetni semestar akademske godine 2026./2027. (kolovoz – '
           'prosinac / ožujak srpanj)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/07_Natjecaj_Pontificia_Universidad_Catolica_de_Chile_2026_2027.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['CL'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA≥2/MA≥1semester, SpanishB2; host programme exclusions. '
             'EUR640/month+EUR1000travel refund and host tuition waiver; home tuition '
             'continues if applicable. Housing assistance is not free lodging; host '
             'final acceptance required. Indicative semester dates follow host '
             'calendar. Up to3 bilateral applications with priorities, not3guaranteed '
             'awards.',
  'deadline': '2026-03-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/08_Natjecaj_Universidad_de_Chile_2026_2027_zimski.pdf',
  'title': 'Natječaj za stipendiju – Universidad de Chile (Čile), za zimski semestar '
           'akademske godine 2026./2027. (kolovoz – prosinac)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/08_Natjecaj_Universidad_de_Chile_2026_2027_zimski.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['CL'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BAonly≥4semesters, SpanishDELEB1. EUR640/month+EUR1000travel refund and '
             'host tuition waiver; home tuition continues if applicable. Housing '
             'assistance is not free lodging; host final acceptance required. '
             'Indicative semester dates follow host calendar. Up to3 bilateral '
             'applications with priorities, not3guaranteed awards.',
  'deadline': '2026-02-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/09_Natjecaj_Universidad_de_Chile_2026_2027_ljetni.pdf',
  'title': 'Natječaj za stipendiju – Universidad de Chile (Čile), za ljetni semestar '
           'akademske godine 2026./2027. (ožujak – srpanj)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/09_Natjecaj_Universidad_de_Chile_2026_2027_ljetni.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['CL'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BAonly≥4semesters, SpanishDELEB1. EUR640/month+EUR1000travel refund and '
             'host tuition waiver; home tuition continues if applicable. Housing '
             'assistance is not free lodging; host final acceptance required. '
             'Indicative semester dates follow host calendar. Up to3 bilateral '
             'applications with priorities, not3guaranteed awards.',
  'deadline': '2026-03-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/10_Natjecaj_Chuo_University_2026_2027_zimski.pdf',
  'title': 'Natječaj za stipendiju – Chuo University (Japan), za zimski semestar '
           'akademske godine 2026./2027. (rujan – ožujak)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/10_Natjecaj_Chuo_University_2026_2027_zimski.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['JP'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA/MA≥2semesters, English; Japanese proof for Japanese-only study. '
             'EUR800/month+EUR1000travel refund and host tuition waiver; home tuition '
             'continues if applicable. Housing assistance is not free lodging; host '
             'final acceptance required. Indicative semester dates follow host '
             'calendar. Up to3 bilateral applications with priorities, not3guaranteed '
             'awards.',
  'deadline': '2026-02-08',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/11_Natjecaj_Chuo_University_2026_2027_ljetni.pdf',
  'title': 'Natječaj za stipendiju – Chuo University (Japan), za ljetni semestar '
           'akademske godine 2026./2027. (travanj – rujan)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/11_Natjecaj_Chuo_University_2026_2027_ljetni.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['JP'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA/MA≥2semesters, English; Japanese proof for Japanese-only study. '
             'EUR800/month+EUR1000travel refund and host tuition waiver; home tuition '
             'continues if applicable. Housing assistance is not free lodging; host '
             'final acceptance required. Indicative semester dates follow host '
             'calendar. Up to3 bilateral applications with priorities, not3guaranteed '
             'awards.',
  'deadline': '2026-03-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/12_Natjecaj_Kanagawa_University_2026_2027_zimski.pdf',
  'title': 'Natječaj za stipendiju – Kanagawa University (Japan), za zimski semestar '
           'akademske godine 2026./2027. (rujan – siječanj)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/12_Natjecaj_Kanagawa_University_2026_2027_zimski.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['JP'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA≥2/MA≥1semester, English; Japanese proof for Japanese-only study. '
             'EUR800/month+EUR1000travel refund and host tuition waiver; home tuition '
             'continues if applicable. Housing assistance is not free lodging; host '
             'final acceptance required. Indicative semester dates follow host '
             'calendar. Up to3 bilateral applications with priorities, not3guaranteed '
             'awards.',
  'deadline': '2026-02-11',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/13_Natjecaj_Kanagawa_University_2026_2027_ljetni.pdf',
  'title': 'Natječaj za stipendiju – Kanagawa University (Japan), za ljetni semestar '
           'akademske godine 2026./2027. (ožujak – srpanj)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/13_Natjecaj_Kanagawa_University_2026_2027_ljetni.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['JP'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA≥2/MA≥1semester, English; Japanese proof for Japanese-only study. '
             'EUR800/month+EUR1000travel refund and host tuition waiver; home tuition '
             'continues if applicable. Housing assistance is not free lodging; host '
             'final acceptance required. Indicative semester dates follow host '
             'calendar. Up to3 bilateral applications with priorities, not3guaranteed '
             'awards.',
  'deadline': '2026-03-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/14_Natjecaj_Sophia_University_2026_2027_zimski.pdf',
  'title': 'Natječaj za stipendiju – Sophia University (Japan), za zimski semestar '
           'akademske godine 2026./2027. (rujan – siječanj)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/14_Natjecaj_Sophia_University_2026_2027_zimski.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['JP'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA/MA≥2semesters, English; Japanese proof for Japanese-only study. '
             'EUR800/month+EUR1000travel refund and host tuition waiver; home tuition '
             'continues if applicable. Housing assistance is not free lodging; host '
             'final acceptance required. Indicative semester dates follow host '
             'calendar. Up to3 bilateral applications with priorities, not3guaranteed '
             'awards.',
  'deadline': '2026-02-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/15_Natjecaj_Sophia_University_2026_2027_ljetni.pdf',
  'title': 'Natječaj za stipendiju – Sophia University (Japan), za ljetni semestar '
           'akademske godine 2026./2027. (travanj – srpanj)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/15_Natjecaj_Sophia_University_2026_2027_ljetni.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['JP'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA/MA≥2semesters, English; Japanese proof for Japanese-only study. '
             'EUR800/month+EUR1000travel refund and host tuition waiver; home tuition '
             'continues if applicable. Housing assistance is not free lodging; host '
             'final acceptance required. Indicative semester dates follow host '
             'calendar. Up to3 bilateral applications with priorities, not3guaranteed '
             'awards.',
  'deadline': '2026-03-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/16_Natjecaj_Chonnam_National_University_2026_2027_zimski.pdf',
  'title': 'Natječaj za stipendiju – Chonnam National University (Južna Koreja), za '
           'zimski semestar akademske godine 2026./2027. (rujan – prosinac)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/16_Natjecaj_Chonnam_National_University_2026_2027_zimski.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['KR'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA/MA≥2semesters, English; '
             'Medicine/Dentistry/Veterinary/EnglishEducation excluded. '
             'EUR800/month+EUR1000travel refund and host tuition waiver; home tuition '
             'continues if applicable. Housing assistance is not free lodging; host '
             'final acceptance required. Indicative semester dates follow host '
             'calendar. Up to3 bilateral applications with priorities, not3guaranteed '
             'awards.',
  'deadline': '2026-02-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/17_Natjecaj_Chonnam_National_University_2026_2027_ljetni.pdf',
  'title': 'Natječaj za stipendiju – Chonnam National University (Južna Koreja), za '
           'ljetni semestar akademske godine 2026./2027. (ožujak – lipanj)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/17_Natjecaj_Chonnam_National_University_2026_2027_ljetni.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['KR'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA/MA≥2semesters, English; '
             'Medicine/Dentistry/Veterinary/EnglishEducation excluded. '
             'EUR800/month+EUR1000travel refund and host tuition waiver; home tuition '
             'continues if applicable. Housing assistance is not free lodging; host '
             'final acceptance required. Indicative semester dates follow host '
             'calendar. Up to3 bilateral applications with priorities, not3guaranteed '
             'awards.',
  'deadline': '2026-03-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/18_Natjecaj_Hanyang_University_2026_2027_zimski.pdf',
  'title': 'Natječaj za stipendiju – Hanyang University (Južna Koreja), za zimski '
           'semestar akademske godine 2026./2027. (rujan – prosinac)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/18_Natjecaj_Hanyang_University_2026_2027_zimski.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['KR'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BAonly≥2semesters, English. EUR800/month+EUR1000travel refund and host '
             'tuition waiver; home tuition continues if applicable. Housing assistance '
             'is not free lodging; host final acceptance required. Indicative semester '
             'dates follow host calendar. Up to3 bilateral applications with '
             'priorities, not3guaranteed awards.',
  'deadline': '2026-02-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/19_Natjecaj_Hanyang_University_2026_2027_ljetni.pdf',
  'title': 'Natječaj za stipendiju – Hanyang University (Južna Koreja), za ljetni '
           'semestar akademske godine 2026./2027. (ožujak – lipanj)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/19_Natjecaj_Hanyang_University_2026_2027_ljetni.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['KR'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BAonly≥2semesters, English. EUR800/month+EUR1000travel refund and host '
             'tuition waiver; home tuition continues if applicable. Housing assistance '
             'is not free lodging; host final acceptance required. Indicative semester '
             'dates follow host calendar. Up to3 bilateral applications with '
             'priorities, not3guaranteed awards.',
  'deadline': '2026-03-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/20_Natjecaj_City_University_of_Hong_Kong_2026_2027_zimski.pdf',
  'title': 'Natječaj za stipendiju – City University of Hong Kong, za zimski semestar '
           'akademske godine 2026./2027. (rujan – prosinac)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/20_Natjecaj_City_University_of_Hong_Kong_2026_2027_zimski.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['HK'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BAonly≥2semesters, English. EUR800/month+EUR1000travel refund and host '
             'tuition waiver; home tuition continues if applicable. Housing assistance '
             'is not free lodging; host final acceptance required. Indicative semester '
             'dates follow host calendar. Up to3 bilateral applications with '
             'priorities, not3guaranteed awards.',
  'deadline': '2026-02-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/21_Natjecaj_City_University_of_Hong_Kong_2026_2027_ljetni.pdf',
  'title': 'Natječaj za stipendiju – City University of Hong Kong, za ljetni semestar '
           'akademske godine 2026./2027. (siječanj – lipanj)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/21_Natjecaj_City_University_of_Hong_Kong_2026_2027_ljetni.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['HK'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BAonly≥2semesters, English. EUR800/month+EUR1000travel refund and host '
             'tuition waiver; home tuition continues if applicable. Housing assistance '
             'is not free lodging; host final acceptance required. Indicative semester '
             'dates follow host calendar. Up to3 bilateral applications with '
             'priorities, not3guaranteed awards.',
  'deadline': '2026-03-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/29_Natjecaj_NCKU_2026_2027_zimski.pdf',
  'title': 'Natječaj za stipendiju – National Cheng Kung University, za zimski '
           'semestar akademske godine 2026./2027. (rujan – siječanj)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/29_Natjecaj_NCKU_2026_2027_zimski.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['TW'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA/MA≥2semesters, English; Chinese optional ranking bonus. '
             'EUR800/month+EUR1000travel refund and host tuition waiver; home tuition '
             'continues if applicable. Housing assistance is not free lodging; host '
             'final acceptance required. Indicative semester dates follow host '
             'calendar. Up to3 bilateral applications with priorities, not3guaranteed '
             'awards.',
  'deadline': '2026-02-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/30_Natjecaj_NCKU_2026_2027_ljetni.pdf',
  'title': 'Natječaj za stipendiju – National Cheng Kung University, za ljetni '
           'semestar akademske godine 2026./2027. (veljača – lipanj)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/30_Natjecaj_NCKU_2026_2027_ljetni.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['TW'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA/MA≥2semesters, English; Chinese optional ranking bonus. '
             'EUR800/month+EUR1000travel refund and host tuition waiver; home tuition '
             'continues if applicable. Housing assistance is not free lodging; host '
             'final acceptance required. Indicative semester dates follow host '
             'calendar. Up to3 bilateral applications with priorities, not3guaranteed '
             'awards.',
  'deadline': '2026-03-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/31_Natjecaj_NSYSU_2026_2027_zimski.pdf',
  'title': 'Natječaj za stipendiju – National Sun Yat-Sen University (NSYSU), za '
           'zimski semestar akademske godine 2026./2027. (rujan – siječanj)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/31_Natjecaj_NSYSU_2026_2027_zimski.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['TW'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA/MA≥2semesters, English; Chinese optional ranking bonus. '
             'EUR800/month+EUR1000travel refund and host tuition waiver; home tuition '
             'continues if applicable. Housing assistance is not free lodging; host '
             'final acceptance required. Indicative semester dates follow host '
             'calendar. Up to3 bilateral applications with priorities, not3guaranteed '
             'awards.',
  'deadline': '2026-02-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/32_Natjecaj_NSYSU_2026_2027_ljetni.pdf',
  'title': 'Natječaj za stipendiju – National Sun Yat-Sen University (NSYSU), za '
           'ljetni semestar akademske godine 2026./2027. (veljača – lipanj)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/32_Natjecaj_NSYSU_2026_2027_ljetni.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['TW'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA/MA≥2semesters, English; Chinese optional ranking bonus. '
             'EUR800/month+EUR1000travel refund and host tuition waiver; home tuition '
             'continues if applicable. Housing assistance is not free lodging; host '
             'final acceptance required. Indicative semester dates follow host '
             'calendar. Up to3 bilateral applications with priorities, not3guaranteed '
             'awards.',
  'deadline': '2026-03-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/22_Natjecaj_UDEG_2026_2027_zimski.pdf',
  'title': 'Natječaj za stipendiju – Universidad de Guadalajara (Meksiko), za zimski '
           'semestar akademske godine 2026./2027. (kolovoz – prosinac)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/22_Natjecaj_UDEG_2026_2027_zimski.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['MX'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA/MA≥2semesters; EnglishB2 and/or SpanishB1 documents versus broader '
             'English firstpage. EUR640/month+EUR1000travel refund and host tuition '
             'waiver; home tuition continues if applicable. Housing assistance is not '
             'free lodging; host final acceptance required. Indicative semester dates '
             'follow host calendar. Up to3 bilateral applications with priorities, '
             'not3guaranteed awards.',
  'deadline': '2026-02-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/23_Natjecaj_UDEG_2026_2027_ljetni.pdf',
  'title': 'Natječaj za stipendiju – Universidad de Guadalajara (Meksiko), za ljetni '
           'semestar akademske godine 2026./2027. (siječanj – lipanj)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/23_Natjecaj_UDEG_2026_2027_ljetni.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['MX'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA/MA≥2semesters; EnglishB2 and/or SpanishB1 documents versus broader '
             'English firstpage. EUR640/month+EUR1000travel refund and host tuition '
             'waiver; home tuition continues if applicable. Housing assistance is not '
             'free lodging; host final acceptance required. Indicative semester dates '
             'follow host calendar. Up to3 bilateral applications with priorities, '
             'not3guaranteed awards.',
  'deadline': '2026-03-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/25_Natjecaj_University_of_New_Mexico_2026_2027_zimski.pdf',
  'title': 'Natječaj za stipendiju – University of New Mexico (SAD), za zimski '
           'semestar akademske godine 2026./2027. (kolovoz – prosinac)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/25_Natjecaj_University_of_New_Mexico_2026_2027_zimski.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['US'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA/MA≥2semesters, English. EUR800/month+EUR1000travel refund and host '
             'tuition waiver; home tuition continues if applicable. Housing assistance '
             'is not free lodging; host final acceptance required. Indicative semester '
             'dates follow host calendar. Up to3 bilateral applications with '
             'priorities, not3guaranteed awards.',
  'deadline': '2026-02-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/26_Natjecaj_University_of_New_Mexico_2026_2027_ljetni.pdf',
  'title': 'Natječaj za stipendiju – University of New Mexico (SAD), za ljetni '
           'semestar akademske godine 2026./2027. (siječanj – svibanj)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/26_Natjecaj_University_of_New_Mexico_2026_2027_ljetni.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['US'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA/MA≥2semesters, English. EUR800/month+EUR1000travel refund and host '
             'tuition waiver; home tuition continues if applicable. Housing assistance '
             'is not free lodging; host final acceptance required. Indicative semester '
             'dates follow host calendar. Up to3 bilateral applications with '
             'priorities, not3guaranteed awards.',
  'deadline': '2026-03-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/27_Natjecaj_National_University_of_Singapore_2026_2027_zimski.pdf',
  'title': 'Natječaj za stipendiju – National University of Singapore (Singapur), za '
           'zimski semestar akademske godine 2026./2027. (kolovoz – prosinac)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/27_Natjecaj_National_University_of_Singapore_2026_2027_zimski.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['SG'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA≥2semesters/no grade<3; College '
             'Design/Engineering,Arts/SocialSciences,Science,Computing ONLY; Zagreb '
             'Economics/Law excluded; English. EUR800/month+EUR1000travel refund and '
             'host tuition waiver; home tuition continues if applicable. Housing '
             'assistance is not free lodging; host final acceptance required. '
             'Indicative semester dates follow host calendar. Up to3 bilateral '
             'applications with priorities, not3guaranteed awards.',
  'deadline': '2026-02-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/28_Natjecaj_National_University_of_Singapore_2026_2027_ljetni.pdf',
  'title': 'Natječaj za stipendiju – National University of Singapore (Singapur), za '
           'ljetni semestar akademske godine 2026./2027. (siječanj – svibanj)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/28_Natjecaj_National_University_of_Singapore_2026_2027_ljetni.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['SG'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA≥2semesters/no grade<3; College '
             'Design/Engineering,Arts/SocialSciences,Science,Computing ONLY; Zagreb '
             'Economics/Law excluded; English. EUR800/month+EUR1000travel refund and '
             'host tuition waiver; home tuition continues if applicable. Housing '
             'assistance is not free lodging; host final acceptance required. '
             'Indicative semester dates follow host calendar. Up to3 bilateral '
             'applications with priorities, not3guaranteed awards.',
  'deadline': '2026-03-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/33_Natjecaj_Universidad_Catolica_del_Uruguay_2026_2027_zimski.pdf',
  'title': 'Natječaj za stipendiju – Universidad Católica del Uruguay (Urugvaj), za '
           'zimski semestar akademske godine 2026./2027. (kolovoz – prosinac)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/33_Natjecaj_Universidad_Catolica_del_Uruguay_2026_2027_zimski.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['UY'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA/MA≥2semesters, SpanishB1. EUR640/month+EUR1000travel refund and host '
             'tuition waiver; home tuition continues if applicable. Housing assistance '
             'is not free lodging; host final acceptance required. Indicative semester '
             'dates follow host calendar. Own Uruguay call does not state other '
             'calls’3-application cap.',
  'deadline': '2026-02-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/34_Natjecaj_Universidad_Catolica_del_Uruguay_2026_2027_ljetni.pdf',
  'title': 'Natječaj za stipendiju – Universidad Católica del Uruguay (Urugvaj), za '
           'ljetni semestar akademske godine 2026./2027. (ožujak – srpanj)',
  'url': 'https://www.unizg.hr/fileadmin/rektorat/Suradnja/Medunarodna_razmjena/Studenata/Bilaterala/2026-27/Natjecaji/34_Natjecaj_Universidad_Catolica_del_Uruguay_2026_2027_ljetni.pdf',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['UY'],
  'summary': 'One semester place; full-time Zagreb affiliation maintained, GPA≥4.000; '
             'BA/MA≥2semesters, SpanishB1. EUR640/month+EUR1000travel refund and host '
             'tuition waiver; home tuition continues if applicable. Housing assistance '
             'is not free lodging; host final acceptance required. Indicative semester '
             'dates follow host calendar. Own Uruguay call does not state other '
             'calls’3-application cap.',
  'deadline': '2026-03-25',
  'proof': 'Complete own bilateral Predmet call and literal '
           'qualification/support/semester clauses.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-stipendiju-u-svrhu-kratkog-istrazivackog-boravka-na-sveucilistu-johannes-gutenberg-3/|PhD',
  'title': 'Natječaj za stipendiju u svrhu kratkog istraživačkog boravka na '
           'Sveučilištu Johannes Gutenberg u Mainzu (i Campusu Germersheimu) za jednog '
           'doktoranda i jednog poslijedoktoranda Sveučilišta u Zagrebu u 2026. godini',
  'url': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-stipendiju-u-svrhu-kratkog-istrazivackog-boravka-na-sveucilistu-johannes-gutenberg-3/',
  'category': 'fellowships',
  'kind': 'opportunity',
  'hosts': ['DE'],
  'summary': 'Zagreb PhD≥thirdsemester, employed or not;EUR54/day for14-day2026 Mainz '
             'research stay plusEUR350travel. German OR English B2+, host acceptance '
             'required, all JGUfields. Participant pays lodging and necessary '
             'insurance. One place each; if no postdoc applicants, up to3PhD '
             'conditional. Closing Jan18,2026 date-only. Joint2025–27 project is not '
             'a3-year personal award.',
  'deadline': '2026-01-18',
  'proof': 'Original own notice student-latest-8; reviewed call and its integral '
           'literal materials; track PhD.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-stipendiju-u-svrhu-kratkog-istrazivackog-boravka-na-sveucilistu-johannes-gutenberg-3/|postdoc',
  'title': 'Natječaj za stipendiju u svrhu kratkog istraživačkog boravka na '
           'Sveučilištu Johannes Gutenberg u Mainzu (i Campusu Germersheimu) za jednog '
           'doktoranda i jednog poslijedoktoranda Sveučilišta u Zagrebu u 2026. godini',
  'url': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-stipendiju-u-svrhu-kratkog-istrazivackog-boravka-na-sveucilistu-johannes-gutenberg-3/',
  'category': 'fellowships',
  'kind': 'opportunity',
  'hosts': ['DE'],
  'summary': 'Zagreb/constituent employee doctorate within3years;EUR89/day '
             'for14-day2026 Mainz research stay plusEUR350travel. German OR English '
             'B2+, host acceptance required, all JGUfields. Participant pays lodging '
             'and necessary insurance. One place each; if no postdoc applicants, up '
             'to3PhD conditional. Closing Jan18,2026 date-only. Joint2025–27 project '
             'is not a3-year personal award.',
  'deadline': '2026-01-18',
  'proof': 'Original own notice student-latest-8; reviewed call and its integral '
           'literal materials; track postdoc.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|ADU|Univerzitet '
         'u Sarajevu|0211/0215 audiovisual/music/performance',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['BA'],
  'summary': 'Zagreb staff ADU, 0211/0215 audiovisual/music/performance: Univerzitet u '
             'Sarajevu. Funded5activity+1travel days, travelEUR211; eligible '
             'duration5days–2months is not all funded. Listed190EUR/day; travel '
             'allocation may conflict with generic guide, confirm actual award. STA '
             'teaching/combined academic only (8h/4h); academic otherfaculty fallback '
             'conditional no targeted applicant/samefield. Sep28,2026–Jun30,2027; '
             'July3 closing has erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track ADU|Univerzitet u Sarajevu|0211/0215 '
           'audiovisual/music/performance.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|AGR|Universidad '
         'de Buenos Aires|0810/0888 agriculture',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['AR'],
  'summary': 'Zagreb staff AGR, 0810/0888 agriculture: Universidad de Buenos Aires. '
             'Funded7activity+2travel days, travelEUR1735; eligible '
             'duration5days–2months is not all funded. Listed190EUR/day; travel '
             'allocation may conflict with generic guide, confirm actual award. STA '
             'teaching/combined academic only (8h/4h); academic otherfaculty fallback '
             'conditional no targeted applicant/samefield. Sep28,2026–Jun30,2027; '
             'July3 closing has erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track AGR|Universidad de Buenos Aires|0810/0888 agriculture.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|AGR|Universidad '
         'de la Frontera / Pontificia Universidad Católica de Chile|0888/0821/0810 '
         'agriculture/forestry',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['CL'],
  'summary': 'Zagreb staff AGR, 0888/0821/0810 agriculture/forestry: Universidad de la '
             'Frontera / Pontificia Universidad Católica de Chile. '
             'Funded7activity+2travel days, travelEUR1735; eligible '
             'duration5days–2months is not all funded. Listed190EUR/day; travel '
             'allocation may conflict with generic guide, confirm actual award. STA '
             'academic teaching/combined8h/4h orSTT; nonteaching STT ownfaculty only; '
             'no conferences; academic otherfaculty fallback conditional no targeted '
             'applicant/samefield. Sep28,2026–Jun30,2027; July3 closing has erroneous '
             'weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track AGR|Universidad de la Frontera / Pontificia Universidad '
           'Católica de Chile|0888/0821/0810 agriculture/forestry.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|ARHITEKT|Pontificia '
         'Universidad Católica de Chile|0730/0731 architecture',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['CL'],
  'summary': 'Zagreb staff ARHITEKT, 0730/0731 architecture: Pontificia Universidad '
             'Católica de Chile. Funded7activity+2travel days, travelEUR1735; eligible '
             'duration5days–2months is not all funded. Listed190EUR/day; travel '
             'allocation may conflict with generic guide, confirm actual award. STA '
             'academic teaching/combined8h/4h orSTT; nonteaching STT ownfaculty only; '
             'no conferences; academic otherfaculty fallback conditional no targeted '
             'applicant/samefield. Sep28,2026–Jun30,2027; July3 closing has erroneous '
             'weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track ARHITEKT|Pontificia Universidad Católica de '
           'Chile|0730/0731 architecture.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|ARHITEKT|Univerzitet '
         'u Sarajevu|0731/0212/0211 architecture/design/audiovisual',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['BA'],
  'summary': 'Zagreb staff ARHITEKT, 0731/0212/0211 architecture/design/audiovisual: '
             'Univerzitet u Sarajevu. Funded5activity+1travel days, travelEUR211; '
             'eligible duration5days–2months is not all funded. Listed190EUR/day; '
             'travel allocation may conflict with generic guide, confirm actual award. '
             'STA teaching/combined academic only (8h/4h); academic otherfaculty '
             'fallback conditional no targeted applicant/samefield. '
             'Sep28,2026–Jun30,2027; July3 closing has erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track ARHITEKT|Univerzitet u Sarajevu|0731/0212/0211 '
           'architecture/design/audiovisual.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|EFZG|Sveučilište '
         'u Mostaru|0311/0410/0421 economics/business/law',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['BA'],
  'summary': 'Zagreb staff EFZG, 0311/0410/0421 economics/business/law: Sveučilište u '
             'Mostaru. Funded5activity+1travel days, travelEUR211; eligible '
             'duration5days–2months is not all funded. Listed190EUR/day; travel '
             'allocation may conflict with generic guide, confirm actual award. STA '
             'teaching/combined academic only (8h/4h); academic otherfaculty fallback '
             'conditional no targeted applicant/samefield. Sep28,2026–Jun30,2027; '
             'July3 closing has erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track EFZG|Sveučilište u Mostaru|0311/0410/0421 '
           'economics/business/law.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|EFZG|Univerzitet '
         'Crne Gore|0311/0410/0421 economics/business/law',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['ME'],
  'summary': 'Zagreb staff EFZG, 0311/0410/0421 economics/business/law: Univerzitet '
             'Crne Gore. Funded5activity+1travel days, travelEUR211; eligible '
             'duration5days–2months is not all funded. Listed190EUR/day; travel '
             'allocation may conflict with generic guide, confirm actual award. STA '
             'teaching/combined academic only (8h/4h); academic otherfaculty fallback '
             'conditional no targeted applicant/samefield. Sep28,2026–Jun30,2027; '
             'July3 closing has erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track EFZG|Univerzitet Crne Gore|0311/0410/0421 '
           'economics/business/law.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|EFZG|Pontificia '
         'Universidad Javeriana|0311/0410/0421 economics/business/law',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['CO'],
  'summary': 'Zagreb staff EFZG, 0311/0410/0421 economics/business/law: Pontificia '
             'Universidad Javeriana. Funded7activity+2travel days, travelEUR1735; '
             'eligible duration5days–2months is not all funded. Listed190EUR/day; '
             'travel allocation may conflict with generic guide, confirm actual award. '
             'STA teaching/combined academic only (8h/4h); academic otherfaculty '
             'fallback conditional no targeted applicant/samefield. '
             'Sep28,2026–Jun30,2027; July3 closing has erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track EFZG|Pontificia Universidad Javeriana|0311/0410/0421 '
           'economics/business/law.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|EFZG|Instituto '
         'Tecnológico y de Estudios Superiores de Monterrey|0311/0410 '
         'economics/business',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['MX'],
  'summary': 'Zagreb staff EFZG, 0311/0410 economics/business: Instituto Tecnológico y '
             'de Estudios Superiores de Monterrey. Funded7activity+2travel days, '
             'travelEUR1735; eligible duration5days–2months is not all funded. '
             'Listed190EUR/day; travel allocation may conflict with generic guide, '
             'confirm actual award. STA teaching/combined academic only (8h/4h); '
             'academic otherfaculty fallback conditional no targeted '
             'applicant/samefield. Sep28,2026–Jun30,2027; July3 closing has erroneous '
             'weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track EFZG|Instituto Tecnológico y de Estudios Superiores de '
           'Monterrey|0311/0410 economics/business.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|EFZG|KIMEP '
         'University|0311/0410/0421 economics/business/law',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['KZ'],
  'summary': 'Zagreb staff EFZG, 0311/0410/0421 economics/business/law: KIMEP '
             'University. Funded7activity+2travel days, travelEUR1188; eligible '
             'duration5days–2months is not all funded. Listed190EUR/day; travel '
             'allocation may conflict with generic guide, confirm actual award. STT '
             'training only; nonteaching ownfaculty only; no conferences; academic '
             'otherfaculty fallback conditional no targeted applicant/samefield. '
             'Sep28,2026–Jun30,2027; July3 closing has erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track EFZG|KIMEP University|0311/0410/0421 '
           'economics/business/law.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|EFZG|Lucerne '
         'University of Applied Sciences and Arts|0311/0410/0421 '
         'economics/business/law',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['CH'],
  'summary': 'Zagreb staff EFZG, 0311/0410/0421 economics/business/law: Lucerne '
             'University of Applied Sciences and Arts. Funded5activity+2travel days, '
             'travelEUR309; eligible duration5days–2months is not all funded. Daily '
             'rate conflicts: guide144 versus allocation152EUR; exact rate unknown. '
             'STA teaching/combined academic only (8h/4h); academic otherfaculty '
             'fallback conditional no targeted applicant/samefield. '
             'Sep28,2026–Jun30,2027; July3 closing has erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track EFZG|Lucerne University of Applied Sciences and '
           'Arts|0311/0410/0421 economics/business/law.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|FER|Sveučilište '
         'u Mostaru / Univerzitet u Sarajevu / Univerzitet u Tuzli|0612/0613/0713/0714 '
         'computing/electrical',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['BA'],
  'summary': 'Zagreb staff FER, 0612/0613/0713/0714 computing/electrical: Sveučilište '
             'u Mostaru / Univerzitet u Sarajevu / Univerzitet u Tuzli. '
             'Funded5activity+1travel days, travelEUR211; eligible '
             'duration5days–2months is not all funded. Listed190EUR/day; travel '
             'allocation may conflict with generic guide, confirm actual award. STA '
             'teaching/combined academic only (8h/4h); academic otherfaculty fallback '
             'conditional no targeted applicant/samefield. Sep28,2026–Jun30,2027; '
             'July3 closing has erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track FER|Sveučilište u Mostaru / Univerzitet u Sarajevu / '
           'Univerzitet u Tuzli|0612/0613/0713/0714 computing/electrical.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|FER|Univerzitet '
         'Crne Gore|0612/0613/0713/0714 computing/electrical',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['ME'],
  'summary': 'Zagreb staff FER, 0612/0613/0713/0714 computing/electrical: Univerzitet '
             'Crne Gore. Funded5activity+1travel days, travelEUR211; eligible '
             'duration5days–2months is not all funded. Listed190EUR/day; travel '
             'allocation may conflict with generic guide, confirm actual award. STT '
             'training only; nonteaching ownfaculty only; no conferences; academic '
             'otherfaculty fallback conditional no targeted applicant/samefield. '
             'Sep28,2026–Jun30,2027; July3 closing has erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track FER|Univerzitet Crne Gore|0612/0613/0713/0714 '
           'computing/electrical.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|FER|Pontificia '
         'Universidad Javeriana|0612/0613/0713/0714 computing/electrical',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['CO'],
  'summary': 'Zagreb staff FER, 0612/0613/0713/0714 computing/electrical: Pontificia '
             'Universidad Javeriana. Funded7activity+2travel days, travelEUR1735; '
             'eligible duration5days–2months is not all funded. Listed190EUR/day; '
             'travel allocation may conflict with generic guide, confirm actual award. '
             'STA teaching/combined academic only (8h/4h); academic otherfaculty '
             'fallback conditional no targeted applicant/samefield. '
             'Sep28,2026–Jun30,2027; July3 closing has erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track FER|Pontificia Universidad Javeriana|0612/0613/0713/0714 '
           'computing/electrical.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|FER|Instituto '
         'Tecnológico y de Estudios Superiores de Monterrey|0612/0613/0713/0714 '
         'computing/electrical',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['MX'],
  'summary': 'Zagreb staff FER, 0612/0613/0713/0714 computing/electrical: Instituto '
             'Tecnológico y de Estudios Superiores de Monterrey. '
             'Funded7activity+2travel days, travelEUR1735; eligible '
             'duration5days–2months is not all funded. Listed190EUR/day; travel '
             'allocation may conflict with generic guide, confirm actual award. STA '
             'teaching/combined academic only (8h/4h); academic otherfaculty fallback '
             'conditional no targeted applicant/samefield. Sep28,2026–Jun30,2027; '
             'July3 closing has erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track FER|Instituto Tecnológico y de Estudios Superiores de '
           'Monterrey|0612/0613/0713/0714 computing/electrical.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|FOI|The '
         'Pennsylvania State University|0610/0410 computing/business',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['US'],
  'summary': 'Zagreb staff FOI, 0610/0410 computing/business: The Pennsylvania State '
             'University. Funded7activity+2travel days, travelEUR1188; eligible '
             'duration5days–2months is not all funded. Listed190EUR/day; travel '
             'allocation may conflict with generic guide, confirm actual award. STA '
             'teaching/combined academic only (8h/4h); academic otherfaculty fallback '
             'conditional no targeted applicant/samefield. Sep28,2026–Jun30,2027; '
             'July3 closing has erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track FOI|The Pennsylvania State University|0610/0410 '
           'computing/business.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|FHS|Universidad '
         'de Buenos Aires|0230/0232 Croatian literature/linguistics',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['AR'],
  'summary': 'Zagreb staff FHS, 0230/0232 Croatian literature/linguistics: Universidad '
             'de Buenos Aires. Funded7activity+2travel days, travelEUR1735; eligible '
             'duration5days–2months is not all funded. Listed190EUR/day; travel '
             'allocation may conflict with generic guide, confirm actual award. STA '
             'teaching/combined academic only (8h/4h); academic otherfaculty fallback '
             'conditional no targeted applicant/samefield. Sep28,2026–Jun30,2027; '
             'July3 closing has erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track FHS|Universidad de Buenos Aires|0230/0232 Croatian '
           'literature/linguistics.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|FHS|Sveučilište '
         'u Mostaru|0222/0231/0310/0322 history/Croatian/social/library',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['BA'],
  'summary': 'Zagreb staff FHS, 0222/0231/0310/0322 history/Croatian/social/library: '
             'Sveučilište u Mostaru. Funded5activity+1travel days, travelEUR211; '
             'eligible duration5days–2months is not all funded. Listed190EUR/day; '
             'travel allocation may conflict with generic guide, confirm actual award. '
             'STA teaching/combined academic only (8h/4h); academic otherfaculty '
             'fallback conditional no targeted applicant/samefield. '
             'Sep28,2026–Jun30,2027; July3 closing has erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track FHS|Sveučilište u Mostaru|0222/0231/0310/0322 '
           'history/Croatian/social/library.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|FPZG|Universiteti '
         'i Prishtinës Hasan Prishtina|0312 politics/civics',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['XK'],
  'summary': 'Zagreb staff FPZG, 0312 politics/civics: Universiteti i Prishtinës Hasan '
             'Prishtina. Funded5activity+2travel days, travelEUR309; eligible '
             'duration5days–2months is not all funded. Listed190EUR/day; travel '
             'allocation may conflict with generic guide, confirm actual award. STA '
             'teaching/combined academic only (8h/4h); academic otherfaculty fallback '
             'conditional no targeted applicant/samefield. Sep28,2026–Jun30,2027; '
             'July3 closing has erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track FPZG|Universiteti i Prishtinës Hasan Prishtina|0312 '
           'politics/civics.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|FPZG|University '
         'of Lampung AND Far Eastern University|0312 politics/civics',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['ID', 'PH'],
  'summary': 'Zagreb staff FPZG, 0312 politics/civics: University of Lampung AND Far '
             'Eastern University. MANDATORY two5-day visits/twoSTAagreements, not '
             'alternatives; total listedEUR4015. Listed190EUR/day; travel allocation '
             'may conflict with generic guide, confirm actual award. STA academic '
             'teaching/combined8h/4h orSTT; nonteaching STT ownfaculty only; no '
             'conferences; academic otherfaculty fallback conditional no targeted '
             'applicant/samefield. Sep28,2026–Jun30,2027; July3 closing has erroneous '
             'weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track FPZG|University of Lampung AND Far Eastern '
           'University|0312 politics/civics.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|FSB|Universidade '
         'de São Paulo - EESC|0710/0715/0788/0713/0716 engineering/motor vehicles',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['BR'],
  'summary': 'Zagreb staff FSB, 0710/0715/0788/0713/0716 engineering/motor vehicles: '
             'Universidade de São Paulo - EESC. Funded7activity+2travel days, '
             'travelEUR1735; eligible duration5days–2months is not all funded. '
             'Listed190EUR/day; travel allocation may conflict with generic guide, '
             'confirm actual award. STA academic teaching/combined8h/4h orSTT; '
             'nonteaching STT ownfaculty only; no conferences; academic otherfaculty '
             'fallback conditional no targeted applicant/samefield. '
             'Sep28,2026–Jun30,2027; July3 closing has erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track FSB|Universidade de São Paulo - '
           'EESC|0710/0715/0788/0713/0716 engineering/motor vehicles.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|FSB|Universidade '
         'Federal do Rio de Janeiro|0710/0715/0788/0713/0716 engineering/motor '
         'vehicles',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['BR'],
  'summary': 'Zagreb staff FSB, 0710/0715/0788/0713/0716 engineering/motor vehicles: '
             'Universidade Federal do Rio de Janeiro. Funded7activity+2travel days, '
             'travelEUR1735; eligible duration5days–2months is not all funded. '
             'Listed190EUR/day; travel allocation may conflict with generic guide, '
             'confirm actual award. STA academic teaching/combined8h/4h orSTT; '
             'nonteaching STT ownfaculty only; no conferences; academic otherfaculty '
             'fallback conditional no targeted applicant/samefield. '
             'Sep28,2026–Jun30,2027; July3 closing has erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track FSB|Universidade Federal do Rio de '
           'Janeiro|0710/0715/0788/0713/0716 engineering/motor vehicles.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|FSB|Pontificia '
         'Universidad Católica de Valparaíso|0710/0715/0788/0713/0716 '
         'engineering/motor vehicles',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['CL'],
  'summary': 'Zagreb staff FSB, 0710/0715/0788/0713/0716 engineering/motor vehicles: '
             'Pontificia Universidad Católica de Valparaíso. Funded7activity+2travel '
             'days, travelEUR1735; eligible duration5days–2months is not all funded. '
             'Listed190EUR/day; travel allocation may conflict with generic guide, '
             'confirm actual award. STA teaching/combined academic only (8h/4h); '
             'academic otherfaculty fallback conditional no targeted '
             'applicant/samefield. Sep28,2026–Jun30,2027; July3 closing has erroneous '
             'weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track FSB|Pontificia Universidad Católica de '
           'Valparaíso|0710/0715/0788/0713/0716 engineering/motor vehicles.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|FSB|M. '
         'Auezov South Kazakhstan University|0710/0713/0715/0716 '
         'engineering/electrical/mechanical',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['KZ'],
  'summary': 'Zagreb staff FSB, 0710/0713/0715/0716 engineering/electrical/mechanical: '
             'M. Auezov South Kazakhstan University. Funded7activity+2travel days, '
             'travelEUR1188; eligible duration5days–2months is not all funded. '
             'Listed190EUR/day; travel allocation may conflict with generic guide, '
             'confirm actual award. STA teaching/combined academic only (8h/4h); '
             'academic otherfaculty fallback conditional no targeted '
             'applicant/samefield. Sep28,2026–Jun30,2027; July3 closing has erroneous '
             'weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track FSB|M. Auezov South Kazakhstan '
           'University|0710/0713/0715/0716 engineering/electrical/mechanical.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|FSB|Instituto '
         'Tecnológico y de Estudios Superiores de Monterrey|0710 engineering ONLY',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['MX'],
  'summary': 'Zagreb staff FSB, 0710 engineering ONLY: Instituto Tecnológico y de '
             'Estudios Superiores de Monterrey. Funded7activity+2travel days, '
             'travelEUR1735; eligible duration5days–2months is not all funded. '
             'Listed190EUR/day; travel allocation may conflict with generic guide, '
             'confirm actual award. STA teaching/combined academic only (8h/4h); '
             'academic otherfaculty fallback conditional no targeted '
             'applicant/samefield. Sep28,2026–Jun30,2027; July3 closing has erroneous '
             'weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track FSB|Instituto Tecnológico y de Estudios Superiores de '
           'Monterrey|0710 engineering ONLY.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|FSB|Universidade '
         'Eduardo Mondlane|0710/0713 engineering/electrical',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['MZ'],
  'summary': 'Zagreb staff FSB, 0710/0713 engineering/electrical: Universidade Eduardo '
             'Mondlane. Funded5activity+2travel days, travelEUR1188; eligible '
             'duration5days–2months is not all funded. Listed190EUR/day; travel '
             'allocation may conflict with generic guide, confirm actual award. STA '
             'teaching/combined academic only (8h/4h); academic otherfaculty fallback '
             'conditional no targeted applicant/samefield. Sep28,2026–Jun30,2027; '
             'July3 closing has erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track FSB|Universidade Eduardo Mondlane|0710/0713 '
           'engineering/electrical.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|FFZG|Univerzitet '
         'u Sarajevu / Sveučilište u Mostaru|0230/0232 Croatian language/literature',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['BA'],
  'summary': 'Zagreb staff FFZG, 0230/0232 Croatian language/literature: Univerzitet u '
             'Sarajevu / Sveučilište u Mostaru. Funded5activity+1travel days, '
             'travelEUR211; eligible duration5days–2months is not all funded. '
             'Listed190EUR/day; travel allocation may conflict with generic guide, '
             'confirm actual award. STA teaching/combined academic only (8h/4h); '
             'academic otherfaculty fallback conditional no targeted '
             'applicant/samefield. Sep28,2026–Jun30,2027; July3 closing has erroneous '
             'weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track FFZG|Univerzitet u Sarajevu / Sveučilište u '
           'Mostaru|0230/0232 Croatian language/literature.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|FFZG|Universidad '
         'de Buenos Aires|0230/0232 Eastern-Slavic department/Russian ONLY',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['AR'],
  'summary': 'Zagreb staff FFZG, 0230/0232 Eastern-Slavic department/Russian ONLY: '
             'Universidad de Buenos Aires. Funded7activity+2travel days, '
             'travelEUR1735; eligible duration5days–2months is not all funded. '
             'Listed190EUR/day; travel allocation may conflict with generic guide, '
             'confirm actual award. STA teaching/combined academic only (8h/4h); '
             'academic otherfaculty fallback conditional no targeted '
             'applicant/samefield. Sep28,2026–Jun30,2027; July3 closing has erroneous '
             'weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track FFZG|Universidad de Buenos Aires|0230/0232 Eastern-Slavic '
           'department/Russian ONLY.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|FFZG|Universidad '
         'de Buenos Aires|0314/0220 Ethnology/Cultural-Anthropology department ONLY',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['AR'],
  'summary': 'Zagreb staff FFZG, 0314/0220 Ethnology/Cultural-Anthropology department '
             'ONLY: Universidad de Buenos Aires. Funded7activity+2travel days, '
             'travelEUR1735; eligible duration5days–2months is not all funded. '
             'Listed190EUR/day; travel allocation may conflict with generic guide, '
             'confirm actual award. STA teaching/combined academic only (8h/4h); '
             'academic otherfaculty fallback conditional no targeted '
             'applicant/samefield. Sep28,2026–Jun30,2027; July3 closing has erroneous '
             'weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track FFZG|Universidad de Buenos Aires|0314/0220 '
           'Ethnology/Cultural-Anthropology department ONLY.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|MUZA|Yerevan '
         'Komitas State Conservatory|0215 music/performance',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['AM'],
  'summary': 'Zagreb staff MUZA, 0215 music/performance: Yerevan Komitas State '
             'Conservatory. Funded5activity+2travel days, travelEUR395; eligible '
             'duration5days–2months is not all funded. Listed190EUR/day; travel '
             'allocation may conflict with generic guide, confirm actual award. STA '
             'teaching/combined academic only (8h/4h); academic otherfaculty fallback '
             'conditional no targeted applicant/samefield. Sep28,2026–Jun30,2027; '
             'July3 closing has erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track MUZA|Yerevan Komitas State Conservatory|0215 '
           'music/performance.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|MUZA|Univerzitet '
         'u Sarajevu|0215 music/performance',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['BA'],
  'summary': 'Zagreb staff MUZA, 0215 music/performance: Univerzitet u Sarajevu. '
             'Funded5activity+1travel days, travelEUR211; eligible '
             'duration5days–2months is not all funded. Listed190EUR/day; travel '
             'allocation may conflict with generic guide, confirm actual award. STA '
             'teaching/combined academic only (8h/4h); academic otherfaculty fallback '
             'conditional no targeted applicant/samefield. Sep28,2026–Jun30,2027; '
             'July3 closing has erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track MUZA|Univerzitet u Sarajevu|0215 music/performance.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|MUZA|Tbilisi '
         'State Conservatoire|0215 music/performance',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['GE'],
  'summary': 'Zagreb staff MUZA, 0215 music/performance: Tbilisi State Conservatoire. '
             'Funded5activity+2travel days, travelEUR395; eligible '
             'duration5days–2months is not all funded. Listed190EUR/day; travel '
             'allocation may conflict with generic guide, confirm actual award. STA '
             'teaching/combined academic only (8h/4h); academic otherfaculty fallback '
             'conditional no targeted applicant/samefield. Sep28,2026–Jun30,2027; '
             'July3 closing has erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track MUZA|Tbilisi State Conservatoire|0215 music/performance.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|PRAVO|Univerzitet '
         'Crne Gore|0421/0923 law/social work',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['ME'],
  'summary': 'Zagreb staff PRAVO, 0421/0923 law/social work: Univerzitet Crne Gore. '
             'Funded5activity+1travel days, travelEUR211; eligible '
             'duration5days–2months is not all funded. Listed190EUR/day; travel '
             'allocation may conflict with generic guide, confirm actual award. STA '
             'teaching/combined academic only (8h/4h); academic otherfaculty fallback '
             'conditional no targeted applicant/samefield. Sep28,2026–Jun30,2027; '
             'July3 closing has erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track PRAVO|Univerzitet Crne Gore|0421/0923 law/social work.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|PRAVO|Instituto '
         'Tecnológico y de Estudios Superiores de Monterrey|0421 law',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['MX'],
  'summary': 'Zagreb staff PRAVO, 0421 law: Instituto Tecnológico y de Estudios '
             'Superiores de Monterrey. Funded7activity+2travel days, travelEUR1735; '
             'eligible duration5days–2months is not all funded. Listed190EUR/day; '
             'travel allocation may conflict with generic guide, confirm actual award. '
             'STA academic teaching/combined8h/4h orSTT; nonteaching STT ownfaculty '
             'only; no conferences; academic otherfaculty fallback conditional no '
             'targeted applicant/samefield. Sep28,2026–Jun30,2027; July3 closing has '
             'erroneous weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track PRAVO|Instituto Tecnológico y de Estudios Superiores de '
           'Monterrey|0421 law.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/|RGN|Univerzitet '
         'u Tuzli|0532 earth sciences/mining/geology/petroleum; Biology wording',
  'title': 'Erasmus+ KA131: međunarodno otvaranje prema trećim zemljama koje nisu '
           'pridružene programu, razdoblje mobilnosti: 28.09.2026. - 30.06.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka131-medunarodno-otvaranje-prema-trecim-zemljama-koje-nisu-pridruzene-programu-razdob/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['BA'],
  'summary': 'Zagreb staff RGN, 0532 earth sciences/mining/geology/petroleum; Biology '
             'wording: Univerzitet u Tuzli. Funded5activity+1travel days, '
             'travelEUR211; eligible duration5days–2months is not all funded. '
             'Listed190EUR/day; travel allocation may conflict with generic guide, '
             'confirm actual award. STA teaching/combined academic only (8h/4h); '
             'academic otherfaculty fallback conditional no targeted '
             'applicant/samefield. Sep28,2026–Jun30,2027; July3 closing has erroneous '
             'weekday, date-only.',
  'deadline': '2026-07-03',
  'proof': 'Original own notice staff-latest-1; reviewed call and its integral literal '
           'materials; track RGN|Univerzitet u Tuzli|0532 earth '
           'sciences/mining/geology/petroleum; Biology wording.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka171-mobilnost-osoblja-za-trece-zemlje-koje-nisu-pridruzene-programu-razdoblje-mobiln/|AGR|Cadi '
         'Ayyad University|Chemistry department ONLY,0531/0711',
  'title': 'Erasmus+ KA171: mobilnost osoblja za treće zemlje koje nisu pridružene '
           'programu, razdoblje mobilnosti: 01.06.2026.-31.05.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka171-mobilnost-osoblja-za-trece-zemlje-koje-nisu-pridruzene-programu-razdoblje-mobiln/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['MA'],
  'summary': 'Zagreb staff AGR targeted Chemistry department ONLY,0531/0711: Cadi '
             'Ayyad University; teaching/combined academic only,8h/4h; conditional '
             'academic fallback if no targeted applicant/samefield. Funded5+2travel '
             'days, listed travelEUR395; guide180/day contradicts its own190 '
             'andallocation190, exact daily award unknown; generic travel also '
             'conflicts. Jun1,2026–May31,2027. Application May8date-only, not Dec14 '
             'disability-support request.',
  'deadline': '2026-05-08',
  'proof': 'Original own notice staff-latest-4; reviewed call and its integral literal '
           'materials; track AGR|Cadi Ayyad University|Chemistry department '
           'ONLY,0531/0711.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka171-mobilnost-osoblja-za-trece-zemlje-koje-nisu-pridruzene-programu-razdoblje-mobiln/|AGR|IPB '
         'University - Bogor|0888 agriculture/forestry/veterinary',
  'title': 'Erasmus+ KA171: mobilnost osoblja za treće zemlje koje nisu pridružene '
           'programu, razdoblje mobilnosti: 01.06.2026.-31.05.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka171-mobilnost-osoblja-za-trece-zemlje-koje-nisu-pridruzene-programu-razdoblje-mobiln/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['ID'],
  'summary': 'Zagreb staff AGR targeted 0888 agriculture/forestry/veterinary: IPB '
             'University - Bogor; teaching/combined academic only,8h/4h; conditional '
             'academic fallback if no targeted applicant/samefield. Funded5+2travel '
             'days, listed travelEUR1735; guide180/day contradicts its own190 '
             'andallocation190, exact daily award unknown; generic travel also '
             'conflicts. Jun1,2026–May31,2027. Application May8date-only, not Dec14 '
             'disability-support request.',
  'deadline': '2026-05-08',
  'proof': 'Original own notice staff-latest-4; reviewed call and its integral literal '
           'materials; track AGR|IPB University - Bogor|0888 '
           'agriculture/forestry/veterinary.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka171-mobilnost-osoblja-za-trece-zemlje-koje-nisu-pridruzene-programu-razdoblje-mobiln/|ADU|Univerzitet '
         'Crne Gore|0210 arts',
  'title': 'Erasmus+ KA171: mobilnost osoblja za treće zemlje koje nisu pridružene '
           'programu, razdoblje mobilnosti: 01.06.2026.-31.05.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka171-mobilnost-osoblja-za-trece-zemlje-koje-nisu-pridruzene-programu-razdoblje-mobiln/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['ME'],
  'summary': 'Zagreb staff ADU targeted 0210 arts: Univerzitet Crne Gore; '
             'teaching/combined academic only,8h/4h; conditional academic fallback if '
             'no targeted applicant/samefield. Funded5+1travel days, listed '
             'travelEUR211; guide180/day contradicts its own190 andallocation190, '
             'exact daily award unknown; generic travel also conflicts. '
             'Jun1,2026–May31,2027. Application May8date-only, not Dec14 '
             'disability-support request.',
  'deadline': '2026-05-08',
  'proof': 'Original own notice staff-latest-4; reviewed call and its integral literal '
           'materials; track ADU|Univerzitet Crne Gore|0210 arts.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka171-mobilnost-osoblja-za-trece-zemlje-koje-nisu-pridruzene-programu-razdoblje-mobiln/|FSB|Mohammed '
         'First University - Oujda|0710/0713/0715/0716 '
         'engineering/electrical/mechanical',
  'title': 'Erasmus+ KA171: mobilnost osoblja za treće zemlje koje nisu pridružene '
           'programu, razdoblje mobilnosti: 01.06.2026.-31.05.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka171-mobilnost-osoblja-za-trece-zemlje-koje-nisu-pridruzene-programu-razdoblje-mobiln/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['MA'],
  'summary': 'Zagreb staff FSB targeted 0710/0713/0715/0716 '
             'engineering/electrical/mechanical: Mohammed First University - Oujda; '
             'academic teaching8h/combined4h ortraining; nonteaching training '
             'ownfaculty only; conditional academic fallback if no targeted '
             'applicant/samefield. Funded5+2travel days, listed travelEUR309; '
             'guide180/day contradicts its own190 andallocation190, exact daily award '
             'unknown; generic travel also conflicts. Jun1,2026–May31,2027. '
             'Application May8date-only, not Dec14 disability-support request.',
  'deadline': '2026-05-08',
  'proof': 'Original own notice staff-latest-4; reviewed call and its integral literal '
           'materials; track FSB|Mohammed First University - Oujda|0710/0713/0715/0716 '
           'engineering/electrical/mechanical.'},
 {'key': "https://www.unizg.hr/nc/vijest/article/erasmus-ka171-mobilnost-osoblja-za-trece-zemlje-koje-nisu-pridruzene-programu-razdoblje-mobiln/|FSB|Xi'an "
         'Jiaotong University|0710/0713/0715/0716/0788 engineering/interdisciplinary',
  'title': 'Erasmus+ KA171: mobilnost osoblja za treće zemlje koje nisu pridružene '
           'programu, razdoblje mobilnosti: 01.06.2026.-31.05.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka171-mobilnost-osoblja-za-trece-zemlje-koje-nisu-pridruzene-programu-razdoblje-mobiln/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['CN'],
  'summary': 'Zagreb staff FSB targeted 0710/0713/0715/0716/0788 '
             "engineering/interdisciplinary: Xi'an Jiaotong University; "
             'teaching/combined academic only,8h/4h; conditional academic fallback if '
             'no targeted applicant/samefield. Funded7+2travel days, listed '
             'travelEUR1188; guide180/day contradicts its own190 andallocation190, '
             'exact daily award unknown; generic travel also conflicts. '
             'Jun1,2026–May31,2027. Application May8date-only, not Dec14 '
             'disability-support request.',
  'deadline': '2026-05-08',
  'proof': 'Original own notice staff-latest-4; reviewed call and its integral literal '
           "materials; track FSB|Xi'an Jiaotong University|0710/0713/0715/0716/0788 "
           'engineering/interdisciplinary.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka171-mobilnost-osoblja-za-trece-zemlje-koje-nisu-pridruzene-programu-razdoblje-mobiln/|GEOF|Benha '
         'University / Alexandria University|0532 geodesy/cartography/remote sensing',
  'title': 'Erasmus+ KA171: mobilnost osoblja za treće zemlje koje nisu pridružene '
           'programu, razdoblje mobilnosti: 01.06.2026.-31.05.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka171-mobilnost-osoblja-za-trece-zemlje-koje-nisu-pridruzene-programu-razdoblje-mobiln/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['EG'],
  'summary': 'Zagreb staff GEOF targeted 0532 geodesy/cartography/remote sensing: '
             'Benha University / Alexandria University; teaching/combined academic '
             'only,8h/4h; conditional academic fallback if no targeted '
             'applicant/samefield. Funded7+2travel days, listed travelEUR395; '
             'guide180/day contradicts its own190 andallocation190, exact daily award '
             'unknown; generic travel also conflicts. Jun1,2026–May31,2027. '
             'Application May8date-only, not Dec14 disability-support request.',
  'deadline': '2026-05-08',
  'proof': 'Original own notice staff-latest-4; reviewed call and its integral literal '
           'materials; track GEOF|Benha University / Alexandria University|0532 '
           'geodesy/cartography/remote sensing.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka171-mobilnost-osoblja-za-trece-zemlje-koje-nisu-pridruzene-programu-razdoblje-mobiln/|PMF|Cadi '
         'Ayyad University|Chemistry department ONLY,0531/0711',
  'title': 'Erasmus+ KA171: mobilnost osoblja za treće zemlje koje nisu pridružene '
           'programu, razdoblje mobilnosti: 01.06.2026.-31.05.2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-ka171-mobilnost-osoblja-za-trece-zemlje-koje-nisu-pridruzene-programu-razdoblje-mobiln/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['MA'],
  'summary': 'Zagreb staff PMF targeted Chemistry department ONLY,0531/0711: Cadi '
             'Ayyad University; academic teaching8h/combined4h ortraining; nonteaching '
             'training ownfaculty only; conditional academic fallback if no targeted '
             'applicant/samefield. Funded5+2travel days, listed travelEUR395; '
             'guide180/day contradicts its own190 andallocation190, exact daily award '
             'unknown; generic travel also conflicts. Jun1,2026–May31,2027. '
             'Application May8date-only, not Dec14 disability-support request.',
  'deadline': '2026-05-08',
  'proof': 'Original own notice staff-latest-4; reviewed call and its integral literal '
           'materials; track PMF|Cadi Ayyad University|Chemistry department '
           'ONLY,0531/0711.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-natjecaj-za-mobilnost-nastavnog-i-nenastavnog-osoblja-u-ak-god-20262027-drzave-cl/|outgoing-STA',
  'title': 'Erasmus+ natječaj za mobilnost nastavnog i nenastavnog osoblja u ak. god. '
           '2026./2027. - države članice EU-a i treće zemlje pridružene programu '
           '(KA131)',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-natjecaj-za-mobilnost-nastavnog-i-nenastavnog-osoblja-u-ak-god-20262027-drzave-cl/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': [],
  'summary': 'Zagreb academic staff teach/combined8h/4h under active faculty '
             'agreement; EU+6associated hosts.2–60days Oct1,2026–Sep30,2027 but funded '
             'max5activity+2travel.118/136/152EUR/day by destination, conditional '
             'travel/inclusion and zero-grant; no doubleEUfunding. Outgoing closes '
             'May28,2026 noon unzoned; residence/home destination exclusions and '
             'employment/host approval apply.',
  'deadline': '2026-05-28',
  'proof': 'Original own notice staff-latest-3; reviewed call and its integral literal '
           'materials; track outgoing-STA.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-natjecaj-za-mobilnost-nastavnog-i-nenastavnog-osoblja-u-ak-god-20262027-drzave-cl/|outgoing-STT',
  'title': 'Erasmus+ natječaj za mobilnost nastavnog i nenastavnog osoblja u ak. god. '
           '2026./2027. - države članice EU-a i treće zemlje pridružene programu '
           '(KA131)',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-natjecaj-za-mobilnost-nastavnog-i-nenastavnog-osoblja-u-ak-god-20262027-drzave-cl/',
  'category': 'training',
  'kind': 'opportunity',
  'hosts': [],
  'summary': 'Zagreb academic/nonacademic staff training at EU+6associated ECHE/legal '
             'host; no interinstitutional agreement required. '
             'Research/conferences/thesis/library visits excluded except '
             'librarians.2–60days Oct1,2026–Sep30,2027 but funded '
             'max5activity+2travel;118/136/152EUR/day by country, conditional '
             'travel/inclusion/zero-grant, no doubleEUfunding. May28,2026 noon '
             'unzoned. Residence/home exclusions and employer/host approval apply.',
  'deadline': '2026-05-28',
  'proof': 'Original own notice staff-latest-3; reviewed call and its integral literal '
           'materials; track outgoing-STT.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/erasmus-natjecaj-za-mobilnost-nastavnog-i-nenastavnog-osoblja-u-ak-god-20262027-drzave-cl/|incoming-company-teaching',
  'title': 'Erasmus+ natječaj za mobilnost nastavnog i nenastavnog osoblja u ak. god. '
           '2026./2027. - države članice EU-a i treće zemlje pridružene programu '
           '(KA131)',
  'url': 'https://www.unizg.hr/nc/vijest/article/erasmus-natjecaj-za-mobilnost-nastavnog-i-nenastavnog-osoblja-u-ak-god-20262027-drzave-cl/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'summary': 'Croatian constituent applies for incoming NONTEACHING '
             'company/research-organisation staff from EU+6associated countries to '
             'teach; not any overseas university employee.1–60days '
             'Oct1,2026–Sep30,2027 but max5activity+2travel funded,148EUR/day plus '
             'conditional travel/inclusion. Wider FAQ/instructions country language '
             'does not expand explicit current call. Incoming institution closes May28 '
             'date-only (outgoing noon is separate). Host approval, no '
             'doubleEUfunding.',
  'deadline': '2026-05-28',
  'proof': 'Original own notice staff-latest-3; reviewed call and its integral literal '
           'materials; track incoming-company-teaching.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-kombinirane-intenzivne-programe-u-okviru-erasmus-programa-kljucne-aktivnosti-1-unutar-1/|BIP',
  'title': 'NATJEČAJ za kombinirane intenzivne programe u okviru Erasmus+ programa '
           'ključne aktivnosti 1 unutar programskih zemalja (KA131) za razdoblje od 1. '
           'lipnja 2026. do 30. lipnja 2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-kombinirane-intenzivne-programe-u-okviru-erasmus-programa-kljucne-aktivnosti-1-unutar-1/',
  'category': 'grants',
  'kind': 'institutional-grant',
  'hosts': [],
  'summary': 'Institutional constituent application AFTER transparent participant '
             'selection, not open personal stipend.3HEIs/3countries,≥10 funded '
             'crossborder learners/3ECTS; virtual+5–30physicaldays '
             'Jun1,2026–Jun30,2027. Organisational EUR400/learner for10–20 '
             '=4000–8000PROJECT,10%toUniversity. Students79/day '
             'max12+2travel/conditional100; staff max5+2travel, not full costs. '
             'March5,2027 closing or earlier budget/300capacity;3month leadtime '
             'exceptJun/Jul2026. Old15minimum is stale.',
  'deadline': '2027-03-05',
  'proof': 'Original own notice staff-latest-2; reviewed call and its integral literal '
           'materials; track BIP.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-akademsku-mobilnost-u-2026-godini-prvi-krug/|a',
  'title': 'Natječaj za akademsku mobilnost u 2026. godini (prvi krug)',
  'url': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-akademsku-mobilnost-u-2026-godini-prvi-krug/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': [],
  'summary': 'Zagreb employees in designated academic/assistant/postdoc ranks; '
             'Strategic-partner teaching/cooperation; reimbursement of actual costs '
             'cappedEUR800 Europe/Turkey or2000elsewhere, not flat guaranteed grant. '
             'Eligible window whole2026. One funded mobility per person/year, emeriti '
             'excluded; whole annualEUR166000 is institutional budget. Online '
             'Oct9,2025 noon unzoned PLUS variable earlier faculty internal deadlines; '
             'Oct16 institutional handoff is not individual closing.',
  'deadline': '2025-10-09',
  'proof': 'Original own notice academic2026-first-round; reviewed call and its '
           'integral literal materials; track a.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-akademsku-mobilnost-u-2026-godini-prvi-krug/|b',
  'title': 'Natječaj za akademsku mobilnost u 2026. godini (prvi krug)',
  'url': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-akademsku-mobilnost-u-2026-godini-prvi-krug/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': [],
  'summary': 'Zagreb employees in designated academic/assistant/postdoc ranks; '
             'Worldwide HEI/research cooperation, regionalEUR550–1700actual-cost caps; '
             'not pure research/conference/Erasmus; second round excludes strategic '
             'partners. Eligible window firsthalf2026 throughJun30 (July10exception). '
             'One funded mobility per person/year, emeriti excluded; whole '
             'annualEUR166000 is institutional budget. Online Oct9,2025 noon unzoned '
             'PLUS variable earlier faculty internal deadlines; Oct16 institutional '
             'handoff is not individual closing.',
  'deadline': '2025-10-09',
  'proof': 'Original own notice academic2026-first-round; reviewed call and its '
           'integral literal materials; track b.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-akademsku-mobilnost-u-2026-godini-prvi-krug/|c',
  'title': 'Natječaj za akademsku mobilnost u 2026. godini (prvi krug)',
  'url': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-akademsku-mobilnost-u-2026-godini-prvi-krug/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': [],
  'summary': 'Zagreb PhD enrolled at BOTH application and mobility; Zagreb PhD active '
             'conference presentation/poster; registration ONLY cappedEUR300, not '
             'travel/living. Eligible window firsthalf2026 throughJun30 '
             '(July10exception). One funded mobility per person/year, emeriti '
             'excluded; whole annualEUR166000 is institutional budget. Online '
             'Oct9,2025 noon unzoned PLUS variable earlier faculty internal deadlines; '
             'Oct16 institutional handoff is not individual closing.',
  'deadline': '2025-10-09',
  'proof': 'Original own notice academic2026-first-round; reviewed call and its '
           'integral literal materials; track c.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-akademsku-mobilnost-u-2026-godini-prvi-krug/|d',
  'title': 'Natječaj za akademsku mobilnost u 2026. godini (prvi krug)',
  'url': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-akademsku-mobilnost-u-2026-godini-prvi-krug/',
  'category': 'grants',
  'kind': 'institutional-grant',
  'hosts': ['HR'],
  'summary': 'Croatian University constituent host; INSTITUTIONAL incoming '
             'bilateral/new-cooperation host lodging cappedEUR110/day×5, conditional '
             'funds; not personal cash stipend. Eligible window whole2026. One funded '
             'mobility per person/year, emeriti excluded; whole annualEUR166000 is '
             'institutional budget. No d-specific individual closing proof; faculty '
             'procedure/date unknown, not borrowed froma/b/c.',
  'deadline': None,
  'proof': 'Original own notice academic2026-first-round; reviewed call and its '
           'integral literal materials; track d.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-akademsku-mobilnost-u-2026-godini-drugi-krug/|b',
  'title': 'Natječaj za akademsku mobilnost u 2026. godini (drugi krug)',
  'url': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-akademsku-mobilnost-u-2026-godini-drugi-krug/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': [],
  'summary': 'Zagreb employees in designated academic/assistant/postdoc ranks; '
             'Worldwide HEI/research cooperation, regionalEUR550–1700actual-cost caps; '
             'not pure research/conference/Erasmus; second round excludes strategic '
             'partners. Eligible window July1–Dec31,2026 (June21start exception). One '
             'funded mobility per person/year, emeriti excluded; whole annualEUR166000 '
             'is institutional budget. Online Apr9,2026 noon unzoned PLUS variable '
             'earlier faculty internal deadlines; Apr16 institutional handoff is not '
             'individual closing.',
  'deadline': '2026-04-09',
  'proof': 'Original own notice staff-latest-5; reviewed call and its integral literal '
           'materials; track b.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-akademsku-mobilnost-u-2026-godini-drugi-krug/|c',
  'title': 'Natječaj za akademsku mobilnost u 2026. godini (drugi krug)',
  'url': 'https://www.unizg.hr/nc/vijest/article/natjecaj-za-akademsku-mobilnost-u-2026-godini-drugi-krug/',
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': [],
  'summary': 'Zagreb PhD enrolled at BOTH application and mobility; Zagreb PhD active '
             'conference presentation/poster; registration ONLY cappedEUR300, not '
             'travel/living. Eligible window July1–Dec31,2026 (June21start exception). '
             'One funded mobility per person/year, emeriti excluded; whole '
             'annualEUR166000 is institutional budget. Online Apr9,2026 noon unzoned '
             'PLUS variable earlier faculty internal deadlines; Apr16 institutional '
             'handoff is not individual closing.',
  'deadline': '2026-04-09',
  'proof': 'Original own notice staff-latest-5; reviewed call and its integral literal '
           'materials; track c.'},
 {'key': 'https://www.unizg.hr/istrazivanje/istrazivanje-i-inovacije/financiranje-istrazivanja/otvoreni-natjecaji/|UNIC2023',
  'title': 'Poziv za prijave za drugi krug natječaja za dodjelu početnih sredstava za '
           'suradničke inicijative u području društveno angažiranog istraživanja',
  'url': 'https://www.unizg.hr/istrazivanje/istrazivanje-i-inovacije/financiranje-istrazivanja/otvoreni-natjecaji/',
  'category': 'grants',
  'kind': 'institutional-grant',
  'hosts': [],
  'summary': 'Closed2023 collaborative societally engaged research SEED call for '
             'institutional partnerships, not individual scholarship. WholeEUR13782.50 '
             'budget,≥19 partnerships, up to9months; mandatory interest '
             'submissionJune22 precedes fullJuly28application. UNIC institutional '
             'collaboration/actual eligibility applies; no2026 call inferred from '
             'current hub. Current reopening unknown.',
  'deadline': '2023-07-28',
  'proof': 'Original own notice research-open; reviewed call and its integral literal '
           'materials; track UNIC2023.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/bayhost-stipendije-u-bavarskoj-za-ak-god-20272028/|BAYHOST2027-28',
  'title': 'BAYHOST stipendije u Bavarskoj za ak. god. 2027./2028.',
  'url': 'https://www.unizg.hr/nc/vijest/article/bayhost-stipendije-u-bavarskoj-za-ak-god-20272028/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['DE'],
  'summary': 'BAYHOST2027/28 Bavarian postgraduate/PhD/research scholarship; specified '
             'Bulgaria/Croatia/Poland/Romania/Russia/Serbia/Slovakia/Czech/Hungary/Ukraine '
             'citizenship applies; prior degree required. EUR992/month, conditional '
             'subsequent renewal, not guaranteed multi-year award; Further host entry '
             'requirements unknown in own notice. Own closing Dec1,2026 date-only; '
             'Bavaria host, not publisher country.',
  'deadline': '2026-12-01',
  'proof': 'Original own notice ancillary-current-1; reviewed call and its integral '
           'literal materials; track BAYHOST2027-28.',
  'eligible_countries': ['BG', 'HR', 'CZ', 'HU', 'PL', 'RO', 'RU', 'RS', 'SK', 'UA']},
 {'key': 'https://www.unizg.hr/nc/vijest/article/bayhost-stipendije-u-bavarskoj-za-ak-god-20262027/|BAYHOST2026-27',
  'title': 'BAYHOST stipendije u Bavarskoj za ak. god. 2026./2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/bayhost-stipendije-u-bavarskoj-za-ak-god-20262027/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['DE'],
  'summary': 'BAYHOST2026/27 Bavarian postgraduate/PhD/research scholarship; specified '
             'Bulgaria/Croatia/Poland/Romania/Russia/Serbia/Slovakia/Czech/Hungary/Ukraine '
             'citizenship applies; prior degree required. EUR992/month, conditional '
             'renewal, not guaranteed multi-year award. Further host entry '
             'requirements unknown in own notice. Own closing Dec1,2025 date-only; '
             'distinct actually announced edition from2027/28.',
  'deadline': '2025-12-01',
  'proof': 'Original own notice ancillary-current-5; reviewed call and its integral '
           'literal materials; track BAYHOST2026-27.',
  'eligible_countries': ['BG', 'HR', 'CZ', 'HU', 'PL', 'RO', 'RU', 'RS', 'SK', 'UA']},
 {'key': 'https://www.unizg.hr/nc/vijest/article/taiwan-scholarships-2026-stipendije-za-redoviti-studij-i-tecaj-mandarinskog-kineskog-na-tajvanu/|Taiwan-degree',
  'title': 'TAIWAN SCHOLARSHIPS 2026: STIPENDIJE ZA REDOVITI STUDIJ I TEČAJ '
           'MANDARINSKOG KINESKOG NA TAJVANU',
  'url': 'https://www.unizg.hr/nc/vijest/article/taiwan-scholarships-2026-stipendije-za-redoviti-studij-i-tecaj-mandarinskog-kineskog-na-tajvanu/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['TW'],
  'summary': 'Taiwan full-degree scholarship2026/27,3awards via Taipei representative '
             'office in Austria for candidates from Croatia/Austria/Slovenia; '
             'affiliation/territory wording is not automatic passport restriction. '
             'Personal amount AND coverage unknown in own notice; no tuition/living '
             'coverage inferred. Deadline Apr15,2026 date-only; host academic/language '
             'admission separate.',
  'deadline': '2026-04-15',
  'proof': 'Original own notice ancillary-current-2; reviewed call and its integral '
           'literal materials; track Taiwan-degree.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/taiwan-scholarships-2026-stipendije-za-redoviti-studij-i-tecaj-mandarinskog-kineskog-na-tajvanu/|Taiwan-HES',
  'title': 'TAIWAN SCHOLARSHIPS 2026: STIPENDIJE ZA REDOVITI STUDIJ I TEČAJ '
           'MANDARINSKOG KINESKOG NA TAJVANU',
  'url': 'https://www.unizg.hr/nc/vijest/article/taiwan-scholarships-2026-stipendije-za-redoviti-studij-i-tecaj-mandarinskog-kineskog-na-tajvanu/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['TW'],
  'summary': 'Taiwan Huayu Enrichment Scholarship2026/27 for Mandarin '
             'study,3/6/9/12months. Whole programme pool72months is not72months per '
             'student. Taipei representative office in Austria notice addresses '
             'Croatia/Austria/Slovenia; no inferred passport restriction. Exact '
             'personal amount and entry terms not stated in own notice. Apr15,2026 '
             'date-only closing.',
  'deadline': '2026-04-15',
  'proof': 'Original own notice ancillary-current-2; reviewed call and its integral '
           'literal materials; track Taiwan-HES.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/you-and-europe-program-stipendija-za-europske-studente-diplomskih-studija-u-njemackoj/|You-and-Europe',
  'title': 'You and Europe | Program stipendija za europske studente diplomskih '
           'studija u Njemačkoj',
  'url': 'https://www.unizg.hr/nc/vijest/article/you-and-europe-program-stipendija-za-europske-studente-diplomskih-studija-u-njemackoj/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['DE'],
  'summary': 'You and Europe/Alfred Toepfer+Studienstiftung full German master '
             'programme; financial need and top10–15%students, fine arts excluded; '
             'actual own country/programme eligibility applies, not inferred Croatian '
             'passport. Needs-based EUR300–1155/month; professor nomination Feb4,2026 '
             'is prerequisite before Feb23 full application. Admission/language '
             'conditions separate, support is conditional.',
  'deadline': '2026-02-23',
  'proof': 'Original own notice ancillary-current-3; reviewed call and its integral '
           'literal materials; track You-and-Europe.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/hardiman-doktorske-stipendije-na-university-of-galway-2026/|Hardiman2026',
  'title': 'Hardiman doktorske stipendije na University of Galway 2026',
  'url': 'https://www.unizg.hr/nc/vijest/article/hardiman-doktorske-stipendije-na-university-of-galway-2026/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['IE'],
  'summary': 'University of Galway Hardiman structured PhD2026 for new doctoral '
             'entrants; four years with EUR25000/year plus tuition, subject to '
             'selection/admission and research-supervisor conditions. Deadline '
             'Feb6,2026 at17:00 unzoned (date-only normalized); not whole-project '
             'budget. Own notice does not establish passport restriction.',
  'deadline': '2026-02-06',
  'proof': 'Original own notice ancillary-current-4; reviewed call and its integral '
           'literal materials; track Hardiman2026.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/stipendija-za-studij-na-georgia-institute-of-technology-ak-god-20262027-1/|Georgia2026-27',
  'title': 'Stipendija za studij na Georgia Institute of Technology - ak. god. '
           '2026./2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/stipendija-za-studij-na-georgia-institute-of-technology-ak-god-20262027-1/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['US'],
  'summary': 'Naumann-Etienne/Georgia Tech Atlanta2026/27 master scholarship in seven '
             'named engineering/computing fields, actual extended closing Nov23,2025 '
             'replaces Oct24 same call. Up toUSD1500/month living support '
             'plusUSD1500travel and tuition waiver; host acceptance/selection and own '
             'exchange eligibility apply. This is not prior2025/26 edition’s imported '
             'benefit. Bachelor degree, good academic results and Georgia Tech '
             'TOEFL/admission criteria apply; up to2years.',
  'deadline': '2025-11-23',
  'proof': 'Original own notice ancillary-current-6; reviewed call and its integral '
           'literal materials; track Georgia2026-27.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/stipendije-za-ucenje-islandskog-jezika-u-ak-god-20262027/|Iceland2026-27',
  'title': 'Stipendije za učenje islandskog jezika u ak. god. 2026./2027.',
  'url': 'https://www.unizg.hr/nc/vijest/article/stipendije-za-ucenje-islandskog-jezika-u-ak-god-20262027/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['IS'],
  'summary': 'Icelandic language and literature2026/27 at University of Iceland; '
             'about11awards, prior Icelandic knowledge AND at least1year university '
             'study required. Exact personal amount/coverage unknown in own notice; '
             'closing Dec1,2025 date-only. This current edition replaces older own '
             'notice, no imported prior benefit or inferred citizenship.',
  'deadline': '2025-12-01',
  'proof': 'Original own notice ancillary-current-7; reviewed call and its integral '
           'literal materials; track Iceland2026-27.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/bose-program-stipendiranja-ak-god-202627/|BOSE2026',
  'title': 'BOSE program stipendiranja ak. god. 2026./27.',
  'url': 'https://www.unizg.hr/nc/vijest/article/bose-program-stipendiranja-ak-god-202627/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['AT'],
  'summary': 'Sparkasse/Leoben Best of South East2026/27 in Austria; own notice '
             'contradicts EUR1000/year with EUR12000total, so exact personal '
             'amount/period requires publisher clarification. 12months '
             'including2months practice; students from SI/HR/BA/RS/ME/MK; country '
             'origin not inferred passport. No guessed1000monthly. Closing Jan30,2026 '
             'date-only. Own administrator credited; no external catalogue '
             'substitution.',
  'deadline': '2026-01-30',
  'proof': 'Original own notice ancillary-current-8; reviewed call and its integral '
           'literal materials; track BOSE2026.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/azrieli-international-postdoctoral-i-visiting-phd-fellowship-stipendije/|postdoc2026-27',
  'title': 'Azrieli International Postdoctoral i Visiting PhD Fellowship stipendije',
  'url': 'https://www.unizg.hr/nc/vijest/article/azrieli-international-postdoctoral-i-visiting-phd-fellowship-stipendije/',
  'category': 'fellowships',
  'kind': 'opportunity',
  'hosts': ['IL'],
  'summary': 'Azrieli postdoctoral2026/27 research in all Israeli institution fields, '
             'up to3years; own closing 2025-11-19 date-only. Two genuinely different '
             'levels/editions; duplicate notices deduplicated. Personal '
             'amount/coverage and full entry criteria unknown in own notice. Older '
             'Fall2025 ILS12000 is NOT imported. Announced future Fall application '
             'season is not a separate complete call.',
  'deadline': '2025-11-19',
  'proof': 'Original own notice ancillary-ambiguous-41; reviewed call and its integral '
           'literal materials; track postdoc2026-27.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/azrieli-international-postdoctoral-i-visiting-phd-fellowship-stipendije/|PhD-Spring2026',
  'title': 'Azrieli International Postdoctoral i Visiting PhD Fellowship stipendije',
  'url': 'https://www.unizg.hr/nc/vijest/article/azrieli-international-postdoctoral-i-visiting-phd-fellowship-stipendije/',
  'category': 'fellowships',
  'kind': 'opportunity',
  'hosts': ['IL'],
  'summary': 'Azrieli doctoral Spring2026 short research at Israeli institutions; own '
             'closing 2025-10-27 date-only. Two genuinely different levels/editions; '
             'duplicate notices deduplicated. Personal amount/coverage and full entry '
             'criteria unknown in own notice. Older Fall2025 ILS12000 is NOT imported. '
             'Announced future Fall application season is not a separate complete '
             'call.',
  'deadline': '2025-10-27',
  'proof': 'Original own notice ancillary-ambiguous-41; reviewed call and its integral '
           'literal materials; track PhD-Spring2026.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/sveuciliste-u-zuerichu-stipendija/|Zurich-standing',
  'title': 'Sveučilište u Zürichu - stipendija',
  'url': 'https://www.unizg.hr/nc/vijest/article/sveuciliste-u-zuerichu-stipendija/',
  'category': 'scholarships',
  'kind': 'programme-overview',
  'hosts': ['CH'],
  'summary': 'Zurich standing exchange support for financially constrained students '
             'under a valid Zagreb–Zurich agreement in '
             'economics/politics/law/communications/journalism. CHF1500 per SEMESTER '
             'additional to CHF2200SEMP support, not monthly/full tuition guarantee. '
             'Academic host acceptance and financial-need conditions apply. Undated '
             'substantive framework; no invented2026edition, closing/current '
             'availability unknown.',
  'deadline': None,
  'proof': 'Original own notice ancillary-ambiguous-39; reviewed call and its integral '
           'literal materials; track Zurich-standing.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/mofa-taiwan-fellowship/|Taiwan-MOFA2026',
  'title': 'MOFA Taiwan Fellowship',
  'url': 'https://www.unizg.hr/nc/vijest/article/mofa-taiwan-fellowship/',
  'category': 'fellowships',
  'kind': 'opportunity',
  'hosts': ['TW'],
  'summary': 'Taiwan MOFA2026 research fellowship for researchers affiliated with '
             'foreign universities/research institutions; affiliation is not '
             'citizenship. Research on Taiwan politics/economics, diplomacy, cable '
             'security, supply chains, disinformation, gender, '
             'cross-strait/Asia-Pacific/Sinology. Actual window May1–Jun30,2026, '
             'closing June30 date-only. Personal amount, detailed coverage and '
             'duration absent in own notice; no external applicant document '
             'substituted.',
  'deadline': '2026-06-30',
  'proof': 'Original own notice ancillary-current-13; reviewed call and its integral '
           'literal materials; track Taiwan-MOFA2026.',
  'language': 'en'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/bayhost-sipendije-za-tecajeve-njemackog-jezika-u-bavarskoj-2026/|BAYHOST-summer2026',
  'title': 'BAYHOST sipendije za tečajeve njemačkog jezika u Bavarskoj 2026.',
  'url': 'https://www.unizg.hr/nc/vijest/article/bayhost-sipendije-za-tecajeve-njemackog-jezika-u-bavarskoj-2026/',
  'category': 'scholarships',
  'kind': 'opportunity',
  'hosts': ['DE'],
  'summary': 'BAYHOST2026 summer German-language scholarship at Bavarian universities; '
             'German≥B1 and at least2 completed semesters, with own specified country '
             'citizenship list. Amount/coverage unknown in own notice; do not promise '
             'free/full costs. Closing March23,2026 date-only; summer event date is '
             'separate.',
  'deadline': '2026-03-23',
  'proof': 'Original own notice ancillary-current-36; reviewed call and its integral '
           'literal materials; track BAYHOST-summer2026.',
  'eligible_countries': ['AL', 'BA', 'HR', 'XK', 'ME', 'MK', 'RS', 'SI', 'UA']},
 {'key': 'https://www.unizg.hr/nc/vijest/article/universite-dete-francophone-en-relations-internationales-oif-bukurest-srpanj-2026-pot/|OIF2026',
  'title': 'Université d’été francophone en relations internationales - OIF (Bukurešt, '
           'srpanj 2026.) - potpora za putovanje i smještaj',
  'url': 'https://www.unizg.hr/nc/vijest/article/universite-dete-francophone-en-relations-internationales-oif-bukurest-srpanj-2026-pot/',
  'category': 'training',
  'kind': 'opportunity',
  'hosts': ['RO'],
  'summary': 'OIF/POLITEHNICA Bucharest international-relations Francophone school '
             'July6–11,2026. Students/2024–25graduates in international '
             'relations/politics/journalism/translation/law, age20–28 on first day, '
             'French≥B2 and explicit21-country citizenship list. Organiser covers '
             'capital–Bucharest air/rail, travel insurance, lodging, meals and '
             'cultural visits. Apr10closing distinct from event; exceptional '
             'late/contact route is not a guaranteed extension.',
  'deadline': '2026-04-10',
  'proof': 'Original own notice ancillary-current-14; reviewed call and its integral '
           'literal materials; track OIF2026.',
  'eligible_countries': ['AL',
                         'AM',
                         'BA',
                         'BG',
                         'HR',
                         'EE',
                         'GE',
                         'HU',
                         'XK',
                         'LV',
                         'LT',
                         'MK',
                         'MD',
                         'ME',
                         'RO',
                         'PL',
                         'RS',
                         'SK',
                         'SI',
                         'CZ',
                         'UA']},
 {'key': 'https://www.unizg.hr/nc/vijest/article/ljetna-skola-u-rio-de-janeiru/|Rio2026',
  'title': 'Ljetna škola u Rio de Janeiru',
  'url': 'https://www.unizg.hr/nc/vijest/article/ljetna-skola-u-rio-de-janeiru/',
  'category': 'training',
  'kind': 'opportunity',
  'hosts': ['BR'],
  'summary': 'PUC-Rio2026 business/Portuguese educational programme July9–24. Own '
             'closing May30,2026, April30early-bird is NOT final deadline. Fees, entry '
             'requirements and actual current availability not stated in own notice; '
             'no free/full-cost promise. Concrete learning theme and event edition '
             'support this limited educational notice.',
  'deadline': '2026-05-30',
  'proof': 'Original own notice ancillary-current-37; reviewed call and its integral '
           'literal materials; track Rio2026.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/secoias-summer-school-2026-security-and-communication-in-aerospace-embedded-systems-francuska/|SECOIAS2026',
  'title': 'SECOIAS Summer School 2026 – Security and Communication In Aerospace '
           'Embedded Systems, Francuska',
  'url': 'https://www.unizg.hr/nc/vijest/article/secoias-summer-school-2026-security-and-communication-in-aerospace-embedded-systems-francuska/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': ['FR'],
  'summary': 'INSA Toulouse security/communication in aerospace embedded systems; '
             'Jun22–Jul3,2026. Published educational overview; fees, prerequisites, '
             'registration closing and current enrolment unknown. Event dates are NOT '
             'application deadlines. No external catalogue/application material '
             'substituted.',
  'deadline': None,
  'proof': 'Original own notice ancillary-current-15; reviewed call and its integral '
           'literal materials; track SECOIAS2026.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/minato-international-summer-school-on-micro-and-nano-fabrication-toulouse-francuska/|MINATO2026',
  'title': 'MINATO International Summer School on Micro and Nano-fabrication, Toulouse '
           '(Francuska)',
  'url': 'https://www.unizg.hr/nc/vijest/article/minato-international-summer-school-on-micro-and-nano-fabrication-toulouse-francuska/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': ['FR'],
  'summary': 'Toulouse micro/nanofabrication MINATO; June/July2026,2or4weeks. '
             'Published educational overview; fees, prerequisites, registration '
             'closing and current enrolment unknown. Event dates are NOT application '
             'deadlines. No external catalogue/application material substituted.',
  'deadline': None,
  'proof': 'Original own notice ancillary-current-16; reviewed call and its integral '
           'literal materials; track MINATO2026.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/summer-school-universite-de-lorraine-francuska/|Art-Tech-Inclusion2026',
  'title': 'Summer School - Université de Lorraine, Francuska',
  'url': 'https://www.unizg.hr/nc/vijest/article/summer-school-universite-de-lorraine-francuska/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': ['FR'],
  'summary': 'Université de Lorraine Art, Tech and Inclusion Summer Lab; '
             'Jul13–24,2026. Published educational overview; fees, prerequisites, '
             'registration closing and current enrolment unknown. Event dates are NOT '
             'application deadlines. No external catalogue/application material '
             'substituted.',
  'deadline': None,
  'proof': 'Original own notice ancillary-current-19; reviewed call and its integral '
           'literal materials; track Art-Tech-Inclusion2026.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/ljetna-skola-the-european-system-of-human-rights-protection-council-of-europe-eu-osce-njem/|Human-rights2026',
  'title': 'Ljetna škola "The European system of human rights protection – Council of '
           'Europe, EU, OSCE" (Njemačka)',
  'url': 'https://www.unizg.hr/nc/vijest/article/ljetna-skola-the-european-system-of-human-rights-protection-council-of-europe-eu-osce-njem/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': ['DE'],
  'summary': 'Viadrina Frankfurt(Oder) European human-rights protection/Council of '
             'Europe/EU/OSCE; Jul6–17,2026. Published educational overview; fees, '
             'prerequisites, registration closing and current enrolment unknown. Event '
             'dates are NOT application deadlines. No external catalogue/application '
             'material substituted.',
  'deadline': None,
  'proof': 'Original own notice ancillary-current-21; reviewed call and its integral '
           'literal materials; track Human-rights2026.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/international-summer-school-oth-regensburg-njemacka/|Trustworthy-AI2026',
  'title': 'International Summer School - OTH Regensburg, Njemačka',
  'url': 'https://www.unizg.hr/nc/vijest/article/international-summer-school-oth-regensburg-njemacka/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': ['DE'],
  'summary': 'OTH Regensburg Trustworthy AI–Machine Learning Meets Blockchain, '
             'Aug10–14AND17–21,2026; one programme/two weeks, not two invented calls. '
             'Published educational overview; fees, prerequisites, registration '
             'closing and current enrolment unknown. Event dates are NOT application '
             'deadlines. No external catalogue/application material substituted.',
  'deadline': None,
  'proof': 'Original own notice ancillary-current-22; reviewed call and its integral '
           'literal materials; track Trustworthy-AI2026.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/universidad-loyolas-summer-programme-in-spanish-language-culture/|Loyola-Spanish2026',
  'title': 'Universidad Loyola’s Summer Programme in Spanish Language & Culture',
  'url': 'https://www.unizg.hr/nc/vijest/article/universidad-loyolas-summer-programme-in-spanish-language-culture/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': ['ES'],
  'summary': 'Loyola Spanish Language and Culture at Cordoba campus, Jun10–30,2026. '
             'Published educational overview; fees, prerequisites, registration '
             'closing and current enrolment unknown. Event dates are NOT application '
             'deadlines. No external catalogue/application material substituted.',
  'deadline': None,
  'proof': 'Original own notice ancillary-current-24; reviewed call and its integral '
           'literal materials; track Loyola-Spanish2026.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/lidd-design-summer-camp/|LiDD2026',
  'title': 'LiDD Design Summer Camp',
  'url': 'https://www.unizg.hr/nc/vijest/article/lidd-design-summer-camp/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': ['FR'],
  'summary': 'LiDD Design School, Université Catholique de Lille design summer camp '
             'Jun23–Jul22,2026. Published educational overview; fees, prerequisites, '
             'registration closing and current enrolment unknown. Event dates are NOT '
             'application deadlines. No external catalogue/application material '
             'substituted.',
  'deadline': None,
  'proof': 'Original own notice ancillary-current-32; reviewed call and its integral '
           'literal materials; track LiDD2026.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/international-summer-university-isu-osnabrueck-campus-njemacka/|ISU2026',
  'title': 'International Summer University (ISU), Osnabrück Campus (Njemačka)',
  'url': 'https://www.unizg.hr/nc/vijest/article/international-summer-university-isu-osnabrueck-campus-njemacka/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': ['DE'],
  'summary': 'Osnabrück Jul9–31,2026:AI ethics/sustainability, global marketplace, '
             'health psychology, healthcare management, clinical physiotherapy and '
             'food technologies. Own BA/MA levels vary by theme; one grouped notice, '
             'not six unproved calls. Published educational overview; fees, '
             'prerequisites, registration closing and current enrolment unknown. Event '
             'dates are NOT application deadlines. No external catalogue/application '
             'material substituted.',
  'deadline': None,
  'proof': 'Original own notice ancillary-current-27; reviewed call and its integral '
           'literal materials; track ISU2026.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/rennes-school-of-business-francuska/|Rennes2026',
  'title': 'Rennes School of Business, Francuska',
  'url': 'https://www.unizg.hr/nc/vijest/article/rennes-school-of-business-francuska/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': ['FR'],
  'summary': 'Rennes May/June/July2026 grouped themes:AIbusiness,digital '
             'marketing/branding,sustainable business,corporate finance in '
             'French,cross-cultural management; per-course dates unknown. Published '
             'educational overview; fees, prerequisites, registration closing and '
             'current enrolment unknown. Event dates are NOT application deadlines. No '
             'external catalogue/application material substituted.',
  'deadline': None,
  'proof': 'Original own notice ancillary-current-28; reviewed call and its integral '
           'literal materials; track Rennes2026.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/via-summer-school-danska/|VIA2026',
  'title': 'VIA Summer School (Danska)',
  'url': 'https://www.unizg.hr/nc/vijest/article/via-summer-school-danska/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': ['DK'],
  'summary': 'VIA August2026 grouped themes:17UN sustainable-development '
             'goals,entrepreneurship,ToonBoomHarmony2Danimation,comic tools; '
             'per-course dates unknown. Published educational overview; fees, '
             'prerequisites, registration closing and current enrolment unknown. Event '
             'dates are NOT application deadlines. No external catalogue/application '
             'material substituted.',
  'deadline': None,
  'proof': 'Original own notice ancillary-current-29; reviewed call and its integral '
           'literal materials; track VIA2026.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/catolica-porto-business-school-international-summer-school-programmes/|Deep-Tech-Business',
  'title': 'Católica Porto Business School - International Summer School Programmes',
  'url': 'https://www.unizg.hr/nc/vijest/article/catolica-porto-business-school-international-summer-school-programmes/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': ['PT'],
  'summary': 'Católica Porto Deep Tech and Business Case, Jul6–17,2026. Published '
             'educational overview; fees, prerequisites, registration closing and '
             'current enrolment unknown. Event dates are NOT application deadlines. No '
             'external catalogue/application material substituted.',
  'deadline': None,
  'proof': 'Original own notice ancillary-current-26; reviewed call and its integral '
           'literal materials; track Deep-Tech-Business.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/catolica-porto-business-school-international-summer-school-programmes/|Machine-Learning',
  'title': 'Católica Porto Business School - International Summer School Programmes',
  'url': 'https://www.unizg.hr/nc/vijest/article/catolica-porto-business-school-international-summer-school-programmes/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': ['PT'],
  'summary': 'Católica Porto Machine Learning, Jul20–24,2026. Published educational '
             'overview; fees, prerequisites, registration closing and current '
             'enrolment unknown. Event dates are NOT application deadlines. No '
             'external catalogue/application material substituted.',
  'deadline': None,
  'proof': 'Original own notice ancillary-current-26; reviewed call and its integral '
           'literal materials; track Machine-Learning.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/bsb-summer-school-2026-francuska/|Authentic-Leadership',
  'title': 'BSB Summer School 2026 (Francuska)',
  'url': 'https://www.unizg.hr/nc/vijest/article/bsb-summer-school-2026-francuska/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': ['FR'],
  'summary': 'BSB Dijon Authentic Leadership and Human-Centered Innovation/Design '
             'Thinking, Jun22–26,2026. Published educational overview; fees, '
             'prerequisites, registration closing and current enrolment unknown. Event '
             'dates are NOT application deadlines. No external catalogue/application '
             'material substituted.',
  'deadline': None,
  'proof': 'Original own notice ancillary-current-30; reviewed call and its integral '
           'literal materials; track Authentic-Leadership.'},
 {'key': 'https://www.unizg.hr/nc/vijest/article/bsb-summer-school-2026-francuska/|Empowering-Sustainability',
  'title': 'BSB Summer School 2026 (Francuska)',
  'url': 'https://www.unizg.hr/nc/vijest/article/bsb-summer-school-2026-francuska/',
  'category': 'training',
  'kind': 'programme-overview',
  'hosts': ['FR'],
  'summary': 'BSB Lyon Empowering Sustainability:Transformative Pathways in '
             'Consumption, Jun29–Jul3,2026. Published educational overview; fees, '
             'prerequisites, registration closing and current enrolment unknown. Event '
             'dates are NOT application deadlines. No external catalogue/application '
             'material substituted.',
  'deadline': None,
  'proof': 'Original own notice ancillary-current-30; reviewed call and its integral '
           'literal materials; track Empowering-Sustainability.'}]

if __name__ == "__main__":
    sys.exit(main())
