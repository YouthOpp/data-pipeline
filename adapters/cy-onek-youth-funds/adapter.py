"""Collect genuine ONEK funding rounds and the reviewed information session."""

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

SOURCE_ID = "cy-onek-youth-funds"
SOURCE_URL = "https://youthfunds.onek.org.cy/en/services/youth-initiatives/"
WEBSITE_URL = "https://youthfunds.onek.org.cy/en/"
LANGUAGE = "en"
PUBLISHER_COUNTRY = "CY"
PUBLISHER_TYPE = "government"
ATTRIBUTION = "Youth Board of Cyprus (ONEK), https://youthfunds.onek.org.cy/en/; original factual summaries with canonical publisher links"

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
}
FAMILY = "youthopps-onek-publisher-v1"
FAMILY_SOURCES = {"cy-onek-youth-funds"}
COLLECTION_TIMEOUT = 900
RUN_TIMEOUT = 1290
EXPORT_TIMEOUT = 30
_STATE_SCHEMA = 1
_ARTIFACT_NAME = "onek-pacing-state"
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
        raise AdapterError("Corrupt publisher pacing state", "access") from None


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
            if publisher and status in (301, 302, 303, 307, 308):
                if not location or redirect == 5:
                    raise AdapterError("Invalid redirect chain", "access")
                destination = urllib.parse.urljoin(current, location)
                public_url(destination)
                if urllib.parse.urlsplit(destination).scheme != "https":
                    raise AdapterError(
                        "Publisher redirect transport downgrade", "access"
                    )
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
        raise AdapterError("Publisher robots excludes required input", "access")
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
        raw = os.environ.get("ONEK_PACING_BOOTSTRAP", "")
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
    path = os.environ.get("ONEK_PACING_ARTIFACT_PATH")
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
        if record.get("summary_language") != "en":
            raise AdapterError("Invalid summary language", "validate")
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


def public_url(value):
    """Accept only literal reviewed publisher routes, including public paging."""
    try:
        parsed = urllib.parse.urlsplit(value)
        clean = urllib.parse.urlunsplit(parsed._replace(fragment=""))
        allowed = {info["url"] for info in INPUTS.values()} | {
            "https://youthfunds.onek.org.cy/robots.txt",
            "https://onek.org.cy/robots.txt",
        }
        if (
            clean not in allowed
            or parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.port
        ):
            raise ValueError()
    except ValueError:
        raise AdapterError("Unreviewed public source route", "access") from None
    return clean


def material_root(body, key):
    """Retain complete own content, rather than a navigation-only synopsis."""
    root = document_root(body.decode("utf-8", "strict"))
    if key in {"onek-home", "privacy"}:
        return root
    return one(
        nodes(root, "div", "lendiz-content-wrapper"),
        "official material content region",
    )


def factual_text(root):
    """Exclude only the reviewed event-view counter; validate its shape."""
    counters = nodes(root, "div", "wpem-viewed-event")
    for counter in counters:
        if counter.attrs.get(
            "class"
        ) != "wpem-viewed-event wpem-tooltip wpem-tooltip-bottom" or not re.fullmatch(
            r"(\d+) \1 people viewed this event\.", counter.text()
        ):
            raise AdapterError(
                "Changed public event counter structure", "parse"
            )

    def parts(node):
        if isinstance(node, str):
            return [node]
        if node in counters or node.tag in {"script", "style"}:
            return []
        return [part for child in node.children for part in parts(child)]

    return " ".join(" ".join(parts(root)).split())


def factual_links(root):
    """Pin substantive links and encoded contact targets without exporting them."""
    links = []
    for node in nodes(root, "a"):
        href = node.attrs.get("href", "")
        if not href:
            continue
        if href.startswith("/cdn-cgi/l/email-protection#"):
            encoded = href.split("#", 1)[1]
            try:
                if not re.fullmatch(r"(?:[0-9a-f]{2}){2,128}", encoded):
                    raise ValueError()
                raw = bytes.fromhex(encoded)
                target = bytes(byte ^ raw[0] for byte in raw[1:])
                target.decode("utf-8", "strict")
            except (ValueError, UnicodeError):
                raise AdapterError(
                    "Invalid reviewed contact target", "parse"
                ) from None
            href = (
                "reviewed-contact-sha256:" + hashlib.sha256(target).hexdigest()
            )
        links.append((node.text(), href))
    return sorted(set(links))


