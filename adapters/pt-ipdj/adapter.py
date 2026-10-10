"""Collect IPDJ’s bounded public programme, call and educational frontier."""

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

SOURCE_ID = "pt-ipdj"
SOURCE_URL = "https://ipdj.gov.pt/candidaturas"
WEBSITE_URL = "https://ipdj.gov.pt/"
LANGUAGE = "pt"
PUBLISHER_COUNTRY = "PT"
PUBLISHER_TYPE = "public-body"
ATTRIBUTION = "Instituto Português do Desporto e Juventude https://ipdj.gov.pt/ and the named programme administrators"

_GITHUB = "https://api.github.com/repos/YouthOpps/data-source"
_WORKFLOW_GITHUB = "https://api.github.com/repos/YouthOpps/data-pipeline"
_USER_AGENT = (
    "YouthOpps/1.0 (+https://github.com/YouthOpps/data-pipeline; "
    "public opportunity metadata)"
)
CATEGORIES = {"scholarships", "grants", "internships", "jobs", "training", "fellowships", "volunteering", "competitions", "other"}
FAMILY = "youthopps-ipdj-publisher-v1"
FAMILY_SOURCES = {"pt-ipdj"}
COLLECTION_TIMEOUT = 2400
RUN_TIMEOUT = 2790
EXPORT_TIMEOUT = 30
_STATE_SCHEMA = 1
_ARTIFACT_NAME = "ipdj-pacing-state"
_STATE_PATH = _STATE_LOCK = None
_STATE_NEW = False
_COLLECTION_LOCK_FD = None
_COLLECTION_STARTED = None
_RESTORED = _PUBLISHING = _STATE_TRUSTED = False
_ROBOTS = {}
_RUN_END = _PHASE_END = None
_PHASE_NAME = None
_PUBLISHER_COMPLETED_MONOTONIC = None
_PUBLISHER_INTERVAL = 6
_PUBLISHER_GATE_FAILED = False


class Node:
    """Minimal HTML tree retaining official links and structural boundaries."""

    def __init__(self, tag="", attrs=()):
        self.tag = tag
        attrs = list(attrs)
        self.attrs = dict(attrs)
        self.duplicate_attrs = len(self.attrs) != len(attrs)
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


class TransientPublisherError(AdapterError):
    """Typed network failure eligible for one bounded public-GET retry."""


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
    global _STATE_PATH, _STATE_LOCK, _STATE_NEW
    if _STATE_PATH:
        return
    directory = os.path.join(
        tempfile.gettempdir(), FAMILY + ("-") + str(os.getuid())
    )
    try:
        os.mkdir(directory, 0o700)
        _STATE_NEW = True
    except FileExistsError:
        _STATE_NEW = False
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
        _STATE_LOCK, os.O_RDWR | (os.O_CREAT if _STATE_NEW else 0) | os.O_NOFOLLOW, 0o600
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
        if not _STATE_NEW:
            raise AdapterError(
                "Missing mature publisher state; no reset", "access"
            ) from None
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
    global _STATE_NEW
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
        _STATE_NEW = False
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
        if _PUBLISHER_COMPLETED_MONOTONIC is not None:
            target = max(
                target,
                now + max(
                    0,
                    _PUBLISHER_COMPLETED_MONOTONIC
                    + _PUBLISHER_INTERVAL
                    - time.monotonic(),
                ),
            )
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
        while True:
            check_deadline()
            wait = max(0, target - time.time())
            if _PUBLISHER_COMPLETED_MONOTONIC is not None:
                wait = max(
                    wait,
                    _PUBLISHER_COMPLETED_MONOTONIC
                    + _PUBLISHER_INTERVAL
                    - time.monotonic(),
                )
            if wait <= 0:
                break
            if wait + 120 > _PHASE_END - time.monotonic():
                raise AdapterError("Publisher backoff exceeds phase budget", "access")
            if _COLLECTION_STARTED is not None and (
                time.time() + wait + 120
                > _COLLECTION_STARTED + COLLECTION_TIMEOUT
            ):
                raise AdapterError("Publisher backoff exceeds collection budget", "access")
            time.sleep(wait)
        now = time.time()
        if now < target:
            raise AdapterError(
                ("Publisher pacing clock moved backwards"), ("access")
            )
        check_deadline()
        if (
            _PUBLISHER_COMPLETED_MONOTONIC is not None
            and time.monotonic()
            < _PUBLISHER_COMPLETED_MONOTONIC + _PUBLISHER_INTERVAL
        ):
            raise AdapterError("Publisher physical completion gate not elapsed", "access")
        state["starts"] = [t for t in starts if t > now - 60] + [now]
        state["observed_at"] = now
        save_budget(state)



@contextlib.contextmanager
def publisher_attempt(publisher):
    """Persist a completion gate around every physical publisher attempt."""
    global _PUBLISHER_COMPLETED_MONOTONIC, _PUBLISHER_GATE_FAILED
    if not publisher:
        yield
        return
    if _COLLECTION_LOCK_FD is None or _PUBLISHER_GATE_FAILED:
        raise AdapterError("Exclusive healthy publisher collector required", "access")
    check_deadline()
    if (
        _PUBLISHER_COMPLETED_MONOTONIC is not None
        and time.monotonic()
        < _PUBLISHER_COMPLETED_MONOTONIC + _PUBLISHER_INTERVAL
    ):
        raise AdapterError("Publisher physical completion gate not elapsed", "access")
    try:
        yield
    finally:
        _PUBLISHER_COMPLETED_MONOTONIC = time.monotonic()
        _PUBLISHER_GATE_FAILED = True
        with locked_budget():
            state = load_budget()
            now = time.time()
            state["not_before"] = max(
                state["not_before"], now + _PUBLISHER_INTERVAL
            )
            state["observed_at"] = now
            save_budget(state)
        _PUBLISHER_GATE_FAILED = False


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
    error_type = (
        TransientPublisherError
        if not isinstance(cause, ssl.SSLError)
        and isinstance(cause, (TimeoutError, ConnectionError))
        else AdapterError
    )
    return error_type(
        "Publisher transport failure at " + hop + " (" + category + ")",
        "fetch",
    )


def reviewed_redirect(destination, current, status):
    """Reject unchanged routes with bounded credential-free provenance."""
    try:
        return public_url(destination)
    except AdapterError:
        try:
            parsed = urllib.parse.urlsplit(destination)
            target = urllib.parse.urlunsplit(
                (parsed.scheme, parsed.hostname or "", parsed.path, "", "")
            )
        except ValueError:
            target = "invalid redirect destination"
        raise AdapterError(
            "Publisher redirect refused: HTTP " + str(status) + ": "
            + safe_error(current) + " -> " + safe_error(target),
            "access", status,
        ) from None


_DIAGNOSTIC_MODE = False
_DIAGNOSTIC_STARTS = 0
_DIAGNOSTIC_URL = "https://ipdj.gov.pt/-/programa-associa-te-candidaturas-2026-ate-12-de-maio"


def request_bytes(
    url,
    method="GET",
    headers=None,
    payload=None,
    publisher=False,
    byte_limit=8388608,
    headers_only=False,
):
    """Bound response bytes; redirects are explicit and credential-safe."""
    global _DIAGNOSTIC_STARTS
    if headers_only and (not _DIAGNOSTIC_MODE or not publisher or url != _DIAGNOSTIC_URL or method != "GET" or payload is not None):
        raise AdapterError("Invalid finite diagnostic request", "access")
    headers = {"User-Agent": _USER_AGENT, **(headers or {})}
    current = url
    for redirect in range(6):
        check_deadline()
        if publisher:
            public_url(current)
            if _DIAGNOSTIC_MODE and (current not in ("https://ipdj.gov.pt/robots.txt", _DIAGNOSTIC_URL) or _DIAGNOSTIC_STARTS >= 2):
                raise AdapterError("Finite diagnostic request ceiling", "access")
            pace()
        request = urllib.request.Request(
            current, data=payload, headers=headers, method=method
        )
        with publisher_attempt(publisher):
            try:
                if publisher and _DIAGNOSTIC_MODE:
                    _DIAGNOSTIC_STARTS += 1
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
                if headers_only:
                    return status, response_headers, b""
                if publisher and _DIAGNOSTIC_MODE and status in (301, 302, 303, 307, 308):
                    raise AdapterError("Diagnostic robots redirect requires review", "access", status)
                if publisher and status in (301, 302, 303, 307, 308):
                    if not location or redirect == 5:
                        raise AdapterError("Invalid redirect chain", "access")
                    destination = urllib.parse.urljoin(current, location)
                    reviewed_redirect(destination, current, status)
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
                reviewed_redirect(destination, current, status)
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
    global _PUBLISHER_INTERVAL
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
            _PUBLISHER_INTERVAL = max(_PUBLISHER_INTERVAL, delay)
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
        try:
            status, headers, body = request_bytes(reviewed, publisher=True)
        except TransientPublisherError:
            if attempt:
                raise
            status = None
        if status == 200:
            return status, headers, body
        if status in (None, 503) and attempt == 0:
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
        raw = os.environ.get("IPDJ_PACING_BOOTSTRAP", "")
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


def open_fresh_pacing_output(runner_parent, runner_root):
    """Use the official fresh per-step output file, never an inherited marker."""
    value = os.environ.get("GITHUB_OUTPUT")
    if not value:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            raise AdapterError("Fresh step output required", "access")
        return None
    if not os.path.isabs(value):
        raise AdapterError("Unsafe step output path", "access")
    parts = os.path.relpath(value, runner_root).split(os.sep)
    if not parts or any(name in {"", ".", ".."} for name in parts):
        raise AdapterError("Step output outside runner temporary root", "access")
    descriptors = []
    parent = runner_parent
    try:
        for name in parts[:-1]:
            descriptor = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            descriptors.append(descriptor)
            info = os.fstat(descriptor)
            if (
                info.st_uid != os.getuid()
                or not stat.S_ISDIR(info.st_mode)
                or info.st_mode & 0o022
            ):
                raise AdapterError("Unsafe step output parent", "access")
            parent = descriptor
        descriptor = os.open(
            parts[-1], os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW, dir_fd=parent
        )
        descriptors.append(descriptor)
        info = os.fstat(descriptor)
        current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        if (
            info.st_uid != os.getuid()
            or not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o022
            or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise AdapterError("Fresh owned step output required", "access")
        if info.st_size != 0:
            # This path is owned and safe, but it violates the platform's
            # per-step freshness contract. Remove its old marker, then refuse.
            os.ftruncate(descriptor, 0)
            os.fsync(descriptor)
            raise AdapterError("Inherited step output refused", "access")
        return os.dup(descriptor)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def mark_pacing_artifact_ready(descriptor, run_id, attempt):
    """Signal only a fully finalized healthy artifact for this run attempt."""
    if descriptor is None:
        return
    if _PUBLISHER_GATE_FAILED or not _STATE_TRUSTED:
        raise AdapterError("Unhealthy pacing export cannot be signaled", "access")
    payload = (
        f"pacing_run_id={run_id}\npacing_run_attempt={attempt}\n"
        "pacing_ready=true\n"
    ).encode()
    try:
        if os.write(descriptor, payload) != len(payload):
            raise AdapterError("Incomplete pacing export handoff", "access")
        os.fsync(descriptor)
    except BaseException:
        # Cleanup is best effort; the positive signal was never attempted
        # until the healthy, current-run artifact was finalized and verified.
        try:
            os.ftruncate(descriptor, 0)
            os.fsync(descriptor)
        except OSError:
            pass
        raise


def export_family_artifact():
    """Export only healthy infrastructure state; invalidate an owned stale file."""
    path = os.environ.get("IPDJ_PACING_ARTIFACT_PATH")
    if not path or not (_STATE_TRUSTED or _PUBLISHER_GATE_FAILED):
        return
    configured = os.environ.get("RUNNER_TEMP", "")
    if not configured or not os.path.isabs(configured):
        raise AdapterError("Explicit runner temporary directory required", "access")
    root = os.path.realpath(configured)
    if os.path.dirname(os.path.abspath(path)) != root:
        raise AdapterError("Unsafe pacing artifact path", "access")
    name = os.path.basename(path)
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    output = None
    try:
        directory = os.fstat(parent)
        if (
            directory.st_uid != os.getuid()
            or not stat.S_ISDIR(directory.st_mode)
            or directory.st_mode & 0o022
            or directory.st_mode & 0o700 != 0o700
        ):
            raise AdapterError("Unsafe pacing artifact directory owner", "access")
        output = open_fresh_pacing_output(parent, root)
        existing = None
        try:
            existing = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=parent)
        except FileNotFoundError:
            pass
        if existing is not None:
            try:
                info = os.fstat(existing)
                if (
                    info.st_uid != os.getuid()
                    or not stat.S_ISREG(info.st_mode)
                    or info.st_mode & 0o077
                    or info.st_nlink != 1
                ):
                    raise AdapterError("Unsafe existing pacing artifact owner", "access")
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise AdapterError("Pacing artifact identity changed", "access")
                # Invalidate before unlink: an unlink failure cannot leave a
                # valid stale envelope on a writable, owned artifact inode.
                os.ftruncate(existing, 0)
                os.fsync(existing)
                os.unlink(name, dir_fd=parent)
                os.fsync(parent)
            finally:
                os.close(existing)
        if _PUBLISHER_GATE_FAILED:
            raise AdapterError("Publisher completion gate cannot be exported", "access")
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
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        try:
            with os.fdopen(descriptor, "w") as file:
                json.dump(envelope, file, separators=(",", ":"))
                file.flush()
                os.fsync(file.fileno())
            os.fsync(parent)
            check = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                info = os.fstat(check)
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (
                    info.st_uid != os.getuid()
                    or not stat.S_ISREG(info.st_mode)
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_nlink != 1
                    or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
                ):
                    raise AdapterError("Unsafe finalized artifact identity", "access")
                raw = os.read(check, 16385)
                if len(raw) > 16384 or json.loads(raw) != envelope:
                    raise AdapterError("Finalized pacing artifact mismatch", "access")
            finally:
                os.close(check)
            check_deadline()
            mark_pacing_artifact_ready(output, run_id, attempt)
        except BaseException:
            # An incomplete new file cannot masquerade as a valid artifact.
            os.unlink(name, dir_fd=parent)
            raise
    finally:
        if output is not None:
            os.close(output)
        os.close(parent)


def prepare_collection():
    global _COLLECTION_STARTED, _STATE_TRUSTED, _COLLECTION_LOCK_FD
    _COLLECTION_STARTED = time.time()
    budget_file()
    if _COLLECTION_LOCK_FD is None:
        path = os.path.join(os.path.dirname(_STATE_PATH), "collection.lock")
        descriptor = os.open(
            path, os.O_RDWR | (os.O_CREAT if _STATE_NEW else 0) | os.O_NOFOLLOW, 0o600
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
    with locked_budget():
        state = load_budget()
        now = time.time()
        state["not_before"] = max(state["not_before"], now + 60)
        state["observed_at"] = now
        save_budget(state)
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
        allowed = {info["url"] for info in INPUTS.values()} | {"https://ipdj.gov.pt/robots.txt"}
        if clean not in allowed or parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
            raise ValueError()
    except ValueError:
        raise AdapterError("Unreviewed public source route", "access") from None
    return clean


def relative_age_chrome(root):
    """Normalize only the unique plain CMS age label, retaining its structure."""
    found = []

    def walk(parent, path):
        for index, child in enumerate(parent.children):
            if not isinstance(child, Node):
                continue
            child_path = path + [index]
            if "date-info" in child.attrs.get("class", "").split():
                found.append((parent, child, child_path))
            walk(child, child_path)

    walk(root, [])
    if not found:
        return None
    parent, label, path = one(found, "CMS relative-age label")
    if (
        label.tag != "span"
        or label.attrs != {"class": "date-info"}
        or label.duplicate_attrs
        or parent.tag != "div"
        or parent.attrs != {"class": "asset-user-info text-secondary"}
        or parent.duplicate_attrs
        or len(label.children) != 1
        or not isinstance(label.children[0], str)
        or any(
            sibling is not label
            and (not isinstance(sibling, str) or sibling.strip())
            for sibling in parent.children
        )
        or not re.fullmatch(
            r"modificado à (?:(?:0|[1-9][0-9]{0,5}) "
            r"(?:Ano|Horas|Meses|Mês|dias)|1 Dia) atrás\.",
            label.children[0],
        )
    ):
        raise AdapterError("Reviewed CMS relative-age structure changed", "parse")
    label.children = ["modificado à [tempo relativo] atrás."]
    return {"path": path, "parent": [parent.tag, parent.attrs],
            "label": [label.tag, label.attrs]}


def html_fingerprint(body, key=None):
    root = document_root(body.decode("utf-8", "strict"))
    chrome = relative_age_chrome(root)
    # Preserve whole visible material and the complete literal link frontier.
    links = sorted({
        (node.tag, node.attrs.get("rel", ""), node.attrs["href"])
        for tag in ("a", "link") for node in nodes(root, tag)
        if node.attrs.get("href")
    })
    facts = {"text": root.text(), "links": links}
    if chrome is not None:
        facts["relative_age_structure"] = chrome
    encoded = json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


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
            profile["categories"],
            kind=profile["kind"],
            host_countries=profile["hosts"],
            evidence=[ATTRIBUTION, profile["proof"]],
        )
        record["id"] = hashlib.sha256(
            (SOURCE_ID + "|" + profile["key"]).encode()
        ).hexdigest()[:24]
        record["summary"] = profile["summary"]
        record["deadline"] = profile["deadline"]
        record["language"] = profile["language"]
        record["summary_language"] = profile["summary_language"]
        record["location"] = profile["location"]
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
    if len(records) != 245:
        raise AdapterError(
            "Incomplete reviewed identity partition", "validate"
        )
    return records


def collect():
    if _PHASE_END is not None:
        check_deadline()
        return parse_inventory(read_pages())
    with execution():
        with phase("prepublication", 2400):
            prepare_collection()
            return parse_inventory(read_pages())


