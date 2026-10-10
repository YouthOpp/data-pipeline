"""Collect the bounded reviewed INSAIT and EXPLORER public programme frontier."""

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
import xml.etree.ElementTree as ET

SOURCE_ID = "bg-insait"
SOURCE_URL = "https://insait.ai/insait-opens-applications-for-surf-2026-summer-research-internship-program/"
WEBSITE_URL = "https://insait.ai/surf/"
LANGUAGE = "en"
PUBLISHER_COUNTRY = "BG"
PUBLISHER_TYPE = "university"
ATTRIBUTION = "INSAIT at Sofia University https://insait.ai/ and its EXPLORER programme https://explorer.insait.ai/ with DeepMind for the named scholarship"

_GITHUB = "https://api.github.com/repos/YouthOpps/data-source"
_WORKFLOW_GITHUB = "https://api.github.com/repos/YouthOpps/data-pipeline"
_USER_AGENT = (
    "YouthOpps/1.0 (+https://github.com/YouthOpps/data-pipeline; "
    "public opportunity metadata)"
)
CATEGORIES = {"scholarships", "grants", "internships", "jobs", "training", "fellowships"}
FAMILY = "youthopps-insait-publisher-v1"
FAMILY_SOURCES = {"bg-insait"}
COLLECTION_TIMEOUT = 900
RUN_TIMEOUT = 1290
EXPORT_TIMEOUT = 30
_STATE_SCHEMA = 1
_ARTIFACT_NAME = "insait-pacing-state"
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
            location = response.headers.get("Location")
            if publisher and status in (401, 403, 429):
                if status in (401, 403) or retry is None:
                    with locked_budget():
                        state = load_budget()
                        state["blocked"] = True
                        state["observed_at"] = time.time()
                        save_budget(state)
                refused = urllib.parse.urlsplit(current)
                provenance = urllib.parse.urlunsplit(
                    (refused.scheme, refused.hostname or "", refused.path, "", "")
                )
                raise AdapterError(
                    f"Publisher refused access: HTTP {status}: " + provenance,
                    "access", status,
                )
            if publisher and status in (301, 302, 303, 307, 308):
                if not location or redirect == 5:
                    raise AdapterError("Invalid redirect chain", "access")
                destination = urllib.parse.urljoin(current, location)
                public_url(destination)
                if urllib.parse.urlsplit(destination).scheme != "https":
                    raise AdapterError("Publisher redirect transport downgrade", "access")
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
        raw = os.environ.get("INSAIT_PACING_BOOTSTRAP", "")
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
    path = os.environ.get("INSAIT_PACING_ARTIFACT_PATH")
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
    try:
        parsed = urllib.parse.urlsplit(value)
        clean = urllib.parse.urlunsplit(parsed._replace(fragment=""))
        allowed = {info["url"] for info in INPUTS.values()} | {
            "https://insait.ai/robots.txt", "https://explorer.insait.ai/robots.txt"
        }
        if (
            clean not in allowed or parsed.scheme != "https"
            or parsed.username or parsed.password or parsed.port
        ):
            raise ValueError()
    except ValueError:
        raise AdapterError("Unreviewed public source route", "access") from None
    return clean