def calendar_parameters(body, page):
    """Serialize the observed public filter form using its current nonce."""
    root = document_root(body.decode("utf-8", "strict"))
    form = one(
        [
            node
            for node in nodes(root, "form")
            if node.attrs.get("id") == "event_filters"
        ],
        "public event form",
    )
    inputs = nodes(form, "input")
    expected = [
        "wpem_filter_nonce",
        "_wp_http_referer",
        "search_keywords",
        "search_location",
        "search_datetimes[]",
    ]
    if [node.attrs.get("name") for node in inputs] != expected:
        raise AdapterError("Changed public listing filter controls", "parse")
    values = [
        (node.attrs["name"], node.attrs.get("value", "")) for node in inputs
    ]
    if (
        not re.fullmatch(r"[0-9a-f]{10}", values[0][1])
        or values[1][1] != "/en/imerologio-draseon/"
        or any(value for _, value in values[2:])
    ):
        raise AdapterError("Changed public listing filter defaults", "parse")
    selects = nodes(form, "select")
    if [node.attrs.get("name") for node in selects] != [
        "search_categories[]",
        "search_event_types[]",
    ] or any(
        "selected" in option.attrs
        for node in selects
        for option in nodes(node, "option")
    ):
        raise AdapterError("Changed public listing category defaults", "parse")
    definitions = re.findall(
        r"var event_manager_ajax_filters = (\{[^\n]+\});", body.decode("utf-8")
    )
    try:
        config = json.loads(definitions[-1])
        if (
            set(config) != {"ajax_url", "nonce", "is_rtl", "lang"}
            or config["ajax_url"] != "/en/em-ajax/%%endpoint%%/"
            or config["is_rtl"] != "0"
            or config["lang"] is not None
            or not re.fullmatch(r"[0-9a-f]{10}", config["nonce"])
        ):
            raise ValueError()
    except (IndexError, ValueError, TypeError):
        raise AdapterError(
            "Changed observed public pagination route", "parse"
        ) from None
    return urllib.parse.urlencode(
        [
            ("lang", ""),
            ("search_keywords", ""),
            ("search_location", ""),
            ("search_datetimes[]", ""),
            ("per_page", "10"),
            ("orderby", "meta_value"),
            ("order", "ASC"),
            ("page", str(page)),
            ("event_online", ""),
            ("show_pagination", "false"),
            ("form_data", urllib.parse.urlencode(values)),
            ("wpem_filter_nonce", values[0][1]),
        ]
    ).encode()