def metadata(attempt, previous, records, error=None, checked_at=None):
    outcome = {
        "source": SOURCE_ID,
        "name": "IPDJ — bounded public programmes, calls and educational sessions",
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
            else f"Collected {len(records)} reviewed IPDJ opportunities"
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
        or previous.get("publisher_type") != PUBLISHER_TYPE
        or previous.get("attribution") != ATTRIBUTION
        or previous.get("name") != (
            "IPDJ — bounded public programmes, calls and educational sessions"
        )
        or "error" not in previous
        or (
            previous.get("status") == "success"
            and previous["error"] is not None
        )
        or (
            previous.get("status") == "fail"
            and (
                not isinstance(previous["error"], str)
                or not previous["error"].strip()
                or not isinstance(previous.get("failure_stage"), str)
                or not previous["failure_stage"].strip()
            )
        )
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
        with phase("prepublication", 2400):
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


def diagnostic_location(location):
    if not location:
        return None
    try:
        parsed = urllib.parse.urlsplit(urllib.parse.urljoin(_DIAGNOSTIC_URL, location))
        return urllib.parse.urlunsplit((parsed.scheme, parsed.hostname or "", parsed.path, "", ""))[:500]
    except ValueError:
        return "invalid redirect destination"


def run_news195_diagnostic():
    """Two native starts maximum, same family; no body, follow, or publication."""
    global _DIAGNOSTIC_MODE, _DIAGNOSTIC_STARTS
    _DIAGNOSTIC_MODE = True
    _DIAGNOSTIC_STARTS = 0
    result = 1
    try:
        with phase("diagnostic", 210):
            prepare_collection()
            _ROBOTS.clear()
            check_robots(_DIAGNOSTIC_URL)
            status, headers, _ = request_bytes(_DIAGNOSTIC_URL, publisher=True, headers_only=True)
            print(json.dumps({"diagnostic": "news195", "url": _DIAGNOSTIC_URL,
                              "status": status, "location": diagnostic_location(headers.get("Location")),
                              "physical_starts": _DIAGNOSTIC_STARTS}, sort_keys=True))
            result = 0
    except (Exception, PhaseExpired) as error:
        print("Finite news195 diagnostic failed: " + safe_error(str(error)), file=sys.stderr)
    finally:
        try:
            with phase("export", EXPORT_TIMEOUT):
                export_family_artifact()
        except (Exception, PhaseExpired):
            result = 1
        _DIAGNOSTIC_MODE = False
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    global RUN_TIMEOUT
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--publish", action="store_true")
    modes.add_argument("--diagnose-news195", action="store_true")
    args = parser.parse_args()
    if args.diagnose_news195:
        RUN_TIMEOUT = 240
    try:
        with execution():
            return run_news195_diagnostic() if args.diagnose_news195 else run_lifecycle(args.publish)
    except PhaseExpired:
        return 1


INPUTS = {'calendar-public': {'url': 'https://ipdj.gov.pt/candidaturas?p_p_id=pt_gov_ipdj_applications_calendar_portlet_ApplicationsCalendarPortlet&p_p_lifecycle=2&p_p_state=normal&p_p_mode=view&p_p_cacheability=cacheLevelPage',
                     'format': 'empty',
                     'sha256': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
                     'maximum_bytes': 8388608},
 'entry': {'url': 'https://ipdj.gov.pt/candidaturas',
           'format': 'html',
           'sha256': 'd99eccbf552040781745ac5b885a31c8c1a25a37eed341af8978d0265cd209fa',
           'maximum_bytes': 8388608},
 'frontier-1': {'url': 'https://ipdj.gov.pt/academia-de-desenvolvimento-juvenil',
                'format': 'html',
                'sha256': '0d2b8bed95a1ab54b8f3598c90de44a4362d94e1ae334843955b9a7695e4d6fb',
                'maximum_bytes': 8388608},
 'frontier-10': {'url': 'https://ipdj.gov.pt/envolve-te-entidades',
                 'format': 'html',
                 'sha256': '23d4b50ea01b3cfba998e7237eeb94e36a2a15b1e9146a85fffd8f042b0fd0d4',
                 'maximum_bytes': 8388608},
 'frontier-11': {'url': 'https://ipdj.gov.pt/envolve-te-jovens',
                 'format': 'html',
                 'sha256': '39668424ddf8e1009e99435293bb67367476e3687de216d6902d6cfb61176d65',
                 'maximum_bytes': 8388608},
 'frontier-12': {'url': 'https://ipdj.gov.pt/formarmais',
                 'format': 'html',
                 'sha256': '303fee53c18a3676279071f2b8d6202b02321d4ea787bba41fd8d939d9cb02fc',
                 'maximum_bytes': 8388608},
 'frontier-13': {'url': 'https://ipdj.gov.pt/programa-formar-medida-1',
                 'format': 'html',
                 'sha256': 'd98fcaf6c3a0b22705fa4850ca8afc76813a1b83d58b509da59529a3e2560b2a',
                 'maximum_bytes': 8388608},
 'frontier-14': {'url': 'https://ipdj.gov.pt/programa-formar-medida-3',
                 'format': 'html',
                 'sha256': '2836920863f96b2254c70d0cd4cd64b2be1e53e690efe0f333f427a9931711ea',
                 'maximum_bytes': 8388608},
 'frontier-15': {'url': 'https://ipdj.gov.pt/f%C3%A9rias-em-movimento-entidades',
                 'format': 'html',
                 'sha256': '057a03621964295760238e49dc8ac23cac559d675f36b52b4c490cf3dffffda3',
                 'maximum_bytes': 8388608},
 'frontier-16': {'url': 'https://ipdj.gov.pt/geracao-z',
                 'format': 'html',
                 'sha256': '090e53c8ca03dbab57497366af475a29c7fe5263f533bf62118f3e0582c0b77d',
                 'maximum_bytes': 8388608},
 'frontier-17': {'url': 'https://ipdj.gov.pt/geracoes-em-rede',
                 'format': 'html',
                 'sha256': '1f065ee270e0443648dd6a35355ee4e35cb40a6c6570cc014dcd98d35653fa40',
                 'maximum_bytes': 8388608},
 'frontier-18': {'url': 'https://ipdj.gov.pt/reativar',
                 'format': 'html',
                 'sha256': '8ba6634e0a46a953dd094989b404d542b3796458ad194a1b5944f903f5b57331',
                 'maximum_bytes': 8388608},
 'frontier-19': {'url': 'https://ipdj.gov.pt/namorar-com-fair-play',
                 'format': 'html',
                 'sha256': '1ebca8ca2bfb4052b9ff20badf7bc294e4e56f2150c96f7325d83b497f0f07ff',
                 'maximum_bytes': 8388608},
 'frontier-2': {'url': 'https://ipdj.gov.pt/afirma-te-ja',
                'format': 'html',
                'sha256': '1fbc9e1c5c7c2d63ad5fdc17bf8d29df8debead06450bc2b2611a489e32b420d',
                'maximum_bytes': 8388608},
 'frontier-20': {'url': 'https://ipdj.gov.pt/navegas',
                 'format': 'html',
                 'sha256': 'ecd294ebf99ee82cfd2b6ca3247f399660381c69dc23a2f4717f1f04cf0b00cc',
                 'maximum_bytes': 8388608},
 'frontier-21': {'url': 'https://ipdj.gov.pt/ocupacao-de-tempos-livres',
                 'format': 'html',
                 'sha256': 'f1917bf8c803710395f4e218359a2f54383fcc512b2f63ec41779e8005bbbba1',
                 'maximum_bytes': 8388608},
 'frontier-22': {'url': 'https://ipdj.gov.pt/orcamento-participativo-jovem',
                 'format': 'html',
                 'sha256': 'c5b49d09bea4d0226c2bb5c4a31646d8003a12b097b0b4151408836e472ea971',
                 'maximum_bytes': 8388608},
 'frontier-23': {'url': 'https://ipdj.gov.pt/parlamento-dos-jovens',
                 'format': 'html',
                 'sha256': '3dc83bce93b1bb51d4723d210ffd200cb5b797b49429af3daf8a7c865c11e0d5',
                 'maximum_bytes': 8388608},
 'frontier-24': {'url': 'https://ipdj.gov.pt/protocolos-de-apoio-na-area-da-cultura',
                 'format': 'html',
                 'sha256': 'd6a9221ef05ecc54a444de6741f6c9587ba710d7153c547313ae7d7a67bab48e',
                 'maximum_bytes': 8388608},
 'frontier-25': {'url': 'https://ipdj.gov.pt/premio-jovens-pela-igualdade',
                 'format': 'html',
                 'sha256': 'a137ea16683b24167ad1d17a47f011bb00e80bec3ca5cb97976a33c65750dd1f',
                 'maximum_bytes': 8388608},
 'frontier-26': {'url': 'https://ipdj.gov.pt/premios-regionais-de-boas-praticas-de-voluntariado-jovem',
                 'format': 'html',
                 'sha256': 'f1a2fa57a6a5acca78c43716530e7f7b26046ac5f58b350aaaeb1b05a5c33b96',
                 'maximum_bytes': 8388608},
 'frontier-27': {'url': 'https://ipdj.gov.pt/premios-boas-praticas-associativismo-jovem',
                 'format': 'html',
                 'sha256': '592c92d505a3cc23286b95bf516e8d4242e6f045fb95999dcd90882003a715a8',
                 'maximum_bytes': 8388608},
 'frontier-28': {'url': 'https://ipdj.gov.pt/trajetos1',
                 'format': 'html',
                 'sha256': 'abcef1a0a856b04840704804ddda0fda83d0a161332de0fdf35f94eaa589d571',
                 'maximum_bytes': 8388608},
 'frontier-29': {'url': 'https://ipdj.gov.pt/voluntariado-jovem-70-j%C3%81-direitos-da-juventude',
                 'format': 'html',
                 'sha256': 'a7499d762fa9fbc8036320b54a03dc796062b21c348cb4daf878187bb83e3f30',
                 'maximum_bytes': 8388608},
 'frontier-3': {'url': 'https://ipdj.gov.pt/apoio-financeiro-ao-desporto-federado',
                'format': 'html',
                'sha256': 'd54324f9b90a5ade40b83474f0fa9e3dba2df8482b2908e4b45c5f231ca13b25',
                'maximum_bytes': 8388608},
 'frontier-30': {'url': 'https://ipdj.gov.pt/voluntariado-jovem-para-a-natureza-e-florestas',
                 'format': 'html',
                 'sha256': 'd39ec2128125eaa319e90197c3552e040850809e10da7dd8a9e3a6dec44541a5',
                 'maximum_bytes': 8388608},
 'frontier-4': {'url': 'https://ipdj.gov.pt/voluntariado-internacional',
                'format': 'html',
                'sha256': 'befcdcf72b8dcda422e2e7f1f5a74a66178bb71a5095fb2747bbd827254d9050',
                'maximum_bytes': 8388608},
 'frontier-5': {'url': 'https://ipdj.gov.pt/capital-nacional-de-juventude',
                'format': 'html',
                'sha256': '353cbf1b496728d0f1442de093b0ebc290bec674fef0bb0bc8d0f80ea04b84b2',
                'maximum_bytes': 8388608},
 'frontier-6': {'url': 'https://ipdj.gov.pt/clube-top',
                'format': 'html',
                'sha256': '52969d73acb1165dfbb40284107dd68ea8c496acf4554b73b2599286cfebfc72',
                'maximum_bytes': 8388608},
 'frontier-7': {'url': 'https://ipdj.gov.pt/empreende-ja',
                'format': 'html',
                'sha256': 'df04daad9ece9e6000387cbf0345d71ba9ca28a748278fae3e0676f0f7acdaad',
                'maximum_bytes': 8388608},
 'frontier-8': {'url': 'https://ipdj.gov.pt/enquadramento-tecnico-qualificado',
                'format': 'html',
                'sha256': 'd6212f4e710b66cfe9f97f8fbd171fae39026b333a52d000abfe11f7303223a8',
                'maximum_bytes': 8388608},
 'frontier-9': {'url': 'https://ipdj.gov.pt/envolve-te-in',
                'format': 'html',
                'sha256': '9e9552ab7ccf32ab2813917cf39edb62195220ad535c27ccbb746332d4ca9f53',
                'maximum_bytes': 8388608},
 'library-0': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=16ea71aa-3b70-ed5f-4834-056127575e77&groupId=20123',
               'format': 'pdf',
               'sha256': '554e908c3407ae0c05b16fd85f3cd169ebeee5ec4809ada234d38c7d93d3b953',
               'maximum_bytes': 8388608},
 'library-1': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=c9bd048d-70f8-d0b6-1830-14f223e3aa57&groupId=20123',
               'format': 'pdf',
               'sha256': '8342e99d67f5700645a456828e56f312560f1ebd758b1f614fa7d23bbe5fa826',
               'maximum_bytes': 8388608},
 'library-10': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=093962f1-6a70-4c25-6fd2-aa1568e2cb99&groupId=20123',
                'format': 'pdf',
                'sha256': '3641da8f1965341e6cb640d88720c89a23a37d1974ea3941582bb06a4720a935',
                'maximum_bytes': 8388608},
 'library-11': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=eba95d27-8541-edad-c315-3b6e1d62a261&groupId=20123',
                'format': 'pdf',
                'sha256': 'd777296997f225703efbfe999f7d3e74d6d8264f40c18c3fde78a001915fcb4e',
                'maximum_bytes': 8388608},
 'library-13': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=60018a73-656f-10c5-00ae-5f7366e823a9&groupId=20123',
                'format': 'pdf',
                'sha256': '19c141a52bcccc570bf3610ad2a286241301fbffd1520e62f72e8772dc208916',
                'maximum_bytes': 8388608},
 'library-15': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=ee213f04-8b18-a978-04d0-fda2412e27ee&groupId=20123',
                'format': 'pdf',
                'sha256': 'd7c18de8d0426d61f8041043f23f70431c3d9dfe3bbfaa95726671ebd4ce7fd3',
                'maximum_bytes': 8388608},
 'library-17': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=51020da8-d2e5-caf9-0555-b44692a6436f&groupId=20123',
                'format': 'pdf',
                'sha256': 'cc84f2ad6c71259c7912b6a0fcf4d6b20ea01666e96d03cf8beadd70300f8bfd',
                'maximum_bytes': 8388608},
 'library-18': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=5c49a94f-7f75-2fe7-30c9-c3e6f962beb0&groupId=20123',
                'format': 'pdf',
                'sha256': '1ec619fd3d860a3148ac9db963c87df9e179d7e5564ed3b0b0268dd11877ff21',
                'maximum_bytes': 8388608},
 'library-2': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=65585f3d-eafb-6220-e56d-48c992468ec7&groupId=20123',
               'format': 'pdf',
               'sha256': 'a64c0f6bb8eda86cdde2d0bf1bd07344c4eafedf22ae1e184094e6b5e5d0f4dc',
               'maximum_bytes': 8388608},
 'library-20': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=1dc47a6a-bdf8-a870-61ba-30f43c8fdecd&groupId=20123',
                'format': 'pdf',
                'sha256': 'af30d52bb3d23df615e025ead1a38f9bf90327c3b178623f8bbca950228c3733',
                'maximum_bytes': 8388608},
 'library-22': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123',
                'format': 'pdf',
                'sha256': 'af5567ea2a87854b001a8dc52c5d819c40c18889372b6201142283e5671b3122',
                'maximum_bytes': 8388608},
 'library-26': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123',
                'format': 'pdf',
                'sha256': 'a06e4e8df32bb56abfe2cd9a01789c9bfe5636593694e6c08e8c2f6f90750c36',
                'maximum_bytes': 8388608},
 'library-27': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=0d46512c-75df-8f5e-b60d-c1f3cbb2864d&groupId=20123',
                'format': 'pdf',
                'sha256': '943f1948ba1c3c03e7a0aa2f20791ccf0d019288a2bfb1f7a32df021c85d63e2',
                'maximum_bytes': 8388608},
 'library-28': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=224db3cd-3e96-4c9c-0577-e536824b67c3&groupId=20123',
                'format': 'pdf',
                'sha256': '06a5d7445d03883b3eef8ae383c47a29eb73a7104b322b47791aa613be22dc40',
                'maximum_bytes': 8388608},
 'library-3': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=d4e559a9-46f6-c4f4-2b59-f52ee9841273&groupId=20123',
               'format': 'pdf',
               'sha256': '65c6f8cf264338bb64283fb1bb257381e3eb15c2e5775172c5ee529e0a5f44d6',
               'maximum_bytes': 8388608},
 'library-31': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=27ed1fcf-a7e3-a182-7141-926c7414a712&groupId=20123',
                'format': 'pdf',
                'sha256': '19f58d9d56e7b996b5bf34462fef603465b934c8d0d5ba7d89b056bf54f1f08e',
                'maximum_bytes': 8388608},
 'library-32': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=eca756af-d875-be88-9d16-e81a20e4a90c&groupId=20123',
                'format': 'pdf',
                'sha256': '94b89a084f3557c6ee4528331399b4f17193e6254a984e112566e9faee25d39b',
                'maximum_bytes': 8388608},
 'library-33': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=094ce56f-53b3-2877-da6b-70c8dacbc907&groupId=20123',
                'format': 'pdf',
                'sha256': '8b329b5cd765fe7eccfe78af1095bfb1c8d7c2bfe35bbeb92940729e82469ebc',
                'maximum_bytes': 8388608},
 'library-4': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=6d8f0f79-4bff-6ae9-dce5-2583c9917d51&groupId=20123',
               'format': 'pdf',
               'sha256': 'f8bc73a3bec743ce4112ce6776638b28239264b3d31950eabe64e5d5c6e81a7f',
               'maximum_bytes': 8388608},
 'library-41': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=561b62e8-e6ba-03a7-4aa8-e45b53f37b4c&groupId=20123',
                'format': 'pdf',
                'sha256': 'eded4318184a0dbc8d8f47d13fe5998e30badad28fc4a1b2dd0572ae980b2e2d',
                'maximum_bytes': 8388608},
 'library-42': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=30f48521-9a82-91a1-2ef7-aa0a555ad429&groupId=20123',
                'format': 'pdf',
                'sha256': '1efd6ad0c507920b8fe35b90b02ac3647d287c00e2b0391da6835d2b7407f181',
                'maximum_bytes': 8388608},
 'library-44': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=72e77c41-1ca4-00e9-bf68-2caf8892ac92&groupId=20123',
                'format': 'pdf',
                'sha256': '6ddb49de1d4edaca13c2ffb70ebc518e031de43dfb69ceb948064a62d29906bd',
                'maximum_bytes': 8388608},
 'library-5': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=78047dbb-9415-265c-bde9-20ad306a2caa&groupId=20123',
               'format': 'pdf',
               'sha256': '6604c70b12eb6eea23c5d89e1bdac70b15693409fd6bf4c59fabe1f3331d120e',
               'maximum_bytes': 8388608},
 'library-53': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=e7fe8b43-b438-b57f-6d0b-57b5ea503ead&groupId=20123',
                'format': 'pdf',
                'sha256': 'c070207f2b8966874bd38cb284ae4150190f260e5c976c737b80d2c37a488abf',
                'maximum_bytes': 8388608},
 'library-54': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=1a7360e2-3fa1-ab9a-4e96-cfc64a774952&groupId=20123',
                'format': 'pdf',
                'sha256': 'f16b87955e79a812a57f7d9132468cc0dde60822072bc5d3aa245a7cfbcfa36b',
                'maximum_bytes': 8388608},
 'library-55': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=9546eb09-2de1-8726-0f71-d200ca12fd0a&groupId=20123',
                'format': 'pdf',
                'sha256': 'e11daaa4505fad452c262327f0e651d16ed8d391c160583e96c8917278ac244c',
                'maximum_bytes': 8388608},
 'library-56': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=4aca6c3e-6a12-e0cb-4d0a-9b4973f2daf4&groupId=20123',
                'format': 'pdf',
                'sha256': 'b946eaeda55b536f56e538ca1220e462d14c870f3b977101d91237475fdbbf2c',
                'maximum_bytes': 8388608},
 'library-57': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=672c6f4a-b75f-3539-d758-1eed064caa4b&groupId=20123',
                'format': 'pdf',
                'sha256': 'd764aa74d8cdb51ff1e841f77211cbb28bd44c1a23b1f04215e8b63388323b8d',
                'maximum_bytes': 8388608},
 'library-6': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=7aeede28-881d-2d91-76d9-6883c31b8abe&groupId=20123',
               'format': 'docx',
               'sha256': '7cbd43370b859165035efbfa3c24e947bab3067ebdc8d21760f73199ad9a82a7',
               'maximum_bytes': 8388608},
 'library-63': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=79019ed0-25cf-d10d-dcfd-d69157e501b6&groupId=20123',
                'format': 'pdf',
                'sha256': 'cead71484bd46d4515371c5628a351c086da970f60111e3864f877ea69a7ff84',
                'maximum_bytes': 8388608},
 'library-64': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=728ebd41-2f6c-5e45-afba-ae10f69969bc&groupId=20123',
                'format': 'pdf',
                'sha256': '921b3e9ef81c45ec10a54a30e9cdef1022f39fb547df8796794c945e4bca26af',
                'maximum_bytes': 8388608},
 'library-8': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=81f1c6a8-eaed-02d5-d3e8-e98686605152&groupId=20123',
               'format': 'pdf',
               'sha256': '4b643205f6b238085c5cb7484f87c8676b3a596e13c444e7dec5bc0db5cd6783',
               'maximum_bytes': 8388608},
 'library-9': {'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=33d91951-c367-26cb-45d8-8c028605d6f8&groupId=20123',
               'format': 'pdf',
               'sha256': '086fc06fa4b361777a6692c138cb1e15c3492b82a40c0d275dd8ff5574cdb92f',
               'maximum_bytes': 8388608},
 'material-1': {'url': 'https://ipdj.gov.pt/documents/20123/159220/Regulamento+Geral+-+Di%C3%A1rio+da+Rep%C3%BAblica.pdf/e7fe8b43-b438-b57f-6d0b-57b5ea503ead?t=1722930876298',
                'format': 'pdf',
                'sha256': 'c070207f2b8966874bd38cb284ae4150190f260e5c976c737b80d2c37a488abf',
                'maximum_bytes': 8388608},
 'material-3': {'url': 'https://ipdj.gov.pt/documents/20123/36911653/Delibera%C3%A7%C3%A3o+CD+PNDPT2026_Projetos+na+area+da+deficiencia_Federa%C3%A7%C3%B5es+Desportivas_+sign.pdf/62fb573c-7f1c-7def-7a49-b17d0e5f9828?t=1774455176985',
                'format': 'pdf',
                'sha256': '8bf56a8943b52876f38fe792b5268e014d912a90498d0321fbc20ef408fc9b9a',
                'maximum_bytes': 8388608},
 'material-4': {'url': 'https://ipdj.gov.pt/documents/20123/32163811/Guia-Procedimentos-Candidatura-medida-2.pdf/33e2ddf0-7b50-e20d-7c08-3824e2913f0c?t=1744213029843',
                'format': 'pdf',
                'sha256': '91e5d1f7db2ad26dc55d2fff3c22a8aba0dcab36af3a16a7019e16967db241d3',
                'maximum_bytes': 8388608},
 'material-5': {'url': 'https://ipdj.gov.pt/documents/20123/32163811/Aviso-de-Abertura-das-Candidaturas-ao-Premio-Desporto-mais-Igual-2025.pdf/d746b27c-b841-c4e1-f49e-a88665da890e?t=1746195973492',
                'format': 'pdf',
                'sha256': 'c7be2d6234e08d3b73a930788a3d4f7c35c44dbf901c4a16f7ce81ba48fea032',
                'maximum_bytes': 8388608},
 'material-6': {'url': 'https://ipdj.gov.pt/documents/20123/159220/Regulamento+Geral+-+Di%C3%A1rio+da+Rep%C3%BAblica+%281%29.pdf/ae00a44c-071f-882d-7b07-2f1f981fb15c?t=1724069919071',
                'format': 'pdf',
                'sha256': 'c070207f2b8966874bd38cb284ae4150190f260e5c976c737b80d2c37a488abf',
                'maximum_bytes': 8388608},
 'material-7': {'url': 'https://ipdj.gov.pt/documents/20123/159220/2025.06.18_Delibera%C3%A7%C3%A3o+CD+PNDpT+Associativismo_Final_signed.pdf/4aca6c3e-6a12-e0cb-4d0a-9b4973f2daf4?t=1750404330913',
                'format': 'pdf',
                'sha256': 'b946eaeda55b536f56e538ca1220e462d14c870f3b977101d91237475fdbbf2c',
                'maximum_bytes': 8388608},
 'medida2-hub': {'url': 'https://ipdj.gov.pt/medida-2-intervencao-comunitaria',
                 'format': 'html',
                 'sha256': '66d62107bc288f4c1b4422a43c3d827d2bb4b280fc7b1647a4109b26d8495aeb',
                 'maximum_bytes': 8388608},
 'motogp': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-no-grande-pr%C3%A9mio-de-portugal-de-motogp-2026',
            'format': 'html',
            'sha256': '78b9801febd22ac0eaf835fadbef88669d0edb46f9e702bd71233a56fc4a7770',
            'maximum_bytes': 8388608},
 'navigation-1': {'url': 'https://ipdj.gov.pt/programa-pejene-programa-de-est%C3%A1gios-de-jovens-estudantes-do-ensino-superior-e-nas-empresas',
                  'format': 'html',
                  'sha256': '28c41eeceed708a15c4e0642ffeea90c81ec3a2643d4ee7ab2f80a7f138f1d20',
                  'maximum_bytes': 8388608},
 'navigation-10': {'url': 'https://ipdj.gov.pt/apoio-funcoes-escolares-laborais-licencas-formacao-especializada',
                   'format': 'html',
                   'sha256': '41a6790fcb8ad01a274bba8b9d9d758500c6b0cbf551a263fc7d7608e5386cf4',
                   'maximum_bytes': 8388608},
 'navigation-11': {'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
                   'format': 'html',
                   'sha256': '81d21cea61c53a1dfc0d391af61d7be55ec7f6ffdbe9a3043d44843d3cf9b9a0',
                   'maximum_bytes': 8388608},
 'navigation-12': {'url': 'https://ipdj.gov.pt/formacao_de_formadores',
                   'format': 'html',
                   'sha256': 'cd3ffda6e18d70be1b338bb005e7903cc8efc65521df344d885ec1108c81d97b',
                   'maximum_bytes': 8388608},
 'navigation-13': {'url': 'https://ipdj.gov.pt/plano-nacional-de-etica-no-desporto-pned',
                   'format': 'html',
                   'sha256': '54b56347ed77836aa10b487e896bfb4bbf24be6ab301508530ac5835140a326e',
                   'maximum_bytes': 8388608},
 'navigation-2': {'url': 'https://ipdj.gov.pt/procurar-emprego',
                  'format': 'html',
                  'sha256': '88f7cf547840f9cefc7a55a3d56a69810990724fd3849d1685fc323e964b28c4',
                  'maximum_bytes': 8388608},
 'navigation-3': {'url': 'https://ipdj.gov.pt/emprego-e-bolsas-de-emprego-no-%C3%A2mbito-da-ue',
                  'format': 'html',
                  'sha256': '73b0ed6e87c785c4d2726ac103262c978a22148b347b4a671dc9c49813aa1509',
                  'maximum_bytes': 8388608},
 'navigation-4': {'url': 'https://ipdj.gov.pt/programa-de-apoio-a-formacao-de-recursos-humanos-pafrh',
                  'format': 'html',
                  'sha256': '7dc038d054f1e2081d6578ba9378de0b8cc33b0748091f96a1c9f37e205aa41d',
                  'maximum_bytes': 8388608},
 'navigation-5': {'url': 'https://ipdj.gov.pt/programa-de-apoio-a-acoes-de-formacao-paaf',
                  'format': 'html',
                  'sha256': 'b89f1cb6fa986efe9a02fcbcd8db3a488f4705f95a61b0610b6e66cb0e81c1c6',
                  'maximum_bytes': 8388608},
 'navigation-6': {'url': 'https://ipdj.gov.pt/bolsas-acad%C3%A9micas',
                  'format': 'html',
                  'sha256': '0da1b270cfb0706d028193a4336604e2316a5c19185b223b655d3a7f8c0d401d',
                  'maximum_bytes': 8388608},
 'navigation-7': {'url': 'https://ipdj.gov.pt/procedimentos-de-mobilidade',
                  'format': 'html',
                  'sha256': 'b7feaffc43a9c64a23c551ba43281d6e4160401ed78200009939b37a7574e9f2',
                  'maximum_bytes': 8388608},
 'navigation-8': {'url': 'https://ipdj.gov.pt/programa-formar-medida-4',
                  'format': 'html',
                  'sha256': '23e9cede1990f97ffb03b0005392302e3c59d855237764cd55d76486534779cf',
                  'maximum_bytes': 8388608},
 'navigation-9': {'url': 'https://ipdj.gov.pt/apoio-formacao-profissional-treinador-desporto',
                  'format': 'html',
                  'sha256': '7998f11b4cf9d6bb9fe8a15680c62d08dc296b5fe2a56ab187bbbd3d199758d7',
                  'maximum_bytes': 8388608},
 'news': {'url': 'https://ipdj.gov.pt/noticias',
          'format': 'html',
          'sha256': '9f824a6868035acd097fa25a5ec393c9c93f5c8d7a3fd120c338978487939425',
          'maximum_bytes': 8388608},
 'news-1': {'url': 'https://ipdj.gov.pt/-/youth-summit-2026-o-futuro-nao-espera',
            'format': 'html',
            'sha256': '49f69b7445cce472688756a42a07b646a482f733aa27d30ecb95b2a56856813a',
            'maximum_bytes': 8388608},
 'news-106': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-inscricoes-abertas',
              'format': 'html',
              'sha256': '2cda427c9fa198b9378383f00e952a17dff6311a2062da8bd13ff03e167c59f4',
              'maximum_bytes': 8388608},
 'news-107': {'url': 'https://ipdj.gov.pt/-/rede-empregar-pretende-impulsionar-a-empregabilidade-jovem',
              'format': 'html',
              'sha256': '052850967bf81098f744ec8b813a0f4adb7f631f72152ffbec654d385a05bc14',
              'maximum_bytes': 8388608},
 'news-109': {'url': 'https://ipdj.gov.pt/-/programa-novas-liderancas-para-um-desporto-igual-abre-inscricoes-para-a-4-edicao',
              'format': 'html',
              'sha256': '98a86fcf32336f3b9c7fd9e9cca161bdf13905cc1050cd174792786c4e164dff',
              'maximum_bytes': 8388608},
 'news-111': {'url': 'https://ipdj.gov.pt/-/4-encontro-direitos-humanos-desporto-e-educacao',
              'format': 'html',
              'sha256': '9fec189192add31dfdf7de5585da68d9183991a0a249b4c7b68ad9cb23b72059',
              'maximum_bytes': 8388608},
 'news-113': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-ikf-beach-korfball-world-cup-europe-2026-candidaturas-abertas',
              'format': 'html',
              'sha256': 'ad9792f37862276beb7fc2fc6354ff18233523cd5a53caff1b12e1d7e1fdb25d',
              'maximum_bytes': 8388608},
 'news-116': {'url': 'https://ipdj.gov.pt/-/encontro-mostra-transfronteirica-desafios-jovens',
              'format': 'html',
              'sha256': '2314b0afd813288a67a6e0d87af075efcc4607e4ce638b81aae6d540b5bd449e',
              'maximum_bytes': 8388608},
 'news-120': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-para-natureza-florestas-inscricoes-abertas-jovens',
              'format': 'html',
              'sha256': '33dfa2f13e3220f9936bfb3363b8b60430e62e3a7a3da1647f9c502f1775f18c',
              'maximum_bytes': 8388608},
 'news-122': {'url': 'https://ipdj.gov.pt/-/curso-protecao-contra-a-violencia-e-abuso-no-desporto-aprendizagens-essenciais-para-pontos-focais-candidaturas-abertas',
              'format': 'html',
              'sha256': 'cd7ef49333313238587c09eccc911c30a230fd23058e1f8062b547c8a92ea154',
              'maximum_bytes': 8388608},
 'news-125': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-futebol-portugal-nigeria-candidaturas-abertas',
              'format': 'html',
              'sha256': 'ad20d3fdfcb167b7d2024ddd8573bb51065ccf5fba53a4afcf1186a14fd01b7f',
              'maximum_bytes': 8388608},
 'news-127': {'url': 'https://ipdj.gov.pt/-/estao-abertas-as-candidaturas-a-quinta-edicao-desporto-mais-acessivel',
              'format': 'html',
              'sha256': '28bd9d67e997368829ce9c898d3a51abec3f090643f6125c5de14f0d251e402c',
              'maximum_bytes': 8388608},
 'news-133': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-futebol-portugal-chile-candidaturas-abertas',
              'format': 'html',
              'sha256': '4cbf7c1f827211a7877df2da1441d917b07dd6c4a99fdf411450d126748c3e4b',
              'maximum_bytes': 8388608},
 'news-135': {'url': 'https://ipdj.gov.pt/-/4-encontro-odec-educacao-direitos-humanos-e-desporto',
              'format': 'html',
              'sha256': '2d8bd67311045efd465b744779f7c64f7c2e1e9327df2284c9187217b5c3d051',
              'maximum_bytes': 8388608},
 'news-136': {'url': 'https://ipdj.gov.pt/-/premios-clube-top-edicao-2026-candidaturas-abertas',
              'format': 'html',
              'sha256': 'e5816ae8ea42c38a0e7c96f9f177a4b1d80ef4e2a3c6d4968271766b64fd1155',
              'maximum_bytes': 8388608},
 'news-137': {'url': 'https://ipdj.gov.pt/-/seminario-lideranca-do-desporto-no-feminino-destaca-igualdade-de-genero-e-boas-praticas-no-setor',
              'format': 'html',
              'sha256': '80cdca123ce711447cf494a2436019898072b44388b9f76bd4eef8b430730048',
              'maximum_bytes': 8388608},
 'news-139': {'url': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-cinco-ponto-zero-prazo-de-candidaturas-para-entidades-organizadoras-prorrogado',
              'format': 'html',
              'sha256': '810344f3ff29a5e29db43839f6a277979792f3107ee0f59ccf82d75576f64f18',
              'maximum_bytes': 8388608},
 'news-146': {'url': 'https://ipdj.gov.pt/-/expo-future-2026',
              'format': 'html',
              'sha256': 'd97946cf28bb26eec374263a0959a0995d7e86143c2ae09e25f36090f8cf0317',
              'maximum_bytes': 8388608},
 'news-154': {'url': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-oficina-cultural-candidaturas-para-entidades-organizadoras',
              'format': 'html',
              'sha256': '7410b9a2e4c6937b151177fe7fe3b3b62e3e580221ab572c38c608e948beb729',
              'maximum_bytes': 8388608},
 'news-156': {'url': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-cinco-ponto-zero-candidaturas-para-entidades-organizadoras-ate-15-de-maio',
              'format': 'html',
              'sha256': '91ffb22a29e2893739cb597df93956a3a756371a29741bd382448bb7aa460492',
              'maximum_bytes': 8388608},
 'news-158': {'url': 'https://ipdj.gov.pt/-/programa-de-reabilitacao-de-instalacoes-desportivas-2026-abertura-de-candidatura',
              'format': 'html',
              'sha256': '35740177aa717935d21e53096c0d982158c21784b711b22370c9e7436877930d',
              'maximum_bytes': 8388608},
 'news-161': {'url': 'https://ipdj.gov.pt/-/final-da-taca-de-portugal-2026-futebol-voluntariado-jovem-inscricoes-abertas',
              'format': 'html',
              'sha256': '2e79d4d28893cb71549af9c1fad4436650898df4461496807b86fce0ddd62e6f',
              'maximum_bytes': 8388608},
 'news-162': {'url': 'https://ipdj.gov.pt/-/final-feminina-taca-de-portugal-2026-futebol-voluntariado-jovem-inscricoes-abertas',
              'format': 'html',
              'sha256': '881df1d61d78d73bb7c92427446c46dd147c0a7043edf6a91e16200a6e7f2f9d',
              'maximum_bytes': 8388608},
 'news-167': {'url': 'https://ipdj.gov.pt/-/lisboa-acolhe-o-sport-for-all-training-workshop-dedicado-a-inclusao-no-desporto',
              'format': 'html',
              'sha256': '2dcdd0aac0f4147d468412cf5240efa3a204844fb681f728107678d31f1d89db',
              'maximum_bytes': 8388608},
 'news-169': {'url': 'https://ipdj.gov.pt/-/nova-acao-envolve-te-agora-nos-abre-candidaturas-para-projetos-de-voluntariado-jovem',
              'format': 'html',
              'sha256': 'd5f5cbebcf1ffc6803ba2daf0cf83ddf50e7ac0126f579a2965d11f52e554bde',
              'maximum_bytes': 8388608},
 'news-173': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-no-formula-kite-world-championship-2026-candidaturas-abertas',
              'format': 'html',
              'sha256': '3c01e2d412c12b874a9c71cfd2b292893750534af18f5b925cc28f4f147f5780',
              'maximum_bytes': 8388608},
 'news-18': {'url': 'https://ipdj.gov.pt/-/capital-nacional-juventude-candidaturas-abertas',
             'format': 'html',
             'sha256': 'b454867e3cc7ea34e4ae25b87f5f0406a16bd0996a11542f23359178baecd48b',
             'maximum_bytes': 8388608},
 'news-180': {'url': 'https://ipdj.gov.pt/-/integridade-no-desporto-em-destaque-em-conferencia-na-faculdade-de-direito-de-lisboa',
              'format': 'html',
              'sha256': 'f34a115480b6c340227866870e5664e3a9eb41068fea49befbe3512dceac600f',
              'maximum_bytes': 8388608},
 'news-182': {'url': 'https://ipdj.gov.pt/-/programa-nacional-de-desporto-para-todos-pndpt-associativismo-2026-2027-candidaturas-abertas',
              'format': 'html',
              'sha256': '3a50c3d9f9100302dbda1da77db161f3159aa105c109c849dd361303b18f8ed5',
              'maximum_bytes': 8388608},
 'news-19': {'url': 'https://ipdj.gov.pt/-/unidade-apoio-formativo-associativismo-jovem-candidaturas-abertas',
             'format': 'html',
             'sha256': '602821b3db1245a63eb2500ec7147fd491831cc591c739cce2d7d365563c9ddd',
             'maximum_bytes': 8388608},
 'news-191': {'url': 'https://ipdj.gov.pt/-/braga-recebe-o-evento-juventude-em-movimento-portugal-e-marrocos-juntos-pelo-dialogo-intercultural-voluntariado-e-participacao-civica',
              'format': 'html',
              'sha256': 'f957e4c87830fe379c765feb20eae0315b8428ead5e4de23d12bdaf3c5803c0b',
              'maximum_bytes': 8388608},
 'news-193': {'url': 'https://ipdj.gov.pt/-/programa-nacional-de-desporto-para-todos-candidaturas-para-apoio-a-projetos-na-area-da-deficiencia',
              'format': 'html',
              'sha256': '03cf52ccf31830b22f2ab66a520ecaea92dd4a7d8bf4d9ec77913826b6832aa7',
              'maximum_bytes': 8388608},
 'news-195': {'url': 'https://ipdj.gov.pt/-/programa-associa-te-candidaturas-2026-ate-12-de-maio',
              'format': 'html',
              'sha256': 'a2702fba200e1303db72faed2cb3587ee2919118daba340ffbc267c04db13b7d',
              'maximum_bytes': 8388608},
 'news-197': {'url': 'https://ipdj.gov.pt/-/webinar-europeu-media-representation-of-persons-with-disabilities-in-sport',
              'format': 'html',
              'sha256': 'eded2290ee1518d1ee0d5b055c295944db882329b5898f1e66872248066ad8c2',
              'maximum_bytes': 8388608},
 'news-199': {'url': 'https://ipdj.gov.pt/-/candidaturas-programa-voluntariado-jovem-para-natureza-florestas-2026',
              'format': 'html',
              'sha256': 'b0867dc8cd616daf89ef19361316dce9d310925805efb2c312f9c50e1779429e',
              'maximum_bytes': 8388608},
 'news-20': {'url': 'https://ipdj.gov.pt/-/os-jovens-desistiram-da-politica-webinar-desafia-mitos-e-promove-o-debate',
             'format': 'html',
             'sha256': 'f97c6f07e8d435d82a05075250886f778375504ae3694909857c5f3c47db80e3',
             'maximum_bytes': 8388608},
 'news-201': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-mundial-de-superbikes-2026-candidaturas-abertas',
              'format': 'html',
              'sha256': '2001fa34ee8e1d6a1b26ab47899b1654781aa9bc9e162b783b7a729600ad6a7c',
              'maximum_bytes': 8388608},
 'news-204': {'url': 'https://ipdj.gov.pt/-/programa-otl-modalidade-de-longa-duracao-abre-candidaturas-2026',
              'format': 'html',
              'sha256': '65e03d12b3e4c05688b53623ab55da2f3ee1c80bf1451276a8399d99533ed088',
              'maximum_bytes': 8388608},
 'news-206': {'url': 'https://ipdj.gov.pt/-/aberto-procedimento-para-selecao-de-entidade-organizadora-da-edicao-2026-do-concurso-e-mostra-nacional-jovens-criadores',
              'format': 'html',
              'sha256': 'fa47fb5eab753edb7710cf05b32c6180e741a558424104db49dceb6a0d03ab42',
              'maximum_bytes': 8388608},
 'news-21': {'url': 'https://ipdj.gov.pt/-/formacao-transformacao-digital-para-as-organizacoes-desportivas-inscricoes-abertas',
             'format': 'html',
             'sha256': '3b55a6a54d96e24666c68158203a5dcb553f10947c133d8cf6b3fb1397281c2d',
             'maximum_bytes': 8388608},
 'news-210': {'url': 'https://ipdj.gov.pt/-/dia-nacional-do-estudante-assinalado-com-programa-nacional-de-promocao-da-participacao-e-do-associativismo-estudantil',
              'format': 'html',
              'sha256': '9478c7d5438579aea784a8b021829852aada84d2f219509f57afa6b0acf3eed2',
              'maximum_bytes': 8388608},
 'news-214': {'url': 'https://ipdj.gov.pt/-/internet-mais-segura-em-lisboa-e-vale-do-tejo',
              'format': 'html',
              'sha256': 'b82956252b6bf427f83f04e4c6941d165c5f09b9e92e6c192a5c7c0c5e63836f',
              'maximum_bytes': 8388608},
 'news-218': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-na-qualifica-2026-candidaturas-abertas',
              'format': 'html',
              'sha256': '88acb0f5159999132174bcc611413f71db8f6552e43e56b2d61259b646e67b65',
              'maximum_bytes': 8388608},
 'news-22': {'url': 'https://ipdj.gov.pt/-/prorrogado-o-prazo-para-apresentacao-de-candidaturas-ao-programa-voluntariado-jovem-para-a-natureza-e-florestas-envolve-te-in',
             'format': 'html',
             'sha256': 'e3f2730e6b742ceb8ddfb8ce875b4769946259ccc2322641261ba62083cd510d',
             'maximum_bytes': 8388608},
 'news-230': {'url': 'https://ipdj.gov.pt/-/voluntariado-navegas-em-seguranca-candidaturas-abertas-2026',
              'format': 'html',
              'sha256': 'e6cdd9aac82bafafe3232a34280743f9858f564c0558d5982c308741c2de7f01',
              'maximum_bytes': 8388608},
 'news-233': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-futebol-portugal-vs-eslovaquia-candidaturas-abertas',
              'format': 'html',
              'sha256': '3ccfc839b5f0c825b0ab31d981ec70e32d3971319d7973b2dc34043585dfd2d4',
              'maximum_bytes': 8388608},
 'news-236': {'url': 'https://ipdj.gov.pt/-/candidaturas-abertas-para-voluntariado-jovem-na-futuralia-2026',
              'format': 'html',
              'sha256': '3d1e8fe35e3661fc5f9ab4fec49f4f9e6e3caafb3e9f6c4cd2eb346af3eea7ea',
              'maximum_bytes': 8388608},
 'news-237': {'url': 'https://ipdj.gov.pt/-/programa-de-apoio-pontual-para-associacoes-juvenis-de-estudantes-e-de-carater-juvenil-candidaturas-abertas-1',
              'format': 'html',
              'sha256': '530394c114b247aa5495d86a9d2580ced06c8f1a329fcaa4c3d5f0c8c930be67',
              'maximum_bytes': 8388608},
 'news-238': {'url': 'https://ipdj.gov.pt/-/sport-for-all-training-workshop-candidaturas-a-decorrer',
              'format': 'html',
              'sha256': 'da31cf3683f35f16f69e9de2d6fe72806caf000aa501eae48d239058ac271948',
              'maximum_bytes': 8388608},
 'news-24': {'url': 'https://ipdj.gov.pt/-/envolve-te-in-vjnf-projeto-floresta-limpa-e-vigiada-2026',
             'format': 'html',
             'sha256': '7f8e341218654efa42a05ab215b8c846d71a9f5553902c5081580a59d3442a37',
             'maximum_bytes': 8388608},
 'news-244': {'url': 'https://ipdj.gov.pt/-/abertura-de-candidaturas-4-edicao-do-selo-estudante-atleta-2026-2028',
              'format': 'html',
              'sha256': '2ca93a547ebb3dbf2f93f069ed97359c14e47c09613ac6670233590530632878',
              'maximum_bytes': 8388608},
 'news-25': {'url': 'https://ipdj.gov.pt/-/envolve-te-in-voluntariado-jovem-para-a-natureza-e-florestas-eco-brigada-jovem-nisa-2026',
             'format': 'html',
             'sha256': 'af53b43d52e095d303913ae591afcd4cf55466d07519de4e1471abd3f2d9de23',
             'maximum_bytes': 8388608},
 'news-250': {'url': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-pascoa-2026-candidaturas-para-entidades-promotoras-ate-27-de-fevereiro',
              'format': 'html',
              'sha256': '02802a25218adfec405980a4771fe24b424b7182a83e28d0bea08d729b64d251',
              'maximum_bytes': 8388608},
 'news-251': {'url': 'https://ipdj.gov.pt/-/dia-da-internet-mais-segura-2026-inscricoes-abertas',
              'format': 'html',
              'sha256': 'ad2c4c0d5eac35e45bda6269aa1ba13d2080a9c9269a4e6de0665dbf4bb97323',
              'maximum_bytes': 8388608},
 'news-260': {'url': 'https://ipdj.gov.pt/-/erasmus-juventude-e-desporto-2026-candidaturas-a-decorrer',
              'format': 'html',
              'sha256': 'd54fad24dfc3bf2295241a5d94b09585a8eeab1ddb59fba918dc9ba56afc5613',
              'maximum_bytes': 8388608},
 'news-261': {'url': 'https://ipdj.gov.pt/-/candidaturas-abertas-para-a-xiv-edicao-do-premio-de-imprensa-desporto-com-etica',
              'format': 'html',
              'sha256': '700fb3804224feca43428e547bce3387a37b419b654bed8bafea15e8545fc9a6',
              'maximum_bytes': 8388608},
 'news-262': {'url': 'https://ipdj.gov.pt/-/concurso-literario-a-etica-na-vida-e-no-desporto-candidaturas-abertas-ate-28-de-fevereiro',
              'format': 'html',
              'sha256': '9179a032a6b65cd709b12c66cdc9c8694ea2263208c41f1d38124e10b1200f33',
              'maximum_bytes': 8388608},
 'news-263': {'url': 'https://ipdj.gov.pt/-/concurso-euroscola-com-candidaturas-abertas',
              'format': 'html',
              'sha256': 'e4ae50f51585f9b5b31976fbc9089e7962742b4dfd9b3ac9b2fbebea710a62f8',
              'maximum_bytes': 8388608},
 'news-275': {'url': 'https://ipdj.gov.pt/-/programas-de-apoio-ao-associativismo-jovem-paaj-com-candidaturas-abertas',
              'format': 'html',
              'sha256': '4917e3393799f43b8153a56437cc53ed5898bafa75b42c6d60958847eae00d02',
              'maximum_bytes': 8388608},
 'news-278': {'url': 'https://ipdj.gov.pt/-/candidaturas-capital-nacional-de-juventude-2026-abertas',
              'format': 'html',
              'sha256': '2d66b948adcb71406cd925ecf8d7dfa44efda1c9b5a7af720dff46e0b1dac23f',
              'maximum_bytes': 8388608},
 'news-279': {'url': 'https://ipdj.gov.pt/-/campos-de-trabalho-internacionais-2026-com-candidaturas-abertas-para-entidades',
              'format': 'html',
              'sha256': '0cfd5d456e139bdfaaa35f70ea6a9573ae504c4219b313f8facc71a6ccc1e621',
              'maximum_bytes': 8388608},
 'news-282': {'url': 'https://ipdj.gov.pt/-/webinar-campos-de-trabalho-internacionais-sessao-de-esclarecimentos',
              'format': 'html',
              'sha256': '84305a681f7606b02602e330e7aa842deba7e8a53d233c50489daed0ea7add54',
              'maximum_bytes': 8388608},
 'news-287': {'url': 'https://ipdj.gov.pt/-/programa-anda-conhecer-portugal-tem-mais-5000-dormidas-exclusivas-para-associacoes-federacoes-de-juventude-e-de-estudantes',
              'format': 'html',
              'sha256': 'b43b74768534520882585ce0e1ff65c84b65d7ba26c6c6e6903820ddbc7a5f8d',
              'maximum_bytes': 8388608},
 'news-288': {'url': 'https://ipdj.gov.pt/-/rural-youth-future-3',
              'format': 'html',
              'sha256': 'fd48ad2b6850b9dadd86a33cd7e94b1d5e82a43a02c4070b364a1e8f11474288',
              'maximum_bytes': 8388608},
 'news-292': {'url': 'https://ipdj.gov.pt/-/webinar-diplomacia-desportiva-lusofonia',
              'format': 'html',
              'sha256': '3b8d8f02dfcbe9b44072fbe70f062a26fd376506b77f416458e7bd0ddb84e1d9',
              'maximum_bytes': 8388608},
 'news-305': {'url': 'https://ipdj.gov.pt/-/abertura-de-concurso-para-diretor-geral-na-comunidade-dos-paises-de-lingua-portuguesa',
              'format': 'html',
              'sha256': 'a1572167622e7abb25ba1d28f6e2814d435dec32c050f551fe2b9b4732b44684',
              'maximum_bytes': 8388608},
 'news-309': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-no-stand-da-lisboa-games-week-candidaturas-abertas',
              'format': 'html',
              'sha256': '93a29e704318dcec1948c8ff370e89fd2ccd33b617e61d163fd6ec1148a5c89f',
              'maximum_bytes': 8388608},
 'news-31': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-semana-no-beactive-sport-village-setembro-jamor',
             'format': 'html',
             'sha256': 'eff2e349c7828ff00424127258997877a49525522a10e63510a383924c69e5ae',
             'maximum_bytes': 8388608},
 'news-310': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-portugal-vs-pa%C3%ADses-baixos',
              'format': 'html',
              'sha256': 'a6c4ab23b834d6c9e19161ba72ec915b87b25905451b2998d5aa867c19eac0ef',
              'maximum_bytes': 8388608},
 'news-313': {'url': 'https://ipdj.gov.pt/-/ipdj-organiza-formacao-de-formadores-sob-o-tema-metodologia-ativas-para-o-desenvolvimento-de-competencias',
              'format': 'html',
              'sha256': '1ba39e54c0bcfd06313e01c607e58e244f9d20ee1cd05a242c5e9d5e22de0da0',
              'maximum_bytes': 8388608},
 'news-318': {'url': 'https://ipdj.gov.pt/-/acao-de-formacao-os/as-jovens-e-a-seguranca-digital-ii',
              'format': 'html',
              'sha256': '29e066e97bde2ff8163727fafb2cde17e2d910a6a22f3fdb7d5648dbc97af40d',
              'maximum_bytes': 8388608},
 'news-32': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-no-dia-do-desporto-inclusivo-24-de-setembro-jamor',
             'format': 'html',
             'sha256': 'e875c84cbc8775a8fe44fe8645b2aa2720c707315375ddb3a197785548b83776',
             'maximum_bytes': 8388608},
 'news-320': {'url': 'https://ipdj.gov.pt/-/ipdj-investe-na-formacao-artistica-e-no-talento-jovem-atraves-do-apoio-a-orquestra-sinfonica-juvenil',
              'format': 'html',
              'sha256': 'da5fe11b3896d0a57e46a55fa6e993da1a0bbf1679c15a923b220cc4349e43c2',
              'maximum_bytes': 8388608},
 'news-322': {'url': 'https://ipdj.gov.pt/-/simposio-clube-top-digitalizacao-de-clubes-desportivos-mais-do-que-o-futuro-o-presente',
              'format': 'html',
              'sha256': '47a62319f9fe4f649f8f5987f614289461c455858f95966e130210ebb0fb0e02',
              'maximum_bytes': 8388608},
 'news-325': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-portugal-vs-armenia',
              'format': 'html',
              'sha256': '8f4cb280e585d2012ae43ea833f060eeba4cda675e1b672b041bf42d1ac74497',
              'maximum_bytes': 8388608},
 'news-336': {'url': 'https://ipdj.gov.pt/-/iii-seminario-regional-de-dinamizadores-comunitarios-do-programa-escolhas',
              'format': 'html',
              'sha256': '39d3e3b4eff418062d4a8325871994f5286d237dae674f01247861f726361622',
              'maximum_bytes': 8388608},
 'news-340': {'url': 'https://ipdj.gov.pt/-/seminario-internacional-da-formacao-a-competicao-como-conciliar',
              'format': 'html',
              'sha256': '24c2fcccfeec03e0d223d86fe7f5c3edc501089c8a9052a559f6091993939464',
              'maximum_bytes': 8388608},
 'news-343': {'url': 'https://ipdj.gov.pt/-/fundacao-jornada-abre-candidaturas-ao-programa-jovens-agentes-de-esperanca',
              'format': 'html',
              'sha256': '502bca7c98d9be1161a9c20e662b2b8483bc90c9b949749dc88aa2504039e374',
              'maximum_bytes': 8388608},
 'news-344': {'url': 'https://ipdj.gov.pt/-/candidaturas-abertas-aos-premios-boas-praticas-associativismo-jovem-2025',
              'format': 'html',
              'sha256': '20f09ec92fb04f6fd57ee9656dd019df01269dbab0f148a4ba4eace5031430ae',
              'maximum_bytes': 8388608},
 'news-345': {'url': 'https://ipdj.gov.pt/-/voluntariado-motogp-algarve',
              'format': 'html',
              'sha256': '7e05cfe7305000980e0ee6d59b4bc0489d885be2473a098b3c2907090a5638cd',
              'maximum_bytes': 8388608},
 'news-346': {'url': 'https://ipdj.gov.pt/-/webinar-formacao-e-desenvolvimento-de-treinadores-arbitros-e-agentes-do-desporto-paralimpico',
              'format': 'html',
              'sha256': '8aaa404f6ed84fa9d2a83ecd7d858523d7eea1671cb15d0148ef68314ecc7f39',
              'maximum_bytes': 8388608},
 'news-349': {'url': 'https://ipdj.gov.pt/-/cig-e-ipdj-realizam-acoes-de-capacitaao-sobre-igualdade-de-genero-violencia-de-genero-e-nao-discriminacao',
              'format': 'html',
              'sha256': '53be3bf3aca26c865be7624aa6015e1e1513be59f9450e01fb78f69580ec4954',
              'maximum_bytes': 8388608},
 'news-352': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-4-edicao-da-porto-design-biennale-2025',
              'format': 'html',
              'sha256': 'acd94c0c643b6af9d1388bfd8524f9910a5eeeeab239b6303b04c36b2020904a',
              'maximum_bytes': 8388608},
 'news-358': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-portugal-republica-da-irlanda-e-portugal-hungria',
              'format': 'html',
              'sha256': 'a2ddf96aecb672dac5b83d3296b1cfe8f960c936f60c72cbc13a67f3203b11c6',
              'maximum_bytes': 8388608},
 'news-359': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-portugal-football-summit-candidaturas-abertas',
              'format': 'html',
              'sha256': '11b3ce2c51aea5137020fdb91a0c2ac6681adca9d6b5a4bee86a0f976a3c015d',
              'maximum_bytes': 8388608},
 'news-36': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-no-portugal-football-summit-cidade-do-futebol',
             'format': 'html',
             'sha256': 'c8f2717548876ad09bf10dca59bc1faacbe3ef840df76faf6862484f647dd7bf',
             'maximum_bytes': 8388608},
 'news-361': {'url': 'https://ipdj.gov.pt/-/programa-cuida-te-medida1-apoio-personalizado-abre-candidaturas-para-o-dispositivo-1-1-unidades-moveis',
              'format': 'html',
              'sha256': 'b99935128e2ba82f4b4bd4b4313c213eab425b7e889dfc6eaa7fbad72b8f178b',
              'maximum_bytes': 8388608},
 'news-370': {'url': 'https://ipdj.gov.pt/-/webinar-bandeira-da-etica-submissao-de-candidaturas-e-o-novo-regulamento',
              'format': 'html',
              'sha256': '6623de9aa38a66cfcf714f44e4a7a385db372ca31ac0cb3a427c56adac44bc1b',
              'maximum_bytes': 8388608},
 'news-375': {'url': 'https://ipdj.gov.pt/-/intercambio-juvenil-studiostar-cjl',
              'format': 'html',
              'sha256': '26f868c6d575b923a10e089cd7739ba0882c36bdfb79d89d20a7115b99c7afaa',
              'maximum_bytes': 8388608},
 'news-377': {'url': 'https://ipdj.gov.pt/-/ipdj-dinamiza-youth-summit-25-o-maior-encontro-nacional-de-capacitacao-e-participacao-juvenil',
              'format': 'html',
              'sha256': '3956869146d14e09d41f33d7c389838a4b1638fea005a9fbbcff415767c46e25',
              'maximum_bytes': 8388608},
 'news-379': {'url': 'https://ipdj.gov.pt/-/academias-de-desenvolvimento-juvenil-2025-cinco-sessoes-regionais-online-ate-ao-youth-summit',
              'format': 'html',
              'sha256': 'aee01bad47d48eaf0260d3d47706162cf13a6c1626339b1f1f73cd5c6a87bac9',
              'maximum_bytes': 8388608},
 'news-382': {'url': 'https://ipdj.gov.pt/-/academia-de-desenvolvimento-juvenil-2025-arranca-no-alentejo',
              'format': 'html',
              'sha256': 'f055e1447c6564c02e11c3d611651e43b0908531a90a1e12e7d47f3e3bfacfc4',
              'maximum_bytes': 8388608},
 'news-384': {'url': 'https://ipdj.gov.pt/-/edicao-2025-2026-do-parlamento-dos-jovens-abre-inscricoes-para-escolas-de-1-de-setembro-a-15-de-outubro',
              'format': 'html',
              'sha256': 'f7d16dede73acca2ddaded78e58f46f0f5b125935620c05f55fa8f257a5a0b69',
              'maximum_bytes': 8388608},
 'news-388': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-na-aldeia-de-arrouquelas',
              'format': 'html',
              'sha256': 'be96158a4db3d3265704d7602598662d039593648de1a65cee34f09cd1f91e11',
              'maximum_bytes': 8388608},
 'news-39': {'url': 'https://ipdj.gov.pt/-/mostra-nacional-jovens-criadores-2026-prorrogacao-das-candidaturas',
             'format': 'html',
             'sha256': 'a62d283dfe78f704d2d4193371cd8ce469f2d66ff898af2e462c81670fbdfa5a',
             'maximum_bytes': 8388608},
 'news-390': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-na-fabrica-de-vale-de-milhacos',
              'format': 'html',
              'sha256': '3da61bed94f7880ec7671af8e066dc7c7c6501d9b5a90d40b32bbc63c465c533',
              'maximum_bytes': 8388608},
 'news-392': {'url': 'https://ipdj.gov.pt/-/candidaturas-ao-premio-do-cartao-branco-2024-2025',
              'format': 'html',
              'sha256': 'e30d507aaa12a1f7a601c69de9544e80d673abe85a6d62314bfc7833380e3a0c',
              'maximum_bytes': 8388608},
 'news-394': {'url': 'https://ipdj.gov.pt/-/programa-de-reabilitacao-de-instalacoes-desportivas-2025-abre-candidaturas',
              'format': 'html',
              'sha256': '1e4016e6f970ba6a10ac3f1338e3709ef7db6fcfb91e689927c81ee71f8feef0',
              'maximum_bytes': 8388608},
 'news-396': {'url': 'https://ipdj.gov.pt/-/webinares-licenciamento-instalacoes-desportivas-enfoque-adaptados',
              'format': 'html',
              'sha256': '348143a67cd5205fe08cc6cb9038502035a41b175b3b234195b9398ca2c6b0dc',
              'maximum_bytes': 8388608},
 'news-397': {'url': 'https://ipdj.gov.pt/-/liga-portugal-e-ipdj-criam-bolsa-de-voluntarios-para-as-competicoes-profissionais',
              'format': 'html',
              'sha256': 'de64d778285bea01687247cba010d88a2cac4b1af25ba6f237d2a331c5ea787d',
              'maximum_bytes': 8388608},
 'news-40': {'url': 'https://ipdj.gov.pt/-/edicao-2026-2027-parlamento-jovens-abre-inscricoes-para-escolas',
             'format': 'html',
             'sha256': '4d80e4f66e273ce5ede0a40d18c7daeb8e5f9b16c9dc8e3c0e1117f56c696df6',
             'maximum_bytes': 8388608},
 'news-400': {'url': 'https://ipdj.gov.pt/-/curso-de-formacao-continua-construcao-de-ambientes-pedagogicos-positivos-para-criancas-e-jovens-no-desporto',
              'format': 'html',
              'sha256': '6ca4d1e164d41d2fc4729f75514079af2d284840eb3161dfba721947b9a2cd1b',
              'maximum_bytes': 8388608},
 'news-402': {'url': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-ferias-5ponto0-financiamento-de-atividades',
              'format': 'html',
              'sha256': '493885d1eb73a78450f934f78d4c1a5291cbd0bc66ee04272ab837693ac3cb8b',
              'maximum_bytes': 8388608},
 'news-405': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-no-festival-sol-da-caparica-2025',
              'format': 'html',
              'sha256': '705070ab71c70f7163aff42c59e2c0154789ba57445e190d5a84db9193f2e216',
              'maximum_bytes': 8388608},
 'news-406': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-no-parque-ecologico-da-varzea-2025',
              'format': 'html',
              'sha256': '37071a9e9e73c1344c91d17d7ced530734cd163621da14427dc2748d0b99e8ec',
              'maximum_bytes': 8388608},
 'news-407': {'url': 'https://ipdj.gov.pt/-/ipdj-desafia-jovens-a-criar-nova-identidade-dos-seus-espacos-de-juventude',
              'format': 'html',
              'sha256': '307fc6711b9a4c15f267d35d1fdb52287c00b248e4075a249a35c4f58515a891',
              'maximum_bytes': 8388608},
 'news-409': {'url': 'https://ipdj.gov.pt/-/youth-summit-2025-o-maior-encontro-nacional-de-capacitacao-e-participacao-juvenil',
              'format': 'html',
              'sha256': '4caaa98a60c40b0bc988ce74116022b256327ec97f32c9f382ea88ebf0feaead',
              'maximum_bytes': 8388608},
 'news-41': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-portugal-vs-pais-de-gales-no-estadio-jose-alvalade',
             'format': 'html',
             'sha256': 'e950291b21ad9d61c09da0906637bde2cee40e98b8fbc0bdc46a5a935e021b83',
             'maximum_bytes': 8388608},
 'news-416': {'url': 'https://ipdj.gov.pt/-/festival-politica-e-ipdj-abrem-candidaturas-para-as-edicoes-de-loule-e-coimbra-do-concurso-para-jovens-artistas-ativistas-e-criadores',
              'format': 'html',
              'sha256': '379dd75104a59719fd2f2decaf0f1ff1430512d4956d49d4c244cea6308e4f55',
              'maximum_bytes': 8388608},
 'news-419': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-para-a-natureza-e-florestas-na-nazare-2025',
              'format': 'html',
              'sha256': '0b4d2503b0993e8cbe3b0c1f6961d53018d54221756de4bccf7c424ef719457e',
              'maximum_bytes': 8388608},
 'news-431': {'url': 'https://ipdj.gov.pt/-/entretanto-usamos-os-3rs-voluntariado-jovem-para-a-natureza-e-florestas',
              'format': 'html',
              'sha256': '3d51a18ae17fe84f82d4bc670ac8bcdc09e0be19967d8e3c943c205e8333abc2',
              'maximum_bytes': 8388608},
 'news-433': {'url': 'https://ipdj.gov.pt/-/eu-sou-reserva-da-biosfera-das-berlengas-2025-unesco',
              'format': 'html',
              'sha256': '2ad6ba1319646e9d407d70829fc7c9bf164dced8ea782de29df1644b9df28e50',
              'maximum_bytes': 8388608},
 'news-437': {'url': 'https://ipdj.gov.pt/-/academia-de-desenvolvimento-juvenil-2025',
              'format': 'html',
              'sha256': '317ff5ee0ad5a53722b010ead42ad46a0fc61092360c3a5d842293a4b863a6fe',
              'maximum_bytes': 8388608},
 'news-439': {'url': 'https://ipdj.gov.pt/-/programa-nacional-de-desporto-para-todos-associativismo-candidaturas-abertas-ate-23-de-julho',
              'format': 'html',
              'sha256': 'e952be33e3f12f12afb170b13437a9bf7329b63646a2d62baf4dd4b05637c08b',
              'maximum_bytes': 8388608},
 'news-44': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-no-festival-do-crato',
             'format': 'html',
             'sha256': '9ea3989319d363d05f84d712a2802defe203f870093ed8893918dd2ac7cad4a9',
             'maximum_bytes': 8388608},
 'news-443': {'url': 'https://ipdj.gov.pt/-/webinar-sessao-de-apresentacao-da-11-edicao-da-semana-europeia-do-desporto-sed-2025',
              'format': 'html',
              'sha256': '535c68217f4511d84b037b5cf73d89b4aa3c3d36a3eec136f42cb350ebb173eb',
              'maximum_bytes': 8388608},
 'news-446': {'url': 'https://ipdj.gov.pt/-/abertas-ate-dia-31-de-julho-as-candidaturas-a-mostra-nacional-de-jovens-criadores',
              'format': 'html',
              'sha256': '7c4b1fb1ebe4af3a5eed42387045a6e68e993f2b5d27d27165867655353d101a',
              'maximum_bytes': 8388608},
 'news-45': {'url': 'https://ipdj.gov.pt/-/envolve-te-in-vjnf-projeto-welcome-to-the-jungle-h20',
             'format': 'html',
             'sha256': '893c212f11d0e595d7911306eae2bf29532536c02eaee14273fecdbda4ed4c44',
             'maximum_bytes': 8388608},
 'news-453': {'url': 'https://ipdj.gov.pt/-/namorar-com-fair-play-em-belmonte',
              'format': 'html',
              'sha256': 'c157f27c3527c3f47b96d883f3841b05f63007f97d7b8699148f7e3d548b12ae',
              'maximum_bytes': 8388608},
 'news-455': {'url': 'https://ipdj.gov.pt/-/associacao-amato-lusitano-promove-sessoes-navegas-em-seguranca-em-castelo-branco',
              'format': 'html',
              'sha256': '8a0510e30a80adde2afd9fe5502284b00c4bc6dbbd7e1359d578689cdc610c00',
              'maximum_bytes': 8388608},
 'news-464': {'url': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-feriascincopontozero-abertas-as-inscricoes-para-jovens',
              'format': 'html',
              'sha256': '64835d2fa37f6cc2e062ee5df71a583f7b9428710142c7a9788090fd986c036e',
              'maximum_bytes': 8388608},
 'news-465': {'url': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-abertas-as-inscricoes-para-jovens',
              'format': 'html',
              'sha256': 'bcab358e32b471b603137e97f342214365f2f7ca9bfa970342bdd849c0c32b39',
              'maximum_bytes': 8388608},
 'news-469': {'url': 'https://ipdj.gov.pt/-/projeto-ritmo-da-mudanca-danca-para-um-futuro-igualitario',
              'format': 'html',
              'sha256': '596d24fa9b05d822c979720c0f3e650ac866934954d5b79e4f1e436c3c0c4e11',
              'maximum_bytes': 8388608},
 'news-475': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-direitoaterdireitos-impact-iii',
              'format': 'html',
              'sha256': '3cc9815b17d3a44f0df5112e8008a595b0a933b0d0ed04834b2657ad39941c45',
              'maximum_bytes': 8388608},
 'news-478': {'url': 'https://ipdj.gov.pt/-/candidaturas-abertas-para-terceira-edicao-do-selo-estudante-atleta-2025-2027-',
              'format': 'html',
              'sha256': 'd1609db9ebedd6f41e09f6b624f42b1b1fa14a632e42f15b03901cb2423aeb78',
              'maximum_bytes': 8388608},
 'news-479': {'url': 'https://ipdj.gov.pt/-/final-da-taca-de-portugal-masculina-futebol-2024-25-voluntariado-jovem-inscricoes-abertas',
              'format': 'html',
              'sha256': 'f4647cc46daad20634c6489bb7c60ac0dd365156584b40f509474d4af43d51a4',
              'maximum_bytes': 8388608},
 'news-480': {'url': 'https://ipdj.gov.pt/-/candidaturas-abertas-musica-ja-2025',
              'format': 'html',
              'sha256': '6276914b890ca728328be0a079f73ead9943df92296f293d3a7c1e37ba83f032',
              'maximum_bytes': 8388608},
 'news-482': {'url': 'https://ipdj.gov.pt/-/final-feminina-da-taca-de-portugal-futebol-voluntariado-jovem-inscricoes-abertas',
              'format': 'html',
              'sha256': 'eb8de3aa9c493b9755a076d93640930171450d7ca2fcc4efce46f838247120ba',
              'maximum_bytes': 8388608},
 'news-483': {'url': 'https://ipdj.gov.pt/-/webinar-a-integridade-no-desporto-na-cplp',
              'format': 'html',
              'sha256': '3d0aa0871239287d4d05f9bc83d07a05e62c0aa1ff3172cc3e2496485079a7d6',
              'maximum_bytes': 8388608},
 'news-484': {'url': 'https://ipdj.gov.pt/-/prazo-das-candidaturas-aos-premios-clube-top-25-foi-alargado-ate-30-de-maio',
              'format': 'html',
              'sha256': '0ecbfd04026de111589e607adae17306a8d9066af6fb883369211a6cc15ca3ed',
              'maximum_bytes': 8388608},
 'news-488': {'url': 'https://ipdj.gov.pt/-/programa-nacional-de-desporto-para-todos-abertura-de-candidaturas-para-apoio-a-projetos-na-area-da-deficiencia-2025',
              'format': 'html',
              'sha256': 'c1586a4f0f3976902ca3c11a74489a0e8f8d3bec41f261d5620304cf9899f2a8',
              'maximum_bytes': 8388608},
 'news-491': {'url': 'https://ipdj.gov.pt/-/medida-afirma-te-ja-prorroga-as-candidaturas-ate-2-de-maio',
              'format': 'html',
              'sha256': '1c1a51e5fa09b1a852f10afd28312a56d8469f9c9e9a168bbd2f4055de597036',
              'maximum_bytes': 8388608},
 'news-492': {'url': 'https://ipdj.gov.pt/-/segunda-edicao-premio-nacional-da-igualdade-de-genero-no-desporto-desporto-mais-igual-abertura-de-candidaturas',
              'format': 'html',
              'sha256': '06b6b677b8beb44f6a0984873f2cf7de507dbaf0f0a3ed85be0eef61a836559f',
              'maximum_bytes': 8388608},
 'news-497': {'url': 'https://ipdj.gov.pt/-/acao-de-formacao-jovens-e-seguranca-digital-ii',
              'format': 'html',
              'sha256': 'e4591037ac9c8b94003d75e0d619ad965da32c67dc514a9270140bd3dae4ccaa',
              'maximum_bytes': 8388608},
 'news-499': {'url': 'https://ipdj.gov.pt/-/geracao-z-abre-candidaturas-a-organizacao-de-atividades-de-voluntariado-ate-14-de-maio',
              'format': 'html',
              'sha256': 'd6e25f368d8d6c254441be3221750bfc191ec6cd810a682f41d2aedab9415d9f',
              'maximum_bytes': 8388608},
 'news-500': {'url': 'https://ipdj.gov.pt/-/bolsas-jovens-criadores-2025-nas-areas-da-musica-e-literatura-candidaturas-abertas',
              'format': 'html',
              'sha256': 'bd87481321e3906e4b4acb09329068b6350f1393023bf29939479a5fe3c8f7bb',
              'maximum_bytes': 8388608},
 'news-53': {'url': 'https://ipdj.gov.pt/-/inscricoes-abertas-para-voluntariado-jovem-no-festival-sol-da-caparica-2026',
             'format': 'html',
             'sha256': '3367864492f307599756a7395eab6f7f2ce1a77c817e9606133ef8f4aaf04ff2',
             'maximum_bytes': 8388608},
 'news-54': {'url': 'https://ipdj.gov.pt/-/vjnf-projetos-agroal-selvagem-2026-e-ourem-floresta-protegida',
             'format': 'html',
             'sha256': '2e81efa16768688b2af77b0165432ca238f89ee006e0063f3d57e465f7c2add2',
             'maximum_bytes': 8388608},
 'news-56': {'url': 'https://ipdj.gov.pt/-/programa-cuida-te-medida-2-intervencao-comunitaria-prorroga-as-candidaturas-2edicao',
             'format': 'html',
             'sha256': 'c94ba880d48758381dfe7acbbdb58c7edea2c363b95fab069e250dd635c0ed07',
             'maximum_bytes': 8388608},
 'news-59': {'url': 'https://ipdj.gov.pt/-/vjnf-projeto-calluna-restauro-ambiental-iii-associacao-vita-nativa',
             'format': 'html',
             'sha256': 'de61dac3a93ccaa04969871aaef354b61ac852adb1159e9e502ab4af9ade8f2f',
             'maximum_bytes': 8388608},
 'news-60': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-dia-de-portugal-cidade-do-futebol-inscricoes-abertas',
             'format': 'html',
             'sha256': '9d24b9863c6608e3dc3bc29a54c5c3e77bf62a26b3559833bf3b3269fead098d',
             'maximum_bytes': 8388608},
 'news-62': {'url': 'https://ipdj.gov.pt/-/mostra-nacional-jovens-criadores-2026-candidaturas-abertas',
             'format': 'html',
             'sha256': '657ff945bb43d98ea85f5591c0da96590fe8f400f9eae5921e0327136e302432',
             'maximum_bytes': 8388608},
 'news-63': {'url': 'https://ipdj.gov.pt/-/premios-boas-praticas-associativismo-jovem-2026-ja-tem-candidaturas-abertas',
             'format': 'html',
             'sha256': '127321c1e40eb4b99a079961b3dd915a393dfcc54a5daab41430a9adbb2a6496',
             'maximum_bytes': 8388608},
 'news-68': {'url': 'https://ipdj.gov.pt/-/canal-aberto-estreia-com-webinar-de-gabriel-guimarees',
             'format': 'html',
             'sha256': 'bcc87feb55e6c50d23ab92caefeb7afb3a8b79ffff3a94194d87d038a64cf2d0',
             'maximum_bytes': 8388608},
 'news-69': {'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-na-organizacao-do-futebol-profissional-portugues-inscricoes-abertas',
             'format': 'html',
             'sha256': 'a468d61e3b2ce6c33792ed49e13c02ae8792f30b961be44584e5bb4e4dbfcfa1',
             'maximum_bytes': 8388608},
 'news-70': {'url': 'https://ipdj.gov.pt/-/final-supertaca-candido-oliveira-2026-inscricoes-abertas-para-voluntariado-jovem',
             'format': 'html',
             'sha256': '309c49c383ef771ef49725a90bfaa676660c56536383ee8db132ff9d0bbcad3f',
             'maximum_bytes': 8388608},
 'news-73': {'url': 'https://ipdj.gov.pt/-/guardioes-do-tempo-com-inscricoes-abertas-para-voluntariado-jovem',
             'format': 'html',
             'sha256': 'd8c2a9c202a443d94578ee768443b6f51405874e87be638c7e3b06bc246badf6',
             'maximum_bytes': 8388608},
 'news-77': {'url': 'https://ipdj.gov.pt/-/envolve-te-ambiente-guardioes-da-floresta-e-da-natureza-mobiliza-jovens-em-castelo-branco',
             'format': 'html',
             'sha256': '70128276b0786bfd7a50359f7364c56ddb012c98b3c3799b1e5a2ce5e5bdb0cb',
             'maximum_bytes': 8388608},
 'news-79': {'url': 'https://ipdj.gov.pt/-/programa-de-incentivo-ao-desenvolvimento-associativo-ida-apoia-associacoes-juvenis-candidaturas-abertas-2026',
             'format': 'html',
             'sha256': '7e597a9ffaffa9642757e598b6939c7c58ac1037e781efa369f981497f20d607',
             'maximum_bytes': 8388608},
 'news-82': {'url': 'https://ipdj.gov.pt/-/vjnf-projeto-portugal-sem-incendios-e-limpeza-contra-incendios',
             'format': 'html',
             'sha256': 'f520dcbf82036a571a52c6a0ec2a996c973e6735a4539473ec0721bbc8c2788e',
             'maximum_bytes': 8388608},
 'news-84': {'url': 'https://ipdj.gov.pt/-/premio-de-investigacao-sobre-etica-no-desporto-edicao-especial-cartao-branco-candidaturas-abertas',
             'format': 'html',
             'sha256': 'c9eed4e16a5bd76a05ac73be95df671a8e2f6448253daee12403c19845db606b',
             'maximum_bytes': 8388608},
 'news-90': {'url': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-ferias-cinco-pont-zero-inscricoes-jovens-a-decorrer',
             'format': 'html',
             'sha256': '79a49a7936c507bd5feb7c7a3b0bcfac6097e7515819fb147aa3b351bbb4bae1',
             'maximum_bytes': 8388608},
 'news-92': {'url': 'https://ipdj.gov.pt/-/workshop-sobre-cidadania-e-voluntariado-no-ipdj-de-viseu',
             'format': 'html',
             'sha256': 'f19541bbac58790eeff7c40d5ee8925002396504fb95a8377a15fb1015351137',
             'maximum_bytes': 8388608},
 'news-93': {'url': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-oficina-cultural-com-candidaturas-abertas-para-jovens',
             'format': 'html',
             'sha256': '3371c3c272cf4d48d09b837e882309c70efe83bd6f867f4c0b39da13e360a372',
             'maximum_bytes': 8388608},
 'news-95': {'url': 'https://ipdj.gov.pt/-/projeto-pinhas-com-proposito-jovens-pela-floresta-e-pela-comunidade-mobiliza-jovens-pelo-ambiente-e-florestas-em-castelo-branco',
             'format': 'html',
             'sha256': 'fa383f98b5441f157c8003eb40a16518306b8250a95efae36de1ce3d0ea3eff2',
             'maximum_bytes': 8388608},
 'news-96': {'url': 'https://ipdj.gov.pt/-/programa-cuida-te-medida-intervencao-comunitaria-abre-candidaturas',
             'format': 'html',
             'sha256': '4fbd60719a1e32b0375dee1d579d1ab4520849f77bc497930ebe65db7f339fbd',
             'maximum_bytes': 8388608},
 'news-material-137': {'url': 'https://ipdj.gov.pt/documents/20123/36911653/Programa+Seminario+lideranc%CC%A7a+-+entrega+de+premios+Desporto+%281%29.pdf/62295948-263c-90c3-a192-f17af623eb94?t=1778753127823',
                       'format': 'pdf',
                       'sha256': '4561d6325b788b1cab49b224d5d07b7f6516864e30f28a2489c5370172946302',
                       'maximum_bytes': 8388608},
 'pndpt-ies-general-rules': {'url': 'https://ipdj.gov.pt/documents/20123/159220/2023.05.09_Regulamento_geral_PNDpT_IES_assSV+%281%29.pdf/7b7f8292-d86f-bc0f-266e-bb1e1ee9d106?t=1683744652959',
                             'format': 'pdf',
                             'sha256': 'b0d9d626650d8c31f6121518f6f7e1b9d48d15d080eecfb97f613fd1df147191',
                             'maximum_bytes': 8388608},
 'privacy': {'url': 'https://ipdj.gov.pt/politica-privacidade',
             'format': 'html',
             'sha256': 'ffae53a5472c19c828304c0df85f95e0f767b3099bd87541e1426148ae9fde6e',
             'maximum_bytes': 8388608},
 'programme-1': {'url': 'https://ipdj.gov.pt/agora-n%C3%B3s',
                 'format': 'html',
                 'sha256': 'd3f39543b5c1af7d527125c3fed5a57d9aec936bbd9f837b20a7af4705ead4eb',
                 'maximum_bytes': 8388608},
 'programme-10': {'url': 'https://ipdj.gov.pt/pai-programa-de-apoio-infraestrutural',
                  'format': 'html',
                  'sha256': 'b939ac1086c89868cee7a358bce2bd433b526d46b0211330d603fc34c801021d',
                  'maximum_bytes': 8388608},
 'programme-11': {'url': 'https://ipdj.gov.pt/paj-programa-de-apoio-juvenil',
                  'format': 'html',
                  'sha256': '2572356a16175b7475e848fa38e38b97e2a7c8ab0a82b54c0740d137d8435e56',
                  'maximum_bytes': 8388608},
 'programme-12': {'url': 'https://ipdj.gov.pt/paacj-programa-de-apoio-as-associacoes-de-carater-juvenil',
                  'format': 'html',
                  'sha256': 'f7ce938d21d65beb913a767b8fef32a0b720e459130e522a0bb13a05e04c4969',
                  'maximum_bytes': 8388608},
 'programme-13': {'url': 'https://ipdj.gov.pt/programas-de-incentivo-ao-desenvolvimento-associativo-ida',
                  'format': 'html',
                  'sha256': '24a8becbf596ff75ae61e352b6d41a7e940a1f4eb382066743d17f945b4c4f2e',
                  'maximum_bytes': 8388608},
 'programme-14': {'url': 'https://ipdj.gov.pt/programa-de-reabilitacao-de-instalacoes-desportivas-prid',
                  'format': 'html',
                  'sha256': 'cd09ae90cc87bd72199a3c7cea7d39dbc1af03651ac3ee26af97cf29419bea28',
                  'maximum_bytes': 8388608},
 'programme-2': {'url': 'https://ipdj.gov.pt/programa-arribar',
                 'format': 'html',
                 'sha256': 'a5f6ce8dedd3202b8f504098f542ef123477336fd5a41045e77b36731c24d883',
                 'maximum_bytes': 8388608},
 'programme-3': {'url': 'https://ipdj.gov.pt/programa-associa-te',
                 'format': 'html',
                 'sha256': '216cc99411f4e0342a2ec484e1318b6026b14fe9cd287faa9c6475f9d23ca272',
                 'maximum_bytes': 8388608},
 'programme-4': {'url': 'https://ipdj.gov.pt/o-programa',
                 'format': 'html',
                 'sha256': '2bef1f71acc0c28e6c61b834fc744cac7960b802c966e1dc8d1254fa079d24f2',
                 'maximum_bytes': 8388608},
 'programme-5': {'url': 'https://ipdj.gov.pt/programa-escolhas',
                 'format': 'html',
                 'sha256': '265a4d17848e1f5d2845e8bfc689443ca78d3fe0fd95b268a21afa6aa07a2a46',
                 'maximum_bytes': 8388608},
 'programme-6': {'url': 'https://ipdj.gov.pt/programa-jovens-criadores',
                 'format': 'html',
                 'sha256': '9816270e3458eee79ac3bbd491a31a8919b8fccd839a345dcb1aba2f77d1421b',
                 'maximum_bytes': 8388608},
 'programme-7': {'url': 'https://ipdj.gov.pt/programa-nacional-de-desporto-para-todos',
                 'format': 'html',
                 'sha256': 'e35d345d43e793df5a97720009f47768ca3b3f9f0b97ee3133d846dd1ed8422c',
                 'maximum_bytes': 8388608},
 'programme-8': {'url': 'https://ipdj.gov.pt/programa-nacional-de-formacao-de-treinadores',
                 'format': 'html',
                 'sha256': '04157f2d7b42603511e9c89a4f8d9eb105e7184a30690ab7b8341d2bab6215b9',
                 'maximum_bytes': 8388608},
 'programme-9': {'url': 'https://ipdj.gov.pt/pae-programa-de-apoio-estudantil',
                 'format': 'html',
                 'sha256': '5be52967b59b5884650a2f4399d589730cb45f9f2cb6f3f07c70b8440c89c08d',
                 'maximum_bytes': 8388608},
 'programmes': {'url': 'https://ipdj.gov.pt/programas',
                'format': 'html',
                'sha256': 'cfcfd43e5bf8c0daca70cc80cd4e6f6e8c8699f290ace94618aeb01e74972013',
                'maximum_bytes': 8388608},
 'site-map': {'url': 'https://ipdj.gov.pt/mapa-site',
              'format': 'html',
              'sha256': '6e14174dcb8610e8859586337cff985b34d9ba2d981c5fbc3c8f32d53bbfbfc1',
              'maximum_bytes': 8388608},
 'sitemap': {'url': 'https://ipdj.gov.pt/sitemap.xml',
             'format': 'xml',
             'sha256': '428e80b63a3db887f0cbadbe8f2b5065ca8da488e7431056c32da50d5dfe616e',
             'maximum_bytes': 8388608}}

