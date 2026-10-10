"""Collect fifteen guarded Google student and early-career programme pages.

This is a bounded programme-information source, not a global vacancy inventory.
Only original factual summaries and canonical links are republished. IIE terms
prohibit automated extraction, so delegated IIE eligibility is never imported.
"""

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
import zlib

SOURCE_ID = "google"
SOURCE_URL = "https://buildyourfuture.withgoogle.com/"
WEBSITE_URL = "https://www.google.com/about/careers/applications/buildyourfuture/"
LANGUAGE = "en"
PUBLISHER_COUNTRY = "US"
PUBLISHER_TYPE = "company"
ATTRIBUTION = "Google — official student and early-career programme information"
SOURCE_NAME = "Google — student and early-career programme information"

_GITHUB = "https://api.github.com/repos/YouthOpps/data-source"
_WORKFLOW_GITHUB = "https://api.github.com/repos/YouthOpps/data-pipeline"
_USER_AGENT = (
    "YouthOpps/1.0 (+https://github.com/YouthOpps/data-pipeline; "
    "public opportunity metadata)"
)
CATEGORIES = {
    "scholarships",
    "grants",
    "internships",
    "jobs",
    "training",
    "fellowships",
    "competitions",
    "volunteering",
}
FAMILY = "youthopps-google-publisher-v1"
FAMILY_SOURCES = {"google"}
COLLECTION_TIMEOUT = 900
RUN_TIMEOUT = 1290
EXPORT_TIMEOUT = 30
_STATE_SCHEMA = 1
_ARTIFACT_NAME = "google-pacing-state"
_REQUEST_STARTS = 0
MAX_PUBLISHER_STARTS = 100
_COLLECTION_BYTES = 0
MAX_COLLECTION_BYTES = 64 * 1024 * 1024
_STATE_PATH = _STATE_LOCK = None
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
        and (class_name is None or class_name in n.attrs.get("class", "").split())
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
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def validate_budget(state):
    """Validate inert state without age-expiring a future publisher embargo."""
    if not isinstance(state, dict) or set(state) - {"interval"} != {
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
        or not numeric(state.get("interval", 6))
        or state.get("interval", 6) < 6
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
        "interval": 6,
    }


def budget_file():
    global _STATE_PATH, _STATE_LOCK
    if _STATE_PATH:
        return
    directory = os.path.join(tempfile.gettempdir(), FAMILY + ("-") + str(os.getuid()))
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
    descriptor = os.open(_STATE_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
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
        raise AdapterError("Corrupt publisher pacing state", "access") from None


def save_budget(state):
    validate_budget(state)
    fd, name = tempfile.mkstemp(prefix=("pending-"), dir=os.path.dirname(_STATE_PATH))
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
    global _REQUEST_STARTS, _PUBLISHER_INTERVAL
    ("Serialize every publisher request start across the complete " "origin set.")
    check_deadline()
    if _REQUEST_STARTS >= MAX_PUBLISHER_STARTS:
        raise AdapterError("Publisher request-start cap reached", "access")
    with locked_budget():
        state = load_budget()
        if state["blocked"]:
            raise AdapterError(
                ("Publisher backoff requires reviewed recovery"), ("access")
            )
        now = time.time()
        starts = [t for t in state["starts"] if t > now - 60]
        _PUBLISHER_INTERVAL = max(_PUBLISHER_INTERVAL, state.get("interval", 6))
        target = max(now, state["not_before"])
        if _PUBLISHER_COMPLETED_MONOTONIC is not None:
            target = max(
                target,
                now
                + max(
                    0,
                    _PUBLISHER_COMPLETED_MONOTONIC
                    + _PUBLISHER_INTERVAL
                    - time.monotonic(),
                ),
            )
        if starts:
            target = max(target, starts[-1] + state.get("interval", 6))
        if len(starts) == 10:
            target = max(target, starts[0] + 60)
        if target + 120 > time.time() + (_PHASE_END - time.monotonic()):
            raise AdapterError("Publisher backoff exceeds phase budget", "access")
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
                time.time() + wait + 120 > _COLLECTION_STARTED + COLLECTION_TIMEOUT
            ):
                raise AdapterError(
                    "Publisher backoff exceeds collection budget", "access"
                )
            time.sleep(wait)
        now = time.time()
        if now < target:
            raise AdapterError(("Publisher pacing clock moved backwards"), ("access"))
        check_deadline()
        if (
            _PUBLISHER_COMPLETED_MONOTONIC is not None
            and time.monotonic() < _PUBLISHER_COMPLETED_MONOTONIC + _PUBLISHER_INTERVAL
        ):
            raise AdapterError(
                "Publisher physical completion gate not elapsed", "access"
            )
        state["starts"] = [t for t in starts if t > now - 60] + [now]
        state["not_before"] = max(state["not_before"], now + state.get("interval", 6))
        state["observed_at"] = now
        save_budget(state)
        _REQUEST_STARTS += 1


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
        and time.monotonic() < _PUBLISHER_COMPLETED_MONOTONIC + _PUBLISHER_INTERVAL
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
            state["not_before"] = max(state["not_before"], now + _PUBLISHER_INTERVAL)
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
    return AdapterError(
        "Publisher transport failure at " + hop + " (" + category + ")",
        "fetch",
    )


def account_collection_bytes(count):
    """Bound aggregate encoded and decoded publisher bytes for this operation."""
    global _COLLECTION_BYTES
    if (
        type(count) is not int
        or count < 0
        or count > MAX_COLLECTION_BYTES - _COLLECTION_BYTES
    ):
        raise AdapterError("Publisher collection byte budget exceeded", "fetch")
    _COLLECTION_BYTES += count


def decode_observed_gzip(body, byte_limit):
    """Decode one complete gzip member within both encoded and decoded bounds."""
    if not isinstance(body, bytes) or len(body) > byte_limit:
        raise AdapterError("Oversized encoded publisher response", "fetch")
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        decoded = decoder.decompress(body, byte_limit + 1)
    except zlib.error:
        raise AdapterError(
            "Invalid publisher gzip checksum or stream", "fetch"
        ) from None
    if (
        len(decoded) > byte_limit
        or not decoder.eof
        or decoder.unconsumed_tail
        or decoder.unused_data
    ):
        raise AdapterError(
            "Incomplete, oversized or multiple-member publisher gzip", "fetch"
        )
    return decoded