def html_fingerprint(body, key=None):
    root = document_root(body.decode("utf-8", "strict"))
    # Only these exact reviewed contact structures tolerate rotating masks.
    contact_shapes = {
        "explorer-faq": [
            {
                "tag": "a",
                "attrs": {
                    "href": "/cdn-cgi/l/email-protection#target:95a658ec7f1ab783c6f06689029690f4150d094c6b42b832d4aba96ad400adb4"
                },
                "children": [
                    {
                        "tag": "span",
                        "attrs": {
                            "style": "font-weight: 400"
                        },
                        "children": [
                            {
                                "tag": "span",
                                "attrs": {
                                    "class": "__cf_email__",
                                    "data-cfemail": "target:95a658ec7f1ab783c6f06689029690f4150d094c6b42b832d4aba96ad400adb4"
                                },
                                "children": [
                                    "[email\u00a0protected]"
                                ]
                            }
                        ]
                    }
                ]
            },
            {
                "tag": "a",
                "attrs": {
                    "href": "/cdn-cgi/l/email-protection#target:95a658ec7f1ab783c6f06689029690f4150d094c6b42b832d4aba96ad400adb4"
                },
                "children": [
                    {
                        "tag": "span",
                        "attrs": {
                            "style": "font-weight: 400"
                        },
                        "children": [
                            {
                                "tag": "span",
                                "attrs": {
                                    "class": "__cf_email__",
                                    "data-cfemail": "target:95a658ec7f1ab783c6f06689029690f4150d094c6b42b832d4aba96ad400adb4"
                                },
                                "children": [
                                    "[email\u00a0protected]"
                                ]
                            }
                        ]
                    }
                ]
            }
        ],
        "mit": [
            {
                "tag": "a",
                "attrs": {
                    "href": "/cdn-cgi/l/email-protection#target:c5128c36a566838a870b0e98e091930ea1f554b6f144e5e68c0f0f14f72b1cf7",
                    "target": "_blank",
                    "rel": "noreferrer noopener"
                },
                "children": [
                    {
                        "tag": "span",
                        "attrs": {
                            "class": "__cf_email__",
                            "data-cfemail": "target:c5128c36a566838a870b0e98e091930ea1f554b6f144e5e68c0f0f14f72b1cf7"
                        },
                        "children": [
                            "[email\u00a0protected]"
                        ]
                    }
                ]
            }
        ],
        "postdoc": [
            {
                "tag": "a",
                "attrs": {
                    "href": "/cdn-cgi/l/email-protection#target:c5128c36a566838a870b0e98e091930ea1f554b6f144e5e68c0f0f14f72b1cf7"
                },
                "children": [
                    {
                        "tag": "span",
                        "attrs": {
                            "class": "__cf_email__",
                            "data-cfemail": "target:c5128c36a566838a870b0e98e091930ea1f554b6f144e5e68c0f0f14f72b1cf7"
                        },
                        "children": [
                            "[email\u00a0protected]"
                        ]
                    }
                ]
            }
        ],
        "summer-research": [
            {
                "tag": "a",
                "attrs": {
                    "href": "/cdn-cgi/l/email-protection#target:bf012002a335de3e54b0304d92a821cf5d753910dad3a9690cc4af5c8d237703",
                    "target": "_blank",
                    "rel": "noreferrer noopener"
                },
                "children": [
                    {
                        "tag": "span",
                        "attrs": {
                            "class": "__cf_email__",
                            "data-cfemail": "target:c5128c36a566838a870b0e98e091930ea1f554b6f144e5e68c0f0f14f72b1cf7"
                        },
                        "children": [
                            "[email\u00a0protected]"
                        ]
                    }
                ]
            }
        ],
        "surf": [
            {
                "tag": "a",
                "attrs": {
                    "href": "/cdn-cgi/l/email-protection#target:bf012002a335de3e54b0304d92a821cf5d753910dad3a9690cc4af5c8d237703",
                    "target": "_blank",
                    "rel": "noreferrer noopener"
                },
                "children": [
                    {
                        "tag": "span",
                        "attrs": {
                            "class": "__cf_email__",
                            "data-cfemail": "target:bf012002a335de3e54b0304d92a821cf5d753910dad3a9690cc4af5c8d237703"
                        },
                        "children": [
                            "[email\u00a0protected]"
                        ]
                    }
                ]
            }
        ]
    }
    contact_nodes = {}
    if key in contact_shapes:
        def contact_digest(value):
            if not isinstance(value, str) or not re.fullmatch(
                r"[0-9a-f]{4,256}", value
            ) or len(value) % 2:
                raise AdapterError("Changed reviewed contact encoding: " + key, "parse")
            encoded = bytes.fromhex(value)
            try:
                decoded = bytes(char ^ encoded[0] for char in encoded[1:])
                decoded.decode("utf-8", "strict")
            except UnicodeError:
                raise AdapterError("Invalid reviewed contact encoding: " + key, "parse") from None
            return hashlib.sha256(decoded).hexdigest()

        def contact_structure(node):
            if isinstance(node, str):
                return node
            attributes = dict(node.attrs)
            if node.tag == "a":
                match = re.fullmatch(
                    r"/cdn-cgi/l/email-protection#([0-9a-f]{4,256})",
                    attributes.get("href", ""),
                )
                if not match:
                    raise AdapterError("Changed reviewed contact route: " + key, "parse")
                attributes["href"] = "/cdn-cgi/l/email-protection#target:" + contact_digest(match[1])
            if "data-cfemail" in attributes:
                attributes["data-cfemail"] = "target:" + contact_digest(attributes["data-cfemail"])
            return {
                "tag": node.tag, "attrs": attributes,
                "children": [contact_structure(child) for child in node.children],
            }

        protected = [
            node for node in nodes(root, "span")
            if "data-cfemail" in node.attrs
            or node.attrs.get("class") == "__cf_email__"
        ]
        anchors = [
            node for node in nodes(root, "a")
            if "email-protection#" in node.attrs.get("href", "")
            or any(span in protected for span in nodes(node, "span"))
        ]
        expected = contact_shapes[key]
        if len(anchors) != len(expected) or len(protected) != len(expected):
            raise AdapterError("Changed reviewed contact count: " + key, "parse")
        actual = [contact_structure(node) for node in anchors]
        if actual != expected:
            raise AdapterError("Changed reviewed contact target or structure: " + key, "parse")
        contact_nodes = {
            node: ("reviewed-faq-contact:" + shape["attrs"]["href"].split("target:")[1]
                   if key == "explorer-faq" else shape["attrs"]["href"])
            for node, shape in zip(anchors, actual)
        }
    links = sorted({
        (node.tag, node.attrs.get("rel", ""), contact_nodes.get(node, node.attrs["href"]))
        for tag in ("a", "link") for node in nodes(root, tag)
        if node.attrs.get("href")
    })
    facts = json.dumps({"text": root.text(), "links": links},
                       ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(facts.encode()).hexdigest()


def xml_fingerprint(body):
    try:
        root = ET.fromstring(body)
        namespace = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
        if root.tag not in {namespace + "sitemapindex", namespace + "urlset"}:
            raise ValueError()
        locations = [child.find(namespace + "loc").text for child in root]
        if not locations or not all(
            isinstance(value, str) and value.startswith("https://")
            for value in locations
        ):
            raise ValueError()
    except (ET.ParseError, AttributeError, ValueError):
        raise AdapterError("Invalid publisher sitemap frontier", "parse") from None
    facts = json.dumps({"root": root.tag, "locations": locations},
                       separators=(",", ":"))
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
    elif info["format"] == "xml":
        digest = xml_fingerprint(body)
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
        record["deadline"] = profile["deadline"]
        record["language"] = profile.get("language", LANGUAGE)
        record["eligible_countries"] = profile.get("eligible_countries", [])
        deadline = profile["deadline"]
        if deadline is not None:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", deadline):
                record["status"] = (
                    "expired" if now.date().isoformat() > deadline
                    else "open" if now.date().isoformat() < deadline else "unknown"
                )
            else:
                closing = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
                record["status"] = "expired" if now > closing else "open"
        records.append(record)
    validate_records(records)
    if len(records) != 9:
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
        "name": "INSAIT — reviewed public research, recruitment and EXPLORER programmes",
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
            else f"Collected {len(records)} reviewed INSAIT opportunities"
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
    ):
        raise AdapterError("Invalid previous source metadata contract", "publish")
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
        if records and timestamps["last_checked_at"] != max(
            datetime.fromisoformat(record["last_checked_at"].replace("Z", "+00:00"))
            for record in records
        ):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise AdapterError("Invalid previous metadata timestamps", "publish") from None

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
                    validate_prior_metadata(candidate_metadata, candidate_records)
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