PROFILES = [{'key': 'https://ipdj.gov.pt/-/4-encontro-odec-educacao-direitos-humanos-e-desporto',
  'title': '4.º Encontro ODEC Educação, Direitos Humanos e Desporto',
  'url': 'https://ipdj.gov.pt/-/4-encontro-odec-educacao-direitos-humanos-e-desporto',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Free fourth ODEC meeting on human rights, education and sport, May 21, '
             '2026 from 09:00 at FPCEUP in Porto. The programme addresses '
             'participation, accessibility, inclusion and differences between CPLP and '
             'European contexts. Registration is compulsory; its closing is not '
             'stated. No personal cash award is offered.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/4-encontro-odec-educacao-direitos-humanos-e-desporto'},
 {'key': 'https://ipdj.gov.pt/-/abertas-ate-dia-31-de-julho-as-candidaturas-a-mostra-nacional-de-jovens-criadores',
  'title': 'Abertas, até dia 31 de julho, as candidaturas à Mostra Nacional de Jovens '
           'Criadores',
  'url': 'https://ipdj.gov.pt/-/abertas-ate-dia-31-de-julho-as-candidaturas-a-mostra-nacional-de-jovens-criadores',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': '20:00',
  'summary': '2025 national Jovens Criadores artistic competition, subject to the '
             "edition's applicant-age, team and artistic-area rules. Winning proposals "
             'receive EUR 1,000 in each area; capacity and selection do not imply a '
             'prize for every entrant. Applications close July 31, 2025 at 20:00, '
             "without a stated time zone. This is distinct from CNC's Music and "
             'Literature scholarships.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/abertas-ate-dia-31-de-julho-as-candidaturas-a-mostra-nacional-de-jovens-criadores'},
 {'key': 'https://ipdj.gov.pt/-/aberto-procedimento-para-selecao-de-entidade-organizadora-da-edicao-2026-do-concurso-e-mostra-nacional-jovens-criadores',
  'title': 'Aberto o procedimento para seleção de entidade organizadora da edição 2026 '
           'do Concurso e Mostra Nacional «Jovens Criadores»',
  'url': 'https://ipdj.gov.pt/-/aberto-procedimento-para-selecao-de-entidade-organizadora-da-edicao-2026-do-concurso-e-mostra-nacional-jovens-criadores',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 selection of a nonprofit youth or arts organisation to co-organise '
             'the national Jovens Criadores competition and exhibition. Applicants '
             'must work in culture, arts and youth and deliver the prescribed artistic '
             'areas. The application rule is twenty working days from publication of '
             'the notice, cited March 17, 2026; an absolute closing is not computed '
             'without complete procedural counting rules. Amount is unstated.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/aberto-procedimento-para-selecao-de-entidade-organizadora-da-edicao-2026-do-concurso-e-mostra-nacional-jovens-criadores'},
 {'key': 'https://ipdj.gov.pt/-/abertura-de-concurso-para-diretor-geral-na-comunidade-dos-paises-de-lingua-portuguesa',
  'title': 'Abertura de Concurso para Diretor Geral na Comunidade dos Países de Língua '
           'Portuguesa',
  'url': 'https://ipdj.gov.pt/-/abertura-de-concurso-para-diretor-geral-na-comunidade-dos-paises-de-lingua-portuguesa',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-12-09',
  'closing_local_clock': None,
  'summary': 'CPLP Director General vacancy in Lisbon for nationals of CPLP member '
             'states. Applicants need a degree, excellent Portuguese and a foreign '
             "language, at least ten years' experience and seven years in leadership, "
             'plus the published professional requirements. The mandate is three '
             'years, renewable once. Applications close December 9, 2025. Pay is not '
             'stated in the own notice.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/abertura-de-concurso-para-diretor-geral-na-comunidade-dos-paises-de-lingua-portuguesa'},
 {'key': 'https://ipdj.gov.pt/-/academias-de-desenvolvimento-juvenil-2025-cinco-sessoes-regionais-online-ate-ao-youth-summit::alentejo',
  'title': 'Academias de Desenvolvimento Juvenil 2025: cinco sessões regionais online '
           'até ao Youth Summit',
  'url': 'https://ipdj.gov.pt/-/academias-de-desenvolvimento-juvenil-2025-cinco-sessoes-regionais-online-ate-ao-youth-summit',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online ADJ session on youth participation, September 5 at '
             '17:00, 2025. Any young person can join regardless of residence region; '
             'separate registration is required and access is sent afterward. '
             'Facilitator Joaquim Serafim. Fees, registration closing and current '
             'enrolment are unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/academias-de-desenvolvimento-juvenil-2025-cinco-sessoes-regionais-online-ate-ao-youth-summit'},
 {'key': 'https://ipdj.gov.pt/-/academias-de-desenvolvimento-juvenil-2025-cinco-sessoes-regionais-online-ate-ao-youth-summit::algarve',
  'title': 'Academias de Desenvolvimento Juvenil 2025: cinco sessões regionais online '
           'até ao Youth Summit',
  'url': 'https://ipdj.gov.pt/-/academias-de-desenvolvimento-juvenil-2025-cinco-sessoes-regionais-online-ate-ao-youth-summit',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online ADJ session on responsible technology use, September '
             '10 at 15:00, 2025. Any young person can join regardless of residence '
             'region; separate registration is required and access is sent afterward. '
             'Facilitator Edmauro. Fees, registration closing and current enrolment '
             'are unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/academias-de-desenvolvimento-juvenil-2025-cinco-sessoes-regionais-online-ate-ao-youth-summit'},
 {'key': 'https://ipdj.gov.pt/-/academias-de-desenvolvimento-juvenil-2025-cinco-sessoes-regionais-online-ate-ao-youth-summit::centro',
  'title': 'Academias de Desenvolvimento Juvenil 2025: cinco sessões regionais online '
           'até ao Youth Summit',
  'url': 'https://ipdj.gov.pt/-/academias-de-desenvolvimento-juvenil-2025-cinco-sessoes-regionais-online-ate-ao-youth-summit',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online ADJ session on financial literacy, September 10 at '
             '10:30, 2025. Any young person can join regardless of residence region; '
             'separate registration is required and access is sent afterward.  Fees, '
             'registration closing and current enrolment are unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/academias-de-desenvolvimento-juvenil-2025-cinco-sessoes-regionais-online-ate-ao-youth-summit'},
 {'key': 'https://ipdj.gov.pt/-/academias-de-desenvolvimento-juvenil-2025-cinco-sessoes-regionais-online-ate-ao-youth-summit::lvt',
  'title': 'Academias de Desenvolvimento Juvenil 2025: cinco sessões regionais online '
           'até ao Youth Summit',
  'url': 'https://ipdj.gov.pt/-/academias-de-desenvolvimento-juvenil-2025-cinco-sessoes-regionais-online-ate-ao-youth-summit',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online ADJ session on managing a project or association, '
             'September 9 at 14:00, 2025. Any young person can join regardless of '
             'residence region; separate registration is required and access is sent '
             'afterward. Facilitator Cristina Gaspar. Fees, registration closing and '
             'current enrolment are unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/academias-de-desenvolvimento-juvenil-2025-cinco-sessoes-regionais-online-ate-ao-youth-summit'},
 {'key': 'https://ipdj.gov.pt/-/academias-de-desenvolvimento-juvenil-2025-cinco-sessoes-regionais-online-ate-ao-youth-summit::norte',
  'title': 'Academias de Desenvolvimento Juvenil 2025: cinco sessões regionais online '
           'até ao Youth Summit',
  'url': 'https://ipdj.gov.pt/-/academias-de-desenvolvimento-juvenil-2025-cinco-sessoes-regionais-online-ate-ao-youth-summit',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online ADJ session on youth participation, September 10 at '
             '11:00, 2025. Any young person can join regardless of residence region; '
             'separate registration is required and access is sent afterward. '
             'Facilitator Joana Moreira. Fees, registration closing and current '
             'enrolment are unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/academias-de-desenvolvimento-juvenil-2025-cinco-sessoes-regionais-online-ate-ao-youth-summit'},
 {'key': 'https://ipdj.gov.pt/-/acao-de-formacao-jovens-e-seguranca-digital-ii',
  'title': 'Ação de formação «Os/As Jovens e a Segurança Digital» - II',
  'url': 'https://ipdj.gov.pt/-/acao-de-formacao-jovens-e-seguranca-digital-ii',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-04-18',
  'closing_local_clock': None,
  'summary': 'Free 10-hour 2025 digital-safety course for Navegas youth volunteers, '
             'technical/coordinating staff and youth workers. Learn online risks, '
             'rights and intervention tools. Sessions April 21, 28 and 29; '
             'registration closes April 18, 2025. At least 90% completion is required '
             'for the IPDJ participation certificate. No personal grant stated.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/acao-de-formacao-jovens-e-seguranca-digital-ii'},
 {'key': 'https://ipdj.gov.pt/-/acao-de-formacao-os/as-jovens-e-a-seguranca-digital-ii',
  'title': 'Ação de formação «Os/As Jovens e a Segurança Digital» II',
  'url': 'https://ipdj.gov.pt/-/acao-de-formacao-os/as-jovens-e-a-seguranca-digital-ii',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-11-14',
  'closing_local_clock': None,
  'summary': 'Ten-hour digital-safety training, November 17–25, 2025, for youth '
             'volunteers (priority), Navegas technical/coordinating staff and youth '
             'workers. Four after-work sessions, 17–19:30. At least 90% completion '
             'required for certificate. Closing November 14, 2025. The title says '
             'edition II while the body says III; one course with conflicting edition '
             'labels.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/acao-de-formacao-os/as-jovens-e-a-seguranca-digital-ii'},
 {'key': 'https://ipdj.gov.pt/-/bolsas-jovens-criadores-2025-nas-areas-da-musica-e-literatura-candidaturas-abertas',
  'title': '«Bolsas Jovens Criadores» 2025 nas áreas da Música e Literatura',
  'url': 'https://ipdj.gov.pt/-/bolsas-jovens-criadores-2025-nas-areas-da-musica-e-literatura-candidaturas-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-05-15',
  'closing_local_clock': None,
  'summary': 'CNC Music and Literature scholarships for creators aged at most 30 '
             'residing in Portugal who have already begun their artistic practice. Up '
             'to four selected awards are worth at most EUR 3,000 each, subject to the '
             'artistic project and selection conditions. Applications close May 15, '
             '2025. CNC administers the scheme with IPDJ support; the partner budget '
             'is not an individual entitlement.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/bolsas-jovens-criadores-2025-nas-areas-da-musica-e-literatura-candidaturas-abertas'},
 {'key': 'https://ipdj.gov.pt/-/braga-recebe-o-evento-juventude-em-movimento-portugal-e-marrocos-juntos-pelo-dialogo-intercultural-voluntariado-e-participacao-civica',
  'title': 'Braga recebe o evento «Juventude em Movimento: Portugal e Marrocos juntos '
           'pelo diálogo intercultural, voluntariado e participação cívica»',
  'url': 'https://ipdj.gov.pt/-/braga-recebe-o-evento-juventude-em-movimento-portugal-e-marrocos-juntos-pelo-dialogo-intercultural-voluntariado-e-participacao-civica',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical Youth in Motion educational programme, March 29–April 3, '
             '2026, Braga, bringing young people from Portugal and Morocco together '
             'for intercultural dialogue, volunteering and civic participation. Six '
             'days of workshops, cultural visits, debates and team-building develop '
             'youth-work skills and cooperation. The own announcement states no '
             'admissions route, application closing or participant cost support; '
             'current entry is unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/braga-recebe-o-evento-juventude-em-movimento-portugal-e-marrocos-juntos-pelo-dialogo-intercultural-voluntariado-e-participacao-civica'},
 {'key': 'https://ipdj.gov.pt/-/campos-de-trabalho-internacionais-2026-com-candidaturas-abertas-para-entidades',
  'title': 'Campos de Trabalho Internacionais com candidaturas abertas para entidades',
  'url': 'https://ipdj.gov.pt/-/campos-de-trabalho-internacionais-2026-com-candidaturas-abertas-para-entidades',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-01-09',
  'closing_local_clock': None,
  'summary': '2026 international-workcamp promoter call for eligible nonprofit '
             'organisers. Awards contribute to camp accommodation, meals, insurance '
             'and local activity transport; they are whole organiser support, not '
             "participant pocket money. The edition's extended entity window closes "
             'January 9, 2026. Participant registrations and their 22 named camp '
             'activity periods are separate.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/campos-de-trabalho-internacionais-2026-com-candidaturas-abertas-para-entidades'},
 {'key': 'https://ipdj.gov.pt/-/canal-aberto-estreia-com-webinar-de-gabriel-guimarees',
  'title': 'Canal Aberto do IPDJ estreia com webinar de Gabriel Guimarães focado em '
           'combater a desinformação',
  'url': 'https://ipdj.gov.pt/-/canal-aberto-estreia-com-webinar-de-gabriel-guimarees',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Free online media-literacy webinar, July 20, 2026 at 17:00, with Gabriel '
             'Guimarães. Young people can learn to verify information and recognise '
             'misinformation in the Canal Aberto series. Prior registration is '
             'required and participation is certified. Other planned sessions are not '
             'separate announced opportunities. Registration closing is not stated.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/canal-aberto-estreia-com-webinar-de-gabriel-guimarees'},
 {'key': 'https://ipdj.gov.pt/-/candidaturas-abertas-aos-premios-boas-praticas-associativismo-jovem-2025::higher-education-student-associations',
  'title': 'Candidaturas abertas aos Prémios «Boas Práticas Associativismo Jovem» 2025',
  'url': 'https://ipdj.gov.pt/-/candidaturas-abertas-aos-premios-boas-praticas-associativismo-jovem-2025',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-11-09',
  'closing_local_clock': None,
  'summary': 'Historical 2025 good-practice award for effective RNAJ student '
             'associations and federations, recognising projects delivered in 2024. '
             'Required project/supporting documents must accompany the application. '
             'Each regional contest awards one organisation EUR 1,500 and up to five '
             'EUR 350 mentions. Closing November 9, 2025; institutional award, not '
             'personal funding.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/candidaturas-abertas-aos-premios-boas-praticas-associativismo-jovem-2025'},
 {'key': 'https://ipdj.gov.pt/-/candidaturas-abertas-aos-premios-boas-praticas-associativismo-jovem-2025::youth-associations',
  'title': 'Candidaturas abertas aos Prémios «Boas Práticas Associativismo Jovem» 2025',
  'url': 'https://ipdj.gov.pt/-/candidaturas-abertas-aos-premios-boas-praticas-associativismo-jovem-2025',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-11-09',
  'closing_local_clock': None,
  'summary': 'Historical 2025 good-practice award for effective RNAJ youth '
             'associations and federations, recognising projects delivered in 2024. '
             'Required project/supporting documents must accompany the application. '
             'Each regional contest awards one organisation EUR 1,500 and up to five '
             'EUR 350 mentions. Closing November 9, 2025; institutional award, not '
             'personal funding.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/candidaturas-abertas-aos-premios-boas-praticas-associativismo-jovem-2025'},
 {'key': 'https://ipdj.gov.pt/-/candidaturas-abertas-musica-ja-2025',
  'title': 'Candidaturas abertas para a 6.ª edição da MOSTRA DE MÚSICA MODERNA - '
           '“PALCO RUA – MÚSICA JA”',
  'url': 'https://ipdj.gov.pt/-/candidaturas-abertas-musica-ja-2025',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-05-30',
  'closing_local_clock': None,
  'summary': '2025 Palco Rua–Música JA competition for musicians aged 14–35 who are '
             'native to or resident in Algarve. Selected performances are valued at '
             'EUR 1,500, 1,000, 500 or 300 under the competition conditions, not '
             'unconditional cash for entrants. Applications close May 30, 2025. A '
             'chrome-extension-wrapped external link was not used as a replacement '
             'URL.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/candidaturas-abertas-musica-ja-2025'},
 {'key': 'https://ipdj.gov.pt/-/candidaturas-abertas-para-a-xiv-edicao-do-premio-de-imprensa-desporto-com-etica',
  'title': 'Candidaturas Abertas para a XIV Edição do Prémio de Imprensa «Desporto com '
           'Ética»',
  'url': 'https://ipdj.gov.pt/-/candidaturas-abertas-para-a-xiv-edicao-do-premio-de-imprensa-desporto-com-etica',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-01-31',
  'closing_local_clock': None,
  'summary': 'Fourteenth Desporto com Ética journalism prize for authors with or '
             'without professional accreditation. Eligible ethics-in-sport work must '
             'have appeared in 2025 in qualifying Portuguese press, radio or TV, '
             'including registered island and emigrant-community outlets. Regional and '
             'sport/general-media segments apply. Closing January 31, 2026. Amount and '
             'additional delegated terms remain unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/candidaturas-abertas-para-a-xiv-edicao-do-premio-de-imprensa-desporto-com-etica'},
 {'key': 'https://ipdj.gov.pt/-/candidaturas-abertas-para-voluntariado-jovem-na-futuralia-2026',
  'title': 'Candidaturas abertas para voluntariado Jovem na Futurália 2026',
  'url': 'https://ipdj.gov.pt/-/candidaturas-abertas-para-voluntariado-jovem-na-futuralia-2026',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Volunteer at IPDJ’s Futurália stand, March 11–14, 2026, FIL, Lisbon, '
             'helping inform young visitors about education, training and employment. '
             'Volunteering preparation, accident/liability insurance and expense '
             'reimbursement are offered; amount and age eligibility are not stated in '
             'this own notice. Registration closing unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/candidaturas-abertas-para-voluntariado-jovem-na-futuralia-2026'},
 {'key': 'https://ipdj.gov.pt/-/candidaturas-capital-nacional-de-juventude-2026-abertas',
  'title': 'Candidaturas à Capital Nacional de Juventude 2026 abertas de 8 de dezembro '
           'a 18 de janeiro',
  'url': 'https://ipdj.gov.pt/-/candidaturas-capital-nacional-de-juventude-2026-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-01-18',
  'closing_local_clock': None,
  'summary': 'Capital Nacional de Juventude 2026 institutional competition for '
             'Portuguese municipalities with an active Municipal Youth Council. The '
             'winning municipal plan receives EUR 50,000 for youth-policy activities, '
             'not a personal scholarship. Applicants must supply the required plan and '
             'budget. Closing January 18, 2026; this is a distinct edition from the '
             'announced 2027 call.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/candidaturas-capital-nacional-de-juventude-2026-abertas'},
 {'key': 'https://ipdj.gov.pt/-/capital-nacional-juventude-candidaturas-abertas',
  'title': '«Capital Nacional de Juventude 2027»: candidaturas abertas até 21 de '
           'outubro',
  'url': 'https://ipdj.gov.pt/-/capital-nacional-juventude-candidaturas-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-10-21',
  'closing_local_clock': None,
  'summary': 'Portuguese municipalities with an active Municipal Youth Council can '
             'propose a 2027 youth-policy activity plan. The winning municipality '
             'receives EUR 50,000 for the approved activities and agreed IPDJ '
             'initiatives, not a personal award. The application includes an activity '
             'plan and financial planning. Applications close October 21, 2026.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/capital-nacional-juventude-candidaturas-abertas'},
 {'key': 'https://ipdj.gov.pt/-/cig-e-ipdj-realizam-acoes-de-capacitaao-sobre-igualdade-de-genero-violencia-de-genero-e-nao-discriminacao::enraizar-mudanca',
  'title': 'CIG e IPDJ realizam ações de capacitação sobre Igualdade de Género, '
           'Violência de Género e Não-Discriminação',
  'url': 'https://ipdj.gov.pt/-/cig-e-ipdj-realizam-acoes-de-capacitaao-sobre-igualdade-de-genero-violencia-de-genero-e-nao-discriminacao',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-10-20',
  'closing_local_clock': None,
  'summary': 'Free online CIG/IPDJ module on preventing dating violence and gender '
             'violence, October 22, 2025,09:30–13:00. Young people, particularly '
             'association members, and professionals working with youth can register '
             'separately. Closing October 20, 2025. This is a historical educational '
             'session; no personal grant is stated.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/cig-e-ipdj-realizam-acoes-de-capacitaao-sobre-igualdade-de-genero-violencia-de-genero-e-nao-discriminacao'},
 {'key': 'https://ipdj.gov.pt/-/cig-e-ipdj-realizam-acoes-de-capacitaao-sobre-igualdade-de-genero-violencia-de-genero-e-nao-discriminacao::fgm',
  'title': 'CIG e IPDJ realizam ações de capacitação sobre Igualdade de Género, '
           'Violência de Género e Não-Discriminação',
  'url': 'https://ipdj.gov.pt/-/cig-e-ipdj-realizam-acoes-de-capacitaao-sobre-igualdade-de-genero-violencia-de-genero-e-nao-discriminacao',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-10-20',
  'closing_local_clock': None,
  'summary': 'Free online CIG/IPDJ module on preventing female genital mutilation, '
             'November 4, 2025,09:30–12:30. Young people, particularly association '
             'members, and professionals working with youth can register separately. '
             'Closing October 20, 2025. Historical educational session; no personal '
             'grant stated.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/cig-e-ipdj-realizam-acoes-de-capacitaao-sobre-igualdade-de-genero-violencia-de-genero-e-nao-discriminacao'},
 {'key': 'https://ipdj.gov.pt/-/cig-e-ipdj-realizam-acoes-de-capacitaao-sobre-igualdade-de-genero-violencia-de-genero-e-nao-discriminacao::oiec',
  'title': 'CIG e IPDJ realizam ações de capacitação sobre Igualdade de Género, '
           'Violência de Género e Não-Discriminação',
  'url': 'https://ipdj.gov.pt/-/cig-e-ipdj-realizam-acoes-de-capacitaao-sobre-igualdade-de-genero-violencia-de-genero-e-nao-discriminacao',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-10-20',
  'closing_local_clock': None,
  'summary': 'Free online CIG/IPDJ module on sexual orientation, gender '
             'identity/expression and sex characteristics, and LGBTI '
             'equality/nondiscrimination, October 28, 2025,09:30–12:30. Young people, '
             'particularly association members, and youth professionals can register '
             'separately. Closing October 20, 2025; no personal grant stated.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/cig-e-ipdj-realizam-acoes-de-capacitaao-sobre-igualdade-de-genero-violencia-de-genero-e-nao-discriminacao'},
 {'key': 'https://ipdj.gov.pt/-/concurso-euroscola-com-candidaturas-abertas',
  'title': '«Concurso Euroscola» com candidaturas abertas',
  'url': 'https://ipdj.gov.pt/-/concurso-euroscola-com-candidaturas-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['FR'],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-03-08',
  'closing_local_clock': None,
  'summary': 'Euroscola 2026 selects Portuguese secondary schools for European '
             'Parliament sessions in Strasbourg. Each eligible school submits written '
             'work and an oral presentation by two year-10 or year-11 pupils on '
             'fintech, cryptocurrency and youth security. Applications were extended '
             'to March 8, 2026. Selected educational participation is not an '
             'unconditional cash award; travel funding is not stated here.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/concurso-euroscola-com-candidaturas-abertas'},
 {'key': 'https://ipdj.gov.pt/-/concurso-literario-a-etica-na-vida-e-no-desporto-candidaturas-abertas-ate-28-de-fevereiro',
  'title': 'Concurso literário «A Ética na Vida e no Desporto»: candidaturas abertas '
           'até 28 de fevereiro',
  'url': 'https://ipdj.gov.pt/-/concurso-literario-a-etica-na-vida-e-no-desporto-candidaturas-abertas-ate-28-de-fevereiro',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-02-28',
  'closing_local_clock': None,
  'summary': 'Fourteenth ethics-in-sport literary contest for secondary or vocational '
             'students in qualifying schools, educational centres and prisons across '
             'mainland Portugal, Azores and Madeira. Regional winners may advance to '
             'the national phase, up to six texts per region. Applications close '
             'February 28, 2026. Prize amounts and additional delegated terms are '
             'unknown; this is not an automatic pupil stipend.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/concurso-literario-a-etica-na-vida-e-no-desporto-candidaturas-abertas-ate-28-de-fevereiro'},
 {'key': 'https://ipdj.gov.pt/-/curso-de-formacao-continua-construcao-de-ambientes-pedagogicos-positivos-para-criancas-e-jovens-no-desporto',
  'title': 'Curso de Formação Contínua «Construção de ambientes pedagógicos positivos '
           'para crianças e jovens no desporto»',
  'url': 'https://ipdj.gov.pt/-/curso-de-formacao-continua-construcao-de-ambientes-pedagogicos-positivos-para-criancas-e-jovens-no-desporto',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Continuing-education course on creating positive pedagogical '
             'environments for children and young people in sport, offered through NAU '
             'in 2025. The course develops the announced educational competences '
             'rather than offering a trainer job. Access, certification and any credit '
             'depend on the stated course conditions. Registration closing and '
             'personal financial support are not established.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/curso-de-formacao-continua-construcao-de-ambientes-pedagogicos-positivos-para-criancas-e-jovens-no-desporto'},
 {'key': 'https://ipdj.gov.pt/-/curso-protecao-contra-a-violencia-e-abuso-no-desporto-aprendizagens-essenciais-para-pontos-focais-candidaturas-abertas',
  'title': 'Curso «Proteção contra a Violência e Abuso no Desporto, Aprendizagens '
           'Essenciais para Pontos Focais»: candidaturas abertas',
  'url': 'https://ipdj.gov.pt/-/curso-protecao-contra-a-violencia-e-abuso-no-desporto-aprendizagens-essenciais-para-pontos-focais-candidaturas-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-06-10',
  'closing_local_clock': None,
  'summary': 'COP’s 20-hour safeguarding course for current or prospective sport focal '
             'points, June–July 2026. Applicants must first complete the free NAU '
             'safeguarding MOOC and attend all phases: online opening, self-study and '
             'residential bootcamp with food and accommodation. Maximum twenty places; '
             '1.6 trainer credits. Applications close June 10, 2026. Programme fees '
             'are not stated.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/curso-protecao-contra-a-violencia-e-abuso-no-desporto-aprendizagens-essenciais-para-pontos-focais-candidaturas-abertas'},
 {'key': 'https://ipdj.gov.pt/-/dia-da-internet-mais-segura-2026-inscricoes-abertas',
  'title': 'Centro Internet Segura assinala Dia da Internet mais Segura, 10 de '
           'fevereiro: inscrições a decorrer',
  'url': 'https://ipdj.gov.pt/-/dia-da-internet-mais-segura-2026-inscricoes-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 Safer Internet Day learning event on February 10. The notice '
             'invites participants to the concrete programme on digital safety; '
             'registration is required where stated. Fees, personal funding and '
             'registration closing are not established beyond the announcement. The '
             'event date is not a closing date.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/dia-da-internet-mais-segura-2026-inscricoes-abertas'},
 {'key': 'https://ipdj.gov.pt/-/dia-nacional-do-estudante-assinalado-com-programa-nacional-de-promocao-da-participacao-e-do-associativismo-estudantil',
  'title': 'Dia Nacional do Estudante assinalado com programa nacional de promoção da '
           'participação e do associativismo estudantil',
  'url': 'https://ipdj.gov.pt/-/dia-nacional-do-estudante-assinalado-com-programa-nacional-de-promocao-da-participacao-e-do-associativismo-estudantil',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Free remote 2026 capacity-building workshops for basic and secondary '
             'students on creating, organising and running student associations. '
             'Regional IPDJ teams deliver the national programme between March 16 and '
             'April 2. Prior registration is required. These are educational '
             'activities, not cash awards. Registration closing is unstated; the '
             'activity window is not a deadline.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/dia-nacional-do-estudante-assinalado-com-programa-nacional-de-promocao-da-participacao-e-do-associativismo-estudantil'},
 {'key': 'https://ipdj.gov.pt/-/edicao-2025-2026-do-parlamento-dos-jovens-abre-inscricoes-para-escolas-de-1-de-setembro-a-15-de-outubro',
  'title': 'A edição 2025/2026 do Parlamento dos Jovens abre inscrições para escolas '
           'de 1 de setembro a 15 de outubro',
  'url': 'https://ipdj.gov.pt/-/edicao-2025-2026-do-parlamento-dos-jovens-abre-inscricoes-para-escolas-de-1-de-setembro-a-15-de-outubro',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-10-15',
  'closing_local_clock': None,
  'summary': 'Parlamento dos Jovens 2025/26 civic-education programme on financial '
             'literacy. Students in the second and third basic-education cycles and '
             'secondary education participate through registered schools, preparing '
             'parliamentary debates and national Assembly sessions. '
             'School-registration closing October 15, 2025. No personal cash '
             'scholarship.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/edicao-2025-2026-do-parlamento-dos-jovens-abre-inscricoes-para-escolas-de-1-de-setembro-a-15-de-outubro'},
 {'key': 'https://ipdj.gov.pt/-/edicao-2026-2027-parlamento-jovens-abre-inscricoes-para-escolas',
  'title': 'Edição 2026/2027 do Parlamento dos Jovens abre inscrições para escolas a 1 '
           'de setembro',
  'url': 'https://ipdj.gov.pt/-/edicao-2026-2027-parlamento-jovens-abre-inscricoes-para-escolas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-10-01',
  'closing_local_clock': None,
  'summary': '2026/27 Parlamento dos Jovens for students in the second and third '
             'basic-education cycles and secondary education in public, private and '
             'cooperative schools. The theme is housing, youth and territory. Schools '
             'register and include the programme in their annual activity plan by '
             'October 1, 2026. School/district preparation leads to national '
             'parliamentary sessions. This is civic education, not a personal cash '
             'scholarship; pupil access follows the participating school.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/edicao-2026-2027-parlamento-jovens-abre-inscricoes-para-escolas'},
 {'key': 'https://ipdj.gov.pt/-/erasmus-juventude-e-desporto-2026-candidaturas-a-decorrer',
  'title': 'Erasmus+ Juventude e Desporto - 2026: candidaturas a decorrer',
  'url': 'https://ipdj.gov.pt/-/erasmus-juventude-e-desporto-2026-candidaturas-a-decorrer',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Erasmus+ 2026 institutional mobility/cooperation funding framework, with '
             'an approximately EUR 5.2 billion programme-wide budget. Schools, '
             'universities, vocational providers, NGOs and youth groups apply through '
             'national agencies or EACEA, depending on the action. Individuals do not '
             'apply directly; they participate through funded organisations. Specific '
             'action budgets, closing dates and additional eligibility are not stated '
             'in this own notice.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/erasmus-juventude-e-desporto-2026-candidaturas-a-decorrer'},
 {'key': 'https://ipdj.gov.pt/-/estao-abertas-as-candidaturas-a-quinta-edicao-desporto-mais-acessivel',
  'title': 'Estão abertas as candidaturas à 5.ª Edição do Prémio «Desporto + '
           'Acessível»',
  'url': 'https://ipdj.gov.pt/-/estao-abertas-as-candidaturas-a-quinta-edicao-desporto-mais-acessivel',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-06-14',
  'closing_local_clock': None,
  'summary': '2026 Desporto Mais Acessível recognises accessibility, '
             'sport-development, research/training and volunteering projects. Winner: '
             'EUR 6,000 plus seven hours of specialised online consultancy. Four '
             'mentions: EUR 1,000 plus five consultancy hours each. Decathlon support '
             'is conditional, up to EUR 2,000/project within an EUR 8,000 pool. '
             'Closing June 14, 2026. Additional delegated eligibility terms remain '
             'unverified.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/estao-abertas-as-candidaturas-a-quinta-edicao-desporto-mais-acessivel'},
 {'key': 'https://ipdj.gov.pt/-/festival-politica-e-ipdj-abrem-candidaturas-para-as-edicoes-de-loule-e-coimbra-do-concurso-para-jovens-artistas-ativistas-e-criadores::coimbra',
  'title': 'Festival Política e IPDJ abrem candidaturas para as edições de Loulé e de '
           'Coimbra do concurso para jovens artistas, ativistas e criadores',
  'url': 'https://ipdj.gov.pt/-/festival-politica-e-ipdj-abrem-candidaturas-para-as-edicoes-de-loule-e-coimbra-do-concurso-para-jovens-artistas-ativistas-e-criadores',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-07-30',
  'closing_local_clock': None,
  'summary': 'Festival Política 2025 creative proposal call in Coimbra, Centro, for '
             'artists, activists and creators aged 30 or younger. Two selected '
             'proposals receive EUR 500 each on human rights and civic/political '
             'participation under “Revoluções em Curso”, presented at Convento São '
             'Francisco in November. Closing July 30, 2025. Conditional project award, '
             'not payment to every applicant.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/festival-politica-e-ipdj-abrem-candidaturas-para-as-edicoes-de-loule-e-coimbra-do-concurso-para-jovens-artistas-ativistas-e-criadores'},
 {'key': 'https://ipdj.gov.pt/-/festival-politica-e-ipdj-abrem-candidaturas-para-as-edicoes-de-loule-e-coimbra-do-concurso-para-jovens-artistas-ativistas-e-criadores::loule',
  'title': 'Festival Política e IPDJ abrem candidaturas para as edições de Loulé e de '
           'Coimbra do concurso para jovens artistas, ativistas e criadores',
  'url': 'https://ipdj.gov.pt/-/festival-politica-e-ipdj-abrem-candidaturas-para-as-edicoes-de-loule-e-coimbra-do-concurso-para-jovens-artistas-ativistas-e-criadores',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-07-30',
  'closing_local_clock': None,
  'summary': 'Festival Política 2025 creative proposal call in Loulé, Algarve, for '
             'artists, activists and creators aged 30 or younger. Two selected '
             'proposals receive EUR 500 each to address human rights and '
             'civic/political participation under “Revoluções em Curso”, presented at '
             'the Cineteatro in October. Closing July 30, 2025. Conditional project '
             'award, not payment to every applicant.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/festival-politica-e-ipdj-abrem-candidaturas-para-as-edicoes-de-loule-e-coimbra-do-concurso-para-jovens-artistas-ativistas-e-criadores'},
 {'key': 'https://ipdj.gov.pt/-/final-da-taca-de-portugal-2026-futebol-voluntariado-jovem-inscricoes-abertas',
  'title': 'Final da Taça de Portugal 2026 - Futebol: voluntariado jovem, inscrições '
           'abertas',
  'url': 'https://ipdj.gov.pt/-/final-da-taca-de-portugal-2026-futebol-voluntariado-jovem-inscricoes-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'FPF volunteering at the men’s Taça de Portugal final, May 24, 2026, '
             '14–19, Jamor stadium. Applicants aged 17–30 assist event organisation; '
             '40 places announced. Selected volunteers must attend youth-volunteering '
             'and specific training on May 20 at 16:30. Financial support is not '
             'stated in this notice; registration closing unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/final-da-taca-de-portugal-2026-futebol-voluntariado-jovem-inscricoes-abertas'},
 {'key': 'https://ipdj.gov.pt/-/final-da-taca-de-portugal-masculina-futebol-2024-25-voluntariado-jovem-inscricoes-abertas',
  'title': 'Final da Taça de Portugal Masculina - Futebol: voluntariado jovem, '
           'inscrições abertas',
  'url': 'https://ipdj.gov.pt/-/final-da-taca-de-portugal-masculina-futebol-2024-25-voluntariado-jovem-inscricoes-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'FPF volunteering at the men’s Taça de Portugal final, May 25, 2025, '
             '13:30–19, Jamor. Applicants aged 16–30 need full availability and prior '
             'FPF project participation. Compulsory volunteering/specific training; '
             'first-time IPDJ registration required. EUR 13 reimbursement, snack, '
             'accident/liability insurance and certificate provided. Closing unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/final-da-taca-de-portugal-masculina-futebol-2024-25-voluntariado-jovem-inscricoes-abertas'},
 {'key': 'https://ipdj.gov.pt/-/final-feminina-da-taca-de-portugal-futebol-voluntariado-jovem-inscricoes-abertas',
  'title': 'Final Feminina da Taça de Portugal - Futebol: voluntariado jovem, '
           'inscrições abertas',
  'url': 'https://ipdj.gov.pt/-/final-feminina-da-taca-de-portugal-futebol-voluntariado-jovem-inscricoes-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'FPF volunteering at the women’s Taça de Portugal final, May 17, 2025, '
             '13–18, Jamor stadium. Thirty places for ages 16–30, supporting public '
             'guidance, media logistics, accreditation, ticketing and fans. First-time '
             'applicants register with IPDJ. This own notice does not specify '
             'financial support or compulsory training; registration closing unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/final-feminina-da-taca-de-portugal-futebol-voluntariado-jovem-inscricoes-abertas'},
 {'key': 'https://ipdj.gov.pt/-/final-feminina-taca-de-portugal-2026-futebol-voluntariado-jovem-inscricoes-abertas',
  'title': 'Final Feminina da Taça de Portugal 2026 - Futebol: voluntariado jovem, '
           'inscrições abertas',
  'url': 'https://ipdj.gov.pt/-/final-feminina-taca-de-portugal-2026-futebol-voluntariado-jovem-inscricoes-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'FPF volunteering at the women’s Taça de Portugal final, May 17, 2026, '
             '14–19, Jamor stadium. Applicants aged 16–30 assist event organisation; '
             '40 places announced. Selected volunteers must attend youth-volunteering '
             'and specific training on May 13 at 16:30. Financial support is not '
             'stated in this notice; registration closing unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/final-feminina-taca-de-portugal-2026-futebol-voluntariado-jovem-inscricoes-abertas'},
 {'key': 'https://ipdj.gov.pt/-/final-supertaca-candido-oliveira-2026-inscricoes-abertas-para-voluntariado-jovem',
  'title': 'Final «Supertaça Cândido Oliveira 2026»: inscrições abertas para '
           'voluntariado jovem',
  'url': 'https://ipdj.gov.pt/-/final-supertaca-candido-oliveira-2026-inscricoes-abertas-para-voluntariado-jovem',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'FPF volunteering for Supertaça Cândido Oliveira in Coimbra, July '
             '31–August 1, 2026. Applicants aged 17–30 need full availability, '
             'previous FPF participation and compulsory training. Fifty places are '
             'capacity. Event support includes equipment, snack, accident/liability '
             'insurance, certificate and EUR 13 expense reimbursement, not a wage. '
             'Registration closing is not stated.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/final-supertaca-candido-oliveira-2026-inscricoes-abertas-para-voluntariado-jovem'},
 {'key': 'https://ipdj.gov.pt/-/formacao-transformacao-digital-para-as-organizacoes-desportivas-inscricoes-abertas',
  'title': 'Formação «Transformação Digital para as Organizações Desportivas»: '
           'inscrições abertas',
  'url': 'https://ipdj.gov.pt/-/formacao-transformacao-digital-para-as-organizacoes-desportivas-inscricoes-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Free hybrid training on digital transformation, AI, digital platforms '
             'and management innovation for sports managers, coaches, researchers and '
             'other professionals. September 30, 2026 at 18:00, CIUL Lisbon or online. '
             'Organised by Fundação do Desporto, IPDJ and Lisbon municipality. Prior '
             'registration is required; closing is not stated.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/formacao-transformacao-digital-para-as-organizacoes-desportivas-inscricoes-abertas'},
 {'key': 'https://ipdj.gov.pt/-/fundacao-jornada-abre-candidaturas-ao-programa-jovens-agentes-de-esperanca',
  'title': 'Fundação Jornada abre candidaturas ao programa «Jovens, agentes de '
           'Esperança”',
  'url': 'https://ipdj.gov.pt/-/fundacao-jornada-abre-candidaturas-ao-programa-jovens-agentes-de-esperanca',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-12-16',
  'closing_local_clock': None,
  'summary': 'Fundação Jornada’s 2025 Jovens, agentes de Esperança accepts projects '
             'promoted by young people aged 15–35 or organisations serving young '
             'residents of Portugal in that age group. Themes include education, '
             'spirituality, citizenship, mental health and sustainability. Up to EUR '
             '30,000 is a whole-project award; EUR 360,000 is the total pool. Closing '
             'December 16, 2025. Additional project and selection conditions apply.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/fundacao-jornada-abre-candidaturas-ao-programa-jovens-agentes-de-esperanca'},
 {'key': 'https://ipdj.gov.pt/-/geracao-z-abre-candidaturas-a-organizacao-de-atividades-de-voluntariado-ate-14-de-maio',
  'title': 'Geração Z abre candidaturas à organização de atividades de voluntariado '
           'até 14 de maio',
  'url': 'https://ipdj.gov.pt/-/geracao-z-abre-candidaturas-a-organizacao-de-atividades-de-voluntariado-ate-14-de-maio',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-05-14',
  'closing_local_clock': None,
  'summary': '2025 Geração Z organiser call for nonprofit promoters of youth inclusion '
             'and rights volunteering. Volunteers aged 14–30 may receive EUR 13/day '
             'expense reimbursement for up to five hours/day, not wages. A private '
             'project is limited to EUR 3,600 including applicable management support. '
             'Entity applications close May 14, 2025; the activity period '
             'June–November is distinct.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/geracao-z-abre-candidaturas-a-organizacao-de-atividades-de-voluntariado-ate-14-de-maio'},
 {'key': 'https://ipdj.gov.pt/-/guardioes-do-tempo-com-inscricoes-abertas-para-voluntariado-jovem',
  'title': '«Guardiões do Tempo»: Abertas as inscrições para projetos de voluntariado '
           'jovem no património cultural',
  'url': 'https://ipdj.gov.pt/-/guardioes-do-tempo-com-inscricoes-abertas-para-voluntariado-jovem',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-08-13',
  'closing_local_clock': None,
  'summary': 'Guardiões do Tempo heritage volunteering in mainland museums, monuments '
             'and archaeological sites, principally for ages 16–21. Around 200 places '
             'are programme capacity. Participants receive EUR 13/day expense '
             'reimbursement, accident/liability insurance and a certificate; this is '
             'not a salary. Applications were extended to August 13, 2026. Site '
             'allocation and actual participation dates follow the project.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/guardioes-do-tempo-com-inscricoes-abertas-para-voluntariado-jovem'},
 {'key': 'https://ipdj.gov.pt/-/inscricoes-abertas-para-voluntariado-jovem-no-festival-sol-da-caparica-2026',
  'title': 'Inscrições abertas para voluntariado jovem no Festival Sol da Caparica '
           '2026',
  'url': 'https://ipdj.gov.pt/-/inscricoes-abertas-para-voluntariado-jovem-no-festival-sol-da-caparica-2026',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Volunteer at IPDJ’s Sol da Caparica festival space, August 13–16, 2026, '
             'Costa da Caparica. Ages 16–30, initiative, responsibility, communication '
             'and teamwork apply. Participants support activities, explain programmes '
             'and assist visitors in the advertised shifts. The own notice does not '
             'state money, insurance, housing or travel coverage. Registration closing '
             'is unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/inscricoes-abertas-para-voluntariado-jovem-no-festival-sol-da-caparica-2026'},
 {'key': 'https://ipdj.gov.pt/-/integridade-no-desporto-em-destaque-em-conferencia-na-faculdade-de-direito-de-lisboa',
  'title': 'Integridade no Desporto em destaque em conferência na Faculdade de Direito '
           'de Lisboa',
  'url': 'https://ipdj.gov.pt/-/integridade-no-desporto-em-destaque-em-conferencia-na-faculdade-de-direito-de-lisboa',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Public conference on integrity in sport, April 15, 2026 from 09:45 at '
             'the Faculty of Law in Lisbon. Sessions cover competitive integrity, '
             'betting regulation, gambling addiction, antidoping, money laundering and '
             'organisational transparency. The event is open to the public; fees and '
             'registration closing are not stated. No personal funding is offered.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/integridade-no-desporto-em-destaque-em-conferencia-na-faculdade-de-direito-de-lisboa'},
 {'key': 'https://ipdj.gov.pt/-/ipdj-desafia-jovens-a-criar-nova-identidade-dos-seus-espacos-de-juventude',
  'title': 'IPDJ desafia jovens a criar a nova identidade dos seus espaços de '
           'juventude',
  'url': 'https://ipdj.gov.pt/-/ipdj-desafia-jovens-a-criar-nova-identidade-dos-seus-espacos-de-juventude',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-08-12',
  'closing_local_clock': None,
  'summary': '2025 competition to name and design IPDJ youth spaces for Portugal '
             'residents. Individual, informal-group, organisation and teams of up to '
             'five may enter; most members must be aged 15–30. Winning proposals '
             'receive EUR 1,000, 500 or 250 and certification, subject to jury and '
             'public voting. Initial expressions of interest close August 12, 2025; '
             'later proposal stages are separate.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/ipdj-desafia-jovens-a-criar-nova-identidade-dos-seus-espacos-de-juventude'},
 {'key': 'https://ipdj.gov.pt/-/ipdj-organiza-formacao-de-formadores-sob-o-tema-metodologia-ativas-para-o-desenvolvimento-de-competencias',
  'title': 'IPDJ organiza Formação de Formadores sob o tema «Metodologia Ativas para o '
           'Desenvolvimento de Competências»',
  'url': 'https://ipdj.gov.pt/-/ipdj-organiza-formacao-de-formadores-sob-o-tema-metodologia-ativas-para-o-desenvolvimento-de-competencias',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 active-methodology training for people responsible for coach '
             'education in sports federations, at Lisbon Youth Centre. Two eight-hour '
             'sessions on November 14 and 28 develop theoretical and practical '
             'teaching skills for replication within the sport. Current registration, '
             'fees and closing are unstated. No trainer employment or personal grant '
             'is offered.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/ipdj-organiza-formacao-de-formadores-sob-o-tema-metodologia-ativas-para-o-desenvolvimento-de-competencias'},
 {'key': 'https://ipdj.gov.pt/-/liga-portugal-e-ipdj-criam-bolsa-de-voluntarios-para-as-competicoes-profissionais',
  'title': 'Liga Portugal e IPDJ criam bolsa de voluntários para as competições '
           'profissionais',
  'url': 'https://ipdj.gov.pt/-/liga-portugal-e-ipdj-criam-bolsa-de-voluntarios-para-as-competicoes-profissionais',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical 2025 Liga Portugal volunteer pool for applicants aged 18–30 '
             'residing in mainland Portugal. Support match-day marketing, ticketing, '
             'access, logistics and fan zones; selection and preparation precede '
             'club-specific training. Accident insurance, information, identification, '
             'lunch box and certificate provided. Registration starts August 6; '
             'closing unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/liga-portugal-e-ipdj-criam-bolsa-de-voluntarios-para-as-competicoes-profissionais'},
 {'key': 'https://ipdj.gov.pt/-/medida-afirma-te-ja-prorroga-as-candidaturas-ate-2-de-maio',
  'title': 'Medida Afirma-te Já | Prorroga as candidaturas até 2 de maio',
  'url': 'https://ipdj.gov.pt/-/medida-afirma-te-ja-prorroga-as-candidaturas-ate-2-de-maio',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-05-02',
  'closing_local_clock': None,
  'summary': 'Afirma-te Já 2025 institutional call for eligible nonprofit local '
             'inclusion projects, extended to May 2. Young NEET people aged 18–29 must '
             "meet the programme's specified vulnerability conditions as "
             'beneficiaries. The project limit is EUR 35,000/year and EUR 105,000 '
             'across 36 months, not a personal stipend; the EUR 1.5 million figure is '
             'the total pool. Published implementation-month wording conflicts between '
             'the hub and project table.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/medida-afirma-te-ja-prorroga-as-candidaturas-ate-2-de-maio'},
 {'key': 'https://ipdj.gov.pt/-/mostra-nacional-jovens-criadores-2026-prorrogacao-das-candidaturas',
  'title': 'Mostra Nacional Jovens Criadores 2026: prorrogação das candidaturas',
  'url': 'https://ipdj.gov.pt/-/mostra-nacional-jovens-criadores-2026-prorrogacao-das-candidaturas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': '20:00',
  'summary': '2026 national young-creators competition for artists aged 16–30 residing '
             'in Portugal, across fifteen artistic areas. Selected works are publicly '
             'presented; each area winner receives EUR 1,100 and a 50% discount on '
             'Gerador’s 2027 critical-thinking programme. EUR 16,500 is the whole '
             'prize pool. Applications were extended to September 20, 2026 at 20:00, '
             'without a stated zone. This is not a participation stipend.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/mostra-nacional-jovens-criadores-2026-prorrogacao-das-candidaturas'},
 {'key': 'https://ipdj.gov.pt/-/nova-acao-envolve-te-agora-nos-abre-candidaturas-para-projetos-de-voluntariado-jovem',
  'title': 'Nova ação “Envolve-te – Agora Nós” abre candidaturas para projetos de '
           'voluntariado jovem',
  'url': 'https://ipdj.gov.pt/-/nova-acao-envolve-te-agora-nos-abre-candidaturas-para-projetos-de-voluntariado-jovem',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-05-04',
  'closing_local_clock': None,
  'summary': '2026 Agora Nós organiser call for private nonprofits closes May 4. Each '
             'entity may have at most three projects covering up to three advertised '
             'community themes, with activity June 15–October 30. Funding combines EUR '
             '13/day/volunteer expense reimbursement with EUR 50–600 management '
             'support according to participant count; the entity pays volunteers '
             'directly. IPDJ supplies accident/liability insurance. These are project '
             'costs, not wages.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/nova-acao-envolve-te-agora-nos-abre-candidaturas-para-projetos-de-voluntariado-jovem'},
 {'key': 'https://ipdj.gov.pt/-/os-jovens-desistiram-da-politica-webinar-desafia-mitos-e-promove-o-debate',
  'title': 'Os jovens desistiram da política? Webinar desafia mitos e promove o debate',
  'url': 'https://ipdj.gov.pt/-/os-jovens-desistiram-da-politica-webinar-desafia-mitos-e-promove-o-debate',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Free online political-literacy webinar for young people and others '
             'interested in democratic participation, September 28, 2026 at 17:30, '
             'with Adriana Cardoso. Prior registration is required and participants '
             'receive a certificate. A registration closing date is not stated.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/os-jovens-desistiram-da-politica-webinar-desafia-mitos-e-promove-o-debate'},
 {'key': 'https://ipdj.gov.pt/-/prazo-das-candidaturas-aos-premios-clube-top-25-foi-alargado-ate-30-de-maio',
  'title': "Prazo da candidaturas aos «Prémios Clube Top'25» foi alargado até ao dia "
           '30 de maio',
  'url': 'https://ipdj.gov.pt/-/prazo-das-candidaturas-aos-premios-clube-top-25-foi-alargado-ate-30-de-maio',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-05-30',
  'closing_local_clock': None,
  'summary': 'Historical 2025 Clube Top award call for sports clubs/associations, '
             'sport-promoting associations and practitioner clubs registered in RNFDC. '
             'Recognises management practices contributing to sporting and social '
             'development. Application extended to May 30, 2025. Award amounts and '
             'additional eligibility terms are unverified; awards are institutional, '
             'not personal stipends.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/prazo-das-candidaturas-aos-premios-clube-top-25-foi-alargado-ate-30-de-maio'},
 {'key': 'https://ipdj.gov.pt/-/premio-de-investigacao-sobre-etica-no-desporto-edicao-especial-cartao-branco-candidaturas-abertas',
  'title': 'Prémio de «Investigação sobre Ética no Desporto» - edição especial Cartão '
           'Branco: candidaturas abertas',
  'url': 'https://ipdj.gov.pt/-/premio-de-investigacao-sobre-etica-no-desporto-edicao-especial-cartao-branco-candidaturas-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-09-15',
  'closing_local_clock': None,
  'summary': '2026 research prize on positive reinforcement and fair play in sport, '
             'especially Cartão Branco. Applicants of any nationality must be enrolled '
             'at a public or private higher-education institution based in Portugal '
             "during submission. The two categories are master's/doctoral "
             'dissertations and scientific articles. Closing September 15, 2026. Prize '
             'amounts and additional regulation conditions are not established by the '
             'accessible notice.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/premio-de-investigacao-sobre-etica-no-desporto-edicao-especial-cartao-branco-candidaturas-abertas'},
 {'key': 'https://ipdj.gov.pt/-/premios-boas-praticas-associativismo-jovem-2026-ja-tem-candidaturas-abertas::higher-education-student-associations',
  'title': 'Prémios «Boas Práticas Associativismo Jovem» 2026 já têm candidaturas '
           'abertas',
  'url': 'https://ipdj.gov.pt/-/premios-boas-praticas-associativismo-jovem-2026-ja-tem-candidaturas-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-08-31',
  'closing_local_clock': None,
  'summary': 'Effective RNAJ higher-education student associations and federations '
             'seek 2026 good-practice awards for completed projects, with mandatory '
             'public presentation. Regional winner EUR 1,750; closing August 31, 2026. '
             'The notice and regulation conflict on mainland/island scope and number '
             'of EUR 500 mentions, so additional awards are uncertain. Institutional '
             'award, not a personal stipend.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/premios-boas-praticas-associativismo-jovem-2026-ja-tem-candidaturas-abertas'},
 {'key': 'https://ipdj.gov.pt/-/premios-boas-praticas-associativismo-jovem-2026-ja-tem-candidaturas-abertas::youth-associations',
  'title': 'Prémios «Boas Práticas Associativismo Jovem» 2026 já têm candidaturas '
           'abertas',
  'url': 'https://ipdj.gov.pt/-/premios-boas-praticas-associativismo-jovem-2026-ja-tem-candidaturas-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-08-31',
  'closing_local_clock': None,
  'summary': 'Effective RNAJ youth associations and federations seek 2026 '
             'good-practice awards for completed projects, with mandatory public '
             'presentation. Each regional winner receives EUR 1,750. Closing August '
             '31, 2026. The own notice and regulation conflict on mainland/island '
             'scope and number of EUR 500 mentions, so additional awards are '
             'uncertain. Institutional award, not a personal stipend.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/premios-boas-praticas-associativismo-jovem-2026-ja-tem-candidaturas-abertas'},
 {'key': 'https://ipdj.gov.pt/-/premios-clube-top-edicao-2026-candidaturas-abertas',
  'title': '«Prémios Clube Top», edição 2026: candidaturas abertas',
  'url': 'https://ipdj.gov.pt/-/premios-clube-top-edicao-2026-candidaturas-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-06-29',
  'closing_local_clock': None,
  'summary': '2026 Clube Top institutional sports-club awards recognise good '
             'management and contributions to sport and society. Applications close '
             'June 29, 2026. Prize amounts, detailed categories and applicant '
             'conditions require the actual linked terms and are not established by '
             'this short own notice. Awards are not personal stipends.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/premios-clube-top-edicao-2026-candidaturas-abertas'},
 {'key': 'https://ipdj.gov.pt/-/programa-anda-conhecer-portugal-tem-mais-5000-dormidas-exclusivas-para-associacoes-federacoes-de-juventude-e-de-estudantes',
  'title': 'Programa «ANDA conhecer Portugal» tem mais 5 000 dormidas exclusivas para '
           'Associações/Federações de Juventude e de Estudantes',
  'url': 'https://ipdj.gov.pt/-/programa-anda-conhecer-portugal-tem-mais-5000-dormidas-exclusivas-para-associacoes-federacoes-de-juventude-e-de-estudantes',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['other'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Free ANDA accommodation vouchers: young people aged 18–30 can receive '
             'six youth-hostel nights through limited group reservations requested by '
             'effective RNAJ associations/federations. Youth-association members and '
             'board members, and higher-education student-association board members '
             'aged at most 30, follow the stated access rules. Five thousand nights '
             'are the pool, not personal awards. Train travel is excluded; closing is '
             'unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programa-anda-conhecer-portugal-tem-mais-5000-dormidas-exclusivas-para-associacoes-federacoes-de-juventude-e-de-estudantes'},
 {'key': 'https://ipdj.gov.pt/-/programa-associa-te-candidaturas-2026-ate-12-de-maio',
  'title': 'Programa «Associa-te»: candidaturas 2026 decorrem até 12 de maio',
  'url': 'https://ipdj.gov.pt/-/programa-associa-te-candidaturas-2026-ate-12-de-maio',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-05-12',
  'closing_local_clock': None,
  'summary': '2026 Associa-te equipment support for effective RNAJ basic and secondary '
             'student associations. One application per school/year must concern the '
             'current school-year project and have school-management acknowledgement. '
             'Maximum EUR 500/association; EUR 9,000 is the call pool. Previously '
             'awarded same equipment is ineligible. Applications close May 12, 2026. '
             'Equipment benefits belong to the association, not each pupil.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programa-associa-te-candidaturas-2026-ate-12-de-maio'},
 {'key': 'https://ipdj.gov.pt/-/programa-cuida-te-medida-2-intervencao-comunitaria-prorroga-as-candidaturas-2edicao',
  'title': 'Programa «Cuida-te - Medida 2: Intervenção Comunitária» prorroga as '
           'candidaturas à 2ª edição',
  'url': 'https://ipdj.gov.pt/-/programa-cuida-te-medida-2-intervencao-comunitaria-prorroga-as-candidaturas-2edicao',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-08-10',
  'closing_local_clock': None,
  'summary': 'Second Cuida-te community-prevention project call for qualified youth, '
             'health or social-intervention organisations and consortia. Schools and '
             'municipalities may apply only within a consortium. Projects require '
             'trained teams and evidence-based health-promotion activities; awards '
             'fund the whole project, not participant stipends. Closing extended to '
             'August 10, 2026; full project and application-guide conditions apply.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programa-cuida-te-medida-2-intervencao-comunitaria-prorroga-as-candidaturas-2edicao'},
 {'key': 'https://ipdj.gov.pt/-/programa-de-apoio-pontual-para-associacoes-juvenis-de-estudantes-e-de-carater-juvenil-candidaturas-abertas-1::paacj',
  'title': 'Programa de Apoio Pontual para Associações Juvenis, de Estudantes e de '
           'Caráter Juvenil: candidaturas abertas',
  'url': 'https://ipdj.gov.pt/-/programa-de-apoio-pontual-para-associacoes-juvenis-de-estudantes-e-de-carater-juvenil-candidaturas-abertas-1',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-10-02',
  'closing_local_clock': None,
  'summary': '2026 PAACJ occasional institutional project support for effective RNAJ '
             'youth-related associations. Up to EUR 1,500/project, subject to '
             'annual-support and application-count conditions and cofinancing rules. '
             'Applications close October 2, 2026. Approved association costs are '
             'supported, not personal stipends.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programa-de-apoio-pontual-para-associacoes-juvenis-de-estudantes-e-de-carater-juvenil-candidaturas-abertas-1'},
 {'key': 'https://ipdj.gov.pt/-/programa-de-apoio-pontual-para-associacoes-juvenis-de-estudantes-e-de-carater-juvenil-candidaturas-abertas-1::pae',
  'title': 'Programa de Apoio Pontual para Associações Juvenis, de Estudantes e de '
           'Caráter Juvenil: candidaturas abertas',
  'url': 'https://ipdj.gov.pt/-/programa-de-apoio-pontual-para-associacoes-juvenis-de-estudantes-e-de-carater-juvenil-candidaturas-abertas-1',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-10-02',
  'closing_local_clock': None,
  'summary': '2026 PAE occasional support for effective RNAJ student associations: '
             'higher-education associations with annual support may seek EUR 1,500; '
             'without annual support, up to two EUR 5,000 applications. '
             'Basic/secondary associations may seek up to three EUR 1,000 '
             'applications. Closing October 2, 2026. Institutional project funding '
             'subject to cofinancing/eligibility rules.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programa-de-apoio-pontual-para-associacoes-juvenis-de-estudantes-e-de-carater-juvenil-candidaturas-abertas-1'},
 {'key': 'https://ipdj.gov.pt/-/programa-de-apoio-pontual-para-associacoes-juvenis-de-estudantes-e-de-carater-juvenil-candidaturas-abertas-1::paj',
  'title': 'Programa de Apoio Pontual para Associações Juvenis, de Estudantes e de '
           'Caráter Juvenil: candidaturas abertas',
  'url': 'https://ipdj.gov.pt/-/programa-de-apoio-pontual-para-associacoes-juvenis-de-estudantes-e-de-carater-juvenil-candidaturas-abertas-1',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-10-02',
  'closing_local_clock': None,
  'summary': '2026 PAJ occasional institutional project support for effective RNAJ '
             'youth associations and federations. Up to EUR 1,500/project, subject to '
             'annual-support and application-count conditions and cofinancing rules. '
             'Applications close October 2, 2026. Funding covers approved association '
             'costs, not personal stipends.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programa-de-apoio-pontual-para-associacoes-juvenis-de-estudantes-e-de-carater-juvenil-candidaturas-abertas-1'},
 {'key': 'https://ipdj.gov.pt/-/programa-de-incentivo-ao-desenvolvimento-associativo-ida-apoia-associacoes-juvenis-candidaturas-abertas-2026',
  'title': 'Programa de Incentivo ao Desenvolvimento Associativo (IDA) apoia '
           'associações juvenis na integração de jovens em contexto de trabalho',
  'url': 'https://ipdj.gov.pt/-/programa-de-incentivo-ao-desenvolvimento-associativo-ida-apoia-associacoes-juvenis-candidaturas-abertas-2026',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'RNAJ youth and student associations, federations and CNJ with '
             'IEFP-approved internships may seek institutional internship-management '
             'support under IDA. Interns are generally 18–30, with conditional '
             'eligibility through 35 under the internship rules. Submit within five '
             'working days of the IEFP application; funding depends on remaining '
             'budget. Amounts follow the annual order and are not established as '
             'personal pay by this notice. No universal closing date is stated.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programa-de-incentivo-ao-desenvolvimento-associativo-ida-apoia-associacoes-juvenis-candidaturas-abertas-2026'},
 {'key': 'https://ipdj.gov.pt/-/programa-de-reabilitacao-de-instalacoes-desportivas-2025-abre-candidaturas',
  'title': 'Programa de Reabilitação de Instalações Desportivas 2025 abre candidaturas '
           'de 14 de agosto a 15 de setembro',
  'url': 'https://ipdj.gov.pt/-/programa-de-reabilitacao-de-instalacoes-desportivas-2025-abre-candidaturas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': '17:00',
  'summary': '2025 PRID rehabilitation grant for eligible mainland nonprofit sports '
             'clubs and associations. Institutional support is conditional on project '
             'and property-management eligibility; it is not an individual athlete '
             'award. Applications close September 15, 2025 at 17:00, time zone '
             'unstated. Current 2026 amounts and exclusions are not assumed to apply '
             'unchanged to this historical edition.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programa-de-reabilitacao-de-instalacoes-desportivas-2025-abre-candidaturas'},
 {'key': 'https://ipdj.gov.pt/-/programa-de-reabilitacao-de-instalacoes-desportivas-2026-abertura-de-candidatura',
  'title': 'Programa de Reabilitação de Instalações Desportivas 2026 | Abertura de '
           'candidatura',
  'url': 'https://ipdj.gov.pt/-/programa-de-reabilitacao-de-instalacoes-desportivas-2026-abertura-de-candidatura',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': '17:00',
  'summary': '2026 PRID facility-rehabilitation support for eligible mainland '
             'nonprofit sports clubs and associations. Eligible project costs cannot '
             'exceed EUR 150,000 including VAT; the whole grant is limited to EUR '
             '50,000 and 75% of eligible cost. Exclusions, prior-support rules and '
             'sufficient property-management rights apply. Closing May 25, 2026 at '
             '17:00, with no time zone stated. New construction and duplicate funding '
             'for the same object are excluded.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programa-de-reabilitacao-de-instalacoes-desportivas-2026-abertura-de-candidatura'},
 {'key': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-abertas-as-inscricoes-para-jovens',
  'title': 'Programa «Férias em Movimento»: abertas as inscrições para jovens',
  'url': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-abertas-as-inscricoes-para-jovens',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Free 2025 Oficina Cultural camps for ages 11–17, with at least half '
             'cultural activities. Residential camps last 6–14 nights and '
             'nonresidential camps 5–15 days; guardian permission applies. '
             'Registration is relative to the chosen camp and vacancies, not a '
             'universal absolute deadline. Organiser grants are not personal awards.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programa-ferias-em-movimento-abertas-as-inscricoes-para-jovens'},
 {'key': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-cinco-ponto-zero-prazo-de-candidaturas-para-entidades-organizadoras-prorrogado',
  'title': 'Programa «Férias em Movimento» - 5.0 | O prazo de candidaturas para '
           'entidades organizadoras foi prorrogado',
  'url': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-cinco-ponto-zero-prazo-de-candidaturas-para-entidades-organizadoras-prorrogado',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-05-29',
  'closing_local_clock': None,
  'summary': '2026 Férias 5.0 organiser funding for licensed nonprofit camp providers. '
             'Camps for ages 6–10 must be nonresidential, last 5–15 days and dedicate '
             'at least half their activities to digital learning. Conditional support '
             'covers EUR 7/day/child plus defined staff and material costs, not a '
             "child's personal award. The organiser application extension closes May "
             '29, 2026.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programa-ferias-em-movimento-cinco-ponto-zero-prazo-de-candidaturas-para-entidades-organizadoras-prorrogado'},
 {'key': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-ferias-5ponto0-financiamento-de-atividades',
  'title': 'Programa Férias em Movimento - Férias 5.0 | financiamento de atividades de '
           'Campos de Férias para crianças e jovens com foco no Digital',
  'url': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-ferias-5ponto0-financiamento-de-atividades',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-09-01',
  'closing_local_clock': None,
  'summary': 'Historical 2025 Férias em Movimento — Férias 5.0 call funds organisers '
             'to develop holiday-camp activities, in partnership with IPDJ under the '
             'PRR. Applications closed on September 1, 2025. The own notice does not '
             'state individual funding amounts or full additional eligibility '
             'conditions.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programa-ferias-em-movimento-ferias-5ponto0-financiamento-de-atividades'},
 {'key': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-ferias-cinco-pont-zero-inscricoes-jovens-a-decorrer',
  'title': 'Programa «Férias em Movimento» - Férias 5.0 | inscrições para jovens a '
           'decorrer',
  'url': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-ferias-cinco-pont-zero-inscricoes-jovens-a-decorrer',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Free 2026 Férias 5.0 nonresidential camps for children aged 6–10, '
             'lasting 5–15 days with at least half the programme devoted to robotics, '
             'programming, AI or other digital learning. Parent or guardian permission '
             'and availability at the selected camp apply. Registration follows the '
             "individual camp's relative lead time; no single absolute closing date is "
             'established. Organiser funding is not a payment to each child.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programa-ferias-em-movimento-ferias-cinco-pont-zero-inscricoes-jovens-a-decorrer'},
 {'key': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-feriascincopontozero-abertas-as-inscricoes-para-jovens',
  'title': 'Programa «Férias em Movimento» - Férias 5.0 | abertas as inscrições para '
           'jovens',
  'url': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-feriascincopontozero-abertas-as-inscricoes-para-jovens',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Free 2025 Férias 5.0 nonresidential camps for ages 6–10, with digital '
             'learning as at least half the programme. Camps last 5–15 days and '
             "require guardian permission. Applications follow the selected camp's "
             'relative five-day lead time and available places; there is no universal '
             'absolute deadline. Institutional organiser support is not personal cash.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programa-ferias-em-movimento-feriascincopontozero-abertas-as-inscricoes-para-jovens'},
 {'key': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-oficina-cultural-candidaturas-para-entidades-organizadoras',
  'title': 'Programa «Férias em Movimento» Oficina Cultural | prorroga candidaturas '
           'para entidades organizadoras até 22 de maio',
  'url': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-oficina-cultural-candidaturas-para-entidades-organizadoras',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 Oficina Cultural funding for licensed nonprofit camp organisers, '
             'serving ages 11–17 with at least half cultural activities. Conditional '
             'support is capped at EUR 30/night/child residential or EUR 23/day/child '
             "nonresidential, not a personal award. The extension notice's headline "
             'says May 22 while its body says May 15; the exact closing is unresolved.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programa-ferias-em-movimento-oficina-cultural-candidaturas-para-entidades-organizadoras'},
 {'key': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-oficina-cultural-com-candidaturas-abertas-para-jovens',
  'title': 'Programa Férias em Movimento “Oficina Cultural” com candidaturas abertas '
           'para jovens',
  'url': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-oficina-cultural-com-candidaturas-abertas-para-jovens',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Free 2026 Oficina Cultural camps for ages 11–17, with at least half the '
             'activities devoted to culture. Camps can be residential for 6–14 nights '
             'or nonresidential for 5–15 days. Parent or guardian permission and '
             'individual camp availability apply. Registration follows camp-specific '
             'lead times; no universal absolute closing date is established. Organiser '
             'grants are not personal awards.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programa-ferias-em-movimento-oficina-cultural-com-candidaturas-abertas-para-jovens'},
 {'key': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-pascoa-2026-candidaturas-para-entidades-promotoras-ate-27-de-fevereiro',
  'title': 'Programa Férias em Movimento | Páscoa 2026 - Candidaturas para entidades '
           'promotoras até 27 de fevereiro',
  'url': 'https://ipdj.gov.pt/-/programa-ferias-em-movimento-pascoa-2026-candidaturas-para-entidades-promotoras-ate-27-de-fevereiro',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-02-27',
  'closing_local_clock': None,
  'summary': '2026 Easter Férias em Movimento organiser call. Eligible licensed '
             'nonprofit camp providers propose the announced educational camps under '
             "the edition's participant-age, programme and budget conditions. "
             'Organiser funding is not an unconditional payment to each child. '
             'Applications close February 27, 2026; individual camp registration and '
             'activity dates are separate.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programa-ferias-em-movimento-pascoa-2026-candidaturas-para-entidades-promotoras-ate-27-de-fevereiro'},
 {'key': 'https://ipdj.gov.pt/-/programa-nacional-de-desporto-para-todos-abertura-de-candidaturas-para-apoio-a-projetos-na-area-da-deficiencia-2025',
  'title': 'Programa Nacional de Desporto para Todos (PNDpT): abertura de candidaturas '
           'para apoio a projetos na área da deficiência',
  'url': 'https://ipdj.gov.pt/-/programa-nacional-de-desporto-para-todos-abertura-de-candidaturas-para-apoio-a-projetos-na-area-da-deficiencia-2025',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': '17:00',
  'summary': '2025 PNDpT disability-sport project support for eligible UPD '
             'federations. EUR 400,000 is the whole call pool, split between INR and '
             'IPDJ, not an individual award. One application and the governing '
             'project-cost exclusions apply. Closing May 16, 2025 at 17:00, time zone '
             'unstated. The possible 50% evaluation uplift changes scoring, not the '
             'cash grant.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programa-nacional-de-desporto-para-todos-abertura-de-candidaturas-para-apoio-a-projetos-na-area-da-deficiencia-2025'},
 {'key': 'https://ipdj.gov.pt/-/programa-nacional-de-desporto-para-todos-associativismo-candidaturas-abertas-ate-23-de-julho',
  'title': 'Programa Nacional de Desporto para Todos – Associativismo: candidaturas '
           'abertas até 23 de julho',
  'url': 'https://ipdj.gov.pt/-/programa-nacional-de-desporto-para-todos-associativismo-candidaturas-abertas-ate-23-de-julho',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': '17:00',
  'summary': '2025/26 PNDpT Associativismo institutional call for eligible nonprofit '
             'sports promoters. Community project funding comes from a EUR 1.5 million '
             'total pool; it is not a personal athlete stipend. Additional 2025/26 '
             'project limits and exclusions apply. Applications close July 23, 2025 at '
             '17:00, time zone unstated; activity runs July 2025–June 2026.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programa-nacional-de-desporto-para-todos-associativismo-candidaturas-abertas-ate-23-de-julho'},
 {'key': 'https://ipdj.gov.pt/-/programa-nacional-de-desporto-para-todos-candidaturas-para-apoio-a-projetos-na-area-da-deficiencia',
  'title': 'Programa Nacional de Desporto para Todos (PNDpT): candidaturas para apoio '
           'a projetos na área da deficiência',
  'url': 'https://ipdj.gov.pt/-/programa-nacional-de-desporto-para-todos-candidaturas-para-apoio-a-projetos-na-area-da-deficiencia',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': '17:00',
  'summary': '2026 PNDpT disability-sport funding for eligible UPD sports federations, '
             'with one application and whole-project budget below EUR 100,000. '
             'Normally at most 80% of eligible costs is funded, subject to authorised '
             'exceptions. EUR 366,500 is the total call pool, not a personal award. '
             'Closing April 16, 2026 at 17:00, time zone unstated. A possible 50% '
             'scoring uplift is not an extra cash grant.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programa-nacional-de-desporto-para-todos-candidaturas-para-apoio-a-projetos-na-area-da-deficiencia'},
 {'key': 'https://ipdj.gov.pt/-/programa-nacional-de-desporto-para-todos-pndpt-associativismo-2026-2027-candidaturas-abertas',
  'title': 'Programa Nacional de Desporto para Todos (PNDpT) - Associativismo '
           '2026-2027: candidaturas abertas',
  'url': 'https://ipdj.gov.pt/-/programa-nacional-de-desporto-para-todos-pndpt-associativismo-2026-2027-candidaturas-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': '17:00',
  'summary': 'PNDpT Associativismo 2026/27 supports nonprofit sport-promotion '
             'organisations’ mainland community projects. EUR 1.5 million is the whole '
             'call pool, not a personal award. Applications close May 6, 2026 at '
             '17:00, without a stated zone. The applicable general regulation is '
             'accessible, but the linked current deliberation returned 404; current '
             'edition-specific grant limits and additional conditions remain unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programa-nacional-de-desporto-para-todos-pndpt-associativismo-2026-2027-candidaturas-abertas'},
 {'key': 'https://ipdj.gov.pt/-/programa-novas-liderancas-para-um-desporto-igual-abre-inscricoes-para-a-4-edicao',
  'title': 'Programa "Novas Lideranças | Para um Desporto +Igual" abre inscrições para '
           'a 4.ª edição',
  'url': 'https://ipdj.gov.pt/-/programa-novas-liderancas-para-um-desporto-igual-abre-inscricoes-para-a-4-edicao',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-06-30',
  'closing_local_clock': None,
  'summary': 'Fourth Novas Lideranças course and mentoring programme for sports '
             'leaders aged at most 35, organised by COP. Twenty selected participants '
             'comprise ten women and ten men. September–November 2026 activities '
             'include two residential weekends and individual mentoring; 4.4 TPTD '
             'credits apply. Participants develop organisational gender-equality '
             'action plans. Applications close June 30, 2026. Fees and travel support '
             'are unstated.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programa-novas-liderancas-para-um-desporto-igual-abre-inscricoes-para-a-4-edicao'},
 {'key': 'https://ipdj.gov.pt/-/programa-otl-modalidade-de-longa-duracao-abre-candidaturas-2026',
  'title': 'Programa OTL - modalidade de «Longa Duração» abre candidaturas',
  'url': 'https://ipdj.gov.pt/-/programa-otl-modalidade-de-longa-duracao-abre-candidaturas-2026',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['internships'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'OTL long-duration practical community placements for ages 18–30, subject '
             'to the published experience and unemployment-protection conditions. The '
             'support is EUR 2.50/hour for 264–396 hours, totalling EUR 660–990, '
             'rather than a guaranteed employment salary. Participation is organised '
             'through an eligible partner entity. The 2026 notice and individual '
             'project determine timing; no universal absolute closing is inferred.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programa-otl-modalidade-de-longa-duracao-abre-candidaturas-2026'},
 {'key': 'https://ipdj.gov.pt/-/programas-de-apoio-ao-associativismo-jovem-paaj-com-candidaturas-abertas::paacj',
  'title': 'Programas de Apoio ao Associativismo Jovem (PAAJ): prorrogação de '
           'candidaturas até 11 de janeiro de 2026',
  'url': 'https://ipdj.gov.pt/-/programas-de-apoio-ao-associativismo-jovem-paaj-com-candidaturas-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-01-11',
  'closing_local_clock': None,
  'summary': '2026 PAACJ annual activity support for effective RNAJ youth-related '
             'associations; application extended to January 11, 2026. Institutional '
             'funding covers approved activity costs, with 30% own financing under the '
             'framework. Linked 2025 expenditure/scoring files do not establish all '
             'current 2026 conditions. No personal stipend.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programas-de-apoio-ao-associativismo-jovem-paaj-com-candidaturas-abertas'},
 {'key': 'https://ipdj.gov.pt/-/programas-de-apoio-ao-associativismo-jovem-paaj-com-candidaturas-abertas::pae',
  'title': 'Programas de Apoio ao Associativismo Jovem (PAAJ): prorrogação de '
           'candidaturas até 11 de janeiro de 2026',
  'url': 'https://ipdj.gov.pt/-/programas-de-apoio-ao-associativismo-jovem-paaj-com-candidaturas-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-01-11',
  'closing_local_clock': None,
  'summary': '2026 PAE annual activity support for effective RNAJ higher-education '
             'student associations and federations; application extended to January '
             '11, 2026. Institutional funding covers approved activity costs, with 30% '
             'own financing under the framework. Linked 2025 expenditure/scoring files '
             'do not establish all current 2026 conditions. No personal stipend.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programas-de-apoio-ao-associativismo-jovem-paaj-com-candidaturas-abertas'},
 {'key': 'https://ipdj.gov.pt/-/programas-de-apoio-ao-associativismo-jovem-paaj-com-candidaturas-abertas::pai',
  'title': 'Programas de Apoio ao Associativismo Jovem (PAAJ): prorrogação de '
           'candidaturas até 11 de janeiro de 2026',
  'url': 'https://ipdj.gov.pt/-/programas-de-apoio-ao-associativismo-jovem-paaj-com-candidaturas-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-01-11',
  'closing_local_clock': None,
  'summary': '2026 PAI infrastructure/equipment support for effective RNAJ '
             'associations; application extended to January 11, 2026. Framework caps '
             'are EUR 50,000/year for infrastructure and EUR 2,500/year for equipment, '
             'with eligibility and cofinancing conditions. Linked 2025 files do not '
             'certify all 2026 rules; approved institutional costs, not personal '
             'awards.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programas-de-apoio-ao-associativismo-jovem-paaj-com-candidaturas-abertas'},
 {'key': 'https://ipdj.gov.pt/-/programas-de-apoio-ao-associativismo-jovem-paaj-com-candidaturas-abertas::paj',
  'title': 'Programas de Apoio ao Associativismo Jovem (PAAJ): prorrogação de '
           'candidaturas até 11 de janeiro de 2026',
  'url': 'https://ipdj.gov.pt/-/programas-de-apoio-ao-associativismo-jovem-paaj-com-candidaturas-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-01-11',
  'closing_local_clock': None,
  'summary': '2026 PAJ annual activity support for effective RNAJ youth associations '
             'and federations; application extended to January 11, 2026. Institutional '
             'funding covers approved activity costs, with 30% own financing under the '
             'framework. The linked 2025 expenditure/scoring files do not establish '
             'all current 2026 conditions. No personal stipend.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/programas-de-apoio-ao-associativismo-jovem-paaj-com-candidaturas-abertas'},
 {'key': 'https://ipdj.gov.pt/-/prorrogado-o-prazo-para-apresentacao-de-candidaturas-ao-programa-voluntariado-jovem-para-a-natureza-e-florestas-envolve-te-in',
  'title': 'Prorrogado o prazo para apresentação de candidaturas ao Programa '
           'Voluntariado Jovem para a Natureza e Florestas (Envolve-te In)',
  'url': 'https://ipdj.gov.pt/-/prorrogado-o-prazo-para-apresentacao-de-candidaturas-ao-programa-voluntariado-jovem-para-a-natureza-e-florestas-envolve-te-in',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-10-30',
  'closing_local_clock': None,
  'summary': '2026 environmental-volunteering project support for eligible public or '
             'nonprofit promoters, with volunteers aged 14–30. Conditional support '
             'covers expense reimbursement of EUR 13/day through the entity or '
             'directly for public promoters, plus applicable management and '
             'accommodation costs; this is not a wage. Project applications close '
             'October 30; the explicit extension allows activity through November 30, '
             '2026. Required lead times and parental permission apply.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/prorrogado-o-prazo-para-apresentacao-de-candidaturas-ao-programa-voluntariado-jovem-para-a-natureza-e-florestas-envolve-te-in'},
 {'key': 'https://ipdj.gov.pt/-/segunda-edicao-premio-nacional-da-igualdade-de-genero-no-desporto-desporto-mais-igual-abertura-de-candidaturas',
  'title': '2.ª Edição do Prémio Nacional da Igualdade de Género no Desporto «Desporto '
           '+ Igual» 2025: Abertura de candidaturas',
  'url': 'https://ipdj.gov.pt/-/segunda-edicao-premio-nacional-da-igualdade-de-genero-no-desporto-desporto-mais-igual-abertura-de-candidaturas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-06-30',
  'closing_local_clock': None,
  'summary': '2025 Desporto Mais Igual institutional prize for eligible organisations '
             'promoting gender equality in sport. The three winning project awards are '
             'EUR 6,000, 3,000 and 1,000, not grants to all participants. Eligibility '
             'and exclusion conditions in the own regulation apply. Applications close '
             'June 30, 2025; prize-ceremony dates are separate.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/segunda-edicao-premio-nacional-da-igualdade-de-genero-no-desporto-desporto-mais-igual-abertura-de-candidaturas'},
 {'key': 'https://ipdj.gov.pt/-/seminario-internacional-da-formacao-a-competicao-como-conciliar',
  'title': 'Seminário Internacional "Da formação à competição, como conciliar?"',
  'url': 'https://ipdj.gov.pt/-/seminario-internacional-da-formacao-a-competicao-como-conciliar',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'International seminar on reconciling training and competition, with its '
             'own announced educational programme and access process in 2025. '
             'Registration is distinct from athlete funding or a job application. '
             'Costs, financial support and registration closing are not established '
             'beyond the own notice.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/seminario-internacional-da-formacao-a-competicao-como-conciliar'},
 {'key': 'https://ipdj.gov.pt/-/seminario-lideranca-do-desporto-no-feminino-destaca-igualdade-de-genero-e-boas-praticas-no-setor',
  'title': 'Seminário «Liderança do Desporto no Feminino» destaca igualdade de género '
           'e boas práticas no setor',
  'url': 'https://ipdj.gov.pt/-/seminario-lideranca-do-desporto-no-feminino-destaca-igualdade-de-genero-e-boas-praticas-no-setor',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': "Seminar on women's leadership in sport, equality and good practice, with "
             'the published thematic programme. Participation is an educational event, '
             'distinct from the accompanying institutional prize ceremony. Access and '
             'any fee or assistance follow the own announcement; registration closing '
             'is not stated.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/seminario-lideranca-do-desporto-no-feminino-destaca-igualdade-de-genero-e-boas-praticas-no-setor'},
 {'key': 'https://ipdj.gov.pt/-/simposio-clube-top-digitalizacao-de-clubes-desportivos-mais-do-que-o-futuro-o-presente',
  'title': 'Simpósio Clube Top: «Digitalização de Clubes Desportivos - Mais do que o '
           'futuro, o presente»',
  'url': 'https://ipdj.gov.pt/-/simposio-clube-top-digitalizacao-de-clubes-desportivos-mais-do-que-o-futuro-o-presente',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Free hybrid Clube Top symposium on digitalisation of sports clubs in '
             '2025, addressing practical innovation and management. Participants '
             'choose the stated in-person or online access route. Registration is '
             'required; closing and financial assistance are not stated. The event is '
             'distinct from institutional merit awards.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/simposio-clube-top-digitalizacao-de-clubes-desportivos-mais-do-que-o-futuro-o-presente'},
 {'key': 'https://ipdj.gov.pt/-/sport-for-all-training-workshop-candidaturas-a-decorrer',
  'title': '«Sport For All training workshop»: candidaturas a decorrer',
  'url': 'https://ipdj.gov.pt/-/sport-for-all-training-workshop-candidaturas-a-decorrer',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': '23:59',
  'summary': 'Sport For All workshop for inclusive-sport professionals, teachers, '
             'volunteers and other relevant practitioners, selected by the Council of '
             'Europe team. Applications close February 26, 2026 at 23:59 without a '
             'stated zone; the notice’s weekday is inconsistent. The applicant notice '
             'advertises April 20–24 in Lisbon, while later coverage describes April '
             '21–23. Fees and travel support are unstated; this is one workshop.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/sport-for-all-training-workshop-candidaturas-a-decorrer'},
 {'key': 'https://ipdj.gov.pt/-/unidade-apoio-formativo-associativismo-jovem-candidaturas-abertas',
  'title': 'Unidade de Apoio Formativo ao Associativismo Jovem 2026 já tem '
           'candidaturas abertas',
  'url': 'https://ipdj.gov.pt/-/unidade-apoio-formativo-associativismo-jovem-candidaturas-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-09-29',
  'closing_local_clock': None,
  'summary': '2026 training-plan funding for effective RNAJ youth associations and '
             'federations, including consortia submitted by one entity. Actions '
             'require 15–20 learners; the grant is limited to EUR 3,000 per plan and '
             'EUR 1,000 per action, with at least 30% cofinancing. Complete both '
             'application stages. Closing September 29; activities must finish '
             'December 31, 2026. Published scoring weights conflict between the order '
             'and annex.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/unidade-apoio-formativo-associativismo-jovem-candidaturas-abertas'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-4-edicao-da-porto-design-biennale-2025',
  'title': 'Voluntariado Jovem | 4.ª Edição da Porto Design Biennale 2025',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-4-edicao-da-porto-design-biennale-2025',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-10-10',
  'closing_local_clock': None,
  'summary': 'Porto Design Biennale volunteering, October 25–December 21, 2025, '
             'Porto/Matosinhos. Thirty-five places for ages 18–30 with cultural '
             'interest, good English and availability. Compulsory two-hour training '
             'October 17 at 15:00 in Matosinhos. Tote bag, clothing/identification, '
             'accident/liability insurance and EUR 13/day expense reimbursement '
             'offered. Closing October 10, 2025.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-4-edicao-da-porto-design-biennale-2025'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-dia-de-portugal-cidade-do-futebol-inscricoes-abertas',
  'title': 'Voluntariado Jovem «Dia de Portugal + | Cidade do Futebol»: inscrições '
           'abertas',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-dia-de-portugal-cidade-do-futebol-inscricoes-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'FPF event volunteering at Cidade do Futebol on August 22, 2026, '
             '10:00–15:00. Applicants aged 16–30 need full availability, prior FPF '
             'participation and compulsory August 19 training. Forty places are '
             'capacity, not separate offers. Support includes insurance, '
             'identification clothing, a snack, certificate and EUR 13 expense '
             'reimbursement; this is not a wage. Registration closing is not stated.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-dia-de-portugal-cidade-do-futebol-inscricoes-abertas'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-direitoaterdireitos-impact-iii',
  'title': 'Voluntariado jovem « #DIREITOATERDIREITOS - Impact III»',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-direitoaterdireitos-impact-iii',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Impact III human-rights volunteering for ages 14–30 in 2025. '
             "Participants work four or five hours/day under the project's training "
             'and activity conditions. Conditional EUR 13/day reimburses expenses, not '
             'a salary. December 15 is the activity end, not an application closing '
             'date; registration depends on the project and places.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-direitoaterdireitos-impact-iii'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-futebol-portugal-chile-candidaturas-abertas',
  'title': 'Voluntariado Jovem, futebol - Portugal vs Chile: candidaturas abertas',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-futebol-portugal-chile-candidaturas-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'FPF volunteering at Portugal–Chile, June 6, 2026, Jamor stadium. '
             'Applicants aged 17–30 must live nearby and attend June 5 (10–12 and '
             '14–17) and June 6 (15–20). Help with access, protocol, media, ticketing, '
             'accreditation and fans. T-shirt/identification, accident/liability '
             'insurance, certificate, EUR 13 reimbursement and snack provided. Closing '
             'unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-futebol-portugal-chile-candidaturas-abertas'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-futebol-portugal-nigeria-candidaturas-abertas',
  'title': 'Voluntariado Jovem: futebol - Portugal vs Nigéria: candidaturas abertas',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-futebol-portugal-nigeria-candidaturas-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'FPF volunteering at Portugal–Nigeria, June 10, 2026, Leiria stadium, '
             'with attendance also June 9. Applicants aged 17–30 must live nearby and '
             'be available both days. Help with access, protocol, media, ticketing, '
             'accreditation and fans. T-shirt/identification, accident/liability '
             'insurance, certificate, EUR 13 expense reimbursement and snack are '
             'provided. Closing unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-futebol-portugal-nigeria-candidaturas-abertas'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-futebol-portugal-vs-eslovaquia-candidaturas-abertas',
  'title': 'Voluntariado jovem, futebol - Portugal vs Eslováquia: candidaturas abertas',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-futebol-portugal-vs-eslovaquia-candidaturas-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'FPF volunteering at Portugal–Slovakia women’s match, March 7, 2026, '
             'Barcelos stadium. Applicants aged 16–30 need availability and compulsory '
             'volunteering/specific training. Support protocol, access and fan areas. '
             'Accident/liability insurance, certificate, training and EUR 13 '
             'transport-expense reimbursement provided. First-time applicants register '
             'with IPDJ. Closing unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-futebol-portugal-vs-eslovaquia-candidaturas-abertas'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-ikf-beach-korfball-world-cup-europe-2026-candidaturas-abertas',
  'title': 'Voluntariado Jovem-IKF Beach Korfball World Cup Europe 2026: candidaturas '
           'abertas',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-ikf-beach-korfball-world-cup-europe-2026-candidaturas-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Beach Korfball volunteering in Costa da Caparica for ages 18–30 residing '
             'near the event. The notice says July 11–12, 2026 for the event but June '
             '11–12 for required availability; the exact participation dates conflict. '
             'Support includes accident/liability insurance, food, kit and '
             'certificate, not wages. Seventy-two places are capacity. Duties support '
             'teams and the field. Registration closing is unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-ikf-beach-korfball-world-cup-europe-2026-candidaturas-abertas'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-inscricoes-abertas',
  'title': 'Voluntariado jovem: inscrições abertas para o «Envolve-te - Agora Nós»',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-inscricoes-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Envolve-te — Agora Nós youth-volunteering framework, with 110 projects '
             'announced across Portugal in sport, social inclusion and cultural '
             'exchange. Activities and participation periods vary by individual '
             'project, which has its own closing dates and criteria. The own notice '
             'directs applicants to the project catalogue; it does not establish one '
             'universal age limit, benefit package or application closing.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-inscricoes-abertas'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-mundial-de-superbikes-2026-candidaturas-abertas',
  'title': 'Voluntariado jovem para o «Mundial de Superbikes 2026»: candidaturas '
           'abertas',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-mundial-de-superbikes-2026-candidaturas-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'World Superbikes volunteering, March 27–29, 2026, Algarve circuit, '
             'Portimão. Applicants aged 16–30 need availability and must attend '
             'volunteering training. Duties include public information and access '
             'control. Accident/liability insurance, certificate, general/specific '
             'training and food are provided; no salary or housing stated. Closing '
             'unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-mundial-de-superbikes-2026-candidaturas-abertas'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-na-organizacao-do-futebol-profissional-portugues-inscricoes-abertas',
  'title': 'Voluntariado jovem na organização do futebol profissional português: '
           'inscrições abertas',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-na-organizacao-do-futebol-profissional-portugues-inscricoes-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Liga Portugal volunteer pool for people aged 18–30, assigned to '
             'participating football stadiums near their residence, including listed '
             'island venues. Duties include visitor assistance, access, logistics and '
             'communication. Selection requires online training. Support includes '
             'accident insurance, food, official equipment and a certificate; no wage '
             'is stated. Registration closing and match-by-match availability are '
             'unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-na-organizacao-do-futebol-profissional-portugues-inscricoes-abertas'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-na-qualifica-2026-candidaturas-abertas',
  'title': 'Voluntariado Jovem na Qualifica 2026: candidaturas abertas',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-na-qualifica-2026-candidaturas-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Volunteer at IPDJ’s Qualifica stand, March 25–28, 2026, Exponor. '
             'Applicants aged 16–30 need communication, responsibility, flexibility, '
             'interpersonal/team skills and interest in sport/leisure. Volunteering '
             'preparation, accident/liability insurance and EUR 13 reimbursement for '
             'food/transport expenses provided. Registration closing unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-na-qualifica-2026-candidaturas-abertas'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-no-dia-do-desporto-inclusivo-24-de-setembro-jamor',
  'title': 'Voluntariado Jovem no Dia do Desporto Inclusivo | 24 de setembro – JAMOR',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-no-dia-do-desporto-inclusivo-24-de-setembro-jamor',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Inclusive Sport Day volunteering at Jamor on September 24, 2026, '
             '08:30–13:30, for ages 18–30 with full availability. Ten places are '
             'capacity. Mandatory IPDJ and sport-specific preparation applies. Support '
             'includes identification, accident/liability insurance, certificate and '
             'EUR 13 expense reimbursement, not a wage. Registration closing is not '
             'stated.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-no-dia-do-desporto-inclusivo-24-de-setembro-jamor'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-no-festival-do-crato',
  'title': 'Voluntariado Jovem no «Festival do Crato»',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-no-festival-do-crato',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Volunteer at IPDJ’s Festival do Crato stand, August 26–29, 2026. Ages '
             '16–30, motivation, initiative, responsibility and availability apply. '
             'Duties include explaining youth programmes, helping activities and '
             'digital communication. The short own notice does not state financial '
             'support, accommodation or travel coverage. Registration closing is '
             'unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-no-festival-do-crato'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-no-festival-sol-da-caparica-2025',
  'title': 'Voluntariado Jovem no «Festival Sol da Caparica»',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-no-festival-sol-da-caparica-2025',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Volunteer at IPDJ’s Sol da Caparica festival stand, August 14–17, 2025. '
             'Applicants aged 16–30 need motivation, initiative, responsibility and '
             'availability. Promote IPDJ programmes, support stand activities and '
             'digital communication. This own notice does not specify insurance, '
             'financial support or registration closing.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-no-festival-sol-da-caparica-2025'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-no-formula-kite-world-championship-2026-candidaturas-abertas',
  'title': 'Voluntariado Jovem no «Formula Kite World Championship 2026»: candidaturas '
           'abertas',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-no-formula-kite-world-championship-2026-candidaturas-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Formula Kite World Championship volunteering, May 9–16, 2026, Viana do '
             'Castelo. Applicants aged 14–30 need English, communication skills, '
             'interest in nautical sports, responsibility and initiative. Ten places '
             'involve beach cleanup; 20 support event logistics/access. T-shirt, food, '
             'insurance and certificate provided. Registration closing unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-no-formula-kite-world-championship-2026-candidaturas-abertas'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-no-grande-pr%C3%A9mio-de-portugal-de-motogp-2026',
  'title': 'Voluntariado Jovem no «Grande Prémio de Portugal de MotoGP 2026»',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-no-grande-pr%C3%A9mio-de-portugal-de-motogp-2026',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Volunteering at the Portuguese MotoGP Grand Prix, November 20–22, 2026, '
             'Autódromo Internacional do Algarve. Young people 16–30 assist the '
             'public. Certificate, accident/liability insurance, food and local '
             'Portimão–circuit transport are provided; no salary, housing or '
             'international travel support is stated. BDU registration and account '
             'activation are required. Registration closing unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-no-grande-pr%C3%A9mio-de-portugal-de-motogp-2026'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-no-portugal-football-summit-cidade-do-futebol',
  'title': 'Candidaturas Voluntariado Jovem no «Portugal Football Summit», Cidade do '
           'Futebol',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-no-portugal-football-summit-cidade-do-futebol',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'FPF volunteering at Portugal Football Summit in Cidade do Futebol, '
             'September 22–25, 2026. Ages 16–30, full availability, previous FPF '
             'participation and compulsory preparation apply. Twenty-seven places are '
             'capacity. Support includes identification clothing, accident/liability '
             'insurance, certificate, EUR 13 expense reimbursement and a snack; no '
             'wage is offered. Registration closing is not stated.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-no-portugal-football-summit-cidade-do-futebol'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-no-stand-da-lisboa-games-week-candidaturas-abertas',
  'title': 'Voluntariado jovem no stand IPDJ da Lisboa Games Week: candidaturas '
           'abertas',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-no-stand-da-lisboa-games-week-candidaturas-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Volunteer at IPDJ’s Lisboa Games Week stand, November 20–23, 2025, FIL. '
             'Applicants aged 16–30 must reside in Greater Lisbon and be available '
             'during the event. Help deliver youth/sport activities and digital-safety '
             'information. This own notice does not specify financial support or '
             'registration closing.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-no-stand-da-lisboa-games-week-candidaturas-abertas'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-para-natureza-florestas-inscricoes-abertas-jovens',
  'title': 'Voluntariado Jovem para a Natureza e Florestas: Inscrições abertas para '
           'jovens',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-para-natureza-florestas-inscricoes-abertas-jovens',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 Natureza e Florestas volunteering for Portugal residents aged 14–30 '
             'in approved mainland environmental projects. Minors require guardian '
             'permission and participants must not have the stated environmental or '
             'forest-offence impediment. Projects include ecological awareness, '
             'restoration and monitoring; work is limited to five hours/day. '
             'Conditional EUR 13/day reimburses expenses, not wages. Register at least '
             'five days before the selected project starts, subject to vacancies; no '
             'universal absolute closing.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-para-natureza-florestas-inscricoes-abertas-jovens'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-portugal-football-summit-candidaturas-abertas',
  'title': 'Voluntariado jovem no «Portugal Football Summit»: candidaturas abertas',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-portugal-football-summit-candidaturas-abertas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Portugal Football Summit volunteering, October 7–11, 2025, Oeiras. '
             'Applicants aged 18–30 need availability, nearby residence, prior '
             'sports-volunteering experience, English and responsibility. Assist '
             'protocol/VIP, ticketing/welcome, organisation and operations. Training, '
             'food, insurance and identification offered. Registration closing '
             'unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-portugal-football-summit-candidaturas-abertas'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-portugal-republica-da-irlanda-e-portugal-hungria::hungary-oct14',
  'title': 'Voluntariado Jovem: Portugal vs República da Irlanda e Portugal vs Hungria',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-portugal-republica-da-irlanda-e-portugal-hungria',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'FPF volunteering at Portugal–Hungary, October 14, 2025, José Alvalade '
             'stadium, Lisbon. Applicants 18–30 must live nearby, have prior '
             'sports-volunteering experience, responsibility and full availability. '
             'Training, food, insurance and identification are provided; '
             'salary/housing not stated. Registration closing unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-portugal-republica-da-irlanda-e-portugal-hungria'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-portugal-republica-da-irlanda-e-portugal-hungria::ireland-oct11',
  'title': 'Voluntariado Jovem: Portugal vs República da Irlanda e Portugal vs Hungria',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-portugal-republica-da-irlanda-e-portugal-hungria',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'FPF volunteering at Portugal–Republic of Ireland, October 11, 2025, José '
             'Alvalade stadium, Lisbon. Applicants 18–30 must live nearby, have prior '
             'sports-volunteering experience, responsibility and full availability. '
             'Training, food, insurance and identification are provided; '
             'salary/housing not stated. Registration closing unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-portugal-republica-da-irlanda-e-portugal-hungria'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-portugal-vs-armenia',
  'title': 'Voluntariado Jovem: Portugal vs Arménia',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-portugal-vs-armenia',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'FPF volunteering at Portugal–Armenia, November 16, 2025, Dragão stadium, '
             'Porto. Applicants aged 18–30 need English, availability, communication '
             'skills, responsibility and interest in football. Sixty places announced. '
             'T-shirt/vest, identification, snack, insurance, EUR 13 expense '
             'reimbursement and certificate provided. Registration closing unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-portugal-vs-armenia'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-portugal-vs-pa%C3%ADses-baixos',
  'title': 'Voluntariado Jovem no Jogo de preparação da Seleção A Feminina - Portugal '
           'vs Países Baixos, em Braga',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-portugal-vs-pa%C3%ADses-baixos',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'FPF volunteering at the women’s Portugal–Netherlands preparation match, '
             'November 28, 2025, Braga stadium. Applicants aged 18–30 need English, '
             'availability, communication skills and responsibility. Assist access, '
             'ticketing, media, fans and accreditation. T-shirt, identification, '
             'snack, preparation and participation allowance offered; allowance '
             'amount/closing unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-portugal-vs-pa%C3%ADses-baixos'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-portugal-vs-pais-de-gales-no-estadio-jose-alvalade',
  'title': 'Voluntariado jovem | Portugal vs País de Gales, no Estádio José Alvalade',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-portugal-vs-pais-de-gales-no-estadio-jose-alvalade',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'FPF volunteering for Portugal–Wales at Estádio José Alvalade, September '
             '23–24, 2026. Ages 17–30, full availability, previous FPF participation '
             'and compulsory preparation apply. Fifty-five places are capacity. '
             'Support includes clothing, accident/liability insurance, certificate, '
             'EUR 13 expense reimbursement and a snack; no wage is offered. '
             'Registration closing is not stated.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-portugal-vs-pais-de-gales-no-estadio-jose-alvalade'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-jovem-semana-no-beactive-sport-village-setembro-jamor',
  'title': 'Voluntariado Jovem no BeACTIVE Sports Village | 26 setembro - JAMOR',
  'url': 'https://ipdj.gov.pt/-/voluntariado-jovem-semana-no-beactive-sport-village-setembro-jamor',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Jamor BeACTIVE Sports Village volunteering on September 26, 2026, for '
             'ages 18–30 with full shift availability. Twenty places span two '
             'five-hour shifts. Mandatory IPDJ and sport-specific preparation applies. '
             'Support includes identification, accident/liability insurance, '
             'certificate and EUR 13 expense reimbursement, not a wage. Registration '
             'closing is not stated.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-jovem-semana-no-beactive-sport-village-setembro-jamor'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-motogp-algarve',
  'title': 'Voluntariado Jovem no Grande Prémio de Portugal de MotoGP de 2025',
  'url': 'https://ipdj.gov.pt/-/voluntariado-motogp-algarve',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Volunteer at the 2025 MotoGP Portuguese Grand Prix in Portimão, November '
             '7–9. Ages 16–30 and full availability apply. Support includes food, '
             'accident insurance and a participation certificate; no wage, '
             'accommodation or international travel is promised. The three event days '
             'are not application closing dates.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-motogp-algarve'},
 {'key': 'https://ipdj.gov.pt/-/voluntariado-navegas-em-seguranca-candidaturas-abertas-2026',
  'title': 'Voluntariado jovem «Naveg@s em Segurança?» - candidaturas abertas: '
           '«Envolve-te» na segurança digital',
  'url': 'https://ipdj.gov.pt/-/voluntariado-navegas-em-seguranca-candidaturas-abertas-2026',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Naveg@s em Segurança digital-safety volunteering for ages 16–30 with '
             'topic knowledge, communication skills and availability. Approximately '
             '60-minute sessions online or usually in person; compulsory '
             'self-study/online training. EUR 13 per participation/preparation day, '
             'preparation limited to three days/month; maximum four sessions/day. '
             'Accident/liability insurance and certificate included. Apply in '
             'residence district; first-time IPDJ registration required. Closing '
             'unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/voluntariado-navegas-em-seguranca-candidaturas-abertas-2026'},
 {'key': 'https://ipdj.gov.pt/-/webinar-a-integridade-no-desporto-na-cplp',
  'title': 'Webinar «A Integridade no Desporto na CPLP»: incrições a decorrer',
  'url': 'https://ipdj.gov.pt/-/webinar-a-integridade-no-desporto-na-cplp',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Free online CPLP sports-integrity webinar, May 14, 2025, 11:00 '
             'Portuguese time. Youth/athletes, policymakers, sports managers/staff, '
             'clubs, civil-society specialists and public/international bodies explore '
             'ethics, corruption, competition manipulation and good practice. '
             'Registration compulsory; closing unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/webinar-a-integridade-no-desporto-na-cplp'},
 {'key': 'https://ipdj.gov.pt/-/webinar-bandeira-da-etica-submissao-de-candidaturas-e-o-novo-regulamento',
  'title': 'Webinar «Bandeira da Ética - submissão de candidaturas e o novo '
           'regulamento»',
  'url': 'https://ipdj.gov.pt/-/webinar-bandeira-da-etica-submissao-de-candidaturas-e-o-novo-regulamento',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 webinar on Bandeira da Ética applications and the revised '
             'regulation. Participants learn the procedure through the announced '
             'online session. The webinar is an educational opportunity distinct from '
             'the certification itself; no personal funding is offered. Registration '
             'closing is not stated beyond the notice.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/webinar-bandeira-da-etica-submissao-de-candidaturas-e-o-novo-regulamento'},
 {'key': 'https://ipdj.gov.pt/-/webinar-campos-de-trabalho-internacionais-sessao-de-esclarecimentos',
  'title': 'Webinar «Campos de Trabalho Internacionais – Sessão de esclarecimentos»',
  'url': 'https://ipdj.gov.pt/-/webinar-campos-de-trabalho-internacionais-sessao-de-esclarecimentos',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-12-11',
  'closing_local_clock': None,
  'summary': 'Online clarification webinar for prospective international-workcamp '
             'organising entities on December 15, 2025. Municipalities are explicitly '
             'excluded from the eligible organiser audience. Registration closes '
             'December 11, 2025. The session explains the project procedure; it is not '
             'a separate financial grant.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/webinar-campos-de-trabalho-internacionais-sessao-de-esclarecimentos'},
 {'key': 'https://ipdj.gov.pt/-/webinar-diplomacia-desportiva-lusofonia',
  'title': 'IPDJ promove webinar “Sabia que?” dedicado ao papel do desporto na CPLP',
  'url': 'https://ipdj.gov.pt/-/webinar-diplomacia-desportiva-lusofonia',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Free online Sabia que? webinar on the role of sport in the CPLP, '
             'December 17, 2025. The named learning session is open through the '
             'announced registration process. No salary or scholarship is offered. '
             'Registration closing is not stated; the event date is not a deadline.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/webinar-diplomacia-desportiva-lusofonia'},
 {'key': 'https://ipdj.gov.pt/-/webinar-europeu-media-representation-of-persons-with-disabilities-in-sport',
  'title': 'Webinar europeu «Media representation of persons with disabilities in '
           'sport»',
  'url': 'https://ipdj.gov.pt/-/webinar-europeu-media-representation-of-persons-with-disabilities-in-sport',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Free English-language webinar on media representation of disabled people '
             'in sport, March 24, 2026, 10:00–12:20 CEST as printed. Journalists, '
             'researchers, sport organisations and athletes discuss ethical narratives '
             'and inclusive practice. Automatic transcription and later video access '
             'are offered; registration is required. Registration closing is not '
             'stated; the published session zone is not an application deadline.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/webinar-europeu-media-representation-of-persons-with-disabilities-in-sport'},
 {'key': 'https://ipdj.gov.pt/-/webinar-formacao-e-desenvolvimento-de-treinadores-arbitros-e-agentes-do-desporto-paralimpico',
  'title': 'Webinar «Formação e desenvolvimento de treinadores, árbitros e agentes do '
           'desporto paralímpico»',
  'url': 'https://ipdj.gov.pt/-/webinar-formacao-e-desenvolvimento-de-treinadores-arbitros-e-agentes-do-desporto-paralimpico',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Free English-language online webinar on developing Paralympic coaches, '
             'referees and sports professionals, October 14, 2025, 10–12:20 CEST. '
             'Sports-organisation staff learn European good practices, research and '
             'inclusion strategies. Live automatic transcription and later video '
             'access offered. Registration required; closing unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/webinar-formacao-e-desenvolvimento-de-treinadores-arbitros-e-agentes-do-desporto-paralimpico'},
 {'key': 'https://ipdj.gov.pt/-/webinar-sessao-de-apresentacao-da-11-edicao-da-semana-europeia-do-desporto-sed-2025',
  'title': 'Webinar «Sessão de apresentação da 11.ª Edição da Semana Europeia do '
           'Desporto (SED 2025)»',
  'url': 'https://ipdj.gov.pt/-/webinar-sessao-de-apresentacao-da-11-edicao-da-semana-europeia-do-desporto-sed-2025',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 webinar introducing the eleventh European Week of Sport and its '
             'implementation. The named session offers educational preparation for '
             'relevant organisers rather than a project grant. Registration follows '
             'the notice; fees, assistance and registration closing are not '
             'established beyond its stated facts.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/webinar-sessao-de-apresentacao-da-11-edicao-da-semana-europeia-do-desporto-sed-2025'},
 {'key': 'https://ipdj.gov.pt/-/webinares-licenciamento-instalacoes-desportivas-enfoque-adaptados::clubs-associations',
  'title': 'Webinares sobre Licenciamento de Instalações Desportivas | Enfoque '
           'adaptados para municípios, clubes e associações',
  'url': 'https://ipdj.gov.pt/-/webinares-licenciamento-instalacoes-desportivas-enfoque-adaptados',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Free online sports-facility licensing seminar for clubs and nonprofit '
             'associations, September 11, 2025,18:00, two hours. Ana Cláudia Guedes '
             'covers legal regularisation and duties. Registration is compulsory; '
             'closing unknown. Historical educational event, not a personal grant.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/webinares-licenciamento-instalacoes-desportivas-enfoque-adaptados'},
 {'key': 'https://ipdj.gov.pt/-/webinares-licenciamento-instalacoes-desportivas-enfoque-adaptados::municipalities',
  'title': 'Webinares sobre Licenciamento de Instalações Desportivas | Enfoque '
           'adaptados para municípios, clubes e associações',
  'url': 'https://ipdj.gov.pt/-/webinares-licenciamento-instalacoes-desportivas-enfoque-adaptados',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Free online sports-facility licensing seminar for municipal technicians '
             'and councillors, September 8, 2025,14:00, four hours. Fernanda Paula '
             'Oliveira covers legal procedures and inspection. Registration is '
             'compulsory; closing unknown. Historical educational event, not a '
             'personal grant.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/webinares-licenciamento-instalacoes-desportivas-enfoque-adaptados'},
 {'key': 'https://ipdj.gov.pt/-/youth-summit-2025-o-maior-encontro-nacional-de-capacitacao-e-participacao-juvenil',
  'title': 'IPDJ promove «Youth Summit», o maior encontro nacional de capacitação e '
           'participação juvenil',
  'url': 'https://ipdj.gov.pt/-/youth-summit-2025-o-maior-encontro-nacional-de-capacitacao-e-participacao-juvenil',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Youth Summit 2025 in Póvoa de Varzim for ages 16–30 offers '
             'youth-participation training, workshops and exchange. Registration and '
             'capacity conditions apply; Friday morning is reserved for institutional '
             'participants. The detailed notice offers free accommodation and food '
             'under the stated arrangements. Activity dates and the already closed '
             'registration are distinct; no personal cash award is offered.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/youth-summit-2025-o-maior-encontro-nacional-de-capacitacao-e-participacao-juvenil'},
 {'key': 'https://ipdj.gov.pt/-/youth-summit-2026-o-futuro-nao-espera',
  'title': 'Youth Summit 2026: O futuro não espera. Participar é transformar!',
  'url': 'https://ipdj.gov.pt/-/youth-summit-2026-o-futuro-nao-espera',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Two-day youth participation and nonformal-learning event in Santarém, '
             'November 13–14, 2026. Young people, association leaders and municipal '
             'youth professionals can join practical workshops and discussion tracks '
             'on civic participation, inclusion, digital literacy, wellbeing and '
             'sustainability. Registration is required; fees and lodging support are '
             'not stated. Registration closing is unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/-/youth-summit-2026-o-futuro-nao-espera'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123::PT-AV-04-25::2025::::::',
  'title': 'EcoAction QEM',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Oliveirinha, Aveiro',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 IPDJ workcamp for ages 18–30 in Oliveirinha, Aveiro, July 15–26. '
             'Restore forest trails, ponds and habitats. Shared mattresses and '
             'provided meals with vegetarian option; own sleeping bag. English '
             'preferred, Portuguese/Spanish also mentioned. The brochure states no '
             'fees and accident insurance. No personal stipend or international-travel '
             'reimbursement is promised.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123::PT-AV-05-25::2025::::::',
  'title': 'AVANCA 2025',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Avanca',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 IPDJ workcamp for ages 18–30 in Avanca, July 18–29. Film-festival '
             'preparation, guest support and cleanup. School-classroom mattresses and '
             'meals; own sleeping bag. English and permission before leaving camp '
             'apply. The brochure states no fees and accident insurance. No personal '
             'stipend or international-travel reimbursement is promised.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123::PT-AV-08-25::2025::::::',
  'title': 'EcoFrossos',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Frossos, Albergaria-a-Velha',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 IPDJ workcamp for ages 18–30 in Frossos, Albergaria-a-Velha, '
             'September 2–13. Habitat/trail restoration and nature learning. Shared '
             'beds and meals with vegetarian option; own sleeping bag. English '
             'preferred, Portuguese/Spanish also mentioned. The brochure states no '
             'fees and accident insurance. No personal stipend or international-travel '
             'reimbursement is promised.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123::PT-BG-18-25::2025::::::',
  'title': 'United by Donkeys III',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Miranda do Douro',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 IPDJ workcamp for ages 18–30 in Miranda do Douro, July 21–August 1. '
             'Donkey welfare, shelter and stone-wall work. Shared bunk-room lodging or '
             'own tent, English and local pickup. Meals are not explicit in the '
             'detailed section; the general CTI manual states food provision. The '
             'brochure states no fees and accident insurance. No personal stipend or '
             'international-travel reimbursement is promised.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123::PT-BG-19-25::2025::::::',
  'title': 'Nature Ambassadors III',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Espinhosela, Montesinho',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 IPDJ workcamp for ages 18–30 in Espinhosela, Montesinho, August '
             '18–29. Wildlife coexistence, signs and community work. Renovated-school '
             'lodging, meals and rotating cooking; Portuguese/Italian/English. Limited '
             'internet and local pickup; contact address is not the camp location. The '
             'brochure states no fees and accident insurance. No personal stipend or '
             'international-travel reimbursement is promised.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123::PT-BR-03-25::2025::::::',
  'title': 'Guardians of Nature',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Vermil, Guimarães',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 IPDJ workcamp for ages 18–30 in Vermil, Guimarães, July 21–31. '
             'Waterway cleanup and biodiversity/invasive-species education. Shared '
             'rooms and four meals; English and collaborative cooking apply. ID and '
             'health-cover documents have alternatives, not citizenship restrictions. '
             'The brochure states no fees and accident insurance. No personal stipend '
             'or international-travel reimbursement is promised.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123::PT-BR-10-25::2025::::::',
  'title': 'Live Nature Live Together 2025',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Braga',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 IPDJ workcamp for ages 18–30 in Braga, August 2–13. Environmental '
             'awareness and inclusion; English and physical handy work. Individual '
             'tents, five meals and shared bathrooms. Campsite versus bus-station '
             'meeting instructions conflict. The brochure states no fees and accident '
             'insurance. No personal stipend or international-travel reimbursement is '
             'promised.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123::PT-BR-12-25::2025::::::',
  'title': 'Ecofestival Mother Earth',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Fornelos, Fafe',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 IPDJ workcamp for ages 18–30 in Fornelos, Fafe, July 12–23. '
             'Sustainable-festival work with tents and mostly vegetarian meals, '
             'occasional fish but no meat. English/Portuguese and shared cleaning/work '
             'apply; no internet. Local pickup, not funded international travel. The '
             'brochure states no fees and accident insurance. No personal stipend or '
             'international-travel reimbursement is promised.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123::PT-CB-07-25::2025::::::',
  'title': 'Hands On',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Belmonte',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 IPDJ workcamp for ages 18–30 in Belmonte, July 1–12. Social-shop '
             'distribution and supervised children’s activities. Local lodging and '
             'Mediterranean meals with dietary alternatives; English. Alcohol, drugs, '
             'tobacco and vaping are prohibited. The brochure states no fees and '
             'accident insurance. No personal stipend or international-travel '
             'reimbursement is promised.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123::PT-EV-06-25::2025::::::',
  'title': 'For Climate 5.0',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Montemor-o-Novo',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 IPDJ workcamp for ages 18–30 in Montemor-o-Novo, July 7–18. Native '
             'nursery, water management and climate education. Individual tents and '
             'food; English and shared cooking/cleaning. Local activity transport is '
             'provided. The brochure states no fees and accident insurance. No '
             'personal stipend or international-travel reimbursement is promised.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123::PT-FA-13-25::2025::::::',
  'title': 'A Forest of Gratitude: Volunteer in Nature Conservation in Portugal',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Aljezur',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 IPDJ workcamp for ages 18–30 in Aljezur, September 1–12. Forest '
             'restoration and invasive control. Converted-school lodging and three '
             'meals with vegetarian option; English/Portuguese/Spanish. Volunteers '
             'worldwide are expressly welcomed. The brochure states no fees and '
             'accident insurance. No personal stipend or international-travel '
             'reimbursement is promised.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123::PT-GU-09-25::2025::::::',
  'title': 'Control and management of invasive species 2.0',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Maceira, Fornos de Algodres',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 IPDJ workcamp for ages 18–30 in Maceira, Fornos de Algodres, July '
             '14–25. Invasive-species management and environmental education. Shared '
             'hostel dormitories, five meals and local station transfers; English. The '
             'brochure states no fees and accident insurance. No personal stipend or '
             'international-travel reimbursement is promised.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123::PT-GU-15-25::2025::::::',
  'title': 'Take care of your Village',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Aldeia de S. Sebastião',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 IPDJ workcamp for ages 18–30 in Aldeia de S. Sebastião, July 8–19. '
             'Document and support a historic village. Shared bungalows and food '
             'ingredients with participant cooking; English. Table says 2025 but '
             'detailed dates and arrival say 2024; the edition’s practical '
             'applicability is uncertain. The brochure states no fees and accident '
             'insurance. No personal stipend or international-travel reimbursement is '
             'promised.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123::PT-PO-01-25::2025::::::',
  'title': 'Um outro mundo é possível! (Another World Is Possible!)',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Amarante',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 IPDJ workcamp for ages 18–30 in Amarante, September 19–30. Organic '
             'farming and fair trade. Shared four-person rooms and healthy '
             'organic/vegetarian meals; English and daily household duties apply. The '
             'brochure states no fees and accident insurance. No personal stipend or '
             'international-travel reimbursement is promised.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123::PT-PO-02-25::2025::::::',
  'title': 'Stage on the street: all included!',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Amarante',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 IPDJ workcamp for ages 18–30 in Amarante, July 1–12. Inclusive '
             'street theatre with young people with disabilities. Shared rooms and '
             'organic/vegetarian meals; English. Bring black performance clothes. The '
             'brochure states no fees and accident insurance. No personal stipend or '
             'international-travel reimbursement is promised.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123::PT-PO-11-25::2025::::::',
  'title': 'Get to Work: Create to Play',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Ermesinde',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 IPDJ workcamp for ages 18–30 in Ermesinde, July 14–25. Make '
             'recycled toys and educational games with local children. Shared lodging '
             'and three meals with vegetarian option; English. Physical '
             'crafting/painting; no specific skills required. The brochure states no '
             'fees and accident insurance. No personal stipend or international-travel '
             'reimbursement is promised.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123::PT-SA-14-25::2025::::::',
  'title': 'Renewed Roots: Revitalizing the Garden and Biodiversity',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Constância',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 IPDJ workcamp for ages 18–30 in Constância, September 18–28. Garden '
             'restoration and biodiversity technology. Camping equipment can be lent; '
             'three meals and local pickup. English and a short motivation letter are '
             'required; own international travel remains unfunded. The brochure states '
             'no fees and accident insurance. No personal stipend or '
             'international-travel reimbursement is promised.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123::PT-SA-16-25::2025::::::',
  'title': 'Dream Clean',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Salvaterra de Magos',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 IPDJ workcamp for ages 18–30 in Salvaterra de Magos, July 1–12. '
             'Public-space and heritage maintenance. Shared lodging and three meals; '
             'basic English. Bring mattress, sleeping bag and pillow. Table code '
             'SA-16/title Deam Clean conflicts with detail code SA-15/title Dream '
             'Clean; one camp identity. The brochure states no fees and accident '
             'insurance. No personal stipend or international-travel reimbursement is '
             'promised.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123::PT-VI-17-25::2025::::::',
  'title': 'Giving nature a hand 3.0',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Carvalhais, São Pedro do Sul',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 IPDJ workcamp for ages 18–30 in Carvalhais, São Pedro do Sul, July '
             '15–24. Ecosystem restoration, camera trapping and invasive control. '
             'Camping and four meals; English. At least 45 minutes walking and five '
             'hours physical work daily; own sleeping bag, no laundry. The brochure '
             'states no fees and accident insurance. No personal stipend or '
             'international-travel reimbursement is promised.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123::PT-VI-23-25::2025::::::',
  'title': 'Active Granja',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Granja, Castro Daire',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 IPDJ workcamp for ages 18–30 in Granja, Castro Daire, July 19–28. '
             'Intergenerational, cultural and environmental community work. Shared '
             'sports-hall camp beds and food; English and shared cooking/cleaning. '
             'Prepare a cultural presentation; local transfers. The brochure states no '
             'fees and accident insurance. No personal stipend or international-travel '
             'reimbursement is promised.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123::PT-VR-20-25::2025::::::',
  'title': 'CLOSER - Art & Solidarity',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Vila Real',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 IPDJ workcamp for ages 18–30 in Vila Real, July 1–12. Organise a '
             'solidarity concert/fashion show and food baskets. Shared residential '
             'lodging and four meals; English. Local van trips and completion diploma; '
             'no wage. The brochure states no fees and accident insurance. No personal '
             'stipend or international-travel reimbursement is promised.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123::PT-VR-21-25::2025::::::',
  'title': 'Culture & Solidarity',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Vila Real',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 IPDJ workcamp for ages 18–30 in Vila Real, July 13–24. Organise a '
             'solidarity concert/walk supporting a breast-cancer association. Shared '
             'residential lodging and four meals; English. Local van trips and '
             'completion diploma; distinct host/dates from CLOSER. The brochure states '
             'no fees and accident insurance. No personal stipend or '
             'international-travel reimbursement is promised.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=3b49066d-e93e-55a2-f857-a00757933b47&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123::PT-AV-05-26::2026::::::',
  'title': 'EcoMoita',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Oliveirinha, Aveiro',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 IPDJ workcamp for ages 18–30 in Oliveirinha, Aveiro, July 1–12. '
             'Restore habitats, forest trails and ponds. Shared mattresses; bring a '
             'sleeping bag. Meals and vegetarian options are provided; English '
             'preferred. Accident insurance; no personal stipend or international '
             'travel promised. The brochure says no camp fees, but the 2026 evaluation '
             'rules allow a EUR 25 charge for Portuguese or Portugal-resident '
             'participants.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123::PT-AV-06-26::2026::::::',
  'title': 'AVANCA 2026',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Avanca',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 IPDJ workcamp for ages 18–30 in Avanca, July 24–August 4. Help '
             'prepare and run a film festival. School-classroom mattresses and meals; '
             'bring a sleeping bag. English and permission before leaving camp apply. '
             'Accident insurance; no personal stipend or international travel '
             'promised. The brochure says no camp fees, but the 2026 evaluation rules '
             'allow a EUR 25 charge for Portuguese or Portugal-resident participants.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123::PT-AV-10-26::2026::::::',
  'title': 'EcoFílveda',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Albergaria-a-Velha',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 IPDJ workcamp for ages 18–30 in Albergaria-a-Velha, September 2–13. '
             'Restore habitats and trails and assist nature activities. Shared beds, '
             'provided meals and vegetarian options; bring a sleeping bag. English '
             'preferred. Accident insurance; no personal stipend or international '
             'travel promised. The brochure says no camp fees, but the 2026 evaluation '
             'rules allow a EUR 25 charge for Portuguese or Portugal-resident '
             'participants.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123::PT-BG-21-26::2026::::::',
  'title': 'Nature Ambassadors IV',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Espinhosela, Montesinho',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 IPDJ workcamp for ages 18–30 in Espinhosela, Montesinho, August '
             '10–21. Help wildlife coexistence, route signs and community work. '
             'Renovated-school lodging and provided meals with shared cooking. '
             'Portuguese, Italian or English; local pickup. Accident insurance; no '
             'personal stipend or international travel promised. The brochure says no '
             'camp fees, but the 2026 evaluation rules allow a EUR 25 charge for '
             'Portuguese or Portugal-resident participants.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123::PT-BG-22-26::2026::::::',
  'title': 'United by Donkeys IV',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Palancar, Miranda do Douro',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 IPDJ workcamp for ages 18–30 in Palancar, Miranda do Douro, July '
             '13–24. Help care for donkeys and maintain their shelter and stone walls. '
             'Shared bunk beds and local pickup. English, Portuguese or Italian '
             'preferred. Detailed meal provision is not stated. Accident insurance; no '
             'personal stipend or international travel promised. The brochure says no '
             'camp fees, but the 2026 evaluation rules allow a EUR 25 charge for '
             'Portuguese or Portugal-resident participants.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123::PT-BR-07-26::2026::::::',
  'title': 'Cacarejo N’Aldeia – Village Eco & Community Festival',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Vermil, Guimarães',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 IPDJ workcamp for ages 18–30 in Vermil, Guimarães, July 20–29. Help '
             'organise a sustainable community festival. Shared bunk rooms and four '
             'meals with dietary options. English and shared cooking/cleaning are '
             'required. Accident insurance; no personal stipend or international '
             'travel promised. The brochure says no camp fees, but the 2026 evaluation '
             'rules allow a EUR 25 charge for Portuguese or Portugal-resident '
             'participants.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123::PT-BR-16-26::2026::::::',
  'title': 'Live Nature Live Together 2026',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Braga',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 IPDJ workcamp for ages 18–30 in Braga, August 2–13. '
             'Environmental-awareness and inclusion work; English and willingness for '
             'physical tasks apply. Individual tents, five meals and shared bathrooms. '
             'Participant travel is not reimbursed. Accident insurance; no personal '
             'stipend or international travel promised. The brochure says no camp '
             'fees, but the 2026 evaluation rules allow a EUR 25 charge for Portuguese '
             'or Portugal-resident participants.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123::PT-BR-19-26::2026::::::',
  'title': 'Ecofestival Mother Earth',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Fornelos, Fafe',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 IPDJ workcamp for ages 18–30 in Fornelos, Fafe, July 11–22. '
             'Sustainable-festival work with tents, vegetarian meals and shared '
             'duties. Table says 2026, detail code/dates say 2025, but arrival says '
             '2026; applicability is uncertain. No meat onsite. Accident insurance; no '
             'personal stipend or international travel promised. The brochure says no '
             'camp fees, but the 2026 evaluation rules allow a EUR 25 charge for '
             'Portuguese or Portugal-resident participants.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123::PT-CB-13-26::2026::::::',
  'title': 'building Connections',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Belmonte',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 IPDJ workcamp for ages 18–30 in Belmonte, July 6–17. Community '
             'activities with children, older people and vulnerable families. Shared '
             'rooms and three meals with requested diets. English recommended; no '
             'technical skills specified. Accident insurance; no personal stipend or '
             'international travel promised. The brochure says no camp fees, but the '
             '2026 evaluation rules allow a EUR 25 charge for Portuguese or '
             'Portugal-resident participants.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123::PT-CO-18-26::2026::::::',
  'title': 'Candal: Living village',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Candal, Lousã',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 IPDJ workcamp for ages 18–30 in Candal, Lousã, August 31–September '
             '11. Restore paths and fountains after wildfire. Shared youth-hostel '
             'rooms, food budget and local activity van. English; shopping, cooking '
             'and cleaning are shared. First/last days mandatory; meal count '
             'conflicts. Accident insurance; no personal stipend or international '
             'travel promised. The brochure says no camp fees, but the 2026 evaluation '
             'rules allow a EUR 25 charge for Portuguese or Portugal-resident '
             'participants.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123::PT-EV-12-26::2026::::::',
  'title': 'For Climate 6.0',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Montemor-o-Novo',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 IPDJ workcamp for ages 18–30 in Montemor-o-Novo, July 13–24. '
             'Native-plant nursery, water and invasive-species work. Individual tents '
             'and food; English, weekend cooking and shared cleaning. Bring a tent or '
             'arrange one; local activity transport. Accident insurance; no personal '
             'stipend or international travel promised. The brochure says no camp '
             'fees, but the 2026 evaluation rules allow a EUR 25 charge for Portuguese '
             'or Portugal-resident participants.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123::PT-FA-08-26::2026::::::',
  'title': 'Hands on the Land – Regenerating Ecosystems',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Odiáxere, Lagos',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 IPDJ workcamp for ages 18–30 in Odiáxere, Lagos, September 17–28. '
             'Restore fire-affected ecosystems and help the community. Tents can be '
             'lent; five mainly vegetarian meals. English and shared daily duties '
             'apply. Accident insurance; no personal stipend or international travel '
             'promised. The brochure says no camp fees, but the 2026 evaluation rules '
             'allow a EUR 25 charge for Portuguese or Portugal-resident participants.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123::PT-GU-17-26::2026::::::',
  'title': 'Invasive species 3.0',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Maceira, Fornos de Algodres',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 IPDJ workcamp for ages 18–30 in Maceira, Fornos de Algodres, July '
             '13–24. Environmental education and invasive control. Shared hostel '
             'dormitories, five meals and local station transfer; English. Meeting '
             'date July 20 conflicts with the July 13 start. Accident insurance; no '
             'personal stipend or international travel promised. The brochure says no '
             'camp fees, but the 2026 evaluation rules allow a EUR 25 charge for '
             'Portuguese or Portugal-resident participants.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123::PT-LI-09-26::2026::::::',
  'title': 'EcoFrossos 2.0',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Frossos, Albergaria-a-Velha',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 IPDJ workcamp for ages 18–30 in Frossos, Albergaria-a-Velha, July '
             '14–25. Restore pollinator habitats and trails. Shared beds, meals and '
             'vegetarian option; English preferred, Portuguese/Spanish mentioned. '
             'Table labels Lisboa, conflicting with the detailed host location. '
             'Accident insurance; no personal stipend or international travel '
             'promised. The brochure says no camp fees, but the 2026 evaluation rules '
             'allow a EUR 25 charge for Portuguese or Portugal-resident participants.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123::PT-PO-01-26::2026::::::',
  'title': 'Um outro mundo é possível! (Another World Is Possible!)',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Amarante',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 IPDJ workcamp for ages 18–30 in Amarante, September 19–30. Organic '
             'farming and fair-trade work. Shared rooms and healthy organic/vegetarian '
             'meals; English and daily household duties apply. Accident insurance; no '
             'personal stipend or international travel promised. The brochure says no '
             'camp fees, but the 2026 evaluation rules allow a EUR 25 charge for '
             'Portuguese or Portugal-resident participants.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123::PT-PO-02-26::2026::::::',
  'title': 'Stage on the street: all included!',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Amarante',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 IPDJ workcamp for ages 18–30 in Amarante, July 7–18. Inclusive '
             'street theatre with young people with disabilities. Shared rooms and '
             'organic/vegetarian meals. English; bring black performance clothes and '
             'share household duties. Accident insurance; no personal stipend or '
             'international travel promised. The brochure says no camp fees, but the '
             '2026 evaluation rules allow a EUR 25 charge for Portuguese or '
             'Portugal-resident participants.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123::PT-PO-14-26::2026::::::',
  'title': 'ReSound',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Ermesinde',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 IPDJ workcamp for ages 18–30 in Ermesinde, September 1–12. Build '
             'recycled musical instruments and public performances. Shared lodging and '
             'three meals with vegetarian option; English. Physical crafting/painting; '
             'no specific skills required. Accident insurance; no personal stipend or '
             'international travel promised. The brochure says no camp fees, but the '
             '2026 evaluation rules allow a EUR 25 charge for Portuguese or '
             'Portugal-resident participants.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123::PT-SA-03-26::2026::::::',
  'title': 'DREAM SCHOOL ACTION',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Salvaterra de Magos',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 IPDJ workcamp for ages 18–30 in Salvaterra de Magos, July 1–12. '
             'Maintain and improve community school spaces. Shared accommodation and '
             'provided meals; basic English. Bring the required sleeping equipment and '
             'take part in group work. Accident insurance; no personal stipend or '
             'international travel promised. The brochure says no camp fees, but the '
             '2026 evaluation rules allow a EUR 25 charge for Portuguese or '
             'Portugal-resident participants.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123::PT-SA-04-26::2026::::::',
  'title': 'DREAM CARE ACTION',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Salvaterra de Magos',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 IPDJ workcamp for ages 18–30 in Salvaterra de Magos, July 17–28. '
             'Maintain elderly-care spaces and join intergenerational activities. '
             'Shared lodging, food and group cooking/cleaning; English. Bring the '
             'required sleeping equipment. Accident insurance; no personal stipend or '
             'international travel promised. The brochure says no camp fees, but the '
             '2026 evaluation rules allow a EUR 25 charge for Portuguese or '
             'Portugal-resident participants.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123::PT-VI-11-26::2026::::::',
  'title': 'YouConnect',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Tarouca',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 IPDJ workcamp for ages 18–30 in Tarouca, September 6–15. '
             'Environmental, agricultural and civic activities. Shared rural '
             'dormitories and five meals with dietary options; English and group '
             'cooking/cleaning. No internet. Accident insurance; no personal stipend '
             'or international travel promised. The brochure says no camp fees, but '
             'the 2026 evaluation rules allow a EUR 25 charge for Portuguese or '
             'Portugal-resident participants.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123::PT-VI-20-26::2026::::::',
  'title': 'Active Granja',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Granja, Castro Daire',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 IPDJ workcamp for ages 18–30 in Granja, Castro Daire, July '
             '23–August 2. Intergenerational, cultural and environmental community '
             'work. Shared camp beds, food and shared cooking/cleaning. English; '
             'prepare a cultural performance. Accident insurance; no personal stipend '
             'or international travel promised. The brochure says no camp fees, but '
             'the 2026 evaluation rules allow a EUR 25 charge for Portuguese or '
             'Portugal-resident participants.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123::PT-VR-15-26::2026::::::',
  'title': 'AHAS Heritage Volunteers:',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123',
  'language': 'en',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'eligible_countries': [],
  'location': 'Sabrosa',
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 IPDJ workcamp for ages 18–30 in Sabrosa, July 6–17. Supervised '
             'noninvasive heritage documentation and theatre. Shared rural '
             'beds/mattresses and food with group cooking; basic English, own sleeping '
             'bag, limited internet/services. Detail insurance field is blank; the '
             'general brochure clause applies. Accident insurance; no personal stipend '
             'or international travel promised. The brochure says no camp fees, but '
             'the 2026 evaluation rules allow a EUR 25 charge for Portuguese or '
             'Portugal-resident participants.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=712709c2-83ec-9da6-4887-f9932764661e&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/programa-arribar::Arribar 2025–2028::::::::',
  'title': 'Programa Arribar',
  'url': 'https://ipdj.gov.pt/programa-arribar',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Consortia led by a nonprofit with prison or educational-centre partners '
             'support social and employment reintegration of young people deprived of '
             'liberty in Norte, Centro and Alentejo. Projects last 36 months; maximum '
             'EUR 75,000/project or EUR 25,000/year. Five funded projects are results, '
             'not five new offers. The actual scheme runs May 2025–April 2028; further '
             'application availability and closing are unknown.',
  'proof': 'Original published facts: https://ipdj.gov.pt/programa-arribar'},
 {'key': 'https://ipdj.gov.pt/programa-escolhas::Escolhas — Nona Geração::::::::',
  'title': 'Programa Escolhas',
  'url': 'https://ipdj.gov.pt/programa-escolhas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Ninth-generation inclusion programme, October 2023–September 2026, '
             'delivered by local consortia of at least four public/private entities. '
             'Priority participants are vulnerable young people aged 6–25; education, '
             'employment and civic/community skills are supported. Funding covers at '
             'most 85% of whole-project costs, with at least 15% consortium '
             'contribution. The 118 financed projects are results; new application '
             'availability and closing are unknown.',
  'proof': 'Original published facts: https://ipdj.gov.pt/programa-escolhas'},
 {'key': 'https://ipdj.gov.pt/programa-nacional-de-formacao-de-treinadores::Programa '
         'Nacional de Formação de Treinadores::::::::',
  'title': 'Programa Nacional de Formação de Treinadores',
  'url': 'https://ipdj.gov.pt/programa-nacional-de-formacao-de-treinadores',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Structured coach-qualification framework with four grades and '
             'sport-specific training, experience and certification conditions. '
             'Minimum ages vary by grade, 18/19/21/24, with schooling determined by '
             'birth year. This is a qualification pathway, not an advertised coach '
             'vacancy. Fees, current course intake and closing depend on the actual '
             'approved provider and are unknown here.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/programa-nacional-de-formacao-de-treinadores'},
 {'key': 'https://ipdj.gov.pt/apoio-financeiro-ao-desporto-federado::Apoio à atividade '
         'regular das federações — 2026::::::::',
  'title': 'Apoio financeiro ao Desporto Federado',
  'url': 'https://ipdj.gov.pt/apoio-financeiro-ao-desporto-federado',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-04-10',
  'closing_local_clock': None,
  'summary': '2026 regular-activity support for eligible sports federations through '
             'the published institutional procedure. Applications close April 10, '
             '2026. Funding supports the federation’s approved programme rather than '
             'each athlete’s personal stipend. Grant amount and additional '
             'edition-specific terms are not stated in the own overview. Eligible '
             'applicants include UPD federations, confederative bodies, '
             'Olympic/Paralympic bodies and Fundação do Desporto.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/apoio-financeiro-ao-desporto-federado'},
 {'key': 'https://ipdj.gov.pt/apoio-financeiro-ao-desporto-federado::Apoio à '
         'organização de eventos desportivos internacionais — 2026::::::::',
  'title': 'Apoio financeiro ao Desporto Federado',
  'url': 'https://ipdj.gov.pt/apoio-financeiro-ao-desporto-federado',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-01-20',
  'closing_local_clock': None,
  'summary': '2026 support for eligible federations organising international sport '
             'events. Applications close January 20, 2026. Institutional project and '
             'event criteria apply; amounts and extra edition conditions are not '
             'stated in the own overview. This is not an unrestricted athlete travel '
             'award. UPD federations are preferred, not the exclusive applicant class.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/apoio-financeiro-ao-desporto-federado'},
 {'key': 'https://ipdj.gov.pt/apoio-financeiro-ao-desporto-federado::Deslocações às '
         'Regiões Autónomas — época 2025/2026::::::::',
  'title': 'Apoio financeiro ao Desporto Federado',
  'url': 'https://ipdj.gov.pt/apoio-financeiro-ao-desporto-federado',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2026-01-30',
  'closing_local_clock': None,
  'summary': '2025/26 autonomous-region travel support is restricted to the six '
             'federations specifically named by the own programme. Applications close '
             'January 30, 2026. Funding supports approved federation travel, not a '
             'personal grant for anyone residing in an island region. Amount and '
             'additional edition-specific conditions are not stated here.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/apoio-financeiro-ao-desporto-federado'},
 {'key': 'https://ipdj.gov.pt/empreende-ja::Empreende Já::::::::',
  'title': 'Empreende Já',
  'url': 'https://ipdj.gov.pt/empreende-ja',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants', 'training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Entrepreneurship training, tutoring and technical support for Portugal '
             'residents aged 18–29, with completed year 12, not in '
             'employment/education/training and registered with the employment '
             'service. Applicants need tax/social-security compliance and cannot have '
             'previous Empreende Já or overlapping youth entrepreneurship/employment '
             'support. Individuals or teams of at most three follow the stated '
             'conditions; application documents require mainland residence. Financial '
             'support is offered; its amount and current closing are unknown.',
  'proof': 'Original published facts: https://ipdj.gov.pt/empreende-ja'},
 {'key': 'https://ipdj.gov.pt/programa-formar-medida-3::Formar+ — Medida 3 — '
         '2025::::::::',
  'title': 'Formar+ | Medida 3 - Apoio Formativo ao Associativismo',
  'url': 'https://ipdj.gov.pt/programa-formar-medida-3',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical 2025 training-plan support for effective mainland RNAJ youth '
             'associations/federations. Actions require 10–20 learners; at least 30% '
             'self-financing, learner insurance and qualified trainers. Maximum EUR '
             '3,000/plan and EUR 1,000/action fund institutional costs, not learner '
             'stipends. The FAQ’s November 15 is yearless, so absolute closing is '
             'unknown. The later ADJ programme replaces this measure without erasing '
             'the historical edition.',
  'proof': 'Original published facts: https://ipdj.gov.pt/programa-formar-medida-3'},
 {'key': 'https://ipdj.gov.pt/geracoes-em-rede::Gerações em Rede 2023::::::::',
  'title': 'Gerações em Rede 2023',
  'url': 'https://ipdj.gov.pt/geracoes-em-rede',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2023 intergenerational digital-literacy volunteering for ages 16–30, '
             'serving adult residents of Portugal. Participants receive preparation, '
             'insurance, certification and conditional EUR 12/day expense '
             'reimbursement, not wages. Eligible promoters receive separate management '
             'support tied to beneficiary count. Registration uses project-relative '
             'lead times; absolute closing and present availability are unknown.',
  'proof': 'Original published facts: https://ipdj.gov.pt/geracoes-em-rede'},
 {'key': 'https://ipdj.gov.pt/reativar::Reativar Desporto 2021::::::::',
  'title': 'Medida REATIVAR DESPORTO',
  'url': 'https://ipdj.gov.pt/reativar',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2021-09-04',
  'closing_local_clock': None,
  'summary': '2021 pandemic-recovery support for mainland nonprofit clubs affiliated '
             'with qualifying sports federations. Whole-club support is calculated '
             'from up to EUR 50/registered athlete and the applicable factor; EUR 30 '
             'million is the programme pool, not personal awards. The hub’s September '
             '4 extension differs from the older guide’s August 16 date. Eligibility '
             'and activity conditions apply; this is a historical call.',
  'proof': 'Original published facts: https://ipdj.gov.pt/reativar'},
 {'key': 'https://ipdj.gov.pt/namorar-com-fair-play::Namorar com Fair Play — entidades '
         '— 2025::::::::',
  'title': 'Namorar com Fair Play',
  'url': 'https://ipdj.gov.pt/namorar-com-fair-play',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-03-30',
  'closing_local_clock': None,
  'summary': '2025 dating-violence prevention volunteering for ages 14–30, subject to '
             'preparation and the project’s access conditions. Work is limited to five '
             'hours/day; conditional EUR 13/day reimburses expenses rather than wages. '
             'Participant registration is generally at least five days before '
             'activity, or during it if places remain. Absolute closing is unknown; '
             'organiser funding is not a separate personal award.',
  'proof': 'Original published facts: https://ipdj.gov.pt/namorar-com-fair-play'},
 {'key': 'https://ipdj.gov.pt/namorar-com-fair-play::Namorar com Fair Play — '
         'voluntariado — 2025::::::::',
  'title': 'Namorar com Fair Play',
  'url': 'https://ipdj.gov.pt/namorar-com-fair-play',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2025 dating-violence prevention volunteering for ages 14–30, subject to '
             'preparation and the project’s access conditions. Work is limited to five '
             'hours/day; conditional EUR 13/day reimburses expenses rather than wages. '
             'Participant registration is generally at least five days before '
             'activity, or during it if places remain. Absolute closing is unknown; '
             'organiser funding is not a separate personal award.',
  'proof': 'Original published facts: https://ipdj.gov.pt/namorar-com-fair-play'},
 {'key': 'https://ipdj.gov.pt/navegas::Naveg@s em Segurança? — entidades — '
         '2025::::::::',
  'title': 'Navega(s) em Segurança?',
  'url': 'https://ipdj.gov.pt/navegas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-03-17',
  'closing_local_clock': None,
  'summary': '2025 digital-safety project call for eligible private nonprofits, with '
             'peer volunteers aged 16–30. Applications close March 17, 2025; activity '
             'March 31–December 15 is separate. Support includes conditional EUR '
             '13/day volunteer expense reimbursement, insurance, certification and '
             'institutional management/session costs. It is not wages or an automatic '
             'grant per participant.',
  'proof': 'Original published facts: https://ipdj.gov.pt/navegas'},
 {'key': 'https://ipdj.gov.pt/premio-jovens-pela-igualdade::Jovens pela '
         'Igualdade::::::::',
  'title': 'Prémio «Jovens pela Igualdade»',
  'url': 'https://ipdj.gov.pt/premio-jovens-pela-igualdade',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Institutional award for eligible nonprofit organisations promoting the '
             'named youth programmes and equality. The three project awards are EUR '
             '1,700, 1,300 and 1,000, not payments to every young participant. '
             'Project, programme and selection conditions apply. The own hub does not '
             'establish a current dated application window; closing and availability '
             'are unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/premio-jovens-pela-igualdade'},
 {'key': 'https://ipdj.gov.pt/premios-regionais-de-boas-praticas-de-voluntariado-jovem::Boas '
         'Práticas — Voluntariado Jovem::::::::',
  'title': 'Prémios Regionais de Boas Práticas de Voluntariado Jovem',
  'url': 'https://ipdj.gov.pt/premios-regionais-de-boas-praticas-de-voluntariado-jovem',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Regional recognition for eligible nonprofit volunteering promoters; '
             'public entities receive honorary recognition instead of cash. The '
             'current hub states EUR 3,100/regional winner, while the linked older '
             '2022 rules describe different award amounts and ranking. Exact '
             'applicable current award terms are therefore uncertain. Annual '
             'region-specific submission windows lack a complete year, so closing and '
             'availability remain unknown.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/premios-regionais-de-boas-praticas-de-voluntariado-jovem'},
 {'key': 'https://ipdj.gov.pt/voluntariado-jovem-70-j%C3%81-direitos-da-juventude::70JÁ! '
         '— 2022::::::::',
  'title': 'Voluntariado Jovem 70JÁ!',
  'url': 'https://ipdj.gov.pt/voluntariado-jovem-70-j%C3%81-direitos-da-juventude',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['volunteering'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical 2022 human-rights and citizenship volunteering for ages '
             '14–30, delivered by eligible nonprofits. The 2022 document states EUR '
             '12/day expense reimbursement and an institutional management limit; '
             'later general hubs use other rates, not verified for this edition. '
             'Training, insurance and project conditions apply. Participant and entity '
             'lead times are relative to activity; no absolute closing is established.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/voluntariado-jovem-70-j%C3%81-direitos-da-juventude'},
 {'key': 'https://ipdj.gov.pt/programa-pejene-programa-de-est%C3%A1gios-de-jovens-estudantes-do-ensino-superior-e-nas-empresas::PEJENE::::::::',
  'title': 'Programa de Estágios de Jovens Estudantes do Ensino Superior nas Empresas',
  'url': 'https://ipdj.gov.pt/programa-pejene-programa-de-est%C3%A1gios-de-jovens-estudantes-do-ensino-superior-e-nas-empresas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['internships'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Unpaid two- to three-month summer internships for higher-education '
             'students in their penultimate or final bachelor’s, Bologna master’s or '
             'integrated-master’s year, across study fields. Fundação da Juventude '
             'preselects candidates before host selection. Internship conditions and '
             'availability depend on the selected host. The own programme states no '
             'current dated application closing or personal grant amount.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/programa-pejene-programa-de-est%C3%A1gios-de-jovens-estudantes-do-ensino-superior-e-nas-empresas'},
 {'key': 'https://ipdj.gov.pt/programa-de-apoio-a-formacao-de-recursos-humanos-pafrh::PAFRH '
         '— 2026::::::::',
  'title': 'Programa de Apoio à Formação de Recursos Humanos (PAFRH)',
  'url': 'https://ipdj.gov.pt/programa-de-apoio-a-formacao-de-recursos-humanos-pafrh',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 institutional training-plan funding for eligible UPD sports '
             'federations, excluding athlete-practice training. Eligible action types '
             'and participant thresholds determine conditional funding; at least 33.3% '
             'female participation applies to coach-training places, with stated '
             'exceptions/conditions. Online actions receive a 25% reduction. This is '
             'federation support, not learner stipends; current closing follows the '
             'announced platform procedure and is unknown here.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/programa-de-apoio-a-formacao-de-recursos-humanos-pafrh'},
 {'key': 'https://ipdj.gov.pt/programa-de-apoio-a-acoes-de-formacao-paaf::PAAF — '
         '2026::::::::',
  'title': 'Programa de Apoio a Ações de Formação (PAAF)',
  'url': 'https://ipdj.gov.pt/programa-de-apoio-a-acoes-de-formacao-paaf',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': '2026 ad-hoc training-plan funding for eligible sports clubs, '
             'higher-education institutions, sport promoters and other nonprofit '
             'sport-linked organisations. Maximum EUR 8,000 for institutional costs, '
             'with at least 50 planned learners and national/international coverage. '
             'Apply at least 30 days before the action; no overlapping IPDJ '
             'human-resource-training support. Absolute closing unknown without an '
             'event trigger; not a personal stipend.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/programa-de-apoio-a-acoes-de-formacao-paaf'},
 {'key': 'https://ipdj.gov.pt/bolsas-acad%C3%A9micas::Bolsas Académicas Praticantes de '
         'Alto Rendimento::::::::',
  'title': 'Bolsas Académicas',
  'url': 'https://ipdj.gov.pt/bolsas-acad%C3%A9micas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['scholarships'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Conditional academic support for high-performance athletes studying in '
             'Portugal or abroad where study and training are compatible. The '
             'athlete’s federation applies with admission, sporting, household-income '
             'and other-support evidence. Maximum support follows the legal Portuguese '
             'public-HEI annual tuition cap in the sporting-result year, not a fixed '
             'euro award. Apply by December 31 of the following year; without the '
             'actual result trigger, absolute closing is unknown.',
  'proof': 'Original published facts: https://ipdj.gov.pt/bolsas-acad%C3%A9micas'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=672c6f4a-b75f-3539-d758-1eed064caa4b&groupId=20123::PNDpT '
         '— Instituições de Ensino Superior — 2023/2025::::::::',
  'title': 'PNDpT-IES - biénio 2023-2025',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=672c6f4a-b75f-3539-d758-1eed064caa4b&groupId=20123',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': '23:59',
  'summary': 'Historical 2023–2025 higher-education community-sport research call, '
             'with a total EUR 200,000 pool for the specified participation and '
             'community-research areas. Funding belongs to approved institutional '
             'projects, not individual researchers. Applications close July 15, 2023 '
             'at 23:59, with no stated zone; activities September 2023–August 2025 are '
             'separate. Full applicant and project conditions apply. The linked '
             'general-rule PDF is labelled a consultation draft; final additional '
             'rules are unverified.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=672c6f4a-b75f-3539-d758-1eed064caa4b&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=728ebd41-2f6c-5e45-afba-ae10f69969bc&groupId=20123::Associa-te '
         '— 2025::::::::',
  'title': 'Despacho Programa Associa-te 2025',
  'url': 'https://ipdj.gov.pt/c/document_library/get_file?uuid=728ebd41-2f6c-5e45-afba-ae10f69969bc&groupId=20123',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': '2025-06-27',
  'closing_local_clock': None,
  'summary': '2025 Associa-te equipment support for effective RNAJ basic and secondary '
             'student associations in mainland Portugal. Maximum EUR 500/association '
             'within a EUR 9,000 call pool. One school/year application must concern '
             'the current academic-year project and have school-management agreement; '
             'previously supported same equipment is ineligible. Applications close '
             'June 27, 2025. Equipment is an association benefit, not a pupil stipend.',
  'proof': 'Original published facts: '
           'https://ipdj.gov.pt/c/document_library/get_file?uuid=728ebd41-2f6c-5e45-afba-ae10f69969bc&groupId=20123'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Cuida-te+ em tempos de '
         'pandemia | Saúde Mental::2021::10::março::17h00',
  'title': 'Cuida-te+ em tempos de pandemia | Saúde Mental',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on mental health during the '
             'pandemic, announced for 10 March 2021 at 17h00, with the published '
             'facilitator. The calendar offers the named educational session and, for '
             'some topics, supporting materials. Fees, entry requirements and '
             'application closing are not stated; present enrolment availability is '
             'unknown. The published time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Cuida-te+ em tempos de '
         'pandemia | Comportamentos Aditivos e Dependências::2021::4::março::17h00',
  'title': 'Cuida-te+ em tempos de pandemia | Comportamentos Aditivos e Dependências',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on addictive behaviours and '
             'dependencies during the pandemic, announced for 4 March 2021 at 17h00. '
             'The calendar offers the named educational session and, for some topics, '
             'supporting materials. Fees, entry requirements and application closing '
             'are not stated; present enrolment availability is unknown. The published '
             'time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Cuida-te+ em tempos de '
         'pandemia | Sexualidade e riscos online ::2021::26::fevereiro::11h30',
  'title': 'Cuida-te+ em tempos de pandemia | Sexualidade e riscos online ',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on sexuality and online risks, '
             'announced for 26 February 2021 at 11h30, with the published facilitator. '
             'The calendar offers the named educational session and, for some topics, '
             'supporting materials. Fees, entry requirements and application closing '
             'are not stated; present enrolment availability is unknown. The published '
             'time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Cuida-te+ em tempos de '
         'pandemia | Alimentação e Atividade Física e '
         'Desportiva::2021::25::fevereiro::14h30',
  'title': 'Cuida-te+ em tempos de pandemia | Alimentação e Atividade Física e '
           'Desportiva',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on food, physical activity and '
             'sport during the pandemic, announced for 25 February 2021 at 14h30, with '
             'the published facilitator. The calendar offers the named educational '
             'session and, for some topics, supporting materials. Fees, entry '
             'requirements and application closing are not stated; present enrolment '
             'availability is unknown. The published time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Esports: desafios para toda '
         'uma sociedade::2020::15::julho::17h30',
  'title': 'Esports: desafios para toda uma sociedade',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on esports and their social '
             'challenges, announced for 15 July 2020 at 17h30, with the published '
             'facilitator. The calendar offers the named educational session and, for '
             'some topics, supporting materials. Fees, entry requirements and '
             'application closing are not stated; present enrolment availability is '
             'unknown. The published time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Workshop '
         'Empreendedorismo::2020::7::julho::14h30',
  'title': 'Workshop Empreendedorismo',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on entrepreneurship, announced '
             'for 7 July 2020 at 14h30, with the published facilitator. The calendar '
             'offers the named educational session and, for some topics, supporting '
             'materials. Fees, entry requirements and application closing are not '
             'stated; present enrolment availability is unknown. The published time is '
             'the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Formação e Qualificação no '
         'Desporto | A Nova Portaria da Formação Contínua de '
         'Treinadores::2020::30::junho::16h30',
  'title': 'Formação e Qualificação no Desporto | A Nova Portaria da Formação Contínua '
           'de Treinadores',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on the continuing-education '
             'rules for sports coaches, announced for 30 June 2020 at 16h30, with the '
             'published facilitator. The calendar offers the named educational session '
             'and, for some topics, supporting materials. Fees, entry requirements and '
             'application closing are not stated; present enrolment availability is '
             'unknown. The published time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Perigos nas Redes '
         'Sociais::2020::30::junho::11h00',
  'title': 'Perigos nas Redes Sociais',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on risks on social networks, '
             'announced for 30 June 2020 at 11h00, with the published facilitator. The '
             'calendar offers the named educational session and, for some topics, '
             'supporting materials. Fees, entry requirements and application closing '
             'are not stated; present enrolment availability is unknown. The published '
             'time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Políticas de juventude de '
         'apoio à comunidade LGBTI::2020::25::junho::17h30',
  'title': 'Políticas de juventude de apoio à comunidade LGBTI',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on youth-policy support for '
             'LGBTI communities, announced for 25 June 2020 at 17h30, with the '
             'published facilitator. The calendar offers the named educational session '
             'and, for some topics, supporting materials. Fees, entry requirements and '
             'application closing are not stated; present enrolment availability is '
             'unknown. The published time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Empreende Já - Casos de '
         'sucesso de empreendedorismo jovem (1.ª edição)::2020::19::junho::15h00',
  'title': 'Empreende Já - Casos de sucesso de empreendedorismo jovem (1.ª edição)',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on young entrepreneurs’ '
             'first-edition experiences, announced for 19 June 2020 at 15h00, with the '
             'published facilitator. The calendar offers the named educational session '
             'and, for some topics, supporting materials. Fees, entry requirements and '
             'application closing are not stated; present enrolment availability is '
             'unknown. The published time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Métodos e Instrumentos de '
         'Ação com Jovens::2020::18::junho::17h30',
  'title': 'Métodos e Instrumentos de Ação com Jovens',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on methods and tools for youth '
             'work, announced for 18 June 2020 at 17h30, with the published '
             'facilitator. The calendar offers the named educational session and, for '
             'some topics, supporting materials. Fees, entry requirements and '
             'application closing are not stated; present enrolment availability is '
             'unknown. The published time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Educar para o '
         'Empreendedorismo: Medidas ativas de emprego, apoios e '
         'programas::2020::16::junho::15h00',
  'title': 'Educar para o Empreendedorismo: Medidas ativas de emprego, apoios e '
           'programas',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on entrepreneurship education, '
             'employment support and programmes, announced for 16 June 2020 at 15h00, '
             'with the published facilitator. The calendar offers the named '
             'educational session and, for some topics, supporting materials. Fees, '
             'entry requirements and application closing are not stated; present '
             'enrolment availability is unknown. The published time is the session '
             'time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Cuida-te+ de ti e dos outros '
         'protegendo-te do estigma e da discriminação::2020::9::junho::11h30',
  'title': 'Cuida-te+ de ti e dos outros protegendo-te do estigma e da discriminação',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on stigma and discrimination, '
             'announced for 9 June 2020 at 11h30, with the published facilitator. The '
             'calendar offers the named educational session and, for some topics, '
             'supporting materials. Fees, entry requirements and application closing '
             'are not stated; present enrolment availability is unknown. The published '
             'time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Gestão de Projetos e Linhas '
         'de Financiamento::2020::8::junho::17h30',
  'title': 'Gestão de Projetos e Linhas de Financiamento',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on project management and '
             'funding routes, announced for 8 June 2020 at 17h30, with the published '
             'facilitator. The calendar offers the named educational session and, for '
             'some topics, supporting materials. Fees, entry requirements and '
             'application closing are not stated; present enrolment availability is '
             'unknown. The published time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Igualdade e Juventude em '
         'tempos de COVID-19::2020::4::junho::18h00',
  'title': 'Igualdade e Juventude em tempos de COVID-19',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on equality and youth during '
             'COVID-19, announced for 4 June 2020 at 18h00, with the published '
             'facilitator. The calendar offers the named educational session and, for '
             'some topics, supporting materials. Fees, entry requirements and '
             'application closing are not stated; present enrolment availability is '
             'unknown. The published time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Como repensar e reinventar '
         'Organizações::2020::4::junho::17h30',
  'title': 'Como repensar e reinventar Organizações',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on rethinking organisations, '
             'announced for 4 June 2020 at 17h30, with the published facilitator. The '
             'calendar offers the named educational session and, for some topics, '
             'supporting materials. Fees, entry requirements and application closing '
             'are not stated; present enrolment availability is unknown. The published '
             'time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::O programa nacional de '
         'formação de treinadores::2020::2::junho::16h00',
  'title': 'O programa nacional de formação de treinadores',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on the national coach-training '
             'programme, announced for 2 June 2020 at 16h00, with the published '
             'facilitator. The calendar offers the named educational session and, for '
             'some topics, supporting materials. Fees, entry requirements and '
             'application closing are not stated; present enrolment availability is '
             'unknown. The published time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Cuida-te+ de ti e do teu '
         'bem-estar::2020::2::junho::11h30',
  'title': 'Cuida-te+ de ti e do teu bem-estar',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on personal wellbeing, '
             'announced for 2 June 2020 at 11h30, with the published facilitator. The '
             'calendar offers the named educational session and, for some topics, '
             'supporting materials. Fees, entry requirements and application closing '
             'are not stated; present enrolment availability is unknown. The published '
             'time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Estratégia 2030 do CoE para '
         'a Juventude::2020::28::maio::17h30',
  'title': 'Estratégia 2030 do CoE para a Juventude',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on the Council of Europe’s '
             'youth strategy, announced for 28 May 2020 at 17h30, with the published '
             'facilitator. The calendar offers the named educational session and, for '
             'some topics, supporting materials. Fees, entry requirements and '
             'application closing are not stated; present enrolment availability is '
             'unknown. The published time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Redes '
         'sociais::2020::27::maio::15h00',
  'title': 'Redes sociais',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on social networks, announced '
             'for 27 May 2020 at 15h00, with the published facilitator. The calendar '
             'offers the named educational session and, for some topics, supporting '
             'materials. Fees, entry requirements and application closing are not '
             'stated; present enrolment availability is unknown. The published time is '
             'the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Cuida-te+ resolvendo e '
         'evitando conflitos em família::2020::26::maio::11h30',
  'title': 'Cuida-te+ resolvendo e evitando conflitos em família',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on family conflict prevention '
             'and resolution, announced for 26 May 2020 at 11h30, with the published '
             'facilitator. The calendar offers the named educational session and, for '
             'some topics, supporting materials. Fees, entry requirements and '
             'application closing are not stated; present enrolment availability is '
             'unknown. The published time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Ciberbulliyng::2020::25::maio::17h00',
  'title': 'Ciberbulliyng',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on cyberbullying, announced for '
             '25 May 2020 at 17h00, with the published facilitator. The calendar '
             'offers the named educational session and, for some topics, supporting '
             'materials. Fees, entry requirements and application closing are not '
             'stated; present enrolment availability is unknown. The published time is '
             'the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Uso seguro da Internet/bons '
         'hábitos tecnológicos::2020::22::maio::15h00',
  'title': 'Uso seguro da Internet/bons hábitos tecnológicos',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on safe internet use and '
             'healthy technology habits, announced for 22 May 2020 at 15h00, with the '
             'published facilitator. The calendar offers the named educational session '
             'and, for some topics, supporting materials. Fees, entry requirements and '
             'application closing are not stated; present enrolment availability is '
             'unknown. The published time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Inovação Social: uma opção '
         'no trabalho juvenil::2020::21::maio::17h30',
  'title': 'Inovação Social: uma opção no trabalho juvenil',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on social innovation in youth '
             'work, announced for 21 May 2020 at 17h30, with the published '
             'facilitator. The calendar offers the named educational session and, for '
             'some topics, supporting materials. Fees, entry requirements and '
             'application closing are not stated; present enrolment availability is '
             'unknown. The published time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::OPJP - uma boa '
         'prática::2020::21::maio::11h00',
  'title': 'OPJP - uma boa prática',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on participatory youth '
             'budgeting, announced for 21 May 2020 at 11h00, with the published '
             'facilitator. The calendar offers the named educational session and, for '
             'some topics, supporting materials. Fees, entry requirements and '
             'application closing are not stated; present enrolment availability is '
             'unknown. The published time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Direitos Humanos '
         'online::2020::19::maio::16h00',
  'title': 'Direitos Humanos online',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on human rights online, '
             'announced for 19 May 2020 at 16h00, with the published facilitator. The '
             'calendar offers the named educational session and, for some topics, '
             'supporting materials. Fees, entry requirements and application closing '
             'are not stated; present enrolment availability is unknown. The published '
             'time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Cuida-te+ e segue as '
         'recomendações para trabalho, aulas ou estudo por via '
         'remota::2020::19::maio::11h30',
  'title': 'Cuida-te+ e segue as recomendações para trabalho, aulas ou estudo por via '
           'remota',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on remote work, classes and '
             'study, announced for 19 May 2020 at 11h30, with the published '
             'facilitator. The calendar offers the named educational session and, for '
             'some topics, supporting materials. Fees, entry requirements and '
             'application closing are not stated; present enrolment availability is '
             'unknown. The published time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Gaming: riscos e '
         'benefícios::2020::18::maio::15h00',
  'title': 'Gaming: riscos e benefícios',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on gaming risks and benefits, '
             'announced for 18 May 2020 at 15h00, with the published facilitator. The '
             'calendar offers the named educational session and, for some topics, '
             'supporting materials. Fees, entry requirements and application closing '
             'are not stated; present enrolment availability is unknown. The published '
             'time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Privacidade, proteção de '
         'dados::2020::15::maio::15h00',
  'title': 'Privacidade, proteção de dados',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on privacy and data protection, '
             'announced for 15 May 2020 at 15h00, with the published facilitator. The '
             'calendar offers the named educational session and, for some topics, '
             'supporting materials. Fees, entry requirements and application closing '
             'are not stated; present enrolment availability is unknown. The published '
             'time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::ODS & Young Goals - como se '
         'complementam a favor da Juventude::2020::14::maio::17h30',
  'title': 'ODS & Young Goals - como se complementam a favor da Juventude',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on sustainable-development and '
             'youth goals, announced for 14 May 2020 at 17h30, with the published '
             'facilitator. The calendar offers the named educational session and, for '
             'some topics, supporting materials. Fees, entry requirements and '
             'application closing are not stated; present enrolment availability is '
             'unknown. The published time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Literacia '
         'digital::2020::13::maio::15h00',
  'title': 'Literacia digital',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on digital literacy, announced '
             'for 13 May 2020 at 15h00, with the published facilitator. The calendar '
             'offers the named educational session and, for some topics, supporting '
             'materials. Fees, entry requirements and application closing are not '
             'stated; present enrolment availability is unknown. The published time is '
             'the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Voluntariado Jovem para a '
         'Natureza e Florestas::2020::12::maio::11h00',
  'title': 'Voluntariado Jovem para a Natureza e Florestas',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on youth volunteering for '
             'nature and forests, announced for 12 May 2020 at 11h00, with the '
             'published facilitator. The calendar offers the named educational session '
             'and, for some topics, supporting materials. Fees, entry requirements and '
             'application closing are not stated; present enrolment availability is '
             'unknown. The published time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::BE MORE - Education througt '
         'art an Erasmus+ experience::2020::11::maio::17h30',
  'title': 'BE MORE - Education througt art an Erasmus+ experience',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on education through art and an '
             'Erasmus experience, announced for 11 May 2020 at 17h30, with the '
             'published facilitator. The calendar offers the named educational session '
             'and, for some topics, supporting materials. Fees, entry requirements and '
             'application closing are not stated; present enrolment availability is '
             'unknown. The published time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Redes '
         'sociais::2020::11::maio::11h00',
  'title': 'Redes sociais',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on social networks, announced '
             'for 11 May 2020 at 11h00, with the published facilitator. The calendar '
             'offers the named educational session and, for some topics, supporting '
             'materials. Fees, entry requirements and application closing are not '
             'stated; present enrolment availability is unknown. The published time is '
             'the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Internet das '
         'coisas::2020::8::maio::15h00',
  'title': 'Internet das coisas',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on the internet of things, '
             'announced for 8 May 2020 at 15h00, with the published facilitator. The '
             'calendar offers the named educational session and, for some topics, '
             'supporting materials. Fees, entry requirements and application closing '
             'are not stated; present enrolment availability is unknown. The published '
             'time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Fake '
         'News::2020::6::maio::11h00',
  'title': 'Fake News',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on misinformation, announced '
             'for 6 May 2020 at 11h00, with the published facilitator. The calendar '
             'offers the named educational session and, for some topics, supporting '
             'materials. Fees, entry requirements and application closing are not '
             'stated; present enrolment availability is unknown. The published time is '
             'the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Uso seguro e responsável da '
         'Internet/Bons hábitos Tecnológicos::2020::4::maio::11h00',
  'title': 'Uso seguro e responsável da Internet/Bons hábitos Tecnológicos',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on safe and responsible '
             'internet use, announced for 4 May 2020 at 11h00, with the published '
             'facilitator. The calendar offers the named educational session and, for '
             'some topics, supporting materials. Fees, entry requirements and '
             'application closing are not stated; present enrolment availability is '
             'unknown. The published time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Dia do Associativismo Jovem: '
         'Participação Juvenil em tempos de Covid::2020::30::abril::18h30',
  'title': 'Dia do Associativismo Jovem: Participação Juvenil em tempos de Covid',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on youth participation during '
             'COVID-19, announced for 30 April 2020 at 18h30, with the published '
             'facilitator. The calendar offers the named educational session and, for '
             'some topics, supporting materials. Fees, entry requirements and '
             'application closing are not stated; present enrolment availability is '
             'unknown. The published time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Empreendedorismo::2020::30::abril::17h30',
  'title': 'Empreendedorismo',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on entrepreneurship, announced '
             'for 30 April 2020 at 17h30, with the published facilitator. The calendar '
             'offers the named educational session and, for some topics, supporting '
             'materials. Fees, entry requirements and application closing are not '
             'stated; present enrolment availability is unknown. The published time is '
             'the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Namorar com Fair '
         'Play::2020::30::abril::15h00',
  'title': 'Namorar com Fair Play',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on dating-violence prevention, '
             'announced for 30 April 2020 at 15h00, with the published facilitator. '
             'The calendar offers the named educational session and, for some topics, '
             'supporting materials. Fees, entry requirements and application closing '
             'are not stated; present enrolment availability is unknown. The published '
             'time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Voluntariado '
         'Jovem::2020::29::abril::17h30',
  'title': 'Voluntariado Jovem',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on volunteering, announced for '
             '29 April 2020 at 17h30, with the published facilitator. The calendar '
             'offers the named educational session and, for some topics, supporting '
             'materials. Fees, entry requirements and application closing are not '
             'stated; present enrolment availability is unknown. The published time is '
             'the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Saúde juvenil: '
         'Cuida-te+::2020::28::abril::17h30',
  'title': 'Saúde juvenil: Cuida-te+',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on youth health, announced for '
             '28 April 2020 at 17h30, with the published facilitator. The calendar '
             'offers the named educational session and, for some topics, supporting '
             'materials. Fees, entry requirements and application closing are not '
             'stated; present enrolment availability is unknown. The published time is '
             'the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Direitos d@s '
         'jovens::2020::27::abril::17h30',
  'title': 'Direitos d@s jovens',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on young people’s rights, '
             'announced for 27 April 2020 at 17h30, with the published facilitator. '
             'The calendar offers the named educational session and, for some topics, '
             'supporting materials. Fees, entry requirements and application closing '
             'are not stated; present enrolment availability is unknown. The published '
             'time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Gestão de Campos de '
         'Férias::2020::24::abril::17h30',
  'title': 'Gestão de Campos de Férias',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on organising holiday camps, '
             'announced for 24 April 2020 at 17h30, with the published facilitator. '
             'The calendar offers the named educational session and, for some topics, '
             'supporting materials. Fees, entry requirements and application closing '
             'are not stated; present enrolment availability is unknown. The published '
             'time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Privacidade '
         'online::2020::24::abril::15h00',
  'title': 'Privacidade online',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on online privacy, announced '
             'for 24 April 2020 at 15h00, with the published facilitator. The calendar '
             'offers the named educational session and, for some topics, supporting '
             'materials. Fees, entry requirements and application closing are not '
             'stated; present enrolment availability is unknown. The published time is '
             'the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'},
 {'key': 'https://ipdj.gov.pt/formacao-online-e-oficinas::Prevenção da radicalização '
         'violenta::2020::23::abril::17h30',
  'title': 'Prevenção da radicalização violenta',
  'url': 'https://ipdj.gov.pt/formacao-online-e-oficinas',
  'language': 'pt',
  'summary_language': 'en',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': [],
  'eligible_countries': [],
  'location': None,
  'deadline': None,
  'closing_local_clock': None,
  'summary': 'Historical online youth-learning session on preventing violent '
             'radicalisation, announced for 23 April 2020 at 17h30, with the published '
             'facilitator. The calendar offers the named educational session and, for '
             'some topics, supporting materials. Fees, entry requirements and '
             'application closing are not stated; present enrolment availability is '
             'unknown. The published time is the session time.',
  'proof': 'Original published facts: https://ipdj.gov.pt/formacao-online-e-oficinas'}]

if __name__ == "__main__":
    sys.exit(main())
