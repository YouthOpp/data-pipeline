"""Collect the reviewed FRSE funding, training, jobs and awards frontier."""

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

SOURCE_ID = "pl-frse"
SOURCE_URL = "https://www.frse.org.pl/"
WEBSITE_URL = "https://www.frse.org.pl/"
LANGUAGE = "pl"
PUBLISHER_COUNTRY = "PL"
PUBLISHER_TYPE = "education-foundation"
ATTRIBUTION = (
    "Fundacja Rozwoju Systemu Edukacji (FRSE), https://www.frse.org.pl/; "
    "official Erasmus+, European Solidarity Corps and named FRSE programmes"
)

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
FAMILY = "youthopps-frse-publisher-v1"
FAMILY_SOURCES = {"pl-frse"}
COLLECTION_TIMEOUT = 1800
RUN_TIMEOUT = 2190
EXPORT_TIMEOUT = 30
_STATE_SCHEMA = 1
_ARTIFACT_NAME = "frse-pacing-state"
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
        if target > now:
            time.sleep(target - now)
        now = time.time()
        if now + 0.001 < target:
            raise AdapterError(
                ("Publisher pacing clock moved backwards"), ("access")
            )
        state["starts"] = [t for t in starts if t > now - 60] + [now]
        state["not_before"] = max(
            state["not_before"], now + state.get("interval", 6)
        )
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
                    state["interval"] = max(state.get("interval", 6), delay)
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
        raw = os.environ.get("FRSE_PACING_BOOTSTRAP", "")
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
        combined["interval"] = max(
            local.get("interval", 6), incoming.get("interval", 6)
        )
        combined[("starts")] = sorted(
            set(local[("starts")] + incoming[("starts")])
        )[-10:]
        save_budget(combined)
    _RESTORED, _STATE_TRUSTED = True, True