INPUTS = {'assigned-surf-news': {'url': 'https://insait.ai/insait-opens-applications-for-surf-2026-summer-research-internship-program/',
                        'format': 'html',
                        'sha256': '768dbf4b7937dc9f2a440456a515c0d1b5b16d87bb0280d217e116c5d9511b84'},
 'category-sitemap': {'url': 'https://insait.ai/category-sitemap.xml',
                      'format': 'xml',
                      'sha256': 'add840347f9a81890e8794b237122d26b2c074bb851459aab99cb58da8884e51'},
 'deepmind': {'url': 'https://insait.ai/deepmind-phd-scholarships/',
              'format': 'html',
              'sha256': '9271fcb2915b3922f296acc4ffaf7cb1c50baa2653371680774738213519e549'},
 'explorer': {'url': 'https://explorer.insait.ai/',
              'format': 'html',
              'sha256': '5da480ca787e7838eec7c498aa30fa9389711975c1057a84d04d5bf34fa39ae2'},
 'explorer-apply': {'url': 'https://explorer.insait.ai/kandidatstvane/',
                    'format': 'html',
                    'sha256': '13a154313febbdbc180e1a8ca2ab635d36bc57df073a74648846508710f0b0ce'},
 'explorer-category-sitemap': {'url': 'https://explorer.insait.ai/category-sitemap.xml',
                               'format': 'xml',
                               'sha256': '8a796ef9b42151039d1c6c659973389261b5f84b397a65c4fcec3e9916373054'},
 'explorer-faq': {'url': 'https://explorer.insait.ai/vuprosi-i-otgovori/',
                  'format': 'html',
                  'sha256': 'b3f963ac32c5035fe7efe6ea68620b8b74227cd2a38f3fc9c4d044da80f6fed9'},
 'explorer-page-sitemap': {'url': 'https://explorer.insait.ai/page-sitemap.xml',
                           'format': 'xml',
                           'sha256': '3706be5d3deddaa29a35a3d8962eaa2b248dd11624128f42bee7b1de3db80d74'},
 'explorer-post-sitemap': {'url': 'https://explorer.insait.ai/post-sitemap.xml',
                           'format': 'xml',
                           'sha256': 'c70d3f8ede38acf447fd44606d3343865747e2f8413297791e2551b5d378c760'},
 'explorer-sitemap': {'url': 'https://explorer.insait.ai/sitemap_index.xml',
                      'format': 'xml',
                      'sha256': '9f0105e864329ad3565dcd4fc69f48c626ddb044cc8fa65bf8391db4dc364b02'},
 'explorer2026-news': {'url': 'https://insait.ai/with-over-1000000-bgn-in-donations-insait-launches-explorer-2026-a-unique-ai-and-deep-tech-bachelor-program/',
                       'format': 'html',
                       'sha256': '3adfaf22d37d28d18d967362445e0ff0fc0bc431c2fbf36914ee1b610218cab4'},
 'faculty-call': {'url': 'https://insait.ai/faculty/',
                  'format': 'html',
                  'sha256': '46991d23056d6b58b158898ea4ffcebd85773f771badd626ca7da6bd8f0927cc'},
 'join': {'url': 'https://insait.ai/join-insait/',
          'format': 'html',
          'sha256': '894db7dcc72ca351600f272a54e0582a504c1a5e53a9b3743fad59a46e39b73f'},
 'mit': {'url': 'https://insait.ai/mit/',
         'format': 'html',
         'sha256': 'a625b43861cb791c4255202feee31608b2b6256317340f1e724499f95603e57e'},
 'nonacademic-bg': {'url': 'https://insait.ai/bg/non-academic-position-at-insait/',
                    'format': 'html',
                    'sha256': '46fd308334c459143a1bac9bd4eeb7b31adaef237467ddf270eccfe51f185126'},
 'page-sitemap1': {'url': 'https://insait.ai/page-sitemap1.xml',
                   'format': 'xml',
                   'sha256': '8b9f7bbc33ded4134d350d57aefabdbdc86b9831747483a91ac03c4102b10d9e'},
 'page-sitemap2': {'url': 'https://insait.ai/page-sitemap2.xml',
                   'format': 'xml',
                   'sha256': '1189819c6efa0108bcca6ef568e81eff5237bface8855206e675a06bfee29302'},
 'phd': {'url': 'https://insait.ai/phd/',
         'format': 'html',
         'sha256': '0d88082461ab52e1586b1c46585f815a300dd1047f2396fa4ef2ecdec9bc8ab5'},
 'phd-bg': {'url': 'https://insait.ai/bg/phd/',
            'format': 'html',
            'sha256': '80fa9dd447791e44b1e465c25b12b6ba31793f3765395016638a5f1df4249c77'},
 'phd-faq-bg': {'url': 'https://insait.ai/bg/phd-program-frequently-asked-questions/',
                'format': 'html',
                'sha256': 'd9e93e4bb8c385c3fb9030bc702d855658ab8a2be71e5dc4cd8dca3ca7504f6c'},
 'post-sitemap': {'url': 'https://insait.ai/post-sitemap.xml',
                  'format': 'xml',
                  'sha256': '7259a4a1ee40bc9bd5a07ff305e06be58f87de10e8ac5b3bce6c6ebe05a3b7c5'},
 'postdoc': {'url': 'https://insait.ai/post-doc-at-insait/',
             'format': 'html',
             'sha256': 'edeb41b26ff1b4ac6cc3904e797549047d0c666d3383f0dcbe81179c83821692'},
 'sitemap': {'url': 'https://insait.ai/sitemap_index.xml',
             'format': 'xml',
             'sha256': '21ba9cf8265a92c29faae18f89b58a97ad68ad39c776d7fdec4a698a8e20d5e0'},
 'staff-positions-bg': {'url': 'https://insait.ai/bg/staff-positions-at-insait/',
                        'format': 'html',
                        'sha256': '377e519ec801acc49760157cb62373bd1cb88caf02feb94da53562dfbfff148a'},
 'summer-research': {'url': 'https://insait.ai/summer-ai-research-program-for-students/',
                     'format': 'html',
                     'sha256': '69f677ca50d3bd85a24e0762868d3a2bc7053921dae5b9b536ccd3f8337f0b34'},
 'surf': {'url': 'https://insait.ai/surf/',
          'format': 'html',
          'sha256': 'cd1ee150f2f77715727d5c49d299d117e73c6aa23a378796dbb2af2766681579'},
 'tenure-regulations': {'url': 'https://insait.ai/wp-content/uploads/2023/09/Tenure-Track-Regulations.pdf',
                        'format': 'pdf',
                        'sha256': 'ecc4e3f6102e7a162e0ae6e5a3423e48191f6a60d171e4e7f9c979144ad494f8'}}

