"""Collect eighteen guarded official Bosch student/graduate programme overviews.

This permitted, bounded programme scope excludes blocked job inventories and
English country routes. It does not establish current vacancies or enrolment.
Republish minimal original factual summaries and direct official links only.
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

SOURCE_ID = "de-bosch"
SOURCE_URL = WEBSITE_URL = "https://www.bosch.com/careers/"
LANGUAGE = "en"
PUBLISHER_COUNTRY = "DE"
PUBLISHER_TYPE = "company"
SOURCE_NAME = "Bosch career programmes"
ATTRIBUTION = "Bosch — official career programme information; direct links to original sources."
DESCRIPTION = "Official allowed student and graduate programme overviews; excludes blocked vacancies and country routes; availability is unknown."
_GITHUB = "https://api.github.com/repos/YouthOpps/data-source"
_WORKFLOW_GITHUB = "https://api.github.com/repos/YouthOpps/data-pipeline"
_USER_AGENT = "YouthOpps/1.0 (+https://github.com/YouthOpps/data-pipeline; public opportunity metadata)"
CATEGORIES = {
    "scholarships",
    "internships",
    "volunteering",
    "training",
    "jobs",
    "competitions",
    "grants",
    "fellowships",
    "other",
}
FAMILY = "youthopps-bosch-publisher-v1"
FAMILY_SOURCES = {SOURCE_ID}
COLLECTION_TIMEOUT = 900
PUBLICATION_TIMEOUT = 900
RUN_TIMEOUT = 2520
EXPORT_TIMEOUT = 120
_STATE_SCHEMA = 1
_ARTIFACT_NAME = "bosch-pacing-state"
MAX_PUBLISHER_STARTS = 64
MAX_PUBLISHER_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_REQUEST_STARTS = _PUBLISHER_BYTES = 0
_STATE_PATH = _STATE_LOCK = None
_STATE_DIR_FD = _STATE_DIR_ID = None
_STATE_INITIALIZING = False
_COLLECTION_LOCK_FD = None
_COLLECTION_STARTED = None
_LEGACY_FDS = []
_RESTORED = _PUBLISHING = _STATE_TRUSTED = False
_ROBOTS = {}
_PUBLISHER_COMPLETED_MONOTONIC = None
_PUBLISHER_INTERVAL = 6
_PUBLISHER_GATE_FAILED = False
_RUN_END = _PHASE_END = None
_PHASE_NAME = None


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
        while _LEGACY_FDS:
            os.close(_LEGACY_FDS.pop())
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


def numeric(value):
    try:
        return (
            type(value) in (int, float) and math.isfinite(value) and value >= 0
        )
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


def pace():
    (
        "Serialize every publisher request start across the complete "
        "origin set."
    )
    global _PUBLISHER_INTERVAL, _REQUEST_STARTS
    check_deadline()
    if _REQUEST_STARTS >= MAX_PUBLISHER_STARTS:
        raise AdapterError("Publisher request-start cap reached", "access")
    with locked_budget():
        state = load_budget()
        _PUBLISHER_INTERVAL = max(_PUBLISHER_INTERVAL, state.get("interval", 6))
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
                raise AdapterError(
                    "Publisher backoff exceeds phase budget", "access"
                )
            if _COLLECTION_STARTED is not None and (
                time.time() + wait + 120
                > _COLLECTION_STARTED + COLLECTION_TIMEOUT
            ):
                raise AdapterError(
                    "Publisher backoff exceeds collection budget", "access"
                )
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
            raise AdapterError(
                "Publisher physical completion gate not elapsed", "access"
            )
        state["starts"] = [t for t in starts if t > now - 60] + [now]
        state["not_before"] = max(
            state["not_before"], now + state.get("interval", 6)
        )
        state["observed_at"] = now
        save_budget(state)
        _REQUEST_STARTS += 1


@contextlib.contextmanager
def publisher_attempt(publisher):
    """Persist a completion gate around every physical publisher attempt."""
    global _PUBLISHER_COMPLETED_MONOTONIC, _PUBLISHER_GATE_FAILED, _PUBLISHER_INTERVAL
    if not publisher:
        yield
        return
    if _COLLECTION_LOCK_FD is None or _PUBLISHER_GATE_FAILED:
        raise AdapterError(
            "Exclusive healthy publisher collector required", "access"
        )
    check_deadline()
    if (
        _PUBLISHER_COMPLETED_MONOTONIC is not None
        and time.monotonic()
        < _PUBLISHER_COMPLETED_MONOTONIC + _PUBLISHER_INTERVAL
    ):
        raise AdapterError(
            "Publisher physical completion gate not elapsed", "access"
        )
    try:
        yield
    finally:
        _PUBLISHER_COMPLETED_MONOTONIC = time.monotonic()
        _PUBLISHER_GATE_FAILED = True
        with locked_budget():
            state = load_budget()
            _PUBLISHER_INTERVAL = max(
                _PUBLISHER_INTERVAL, state.get("interval", 6)
            )
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
    byte_limit=MAX_RESPONSE_BYTES,
):
    """Bound response bytes; redirects are explicit and credential-safe."""
    global _PUBLISHER_BYTES
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept-Encoding": "identity",
        **(headers or {}),
    }
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
                        f"Publisher refused access: HTTP {status}: "
                        + provenance,
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
                    if publisher:
                        byte_limit = min(
                            byte_limit, MAX_PUBLISHER_BYTES - _PUBLISHER_BYTES
                        )
                        if byte_limit <= 0:
                            raise AdapterError(
                                "Whole publisher byte cap reached", "fetch"
                            )
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
                            raise publisher_transport_error(
                                error, current
                            ) from None
                        raise
                    if (
                        len(body) > byte_limit
                        or declared
                        and len(body) != int(declared)
                    ):
                        raise AdapterError(
                            "Incomplete or oversized response", "fetch"
                        )
                    if publisher:
                        _PUBLISHER_BYTES += len(body)
                    encoding = response.headers.get(
                        "Content-Encoding", "identity"
                    )
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
        raw = os.environ.get("BOSCH_PACING_BOOTSTRAP", "")
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
    merge_budget(incoming)
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
    path = os.environ.get("BOSCH_PACING_ARTIFACT_PATH")
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


def phase_error(name):
    return AdapterError(
        "Whole " + name + " phase deadline exceeded",
        "publish" if name == "publication" else "access",
    )


def serialize(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


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
            with phase("publication", PUBLICATION_TIMEOUT):
                publish_files(snapshot, desired)
        except PhaseExpired:
            failure = phase_error("publication")
        except Exception as error:
            failure = error
    if failure is not None:
        outcome = metadata(attempt, previous, old, failure)
        if publishing:
            try:
                with phase("failure", PUBLICATION_TIMEOUT):
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


def verify_state_directory():
    """Bind the mature leaf to its retained descriptor before each operation."""
    info = os.fstat(_STATE_DIR_FD)
    current = os.stat(os.path.dirname(_STATE_PATH), follow_symlinks=False)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
        or info.st_nlink < 2
        or (info.st_dev, info.st_ino) != _STATE_DIR_ID
        or (current.st_dev, current.st_ino) != _STATE_DIR_ID
    ):
        raise AdapterError(
            "Publisher state directory identity changed", "access"
        )


def budget_file():
    global _STATE_PATH, _STATE_LOCK, _STATE_DIR_FD, _STATE_DIR_ID
    global _STATE_INITIALIZING
    if _STATE_PATH:
        verify_state_directory()
        return
    temporary = tempfile.gettempdir()
    parent = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        leaf = FAMILY + "-" + str(os.getuid())
        created = False
        try:
            os.mkdir(leaf, 0o700, dir_fd=parent)
            created = True
        except FileExistsError:
            pass
        linked = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        descriptor = os.open(
            leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_nlink < 2
            or (info.st_dev, info.st_ino) != (linked.st_dev, linked.st_ino)
        ):
            os.close(descriptor)
            raise AdapterError("Unsafe publisher pacing directory", "access")
        _STATE_INITIALIZING = created
        _STATE_DIR_FD = descriptor
        _STATE_DIR_ID = (info.st_dev, info.st_ino)
        directory = os.path.join(temporary, leaf)
        _STATE_PATH = os.path.join(directory, "state.json")
        _STATE_LOCK = os.path.join(directory, "state.lock")
        verify_state_directory()
    finally:
        os.close(parent)


def secure_file(name, flags):
    verify_state_directory()
    descriptor = os.open(
        name, flags | os.O_NOFOLLOW, 0o600, dir_fd=_STATE_DIR_FD
    )
    info = os.fstat(descriptor)
    if (
        info.st_uid != os.getuid()
        or not stat.S_ISREG(info.st_mode)
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_nlink != 1
    ):
        os.close(descriptor)
        raise AdapterError("Unsafe publisher state file", "access")
    return descriptor


def locked_budget():
    budget_file()
    descriptor = secure_file(
        "state.lock", os.O_RDWR | (os.O_CREAT if _STATE_INITIALIZING else 0)
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        raise AdapterError("Publisher state lock is busy", "access") from None
    return os.fdopen(descriptor, "a+")


def load_budget():
    try:
        descriptor = secure_file("state.json", os.O_RDONLY)
    except FileNotFoundError:
        if not _STATE_INITIALIZING:
            raise AdapterError(
                "Mature publisher state is missing", "access"
            ) from None
        return empty_budget(time.time())
    with os.fdopen(descriptor, "rb") as file:
        raw = file.read(16385)
    if len(raw) > 16384:
        raise AdapterError("Oversized publisher state", "access")
    try:
        return validate_budget(json.loads(raw))
    except (ValueError, TypeError):
        raise AdapterError("Corrupt publisher state", "access") from None


def save_budget(state):
    global _STATE_INITIALIZING
    validate_budget(state)
    verify_state_directory()
    name = "pending-" + str(os.getpid()) + "-" + str(time.time_ns())
    descriptor = secure_file(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        with os.fdopen(descriptor, "w") as file:
            json.dump(state, file, separators=(",", ":"))
            file.flush()
            os.fsync(file.fileno())
        verify_state_directory()
        os.replace(
            name,
            "state.json",
            src_dir_fd=_STATE_DIR_FD,
            dst_dir_fd=_STATE_DIR_FD,
        )
        # Once a state has existed, missing state must never become fresh again,
        # including if the following directory fsync fails.
        _STATE_INITIALIZING = False
        os.fsync(_STATE_DIR_FD)
    finally:
        try:
            os.unlink(name, dir_fd=_STATE_DIR_FD)
        except FileNotFoundError:
            pass


def merge_budget(incoming):
    """Conserve all timing restrictions and refuse uncertainty after union."""
    with locked_budget():
        local = load_budget()
        now = time.time()
        combined = empty_budget(now)
        combined["interval"] = max(
            local.get("interval", 6), incoming.get("interval", 6)
        )
        combined["not_before"] = max(
            local["not_before"], incoming["not_before"], now + 60
        )
        combined["blocked"] = local["blocked"] or incoming["blocked"]
        union = sorted(set(local["starts"] + incoming["starts"]))
        if union:
            combined["not_before"] = max(
                combined["not_before"], union[-1] + combined["interval"]
            )
        if len(union) > 10:
            combined["not_before"] = max(combined["not_before"], union[-1] + 60)
        combined["starts"] = union[-10:]
        save_budget(combined)


def preserve_legacy_pacing():
    """Keep every legacy inode and its exclusive lock throughout collection."""
    roots = [
        ("issue119-bosch-research", "bosch-family.budget.json"),
        ("youthopps-source-triage/119", "pacing.json"),
    ]
    for directory, filename in roots:
        parent = os.open(
            tempfile.gettempdir(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        opened = [parent]
        try:
            for name in directory.split("/"):
                descriptor = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent,
                )
                opened.append(descriptor)
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700
                    or info.st_nlink < 2
                ):
                    raise AdapterError(
                        "Unsafe legacy publisher directory", "access"
                    )
                parent = descriptor
            descriptor = os.open(
                filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent
            )
            opened.append(descriptor)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise AdapterError("Unsafe legacy publisher file", "access")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise AdapterError(
                    "Legacy publisher collector active", "access"
                ) from None
            raw = os.read(descriptor, 16385)
            try:
                legacy = json.loads(raw)
                # Historical research retains eleven aged starts after a wait;
                # accept that exact known shape without altering legacy bytes.
                if (
                    len(raw) > 16384
                    or not isinstance(legacy, dict)
                    or not {"starts", "until"} <= set(legacy)
                    or set(legacy) - {"starts", "until", "interval", "blocked"}
                    or not numeric(legacy["until"])
                    or not numeric(legacy.get("interval", 6))
                    or legacy.get("interval", 6) < 6
                    or type(legacy.get("blocked", False)) is not bool
                    or not isinstance(legacy["starts"], list)
                    or len(legacy["starts"]) > 11
                    or any(not numeric(t) for t in legacy["starts"])
                    or legacy["starts"] != sorted(set(legacy["starts"]))
                    or any(t > time.time() + 1 for t in legacy["starts"])
                ):
                    raise ValueError("Invalid legacy schema")
            except (ValueError, TypeError, KeyError):
                raise AdapterError(
                    "Invalid legacy publisher history", "access"
                ) from None
            incoming = empty_budget(time.time())
            incoming.update(
                not_before=legacy["until"],
                blocked=legacy.get("blocked", False),
                interval=legacy.get("interval", 6),
                starts=legacy["starts"],
            )
            merge_budget(incoming)
            _LEGACY_FDS.extend(opened)
            opened = []
        except FileNotFoundError:
            pass
        finally:
            for descriptor in reversed(opened):
                os.close(descriptor)


def prepare_collection():
    global _COLLECTION_STARTED, _STATE_TRUSTED, _COLLECTION_LOCK_FD
    global _REQUEST_STARTS, _PUBLISHER_BYTES
    _COLLECTION_STARTED = time.time()
    _REQUEST_STARTS = _PUBLISHER_BYTES = 0
    budget_file()
    if _COLLECTION_LOCK_FD is not None:
        raise AdapterError("Nested family collection refused", "access")
    descriptor = secure_file(
        "collection.lock", os.O_RDWR | (os.O_CREAT if _STATE_INITIALIZING else 0)
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        raise AdapterError(
            "Another local family collector active", "access"
        ) from None
    _COLLECTION_LOCK_FD = descriptor
    preserve_legacy_pacing()
    if os.environ.get("GITHUB_ACTIONS") == "true":
        restore_family_artifact()
    with locked_budget():
        state = load_budget()
        state["not_before"] = max(state["not_before"], _COLLECTION_STARTED + 60)
        state["observed_at"] = time.time()
        save_budget(state)
    _ROBOTS.clear()
    _STATE_TRUSTED = True


def public_url(value):
    parsed = urllib.parse.urlsplit(value)
    clean = urllib.parse.urlunsplit(parsed._replace(fragment=""))
    if (
        clean not in {info["url"] for info in INPUTS.values()}
        or parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port
    ):
        raise AdapterError("Unreviewed public Bosch route", "access")
    return clean


def html_fingerprint(body):
    """Guard full material prose, literal links and finite-frontier controls."""
    root = document_root(body.decode("utf-8", "strict"))
    links = sorted(
        {
            (node.tag, node.attrs.get("rel", ""), node.attrs["href"])
            for tag in ("a", "link")
            for node in nodes(root, tag)
            if node.attrs.get("href")
        }
    )
    controls = []
    for node in root.walk():
        attrs = {
            key: value
            for key, value in node.attrs.items()
            if key in {"action", "method", "name", "value", "selected"}
            or key.startswith("data-")
        }
        if attrs:
            controls.append((node.tag, sorted(attrs.items())))
    facts = {"text": root.text(), "links": links, "controls": controls}
    return hashlib.sha256(
        json.dumps(facts, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def check_robots(url):
    import urllib.robotparser

    parsed = urllib.parse.urlsplit(public_url(url))
    origin = parsed.scheme + "://" + parsed.netloc
    if parsed.path == "/robots.txt":
        return
    if origin not in _ROBOTS:
        key = next(
            (
                key
                for key, info in INPUTS.items()
                if info["url"] == origin + "/robots.txt"
            ),
            None,
        )
        if key is None:
            raise AdapterError("Unreviewed Bosch robots origin", "access")
        read_input(key)
    parser = _ROBOTS[origin]
    if not parser.can_fetch(_USER_AGENT, url):
        raise AdapterError(
            "Publisher robots excludes required Bosch input", "access"
        )


def fetch_public(url):
    reviewed = public_url(url)
    check_robots(reviewed)
    status, headers, body = request_bytes(reviewed, publisher=True)
    if status != 200:
        raise AdapterError(
            "Required publisher input returned HTTP " + str(status),
            "fetch",
            status,
        )
    return status, headers, body


def read_input(key):
    import urllib.robotparser

    info = INPUTS[key]
    status, headers, body = fetch_public(info["url"])
    content_type = (
        headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
    )
    if info["format"] == "robots":
        if content_type not in {"text/plain", "text/html"}:
            raise AdapterError("Unexpected robots response format", "access")
        digest = hashlib.sha256(body).hexdigest()
    else:
        if content_type not in {"text/html", "application/xhtml+xml"}:
            raise AdapterError("Unexpected programme response format", "parse")
        digest = html_fingerprint(body)
    if digest != info["sha256"]:
        raise AdapterError(
            "Reviewed Bosch material or frontier changed: " + key, "parse"
        )
    if info["format"] == "robots":
        parsed = urllib.parse.urlsplit(info["url"])
        origin = parsed.scheme + "://" + parsed.netloc
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(body.decode("utf-8", "strict").splitlines())
        delay = parser.crawl_delay(_USER_AGENT)
        if delay is not None and delay > 6:
            with locked_budget():
                state = load_budget()
                state["interval"] = max(state.get("interval", 6), delay)
                if state["starts"]:
                    state["not_before"] = max(
                        state["not_before"], state["starts"][-1] + delay
                    )
                state["observed_at"] = time.time()
                save_budget(state)
        rate = parser.request_rate(_USER_AGENT)
        if rate and rate.seconds / rate.requests > 6:
            raise AdapterError(
                "Stricter publisher request rate requires review", "access"
            )
        _ROBOTS[origin] = parser
    return digest


def read_pages():
    """Exhaust all49 nominal inputs; fresh robots precede every document."""
    order = [key for key, info in INPUTS.items() if info["format"] == "robots"]
    order += [key for key, info in INPUTS.items() if info["format"] != "robots"]
    return {key: read_input(key) for key in order}


def profile_identity(profile):
    return hashlib.sha256(
        (SOURCE_ID + "|" + profile["identity"]).encode()
    ).hexdigest()[:24]


def make_record(profile):
    now = utc_now()
    return {
        "id": profile_identity(profile),
        "title": profile["title"],
        "url": profile["url"],
        "source": SOURCE_ID,
        "source_url": SOURCE_URL,
        "published_at": None,
        "summary": profile["summary"],
        "summary_language": "en",
        "tags": [],
        "location": None,
        "deadline": None,
        "language": LANGUAGE,
        "category": profile["categories"][0],
        "categories": profile["categories"],
        "kind": "programme-overview",
        "host_countries": profile["hosts"],
        "eligible_countries": [],
        "publisher_country": PUBLISHER_COUNTRY,
        "created_at": now,
        "updated_at": now,
        "first_seen_at": now,
        "last_seen_at": now,
        "last_checked_at": now,
        "status": "unknown",
        "classification": {
            "method": "editorial-review",
            "status": "classified",
            "evidence": [profile["url"]],
        },
    }


def validate_owned_records(records):
    temporal = {
        "created_at",
        "updated_at",
        "first_seen_at",
        "last_seen_at",
        "last_checked_at",
    }
    profiles = {profile_identity(profile): profile for profile in PROFILES}
    for record in records:
        profile = profiles.get(record.get("id"))
        if profile is None:
            raise AdapterError("Unowned Bosch identity", "validate")
        expected = make_record(profile)
        if {k: v for k, v in record.items() if k not in temporal} != {
            k: v for k, v in expected.items() if k not in temporal
        }:
            raise AdapterError("Owned Bosch factual recipe changed", "validate")
        clocks = {
            field: datetime.fromisoformat(record[field].replace("Z", "+00:00"))
            for field in temporal
        }
        if not (
            clocks["created_at"]
            <= clocks["updated_at"]
            <= clocks["last_checked_at"]
            and clocks["first_seen_at"]
            <= clocks["last_seen_at"]
            <= clocks["last_checked_at"]
        ):
            raise AdapterError("Invalid Bosch chronology", "validate")


def validate_records(records):
    if (
        not isinstance(records, list)
        or not records
        or len(records) != len(PROFILES)
    ):
        raise AdapterError(
            "Incomplete reviewed Bosch identity inventory", "validate"
        )
    expected_fields = set(make_record(PROFILES[0]))
    identities = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != expected_fields:
            raise AdapterError("Invalid complete25-field contract", "validate")
        if (
            not isinstance(record.get("id"), str)
            or not re.fullmatch(r"[0-9a-f]{24}", record.get("id", ""))
            or record["id"] in identities
        ):
            raise AdapterError(
                "Invalid or duplicate Bosch identity", "validate"
            )
        identities.add(record["id"])
        for field in (
            "created_at",
            "updated_at",
            "first_seen_at",
            "last_seen_at",
            "last_checked_at",
        ):
            try:
                value = record[field]
                if (
                    not isinstance(value, str)
                    or not re.fullmatch(
                        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z",
                        value,
                    )
                    or datetime.fromisoformat(
                        value.replace("Z", "+00:00")
                    ).tzinfo
                    is None
                ):
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise AdapterError(
                    "Invalid Bosch UTC timestamp", "validate"
                ) from None
        summary = record["summary"]
        if (
            not isinstance(summary, str)
            or not summary.strip()
            or len(summary.encode("utf-16-le")) // 2 > 580
            or re.search(r"<[^>]+>|[\x00-\x1f\x7f]", summary)
        ):
            raise AdapterError("Invalid original factual summary", "validate")
        for name in ("host_countries", "eligible_countries"):
            values = record[name]
            if (
                not isinstance(values, list)
                or any(
                    not isinstance(v, str) or not re.fullmatch(r"[A-Z]{2}", v)
                    for v in values
                )
                or len(values) != len(set(values))
            ):
                raise AdapterError("Invalid country array", "validate")
        if not isinstance(record["title"], str) or not record["title"].strip():
            raise AdapterError("Invalid programme title", "validate")
        if not isinstance(record["url"], str):
            raise AdapterError("Invalid official URL type", "validate")
        try:
            url = urllib.parse.urlsplit(record["url"])
            invalid_url = (
                url.scheme != "https"
                or not url.hostname
                or url.username
                or url.password
                or url.port
            )
        except ValueError:
            invalid_url = True
        if invalid_url:
            raise AdapterError("Unsafe official URL", "validate")
    validate_owned_records(records)


def parse_inventory(pages):
    if set(pages) != set(INPUTS) or any(
        pages[key] != info["sha256"] for key, info in INPUTS.items()
    ):
        raise AdapterError(
            "Incomplete reviewed Bosch material frontier", "parse"
        )
    records = [make_record(profile) for profile in PROFILES]
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


def metadata(attempt, previous, records, error=None, checked_at=None):
    outcome = {
        "source": SOURCE_ID,
        "name": SOURCE_NAME,
        "description": DESCRIPTION,
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
            else f"Collected {len(records)} reviewed Bosch programme overviews"
        ),
        "error": safe_error(error) if error else None,
    }
    if error:
        outcome["failure_stage"] = getattr(error, "stage", "parse")
    return outcome


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
            updated_at=before.get("updated_at", checked) if same else checked,
            last_seen_at=attempt,
            last_checked_at=checked,
        )
    records.sort(key=lambda record: record["id"])
    validate_records(records)


def validate_prior_metadata(previous, records):
    """Accept failure writes only after the complete owned last-good pair."""
    if (
        not isinstance(previous, dict)
        or not {
            "description",
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
        or previous.get("description") != DESCRIPTION
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
        or (
            previous.get("status") == "success"
            and previous.get("error") is not None
        )
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


_OPENER = urllib.request.build_opener(NoRedirect())

INPUTS = {
    "input-00": {
        "url": "https://www.bosch.africa/careers/students-and-graduates/",
        "format": "html",
        "sha256": "9829a10fde15762cca1a1d89c201f7976efc845d555f32ae2f0038d0ef08f891",
    },
    "input-01": {
        "url": "https://www.bosch.africa/legal-notice/?prevent-auto-open-privacy-settings=1",
        "format": "html",
        "sha256": "d0bf69d352401f9394cc2f6837af12378ddab2fc39a480fb73dc93e276c9404e",
    },
    "input-02": {
        "url": "https://www.bosch.africa/robots.txt",
        "format": "robots",
        "sha256": "9413b582ba30e2177c263ea05ad47889c9612aaf120a03494eaababe7130fb6b",
    },
    "input-03": {
        "url": "https://www.bosch.at/robots.txt",
        "format": "robots",
        "sha256": "b4703ca8b1c7cdf98d9bcdc7bd7b7baa7cc24ccd02c009b8a4bef88fa0551fc0",
    },
    "input-04": {
        "url": "https://www.bosch.ca/careers/students-and-graduates/",
        "format": "html",
        "sha256": "af3cae0657408cb7f57585f1d78dc513a3649cd69b12dd8202f8282014fb2dd1",
    },
    "input-05": {
        "url": "https://www.bosch.ca/legal-notice/?prevent-auto-open-privacy-settings=1",
        "format": "html",
        "sha256": "088a0afd85eddababd63815cacb3c8e1f08ca6c8afb580c23de4923565504c1d",
    },
    "input-06": {
        "url": "https://www.bosch.ca/robots.txt",
        "format": "robots",
        "sha256": "9413b582ba30e2177c263ea05ad47889c9612aaf120a03494eaababe7130fb6b",
    },
    "input-07": {
        "url": "https://www.bosch.ch/robots.txt",
        "format": "robots",
        "sha256": "b4703ca8b1c7cdf98d9bcdc7bd7b7baa7cc24ccd02c009b8a4bef88fa0551fc0",
    },
    "input-08": {
        "url": "https://www.bosch.com.cn/robots.txt",
        "format": "robots",
        "sha256": "b4703ca8b1c7cdf98d9bcdc7bd7b7baa7cc24ccd02c009b8a4bef88fa0551fc0",
    },
    "input-09": {
        "url": "https://www.bosch.com.sg/careers/students-and-graduates/",
        "format": "html",
        "sha256": "3a65abe003687d7480557af2df961e9076e29a3d5c23bda554de7ff47bc1568b",
    },
    "input-10": {
        "url": "https://www.bosch.com.sg/careers/students-and-graduates/final-thesis/",
        "format": "html",
        "sha256": "d3745db91410022c0a6e738d66562189477271a7a7ad8ea55e6bc786bb496e21",
    },
    "input-11": {
        "url": "https://www.bosch.com.sg/careers/students-and-graduates/internships/",
        "format": "html",
        "sha256": "7a2c3c75d182bc2c96fb34db5873dd9eb2e61d654336f884310ce046127fd334",
    },
    "input-12": {
        "url": "https://www.bosch.com.sg/careers/students-and-graduates/junior-managers-program/",
        "format": "html",
        "sha256": "de9a193a80839bd538c8cccd0641317e0f1c9bee888088dc6888fed1c867e815",
    },
    "input-13": {
        "url": "https://www.bosch.com.sg/careers/students-and-graduates/phd/",
        "format": "html",
        "sha256": "15226723dcbf178b9099310c341220493b839a0970b4c0be27203506d0499e57",
    },
    "input-14": {
        "url": "https://www.bosch.com.sg/careers/students-and-graduates/students-bosch/",
        "format": "html",
        "sha256": "0ba6362f42ea5a56de5530bbf58e4759df8000411c884b92f2c28ebeeed4e5f1",
    },
    "input-15": {
        "url": "https://www.bosch.com.sg/legal-notice/?prevent-auto-open-privacy-settings=1",
        "format": "html",
        "sha256": "9d57c0d36cdb7c96027b41675454d4f71104d7b66ffc3e7d46867ccc105811b0",
    },
    "input-16": {
        "url": "https://www.bosch.com.sg/robots.txt",
        "format": "robots",
        "sha256": "6763f04d00f1166333a88d3510c6729e649ff60818bb1e633d034ed54b215d7e",
    },
    "input-17": {
        "url": "https://www.bosch.com.tr/robots.txt",
        "format": "robots",
        "sha256": "b4703ca8b1c7cdf98d9bcdc7bd7b7baa7cc24ccd02c009b8a4bef88fa0551fc0",
    },
    "input-18": {
        "url": "https://www.bosch.com/careers/",
        "format": "html",
        "sha256": "39aab17d6de3a2f4415ec1acbb302bc00df048baeec97195896888659473ae8d",
    },
    "input-19": {
        "url": "https://www.bosch.com/careers/career-opportunities/",
        "format": "html",
        "sha256": "35e5dbd13143b2e8b5fcffdac7a1479c849eb1b9df386ea1dd90899735fb5ba4",
    },
    "input-20": {
        "url": "https://www.bosch.com/careers/career-opportunities/junior-managers-program/",
        "format": "html",
        "sha256": "462025e37cadfe3d9ddfa8b4447a5884c3fecc9f0de9865b8e229f490f97c366",
    },
    "input-21": {
        "url": "https://www.bosch.com/careers/faq/",
        "format": "html",
        "sha256": "6da147a9ba1d015dba9b72564ace2dff1a749c48a4b9ba71bb73b226d997d888",
    },
    "input-22": {
        "url": "https://www.bosch.com/legal-notice/?prevent-auto-open-privacy-settings=1",
        "format": "html",
        "sha256": "29da1b00655ca187c6e16e3dbca2870da6dd827158818c4fc1ca4596960ac116",
    },
    "input-23": {
        "url": "https://www.bosch.com/robots.txt",
        "format": "robots",
        "sha256": "7f03f031ad7213632f4c0bda4daf101371e779a6c8181e02a225d980b3d45da7",
    },
    "input-24": {
        "url": "https://www.bosch.cz/robots.txt",
        "format": "robots",
        "sha256": "b4703ca8b1c7cdf98d9bcdc7bd7b7baa7cc24ccd02c009b8a4bef88fa0551fc0",
    },
    "input-25": {
        "url": "https://www.bosch.de/robots.txt",
        "format": "robots",
        "sha256": "9d191636043dd361d07221a83532669ccf8c6919bbc6b63100de701160d015c9",
    },
    "input-26": {
        "url": "https://www.bosch.dk/robots.txt",
        "format": "robots",
        "sha256": "b4703ca8b1c7cdf98d9bcdc7bd7b7baa7cc24ccd02c009b8a4bef88fa0551fc0",
    },
    "input-27": {
        "url": "https://www.bosch.fr/robots.txt",
        "format": "robots",
        "sha256": "b4703ca8b1c7cdf98d9bcdc7bd7b7baa7cc24ccd02c009b8a4bef88fa0551fc0",
    },
    "input-28": {
        "url": "https://www.bosch.hu/robots.txt",
        "format": "robots",
        "sha256": "b4703ca8b1c7cdf98d9bcdc7bd7b7baa7cc24ccd02c009b8a4bef88fa0551fc0",
    },
    "input-29": {
        "url": "https://www.bosch.in/careers/students-and-graduates/",
        "format": "html",
        "sha256": "8d9496031d4f5a565b6a5d69a08e3405ba5f5c7436f5e53c062cf7cff274799f",
    },
    "input-30": {
        "url": "https://www.bosch.in/careers/students-and-graduates/commercial-management-trainee-program/",
        "format": "html",
        "sha256": "a0a090a3450f1ce00b0d4ab476cf861d51817f9d5520d2690919626635ac1b60",
    },
    "input-31": {
        "url": "https://www.bosch.in/careers/students-and-graduates/graduate-apprentice/",
        "format": "html",
        "sha256": "2b4c46b3e96ce2607d7b4684b9016e387e0b0ed5cf82708265f97f8981f0514a",
    },
    "input-32": {
        "url": "https://www.bosch.in/careers/students-and-graduates/indian-institute-of-technology-recruitment/",
        "format": "html",
        "sha256": "0c4b9a66d27c1d3651ab7b45cb1cfb88a8556a8be178d5f1e911440bb6937ac3",
    },
    "input-33": {
        "url": "https://www.bosch.in/careers/students-and-graduates/internships/",
        "format": "html",
        "sha256": "2c9368e4c1ecfe19be094f368661bdc18ee6396cc0e5d57186c79b0ebe5c6a15",
    },
    "input-34": {
        "url": "https://www.bosch.in/careers/students-and-graduates/junior-managers-program/",
        "format": "html",
        "sha256": "8d048aaa6c9f5edf22880b3873b553c28578c9f57b93e2ec4871198a8ea74236",
    },
    "input-35": {
        "url": "https://www.bosch.in/careers/students-and-graduates/technical-management-trainee-program/",
        "format": "html",
        "sha256": "cb47eae8fccc86049c81ca22fb6f3f71f6fc11fd9595c5cfb5c0d4392a7e1255",
    },
    "input-36": {
        "url": "https://www.bosch.in/legal-notice/?prevent-auto-open-privacy-settings=1",
        "format": "html",
        "sha256": "96cc1faaf476bbc5945f45b13619f4e1f4059ea0b28c02037dcc7c7259d488b3",
    },
    "input-37": {
        "url": "https://www.bosch.in/robots.txt",
        "format": "robots",
        "sha256": "9413b582ba30e2177c263ea05ad47889c9612aaf120a03494eaababe7130fb6b",
    },
    "input-38": {
        "url": "https://www.bosch.it/robots.txt",
        "format": "robots",
        "sha256": "b4703ca8b1c7cdf98d9bcdc7bd7b7baa7cc24ccd02c009b8a4bef88fa0551fc0",
    },
    "input-39": {
        "url": "https://www.bosch.nl/robots.txt",
        "format": "robots",
        "sha256": "b4703ca8b1c7cdf98d9bcdc7bd7b7baa7cc24ccd02c009b8a4bef88fa0551fc0",
    },
    "input-40": {
        "url": "https://www.bosch.pl/robots.txt",
        "format": "robots",
        "sha256": "ae43f983986fef50defdc9c3ab497a016e6f49ad1e1517850af2202f2b918548",
    },
    "input-41": {
        "url": "https://www.bosch.pt/robots.txt",
        "format": "robots",
        "sha256": "b4703ca8b1c7cdf98d9bcdc7bd7b7baa7cc24ccd02c009b8a4bef88fa0551fc0",
    },
    "input-42": {
        "url": "https://www.bosch.ro/robots.txt",
        "format": "robots",
        "sha256": "b4703ca8b1c7cdf98d9bcdc7bd7b7baa7cc24ccd02c009b8a4bef88fa0551fc0",
    },
    "input-43": {
        "url": "https://www.bosch.se/robots.txt",
        "format": "robots",
        "sha256": "b4703ca8b1c7cdf98d9bcdc7bd7b7baa7cc24ccd02c009b8a4bef88fa0551fc0",
    },
    "input-44": {
        "url": "https://www.bosch.us/careers/start-your-career/graduates/",
        "format": "html",
        "sha256": "8f02e851068f26f348c13637c4aef7096fecb8c09b869c1bb65caba9fc4af526",
    },
    "input-45": {
        "url": "https://www.bosch.us/careers/start-your-career/students/",
        "format": "html",
        "sha256": "9a60aec2c93c2cdc242cea5b4a4c0cf30ebe08f68f2cfe17a6322d027384d2b5",
    },
    "input-46": {
        "url": "https://www.bosch.us/legal-notice/?prevent-auto-open-privacy-settings=1",
        "format": "html",
        "sha256": "3ed6be7415dfc5c528f7b294ffca5d1b71708c5beebebe7b64644890acdbaf97",
    },
    "input-47": {
        "url": "https://www.bosch.us/robots.txt",
        "format": "robots",
        "sha256": "9413b582ba30e2177c263ea05ad47889c9612aaf120a03494eaababe7130fb6b",
    },
    "input-48": {
        "url": "https://www.grupo-bosch.es/robots.txt",
        "format": "robots",
        "sha256": "b4703ca8b1c7cdf98d9bcdc7bd7b7baa7cc24ccd02c009b8a4bef88fa0551fc0",
    },
}

PROFILES = [
    {
        "identity": "global-jmp",
        "title": "Bosch Junior Managers Program — global overview",
        "url": "https://www.bosch.com/careers/career-opportunities/junior-managers-program/",
        "categories": ["training", "jobs"],
        "hosts": [],
        "summary": "Graduate management-development framework offered across countries, "
        "combining rotations, an international assignment and mentoring. Entry "
        "conditions vary by country. This overview does not establish a current "
        "vacancy, closing date or guaranteed management appointment.",
        "proof": "Graduate audience; country-specific conditions apply; no universal "
        "degree/GPA/age/work authorization established; Cross-functional and "
        "cross-divisional rotations; international assignment; experienced personal "
        "mentor",
        "parents": [],
    },
    {
        "identity": "in-jmp",
        "title": "Bosch India Junior Managers Program",
        "url": "https://www.bosch.in/careers/students-and-graduates/junior-managers-program/",
        "categories": ["training", "jobs"],
        "hosts": ["IN"],
        "summary": "India management-development programme with 18–24 months of rotations, "
        "including an assignment abroad, mentoring and seminars. Requires "
        "postgraduate MBA, M.Tech or Chartered Accountancy qualifications, over "
        "60% throughout academics and 1–3 years of professional experience. "
        "Overseas experience is preferred; current vacancies must be checked with "
        "Bosch.",
        "proof": "Postgraduate MBA/M.Tech/Chartered Accountancy; more than 60% throughout "
        "academic studies; 1–3 years professional experience; foreign experience "
        "preferred; mobility, leadership and language-learning readiness; 18–24 "
        "months; 4–6 assignments including one abroad; nine functions; mentor and "
        "seminars",
        "parents": [],
    },
    {
        "identity": "in-tmt",
        "title": "Bosch India Technical Management Trainee Program",
        "url": "https://www.bosch.in/careers/students-and-graduates/technical-management-trainee-program/",
        "categories": ["training", "jobs"],
        "hosts": ["IN"],
        "summary": "An 18-month engineering-graduate training programme combining classroom "
        "learning, workplace assignments, projects and mentoring. Applicants must "
        "be full-time engineering graduates from Bosch-selected institutes with "
        "at least 65% throughout academics. Apply through the institute placement "
        "office; selection includes assessments and interviews.",
        "proof": "Full-time engineering graduates from Bosch-selected institutes; at least "
        "65% throughout academics; application through institute placement office; "
        "selection assessments/interview; 18-month classroom and on-the-job "
        "training across functions/locations; projects, workshops and mentoring",
        "parents": [],
    },
    {
        "identity": "in-cmt",
        "title": "Bosch India Commercial Management Trainee Program",
        "url": "https://www.bosch.in/careers/students-and-graduates/commercial-management-trainee-program/",
        "categories": ["training", "jobs"],
        "hosts": ["IN"],
        "summary": "An 18-month management-graduate programme with practical assignments, "
        "projects, seminars and senior guidance. Requires full-time management "
        "graduates from Bosch-selected institutes and first-class results "
        "throughout academics, including postgraduate study. Applications go "
        "through institute placement offices, followed by assessments and "
        "interviews.",
        "proof": "Full-time management graduates from Bosch-selected institutes; first class "
        "throughout academics including postgraduate study; institute placement "
        "route; staged assessments/domain and HR interview; 18-month training in "
        "one functional area; projects, seminars and senior guidance",
        "parents": [],
    },
    {
        "identity": "in-ga",
        "title": "Bosch India Graduate Apprentice",
        "url": "https://www.bosch.in/careers/students-and-graduates/graduate-apprentice/",
        "categories": ["training"],
        "hosts": ["IN"],
        "summary": "A 12-month engineering apprenticeship combining workplace and classroom "
        "learning. Requires first-class results throughout, every semester or "
        "year passed on the first attempt without backlog, and the final "
        "engineering examination in the selection year. Eligible engineering "
        "disciplines and selected institutes apply. Recruitment uses college "
        "placement officers and assessments.",
        "proof": "Full-time engineering graduate; first-class results throughout; every "
        "semester/year passed first attempt with no backlog; final engineering "
        "examination in selection year; listed mechanical-related or "
        "electrical/electronics disciplines when required; selected institutes; 12 "
        "months workplace and classroom learning, projects, seminars and guidance",
        "parents": [],
    },
    {
        "identity": "in-iit",
        "title": "Bosch India IIT Graduate Recruitment",
        "url": "https://www.bosch.in/careers/students-and-graduates/indian-institute-of-technology-recruitment/",
        "categories": ["training", "jobs"],
        "hosts": ["IN"],
        "summary": "IIT engineering-graduate recruitment with on-the-job training, mentoring "
        "and workshops. Requires at least 70% throughout academics or a minimum "
        "CGPA of 7.0, including graduation, with every semester or year passed on "
        "the first attempt without backlog. Listed engineering disciplines apply. "
        "Recruitment proceeds through institute placement offices and "
        "assessments.",
        "proof": "Full-time engineering graduate from any IIT; at least 70% throughout or "
        "minimum CGPA 7.0 including graduation; every semester/year first attempt "
        "without backlog; specified mechanical or electrical/electronics "
        "disciplines; placement-office route; On-the-job training in a functional "
        "area, varied assignments, mentor and workshops",
        "parents": [],
    },
    {
        "identity": "in-internship",
        "title": "Bosch India Internships",
        "url": "https://www.bosch.in/careers/students-and-graduates/internships/",
        "categories": ["internships"],
        "hosts": ["IN"],
        "summary": "Practical project-based internships lasting up to one year, with "
        "remuneration and support from experienced colleagues. Applicants need "
        "strong academic and subject knowledge, good English, teamwork and "
        "flexibility. Apply through the institute placement office; selection "
        "includes assessments and an interview. Future employment may be possible "
        "but is not guaranteed.",
        "proof": "Strong subject knowledge and academic results; dedication, flexibility, "
        "teamwork and good English; other foreign languages advantageous; institute "
        "placement route with assessments and interview; Practical project "
        "responsibility and international teamwork; up to one year; remuneration "
        "stated, amount unknown; possible future employment, not guaranteed",
        "parents": [],
    },
    {
        "identity": "sg-jmp",
        "title": "Bosch Singapore Junior Managers Program",
        "url": "https://www.bosch.com.sg/careers/students-and-graduates/junior-managers-program/",
        "categories": ["training", "jobs"],
        "hosts": ["SG"],
        "summary": "Singapore graduate management programme with 18–24 months of functional "
        "rotations, an assignment abroad, mentoring and seminars. Requires "
        "outstanding bachelor’s or master’s results in IT, engineering, sciences "
        "or business, foreign experience, and fresh-graduate status or fewer than "
        "three years of experience.",
        "proof": "Bachelor/master in IT, engineering, sciences or business; outstanding "
        "grades; fresh graduate or fewer than 3 years experience; foreign "
        "experience; literal source wording: during a semester of internship; "
        "leadership/mobility; 18–24 months; 4–6 functional assignments including "
        "one abroad; mentoring and seminars",
        "parents": [],
    },
    {
        "identity": "sg-phd",
        "title": "Bosch Singapore PhD Research Programme",
        "url": "https://www.bosch.com.sg/careers/students-and-graduates/phd/",
        "categories": ["training", "jobs"],
        "hosts": ["SG"],
        "summary": "Doctoral research supported through a 2–4-year temporary work contract, "
        "specialist mentoring and a PhD candidate network. Requires a completed "
        "bachelor’s or master’s degree with top grades; technical disciplines are "
        "preferred, and practical or overseas experience is ideal. Financial "
        "support is stated without an amount.",
        "proof": "Completed bachelor/master with top grades; technical discipline preferred; "
        "practical and overseas experience ideal; entrepreneurial, creative and "
        "collaborative outlook; 2–4-year temporary work contract; financial "
        "support, amount unknown; specialist mentoring through dissertation "
        "publication; PhD network",
        "parents": ["https://www.bosch.com.sg/careers/students-and-graduates/"],
    },
    {
        "identity": "sg-internship",
        "title": "Bosch Singapore Internships",
        "url": "https://www.bosch.com.sg/careers/students-and-graduates/internships/",
        "categories": ["internships"],
        "hosts": ["SG"],
        "summary": "Study-related compulsory or voluntary internships with practical "
        "responsibilities alongside specialists and international teams. Strong "
        "academic knowledge, teamwork, creativity and flexibility are expected. "
        "Voluntary placement duration and field must fit the student’s studies "
        "and Bosch. A placement can lead to consideration for students@bosch.",
        "proof": "Study-related strong theoretical knowledge and academic results; "
        "dedication/flexibility, creativity, interpersonal skills and teamwork; "
        "compulsory or voluntary placements; Practical responsibilities with "
        "specialists/international teams; voluntary duration/field agreed to fit "
        "studies; eligibility for students@bosch not automatic",
        "parents": [],
    },
    {
        "identity": "sg-thesis",
        "title": "Bosch Singapore Bachelor’s and Master’s Thesis Programme",
        "url": "https://www.bosch.com.sg/careers/students-and-graduates/final-thesis/",
        "categories": ["training"],
        "hosts": ["SG"],
        "summary": "Bachelor’s and master’s thesis opportunities combining university study "
        "with practical work alongside Bosch specialists. Participants receive a "
        "personal mentor and access to relevant expertise, while developing "
        "professional contacts. Sound subject knowledge, curiosity, flexibility, "
        "creativity and teamwork are expected.",
        "proof": "Bachelor/master final-thesis student; sound subject knowledge, curiosity, "
        "dedication/flexibility, creativity and teamwork; Personal mentor; "
        "collaboration with Bosch specialists and university; practical research "
        "and professional network",
        "parents": [],
    },
    {
        "identity": "students-bosch",
        "title": "Bosch students@bosch Programme",
        "url": "https://www.bosch.com.sg/careers/students-and-graduates/students-bosch/",
        "categories": ["training", "internships"],
        "hosts": [],
        "summary": "An invitation-based development network: strong Bosch internship "
        "performance can lead to an invitation, and STEM students may be "
        "considered. Participants receive specialist mentoring, HR career "
        "guidance, events and peer networking, with opportunities for "
        "international internships. Admission is not an unrestricted public "
        "application.",
        "proof": "Invitation after strong performance during a Bosch internship, explicitly "
        "stated on SG parent; no public unrestricted admission; SG and Canada "
        "parents conditionally state that students studying a STEM subject may be "
        "chosen; this is consideration, not a universal STEM-only admissions rule.; "
        "Department mentor, HR career guidance, events and peer contacts; "
        "internships around the world",
        "parents": [
            "https://www.bosch.com.sg/careers/students-and-graduates/",
            "https://www.bosch.ca/careers/students-and-graduates/",
        ],
    },
    {
        "identity": "us-rdp",
        "title": "Bosch USA Rotational Development Program",
        "url": "https://www.bosch.us/careers/start-your-career/graduates/",
        "categories": ["training", "jobs"],
        "hosts": ["US"],
        "summary": "An 18–24-month US graduate rotational programme with executive "
        "mentoring. Requires a related bachelor’s or master’s degree, GPA of at "
        "least 3.0, and a completed internship/co-op or 6 months–5 years of "
        "relevant full-time experience. Candidates must accept relocations and "
        "have indefinite US work authorization; future sponsorship is "
        "unavailable.",
        "proof": "Bachelor/master in listed "
        "engineering/computing/supply-chain/finance/accounting/marketing/HR "
        "disciplines; GPA >=3.0; at least one completed internship/co-op or 6 "
        "months–5 years related full-time experience; willingness/ability multiple "
        "US relocations and international assignment; indefinite US work "
        "authorization; future sponsorship unavailable; 18–24 months function/team "
        "rotations; executive mentor and programme coordinator; international "
        "rotation conditional on position",
        "parents": [],
    },
    {
        "identity": "us-internship",
        "title": "Bosch USA Student Internships",
        "url": "https://www.bosch.us/careers/start-your-career/students/",
        "categories": ["internships"],
        "hosts": ["US"],
        "summary": "US internships offering degree-related practical work and team "
        "responsibilities. Applicants must be at least 18, enrolled at an "
        "accredited university in a related bachelor’s or master’s programme, and "
        "have an overall GPA of at least 3.0. Duration depends on the placement. "
        "Check Bosch for current openings.",
        "proof": "At least 18 years old; currently enrolled in accredited university "
        "pursuing related bachelor/master; overall GPA >=3.0; Degree-related "
        "practical team experience with real responsibilities; duration varies by "
        "internship",
        "parents": [],
    },
    {
        "identity": "us-coop",
        "title": "Bosch USA Co-op Programme",
        "url": "https://www.bosch.us/careers/start-your-career/students/",
        "categories": ["training", "internships"],
        "hosts": ["US"],
        "summary": "A US co-op programme with select universities, combining academic study "
        "with practical work, structured training and increasing "
        "responsibilities. Applicants must be at least 18, enrolled at an "
        "accredited university for a related bachelor’s or master’s degree, and "
        "have an overall GPA of at least 3.0.",
        "proof": "At least 18 years old; accredited-university related bachelor/master "
        "enrolment; overall GPA >=3.0; programme designated with select "
        "universities; On-the-job academic/work experience; structured "
        "technical/professional training and increased responsibilities",
        "parents": [],
    },
    {
        "identity": "ca-gsp",
        "title": "Bosch Canada Graduate Specialist Program",
        "url": "https://www.bosch.ca/careers/students-and-graduates/",
        "categories": ["training", "jobs"],
        "hosts": ["CA"],
        "summary": "Graduate specialist development through rotations across functions, "
        "divisions and locations, with mentoring from experienced managers. The "
        "programme develops subject expertise and professional responsibility. "
        "This overview does not establish a current vacancy, specific entry "
        "threshold or closing date.",
        "proof": "Graduate audience; no specific grade, degree field, nationality, age or "
        "experience criterion established; Functional/division/location rotations; "
        "experienced manager mentoring; subject expertise and responsibility",
        "parents": [],
    },
    {
        "identity": "africa-gep",
        "title": "Bosch Africa Graduate Experience Program",
        "url": "https://www.bosch.africa/careers/students-and-graduates/",
        "categories": ["training", "jobs"],
        "hosts": [],
        "summary": "Graduate development through rotations across functions, divisions and "
        "locations, supported by experienced managers. Participants build "
        "specialist knowledge and take responsibility through practical "
        "assignments. Specific locations, entry conditions, vacancies and closing "
        "dates must be checked with Bosch.",
        "proof": "Graduate audience; no universal specific degree, nationality, "
        "age/GPA/work-authorization threshold established; "
        "Function/division/location rotations; experienced top-manager mentoring; "
        "specialist knowledge and responsibility",
        "parents": [],
    },
    {
        "identity": "za-learnership",
        "title": "Bosch South Africa Learnerships",
        "url": "https://www.bosch.africa/careers/students-and-graduates/",
        "categories": ["training"],
        "hosts": ["ZA"],
        "summary": "South African learnership framework combining structured study at a "
        "college or training centre with workplace learning linked to employment. "
        "It leads to a nationally recognized occupational qualification. This "
        "overview does not establish current enrolment, pay, a duration or "
        "specific entry requirements.",
        "proof": "Occupational learnership audience; no specific entry "
        "qualification/age/grade/enrolment requirements stated; Employment-linked "
        "structured college/training-centre study and workplace learning; "
        "nationally recognized occupational qualification",
        "parents": [],
    },
]

if __name__ == "__main__":
    sys.exit(main())