def export_family_artifact():
    """Best-effort infrastructure state; never contains records or secrets."""
    path = os.environ.get("FRSE_PACING_ARTIFACT_PATH")
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
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as file:
        info = os.fstat(file.fileno())
        if (
            info.st_uid != os.getuid()
            or not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise AdapterError("Unsafe pacing export file", "access")
        file.truncate(0)
        json.dump(envelope, file, separators=(",", ":"))


def preserve_legacy_pacing():
    """Lock bound legacy state through read and merge, retaining its inode."""
    descriptors = []
    try:
        parent = os.open(
            tempfile.gettempdir(),
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        descriptors.append(parent)
        for name in ("youthopps-source-triage", "37"):
            try:
                directory = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent,
                )
            except FileNotFoundError:
                return
            descriptors.append(directory)
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise AdapterError("Unsafe legacy pacing directory", "access")
            parent = directory
        try:
            descriptor = os.open(
                "pacing.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent
            )
        except FileNotFoundError:
            return
        descriptors.append(descriptor)
        info = os.fstat(descriptor)
        if (
            info.st_uid != os.getuid()
            or not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise AdapterError("Unsafe legacy FRSE pacing state", "access")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise AdapterError(
                "Legacy FRSE collector active", "access"
            ) from None
        raw = os.read(descriptor, 16385)
        try:
            legacy = json.loads(raw)
            if (
                len(raw) > 16384
                or not isinstance(legacy, dict)
                or not {"starts", "interval", "until"} <= set(legacy)
                or set(legacy) - {"starts", "interval", "until", "blocked"}
                or not numeric(legacy["until"])
                or not numeric(legacy["interval"])
                or legacy["interval"] < 6
                or type(legacy.get("blocked", False)) is not bool
                or not isinstance(legacy["starts"], list)
                or len(legacy["starts"]) > 10
                or any(not numeric(value) for value in legacy["starts"])
                or legacy["starts"] != sorted(set(legacy["starts"]))
                or any(value > time.time() + 1 for value in legacy["starts"])
            ):
                raise ValueError("Invalid legacy timing history")
        except (ValueError, TypeError, KeyError):
            raise AdapterError(
                "Invalid legacy FRSE pacing state", "access"
            ) from None
        with locked_budget():
            state = load_budget()
            state["interval"] = max(
                state.get("interval", 6), legacy["interval"]
            )
            state["not_before"] = max(state["not_before"], legacy["until"])
            state["blocked"] = state["blocked"] or legacy.get("blocked", False)
            state["starts"] = sorted(set(state["starts"] + legacy["starts"]))[
                -10:
            ]
            if state["starts"]:
                state["not_before"] = max(
                    state["not_before"], state["starts"][-1] + state["interval"]
                )
            state["observed_at"] = time.time()
            save_budget(state)

    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


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
        state["not_before"] = max(state["not_before"], _COLLECTION_STARTED + 60)
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

    validate_owned_records(records)


def profile_identity(profile):
    """Bind original URL and, for jobs, the publisher's literal reference."""
    identity = profile["url"]
    if profile["key"].startswith("job-FRSE-"):
        reference = profile["key"].removeprefix("job-").replace("-", "/")
        identity += "|" + reference
    return hashlib.sha256((SOURCE_ID + "|" + identity).encode()).hexdigest()[
        :24
    ]


def validate_owned_records(records):
    """Reject foreign identities and impossible owned record chronologies."""
    profiles = {profile_identity(profile): profile for profile in PROFILES}
    for record in records:
        profile = profiles.get(record["id"])
        if profile is None or record["url"] != profile["url"]:
            raise AdapterError(
                "Unowned FRSE record identity or URL", "validate"
            )
        values = {
            field: datetime.fromisoformat(record[field].replace("Z", "+00:00"))
            for field in (
                "created_at",
                "updated_at",
                "first_seen_at",
                "last_seen_at",
                "last_checked_at",
            )
        }
        if not (
            values["created_at"]
            <= values["updated_at"]
            <= values["last_checked_at"]
            and values["first_seen_at"]
            <= values["last_seen_at"]
            <= values["last_checked_at"]
        ):
            raise AdapterError("Invalid owned FRSE chronology", "validate")
        if (
            record["status"] == "expired"
            and record["deadline"] is None
            and not (
                profile["status"] == "expired"
                and profile["kind"] == "institutional-grant"
                and "institutional competition closed" in profile["proof"]
            )
        ):
            raise AdapterError("Missing guarded FRSE call closure", "validate")


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
    parsed = urllib.parse.urlsplit(value)
    clean = urllib.parse.urlunsplit(parsed._replace(fragment=""))
    allowed = {info["url"] for info in INPUTS.values()} | {
        origin + "/robots.txt" for origin in ROBOTS_ORIGINS
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


def newsletter_token(root, key):
    """Validate the sole observed newsletter token before canonicalizing it."""
    tokens = [
        node for node in root.walk() if node.attrs.get("name") == "_token"
    ]
    if key != "etwinning-policy":
        if tokens:
            raise AdapterError("Unreviewed newsletter token context", "parse")
        return None
    forms = [
        node
        for node in nodes(root, "form")
        if "myform2__regnewsletter_form" in node.attrs.get("class", "").split()
    ]
    if len(tokens) != 1 or len(forms) != 1:
        raise AdapterError("Changed newsletter token cardinality", "parse")
    token, form = tokens[0], forms[0]
    if (
        form.duplicate_attrs
        or token.duplicate_attrs
        or form.attrs
        != {
            "class": (
                "myform2__regnewsletter_form " "myform2__regnewsletter_form_s"
            ),
            "method": "post",
        }
        or not form.children
        or form.children[0] is not token
        or token.tag != "input"
        or token.children
        or set(token.attrs) != {"type", "name", "value"}
        or token.attrs.get("type") != "hidden"
        or not isinstance(token.attrs.get("value"), str)
        or not re.fullmatch(r"[A-Za-z0-9]{40}", token.attrs["value"])
    ):
        raise AdapterError("Changed newsletter token structure", "parse")

    def structure(node):
        return {
            "duplicate_attributes": node.duplicate_attrs,
            "tag": node.tag,
            "attribute_names": sorted(node.attrs),
            "children": [
                structure(child) if isinstance(child, Node) else child
                for child in node.children
            ],
        }

    token.newsletter_structure = structure(form)
    return token


def html_fingerprint(body, key=None):
    """Bind complete visible material, literal links and frontier controls."""
    root = document_root(body.decode("utf-8", "strict"))
    token = newsletter_token(root, key)
    links = sorted(
        {
            (node.tag, node.attrs.get("rel", ""), node.attrs["href"])
            for tag in ("a", "link")
            for node in nodes(root, tag)
            if node.attrs.get("href")
        }
    )
    controls = []
    if token is not None:
        controls.append(
            ("reviewed-newsletter-structure", token.newsletter_structure)
        )
    for node in root.walk():
        attrs = {
            name: value
            for name, value in node.attrs.items()
            if name
            in {
                "action",
                "method",
                "name",
                "value",
                "selected",
                "data-pagescount",
                "data-activepage",
                "data-active-page",
            }
        }
        if node is token:
            attrs["value"] = "reviewed-newsletter-csrf-token"
        if attrs:
            controls.append((node.tag, sorted(attrs.items())))
    facts = json.dumps(
        {"text": root.text(), "links": links, "controls": controls},
        ensure_ascii=False,
        separators=(",", ":"),
    )
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
    """Exhaust all eighty-eight required inputs in their reviewed order."""
    return {key: read_input(key) for key in INPUTS}


def parse_inventory(pages):
    """Normalize only the complete guarded fifty-three-identity partition."""
    if set(pages) != set(INPUTS) or any(
        pages[key] != info["sha256"] for key, info in INPUTS.items()
    ):
        raise AdapterError("Incomplete reviewed FRSE frontier", "parse")
    records = []
    now = datetime.now(timezone.utc)
    for profile in PROFILES:
        record = make_record(
            profile["title"],
            profile["url"],
            profile["categories"],
            kind=profile["kind"],
            host_countries=profile["hosts"],
            evidence=[ATTRIBUTION, profile["proof"]]
            + [INPUTS[key]["url"] for key in profile["inputs"]],
        )
        record["id"] = profile_identity(profile)
        record["summary"] = profile["summary"]
        record["country"] = PUBLISHER_COUNTRY
        record["deadline"] = profile["deadline"]
        record["location"] = profile["location"]
        record["status"] = profile["status"]
        deadline = profile["deadline"]
        if deadline is not None:
            if len(deadline) == 10:
                if now.date().isoformat() > deadline:
                    record["status"] = "expired"
                elif now.date().isoformat() == deadline:
                    record["status"] = "unknown"
            elif now >= datetime.fromisoformat(deadline.replace("Z", "+00:00")):
                record["status"] = "expired"
        records.append(record)
    validate_records(records)
    if len(records) != 53:
        raise AdapterError("Incomplete reviewed identity partition", "validate")
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
        "name": "FRSE — funding, training, jobs and awards",
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
            else f"Collected {len(records)} reviewed FRSE opportunities"
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
        or previous.get("name") != "FRSE — funding, training, jobs and awards"
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


ROBOTS_ORIGINS = [
    "https://eduinspiracje.org.pl",
    "https://eks.org.pl",
    "https://erasmusplus.org.pl",
    "https://etwinning.pl",
    "https://selfieplus.frse.org.pl",
    "https://www.frse.org.pl",
]

INPUTS = {
    "funding-teach": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/w"
            "yjedz-prowadzic-zajecia"
        ),
        "format": "html",
        "sha256": (
            "5891c581b1d4d4e0b4de8b7cb790c9687091c0431271c11f"
            "1adf3adc3b390393"
        ),
    },
    "funding-study": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/s"
            "tudiuj-w-europie"
        ),
        "format": "html",
        "sha256": (
            "23a97021581aa6d798d7bafb8d29cb9a8f81639008bf6e5f"
            "0aa2f71a04ec2ec3"
        ),
    },
    "funding-03": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/w"
            "yjedz-na-szkolenie-kadry-lub-z-uczniami-na-zajec"
            "ia"
        ),
        "format": "html",
        "sha256": (
            "a80c115102d4824858e58c93e4e59f4e9105cbc4a6546cd7"
            "9694f463565222bc"
        ),
    },
    "funding-04": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/t"
            "woja-szkola-w-partnerstwie-z-europa"
        ),
        "format": "html",
        "sha256": (
            "2b50a7a8cdcd995eada78be73e36faec72f5ce514cb0a7f2"
            "63221777575c474b"
        ),
    },
    "funding-05": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/z"
            "mieniaj-edukacje"
        ),
        "format": "html",
        "sha256": (
            "3c5a931b384f36de133d33d1926a40bf6ca43354e60c67ae"
            "db197b7581f63d69"
        ),
    },
    "funding-06": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/u"
            "czniowie-na-zagranicznych-praktykach"
        ),
        "format": "html",
        "sha256": (
            "baaf31dd85b051ab4f5345187c73c9366c779b1220ff55f1"
            "94b5567d488982d8"
        ),
    },
    "funding-07": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/z"
            "agraniczne-wyjazdy-doskonalenia-kadry"
        ),
        "format": "html",
        "sha256": (
            "124c55fa24453599b4065cff76d990deb9b528e93cb001f1"
            "ecd256375cc52fda"
        ),
    },
    "funding-08": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/r"
            "ozwoj-instytucji-ksztalcenia-zawodowego"
        ),
        "format": "html",
        "sha256": (
            "cf2d8eb67ae875709e96dc2468ff1e5fd14e12349738fd1e"
            "6221e327f5ba7154"
        ),
    },
    "funding-09": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/m"
            "obilnosci-kadry-sportowej"
        ),
        "format": "html",
        "sha256": (
            "4fe1621fdfafc658fdfd1580dc139326ff7f1e68c2aa02af"
            "7a5414cf2c32cbb2"
        ),
    },
    "funding-10": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/m"
            "iedzynarodowe-partnerstwa-w-dziedzinie-sportu"
        ),
        "format": "html",
        "sha256": (
            "8566d5f516e38d64d8190b578ff3d99831a4858d2170a204"
            "6034c2ef70c64878"
        ),
    },
    "funding-11": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/s"
            "ojusze-na-rzecz-edukacji-i-przedsiebiorstw"
        ),
        "format": "html",
        "sha256": (
            "8492435671bee1a57a7adb98dd5144679069f40d6408e667"
            "4746a253f7cea2d7"
        ),
    },
    "funding-12": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/s"
            "ojusze-na-rzecz-wspolpracy-sektorowej"
        ),
        "format": "html",
        "sha256": (
            "4688abf8565b8cb345cdb847084638a75adb717c261a4bd8"
            "65a9ee6017489a11"
        ),
    },
    "funding-13": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/d"
            "zialania-jean-monnet-nauczanie-i-badania"
        ),
        "format": "html",
        "sha256": (
            "8c65d574a21c532c2eef2bfe9ce7071dd833ffd74eda4a01"
            "c977ea99e3027012"
        ),
    },
    "funding-14": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/d"
            "zialania-jean-monnet-debata-nt-polityk"
        ),
        "format": "html",
        "sha256": (
            "0014f2ea3bfeb8834ab874bba9f670f408b3f1bb58e0d75a"
            "03b6f2ce16d33d3c"
        ),
    },
    "funding-15": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/b"
            "udowanie-potencjalu-w-szkolnictwie-wyzszym"
        ),
        "format": "html",
        "sha256": (
            "709d72d1b48f71b42ee9484b2d587b66f58e5617941fd98b"
            "f03e94c152648180"
        ),
    },
    "funding-16": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/e"
            "rasmus-mundus-joint-masters"
        ),
        "format": "html",
        "sha256": (
            "2739cdfb7e47101f7d5f5460b65c874d1d374376d0ba1ea6"
            "bad67a6c01b53703"
        ),
    },
    "funding-17": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/e"
            "rasmus-mundus-design-measures"
        ),
        "format": "html",
        "sha256": (
            "2703b17ac57028f10b5024a73c52fb4da9e993fa12bccdb8"
            "033ad803b50d37d4"
        ),
    },
    "funding-18": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/e"
            "dukacja-przez-cale-zycie"
        ),
        "format": "html",
        "sha256": (
            "0e2db0b4ae267761bda08fa5b07a6daa85ef6f3e93c31d70"
            "29ed3c54f53dd80c"
        ),
    },
    "funding-19": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/r"
            "ozwin-metody-pracy-z-doroslymi"
        ),
        "format": "html",
        "sha256": (
            "d71cbe6ad4526bd5806147e8ae89bde6b01ef6e5b860a5be"
            "8c1c82b0921a089e"
        ),
    },
    "funding-20": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/w"
            "ymiany-mlodziezowe-wspolpraca-poprzez-organizacj"
            "e-spotkan"
        ),
        "format": "html",
        "sha256": (
            "f2fda955790113f86b76eb21a9f1bd666202ba5f78f492a5"
            "ee75ed9e056479da"
        ),
    },
    "funding-21": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/m"
            "lodziezowe-spotkania-debaty-kampanie-w-kraju-i-z"
            "a-granica"
        ),
        "format": "html",
        "sha256": (
            "43d7f9a30812d04561d1d1ad001cb007c6d3f4df2dba196f"
            "432d050f7754f965"
        ),
    },
    "funding-22": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/w"
            "spolpracuj-z-innymi-na-rzecz-mlodziezy"
        ),
        "format": "html",
        "sha256": (
            "02ad825966d6e58477e649ea2a9426662d5471a46bf180e9"
            "969138cf2c445b25"
        ),
    },
    "funding-23": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/s"
            "tworz-branzowe-centrum-umiejetnosci"
        ),
        "format": "html",
        "sha256": (
            "7c9d757bc5203aeca9c2d6577b4a700714f5aa335dca8e21"
            "2f1ad0f21e06b3b6"
        ),
    },
    "funding-24": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/k"
            "oordynuj-regionalne-dzialania-lll"
        ),
        "format": "html",
        "sha256": (
            "1835f40084ad6136e3d44e060c78d0f9ce3c8e318cc23340"
            "c5f6827b150b0927"
        ),
    },
    "funding-25": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/z"
            "miana-ktora-ty-tworzysz"
        ),
        "format": "html",
        "sha256": (
            "2735835bc1bda122d13afe9cbc60c04335fbe18023202bb1"
            "b562fca58ee30a70"
        ),
    },
    "funding-26": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/z"
            "miana-ktora-pomagasz-tworzyc"
        ),
        "format": "html",
        "sha256": (
            "e28ebbb4fb30214e94d7fdf8cedd022b8164444d5058a82b"
            "801988aeb4725913"
        ),
    },
    "funding-27": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/z"
            "ostan-wolontariuszem-badz-solidarny"
        ),
        "format": "html",
        "sha256": (
            "92884a1a52ef7ac192ea5f29e17bffba95e91b60d78f8881"
            "f2b779ee75bd19e5"
        ),
    },
    "funding-28": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/z"
            "organizuj-wolontariat-wzmocnij-swoja-organizacje"
        ),
        "format": "html",
        "sha256": (
            "dfab56caf73f4915bf8990496e085e1c1cb6ecbe93ef2b4f"
            "5831c66ffe4f595a"
        ),
    },
    "funding-29": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/o"
            "rganizuj-wyjazdy-z-mlodzieza-z-litwy"
        ),
        "format": "html",
        "sha256": (
            "2987414d839c19219d164a6b066a4f3f98c7a1937aba7d06"
            "21a545c29c6ad2d9"
        ),
    },
    "funding-30": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/o"
            "rganizuj-miedzynarodowe-wydarzenia"
        ),
        "format": "html",
        "sha256": (
            "be46ad5a498ea5f3b374dee0002a594b9490e36feac92b1a"
            "9550e365f9ed18c1"
        ),
    },
    "training-02": {
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/w"
            "ebinarium-powrot-szwajcarii-do-programu-erasmus"
        ),
        "format": "html",
        "sha256": (
            "cb09dac83dba4a88a6ac1365d21fdf4b734e86e6bd5d1c4f"
            "7828aa257fcb739a"
        ),
    },
    "training-03": {
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/w"
            "ebinarium-powrot-wielkiej-brytanii-do-erasmusa-s"
            "ektor-mlodziez"
        ),
        "format": "html",
        "sha256": (
            "4029595595535167f54ed68f2b06d05e267daf2f67cdc027"
            "70f5712de3cccf43"
        ),
    },
    "training-04": {
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/r"
            "pi-slupsk-konferencja-erasmus-zmienia-perspektyw"
            "e"
        ),
        "format": "html",
        "sha256": (
            "3fa80df691cb09972f93940808579a5e5dcfcb1d0114e0d4"
            "00330c99fb7a1de9"
        ),
    },
    "training-05": {
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/e"
            "urolekcje-eurodesk"
        ),
        "format": "html",
        "sha256": (
            "e7f93538bfc86ee5c4d1faa0280a825a69ee2499b4a5405f"
            "b8bbca68ec7128a4"
        ),
    },
    "training-06": {
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/k"
            "ursy-internetowe-etwinning"
        ),
        "format": "html",
        "sha256": (
            "466983204e0e69b59f1dbf7a78093bdaf31a52ce077fe7ce"
            "1b246333abc4cf6d"
        ),
    },
    "training-07": {
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/w"
            "ebinaria-etwinning"
        ),
        "format": "html",
        "sha256": (
            "e7d5cdf6c89a1d1d10a73e77c261ce41f877326fb9f6b9c1"
            "aed09a40830a9d7d"
        ),
    },
    "training-11": {
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/f"
            "orum-edukacji-doroslych"
        ),
        "format": "html",
        "sha256": (
            "58b84cb6830b85c06a13b4d4e9d929134bcc04e91878f6a5"
            "4e1a59d9c409e9d9"
        ),
    },
    "training-13": {
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/k" "ongres-edukacji"
        ),
        "format": "html",
        "sha256": (
            "64802afaa9a74a3d67a944d331171b1684fc6604fc915fc0"
            "516f37cf6f020a93"
        ),
    },
    "training-14": {
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/m"
            "obilne-centrum-edukacyjne-frse"
        ),
        "format": "html",
        "sha256": (
            "fb908f17daed0b23042fadf7ecd43bae6b5b33170ef976ca"
            "b73086339586e5a0"
        ),
    },
    "training-15": {
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/o"
            "golnopolski-dzien-informacyjny-frse"
        ),
        "format": "html",
        "sha256": (
            "ed0781fd201f3f380ebf9d4ed6cf93f56c4079dd98953bff"
            "281a18e87916591a"
        ),
    },
    "training-17": {
        "url": "https://www.frse.org.pl/wydarzenia-i-szkolenia/tool-fair",
        "format": "html",
        "sha256": (
            "d038fda656fadf5470746b424d3bc772ab0f79d78c812eb9"
            "cba89a14ad399753"
        ),
    },
    "training-20": {
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/t"
            "edxwarsaw-youth-2026"
        ),
        "format": "html",
        "sha256": (
            "2411b461914b576095747a9733cfe7be19ab8151fe964f08"
            "ec9526247e091320"
        ),
    },
    "training-21": {
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/w"
            "ebinarium-erasmus-dzialania-jean-monnet"
        ),
        "format": "html",
        "sha256": (
            "59ff0b167a571115f280a4d6f52ccce73389efd5ea46c3c1"
            "371ac542ddf0c1e7"
        ),
    },
    "training-22": {
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/w"
            "ebinarium-dzialania-erasmus-mundus"
        ),
        "format": "html",
        "sha256": (
            "eef787ae3728439c86e44dab2032b765dd8a073d3c1352cf"
            "4e608dad373c19e1"
        ),
    },
    "funding": {
        "url": "https://www.frse.org.pl/finansowanie-projektow",
        "format": "html",
        "sha256": (
            "d79ec32f467b726b41773411105cf4e792b51091ba043975"
            "65886cbd27351ed8"
        ),
    },
    "funding-history-page2": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow?f"
            "ilter_for_who=0&filter_string=&filter_send=filtr"
            "uj&active_page=2"
        ),
        "format": "html",
        "sha256": (
            "841abf351ab1428912bbd024e00ae07c298ca11e5a70f110"
            "0c92b1a0cdf7f2d2"
        ),
    },
    "funding-history-page3": {
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow?f"
            "ilter_for_who=0&filter_string=&filter_send=filtr"
            "uj&active_page=3"
        ),
        "format": "html",
        "sha256": (
            "fd8258135e49bd9218d0b0aa9f9b9d59a866f7226804ccd5"
            "5f000d1030720a00"
        ),
    },
    "contests": {
        "url": "https://www.frse.org.pl/konkursy",
        "format": "html",
        "sha256": (
            "cd9b7b004b120a1e6341f50e1966bd9ff23441d9068e7d90"
            "505cd116ea1351b3"
        ),
    },
    "contests-history-page2": {
        "url": (
            "https://www.frse.org.pl/konkursy?filter_for_who="
            "0&filter_string=&filter_send=filtruj&active_page"
            "=2"
        ),
        "format": "html",
        "sha256": (
            "7519b232c333e18dea38c813738e7f9aaa9ae52708558b91"
            "07110a595cdfe7fa"
        ),
    },
    "events": {
        "url": "https://www.frse.org.pl/wydarzenia-i-szkolenia",
        "format": "html",
        "sha256": (
            "40d14282f1c8b2c9ae4bfae1f77cc96a2ff3c7db9a5269b5"
            "a1aa95ceaabf17b4"
        ),
    },
    "events-history-page2": {
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia?s"
            "_filter_for_who=0&s_filter_terms=0&s_filter_stri"
            "ng=&s_filter_send=filtruj&active_page=2"
        ),
        "format": "html",
        "sha256": (
            "2ee6dc6b0080eb17099923a5763e2cde2ad7eb2a45b920bf"
            "6150b37cf6dfed12"
        ),
    },
    "events-history-page3": {
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia?s"
            "_filter_for_who=0&s_filter_terms=0&s_filter_stri"
            "ng=&s_filter_send=filtruj&active_page=3"
        ),
        "format": "html",
        "sha256": (
            "092f3d0d2f65763a9f1a5e88872562f9195e1c197e18a8c9"
            "ee358abcff944418"
        ),
    },
    "jobs": {
        "url": "https://www.frse.org.pl/praca-w-frse",
        "format": "html",
        "sha256": (
            "37b9f410fa23aa71ea912209e0d96a2d5b97ae9baea2dba8"
            "6202fe761bfbd287"
        ),
    },
    "discover": {
        "url": (
            "https://www.frse.org.pl/aktualnosci/konkurs-disc"
            "overeu-ruszyla-runda-jesienna"
        ),
        "format": "html",
        "sha256": (
            "57743467e2db85dc0407337a127b06ff761eb9398dfde3e1"
            "ba22100bd565a640"
        ),
    },
    "etwinning-call": {
        "url": (
            "https://www.frse.org.pl/aktualnosci/konkurs%C2%A"
            "0nasz-projekt-etwinning-2027"
        ),
        "format": "html",
        "sha256": (
            "b4122e1312e1f451fc752417545a00486bb5279da011c6f1"
            "3345c46d96bd767a"
        ),
    },
    "professional-school": {
        "url": (
            "https://www.frse.org.pl/aktualnosci/nabor-uzupel"
            "niajacy-profesjonalna-szkola-roku"
        ),
        "format": "html",
        "sha256": (
            "97dce4fc0d0022f9e6b324448a6f7621df7ca31509761007"
            "85517afc7a3c9359"
        ),
    },
    "professional-school-hub": {
        "url": "https://www.frse.org.pl/profesjonalna-szkola-roku",
        "format": "html",
        "sha256": (
            "f5dc42de7151eb1d786f6bf9682bf8ec8103b32226dcd938"
            "ee5b2ae301f676f8"
        ),
    },
    "professional-school-regulations": {
        "url": (
            "https://www.frse.org.pl/brepo/panel_repo_files/2"
            "026/08/27/zj6fq8/psr-regulamin-konkurs-uzupelnia"
            "jacy.pdf"
        ),
        "format": "pdf",
        "sha256": (
            "61ee04fde91614566053070b655363ec37e24d6d0c391b51"
            "7ec47607a30bcfae"
        ),
    },
    "etwinning-regulations": {
        "url": (
            "https://etwinning.pl/brepo/panel_repo_files/2026"
            "/09/26/slsxob/regulamin-konkursu-nasz-projekt-et"
            "winning.pdf"
        ),
        "format": "pdf",
        "sha256": (
            "c577fa213cdadc05bbf43a0162d1c1425f0ba54b3565dcec"
            "9e2ccb88e68eebfe"
        ),
    },
    "etwinning-policy": {
        "url": "https://etwinning.pl/klauzula-rodo",
        "format": "html",
        "sha256": (
            "30e804cac14e720b68560c19b5c3c1d5cad9a464668bd4ac"
            "d2de9a3c4e51eb18"
        ),
    },
    "erasmus-call-pdf": {
        "url": (
            "https://www.frse.org.pl/brepo/panel_repo_files/2"
            "025/11/13/ctajct/oj-c-202506080-pl-txt.pdf"
        ),
        "format": "pdf",
        "sha256": (
            "f49ccf5c4761928d762bd7861327f94ea8f17a88bbebebef"
            "44574a371e6210d0"
        ),
    },
    "erasmus-documents": {
        "url": "https://erasmusplus.org.pl/dokumenty",
        "format": "html",
        "sha256": (
            "78df90a75713a81cd9f14d969a20fd2da33953d89bbe8167"
            "5558282c13ae7e9d"
        ),
    },
    "erasmus-guide2026": {
        "url": (
            "https://erasmusplus.org.pl/brepo/panel_repo_file"
            "s/2025/11/13/n1naeo/programme-guide-2026-pl.pdf"
        ),
        "format": "pdf",
        "sha256": (
            "f7eec2fe597cbb9afcda01019e58ec51ce256b9cd55105d5"
            "7bb44acbc0dad7f4"
        ),
    },
    "erasmus-guide2026-en": {
        "url": (
            "https://erasmusplus.org.pl/brepo/panel_repo_file"
            "s/2025/11/13/xrqjug/programme-guide-2026-en.pdf"
        ),
        "format": "pdf",
        "sha256": (
            "8f14f67fc83d5081e7e85744ff39ae855aef34af8c842d22"
            "ba8c52e464c331a0"
        ),
    },
    "erasmus-policy": {
        "url": (
            "https://erasmusplus.org.pl/ochrona-danych-osobow"
            "ych-w-programie-erasmus-2021-2027"
        ),
        "format": "html",
        "sha256": (
            "cc11877f254b88ad93715592d853dcd45bbc7b3fb5b5ca6e"
            "0f9ace4ff9cad40b"
        ),
    },
    "eks-documents": {
        "url": "https://eks.org.pl/dokumenty",
        "format": "html",
        "sha256": (
            "98e4f064e3a4875e14d0ced0b9c33d20f2d46a073fab0fb3"
            "ae87ff44e719e35a"
        ),
    },
    "eks-guide2026": {
        "url": (
            "https://eks.org.pl/brepo/panel_repo_files/2025/1"
            "1/18/wi9dq2/european-solidarity-corps-guide-2026"
            "-pl.pdf"
        ),
        "format": "pdf",
        "sha256": (
            "04162b445649ed5130e526900a60364a8817874d78691189"
            "8e8d200ba83807d4"
        ),
    },
    "eks-cookies": {
        "url": "https://eks.org.pl/pliki-cookies",
        "format": "html",
        "sha256": (
            "e68229644ee9f8251023c806fdb62a93ac5be64199f90de8"
            "37de18067d9454dd"
        ),
    },
    "bcu-call3": {
        "url": "https://www.frse.org.pl/kpo-bcu-wnioskowanie-konkurs-iii",
        "format": "html",
        "sha256": (
            "2a6ad33aef2397f04db24cf3ca2683eb47e1ca82bce3d41f"
            "28ad2cff0b89016c"
        ),
    },
    "bcu-reglatest": {
        "url": (
            "https://www.frse.org.pl/brepo/panel_repo_files/2"
            "025/01/23/iyb7uu/regulamin-konkursu-utworzenie-i"
            "-wsparcie-funkcjono.pdf"
        ),
        "format": "pdf",
        "sha256": (
            "3bcd5c725d29a40c7693272c866daace2c0e65baf64fa0ed"
            "5de1b57a0669b6cf"
        ),
    },
    "bcu-amendments": {
        "url": (
            "https://www.frse.org.pl/brepo/panel_repo_files/2"
            "025/01/23/embgsr/kpo-bcu-zestawienie-zmian-w-reg"
            "ulaminie-konkursu-i.pdf"
        ),
        "format": "pdf",
        "sha256": (
            "1585913255af7c29e57b9caf3012cf3b8aa5a8cbf4afdf0a"
            "2d8ff15e695a7513"
        ),
    },
    "selfie2025": {
        "url": "https://selfieplus.frse.org.pl/konkurs-2025",
        "format": "html",
        "sha256": (
            "505b70c45c8d8628be45ad0bc45e991e26048c44782ed20b"
            "f0901265cc3ca675"
        ),
    },
    "selfie-reg2025": {
        "url": (
            "https://selfieplus.frse.org.pl/brepo/panel_repo_"
            "files/2025/05/14/svbrks/selfie-regulamin-2025.pd"
            "f"
        ),
        "format": "pdf",
        "sha256": (
            "147aaed0912e67f1e76ee266f3f585a42aa9a4084f769b9d"
            "3c5382864af84484"
        ),
    },
    "selfie-cookies": {
        "url": "https://selfieplus.frse.org.pl/pliki-cookies",
        "format": "html",
        "sha256": (
            "837ad1e0a6f7a8a58d3004a159a99a5da3167273d494244c"
            "0b356d45256d7292"
        ),
    },
    "edu-inspirator-rules": {
        "url": "https://eduinspiracje.org.pl/eduinspirator/regulamin",
        "format": "html",
        "sha256": (
            "d34303bd4a8d2c456419466047c0c97a8c1b7a36870ac7ba"
            "153fc0f8c3cffd18"
        ),
    },
    "edu-inspirator-reg2025": {
        "url": (
            "https://eduinspiracje.org.pl/brepo/panel_repo_fi"
            "les/2025/08/26/xcc1pd/regulamin-eduinspirator-20"
            "25.pdf"
        ),
        "format": "pdf",
        "sha256": (
            "b04940e40f4e44fbacf61346bcd0adf90cce8a3a0608d1b4"
            "2cb1abebf7025d33"
        ),
    },
    "edu-rodo": {
        "url": "https://eduinspiracje.org.pl/rodo",
        "format": "html",
        "sha256": (
            "5684c28baf6592ac679b14908fa1c421c91e321e38a3986a"
            "815a4ca166f64ea3"
        ),
    },
    "edu-cookies": {
        "url": "https://eduinspiracje.org.pl/pliki-cookies",
        "format": "html",
        "sha256": (
            "eb0566c232e72787338410f1b0e3b7dcf649b6604fccd43e"
            "2054751785b14ad8"
        ),
    },
    "publishing-call": {
        "url": "https://www.frse.org.pl/wydawnictwo/obwieszczenie-o-konkursie",
        "format": "html",
        "sha256": (
            "cda7303f508024561ee5b02b7fd68c03ea2fe46d32ec284d"
            "4b53f92dd67e58a1"
        ),
    },
    "publishing-reg": {
        "url": "https://www.frse.org.pl/wydawnictwo/regulamin",
        "format": "html",
        "sha256": (
            "84e5350547bb1cf51e72000b667d2d547762fd50dab6a2d6"
            "f86bc289017223db"
        ),
    },
    "publishing-standards": {
        "url": (
            "https://www.frse.org.pl/brepo/panel_repo_files/2"
            "026/02/20/e2zdqx/standardy-techniczne-2026.pdf"
        ),
        "format": "pdf",
        "sha256": (
            "56ed2775bbb9f7d82b784911e282c3a5b354ca7f436d8b76"
            "0a8ca0b45a2ef510"
        ),
    },
    "publishing-ethics": {
        "url": (
            "https://www.frse.org.pl/brepo/panel_repo_files/2"
            "026/02/20/igfxwm/zasady-etyki-publikacyjnej-2026"
            ".pdf"
        ),
        "format": "pdf",
        "sha256": (
            "b366062caab547765d74296191f2c8a478eef1882807fa78"
            "2b099aa5d5aec93a"
        ),
    },
    "publishing-review": {
        "url": (
            "https://www.frse.org.pl/brepo/panel_repo_files/2"
            "026/02/20/ry7rgc/procedura-recenzowania-2026.pdf"
        ),
        "format": "pdf",
        "sha256": (
            "b7d529cad93dbd127e0ce4d3a49579b60733a4aad6dd2912"
            "04ac39abe4055e50"
        ),
    },
    "publishing-production": {
        "url": (
            "https://www.frse.org.pl/brepo/panel_repo_files/2"
            "026/02/20/kxkni2/procedury-wydawnicze-monografii"
            "-naukowych-2026.pdf"
        ),
        "format": "pdf",
        "sha256": (
            "1eecb433c938a938db5649ef53a6415d971cb198fb9cefbf"
            "a9ac740121ba8f3f"
        ),
    },
    "slupsk-programme": {
        "url": (
            "https://www.frse.org.pl/brepo/panel_repo_files/2"
            "026/10/06/0gzurw/program-rpi-slupsk-2026-10-15.p"
            "df"
        ),
        "format": "pdf",
        "sha256": (
            "44f059391ca8d71566a106b6c6a1fd008b05205858c081bc"
            "77363152088e221c"
        ),
    },
    "data-policy": {
        "url": "https://www.frse.org.pl/rodo",
        "format": "html",
        "sha256": (
            "2249b2221d1ef20fd69dc20473c515b091af1ee8eb521aa3"
            "719da48e3f6bd0c6"
        ),
    },
    "cookies": {
        "url": "https://www.frse.org.pl/pliki-cookies",
        "format": "html",
        "sha256": (
            "4e19a35e762f2c04975141ce8c5f78c165e8b2ed0d996671"
            "62fbb27b124a518d"
        ),
    },
    "uk-webinar": {
        "url": (
            "https://www.frse.org.pl/aktualnosci/webinarium-p"
            "owrot-wielkiej-brytanii-do-erasmusa-sektor-mlodz"
            "iez"
        ),
        "format": "html",
        "sha256": (
            "bd0844829db8c3bc5e84428399bcc1c101316e0b767dbe45"
            "ffb03ebcece19050"
        ),
    },
}