PROFILES = [{'key': 'https://insait.ai/surf/|SURF|2026',
  'title': 'Summer Undergraduate Research Fellowship 2026',
  'url': 'https://insait.ai/surf/',
  'language': 'en',
  'kind': 'opportunity',
  'category': 'internships',
  'hosts': ['BG'],
  'deadline': '2026-03-08T21:59:00.000Z',
  'summary': 'Onsite Sofia research, 8–12 weeks in summer 2026; no remote. STEM '
             'bachelor students at any accredited university, GPA≥3.5/4 or equivalent; '
             'first-years and spring graduates eligible, no local preference. €1,500 '
             'MONTHLY plus arranged/paid accommodation and travel; visa assistance. '
             'Closing March 8,2026 23:59 EET (=21:59 UTC), now historical. Projects '
             'approximately June 1–August 31,2026.',
  'proof': 'Published programme facts: https://insait.ai/surf/ '
           'https://insait.ai/insait-opens-applications-for-surf-2026-summer-research-internship-program/'},
 {'key': 'https://insait.ai/faculty/|faculty|tenure-track-and-tenured',
  'title': 'Faculty at INSAIT',
  'url': 'https://insait.ai/faculty/',
  'language': 'en',
  'kind': 'opportunity',
  'category': 'jobs',
  'hosts': ['BG'],
  'deadline': None,
  'summary': 'Faculty research positions: junior tenure track and established tenured '
             'researchers. Tenure-track contracts start at 2 years, renewed for 3; '
             'positive final evaluation is required but does NOT oblige INSAIT to '
             'offer a permanent contract. Outside commercial work requires prior '
             'written permission. International-comparable salary/package, amount '
             'unknown. Develop research and supervise students; closing and detailed '
             'applicant degree criteria not published here.',
  'proof': 'Published programme facts: https://insait.ai/faculty/ '
           'https://insait.ai/wp-content/uploads/2023/09/Tenure-Track-Regulations.pdf'},
 {'key': 'https://insait.ai/post-doc-at-insait/|postdoc|rolling',
  'title': 'Postdoc at INSAIT',
  'url': 'https://insait.ai/post-doc-at-insait/',
  'language': 'en',
  'kind': 'opportunity',
  'category': 'jobs',
  'hosts': ['BG'],
  'deadline': None,
  'summary': 'Rolling applications for AI/computer-science postdoctoral research. PhD '
             'in CS, data science, mathematics, physics, statistics or electrical '
             'engineering must be completed by job start; finishing candidates may '
             'apply. Strong academic background; research statement, CV, degrees and '
             'at least 2 references, documents in English/PDF. Applicants receive a '
             'response within 2 months. Salary amount not stated.',
  'proof': 'Published programme facts: https://insait.ai/post-doc-at-insait/'},
 {'key': 'https://insait.ai/phd/|PhD|rolling',
  'title': 'PhD at INSAIT',
  'url': 'https://insait.ai/phd/',
  'language': 'en',
  'kind': 'opportunity',
  'category': 'scholarships',
  'hosts': ['BG'],
  'deadline': None,
  'summary': 'Rolling full-time PhD at INSAIT/Sofia University; BA/MA completed by '
             'start. CS, data science, mathematics, physics, statistics and electrical '
             'engineering preferred, not exclusive. EN/BG programme: €39,684 '
             'gross/year; undated BG FAQ: €36,000 gross/year—confirm amount. Up to 5 '
             'years; BA-only entrants complete a master in first 2 supported years. '
             'International applicants welcome; English working language, strong '
             'English and English documents. No outside jobs; external-adviser visits '
             'funded, destinations not fixed.',
  'proof': 'Published programme facts: https://insait.ai/phd/ '
           'https://insait.ai/bg/phd/ '
           'https://insait.ai/bg/phd-program-frequently-asked-questions/'},
 {'key': 'https://insait.ai/mit/|MIT-CSAIL|yearless-programme',
  'title': 'INSAIT and MIT CSAIL Joint Research Program',
  'url': 'https://insait.ai/mit/',
  'language': 'en',
  'kind': 'programme-overview',
  'category': 'fellowships',
  'hosts': ['US'],
  'deadline': None,
  'summary': 'One-year physical research specialisation at MIT CSAIL for holders of an '
             'INSAIT tenure-track-position OFFER. Selected candidates will be employed '
             'by INSAIT, which pays salary, benefits and health insurance during the '
             'US stay; amount unknown. Initial cycle supports up to 6 participants. '
             'Yearly applications by April 30, with no year specified. Faculty '
             'proposals by May 31; joint selection by July 31.',
  'proof': 'Published programme facts: https://insait.ai/mit/ '
           'https://insait.ai/wp-content/uploads/2023/09/Tenure-Track-Regulations.pdf'},
 {'key': 'https://insait.ai/deepmind-phd-scholarships/|DeepMind|ML-AI-PhD',
  'title': 'DeepMind PhD Scholarships at INSAIT',
  'url': 'https://insait.ai/deepmind-phd-scholarships/',
  'language': 'en',
  'kind': 'opportunity',
  'category': 'scholarships',
  'hosts': ['BG'],
  'deadline': None,
  'summary': 'Distinct DeepMind-supported ML/AI doctoral scholarship with mentoring. '
             'Applicants must ordinarily RESIDE in an EU member state AND identify '
             'with the underrepresented group female; this is not an EU-passport rule. '
             'Eligible INSAIT PhD applicants with an ML/AI research proposal are '
             'automatically considered. Scholarship amount, period and closing are not '
             'stated.',
  'proof': 'Published programme facts: https://insait.ai/deepmind-phd-scholarships/ '
           'https://insait.ai/phd/'},
 {'key': 'https://insait.ai/summer-ai-research-program-for-students/|STARS|2024-historical-overview',
  'title': 'Summer AI Research Program for Students',
  'url': 'https://insait.ai/summer-ai-research-program-for-students/',
  'language': 'en',
  'kind': 'programme-overview',
  'category': 'internships',
  'hosts': ['BG'],
  'deadline': None,
  'summary': 'Historical summer 2024 STARS research programme. Grades 10–11 students '
             'at accredited institutions;8 weeks IN PERSON at Sofia Tech Park with '
             'INSAIT mentors, project plan/report/presentation. Selected students '
             'receive 1,500 BGN MONTHLY; other support unknown. The planned '
             'application window was early 2024; exact closing not published. Current '
             'availability unconfirmed.',
  'proof': 'Published programme facts: '
           'https://insait.ai/summer-ai-research-program-for-students/'},
 {'key': 'https://insait.ai/with-over-1000000-bgn-in-donations-insait-launches-explorer-2026-a-unique-ai-and-deep-tech-bachelor-program/|EXPLORER|2026-call',
  'title': 'With Over 1,000,000 BGN in Donations, INSAIT Launches EXPLORER 2026 – a '
           'unique AI and Deep Tech bachelor program',
  'url': 'https://insait.ai/with-over-1000000-bgn-in-donations-insait-launches-explorer-2026-a-unique-ai-and-deep-tech-bachelor-program/',
  'language': 'en',
  'kind': 'opportunity',
  'category': 'scholarships',
  'hosts': ['BG'],
  'deadline': '2026-02-02',
  'summary': 'Closed 2026 EXPLORER call: high-school graduates and first-year students '
             'in FMI/Sofia University’s bachelor programme (called Computer Science in '
             'this notice). €36,000 TOTAL for the duration of studies, INSAIT research '
             'and paid summer engineering internships. Applications Dec 3,2025–Feb '
             '2,2026, DATEONLY. Programme sponsors donated over 1 millionBGN in '
             'aggregate.',
  'proof': 'Published programme facts: '
           'https://insait.ai/with-over-1000000-bgn-in-donations-insait-launches-explorer-2026-a-unique-ai-and-deep-tech-bachelor-program/'},
 {'key': 'https://explorer.insait.ai/|EXPLORER|2027-28-upcoming-overview',
  'title': 'EXPLORER – бакалавърска програма в AI и Deep Tech',
  'url': 'https://explorer.insait.ai/',
  'language': 'bg',
  'kind': 'programme-overview',
  'category': 'scholarships',
  'hosts': ['BG'],
  'deadline': None,
  'summary': 'Прием 2027/28 предстои; сроковете и финансирането още не са обявени. За '
             '„Информатика“, ФМИ/СУ: кандидат-студентите трябва да се запишат в 1. '
             'курс; допустими са 1./2. курс, във 2. курс с всички изпити от 1. курс. '
             'Отличен български И английски, не изискване за гражданство. '
             'Продължаване: следващ курс, одобрен успех и положителна оценка вINSAIT, '
             'успешен стаж и съгласувана друга работа.4 години,10-седмични платени '
             'стажове; проектите през първите 2 години са доброволни.36 000 € '
             'общо/1022 € месечно за 2026/27 НЕ гарантират финансирането за 2027/28.',
  'proof': 'Published programme facts: https://explorer.insait.ai/ '
           'https://explorer.insait.ai/kandidatstvane/ '
           'https://explorer.insait.ai/vuprosi-i-otgovori/'}]

if __name__ == "__main__":
    sys.exit(main())