def html_fingerprint(body, key):
    root = material_root(body, key)
    document = document_root(body.decode("utf-8", "strict"))
    footers = nodes(document, "footer", "site-footer")
    if key not in {"onek-home", "privacy"} and len(footers) != 1:
        raise AdapterError("Changed official footer structure", "parse")
    controls = []
    if key == "calendar":
        calendar_parameters(body, 1)
        for node in root.walk():
            if node.tag in {"input", "select", "option"}:
                attrs = dict(node.attrs)
                if attrs.get("name") == "wpem_filter_nonce":
                    attrs["value"] = "current-public-nonce"
                controls.append((node.tag, attrs, node.text()))
        listings = one(
            [
                node
                for node in nodes(root, "div", "event_listings")
                if "data-per_page" in node.attrs
            ],
            "public listing controls",
        )
        controls.append(("listing", listings.attrs, ""))
    facts = {
        "text": factual_text(root),
        "links": factual_links(root),
        "controls": controls,
        "footers": [
            {"text": factual_text(node), "links": factual_links(node)}
            for node in footers
        ],
    }
    return hashlib.sha256(
        json.dumps(
            facts, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def listing_fingerprint(body, key):
    """Bind the entire observed result and terminal page, including exclusions."""
    try:
        result = json.loads(body)
        if (
            set(result)
            != {
                "found_events",
                "html",
                "filter_value",
                "showing_applied_filters",
                "showing_links",
                "max_num_pages",
            }
            or type(result["found_events"]) is not bool
            or type(result["max_num_pages"]) is not int
            or not isinstance(result["html"], str)
        ):
            raise ValueError()
        root = PublisherHTML(result.pop("html")).root
        result["material"] = {
            "text": factual_text(root),
            "links": factual_links(root),
        }
        if key == "calendar-listing-1":
            urls = {
                href for _, href in factual_links(root) if "/ekdilosi/" in href
            }
            expected = {
                info["url"]
                for name, info in INPUTS.items()
                if name == "first-event" or re.fullmatch(r"event-\d+", name)
            }
            if (
                urls != expected
                or len(urls) != 10
                or not result["found_events"]
            ):
                raise ValueError()
        elif result["found_events"] or result["max_num_pages"] != 0:
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise AdapterError(
            "Changed or incomplete public event frontier", "parse"
        ) from None
    return hashlib.sha256(
        json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def read_input(key, calendar_body=None):
    info = INPUTS[key]
    if info["format"] == "json":
        if calendar_body is None:
            raise AdapterError("Missing current public listing form", "parse")
        payload = calendar_parameters(calendar_body, int(key[-1]))
        check_robots(info["url"])
        status, headers, body = request_bytes(
            info["url"],
            method="POST",
            payload=payload,
            publisher=True,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
            },
        )
    else:
        status, headers, body = fetch_public(info["url"])
    if status != 200:
        raise AdapterError(
            "Required public input failed: " + key, "fetch", status
        )
    mime = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    expected_mime = {
        "html": "text/html",
        "json": "application/json",
        "pdf": "application/pdf",
    }
    if mime != expected_mime[info["format"]]:
        raise AdapterError("Changed public input content type: " + key, "parse")
    if info["format"] == "html":
        digest = html_fingerprint(body, key)
    elif info["format"] == "json":
        digest = listing_fingerprint(body, key)
    else:
        if not body.startswith(b"%PDF-"):
            raise AdapterError("Invalid public guide: " + key, "parse")
        digest = hashlib.sha256(body).hexdigest()
    if digest != info["sha256"]:
        raise AdapterError(
            "Reviewed source facts/frontier changed: " + key, "parse"
        )
    return digest, body if key == "calendar" else None


def read_pages():
    pages = {}
    calendar_body = None
    for key, info in INPUTS.items():
        if info["format"] != "json":
            pages[key], retained = read_input(key)
            if retained is not None:
                calendar_body = retained
    for key, info in INPUTS.items():
        if info["format"] == "json":
            pages[key], _ = read_input(key, calendar_body)
    return pages


def parse_inventory(pages):
    if set(pages) != set(INPUTS) or any(
        pages[key] != info["sha256"] for key, info in INPUTS.items()
    ):
        raise AdapterError("Incomplete reviewed ONEK frontier", "parse")
    records = []
    today = datetime.now(timezone.utc).date().isoformat()
    checked = utc_now()
    for profile in PROFILES:
        record = make_record(
            profile["title"],
            profile["url"],
            [profile["category"]],
            host_countries=profile["hosts"],
            evidence=[ATTRIBUTION, profile["proof"]],
        )
        record["id"] = hashlib.sha256(
            (SOURCE_ID + "|" + profile["key"]).encode()
        ).hexdigest()[:24]
        record["summary"] = profile["summary"]
        record["summary_language"] = "en"
        record["deadline"] = profile["deadline"]
        record["status"] = (
            "expired"
            if today > profile["deadline"]
            else (
                "unknown"
                if today == profile["deadline"]
                or profile.get("opens")
                and today < profile["opens"]
                else "open"
            )
        )
        for field in (
            "created_at",
            "updated_at",
            "first_seen_at",
            "last_seen_at",
            "last_checked_at",
        ):
            record[field] = checked
        records.append(record)
    validate_records(records)
    if len(records) != 14:
        raise AdapterError(
            "Incomplete reviewed ONEK identity partition", "validate"
        )
    return records


def collect():
    if _PHASE_END is not None:
        check_deadline()
        return parse_inventory(read_pages())
    with execution():
        with phase("prepublication", COLLECTION_TIMEOUT):
            prepare_collection()
            return parse_inventory(read_pages())


def metadata(attempt, previous, records, error=None, checked_at=None):
    outcome = {
        "source": SOURCE_ID,
        "name": "ONEK — youth funding rounds and information session",
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
            else f"Collected {len(records)} reviewed ONEK opportunities"
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
        if records and timestamps["last_checked_at"] != max(
            datetime.fromisoformat(
                record["last_checked_at"].replace("Z", "+00:00")
            )
            for record in records
        ):
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


INPUTS = {
    "home": {
        "url": "https://youthfunds.onek.org.cy/en/",
        "format": "html",
        "sha256": "7c411699aa066770ff5edf23385ac4d44ae2b54056ca8b88a3e1e5fe959c5d26",
    },
    "programmes": {
        "url": "https://youthfunds.onek.org.cy/en/programmata/",
        "format": "html",
        "sha256": "02cbf0f22cb069da954d6899b127880df8a540b161e085e31aa3573bfa5d9421",
    },
    "initiatives": {
        "url": "https://youthfunds.onek.org.cy/en/services/youth-initiatives/",
        "format": "html",
        "sha256": "7441aaa8f2ed0cdfc58db5ee78c2090ac02a4f867be53079bc99d9885c9c20a3",
    },
    "green": {
        "url": "https://youthfunds.onek.org.cy/en/services/green-volunteering/",
        "format": "html",
        "sha256": "149bbfe407bce7407937a6484bf58489e2a07da0779b66e0e17141caca6caf91",
    },
    "world": {
        "url": "https://youthfunds.onek.org.cy/en/services/neoi-kai-kosmos/",
        "format": "html",
        "sha256": "6b8fa5e62fef259021e7fecc3e513901ecfaf5f4589b4abd94fb49d39b4018f1",
    },
    "faq": {
        "url": "https://youthfunds.onek.org.cy/en/sychnes-erotiseis/",
        "format": "html",
        "sha256": "58d2ac26d5ee426edc81491f9411881e5e1798e2a68a448a3dd585fa4de61473",
    },
    "news": {
        "url": "https://youthfunds.onek.org.cy/en/nea-anakoinoseis/",
        "format": "html",
        "sha256": "9ce598a0bd34503ae95ba11c0c597907d469197c5c17b82559cc8dd62d728981",
    },
    "news-2": {
        "url": "https://youthfunds.onek.org.cy/en/nea-anakoinoseis/page/2/",
        "format": "html",
        "sha256": "201f57abafe703ce0586d6c6f9bdf894c454d8cbe8a1a33e462c3ce2eadaac8e",
    },
    "green-call-2027": {
        "url": "https://youthfunds.onek.org.cy/en/a1-and-a2-calls-for-applications-under-the-green-volunteering-programme-for-2027/",
        "format": "html",
        "sha256": "b1e5c9b532b09cb72c1b66f4c3a88cbfdb94f127883a960c61a72ba157995548",
    },
    "youth-call-2027": {
        "url": "https://youthfunds.onek.org.cy/en/first-call-for-applications-under-the-youth-initiatives-programme-for-2027/",
        "format": "html",
        "sha256": "0f783c660fb4034ba26d57489b0c05aa2f5729be9cce617cb778cea25875d57b",
    },
    "green-call-2026": {
        "url": "https://youthfunds.onek.org.cy/en/dimosiefthike-i-v-prosklisi-tou-programmatos-prasinos-ethelontismos-gia-to-2026/",
        "format": "html",
        "sha256": "84f342885880253e7344823c0d4984425d84528632b1b275a0f993df98c3e2cf",
    },
    "world-call-2026": {
        "url": "https://youthfunds.onek.org.cy/en/dimosiefthike-i-v-prosklisi-tou-programmatos-neoi-kai-kosmos-gia-to-2026/",
        "format": "html",
        "sha256": "646f32eeecba0e7ec15ff30b484272526809615c57a05862c4ffe4b005152fb6",
    },
    "third-youth-2026": {
        "url": "https://youthfunds.onek.org.cy/en/dimosiefthike-i-g-prosklisi-tou-programmatos-protovoulies-neon-gia-to-2026/",
        "format": "html",
        "sha256": "1d52274d9525300c446c36ed1c7c18fad7bc9ec26317cd8408d5a937f10de793",
    },
    "green-guide-news": {
        "url": "https://youthfunds.onek.org.cy/en/friendly-business-policies/",
        "format": "html",
        "sha256": "b51ce55c48269d11b5877da5ccc245bd27923e2de46742597d9fd78d8db66a5f",
    },
    "green-extension": {
        "url": "https://youthfunds.onek.org.cy/en/reduce-your-home-loan-rate/",
        "format": "html",
        "sha256": "c53a4821e51bf26e8a6f66c129162cb2206b62b24362204e3dd69748bde25189",
    },
    "green-older-announcement": {
        "url": "https://youthfunds.onek.org.cy/en/anakoinosi-gia-to-programma-prasinos-ethelontismos/",
        "format": "html",
        "sha256": "1bd64712387a8a2ee66871fdcb0c9ffa76a146c8c241cc229e484392ae9e3791",
    },
    "world-older-call": {
        "url": "https://youthfunds.onek.org.cy/en/business-financing-loans/",
        "format": "html",
        "sha256": "9502a7df6edb54045fe5f7e3dee19e716ab1c3b1d842363b5a30e0544c53d8c8",
    },
    "presentation-result": {
        "url": "https://youthfunds.onek.org.cy/en/chrimatodotika-programmata-onek-41-tropoi-gia-na-kaneis-tin-idea-sou-praxi/",
        "format": "html",
        "sha256": "ec54c5a261d0148387a7bc52481f5bfc4cb0c04ad9344a480fb915075335c294",
    },
    "presentation-invite": {
        "url": "https://youthfunds.onek.org.cy/en/41-tropoi-gia-na-kaneis-tin-idea-sou-praxi-parousiasi-ton-chrimatodotikon-programmaton-tou-organismou-neolaias/",
        "format": "html",
        "sha256": "ffeb97861a39687aa7922570a0daefa2cfedf1e2424ea71cdab4151b902cde5b",
    },
    "online-presentation": {
        "url": "https://youthfunds.onek.org.cy/en/parousiasi-logismikou-tou-chrimatodotikou-programmatos-protovoulies-neon/",
        "format": "html",
        "sha256": "c695b4c2a604f39d7759bf21370a7587bb498d362693ff5fa80752063e7f4332",
    },
    "guide-initiatives": {
        "url": "https://youthfunds.onek.org.cy/wp-content/uploads/2019/04/Odhgos-Protoboylies-Neon.pdf",
        "format": "pdf",
        "sha256": "ee08e5e07ac722bbcf8386edd13b513d9efef4eac29cdd7f01f76cc68c230ca1",
    },
    "guide-green": {
        "url": "https://youthfunds.onek.org.cy/wp-content/uploads/2019/04/Odhgos-Prasinos-Ethelontismos.pdf",
        "format": "pdf",
        "sha256": "74fe5e1e51c09684dd97171065d69e46499298bd83e7471cb305d7d566450938",
    },
    "guide-world": {
        "url": "https://youthfunds.onek.org.cy/wp-content/uploads/2019/04/Odigos-neoi-kai-kosmos-1.pdf",
        "format": "pdf",
        "sha256": "2675de1227ba386fce17a88179154347920930e4f880ce124bdbf8ef06ee2e30",
    },
    "calendar": {
        "url": "https://youthfunds.onek.org.cy/en/imerologio-draseon/",
        "format": "html",
        "sha256": "c01c6461d6281543d712fa2fa4467fc097562faaec7c11e6b9fc3e8ac6e32506",
    },
    "first-event": {
        "url": "https://youthfunds.onek.org.cy/ekdilosi/enimerotiki-imerida-evropaika-programmata-chrimatodotisis-2026/",
        "format": "html",
        "sha256": "74274287b11b88950fe650aa2329b1a6c9a62642b1bd9fae67225f57ab4d857f",
    },
    "event-2": {
        "url": "https://youthfunds.onek.org.cy/ekdilosi/ergastirio-syntaxi-protasis-gia-horizon-europe/",
        "format": "html",
        "sha256": "6a9b3e7f3789f57e1a18f98a068842525a7ac3d899f99d341259bd72fea59fe0",
    },
    "event-3": {
        "url": "https://youthfunds.onek.org.cy/ekdilosi/seminario-chrimatodotisi-neofyon-epicheiriseon/",
        "format": "html",
        "sha256": "16d571ad78ab6c316e86f02054d8c292e3b615ae4cd8ca68b4d07ef02be749fa",
    },
    "event-4": {
        "url": "https://youthfunds.onek.org.cy/ekdilosi/workshop-psifiakos-metaschimatismos-mme/",
        "format": "html",
        "sha256": "338dcd068c62231e081f3edf3d84db9de2335cb16ea48ef69a515e35e9fb7265",
    },
    "event-5": {
        "url": "https://youthfunds.onek.org.cy/ekdilosi/imerida-chrimatodotisi-athlitikon-ypodomon/",
        "format": "html",
        "sha256": "2370a10d9a5e62d2b43943b059d333add4ef627be5e9723bb7f38f98bf3b4ec2",
    },
    "event-6": {
        "url": "https://youthfunds.onek.org.cy/ekdilosi/parousiasi-erasmus-gia-scholeia-kai-organismous/",
        "format": "html",
        "sha256": "3c0394735238970ac8b892804b4b28f8b730847e0ef290fd60412c4bc098de59",
    },
    "event-7": {
        "url": "https://youthfunds.onek.org.cy/ekdilosi/ergastirio-diacheirisi-evropaikon-ergon/",
        "format": "html",
        "sha256": "4b9e2fe8232b4eb4592281f9a766bb4b7ba1c1092455e28f2415af8fad4c62b0",
    },
    "event-8": {
        "url": "https://youthfunds.onek.org.cy/ekdilosi/diavoulefsi-ethniko-schedio-anakampsis-kai-anthektikotitas/",
        "format": "html",
        "sha256": "76405f476ed0efd65b8afe96ef088a82ac281d208d2d14355917530a3957e85e",
    },
    "event-9": {
        "url": "https://youthfunds.onek.org.cy/ekdilosi/seminario-epidotiseis-gia-prasini-energeia/",
        "format": "html",
        "sha256": "08675e56fbdefdf0f61cd71dd4b7281b15fe2da9fb98229b0193802daac2d69b",
    },
    "event-10": {
        "url": "https://youthfunds.onek.org.cy/ekdilosi/enimerosi-tameio-synochis-kai-perifereiaki-anaptyxi/",
        "format": "html",
        "sha256": "a15e39fd568628e8d808fcb27965ff00d2ae4054eae3ef091fe6039ef73e3e74",
    },
    "calendar-listing-1": {
        "url": "https://youthfunds.onek.org.cy/en/em-ajax/get_listings/",
        "format": "json",
        "sha256": "db706b49a3b409d0591088891d69ec34ed04bcf268f56793c7a41dfd85dc1083",
    },
    "calendar-listing-2": {
        "url": "https://youthfunds.onek.org.cy/en/em-ajax/get_listings/",
        "format": "json",
        "sha256": "1e7cccd419b3ae0142ced841fe3db4cbadf14992a564907728092680187fcf2c",
    },
    "onek-home": {
        "url": "https://onek.org.cy",
        "format": "html",
        "sha256": "a88a9bf4c6054637afa76c6ee1cb805642fb04b7c3a76145eae1ae366b90bb75",
    },
    "privacy": {
        "url": "https://onek.org.cy/poioi-eimaste/politiki-prostasias-prosopikon-dedomenon/",
        "format": "html",
        "sha256": "01f4f5c0f9e878a59d8200daa945e74a269224f54f7c37013059d08f2ec50db4",
    },
    "initiatives-gr": {
        "url": "https://youthfunds.onek.org.cy/services/protovoulies-neon/",
        "format": "html",
        "sha256": "0f68201cc4af64211621fefccd901617951578e6ce351fa0109903b36d3d6b32",
    },
    "green-gr": {
        "url": "https://youthfunds.onek.org.cy/services/prasinos-ethelontismos/",
        "format": "html",
        "sha256": "87414fc6bc8cd46fafb15fc11877173cdf139a6ef3da123e10fac8a59e66b869",
    },
    "world-gr": {
        "url": "https://youthfunds.onek.org.cy/services/neoi-kai-kosmos/",
        "format": "html",
        "sha256": "989782788a1daf515fef56c0ad128fb167dd8ec25e92306ef2a592626aded234",
    },
    "guide-world-gr": {
        "url": "https://youthfunds.onek.org.cy/wp-content/uploads/2019/04/Odigos-neoi-kai-kosmos.pdf",
        "format": "pdf",
        "sha256": "2675de1227ba386fce17a88179154347920930e4f880ce124bdbf8ef06ee2e30",
    },
}

PROFILES = [
    {
        "key": "youth-initiatives-2027-a",
        "title": "First Call for Applications under the “Youth Initiatives” Programme for "
        "2027",
        "url": "https://youthfunds.onek.org.cy/en/first-call-for-applications-under-the-youth-initiatives-programme-for-2027/",
        "category": "grants",
        "hosts": ["CY"],
        "deadline": "2026-09-16",
        "summary": "Grants for eligible non-profit youth activities in Cyprus, for young "
        "people, groups of 4+, youth centres and organisations. Individual "
        "applicants have restricted activity eligibility. Current pages say "
        "13–35; the guide uses a different age cutoff, so confirm age and current "
        "expense caps before applying. CallA is closed; activities run 1 Dec "
        "2026–31 Mar 2027.",
        "proof": "Actual published funding/event facts: "
        "https://youthfunds.onek.org.cy/en/first-call-for-applications-under-the-youth-initiatives-programme-for-2027/",
    },
    {
        "key": "youth-initiatives-2027-b",
        "title": "Youth Initiatives",
        "url": "https://youthfunds.onek.org.cy/en/services/youth-initiatives/",
        "category": "grants",
        "hosts": ["CY"],
        "deadline": "2027-01-16",
        "summary": "Grants for eligible non-profit youth activities in Cyprus, for young "
        "people, groups of 4+, youth centres and organisations. Individual "
        "applicants have restricted activity eligibility. Current pages say "
        "13–35; the guide uses a different age cutoff, so confirm age and current "
        "expense caps before applying. CallB applications run 10 Dec 2026–16 Jan "
        "2027 for activities 1 Apr–31 Jul 2027; not yet open.",
        "proof": "Actual published funding/event facts: "
        "https://youthfunds.onek.org.cy/en/services/youth-initiatives/",
        "opens": "2026-12-10",
    },
    {
        "key": "youth-initiatives-2027-c",
        "title": "Youth Initiatives",
        "url": "https://youthfunds.onek.org.cy/en/services/youth-initiatives/",
        "category": "grants",
        "hosts": ["CY"],
        "deadline": "2027-05-16",
        "summary": "Grants for eligible non-profit youth activities in Cyprus, for young "
        "people, groups of 4+, youth centres and organisations. Individual "
        "applicants have restricted activity eligibility. Current pages say "
        "13–35; the guide uses a different age cutoff, so confirm age and current "
        "expense caps before applying. CallC applications run 10 Apr–16 May 2027 "
        "for activities 1 Aug–30 Nov 2027; not yet open.",
        "proof": "Actual published funding/event facts: "
        "https://youthfunds.onek.org.cy/en/services/youth-initiatives/",
        "opens": "2027-04-10",
    },
    {
        "key": "youth-initiatives-2026-c",
        "title": "Third Call for Applications under the “Youth Initiatives” Programme for "
        "2026",
        "url": "https://youthfunds.onek.org.cy/en/dimosiefthike-i-g-prosklisi-tou-programmatos-protovoulies-neon-gia-to-2026/",
        "category": "grants",
        "hosts": ["CY"],
        "deadline": "2026-05-16",
        "summary": "Grants for eligible non-profit youth activities in Cyprus, for young "
        "people, groups of 4+, youth centres and organisations. Individual "
        "applicants have restricted activity eligibility. Current pages say "
        "13–35; the guide uses a different age cutoff, so confirm age and current "
        "expense caps before applying. Historical third 2026 call closed 16 May "
        "2026, for activities 1 Aug–30 Nov 2026.",
        "proof": "Actual published funding/event facts: "
        "https://youthfunds.onek.org.cy/en/dimosiefthike-i-g-prosklisi-tou-programmatos-protovoulies-neon-gia-to-2026/",
    },
    {
        "key": "green-volunteering-2027-a",
        "title": "A’1 and A’2 Calls for Applications under the “Green Volunteering” "
        "Programme for 2027",
        "url": "https://youthfunds.onek.org.cy/en/a1-and-a2-calls-for-applications-under-the-green-volunteering-programme-for-2027/",
        "category": "grants",
        "hosts": [],
        "deadline": "2026-11-03",
        "summary": "Environmental volunteer grants for groups of 4+ youths 13–35, secondary "
        "schools, youth centres and organisations. Eligible costs depend on "
        "activity; the linked 2024 guide differs from current action numbering, "
        "so confirm current caps and age rules. Joint 2027 A 1/A 2 applications "
        "close 3 Nov 2026. A 1 actions 1–4 run 1 Jan–30 Jun 2027; A 2 "
        "larger/innovative projects run 1 Jan–30 Nov 2027.",
        "proof": "Actual published funding/event facts: "
        "https://youthfunds.onek.org.cy/en/a1-and-a2-calls-for-applications-under-the-green-volunteering-programme-for-2027/",
        "opens": "2026-10-03",
    },
    {
        "key": "green-volunteering-2027-b",
        "title": "Green Volunteering",
        "url": "https://youthfunds.onek.org.cy/en/services/green-volunteering/",
        "category": "grants",
        "hosts": [],
        "deadline": "2027-06-17",
        "summary": "Environmental volunteer grants for groups of 4+ youths 13–35, secondary "
        "schools, youth centres and organisations. Eligible costs depend on "
        "activity; the linked 2024 guide differs from current action numbering, "
        "so confirm current caps and age rules. The Greek programme table "
        "confirms applications 17 May–17 Jun 2027 for activities 1 Jul–30 Nov "
        "2027. This is a future application window.",
        "proof": "Actual published funding/event facts: "
        "https://youthfunds.onek.org.cy/en/services/green-volunteering/",
        "opens": "2027-05-17",
    },
    {
        "key": "green-volunteering-2026-b",
        "title": "Second Call for Applications under the “Green Volunteering” Programme for "
        "2026",
        "url": "https://youthfunds.onek.org.cy/en/dimosiefthike-i-v-prosklisi-tou-programmatos-prasinos-ethelontismos-gia-to-2026/",
        "category": "grants",
        "hosts": [],
        "deadline": "2026-06-17",
        "summary": "Environmental volunteer grants for groups of 4+ youths 13–35, secondary "
        "schools, youth centres and organisations. Eligible costs depend on "
        "activity; the linked 2024 guide differs from current action numbering, "
        "so confirm current caps and age rules. Historical second 2026 call "
        "closed 17 Jun 2026; activities run 1 Jul–30 Nov 2026.",
        "proof": "Actual published funding/event facts: "
        "https://youthfunds.onek.org.cy/en/dimosiefthike-i-v-prosklisi-tou-programmatos-prasinos-ethelontismos-gia-to-2026/",
    },
    {
        "key": "green-volunteering-2025-a",
        "title": "First Call for Applications for 2025 and New “Green Volunteering” "
        "Programme Guide",
        "url": "https://youthfunds.onek.org.cy/en/friendly-business-policies/",
        "category": "grants",
        "hosts": [],
        "deadline": "2024-11-06",
        "summary": "Environmental volunteer grants for groups of 4+ youths 13–35, secondary "
        "schools, youth centres and organisations. Eligible costs depend on "
        "activity; the linked 2024 guide differs from current action numbering, "
        "so confirm current caps and age rules. Historical first 2025 call opened "
        "3 Oct 2024; deadline extended to 6 Nov 2024. Actions 1–4 runJan–Jun "
        "2025, actions 5–6 Jan–Nov 2025.",
        "proof": "Actual published funding/event facts: "
        "https://youthfunds.onek.org.cy/en/friendly-business-policies/",
    },
    {
        "key": "green-volunteering-2024-b",
        "title": "Announcement on the “Green Volunteering” Programme",
        "url": "https://youthfunds.onek.org.cy/en/anakoinosi-gia-to-programma-prasinos-ethelontismos/",
        "category": "grants",
        "hosts": [],
        "deadline": "2024-07-31",
        "summary": "Environmental volunteer grants for groups of 4+ youths 13–35, secondary "
        "schools, youth centres and organisations. Eligible costs depend on "
        "activity; the linked 2024 guide differs from current action numbering, "
        "so confirm current caps and age rules. Historical round for activities "
        "20 Sep–30 Nov 2024 extended its deadline to 31 Jul 2024 at 23:59:59; no "
        "timezone was stated.",
        "proof": "Actual published funding/event facts: "
        "https://youthfunds.onek.org.cy/en/anakoinosi-gia-to-programma-prasinos-ethelontismos/",
    },
    {
        "key": "youth-and-world-2027-a",
        "title": "Youth and the World",
        "url": "https://youthfunds.onek.org.cy/en/services/neoi-kai-kosmos/",
        "category": "grants",
        "hosts": [],
        "deadline": "2027-04-30",
        "summary": "Travel support for invited or selected international participation by "
        "youths and youth organisations. The linked guide requires lawful "
        "permanent Cyprus residence over 6 months; this is not a citizenship "
        "rule. Confirm current expense caps, age conditions and permitted "
        "activities. Apply before participating. 2027 CallA applications run 1 "
        "Nov 2026–30 Apr 2027; activities 1 Dec 2026–31 May 2027. Not yet open "
        "despite the generic Open badge.",
        "proof": "Actual published funding/event facts: "
        "https://youthfunds.onek.org.cy/en/services/neoi-kai-kosmos/",
        "opens": "2026-11-01",
    },
    {
        "key": "youth-and-world-2027-b",
        "title": "Youth and the World",
        "url": "https://youthfunds.onek.org.cy/en/services/neoi-kai-kosmos/",
        "category": "grants",
        "hosts": [],
        "deadline": "2027-10-31",
        "summary": "Travel support for invited or selected international participation by "
        "youths and youth organisations. The linked guide requires lawful "
        "permanent Cyprus residence over 6 months; this is not a citizenship "
        "rule. Confirm current expense caps, age conditions and permitted "
        "activities. Apply before participating. 2027 CallB applications run 1 "
        "May–31 Oct 2027; activities 1 Jun–30 Nov 2027. This is a future "
        "application window.",
        "proof": "Actual published funding/event facts: "
        "https://youthfunds.onek.org.cy/en/services/neoi-kai-kosmos/",
        "opens": "2027-05-01",
    },
    {
        "key": "youth-and-world-2026-b",
        "title": "Second Call for Applications under the “Youth and the World” Programme for "
        "2026",
        "url": "https://youthfunds.onek.org.cy/en/dimosiefthike-i-v-prosklisi-tou-programmatos-neoi-kai-kosmos-gia-to-2026/",
        "category": "grants",
        "hosts": [],
        "deadline": "2026-10-31",
        "summary": "Travel support for invited or selected international participation by "
        "youths and youth organisations. The linked guide requires lawful "
        "permanent Cyprus residence over 6 months; this is not a citizenship "
        "rule. Confirm current expense caps, age conditions and permitted "
        "activities. Apply before participating. Current second 2026 call closes "
        "31 Oct 2026, for activities 1 Jun–30 Nov 2026.",
        "proof": "Actual published funding/event facts: "
        "https://youthfunds.onek.org.cy/en/dimosiefthike-i-v-prosklisi-tou-programmatos-neoi-kai-kosmos-gia-to-2026/",
    },
    {
        "key": "youth-and-world-2025-a",
        "title": "New Application Period for the “Youth and the World” Funding Programme",
        "url": "https://youthfunds.onek.org.cy/en/business-financing-loans/",
        "category": "grants",
        "hosts": [],
        "deadline": "2025-04-30",
        "summary": "Travel support for invited or selected international participation by "
        "youths and youth organisations. The linked guide requires lawful "
        "permanent Cyprus residence over 6 months; this is not a citizenship "
        "rule. Confirm current expense caps, age conditions and permitted "
        "activities. Apply before participating. Historical round accepted "
        "applications 1 Nov 2024–30 Apr 2025 for activities 1 Dec 2024–31 May "
        "2025.",
        "proof": "Actual published funding/event facts: "
        "https://youthfunds.onek.org.cy/en/business-financing-loans/",
    },
    {
        "key": "funding-presentation-2024",
        "title": "“4+1 Ways to Turn Your Idea into Action”: Presentation of the Youth Board "
        "of Cyprus Funding Programmes",
        "url": "https://youthfunds.onek.org.cy/en/41-tropoi-gia-na-kaneis-tin-idea-sou-praxi-parousiasi-ton-chrimatodotikon-programmaton-tou-organismou-neolaias/",
        "category": "training",
        "hosts": ["CY"],
        "deadline": "2024-06-13",
        "summary": "Historical ONEK information session for young people, informal groups "
        "and youth organisations about funding social, cultural, environmental "
        "and international activities. Held at the Eleftheria Square Amphitheatre "
        "on 19 Jun 2024 at 18:30; RSVP closed 13 Jun 2024. No attendance funding "
        "or fee was specified. The subsequent event report describes the same "
        "session.",
        "proof": "Actual published funding/event facts: "
        "https://youthfunds.onek.org.cy/en/41-tropoi-gia-na-kaneis-tin-idea-sou-praxi-parousiasi-ton-chrimatodotikon-programmaton-tou-organismou-neolaias/",
    },
]


if __name__ == "__main__":
    sys.exit(main())