PROFILES = [
    {
        "key": "funding-01",
        "title": "Wyjedź prowadzić zajęcia!",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/w"
            "yjedz-prowadzic-zajecia"
        ),
        "categories": ["training"],
        "kind": "programme-overview",
        "status": "unknown",
        "summary": "Mobilność kadry uczelni: zajęcia lub szkolenie za granicą. "
        "Wniosek instytucjonalny składa uczelnia z ECHE, pracownik "
        "zgłasza się w macierzystej uczelni. W UE i krajach "
        "stowarzyszonych zwykle 2–60 dni; zaproszeni pracownicy "
        "przedsiębiorstw od 1 dnia, mobilność z krajami "
        "niestowarzyszonymi od 5 dni. Dofinansowanie podróży i "
        "utrzymania zależy od kraju i długości wyjazdu. Nabór "
        "instytucjonalny 2026 zakończony; nie jest to termin rekrutacji "
        "pracowników.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "No literal participant application deadline; do not use "
        "institutional or event dates.",
        "inputs": [
            "funding-teach",
            "erasmus-guide2026-en",
            "erasmus-guide2026",
            "erasmus-call-pdf",
        ],
    },
    {
        "key": "funding-02",
        "title": "Studiuj w Europie!",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/s"
            "tudiuj-w-europie"
        ),
        "categories": ["scholarships", "internships"],
        "kind": "programme-overview",
        "status": "unknown",
        "summary": "Dla studentów uczelni uczestniczących w Erasmus+"
        ": studia lub "
        "praktyki za granicą, zwykle 2–12 miesięcy; krótka mobilność "
        "mieszana 5–30 dni. Limit 12 miesięcy na cykl studiów, 24 na "
        "studiach jednolitych, obejmuje wcześniejsze wyjazdy. "
        "Absolwentów na praktyki wybiera uczelnia przed ukończeniem "
        "studiów; praktyka do roku po dyplomie. Stypendium zależy od "
        "kraju i czasu, możliwe wsparcie osób z mniejszymi szansami. "
        "Nabór uczelni 2026 zamknięty, rekrutację osób ustala uczelnia.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "No literal participant application deadline; do not use "
        "institutional or event dates.",
        "inputs": [
            "funding-study",
            "erasmus-guide2026-en",
            "erasmus-guide2026",
            "erasmus-call-pdf",
        ],
    },
    {
        "key": "funding-03",
        "title": "Wyjedź na szkolenie kadry lub z uczniami na zajęcia",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/w"
            "yjedz-na-szkolenie-kadry-lub-z-uczniami-na-zajec"
            "ia"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Mobilność uczniów i kadry edukacji szkolnej. Wni"
        "oskować mogą "
        "uprawnione polskie przedszkola, szkoły i inne instytucje "
        "sektora, przez projekt krótkoterminowy, akredytację lub "
        "konsorcjum. Wsparcie obejmuje podróż, utrzymanie i organizację "
        "działań; koszty zależą od formatu i kraju. Projekty "
        "krótkoterminowe trwają 6–18 miesięcy. Udział osób organizuje "
        "placówka; zakończony nabór instytucjonalny 2026 nie wyznacza "
        "ich indywidualnego terminu.",
        "deadline": "2026-02-19T11:00:00Z",
        "hosts": [],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure.",
        "inputs": [
            "funding-03",
            "erasmus-guide2026-en",
            "erasmus-guide2026",
            "erasmus-call-pdf",
        ],
    },
    {
        "key": "funding-04",
        "title": "Twoja szkoła w partnerstwie z Europą",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/t"
            "woja-szkola-w-partnerstwie-z-europa"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Granty dla instytucji edukacji szkolnej i partnerów: "
        "partnerstwa współpracy lub na małą skalę, bez wymogu "
        "akredytacji Erasmus+. Standardowe partnerstwo wymaga co "
        "najmniej 3 organizacji z 3 krajów UE/stowarzyszonych; małe co "
        "najmniej 2 z 2 krajów. Ryczałty na cały projekt: 120, 250 lub "
        "400 tys. EUR na 12–36 miesięcy albo 30 lub 60 tys. EUR na 6–24 "
        "miesiące. Nie są to stypendia osobowe. Nabór 2026 zakończony; "
        "szczegółowe warunki określa aktualny przewodnik.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure. Multiple national/central "
        "variants differ in clock/routes: no universal precise deadline.",
        "inputs": [
            "funding-04",
            "erasmus-guide2026-en",
            "erasmus-guide2026",
            "erasmus-call-pdf",
        ],
    },
    {
        "key": "funding-05",
        "title": "Zmieniaj edukację!",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/z"
            "mieniaj-edukacje"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Europejskie partnerstwa rozwoju szkół: koordynat"
        "or to lokalny "
        "lub regionalny organ edukacji albo organ koordynujący szkoły z "
        "UE/kraju stowarzyszonego. Co najmniej 6 organizacji; w kraju "
        "koordynatora i drugim kraju po organie oraz 2 szkoły. Projekt "
        "musi objąć minimum 3 zadania, w tym po jednym z obu kategorii "
        "działań. Grant ryczałtowy 400 tys. EUR na 36 miesięcy dla "
        "projektu. Nabór 2026 zamknięty.",
        "deadline": "2026-04-09T10:00:00Z",
        "hosts": [],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure.",
        "inputs": [
            "funding-05",
            "erasmus-guide2026-en",
            "erasmus-guide2026",
            "erasmus-call-pdf",
        ],
    },
    {
        "key": "funding-06",
        "title": "Uczniowie na zagranicznych praktykach",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/u"
            "czniowie-na-zagranicznych-praktykach"
        ),
        "categories": ["internships"],
        "kind": "programme-overview",
        "status": "unknown",
        "summary": "Zagraniczne praktyki dla osób uczących się w kształceniu "
        "zawodowym i absolwentów do roku po ukończeniu nauki. "
        "Rekrutację prowadzi uprawniona instytucja sektora VET, która "
        "wnioskuje o grant. Praktyki 10–89 dni lub ErasmusPro 90–365 "
        "dni; udział w konkursach umiejętności 1–10 dni. Dofinansowanie "
        "podróży, utrzymania i organizacji, bez obowiązkowego wkładu "
        "uczestnika. Nabór instytucji 2026 zakończony; terminy "
        "uczestników zależą od projektu.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "No literal participant application deadline; do not use "
        "institutional or event dates.",
        "inputs": [
            "funding-06",
            "erasmus-guide2026-en",
            "erasmus-guide2026",
            "erasmus-call-pdf",
        ],
    },
    {
        "key": "funding-07",
        "title": "Zagraniczne wyjazdy doskonalenia kadry",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/z"
            "agraniczne-wyjazdy-doskonalenia-kadry"
        ),
        "categories": ["training"],
        "kind": "programme-overview",
        "status": "unknown",
        "summary": "Mobilność kadry instytucji kształcenia zawodoweg"
        "o: obserwacja "
        "pracy 2–60 dni, prowadzenie zajęć 2–365 dni lub kursy 2–10 "
        "dni. Wniosek o finansowanie składa uprawniona instytucja VET; "
        "pracowników rekrutuje ich organizacja. Wsparcie pokrywa według "
        "zasad programu podróż, utrzymanie i organizację wyjazdu. Nabór "
        "instytucjonalny 2026 zakończony; nie oznacza zakończenia "
        "wszystkich rekrutacji kadry. Pełne warunki kosztów kursów "
        "określa przewodnik 2026.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "No literal participant application deadline; do not use "
        "institutional or event dates.",
        "inputs": [
            "funding-07",
            "erasmus-guide2026-en",
            "erasmus-guide2026",
            "erasmus-call-pdf",
        ],
    },
    {
        "key": "funding-08",
        "title": "Rozwój instytucji kształcenia zawodowego",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/r"
            "ozwoj-instytucji-ksztalcenia-zawodowego"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Partnerstwa instytucji kształcenia zawodowego z "
        "organizacjami "
        "edukacyjnymi, branżowymi i innymi uprawnionymi partnerami. "
        "Partnerstwo współpracy: minimum 3 organizacje z 3 krajów, "
        "12–36 miesięcy i 120/250/400 tys. EUR. Mała skala: minimum 2 "
        "organizacje z 2 krajów, 6–24 miesiące i 30/60 tys. EUR. Kwoty "
        "to ryczałty projektowe, nie wypłaty dla uczestnika. "
        "Koordynator z UE/kraju stowarzyszonego; szczegółowe zasady "
        "krajów określa przewodnik. Nabór 2026 zakończony.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure. Multiple national/central "
        "variants differ in clock/routes: no universal precise deadline.",
        "inputs": [
            "funding-08",
            "erasmus-guide2026-en",
            "erasmus-guide2026",
            "erasmus-call-pdf",
        ],
    },
    {
        "key": "funding-09",
        "title": "Mobilności kadry sportowej",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/m"
            "obilnosci-kadry-sportowej"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Mobilność pracowników i wolontariuszy sportu, pr"
        "zede wszystkim "
        "organizacji sportu powszechnego. Polska organizacja wnioskuje "
        "o projekt; uczestnik musi mieć udokumentowany związek "
        "pracy/wolontariatu i mieszkać w kraju wysyłającym. Obserwacja "
        "pracy 2–14 dni lub prowadzenie treningów 15–60 dni; "
        "maksymalnie 10 osób. Budżet obejmuje podróż, utrzymanie, "
        "organizację i wsparcie włączenia. Nabór 2026 zakończony; "
        "zawody zawodników nie są tą mobilnością.",
        "deadline": "2026-02-12T11:00:00Z",
        "hosts": [],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure.",
        "inputs": [
            "funding-09",
            "erasmus-guide2026-en",
            "erasmus-guide2026",
            "erasmus-call-pdf",
        ],
    },
    {
        "key": "funding-10",
        "title": "Międzynarodowe partnerstwa w dziedzinie sportu",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/m"
            "iedzynarodowe-partnerstwa-w-dziedzinie-sportu"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Międzynarodowa współpraca organizacji sportowych"
        ": partnerstwa "
        "współpracy, małej skali i niekomercyjne imprezy europejskie. "
        "Wnioskodawcą jest uprawniona organizacja, nie pojedynczy "
        "sportowiec. Liczba partnerów, czas i ryczałt zależą od "
        "działania; wskazane 30–450 tys. EUR nie stanowi jednej "
        "wspólnej stawki dla wszystkich akcji. Finansowanie projektu "
        "wymaga warunków przewodnika 2026 i właściwego konkursu EACEA. "
        "Nabór 2026 zakończony.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure.",
        "inputs": [
            "funding-10",
            "erasmus-guide2026-en",
            "erasmus-guide2026",
            "erasmus-call-pdf",
        ],
    },
    {
        "key": "funding-11",
        "title": "Sojusze na rzecz edukacji i przedsiębiorstw",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/s"
            "ojusze-na-rzecz-edukacji-i-przedsiebiorstw"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Sojusze edukacji i przedsiębiorstw: minimum 8 be"
        "neficjentów z "
        "4 krajów UE/stowarzyszonych, w tym 3 podmioty rynku pracy i 3 "
        "organizacje edukacji, przynajmniej jedna uczelnia oraz jeden "
        "dostawca VET. Uczelnie UE/stowarzyszone wymagają ECHE. "
        "Koordynator z UE/kraju stowarzyszonego; inni partnerzy tylko "
        "według przewodnika. Grant pokrywa do 80% kosztów, maks. 1 mln "
        "EUR na 2 lata lub 1,5 mln EUR na 3 lata. Nabór 2026 "
        "zakończony.",
        "deadline": "2026-03-10T16:00:00Z",
        "hosts": [],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure.",
        "inputs": [
            "funding-11",
            "erasmus-guide2026-en",
            "erasmus-guide2026",
            "erasmus-call-pdf",
        ],
    },
    {
        "key": "funding-12",
        "title": "Sojusze na rzecz współpracy sektorowej",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/s"
            "ojusze-na-rzecz-wspolpracy-sektorowej"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Sojusze współpracy sektorowej rozwijają umiejętności w "
        "określonych ekosystemach przemysłowych. Wymagane minimum 12 "
        "beneficjentów z 8 krajów UE/stowarzyszonych, co najmniej 5 "
        "podmiotów rynku pracy i 5 dostawców edukacji, w tym uczelnia i "
        "VET. Uczelnie UE/stowarzyszone muszą mieć ECHE. Dofinansowanie "
        "do 80% kosztów projektu, maks. 4 mln EUR na 4 lata. Nabór 2026 "
        "zakończony; skład i kwalifikowalność partnerów określa "
        "przewodnik.",
        "deadline": "2026-03-10T16:00:00Z",
        "hosts": [],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure.",
        "inputs": [
            "funding-12",
            "erasmus-guide2026-en",
            "erasmus-guide2026",
            "erasmus-call-pdf",
        ],
    },
    {
        "key": "funding-13",
        "title": "Działania Jean Monnet – nauczanie i badania",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/d"
            "zialania-jean-monnet-nauczanie-i-badania"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Jean Monnet finansuje nauczanie i badania o UE w"
        " uprawnionych "
        "uczelniach. Uczelnie UE/krajów stowarzyszonych wymagają ECHE; "
        "pozostałe kraje podlegają ograniczeniom przewodnika. Moduły, "
        "katedry i centra doskonałości mają odrębne wymagania godzinowe "
        "i stawki, projekty zwykle trwają 3 lata. Grant "
        "instytucjonalny, nie stypendium studenta. Nabór 2026 "
        "zakończony. Ogólne limity na stronie FRSE należy sprawdzić w "
        "aktualnym przewodniku, nie traktować jako jednej stawki.",
        "deadline": "2026-02-03T16:00:00Z",
        "hosts": [],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure.",
        "inputs": [
            "funding-13",
            "erasmus-guide2026-en",
            "erasmus-guide2026",
            "erasmus-call-pdf",
        ],
    },
    {
        "key": "funding-14",
        "title": "Działania Jean Monnet – debata nt. polityk",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/d"
            "zialania-jean-monnet-debata-nt-polityk"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Sieci Jean Monnet 2026: wewnętrzna co najmniej 1"
        "2 uczelni z 7 "
        "krajów UE/stowarzyszonych; UE–Indie co najmniej 12 "
        "beneficjentów, w tym 6 z Indii, koordynator z UE/kraju "
        "stowarzyszonego. Uczelnie UE/stowarzyszone wymagają ECHE. "
        "Projekty zwykle 36 miesięcy, do 80% kosztów, maks. 1 mln/1,2 "
        "mln EUR. Nabór zamknięty. Opis FRSE zawiera sprzeczne stare "
        "wymagania Kanada/USA; aktualny przewodnik 2026 w obu językach "
        "wskazuje Indie.",
        "deadline": "2026-02-03T16:00:00Z",
        "hosts": [],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure.",
        "inputs": [
            "funding-14",
            "erasmus-guide2026-en",
            "erasmus-guide2026",
            "erasmus-call-pdf",
        ],
    },
    {
        "key": "funding-15",
        "title": "Budowanie potencjału w szkolnictwie wyższym",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/b"
            "udowanie-potencjalu-w-szkolnictwie-wyzszym"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Budowanie potencjału szkolnictwa wyższego: granty dla "
        "uprawnionych uczelni, organizacji uczelni i konsorcjów z "
        "kwalifikujących się krajów, według części projektu i regionu. "
        "Stawki projektowe: 200–400 tys. EUR na 24/36 miesięcy, 400–800 "
        "tys. EUR na 24/36 miesięcy lub 800 tys.–1 mln EUR na 36/48 "
        "miesięcy. Skład konsorcjum i udział władz krajowych różnią się "
        "między częściami; pełne warunki w przewodniku 2026. Nabór 2026 "
        "zakończony; wzmianka o 2023 r. nie jest nowym naborem.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure. Multiple strands/regional "
        "routes; no falsely universal deadline.",
        "inputs": [
            "funding-15",
            "erasmus-guide2026-en",
            "erasmus-guide2026",
            "erasmus-call-pdf",
        ],
    },
    {
        "key": "funding-16",
        "title": "Erasmus Mundus Joint Masters",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/e"
            "rasmus-mundus-joint-masters"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Erasmus Mundus Joint Masters: grant dla konsorcj"
        "um minimum 3 "
        "uczelni z 3 krajów, w tym 2 z UE/krajów stowarzyszonych; ECHE "
        "dla tych uczelni. Finansuje wspólny program i stypendia "
        "wybranych studentów pełnych studiów: 1400 EUR/miesiąc przez "
        "12/18/24 miesiące, bez czesnego dla stypendysty. Wymagane dwa "
        "semestry w dwóch krajach innych niż kraj zamieszkania przy "
        "zapisie, w tym jednym UE/stowarzyszonym; wcześniejsze "
        "stypendium EMJM wyklucza kolejne. Nabór konsorcjów 2026 "
        "zakończony; kandydaci studiujący aplikują do konkretnych "
        "programów w ich terminach.",
        "deadline": "2026-02-12T16:00:00Z",
        "hosts": [],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure.",
        "inputs": [
            "funding-16",
            "erasmus-guide2026-en",
            "erasmus-guide2026",
            "erasmus-call-pdf",
        ],
    },
    {
        "key": "funding-17",
        "title": "Erasmus Mundus Design Measures",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/e"
            "rasmus-mundus-design-measures"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Erasmus Mundus Design Measures wspiera opracowan"
        "ie wspólnych "
        "międzynarodowych studiów magisterskich. Wnioskuje uprawniona "
        "uczelnia; uczelnie UE/krajów stowarzyszonych muszą mieć ECHE. "
        "Projekt ma przygotować wspólny program minimum 3 uczelni w 3 "
        "krajach, w tym 2 UE/stowarzyszone. Grant 60 tys. EUR jako "
        "ryczałt na projekt trwający 15 miesięcy; nie jest stypendium "
        "osobowym ani finansowaniem już prowadzonych studiów. Nabór "
        "2026 zakończony; szczegóły w przewodniku.",
        "deadline": "2026-02-12T16:00:00Z",
        "hosts": [],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure.",
        "inputs": [
            "funding-17",
            "erasmus-guide2026-en",
            "erasmus-guide2026",
            "erasmus-call-pdf",
        ],
    },
    {
        "key": "funding-18",
        "title": "Edukacja przez całe życie",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/e"
            "dukacja-przez-cale-zycie"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Mobilność niezawodowej edukacji dorosłych dla uprawnionych "
        "instytucji, np. bibliotek, NGO i uniwersytetów trzeciego "
        "wieku. Wyjazdy obejmują kadrę i dorosłych uczących się; "
        "priorytet mają osoby o mniejszych szansach. Grant na podróż, "
        "utrzymanie, organizację, kursy i włączenie zależy od formatu. "
        "Projekty krótkoterminowe 6–18 miesięcy; akredytowane 15 "
        "miesięcy z możliwością wydłużenia. Nabór instytucji 2026 "
        "zakończony, rekrutację osób prowadzą organizacje.",
        "deadline": "2026-02-19T11:00:00Z",
        "hosts": [],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure.",
        "inputs": [
            "funding-18",
            "erasmus-guide2026-en",
            "erasmus-guide2026",
            "erasmus-call-pdf",
        ],
    },
    {
        "key": "funding-19",
        "title": "Rozwiń metody pracy z dorosłymi",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/r"
            "ozwin-metody-pracy-z-doroslymi"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Partnerstwa niezawodowej edukacji dorosłych dla "
        "uprawnionych "
        "organizacji. Współpraca wymaga minimum 3 organizacji z 3 "
        "krajów UE/stowarzyszonych; mała skala minimum 2 z 2 krajów. "
        "Ryczałty całego projektu: 120/250/400 tys. EUR na 12–36 "
        "miesięcy albo 30/60 tys. EUR na 6–24 miesiące, według "
        "przewodnika 2026. To wsparcie instytucji, nie wypłata dla "
        "słuchacza. Nabór 2026 zakończony; projekty mają rozwijać "
        "kompetencje i metody pracy z dorosłymi.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure. Multiple national/central "
        "variants differ in clock/routes: no universal precise deadline.",
        "inputs": [
            "funding-19",
            "erasmus-guide2026-en",
            "erasmus-guide2026",
            "erasmus-call-pdf",
        ],
    },
    {
        "key": "funding-20",
        "title": "Wymiany młodzieżowe – współpraca poprzez organizację spotkań",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/w"
            "ymiany-mlodziezowe-wspolpraca-poprzez-organizacj"
            "e-spotkan"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Wymiany dla młodzieży 13–30 lat mieszkającej w krajach "
        "organizacji wysyłających/przyjmujących; minimum 2 grupy z 2 "
        "krajów, 5–21 dni. Wnioskować mogą uprawnione organizacje lub "
        "nieformalne grupy z UE/krajów stowarzyszonych, mimo węższego "
        "opisu FRSE. Projekty 3–24 miesiące obejmują budżet podróży, "
        "utrzymania i włączenia; stawki zależą od kraju. Zwykle 16–60 "
        "uczestników, minimum 10 przy wyłącznie mniejszych szansach. "
        "Nabór grantów 2026 zakończony; nie jest to indywidualny termin "
        "uczestnika.",
        "deadline": "2026-10-01T10:00:00Z",
        "hosts": [],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure.",
        "inputs": [
            "funding-20",
            "erasmus-guide2026-en",
            "erasmus-guide2026",
            "erasmus-call-pdf",
        ],
    },
    {
        "key": "funding-21",
        "title": (
            "Młodzieżowe spotkania, debaty, kampanie – w kraj" "u i za granicą"
        ),
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/m"
            "lodziezowe-spotkania-debaty-kampanie-w-kraju-i-z"
            "a-granica"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Działania uczestnictwa młodzieży 13–30 lat mieszkającej w "
        "krajach uczestniczących organizacji: lokalne lub "
        "międzynarodowe uczenie się demokracji. Wnioskują uprawnione "
        "organizacje lub nieformalne grupy, nie pojedyncza osoba; opis "
        "FRSE jest węższy od przewodnika. Projekt 3–24 miesiące, grant "
        "do 60 tys. EUR na zarządzanie, wydarzenia, mobilność, coaching "
        "i włączenie. Nie finansuje działań partyjnych ani "
        "infrastruktury. Nabór grantów 2026 zakończony; pełne warunki w "
        "przewodniku.",
        "deadline": "2026-10-01T10:00:00Z",
        "hosts": [],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure.",
        "inputs": [
            "funding-21",
            "erasmus-guide2026-en",
            "erasmus-guide2026",
            "erasmus-call-pdf",
        ],
    },
    {
        "key": "funding-22",
        "title": "Współpracuj z innymi na rzecz młodzieży!",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/w"
            "spolpracuj-z-innymi-na-rzecz-mlodziezy"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Międzynarodowe partnerstwa na rzecz młodzieży dl"
        "a uprawnionych "
        "organizacji, z koordynatorem w UE/kraju stowarzyszonym. "
        "Partnerstwa współpracy: minimum 3 organizacje z 3 krajów, "
        "120/250/400 tys. EUR na 12–36 miesięcy. Mała skala: minimum 2 "
        "z 2 krajów, 30/60 tys. EUR na 6–24 miesięcy. Kwoty to ryczałty "
        "projektu, nie osobiste stypendia; wzmianka o osobach w opisie "
        "FRSE nie zastępuje zasad wnioskodawcy. Nabór 2026 zakończony.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure. Multiple national/central "
        "variants differ in clock/routes: no universal precise deadline.",
        "inputs": [
            "funding-22",
            "erasmus-guide2026-en",
            "erasmus-guide2026",
            "erasmus-call-pdf",
        ],
    },
    {
        "key": "funding-23",
        "title": "Stwórz branżowe centrum umiejętności",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/s"
            "tworz-branzowe-centrum-umiejetnosci"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Grant na utworzenie branżowego centrum umiejętności: "
        "uprawniony ogólnopolski podmiot branżowy, spółka Skarbu "
        "Państwa lub organ szkoły zawodowej/CKZ, z wymaganym "
        "partnerstwem branży i placówki. Nabór III zamknięty 31.01.2025 "
        "o 16:00 (brak strefy). Limity dziedzinowe 9–16 mln PLN "
        "wynikają z regulaminu. Finansowanie projektu, nie uczestnika; "
        "bez obowiązkowego wkładu, z badaniem zdolności finansowej "
        "podmiotów niepublicznych.",
        "deadline": "2025-01-31",
        "hosts": [],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure.",
        "inputs": [
            "funding-23",
            "bcu-call3",
            "bcu-reglatest",
            "bcu-amendments",
        ],
    },
    {
        "key": "funding-24",
        "title": "Koordynuj regionalne działania LLL",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/k"
            "oordynuj-regionalne-dzialania-lll"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Koordynacja uczenia się przez całe życie w regionach: "
        "niekonkurencyjne granty dla samorządów województw lub ich "
        "upoważnionych jednostek. Jeden projekt na region, do 21,5 mln "
        "PLN; łączna alokacja 344 mln PLN nie jest stawką osobową. "
        "Uzupełniający nabór dla Wielkopolski zakończył się 31.10.2023; "
        "realizacja do 30.06.2026 nie jest terminem aplikacji. Brak "
        "potwierdzonego nowego naboru; nie jest to otwarte stypendium "
        "dla mieszkańca.",
        "deadline": "2023-10-31",
        "hosts": [],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure.",
        "inputs": ["funding-24"],
    },
    {
        "key": "funding-25",
        "title": "Zmiana, którą Ty tworzysz",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/z"
            "miana-ktora-ty-tworzysz"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Projekty solidarności EKS: minimum 5 osób w wiek"
        "u 18–30 lat na "
        "początku projektu, legalnie mieszkających w tym samym kraju UE "
        "lub stowarzyszonym z EKS, z rejestracją w portalu. Grupa albo "
        "organizacja w jej imieniu realizuje lokalny, niekomercyjny "
        "projekt 2–12 miesięcy. Wsparcie 630 EUR na projekt/miesiąc; "
        "coaching i koszty włączenia wymagają uzasadnienia. Nabór 2026 "
        "zakończony.",
        "deadline": "2026-10-01T10:00:00Z",
        "hosts": [],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure.",
        "inputs": ["funding-25", "funding-26", "eks-guide2026"],
    },
    {
        "key": "funding-27",
        "title": "Zostań wolontariuszem – bądź solidarny!",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/z"
            "ostan-wolontariuszem-badz-solidarny"
        ),
        "categories": ["volunteering"],
        "kind": "programme-overview",
        "status": "unknown",
        "summary": "Wolontariat EKS dla osób 18–30 lat w chwili rozpoczęcia, "
        "legalnie mieszkających w uprawnionych krajach; rejestracja od "
        "17 lat nie obniża wieku udziału. Indywidualnie 14–366 dni lub "
        "zespołowo 14–59 dni, bez wynagrodzenia i opłat udziału. "
        "Wsparcie podróży, mieszkania, wyżywienia, języka, "
        "ubezpieczenia i kieszonkowego. Obowiązuje łączny limit 12 "
        "miesięcy wcześniejszego wolontariatu i ograniczenie kolejnego "
        "długiego wyjazdu. Rekrutację ustala projekt; zamknięty nabór "
        "instytucji nie zamyka wszystkich ofert.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "No literal participant application deadline; do not use "
        "institutional or event dates.",
        "inputs": ["funding-27", "eks-guide2026"],
    },
    {
        "key": "funding-28",
        "title": "Zorganizuj wolontariat – wzmocnij swoją organizację!",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/z"
            "organizuj-wolontariat-wzmocnij-swoja-organizacje"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Grant EKS dla uprawnionej organizacji prowadzącej i "
        "organizującej wolontariat; wymagany ważny Znak Jakości. "
        "Projekt angażuje osoby 18–30 lat z uprawnioną legalną "
        "rezydencją, obejmuje koszty organizacji i wsparcia "
        "uczestników. Nie jest indywidualną ofertą pracy. Nabór grantów "
        "2026 zakończony; przewodnik wskazuje 18 lutego i opcjonalnie 1 "
        "października w południe czasu Brukseli, wbrew wzmiance FRSE o "
        "maju/październiku. Czas i dopuszczalne działania określa "
        "aktualny przewodnik EKS.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure. Mandatory February18, optional "
        "October1 not proved Poland-specific; no guessed applicable "
        "closing.",
        "inputs": ["funding-28", "eks-guide2026"],
    },
    {
        "key": "funding-29",
        "title": "Organizuj wyjazdy z młodzieżą z Litwy",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/o"
            "rganizuj-wyjazdy-z-mlodzieza-z-litwy"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Grant dla niekomercyjnych osób prawnych zarejestrowanych w "
        "Polsce/Litwie, z doświadczeniem pracy z młodzieżą, na "
        "partnerstwo obu krajów. Format 1: osoby 13–30 lat poza "
        "liderami, 10–24 uczestników wraz z maks. 4 opiekunami. Format "
        "2: 13–30 lat lub starsi pracownicy młodzieżowi, minimum 10 "
        "osób poza opiekunami. Wyjazd 4–7 dni, jeden priorytet 2026. Do "
        "100% kosztów projektu: 1500–15 000 EUR albo 1500–7500 EUR, bez "
        "wkładu własnego. Nabór 2026 zakończony; kwiecień–październik "
        "to realizacja, nie termin aplikacji.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure.",
        "inputs": ["funding-29"],
    },
    {
        "key": "funding-30",
        "title": "Organizuj międzynarodowe wydarzenia!",
        "url": (
            "https://www.frse.org.pl/finansowanie-projektow/o"
            "rganizuj-miedzynarodowe-wydarzenia"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "status": "expired",
        "summary": "Polsko-Ukraińska Rada Wymiany Młodzieży: projekty "
        "niekomercyjnych podmiotów zarejestrowanych w Polsce/Ukrainie, "
        "działających na rzecz młodzieży, działania 5–10 dni. Wariant A "
        "obejmuje grupę przyjeżdżającą z Ukrainy i partnera "
        "ukraińskiego; wariant B młodzież ukraińską mieszkającą w "
        "Polsce, bez wymogu ukraińskiej organizacji. 100% kosztów "
        "projektu, bez wymaganego wkładu własnego, podróży, pobytu, "
        "działań i szczególnych potrzeb. Nabór 2026 zakończony; "
        "maj–październik to okres realizacji, nie aplikowania.",
        "deadline": None,
        "hosts": ["PL"],
        "location": None,
        "proof": "Literal own institutional competition closed: Nabór 2026 "
        "zakończony (or actual dedicated closed round). This is not "
        "individual recruitment closure. Literal formatA arrival "
        "inPoland/formatB Ukrainian youth alreadyinPoland; "
        "institutionalestablishment not citizenship.",
        "inputs": ["funding-30"],
    },
    {
        "key": "training-02",
        "title": "Webinarium: Powrót Szwajcarii do programu Erasmus+",
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/w"
            "ebinarium-powrot-szwajcarii-do-programu-erasmus"
        ),
        "categories": ["training"],
        "kind": "opportunity",
        "status": "unknown",
        "summary": "Webinarium po angielsku dla biur współpracy międ"
        "zynarodowej, "
        "koordynatorów Erasmus+ i zainteresowanych powrotem Szwajcarii "
        "do programu. 12 października 2026, 11:00–12:15, MS Teams. "
        "Obowiązuje rejestracja przez oficjalny formularz. To termin "
        "wydarzenia, nie zamknięcia zapisów; strona nie podaje terminu "
        "rekrutacji, strefy czasowej ani opłaty. Warunki udziału należy "
        "potwierdzić przy rejestracji.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "Registration closing unknown. Event date/time, recurring old "
        "edition, fixed capacity and programme modules do not establish "
        "closing/open status.",
        "inputs": ["training-02"],
    },
    {
        "key": "training-03",
        "title": "Webinarium: Powrót Wielkiej Brytanii do Erasmusa+, sektor "
        "Młodzież",
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/w"
            "ebinarium-powrot-wielkiej-brytanii-do-erasmusa-s"
            "ektor-mlodziez"
        ),
        "categories": ["training"],
        "kind": "opportunity",
        "status": "unknown",
        "summary": "Webinarium po angielsku dla organizacji młodzieżowych, "
        "koordynatorów, osób pracujących z młodzieżą i "
        "zainteresowanych. Dotyczy powrotu Wielkiej Brytanii do "
        "Erasmus+ w sektorze młodzieży. 14 października 2026, "
        "14:00–15:30, MS Teams; wymagane zapisy. Data wydarzenia nie "
        "jest terminem zapisów, brak podanej strefy i opłaty. Powrót UK "
        "od 2027 r. nie oznacza kwalifikowalności w konkursie "
        "DiscoverEU 2026.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "Registration closing unknown. Event date/time, recurring old "
        "edition, fixed capacity and programme modules do not establish "
        "closing/open status.",
        "inputs": ["training-03", "uk-webinar"],
    },
    {
        "key": "training-04",
        "title": "RPI Słupsk. Konferencja: Erasmus+ zmienia perspektywę",
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/r"
            "pi-slupsk-konferencja-erasmus-zmienia-perspektyw"
            "e"
        ),
        "categories": ["training"],
        "kind": "opportunity",
        "status": "unknown",
        "summary": "Konferencja edukacyjna w Słupsku: 15 października 2026, "
        "15:00–18:10, PODN, ul. Poniatowskiego 4a. Program obejmuje "
        "kompetencje przyszłości, doświadczenia uczestników Erasmus+, "
        "eTwinning i potrzeby pracodawców. Rejestracja na miejscu w "
        "programie 14:30–15:00 nie jest terminem zamknięcia "
        "internetowych zapisów. Szczegóły uczestnictwa należy "
        "potwierdzić u organizatora; brak podanej opłaty i strefy "
        "czasowej.",
        "deadline": None,
        "hosts": ["PL"],
        "location": "Słupsk, PODN, ul. Poniatowskiego 4a",
        "proof": "Registration closing unknown. Event date/time, recurring old "
        "edition, fixed capacity and programme modules do not establish "
        "closing/open status.",
        "inputs": ["training-04", "slupsk-programme"],
    },
    {
        "key": "training-05",
        "title": "Eurolekcje EURODESK",
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/e"
            "urolekcje-eurodesk"
        ),
        "categories": ["training"],
        "kind": "programme-overview",
        "status": "unknown",
        "summary": "Bezpłatne Eurolekcje dla uczniów szkół ponadpodstawowych, "
        "głównie 16–19 lat. Konsultanci Eurodesk prowadzą 45-minutowe "
        "lekcje lub 90-minutowe warsztaty o mobilności, pracy, "
        "projektach i aktywności w Europie, także online. Placówka "
        "kontaktuje się z prowadzącymi w sprawie realizacji. To program "
        "edukacyjny bez opublikowanego wspólnego terminu naboru.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "Registration closing unknown. Event date/time, recurring old "
        "edition, fixed capacity and programme modules do not establish "
        "closing/open status.",
        "inputs": ["training-05"],
    },
    {
        "key": "training-06",
        "title": "Kursy internetowe eTwinning",
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/k"
            "ursy-internetowe-etwinning"
        ),
        "categories": ["training"],
        "kind": "programme-overview",
        "status": "unknown",
        "summary": "Bezpłatne kursy internetowe eTwinning dla nauczy"
        "cieli różnych "
        "przedmiotów i typów placówek. Nauka na platformie "
        "Moodle/eTwinning rozwija pracę projektową i wykorzystanie "
        "narzędzi cyfrowych; wymagane konto oraz zapis do konkretnego "
        "kursu. To przegląd programu, nie aktualny nabór do wszystkich "
        "kursów. Terminy, czas i warunki danej edycji trzeba sprawdzić "
        "na stronie eTwinning; strona FRSE nie podaje wspólnego "
        "terminu.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "Registration closing unknown. Event date/time, recurring old "
        "edition, fixed capacity and programme modules do not establish "
        "closing/open status.",
        "inputs": ["training-06"],
    },
    {
        "key": "training-07",
        "title": "Webinaria eTwinning",
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/w"
            "ebinaria-etwinning"
        ),
        "categories": ["training"],
        "kind": "programme-overview",
        "status": "unknown",
        "summary": "Bezpłatne webinaria eTwinning dla nauczycieli wszystkich "
        "przedmiotów i typów szkół, dotyczące projektów, narzędzi oraz "
        "współpracy. Wymagane konto programu i rejestracja do "
        "konkretnego spotkania. Strona FRSE używa dawnej nazwy "
        "eTwinning Live; nie potwierdza ona bieżącego adresu platformy. "
        "Przegląd programu bez wspólnego terminu naboru; daty i "
        "aktualną procedurę sprawdź w oficjalnych szkoleniach "
        "eTwinning.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "Registration closing unknown. Event date/time, recurring old "
        "edition, fixed capacity and programme modules do not establish "
        "closing/open status.",
        "inputs": ["training-07"],
    },
    {
        "key": "training-11",
        "title": "Forum Edukacji Dorosłych",
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/f"
            "orum-edukacji-doroslych"
        ),
        "categories": ["training"],
        "kind": "programme-overview",
        "status": "unknown",
        "summary": "Forum Edukacji Dorosłych to coroczna konferencja"
        " edukacyjna "
        "EPALE dla praktyków, badaczy, trenerów, osób rozwijających "
        "kadry i innych zainteresowanych uczeniem się dorosłych. "
        "Wspiera wymianę doświadczeń i rozwój kompetencji zawodowych. "
        "Strona FRSE opisuje program, nie konkretną aktualną edycję; "
        "nie podaje bieżącego terminu zapisów, daty, miejsca ani "
        "opłaty. Warunki edycji należy potwierdzić u organizatora.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "Registration closing unknown. Event date/time, recurring old "
        "edition, fixed capacity and programme modules do not establish "
        "closing/open status.",
        "inputs": ["training-11"],
    },
    {
        "key": "training-13",
        "title": "Kongres Edukacji",
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/k" "ongres-edukacji"
        ),
        "categories": ["training"],
        "kind": "programme-overview",
        "status": "unknown",
        "summary": "Kongres Edukacji FRSE: bezpłatna konferencja dla"
        " edukatorów, "
        "instytucji, beneficjentów programów, władz i zainteresowanych "
        "rozwojem edukacji. Program obejmuje eksperckie wystąpienia i "
        "wymianę doświadczeń. To przegląd corocznego wydarzenia, bez "
        "aktualnego terminu zapisów; zwyczajowego miejsca w Warszawie "
        "ani poprzedniej edycji w Gdańsku nie należy uznawać za "
        "potwierdzoną lokalizację nowej edycji.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "Registration closing unknown. Event date/time, recurring old "
        "edition, fixed capacity and programme modules do not establish "
        "closing/open status.",
        "inputs": ["training-13"],
    },
    {
        "key": "training-14",
        "title": "Mobilne Centrum Edukacyjne FRSE",
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/m"
            "obilne-centrum-edukacyjne-frse"
        ),
        "categories": ["training"],
        "kind": "programme-overview",
        "status": "unknown",
        "summary": "Mobilne Centrum Edukacyjne FRSE oferuje grupom warsztaty o "
        "europejskich programach edukacyjnych i nowych technologiach. "
        "Pojedyncze zajęcia do 60 minut, grupa do 30 osób; działania "
        "docierają do różnych regionów Polski. Przegląd programu, bez "
        "podanego aktualnego harmonogramu lub wspólnego terminu naboru. "
        "Dla zainteresowanej placówki udział i warunki uzgadnia się z "
        "organizatorem.",
        "deadline": None,
        "hosts": ["PL"],
        "location": "Różne regiony Polski według harmonogramu",
        "proof": "Registration closing unknown. Event date/time, recurring old "
        "edition, fixed capacity and programme modules do not establish "
        "closing/open status.",
        "inputs": ["training-14"],
    },
    {
        "key": "training-15",
        "title": "Ogólnopolski Dzień Informacyjny FRSE",
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/o"
            "golnopolski-dzien-informacyjny-frse"
        ),
        "categories": ["training"],
        "kind": "programme-overview",
        "status": "unknown",
        "summary": "Ogólnopolski Dzień Informacyjny FRSE łączy ekspe"
        "rckie sesje, "
        "konsultacje i prezentacje możliwości europejskich programów "
        "dla organizacji, instytucji i firm. Wydarzenie edukacyjne "
        "odbywa się stacjonarnie lub online zależnie od edycji. "
        "Aktualna strona jest przeglądem programu i odsyła do "
        "poprzedniej edycji; nie potwierdza daty nowej edycji, opłaty "
        "ani terminu zapisów. Sprawdź aktualny komunikat organizatora.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "Registration closing unknown. Event date/time, recurring old "
        "edition, fixed capacity and programme modules do not establish "
        "closing/open status.",
        "inputs": ["training-15"],
    },
    {
        "key": "training-17",
        "title": "Tool Fair",
        "url": "https://www.frse.org.pl/wydarzenia-i-szkolenia/tool-fair",
        "categories": ["training"],
        "kind": "programme-overview",
        "status": "unknown",
        "summary": "Tool Fair to program międzynarodowej wymiany nar"
        "zędzi edukacji "
        "pozaformalnej dla pracowników młodzieżowych, trenerów, "
        "facylitatorów oraz osób z NGO i wolontariuszy. Służy uczeniu "
        "się i wymianie praktyk. Strona FRSE wskazuje poprzednią edycję "
        "2023; nie jest to potwierdzony nabór 2026. Brak bieżącego "
        "terminu, miejsca i warunków finansowania; należy sprawdzić "
        "aktualną edycję u organizatora.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "Registration closing unknown. Event date/time, recurring old "
        "edition, fixed capacity and programme modules do not establish "
        "closing/open status.",
        "inputs": ["training-17"],
    },
    {
        "key": "training-20",
        "title": "TEDxWarsaw Youth 2026",
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/t"
            "edxwarsaw-youth-2026"
        ),
        "categories": ["training"],
        "kind": "opportunity",
        "status": "unknown",
        "summary": "FRSE udostępnia bezpłatne bilety na TEDxWarsaw Y"
        "outh 2026: 23 "
        "października, 9:00–16:00, Teatr 6. piętro, PKiN w Warszawie. "
        "Uczestnik zgłasza motywację; wybór następuje w trzech turach, "
        "nie według kolejności zgłoszeń. Strona nie podaje wspólnego "
        "terminu zamknięcia, dokładnego limitu wieku ani strefy "
        "czasowej. Jest to selektywne wsparcie udziału w wydarzeniu "
        "edukacyjnym; złożenie zgłoszenia nie gwarantuje biletu.",
        "deadline": None,
        "hosts": ["PL"],
        "location": "Warszawa, Teatr 6. piętro, Pałac Kultury i Nauki",
        "proof": "Registration closing unknown. Event date/time, recurring old "
        "edition, fixed capacity and programme modules do not establish "
        "closing/open status.",
        "inputs": ["training-20"],
    },
    {
        "key": "training-21",
        "title": "Webinarium Erasmus+: działania Jean Monnet",
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/w"
            "ebinarium-erasmus-dzialania-jean-monnet"
        ),
        "categories": ["training"],
        "kind": "opportunity",
        "status": "unknown",
        "summary": "Webinarium Erasmus+ Jean Monnet dla biur współpracy "
        "międzynarodowej, koordynatorów i zainteresowanych studiami o "
        "UE. 27 października 2026, 10:00–11:00, MS Teams; wymagane "
        "zapisy przez oficjalny formularz. Wydarzenie przedstawia "
        "działania Jean Monnet, nie stanowi osobnego grantu. Strona nie "
        "określa zamknięcia zapisów, strefy czasowej ani opłaty; podana "
        "data to termin spotkania.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "Registration closing unknown. Event date/time, recurring old "
        "edition, fixed capacity and programme modules do not establish "
        "closing/open status.",
        "inputs": ["training-21"],
    },
    {
        "key": "training-22",
        "title": "Webinarium: działania Erasmus Mundus",
        "url": (
            "https://www.frse.org.pl/wydarzenia-i-szkolenia/w"
            "ebinarium-dzialania-erasmus-mundus"
        ),
        "categories": ["training"],
        "kind": "opportunity",
        "status": "unknown",
        "summary": "Webinarium o działaniach Erasmus Mundus dla biur"
        " współpracy "
        "międzynarodowej, koordynatorów i zainteresowanych. 28 "
        "października 2026, 10:00–11:00, MS Teams, wymagane zapisy. "
        "Spotkanie dotyczy programów wspólnych studiów i ich "
        "opracowania, nie jest indywidualnym stypendium. Strona nie "
        "podaje zamknięcia zapisów, strefy czasowej ani opłaty; data i "
        "godziny określają wydarzenie.",
        "deadline": None,
        "hosts": [],
        "location": None,
        "proof": "Registration closing unknown. Event date/time, recurring old "
        "edition, fixed capacity and programme modules do not establish "
        "closing/open status.",
        "inputs": ["training-22"],
    },
    {
        "key": "job-FRSE-27-2026",
        "title": "Specjalista/specjalistka/ starszy specjalista/starsza "
        "specjalistka / główny specjalista/główna specjalistka w Zespole "
        "ds. projektów finansowanych z Funduszy Norweskich i EOG",
        "url": "https://www.frse.org.pl/praca-w-frse",
        "categories": ["jobs"],
        "kind": "opportunity",
        "status": "open",
        "summary": "Praca w Warszawie przy projektach z Funduszy Nor"
        "weskich i EOG; "
        "dwa wakaty. Wymagane wyższe wykształcenie, staż 2/4/5 lat "
        "zależnie od stanowiska, angielski minimum B1, bardzo dobra "
        "znajomość MS Office/Excel i gotowość do wyjazdów. Umowa o "
        "pracę, opieka medyczna, szkolenia, lekcje angielskiego i "
        "świadczenia dodatkowe. CV i list motywacyjny do 23 "
        "października 2026 o 17:00; źródło nie podaje strefy ani "
        "wynagrodzenia.",
        "deadline": "2026-10-23",
        "hosts": ["PL"],
        "location": "Warszawa",
        "proof": "Actual reference FRSE/27/2026 and explicit appli"
        "cation closing; "
        "clock17:00 has no literalzone, preserve dateonly. Same "
        "canonicaljobsURL, stable identity includes actual reference.",
        "inputs": ["jobs"],
    },
    {
        "key": "job-FRSE-25-2026",
        "title": "Młodszy specjalista/młodsza specjalistka / "
        "specjalista/specjalistka / starszy specjalista/starsza "
        "specjalistka / główny specjalista/główna specjalistka w Zespole "
        "Projektu FERS edukacja szkolna",
        "url": "https://www.frse.org.pl/praca-w-frse",
        "categories": ["jobs"],
        "kind": "opportunity",
        "status": "expired",
        "summary": "Oferta FRSE/25/2026: 1 etat w Warszawie, projekty FERS "
        "edukacja szkolna. Wymagane wyższe wykształcenie I/II stopnia, "
        "doświadczenie przy środkach europejskich, angielski, "
        "zaawansowany Excel i gotowość do wyjazdów; B1 wskazano jako "
        "dodatkowy atut. Umowa na czas projektu, nie dłużej niż do "
        "listopada 2029, opieka medyczna i szkolenia. Termin CV i "
        "listu: 25 września 2026 o 17:00, już upłynął. Brak podanej "
        "strefy i stawki wynagrodzenia.",
        "deadline": "2026-09-25",
        "hosts": ["PL"],
        "location": "Warszawa",
        "proof": "Actual reference FRSE/25/2026 and explicit appli"
        "cation closing; "
        "clock17:00 has no literalzone, preserve dateonly. Same "
        "canonicaljobsURL, stable identity includes actual reference.",
        "inputs": ["jobs"],
    },
    {
        "key": "job-FRSE-24-2026",
        "title": "Młodsza specjalistka/młodszy specjalista / "
        "specjalistka/specjalista / starsza specjalistka/starszy "
        "specjalista / główna specjalistka/główny specjalista w Biurze "
        "Programów Edukacji Szkolnej oraz Edukacji Dorosłych w Zespole "
        "Erasmus+ SE-KA1",
        "url": "https://www.frse.org.pl/praca-w-frse",
        "categories": ["jobs"],
        "kind": "opportunity",
        "status": "expired",
        "summary": "Oferta FRSE/24/2026: 1 wakat w Warszawie w Zespo"
        "le Erasmus+ "
        "SE-KA1. Wymagane wyższe wykształcenie I/II stopnia, angielski, "
        "dobra znajomość MS Office, komunikacja, organizacja pracy i "
        "zainteresowanie edukacją szkolną; doświadczenie "
        "Erasmus+/projektowe to dodatkowy atut. Umowa o pracę, opieka "
        "medyczna, szkolenia i lekcje angielskiego. CV i list "
        "motywacyjny należało złożyć do 30 września 2026 o 17:00; "
        "termin minął, brak strefy oraz wynagrodzenia.",
        "deadline": "2026-09-30",
        "hosts": ["PL"],
        "location": "Warszawa",
        "proof": "Actual reference FRSE/24/2026 and explicit appli"
        "cation closing; "
        "clock17:00 has no literalzone, preserve dateonly. Same "
        "canonicaljobsURL, stable identity includes actual reference.",
        "inputs": ["jobs"],
    },
    {
        "key": "job-FRSE-23-2026",
        "title": "Specjalistka/specjalista / starsza specjalistka/starszy "
        "specjalista / główna specjalistka/główny specjalista w Zespole "
        "Młodzież Erasmus+ i Discover EU – Akcja 1",
        "url": "https://www.frse.org.pl/praca-w-frse",
        "categories": ["jobs"],
        "kind": "opportunity",
        "status": "expired",
        "summary": "Oferta FRSE/23/2026: 1 wakat w Warszawie, Erasmu"
        "s+ Młodzież i "
        "DiscoverEU KA1. Wymagane magisterium, minimum 2 lata "
        "doświadczenia dla specjalisty lub 4 dla starszego specjalisty, "
        "znajomość pracy z młodzieżą/edukacji pozaformalnej, angielski "
        "minimum B2 i MS Office/Excel. Umowa o pracę, wdrożenie, opieka "
        "medyczna i szkolenia. Dla głównego specjalisty źródło nie "
        "podaje odrębnego minimalnego stażu. CV i list do 11 września "
        "2026 o 17:00; termin upłynął, bez strefy.",
        "deadline": "2026-09-11",
        "hosts": ["PL"],
        "location": "Warszawa",
        "proof": "Actual reference FRSE/23/2026 and explicit appli"
        "cation closing; "
        "clock17:00 has no literalzone, preserve dateonly. Same "
        "canonicaljobsURL, stable identity includes actual reference.",
        "inputs": ["jobs"],
    },
    {
        "key": "discover",
        "title": "Konkurs DiscoverEU: ruszyła runda jesienna!",
        "url": (
            "https://www.frse.org.pl/aktualnosci/konkurs-disc"
            "overeu-ruszyla-runda-jesienna"
        ),
        "categories": ["competitions"],
        "kind": "opportunity",
        "status": "open",
        "summary": "DiscoverEU jesień 2026 dla osób urodzonych w 200"
        "8 r. legalnie "
        "mieszkających w UE lub krajach stowarzyszonych: Islandia, "
        "Liechtenstein, Macedonia Północna, Norwegia, Serbia i Turcja. "
        "Darmowy bilet na podróż po Europie, zwykle kolejową, do 7 dni "
        "podróży w miesiącu, samodzielnie lub z maks. 4 kwalifikującymi "
        "się znajomymi; wsparcie szczególnych potrzeb. Zgłoszenia do 15 "
        "października 2026 w południe, bez podanej strefy. Podróż "
        "1.03.2027–31.05.2028; opis katalogowy o obywatelstwie jest "
        "węższy od aktualnego naboru.",
        "deadline": "2026-10-15",
        "hosts": [],
        "location": None,
        "proof": "Actual invitation/closed-call proof and date. Pr"
        "eserve dateonly "
        "ifclock has no sourcezone; futuredate alone is never enough "
        "foropen.",
        "inputs": ["discover"],
    },
    {
        "key": "etwinning-call",
        "title": "Konkurs: Nasz projekt eTwinning 2027",
        "url": (
            "https://www.frse.org.pl/aktualnosci/konkurs%C2%A"
            "0nasz-projekt-etwinning-2027"
        ),
        "categories": ["competitions"],
        "kind": "opportunity",
        "status": "open",
        "summary": "Dla nauczycieli i uprawnionej kadry polskich placówek "
        "realizujących międzynarodowy projekt eTwinning w 2025/26 lub "
        "2026/27, z Krajową Odznaką Jakości. Nie obejmuje projektów "
        "czysto krajowych, wcześniej nagrodzonych ani nauczycieli i "
        "szkół zwycięskich w poprzedniej edycji; ambasadorzy tylko we "
        "własnej kategorii. Nauczyciel może zgłosić do 4 projektów; "
        "wymagany dostęp jury i zgody. Konkurs wyróżnia projekty, bez "
        "potwierdzonej kwoty nagrody. Zgłoszenia do 15 stycznia 2027 o "
        "23:59, źródło nie podaje strefy.",
        "deadline": "2027-01-15",
        "hosts": [],
        "location": None,
        "proof": "Actual invitation/closed-call proof and date. Pr"
        "eserve dateonly "
        "ifclock has no sourcezone; futuredate alone is never enough "
        "foropen.",
        "inputs": [
            "etwinning-call",
            "etwinning-regulations",
            "etwinning-policy",
        ],
    },
    {
        "key": "professional-school",
        "title": "Nabór uzupełniający: Profesjonalna Szkoła Roku",
        "url": (
            "https://www.frse.org.pl/aktualnosci/nabor-uzupel"
            "niajacy-profesjonalna-szkola-roku"
        ),
        "categories": ["competitions"],
        "kind": "opportunity",
        "status": "open",
        "summary": "Nabór uzupełniający 2025: polskie szkoły branżowe I (poza "
        "wyłącznie młodocianymi pracownikami), technika, branżowe II i "
        "policealne dzienne/stacjonarne. Obszary: "
        "chemia/petrochemia/środowisko, IT/telekomunikacja, "
        "motoryzacja/elektromobilność lub zdrowie/bezpieczeństwo/opieka "
        "społeczna. Dyrektor/reprezentant zgłasza raz w jednym "
        "obszarze, na podstawie działań 2022/23–2024/25. Nagroda: "
        "tytuł, statuetka, dyplom i możliwe punkty w późniejszym "
        "konkursie grantowym, bez dotacji pieniężnej. Udział bezpłatny; "
        "do 15.10.2026 o 15:00, brak strefy.",
        "deadline": "2026-10-15",
        "hosts": [],
        "location": None,
        "proof": "Actual invitation/closed-call proof and date. Pr"
        "eserve dateonly "
        "ifclock has no sourcezone; futuredate alone is never enough "
        "foropen.",
        "inputs": [
            "professional-school",
            "professional-school-hub",
            "professional-school-regulations",
        ],
    },
    {
        "key": "selfie2025",
        "title": "Konkurs 2025",
        "url": "https://selfieplus.frse.org.pl/konkurs-2025",
        "categories": ["competitions"],
        "kind": "opportunity",
        "status": "expired",
        "summary": "Konkurs fotograficzny dla instytucjonalnych beneficjentów "
        "Erasmus+, EKS, PO WER, FERS oraz wymian polsko-litewskich i "
        "polsko-ukraińskich. Jedno niekolażowe zdjęcie z projektu, "
        "JPG/PNG ponad 1 MB i opis 500–1000 znaków; wymagane prawa "
        "autorskie oraz zgody osób na zdjęciu. Nagrody rzeczowe dla "
        "instytucji: aparaty cyfrowe lub natychmiastowe, zależnie od "
        "kategorii. Nabór zakończony 31 października 2025. Zdjęcia "
        "później dodane na platformę nie oznaczają przedłużenia "
        "konkursu.",
        "deadline": "2025-10-31",
        "hosts": [],
        "location": None,
        "proof": "Actual invitation/closed-call proof and date. Pr"
        "eserve dateonly "
        "ifclock has no sourcezone; futuredate alone is never enough "
        "foropen.",
        "inputs": ["selfie2025", "selfie-reg2025"],
    },
    {
        "key": "edu-inspirator-rules",
        "title": "EDUinspirator 2025",
        "url": "https://eduinspiracje.org.pl/eduinspirator/regulamin",
        "categories": ["competitions"],
        "kind": "opportunity",
        "status": "expired",
        "summary": "Konkurs dla osób, które uczestniczyły w projekta"
        "ch programów "
        "FRSE w latach 2015–2025 i rozwijają edukację w Polsce; dla "
        "niepełnoletnich wymagana zgoda opiekuna. Oceniane działania "
        "mają mieć międzynarodowy charakter i odpowiadać kryteriom "
        "innowacji, technologii, języków lub przedsiębiorczości. "
        "Nagrody rzeczowe lub vouchery, bez wymiany na gotówkę; "
        "dodatkowe 11,11% wartości nagrody służy podatkowi, nie "
        "stypendium. Zgłoszenia zakończone 31 października 2025.",
        "deadline": "2025-10-31",
        "hosts": [],
        "location": None,
        "proof": "Actual invitation/closed-call proof and date. Pr"
        "eserve dateonly "
        "ifclock has no sourcezone; futuredate alone is never enough "
        "foropen.",
        "inputs": ["edu-inspirator-rules", "edu-inspirator-reg2025"],
    },
    {
        "key": "publishing-call",
        "title": "EDUinspiracje Nauka - Monografie FRSE",
        "url": "https://www.frse.org.pl/wydawnictwo/obwieszczenie-o-konkursie",
        "categories": ["grants"],
        "kind": "opportunity",
        "status": "expired",
        "summary": "Dla autorów oryginalnych, nieopublikowanych monografii o "
        "edukacji, z nauk humanistycznych/społecznych, po polsku lub "
        "angielsku; praca nie może być w innym procesie wydawniczym ani "
        "wygenerowana przez AI, obowiązują wyłączenia konfliktu "
        "interesów. Wymagane pozytywne oceny i recenzje oraz licencja. "
        "FRSE finansuje wydanie, promocję i tłumaczenie; honorarium "
        "5000 PLN brutto, rozdziały pracy zbiorowej do 1200 PLN brutto. "
        "Termin 30 kwietnia 2026 minął; aktualne obwieszczenie "
        "potwierdza zamknięcie.",
        "deadline": "2026-04-30",
        "hosts": [],
        "location": None,
        "proof": "Actual invitation/closed-call proof and date. Pr"
        "eserve dateonly "
        "ifclock has no sourcezone; futuredate alone is never enough "
        "foropen.",
        "inputs": [
            "publishing-call",
            "publishing-reg",
            "publishing-standards",
            "publishing-ethics",
            "publishing-review",
            "publishing-production",
        ],
    },
]


if __name__ == "__main__":
    sys.exit(main())