def request_bytes(
    url,
    method="GET",
    headers=None,
    payload=None,
    publisher=False,
    byte_limit=8388608,
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
        with publisher_attempt(publisher):
            try:
                response = _OPENER.open(request, timeout=request_timeout(publisher))
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
                    with locked_budget():
                        state = load_budget()
                        state["blocked"] = True
                        state["observed_at"] = time.time()
                        save_budget(state)
                    refused = urllib.parse.urlsplit(current)
                    provenance = urllib.parse.urlunsplit(
                        (
                            refused.scheme,
                            refused.hostname or "",
                            refused.path,
                            "",
                            "",
                        )
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
                    if (
                        urllib.parse.urlsplit(current).scheme == "https"
                        and urllib.parse.urlsplit(destination).scheme != "https"
                    ):
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
                    if publisher:
                        account_collection_bytes(len(body))
                    encoding = response.headers.get("Content-Encoding", "identity")
                    if (
                        publisher
                        and encoding == "gzip"
                        and INPUTS.get(current, {}).get("format") == "redirect-js"
                    ):
                        body = decode_observed_gzip(body, byte_limit)
                        account_collection_bytes(len(body))
                    elif encoding != "identity":
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
                    raise AdapterError(("Unsafe authenticated redirect"), ("publish"))
                if new.netloc != old.netloc:
                    headers = {
                        key: value
                        for key, value in headers.items()
                        if key.lower() != "authorization"
                    }
                if urllib.parse.urlsplit(url).hostname == "api.github.com":
                    raise AdapterError(("Unexpected GitHub API redirect"), ("publish"))
            current = destination
            continue
        return status, response_headers, body
    raise AdapterError("Unresolved redirect", "access")


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
            f"Required publisher input {key} returned HTTP {status}: " + provenance,
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
        raise AdapterError(("Workflow pacing state unavailable"), ("access")) from None


def current_run_identity():
    if (
        os.environ.get("GITHUB_REPOSITORY") != "YouthOpps/data-pipeline"
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
    ):
        raise AdapterError("Unexpected workflow repository", "access")
    values = [
        os.environ.get(key, "") for key in (("GITHUB_RUN_ID"), ("GITHUB_RUN_ATTEMPT"))
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
            or run[("head_repository")][("full_name")] != ("YouthOpps/data-pipeline")
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
        raise AdapterError(("Untrusted completed family attempt"), ("access")) from None


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
                        (f"/actions/runs/{identity}/attempts/" f"{current_attempt - 1}")
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
                raise AdapterError(("Workflow inventory count mismatch"), ("access"))
            if not candidates:
                return None
            ordered = sorted(candidates)
            if len(ordered) > 1 and ordered[-1][0] == ordered[-2][0]:
                raise AdapterError(("Ambiguous family attempt chronology"), ("access"))
            return ordered[-1][2:]
        if len(runs) != 100:
            raise AdapterError("Incomplete workflow inventory", "access")
    raise AdapterError("Workflow inventory exceeds reviewed bound", "access")


def download_inert_artifact(identity, artifact_id, digest=None):
    ("Download a bounded authenticated artifact, stripping " "cross-origin auth.")
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
        raw = os.environ.get("GOOGLE_PACING_BOOTSTRAP", "")
        try:
            bootstrap = json.loads(raw)
            if (
                set(bootstrap) != {"schema", "family", "not_before", "evidence"}
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
            incoming[("not_before")] = max(time.time() + 60, bootstrap[("not_before")])
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
            raise AdapterError("Incomplete family artifact inventory", "access")
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
        combined["interval"] = max(
            local.get("interval", 6), incoming.get("interval", 6)
        )
        combined[("starts")] = sorted(set(local[("starts")] + incoming[("starts")]))[
            -10:
        ]
        save_budget(combined)
    _RESTORED, _STATE_TRUSTED = True, True


def mark_pacing_artifact_ready(runner_parent, runner_root):
    """Signal only a successfully finalized artifact via fresh step output."""
    value = os.environ.get("GITHUB_OUTPUT")
    if not value:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            raise AdapterError("Fresh step output required", "access")
        return
    if not os.path.isabs(value):
        raise AdapterError("Unsafe step output path", "access")
    relative = os.path.relpath(value, runner_root)
    parts = relative.split(os.sep)
    if not parts or any(name in {"", ".", ".."} for name in parts):
        raise AdapterError("Step output outside runner temporary root", "access")
    descriptors = []
    parent = runner_parent
    try:
        for name in parts[:-1]:
            descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            descriptors.append(descriptor)
            info = os.fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise AdapterError("Unsafe step output parent", "access")
            parent = descriptor
        descriptor = os.open(parts[-1], os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, dir_fd=parent)
        descriptors.append(descriptor)
        info = os.fstat(descriptor)
        current = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
            or info.st_nlink != 1
            or info.st_size != 0
            or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise AdapterError("Fresh owned step output required", "access")
        line = b"pacing_ready=true\n"
        if os.write(descriptor, line) != len(line):
            raise AdapterError("Incomplete pacing-ready handoff", "access")
        os.fsync(descriptor)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def export_family_artifact():
    """Export only healthy infrastructure state; invalidate an owned stale file."""
    path = os.environ.get("GOOGLE_PACING_ARTIFACT_PATH")
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
    try:
        directory = os.fstat(parent)
        if (
            directory.st_uid != os.getuid()
            or not stat.S_ISDIR(directory.st_mode)
            or directory.st_mode & 0o022
            or directory.st_mode & 0o700 != 0o700
        ):
            raise AdapterError("Unsafe pacing artifact directory owner", "access")
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
            mark_pacing_artifact_ready(parent, root)
        except BaseException:
            # An incomplete new file cannot masquerade as a valid artifact.
            os.unlink(name, dir_fd=parent)
            raise
    finally:
        os.close(parent)



def prepare_collection():
    global _COLLECTION_STARTED, _STATE_TRUSTED, _COLLECTION_LOCK_FD
    _COLLECTION_STARTED = time.time()
    budget_file()
    if _COLLECTION_LOCK_FD is None:
        path = os.path.join(os.path.dirname(_STATE_PATH), "collection.lock")
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if (
                info.st_uid != os.getuid()
                or not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise OSError("Unsafe collection lock")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(descriptor)
            raise AdapterError(
                ("Another local family collector is active"), ("access")
            ) from None
        _COLLECTION_LOCK_FD = descriptor
    preserve_legacy_pacing()
    if os.environ.get("GITHUB_ACTIONS") == "true":
        restore_family_artifact()
    with locked_budget():
        state = load_budget()
        state["not_before"] = max(state["not_before"], time.time() + 60)
        state["observed_at"] = time.time()
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


def validate_records(records):
    """Validate all real records against the source and consumer contracts."""
    if not isinstance(records, list) or not records:
        raise AdapterError("Empty or invalid opportunity collection", "validate")
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
            if not isinstance(record.get(field), str) or not record[field].strip():
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
                        datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo
                        is None
                    ):
                        raise ValueError()
                except (ValueError, TypeError, AttributeError):
                    raise AdapterError("Invalid record timestamp: " + field, "validate")
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
            or any(not isinstance(value, str) for value in classification["evidence"])
        ):
            raise AdapterError("Invalid classification evidence", "validate")

    validate_owned_records(records)


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
            payload=(json.dumps(payload).encode() if payload is not None else None),
        )
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise AdapterError(
            "GitHub connection failed; outcome unconfirmed", "publish"
        ) from error
    if status == 404 and missing_ok:
        return None
    if status not in (200, 201):
        raise AdapterError(f"GitHub publication HTTP {status}", "publish", status)
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
            raise AdapterError("Unsupported existing source file encoding", "publish")
        files[name] = (
            None if file is None else base64.b64decode(file["content"]).decode("utf-8")
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


def metadata(attempt, previous, records, error=None, checked_at=None):
    outcome = {
        "source": SOURCE_ID,
        "name": SOURCE_NAME,
        "source_url": SOURCE_URL,
        "website_url": WEBSITE_URL,
        "language": LANGUAGE,
        "publisher_country": PUBLISHER_COUNTRY,
        "publisher_type": PUBLISHER_TYPE,
        "attribution": ATTRIBUTION,
        "status": "fail" if error else "success",
        "last_attempt_at": attempt,
        "last_success_at": (previous.get("last_success_at") if error else attempt),
        "last_checked_at": (previous.get("last_checked_at") if error else checked_at),
        "record_count": len(records),
        "message": (
            "Collection failed; last-good data preserved"
            if error
            else f"Collected {len(records)} reviewed Google programme information pages"
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
        or not {
            "source",
            "name",
            "source_url",
            "website_url",
            "language",
            "publisher_country",
            "publisher_type",
            "attribution",
            "status",
            "last_attempt_at",
            "last_success_at",
            "last_checked_at",
            "record_count",
            "message",
            "error",
        }
        <= previous.keys()
        or previous.get("name") != SOURCE_NAME
        or previous.get("publisher_type") != PUBLISHER_TYPE
        or previous.get("attribution") != ATTRIBUTION
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
        or (
            previous.get("status") == "fail"
            and (
                not isinstance(previous.get("error"), str)
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
        for record in records:
            parsed = datetime.fromisoformat(
                record["last_checked_at"].replace("Z", "+00:00")
            )
            if parsed.tzinfo is None or parsed != timestamps["last_checked_at"]:
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
        with phase("prepublication", COLLECTION_TIMEOUT):
            prepare_collection()
            if publishing:
                candidate = read_snapshot()
                if candidate["files"]["data.json"] is not None:
                    candidate_records = json.loads(candidate["files"]["data.json"])
                    candidate_metadata = json.loads(candidate["files"]["metadata.json"])
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


UNRESERVED = set(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def normalize_octets(value):
    if not (
        isinstance(value, str)
        and len(value) <= 8192
        and (not any((ord(c) < 32 or ord(c) == 127 for c in value)))
    ):
        raise AdapterError("Invalid bounded robots policy or URI", "access")
    out = []
    i = 0
    while i < len(value):
        c = value[i]
        if c == "%":
            if not (
                i + 2 < len(value)
                and re.fullmatch("[a-fA-F0-9]{2}", value[i + 1 : i + 3])
            ):
                raise AdapterError("Invalid bounded robots policy or URI", "access")
            n = int(value[i + 1 : i + 3], 16)
            out.append(
                chr(n) if n in UNRESERVED else "%" + value[i + 1 : i + 3].upper()
            )
            i += 3
        else:
            out.append(
                c
                if ord(c) < 128
                else "".join(("%%%02X" % b for b in c.encode("utf-8")))
            )
            i += 1
    return "".join(out)


def groups(body):
    if not (isinstance(body, bytes) and len(body) <= 65536):
        raise AdapterError("Invalid bounded robots policy or URI", "access")
    lines = body.decode("utf-8-sig", "strict").splitlines()
    if not len(lines) <= 2048:
        raise AdapterError("Invalid bounded robots policy or URI", "access")
    result = []
    agents = []
    rules = []
    directives = False
    for line in lines + [""]:
        line = line.split("#", 1)[0].strip()
        if not line:
            if agents and directives:
                result.append((agents, rules))
                agents = []
                rules = []
                directives = False
            continue
        if ":" not in line:
            continue
        key, value = (v.strip() for v in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if directives:
                result.append((agents, rules))
                agents = []
                rules = []
                directives = False
            if not (value == "*" or re.fullmatch("[A-Za-z_-]+", value)):
                raise AdapterError("Invalid bounded robots policy or URI", "access")
            agents.append(value.lower())
            if not len(agents) <= 32:
                raise AdapterError("Invalid bounded robots policy or URI", "access")
        elif key in {"allow", "disallow"} and agents:
            directives = True
            if not value:
                continue
            if not (
                value.startswith("/") and len(value) <= 2048 and (value.count("*") <= 8)
            ):
                raise AdapterError("Invalid bounded robots policy or URI", "access")
            value = normalize_octets(value)
            rules.append((key == "allow", value))
            if not len(rules) <= 512:
                raise AdapterError("Invalid bounded robots policy or URI", "access")
        elif agents:
            directives = True
    if not len(result) <= 100:
        raise AdapterError("Invalid bounded robots policy or URI", "access")
    return result


def rep_allowed(body, url, agent="YouthOpps"):
    p = urllib.parse.urlsplit(url)
    if not (
        p.scheme == "https"
        and p.hostname
        in {"www.google.com", "buildyourfuture.withgoogle.com", "policies.google.com"}
        and (not p.username)
        and (not p.password)
        and (not p.fragment)
        and (p.port in {None, 443})
    ):
        raise AdapterError("Invalid bounded robots policy or URI", "access")
    target = normalize_octets((p.path or "/") + ("?" + p.query if "?" in url else ""))
    best = -1
    chosen = []
    for agents, rules in groups(body):
        scores = [
            0 if a == "*" else len(a) for a in agents if a == "*" or a in agent.lower()
        ]
        if not scores:
            continue
        score = max(scores)
        if score > best:
            best = score
            chosen = list(rules)
        elif score == best:
            chosen.extend(rules)
    matches = []
    for permit, pattern in chosen:
        end = pattern.endswith("$")
        literal = pattern[:-1] if end else pattern
        expr = "^" + re.escape(literal).replace("\\*", ".*") + ("$" if end else "")
        if re.search(expr, target):
            matches.append(
                (
                    len(literal.replace("*", "").encode("utf-8"))
                    - 2 * len(re.findall("%[A-F0-9]{2}", literal)),
                    permit,
                    pattern,
                )
            )
    if not matches:
        return True
    longest = max((v[0] for v in matches))
    return any((v[1] for v in matches if v[0] == longest))


def check_robots(url):
    """Honor fresh ordinary robots policies with query-aware REP matching."""
    parsed = urllib.parse.urlsplit(public_url(url))
    origin = parsed.scheme + "://" + parsed.netloc
    if parsed.path == "/robots.txt":
        return
    if origin not in _ROBOTS:
        status, _, body = request_bytes(
            origin + "/robots.txt", publisher=True, byte_limit=65536
        )
        if status == 404:
            _ROBOTS[origin] = None
        elif status == 200:
            groups(body)
            _ROBOTS[origin] = body
        else:
            raise AdapterError("Publisher robots policy unavailable", "access", status)
    body = _ROBOTS[origin]
    if body is None:
        return
    if not rep_allowed(body, url):
        raise AdapterError("Publisher robots excludes required input", "access")
    # Unknown rate extensions never silently relax the six-second family limit.
    for raw in body.decode("utf-8-sig", "strict").splitlines():
        line = raw.split("#", 1)[0].strip()
        if re.match(r"(?i)^(crawl-delay|request-rate)\s*:", line):
            raise AdapterError(
                "Publisher rate directive requires scoped review", "access"
            )


def preserve_legacy_pacing():
    """Merge all four actual research states, preserving each original inode."""
    root = os.path.join(tempfile.gettempdir(), "issue111-google-research")
    try:
        parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    descriptors = [parent]
    try:
        info = os.fstat(parent)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise AdapterError("Unsafe legacy Google directory", "access")
        incoming = []
        for host in LEGACY_HOSTS:
            try:
                fd = os.open(
                    host + ".budget.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent
                )
            except FileNotFoundError:
                continue
            descriptors.append(fd)
            info = os.fstat(fd)
            if (
                info.st_uid != os.getuid()
                or not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise AdapterError("Unsafe legacy Google pacing file", "access")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise AdapterError("Legacy Google collector active", "access") from None
            raw = os.read(fd, 16385)
            try:
                state = json.loads(raw)
                if (
                    len(raw) > 16384
                    or not isinstance(state, dict)
                    or not {"starts", "until", "blocked"} <= set(state)
                    or set(state) - {"starts", "until", "blocked", "interval"}
                    or not numeric(state["until"])
                    or not numeric(state.get("interval", 6))
                    or state.get("interval", 6) < 6
                    or type(state["blocked"]) is not bool
                    or not isinstance(state["starts"], list)
                    or len(state["starts"]) > 10
                    or any(
                        not numeric(t) or t > time.time() + 1 for t in state["starts"]
                    )
                    or state["starts"] != sorted(set(state["starts"]))
                ):
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                raise AdapterError(
                    "Invalid legacy Google pacing state", "access"
                ) from None
            incoming.append(state)
        with locked_budget():
            state = load_budget()
            starts = set(state["starts"])
            for prior in incoming:
                state["interval"] = max(
                    state.get("interval", 6), prior.get("interval", 6)
                )
                state["not_before"] = max(state["not_before"], prior["until"])
                state["blocked"] = state["blocked"] or prior["blocked"]
                starts.update(prior["starts"])
            ordered = sorted(starts)
            if ordered:
                state["not_before"] = max(
                    state["not_before"], ordered[-1] + state["interval"]
                )
                if len(ordered) > 10:
                    # Retained capacity is ten; wait until every merged start ages.
                    state["not_before"] = max(state["not_before"], ordered[-1] + 60)
            state["starts"] = ordered[-10:]
            state["observed_at"] = time.time()
            save_budget(state)
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


class MaterialFacts(HTMLParser):
    """Bind full visible facts, literal links and all actual filter controls."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.text, self.links, self.controls, self.h1 = [], [], [], []
        self.capture = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if len(values) != len(attrs):
            raise AdapterError("Duplicate material HTML attributes", "parse")
        if tag in {"script", "style"}:
            self.skip += 1
        if tag == "h1":
            self.capture = True
        if not self.skip:
            if tag == "a" and values.get("href"):
                self.links.append(values["href"])
            selected = {
                key: value
                for key, value in values.items()
                if key in {"action", "method", "name", "value", "selected", "data-cy"}
                or key.startswith("data-glue-filter-")
            }
            if selected:
                self.controls.append((tag, sorted(selected.items())))

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.skip -= 1
            if self.skip < 0:
                raise AdapterError("Unbalanced material HTML", "parse")
        if tag == "h1":
            self.capture = False

    def handle_data(self, data):
        if not self.skip and data.strip():
            text = " ".join(data.split())
            self.text.append(text)
            if self.capture:
                self.h1.append(text)


def html_fingerprint(body):
    parser = MaterialFacts()
    parser.feed(body)
    parser.close()
    if parser.skip:
        raise AdapterError("Incomplete material HTML", "parse")
    facts = {"text": parser.text, "links": parser.links, "controls": parser.controls}
    digest = hashlib.sha256(
        json.dumps(facts, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return digest, parser.h1


def embedded_html(body):
    """Decode only the observed ds:0 first string, never execute scripts."""
    text = body.decode("utf-8", "strict")
    document_root(text, complete=False)
    pattern = r"AF_initDataCallback\(\{key: 'ds:0', hash: '[0-9]+', data:\["
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        raise AdapterError("Changed Google embedded document boundary", "parse")
    start = matches[0].end()
    try:
        value, used = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        raise AdapterError("Invalid Google embedded document", "parse") from None
    if (
        not isinstance(value, str)
        or not value.lower().startswith("<!doctype html>")
        or not text[start + used :].startswith(",")
        or len(value.encode()) > 8388608
    ):
        raise AdapterError("Invalid Google embedded document envelope", "parse")
    document_root(value)
    return value


def public_url(value):
    parsed = urllib.parse.urlsplit(value)
    allowed = set(INPUTS) | {origin + "/robots.txt" for origin in ROBOTS_ORIGINS}
    if (
        value not in allowed
        or parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port
        or parsed.fragment
    ):
        raise AdapterError("Unreviewed Google public route", "access")
    return value


def read_input(url):
    info = INPUTS[url]
    _, _, body = fetch_public(url)
    if info["format"] == "embedded-html":
        digest, h1 = html_fingerprint(embedded_html(body))
    elif info["format"] == "html":
        text = body.decode("utf-8", "strict")
        root = document_root(
            text, complete=urllib.parse.urlsplit(url).hostname != "policies.google.com"
        )
        if url == SOURCE_URL:
            scripts = [
                urllib.parse.urljoin(url, node.attrs["src"])
                for node in nodes(root, "script")
                if node.attrs.get("src", "").startswith("main.")
            ]
            reviewed = [
                route
                for route, value in INPUTS.items()
                if value["format"] == "redirect-js"
            ]
            if scripts != reviewed:
                raise AdapterError(
                    "Changed original Google redirect script link", "parse"
                )
        digest, h1 = html_fingerprint(text)
    elif info["format"] == "redirect-js":
        text = body.decode("utf-8", "strict")
        # Review original redirect script as bytes: no execution or downloaded code.
        digest, h1 = hashlib.sha256(body).hexdigest(), []
        if 'const Q="' + WEBSITE_URL + '"' not in text:
            raise AdapterError("Changed original Google redirect destination", "parse")
    else:
        raise AdapterError("Invalid guarded Google input type", "parse")
    if digest != info["sha256"] or h1 != info["h1"]:
        raise AdapterError(
            "Reviewed Google material facts/frontier changed: " + url, "parse"
        )
    return digest


def read_pages():
    """Exhaust all twenty-three guarded inputs plus three fresh robots origins."""
    return {url: read_input(url) for url in INPUTS}


def profile_identity(profile):
    return hashlib.sha256((SOURCE_ID + "|" + profile["url"]).encode()).hexdigest()[:24]


def validate_owned_records(records):
    profiles = {profile_identity(profile): profile for profile in PROFILES}
    if len(records) != 15 or {record["id"] for record in records} != set(profiles):
        raise AdapterError("Incomplete Google programme identity partition", "validate")
    temporal = {
        "created_at",
        "updated_at",
        "first_seen_at",
        "last_seen_at",
        "last_checked_at",
    }
    for record in records:
        profile = profiles[record["id"]]
        expected = dict(profile["fields"], id=profile_identity(profile))
        if {
            key: value for key, value in record.items() if key not in temporal
        } != expected:
            raise AdapterError(
                "Changed owned Google record material fields", "validate"
            )
        values = {
            key: datetime.fromisoformat(record[key].replace("Z", "+00:00"))
            for key in temporal
        }
        if not (
            values["created_at"] <= values["updated_at"] <= values["last_checked_at"]
            and values["first_seen_at"]
            <= values["last_seen_at"]
            <= values["last_checked_at"]
        ):
            raise AdapterError("Invalid owned Google chronology", "validate")


def parse_inventory(pages):
    """Normalize only the entire independently reviewed fifteen-page partition."""
    if set(pages) != set(INPUTS) or any(
        pages[url] != info["sha256"] for url, info in INPUTS.items()
    ):
        raise AdapterError("Incomplete guarded Google frontier", "parse")
    now = utc_now()
    records = []
    for profile in PROFILES:
        record = json.loads(json.dumps(profile["fields"]))
        record["id"] = profile_identity(profile)
        record.update(
            {
                key: now
                for key in (
                    "created_at",
                    "updated_at",
                    "first_seen_at",
                    "last_seen_at",
                    "last_checked_at",
                )
            }
        )
        records.append(record)
    validate_records(records)
    return records


def collect():
    if _PHASE_END is not None:
        check_deadline()
        return parse_inventory(read_pages())
    with execution():
        with phase("prepublication", COLLECTION_TIMEOUT):
            attempt = utc_now()
            prepare_collection()
            records = parse_inventory(read_pages())
            fresh_records(records, [], attempt, utc_now())
            return records


LEGACY_HOSTS = [
    "buildyourfuture.withgoogle.com",
    "www.google.com",
    "policies.google.com",
    "summerofcode.withgoogle.com",
]
ROBOTS_ORIGINS = [
    "https://www.google.com",
    "https://buildyourfuture.withgoogle.com",
    "https://policies.google.com",
]
INPUTS = {
    "https://www.google.com/about/careers/applications/buildyourfuture/": {
        "format": "embedded-html",
        "sha256": "f00a00ff645287bdb255e11c574e66c6893bbc391f3ec863e139e2be62e15400",
        "h1": ["Build " "your " "future " "with " "Google"],
    },
    "https://www.google.com/about/careers/applications/buildyourfuture/scholarships/": {
        "format": "embedded-html",
        "sha256": "db3587e8041ae004e2bd5bc78fbb928c06064a2bcb4bf44b9400466a45087244",
        "h1": ["Scholarships"],
    },
    "https://www.google.com/about/careers/applications/buildyourfuture/programs/": {
        "format": "embedded-html",
        "sha256": "f4c4bef3401b0ef6d41f2fb2836833e88c7883e0a432f48f4f3ede402052c4fc",
        "h1": ["Programs"],
    },
    "https://www.google.com/about/careers/applications/buildyourfuture/apprenticeships/": {
        "format": "embedded-html",
        "sha256": "bd8b7940591ed28d649df8d632c6d1e7d9a365f80434ec987f5bae273f8bc395",
        "h1": ["Apprenticeships"],
    },
    "https://www.google.com/about/careers/applications/internships": {
        "format": "embedded-html",
        "sha256": "52f0eda94acb9fd1b04680783185c8cf31cec3266e6830bf1e594ed403f17eb1",
        "h1": ["Internships " "at Google"],
    },
    "https://www.google.com/about/careers/applications/buildyourfuture/scholarships/googleireland/": {
        "format": "embedded-html",
        "sha256": "0a9162190651cd2919e848014cf5839eb4831585689aa4a6cb509714d802c5ac",
        "h1": [
            "Scholarship: "
            "Google "
            "Ireland "
            "for "
            "women "
            "in "
            "computer "
            "science"
        ],
    },
    "https://www.google.com/about/careers/applications/buildyourfuture/programs/summer-of-code/": {
        "format": "embedded-html",
        "sha256": "eb3b55cf667e7a51b31c86201b3f6584ca2da302c7f7a8e9eef01616115bdec4",
        "h1": ["Google " "Summer " "of " "Code"],
    },
    "https://www.google.com/about/careers/applications/buildyourfuture/programs/googlestudentambassador": {
        "format": "embedded-html",
        "sha256": "2a7e4593031da7767bc365921bc2398d7594dabfe3598eecc90e99e98f3cba5a",
        "h1": ["US " "Google " "Student " "Ambassador " "Program"],
    },
    "https://www.google.com/about/careers/applications/early-career": {
        "format": "embedded-html",
        "sha256": "46876faa1a23697f18a910a154690e4cbbcfe8bf83b2352166f25579e5c837c5",
        "h1": ["Early " "career at " "Google"],
    },
    "https://www.google.com/about/careers/applications/programs/apm/": {
        "format": "embedded-html",
        "sha256": "3decc2b97de2f6da9ecbce68bb2efb7699feceb55feae8491c3ae55234b0773c",
        "h1": ["Google " "Associate " "Product " "Manager " "Program"],
    },
    "https://www.google.com/about/careers/applications/buildyourfuture/programs/lsi/": {
        "format": "embedded-html",
        "sha256": "90acc1e0d4fe508e49c7cb138f4689889b4b9ec1f12eb171bab80499d2532151",
        "h1": ["US " "Legal " "Summer " "Institute"],
    },
    "https://www.google.com/about/careers/applications/students/engineering-and-technical-internships/": {
        "format": "embedded-html",
        "sha256": "73a8106d637c94e5a4362a98c53d99b46a9dc07ea405ac071fcb18d3e8238112",
        "h1": ["Engineering " "and " "technical " "internships"],
    },
    "https://www.google.com/about/careers/applications/research-internships/": {
        "format": "embedded-html",
        "sha256": "7c7a925b2a60f54eaeb6201ca78a9b6ebc59bc1957f5383575557c85fdf64699",
        "h1": ["Research " "internships"],
    },
    "https://www.google.com/about/careers/applications/students/business-internships/": {
        "format": "embedded-html",
        "sha256": "fe573aa29a9a7280bad615ea8ea379811a45a3a3d9f872db808f554b109be8a4",
        "h1": ["Business " "internships"],
    },
    "https://www.google.com/about/careers/applications/programs/aicareercatalyst": {
        "format": "embedded-html",
        "sha256": "c5696a1a3c6e3c40668f5e1c769e350605442fcb19d357ba1a0eb2ebef06c259",
        "h1": ["Google " "AI " "Career " "Catalyst " "Program"],
    },
    "https://www.google.com/about/careers/applications/buildyourfuture/programs/americas-sales-associate/": {
        "format": "embedded-html",
        "sha256": "d7d4d59d9fee6f696217c0eef4d12de5a7ac4555b3b8a8f1595ec8f75da8c555",
        "h1": ["Americas " "Sales " "Associate " "Program"],
    },
    "https://www.google.com/about/careers/applications/programs/spark/": {
        "format": "embedded-html",
        "sha256": "1a349c3be3a82b615036ec3fab00a27b9e08276e629de4d36c96805e7a9bc583",
        "h1": ["Your " "Future " "Starts " "Here"],
    },
    "https://www.google.com/about/careers/applications/buildyourfuture/programs/genesis": {
        "format": "embedded-html",
        "sha256": "19d20a4c26dc96245e022572f2e98a4143326a48823508d3ff339537c2702bf0",
        "h1": ["Genesis " "Finance " "Program"],
    },
    "https://www.google.com/about/careers/applications/buildyourfuture/programs/emea-lsi/": {
        "format": "embedded-html",
        "sha256": "cfd25d4c1993c424fa56944b8bfb636c99b5d6e824a437705f0667059a5f3586",
        "h1": ["EMEA " "Legal " "Summer " "Institute " "(LSI)"],
    },
    "https://www.google.com/about/careers/applications/programs/apmm/": {
        "format": "embedded-html",
        "sha256": "a2c440cefe084f9762a3c36a5864ab13d8b083ccfff6d65a60afd3e280410284",
        "h1": ["Connect " "users to " "the " "magic"],
    },
    "https://buildyourfuture.withgoogle.com/": {
        "format": "html",
        "sha256": "74cd3fb423f468938fbbb5a31693d0698bc171bb7213f3b9ed5994794e0b16f3",
        "h1": [],
    },
    "https://policies.google.com/terms": {
        "format": "html",
        "sha256": "635543b17b2fa9d38d9587c93a86c9bede0bef528bd298278441ab1a22c74e20",
        "h1": [
            "Privacy & Terms",
            "Google Terms of Service",
            "Terms",
            "Your relationship with Google",
            "Using Google services",
            "Content in Google services",
            "Software in Google services",
            "In case of problems or disagreements",
            "About these terms",
            "Definitions",
        ],
    },
    "https://buildyourfuture.withgoogle.com/main.9ca0c65bf5ce4c16.js": {
        "format": "redirect-js",
        "sha256": "5a9c216c54299e5a67467f5fa8584e2fc5ce14cb5177a85a9aef0a2e514e1c7d",
        "h1": [],
    },
}
PROFILES = [
    {
        "key": "gsoc",
        "url": "https://www.google.com/about/careers/applications/buildyourfuture/programs/summer-of-code/",
        "inputs": [
            "https://www.google.com/about/careers/applications/buildyourfuture/programs/summer-of-code/"
        ],
        "fields": {
            "title": "Google Summer of Code",
            "url": "https://www.google.com/about/careers/applications/buildyourfuture/programs/summer-of-code/",
            "source": "google",
            "source_url": "https://buildyourfuture.withgoogle.com/",
            "published_at": None,
            "summary": "Google Summer of Code pairs students or beginner open-source "
            "contributors aged over 18 with mentors for 12+ week coding "
            "projects. Applicants need work eligibility where they live, "
            "non-embargoed residence and at most two prior acceptances. "
            "Passed evaluations earn a stipend; amounts are not specified "
            "here. The 2026 application window was March 16-31. The page "
            "also labels parts of its timeline 2025; check linked official "
            "rules for additional conditions.",
            "tags": [],
            "location": None,
            "deadline": "2026-03-31",
            "language": "en",
            "summary_language": "en",
            "category": "training",
            "categories": ["training", "grants"],
            "kind": "programme-overview",
            "host_countries": [],
            "eligible_countries": [],
            "publisher_country": "US",
            "status": "expired",
            "classification": {
                "method": "editorial-review",
                "status": "classified",
                "evidence": [
                    "Google official programme information; "
                    "original factual summary",
                    "Bounded programmeoverview; current "
                    "global vacancies not collected",
                    "https://www.google.com/about/careers/applications/buildyourfuture/programs/summer-of-code/",
                ],
            },
        },
    },
    {
        "key": "student-ambassador",
        "url": "https://www.google.com/about/careers/applications/buildyourfuture/programs/googlestudentambassador",
        "inputs": [
            "https://www.google.com/about/careers/applications/buildyourfuture/programs/googlestudentambassador",
            "https://www.google.com/about/careers/applications/buildyourfuture/programs/",
        ],
        "fields": {
            "title": "US Google Student Ambassador Program",
            "url": "https://www.google.com/about/careers/applications/buildyourfuture/programs/googlestudentambassador",
            "source": "google",
            "source_url": "https://buildyourfuture.withgoogle.com/",
            "published_at": None,
            "summary": "This paid, remote and campus-based programme recruits U.S. "
            "college students to connect peers with Google AI products, "
            "host campus events and share product insights. Active campus "
            "involvement, interest in AI and experience organising events "
            "or social content strengthen applications. Recruitment "
            "follows the academic calendar, and the page says applications "
            "are closed. The programme index mistakenly repeats a Summer "
            "of Code description; the individual page supplies these "
            "facts.",
            "tags": [],
            "location": None,
            "deadline": None,
            "language": "en",
            "summary_language": "en",
            "category": "jobs",
            "categories": ["jobs"],
            "kind": "programme-overview",
            "host_countries": ["US"],
            "eligible_countries": [],
            "publisher_country": "US",
            "status": "expired",
            "classification": {
                "method": "editorial-review",
                "status": "classified",
                "evidence": [
                    "Google official programme information; "
                    "original factual summary",
                    "Bounded programmeoverview; current "
                    "global vacancies not collected",
                    "https://www.google.com/about/careers/applications/buildyourfuture/programs/googlestudentambassador",
                    "https://www.google.com/about/careers/applications/buildyourfuture/programs/",
                ],
            },
        },
    },
    {
        "key": "ai-career-catalyst",
        "url": "https://www.google.com/about/careers/applications/programs/aicareercatalyst",
        "inputs": [
            "https://www.google.com/about/careers/applications/programs/aicareercatalyst"
        ],
        "fields": {
            "title": "Google AI Career Catalyst Program",
            "url": "https://www.google.com/about/careers/applications/programs/aicareercatalyst",
            "source": "google",
            "source_url": "https://buildyourfuture.withgoogle.com/",
            "published_at": None,
            "summary": "The AI Career Catalyst programme offers early-career software "
            "engineers 13 months of development in AI and infrastructure: "
            "three four-month rotations, senior mentorship and a final "
            "full-time team placement matched to business needs. It is "
            "based in Sunnyvale, with possible other U.S. rotations. The "
            "page lists a June 2027 start but says the 2026 cohort is "
            "closed and the 2027 intake will open this summer; current "
            "intake status and detailed qualifications need checking.",
            "tags": [],
            "location": None,
            "deadline": None,
            "language": "en",
            "summary_language": "en",
            "category": "jobs",
            "categories": ["jobs"],
            "kind": "programme-overview",
            "host_countries": ["US"],
            "eligible_countries": [],
            "publisher_country": "US",
            "status": "unknown",
            "classification": {
                "method": "editorial-review",
                "status": "classified",
                "evidence": [
                    "Google official programme information; "
                    "original factual summary",
                    "Bounded programmeoverview; current "
                    "global vacancies not collected",
                    "https://www.google.com/about/careers/applications/programs/aicareercatalyst",
                ],
            },
        },
    },
    {
        "key": "americas-sales-associate",
        "url": "https://www.google.com/about/careers/applications/buildyourfuture/programs/americas-sales-associate/",
        "inputs": [
            "https://www.google.com/about/careers/applications/buildyourfuture/programs/americas-sales-associate/"
        ],
        "fields": {
            "title": "Americas Sales Associate Program",
            "url": "https://www.google.com/about/careers/applications/buildyourfuture/programs/americas-sales-associate/",
            "source": "google",
            "source_url": "https://buildyourfuture.withgoogle.com/",
            "published_at": None,
            "summary": "This U.S. sales programme has quarterly starts and two years "
            "of rotations, with mentors and routes into account-management "
            "or analytical roles. Both tracks require a degree or "
            "equivalent practical experience and one year of relevant "
            "sales/advertising or analytical experience. Track-specific "
            "communication, client, Google Ads, data-visualisation or "
            "spreadsheet/SQL skills also apply. The page links two "
            "applications and says they are open; individual vacancy "
            "conditions and closing dates were not collected.",
            "tags": [],
            "location": None,
            "deadline": None,
            "language": "en",
            "summary_language": "en",
            "category": "jobs",
            "categories": ["jobs"],
            "kind": "programme-overview",
            "host_countries": ["US"],
            "eligible_countries": [],
            "publisher_country": "US",
            "status": "unknown",
            "classification": {
                "method": "editorial-review",
                "status": "classified",
                "evidence": [
                    "Google official programme information; "
                    "original factual summary",
                    "Bounded programmeoverview; current "
                    "global vacancies not collected",
                    "https://www.google.com/about/careers/applications/buildyourfuture/programs/americas-sales-associate/",
                ],
            },
        },
    },
    {
        "key": "apm",
        "url": "https://www.google.com/about/careers/applications/programs/apm/",
        "inputs": ["https://www.google.com/about/careers/applications/programs/apm/"],
        "fields": {
            "title": "Google Associate Product Manager Program",
            "url": "https://www.google.com/about/careers/applications/programs/apm/",
            "source": "google",
            "source_url": "https://buildyourfuture.withgoogle.com/",
            "published_at": None,
            "summary": "APM offers recent graduates two one-year product-management "
            "rotations, mentors and professional development; summer "
            "interns join one team for 12 weeks. Internship applicants "
            "must graduate within 12 months after the internship. "
            "Technical understanding, product judgement, analysis and "
            "communication matter. U.S. applications are listed as "
            "September 22-October 6 and London internships October "
            "5-November 12, without a year; other graduate intake dates "
            "are unknown. Check actual role requirements.",
            "tags": [],
            "location": None,
            "deadline": None,
            "language": "en",
            "summary_language": "en",
            "category": "jobs",
            "categories": ["jobs", "internships"],
            "kind": "programme-overview",
            "host_countries": ["US", "GB", "DE", "CH", "JP"],
            "eligible_countries": [],
            "publisher_country": "US",
            "status": "unknown",
            "classification": {
                "method": "editorial-review",
                "status": "classified",
                "evidence": [
                    "Google official programme information; "
                    "original factual summary",
                    "Bounded programmeoverview; current "
                    "global vacancies not collected",
                    "https://www.google.com/about/careers/applications/programs/apm/",
                ],
            },
        },
    },
    {
        "key": "spark",
        "url": "https://www.google.com/about/careers/applications/programs/spark/",
        "inputs": ["https://www.google.com/about/careers/applications/programs/spark/"],
        "fields": {
            "title": "Your Future Starts Here",
            "url": "https://www.google.com/about/careers/applications/programs/spark/",
            "source": "google",
            "source_url": "https://buildyourfuture.withgoogle.com/",
            "published_at": None,
            "summary": "Spark at Google develops early-career digital-marketing and "
            "sales staff through three months of initial training, ongoing "
            "learning and mentorship while advising small-business "
            "clients. Prior sales experience is not required; specific job "
            "qualifications still apply. Locations include New York, the "
            "Bay Area and Mexico City, generally with three office days "
            "and two flexible days weekly. U.S. applicants need U.S. work "
            "authorisation. Apply only when a Spark role is posted; "
            "current vacancies and deadlines are unknown.",
            "tags": [],
            "location": None,
            "deadline": None,
            "language": "en",
            "summary_language": "en",
            "category": "jobs",
            "categories": ["jobs"],
            "kind": "programme-overview",
            "host_countries": ["US", "MX"],
            "eligible_countries": [],
            "publisher_country": "US",
            "status": "unknown",
            "classification": {
                "method": "editorial-review",
                "status": "classified",
                "evidence": [
                    "Google official programme information; "
                    "original factual summary",
                    "Bounded programmeoverview; current "
                    "global vacancies not collected",
                    "https://www.google.com/about/careers/applications/programs/spark/",
                ],
            },
        },
    },
    {
        "key": "genesis-finance",
        "url": "https://www.google.com/about/careers/applications/buildyourfuture/programs/genesis",
        "inputs": [
            "https://www.google.com/about/careers/applications/buildyourfuture/programs/genesis"
        ],
        "fields": {
            "title": "Genesis Finance Program",
            "url": "https://www.google.com/about/careers/applications/buildyourfuture/programs/genesis",
            "source": "google",
            "source_url": "https://buildyourfuture.withgoogle.com/",
            "published_at": None,
            "summary": "Genesis Finance develops early-career finance staff through "
            "four six-month rotations over two years, training, mentors "
            "and a peer cohort. No specific experience is universally "
            "required; individual job qualifications apply, and applicants "
            "must have work authorisation in the country chosen. The page "
            "lists a summer 2026 start and fall 2025 closing without a "
            "day, alongside usual regional recruitment seasons. Current "
            "vacancies are not verified; the location list contains "
            "duplicate Ireland wording.",
            "tags": [],
            "location": None,
            "deadline": None,
            "language": "en",
            "summary_language": "en",
            "category": "jobs",
            "categories": ["jobs"],
            "kind": "programme-overview",
            "host_countries": ["US", "IE", "SG"],
            "eligible_countries": [],
            "publisher_country": "US",
            "status": "unknown",
            "classification": {
                "method": "editorial-review",
                "status": "classified",
                "evidence": [
                    "Google official programme information; "
                    "original factual summary",
                    "Bounded programmeoverview; current "
                    "global vacancies not collected",
                    "https://www.google.com/about/careers/applications/buildyourfuture/programs/genesis",
                ],
            },
        },
    },
    {
        "key": "us-lsi",
        "url": "https://www.google.com/about/careers/applications/buildyourfuture/programs/lsi/",
        "inputs": [
            "https://www.google.com/about/careers/applications/buildyourfuture/programs/lsi/"
        ],
        "fields": {
            "title": "US Legal Summer Institute",
            "url": "https://www.google.com/about/careers/applications/buildyourfuture/programs/lsi/",
            "source": "google",
            "source_url": "https://buildyourfuture.withgoogle.com/",
            "published_at": None,
            "summary": "US LSI combines paid partner-law-firm summer work with a "
            "Google campus week, not an in-house Google internship. Rising "
            "2L/3E students need U.S. work authorisation, an ABA-approved "
            "law school, transcripts, résumé and essays, and must attend "
            "the whole Google week. The page says 2027 applications "
            "closed, yet its 2028 calendar lists November 30, 2026-January "
            "11, 2027 applications. Its class-of-2028 and spring-2029 "
            "eligibility wording conflicts; verify cohort eligibility "
            "before applying.",
            "tags": [],
            "location": None,
            "deadline": "2027-01-11",
            "language": "en",
            "summary_language": "en",
            "category": "internships",
            "categories": ["internships"],
            "kind": "programme-overview",
            "host_countries": ["US"],
            "eligible_countries": [],
            "publisher_country": "US",
            "status": "unknown",
            "classification": {
                "method": "editorial-review",
                "status": "classified",
                "evidence": [
                    "Google official programme information; "
                    "original factual summary",
                    "Bounded programmeoverview; current "
                    "global vacancies not collected",
                    "https://www.google.com/about/careers/applications/buildyourfuture/programs/lsi/",
                ],
            },
        },
    },
    {
        "key": "emea-lsi",
        "url": "https://www.google.com/about/careers/applications/buildyourfuture/programs/emea-lsi/",
        "inputs": [
            "https://www.google.com/about/careers/applications/buildyourfuture/programs/emea-lsi/",
            "https://www.google.com/about/careers/applications/early-career",
        ],
        "fields": {
            "title": "EMEA Legal Summer Institute (LSI)",
            "url": "https://www.google.com/about/careers/applications/buildyourfuture/programs/emea-lsi/",
            "source": "google",
            "source_url": "https://buildyourfuture.withgoogle.com/",
            "published_at": None,
            "summary": "EMEA LSI combines paid partner-law-firm experience with a "
            "Google learning week and optional six-month legal mentorship. "
            "Law students, apprentices or recent graduates need work "
            "authorisation in the placement country, academic excellence, "
            "a résumé, English and the local language. Applications are "
            "closed; the Google week was September 7-11, 2026. Placement "
            "timing varies, and travel or accommodation is not covered. "
            "The detail lists 23 countries although the programme index "
            "says 19.",
            "tags": [],
            "location": None,
            "deadline": None,
            "language": "en",
            "summary_language": "en",
            "category": "internships",
            "categories": ["internships"],
            "kind": "programme-overview",
            "host_countries": [
                "AT",
                "BE",
                "EG",
                "EE",
                "FR",
                "DE",
                "GB",
                "IE",
                "IL",
                "IT",
                "KE",
                "LV",
                "LT",
                "NL",
                "PL",
                "PT",
                "RO",
                "SA",
                "ES",
                "SE",
                "CH",
                "TR",
                "AE",
            ],
            "eligible_countries": [],
            "publisher_country": "US",
            "status": "expired",
            "classification": {
                "method": "editorial-review",
                "status": "classified",
                "evidence": [
                    "Google official programme information; "
                    "original factual summary",
                    "Bounded programmeoverview; current "
                    "global vacancies not collected",
                    "https://www.google.com/about/careers/applications/buildyourfuture/programs/emea-lsi/",
                    "https://www.google.com/about/careers/applications/early-career",
                ],
            },
        },
    },
    {
        "key": "apmm",
        "url": "https://www.google.com/about/careers/applications/programs/apmm/",
        "inputs": [
            "https://www.google.com/about/careers/applications/programs/apmm/",
            "https://www.google.com/about/careers/applications/early-career",
        ],
        "fields": {
            "title": "Connect users to the magic",
            "url": "https://www.google.com/about/careers/applications/programs/apmm/",
            "source": "google",
            "source_url": "https://buildyourfuture.withgoogle.com/",
            "published_at": None,
            "summary": "The Associate Product Marketing Manager programme lasts two "
            "years according to the index and offers two roles, training "
            "and mentors. Applicants may be students or have a few years "
            "of experience; no single prior experience is universally "
            "required. Technology and marketing interest, leadership, "
            "problem solving and creative analysis matter. Participants "
            "must start in their home country, and visa sponsorship is "
            "unavailable. Roles open according to business demand; "
            "detailed job requirements and deadlines vary.",
            "tags": [],
            "location": None,
            "deadline": None,
            "language": "en",
            "summary_language": "en",
            "category": "jobs",
            "categories": ["jobs"],
            "kind": "programme-overview",
            "host_countries": [],
            "eligible_countries": [],
            "publisher_country": "US",
            "status": "unknown",
            "classification": {
                "method": "editorial-review",
                "status": "classified",
                "evidence": [
                    "Google official programme information; "
                    "original factual summary",
                    "Bounded programmeoverview; current "
                    "global vacancies not collected",
                    "https://www.google.com/about/careers/applications/programs/apmm/",
                    "https://www.google.com/about/careers/applications/early-career",
                ],
            },
        },
    },
    {
        "key": "google-ireland",
        "url": "https://www.google.com/about/careers/applications/buildyourfuture/scholarships/googleireland/",
        "inputs": [
            "https://www.google.com/about/careers/applications/buildyourfuture/scholarships/googleireland/",
            "https://www.google.com/about/careers/applications/buildyourfuture/scholarships/",
        ],
        "fields": {
            "title": "Scholarship: Google Ireland for women in computer science",
            "url": "https://www.google.com/about/careers/applications/buildyourfuture/scholarships/googleireland/",
            "source": "google",
            "source_url": "https://buildyourfuture.withgoogle.com/",
            "published_at": None,
            "summary": "Google Ireland awards selected undergraduate computer-science "
            "students EUR 5,000 annually for two consecutive years, based "
            "on leadership and academic background. Google states the "
            "application deadline was October 5, 2026 and encourages women "
            "to apply. Complete minimum eligibility is delegated to IIE "
            "and is not verified by this collector; follow the "
            "administrator link before relying on eligibility. No Irish "
            "citizenship or women-only requirement is inferred from the "
            "programme name.",
            "tags": [],
            "location": None,
            "deadline": "2026-10-05",
            "language": "en",
            "summary_language": "en",
            "category": "scholarships",
            "categories": ["scholarships"],
            "kind": "programme-overview",
            "host_countries": ["IE"],
            "eligible_countries": [],
            "publisher_country": "US",
            "status": "expired",
            "classification": {
                "method": "editorial-review",
                "status": "classified",
                "evidence": [
                    "Google official programme information; "
                    "original factual summary",
                    "Bounded programmeoverview; current "
                    "global vacancies not collected",
                    "https://www.google.com/about/careers/applications/buildyourfuture/scholarships/googleireland/",
                    "https://www.google.com/about/careers/applications/buildyourfuture/scholarships/",
                ],
            },
        },
    },
    {
        "key": "apprenticeships",
        "url": "https://www.google.com/about/careers/applications/buildyourfuture/apprenticeships/",
        "inputs": [
            "https://www.google.com/about/careers/applications/buildyourfuture/apprenticeships/"
        ],
        "fields": {
            "title": "Apprenticeships",
            "url": "https://www.google.com/about/careers/applications/buildyourfuture/apprenticeships/",
            "source": "google",
            "source_url": "https://buildyourfuture.withgoogle.com/",
            "published_at": None,
            "summary": "Google apprenticeships offer paid work, training and "
            "benefits. Applicants need local work rights and "
            "English/local-language fluency; generally age over 18, with a "
            "Swiss exception. U.S. programmes take 40 hours weekly, "
            "exclude same-field bachelor's students/graduates and require "
            "under 12 months' experience (under six with an associate "
            "degree). This overview covers 20 tracks; individual vacancies "
            "are not collected. Several cohort dates are stale or closed. "
            "India Software Application Development says 12 months in the "
            "heading and 24 in the text.",
            "tags": [],
            "location": None,
            "deadline": None,
            "language": "en",
            "summary_language": "en",
            "category": "jobs",
            "categories": ["jobs", "training"],
            "kind": "programme-overview",
            "host_countries": ["US", "FR", "IE", "CH", "GB", "IN", "BR"],
            "eligible_countries": [],
            "publisher_country": "US",
            "status": "unknown",
            "classification": {
                "method": "editorial-review",
                "status": "classified",
                "evidence": [
                    "Google official programme information; "
                    "original factual summary",
                    "Bounded programmeoverview; current "
                    "global vacancies not collected",
                    "https://www.google.com/about/careers/applications/buildyourfuture/apprenticeships/",
                ],
            },
        },
    },
    {
        "key": "engineering-internships",
        "url": "https://www.google.com/about/careers/applications/students/engineering-and-technical-internships/",
        "inputs": [
            "https://www.google.com/about/careers/applications/students/engineering-and-technical-internships/",
            "https://www.google.com/about/careers/applications/internships",
        ],
        "fields": {
            "title": "Engineering and technical internships",
            "url": "https://www.google.com/about/careers/applications/students/engineering-and-technical-internships/",
            "source": "google",
            "source_url": "https://buildyourfuture.withgoogle.com/",
            "published_at": None,
            "summary": "Google describes software, user-experience, "
            "product-management, mechanical and hardware internships, not "
            "an exhaustive vacancy list. Software roles serve "
            "undergraduate and graduate or PhD students; mechanical roles "
            "require full-time degree study in mechanical engineering or a "
            "related field, and hardware roles degree study in electrical "
            "engineering with system-design emphasis. Dates and "
            "qualifications vary by role and location. Google offers "
            "interns compensation, benefits and mentorship; specific "
            "vacancies were not collected.",
            "tags": [],
            "location": None,
            "deadline": None,
            "language": "en",
            "summary_language": "en",
            "category": "internships",
            "categories": ["internships"],
            "kind": "programme-overview",
            "host_countries": [],
            "eligible_countries": [],
            "publisher_country": "US",
            "status": "unknown",
            "classification": {
                "method": "editorial-review",
                "status": "classified",
                "evidence": [
                    "Google official programme information; "
                    "original factual summary",
                    "Bounded programmeoverview; current "
                    "global vacancies not collected",
                    "https://www.google.com/about/careers/applications/students/engineering-and-technical-internships/",
                    "https://www.google.com/about/careers/applications/internships",
                ],
            },
        },
    },
    {
        "key": "research-internships",
        "url": "https://www.google.com/about/careers/applications/research-internships/",
        "inputs": [
            "https://www.google.com/about/careers/applications/research-internships/",
            "https://www.google.com/about/careers/applications/internships",
        ],
        "fields": {
            "title": "Research internships",
            "url": "https://www.google.com/about/careers/applications/research-internships/",
            "source": "google",
            "source_url": "https://buildyourfuture.withgoogle.com/",
            "published_at": None,
            "summary": "Google research internships let final-year PhD students work "
            "with researchers on computing and scientific projects. A "
            "separate Student Researcher programme hires students into "
            "projects aligned to company research priorities, with varying "
            "locations and durations. Google offers interns compensation, "
            "benefits and mentorship. These are programme descriptions; "
            "individual project qualifications, current vacancies and "
            "deadlines were not collected. The linked student-engagement "
            "site supplies additional information.",
            "tags": [],
            "location": None,
            "deadline": None,
            "language": "en",
            "summary_language": "en",
            "category": "internships",
            "categories": ["internships"],
            "kind": "programme-overview",
            "host_countries": [],
            "eligible_countries": [],
            "publisher_country": "US",
            "status": "unknown",
            "classification": {
                "method": "editorial-review",
                "status": "classified",
                "evidence": [
                    "Google official programme information; "
                    "original factual summary",
                    "Bounded programmeoverview; current "
                    "global vacancies not collected",
                    "https://www.google.com/about/careers/applications/research-internships/",
                    "https://www.google.com/about/careers/applications/internships",
                ],
            },
        },
    },
    {
        "key": "business-internships",
        "url": "https://www.google.com/about/careers/applications/students/business-internships/",
        "inputs": [
            "https://www.google.com/about/careers/applications/students/business-internships/",
            "https://www.google.com/about/careers/applications/internships",
        ],
        "fields": {
            "title": "Business internships",
            "url": "https://www.google.com/about/careers/applications/students/business-internships/",
            "source": "google",
            "source_url": "https://buildyourfuture.withgoogle.com/",
            "published_at": None,
            "summary": "Google describes business internships for undergraduate and "
            "graduate students, MBA internships for currently enrolled MBA "
            "students and legal internships for law students in selected "
            "countries. Requirements and application dates vary by role "
            "and location; MBA recruitment generally opens in "
            "September-October and legal recruitment in October, without a "
            "cohort year here. Google offers interns compensation, "
            "benefits and mentorship. The separate four-years-experience "
            "career-break gCareer route is excluded from this student "
            "scope.",
            "tags": [],
            "location": None,
            "deadline": None,
            "language": "en",
            "summary_language": "en",
            "category": "internships",
            "categories": ["internships"],
            "kind": "programme-overview",
            "host_countries": [],
            "eligible_countries": [],
            "publisher_country": "US",
            "status": "unknown",
            "classification": {
                "method": "editorial-review",
                "status": "classified",
                "evidence": [
                    "Google official programme information; "
                    "original factual summary",
                    "Bounded programmeoverview; current "
                    "global vacancies not collected",
                    "https://www.google.com/about/careers/applications/students/business-internships/",
                    "https://www.google.com/about/careers/applications/internships",
                ],
            },
        },
    },
]

if __name__ == "__main__":
    raise SystemExit(main())
