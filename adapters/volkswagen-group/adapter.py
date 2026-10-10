"""Collect the reviewed finite Volkswagen AG youth individual job frontier.

The advertised Volkswagen AG partition excludes other Group brands,
non-youth roles and the robots-blocked HRD portal. English aliases are deduplicated.
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

SOURCE_ID = "volkswagen-group"
SOURCE_URL = WEBSITE_URL = "https://www.volkswagen-karriere.de/"
LANGUAGE = "de"
PUBLISHER_COUNTRY = "DE"
PUBLISHER_TYPE = "company"
SOURCE_NAME = "Volkswagen AG student and early-career opportunities"
ATTRIBUTION = "Volkswagen AG — original factual summaries and direct official individual opportunity links."
DESCRIPTION = "Volkswagen AG individual student and early-career listings; excludes non-youth roles, other Group brands and the robots-blocked HRD apprenticeship portal."
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
FAMILY = "youthopps-volkswagen-group-publisher-v1"
FAMILY_SOURCES = {SOURCE_ID}
COLLECTION_TIMEOUT = 2400
PUBLICATION_TIMEOUT = 900
RUN_TIMEOUT = 4320
EXPORT_TIMEOUT = 120
_STATE_SCHEMA = 1
_ARTIFACT_NAME = "volkswagen-group-pacing-state"
MAX_PUBLISHER_STARTS = 240
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
            if _PUBLISHER_BYTES >= MAX_PUBLISHER_BYTES:
                raise AdapterError("Whole publisher byte cap reached", "fetch")
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
        raw = os.environ.get("VOLKSWAGEN_GROUP_PACING_BOOTSTRAP", "")
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
    path = os.environ.get("VOLKSWAGEN_GROUP_PACING_ARTIFACT_PATH")
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
        raise AdapterError("GitHub App configuration required", "publish")
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
    descriptor = secure_file("state.lock", os.O_RDWR | (os.O_CREAT if _STATE_INITIALIZING else 0))
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
            raise AdapterError("Mature publisher state is missing", "access") from None
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
        ("issue117-vw-research", "vw-family.budget.json"),
        ("youthopps-source-triage/117", "pacing.json"),
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
    descriptor = secure_file("collection.lock", os.O_RDWR | (os.O_CREAT if _STATE_INITIALIZING else 0))
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
        raise AdapterError("Unreviewed public Volkswagen AG route", "access")
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
            raise AdapterError("Unreviewed Volkswagen AG robots origin", "access")
        read_input(key)
    parser = _ROBOTS[origin]
    if not parser.can_fetch(_USER_AGENT, url):
        raise AdapterError(
            "Publisher robots excludes required Volkswagen AG input", "access"
        )


def fetch_public(url):
    """Retry only bounded transient server failures under original pacing."""
    reviewed = public_url(url)
    check_robots(reviewed)
    for attempt in range(2):
        status, headers, body = request_bytes(reviewed, publisher=True)
        if status == 200:
            return status, headers, body
        if status in (502, 503, 504) and attempt == 0:
            with locked_budget():
                state = load_budget()
                now = time.time()
                state["not_before"] = max(state["not_before"], now + 30)
                state["observed_at"] = now
                save_budget(state)
            continue
        raise AdapterError(
            "Required publisher input returned HTTP " + str(status),
            "fetch", status,
        )




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
            "Reviewed Volkswagen AG material or frontier changed: " + key, "parse"
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
    """Exhaust all199 nominal inputs; fresh robots precede every document."""
    order = [key for key, info in INPUTS.items() if info["format"] == "robots"]
    order += [key for key, info in INPUTS.items() if info["format"] != "robots"]
    return {key: read_input(key) for key in order}


def profile_identity(profile):
    return SOURCE_ID + "-" + hashlib.sha256(
        (SOURCE_ID + "|" + profile["identity"]).encode()
    ).hexdigest()[:24]




def make_record(profile, checked=None):
    now = utc_now()
    record = json.loads(json.dumps(profile["fields"]))
    record["id"] = profile_identity(profile)
    for field in ("created_at", "updated_at", "first_seen_at", "last_seen_at", "last_checked_at"):
        record[field] = now
    window = profile["application_window"]
    today = (checked or now)[:10]
    record["status"] = "unknown"
    if window:
        if today > window["closes"]:
            record["status"] = "expired"
        elif window["opens"] <= today <= window["closes"]:
            record["status"] = "open"
    return record




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
            raise AdapterError("Unowned Volkswagen AG identity", "validate")
        expected = make_record(profile, record.get("last_checked_at"))
        if {k: v for k, v in record.items() if k not in temporal} != {
            k: v for k, v in expected.items() if k not in temporal
        }:
            raise AdapterError("Owned Volkswagen AG factual recipe changed", "validate")
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
            raise AdapterError("Invalid Volkswagen AG chronology", "validate")


def validate_records(records):
    if (
        not isinstance(records, list)
        or not records
        or len(records) != len(PROFILES)
    ):
        raise AdapterError(
            "Incomplete reviewed Volkswagen AG identity inventory", "validate"
        )
    expected_fields = set(make_record(PROFILES[0]))
    identities = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != expected_fields:
            raise AdapterError("Invalid complete25-field contract", "validate")
        if (
            not isinstance(record.get("id"), str)
            or not re.fullmatch(SOURCE_ID + r"-[0-9a-f]{24}", record.get("id", ""))
            or record["id"] in identities
        ):
            raise AdapterError(
                "Invalid or duplicate Volkswagen AG identity", "validate"
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
                    "Invalid Volkswagen AG UTC timestamp", "validate"
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
            "Incomplete reviewed Volkswagen AG material frontier", "parse"
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
            else f"Collected {len(records)} reviewed Volkswagen AG individual opportunities"
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


INPUTS = {'input-001': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Doktorandin-Doktorand-Lebensdauerprognose-Leistungsmodule-%28wmd%29-34225/1446023933/',
               'format': 'html',
               'sha256': 'ece211d42ea2a2907973a190d15a0748235608af0893c76509bc452ee6fbc451',
               'role': 'German individual all-level detail'},
 'input-002': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-PhD-Student-Lifetime-Prediction-Power-Modules-%28fmd%29-34225/1446024033/',
               'format': 'html',
               'sha256': '220f4d9190200908a5299b8ba86510b3bf37a962c2e7b9f6c088115460cfc2e1',
               'role': 'English duplicate-JobID alias detail'},
 'input-003': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Praktikum-Abschlussarbeit-GenAI-&-Machine-Learning-Leistungselektronik-%28wmd%29-34225/1445034833/',
               'format': 'html',
               'sha256': '88d75deed5dac9f0f18ca7d790519321ceb6d704c81476a270beff047c946d22',
               'role': 'German individual all-level detail'},
 'input-004': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Praktikum-Fachoberschule-kaufm%C3%A4nnisch-%28wmd%29-34225/1439332933/',
               'format': 'html',
               'sha256': '03a0e277e62f7920459e4222849637d0a7864af32d98a2ab32ddde29cd54927f',
               'role': 'German individual all-level detail'},
 'input-005': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Praktikum-Fachoberschule-technisch-%28wmd%29-34225/1439333633/',
               'format': 'html',
               'sha256': '4f8dc702064ab6e5132152d7f7aa0e4919060bd0a4d5a1775009f30d77e3288a',
               'role': 'German individual all-level detail'},
 'input-006': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Praktikum-Hardware-Test-Elektroantriebe-%28wmd%29-34225/1425947733/',
               'format': 'html',
               'sha256': 'a3c7dca5eb1eb909be5f00d6b3d05cf2b250290192c8f93ea94fa74fbc29d7da',
               'role': 'German individual all-level detail'},
 'input-007': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Werkstudentin-Werkstudent-Arbeitsordnung-%28wmd%29-34225/1432221533/',
               'format': 'html',
               'sha256': '391193f6cf787d1051a831dc58d17197ab0b6670618ff9c9d62fc6c86b9aaac1',
               'role': 'German individual all-level detail'},
 'input-008': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-38037/1411109133/',
               'format': 'html',
               'sha256': '7cb7c66972bfc8abb36405bb9b510af984eb678008b703ff37d8789966656f80',
               'role': 'German individual all-level detail'},
 'input-009': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Informations-und-Systemtechnik-%28wmd%29-2027-38037/1405555233/',
               'format': 'html',
               'sha256': 'c866bf4b80fc44df0986bceaa3688fc0f7ed4a47c377ae74638adbfe1675e40f',
               'role': 'German individual all-level detail'},
 'input-010': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Fachkraft-f%C3%BCr-Lagerlogistik-%28wmd%29-2027-38037/1411109833/',
               'format': 'html',
               'sha256': '9d83460c8f16c048c38c65a13343bc045b9c8b69c11124cc9f7f8e6b9a6abe68',
               'role': 'German individual all-level detail'},
 'input-011': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-38037/1410102533/',
               'format': 'html',
               'sha256': '76e0c5c90b82ebc1f5e6b015a46eb591d69ade1c01fea9f73fe17b5cd0274a84',
               'role': 'German individual all-level detail'},
 'input-012': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-38037/1410616833/',
               'format': 'html',
               'sha256': '24f000f02f3afa87cf4aa66135316d82dddd9f953a59017754875d72177b1f47',
               'role': 'German individual all-level detail'},
 'input-013': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Zerspanungsmechanikerin-Zerspanungsmechaniker-%28wmd%29-2027-38037/1405554733/',
               'format': 'html',
               'sha256': '5003583d347ed4c8f4c5b33e35e59526fae7662e626043810371f223bf9a7218',
               'role': 'German individual all-level detail'},
 'input-014': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Duales-Studium-Digital-Engineering-Maschinenbau-%28wmd%29-2027-38037/1411118833/',
               'format': 'html',
               'sha256': 'c5694ce6a4e4b0ca84f6154144fb1c111913c19f5e7c6b02e95c33145aa2e262',
               'role': 'German individual all-level detail'},
 'input-015': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Duales-Studium-Elektro-und-Informationstechnik-%28wmd%29-2027-38037/1411121033/',
               'format': 'html',
               'sha256': '6ccf651f7412f775e42c08676859f472899a8a5ada457cf3e5d905a80850b300',
               'role': 'German individual all-level detail'},
 'input-016': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Duales-Studium-Fahrzeuginformatik-%28wmd%29-2027-38037/1411122233/',
               'format': 'html',
               'sha256': 'e7cec515f410a6b30f17ffecd24c8e2f953e3ba1137726eb60c813f0cb1d42f3',
               'role': 'German individual all-level detail'},
 'input-017': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Duales-Studium-Mechatronik-%28wmd%29-2027-38037/1411119333/',
               'format': 'html',
               'sha256': 'b70985770c5b7ad963bfa754f8100be29629d23ac2cfdb7c725bcf68b8b982a2',
               'role': 'German individual all-level detail'},
 'input-018': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Abschlussarbeit-Operative-Automatisierungstechnik-%28wmd%29-26723/1442352133/',
               'format': 'html',
               'sha256': '995012d6602381e2b69ad078ec5d08dd3ffa657c38dfef9d04d1aa0550f96dfa',
               'role': 'German individual all-level detail'},
 'input-019': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-26703/1407824233/',
               'format': 'html',
               'sha256': '17b13b69d6c996ad16df79e3ea050879f5191a01a6df9acbdda245c0b2c196fb',
               'role': 'German individual all-level detail'},
 'input-020': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Fachinformatikerin-Fachinformatiker-f%C3%BCr-Systemintegration-%28wmd%29-2027-26703/1407816633/',
               'format': 'html',
               'sha256': '8cb049e275fbc0d4b3e3486bfc35a7f1d56cbc13cb28c98ab84b069d79b3f12b',
               'role': 'German individual all-level detail'},
 'input-021': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Fachkraft-f%C3%BCr-Metalltechnik-Fachrichtung-Montagetechnik-%28wmd%29-2027-26703/1411220333/',
               'format': 'html',
               'sha256': '143a6d30ecf9d59fea868f56a7831322e83fb5ba1e8cccdf411e6849585bba9f',
               'role': 'German individual all-level detail'},
 'input-022': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Fahrzeuglackiererin-Fahrzeuglackierer-%28wmd%29-2027-26703/1405499833/',
               'format': 'html',
               'sha256': '8e9d548abfe708965f18c75f9d17cb96fe14ee843b8cd9190d584aef15b67392',
               'role': 'German individual all-level detail'},
 'input-023': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Industrieelektrikerin-Industrieelektriker-%28wmd%29-2027-26703/1407808033/',
               'format': 'html',
               'sha256': '29bb6c916395f2ed7f48c56f0438e8efaa2e72adf0c20db2c790d83a2fb782fa',
               'role': 'German individual all-level detail'},
 'input-024': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-26703/1413696133/',
               'format': 'html',
               'sha256': 'c064dcfb2d1e015ae2c9ffd69a18e1c00798c044326241c5755bf8c88d7a209c',
               'role': 'German individual all-level detail'},
 'input-025': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Kfz-Mechatronikerin-Kfz-Mechatroniker-f%C3%BCr-System-&-Hochvolttechnik-%28wmd%29-2027-26703/1405507733/',
               'format': 'html',
               'sha256': '41cd60ce8619bd075c8a414423c7625afcbf522dd9fbfc042340088c73f0e061',
               'role': 'German individual all-level detail'},
 'input-026': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Mechatronikerin-Mechatroniker-%28wmd%29-2027-26703/1410689633/',
               'format': 'html',
               'sha256': 'c6bb59d6f35bcef81a6053805aca13116d666e986bd6268f9fdd60152be9d39c',
               'role': 'German individual all-level detail'},
 'input-027': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Technische-Produktdesignerin-Technischer-Produktdesigner-%28wmd%29-2027-26703/1413714833/',
               'format': 'html',
               'sha256': '5f88a493e988b82a23d4eabe0abc3c23e3c60db280cfd7f4918d8832343d321e',
               'role': 'German individual all-level detail'},
 'input-028': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-26703/1407832133/',
               'format': 'html',
               'sha256': 'c880fe87f1746ff0c1423e358737096e72cbd97b06613e3d48db5a2be8cc39b1',
               'role': 'German individual all-level detail'},
 'input-029': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Werkzeugmechanikerin-Werkzeugmechaniker-%28wmd%29-2027-26703/1413725433/',
               'format': 'html',
               'sha256': '53cfd01480c513d00c51081beb964870ca988a36503f4b28d36339ce51cbdc8c',
               'role': 'German individual all-level detail'},
 'input-030': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Duales-Studium-Elektrotechnik-%28wmd%29-2027-26703/1414143533/',
               'format': 'html',
               'sha256': '7d7db766eb68cd4cae61abc73b126b5456dd2994657c928ecb7c21b390b32d8b',
               'role': 'German individual all-level detail'},
 'input-031': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Praktikum-Operative-Automatisierungstechnik-%28wmd%29-26723/1442221833/',
               'format': 'html',
               'sha256': '2eddf95c89b9d360ce513b77866589dfde2c230297101afcea037593ff8ed299',
               'role': 'German individual all-level detail'},
 'input-032': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-30419/1411198733/',
               'format': 'html',
               'sha256': '57dfb9c799a24f29ac952dca5f8c465f1d94471e5b444463a2d447cc890a3517',
               'role': 'German individual all-level detail'},
 'input-033': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Fachkraft-f%C3%BCr-Systemgastronomie-%28wmd%29-2027-30419/1415635033/',
               'format': 'html',
               'sha256': '7e484258ce7fda0a8675bc0fbe9aab4a35ccdaf978e69e2007ca12eedada8c13',
               'role': 'German individual all-level detail'},
 'input-034': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Fertigungsmechanikerin-Fertigungsmechaniker-%28wmd%29-2027-30419/1415645233/',
               'format': 'html',
               'sha256': 'ddae5a8ad34fe2e98e661944637d6d585c3e4b06d1e3803aa167fd361eac9c06',
               'role': 'German individual all-level detail'},
 'input-035': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-30419/1413693433/',
               'format': 'html',
               'sha256': '2ad228ea3b6714e78390c71d5d617941bae72d48a699a47186c4d94d62ab835d',
               'role': 'German individual all-level detail'},
 'input-036': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Kraftfahrzeugmechatronikerin-Kraftfahrzeugmechatroniker-%28wmd%29-2027-30419/1413701733/',
               'format': 'html',
               'sha256': '9efca1287a6db4d1870de6b32ddb997d8faebf276085f67ba7c2492bff0b372b',
               'role': 'German individual all-level detail'},
 'input-037': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Mechatronikerin-Mechatroniker-%28wmd%29-2027-30419/1413708433/',
               'format': 'html',
               'sha256': '09d160a5b9a9979ecd4b7f8ec98d5e0daea1b034498c7dfd1bcbff7202abb300',
               'role': 'German individual all-level detail'},
 'input-038': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-30419/1413717433/',
               'format': 'html',
               'sha256': '45f80f98e808dcc55bbe3f39c35cad11726758e238f4272f7c2279d3592e2a08',
               'role': 'German individual all-level detail'},
 'input-039': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Duales-Studium-Elektro-und-Informationstechnik-%28wmd%29-2027-30419/1415741333/',
               'format': 'html',
               'sha256': 'dcc52d7f92e433d639178913693a5ac3f9a0a5b2f403ef34c43b4a7b2d56fcf8',
               'role': 'German individual all-level detail'},
 'input-040': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Duales-Studium-Ingenieurinformatik-%28wmd%29-2027-30419/1415752133/',
               'format': 'html',
               'sha256': '0f79b52efe00ebc7e03921f81522bdd33c76c176454035147a95316dd150bb91',
               'role': 'German individual all-level detail'},
 'input-041': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Beschaffung-Interieur-%28wmd%29-30419/1441031833/',
               'format': 'html',
               'sha256': 'e70a0c0e5694a91db9a60ef4f440a8ef793e5997d1c68fa0b07aed8747afcaad',
               'role': 'German individual all-level detail'},
 'input-042': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Data-Analytics%2C-Business-Intelligence-&-K%C3%BCnstliche-Intelligenz-%28wmd%29-30419/1446004133/',
               'format': 'html',
               'sha256': '2b7fd7ef5711df1f42c99e1b9dd002597327a11d1b57774fdde2d08e09113997',
               'role': 'German individual all-level detail'},
 'input-043': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Inboundlogistik-Just-in-Sequence-%28wmd%29-30419/1443606233/',
               'format': 'html',
               'sha256': 'ad606364edc639c1ba36ba5a18409f153f57fb5b5d2653f360f750da2402c60e',
               'role': 'German individual all-level detail'},
 'input-044': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Lackiererei-%28wmd%29-30419/1433367433/',
               'format': 'html',
               'sha256': '7c0177e855be11fada316fbb49716b9ce532314a304e823a2230a16063aa8982',
               'role': 'German individual all-level detail'},
 'input-045': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Produktmanagement-ID_-Buzz-%28wmd%29-30419/1441033733/',
               'format': 'html',
               'sha256': 'fdfcc9c0d151612c1f256897e3d089fb5e1dc4488a0e6d38cd6f47ff4776d415',
               'role': 'German individual all-level detail'},
 'input-046': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Produktstrategie-%28wmd%29-30419/1446026333/',
               'format': 'html',
               'sha256': 'b47823cab2a1861a1e15f8962a9212db4da9935302d9fbefa68230283075274f',
               'role': 'German individual all-level detail'},
 'input-047': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Vertrieb-Europa-%28wmd%29-30419/1401231733/',
               'format': 'html',
               'sha256': '91017c185d5e36fede1b0aff57dbb12bb3b2d3dccd0c510074d14acbd500ee08',
               'role': 'German individual all-level detail'},
 'input-048': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Vertriebscontrolling-%28wmd%29-30419/1443903833/',
               'format': 'html',
               'sha256': '279426bc4bebcfa4d40c11e5230df9b4f5590ffdc60143b696a0207c4cddac41',
               'role': 'German individual all-level detail'},
 'input-049': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Vorserienlogistik-%28wmd%29-30419/1433366233/',
               'format': 'html',
               'sha256': '7e230350659d61098460a2e8389783dfa07da560310a58ece8cffcbfe7b3bf1d',
               'role': 'German individual all-level detail'},
 'input-050': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-Camper-Experience-California-App-%28wmd%29-30419/1441022933/',
               'format': 'html',
               'sha256': '308db82aaec8bb600708e9a281b458914d24993e5080a64ebe0a37b9db959cab',
               'role': 'German individual all-level detail'},
 'input-051': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-E-Commerce-Management-%28mwd%29-30419/1441028633/',
               'format': 'html',
               'sha256': 'd88a611770474cc6554592a91608bcae97b173e108614b4843f48eec90f8414a',
               'role': 'German individual all-level detail'},
 'input-052': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-Finanzsteuerung-%28wmd%29-30419/1441366633/',
               'format': 'html',
               'sha256': 'dc5965743838ed1ba48876befa589c68ab3986be1c12d6214d5d2afec01573d0',
               'role': 'German individual all-level detail'},
 'input-053': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-Produktmarketing-California-Fahrzeuge-%28wmd%29-30419/1446062133/',
               'format': 'html',
               'sha256': 'ddab0c73b5cd6d3789ce12e485514067903f59dbdf46d44bab9608517820be6c',
               'role': 'German individual all-level detail'},
 'input-054': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-Projektmanagement-Filmstudio-%28wmd%29-30419/1441374733/',
               'format': 'html',
               'sha256': '8623dd9ccf2d6d2f45e8200408146f120ef902431e60d408879f5576a1243432',
               'role': 'German individual all-level detail'},
 'input-055': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-Verkaufsplanung-Deutschland-%28wmd%29-30419/1437959833/',
               'format': 'html',
               'sha256': '876bf36f390ef4a9708b93701bc55514994bfdd3cad869d40ec1351fcee422d3',
               'role': 'German individual all-level detail'},
 'input-056': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-34219/1405053833/',
               'format': 'html',
               'sha256': '0f0f65f7206310ff1d715e7baea83dfc1ea4e36a97329cb1109d627229704fe4',
               'role': 'German individual all-level detail'},
 'input-057': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Fachkraft-f%C3%BCr-Lagerlogistik-%28wmd%29-2027-34219/1404986633/',
               'format': 'html',
               'sha256': 'f288ea0f96885b827ae16e072b7a102c1f99787818c87c65091be97c8b53e62a',
               'role': 'German individual all-level detail'},
 'input-058': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Fachkraft-f%C3%BCr-Schutz-und-Sicherheit-%28wmd%29-2027-34219/1412368133/',
               'format': 'html',
               'sha256': 'a1ecfda38ee67383643517d3ac09756e876d9ba24ba1afde4d0c94fe33f68b87',
               'role': 'German individual all-level detail'},
 'input-059': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Fachkraft-f%C3%BCr-Systemgastronomie-%28wmd%29-2027-34219/1405067033/',
               'format': 'html',
               'sha256': '0d240020b8b6c3b6a0445e8ff79d7534c654d468066918df67820f86dd2b5d52',
               'role': 'German individual all-level detail'},
 'input-060': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Gie%C3%9Fereimechanikerin-Gie%C3%9Fereimechaniker-f%C3%BCr-Druck-und-Kokillenguss-%28wmd%29-2027-34219/1405395933/',
               'format': 'html',
               'sha256': 'd92915f55ef08769a9e299ad609c51702545ed7d7740478398db95cdce4318b5',
               'role': 'German individual all-level detail'},
 'input-061': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-IT-Systemelektronikerin-IT-Systemelektroniker-%28wmd%29-2027-34219/1405034433/',
               'format': 'html',
               'sha256': '5e79da88bbff52e2722dfdf147af9ec4fef339df47494e1a07dcae9f412a3413',
               'role': 'German individual all-level detail'},
 'input-062': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-34219/1405439533/',
               'format': 'html',
               'sha256': '1ad24c37728187fe44e98ace36d892f732de442fa1163750a5ddd32c2e3dc30b',
               'role': 'German individual all-level detail'},
 'input-063': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Mechatronikerin-Mechatroniker-%28wmd%29-2027-34219/1405072933/',
               'format': 'html',
               'sha256': '66a3460d33a24a452642bdd4d2014dd05d4539772e3f1218cc3ebf5ce5795ed3',
               'role': 'German individual all-level detail'},
 'input-064': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-34219/1405077633/',
               'format': 'html',
               'sha256': 'dfd8ac927ce165bb1011a0cb0f1ab2d3da7ace1f2c1cc58e4dc22ff7d2020780',
               'role': 'German individual all-level detail'},
 'input-065': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Werkstoffpr%C3%BCferin-Werkstoffpr%C3%BCfer-%28wmd%29-2027-34219/1405421433/',
               'format': 'html',
               'sha256': '7753a7cb351f2b4e10741a31bb063bd712e9a85359a4e60366e886ea0af2d571',
               'role': 'German individual all-level detail'},
 'input-066': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Elektrotechnik-%28wmd%29-2027-34219/1405084733/',
               'format': 'html',
               'sha256': 'a84d4f89723ce3fb5436ea5254b356e9f92849bf230d5630d0fb7a2614957680',
               'role': 'German individual all-level detail'},
 'input-067': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Maschinenbau-%28wmd%29-2027-34219/1405120333/',
               'format': 'html',
               'sha256': 'fd79fc76d1351a5f8f15860e6f1ec5175619228bc64d46bdfe3587f299ce2737',
               'role': 'German individual all-level detail'},
 'input-068': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Maschinenbau-Schwerpunkt-Gie%C3%9Fereitechnik-%28wmd%29-2027-34219/1405124733/',
               'format': 'html',
               'sha256': 'd1c05035a4235003aa20cebad95638d6eb2bb592d4ee55463b54aa30ed4509b9',
               'role': 'German individual all-level detail'},
 'input-069': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Wirtschaftsinformatik-%28wmd%29-2027-34219/1415578133/',
               'format': 'html',
               'sha256': '58ec151babc453b94f2e9100581f1e218653c50598c9905498415400d2b5cfa6',
               'role': 'German individual all-level detail'},
 'input-070': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Wirtschaftsingenieurwesen-Schwerpunkt-Elektrotechnik-%28wmd%29-2027-34219/1405129433/',
               'format': 'html',
               'sha256': 'f3814b86079ae694e2283462d2aaf24a82d28e1cd95e51b4188a8a1a5b93584e',
               'role': 'German individual all-level detail'},
 'input-071': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Wirtschaftsingenieurwesen-Schwerpunkt-Maschinenbau-%28wmd%29-2027-34219/1405136533/',
               'format': 'html',
               'sha256': '87ec12263e94a10ff56e2e153667eaca7b830d185b74ba6253513fb4ec3f0698',
               'role': 'German individual all-level detail'},
 'input-072': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Praktikum-Erprobung-Antriebsmodule-%28wmd%29-34219/1435589233/',
               'format': 'html',
               'sha256': 'a56fccc1c0296255bed680ae0928b4e5ebb2b57f145d2b8e012561624fbf7375',
               'role': 'German individual all-level detail'},
 'input-073': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Werkstudentin-Werkstudent-After-Sales-Supply-Chain-%28wmd%29-34219/1445550633/',
               'format': 'html',
               'sha256': '7f2c6bd6577a87760161ab5df27b4e6f6b5deca0e16b49ce480b5b169edf61bd',
               'role': 'German individual all-level detail'},
 'input-074': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Werkstudentin-Werkstudent-Fertigungsprozessentwicklung-E-Antrieb-%28wmd%29-34219/1445572433/',
               'format': 'html',
               'sha256': '7bb6c40a0ebc1401cf8e58b82cb51c60b63344cbc3cc9310cf571d0b7a16abd6',
               'role': 'German individual all-level detail'},
 'input-075': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Praktikum-Abschlussarbeit-CO2-Einsatz-als-Rohstoff-inkl_-Analytik-%28wmd%29/1426505833/',
               'format': 'html',
               'sha256': '016d2c2f86f59f5728858e61e201698f48770ef92c03c840c4def808bf47a3ee',
               'role': 'German individual all-level detail'},
 'input-076': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Praktikum-Abschlussarbeit-Kunststoffrecycling-%28wmd%29/1444149733/',
               'format': 'html',
               'sha256': 'e018d92fe268a618071bc282bd1d81234f972adabcc0eeea651584f1cb969f0a',
               'role': 'German individual all-level detail'},
 'input-077': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Ausbildung-Chemielaborantin-Chemielaborant-%28wmd%29-2027-38231/1410618533/',
               'format': 'html',
               'sha256': '16b1b25953f6d95b3e52569f5d7918d74a45949c56db3224f7894869ac75f49c',
               'role': 'German individual all-level detail'},
 'input-078': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-38231/1410619933/',
               'format': 'html',
               'sha256': '3174cc3f99d49b68816e124c34dabd806f65f264b6627e758f3b8a15b225209c',
               'role': 'German individual all-level detail'},
 'input-079': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Informations-und-Systemtechnik-%28wmd%29-2027-38231/1410620633/',
               'format': 'html',
               'sha256': 'e8f777ad904e8835867c74cd74ebd543c01c5868571ccc9f04b1d2d2dbdcb514',
               'role': 'German individual all-level detail'},
 'input-080': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-38231/1410622633/',
               'format': 'html',
               'sha256': '09473ea0151517c7537eafff5fe9c2cbce08c03d9ffff9e11ae235343c139a59',
               'role': 'German individual all-level detail'},
 'input-081': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-38231/1410623433/',
               'format': 'html',
               'sha256': '1bde6872a703d91341a2390a133ba986f7829f4753ad60efbb1f014df05762f8',
               'role': 'German individual all-level detail'},
 'input-082': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Duales-Studium-Batterie-und-Wasserstofftechnologie-%28wmd%29-2027-38231/1410627233/',
               'format': 'html',
               'sha256': 'ee0f18a3f0b5f13636c87981552a357683efe439312db00ddc321570c5e3cde5',
               'role': 'German individual all-level detail'},
 'input-083': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Duales-Studium-Chemie-%28wmd%29-2027-38231/1410628733/',
               'format': 'html',
               'sha256': '03e2448aa210f0b0a91ab768ebec52aec5a8f508ee8acf1c0bc0a210ae1c4542',
               'role': 'German individual all-level detail'},
 'input-084': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Duales-Studium-Chemieingenieurwesen-%28wmd%29-2027-38231/1410629233/',
               'format': 'html',
               'sha256': '4dec8075c1923948b0b920b81b31e788f4021e4dbeffdfa488c55c14be4ec0f0',
               'role': 'German individual all-level detail'},
 'input-085': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Duales-Studium-Digital-Engineering-Maschinenbau-%28wmd%29-2027-38231/1410645533/',
               'format': 'html',
               'sha256': 'c0202b9fb509506316357d44e8c59293a3f8ef4d3210bb8e15ef980fa92011b4',
               'role': 'German individual all-level detail'},
 'input-086': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Duales-Studium-Elektro-und-Informationstechnik-%28wmd%29-2027-38231/1410635133/',
               'format': 'html',
               'sha256': '1e3b4fa0166d898dd3a3cfb29e675dabf83c8fc06b62ae77264e5914dabc1669',
               'role': 'German individual all-level detail'},
 'input-087': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Internship-Battery-Cell-Technology-Development-%28fmd%29-38231/1438371433/',
               'format': 'html',
               'sha256': '8850b7a64a95f19341c63a5fe045847217541be837560c0b62c27f6ed40bf5f6',
               'role': 'English duplicate-JobID alias detail'},
 'input-088': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Praktikum-Entwicklung-Batteriezelle-%28wmd%29-38231/1437461733/',
               'format': 'html',
               'sha256': '4f4381637e42d66d26bf945470054ba0f21f85014956b665f23c9ca6a2340f58',
               'role': 'German individual all-level detail'},
 'input-089': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-%28Senior%29-Consultant%2C-Project-Manager-Volkswagen-Group-Consulting-%28wmd%29-38436/1407460233/',
               'format': 'html',
               'sha256': 'f4452efcb0ba58b535aa0dc9d740d5d428101ea03c5396b3941e8e7495aee9f0',
               'role': 'German individual all-level detail'},
 'input-090': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Abschlussarbeit-%28Master%29-Klimatisierung-%28wmd%29-38436/1390265033/',
               'format': 'html',
               'sha256': '07ef2be92ccf060efb238e00393e2321b3141090f26a05b0267701a6a305a16f',
               'role': 'German individual all-level detail'},
 'input-091': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Abschlussarbeit-Aerodynamik-%28wmd%29-38436/1391398833/',
               'format': 'html',
               'sha256': 'e6275da45613299956f666fc8f2461f2bf24bf7802a936243d5b7fb5b91a0403',
               'role': 'German individual all-level detail'},
 'input-092': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Apprenticeship%2C-Internship-&-Academic-Programs-%28fmd%29-38436/1437940733/',
               'format': 'html',
               'sha256': 'baf68c993e158d706040ad6fac7109a63dd1a8828321c0b49f3649c79b330d99',
               'role': 'English duplicate-JobID alias detail'},
 'input-093': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Apprenticeship%2C-Internship-&-Academic-Programs-%28fmd%29-38440/1435700133/',
               'format': 'html',
               'sha256': 'a2efa90ece0b27b4df8f7baeca587531efc151648765cc15ae1a471877f3f16f',
               'role': 'English duplicate-JobID alias detail'},
 'input-094': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Associate-Principal-oder-Senior-Project-Manager-in-der-Volkswagen-Group-Consulting-%28wmd%29-38436/1024301901/',
               'format': 'html',
               'sha256': 'ac39dd2b857ecf9eb49f75ccb15ffdb73602aba5cac766c99a691e86b80011e1',
               'role': 'German individual all-level detail'},
 'input-095': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-38436/1411195433/',
               'format': 'html',
               'sha256': 'e310aa43d8c09bd7dac357c8518d1e8b40d5fd6a4229dc0983ca2fba04b0dc45',
               'role': 'German individual all-level detail'},
 'input-096': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Informations-und-Systemtechnik-%28wmd%29-2027-38436/1411191933/',
               'format': 'html',
               'sha256': 'd53f99f660e0eb4f36370f73d580b504d9f2f6980955d03599e194862aa08152',
               'role': 'German individual all-level detail'},
 'input-097': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Fachkraft-f%C3%BCr-Schutz-und-Sicherheit-%28wmd%29-2027-38436/1413683433/',
               'format': 'html',
               'sha256': '1c45f9c6f7fc03804ceb99f0ac48be0b1fe59b3748dce13ccc4ec8997e907042',
               'role': 'German individual all-level detail'},
 'input-098': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Fachkraft-f%C3%BCr-Systemgastronomie-%28wmd%29-2027-38436/1413678933/',
               'format': 'html',
               'sha256': 'cb078245348b22caad583e0da89120959a762d385bcde49b93f81bc2891af24a',
               'role': 'German individual all-level detail'},
 'input-099': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Fertigungsmechanikerin-Fertigungsmechaniker-%28wmd%29-2027-38436/1413686333/',
               'format': 'html',
               'sha256': 'e19b4a5f836024536c984b438366ab874f00f7f8530b36ce8efe45a75fe5de1c',
               'role': 'German individual all-level detail'},
 'input-100': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Gie%C3%9Fereimechanikerin-Gie%C3%9Fereimechaniker-f%C3%BCr-Druck-und-Kokillenguss-%28wmd%29-2027-38436/1413688333/',
               'format': 'html',
               'sha256': '49c282e7936d3f5143a88af3b0963b5f1b8ae5fc8a064e505af8999ee4f356e9',
               'role': 'German individual all-level detail'},
 'input-101': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Industriekauffrau-Industriekaufmann-%28wmd%29-2027-38436/1401796233/',
               'format': 'html',
               'sha256': '9b571bff10c6f274cee185b92499bf301574ee698a85187c0cb326fd04d02d62',
               'role': 'German individual all-level detail'},
 'input-102': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-38436/1413691833/',
               'format': 'html',
               'sha256': '008ba65fffdecc206824b4dd3cf94dbe9f196bc9e47ae9ff1d489bf8a0c7a706',
               'role': 'German individual all-level detail'},
 'input-103': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-K%C3%B6chin-Koch-%28wmd%29-2027-38436/1413700133/',
               'format': 'html',
               'sha256': 'c05284e44abbb54b212aefb98b67aa26158bba19b74923b0d07f6f03af7cf57d',
               'role': 'German individual all-level detail'},
 'input-104': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Kauffrau-Kaufmann-f%C3%BCr-B%C3%BCromanagement-%28wmd%29-2027-38436/1401809933/',
               'format': 'html',
               'sha256': 'c803e81164b8390eb732738ff9beed6d0befc34500b738a1db49c1144dd3202f',
               'role': 'German individual all-level detail'},
 'input-105': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Kauffrau-Kaufmann-f%C3%BCr-Digitalisierungsmanagement-%28wmd%29-2027-38436/1401791333/',
               'format': 'html',
               'sha256': 'fae8f865d1431c604ae3704466ebe7f46c517b1023c2d6089515cc12ef6e1942',
               'role': 'German individual all-level detail'},
 'input-106': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Kfz-Mechatronikerin-Kfz-Mechatroniker-f%C3%BCr-System-&-Hochvolttechnik-%28wmd%29-2027-38436/1413704833/',
               'format': 'html',
               'sha256': '26c66af1b94c4aa7a05dde44e253cc491745031eeccaaf4fa3926e2b34ce207f',
               'role': 'German individual all-level detail'},
 'input-107': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Mechatronikerin-Mechatroniker-%28wmd%29-2027-38436/1413707733/',
               'format': 'html',
               'sha256': 'de3507dc27026d905a2e9ced6d9aee816dbeb30e9963a0500187c0cb109ecf8d',
               'role': 'German individual all-level detail'},
 'input-108': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Mediengestalterin-Mediengestalter-Digital-und-Print-%28wmd%29-2027-38436/1407959133/',
               'format': 'html',
               'sha256': '62ed41d8f7f46c58a7ea1314e54cfbd144e14ef46a0ac9644265a813e34639f6',
               'role': 'German individual all-level detail'},
 'input-109': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Medientechnologin-Medientechnologe-Druck-%28wmd%29-2027-38436/1413711833/',
               'format': 'html',
               'sha256': 'ccbee81619debe27ded650c3a308c52dff30a7d458093b4b370d4444b2ef1176',
               'role': 'German individual all-level detail'},
 'input-110': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-38436/1413716033/',
               'format': 'html',
               'sha256': 'dbf5f5474e2ddb354feab53668d0131fe1d5e123834666e58b304d320ecfb693',
               'role': 'German individual all-level detail'},
 'input-111': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildungsintegriertes-Duales-Studium-Digital-Engineering-Maschinenbau-%28wmd%29-2027-38436/1415714633/',
               'format': 'html',
               'sha256': '01f5e9a877f05ba90af9e5726eb52a5afdd38806505ad518ebb65fc3fbb72ba0',
               'role': 'German individual all-level detail'},
 'input-112': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildungsintegriertes-Duales-Studium-Elektro-und-Informations%C2%ADtechnik-%28wmd%29-2027-38436/1415718033/',
               'format': 'html',
               'sha256': '1411b1a367859c966b00135a231681fef25c4c7b59bcb96b20db9cd1a1d2497f',
               'role': 'German individual all-level detail'},
 'input-113': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildungsintegriertes-Duales-Studium-WING-Elektro-und-Informationstechnik-%28wmd%29-2027-38436/1415719733/',
               'format': 'html',
               'sha256': '76c33b3ea8746c379db3572ebce2f8641b0cc20f20c07b5e5ae22b666f6d3bfc',
               'role': 'German individual all-level detail'},
 'input-114': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildungsintegriertes-Duales-Studium-Wirtschaftsingenieurwesen-Maschinenbau-%28wmd%29-2027-38436/1415722433/',
               'format': 'html',
               'sha256': 'e79cad2e89105d60ad94113d15f2ccb8ca5825927d778fc69b8afec59ebc8cc7',
               'role': 'German individual all-level detail'},
 'input-115': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Consultant-Volkswagen-Group-Consulting-%28wmd%29-38436/1407454333/',
               'format': 'html',
               'sha256': 'a5a18ec25fb2854d2d3c46d91adb6bad2ac34d27974c1a0103814fad8242ce96',
               'role': 'German individual all-level detail'},
 'input-116': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Director-Mergers-&-Acquisitions-%28wmd%29-38436/1257912701/',
               'format': 'html',
               'sha256': 'a97ca68621fc0d4ee1503896c7cd84db62e4894dc08826dc901e92a207020238',
               'role': 'German individual all-level detail'},
 'input-117': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Director-Mergers-&-Acquisitions-%28wmd%29-38436/1257913701/',
               'format': 'html',
               'sha256': '37441266cdef22fbdf246d6d08da0128c2af3c0c77e688b5972ebd5464b03330',
               'role': 'German individual all-level detail'},
 'input-118': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Doktorandin-Doktorand-Entwicklung-AI%E2%80%91Native-Entwicklungsmodelle-&-virtuelle-KI%E2%80%91Agenten-%28wmd%29-38436/1406590533/',
               'format': 'html',
               'sha256': '37ea1fabe9bc1ebc6864c08410ca8b6419db592ea95d1d2ef91223b842970952',
               'role': 'German individual all-level detail'},
 'input-119': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Doktorandin-Doktorand-KI-Systeme-Prozessplanung-Automobilproduktion-%28wmd%29-38436/1441334833/',
               'format': 'html',
               'sha256': 'fb70884a3938e7cf5fb6914e71cde1c59651c27b0bafe5b13298b132e7f56951',
               'role': 'German individual all-level detail'},
 'input-120': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Doktorandin-Doktorand-KI-gest%C3%BCtzte-globale-Kundenfeedback-und-Entscheidungsplattformen-%28wmd%29-38436/1434081033/',
               'format': 'html',
               'sha256': 'f5daf5d6dc167c66fcb5224a843f82cfd9dfe4e542e4fd3916564c6cbdfa8583',
               'role': 'German individual all-level detail'},
 'input-121': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Doktorandin-Doktorand-Leistungselektronik-%28wmd%29-38436/1440148933/',
               'format': 'html',
               'sha256': 'b1a8489e9670331aa4400480ece33feb9ba45d8baa19729b3de1ea6f3f527e19',
               'role': 'German individual all-level detail'},
 'input-122': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-AI-Engineering-%28wmd%29-2027-38436/1444719633/',
               'format': 'html',
               'sha256': 'd7b161c4087236edb456a52c0b4532ac57da686932f3b4b152c80d06e72e1c37',
               'role': 'German individual all-level detail'},
 'input-123': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Bauingenieurwesen-%28wmd%29-2027-38436/1415728933/',
               'format': 'html',
               'sha256': 'a6f90cb75f9bd0f7b27f0fe151fb8ae2846bcfaf1a25b4a64c06088e00ebd74a',
               'role': 'German individual all-level detail'},
 'input-124': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Betriebswirtschaft-%28wmd%29-2027-38436/1415731233/',
               'format': 'html',
               'sha256': '6b7f30973d9e6869ae813f6a9fa606474b810dfe8d6cda5e2f553c1cc25e9bb3',
               'role': 'German individual all-level detail'},
 'input-125': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Chemie-%28wmd%29-2027-38436/1415727933/',
               'format': 'html',
               'sha256': 'f8fefc63af9a6587319a3a8af42fd92b52caac00d9a331581d0ce0023335578b',
               'role': 'German individual all-level detail'},
 'input-126': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Energie-und-Geb%C3%A4udetechnik-%28wmd%29-2027-38436/1415742833/',
               'format': 'html',
               'sha256': '695be6a09124d708d2adc2708cbd1492f76ebc1ffa6490578e7e93c9c8ef3895',
               'role': 'German individual all-level detail'},
 'input-127': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Fahrzeuginformatik-%28wmd%29-2027-38436/1415744133/',
               'format': 'html',
               'sha256': '1a66b134c4d4364178eb3586c99eea73da25ff025bb7c0ea30624d8be838228d',
               'role': 'German individual all-level detail'},
 'input-128': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Fahrzeugtechnik-%28wmd%29-2027-38436/1415746333/',
               'format': 'html',
               'sha256': '018a83cfadaf734251316910b6c9a5abb0ee1f4282b789b117b3ff2d9b9af3a5',
               'role': 'German individual all-level detail'},
 'input-129': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Informatik-%28wmd%29-2027-38436/1415749433/',
               'format': 'html',
               'sha256': '89fde97fc1311f694b0201a06704757d34cf6efffe4845fb55261365f7b5ab1d',
               'role': 'German individual all-level detail'},
 'input-130': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Ingenieurinformatik-%28wmd%29-2027-38436/1415753733/',
               'format': 'html',
               'sha256': '03a7061368e49b3e4c9a3ecca1553715d38d02ff7cab7edb0356b92d8e453af2',
               'role': 'German individual all-level detail'},
 'input-131': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Internationales-Management-%28wmd%29-2027-38436/1415756733/',
               'format': 'html',
               'sha256': '1b7ba726717ab233db70953075c4153f3c0d99ab766b09d3846d3dc3f2ce9200',
               'role': 'German individual all-level detail'},
 'input-132': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Lehramt-Bildung-Beruf%2C-Fachrichtung-Wirtschaft-und-Verwaltung-%28wmd%29-2027-38436/1415762233/',
               'format': 'html',
               'sha256': '31c1f474df1fe9ec5525b391054222927e528eeacb99bc2857f31673aa97a8fa',
               'role': 'German individual all-level detail'},
 'input-133': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Logistik-und-Informationsmanagement-%28wmd%29-2027-38436/1415763933/',
               'format': 'html',
               'sha256': 'e5f2ce9e227a5aaff01641b5ab9b2886ace35d6e07897ed438b2237cd1b6a893',
               'role': 'German individual all-level detail'},
 'input-134': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Materialwissenschaft-und-Werkzeugtechnik-%28wmd%29-2027-38436/1415767533/',
               'format': 'html',
               'sha256': 'd020d071887a323273477ad4e8d727ea6ae27d494096a557cede583bb577b137',
               'role': 'German individual all-level detail'},
 'input-135': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Smart-Vehicle-Systems-%28wmd%29-2027-38436/1415770133/',
               'format': 'html',
               'sha256': '31eefef435eb9ea656d5faf282a1d07ba8aecd35ee9660333bb80354ec19ed6e',
               'role': 'German individual all-level detail'},
 'input-136': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Wirtschaftsinformatik-%28wmd%29-2027-38436/1415771433/',
               'format': 'html',
               'sha256': '55581126be6f597ca3dd5a54c44c427addd7bd4029640a30c98fc9e2ac4650ce',
               'role': 'German individual all-level detail'},
 'input-137': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Wirtschaftsingenieur-Logistik-%28wmd%29-2027-38436/1415778433/',
               'format': 'html',
               'sha256': '560291a69f30177e9dde7ad3cfb35251aedfd825010fb056c912a3a212af45e4',
               'role': 'German individual all-level detail'},
 'input-138': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Fach%C3%A4rztin-Facharzt-%28in-Weiterbildung%29-f%C3%BCr-Arbeitsmedizin-%28wmd%29-38436/1372918933/',
               'format': 'html',
               'sha256': '2dd402cf9d2776bae47cd7dfc91658e2ba9cb94eba51110b667ecb69de56f64c',
               'role': 'German individual all-level detail'},
 'input-139': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Masterabschlussarbeit-Cybersecurity-%28wmd%29-38436/1432396533/',
               'format': 'html',
               'sha256': 'dbe45ef38f8ea2bfec4ab002d6955d3ceabcf5729a2e4ddd7ab8d872f150d7c9',
               'role': 'German individual all-level detail'},
 'input-140': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Masterarbeit-Virtuelle-Absicherung-%28wmd%29-38436/1446057233/',
               'format': 'html',
               'sha256': 'fb149d3a46cdbd5e688f1cc0777147bbcadf8491264408d25a8ab39d959c04e5',
               'role': 'German individual all-level detail'},
 'input-141': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-PhD-Student-AI-driven-Global-Customer-Feedback-And-Decision-making-Platforms-%28fmd%29-38436/1434081133/',
               'format': 'html',
               'sha256': '4ed52839371199c20b9553975a18f971f7b3989769b2fb5db59b1b3cb9bd8eb8',
               'role': 'English duplicate-JobID alias detail'},
 'input-142': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-PhD-Student-Agentic-AI-Process-Design-Automotive-Production-%28fmd%29-38436/1441334933/',
               'format': 'html',
               'sha256': 'ceb62ea3c0f7bb0129c46b81a1e0d3ddebd88057eb83826ef41f1cb88086dbfc',
               'role': 'English duplicate-JobID alias detail'},
 'input-143': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-PhD-Student-Development-AI%E2%80%91Native-Development-Models-&-Virtual-AI-Agents-%28fmd%29-38436/1406590633/',
               'format': 'html',
               'sha256': '306ba4ae8b8b3031f9d772d149076ef4fbed57143e296482f4d5079fb9fd1009',
               'role': 'English duplicate-JobID alias detail'},
 'input-144': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-PhD-Student-Power-Electronics-%28fmd%29-38436/1440149033/',
               'format': 'html',
               'sha256': '87f38bb27b3b4496dbe421f8776c0248ecf770f39af72c1ba35d7ee5a3617d49',
               'role': 'English duplicate-JobID alias detail'},
 'input-145': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Abschlussarbeit-Customer-Data-Analytics-&-AI-%28wmd%29-38436/1427423533/',
               'format': 'html',
               'sha256': '97a340baecf6bb767cc6463a5dc907de07249d9a351b69e6963ed6b0b5c5d73b',
               'role': 'German individual all-level detail'},
 'input-146': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Abschlussarbeit-Design-Interieur-%28wmd%29-38436/1445518133/',
               'format': 'html',
               'sha256': 'e1583fcb923acece099fdd39c56361d435e83831a5b38e68b68100ff120ac979',
               'role': 'German individual all-level detail'},
 'input-147': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Abschlussarbeit-Gefahrstoffanalytik-%28wmd%29-38436/1432775533/',
               'format': 'html',
               'sha256': '370c71214c6fdc54ba0f4a704aeb410d69b76544839fa60a1dff5e9721563ad3',
               'role': 'German individual all-level detail'},
 'input-148': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Abschlussarbeit-W%C3%A4rmeleitung-%28wmd%29-38436/1439987633/',
               'format': 'html',
               'sha256': '6a54ff4f87351ef8b877440df6bfc26bf844b38170ee1abc0c1d951c6c34a661',
               'role': 'German individual all-level detail'},
 'input-149': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Bedarfs-&-Anforderungsmanagement-%28wmd%29-38436/1432777033/',
               'format': 'html',
               'sha256': 'f01aca2ed49ab82eae24981f690e96cc0cac22d6a47d4b29d2bcdc0db0baf3e2',
               'role': 'German individual all-level detail'},
 'input-150': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Digitalisierung-Presswerk-%28wmd%29-38436/1433294733/',
               'format': 'html',
               'sha256': '4925e8728feb2dfc9bdc237087caaafa1a94f11a8316b5dacec3e8fee3a2fe91',
               'role': 'German individual all-level detail'},
 'input-151': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Digitalisierung-Werklogistik-%28wmd%29-38436/1437940633/',
               'format': 'html',
               'sha256': '1dbb82bd758d6817494a1237b2aade096ba7cfc83a21847d575254900596b6cc',
               'role': 'German individual all-level detail'},
 'input-152': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Entwicklung-Karosseriefunktionen-%28wmd%29-38436/1435699833/',
               'format': 'html',
               'sha256': 'dae6f78dd52fe92f222033d2543b62bab7030be7d954796a99150c5621dd2a93',
               'role': 'German individual all-level detail'},
 'input-153': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Global-Assignments-Steuern-und-Sozialversicherung-%28wmd%29-38436/1382060033/',
               'format': 'html',
               'sha256': '399b1a056b24d4b26de4c21c074032a5c404a3a8c047de689e8372743ae22fa6',
               'role': 'German individual all-level detail'},
 'input-154': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Grundierung-Korrosionsschutz-Klebstoffe-%28wmd%29-38436/1433292633/',
               'format': 'html',
               'sha256': '68bfd36b087636d89618fe80dc7ea0adfd5c6084e6e9cd13e3bfd3da0e10b427',
               'role': 'German individual all-level detail'},
 'input-155': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Human-Factors-%28wmd%29-38436/1426504333/',
               'format': 'html',
               'sha256': '989d86063273500558507629cfe4b1371c01d3fce8f9831756d590d01b67d80e',
               'role': 'German individual all-level detail'},
 'input-156': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-International-Marketing-Campaigns-&-Content-%28wmd%29-38436/1388391933/',
               'format': 'html',
               'sha256': 'b9f531868a61a5e59c245b21ed41fb317a26e2cb19eb50a1d94fdc5d4b502a4f',
               'role': 'German individual all-level detail'},
 'input-157': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-International-Used-Car-Strategy-%28wmd%29-38436/1440469833/',
               'format': 'html',
               'sha256': '2dfb88fc0f92c9d1c819e00aedd6a864553c693870c130fb50b070e89757f10c',
               'role': 'German individual all-level detail'},
 'input-158': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Kommunikation-%28wmd%29-38440/1401771333/',
               'format': 'html',
               'sha256': '914018dfd655c296fc669c1403dd43bc31879a103f8ca0fe1b348aecd0906f94',
               'role': 'German individual all-level detail'},
 'input-159': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Konzernbeschaffung-Produktanl%C3%A4ufe-MEB-%28wmd%29-38436/1441415633/',
               'format': 'html',
               'sha256': '3b6cc50c8e71f94878b86adec4857bdd8be0b9c578cf40bd3347edaa59170825',
               'role': 'German individual all-level detail'},
 'input-160': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Montagelinie-1-&-4-%28wmd%29-38436/1437981133/',
               'format': 'html',
               'sha256': '9f43ff361aafa513f738d31ed2131351266459626a7cef0612cb4a780fa47abd',
               'role': 'German individual all-level detail'},
 'input-161': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Montagelinie-Golf-%28wmd%29-38436/1437042733/',
               'format': 'html',
               'sha256': 'ac68a7ff807272d4de52f6495d9ffc3634148fdd415828f71e2fac628a8a9cf3',
               'role': 'German individual all-level detail'},
 'input-162': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Personal-Technische-Bereiche-%28wmd%29-38436/1443182433/',
               'format': 'html',
               'sha256': 'f01861e5a2d7bc6f47c8b86772d35e3a6a7afd22fd088a9daf0b487e4f841f84',
               'role': 'German individual all-level detail'},
 'input-163': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Pilothallenverbund-%28wmd%29-38436/1427420833/',
               'format': 'html',
               'sha256': 'bf5c6e044fa4ce2513c20a6188e193f69cb865839a73319401c6d7f4049ed59d',
               'role': 'German individual all-level detail'},
 'input-164': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Presswerk-Technikplanung-%28wmd%29-38436/1435725633/',
               'format': 'html',
               'sha256': '3d777cf74956aaebc590448479f4df19a42c21019ddf91f836a76f2e017b8328',
               'role': 'German individual all-level detail'},
 'input-165': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Produktivit%C3%A4tssteuerung-%28wmd%29-38440/1437929933/',
               'format': 'html',
               'sha256': '280826ff8605233f05f5011ec3574b5fb5220981cdfc803abd04ed8310bb9c03',
               'role': 'German individual all-level detail'},
 'input-166': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Qualit%C3%A4tssicherung-%28wmd%29-38436/1440786033/',
               'format': 'html',
               'sha256': '99076ed15f5a61fb21721f46c40f373cbc85738b07b087bba2c6d2d32e3cf50c',
               'role': 'German individual all-level detail'},
 'input-167': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Qualit%C3%A4tssicherung-Analysezentrum-Gesamtfahrzeug-Elektrik-%28wmd%29-38436/1403184933/',
               'format': 'html',
               'sha256': '9bb99077806c95153d58c2342254d106395e914f1866dded1d64fb0fa3c8eb15',
               'role': 'German individual all-level detail'},
 'input-168': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-R%C3%A4der-Reifenentwicklung-%28wmd%29-38436/1443186533/',
               'format': 'html',
               'sha256': '3676f39a499af199ecd6bf792d6bab0bc37e44c88597ec2c647b7f130011981a',
               'role': 'German individual all-level detail'},
 'input-169': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Softwareentwicklung-Messdatenanalyse-%28wmd%29-38436/1429942733/',
               'format': 'html',
               'sha256': '6052aa6459b7444689dbab9cf39913d9e30c8f47b2ef7737916343f3cdf6a820',
               'role': 'German individual all-level detail'},
 'input-170': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-UI-Design-f%C3%BCr-nutzerzentrierte-Softwarel%C3%B6sungen-%28mwd%29-38436/1443242233/',
               'format': 'html',
               'sha256': 'cfcceaa9e93e7012534c3ebbf441ae74cdb6dc1ada60078093949bf00456afbb',
               'role': 'German individual all-level detail'},
 'input-171': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Vertriebscontrolling-%28wmd%29-38436/1430065033/',
               'format': 'html',
               'sha256': 'e00290aa4991a372af370da5a53e459fb9e2485c8463e1cc2debb9c4aba1b4e3',
               'role': 'German individual all-level detail'},
 'input-172': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Visuelle-Kommunikation-%28wmd%29-38436/1427432733/',
               'format': 'html',
               'sha256': '445d41f7b77038dbf5a5978b3a8f6e1729db3108e0f60acf39bd48eccecc06a6',
               'role': 'German individual all-level detail'},
 'input-173': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Volkswagen-Group-Academy%2C-Redaktion-&-interne-Kommunikation-%28wmd%29-38436/1444149233/',
               'format': 'html',
               'sha256': 'b07a874b69978d967b849e73ff26e83074b4e95bb37e8f9d61aac4b3c765b10f',
               'role': 'German individual all-level detail'},
 'input-174': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Werkleitung-und-Fahrzeugbau-Wolfsburg-%28wmd%29-38440/1435699933/',
               'format': 'html',
               'sha256': '71633f99fae0c6285083114e23b5bb6ad2c69a2088ed868136582f2a5d90400b',
               'role': 'German individual all-level detail'},
 'input-175': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Rechtsreferendariat-Integrit%C3%A4t-&-Recht-%28wmd%29-38436/1074619901/',
               'format': 'html',
               'sha256': '3167a0321863f648fc94f355d0ff4dcce194fb9270e3a272f2a4f9a206121cb1',
               'role': 'German individual all-level detail'},
 'input-176': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Unsolicited-application-%28Senior%29-Consultant%2C-Project-Manager-Volkswagen-Group-Consulting-%28fmd%29-38436/1407460333/',
               'format': 'html',
               'sha256': '8db5b70d29d78a88a45cbc406d7f4d5dc85f13c6e655f2d24da809ef442f96d9',
               'role': 'English duplicate-JobID alias detail'},
 'input-177': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Unsolicited-application-Consultant-Volkswagen-Group-Consulting-%28fmd%29-38436/1407454433/',
               'format': 'html',
               'sha256': 'b8f2d45c7cdbbe2029e12f7623a68793e9bb944d89817038aeeeb9340c40f632',
               'role': 'English duplicate-JobID alias detail'},
 'input-178': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Visiting-Consultant-Volkswagen-Group-Consulting-%28wmd%29-38436/1129477601/',
               'format': 'html',
               'sha256': '57a319ddf41cae0aff1c0d2c0ab9f34b216b085ae79c4837ece71c97a4b89bc1',
               'role': 'German individual all-level detail'},
 'input-179': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-%28Master%29-VW-Kraftwerk-GmbH-%28wmd%29-38436/1388142733/',
               'format': 'html',
               'sha256': '251ae9bd433c1f23c6a1d9256a8f56c3b97fe30043a6201fc0670f1a7787ded9',
               'role': 'German individual all-level detail'},
 'input-180': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-%28wmd%29-Werkstoffentwicklung-38436/1437580133/',
               'format': 'html',
               'sha256': 'c36ad0b71a5e57b5868de440fa9630487ef6ef3473813715ac7fc4c2d60651f7',
               'role': 'German individual all-level detail'},
 'input-181': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-Betriebsstoffentwicklung-&-AGN-Systeme-%28wmd%29-38436/1431789533/',
               'format': 'html',
               'sha256': '71b216b0d52a5ba6e5ef8ae15685c2d66fd810b08b6fed0b86d6e189b4e04023',
               'role': 'German individual all-level detail'},
 'input-182': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-Interne-Kommunikation-%28wmd%29-38436/1435639833/',
               'format': 'html',
               'sha256': '9663375e1b34f6e08bd6d8f5f7b696bf228bc9119c229b8a3ff56aeea19fe82f',
               'role': 'German individual all-level detail'},
 'input-183': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-Kommunikation-%28wmd%29-38436/1436202133/',
               'format': 'html',
               'sha256': '90e0e45f65c5e8b88d7081174d0e84a2531507d539abe473024bddc8d63e63a4',
               'role': 'German individual all-level detail'},
 'input-184': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-Videobearbeitung-%28wmd%29-38436/1433790133/',
               'format': 'html',
               'sha256': '81a1e6565c297102748dca3a978fe522302120390dc7be4578caa7794df1dce6',
               'role': 'German individual all-level detail'},
 'input-185': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/search/?q=&sortColumn=referencedate&sortDirection=desc&searchby=location&d=15&optionsFacetsDD_facility=Volkswagen+AG&startrow=100&locale=de_DE',
               'format': 'html',
               'sha256': '4042a4b8520a1c8cc2dbbfb299d48dac312d9a9f61cc0685a450403ede9feb0d',
               'role': 'literal finite German pagination'},
 'input-186': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/search/?q=&sortColumn=referencedate&sortDirection=desc&searchby=location&d=15&optionsFacetsDD_facility=Volkswagen+AG&startrow=125&locale=de_DE',
               'format': 'html',
               'sha256': '8db9ae2e4a9b4e3a7a39fb33de9fba19bd26223d7606f306a4f0c9f0894157aa',
               'role': 'literal finite German pagination'},
 'input-187': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/search/?q=&sortColumn=referencedate&sortDirection=desc&searchby=location&d=15&optionsFacetsDD_facility=Volkswagen+AG&startrow=150&locale=de_DE',
               'format': 'html',
               'sha256': '1db968160c42cc54cac561f0338de22efe9c025c1d6cc8e8acc34d5d048cdc78',
               'role': 'literal finite German pagination'},
 'input-188': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/search/?q=&sortColumn=referencedate&sortDirection=desc&searchby=location&d=15&optionsFacetsDD_facility=Volkswagen+AG&startrow=25&locale=de_DE',
               'format': 'html',
               'sha256': 'b507dc95815aef78c74a25863b275f89add51eed62e9da83683ef3540232f8d1',
               'role': 'literal finite German pagination'},
 'input-189': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/search/?q=&sortColumn=referencedate&sortDirection=desc&searchby=location&d=15&optionsFacetsDD_facility=Volkswagen+AG&startrow=50&locale=de_DE',
               'format': 'html',
               'sha256': '54e2d6aaeb2999653d4afb67e2eed4b35485bea6e2e47a8a5d0a8fc0251133d1',
               'role': 'literal finite German pagination'},
 'input-190': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/search/?q=&sortColumn=referencedate&sortDirection=desc&searchby=location&d=15&optionsFacetsDD_facility=Volkswagen+AG&startrow=75&locale=de_DE',
               'format': 'html',
               'sha256': 'a7f99d93d18d79937f61d3f0f54f5334ad33c570cb20a7a09f83028c7f4a18c8',
               'role': 'literal finite German pagination'},
 'input-191': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/search/?searchby=location&createNewAlert=false&q=&locationsearch=&geolocation=&optionsFacetsDD_city=&optionsFacetsDD_facility=Volkswagen+AG&optionsFacetsDD_department=&optionsFacetsDD_customfield2=&optionsFacetsDD_shifttype=&optionsFacetsDD_customfield3=&locale=de_DE',
               'format': 'html',
               'sha256': 'cea6bcce03844332a91f985756f491e757355502755f9505458bef099e9315ab',
               'role': 'literal finite German pagination'},
 'input-192': {'url': 'https://jobs.volkswagen-group.com/Volkswagen/search/?searchby=location&createNewAlert=false&q=&locationsearch=&geolocation=&optionsFacetsDD_location=&optionsFacetsDD_facility=Volkswagen+AG&optionsFacetsDD_department=&optionsFacetsDD_customfield2=&optionsFacetsDD_shifttype=&optionsFacetsDD_customfield3=&locale=en_US',
               'format': 'html',
               'sha256': 'a9fc96fae228225e7c229beaa21a37a577ee0dcc0976390be0b1c3f6a4114ad7',
               'role': 'English finite ten-alias listing'},
 'input-193': {'url': 'https://jobs.volkswagen-group.com/content/Nutzungsbedingungen/?locale=de_DE',
               'format': 'html',
               'sha256': '257d17b39bbddfb809d03015552a054c22a979f585f593ef52b91a5c1e58eafa',
               'role': 'legal terms'},
 'input-194': {'url': 'https://jobs.volkswagen-group.com/robots.txt',
               'format': 'robots',
               'sha256': '91669efc2767d124199e1d848655af18d64ac71e61ae1227be7b6aa866f03caf',
               'role': 'fresh origin robots/policy'},
 'input-195': {'url': 'https://www.hrd-portal.com/robots.txt',
               'format': 'robots',
               'sha256': '58c109cc44da82a7ff610aaaa11a3801cb99eaa3cd98f3e05183607801af6608',
               'role': 'fresh origin robots/policy'},
 'input-196': {'url': 'https://www.volkswagen-karriere.de/de.html',
               'format': 'html',
               'sha256': '7fe77a3fe2cbe0f50828963bcb268f3d717b7d38954b7872198f8dada08bcec5',
               'role': 'official homepage'},
 'input-197': {'url': 'https://www.volkswagen-karriere.de/de/nutzungsbedingungen.html',
               'format': 'html',
               'sha256': '2c333250fb0f843e9ee1b1c414a8fff4e5a7bc69f804ce30007cf90e6b950185',
               'role': 'legal terms'},
 'input-198': {'url': 'https://www.volkswagen-karriere.de/en.html',
               'format': 'html',
               'sha256': '1a6f26069f0dfad07cdb9caabd2afa781b5867e48193369bc7fec10b59752eb7',
               'role': 'official homepage'},
 'input-199': {'url': 'https://www.volkswagen-karriere.de/robots.txt',
               'format': 'robots',
               'sha256': 'dd84e3470dafcf77ca25f1d3d448c84dfd4255988f006a0318437961d06368fb',
               'role': 'fresh origin robots/policy'}}
PROFILES = [{'identity': '29435',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-Kommunikation-%28wmd%29-38436/1436202133/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-10',
  'fields': {'title': 'Werkstudentin / Werkstudent Kommunikation (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-Kommunikation-%28wmd%29-38436/1436202133/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Bachelor ≥89 CP or Master in economics/communication or '
                        'related studies; good–very good grades. Web '
                        'design/maintenance and media editing skills, e.g. Adobe. DE '
                        'C1. Docs: CV; enrolment proof; current transcript; work '
                        'permit for non-EU applicants. €18.33 gross/hour.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'jobs',
             'categories': ['jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-Kommunikation-%28wmd%29-38436/1436202133/',
                                             'Actual publisher Karrierelevel: '
                                             'Studierende']}}},
 {'identity': '30199',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Werkleitung-und-Fahrzeugbau-Wolfsburg-%28wmd%29-38440/1435699933/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-09',
  'fields': {'title': 'Praktikum Werkleitung und Fahrzeugbau Wolfsburg (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Werkleitung-und-Fahrzeugbau-Wolfsburg-%28wmd%29-38440/1435699933/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Media/digital-management/business students or Gap Year; '
                        'good–very good grades. Initial digital/change-communication '
                        'knowledge and project/presentation experience. DE C1. Docs: '
                        'CV; enrolment proof except Gap Year; current transcript; '
                        'university certificate for mandatory internship. Current '
                        'statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38440',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Werkleitung-und-Fahrzeugbau-Wolfsburg-%28wmd%29-38440/1435699933/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '30114',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Praktikum-Erprobung-Antriebsmodule-%28wmd%29-34219/1435589233/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-09',
  'fields': {'title': 'Praktikum Erprobung Antriebsmodule (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Praktikum-Erprobung-Antriebsmodule-%28wmd%29-34219/1435589233/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Mechanical/vehicle/mechatronics/materials or related '
                        'students; good–very good grades. Good Office skills; testing '
                        'knowledge preferred. DE+EN B2. Docs: CV; enrolment proof; '
                        'current transcript; university certificate for mandatory '
                        'internship. Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Kassel, DE, 34219',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Praktikum-Erprobung-Antriebsmodule-%28wmd%29-34219/1435589233/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '30241',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Presswerk-Technikplanung-%28wmd%29-38436/1435725633/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-09',
  'fields': {'title': 'Praktikum Presswerk Technikplanung (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Presswerk-Technikplanung-%28wmd%29-38436/1435725633/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Mechanical/CS/electrical or related students or Gap Year, '
                        'basic study completed; good–very good grades. Basic '
                        'coding/data-analysis and Office; Python ideal; '
                        'measurement/automation knowledge advantageous. DE C1. Docs: '
                        'CV; enrolment proof except Gap Year; current transcript; '
                        'university certificate for mandatory internship. Current '
                        'statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Presswerk-Technikplanung-%28wmd%29-38436/1435725633/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '30313',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-Interne-Kommunikation-%28wmd%29-38436/1435639833/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-09',
  'fields': {'title': 'Werkstudentin / Werkstudent Interne Kommunikation (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-Interne-Kommunikation-%28wmd%29-38436/1435639833/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Media/communication/journalism or related students; Bachelor '
                        'basic studies completed AND ≥89 CP, OR Master; good–very good '
                        'grades. Social-media affinity and Photoshop/InDesign/Premiere '
                        'skills; communication/PR/journalism experience preferred. DE '
                        'C2. Docs: CV; enrolment proof except Gap Year; current '
                        'transcript; university certificate for mandatory internship; '
                        'work+residence permits and supplementary sheet if applicable '
                        'for non-EU applicants. €18.33 gross/hour.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'jobs',
             'categories': ['jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-Interne-Kommunikation-%28wmd%29-38436/1435639833/',
                                             'Actual publisher Karrierelevel: '
                                             'Studierende']}}},
 {'identity': '28381',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Entwicklung-Karosseriefunktionen-%28wmd%29-38436/1435699833/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-09',
  'fields': {'title': 'Praktikum Entwicklung Karosseriefunktionen (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Entwicklung-Karosseriefunktionen-%28wmd%29-38436/1435699833/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Mechanical/systems-engineering or related students or Gap '
                        'Year; good–very good grades. Mechatronics interest; initial '
                        'systems-engineering/CAD/requirements-tool experience '
                        'preferred. DE+EN B2. Docs: CV; enrolment proof except Gap '
                        'Year; current transcript; university certificate for '
                        'mandatory internship; work permit for non-EU applicants. '
                        'Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Entwicklung-Karosseriefunktionen-%28wmd%29-38436/1435699833/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '31008',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Doktorandin-Doktorand-Lebensdauerprognose-Leistungsmodule-%28wmd%29-34225/1446023933/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-09',
  'fields': {'title': 'Doktorandin / Doktorand Lebensdauerprognose Leistungsmodule '
                      '(w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Doktorandin-Doktorand-Lebensdauerprognose-Leistungsmodule-%28wmd%29-34225/1446023933/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Good–very good doctoral-qualifying degree in mechanical/civil '
                        'engineering/technical mathematics or related field. Materials '
                        'modelling/testing, FEM/commercial FE and good–very good '
                        'coding; Abaqus/Python preferred. DE C1 OR EN C1. Docs: CV; '
                        'transcript or Master certificate. 35h; employment years1/2/3: '
                        '€3030/3194/3497 gross/month +€167 allowance;49% '
                        'variable-bonus participation. 30 leave days+Dec24/31 off; '
                        'flex/mobile work, doctoral seminars/college; vehicle '
                        'conditions.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Baunatal, DE, 34225',
             'deadline': None,
             'language': 'de',
             'category': 'jobs',
             'categories': ['jobs', 'training'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Doktorandin-Doktorand-Lebensdauerprognose-Leistungsmodule-%28wmd%29-34225/1446023933/',
                                             'Actual publisher Karrierelevel: '
                                             'Doktoranden']}}},
 {'identity': '30765',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Masterarbeit-Virtuelle-Absicherung-%28wmd%29-38436/1446057233/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-09',
  'fields': {'title': 'Masterarbeit Virtuelle Absicherung (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Masterarbeit-Virtuelle-Absicherung-%28wmd%29-38436/1446057233/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Master students in '
                        'vehicle/mechatronics/electrical/CS/mechanical/control '
                        'engineering or related field; good–very good grades. '
                        'Drive/hybrid/simulation interest; MATLAB/Simulink or Python '
                        'experience advantageous. DE B2 OR EN B2. Docs: CV; enrolment '
                        'proof; current transcript; university certificate for '
                        'mandatory internship; work+residence permits and '
                        'supplementary sheet if applicable for non-EU applicants. '
                        'Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Masterarbeit-Virtuelle-Absicherung-%28wmd%29-38436/1446057233/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '31130',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Data-Analytics%2C-Business-Intelligence-&-K%C3%BCnstliche-Intelligenz-%28wmd%29-30419/1446004133/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-09',
  'fields': {'title': 'Praktikum Data Analytics, Business Intelligence & Künstliche '
                      'Intelligenz (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Data-Analytics%2C-Business-Intelligence-&-K%C3%BCnstliche-Intelligenz-%28wmd%29-30419/1446004133/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'CS/business-IT/data-science/industrial-engineering or related '
                        'students or Gap Year; good–very good grades. Data/AI '
                        'interest; initial data-processing/coding/BI knowledge '
                        'advantageous. DE B2. Docs: CV; enrolment proof except Gap '
                        'Year; current transcript; university certificate for '
                        'mandatory internship; work+residence permits for non-EU '
                        'applicants. Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Data-Analytics%2C-Business-Intelligence-&-K%C3%BCnstliche-Intelligenz-%28wmd%29-30419/1446004133/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '30858',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-Produktmarketing-California-Fahrzeuge-%28wmd%29-30419/1446062133/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-09',
  'fields': {'title': 'Werkstudentin / Werkstudent Produktmarketing California '
                      'Fahrzeuge (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-Produktmarketing-California-Fahrzeuge-%28wmd%29-30419/1446062133/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Bachelor ≥89 CP or Master in '
                        'business/industrial/mechanical/vehicle '
                        'engineering/business-IT/innovation or related field; '
                        'good–very good grades. Technical/strategic understanding and '
                        'PowerPoint/Excel skills. DE C1. Docs: CV; enrolment proof; '
                        'current transcript; work permit for non-EU applicants. €18.33 '
                        'gross/hour.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': None,
             'language': 'de',
             'category': 'jobs',
             'categories': ['jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-Produktmarketing-California-Fahrzeuge-%28wmd%29-30419/1446062133/',
                                             'Actual publisher Karrierelevel: '
                                             'Studierende']}}},
 {'identity': '31142',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Produktstrategie-%28wmd%29-30419/1446026333/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-09',
  'fields': {'title': 'Praktikum Produktstrategie (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Produktstrategie-%28wmd%29-30419/1446026333/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Economics/industrial-engineering/business-IT or related '
                        'students or Gap Year; good–very good grades. '
                        'Strategic/business/automotive interest and Office; '
                        'automotive/strategy experience advantageous. DE C1. Docs: CV; '
                        'enrolment proof except Gap Year; current transcript; '
                        'university certificate for mandatory internship; '
                        'work+residence permits for non-EU applicants. Current '
                        'statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Produktstrategie-%28wmd%29-30419/1446026333/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '28190',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Duales-Studium-Elektrotechnik-%28wmd%29-2027-26703/1414143533/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-10-08',
  'fields': {'title': 'Duales Studium Elektrotechnik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Duales-Studium-Elektrotechnik-%28wmd%29-2027-26703/1414143533/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,technical drawing. DE '
                        'B2. Docs: CV; all latest-report pages and all school '
                        'qualifications; foreign certificates need certified German '
                        'translations. >€1401.50/month;35h/week;22 leave days; '
                        'possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Emden, DE, 26703',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Duales-Studium-Elektrotechnik-%28wmd%29-2027-26703/1414143533/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '30692',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Werkstudentin-Werkstudent-Fertigungsprozessentwicklung-E-Antrieb-%28wmd%29-34219/1445572433/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-08',
  'fields': {'title': 'Werkstudentin / Werkstudent Fertigungsprozessentwicklung '
                      'E-Antrieb (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Werkstudentin-Werkstudent-Fertigungsprozessentwicklung-E-Antrieb-%28wmd%29-34219/1445572433/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Master or advanced Bachelor ≥90 ECTS in electrical/mechanical '
                        'or related engineering; good–very good grades. Advanced CAD, '
                        'good Office and licence B required; design/project/e-machine '
                        'experience preferred. DE+EN B2. Docs: CV; enrolment proof; '
                        'current transcript. €18.33 gross/hour.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Kassel, DE, 34219',
             'deadline': None,
             'language': 'de',
             'category': 'jobs',
             'categories': ['jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Werkstudentin-Werkstudent-Fertigungsprozessentwicklung-E-Antrieb-%28wmd%29-34219/1445572433/',
                                             'Actual publisher Karrierelevel: '
                                             'Studierende']}}},
 {'identity': '31180',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Werkstudentin-Werkstudent-After-Sales-Supply-Chain-%28wmd%29-34219/1445550633/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-08',
  'fields': {'title': 'Werkstudentin / Werkstudent After Sales Supply Chain (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Werkstudentin-Werkstudent-After-Sales-Supply-Chain-%28wmd%29-34219/1445550633/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Business-IT/economics/project-management or related students '
                        'or Gap Year; good–very good grades. Good–very good Office; '
                        'communication, creativity and initiative. DE C1,EN B2. Docs: '
                        'CV; enrolment proof; current transcript. €18.33 gross/hour.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Kassel, DE, 34219',
             'deadline': None,
             'language': 'de',
             'category': 'jobs',
             'categories': ['jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Werkstudentin-Werkstudent-After-Sales-Supply-Chain-%28wmd%29-34219/1445550633/',
                                             'Actual publisher Karrierelevel: '
                                             'Studierende']}}},
 {'identity': '30717',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Abschlussarbeit-Design-Interieur-%28wmd%29-38436/1445518133/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-08',
  'fields': {'title': 'Praktikum Abschlussarbeit Design Interieur (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Abschlussarbeit-Design-Interieur-%28wmd%29-38436/1445518133/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Industrial/product/vehicle/transportation/automotive-design '
                        'or related students; good–very good grades. Design software, '
                        'sketching/Photoshop/3D skills; AI-tool basics preferred. '
                        'DE+EN B2. Docs: CV; enrolment proof; current transcript; '
                        'university certificate for mandatory internship; '
                        'work+residence permits and supplementary sheet if applicable '
                        'for non-EU applicants. Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'training',
             'categories': ['training'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Abschlussarbeit-Design-Interieur-%28wmd%29-38436/1445518133/',
                                             'Actual publisher Karrierelevel: '
                                             'Studierende']}}},
 {'identity': '25062',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Abschlussarbeit-%28Master%29-Klimatisierung-%28wmd%29-38436/1390265033/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-07',
  'fields': {'title': 'Abschlussarbeit (Master) Klimatisierung (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Abschlussarbeit-%28Master%29-Klimatisierung-%28wmd%29-38436/1390265033/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Master in '
                        'physics/mechanical/vehicle/energy/process/fluid/thermal/climate '
                        'engineering or related field; good–very good grades. '
                        'Technical understanding; CFD/1D-calculation experience '
                        'preferred. DE B2. Docs: CV; enrolment proof; current '
                        'transcript; university certificate for mandatory internship. '
                        'Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Abschlussarbeit-%28Master%29-Klimatisierung-%28wmd%29-38436/1390265033/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '30973',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Praktikum-Abschlussarbeit-GenAI-&-Machine-Learning-Leistungselektronik-%28wmd%29-34225/1445034833/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-07',
  'fields': {'title': 'Praktikum / Abschlussarbeit GenAI & Machine Learning '
                      'Leistungselektronik (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Praktikum-Abschlussarbeit-GenAI-&-Machine-Learning-Leistungselektronik-%28wmd%29-34225/1445034833/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Bachelor >89 CP or Master in '
                        'electrical/system/CS/mechatronics/embedded systems or related '
                        'field; good–very good grades. Good coding, preferably Python, '
                        'and GenAI/LLM/AI-engineering experience; simulation tools '
                        'advantageous. EN+DE B2. Docs: CV; enrolment proof except Gap '
                        'Year; current transcript; university certificate for '
                        'mandatory internship. Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Baunatal, DE, 34225',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Praktikum-Abschlussarbeit-GenAI-&-Machine-Learning-Leistungselektronik-%28wmd%29-34225/1445034833/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '31119',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-AI-Engineering-%28wmd%29-2027-38436/1444719633/',
  'application_window': {'opens': '2026-09-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-10-06',
  'fields': {'title': 'Duales Studium AI Engineering (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-AI-Engineering-%28wmd%29-2027-38436/1444719633/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Technical understanding and analytical thinking. DE '
                        'B2. Docs: CV; all latest-report pages and all school '
                        'qualifications; foreign certificates need certified German '
                        'translations. €1401.50/month;35h/week;22 leave days; possible '
                        'hire after completion. Pension, vehicle purchase/leasing '
                        'conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-AI-Engineering-%28wmd%29-2027-38436/1444719633/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27822',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Werkzeugmechanikerin-Werkzeugmechaniker-%28wmd%29-2027-26703/1413725433/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-10-05',
  'fields': {'title': 'Ausbildung Werkzeugmechanikerin / Werkzeugmechaniker (w/m/d) '
                      '2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Werkzeugmechanikerin-Werkzeugmechaniker-%28wmd%29-2027-26703/1413725433/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in maths, '
                        'physics/German. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. €1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Emden, DE, 26703',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Werkzeugmechanikerin-Werkzeugmechaniker-%28wmd%29-2027-26703/1413725433/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27753',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Technische-Produktdesignerin-Technischer-Produktdesigner-%28wmd%29-2027-26703/1413714833/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-10-05',
  'fields': {'title': 'Ausbildung Technische Produktdesignerin / Technischer '
                      'Produktdesigner (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Technische-Produktdesignerin-Technischer-Produktdesigner-%28wmd%29-2027-26703/1413714833/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in technology, '
                        'physics/maths. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. €1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Emden, DE, 26703',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Technische-Produktdesignerin-Technischer-Produktdesigner-%28wmd%29-2027-26703/1413714833/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27751',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-26703/1413696133/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-10-05',
  'fields': {'title': 'Ausbildung Industriemechanikerin / Industriemechaniker (w/m/d) '
                      '2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-26703/1413696133/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in maths, '
                        'physics/German. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. €1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Emden, DE, 26703',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-26703/1413696133/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27058',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Fachkraft-f%C3%BCr-Schutz-und-Sicherheit-%28wmd%29-2027-38436/1413683433/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-10-05',
  'fields': {'title': 'Ausbildung Fachkraft für Schutz und Sicherheit (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Fachkraft-f%C3%BCr-Schutz-und-Sicherheit-%28wmd%29-2027-38436/1413683433/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education; licence B required. Interest in '
                        'sports, technology/German. DE B2. Docs: CV; all latest-report '
                        'pages and all school qualifications; foreign certificates '
                        'need certified German translations; current criminal-record '
                        'certificate. €1327/month;35h/week;30 leave days; possible '
                        'hire after completion. Pension, vehicle purchase/leasing '
                        'conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Fachkraft-f%C3%BCr-Schutz-und-Sicherheit-%28wmd%29-2027-38436/1413683433/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27053',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Mechatronikerin-Mechatroniker-%28wmd%29-2027-38436/1413707733/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-10-05',
  'fields': {'title': 'Ausbildung Mechatronikerin / Mechatroniker (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Mechatronikerin-Mechatroniker-%28wmd%29-2027-38436/1413707733/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in technology/manual '
                        'work. DE B2. Docs: CV; all latest-report pages and all school '
                        'qualifications; foreign certificates need certified German '
                        'translations. >€1327/month;35h/week;30 leave days; possible '
                        'hire after completion. Pension, vehicle purchase/leasing '
                        'conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Mechatronikerin-Mechatroniker-%28wmd%29-2027-38436/1413707733/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '26973',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-38436/1413691833/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-10-05',
  'fields': {'title': 'Ausbildung Industriemechanikerin / Industriemechaniker (w/m/d) '
                      '2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-38436/1413691833/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in maths, '
                        'physics/German. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. >€1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-38436/1413691833/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27045',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-38436/1413716033/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-10-05',
  'fields': {'title': 'Ausbildung Werkfeuerwehrfrau / Werkfeuerwehrmann (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-38436/1413716033/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education; age ≥16½ at start; driving '
                        'licence within18 months after training starts. Interest in '
                        'sports, technology/German. DE B2. Docs: CV; all latest-report '
                        'pages and all school qualifications; foreign certificates '
                        'need certified German translations. >€1327/month;35h/week;30 '
                        'leave days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-38436/1413716033/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27054',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Fachkraft-f%C3%BCr-Systemgastronomie-%28wmd%29-2027-38436/1413678933/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-10-05',
  'fields': {'title': 'Ausbildung Fachkraft für Systemgastronomie (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Fachkraft-f%C3%BCr-Systemgastronomie-%28wmd%29-2027-38436/1413678933/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in German, '
                        'maths/business. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. €1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Fachkraft-f%C3%BCr-Systemgastronomie-%28wmd%29-2027-38436/1413678933/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27729',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Fertigungsmechanikerin-Fertigungsmechaniker-%28wmd%29-2027-38436/1413686333/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-10-05',
  'fields': {'title': 'Ausbildung Fertigungsmechanikerin / Fertigungsmechaniker '
                      '(w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Fertigungsmechanikerin-Fertigungsmechaniker-%28wmd%29-2027-38436/1413686333/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education; Realschule recommended. Interest '
                        'in business, maths/English. DE B2. Docs: CV; all '
                        'latest-report pages and all school qualifications; foreign '
                        'certificates need certified German translations. '
                        '>€1327/month;35h/week;30 leave days; possible hire after '
                        'completion. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Fertigungsmechanikerin-Fertigungsmechaniker-%28wmd%29-2027-38436/1413686333/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27047',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Gie%C3%9Fereimechanikerin-Gie%C3%9Fereimechaniker-f%C3%BCr-Druck-und-Kokillenguss-%28wmd%29-2027-38436/1413688333/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-10-05',
  'fields': {'title': 'Ausbildung Gießereimechanikerin / Gießereimechaniker für Druck- '
                      'und Kokillenguss (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Gie%C3%9Fereimechanikerin-Gie%C3%9Fereimechaniker-f%C3%BCr-Druck-und-Kokillenguss-%28wmd%29-2027-38436/1413688333/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in maths, '
                        'physics/German. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. €1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Gie%C3%9Fereimechanikerin-Gie%C3%9Fereimechaniker-f%C3%BCr-Druck-und-Kokillenguss-%28wmd%29-2027-38436/1413688333/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27061',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-K%C3%B6chin-Koch-%28wmd%29-2027-38436/1413700133/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-10-05',
  'fields': {'title': 'Ausbildung Köchin / Koch (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-K%C3%B6chin-Koch-%28wmd%29-2027-38436/1413700133/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in German, '
                        'maths/business. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. >€1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-K%C3%B6chin-Koch-%28wmd%29-2027-38436/1413700133/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27069',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Medientechnologin-Medientechnologe-Druck-%28wmd%29-2027-38436/1413711833/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-10-05',
  'fields': {'title': 'Ausbildung Medientechnologin / Medientechnologe Druck (w/m/d) '
                      '2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Medientechnologin-Medientechnologe-Druck-%28wmd%29-2027-38436/1413711833/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education; Realschule recommended. Interest '
                        'in business, maths/English. DE B2. Docs: CV; all '
                        'latest-report pages and all school qualifications; foreign '
                        'certificates need certified German translations. '
                        '€1327/month;35h/week;30 leave days; possible hire after '
                        'completion. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Medientechnologin-Medientechnologe-Druck-%28wmd%29-2027-38436/1413711833/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27044',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Kfz-Mechatronikerin-Kfz-Mechatroniker-f%C3%BCr-System-&-Hochvolttechnik-%28wmd%29-2027-38436/1413704833/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-10-05',
  'fields': {'title': 'Ausbildung Kfz-Mechatronikerin / Kfz-Mechatroniker für System- '
                      '& Hochvolttechnik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Kfz-Mechatronikerin-Kfz-Mechatroniker-f%C3%BCr-System-&-Hochvolttechnik-%28wmd%29-2027-38436/1413704833/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in technology, '
                        'physics/computing. DE B2. Docs: CV; all latest-report pages '
                        'and all school qualifications; foreign certificates need '
                        'certified German translations. >€1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Kfz-Mechatronikerin-Kfz-Mechatroniker-f%C3%BCr-System-&-Hochvolttechnik-%28wmd%29-2027-38436/1413704833/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27085',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-30419/1413717433/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-10-05',
  'fields': {'title': 'Ausbildung Werkfeuerwehrfrau / Werkfeuerwehrmann (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-30419/1413717433/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education; age ≥16½ at start; licence B '
                        'within18 months after training starts. Interest in sports, '
                        'technology/German. DE B2. Docs: CV; all latest-report pages '
                        'and all school qualifications; foreign certificates need '
                        'certified German translations. >€1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-30419/1413717433/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '26975',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-30419/1413693433/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-10-05',
  'fields': {'title': 'Ausbildung Industriemechanikerin / Industriemechaniker (w/m/d) '
                      '2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-30419/1413693433/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in maths, '
                        'physics/German. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. >€1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-30419/1413693433/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '26985',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Kraftfahrzeugmechatronikerin-Kraftfahrzeugmechatroniker-%28wmd%29-2027-30419/1413701733/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-10-05',
  'fields': {'title': 'Ausbildung Kraftfahrzeugmechatronikerin / '
                      'Kraftfahrzeugmechatroniker (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Kraftfahrzeugmechatronikerin-Kraftfahrzeugmechatroniker-%28wmd%29-2027-30419/1413701733/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in technology, '
                        'physics/computing. DE B2. Docs: CV; all latest-report pages '
                        'and all school qualifications; foreign certificates need '
                        'certified German translations. >€1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Kraftfahrzeugmechatronikerin-Kraftfahrzeugmechatroniker-%28wmd%29-2027-30419/1413701733/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27077',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Mechatronikerin-Mechatroniker-%28wmd%29-2027-30419/1413708433/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-10-05',
  'fields': {'title': 'Ausbildung Mechatronikerin / Mechatroniker (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Mechatronikerin-Mechatroniker-%28wmd%29-2027-30419/1413708433/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in technology/manual '
                        'work. DE B2. Docs: CV; all latest-report pages and all school '
                        'qualifications; foreign certificates need certified German '
                        'translations. >€1327/month;35h/week;30 leave days; possible '
                        'hire after completion. Pension, vehicle purchase/leasing '
                        'conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Mechatronikerin-Mechatroniker-%28wmd%29-2027-30419/1413708433/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '31020',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Praktikum-Abschlussarbeit-Kunststoffrecycling-%28wmd%29/1444149733/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-05',
  'fields': {'title': 'Praktikum / Abschlussarbeit Kunststoffrecycling (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Praktikum-Abschlussarbeit-Kunststoffrecycling-%28wmd%29/1444149733/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Mechanical/materials/plastics/polymer-chemistry or related '
                        'students. Office skills, initiative and teamwork. Docs: CV; '
                        'enrolment proof; current transcript; university certificate '
                        'for mandatory internship; work+residence permits for non-EU '
                        'applicants. Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': None,
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Praktikum-Abschlussarbeit-Kunststoffrecycling-%28wmd%29/1444149733/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '31012',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Volkswagen-Group-Academy%2C-Redaktion-&-interne-Kommunikation-%28wmd%29-38436/1444149233/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-05',
  'fields': {'title': 'Praktikum Volkswagen Group Academy, Redaktion & interne '
                      'Kommunikation (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Volkswagen-Group-Academy%2C-Redaktion-&-interne-Kommunikation-%28wmd%29-38436/1444149233/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Communication/media/journalism/design/digital-media or '
                        'related students or Gap Year. Creative '
                        'writing/design/storytelling; editorial/graphics experience '
                        'and ideally CMS preferred. DE+EN C1. Docs: CV; enrolment '
                        'proof except Gap Year; current transcript; university '
                        'certificate for mandatory internship; work+residence permits '
                        'for non-EU applicants. Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Volkswagen-Group-Academy%2C-Redaktion-&-interne-Kommunikation-%28wmd%29-38436/1444149233/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '26164',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Kommunikation-%28wmd%29-38440/1401771333/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-03',
  'fields': {'title': 'Praktikum Kommunikation (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Kommunikation-%28wmd%29-38440/1401771333/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Communication/media/technology or related students or Gap '
                        'Year. Technical interest, creative language/visualization, '
                        'reliable work. DE AND EN C1. Docs: CV; enrolment proof; '
                        'current transcript; university certificate for mandatory '
                        'internship. Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38440',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Kommunikation-%28wmd%29-38440/1401771333/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '26153',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Doktorandin-Doktorand-Entwicklung-AI%E2%80%91Native-Entwicklungsmodelle-&-virtuelle-KI%E2%80%91Agenten-%28wmd%29-38436/1406590533/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-03',
  'fields': {'title': 'Doktorandin / Doktorand Entwicklung AI‑Native '
                      'Entwicklungsmodelle & virtuelle KI‑Agenten (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Doktorandin-Doktorand-Entwicklung-AI%E2%80%91Native-Entwicklungsmodelle-&-virtuelle-KI%E2%80%91Agenten-%28wmd%29-38436/1406590533/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Good–very good doctoral-qualifying degree in CS/AI-focused '
                        'business or engineering/HCI or related field. Sound AI and '
                        'software/development-process knowledge; '
                        'digital-transformation/process experience ideal. DE C1,EN B2. '
                        'Docs: CV; cover letter explaining doctorate '
                        'motivation/research focus; transcript or Master/Diplom '
                        'certificate. 35h; employment years1/2/3: €3030/3194/3497 '
                        'gross/month +€167 allowance;49% variable-bonus participation. '
                        '30 leave days+Dec24/31 off; flex/mobile work, doctoral '
                        'seminars/college; vehicle conditions.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'jobs',
             'categories': ['jobs', 'training'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Doktorandin-Doktorand-Entwicklung-AI%E2%80%91Native-Entwicklungsmodelle-&-virtuelle-KI%E2%80%91Agenten-%28wmd%29-38436/1406590533/',
                                             'Actual publisher Karrierelevel: '
                                             'Doktoranden']}}},
 {'identity': '29436',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-Videobearbeitung-%28wmd%29-38436/1433790133/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-03',
  'fields': {'title': 'Werkstudentin / Werkstudent Videobearbeitung (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-Videobearbeitung-%28wmd%29-38436/1433790133/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Basic studies completed ≥89 credits or Master in '
                        'media/design/film/journalism/communication or related field; '
                        'good–very good grades. Video production/editing and '
                        'camera/Premiere skills; other Creative Cloud advantageous. DE '
                        'C1,EN B1. Docs: CV; enrolment proof; current transcript; '
                        'work+residence permits and supplementary sheet if applicable '
                        'for non-EU applicants. €18.33 gross/hour.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'jobs',
             'categories': ['jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-Videobearbeitung-%28wmd%29-38436/1433790133/',
                                             'Actual publisher Karrierelevel: '
                                             'Studierende']}}},
 {'identity': '27984',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Doktorandin-Doktorand-KI-gest%C3%BCtzte-globale-Kundenfeedback-und-Entscheidungsplattformen-%28wmd%29-38436/1434081033/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-03',
  'fields': {'title': 'Doktorandin / Doktorand KI-gestützte globale Kundenfeedback- '
                      'und Entscheidungsplattformen (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Doktorandin-Doktorand-KI-gest%C3%BCtzte-globale-Kundenfeedback-und-Entscheidungsplattformen-%28wmd%29-38436/1434081033/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Good–very good doctoral-qualifying degree in '
                        'CS/AI/data-science/business-IT/industrial-engineering or '
                        'related field. ML/generative-AI/multi-agent knowledge plus '
                        'data/statistics/software experience. DE+EN C1. Docs: CV; '
                        'transcript or Master/Diplom certificate. 35h; employment '
                        'years1/2/3: €3030/3194/3497 gross/month +€167 allowance;49% '
                        'variable-bonus participation. 30 leave days+Dec24/31 off; '
                        'flex/mobile work, doctoral seminars/college; vehicle '
                        'conditions.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'jobs',
             'categories': ['jobs', 'training'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Doktorandin-Doktorand-KI-gest%C3%BCtzte-globale-Kundenfeedback-und-Entscheidungsplattformen-%28wmd%29-38436/1434081033/',
                                             'Actual publisher Karrierelevel: '
                                             'Doktoranden']}}},
 {'identity': '30024',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Digitalisierung-Presswerk-%28wmd%29-38436/1433294733/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-02',
  'fields': {'title': 'Praktikum Digitalisierung Presswerk (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Digitalisierung-Presswerk-%28wmd%29-38436/1433294733/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Business/business-IT/industrial-engineering or related '
                        'students or Gap Year; good–very good grades. Excel/PowerPoint '
                        'and digital/process interest; low-code/BI knowledge '
                        'advantageous. DE B2,EN B1. Docs: CV; enrolment proof except '
                        'Gap Year; current transcript; university certificate for '
                        'mandatory internship. Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Digitalisierung-Presswerk-%28wmd%29-38436/1433294733/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '29992',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Grundierung-Korrosionsschutz-Klebstoffe-%28wmd%29-38436/1433292633/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-02',
  'fields': {'title': 'Praktikum Grundierung Korrosionsschutz Klebstoffe (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Grundierung-Korrosionsschutz-Klebstoffe-%28wmd%29-38436/1433292633/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Chemistry/chemical-process-engineering or related students or '
                        'Gap Year; good–very good grades. Polymer chemistry, '
                        'statistics and Office; analytical-chemistry experience '
                        'advantageous. DE B2. Docs: CV; enrolment proof except Gap '
                        'Year; current transcript; university certificate for '
                        'mandatory internship. Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Grundierung-Korrosionsschutz-Klebstoffe-%28wmd%29-38436/1433292633/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '29950',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Lackiererei-%28wmd%29-30419/1433367433/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-02',
  'fields': {'title': 'Praktikum Lackiererei (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Lackiererei-%28wmd%29-30419/1433367433/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'CS/industrial/automation-engineering or related students or '
                        'Gap Year; good–very good grades. Automotive experience '
                        'preferred. DE B2. Docs: CV; enrolment proof except Gap Year; '
                        'current transcript; university certificate for mandatory '
                        'internship; work permit for non-EU applicants. Current '
                        'statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Lackiererei-%28wmd%29-30419/1433367433/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '29782',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Vorserienlogistik-%28wmd%29-30419/1433366233/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-02',
  'fields': {'title': 'Praktikum Vorserienlogistik (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Vorserienlogistik-%28wmd%29-30419/1433366233/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Business/technical or related students or Gap Year; logistics '
                        'focus preferred; good–very good grades. Office; '
                        'scientific-method knowledge advantageous. DE C1. Docs: CV; '
                        'enrolment proof except Gap Year; current transcript; '
                        'university certificate for mandatory internship; work permit '
                        'for non-EU applicants. Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Vorserienlogistik-%28wmd%29-30419/1433366233/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '28321',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Fachkraft-f%C3%BCr-Schutz-und-Sicherheit-%28wmd%29-2027-34219/1412368133/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-10-02',
  'fields': {'title': 'Ausbildung Fachkraft für Schutz und Sicherheit (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Fachkraft-f%C3%BCr-Schutz-und-Sicherheit-%28wmd%29-2027-34219/1412368133/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education; licence B required. Interest in '
                        'sports, technology/German. DE B2. Docs: CV; all latest-report '
                        'pages and all school qualifications; foreign certificates '
                        'need certified German translations; current criminal-record '
                        'certificate. €1327/month;35h/week;30 leave days; possible '
                        'hire after completion. Pension, vehicle purchase/leasing '
                        'conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Kassel, DE, 34219',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Fachkraft-f%C3%BCr-Schutz-und-Sicherheit-%28wmd%29-2027-34219/1412368133/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '30912',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Inboundlogistik-Just-in-Sequence-%28wmd%29-30419/1443606233/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-02',
  'fields': {'title': 'Praktikum Inboundlogistik Just in Sequence (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Inboundlogistik-Just-in-Sequence-%28wmd%29-30419/1443606233/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Logistics or related students or Gap Year; good–very good '
                        'grades. Advanced Office; supply-chain knowledge preferred. DE '
                        'C1,EN B2. Docs: CV; enrolment proof except Gap Year; current '
                        'transcript; university certificate for mandatory internship; '
                        'work+residence permits for non-EU applicants. Current '
                        'statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Inboundlogistik-Just-in-Sequence-%28wmd%29-30419/1443606233/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '30991',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Vertriebscontrolling-%28wmd%29-30419/1443903833/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-02',
  'fields': {'title': 'Praktikum Vertriebscontrolling (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Vertriebscontrolling-%28wmd%29-30419/1443903833/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Business/economics/business-IT/industrial-engineering or '
                        'related students or Gap Year; good–very good grades. '
                        'Numerical affinity and advanced IT/BI knowledge preferred. DE '
                        'C1,EN B2. Docs: CV; enrolment proof except Gap Year; current '
                        'transcript; university certificate for mandatory internship; '
                        'work+residence permits for non-EU applicants. Current '
                        'statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Vertriebscontrolling-%28wmd%29-30419/1443903833/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '29993',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Abschlussarbeit-Gefahrstoffanalytik-%28wmd%29-38436/1432775533/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-01',
  'fields': {'title': 'Praktikum / Abschlussarbeit Gefahrstoffanalytik (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Abschlussarbeit-Gefahrstoffanalytik-%28wmd%29-38436/1432775533/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Technology/natural-science or related students or Gap Year; '
                        'good–very good grades. Advanced Office; '
                        'sample-preparation/hazardous-material and HPLC/GC experience '
                        'preferred. Docs: CV; enrolment proof except Gap Year; current '
                        'transcript; university certificate for mandatory internship; '
                        'work permit for non-EU applicants. Current statutory minimum '
                        'wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Abschlussarbeit-Gefahrstoffanalytik-%28wmd%29-38436/1432775533/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '29731',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Bedarfs-&-Anforderungsmanagement-%28wmd%29-38436/1432777033/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-01',
  'fields': {'title': 'Praktikum Bedarfs- & Anforderungsmanagement (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Bedarfs-&-Anforderungsmanagement-%28wmd%29-38436/1432777033/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Bachelor applicants need ≥90CP; '
                        'Vehicle/electrical/industrial/mechanical '
                        'engineering/IT/science/business or related students or Gap '
                        'Year. Very good Office, licence B or higher; JIRA/Confluence '
                        'preferred. DE+EN B2. Docs: CV; enrolment proof except Gap '
                        'Year; current transcript; university certificate for '
                        'mandatory internship; work permit for non-EU applicants. '
                        'Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Bedarfs-&-Anforderungsmanagement-%28wmd%29-38436/1432777033/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '26211',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Vertrieb-Europa-%28wmd%29-30419/1401231733/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-01',
  'fields': {'title': 'Praktikum Vertrieb Europa (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Vertrieb-Europa-%28wmd%29-30419/1401231733/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Economics or related students or Gap Year; good–very good '
                        'grades. Confident creative PowerPoint/Excel use. EN+DE C1. '
                        'Docs: CV; enrolment proof; current transcript; university '
                        'certificate for mandatory internship; work permit for non-EU '
                        'applicants. Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Vertrieb-Europa-%28wmd%29-30419/1401231733/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '30736',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-R%C3%A4der-Reifenentwicklung-%28wmd%29-38436/1443186533/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-01',
  'fields': {'title': 'Praktikum Räder-/Reifenentwicklung (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-R%C3%A4der-Reifenentwicklung-%28wmd%29-38436/1443186533/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Mechanical/vehicle engineering/business-IT/CS/data-management '
                        'or related students or Gap Year; good–very good grades. Basic '
                        'agile understanding, database/data-quality interest and '
                        'Office. DE C2,EN B2. Docs: CV; enrolment proof except Gap '
                        'Year; current transcript; university certificate for '
                        'mandatory internship; work+residence permits and '
                        'supplementary sheet if applicable for non-EU applicants. '
                        'Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'jobs',
             'categories': ['jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-R%C3%A4der-Reifenentwicklung-%28wmd%29-38436/1443186533/',
                                             'Actual publisher Karrierelevel: '
                                             'Studierende']}}},
 {'identity': '30821',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-UI-Design-f%C3%BCr-nutzerzentrierte-Softwarel%C3%B6sungen-%28mwd%29-38436/1443242233/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-01',
  'fields': {'title': 'Praktikum UI-Design für nutzerzentrierte Softwarelösungen '
                      '(m/w/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-UI-Design-f%C3%BCr-nutzerzentrierte-Softwarel%C3%B6sungen-%28mwd%29-38436/1443242233/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'UX/UI/interaction/communication/product-design or related '
                        'students or Gap Year. Figma/design tools, digital-interface '
                        'and AI-design experience; basic agile/product understanding. '
                        'EN B2. Docs: CV; enrolment proof except Gap Year; current '
                        'transcript; university certificate for mandatory internship. '
                        'Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-UI-Design-f%C3%BCr-nutzerzentrierte-Softwarel%C3%B6sungen-%28mwd%29-38436/1443242233/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '30928',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Personal-Technische-Bereiche-%28wmd%29-38436/1443182433/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-10-01',
  'fields': {'title': 'Praktikum Personal Technische Bereiche (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Personal-Technische-Bereiche-%28wmd%29-38436/1443182433/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Economics/law/humanities/social-science or related students '
                        'or Gap Year; good–very good grades. Office; HR '
                        'focus/experience and personnel-software experience ideal. '
                        'Docs: CV; enrolment proof except Gap Year; current '
                        'transcript; university certificate for mandatory internship; '
                        'work+residence permits for non-EU applicants. Current '
                        'statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Personal-Technische-Bereiche-%28wmd%29-38436/1443182433/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '28770',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Masterabschlussarbeit-Cybersecurity-%28wmd%29-38436/1432396533/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-30',
  'fields': {'title': 'Masterabschlussarbeit Cybersecurity (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Masterabschlussarbeit-Cybersecurity-%28wmd%29-38436/1432396533/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Master in CS/cybersecurity/mathematics/engineering or related '
                        'field; good–very good grades. Embedded cybersecurity OR ML '
                        'with secure-testing/fuzzing background; Python or C/C++. EN '
                        'B2. Docs: CV; enrolment proof; current transcript; '
                        'work+residence permits and supplementary sheet if applicable '
                        'for non-EU applicants. Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Masterabschlussarbeit-Cybersecurity-%28wmd%29-38436/1432396533/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '29827',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Werkstudentin-Werkstudent-Arbeitsordnung-%28wmd%29-34225/1432221533/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-30',
  'fields': {'title': 'Werkstudentin / Werkstudent Arbeitsordnung (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Werkstudentin-Werkstudent-Arbeitsordnung-%28wmd%29-34225/1432221533/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Employment/business-law or related students or Gap Year; '
                        'good–very good grades. Good Office and proactive teamwork. DE '
                        'B2. Docs: CV; enrolment proof; current transcript. €18.33 '
                        'gross/hour.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Baunatal, DE, 34225',
             'deadline': None,
             'language': 'de',
             'category': 'jobs',
             'categories': ['jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Werkstudentin-Werkstudent-Arbeitsordnung-%28wmd%29-34225/1432221533/',
                                             'Actual publisher Karrierelevel: '
                                             'Studierende']}}},
 {'identity': '29028',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-Betriebsstoffentwicklung-&-AGN-Systeme-%28wmd%29-38436/1431789533/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-29',
  'fields': {'title': 'Werkstudentin / Werkstudent Betriebsstoffentwicklung & '
                      'AGN-Systeme (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-Betriebsstoffentwicklung-&-AGN-Systeme-%28wmd%29-38436/1431789533/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Basic studies completed ≥89 credits or Master in '
                        'engineering/science/chemistry or related field; good–very '
                        'good grades. Data-management/analysis affinity; '
                        'engine/chemistry interest preferred. DE C1 OR EN B2. Docs: '
                        'CV; enrolment proof; current transcript; work+residence '
                        'permits and supplementary sheet if applicable for non-EU '
                        'applicants. €18.33 gross/hour.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'jobs',
             'categories': ['jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-Betriebsstoffentwicklung-&-AGN-Systeme-%28wmd%29-38436/1431789533/',
                                             'Actual publisher Karrierelevel: '
                                             'Studierende']}}},
 {'identity': '25351',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-International-Marketing-Campaigns-&-Content-%28wmd%29-38436/1388391933/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-29',
  'fields': {'title': 'Praktikum International Marketing Campaigns & Content (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-International-Marketing-Campaigns-&-Content-%28wmd%29-38436/1388391933/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Business/marketing/media-design/graphics or related students '
                        'or Gap Year; good–very good grades. Office/photo-video '
                        'editing; AI interest/experience. DE+EN B2. Docs: CV; '
                        'enrolment proof; current transcript; university certificate '
                        'for mandatory internship; work permit for non-EU applicants. '
                        'Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-International-Marketing-Campaigns-&-Content-%28wmd%29-38436/1388391933/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '30626',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Abschlussarbeit-Operative-Automatisierungstechnik-%28wmd%29-26723/1442352133/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-29',
  'fields': {'title': 'Abschlussarbeit Operative Automatisierungstechnik (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Abschlussarbeit-Operative-Automatisierungstechnik-%28wmd%29-26723/1442352133/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Mechanical-engineering or related students. '
                        'Automation/manufacturing/work-organization, basic CAD and '
                        'Office; metal-trade training ideal. DE B2,EN B1. Docs: CV; '
                        'enrolment proof; current transcript; university certificate '
                        'for mandatory internship. Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Emden, DE, 26723',
             'deadline': None,
             'language': 'de',
             'category': 'training',
             'categories': ['training'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Abschlussarbeit-Operative-Automatisierungstechnik-%28wmd%29-26723/1442352133/',
                                             'Actual publisher Karrierelevel: '
                                             'Studierende']}}},
 {'identity': '27653',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Praktikum-Operative-Automatisierungstechnik-%28wmd%29-26723/1442221833/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-29',
  'fields': {'title': 'Praktikum Operative Automatisierungstechnik (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Praktikum-Operative-Automatisierungstechnik-%28wmd%29-26723/1442221833/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Electrical/automation-engineering or related students or Gap '
                        'Year; good–very good grades. Robotics and basic PLC coding; '
                        'FANUC experience advantageous, body-building basics '
                        'preferred. DE B2,EN B1. Docs: CV; enrolment proof except Gap '
                        'Year; current transcript; university certificate for '
                        'mandatory internship. Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Emden, DE, 26723',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Praktikum-Operative-Automatisierungstechnik-%28wmd%29-26723/1442221833/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '27429',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Kfz-Mechatronikerin-Kfz-Mechatroniker-f%C3%BCr-System-&-Hochvolttechnik-%28wmd%29-2027-26703/1405507733/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Kfz-Mechatronikerin / Kfz-Mechatroniker für System- '
                      '& Hochvolttechnik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Kfz-Mechatronikerin-Kfz-Mechatroniker-f%C3%BCr-System-&-Hochvolttechnik-%28wmd%29-2027-26703/1405507733/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in technology, '
                        'physics/computing. DE B2. Docs: CV; all latest-report pages '
                        'and all school qualifications; foreign certificates need '
                        'certified German translations. €1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Emden, DE, 26703',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Kfz-Mechatronikerin-Kfz-Mechatroniker-f%C3%BCr-System-&-Hochvolttechnik-%28wmd%29-2027-26703/1405507733/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27718',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-26703/1407824233/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Elektronikerin / Elektroniker für '
                      'Automatisierungstechnik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-26703/1407824233/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in computing, '
                        'English/physics. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. €1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Emden, DE, 26703',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-26703/1407824233/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27712',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Fachinformatikerin-Fachinformatiker-f%C3%BCr-Systemintegration-%28wmd%29-2027-26703/1407816633/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Fachinformatikerin / Fachinformatiker für '
                      'Systemintegration (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Fachinformatikerin-Fachinformatiker-f%C3%BCr-Systemintegration-%28wmd%29-2027-26703/1407816633/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in computing, '
                        'English/physics. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. €1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Emden, DE, 26703',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Fachinformatikerin-Fachinformatiker-f%C3%BCr-Systemintegration-%28wmd%29-2027-26703/1407816633/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27709',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Industrieelektrikerin-Industrieelektriker-%28wmd%29-2027-26703/1407808033/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Industrieelektrikerin / Industrieelektriker (w/m/d) '
                      '2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Industrieelektrikerin-Industrieelektriker-%28wmd%29-2027-26703/1407808033/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in computing, '
                        'maths/physics. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. €1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Emden, DE, 26703',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Industrieelektrikerin-Industrieelektriker-%28wmd%29-2027-26703/1407808033/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27726',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-26703/1407832133/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Werkfeuerwehrfrau / Werkfeuerwehrmann (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-26703/1407832133/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education; age ≥16½ at start; licence B '
                        'within18 months after training starts. Interest in sports, '
                        'technology/German. DE B2. Docs: CV; all latest-report pages '
                        'and all school qualifications; foreign certificates need '
                        'certified German translations. €1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Emden, DE, 26703',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-26703/1407832133/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27426',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Fahrzeuglackiererin-Fahrzeuglackierer-%28wmd%29-2027-26703/1405499833/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Fahrzeuglackiererin / Fahrzeuglackierer (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Fahrzeuglackiererin-Fahrzeuglackierer-%28wmd%29-2027-26703/1405499833/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in maths, '
                        'physics/chemistry. DE B2. Docs: CV; all latest-report pages '
                        'and all school qualifications; foreign certificates need '
                        'certified German translations. €1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Emden, DE, 26703',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Fahrzeuglackiererin-Fahrzeuglackierer-%28wmd%29-2027-26703/1405499833/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27760',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Mechatronikerin-Mechatroniker-%28wmd%29-2027-26703/1410689633/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Mechatronikerin / Mechatroniker (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Mechatronikerin-Mechatroniker-%28wmd%29-2027-26703/1410689633/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in technology/manual '
                        'work. DE B2. Docs: CV; all latest-report pages and all school '
                        'qualifications; foreign certificates need certified German '
                        'translations. €1327/month;35h/week;30 leave days; possible '
                        'hire after completion. Pension, vehicle purchase/leasing '
                        'conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Emden, DE, 26703',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Mechatronikerin-Mechatroniker-%28wmd%29-2027-26703/1410689633/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27748',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Fachkraft-f%C3%BCr-Metalltechnik-Fachrichtung-Montagetechnik-%28wmd%29-2027-26703/1411220333/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Fachkraft für Metalltechnik Fachrichtung '
                      'Montagetechnik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Fachkraft-f%C3%BCr-Metalltechnik-Fachrichtung-Montagetechnik-%28wmd%29-2027-26703/1411220333/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in technology, '
                        'physics/computing. DE B2. Docs: CV; all latest-report pages '
                        'and all school qualifications; foreign certificates need '
                        'certified German translations. €1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Emden, DE, 26703',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Emden-Ausbildung-Fachkraft-f%C3%BCr-Metalltechnik-Fachrichtung-Montagetechnik-%28wmd%29-2027-26703/1411220333/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27248',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-38231/1410622633/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Industriemechanikerin / Industriemechaniker (w/m/d) '
                      '2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-38231/1410622633/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in maths, '
                        'physics/German. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. >€1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Salzgitter, DE, 38231',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-38231/1410622633/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27754',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Duales-Studium-Chemie-%28wmd%29-2027-38231/1410628733/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Duales Studium Chemie (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Duales-Studium-Chemie-%28wmd%29-2027-38231/1410628733/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good advanced-course '
                        'maths/chemistry grades. Interest in modern '
                        'technology,chemistry. DE B2. Docs: CV; all latest-report '
                        'pages and all school qualifications; foreign certificates '
                        'need certified German translations. '
                        '€1401.50/month;35h/week;22 leave days; possible hire after '
                        'completion. Year conflict:2027 title/2026 start; confirm. '
                        'Pension, vehicle purchase/leasing conditions, bicycle '
                        'leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Salzgitter, DE, 38231',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Duales-Studium-Chemie-%28wmd%29-2027-38231/1410628733/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '26799',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Ausbildung-Chemielaborantin-Chemielaborant-%28wmd%29-2027-38231/1410618533/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Chemielaborantin / Chemielaborant (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Ausbildung-Chemielaborantin-Chemielaborant-%28wmd%29-2027-38231/1410618533/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in chemistry, '
                        'physics/maths. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. >€1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Salzgitter, DE, 38231',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Ausbildung-Chemielaborantin-Chemielaborant-%28wmd%29-2027-38231/1410618533/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27756',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Duales-Studium-Elektro-und-Informationstechnik-%28wmd%29-2027-38231/1410635133/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Duales Studium Elektro- und Informationstechnik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Duales-Studium-Elektro-und-Informationstechnik-%28wmd%29-2027-38231/1410635133/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,technical drawing. DE '
                        'B2. Docs: CV; all latest-report pages and all school '
                        'qualifications; foreign certificates need certified German '
                        'translations. €1401.50/month;35h/week;22 leave days; possible '
                        'hire after completion. Year conflict:2027 title/2026 start; '
                        'confirm. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Salzgitter, DE, 38231',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Duales-Studium-Elektro-und-Informationstechnik-%28wmd%29-2027-38231/1410635133/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27250',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-38231/1410623433/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Werkfeuerwehrfrau / Werkfeuerwehrmann (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-38231/1410623433/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education; age ≥16½ at start; licence B '
                        'within18 months after training starts. Interest in sports, '
                        'technology/German. DE B2. Docs: CV; all latest-report pages '
                        'and all school qualifications; foreign certificates need '
                        'certified German translations. >€1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Salzgitter, DE, 38231',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-38231/1410623433/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27755',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Duales-Studium-Chemieingenieurwesen-%28wmd%29-2027-38231/1410629233/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Duales Studium Chemieingenieurwesen (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Duales-Studium-Chemieingenieurwesen-%28wmd%29-2027-38231/1410629233/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good advanced-course '
                        'maths/chemistry grades. Interest in modern '
                        'technology,chemistry. DE B2. Docs: CV; all latest-report '
                        'pages and all school qualifications; foreign certificates '
                        'need certified German translations. '
                        '€1401.50/month;35h/week;22 leave days; possible hire after '
                        'completion. Year conflict:2027 title/2026 start; confirm. '
                        'Pension, vehicle purchase/leasing conditions, bicycle '
                        'leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Salzgitter, DE, 38231',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Duales-Studium-Chemieingenieurwesen-%28wmd%29-2027-38231/1410629233/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27763',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Duales-Studium-Digital-Engineering-Maschinenbau-%28wmd%29-2027-38231/1410645533/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Duales Studium Digital Engineering Maschinenbau (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Duales-Studium-Digital-Engineering-Maschinenbau-%28wmd%29-2027-38231/1410645533/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,coding. DE B2. Docs: '
                        'CV; all latest-report pages and all school qualifications; '
                        'foreign certificates need certified German translations. '
                        '€1401.50/month;35h/week;22 leave days; possible hire after '
                        'completion. Year conflict:2027 title/2026 start; confirm. '
                        'Pension, vehicle purchase/leasing conditions, bicycle '
                        'leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Salzgitter, DE, 38231',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Duales-Studium-Digital-Engineering-Maschinenbau-%28wmd%29-2027-38231/1410645533/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27146',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Informations-und-Systemtechnik-%28wmd%29-2027-38231/1410620633/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Elektronikerin / Elektroniker für Informations- und '
                      'Systemtechnik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Informations-und-Systemtechnik-%28wmd%29-2027-38231/1410620633/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in computing, '
                        'English/physics. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. >€1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Salzgitter, DE, 38231',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Informations-und-Systemtechnik-%28wmd%29-2027-38231/1410620633/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27752',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Duales-Studium-Batterie-und-Wasserstofftechnologie-%28wmd%29-2027-38231/1410627233/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Duales Studium Batterie- und Wasserstofftechnologie (w/m/d) '
                      '2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Duales-Studium-Batterie-und-Wasserstofftechnologie-%28wmd%29-2027-38231/1410627233/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,renewable energy. DE '
                        'B2. Docs: CV; all latest-report pages and all school '
                        'qualifications; foreign certificates need certified German '
                        'translations. €1401.50/month;35h/week;22 leave days; possible '
                        'hire after completion. Year conflict:2027 title/2026 start; '
                        'confirm. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Salzgitter, DE, 38231',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Duales-Studium-Batterie-und-Wasserstofftechnologie-%28wmd%29-2027-38231/1410627233/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27143',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-38231/1410619933/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Elektronikerin / Elektroniker für '
                      'Automatisierungstechnik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-38231/1410619933/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in computing, '
                        'English/physics. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. >€1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Salzgitter, DE, 38231',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-38231/1410619933/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '26977',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Kauffrau-Kaufmann-f%C3%BCr-Digitalisierungsmanagement-%28wmd%29-2027-38436/1401791333/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Kauffrau / Kaufmann für Digitalisierungsmanagement '
                      '(w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Kauffrau-Kaufmann-f%C3%BCr-Digitalisierungsmanagement-%28wmd%29-2027-38436/1401791333/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education; Realschule recommended. Interest '
                        'in computing, maths/English. DE B2. Docs: CV; all '
                        'latest-report pages and all school qualifications; foreign '
                        'certificates need certified German translations. '
                        '>€1327/month;35h/week;30 leave days; possible hire after '
                        'completion. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Kauffrau-Kaufmann-f%C3%BCr-Digitalisierungsmanagement-%28wmd%29-2027-38436/1401791333/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '26986',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Kauffrau-Kaufmann-f%C3%BCr-B%C3%BCromanagement-%28wmd%29-2027-38436/1401809933/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Kauffrau / Kaufmann für Büromanagement (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Kauffrau-Kaufmann-f%C3%BCr-B%C3%BCromanagement-%28wmd%29-2027-38436/1401809933/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education; Realschule recommended. Interest '
                        'in German, English/maths. DE B2. Docs: CV; all latest-report '
                        'pages and all school qualifications; foreign certificates '
                        'need certified German translations. >€1327/month;35h/week;30 '
                        'leave days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Kauffrau-Kaufmann-f%C3%BCr-B%C3%BCromanagement-%28wmd%29-2027-38436/1401809933/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27050',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Informations-und-Systemtechnik-%28wmd%29-2027-38436/1411191933/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Elektronikerin / Elektroniker für Informations- und '
                      'Systemtechnik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Informations-und-Systemtechnik-%28wmd%29-2027-38436/1411191933/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in computing, '
                        'English/physics. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. >€1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Informations-und-Systemtechnik-%28wmd%29-2027-38436/1411191933/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27048',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-38436/1411195433/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Elektronikerin / Elektroniker für '
                      'Automatisierungstechnik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-38436/1411195433/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in computing, '
                        'English/physics. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. >€1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-38436/1411195433/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27179',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-38037/1411109133/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Elektronikerin / Elektroniker für '
                      'Automatisierungstechnik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-38037/1411109133/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in computing, '
                        'English/physics. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. >€1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Braunschweig, DE, 38037',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-38037/1411109133/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27898',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Duales-Studium-Elektro-und-Informationstechnik-%28wmd%29-2027-38037/1411121033/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Duales Studium Elektro- und Informationstechnik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Duales-Studium-Elektro-und-Informationstechnik-%28wmd%29-2027-38037/1411121033/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,technical drawing. DE '
                        'B2. Docs: CV; all latest-report pages and all school '
                        'qualifications; foreign certificates need certified German '
                        'translations. >€1401.50/month;35h/week;22 leave days; '
                        'possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Braunschweig, DE, 38037',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Duales-Studium-Elektro-und-Informationstechnik-%28wmd%29-2027-38037/1411121033/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27404',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Informations-und-Systemtechnik-%28wmd%29-2027-38037/1405555233/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Elektronikerin / Elektroniker für Informations- und '
                      'Systemtechnik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Informations-und-Systemtechnik-%28wmd%29-2027-38037/1405555233/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in computing, '
                        'English/physics. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. >€1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Braunschweig, DE, 38037',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Informations-und-Systemtechnik-%28wmd%29-2027-38037/1405555233/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '28013',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-38037/1410616833/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Werkfeuerwehrfrau / Werkfeuerwehrmann (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-38037/1410616833/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education; age ≥16½ at start; driving '
                        'licence within18 months after training starts. Interest in '
                        'sports, technology/German. DE B2. Docs: CV; all latest-report '
                        'pages and all school qualifications; foreign certificates '
                        'need certified German translations. >€1327/month;35h/week;30 '
                        'leave days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Braunschweig, DE, 38037',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-38037/1410616833/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27186',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Fachkraft-f%C3%BCr-Lagerlogistik-%28wmd%29-2027-38037/1411109833/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Fachkraft für Lagerlogistik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Fachkraft-f%C3%BCr-Lagerlogistik-%28wmd%29-2027-38037/1411109833/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in German, '
                        'maths/English. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. >€1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Braunschweig, DE, 38037',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Fachkraft-f%C3%BCr-Lagerlogistik-%28wmd%29-2027-38037/1411109833/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27912',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Duales-Studium-Fahrzeuginformatik-%28wmd%29-2027-38037/1411122233/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Duales Studium Fahrzeuginformatik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Duales-Studium-Fahrzeuginformatik-%28wmd%29-2027-38037/1411122233/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,coding. DE B2. Docs: '
                        'CV; all latest-report pages and all school qualifications; '
                        'foreign certificates need certified German translations. '
                        '€1401.50/month;35h/week;22 leave days; possible hire after '
                        'completion. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Braunschweig, DE, 38037',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Duales-Studium-Fahrzeuginformatik-%28wmd%29-2027-38037/1411122233/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27758',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Duales-Studium-Digital-Engineering-Maschinenbau-%28wmd%29-2027-38037/1411118833/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Duales Studium Digital Engineering Maschinenbau (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Duales-Studium-Digital-Engineering-Maschinenbau-%28wmd%29-2027-38037/1411118833/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,coding. DE B2. Docs: '
                        'CV; all latest-report pages and all school qualifications; '
                        'foreign certificates need certified German translations. '
                        '€1401.50/month;35h/week;22 leave days; possible hire after '
                        'completion. Year conflict:2027 title/2026 start; confirm. '
                        'Pension, vehicle purchase/leasing conditions, bicycle '
                        'leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Braunschweig, DE, 38037',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Duales-Studium-Digital-Engineering-Maschinenbau-%28wmd%29-2027-38037/1411118833/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27759',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Duales-Studium-Mechatronik-%28wmd%29-2027-38037/1411119333/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Duales Studium Mechatronik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Duales-Studium-Mechatronik-%28wmd%29-2027-38037/1411119333/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,technical drawing. DE '
                        'B2. Docs: CV; all latest-report pages and all school '
                        'qualifications; foreign certificates need certified German '
                        'translations. €1401.50/month;35h/week;22 leave days; possible '
                        'hire after completion. Year conflict:2027 title/2026 start; '
                        'confirm. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Braunschweig, DE, 38037',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Duales-Studium-Mechatronik-%28wmd%29-2027-38037/1411119333/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27192',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-38037/1410102533/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Industriemechanikerin / Industriemechaniker (w/m/d) '
                      '2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-38037/1410102533/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in maths, '
                        'physics/German. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. >€1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Braunschweig, DE, 38037',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-38037/1410102533/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27197',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Zerspanungsmechanikerin-Zerspanungsmechaniker-%28wmd%29-2027-38037/1405554733/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Zerspanungsmechanikerin / Zerspanungsmechaniker '
                      '(w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Zerspanungsmechanikerin-Zerspanungsmechaniker-%28wmd%29-2027-38037/1405554733/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in maths, '
                        'physics/German. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. >€1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Braunschweig, DE, 38037',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Braunschweig-Ausbildung-Zerspanungsmechanikerin-Zerspanungsmechaniker-%28wmd%29-2027-38037/1405554733/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27362',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-34219/1405077633/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Werkfeuerwehrfrau / Werkfeuerwehrmann (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-34219/1405077633/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education; age ≥16½ at start; licence B '
                        'within18 months after training starts. Interest in sports, '
                        'technology/German. DE B2. Docs: CV; all latest-report pages '
                        'and all school qualifications; foreign certificates need '
                        'certified German translations. €1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Kassel, DE, 34219',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Werkfeuerwehrfrau-Werkfeuerwehrmann-%28wmd%29-2027-34219/1405077633/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27373',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Maschinenbau-%28wmd%29-2027-34219/1405120333/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Duales Studium Maschinenbau (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Maschinenbau-%28wmd%29-2027-34219/1405120333/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,technical drawing. DE '
                        'B2. Docs: CV; all latest-report pages and all school '
                        'qualifications; foreign certificates need certified German '
                        'translations. €1401.50/month;35h/week;22 leave days; possible '
                        'hire after completion. Pension, vehicle purchase/leasing '
                        'conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Kassel, DE, 34219',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Maschinenbau-%28wmd%29-2027-34219/1405120333/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27411',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-34219/1405439533/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Industriemechanikerin / Industriemechaniker (w/m/d) '
                      '2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-34219/1405439533/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in maths, '
                        'physics/German. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. €1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Kassel, DE, 34219',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Industriemechanikerin-Industriemechaniker-%28wmd%29-2027-34219/1405439533/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27377',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Wirtschaftsingenieurwesen-Schwerpunkt-Elektrotechnik-%28wmd%29-2027-34219/1405129433/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Duales Studium Wirtschaftsingenieurwesen Schwerpunkt '
                      'Elektrotechnik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Wirtschaftsingenieurwesen-Schwerpunkt-Elektrotechnik-%28wmd%29-2027-34219/1405129433/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,technical drawing. DE '
                        'B2. Docs: CV; all latest-report pages and all school '
                        'qualifications; foreign certificates need certified German '
                        'translations. €1401.50/month;35h/week;22 leave days; possible '
                        'hire after completion. Pension, vehicle purchase/leasing '
                        'conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Kassel, DE, 34219',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Wirtschaftsingenieurwesen-Schwerpunkt-Elektrotechnik-%28wmd%29-2027-34219/1405129433/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27400',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Gie%C3%9Fereimechanikerin-Gie%C3%9Fereimechaniker-f%C3%BCr-Druck-und-Kokillenguss-%28wmd%29-2027-34219/1405395933/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Gießereimechanikerin / Gießereimechaniker für Druck- '
                      'und Kokillenguss (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Gie%C3%9Fereimechanikerin-Gie%C3%9Fereimechaniker-f%C3%BCr-Druck-und-Kokillenguss-%28wmd%29-2027-34219/1405395933/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in maths, '
                        'physics/German. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. €1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Kassel, DE, 34219',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Gie%C3%9Fereimechanikerin-Gie%C3%9Fereimechaniker-f%C3%BCr-Druck-und-Kokillenguss-%28wmd%29-2027-34219/1405395933/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27340',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Fachkraft-f%C3%BCr-Lagerlogistik-%28wmd%29-2027-34219/1404986633/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Fachkraft für Lagerlogistik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Fachkraft-f%C3%BCr-Lagerlogistik-%28wmd%29-2027-34219/1404986633/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in German, '
                        'maths/English. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. €1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Kassel, DE, 34219',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Fachkraft-f%C3%BCr-Lagerlogistik-%28wmd%29-2027-34219/1404986633/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27355',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-34219/1405053833/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Elektronikerin / Elektroniker für '
                      'Automatisierungstechnik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-34219/1405053833/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in computing, '
                        'English/physics. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. €1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Kassel, DE, 34219',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-34219/1405053833/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27359',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Fachkraft-f%C3%BCr-Systemgastronomie-%28wmd%29-2027-34219/1405067033/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Fachkraft für Systemgastronomie (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Fachkraft-f%C3%BCr-Systemgastronomie-%28wmd%29-2027-34219/1405067033/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in German, '
                        'maths/business. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. €1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Kassel, DE, 34219',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Fachkraft-f%C3%BCr-Systemgastronomie-%28wmd%29-2027-34219/1405067033/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27360',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Mechatronikerin-Mechatroniker-%28wmd%29-2027-34219/1405072933/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Mechatronikerin / Mechatroniker (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Mechatronikerin-Mechatroniker-%28wmd%29-2027-34219/1405072933/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in technology/manual '
                        'work. DE B2. Docs: CV; all latest-report pages and all school '
                        'qualifications; foreign certificates need certified German '
                        'translations. €1327/month;35h/week;30 leave days; possible '
                        'hire after completion. Pension, vehicle purchase/leasing '
                        'conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Kassel, DE, 34219',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Mechatronikerin-Mechatroniker-%28wmd%29-2027-34219/1405072933/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27365',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Elektrotechnik-%28wmd%29-2027-34219/1405084733/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Duales Studium Elektrotechnik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Elektrotechnik-%28wmd%29-2027-34219/1405084733/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,technical drawing. DE '
                        'B2. Docs: CV; all latest-report pages and all school '
                        'qualifications; foreign certificates need certified German '
                        'translations. €1401.50/month;35h/week;22 leave days; possible '
                        'hire after completion. Pension, vehicle purchase/leasing '
                        'conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Kassel, DE, 34219',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Elektrotechnik-%28wmd%29-2027-34219/1405084733/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27407',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Werkstoffpr%C3%BCferin-Werkstoffpr%C3%BCfer-%28wmd%29-2027-34219/1405421433/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Werkstoffprüferin / Werkstoffprüfer (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Werkstoffpr%C3%BCferin-Werkstoffpr%C3%BCfer-%28wmd%29-2027-34219/1405421433/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in technology, '
                        'physics/maths. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. €1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Kassel, DE, 34219',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-Werkstoffpr%C3%BCferin-Werkstoffpr%C3%BCfer-%28wmd%29-2027-34219/1405421433/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27380',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Wirtschaftsingenieurwesen-Schwerpunkt-Maschinenbau-%28wmd%29-2027-34219/1405136533/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Duales Studium Wirtschaftsingenieurwesen Schwerpunkt '
                      'Maschinenbau (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Wirtschaftsingenieurwesen-Schwerpunkt-Maschinenbau-%28wmd%29-2027-34219/1405136533/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,technical drawing. DE '
                        'B2. Docs: CV; all latest-report pages and all school '
                        'qualifications; foreign certificates need certified German '
                        'translations. €1401.50/month;35h/week;22 leave days; possible '
                        'hire after completion. Pension, vehicle purchase/leasing '
                        'conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Kassel, DE, 34219',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Wirtschaftsingenieurwesen-Schwerpunkt-Maschinenbau-%28wmd%29-2027-34219/1405136533/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27374',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Maschinenbau-Schwerpunkt-Gie%C3%9Fereitechnik-%28wmd%29-2027-34219/1405124733/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Duales Studium Maschinenbau Schwerpunkt Gießereitechnik (w/m/d) '
                      '2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Maschinenbau-Schwerpunkt-Gie%C3%9Fereitechnik-%28wmd%29-2027-34219/1405124733/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology. DE B2. Docs: CV; all '
                        'latest-report pages and all school qualifications; foreign '
                        'certificates need certified German translations. '
                        '€1401.50/month;35h/week;22 leave days; possible hire after '
                        'completion. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Kassel, DE, 34219',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Maschinenbau-Schwerpunkt-Gie%C3%9Fereitechnik-%28wmd%29-2027-34219/1405124733/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27353',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-IT-Systemelektronikerin-IT-Systemelektroniker-%28wmd%29-2027-34219/1405034433/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung IT-Systemelektronikerin / IT-Systemelektroniker '
                      '(w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-IT-Systemelektronikerin-IT-Systemelektroniker-%28wmd%29-2027-34219/1405034433/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in electrical '
                        'technology. DE B2. Docs: CV; all latest-report pages and all '
                        'school qualifications; foreign certificates need certified '
                        'German translations. €1327/month;35h/week;30 leave days; '
                        'possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Kassel, DE, 34219',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Ausbildung-IT-Systemelektronikerin-IT-Systemelektroniker-%28wmd%29-2027-34219/1405034433/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27762',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Mediengestalterin-Mediengestalter-Digital-und-Print-%28wmd%29-2027-38436/1407959133/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Mediengestalterin / Mediengestalter Digital und '
                      'Print (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Mediengestalterin-Mediengestalter-Digital-und-Print-%28wmd%29-2027-38436/1407959133/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education; Realschule recommended. Interest '
                        'in modern technology,design/digital media. DE B2. Docs: CV; '
                        'all latest-report pages and all school qualifications; '
                        'foreign certificates need certified German translations; 5–10 '
                        'annotated work samples in onePDF; explain team-project role. '
                        '>€1327/month;35h/week;30 leave days; possible hire after '
                        'completion. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Mediengestalterin-Mediengestalter-Digital-und-Print-%28wmd%29-2027-38436/1407959133/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '26984',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Industriekauffrau-Industriekaufmann-%28wmd%29-2027-38436/1401796233/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Industriekauffrau / Industriekaufmann (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Industriekauffrau-Industriekaufmann-%28wmd%29-2027-38436/1401796233/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education; Realschule or (Fach-)Abitur '
                        'recommended. Interest in business, maths/English. DE B2. '
                        'Docs: CV; all latest-report pages and all school '
                        'qualifications; foreign certificates need certified German '
                        'translations. >€1327/month;35h/week;30 leave days; possible '
                        'hire after completion. Pension, vehicle purchase/leasing '
                        'conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildung-Industriekauffrau-Industriekaufmann-%28wmd%29-2027-38436/1401796233/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '27056',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-30419/1411198733/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-28',
  'fields': {'title': 'Ausbildung Elektronikerin / Elektroniker für '
                      'Automatisierungstechnik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-30419/1411198733/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in computing, '
                        'English/physics. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. €1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Elektronikerin-Elektroniker-f%C3%BCr-Automatisierungstechnik-%28wmd%29-2027-30419/1411198733/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '28887',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Praktikum-Hardware-Test-Elektroantriebe-%28wmd%29-34225/1425947733/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-25',
  'fields': {'title': 'Praktikum Hardware-Test Elektroantriebe (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Praktikum-Hardware-Test-Elektroantriebe-%28wmd%29-34225/1425947733/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Electronics/IT/electrical/communication-engineering or '
                        'related students or Gap Year; good–very good grades. MATLAB, '
                        'good coding, hardware/lab testing and basic ECAD; '
                        'Git/power-electronics/PLECS/LTSpice advantageous. DE B2,EN '
                        'B2; licence B required. Docs: CV; enrolment proof except Gap '
                        'Year; current transcript; university certificate for '
                        'mandatory internship. Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Baunatal, DE, 34225',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Praktikum-Hardware-Test-Elektroantriebe-%28wmd%29-34225/1425947733/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '24196',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Global-Assignments-Steuern-und-Sozialversicherung-%28wmd%29-38436/1382060033/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-25',
  'fields': {'title': 'Praktikum Global Assignments Steuern und Sozialversicherung '
                      '(w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Global-Assignments-Steuern-und-Sozialversicherung-%28wmd%29-38436/1382060033/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Bachelor ≥90 CP or Master in '
                        'economics/law/humanities/social-science, '
                        'general-business/HR/tax focus, or related field; good–very '
                        'good grades. Office; SAP preferred. DE+EN B2. Docs: CV; cover '
                        'letter; enrolment proof; current transcript; university '
                        'certificate for mandatory internship; work permit for non-EU '
                        'applicants. Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Global-Assignments-Steuern-und-Sozialversicherung-%28wmd%29-38436/1382060033/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '28876',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Doktorandin-Doktorand-KI-Systeme-Prozessplanung-Automobilproduktion-%28wmd%29-38436/1441334833/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-25',
  'fields': {'title': 'Doktorandin / Doktorand KI-Systeme Prozessplanung '
                      'Automobilproduktion (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Doktorandin-Doktorand-KI-Systeme-Prozessplanung-Automobilproduktion-%28wmd%29-38436/1441334833/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Good–very good doctoral-qualifying CS degree focused on AI. '
                        'GenAI/agentic-AI/LLM/LangChain/DeepAgents expertise; very '
                        'good Python and RAG/embeddings/vector-DB/prompt/system-design '
                        'knowledge. Docs: CV; transcript or Master/Diplom certificate. '
                        '35h; employment years1/2/3: €3030/3194/3497 gross/month +€167 '
                        'allowance;49% variable-bonus participation. 30 leave '
                        'days+Dec24/31 off; flex/mobile work, doctoral '
                        'seminars/college; vehicle conditions.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'jobs',
             'categories': ['jobs', 'training'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Doktorandin-Doktorand-KI-Systeme-Prozessplanung-Automobilproduktion-%28wmd%29-38436/1441334833/',
                                             'Actual publisher Karrierelevel: '
                                             'Doktoranden']}}},
 {'identity': '28503',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Konzernbeschaffung-Produktanl%C3%A4ufe-MEB-%28wmd%29-38436/1441415633/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-25',
  'fields': {'title': 'Praktikum Konzernbeschaffung Produktanläufe MEB (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Konzernbeschaffung-Produktanl%C3%A4ufe-MEB-%28wmd%29-38436/1441415633/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Economics/engineering or related students or Gap Year; third '
                        'Bachelor semester completed ≥90 CP or Master; good–very good '
                        'grades. Good Office. DE+EN B2. Docs: CV; enrolment proof '
                        'except Gap Year; current transcript; university certificate '
                        'for mandatory internship. Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Konzernbeschaffung-Produktanl%C3%A4ufe-MEB-%28wmd%29-38436/1441415633/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '30643',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-Projektmanagement-Filmstudio-%28wmd%29-30419/1441374733/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-25',
  'fields': {'title': 'Werkstudentin / Werkstudent Projektmanagement Filmstudio '
                      '(w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-Projektmanagement-Filmstudio-%28wmd%29-30419/1441374733/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Bachelor ≥89 CP or Master in '
                        'CS/business/industrial-engineering or related field; '
                        'good–very good grades. Initial camera/photo/video-editing '
                        'experience and Office. DE C1. Docs: CV; enrolment proof; '
                        'current transcript; work permit for non-EU applicants. €18.33 '
                        'gross/hour.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': None,
             'language': 'de',
             'category': 'jobs',
             'categories': ['jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-Projektmanagement-Filmstudio-%28wmd%29-30419/1441374733/',
                                             'Actual publisher Karrierelevel: '
                                             'Studierende']}}},
 {'identity': '30718',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-Finanzsteuerung-%28wmd%29-30419/1441366633/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-25',
  'fields': {'title': 'Werkstudentin / Werkstudent Finanzsteuerung (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-Finanzsteuerung-%28wmd%29-30419/1441366633/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Bachelor ≥89 CP or Master in business or related field; '
                        'good–very good grades. Business/finance understanding and '
                        'Office. DE C1. Docs: CV; enrolment proof; current transcript; '
                        'work permit for non-EU applicants. €18.33 gross/hour.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': None,
             'language': 'de',
             'category': 'jobs',
             'categories': ['jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-Finanzsteuerung-%28wmd%29-30419/1441366633/',
                                             'Actual publisher Karrierelevel: '
                                             'Studierende']}}},
 {'identity': '29816',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Vertriebscontrolling-%28wmd%29-38436/1430065033/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-24',
  'fields': {'title': 'Praktikum Vertriebscontrolling (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Vertriebscontrolling-%28wmd%29-38436/1430065033/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Economics/data-science/CS or related students or Gap Year; '
                        'good–very good grades. Data modelling/visualization and '
                        'BI/Microsoft experience; DAX/Python/SQL basics preferred. DE '
                        'C1. Docs: CV; enrolment proof; current transcript; university '
                        'certificate for mandatory internship. Current statutory '
                        'minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Vertriebscontrolling-%28wmd%29-38436/1430065033/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '26566',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Qualit%C3%A4tssicherung-Analysezentrum-Gesamtfahrzeug-Elektrik-%28wmd%29-38436/1403184933/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-24',
  'fields': {'title': 'Praktikum Qualitätssicherung Analysezentrum Gesamtfahrzeug '
                      'Elektrik (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Qualit%C3%A4tssicherung-Analysezentrum-Gesamtfahrzeug-Elektrik-%28wmd%29-38436/1403184933/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'CS/vehicle-informatics/digital-engineering or related '
                        'students or Gap Year; good–very good grades. Basic coding; '
                        'Python/databases preferred, measurement/embedded experience '
                        'ideal. DE B2,EN B1. Docs: CV; enrolment proof; current '
                        'transcript; university certificate for mandatory internship; '
                        'work+residence permits if applicable. Current statutory '
                        'minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Qualit%C3%A4tssicherung-Analysezentrum-Gesamtfahrzeug-Elektrik-%28wmd%29-38436/1403184933/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '29253',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Softwareentwicklung-Messdatenanalyse-%28wmd%29-38436/1429942733/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-24',
  'fields': {'title': 'Praktikum Softwareentwicklung Messdatenanalyse (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Softwareentwicklung-Messdatenanalyse-%28wmd%29-38436/1429942733/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'CS/software/electrical engineering/mathematics or related '
                        'students or Gap Year; good–very good grades. Container '
                        'knowledge and good–very good Python; AI/software-quality '
                        'interest. EN B1,DE C1. Docs: CV; enrolment proof except Gap '
                        'Year; current transcript; university certificate for '
                        'mandatory internship; work+residence permits and '
                        'supplementary sheet if applicable for non-EU applicants. '
                        'Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Softwareentwicklung-Messdatenanalyse-%28wmd%29-38436/1429942733/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '30476',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Qualit%C3%A4tssicherung-%28wmd%29-38436/1440786033/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-24',
  'fields': {'title': 'Praktikum Qualitätssicherung (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Qualit%C3%A4tssicherung-%28wmd%29-38436/1440786033/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Technical/business students. Good–very good grades. DE+EN C1. '
                        'Docs: CV; cover letter; enrolment proof except Gap Year; '
                        'current transcript; university certificate for mandatory '
                        'internship; work permit for non-EU applicants. Current '
                        'statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Qualit%C3%A4tssicherung-%28wmd%29-38436/1440786033/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '30623',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Produktmanagement-ID_-Buzz-%28wmd%29-30419/1441033733/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-24',
  'fields': {'title': 'Praktikum Produktmanagement ID. Buzz (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Produktmanagement-ID_-Buzz-%28wmd%29-30419/1441033733/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Technical/business or related students or Gap Year; good–very '
                        'good grades. Technical understanding, Office and good–very '
                        'good presentation skills; automotive experience ideal. DE '
                        'C1,EN B2. Docs: CV; enrolment proof except Gap Year; current '
                        'transcript; university certificate for mandatory internship; '
                        'work permit for non-EU applicants. Current statutory minimum '
                        'wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Produktmanagement-ID_-Buzz-%28wmd%29-30419/1441033733/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '30385',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-Camper-Experience-California-App-%28wmd%29-30419/1441022933/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-24',
  'fields': {'title': 'Werkstudentin / Werkstudent Camper Experience - California App '
                      '(w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-Camper-Experience-California-App-%28wmd%29-30419/1441022933/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Bachelor ≥89 CP or Master in '
                        'CS/business/psychology/UX-research or related field; '
                        'good–very good grades. Digital/customer-product interest and '
                        'PowerPoint/Excel. DE B2. Docs: CV; enrolment proof; current '
                        'transcript; work permit for non-EU applicants. €18.33 '
                        'gross/hour.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': None,
             'language': 'de',
             'category': 'jobs',
             'categories': ['jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-Camper-Experience-California-App-%28wmd%29-30419/1441022933/',
                                             'Actual publisher Karrierelevel: '
                                             'Studierende']}}},
 {'identity': '30616',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-E-Commerce-Management-%28mwd%29-30419/1441028633/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-24',
  'fields': {'title': 'Werkstudentin / Werkstudent E-Commerce Management (m/w/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-E-Commerce-Management-%28mwd%29-30419/1441028633/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Bachelor ≥89 CP or Master in education/business/CS or related '
                        'field; good–very good grades. Very good Office and '
                        'e-commerce/digital-sales understanding. DE B2. Docs: CV; '
                        'enrolment proof; current transcript; work permit for non-EU '
                        'applicants. €18.33 gross/hour.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': None,
             'language': 'de',
             'category': 'jobs',
             'categories': ['jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-E-Commerce-Management-%28mwd%29-30419/1441028633/',
                                             'Actual publisher Karrierelevel: '
                                             'Studierende']}}},
 {'identity': '30727',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Beschaffung-Interieur-%28wmd%29-30419/1441031833/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-24',
  'fields': {'title': 'Praktikum Beschaffung Interieur (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Beschaffung-Interieur-%28wmd%29-30419/1441031833/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Business/industrial-engineering or related students or Gap '
                        'Year; good–very good grades. Excel/PowerPoint; '
                        'procurement/supply-chain/project experience advantageous. DE '
                        'C1. Docs: CV; enrolment proof except Gap Year; current '
                        'transcript; university certificate for mandatory internship; '
                        'work permit for non-EU applicants. Current statutory minimum '
                        'wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Praktikum-Beschaffung-Interieur-%28wmd%29-30419/1441031833/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '30701',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-International-Used-Car-Strategy-%28wmd%29-38436/1440469833/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-23',
  'fields': {'title': 'Praktikum International Used Car Strategy (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-International-Used-Car-Strategy-%28wmd%29-38436/1440469833/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Business/social/engineering/information-science or related '
                        'students or Gap Year. Office/strategy-transformation '
                        'interest; consulting/strategy/sales/automotive/project '
                        'experience ideal. DE+EN C1. Docs: CV; enrolment proof; '
                        'current transcript; university certificate for mandatory '
                        'internship; work+residence permits and supplementary sheet if '
                        'applicable. Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-International-Used-Car-Strategy-%28wmd%29-38436/1440469833/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '29739',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Doktorandin-Doktorand-Leistungselektronik-%28wmd%29-38436/1440148933/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-22',
  'fields': {'title': 'Doktorandin / Doktorand Leistungselektronik (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Doktorandin-Doktorand-Leistungselektronik-%28wmd%29-38436/1440148933/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Good–very good doctoral-qualifying electrical-engineering or '
                        'related degree. Power electronics/drives, circuit simulation '
                        'and PCBA-layout tools. Docs: CV; transcript or Master/Diplom '
                        'certificate. 35h; employment years1/2/3: €3030/3194/3497 '
                        'gross/month +€167 allowance;49% variable-bonus participation. '
                        '30 leave days+Dec24/31 off; flex/mobile work, doctoral '
                        'seminars/college; vehicle conditions.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'jobs',
             'categories': ['jobs', 'training'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Doktorandin-Doktorand-Leistungselektronik-%28wmd%29-38436/1440148933/',
                                             'Actual publisher Karrierelevel: '
                                             'Doktoranden']}}},
 {'identity': '30216',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Abschlussarbeit-W%C3%A4rmeleitung-%28wmd%29-38436/1439987633/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-22',
  'fields': {'title': 'Praktikum / Abschlussarbeit Wärmeleitung (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Abschlussarbeit-W%C3%A4rmeleitung-%28wmd%29-38436/1439987633/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Materials/physics/mathematics/mechanical-engineering or '
                        'related students; good–very good grades. Transport-process '
                        'modelling/programming experience, e.g. COMSOL/MATLAB, '
                        'preferred. DE+EN B2. Docs: CV; enrolment proof; current '
                        'transcript; university certificate for mandatory internship; '
                        'work permit for non-EU applicants. Current statutory minimum '
                        'wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Abschlussarbeit-W%C3%A4rmeleitung-%28wmd%29-38436/1439987633/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '30493',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Praktikum-Fachoberschule-kaufm%C3%A4nnisch-%28wmd%29-34225/1439332933/',
  'application_window': {'opens': '2026-09-21',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-21',
  'fields': {'title': 'Praktikum Fachoberschule kaufmännisch (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Praktikum-Fachoberschule-kaufm%C3%A4nnisch-%28wmd%29-34225/1439332933/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Registered Fachoberschule pupil; good–very good school '
                        'grades. Good Excel/PowerPoint, independent work and teamwork. '
                        'DE B1. Docs: CV; brief cover letter specifying intended '
                        'discipline; last3 school reports; work permit for non-EU '
                        'applicants. Tariff leave (count unstated)+Dec24/31 off; '
                        'education/advice and bicycle leasing.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Baunatal, DE, 34225',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Praktikum-Fachoberschule-kaufm%C3%A4nnisch-%28wmd%29-34225/1439332933/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '30462',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Praktikum-Fachoberschule-technisch-%28wmd%29-34225/1439333633/',
  'application_window': {'opens': '2026-09-21',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-21',
  'fields': {'title': 'Praktikum Fachoberschule technisch (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Praktikum-Fachoberschule-technisch-%28wmd%29-34225/1439333633/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Registered Fachoberschule pupil; good–very good school '
                        'grades. Good Excel/PowerPoint, independent work and teamwork. '
                        'DE B1. Tariff leave (count unstated)+Dec24/31 off; '
                        'education/advice and bicycle leasing.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Baunatal, DE, 34225',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Baunatal-Praktikum-Fachoberschule-technisch-%28wmd%29-34225/1439333633/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '25218',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-%28Master%29-VW-Kraftwerk-GmbH-%28wmd%29-38436/1388142733/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-20',
  'fields': {'title': 'Werkstudentin / Werkstudent (Master) VW Kraftwerk GmbH (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-%28Master%29-VW-Kraftwerk-GmbH-%28wmd%29-38436/1388142733/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Master students in architecture/civil-engineering or related '
                        'field; good–very good grades. Office/CAD skills. Docs: CV; '
                        'enrolment proof; current transcript; work permit for non-EU '
                        'applicants. €18.33 gross/hour.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'jobs',
             'categories': ['jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-%28Master%29-VW-Kraftwerk-GmbH-%28wmd%29-38436/1388142733/',
                                             'Actual publisher Karrierelevel: '
                                             'Studierende']}}},
 {'identity': '25579',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Abschlussarbeit-Aerodynamik-%28wmd%29-38436/1391398833/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-19',
  'fields': {'title': 'Abschlussarbeit Aerodynamik (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Abschlussarbeit-Aerodynamik-%28wmd%29-38436/1391398833/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Engineering Master students or equivalent qualification; '
                        'good–very good grades. Strong coding, optical-flow '
                        'measurement, very good aerodynamics/testing and '
                        'measurement-data analysis. DE B2. Docs: CV; enrolment proof; '
                        'current transcript; work permit for non-EU applicants. '
                        'Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Abschlussarbeit-Aerodynamik-%28wmd%29-38436/1391398833/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '27571',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Consultant-Volkswagen-Group-Consulting-%28wmd%29-38436/1407454333/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-18',
  'fields': {'title': 'Consultant Volkswagen Group Consulting (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Consultant-Volkswagen-Group-Consulting-%28wmd%29-38436/1407454333/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Very good university Master degree; automotive and/or '
                        'consulting internships and practical intercultural '
                        'experience. Problem-solving plus digital/AI-solution '
                        'experience. DE C1,EN B2. Docs: motivation letter; academic '
                        'certificates with final grades/transcript; certificates of '
                        'prior practical experience/work. Possible €7136 gross/month '
                        'for35h. Performance-based bonus;30 leave days+Dec24/31 off,6 '
                        'extra after2 years; private-use car,pension,time-value '
                        'retirement account,flex/mobile '
                        'work,education/advice/health,vehicle conditions.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'jobs',
             'categories': ['jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Consultant-Volkswagen-Group-Consulting-%28wmd%29-38436/1407454333/',
                                             'Actual publisher Karrierelevel: '
                                             'Berufseinsteigende']}}},
 {'identity': '30228',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Produktivit%C3%A4tssteuerung-%28wmd%29-38440/1437929933/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-17',
  'fields': {'title': 'Praktikum Produktivitätssteuerung (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Produktivit%C3%A4tssteuerung-%28wmd%29-38440/1437929933/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Business/business-IT/digital-management/industrial-engineering '
                        'or related students or Gap Year; good–very good grades. '
                        'Digital applications, initial project/presentation experience '
                        'and basic data/ERP. DE B2. Docs: CV; enrolment proof except '
                        'Gap Year; current transcript; university certificate for '
                        'mandatory internship. Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38440',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Produktivit%C3%A4tssteuerung-%28wmd%29-38440/1437929933/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '30231',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Digitalisierung-Werklogistik-%28wmd%29-38436/1437940633/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-17',
  'fields': {'title': 'Praktikum Digitalisierung Werklogistik (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Digitalisierung-Werklogistik-%28wmd%29-38436/1437940633/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Business/industrial-engineering or related students or Gap '
                        'Year; good–very good grades. Digital tools, '
                        'project/presentation experience and basic data/ERP. DE B2,EN '
                        'B1. Docs: CV; enrolment proof except Gap Year; current '
                        'transcript; university certificate for mandatory internship. '
                        'Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Digitalisierung-Werklogistik-%28wmd%29-38436/1437940633/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '30318',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Montagelinie-1-&-4-%28wmd%29-38436/1437981133/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-17',
  'fields': {'title': 'Praktikum Montagelinie 1 & 4 (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Montagelinie-1-&-4-%28wmd%29-38436/1437981133/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Business-IT/CS/industrial-engineering/data-science/production-management '
                        'or related students or Gap Year; good–very good grades. '
                        'Excel/basic data-analysis; PowerBI/SQL/database experience '
                        'ideal. DE B2,EN B1. Docs: CV; enrolment proof except Gap '
                        'Year; current transcript; university certificate for '
                        'mandatory internship. Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Montagelinie-1-&-4-%28wmd%29-38436/1437981133/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '30383',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-Verkaufsplanung-Deutschland-%28wmd%29-30419/1437959833/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-17',
  'fields': {'title': 'Werkstudentin / Werkstudent Verkaufsplanung Deutschland (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-Verkaufsplanung-Deutschland-%28wmd%29-30419/1437959833/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Bachelor ≥89 CP or Master in business or related field; '
                        'good–very good grades. Business understanding, IT affinity '
                        'and PowerPoint/Excel. DE C1. Docs: CV; enrolment proof; '
                        'current transcript; work permit for non-EU applicants. €18.33 '
                        'gross/hour.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': None,
             'language': 'de',
             'category': 'jobs',
             'categories': ['jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Werkstudentin-Werkstudent-Verkaufsplanung-Deutschland-%28wmd%29-30419/1437959833/',
                                             'Actual publisher Karrierelevel: '
                                             'Studierende']}}},
 {'identity': '28785',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Abschlussarbeit-Customer-Data-Analytics-&-AI-%28wmd%29-38436/1427423533/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-16',
  'fields': {'title': 'Praktikum / Abschlussarbeit Customer Data Analytics & AI '
                      '(w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Abschlussarbeit-Customer-Data-Analytics-&-AI-%28wmd%29-38436/1427423533/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Mechanical/vehicle/electrical '
                        'engineering/data-science/CS/mathematics/physics or related '
                        'students or Gap Year; good–very good grades. Very good Python '
                        'and PyTorch/TensorFlow experience; ML advantageous, '
                        'BigData/Spark preferred. DE+EN B2. Docs: CV; enrolment proof '
                        'except Gap Year; current transcript; university certificate '
                        'for mandatory internship; work+residence permits and '
                        'supplementary sheet if applicable for non-EU applicants. '
                        'Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Abschlussarbeit-Customer-Data-Analytics-&-AI-%28wmd%29-38436/1427423533/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '22368',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Pilothallenverbund-%28wmd%29-38436/1427420833/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-16',
  'fields': {'title': 'Praktikum Pilothallenverbund (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Pilothallenverbund-%28wmd%29-38436/1427420833/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Mechanical/industrial/production engineering/business or '
                        'related students or Gap Year; good–very good grades. Very '
                        'good Office; technical/automotive affinity. EN B1. Docs: CV; '
                        'enrolment proof except Gap Year; current transcript; '
                        'university certificate for mandatory internship. Current '
                        'statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Pilothallenverbund-%28wmd%29-38436/1427420833/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '28543',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Visuelle-Kommunikation-%28wmd%29-38436/1427432733/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-16',
  'fields': {'title': 'Praktikum Visuelle Kommunikation (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Visuelle-Kommunikation-%28wmd%29-38436/1427432733/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Graphic/communication-design or related students or Gap Year; '
                        'good–very good grades. '
                        'Icon-design/Figma/vector/typography/media skills and basic '
                        'UX/branding; Adobe skills advantageous. DE C1,EN B1. Docs: '
                        'CV; enrolment proof except Gap Year; current transcript; '
                        'university certificate for mandatory internship; work permit '
                        'for non-EU applicants. Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'jobs',
             'categories': ['jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Visuelle-Kommunikation-%28wmd%29-38436/1427432733/',
                                             'Actual publisher Karrierelevel: '
                                             'Studierende']}}},
 {'identity': '29561',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Praktikum-Entwicklung-Batteriezelle-%28wmd%29-38231/1437461733/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-16',
  'fields': {'title': 'Praktikum Entwicklung Batteriezelle (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Praktikum-Entwicklung-Batteriezelle-%28wmd%29-38231/1437461733/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Engineering/science/environmental/industrial-engineering or '
                        'related students or Gap Year. Literature/data/knowledge-work '
                        'experience, Office/digital tools; Chinese/Spanish/Indonesian '
                        'preferred. DE C1,EN B2. Docs: CV; enrolment proof; current '
                        'transcript; university certificate for mandatory internship; '
                        'work+residence permits and supplementary sheet if applicable '
                        'for non-EU applicants. Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Salzgitter, DE, 38231',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Salzgitter-Praktikum-Entwicklung-Batteriezelle-%28wmd%29-38231/1437461733/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '30448',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-%28wmd%29-Werkstoffentwicklung-38436/1437580133/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-16',
  'fields': {'title': 'Werkstudentin / Werkstudent (w/m/d) Werkstoffentwicklung',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-%28wmd%29-Werkstoffentwicklung-38436/1437580133/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Bachelor ≥89 CP or Master in mechanical/materials/vehicle '
                        'engineering/business-IT/industrial-engineering or related '
                        'field; good–very good grades. Excel and rules/standards '
                        'understanding; IT affinity preferred. DE B2. Docs: CV; '
                        'enrolment proof; current transcript; work+residence permits '
                        'and supplementary sheet if applicable for non-EU applicants. '
                        '€18.33 gross/hour.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'jobs',
             'categories': ['jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Werkstudentin-Werkstudent-%28wmd%29-Werkstoffentwicklung-38436/1437580133/',
                                             'Actual publisher Karrierelevel: '
                                             'Studierende']}}},
 {'identity': '29834',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Montagelinie-Golf-%28wmd%29-38436/1437042733/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-15',
  'fields': {'title': 'Praktikum Montagelinie Golf (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Montagelinie-Golf-%28wmd%29-38436/1437042733/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Business-IT/CS/data-science/industrial-engineering/mathematics '
                        'or related students or Gap Year; good–very good grades. '
                        'Office and data/AI interest. DE B2,EN B1. Docs: CV; enrolment '
                        'proof except Gap Year; current transcript; university '
                        'certificate for mandatory internship. Current statutory '
                        'minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Montagelinie-Golf-%28wmd%29-38436/1437042733/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '7255',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Rechtsreferendariat-Integrit%C3%A4t-&-Recht-%28wmd%29-38436/1074619901/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-14',
  'fields': {'title': 'Rechtsreferendariat Integrität & Recht (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Rechtsreferendariat-Integrit%C3%A4t-&-Recht-%28wmd%29-38436/1074619901/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'First state law examination passed; understanding of '
                        'economic/technical matters. Independent work, teamwork and '
                        'communication. DE+EN B2.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Rechtsreferendariat-Integrit%C3%A4t-&-Recht-%28wmd%29-38436/1074619901/',
                                             'Actual publisher Karrierelevel: '
                                             'Referendare']}}},
 {'identity': '10163',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Visiting-Consultant-Volkswagen-Group-Consulting-%28wmd%29-38436/1129477601/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-14',
  'fields': {'title': 'Visiting Consultant Volkswagen Group Consulting (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Visiting-Consultant-Volkswagen-Group-Consulting-%28wmd%29-38436/1129477601/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Bachelor ≥90 CP or Master; very good grades; relevant '
                        'consulting/automotive/industrial experience. Mobility '
                        'interest. DE C1,EN B2. Docs: CV; cover letter; enrolment '
                        'proof; current transcript; university certificate for '
                        'mandatory internship; work permit for non-EU applicants. '
                        'Internship/thesis pay: current minimum wage; student-worker '
                        'pay: €18.33 gross/hour.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Visiting-Consultant-Volkswagen-Group-Consulting-%28wmd%29-38436/1129477601/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '29032',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Praktikum-Abschlussarbeit-CO2-Einsatz-als-Rohstoff-inkl_-Analytik-%28wmd%29/1426505833/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Praktikum / Abschlussarbeit CO2 Einsatz als Rohstoff inkl. '
                      'Analytik (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Praktikum-Abschlussarbeit-CO2-Einsatz-als-Rohstoff-inkl_-Analytik-%28wmd%29/1426505833/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Chemistry/electrochemistry/physics/chemical '
                        'engineering/plastics/polymer/materials or related students; '
                        'good–very good grades. Electrochemical-synthesis knowledge, '
                        'Office and electronic-data-analysis experience. Docs: CV; '
                        'enrolment proof; current transcript; university certificate '
                        'for mandatory internship; work permit for non-EU applicants. '
                        'Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': None,
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Praktikum-Abschlussarbeit-CO2-Einsatz-als-Rohstoff-inkl_-Analytik-%28wmd%29/1426505833/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '27701',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Smart-Vehicle-Systems-%28wmd%29-2027-38436/1415770133/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Duales Studium Smart Vehicle Systems (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Smart-Vehicle-Systems-%28wmd%29-2027-38436/1415770133/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,coding. DE B2. Docs: '
                        'CV; all latest-report pages and all school qualifications; '
                        'foreign certificates need certified German translations. '
                        '€1401.50/month;35h/week;22 leave days; possible hire after '
                        'completion. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Smart-Vehicle-Systems-%28wmd%29-2027-38436/1415770133/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '28303',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Fahrzeugtechnik-%28wmd%29-2027-38436/1415746333/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Duales Studium Fahrzeugtechnik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Fahrzeugtechnik-%28wmd%29-2027-38436/1415746333/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,technical drawing. DE '
                        'B2. Docs: CV; all latest-report pages and all school '
                        'qualifications; foreign certificates need certified German '
                        'translations. >€1401.50/month;35h/week;22 leave days; '
                        'possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Fahrzeugtechnik-%28wmd%29-2027-38436/1415746333/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27702',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Logistik-und-Informationsmanagement-%28wmd%29-2027-38436/1415763933/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Duales Studium Logistik und Informationsmanagement (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Logistik-und-Informationsmanagement-%28wmd%29-2027-38436/1415763933/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,logistics. DE B2. Docs: '
                        'CV; all latest-report pages and all school qualifications; '
                        'foreign certificates need certified German translations. '
                        '€1401.50/month;35h/week;22 leave days; possible hire after '
                        'completion. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Logistik-und-Informationsmanagement-%28wmd%29-2027-38436/1415763933/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27888',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Chemie-%28wmd%29-2027-38436/1415727933/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Duales Studium Chemie (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Chemie-%28wmd%29-2027-38436/1415727933/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good advanced-course '
                        'maths/chemistry grades. Interest in modern '
                        'technology,chemistry. DE B2. Docs: CV; all latest-report '
                        'pages and all school qualifications; foreign certificates '
                        'need certified German translations. '
                        '€1401.50/month;35h/week;22 leave days; possible hire after '
                        'completion. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Chemie-%28wmd%29-2027-38436/1415727933/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '28340',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildungsintegriertes-Duales-Studium-Digital-Engineering-Maschinenbau-%28wmd%29-2027-38436/1415714633/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Ausbildungsintegriertes Duales Studium Digital Engineering '
                      'Maschinenbau (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildungsintegriertes-Duales-Studium-Digital-Engineering-Maschinenbau-%28wmd%29-2027-38436/1415714633/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,coding. DE B2. Docs: '
                        'CV; all latest-report pages and all school qualifications; '
                        'foreign certificates need certified German translations. '
                        '>€1401.50/month;35h/week;22 leave days; possible hire after '
                        'completion. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildungsintegriertes-Duales-Studium-Digital-Engineering-Maschinenbau-%28wmd%29-2027-38436/1415714633/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27894',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Wirtschaftsingenieur-Logistik-%28wmd%29-2027-38436/1415778433/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Duales Studium Wirtschaftsingenieur Logistik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Wirtschaftsingenieur-Logistik-%28wmd%29-2027-38436/1415778433/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,logistics. DE B2. Docs: '
                        'CV; all latest-report pages and all school qualifications; '
                        'foreign certificates need certified German translations. '
                        '>€1401.50/month;35h/week;22 leave days; possible hire after '
                        'completion. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Wirtschaftsingenieur-Logistik-%28wmd%29-2027-38436/1415778433/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27741',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Ingenieurinformatik-%28wmd%29-2027-38436/1415753733/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Duales Studium Ingenieurinformatik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Ingenieurinformatik-%28wmd%29-2027-38436/1415753733/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in coding,IT security. DE B2. Docs: CV; all '
                        'latest-report pages and all school qualifications; foreign '
                        'certificates need certified German translations. '
                        '>€1401.50/month;35h/week;22 leave days; possible hire after '
                        'completion. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Ingenieurinformatik-%28wmd%29-2027-38436/1415753733/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27891',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Wirtschaftsinformatik-%28wmd%29-2027-38436/1415771433/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Duales Studium Wirtschaftsinformatik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Wirtschaftsinformatik-%28wmd%29-2027-38436/1415771433/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,coding. DE B2. Docs: '
                        'CV; all latest-report pages and all school qualifications; '
                        'foreign certificates need certified German translations. '
                        '>€1401.50/month;35h/week;22 leave days; possible hire after '
                        'completion. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Wirtschaftsinformatik-%28wmd%29-2027-38436/1415771433/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27736',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Bauingenieurwesen-%28wmd%29-2027-38436/1415728933/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Duales Studium Bauingenieurwesen (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Bauingenieurwesen-%28wmd%29-2027-38436/1415728933/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good advanced-course '
                        'maths/physics grades. Interest in modern '
                        'technology,civil-engineering technology. DE B2. Docs: CV; all '
                        'latest-report pages and all school qualifications; foreign '
                        'certificates need certified German translations. '
                        '>€1401.50/month;35h/week;22 leave days; possible hire after '
                        'completion. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Bauingenieurwesen-%28wmd%29-2027-38436/1415728933/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '28342',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildungsintegriertes-Duales-Studium-Wirtschaftsingenieurwesen-Maschinenbau-%28wmd%29-2027-38436/1415722433/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Ausbildungsintegriertes Duales Studium '
                      'Wirtschaftsingenieurwesen Maschinenbau (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildungsintegriertes-Duales-Studium-Wirtschaftsingenieurwesen-Maschinenbau-%28wmd%29-2027-38436/1415722433/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,technical drawing. DE '
                        'B2. Docs: CV; all latest-report pages and all school '
                        'qualifications; foreign certificates need certified German '
                        'translations. >€1401.50/month;35h/week;22 leave days; '
                        'possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildungsintegriertes-Duales-Studium-Wirtschaftsingenieurwesen-Maschinenbau-%28wmd%29-2027-38436/1415722433/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27080',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Internationales-Management-%28wmd%29-2027-38436/1415756733/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Duales Studium Internationales Management (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Internationales-Management-%28wmd%29-2027-38436/1415756733/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.4; good relevant advanced-course '
                        'grades. Interest in business processes. DE B2. Docs: CV; all '
                        'latest-report pages and all school qualifications; foreign '
                        'certificates need certified German translations. '
                        '€1401.50/month;35h/week;22 leave days; possible hire after '
                        'completion. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Internationales-Management-%28wmd%29-2027-38436/1415756733/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '28226',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Lehramt-Bildung-Beruf%2C-Fachrichtung-Wirtschaft-und-Verwaltung-%28wmd%29-2027-38436/1415762233/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Duales Studium Lehramt - Bildung - Beruf, Fachrichtung '
                      'Wirtschaft und Verwaltung (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Lehramt-Bildung-Beruf%2C-Fachrichtung-Wirtschaft-und-Verwaltung-%28wmd%29-2027-38436/1415762233/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in IT. DE B2. Docs: CV; all latest-report '
                        'pages and all school qualifications; foreign certificates '
                        'need certified German translations. '
                        '€1401.50/month;35h/week;22 leave days; possible hire after '
                        'completion. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Lehramt-Bildung-Beruf%2C-Fachrichtung-Wirtschaft-und-Verwaltung-%28wmd%29-2027-38436/1415762233/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27733',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Fahrzeuginformatik-%28wmd%29-2027-38436/1415744133/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Duales Studium Fahrzeuginformatik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Fahrzeuginformatik-%28wmd%29-2027-38436/1415744133/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,coding. DE B2. Docs: '
                        'CV; all latest-report pages and all school qualifications; '
                        'foreign certificates need certified German translations. '
                        '>€1401.50/month;35h/week;22 leave days; possible hire after '
                        'completion. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Fahrzeuginformatik-%28wmd%29-2027-38436/1415744133/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '28345',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildungsintegriertes-Duales-Studium-WING-Elektro-und-Informationstechnik-%28wmd%29-2027-38436/1415719733/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Ausbildungsintegriertes Duales Studium WING Elektro- und '
                      'Informationstechnik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildungsintegriertes-Duales-Studium-WING-Elektro-und-Informationstechnik-%28wmd%29-2027-38436/1415719733/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,technical drawing. DE '
                        'B2. Docs: CV; all latest-report pages and all school '
                        'qualifications; foreign certificates need certified German '
                        'translations. >€1401.50/month;35h/week;22 leave days; '
                        'possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildungsintegriertes-Duales-Studium-WING-Elektro-und-Informationstechnik-%28wmd%29-2027-38436/1415719733/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '29077',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Human-Factors-%28wmd%29-38436/1426504333/',
  'application_window': None,
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Praktikum Human Factors (w/m/d)',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Human-Factors-%28wmd%29-38436/1426504333/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Human-factors/cognitive-science/HCI or related students. '
                        'Basic qualitative-user-research/concept/data/statistics '
                        'skills; measurement-data knowledge preferred. DE+EN B2. Docs: '
                        'CV; enrolment proof; current transcript; university '
                        'certificate for mandatory internship; work permit for non-EU '
                        'applicants. Current statutory minimum wage.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': None,
             'language': 'de',
             'category': 'internships',
             'categories': ['internships'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Praktikum-Human-Factors-%28wmd%29-38436/1426504333/',
                                             'Actual publisher Karrierelevel: '
                                             'Praktikanten']}}},
 {'identity': '28223',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Materialwissenschaft-und-Werkzeugtechnik-%28wmd%29-2027-38436/1415767533/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Duales Studium Materialwissenschaft und Werkzeugtechnik (w/m/d) '
                      '2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Materialwissenschaft-und-Werkzeugtechnik-%28wmd%29-2027-38436/1415767533/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Technical understanding and analytical thinking. DE '
                        'B2. Docs: CV; all latest-report pages and all school '
                        'qualifications; foreign certificates need certified German '
                        'translations. €1401.50/month;35h/week;22 leave days; possible '
                        'hire after completion. Pension, vehicle purchase/leasing '
                        'conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Materialwissenschaft-und-Werkzeugtechnik-%28wmd%29-2027-38436/1415767533/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27084',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Informatik-%28wmd%29-2027-38436/1415749433/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Duales Studium Informatik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Informatik-%28wmd%29-2027-38436/1415749433/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in coding,IT security. DE B2. Docs: CV; all '
                        'latest-report pages and all school qualifications; foreign '
                        'certificates need certified German translations. '
                        '>€1401.50/month;35h/week;22 leave days; possible hire after '
                        'completion. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Informatik-%28wmd%29-2027-38436/1415749433/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '28343',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildungsintegriertes-Duales-Studium-Elektro-und-Informations%C2%ADtechnik-%28wmd%29-2027-38436/1415718033/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Ausbildungsintegriertes Duales Studium Elektro- und '
                      'Informations\xadtechnik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildungsintegriertes-Duales-Studium-Elektro-und-Informations%C2%ADtechnik-%28wmd%29-2027-38436/1415718033/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,technical drawing. DE '
                        'B2. Docs: CV; all latest-report pages and all school '
                        'qualifications; foreign certificates need certified German '
                        'translations. >€1401.50/month;35h/week;22 leave days; '
                        'possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Ausbildungsintegriertes-Duales-Studium-Elektro-und-Informations%C2%ADtechnik-%28wmd%29-2027-38436/1415718033/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27698',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Betriebswirtschaft-%28wmd%29-2027-38436/1415731233/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Duales Studium Betriebswirtschaft (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Betriebswirtschaft-%28wmd%29-2027-38436/1415731233/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.4; good relevant advanced-course '
                        'grades. Interest in business processes. DE B2. Docs: CV; all '
                        'latest-report pages and all school qualifications; foreign '
                        'certificates need certified German translations. '
                        '€1401.50/month;35h/week;22 leave days; possible hire after '
                        'completion. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Betriebswirtschaft-%28wmd%29-2027-38436/1415731233/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '28228',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Energie-und-Geb%C3%A4udetechnik-%28wmd%29-2027-38436/1415742833/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Duales Studium Energie- und Gebäudetechnik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Energie-und-Geb%C3%A4udetechnik-%28wmd%29-2027-38436/1415742833/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Technical understanding and analytical thinking. DE '
                        'B2. Docs: CV; all latest-report pages and all school '
                        'qualifications; foreign certificates need certified German '
                        'translations. €1401.50/month;35h/week;22 leave days; possible '
                        'hire after completion. Pension, vehicle purchase/leasing '
                        'conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Wolfsburg, DE, 38436',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Wolfsburg-Duales-Studium-Energie-und-Geb%C3%A4udetechnik-%28wmd%29-2027-38436/1415742833/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '28642',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Fertigungsmechanikerin-Fertigungsmechaniker-%28wmd%29-2027-30419/1415645233/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Ausbildung Fertigungsmechanikerin / Fertigungsmechaniker '
                      '(w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Fertigungsmechanikerin-Fertigungsmechaniker-%28wmd%29-2027-30419/1415645233/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education; Realschule recommended. Interest '
                        'in business, maths/English. DE B2. Docs: CV; all '
                        'latest-report pages and all school qualifications; foreign '
                        'certificates need certified German translations. '
                        '>€1327/month;35h/week;30 leave days; possible hire after '
                        'completion. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Fertigungsmechanikerin-Fertigungsmechaniker-%28wmd%29-2027-30419/1415645233/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '28638',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Fachkraft-f%C3%BCr-Systemgastronomie-%28wmd%29-2027-30419/1415635033/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Ausbildung Fachkraft für Systemgastronomie (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Fachkraft-f%C3%BCr-Systemgastronomie-%28wmd%29-2027-30419/1415635033/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Completed school education. Interest in German, '
                        'maths/business. DE B2. Docs: CV; all latest-report pages and '
                        'all school qualifications; foreign certificates need '
                        'certified German translations. €1327/month;35h/week;30 leave '
                        'days; possible hire after completion. Pension, vehicle '
                        'purchase/leasing conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Ausbildung-Fachkraft-f%C3%BCr-Systemgastronomie-%28wmd%29-2027-30419/1415635033/',
                                             'Actual publisher Karrierelevel: '
                                             'Auszubildende']}}},
 {'identity': '26980',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Duales-Studium-Ingenieurinformatik-%28wmd%29-2027-30419/1415752133/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Duales Studium Ingenieurinformatik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Duales-Studium-Ingenieurinformatik-%28wmd%29-2027-30419/1415752133/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in coding,IT security. DE B2. Docs: CV; all '
                        'latest-report pages and all school qualifications; foreign '
                        'certificates need certified German translations. '
                        '€1401.50/month;35h/week;22 leave days; possible hire after '
                        'completion. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Duales-Studium-Ingenieurinformatik-%28wmd%29-2027-30419/1415752133/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '27119',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Duales-Studium-Elektro-und-Informationstechnik-%28wmd%29-2027-30419/1415741333/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Duales Studium Elektro- und Informationstechnik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Duales-Studium-Elektro-und-Informationstechnik-%28wmd%29-2027-30419/1415741333/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,technical drawing. DE '
                        'B2. Docs: CV; all latest-report pages and all school '
                        'qualifications; foreign certificates need certified German '
                        'translations. €1401.50/month;35h/week;22 leave days; possible '
                        'hire after completion. Pension, vehicle purchase/leasing '
                        'conditions, bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Hannover, DE, 30419',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Hannover-Duales-Studium-Elektro-und-Informationstechnik-%28wmd%29-2027-30419/1415741333/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}},
 {'identity': '28625',
  'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Wirtschaftsinformatik-%28wmd%29-2027-34219/1415578133/',
  'application_window': {'opens': '2026-08-01',
                         'closes': '2027-04-30',
                         'precision': 'calendar-date'},
  'literal_publication_calendar_date': '2026-09-12',
  'fields': {'title': 'Duales Studium Wirtschaftsinformatik (w/m/d) 2027',
             'url': 'https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Wirtschaftsinformatik-%28wmd%29-2027-34219/1415578133/',
             'source': 'volkswagen-group',
             'source_url': 'https://www.volkswagen-karriere.de/',
             'published_at': None,
             'summary': 'Abitur grade threshold 2.7; good relevant advanced-course '
                        'grades. Interest in modern technology,coding. DE B2. Docs: '
                        'CV; all latest-report pages and all school qualifications; '
                        'foreign certificates need certified German translations. '
                        '>€1401.50/month;35h/week;22 leave days; possible hire after '
                        'completion. Pension, vehicle purchase/leasing conditions, '
                        'bicycle leasing; Dec24+31 off.',
             'summary_language': 'en',
             'tags': [],
             'location': 'Kassel, DE, 34219',
             'deadline': '2027-04-30',
             'language': 'de',
             'category': 'training',
             'categories': ['training', 'jobs'],
             'kind': 'individual-opportunity',
             'host_countries': ['DE'],
             'eligible_countries': [],
             'publisher_country': 'DE',
             'classification': {'method': 'editorial-review',
                                'status': 'classified',
                                'evidence': ['https://jobs.volkswagen-group.com/Volkswagen/job/Kassel-Duales-Studium-Wirtschaftsinformatik-%28wmd%29-2027-34219/1415578133/',
                                             'Actual publisher Karrierelevel: Dual '
                                             'Studierende']}}}]

if __name__ == "__main__":
    raise SystemExit(main())
