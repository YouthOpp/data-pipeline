"""Collect the finite reviewed Arts Council Malta funding and opportunity frontier."""

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

SOURCE_ID = "mt-arts-council"
SOURCE_URL = "https://artscouncilmalta.gov.mt/en/funding-and-grants/artivisti/"
WEBSITE_URL = "https://artscouncilmalta.gov.mt/"
LANGUAGE = "en"
PUBLISHER_COUNTRY = "MT"
PUBLISHER_TYPE = "government-agency"
ATTRIBUTION = "Arts Council Malta https://artscouncilmalta.gov.mt/ and the named programme administrators"

_GITHUB = "https://api.github.com/repos/YouthOpps/data-source"
_WORKFLOW_GITHUB = "https://api.github.com/repos/YouthOpps/data-pipeline"
_USER_AGENT = (
    "YouthOpps/1.0 (+https://github.com/YouthOpps/data-pipeline; "
    "public opportunity metadata)"
)
CATEGORIES = {
    "scholarships", "grants", "internships", "jobs", "training", "fellowships", "competitions", "other", "volunteering"
}
FAMILY = "youthopps-artscouncilmalta-publisher-v1"
FAMILY_SOURCES = {"mt-arts-council"}
COLLECTION_TIMEOUT = 2100
RUN_TIMEOUT = 2490
EXPORT_TIMEOUT = 30
_STATE_SCHEMA = 1
_ARTIFACT_NAME = "artscouncilmalta-pacing-state"
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
    if publisher:
        byte_limit = min(byte_limit, 8_000_000)
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
                with locked_budget():
                    state = load_budget()
                    state["blocked"] = True
                    state["observed_at"] = time.time()
                    save_budget(state)
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
                raise AdapterError("Unreviewed publisher redirect", "access", status)
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
    """Read each exact reviewed route once; archived gaps are checked downstream."""
    reviewed = public_url(url)
    check_robots(reviewed)
    return request_bytes(reviewed, publisher=True, byte_limit=8_000_000)


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
        raw = os.environ.get("ARTSCOUNCILMALTA_PACING_BOOTSTRAP", "")
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
    path = os.environ.get("ARTSCOUNCILMALTA_PACING_ARTIFACT_PATH")
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
    ownership = {hashlib.sha256((SOURCE_ID + "|" + profile["identity"]).encode()).hexdigest()[:24]: profile["url"]
                 for profile in PROFILES}
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
        if ownership.get(record["id"]) != record["url"]:
            raise AdapterError("Record identity/canonical outside reviewed own profiles", "validate")
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
        clocks = {field: datetime.fromisoformat(record[field].replace("Z", "+00:00"))
                  for field in ("created_at", "updated_at", "first_seen_at",
                                "last_seen_at", "last_checked_at")}
        if not (clocks["created_at"] <= clocks["updated_at"] <= clocks["last_checked_at"]
                and clocks["first_seen_at"] <= clocks["last_seen_at"] <= clocks["last_checked_at"]):
            raise AdapterError("Incoherent owned record timestamp chronology", "validate")
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














def metadata(attempt, previous, records, error=None, checked_at=None):
    outcome = {
        "source": SOURCE_ID,
        "name": "Arts Council Malta — official funding, training, jobs and programme overviews",
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
            else f"Collected {len(records)} reviewed Arts Council Malta opportunities"
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
        or not {"source", "name", "source_url", "website_url", "language",
                "publisher_country", "publisher_type", "attribution", "status",
                "last_attempt_at", "last_success_at", "last_checked_at",
                "record_count", "message", "error"} <= set(previous)
        or previous.get("publisher_type") != PUBLISHER_TYPE
        or previous.get("attribution") != ATTRIBUTION
        or previous.get("name") != "Arts Council Malta — official funding, training, jobs and programme overviews"
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
            updated_at=before.get("updated_at", checked) if same else checked,
            last_seen_at=checked,
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



def public_url(value):
    parsed = urllib.parse.urlsplit(value)
    allowed = {info["url"] for info in INPUTS.values()} | {ROBOTS_URL}
    if (value not in allowed or parsed.scheme != "https" or parsed.username
            or parsed.password or parsed.port or parsed.fragment):
        raise AdapterError("Unreviewed public source route", "access")
    return value


def decoded_public_contact(ciphertext):
    """Decode only bounded CF public-email ciphertext, never a navigation URL."""
    if (not isinstance(ciphertext, str)
            or not re.fullmatch(r"[0-9a-fA-F]{6,510}", ciphertext)
            or len(ciphertext) % 2):
        raise AdapterError("Malformed protected public contact", "parse")
    raw = bytes.fromhex(ciphertext)
    try:
        email = bytes(value ^ raw[0] for value in raw[1:]).decode("utf-8", "strict")
    except UnicodeError as exc:
        raise AdapterError("Invalid protected contact encoding", "parse") from exc
    if (len(email) > 254
            or not re.fullmatch(
                r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
                r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
                r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+", email)
            or any(value in email.lower() for value in ("javascript:", "://"))):
        raise AdapterError("Invalid protected public email identity", "parse")
    return email


def protected_contact_facts(root):
    """Keep both independent target/label identities and their complete structure."""
    prefix = "/cdn-cgi/l/email-protection"
    contacts, normalized_hrefs = [], {}

    def attrs_for(node):
        attrs = dict(node.attrs)
        if node.duplicate_attrs:
            raise AdapterError("Duplicate protected contact attributes", "parse")
        href = attrs.get("href", "")
        if prefix in href:
            if href.startswith(prefix + "#"):
                attrs["href"] = "protected-email:" + decoded_public_contact(
                    href[len(prefix) + 1:])
                normalized_hrefs[id(node)] = attrs["href"]
            elif href != prefix:
                raise AdapterError("Unexpected protected contact route", "parse")
        if "data-cfemail" in attrs:
            attrs["data-cfemail"] = decoded_public_contact(attrs["data-cfemail"])
        return attrs

    def structure(node):
        return {"tag": node.tag, "attrs": attrs_for(node), "children": [
            structure(child) if isinstance(child, Node) else child
            for child in node.children]}

    def visit(node, path):
        href = node.attrs.get("href", "")
        if (prefix in href or "data-cfemail" in node.attrs
                or "__cf_email__" in node.attrs.get("class", "").split()):
            if node.tag not in ("a", "span"):
                raise AdapterError("Unexpected protected contact element", "parse")
            if "__cf_email__" in node.attrs.get("class", "").split() \
                    and "data-cfemail" not in node.attrs:
                raise AdapterError("Missing protected contact ciphertext", "parse")
            contacts.append({"path": path, "structure": structure(node)})
        for index, child in enumerate(node.children):
            if isinstance(child, Node):
                visit(child, path + [index])

    visit(root, [])
    return contacts, normalized_hrefs


def html_fingerprint(body, key=None):
    """Bind prose, links, controls and decoded public-contact identity/structure."""
    root = document_root(body.decode("utf-8", "strict"))
    contacts, normalized_hrefs = protected_contact_facts(root)
    links = sorted({normalized_hrefs.get(id(node), node.attrs["href"])
                    for node in nodes(root, "a") if node.attrs.get("href")})
    controls = [{name: node.attrs[name] for name in
                 ("class", "data-page", "data-max-page", "data-next-page")
                 if name in node.attrs}
                for node in root.walk()
                if "e-load-more-anchor" in node.attrs.get("class", "").split()]
    facts = {"text": root.text(), "links": links, "controls": controls}
    if contacts:
        facts["protected_contacts"] = contacts
    serialized = json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def read_input(key):
    info = INPUTS[key]
    status, headers, body = fetch_public(info["url"])
    if status != info["status"]:
        raise AdapterError("Reviewed input " + key + " returned unexpected HTTP "
                           + str(status), "fetch", status)
    if status == 404:
        if key not in ARCHIVED_GAPS or info["format"] != "pdf":
            raise AdapterError("Unreviewed missing material input", "fetch", status)
        return "reviewed-archived-404"
    content_type = headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
    expected = {"application/pdf"} if info["format"] == "pdf" else {"text/html", "application/xhtml+xml"}
    if content_type not in expected:
        raise AdapterError("Unexpected material content type: " + key, "parse")
    if info["format"] == "html":
        digest = html_fingerprint(body, key)
    else:
        if not body.startswith(b"%PDF-"):
            raise AdapterError("Invalid reviewed PDF: " + key, "parse")
        digest = hashlib.sha256(body).hexdigest()
    if digest != info["sha256"]:
        raise AdapterError("Reviewed source facts/frontier changed: " + key, "parse")
    return digest


def read_pages():
    """Exhaust all218 reviewed non-robots routes once, including known gaps."""
    return {key: read_input(key) for key in INPUTS}


def parse_inventory(pages):
    if set(pages) != set(INPUTS) or any(pages[k] != v["sha256"] for k, v in INPUTS.items()):
        raise AdapterError("Incomplete reviewed funding/terms frontier", "parse")
    records = []
    for profile in PROFILES:
        record = make_record(profile["title"], profile["url"], profile["categories"],
                             kind=profile["kind"], tags=["arts", "culture"],
                             host_countries=profile["hosts"],
                             evidence=["Reviewed own call: " + profile["leaf"],
                                       "Reviewed identity: " + profile["identity"]])
        record["id"] = hashlib.sha256((SOURCE_ID + "|" + profile["identity"]).encode()).hexdigest()[:24]
        record.update(summary=profile["summary"], summary_language="en",
                      language=profile["language"], deadline=profile["deadline"],
                      status=profile["status"])
        if profile["status"] == "open" and profile["deadline"]:
            value = profile["deadline"]
            end = (datetime.fromisoformat(value.replace("Z", "+00:00"))
                   if "T" in value else datetime.strptime(value, "%Y-%m-%d").replace(
                       hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc))
            if datetime.now(timezone.utc) > end:
                record["status"] = "expired"
        records.append(record)
    validate_records(records)
    if len(records) != 138:
        raise AdapterError("Incomplete reviewed identity partition", "validate")
    return records


def collect():
    if _PHASE_END is not None:
        check_deadline()
        return parse_inventory(read_pages())
    with execution():
        with phase("prepublication", 2100):
            prepare_collection()
            return parse_inventory(read_pages())


ROBOTS_URL = "https://artscouncilmalta.gov.mt/robots.txt"
INPUTS = {'funding': {'url': 'https://artscouncilmalta.gov.mt/en/apply-now/',
             'format': 'html',
             'status': 200,
             'sha256': 'af80a53dd546c0d16b8daac96d156792a823a2325d6abe5a1ee32c4ef425e420'},
 'funding2': {'url': 'https://artscouncilmalta.gov.mt/en/apply-now/2/',
              'format': 'html',
              'status': 200,
              'sha256': 'edaa01e8a4338df8624df550e388702cf348b273324401a58ec62d592bc70a84'},
 'funding3': {'url': 'https://artscouncilmalta.gov.mt/en/apply-now/3/',
              'format': 'html',
              'status': 200,
              'sha256': '26cbeb2b5da0c28826304aac74377757b8d6fb8e4be108738857ab33ba281495'},
 'funding4': {'url': 'https://artscouncilmalta.gov.mt/en/apply-now/4/',
              'format': 'html',
              'status': 200,
              'sha256': '42d0cfc792e3dd70e20ab7ec12b576af9d26d66d55f3e57770d248da133772c3'},
 'funding5': {'url': 'https://artscouncilmalta.gov.mt/en/apply-now/5/',
              'format': 'html',
              'status': 200,
              'sha256': '08594141790b4e8c20daa8a207cb4fa5163988a302b0b70379161467a5d45ca8'},
 'funding6': {'url': 'https://artscouncilmalta.gov.mt/en/apply-now/6/',
              'format': 'html',
              'status': 200,
              'sha256': '5222ad69db4d356427b7b29c524a176da5ea7a041c1f6d2f612a043c35586f9b'},
 'funding7': {'url': 'https://artscouncilmalta.gov.mt/en/apply-now/7/',
              'format': 'html',
              'status': 200,
              'sha256': 'd5e72c8f2357f942b9e359863f9edc7a66b03bcf298d13de9b6550acedcb761a'},
 'funding8': {'url': 'https://artscouncilmalta.gov.mt/en/apply-now/8/',
              'format': 'html',
              'status': 200,
              'sha256': 'c6d77b3c8c346a4bd899a21f72f5fe673d3629a98e24ebc2b107e3b67ed7f4b1'},
 'opportunities': {'url': 'https://artscouncilmalta.gov.mt/en/opportunities/',
                   'format': 'html',
                   'status': 200,
                   'sha256': 'cca833ae8bafc95d8fed1ec54d1388b64dddf8df0216051a72e32120040c22d1'},
 'leaf001': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/artivisti/',
             'format': 'html',
             'status': 200,
             'sha256': '61d09dca360c7a63f975d6dbc18e35e1de52c735b08d2fdcfadc9bbb13e5a31c'},
 'leaf002': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/screen-support-scheme-strand-6/',
             'format': 'html',
             'status': 200,
             'sha256': '49330753d811023bdec7a8d42b08fd72e15e798a6f871968e7a18c453415c588'},
 'leaf003': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/scheme-for-the-partial-subsidisation-of-bank-approved-loans-for-local-band-clubs/',
             'format': 'html',
             'status': 200,
             'sha256': '4558751c88b876f2ffc03912fb8359f223a207a7507856edb555cf53360997bb'},
 'leaf004': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/newspapers-support-scheme-3/',
             'format': 'html',
             'status': 200,
             'sha256': '8cfd25cb2244b436d09cc738ee3fb95df2b2d6e2ecc17fe6349c1596bd982a9c'},
 'leaf005': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/micro-grant-5/',
             'format': 'html',
             'status': 200,
             'sha256': 'f2bac057e1104c32102cc118d203e671eacc325d2c2eba47a91ef77edd3ae1ff'},
 'leaf006': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/artistic-heritage-scheme-4/',
             'format': 'html',
             'status': 200,
             'sha256': '082d7ac1f1342acf5dc9bf1e1ab7d5154e524aace928664626115de043cde50b'},
 'leaf007': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/international-participation-scheme-3/',
             'format': 'html',
             'status': 200,
             'sha256': '04df853aeecaa0e8e9a9b07524eacb3a9d4509b49b471b9e2f956140ab1cb80f'},
 'leaf008': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/apprenticeship-scheme-2/',
             'format': 'html',
             'status': 200,
             'sha256': '3fa997406a575423e897201dbe7882753b03930d896e1cf0176d27c87c1ffc6b'},
 'leaf009': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/klabb-3-16-grant/',
             'format': 'html',
             'status': 200,
             'sha256': 'd95b24f980f606b4ac72ada597d78923d82bda27f2ea4b7d73d56300aeb75daa'},
 'leaf010': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/legal-and-finance-grant/',
             'format': 'html',
             'status': 200,
             'sha256': 'bedd3f88bea5b631c990ea10dba403fcffee7b6e76db2dc372ba66fc0e6766e3'},
 'leaf011': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/culture-pass/',
             'format': 'html',
             'status': 200,
             'sha256': '92635b7b2e7ff66ec3999ee1f81d8d9f43284e5f60e13ae5bcf184e1ecccb86b'},
 'leaf012': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/maltese-music-on-private-radio-stations/',
             'format': 'html',
             'status': 200,
             'sha256': '0b1b2ee22bbb9e699935c272b9c687cb14a796a30d65c259e89a673211f4f64f'},
 'leaf013': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/the-presidents-fund-for-creativity-3/',
             'format': 'html',
             'status': 200,
             'sha256': 'c6d9c0a575e3e6c239c8aac6744adcf7e8aa3e675f6e42b54a9f07b43602aed8'},
 'leaf014': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/arts-support-scheme-7/',
             'format': 'html',
             'status': 200,
             'sha256': 'cef35ea9536bac9400fd7eb9cf6abf59d762ea6fc9df6634da4c21520a21989f'},
 'leaf015': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/training-and-developmentsupport-scheme/',
             'format': 'html',
             'status': 200,
             'sha256': '37a14c5e7b779fee8444fe3db2e20228999ea4e0fa1010a76c5b3cc2f86f152b'},
 'leaf016': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/theatre-productions-support-scheme/',
             'format': 'html',
             'status': 200,
             'sha256': '7ce25b426c0259602e050894f129208d665e91d985d6168597e2e0292124cac4'},
 'leaf017': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/international-cultural-exchanges-scheme-5/',
             'format': 'html',
             'status': 200,
             'sha256': 'a53066ce212c7a56c293bb40d5e5977e86803265987f21b8c1c0cf3e1b7ba0f6'},
 'leaf018': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/arts-in-schools-scheme-5/',
             'format': 'html',
             'status': 200,
             'sha256': '23b537a305680bb70457954eb669eb083dbdbc6cd9a638274723749ae71c63e3'},
 'leaf019': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/screen-support-scheme-strand-1-to-5/',
             'format': 'html',
             'status': 200,
             'sha256': 'a500153d507af25a324e777508cd5de5a6b1643020f5559b433f2ec065223999'},
 'leaf020': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/the-culture-and-health-platform-fund-for-the-maltese-islands/',
             'format': 'html',
             'status': 200,
             'sha256': '3a01c0276768a87c3249533418914e5093ba403cdb094defb179aa588c0aa670'},
 'leaf021': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/artistic-research-and-development-scheme-3/',
             'format': 'html',
             'status': 200,
             'sha256': '6037b69783f75116b69be057b1e0d86c91ac0ce7c200508a26225ea7228c45a0'},
 'leaf022': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/restoration-funding-scheme-4/',
             'format': 'html',
             'status': 200,
             'sha256': '002f279e5abe585ac3404d46b40e4e6220116a843af3752109ff94ec00c3b6eb'},
 'leaf023': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/creative-innovators-and-platforms-fund/',
             'format': 'html',
             'status': 200,
             'sha256': '355d197cdd0cbd7d091a39bc1f79223ca164625c95d91be8adcb1dd4fa3c8554'},
 'leaf024': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/screen-support-scheme-3/',
             'format': 'html',
             'status': 200,
             'sha256': 'cf99d4192b2906c2fbdb949338de25eecb7bb6d72cf7828322ddea80e090fdf4'},
 'leaf025': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/investing-in-cultural-organisations-malta/',
             'format': 'html',
             'status': 200,
             'sha256': '832a026af2fe9117e274cd83878c5875d03a640228a52801648e2417235c2342'},
 'leaf026': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/newspapers-support-scheme-2/',
             'format': 'html',
             'status': 200,
             'sha256': 'f0c1d765f9353387bc358dc317d006d94bd070396d8b432e8b473f9e38b1d55f'},
 'leaf027': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/sabbatical-for-artistic-research-grant-3/',
             'format': 'html',
             'status': 200,
             'sha256': '54e8962d55bfefd914fb7b523d412b6db17d98f08cd99d638d2481462420b1f0'},
 'leaf028': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/micro-grant-4/',
             'format': 'html',
             'status': 200,
             'sha256': '66da64216803b394f0af4f1af9ba0153a89ac702a7a9bf2c70d0f0fa33754a5c'},
 'leaf029': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/artistic-heritage-scheme-3/',
             'format': 'html',
             'status': 200,
             'sha256': 'bcc46abf611358f0068f55af0596ebdb08b245b6aa46da9e8d40066fcf16ae82'},
 'leaf030': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/international-participation-scheme/',
             'format': 'html',
             'status': 200,
             'sha256': '090d498d13e6ffd9653e1a0a99961d75b25284309463fceb9483c12ba7a026b1'},
 'leaf031': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/arts-support-scheme-right-to-culture-4/',
             'format': 'html',
             'status': 200,
             'sha256': '7c0eae9f7a49e69eeb92fef7a72f391b140d193ba4819c4484f83b054e8c4ef4'},
 'leaf032': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/maltese-musicon-private-radio-stationssupport-scheme/',
             'format': 'html',
             'status': 200,
             'sha256': 'b8b55c2fb1ca16959a50b8c767bdee12ef9fc5e8c081fc950fdfed1f28e79386'},
 'leaf033': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/regional-cultural-cooperation-programme-3/',
             'format': 'html',
             'status': 200,
             'sha256': '48dd126ef00308be5ac7cb1c9a52ebfadd567c6d1816a0a8a6ff4b2e0bd55b57'},
 'leaf034': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/the-presidents-fund-for-creativity-2/',
             'format': 'html',
             'status': 200,
             'sha256': '2efc00e7369ee01459becd2d5cb8734fd31a738988cb537df8cea7bd5df1ebbf'},
 'leaf035': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/training-and-development-support-scheme/',
             'format': 'html',
             'status': 200,
             'sha256': '6c6eb9ee0bb9bdbe2461500f1defe4dd64710a51de77daa6f9d1b764f98aa65e'},
 'leaf036': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/arts-support-scheme-6/',
             'format': 'html',
             'status': 200,
             'sha256': 'db0ac349bfa9d24fef42532c045d5127f5cf3dc10ee83fec60c5568bdd9b4164'},
 'leaf037': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/international-cultural-exchanges-scheme-4/',
             'format': 'html',
             'status': 200,
             'sha256': '3f95ef1dbc50e85ce0bb17715f3958fda7454852049e4169432fd09fbf33f1bd'},
 'leaf038': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/culture-and-health-platform-fund-for-the-maltese-islands/',
             'format': 'html',
             'status': 200,
             'sha256': '5a66c4a9595a40e4e39269b017245ae75deca9307bb8839f4c4c1b5e2d586707'},
 'leaf039': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/arts-in-schools-scheme-4/',
             'format': 'html',
             'status': 200,
             'sha256': '5aab80cc8a09ea1df1db1cb659bd7804ced06386c4a4593d18861bc6301fdcf6'},
 'leaf040': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/ghajnuna-finanzjarja-ghall-parrocci/',
             'format': 'html',
             'status': 200,
             'sha256': '446d1b6d687b53571f35595ba2005513cdc1b1097c537a1b608ba896d9345a78'},
 'leaf041': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/screen-support-scheme/',
             'format': 'html',
             'status': 200,
             'sha256': '2187d1327e68c55d43148a409f6ab9e170499100b13297ce18a9a5d341445ab7'},
 'leaf042': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/co-funding-of-creative-europe-cooperation-projects/',
             'format': 'html',
             'status': 200,
             'sha256': '97cf6fb75cf0229f0e35093b7fa7d1ba38ad481a89e1fe66504779c646259c34'},
 'leaf043': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/access-support-programme/',
             'format': 'html',
             'status': 200,
             'sha256': '247921726527cb61a223e6f61de4a25810136c09286526527f46efd89114ba73'},
 'leaf044': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/call-for-series-of-bookmarks/',
             'format': 'html',
             'status': 200,
             'sha256': '0715848a3d00e6d0b7bbb64e45761aa5c20d586364644f9e7a098ee24642f43e'},
 'leaf045': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/artistic-research-and-development-scheme/',
             'format': 'html',
             'status': 200,
             'sha256': 'b91ff0ff7a737e2339fd8c23e18a0628c58eadcd8f78f8b8d505943964af8632'},
 'leaf046': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/restoration-funding-scheme/',
             'format': 'html',
             'status': 200,
             'sha256': 'ca35370bdfc07ced352b00ccd71c05aa457466c12eb88dc28e9fde5aae295c99'},
 'leaf047': {'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/general-budget-guidelines/',
             'format': 'html',
             'status': 200,
             'sha256': 'ff01f635daff5dacbff12e0b5bcb34ab7abb81923918d45ef4fa895bd3413e46'},
 'leaf048': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-for-the-position-of-administrator-at-arts-council-malta-within-the-ministry-for-arts-culture-and-national-heritage/',
             'format': 'html',
             'status': 200,
             'sha256': '63212be12139fe6c5c7ac9aa417eca02dd222e615b4f652054167d199c30287e'},
 'leaf049': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/euro-connection-2027-call-for-projects/',
             'format': 'html',
             'status': 200,
             'sha256': '7af8bf3ecead94f9dde92f9d93263063fde373be1e9fe93db75ec332d1d7e086'},
 'leaf050': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/an-introduction-to-audience-design-with-sile-culley/',
             'format': 'html',
             'status': 200,
             'sha256': '8ea989d9210a188da55fb31079de330670d32e9e3973bc0654e4cb0742047c04'},
 'leaf051': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/the-european-film-festival-2026/',
             'format': 'html',
             'status': 200,
             'sha256': '88e03d948863074b69d190b666beef2e4da4e90ba12700a21b77d0470056058c'},
 'leaf052': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/the-artists-journey-what-it-really-takes-to-become-a-professional-artist/',
             'format': 'html',
             'status': 200,
             'sha256': '2b4f24bca41945cb429968311e291817901d8568069bb3d8acafdf164c2ac19e'},
 'leaf053': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/rockestra-the-evolution-open-call-for-photographers/',
             'format': 'html',
             'status': 200,
             'sha256': 'ae33dd006e9bebe829f22864261fc15f50bf0d50179c415eb5f656e300a48174'},
 'leaf054': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/open-call-for-producers-thessaloniki-agora-delegation-2026/',
             'format': 'html',
             'status': 200,
             'sha256': '5d1aeb3ab2c52eea1ebb9183302e95c765793f58a85a4416f3c01f1696d51e0c'},
 'leaf055': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/series-rough-pitch-the-balkan-way-6-call-for-tv-drama-series-in-development/',
             'format': 'html',
             'status': 200,
             'sha256': '56ea02d7e3571a10f9d615ed63a8b1bcb73793ea0a9b43c241f301ff6bf3be38'},
 'leaf056': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-for-the-position-of-executive-education-development-at-arts-council-malta-within-the-ministry-for-arts-culture-and-national-heritage/',
             'format': 'html',
             'status': 200,
             'sha256': '7ec31a3434a64f7f68af2eb36630ce5e5855407283afca40258ad6de6a6c772a'},
 'leaf057': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/specialised-local-training/',
             'format': 'html',
             'status': 200,
             'sha256': '011cf78ecf52224f25a8eb73b2fc073ba49ba897a49a5eb0502ceee698bf4118'},
 'leaf058': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/audience-design-workshops-october-2026/',
             'format': 'html',
             'status': 200,
             'sha256': '44af59f1991c19a3eab363d6e480366059678e9fcd45de213f28ce1685c2b276'},
 'leaf059': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/open-calls-for-porto-industry-days-2026/',
             'format': 'html',
             'status': 200,
             'sha256': '6c1a79c67b3e726cee5606309b1a1011d93084fa9ac901a26771664d0c83b165'},
 'leaf060': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/open-call-for-international-residencies-in-hamburg/',
             'format': 'html',
             'status': 200,
             'sha256': '7b6bfebc38582ccad95232578e0a1ae2415a8d86c51e98e25ed6ac79c867dc47'},
 'leaf061': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/esf-03-243-strengthening-maltas-cultural-and-creative-sectors-through-lifelong-learning/',
             'format': 'html',
             'status': 200,
             'sha256': '91607b483b6fb4414fc43ba28d94079aa06ecf126474168f22858b7747069275'},
 'leaf062': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-sportarti/',
             'format': 'html',
             'status': 200,
             'sha256': 'b5339f34137ab1338c35292aeff682a27d50dcde4d7f42e4bf872f9b4b0b5af3'},
 'leaf063': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-service-4/',
             'format': 'html',
             'status': 200,
             'sha256': 'd7747bbad74ca5a612d472c83bcc9f874bfaa99f5942cf7e278e3ba6ad77b67b'},
 'leaf064': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/co-production-roadmap-masterclass-for-maltese-producers-filmmakers/',
             'format': 'html',
             'status': 200,
             'sha256': '3c9d38ea827b9a65bed10b972e583eff980dfc1a6ca6c569e4410d71eab03855'},
 'leaf065': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/recreate-europe/',
             'format': 'html',
             'status': 200,
             'sha256': 'bf0063c635a082a92d75b947a859bd4bf88e76a061bc8e8abfbdd9ca846f06e6'},
 'leaf066': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/open-applications-for-the-workshop-and-pitching-forum-lets-pitch-some-shorts/',
             'format': 'html',
             'status': 200,
             'sha256': '143503cc949df45d1ab92c9a95c42736616aa27a77cc999b1831d967c6bc5a6b'},
 'leaf067': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-service-support-coordinator-funding-at-arts-council-malta/',
             'format': 'html',
             'status': 200,
             'sha256': '659b8be653a163d09835ef918ab940453890d1b9e84966229a787ca4b23610dc'},
 'leaf068': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/executive-funds-position-at-arts-council-malta-2/',
             'format': 'html',
             'status': 200,
             'sha256': 'ba21ccdbb71720cbfa76068b2be15c3e838f6d15e08047ed706254a05e386dca'},
 'leaf069': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/open-call-for-participation-in-the-music-cities-convention-2026/',
             'format': 'html',
             'status': 200,
             'sha256': 'd9995884d78492cd6a5eab4c052facc551c9d4525e01091d648d5e6adac03ee0'},
 'leaf070': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/artivisti/',
             'format': 'html',
             'status': 200,
             'sha256': 'c2d08734f10d2286ea890472dd3318c2ae57f818547acc23d03af483023398d9'},
 'leaf071': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/training-grants-for-the-artistic-community/',
             'format': 'html',
             'status': 200,
             'sha256': 'f198f2818c087e5b7ab6dfc2b83c1ef589f9f59b246fdfb3545444898e2ad6bd'},
 'leaf072': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-for-the-position-of-executive-communications-at-arts-council-malta-within-the-ministry-for-culture-lands-and-local/',
             'format': 'html',
             'status': 200,
             'sha256': '96863df530f356ac0f88ae552fcf0c02e879d3b824c288041b3212919e3096bc'},
 'leaf073': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-creating-futures-rethinking-cultural-institutions-infrastructure-and-investment/',
             'format': 'html',
             'status': 200,
             'sha256': '12254178ea97d468c37e739b4db45c1ed34d3f2fc534830fd64081671e95d61f'},
 'leaf074': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-service-3/',
             'format': 'html',
             'status': 200,
             'sha256': '4d49076b652444ba115d1bfff228f1528c93d75cde8fae7c945e7afbcaa7a9c0'},
 'leaf075': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-service-2/',
             'format': 'html',
             'status': 200,
             'sha256': 'f6b5953016f340dab71549d5802e3316a24e82072c245ac4a80a50a8662f8ad1'},
 'leaf076': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/open-call-for-illustrators-and-graphic-designers-to-attend-the-london-book-fair/',
             'format': 'html',
             'status': 200,
             'sha256': '6afb8da980210e9956261a85c0f32f2e3b1bffdf12fb2ce26fb6057281116b8e'},
 'leaf077': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-evaluators-2026-2027/',
             'format': 'html',
             'status': 200,
             'sha256': 'beb877cc0b897ff2a9696632773fd4869fbefa6cd63d74760a6cef858e600a08'},
 'leaf078': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/edinburgh-fringe-the-malta-showcase/',
             'format': 'html',
             'status': 200,
             'sha256': '465cd018941ec12fba237f172d7fbb6a5c296a43b16188c89f7ab3df32759e0c'},
 'leaf079': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-service/',
             'format': 'html',
             'status': 200,
             'sha256': 'e2d72ed4f1f97780362447db2c84041e650d4adb7e105577c870e45cdc497a06'},
 'leaf080': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/erasmus-traineeship-at-the-national-pavilion-of-malta-la-biennale-di-venezia-2026/',
             'format': 'html',
             'status': 200,
             'sha256': '7ef948c78adfcc6e4d179a340a7c21d188b17b3b61757c73ec4405baa0083c19'},
 'leaf081': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/executive-funds-position-at-arts-council-malta/',
             'format': 'html',
             'status': 200,
             'sha256': '9a9989d6564dbde81288a5a4179fa87827bff6e03a62743950fb03b92238c11e'},
 'leaf082': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-service-executive-communications-with-arts-council-malta/',
             'format': 'html',
             'status': 200,
             'sha256': 'e3cab0501683bb3c204069772f8adb14994dbf748b18bf92866699737337a436'},
 'leaf083': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/cultural-representative-for-arts-council-malta-london-based/',
             'format': 'html',
             'status': 200,
             'sha256': 'ac46c8a947ab0cb18f8d18e49bc54aed35e803425bce20e76edb6b570449c9e4'},
 'leaf084': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-for-the-position-of-executive-international-cultural-relations/',
             'format': 'html',
             'status': 200,
             'sha256': '1406c11d397bf6b31a9c53e10f1bf2f37ce549a1116a66ef190c167c37c249a7'},
 'leaf085': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-service-for-support-administrators-with-arts-council-malta/',
             'format': 'html',
             'status': 200,
             'sha256': 'bd883c07ba527f896001829d1eb9d8ed03e9586440a52c1841e8c321e31b85d8'},
 'leaf086': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-for-the-position-of-director-strategy-at-arts-council-malta/',
             'format': 'html',
             'status': 200,
             'sha256': '240207d583c122b2369cbcefa03f33d2b1f1e6fd81e0491dd194c3cd89adc8a6'},
 'leaf087': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-for-the-position-of-senior-officer-finance-at-arts-council-malta/',
             'format': 'html',
             'status': 200,
             'sha256': '5c48f4343c9aead9e7adb2c214775b7aedc943783cfb8fcd07f37326e8d2f8c7'},
 'leaf088': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/halaqat-open-call-for-artists-2026/',
             'format': 'html',
             'status': 200,
             'sha256': '2be0ef50311435f6641b7ec5d2ffb517c8de084c7aa86a86127cb3f42c9ff20a'},
 'leaf089': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/culture-pass-2025-2026/',
             'format': 'html',
             'status': 200,
             'sha256': 'ea63c8dcbf9fe376898a0b3a8ab6b485e6d5a2580f690529e49c4c3a0f458435'},
 'leaf090': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/open-call-for-illustrators-and-graphic-designers-to-lead-live-drawing-sessions-at-the-arts-council-malta-stand-at-the-malta-book-festival-2025/',
             'format': 'html',
             'status': 200,
             'sha256': 'b5d543c1e5b6f4601f5a4b0b832c16e4b35097bacfef09a299d9d0607d8b280f'},
 'leaf091': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/open-call-for-designs-for-the-stand-of-arts-council-malta-at-the-malta-book-festival-2025/',
             'format': 'html',
             'status': 200,
             'sha256': 'dc4bf46ef46fdddd749436800f2f9d197d15d248465b2ddfd919ac2ad1d885f3'},
 'leaf092': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-project-administrator-eu-funds/',
             'format': 'html',
             'status': 200,
             'sha256': '665b0f8ed92c232f90d75518103e9ed15292e8e8e81616660b8ca50178e8fd2f'},
 'leaf093': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-for-communications-coordinator-eu-funds/',
             'format': 'html',
             'status': 200,
             'sha256': '1cc41be6334e7485d591909a3f6516c649c59e70899a03d55fc943d742806146'},
 'leaf094': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-for-the-position-of-executive-internationalisation-at-arts-council-malta/',
             'format': 'html',
             'status': 200,
             'sha256': 'f9bbeea0eed47e61df56e98aa229cac355a77b9ffebea59334d182149429ccad'},
 'leaf095': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-for-the-position-of-head-development-operations-at-arts-council-malta/',
             'format': 'html',
             'status': 200,
             'sha256': '4004b306462888627325ef10519a90863bc54344402195f289a7b83037004ae6'},
 'leaf096': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-project-manager-eu-funds/',
             'format': 'html',
             'status': 200,
             'sha256': '2ff7670c3af4911f0a0ecbbd7eda4ede649cf2cc34c877ac2db8b8511d317f60'},
 'leaf097': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-artistic-proposals/',
             'format': 'html',
             'status': 200,
             'sha256': 'bb0364da761aacc3eb42d5c2eeab798ff8ea421ee6e3db547aefeff523134699'},
 'leaf098': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-an-artistic-team-for-the-malta-pavilion-at-the-16th-gwangju-biennale-2026/',
             'format': 'html',
             'status': 200,
             'sha256': '8f77e70fb6d5da2030b04df8de67a6cf4a5bcba071f9864fe31fb9e8c94a22c7'},
 'leaf099': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/artist-in-residence-call-2025/',
             'format': 'html',
             'status': 200,
             'sha256': 'e760beaf565120c67d34a64938256f3c161e89e0c8b306f245987c3482b1faa8'},
 'leaf100': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/open-call-studio-francis-ebejer/',
             'format': 'html',
             'status': 200,
             'sha256': '2770a829fada31a13b6cdf7ac757850d25faf3a5c91722238fbcc8a144f73e7e'},
 'leaf101': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/now-open-call-for-a-curatorial-team-for-the-malta-pavilion-at-the-61st-international-art-exhibition-at-la-biennale-di-venezia-2026/',
             'format': 'html',
             'status': 200,
             'sha256': 'bba7571867e90d2b36b668a9254370c5034e686a644f4200d564e0b4ffc41aa4'},
 'leaf102': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/open-call-for-participation-in-the-music-cities-convention-2025/',
             'format': 'html',
             'status': 200,
             'sha256': '360f12321958433ee8dd300bae71b7ef9ab7ec1a5ffdea00066dda5c04046600'},
 'leaf103': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-the-applications-for-the-position-of-executive-digital-engagement-at-arts-council-malta/',
             'format': 'html',
             'status': 200,
             'sha256': 'c7d7954415f7a39b7af692b52b6b59e09da67443a8e8a792cc87d1171ee2f4ae'},
 'leaf104': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-the-position-of-executive-partnerships-advocacy-and-collaborationsat-arts-council-malta/',
             'format': 'html',
             'status': 200,
             'sha256': 'ee960c36258becb78e1651cf3532ef5ad8d58dcecb09b0c5f6e2ae281c802d19'},
 'leaf105': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-executive-funds-position-at-arts-council-malta/',
             'format': 'html',
             'status': 200,
             'sha256': '0b0b33a005821a41598b2b5b4d7613ffd535464523717115cab3a3b58ddd1fde'},
 'leaf106': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/atlas-of-invisible-borders-call-for-applications/',
             'format': 'html',
             'status': 200,
             'sha256': '2e22e7538db77693f50c78fd2389419ca66cf0f01700d16b4aec4db28a7243ac'},
 'leaf107': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/open-calls/',
             'format': 'html',
             'status': 200,
             'sha256': '64cc6a91ef3ceb3d497d3de47b3ced97f53b52294e5d03e0d758f4f9b3577182'},
 'leaf108': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-for-the-position-of-executive-communications-at-arts-council-malta/',
             'format': 'html',
             'status': 200,
             'sha256': 'c25f4e0e4da394d1aa2616e8cdd2d2e3e8b67c1edd305939ccb25a1cf1f6d980'},
 'leaf109': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/reduced-tax-rate-for-creative-practitioners/',
             'format': 'html',
             'status': 200,
             'sha256': '9f22535b7c9f6ceebb21f0d2f9eb8c64e616b0992bef1201c89c9f0c96c04290'},
 'leaf110': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/professional-development-initiative-for-book-illustrators-at-the-london-book-festival/',
             'format': 'html',
             'status': 200,
             'sha256': 'b94ca681c9b762d1547438f6dc399df219096aef024caaaca89ec746f73b9d8c'},
 'leaf111': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/newspapers-support-scheme/',
             'format': 'html',
             'status': 200,
             'sha256': 'e3fb8a5e916fe75f2f22e82020162a6177b6497ff5113f66706396799f3362b1'},
 'leaf112': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-proposals-for-original-musicals-in-the-maltese-language-2022-2026/',
             'format': 'html',
             'status': 200,
             'sha256': '34c88dedcf4294881525e7a148fd04ab5cba5e0f0003ffd2825ea7e66dbccebd'},
 'leaf113': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/artistic-financing-for-feast-associations-scheme/',
             'format': 'html',
             'status': 200,
             'sha256': '93aeb76636288d81505c56cbef2fe8f06996821a9257dcc3d70130b2bdd135d4'},
 'leaf114': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/fund-offering-financial-support-to-maltese-fireworks-factories-working-on-a-voluntary-basis/',
             'format': 'html',
             'status': 200,
             'sha256': '047e260560ef6594e97d02b950b35c441c9d04a139d5079e434162ca6d8987ca'},
 'leaf115': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/fund-for-the-purchase-of-musical-instruments-for-the-local-musical-societies/',
             'format': 'html',
             'status': 200,
             'sha256': '28dacc4ac4245c789f5c089e64f0cc0a83ce5a69ce362acd31dfe9c5f139af67'},
 'leaf116': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/fund-to-strengthen-cultural-work-done-by-band-clubs/',
             'format': 'html',
             'status': 200,
             'sha256': '84241795b17e638c0d5ccee11db55360c6ef8b29b02557beec5eee29b75d56dc'},
 'leaf117': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/national-opportunities/',
             'format': 'html',
             'status': 200,
             'sha256': '6fda50f06b6c01744e89425b3f005a79f9d649f613b674db84e2916e28ad38fe'},
 'leaf118': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/young-artist-development-programme/',
             'format': 'html',
             'status': 200,
             'sha256': 'aef91ff44c84d69f2e360c5a612596b4992894e7ba2ff2da1d144d27b7b39f64'},
 'leaf119': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/public-spaces-art-fund/',
             'format': 'html',
             'status': 200,
             'sha256': '34b3e765f44615af97e1a60d88ecf78b9494842a9adec6a15701920f06eb001e'},
 'leaf120': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/radio-new-talent/',
             'format': 'html',
             'status': 200,
             'sha256': '7c32fb03a53563b68686299b1d6be859e7c7a27b3fcacb049b1740f4c08864ff'},
 'leaf121': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/il-premju-tal-president-ghall-kreattivita/',
             'format': 'html',
             'status': 200,
             'sha256': 'ff5c8bec02d4cd13c25294893b7e2af5fd8f450186e1b5f5f55e40878aff9749'},
 'leaf122': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/tax-deduction-on-fees-paid-for-cultural-and-creative-courses/',
             'format': 'html',
             'status': 200,
             'sha256': '38c48a24eb9592671dd06f8a907e992153a68523b114112bd673b603cf286a6b'},
 'leaf123': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/150-tax-deduction-on-donations-to-culture/',
             'format': 'html',
             'status': 200,
             'sha256': 'a27390fe2b0ceb357df88c12c71be20c9883b8e34edc1f4fe295c4cd300d55aa'},
 'leaf124': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/il-premju-ghall-arti-2025/',
             'format': 'html',
             'status': 200,
             'sha256': 'f9b171c62a0df9cd302258333b2a6fe6ece227d262673e94f09f4918069ea99e'},
 'leaf125': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/restoration-funding-scheme/',
             'format': 'html',
             'status': 200,
             'sha256': 'b3a022409cc7c40897357ef7d2677cc34e7065939a834e515d68b8b208e71dc6'},
 'leaf126': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/the-state-of-the-arts-malta-national-symposium-2024/',
             'format': 'html',
             'status': 200,
             'sha256': 'daf77896674eb990fbede1c537aef87ca2d7e46f6aba83e8957d2aaa7dc54869'},
 'leaf127': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/2025-global-connect-open-call/',
             'format': 'html',
             'status': 200,
             'sha256': '78f4b0691b54f3b638c3f95d3a762ecfddf72002d4de537db9f75a203c6a29ff'},
 'leaf128': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/culture-pass-2024/',
             'format': 'html',
             'status': 200,
             'sha256': 'f2813382c098a6cbef86ed4b5dfb83b882fd192bec5a6c5df7d56d113d745ddf'},
 'leaf129': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/ex-rampol-birkirkara-bands-one-time-support-scheme/',
             'format': 'html',
             'status': 200,
             'sha256': '3fdac36d8cab6f690a88e36b40d386c862cd1f30ac184714e4d6b19c035b75b4'},
 'leaf130': {'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/the-state-of-the-arts-malta-national-symposium-2024-2/',
             'format': 'html',
             'status': 200,
             'sha256': '114cbe443bc49544881d3441e7ea33e480a2978b8bccb6dd777e49f24bde7660'},
 'privacy-final': {'url': 'https://artscouncilmalta.gov.mt/politika-ta-privatezza/',
                   'format': 'html',
                   'status': 200,
                   'sha256': 'ae7b4c16e401e5f89ab1b96de65671923497dd5b68b2118f39e02addf96d84eb'},
 'cookie-final': {'url': 'https://artscouncilmalta.gov.mt/politika-tal-cookies/',
                  'format': 'html',
                  'status': 200,
                  'sha256': '612326ce3a4b7ef13d13dcf38275c25005d0c731ad90a920f66cb9204922c04f'},
 'terms-extra1': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/09/NSS-Guidelines-Regulations-2026-1.pdf',
                  'format': 'pdf',
                  'status': 200,
                  'sha256': '807f75e2dfd01bf064c6c699c94f892d22f4187348cab97c00940dbc8fdbfd65'},
 'terms-extra2': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/08/Micro-Grant-Guidelines-Regulations-2026-Updated-20.08.2026.pdf',
                  'format': 'pdf',
                  'status': 200,
                  'sha256': '8f0ec57b68ea114e2a0fc732c40c6352e89201e78d75f213bc3b1fd1d797059e'},
 'terms-extra3': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/09/EC27_appel_ENG_final.pdf',
                  'format': 'pdf',
                  'status': 200,
                  'sha256': '83a6fe77f04cfdbb7d0f0037cc154cde8f74d626a5d3c20ed415500acfe917dc'},
 'terms-extra4': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/07/ESF.03.243-Specialised-Local-Training-CALL-for-applications-final-1.pdf',
                  'format': 'pdf',
                  'status': 200,
                  'sha256': 'a9f1bebd0cfc47b290fab35b319664577438d401ac480efdbe5b9c6c69e156e1'},
 'terms-extra5': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/05/ESF.03.243-2.1-Artistic-Community-CALL-for-applications-v2-1.pdf',
                  'format': 'pdf',
                  'status': 200,
                  'sha256': 'ba53a5e735c1c3ef696e1ad7aa49be47be533149cb3e32e539dc39fd90054132'},
 'terms-extra6': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/01/ACM26-Edinburgh-Fringe-call-for-applications-EN.pdf',
                  'format': 'pdf',
                  'status': 200,
                  'sha256': '5bee89f4a82dfe2957c19f3987ae42046968045dcd0ef3adc2002d746763ec50'},
 'terms-halaqat': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/07/Call-for-Artists_2026_Application-PDF.pdf',
                   'format': 'pdf',
                   'status': 200,
                   'sha256': '92e0192dce1ddd6dcc78adacb883b0e1a416719327f66a9ff551fca91040c069'},
 'terms001': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/02/Artivisti-Guidelines-Regulations-2026.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '38e07a5275bf1c7775333aebb1c184a2435bc1f6d425dbfa30bf394a2530cd72'},
 'terms002': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/03/SSS-Strand-6-Guidelines-Regulations-2026-2.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '1a73463c0789a358e67c0e656805ac3e5ffb399eb4cba1a8b72c88638a045461'},
 'terms003': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/11/Guidelines-Regulations-Subsidisation-Local-Band-Clubs-2.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'a0cd97530d26fd91b0e353ed6782e43ebe85989001ad892c4f262f08cecf5c37'},
 'terms004': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/02/AHS-Linji-Gwida-u-Regolamenti-2026.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '5436a6f5684899716e7b472ae9b4b8dc0ad3ca4d611d70119cc32fd25335c297'},
 'terms005': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/01/IPS-Guidelines-Regulations-2026.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '4f9320dc3e0ccc53380bdeb72de50bf83f45f6c83fe77693dc44a4365babb844'},
 'terms006': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/03/Apprenticeship-Scheme-2026-25.03.2026.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '0c078c5380d11d810431fdba6a2816e4f266652b0f9645df102475f26567a288'},
 'terms007': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/07/KLB-Guidelines-Regulations-2026-Updated-21.07.2026.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '95bf13bcdd76ca1fbf88055b5fc98a8a43f854964ba62750f1520c0cd606c31d'},
 'terms008': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/07/LFG-Guidelines-Regulations-2026_27.07.1016.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'ccfac7a0321fe4570415ae19bdb62ab2c489175f1e8fcc73fe596537299fdd4b'},
 'terms009': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/06/EN-Culture-Pass-Guidelines-2026.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'ddd711d80be7ac760bfb79f3f3834085c3bcdf651eebf357364eb5c28329cdb2'},
 'terms010': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/05/FTP-Guidelines-Regulations-2026-v2-25052026.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'bba60489bb146332aac32b915d78e0e71794fc2b5be6d4de2eafd62e35733a23'},
 'terms011': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/05/ARTS-Guidelines-Regulations-2026-Updated-30.04.2026.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'f6c2e3c0eef3da35fc822b608ea01a9eb5f14921d1976e71e8fc017d8bf2d6da'},
 'terms012': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/01/TDS-Guidelines-Regulations-2026.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'f344ccc18ba2c2d74f23646c4175246d7ce522de857e2ef6823ed13d2fd810e1'},
 'terms013': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/04/TPS-Guidelines-Regulations-2026.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '72efff2b91ffbd2d40be0cc50ba8274836384140861d99a1b34c074db2fbeb39'},
 'terms014': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/04/International-Cultural-Exchanges-Scheme-Guidlines-2026.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '8d242fc2f75ac4fe0f795fe0c0fb89a1eda242df99d2d862814b6839cbbed49d'},
 'terms015': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/03/Arts-in-Schools-Guidelines-Regulations.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'a7dc0cd40e82a59532ccf015a6a242320527a3a84724f4ae0e00b4a3f8ed34b5'},
 'terms016': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/03/SSS-Strands-1-to-5-Guidelines-Regulations-2026-1.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'f3abeab0fa45be8b6542acfeafb3ce35efec8921c03db2b569eb373dedcabb63'},
 'terms017': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/02/CAH-Strand-1-Guidelines-Regulations-2026.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '9e15711824e8e91ca076987a344f269e312042dc2e136612a83d8e298014332b'},
 'terms018': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/02/CAH-Strand-2-Guidelines-Regulations-2026.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'da636aa4d46156e7af52bee44dc7a42e90c054e4aab50a0535ec5796ec20f77f'},
 'terms019': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/02/ARD-Guidelines-Regulations-2026-1.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '5eb4231f0f49fd0d99a67ff567117a47efa81327749f44e9f10d6f969ec44a93'},
 'terms020': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/12/RFS-Guidlines-2026-final.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '69edca79986072a5014fe84d0533a71514cc85abed0bb3330ed60f2cf6403ee5'},
 'terms021': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/05/CIP-Guidelines-Regulations-Updated-08.05.2026.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '81adb2165aef138c398f2c3f8485087198a6c8ac085568a22ae3e8d372e95221'},
 'terms022': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/03/Screen-Support-Scheme-Guidelines-and-Regulations-Strands-1-5-2025-1-1.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'c3294ccc8dfd10f69af04c58ad1c58c880bdbde8b17998942b16fa412ad94b24'},
 'terms023': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/04/Screen-Support-Scheme-Guidelines-and-Regulations-Strands-1-5-2025-Updated-16th-April-2025-1.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '0b4a4df9fc1cab3bab14d1b113f8dcd46414d80586040b60ecfc3fed63fe6402'},
 'terms024': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/06/Screen-Support-Scheme-G-R-Strand-6-International.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '4bd06c31e123528e68b5c1381de7ff9f9e79d53aa8b755f9251ed81f955576b6'},
 'terms025': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/11/ICOM-Strand-1-2-Guidelines-and-Regulations-modified.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'a4059c4f014566a9da4d9652a52e09f48e07c443d39a9405ccdcca37d2b10894'},
 'terms026': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/09/Newspaper-Support-Scheme-Guidelines-2025.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '6ce440a86ee21901663a12d45f785d7f1388400907b2b9d13241557d5a899dde'},
 'terms027': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/09/Sabbatical-Guidelines-and-regulations-2025-EN.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'fa2c2ffec3c3a6afd9877f02ecb9daa9813f6dfe34ec9d2094e685fe5e027e08'},
 'terms028': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/08/Micro-Grant-Guidelines-2025-Final.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '7aa423ea0d1970c52e729c239647ef2f41a31f5a1bc5522710991df83cb2581b'},
 'terms029': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/02/Artistic-Heritage-Scheme-2025-1.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '7feafa0090a4798423d28db7c408762beaffc3073e98520c97469f17fd38dccd'},
 'terms030': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/08/IPS-Guidelines-2025-final.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'bd1a1bd2dd50060eb27c5813d224dcf2efa87d4f2efd1cb9fd67a34d0f84ad88'},
 'terms031': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/07/Arts-Support-Scheme-Right-to-Culture-GR-2025.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '416488581f2756f7dd40cc840d62f79f2c3776218803cf4f530677fd7ca9b473'},
 'terms032': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/08/Maltese-Music-on-Private-Radio-Stations-Support-Scheme-Guidelines-2025.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '1343ec179aa6bd8f54603748afc04206d11660099e2aa50bb252fbffc0d040a6'},
 'terms033': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/06/RCCP-guidelines-2025-EN.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'b9ea23ef98bdc818736b1e4ccb335ba3a09dfd3751115f9cb648fa86057fb8eb'},
 'terms034': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/08/FTP-Guidelines-2025-Final-06.08.2025.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'cc0ad97c5bb6de7e00dec370e108ddce0c9d576a1e76a1659bfbba8bc6e79094'},
 'terms035': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/05/Training-and-Development-Support-Scheme-2025-final-1.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '42d0a6bf07274924924df15c2988faab5a01eda5c11f13ef81fd2e04c15e359d'},
 'terms036': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/05/Arts-Support-Scheme-G-R-2025-final.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'e7ae929f9f3f3217153c4cd45caf01fe605fda0629eea6ab548adc8581743e38'},
 'terms037': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/04/International-Cultural-Exchanges-Scheme-Guidelines-2025-final.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '7b3a473f5c9905fd60959497c4257ab5be0c88b2e62cfa25dfbfbbd5c68e117f'},
 'terms038': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/04/Culture_and_Health_Fund_Guidelines_2025-final.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'd06ffe4c6f7c79a7dd38401cf6d9233538d13388626f96c25cce851e79a49149'},
 'terms039': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/03/Arts-in-Schools-Scheme-Guidelines-2025.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '5e9df4c08e6ed18b6ab5bc83b48d8651bcb2cc9cfffa844f8782dd3dc7549d1b'},
 'terms040': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/03/ghajnuna-finanzjarja-ghall-parrocci-linji-gwida.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '9d46ade6fd76a50f67798c22abd907eea946b544087a4c63e8bae9c81384e363'},
 'terms041': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/02/Screen%20Support%20Scheme%20-%20Guidelines%20and%20Regulations%20Strands%201%20-%205%20AUGUST%202024%20version%202.pdf',
              'format': 'pdf',
              'status': 404,
              'sha256': 'reviewed-archived-404'},
 'terms042': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/02/ADDENDUM%20-%20start%20of%20works%20-%20bidu%20ta%20xoghlijiet.pdf',
              'format': 'pdf',
              'status': 404,
              'sha256': 'reviewed-archived-404'},
 'terms043': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/02/Access-Support-GR-2025-1.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'a46b07a76f4e0317536b8be98a857506430d33ddb59e22c67af15d0ecf91101e'},
 'terms044': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/02/Call-for-Series-of-BookMark-GR.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '893a88714e041a536b45886192a9ff92d4a31be0c00190e76080db40f8966774'},
 'terms045': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/01/Artistic-Research-and-Development-Scheme-2025.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '49381b9e0e1a6d136392f53f62902df7e90dccbd32a6a4bd8a0ec23b34cd85c8'},
 'terms046': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/01/RSF-Guidlines-2025.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'd877b9904f87304ce2327e2ddecbc5bc9d211fa24323f10f3dd7eac592c6c987'},
 'terms047': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/07/Thessaloniki-AGORA-Delegation-2026-Guidelines-EN.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'ae4219b2f0eeee7617cbdfabaa61943449a74c5d5a9e98555f83d1351efa3373'},
 'terms048': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/06/sportarti-2026-final.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '766fbd9d6520742444aec5e7f24fc02486a8d963f86857c076542a77c5128586'},
 'terms049': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/03/ACM_Music-Cities-Convention_Open-Call_Guidelines_2026_Hull.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '66d30edb28521b916165d3225c3f1d612563b427a816f7b9297a463ab154a2a8'},
 'terms050': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/01/EN_Call-for-Artists-Participation-at-LBF-2026.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'b59c28b4cce567a50ac1830ade0344ae62eef6d8fd0f32abe1627922973486ed'},
 'terms051': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/06/EN-Culture-Pass-Guidelines-2025-2.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '724fbb872b64806810f6bfd3e011828d641a711f362569290bfaf7c35fa9f36b'},
 'terms052': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/06/Open-call-for-illustrators-open-call-1.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '8616fb8a4bf6dc2d028a7cd2e7db215aa946d5717f7764f312b9db4a7a28c2f5'},
 'terms053': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/06/Final-draft_Call-for-Artists-to-Design-Stand.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '8d35d2007942e7170cecb61d5233ae69e00406277e5750b3e91e57c18583fc32'},
 'terms054': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/04/ACM-Gwanju-26_Guidelines-1.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'd8d04c49ffa2b3ded978500ee5397280799d24b1501a600b1ffafc8fe947ca09'},
 'terms055': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/04/TM_SFE_Call-EN.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'c30263f0f64acd61ca0b2d34d41a63648da3147ff5b077bbc553e9351d284750'},
 'terms056': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/03/ACM-Venice-Art-Biennale-open-call-2026-OP2-1.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': 'd3c11373157fc2d46d52a229b1a1ba6de71962104687a1d77033fe9b8e81f95c'},
 'terms057': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/02/Sound-diplomacy-open-call-final-v1.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '81a3ef1dcc0dcbb4a57c215f6cfe9166320d0df157fac3ae4343eebcd1c3e727'},
 'terms058': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/02/Newspaper%20Support%20Scheme%20Guidelines%20.pdf',
              'format': 'pdf',
              'status': 404,
              'sha256': 'reviewed-archived-404'},
 'terms059': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/02/ORIGINAL%20MUSICALS%20IN%20THE%20MALTESE%20LANGUAGE%20updated%2021.06.2021.pdf',
              'format': 'pdf',
              'status': 404,
              'sha256': 'reviewed-archived-404'},
 'terms060': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/02/Fond%20Kmamar%20tan-nar%20-%20Linji%20Gwida%202021%2010.05.2021.pdf',
              'format': 'pdf',
              'status': 404,
              'sha256': 'reviewed-archived-404'},
 'terms061': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/02/Education%20and%20Purchase%20of%20a%20Musical%20Instrument-Guidelines%202019%20-min-1.pdf',
              'format': 'pdf',
              'status': 404,
              'sha256': 'reviewed-archived-404'},
 'terms062': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/02/Linji%20Gwida%20-%202021%20V2.pdf',
              'format': 'pdf',
              'status': 404,
              'sha256': 'reviewed-archived-404'},
 'terms063': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/02/Young%20Artist%20Development%20Fund%20Guidelines_22-02-2019.pdf',
              'format': 'pdf',
              'status': 404,
              'sha256': 'reviewed-archived-404'},
 'terms064': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/02/Public%20Spaces%20Art%20Fund%202021%20guidelines.pdf',
              'format': 'pdf',
              'status': 404,
              'sha256': 'reviewed-archived-404'},
 'terms065': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/02/Radio%20New%20Talent%20Scheme%20guidelines%20and%20regulations.pdf',
              'format': 'pdf',
              'status': 404,
              'sha256': 'reviewed-archived-404'},
 'terms066': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/02/PtP%20Strand%202%20Guidelines_2021%2022.07.2021.pdf',
              'format': 'pdf',
              'status': 404,
              'sha256': 'reviewed-archived-404'},
 'terms067': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/02/PtP%20Guidelines_2021%2027052021_compressed.pdf',
              'format': 'pdf',
              'status': 404,
              'sha256': 'reviewed-archived-404'},
 'terms068': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2026/02/Incentive-Guidelines-v3-02.2026.pdf',
              'format': 'pdf',
              'status': 200,
              'sha256': '306e9d37b6ba8048fc1e3841b03eb8806704f354e6f9e91ad8af393ac60674c4'},
 'terms069': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/02/Culture%20Pass%20Guidelines%202024.pdf',
              'format': 'pdf',
              'status': 404,
              'sha256': 'reviewed-archived-404'},
 'terms070': {'url': 'https://artscouncilmalta.gov.mt/wp-content/uploads/2025/02/Ex-Rampol%20Guidelines%20and%20regulations.pdf',
              'format': 'pdf',
              'status': 404,
              'sha256': 'reviewed-archived-404'}}
ARCHIVED_GAPS = {'terms041',
 'terms042',
 'terms058',
 'terms059',
 'terms060',
 'terms061',
 'terms062',
 'terms063',
 'terms064',
 'terms065',
 'terms066',
 'terms067',
 'terms069',
 'terms070'}
PROFILES = [{'key': 'leaf001',
  'leaf': 'leaf001',
  'identity': 'artivisti-2026',
  'title': 'Artivisti',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/artivisti/',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-03-30',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2026 call for individual creatives aged 18–25 in 2026 who are Maltese '
             'citizens or hold the specified Malta residence/citizenship documents. Artivisti '
             'combines 18 months of mentoring with a project of at most 12 months and up to €4,000 '
             '(up to 100% eligible costs), not a €20,000 individual award. The programme includes '
             'a July 7–9 residency and staged payments; prior funding compliance and the full '
             'guidelines apply. The stated closing date is March 30,2026 at noon, without a time '
             'zone.'},
 {'key': 'leaf002',
  'leaf': 'leaf002',
  'identity': 'screen-support-strand6-2026',
  'title': 'Screen Support Scheme Strand 6',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/screen-support-scheme-strand-6/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': [],
  'deadline': '2026-11-18',
  'status': 'open',
  'language': 'en',
  'summary': 'Rolling 2026 international screen-support call until November 18 at noon (no zone '
             'stated), or earlier if funds run out. Active Malta-registered audiovisual entities '
             'and eligible individual professionals must meet ownership/residence, rights and '
             'scheme requirements. Caps: promotion €20,000 (€40,000 at A-list festivals), '
             'participation €5,000 (€10,000 A-list), submissions €2,500; up to 100% eligible '
             'costs. Completed works must be within 18 months or in advanced development; support '
             'is not automatic.'},
 {'key': 'leaf003',
  'leaf': 'leaf003',
  'identity': 'band-club-loan-interest-2026',
  'title': 'Scheme for the Partial Subsidisation of Bank-Approved Loans for Local Band Clubs',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/scheme-for-the-partial-subsidisation-of-bank-approved-loans-for-local-band-clubs/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2026-11-17',
  'status': 'open',
  'language': 'en',
  'summary': 'Registered, compliant Maltese voluntary band clubs may seek a subsidy of 50% of '
             'annual interest on an approved bank loan, for up to 10 consecutive years. The '
             '€500,000 limit concerns eligible loan principal, not a cash award. Applicants '
             'arrange their bank loan and meet the scheme/state-aid conditions. Rolling '
             'applications close November 17,2026 at noon (no zone stated), or when funds are '
             'exhausted; the eligible period is 2026–2035.'},
 {'key': 'leaf004',
  'leaf': 'leaf004',
  'identity': 'newspapers-support-2026',
  'title': 'Newspapers Support Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/newspapers-support-scheme-3/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2026-11-03',
  'status': 'open',
  'language': 'en',
  'summary': 'Registered companies or qualifying voluntary organisations producing officially '
             'listed print newspapers can seek up to €12,000 for Maltese-language linguistic '
             'services or €20,000 for arts/culture content. Support may cover 100% of eligible '
             '2027 project costs, with staged payment and strand-specific publication '
             'requirements. These are institutional project caps, not payments to readers or '
             'journalists. Apply by November 3,2026 at noon; no time zone is stated. Full '
             'eligibility/state-aid rules apply.'},
 {'key': 'leaf005',
  'leaf': 'leaf005',
  'identity': 'micro-grant-2026',
  'title': 'Micro Grant',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/micro-grant-5/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-10-13',
  'status': 'expired',
  'language': 'en',
  'summary': 'The listing marks this 2026 Micro Grant CLOSED although its page gives October '
             '13,2026 at noon (no zone stated); do not assume applications remain open. Creative '
             'practitioners/individual artists with Maltese citizenship or specified Malta '
             'residence/citizenship documents must have received no ACM scheme funding in '
             '2024–2026. Up to €3,000 may cover 100% of eligible 2027 project costs. An applicant '
             'profile is required at least two weeks before the deadline; state-aid and full '
             'guideline conditions apply.'},
 {'key': 'leaf008-hosts',
  'leaf': 'leaf008',
  'identity': 'apprenticeship-hosts-2026',
  'title': 'Call for Hosts',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/apprenticeship-scheme-2/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-05-05',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2026 host call for qualifying sole traders, registered entities, cultural '
             'organisations, cooperatives and voluntary organisations proposing a full-year 2027 '
             'creative-sector apprenticeship. ACM pays hosts up to €18,000 in quarterly €4,500 '
             'instalments; hosts must provide training, mentorship and €18,000 apprentice '
             'remuneration over twelve months. Citizenship/residence, registration and full scheme '
             'conditions apply. Host deadline: May 5,2026; the September 23 deadline is for '
             'apprentices.'},
 {'key': 'leaf008-apprentices',
  'leaf': 'leaf008',
  'identity': 'apprenticeship-apprentices-2026',
  'title': 'Call for Apprentices',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/apprenticeship-scheme-2/',
  'categories': ['internships', 'training'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-09-23',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2026 call for eligible creative practitioners with a relevant MQF level 5+ '
             'qualification, matched to approved hosts for a full-year, in-person 2027 '
             'apprenticeship. Hosts must provide training, mentorship and €18,000 remuneration '
             'over twelve months, pre-financed by ACM through quarterly host payments. '
             'Citizenship/residence, host matching and full scheme conditions apply. Apprentice '
             'deadline: September 23,2026 at noon, no zone stated; hosts had a separate May 5 '
             'call.'},
 {'key': 'leaf071',
  'leaf': 'leaf071',
  'identity': 'artistic-community-training-grants-2026',
  'title': 'Training Grants for the Artistic Community',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/training-grants-for-the-artistic-community/',
  'categories': ['training', 'grants'],
  'kind': 'opportunity',
  'hosts': [],
  'deadline': '2026-11-30',
  'status': 'open',
  'language': 'en',
  'summary': 'Adults with Maltese citizenship or valid Malta residence documents meeting '
             'self-employed/private-or-NGO employee criteria may seek up to €2,000 for March '
             '23–December 31,2026 training. Overseas training: ≤2 weeks in other EU states, '
             'Iceland, Liechtenstein, Norway or UK; online: ≤80 contact hours. Apply ≥15 working '
             'days before training; rolling deadline November 30 or earlier fund exhaustion. '
             'Eligible costs vary by format; employer endorsement, no double funding and full '
             'rules apply.'},
 {'key': 'leaf088',
  'leaf': 'leaf088',
  'identity': 'halaqat-residencies-2026',
  'title': 'Halaqat Open Call for Artists 2026',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/halaqat-open-call-for-artists-2026/',
  'categories': ['grants'],
  'kind': 'programme-overview',
  'hosts': ['LB', 'JO', 'MA', 'EG'],
  'deadline': '2025-07-31T11:00:00.000Z',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed Halaqat 2026 residency framework for adults from AND based in its Arab/EU '
             'focus countries; EU-based artists need recent Arab-region engagement. A publicly '
             'presented artistic track record, gender/care project and host-specific criteria are '
             'required. Six-week/three-month residencies in Lebanon, Jordan, Morocco or Egypt '
             'offer €1,750/€2,950 plus conditional top-ups. Up to two host applications are '
             'allowed, but only one can be selected. Deadline: July 31,2025,12:00 CET. Read the '
             'complete eligibility and host terms.'},
 {'key': 'leaf006',
  'leaf': 'leaf006',
  'identity': 'artistic-heritage-2026',
  'title': 'Artistic Heritage Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/artistic-heritage-scheme-4/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2026-10-06',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2026 project grant for compliant voluntary band/feast music societies, '
             'fireworks factories and feast-decoration organisations with the relevant statutory '
             'activity. Caps are €8,000 for band/feast music, €5,000 for fireworks health/safety '
             '(€2,500 where the factory is not owned by the organisation), and €5,000 for '
             'semi-permanent artistic decorations; up to 100% eligible costs. Deadline: October 6, '
             '2026 at noon, no zone stated. Full eligibility, project and payment rules apply.'},
 {'key': 'leaf007',
  'leaf': 'leaf007',
  'identity': 'international-participation-2026',
  'title': 'International Participation Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/international-participation-scheme-3/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': [],
  'deadline': '2026-09-23',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2026 outgoing mobility grant for adult Maltese or Malta-based '
             'artists/creative practitioners and eligible registered entities, collectives, '
             'cooperatives and voluntary organisations. Up to €2,000 may cover 100% of eligible '
             'international participation costs, with staged payment and '
             'citizenship/residence-document requirements. The current listed deadline was '
             'September 23, 2026 at noon without a stated zone; guidelines also describe an '
             'earlier March session. No individual award is guaranteed.'},
 {'key': 'leaf009',
  'leaf': 'leaf009',
  'identity': 'klabb3-16-2026',
  'title': 'Klabb 3-16 Grant',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/klabb-3-16-grant/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-09-10',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2026 call for individual creative professionals proposing touring '
             'artistic/cultural activities for children aged 3–6 during January–June 2027. The '
             'children are audiences, not grant applicants. Up to €10,000 may cover 100% of '
             'eligible project costs, paid in stages; Maltese citizenship or specified Malta '
             'residence/citizenship documents, a coordinator, project and state-aid rules apply. '
             'Deadline: September 10, 2026 at noon, no zone stated. Five project places do not '
             'constitute five separate calls.'},
 {'key': 'leaf010',
  'leaf': 'leaf010',
  'identity': 'legal-finance-2026',
  'title': 'Legal and Finance Grant',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/legal-and-finance-grant/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-09-02',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2026 grant for creative practitioners and qualifying registered '
             'entities/collectives/cooperatives or economically active voluntary organisations '
             'that received no ACM scheme funding in 2024–2026. Applicants need Maltese '
             'citizenship or specified Malta residence/citizenship documents. Up to €3,000 may '
             'cover eligible legal/financial support costs; approved final reporting and state-aid '
             'rules apply. Deadline: September 2, 2026 at noon, no zone stated. Eligible activity '
             'runs July 13, 2026–December 31, 2027.'},
 {'key': 'leaf011',
  'leaf': 'leaf011',
  'identity': 'culture-pass-2026',
  'title': 'Culture Pass',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/culture-pass/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-08-11',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed Culture Pass 2026 proposal call for creatives aged 18+, artist collectives '
             'and qualifying cultural NGOs, producing experiences for school audiences through '
             'June 2027. This is an opportunity for creative providers, not a student scholarship. '
             'Maltese citizenship or specified residence/citizenship documents are required; '
             'organisations/activities financed through established government line votes are '
             'excluded. Deadline: August 11, 2026 at noon, no zone stated. Full selection, '
             'eligible-cost and delivery rules apply.'},
 {'key': 'leaf012',
  'leaf': 'leaf012',
  'identity': 'private-radio-maltese-music-2026',
  'title': 'Maltese Music on Private Radio Stations Support Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/maltese-music-on-private-radio-stations/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2026-08-04',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2026 institutional grant for Malta-registered companies operating licensed '
             'nationwide private radio or private DAB stations. Broadcast 13 original programmes '
             'per schedule across three schedules from October 2026; each needs 40 minutes of '
             'eligible recent Maltese music, at least 50% with Maltese lyrics. Submit the October '
             'schedule and meet state-aid rules. Deadline: August 4, 2026 at noon, no zone stated. '
             'The page names guidelines but supplies no live terms link; no unverified award '
             'amount is promised.'},
 {'key': 'leaf013',
  'leaf': 'leaf013',
  'identity': 'presidents-creativity-2026',
  'title': 'The President’s Fund for Creativity',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/the-presidents-fund-for-creativity-3/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-07-07',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2026 project/programme/practice grant targeting disadvantaged groups through '
             'arts and cultural participation. Eligible creative practitioners and qualifying '
             'entities/collectives/institutions must work in social/community development or a '
             'cultural/creative field and satisfy the full state-aid or non-state-aid rules. Up to '
             '€30,000 may cover 80% of eligible costs, subject to government funds and staged '
             'payment. Deadline: July 7, 2026 at noon, no zone stated; activity may run through '
             'August 28, 2028.'},
 {'key': 'leaf014',
  'leaf': 'leaf014',
  'identity': 'arts-support-2026',
  'title': 'Arts Support Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/arts-support-scheme-7/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-06-23',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2026 scheme supporting artistic development, experimentation, audience '
             'engagement and community-based projects by creative individuals and qualifying '
             'registered entities, groups, cooperatives or voluntary organisations. Up to €40,000 '
             'may cover 80% of eligible project costs, with staged payment and applicable '
             'state-aid/non-state-aid conditions. Deadline: June 23, 2026 at noon, no zone stated. '
             'Eligible activity runs August 12, 2026–March 11, 2028; the €700,000 session budget '
             'is not an individual award.'},
 {'key': 'leaf015',
  'leaf': 'leaf015',
  'identity': 'training-development-2026',
  'title': 'Training and Development Support Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/training-and-developmentsupport-scheme/',
  'categories': ['training', 'grants'],
  'kind': 'programme-overview',
  'hosts': [],
  'deadline': '2026-06-16',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2026 training/development scheme: Strand 1 supports adult creatives and '
             'qualifying groups/organisations or students; Strand 2 supports outstanding young '
             'artists aged 8–17 by result notification, with guardian involvement. Proposed '
             'training must complement existing formation, not replace it. Maltese citizenship or '
             'specified Malta residence/citizenship documents and full rules apply. Each strand '
             'allows up to €2,000/100% eligible costs, with staged payment. Listed deadline: June '
             '16, 2026 at noon, no zone stated; an earlier March session is separate.'},
 {'key': 'leaf016',
  'leaf': 'leaf016',
  'identity': 'theatre-productions-2026',
  'title': 'Theatre Productions Support Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/theatre-productions-support-scheme/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-06-09',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2026 theatre-production grant for creative individuals and qualifying '
             'registered entities, collectives, cooperatives or voluntary organisations. Up to '
             '€60,000 may cover 80% of eligible project costs, with staged payments and '
             'state-aid/non-state-aid rules. Deadline: June 9, 2026 at noon without a stated zone. '
             'Eligible project activity runs July 24, 2026–July 23, 2028. The scheme supports '
             'theatre practice and audience development; selection, project eligibility and the '
             'complete provider guidelines apply.'},
 {'key': 'leaf017',
  'leaf': 'leaf017',
  'identity': 'international-cultural-exchanges-2026',
  'title': 'International Cultural Exchanges Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/international-cultural-exchanges-scheme-5/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': [],
  'deadline': '2026-05-26',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2026 international cultural-exchange grant for creative individuals and '
             'qualifying registered entities, groups, cooperatives or voluntary organisations with '
             'Maltese citizenship or specified Malta residence/citizenship documents. Up to '
             '€15,000 may cover 100% of eligible project costs, with staged payment and applicable '
             'state-aid/non-state-aid rules. Deadline: May 26, 2026 at noon, no zone stated. '
             'Eligible activity runs July 10, 2026–January 23, 2028; the full scheme conditions '
             'apply.'},
 {'key': 'leaf018',
  'leaf': 'leaf018',
  'identity': 'arts-schools-2026',
  'title': 'Arts in Schools Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/arts-in-schools-scheme-5/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2026-04-21',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2026 institutional project grant for formal educational institutions/colleges '
             'from early years to tertiary education that are active in a creative field. A '
             'project coordinator and the specified citizenship/residence documentation are '
             'required. Up to €5,000 may cover 100% of eligible artistic/cultural project costs '
             'for scholastic year 2026–2027, with staged payment. Deadline: April 21, 2026 at '
             'noon, no zone stated. Students are beneficiaries, not individual grant applicants; '
             'full scheme rules apply.'},
 {'key': 'leaf021',
  'leaf': 'leaf021',
  'identity': 'artistic-research-development-2026',
  'title': 'Artistic Research and Development Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/artistic-research-and-development-scheme-3/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-03-17',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2026 artistic research/development project grant for creative individuals and '
             'qualifying entities, collectives, cooperatives or voluntary organisations. Up to '
             '€15,000 may cover 80% of eligible costs, with staged payment and applicable '
             'state-aid/non-state-aid conditions. Deadline: March 17, 2026 at noon, no zone '
             'stated. Eligible research/project activity runs May 7, 2026–November 7, 2027. '
             'Community-based research is encouraged; the complete provider eligibility, '
             'exclusions and project rules apply.'},
 {'key': 'leaf022',
  'leaf': 'leaf022',
  'identity': 'restoration-2026',
  'title': 'Restoration Funding Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/restoration-funding-scheme-4/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2026-01-27',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2026 institutional restoration grant for qualifying voluntary or canon-law '
             'organisations, represented by an authorised applicant, for eligible parish cultural '
             'property in Malta/Gozo. Heritage must be at least 50 years old and meet the full '
             'property/project criteria. Up to €15,000 may cover 100% of eligible costs, with '
             'staged payment. Deadline: January 27, 2026 at noon, no zone stated. Eligible work '
             'runs March 6, 2026–March 6, 2027; this is not an individual arts scholarship.'},
 {'key': 'leaf057-module01',
  'leaf': 'leaf057',
  'identity': 'specialised-local-training-module-1',
  'title': 'Awareness of Cultural Rights',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/specialised-local-training/',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-08-24',
  'status': 'expired',
  'language': 'en',
  'summary': 'Completed/commenced module; listed deadline August 24,2026, training September 2026. '
             'Free independently selectable MQF 3 module at The Brewhouse, Birkirkara. Adults need '
             'Maltese citizenship/valid Malta residence and qualifying public employment, '
             'registered self-employment or private/NGO employment/endorsement. No qualification '
             'prerequisite. Attendance rules apply; accreditation requires assessment; late '
             'enrolment is conditional and cannot follow module start. Full strand rules apply.'},
 {'key': 'leaf057-module02',
  'leaf': 'leaf057',
  'identity': 'specialised-local-training-module-2',
  'title': 'Participation and Enjoyment of the Arts',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/specialised-local-training/',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-09-17',
  'status': 'expired',
  'language': 'en',
  'summary': 'Completed/commenced module; listed deadline September 17,2026, training October '
             '2026. Free independently selectable MQF 3 module at The Brewhouse, Birkirkara. '
             'Adults need Maltese citizenship/valid Malta residence and qualifying public '
             'employment, registered self-employment or private/NGO employment/endorsement. No '
             'qualification prerequisite. Attendance rules apply; accreditation requires '
             'assessment; late enrolment is conditional and cannot follow module start. Full '
             'strand rules apply.'},
 {'key': 'leaf057-module03',
  'leaf': 'leaf057',
  'identity': 'specialised-local-training-module-3',
  'title': 'Diversity, Equity and Inclusion (DEI) in Cultural Organisations',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/specialised-local-training/',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-10-20',
  'status': 'open',
  'language': 'en',
  'summary': 'Right to Culture applications advertised open; listed deadline October 20,2026, '
             'training November 2026. Free independently selectable MQF 4 module at The Brewhouse, '
             'Birkirkara. Adults need Maltese citizenship/valid Malta residence and qualifying '
             'public employment, registered self-employment or private/NGO employment/endorsement. '
             'No qualification prerequisite. Attendance rules apply; accreditation requires '
             'assessment; late enrolment is conditional and cannot follow module start. Full '
             'strand rules apply.'},
 {'key': 'leaf057-module04',
  'leaf': 'leaf057',
  'identity': 'specialised-local-training-module-4',
  'title': 'Community Cultural Development and Policy Engagement',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/specialised-local-training/',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-11-19',
  'status': 'open',
  'language': 'en',
  'summary': 'Right to Culture applications advertised open; listed deadline November 19,2026, '
             'training December 2026. Free independently selectable MQF 4 module at The Brewhouse, '
             'Birkirkara. Adults need Maltese citizenship/valid Malta residence and qualifying '
             'public employment, registered self-employment or private/NGO employment/endorsement. '
             'No qualification prerequisite. Attendance rules apply; accreditation requires '
             'assessment; late enrolment is conditional and cannot follow module start. Full '
             'strand rules apply.'},
 {'key': 'leaf057-module05',
  'leaf': 'leaf057',
  'identity': 'specialised-local-training-module-5',
  'title': 'Digital Access to Culture',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/specialised-local-training/',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-12-24',
  'status': 'open',
  'language': 'en',
  'summary': 'Right to Culture applications advertised open; listed deadline December 24,2026, '
             'training January 2027. Free independently selectable MQF 5 module at The Brewhouse, '
             'Birkirkara. Adults need Maltese citizenship/valid Malta residence and qualifying '
             'public employment, registered self-employment or private/NGO employment/endorsement. '
             'No qualification prerequisite. Attendance rules apply; accreditation requires '
             'assessment; late enrolment is conditional and cannot follow module start. Full '
             'strand rules apply.'},
 {'key': 'leaf057-module06',
  'leaf': 'leaf057',
  'identity': 'specialised-local-training-module-6',
  'title': 'Advocacy and Strategic Leadership for Cultural Rights',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/specialised-local-training/',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2027-01-19',
  'status': 'open',
  'language': 'en',
  'summary': 'Right to Culture applications advertised open; listed deadline January 19,2027, '
             'training February 2027. Free independently selectable MQF 5 module at The Brewhouse, '
             'Birkirkara. Adults need Maltese citizenship/valid Malta residence and qualifying '
             'public employment, registered self-employment or private/NGO employment/endorsement. '
             'No qualification prerequisite. Attendance rules apply; accreditation requires '
             'assessment; late enrolment is conditional and cannot follow module start. Full '
             'strand rules apply.'},
 {'key': 'leaf057-module07',
  'leaf': 'leaf057',
  'identity': 'specialised-local-training-module-7',
  'title': 'Foundations of Sustainable Cultural Practice',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/specialised-local-training/',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': ['MT'],
  'deadline': None,
  'status': 'unknown',
  'language': 'en',
  'summary': 'Announced for March 2027; registration opens later and the deadline is TBC, not a '
             'current open call. Free independently selectable MQF 3 module at The Brewhouse, '
             'Birkirkara. Adults need Maltese citizenship/valid Malta residence and qualifying '
             'public employment, registered self-employment or private/NGO employment/endorsement. '
             'No qualification prerequisite. Attendance rules apply; accreditation requires '
             'assessment; late enrolment is conditional and cannot follow module start. Full '
             'strand rules apply.'},
 {'key': 'leaf057-module08',
  'leaf': 'leaf057',
  'identity': 'specialised-local-training-module-8',
  'title': 'Cultural Organisations as Drivers of Sustainable Development',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/specialised-local-training/',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': ['MT'],
  'deadline': None,
  'status': 'unknown',
  'language': 'en',
  'summary': 'Announced for April 2027; registration opens later and the deadline is TBC, not a '
             'current open call. Free independently selectable MQF 3 module at The Brewhouse, '
             'Birkirkara. Adults need Maltese citizenship/valid Malta residence and qualifying '
             'public employment, registered self-employment or private/NGO employment/endorsement. '
             'No qualification prerequisite. Attendance rules apply; accreditation requires '
             'assessment; late enrolment is conditional and cannot follow module start. Full '
             'strand rules apply.'},
 {'key': 'leaf057-module09',
  'leaf': 'leaf057',
  'identity': 'specialised-local-training-module-9',
  'title': 'Sustainable Event and Venue Management',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/specialised-local-training/',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': ['MT'],
  'deadline': None,
  'status': 'unknown',
  'language': 'en',
  'summary': 'Announced for May 2027; registration opens later and the deadline is TBC, not a '
             'current open call. Free independently selectable MQF 4 module at The Brewhouse, '
             'Birkirkara. Adults need Maltese citizenship/valid Malta residence and qualifying '
             'public employment, registered self-employment or private/NGO employment/endorsement. '
             'No qualification prerequisite. Attendance rules apply; accreditation requires '
             'assessment; late enrolment is conditional and cannot follow module start. Full '
             'strand rules apply.'},
 {'key': 'leaf057-module10',
  'leaf': 'leaf057',
  'identity': 'specialised-local-training-module-10',
  'title': 'Cross-Sectoral Collaboration for Sustainability',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/specialised-local-training/',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': ['MT'],
  'deadline': None,
  'status': 'unknown',
  'language': 'en',
  'summary': 'Announced for June 2027; registration opens later and the deadline is TBC, not a '
             'current open call. Free independently selectable MQF 4 module at The Brewhouse, '
             'Birkirkara. Adults need Maltese citizenship/valid Malta residence and qualifying '
             'public employment, registered self-employment or private/NGO employment/endorsement. '
             'No qualification prerequisite. Attendance rules apply; accreditation requires '
             'assessment; late enrolment is conditional and cannot follow module start. Full '
             'strand rules apply.'},
 {'key': 'leaf057-module11',
  'leaf': 'leaf057',
  'identity': 'specialised-local-training-module-11',
  'title': 'Transformational Leadership and Corporate Social Responsibility in Culture',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/specialised-local-training/',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': ['MT'],
  'deadline': None,
  'status': 'unknown',
  'language': 'en',
  'summary': 'Announced for July 2027; registration opens later and the deadline is TBC, not a '
             'current open call. Free independently selectable MQF 5 module at The Brewhouse, '
             'Birkirkara. Adults need Maltese citizenship/valid Malta residence and qualifying '
             'public employment, registered self-employment or private/NGO employment/endorsement. '
             'No qualification prerequisite. Attendance rules apply; accreditation requires '
             'assessment; late enrolment is conditional and cannot follow module start. Full '
             'strand rules apply.'},
 {'key': 'leaf057-module12',
  'leaf': 'leaf057',
  'identity': 'specialised-local-training-module-12',
  'title': 'Strategic Sustainability Planning and Auditing in Cultural Organisations',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/specialised-local-training/',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': ['MT'],
  'deadline': None,
  'status': 'unknown',
  'language': 'en',
  'summary': 'Announced for September 2027; registration opens later and the deadline is TBC, not '
             'a current open call. Free independently selectable MQF 5 module at The Brewhouse, '
             'Birkirkara. Adults need Maltese citizenship/valid Malta residence and qualifying '
             'public employment, registered self-employment or private/NGO employment/endorsement. '
             'No qualification prerequisite. Attendance rules apply; accreditation requires '
             'assessment; late enrolment is conditional and cannot follow module start. Full '
             'strand rules apply.'},
 {'key': 'leaf057-module13',
  'leaf': 'leaf057',
  'identity': 'specialised-local-training-module-13',
  'title': 'Awareness of Entrepreneurship in Cultural and Creative Sectors',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/specialised-local-training/',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': ['MT'],
  'deadline': None,
  'status': 'unknown',
  'language': 'en',
  'summary': 'Announced for October 2027; registration opens later and the deadline is TBC, not a '
             'current open call. Free independently selectable MQF 3 module at The Brewhouse, '
             'Birkirkara. Adults need Maltese citizenship/valid Malta residence and qualifying '
             'public employment, registered self-employment or private/NGO employment/endorsement. '
             'No qualification prerequisite. Attendance rules apply; accreditation requires '
             'assessment; late enrolment is conditional and cannot follow module start. Full '
             'strand rules apply.'},
 {'key': 'leaf057-module14',
  'leaf': 'leaf057',
  'identity': 'specialised-local-training-module-14',
  'title': 'Cultural Awareness and Entrepreneurship Values',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/specialised-local-training/',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': ['MT'],
  'deadline': None,
  'status': 'unknown',
  'language': 'en',
  'summary': 'Announced for November 2027; registration opens later and the deadline is TBC, not a '
             'current open call. Free independently selectable MQF 3 module at The Brewhouse, '
             'Birkirkara. Adults need Maltese citizenship/valid Malta residence and qualifying '
             'public employment, registered self-employment or private/NGO employment/endorsement. '
             'No qualification prerequisite. Attendance rules apply; accreditation requires '
             'assessment; late enrolment is conditional and cannot follow module start. Full '
             'strand rules apply.'},
 {'key': 'leaf057-module15',
  'leaf': 'leaf057',
  'identity': 'specialised-local-training-module-15',
  'title': 'Business Modelling and Financial Planning for Cultural Enterprises',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/specialised-local-training/',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': ['MT'],
  'deadline': None,
  'status': 'unknown',
  'language': 'en',
  'summary': 'Announced for December 2027; registration opens later and the deadline is TBC, not a '
             'current open call. Free independently selectable MQF 4 module at The Brewhouse, '
             'Birkirkara. Adults need Maltese citizenship/valid Malta residence and qualifying '
             'public employment, registered self-employment or private/NGO employment/endorsement. '
             'No qualification prerequisite. Attendance rules apply; accreditation requires '
             'assessment; late enrolment is conditional and cannot follow module start. Full '
             'strand rules apply.'},
 {'key': 'leaf057-module16',
  'leaf': 'leaf057',
  'identity': 'specialised-local-training-module-16',
  'title': 'Marketing, Audience Development and Digital Strategy',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/specialised-local-training/',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': ['MT'],
  'deadline': None,
  'status': 'unknown',
  'language': 'en',
  'summary': 'Announced for January 2028; registration opens later and the deadline is TBC, not a '
             'current open call. Free independently selectable MQF 4 module at The Brewhouse, '
             'Birkirkara. Adults need Maltese citizenship/valid Malta residence and qualifying '
             'public employment, registered self-employment or private/NGO employment/endorsement. '
             'No qualification prerequisite. Attendance rules apply; accreditation requires '
             'assessment; late enrolment is conditional and cannot follow module start. Full '
             'strand rules apply.'},
 {'key': 'leaf057-module17',
  'leaf': 'leaf057',
  'identity': 'specialised-local-training-module-17',
  'title': 'Social Entrepreneurship and Future Business Models in CCS',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/specialised-local-training/',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': ['MT'],
  'deadline': None,
  'status': 'unknown',
  'language': 'en',
  'summary': 'Announced for February 2028; registration opens later and the deadline is TBC, not a '
             'current open call. Free independently selectable MQF 5 module at The Brewhouse, '
             'Birkirkara. Adults need Maltese citizenship/valid Malta residence and qualifying '
             'public employment, registered self-employment or private/NGO employment/endorsement. '
             'No qualification prerequisite. Attendance rules apply; accreditation requires '
             'assessment; late enrolment is conditional and cannot follow module start. Full '
             'strand rules apply.'},
 {'key': 'leaf057-module18',
  'leaf': 'leaf057',
  'identity': 'specialised-local-training-module-18',
  'title': 'Leadership and Resilience for Creative Entrepreneurs',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/specialised-local-training/',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': ['MT'],
  'deadline': None,
  'status': 'unknown',
  'language': 'en',
  'summary': 'Announced for March 2028; registration opens later and the deadline is TBC, not a '
             'current open call. Free independently selectable MQF 5 module at The Brewhouse, '
             'Birkirkara. Adults need Maltese citizenship/valid Malta residence and qualifying '
             'public employment, registered self-employment or private/NGO employment/endorsement. '
             'No qualification prerequisite. Attendance rules apply; accreditation requires '
             'assessment; late enrolment is conditional and cannot follow module start. Full '
             'strand rules apply.'},
 {'key': 'leaf020',
  'leaf': 'leaf020',
  'identity': 'culture-health-2026',
  'title': 'The Culture and Health platform fund for the Maltese islands',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/the-culture-and-health-platform-fund-for-the-maltese-islands/',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': ['MT'],
  'deadline': '2026-04-07',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2026 Culture and Health framework for Maltese or Malta-based emerging artists '
             'entering the arts/health/care field; emerging does not impose a young-age limit. '
             'Strand 1 offers up to €3,000 for prescribed international learning mobility, '
             'requiring host agreement; Strand 2 offers up to €9,000 for eligible artist-led '
             'projects, including qualifying entities. Relevant experience/training, named '
             'emerging artist leadership and full rules apply. Up to 100% eligible costs, paid '
             '80/20. Deadline: April 7,2026 at noon; no zone stated.'},
 {'key': 'leaf048',
  'leaf': 'leaf048',
  'identity': 'administrator-2026',
  'title': 'Call for Applications for the position of Administrator at Arts Council Malta within '
           'the Ministry for Arts, Culture and National Heritage',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-for-the-position-of-administrator-at-arts-council-malta-within-the-ministry-for-arts-culture-and-national-heritage/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-09-25',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed Administrator call: indefinite full-time employment, €22,654 basic salary '
             'plus €850 annual allowance. Relevant MQF 5 qualification or MQF 4 plus two years of '
             'experience, ICDL and Maltese/English communication are required. Maltese citizens '
             'and specified EU/equal-treatment, family, long-term-residence and '
             'UK-withdrawal-document applicants may qualify; employment licensing/recognition '
             'rules apply. Deadline: September 25,2026 at noon, no zone stated.'},
 {'key': 'leaf056',
  'leaf': 'leaf056',
  'identity': 'executive-education-development-2026',
  'title': 'Call for Applications for the Position of Executive (Education & Development) at Arts '
           'Council Malta within the Ministry for Arts, Culture and National Heritage.',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-for-the-position-of-executive-education-development-at-arts-council-malta-within-the-ministry-for-arts-culture-and-national-heritage/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-08-07',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed Education & Development executive call: relevant MQF 7 (90 ECTS) plus one '
             'year, or MQF 6 (180 ECTS) plus three years of experience, and Maltese/English '
             'communication. Qualifying citizenship/equal-treatment/residence routes and '
             'recognition/licensing rules apply. Indefinite full-time employment offers €35,877 '
             'base, €1,500 entity and €700 training allowances; performance/disturbance additions '
             'are each up to 15%, not guaranteed. Deadline: August 7,2026 at noon, no zone '
             'stated.'},
 {'key': 'leaf063',
  'leaf': 'leaf063',
  'identity': 'support-coordinator-strategy-2026',
  'title': 'Call for Service',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-service-4/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-07-06',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed Strategy Support Coordinator service call: relevant MQF 5 (30 ECTS) or MQF 4 '
             '(60 ECTS), strong English and the full role skills. Specified '
             'citizenship/equal-treatment/residence routes and recognition/licensing rules apply. '
             'Three-year contract, 40 hours/week mainly in Birkirkara; €29,000/year excluding VAT, '
             'invoiced monthly, plus discretionary performance bonus up to 10%. Renewal is '
             'conditional. Deadline: July 6,2026 at noon, no zone stated.'},
 {'key': 'leaf067',
  'leaf': 'leaf067',
  'identity': 'support-coordinator-funding-2026',
  'title': 'Call for Service – Support Coordinator (Funding) at Arts Council Malta',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-service-support-coordinator-funding-at-arts-council-malta/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-05-02',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed Funding Support Coordinator service call: relevant MQF 5 (30 ECTS) plus one '
             'year, or MQF 4 (60 ECTS) plus three years of experience; Maltese/English and full '
             'role skills required. Qualifying citizenship/equal-treatment/residence routes apply. '
             'Three-year contract, 40 hours/week, €29,000/year excluding VAT, with discretionary '
             'performance addition up to 10% and conditional renewal. Deadline: May 2,2026 at '
             'noon, no zone stated; qualification recognition/licensing rules apply.'},
 {'key': 'leaf068',
  'leaf': 'leaf068',
  'identity': 'executive-funds-april2026',
  'title': 'Executive (Funds) Position at Arts Council Malta',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/executive-funds-position-at-arts-council-malta-2/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-04-28',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed Funds executive call: relevant MQF 7 (90 ECTS) plus one year, or MQF 6 (180 '
             'ECTS) plus three years of experience, and full language/role requirements. '
             'Qualifying citizenship/equal-treatment/residence and recognition/licensing rules '
             'apply. Indefinite full-time employment, €35,877 base plus €1,500 entity and €700 '
             'training allowances; disturbance/performance additions each up to 15%, not '
             'guaranteed. Deadline: April 28,2026 at noon, no zone stated.'},
 {'key': 'leaf072',
  'leaf': 'leaf072',
  'identity': 'executive-communications-2026',
  'title': 'Call for Applications for the Position of Executive (Communications) at Arts Council '
           'Malta within the Ministry for Culture, Lands and Local',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-for-the-position-of-executive-communications-at-arts-council-malta-within-the-ministry-for-culture-lands-and-local/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-03-10',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed Communications executive call: relevant MQF 7 (90 ECTS) plus one year, or MQF '
             '6 (180 ECTS) plus three years of experience; Maltese/English and cultural-sector '
             'skills required. Specified citizenship/equal-treatment/residence and '
             'recognition/licensing rules apply. Indefinite full-time employment offers €35,877 '
             'base, €1,500 entity and €700 training allowances; performance/disturbance additions '
             'each up to 15%, not guaranteed. Deadline: March 10,2026 at noon, no zone stated.'},
 {'key': 'leaf074',
  'leaf': 'leaf074',
  'identity': 'support-administrators-2026',
  'title': 'Call for Service',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-service-3/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-02-20',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed service call for Support Administrators: school-leaving certificate and '
             'English/Maltese/Maths O-levels, Maltese/English fluency and full administrative '
             'skills. Specified citizenship/equal-treatment/residence routes and '
             'recognition/licensing rules apply. Three-year contract, 40 hours/week, €25,000/year '
             'excluding VAT; monthly payment requires a valid VAT invoice and renewal is '
             'conditional. Deadline: February 20,2026 at noon, no zone stated. Multiple positions '
             'do not create separate calls.'},
 {'key': 'leaf075',
  'leaf': 'leaf075',
  'identity': 'support-coordinator-funding-feb2026',
  'title': 'Call for Service',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-service-2/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-02-20',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed Funding Support Coordinator service call: relevant MQF 5 (30 ECTS) plus one '
             'year, or MQF 4 (60 ECTS) plus three years of experience; Maltese/English and full '
             'role skills. Specified citizenship/equal-treatment/residence routes and '
             'recognition/licensing rules apply. Three-year contract, 40 hours/week, €29,000/year '
             'excluding VAT; monthly VAT invoicing, discretionary performance addition and '
             'conditional renewal. Deadline: February 20,2026 at noon, no zone stated.'},
 {'key': 'leaf079',
  'leaf': 'leaf079',
  'identity': 'support-administrators-international-2026',
  'title': 'Call for Service',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-service/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-01-02',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed international-cultural-relations Support Administrator service call: MQF 4 or '
             'recognised equivalent plus four years of experience, Maltese/English and full role '
             'skills. Specified citizenship/equal-treatment/residence routes and '
             'recognition/licensing rules apply. One-year service contract, €25,000/year excluding '
             'VAT, paid against VAT invoices; renewal is conditional. Deadline: January 2,2026 at '
             'noon, no zone stated. Multiple places are part of one call.'},
 {'key': 'leaf092',
  'leaf': 'leaf092',
  'identity': 'project-administrator-eu-funds-2025',
  'title': 'Call for Project Administrator (EU funds)',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-project-administrator-eu-funds/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2025-06-09',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed EU-funds Project Administrator service call: relevant MQF 5 and at least two '
             'years of administrative/project-support experience, plus the full language/role '
             'criteria. Specified citizenship/equal-treatment/residence routes and '
             'recognition/licensing rules apply. Part-time 15 hours/week, stated three-year '
             'contract June 2025–December 2028 (retain the source wording); €9,953/year excluding '
             'VAT, monthly VAT invoices. Deadline: June 9,2025 at noon, no zone stated.'},
 {'key': 'leaf093',
  'leaf': 'leaf093',
  'identity': 'communications-coordinator-eu-funds-2025',
  'title': 'Call for applications for Communications Coordinator (EU funds)',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-for-communications-coordinator-eu-funds/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2025-06-09',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed EU-funds Communications Coordinator service call: relevant MQF 6 (180 ECTS) '
             'and at least two years of relevant experience, with full language/role skills. '
             'Specified citizenship/equal-treatment/residence routes and recognition/licensing '
             'rules apply. Part-time 10 hours/week, stated three-year contract June 2025–December '
             '2028 (source wording); €7,070/year excluding VAT, monthly VAT invoices. Deadline: '
             'June 9,2025 at noon, no zone stated.'},
 {'key': 'leaf096',
  'leaf': 'leaf096',
  'identity': 'project-manager-eu-funds-2025',
  'title': 'Call for Project Manager (EU funds)',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-project-manager-eu-funds/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2025-06-05',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed EU-funds Project Manager service call: relevant MQF 7 (90 ECTS) plus seven '
             'years of EU-project experience, SFD/procurement knowledge and Maltese/English '
             'skills. Specified citizenship/equal-treatment/residence routes and '
             'recognition/licensing rules apply. One-year, part-time 25 hours/week contract; '
             '€25,145/year excluding VAT, monthly VAT invoices, plus performance bonus up to 15%. '
             'Deadline: June 5,2025 at noon, no zone stated. Full qualification/completion '
             'alternatives apply.'},
 {'key': 'leaf104',
  'leaf': 'leaf104',
  'identity': 'partnerships-advocacy-executive-closed',
  'title': 'Call for the position of Executive (Partnerships, Advocacy and Collaborations)at Arts '
           'Council Malta – closed',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-the-position-of-executive-partnerships-advocacy-and-collaborationsat-arts-council-malta/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': None,
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed Partnerships, Advocacy & Collaborations executive call: relevant MQF 7 (60 '
             'ECTS) plus two years, or MQF 6 (180 ECTS) plus five years of relevant experience; '
             'Maltese/English fluency and full role skills. Indefinite full-time employment, '
             '€32,008 basic salary, with disturbance addition up to 15% and performance addition '
             'up to 10%, not guaranteed. The page says March 6 at noon without a year or zone: '
             'closing date remains unknown. Read the complete role and qualification criteria.'},
 {'key': 'leaf105',
  'leaf': 'leaf105',
  'identity': 'executive-funds-jobplus79-2025',
  'title': 'Call for Applications: Executive (Funds) Position at Arts Council Malta',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-executive-funds-position-at-arts-council-malta/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': None,
  'status': 'unknown',
  'language': 'en',
  'summary': 'Funds executive job call: relevant MQF 7 (60 ECTS) plus one year, or MQF 6 (180 '
             'ECTS) plus three years of experience; the source also gives recognised '
             'professional-qualification alternatives and full role skills. Indefinite full-time '
             'employment offers €32,008 base with disturbance addition up to 15% and performance '
             'addition up to 10%, not guaranteed. The stated July 16 noon deadline lacks a year '
             'and zone, so current application status/deadline remain unknown. See the original '
             'role criteria.'},
 {'key': 'leaf081',
  'leaf': 'leaf081',
  'identity': 'executive-funds-december-call',
  'title': 'Executive (Funds) Position at Arts Council Malta',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/executive-funds-position-at-arts-council-malta/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': None,
  'status': 'unknown',
  'language': 'en',
  'summary': 'Funds executive call: relevant MQF 7 (60 ECTS) plus one year, or MQF 6 (180 ECTS) '
             'plus three years, including recognised professional alternatives. Full role, '
             'citizenship/equal-treatment/residence and recognition/licensing criteria apply. '
             'Indefinite full-time employment offers €34,226 base, €1,500 entity and €700 training '
             'allowances; disturbance/performance additions each up to 15%, not guaranteed. The '
             'page says December 9 at noon without a year/zone: closing date and current '
             'application status remain unknown.'},
 {'key': 'leaf082',
  'leaf': 'leaf082',
  'identity': 'executive-communications-service-2025',
  'title': 'Call for Service: Executive (Communications) with Arts Council Malta',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-service-executive-communications-with-arts-council-malta/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2025-11-28',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed Communications service call: relevant MQF 7 (90 ECTS) plus one year, or MQF 6 '
             '(180 ECTS) plus two years; Maltese/English and cultural-sector skills. Qualifying '
             'citizenship/equal-treatment/residence and recognition/licensing rules apply. '
             'One-year contract, €35,000/year excluding VAT,15% disturbance allowance and '
             'discretionary performance bonus up to 15%; monthly VAT invoicing and conditional '
             'renewal. Deadline: November 28,2025 at noon, no zone stated.'},
 {'key': 'leaf083',
  'leaf': 'leaf083',
  'identity': 'london-cultural-representative-2025',
  'title': 'Cultural Representative for Arts Council Malta (London-based)',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/cultural-representative-for-arts-council-malta-london-based/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['GB'],
  'deadline': '2025-11-21T10:00:00.000Z',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed London-based Cultural Representative service call, requiring UK residence AND '
             'right to work, Maltese/English and relevant MQF 7+one year OR MQF 6+three years of '
             'experience. One-year contract averaging 40 hours/week; £30,147.12/year excluding VAT '
             'plus £5,000 living allowance and 15% disturbance allowance; VAT invoices and '
             'conditional renewal. An extension supersedes the original November 14 date: deadline '
             'November 21,2025, noon CEST as explicitly written. Full role/qualification rules '
             'apply.'},
 {'key': 'leaf084',
  'leaf': 'leaf084',
  'identity': 'executive-international-cultural-relations-2025',
  'title': 'Call for Applications for the Position of Executive (International Cultural Relations)',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-for-the-position-of-executive-international-cultural-relations/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2025-11-21T10:00:00.000Z',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed International Cultural Relations executive call: relevant MQF 7 (90 ECTS)+one '
             'year OR MQF 6 (180 ECTS)+three years OR MQF 5+five years; Maltese/English and full '
             'role skills. Specified citizenship/equal-treatment/residence and '
             'recognition/licensing rules apply. Indefinite full-time contract, €34,226 base and '
             '€1,500 entity allowance; performance/disturbance additions each up to 15%. Deadline: '
             'November 21,2025 noon CEST, preserving the explicitly stated zone rather than '
             'correcting it seasonally.'},
 {'key': 'leaf085',
  'leaf': 'leaf085',
  'identity': 'support-administrators-october2025',
  'title': 'Call for service for Support Administrators with Arts Council Malta',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-service-for-support-administrators-with-arts-council-malta/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2025-10-02',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed service call for three Support Administrators, represented as one recruitment '
             'call. School-leaving certificate, English/Maltese/Maths O-levels, Maltese/English '
             'fluency and administrative skills required. Three-year contract with conditional '
             'one-year renewal,40 hours/week, €25,000/year excluding VAT, monthly VAT invoices. '
             'Deadline: October 2,2025 at noon, no zone stated. Full role and appointment criteria '
             'apply; a writing sample is required at interview.'},
 {'key': 'leaf086',
  'leaf': 'leaf086',
  'identity': 'director-strategy-2025',
  'title': 'Call for Applications for the Position of Director (Strategy) at Arts Council Malta',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-for-the-position-of-director-strategy-at-arts-council-malta/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2025-09-26',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed Strategy Director call: relevant MQF 7 (90 ECTS)+five years OR MQF 6 (180 '
             'ECTS)+ten years of experience, with full leadership/language skills and qualifying '
             'citizenship/equal-treatment/residence rules. Three-year full-time contract, '
             'conditional renewal; €40,833 base plus €1,800 communication, €4,659 transport and '
             '€2,000 expense allowances, performance up to 15%. Deadline: September 26,2025 at '
             'noon, no zone stated. Recognition/licensing and complete role requirements apply.'},
 {'key': 'leaf087',
  'leaf': 'leaf087',
  'identity': 'senior-officer-finance-2025',
  'title': 'Call for Applications for the Position of Senior Officer (Finance) at Arts Council '
           'Malta',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-for-the-position-of-senior-officer-finance-at-arts-council-malta/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2025-09-17',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed Senior Finance Officer call: MQF 5 (120 ECTS)+three years OR specified '
             'accountancy/language/maths O-levels+ten years, eight using SAGE. ECDL, Excel and '
             'Maltese/English skills required; qualifying citizenship/equal-treatment/residence '
             'and recognition/licensing rules apply. Indefinite full-time employment, €28,832 '
             'basic salary. Deadline: September 17,2025 at noon, no zone stated. Read the complete '
             'qualification and role criteria.'},
 {'key': 'leaf094',
  'leaf': 'leaf094',
  'identity': 'executive-internationalisation-2025',
  'title': 'Call for Applications for the Position of Executive (Internationalisation) at Arts '
           'Council Malta',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-for-the-position-of-executive-internationalisation-at-arts-council-malta/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2025-06-06',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed Internationalisation executive call: relevant MQF 7 (90 ECTS)+one year OR MQF '
             '6 (180 ECTS)+two years OR MQF 5+five years, with Maltese/English and cultural-sector '
             'skills. Qualifying citizenship/equal-treatment/residence and recognition/licensing '
             'rules apply. Indefinite full-time employment, €32,798 base; disturbance addition up '
             'to 15% and performance addition up to 10%, not guaranteed. Deadline: June 6,2025 at '
             'noon, no zone stated. Complete role/qualification requirements apply.'},
 {'key': 'leaf095',
  'leaf': 'leaf095',
  'identity': 'head-development-operations-2025',
  'title': 'Call for Applications for the Position of Head (Development & Operations) at Arts '
           'Council Malta',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-for-the-position-of-head-development-operations-at-arts-council-malta/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2025-06-06',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed Development & Operations Head call: relevant MQF 7 (90 ECTS)+two years OR MQF '
             '6 (180 ECTS)+six years, with government-procurement experience and Maltese/English '
             'skills. Qualifying citizenship/equal-treatment/residence and recognition/licensing '
             'rules apply. Three-year employment contract with conditional three-year renewal; '
             '€37,133 base plus €1,600 communication and €1,500 transport allowances, performance '
             'up to 15%. Deadline: June 6,2025 at noon, no zone stated; full role rules apply.'},
 {'key': 'leaf103',
  'leaf': 'leaf103',
  'identity': 'digital-engagement-executive-closed',
  'title': 'Call for the Applications for the Position of Executive (Digital Engagement) at Arts '
           'Council Malta – closed',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-the-applications-for-the-position-of-executive-digital-engagement-at-arts-council-malta/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': None,
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed Digital Engagement executive call: relevant MQF 7 (60 ECTS)+one year OR MQF 6 '
             '(180 ECTS)+two years, with Maltese/English, cultural-sector and '
             'digital-marketing/accessibility skills. Indefinite full-time employment, €32,008 '
             'base; disturbance addition up to 15% and performance addition up to 10%, not '
             'guaranteed. The source says March 6 at noon without a year/zone, so the closing date '
             'remains unknown. See full role and qualification requirements.'},
 {'key': 'leaf108',
  'leaf': 'leaf108',
  'identity': 'communications-executive-february2025',
  'title': 'Call for Applications for the Position of Executive (Communications) at Arts Council '
           'Malta – closed',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-for-the-position-of-executive-communications-at-arts-council-malta/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2025-02-25',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed Communications executive call: relevant MQF 7 (60 ECTS)+one year OR MQF 6 '
             '(180 ECTS)+two years, with Maltese/English and cultural-sector skills. Indefinite '
             'full-time employment, €32,008 base; disturbance addition up to 15% and performance '
             'addition up to 10%, not guaranteed. Deadline: February 25,2025 at noon, no zone '
             'stated. Full qualification/role requirements apply; this is distinct from later '
             'service/employment calls with different qualifications or benefits.'},
 {'key': 'leaf023',
  'leaf': 'leaf023',
  'identity': 'creative-innovators-platforms-2025-2029',
  'title': 'Creative Innovators and Platforms Fund',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/creative-innovators-and-platforms-fund/',
  'categories': ['grants'],
  'kind': 'programme-overview',
  'hosts': ['MT'],
  'deadline': '2025-12-18',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed multi-phase framework for eligible creatives/entities under state-aid rules. '
             'Phase 1 entry closed December 18,2025: up to €2,000/project. Selected entrants alone '
             'progress to expert incubation valued up to €5,000, then competitive Phase 3 (July '
             '22,2026 deadline), potentially €150,000 over three years/80% eligible costs, subject '
             'to funds. Later phases are not open to new applicants. The updated 2026 terms '
             'explain staged payments, project rules and full eligibility;25 initial places are '
             'not 25 calls.'},
 {'key': 'leaf024',
  'leaf': 'leaf024',
  'identity': 'screen-support-2025',
  'title': 'Screen Support Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/screen-support-scheme-3/',
  'categories': ['grants'],
  'kind': 'programme-overview',
  'hosts': [],
  'deadline': '2025-11-18',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2025 screen-support framework for eligible independent audiovisual entities '
             'and, in specified strands, individual professionals. Five production/development '
             'strands have different eligibility/caps. International promotion/festival caps are '
             '€20,000/€5,000/€2,500, with A-list exceptions €40,000/€10,000. Completed works must '
             'be within 18 months and meet cultural/rights criteria. Strand 6 closed November '
             '18,2025 or when funds ran out. Full strand, ownership/residence and payment rules '
             'apply; do not relabel it 2026.'},
 {'key': 'leaf025',
  'leaf': 'leaf025',
  'identity': 'investing-cultural-organisations-2025',
  'title': 'Investing in Cultural Organisations – Malta',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/investing-in-cultural-organisations-malta/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2025-11-11',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed institutional framework for Malta-addressed voluntary organisations enrolled '
             'with CVO. Organisational development: up to €20,000/year; recurring cultural '
             'programmes/events: up to €30,000/year. Each is over three consecutive years, at most '
             '80% eligible costs and subject to government funds. Staged payment depends on '
             'progress/action-plan approval; full strand/organisation criteria apply. Deadline: '
             'November 11,2025 at noon, no zone stated. These organisational caps are not '
             'individual scholarships.'},
 {'key': 'leaf026',
  'leaf': 'leaf026',
  'identity': 'newspapers-support-2025',
  'title': 'Newspapers Support Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/newspapers-support-scheme-2/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2025-11-04',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2025 institutional call for eligible registered companies/voluntary '
             'organisations producing officially listed print newspapers. Caps:€12,000 for '
             'Maltese-language linguistic services or €20,000 for arts/culture content, '
             'potentially 100% eligible 2026 project costs. Strand-specific publication, state-aid '
             'and staged-payment rules apply. Deadline: November 4,2025 at noon, no zone stated. '
             'This is a separate edition from 2026, not a direct award to journalists/readers.'},
 {'key': 'leaf027',
  'leaf': 'leaf027',
  'identity': 'sabbatical-artistic-research-2025',
  'title': 'Sabbatical for Artistic Research Grant',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/sabbatical-for-artistic-research-grant-3/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2025-10-21',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2025 sabbatical grant for full-time self-employed creative individuals '
             'undertaking twelve months of artistic research. Up to €20,000 may cover 100% '
             'eligible costs, with mentoring, scheduled payments and a final sharing event. '
             'Participants must not work full-time elsewhere; occasional/part-time commissions are '
             'permitted. Deadline: October 21,2025 at noon, no zone stated; eligible period '
             'January 2026–June 2027. Full eligibility, research and reporting rules apply.'},
 {'key': 'leaf028',
  'leaf': 'leaf028',
  'identity': 'micro-grant-2025',
  'title': 'Micro Grant',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/micro-grant-4/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2025-10-14',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2025 Micro Grant for individual creatives with no ACM scheme funding in '
             '2023–2025 and Maltese citizenship or specified Malta residence/citizenship '
             'documents. Up to €3,000 may cover 100% eligible 2026 project costs, with staged '
             'payment and state-aid rules. The page and guidelines give October 14,2025 at noon '
             '(no zone stated); no current reopening is implied. Full profile-registration, '
             'applicant/project and submission rules apply.'},
 {'key': 'leaf029',
  'leaf': 'leaf029',
  'identity': 'artistic-heritage-2025',
  'title': 'Artistic Heritage Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/artistic-heritage-scheme-3/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2025-10-07',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2025 scheme for qualifying band/feast music societies, fireworks factories '
             'and feast-decoration voluntary organisations. Caps:€8,000 music societies,€5,000 '
             'fireworks health/safety (€2,500 if the organisation does not own the factory),€5,000 '
             'semi-permanent decoration; up to 100% eligible costs with staged payment and '
             'statutory/compliance rules. Guidelines give April 1 and October 7,2025 sessions; no '
             '2026 reopening is implied. Full institutional/project conditions apply.'},
 {'key': 'leaf030',
  'leaf': 'leaf030',
  'identity': 'international-participation-2025',
  'title': 'International Participation Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/international-participation-scheme/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': [],
  'deadline': '2025-09-23',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2025 international mobility grant for eligible creatives, registered '
             'entities, groups, cooperatives and voluntary organisations. Up to €2,000 may cover '
             '100% eligible participation costs, with staged payment and full '
             'citizenship/residence, project and state-aid rules. Guidelines list February 25 and '
             'September 23,2025 sessions with different eligible activity periods. This standing '
             'page does not establish a new 2026 call; read the complete 2025 terms.'},
 {'key': 'leaf031',
  'leaf': 'leaf031',
  'identity': 'arts-support-right-culture-2025',
  'title': 'Arts Support Scheme – Right to Culture (Culture and Health)',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/arts-support-scheme-right-to-culture-4/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2025-09-09',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2025 Right-to-Culture thematic call, including culture/health. Eligible '
             'creative individuals and qualifying entities/collectives/organisations may seek up '
             'to €30,000/80% eligible costs, with staged payment and state-aid/non-state-aid '
             'rules. Deadline: September 9,2025 at noon, no zone stated; activity October '
             '29,2025–April 29,2027. Full applicant/project criteria apply. It is distinct from '
             'general Arts Support, not an automatic individual award.'},
 {'key': 'leaf032',
  'leaf': 'leaf032',
  'identity': 'private-radio-maltese-music-2025',
  'title': 'Maltese Music on Private Radio Stations Support Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/maltese-musicon-private-radio-stationssupport-scheme/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2025-09-02',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2025 institutional grant for registered companies operating officially '
             'licensed private nationwide radio or private DAB stations. Up to €10,000 may cover '
             '80% eligible costs for dedicated Maltese-music programming, with original-broadcast, '
             'scheduling, content and state-aid rules and staged payment. Deadline: September '
             '2,2025 at noon, no zone stated; activity October 2025–June 2026. Broadcasters apply, '
             'not individual musicians; read the full programming requirements.'},
 {'key': 'leaf033',
  'leaf': 'leaf033',
  'identity': 'regional-cultural-cooperation-2025',
  'title': 'Regional Cultural Cooperation Programme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/regional-cultural-cooperation-programme-3/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2025-07-29',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2025 institutional programme for Regions collaborating with creative '
             'practitioners/operators, collectives or enrolled voluntary organisations. Up to '
             '€20,000 may cover 80% eligible cultural-cooperation costs, with staged payment and '
             'full regional/project criteria. Deadline: July 29,2025 at noon, no zone stated; '
             'activity September 5,2025–March 4,2027. This supports regional partnerships, not a '
             'general individual travel award.'},
 {'key': 'leaf034',
  'leaf': 'leaf034',
  'identity': 'presidents-creativity-2025',
  'title': 'The President’s Fund for Creativity',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/the-presidents-fund-for-creativity-2/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2025-07-15',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2025 institutional creativity call for eligible registered entities, groups, '
             'cooperatives, educational/public institutions and voluntary organisations. '
             'Individual creatives are explicitly ineligible. Up to €15,000 may cover 80% eligible '
             'inclusive arts/community costs, subject to government funds, state-aid and '
             'staged-payment rules. Deadline: July 15,2025 at noon, no zone stated; activity '
             'September 2025–September 2027. Do not backfill the broader 2026 individual '
             'eligibility.'},
 {'key': 'leaf035',
  'leaf': 'leaf035',
  'identity': 'training-development-2025',
  'title': 'Training and Development Support Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/training-and-development-support-scheme/',
  'categories': ['training', 'grants'],
  'kind': 'programme-overview',
  'hosts': [],
  'deadline': '2025-07-08',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2025 training/development framework: adult creatives/qualifying groups or '
             'organisations, plus outstanding young artists aged 8–17 with guardian involvement. '
             'Training must complement existing formation, not replace it. Each strand offers up '
             'to €2,000/100% eligible costs with staged payment. Guidelines give March 4 and July '
             '8,2025 sessions, not 2026 dates. Full citizenship/residence, age-reference-date, '
             'project and eligibility rules apply; session budgets are not individual awards.'},
 {'key': 'leaf036',
  'leaf': 'leaf036',
  'identity': 'arts-support-2025',
  'title': 'Arts Support Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/arts-support-scheme-6/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2025-06-17',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2025 general Arts Support call for eligible creatives, registered entities, '
             'groups, cooperatives and voluntary organisations. Up to €30,000 may cover 80% '
             'eligible artistic-development/audience-engagement costs, with staged payment and '
             'state-aid/non-state-aid rules. Guidelines give June 17,2025 deadline; activity '
             'August 1,2025–February 28,2027. The page is not a new 2026 call; full '
             'applicant/project/exclusion criteria apply.'},
 {'key': 'leaf037',
  'leaf': 'leaf037',
  'identity': 'international-cultural-exchanges-2025',
  'title': 'International Cultural Exchanges Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/international-cultural-exchanges-scheme-4/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': [],
  'deadline': '2025-06-10',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2025 international exchange scheme for eligible creatives, registered '
             'entities/groups/cooperatives/voluntary organisations with Maltese citizenship or '
             'specified Malta residence/citizenship documents. Up to €15,000 may cover 100% '
             'eligible costs, with staged payment and applicable state-aid rules. Deadline: June '
             '10,2025 at noon, no zone stated; activity July 2025–January 2027. Full '
             'applicant/project criteria apply; the 2026 edition has separate terms.'},
 {'key': 'leaf038',
  'leaf': 'leaf038',
  'identity': 'culture-health-2025',
  'title': 'Culture and Health platform fund for the Maltese islands',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/culture-and-health-platform-fund-for-the-maltese-islands/',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': ['MT'],
  'deadline': '2025-05-27',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2025 Culture and Health framework for eligible emerging Maltese/Malta-based '
             'artists and qualifying artist-led entities. International learning Strand 1: up to '
             '€3,000; project Strand 2: up to €9,000, potentially 100% eligible costs with 80/20 '
             'payment. Host agreement, emerging-artist experience/training, artist leadership and '
             'full strand rules apply. Guidelines give May 27,2025 deadline; activity July '
             '2025–July 2026. This page does not establish a current 2026 call.'},
 {'key': 'leaf039',
  'leaf': 'leaf039',
  'identity': 'arts-schools-2025',
  'title': 'Arts in Schools Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/arts-in-schools-scheme-4/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2025-04-29',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2025 institutional scheme for formal educational institutions/colleges from '
             'early years to tertiary education, supporting artistic projects in scholastic '
             '2025–2026. Up to €5,000 may cover 100% eligible costs with staged payment; full '
             'coordinator, applicant and project rules apply. Guidelines give April 29,2025 '
             'deadline, not a current application window. Students are beneficiaries, not '
             'individual grant applicants; Arts in Schools 2026 is a separate edition.'},
 {'key': 'leaf040',
  'leaf': 'leaf040',
  'identity': 'parish-financial-assistance-2025',
  'title': 'Għajnuna finanzjarja għall-Parroċċi',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/ghajnuna-finanzjarja-ghall-parrocci/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2025-04-23',
  'status': 'expired',
  'language': 'mt',
  'summary': 'Closed 2025 parish financial-assistance grant for compliant enrolled voluntary '
             'organisations or canon-law entities in Malta/Gozo. Up to €750 per eligible project '
             'may cover 100% costs, paid after agreement and approved expenses. Full '
             'cultural/social parish-activity and applicant/project rules apply. Deadline: April '
             '23,2025 at noon, no zone stated; activity January–December 2025. This is an '
             'institutional grant. Read the full provider conditions.'},
 {'key': 'leaf043',
  'leaf': 'leaf043',
  'identity': 'access-support-2025',
  'title': 'Access Support Programme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/access-support-programme/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2025-04-08',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2025 Access Support grant for eligible creatives, registered entities, '
             'groups, cooperatives and voluntary organisations addressing artistic-participation '
             'access needs. Up to €5,000 may cover 100% eligible expenditure, with staged payment '
             'and full access/applicant/project rules. Guidelines give April 8,2025 deadline; '
             'activity May 16,2025–December 15,2026. The page is not a fresh 2026 call or '
             'automatic entitlement; read the complete terms.'},
 {'key': 'leaf044',
  'leaf': 'leaf044',
  'identity': 'series-bookmarks-2025',
  'title': 'Call for Series of BookMarks',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/call-for-series-of-bookmarks/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2025-03-27',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2025 commission for Malta-based illustrators, including independent artists '
             'and local illustrator-community members, to design ACM strategic-theme bookmarks. '
             'Six artists/one bookmark each are places in one call. Remuneration:€500 excluding '
             'VAT per bookmark; CV/portfolio, style, selection and full delivery/IP conditions '
             'apply. Guidelines give March 27,2025 deadline and May–October 2025 work period. The '
             'page is not a current open call.'},
 {'key': 'leaf045',
  'leaf': 'leaf045',
  'identity': 'artistic-research-development-2025',
  'title': 'Artistic Research and Development Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/artistic-research-and-development-scheme/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2025-03-18',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2025 artistic research/development scheme for eligible creative individuals, '
             'groups/collectives, cooperatives and enrolled voluntary organisations. Up to €15,000 '
             'may cover 80% eligible costs with staged payment and full project/state-aid rules. '
             'Guidelines give March 18,2025 deadline; activity April 25,2025–October 25,2026. '
             'Research may be practice/community-based. This page is not a current reopening; the '
             '2026 edition has separate terms.'},
 {'key': 'leaf046',
  'leaf': 'leaf046',
  'identity': 'restoration-2025',
  'title': 'Restoration Funding Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/restoration-funding-scheme/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2025-01-28',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2025 institutional restoration call for authorised qualifying '
             'voluntary/canon-law organisations addressing parish cultural property in Malta/Gozo '
             'at least 50 years old. Up to €15,000 may cover 100% eligible costs, with staged '
             'payment and full property/project/organisation rules. This page and its 2025 terms '
             'give January 28,2025 deadline; activity March 2025–March 2026. The opportunity copy '
             'has a different explicit 2024 call date; its newer linked PDF must not erase that '
             'historical identity.'},
 {'key': 'leaf049',
  'leaf': 'leaf049',
  'identity': 'euro-connection-2027',
  'title': 'Euro Connection 2027 – Call for Projects',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/euro-connection-2027-call-for-projects/',
  'categories': ['competitions'],
  'kind': 'opportunity',
  'hosts': ['FR'],
  'deadline': '2026-10-20',
  'status': 'open',
  'language': 'en',
  'summary': 'Euro Connection 2027 short-film project call for a Malta-based producing company: '
             'fiction, animation or creative documentary up to 30 minutes, with secured external '
             'funding and an international-coproduction case. Production must start no earlier '
             'than June 2027; one project/company and in-person Clermont-Ferrand participation '
             'February 2–3,2027 are required. Deadline: October 20,2026 (date only). '
             'Selection/networking is not a guaranteed cash award; full project, rights, funding '
             'and repeat-submission rules apply.'},
 {'key': 'leaf050',
  'leaf': 'leaf050',
  'identity': 'introduction-audience-design-2026',
  'title': 'An Introduction to Audience Design with Síle Culley',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/an-introduction-to-audience-design-with-sile-culley/',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': [],
  'deadline': None,
  'status': 'expired',
  'language': 'en',
  'summary': 'Past online audience-design introduction with Síle Culley, explicitly scheduled '
             'September 28,2026:45-minute presentation plus 15-minute Q&A for the domestic film '
             'sector. The registration line says September 18 by 12 pm without a year or zone, so '
             'its full closing date cannot be confirmed. Limited first-come places; the session '
             'was not recorded or distributed. This is professional learning, not a grant or '
             'guaranteed project selection; see the provider for the original participation '
             'details.'},
 {'key': 'leaf052',
  'leaf': 'leaf052',
  'identity': 'artists-journey-gillick-2026',
  'title': 'The Artist’s Journey: What it really takes to become a professional artist',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/the-artists-journey-what-it-really-takes-to-become-a-professional-artist/',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': None,
  'status': 'unknown',
  'language': 'en',
  'summary': 'Free English-language professional-art-practice lecture/masterclass with James '
             'Gillick in Valletta. Sessions are stated for October 20–22, and registration by '
             'October 19 at noon, but neither carries an explicit year or time zone: closing '
             'date/current availability remain unknown. The source supplies session venues and '
             'times. This is a professional learning opportunity, not employment or a cash grant; '
             'check the original programme and registration before assuming it is a current open '
             'call.'},
 {'key': 'leaf053',
  'leaf': 'leaf053',
  'identity': 'rockestra-photographers-2026',
  'title': 'Rockestra: The Evolution – Open Call for Photographers',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/rockestra-the-evolution-open-call-for-photographers/',
  'categories': ['competitions'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-09-05',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed call for professional photographers with images from past Rockestra events to '
             'contribute up to 10 photographs to an exhibition. Participation fee €100, with a '
             'stated option for selected participants to waive it to charity; mounting/printing '
             'are provided. Photographers retain copyright while granting the specified exhibition '
             'permission. Deadline: September 5,2026 (date only). Multiple images/selected '
             'participants are places within one call; full selection, delivery and rights terms '
             'apply.'},
 {'key': 'leaf054',
  'leaf': 'leaf054',
  'identity': 'thessaloniki-agora-delegation-2026',
  'title': 'Open Call for Producers: Thessaloniki AGORA Delegation 2026',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/open-call-for-producers-thessaloniki-agora-delegation-2026/',
  'categories': ['competitions'],
  'kind': 'opportunity',
  'hosts': ['GR'],
  'deadline': '2026-08-24T11:00:00.000Z',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed delegation call for eligible Malta-registered/VAT audiovisual companies with '
             'qualifying Maltese-citizen or permanent-resident ownership/directorship, project '
             'rights and prior release/platform/festival credits since 2021. Fiction '
             'feature/series in advanced development; documentaries excluded. Support covers '
             'accreditation/networking and up to four hotel nights, not a stated flight award. One '
             'project/company, up to four companies. Deadline: August 24,2026 noon CET as written. '
             'Full criteria apply; event in Greece.'},
 {'key': 'leaf055',
  'leaf': 'leaf055',
  'identity': 'series-rough-pitch-balkan6-2026',
  'title': 'Series Rough Pitch – The Balkan Way #6: Call for TV Drama Series in Development!',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/series-rough-pitch-the-balkan-way-6-call-for-tv-drama-series-in-development/',
  'categories': ['competitions'],
  'kind': 'opportunity',
  'hosts': [],
  'deadline': '2026-09-01',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed TV-drama-series development pitch/training call for projects from the eight '
             'stated countries; geographic origin does not establish a citizenship whitelist. '
             'English materials, mandatory October 19–24 preparation and October 30 online '
             'pitching apply. Up to eight projects are places in one call; only the winning '
             'project receives €6,000. Deadline: September 1,2026 “by midnight (CEST)” is '
             'preserved without inventing rollover. Full project, application and participation '
             'rules apply.'},
 {'key': 'leaf058',
  'leaf': 'leaf058',
  'identity': 'audience-design-workshops-2026',
  'title': 'Audience Design Workshops – October 2026',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/audience-design-workshops-october-2026/',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-08-10',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed October 2026 audience-design workshops for Malta-based production companies '
             'with qualifying feature-film development supported in 2024/2025. Local '
             'productions/coproductions qualify; service work does not. Producer and director must '
             'participate, with an optional third team member; unaffiliated writers/directors '
             'cannot apply alone. Six company places in two-day sessions October 6–7 or 8–9 are '
             'not six calls. Deadline: August 10,2026 (date only). Full film/project/company and '
             'attendance rules apply.'},
 {'key': 'leaf059',
  'leaf': 'leaf059',
  'identity': 'porto-industry-days-2026',
  'title': 'Open Calls for Porto Industry Days 2026',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/open-calls-for-porto-industry-days-2026/',
  'categories': ['competitions'],
  'kind': 'opportunity',
  'hosts': ['PT'],
  'deadline': '2026-07-15',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed Porto Industry Days call for postproduction/rough-cut or work-in-progress '
             'projects across genres, represented by their producer/company, filmmaker or '
             'authorised representative, with the specified MEDIA-country connection. That '
             'connection is not a passport-only rule. Application window: May 15–July 15,2026 '
             '(date-only end). Up to 12 selected projects are places within one industry '
             'presentation/networking call in Portugal, not 12 awards. Full project, rights and '
             'submission terms apply; no cash amount is promised.'},
 {'key': 'leaf060',
  'leaf': 'leaf060',
  'identity': 'hamburg-residencies-2027-2028',
  'title': 'Open call for international residencies in Hamburg',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/open-call-for-international-residencies-in-hamburg/',
  'categories': ['competitions'],
  'kind': 'opportunity',
  'hosts': ['DE'],
  'deadline': '2026-08-27T22:59:00.000Z',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed international visual-art/photography residency call for professionals aged '
             '23+ residing OUTSIDE Germany; qualifying film/music practice must connect to visual '
             'art, and curators are excluded. Three months in Hamburg offer a rent-free '
             'flat,€900/month, return travel and €500 final-presentation support. Insurance/visa '
             'are own costs; no family/pets, and reports/presentation apply. Eight date windows '
             'are not eight calls. Deadline: August 27,2026,23:59 CET as written. Full eligibility '
             'and residency conditions apply.'},
 {'key': 'leaf062',
  'leaf': 'leaf062',
  'identity': 'sportarti-2026',
  'title': 'Call for Applications: SportArti',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-sportarti/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2026-07-28',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2026 SportArti institutional call for eligible Youth/Gozo Youth Football '
             'Association nurseries or Malta Football Association clubs engaging creative '
             'practitioners in artistic activity for athletes aged 5–12. Children are '
             'beneficiaries, not grant applicants. Deadline: July 28,2026 at noon, no zone stated. '
             'The full own guidelines specify organisation, project, funding and payment '
             'conditions; this is not a general individual sports scholarship or a separate call '
             'for each participating child.'},
 {'key': 'leaf064',
  'leaf': 'leaf064',
  'identity': 'coproduction-roadmap-2026',
  'title': 'CO-PRODUCTION ROADMAP MASTERCLASS For Maltese Producers & Filmmakers',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/co-production-roadmap-masterclass-for-maltese-producers-filmmakers/',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-06-19',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed Co-Production Roadmap masterclass for Maltese producers/filmmakers: July '
             '6,2026 learning session and conditional July 7 one-to-one follow-up. Deadline: June '
             '19,2026 (date only). The two activity components form one professional development '
             'call, not two guaranteed awards. See the provider for project selection, attendance '
             'and registration conditions; no nationality-only eligibility or cash benefit is '
             'inferred from the event label.'},
 {'key': 'leaf066',
  'leaf': 'leaf066',
  'identity': 'lets-pitch-some-shorts-2026',
  'title': 'Open Applications for the Workshop and Pitching Forum: “Let’s pitch some shorts!”',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/open-applications-for-the-workshop-and-pitching-forum-lets-pitch-some-shorts/',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['HR'],
  'deadline': None,
  'status': 'unknown',
  'language': 'en',
  'summary': 'Short-fiction workshop/pitch forum for film professionals/students, primarily '
             'directors/writers, from the stated country group; “from” is not a citizenship '
             'whitelist. Up to 10 projects are places in one call, with €3,000 for the best pitch '
             'only. The source gives June 15–18 in Zagreb and May 17 “end of day” application '
             'deadline without an explicit year or zone; closing date/current availability remain '
             'unknown. English/project/preparation and full selection rules apply; no universal '
             '€3,000 award is promised.'},
 {'key': 'leaf069',
  'leaf': 'leaf069',
  'identity': 'music-cities-hull-2026',
  'title': 'Open Call for Participation in the Music Cities Convention 2026',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/open-call-for-participation-in-the-music-cities-convention-2026/',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['GB'],
  'deadline': '2026-04-09T11:00:00.000Z',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed delegation call to the Music Cities Convention in Hull, UK, June 9–11,2026. '
             'Applicants need at least three years of active Malta-music-sector professional '
             'experience; the full eligibility, commitment and preference criteria apply. Two '
             'selected places form one call. ACM supports convention entry, travel and '
             'accommodation under its guidelines, not an unrestricted cash grant. Deadline: April '
             '9,2026,12:00 CET. Full selection/attendance rules apply; no UK-citizenship filter is '
             'implied by the host location.'},
 {'key': 'leaf073',
  'leaf': 'leaf073',
  'identity': 'creating-futures-salzburg-2026',
  'title': 'Call for Applications – Creating Futures: Rethinking Cultural Institutions, '
           'Infrastructure, and Investment',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-applications-creating-futures-rethinking-cultural-institutions-infrastructure-and-investment/',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['AT'],
  'deadline': '2026-02-20T16:00:00.000Z',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed Creating Futures programme for cultural institutional '
             'leaders/funders/policymakers and related professionals in/from Malta. ACM offers two '
             'scholarships covering programme costs, room/board and travel to Salzburg; these are '
             'places in one call. Mandatory online co-creation March 19/26 and in-person April '
             '13–18,2026, plus subsequent meetings; participation priorities are not citizenship '
             'exclusions. Deadline: February 20,2026,17:00 CET. Complete experience, application '
             'and attendance criteria apply.'},
 {'key': 'leaf076',
  'leaf': 'leaf076',
  'identity': 'london-book-fair-illustrators-2026',
  'title': 'Open Call for Illustrators and Graphic Designers to attend the London Book Fair',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/open-call-for-illustrators-and-graphic-designers-to-attend-the-london-book-fair/',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['GB'],
  'deadline': '2026-02-05',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed call for eligible Malta-based illustrators/graphic designers active in '
             'publishing to attend the London Book Fair, March 10–12,2026. Up to four selected '
             'participants may receive travel/accommodation subsidy up to €1,200 and an '
             'exhibitor/guest pass, with access to the shared stand. Other expenses are not '
             'refunded; preparation and full applicant/project requirements apply. Four places are '
             'not four calls. Deadline: February 5,2026 (date only); full eligible-cost and '
             'attendance rules apply.'},
 {'key': 'leaf077',
  'leaf': 'leaf077',
  'identity': 'evaluators-2026-2027',
  'title': 'Call for Evaluators 2026-2027',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-evaluators-2026-2027/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2026-03-31',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed evaluator-pool call for 2026–2027: relevant cultural/creative sector '
             'expertise, Maltese OR English fluency and VAT registration required. ACM staff/board '
             'members are excluded; conflicts bar reviewing affected applications. Payment is €30 '
             'excluding VAT per evaluated application, with occasional short sessions, not '
             'guaranteed two-year employment or workload. Deadline: March 31,2026 (date only). One '
             'evaluator pool, not separate jobs for every funded scheme; full selection/conflict '
             'and reporting rules apply.'},
 {'key': 'leaf078',
  'leaf': 'leaf078',
  'identity': 'edinburgh-fringe-malta-showcase-2027',
  'title': 'Edinburgh Fringe – The Malta Showcase',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/edinburgh-fringe-the-malta-showcase/',
  'categories': ['competitions'],
  'kind': 'opportunity',
  'hosts': ['GB'],
  'deadline': '2026-02-15T11:00:00.000Z',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed performing-arts selection for Maltese or Malta-based practitioners for an '
             '18-month April 2026–September 2027 Edinburgh Fringe development pathway. Free '
             'training/mentoring and staged shortlisting apply; selected 2026 pitch '
             'travel/accommodation support is conditional, and a 2027 production/showcase is not '
             'guaranteed. Deadline: February 15,2026 noon CET. The full own guidelines define '
             'team/project eligibility, selection, rights and attendance; neither Maltese-only '
             'citizenship nor an automatic cash award is implied.'},
 {'key': 'leaf080',
  'leaf': 'leaf080',
  'identity': 'erasmus-venice-traineeship-2026',
  'title': 'ERASMUS+ Traineeship at the National Pavilion of Malta – La Biennale di Venezia 2026',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/erasmus-traineeship-at-the-national-pavilion-of-malta-la-biennale-di-venezia-2026/',
  'categories': ['internships'],
  'kind': 'opportunity',
  'hosts': ['IT'],
  'deadline': '2026-01-30',
  'status': 'expired',
  'language': 'en',
  'summary': "Closed Erasmus+ traineeship at Malta's Venice pavilion, May–November 2026. "
             'Applicants must be registered University of Malta students, including qualifying '
             'final-year students undertaking the placement within one year of graduation. Minimum '
             'two months, roughly four full days/week; ACM endorsement specifies the agreed '
             'dates/period. Deadline: January 30,2026 (date only). Host country is Italy, not an '
             'eligibility nationality; no stipend amount is stated here. Full Erasmus, study and '
             'placement requirements apply.'},
 {'key': 'leaf089',
  'leaf': 'leaf089',
  'identity': 'culture-pass-2025-2026',
  'title': 'Culture Pass 2025/2026',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/culture-pass-2025-2026/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2025-08-12',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2025/2026 Culture Pass proposal call for eligible adult creatives, artist '
             'collectives and cultural NGOs delivering artistic experiences to school audiences '
             'through June 2026. Students are beneficiaries, not individual grant applicants. The '
             'full 2025 terms define citizenship/residence documents, public-line-vote exclusions, '
             'project selection, costs and delivery. Deadline: August 12,2025 at noon, no zone '
             'stated. Do not import 2026 edition rules or treat selected productions as separate '
             'calls.'},
 {'key': 'leaf090',
  'leaf': 'leaf090',
  'identity': 'book-festival-live-drawing-2025',
  'title': 'Open Call for Illustrators and Graphic Designers to Lead Live Drawing Sessions at the '
           'Arts Council Malta Stand at the Malta Book Festival 2025',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/open-call-for-illustrators-and-graphic-designers-to-lead-live-drawing-sessions-at-the-arts-council-malta-stand-at-the-malta-book-festival-2025/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2025-07-16',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed commission call for Malta-based illustrators/graphic designers to lead live '
             "drawing at ACM's Malta Book Festival stand, November 5–9,2025. Ten selected "
             'artists/session places form one call. Payment is €40 excluding VAT per hour, with '
             'the specified participation, VAT-invoicing and preparation/delivery rules. Deadline: '
             'July 16,2025 (date only). It is a paid artistic opportunity, not a €40 grant to '
             'every applicant; read the complete own selection and copyright conditions.'},
 {'key': 'leaf091',
  'leaf': 'leaf091',
  'identity': 'book-festival-stand-design-2025',
  'title': 'Open Call for Designs for the Stand of Arts Council Malta at the Malta Book Festival '
           '2025',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/open-call-for-designs-for-the-stand-of-arts-council-malta-at-the-malta-book-festival-2025/',
  'categories': ['jobs'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2025-07-14',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed commission for one Malta-based artist, illustrator or graphic designer to '
             "design ACM's 2025 Malta Book Festival stand. Remuneration €800 excluding VAT; full "
             'design specifications, selection, rights and valid VAT-invoicing conditions apply. '
             'Deadline: July 14,2025 (date only), with final delivery in the first week of October '
             'and festival November 5–9. This is one paid design call, not separate jobs for each '
             'stand panel or event day.'},
 {'key': 'leaf019',
  'leaf': 'leaf019',
  'identity': 'screen-support-strands1-5-2026',
  'title': 'Screen Support Scheme Strand 1 to 5',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/screen-support-scheme-strand-1-to-5/',
  'categories': ['grants'],
  'kind': 'programme-overview',
  'hosts': ['MT'],
  'deadline': '2026-04-14',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2026 screen-production framework: eligible Malta-registered independent '
             'audiovisual entities and, in specified strands, eligible individual professionals. '
             'Caps: screenwriting €25,000; development €50,000; short film €35,000; feature '
             '€450,000; cultural programmes €20,000. Feature support is up to 50% eligible costs '
             '(60% for qualifying EU coproductions); other strands up to 100%, with staged '
             'payment. Deadline: April 14,2026 at noon, no zone stated. Ownership/residence, VAT, '
             'rights and cultural/project criteria differ by strand; read full terms.'},
 {'key': 'leaf042',
  'leaf': 'leaf042',
  'identity': 'creative-europe-cofunding-2025',
  'title': 'Co-Funding of Creative Europe Cooperation Projects',
  'url': 'https://artscouncilmalta.gov.mt/en/funding-and-grants/co-funding-of-creative-europe-cooperation-projects/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2025-04-15',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed co-funding expression of interest for Malta-addressed legal entities or '
             'self-employed sole traders joining an eligible Creative Europe European Cooperation '
             'Project consortium, including coordinators. An ACM endorsement demonstrating '
             'financial capacity is required. Own HTML describes support up to €15,000, without '
             'guaranteeing full co-funding or sole-source finance. Deadline: April 15,2025 at '
             'noon, no zone stated. The linked EOI terms return 404; this limited overview does '
             'not backfill unseen conditions or promise a current call.'},
 {'key': 'leaf110',
  'leaf': 'leaf110',
  'identity': 'london-book-fair-illustrators-2025',
  'title': 'Professional Development initiative for book illustrators at the London Book Festival',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/professional-development-initiative-for-book-illustrators-at-the-london-book-festival/',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['GB'],
  'deadline': '2025-02-12',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed professional development call for local illustrators to attend the London '
             'Book Fair, March 11–13,2025. Up to four selected participants may receive '
             'travel/accommodation subsidy and an exhibitor pass; four places form one call, not '
             'guaranteed cash for every applicant. Deadline: February 12,2025 at noon, no zone '
             'stated. The own article provides only a limited overview and registration link, not '
             'complete terms or a subsidy amount; verify provider eligibility and participation '
             'conditions.'},
 {'key': 'leaf111',
  'leaf': 'leaf111',
  'identity': 'newspapers-support-2023',
  'title': 'Newspapers Support Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/newspapers-support-scheme/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2023-05-23',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2023 institutional newspapers-support call for eligible registered companies '
             'or enrolled voluntary organisations represented by a print-media legal '
             'representative, with an editor registered with the Department of Information. The '
             'programme supports Maltese-language linguistic quality and arts/culture content; '
             'publication requirements apply. Deadline: May 23,2023 at noon, no zone stated. '
             'Archived guidelines return 404, so this limited historical overview does not import '
             '2025/2026 award caps or claim a current call.'},
 {'key': 'leaf112',
  'leaf': 'leaf112',
  'identity': 'original-musicals-2022-2026',
  'title': 'Call for Proposals for Original Musicals in the Maltese Language (2022 – 2026)',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-proposals-for-original-musicals-in-the-maltese-language-2022-2026/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2021-07-04',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed original Maltese-language musicals call for qualifying registered entities, '
             'collectives, cooperatives and voluntary organisations. Deadline: July 4,2021; '
             "production period 2022–2026. ACM's annual up-to €25,000 excluding VAT is payable to "
             'MCC for venue/services, not promised cash to applicants; MCC provides up to '
             '€50,000/year production expenses under a negotiated five-year contract. Archived '
             'guidelines return 404. Full selection/project conditions cannot be backfilled from '
             'later schemes; this is a limited historical overview.'},
 {'key': 'leaf113',
  'leaf': 'leaf113',
  'identity': 'feast-association-financing-2021',
  'title': 'Artistic Financing for Feast Associations Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/artistic-financing-for-feast-associations-scheme/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2021-07-02',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2021 institutional artistic-financing call for voluntary feast-decoration, '
             'cultural and social associations in Malta/Gozo, covering specified artistic '
             'decorations, related training/restoration and cultural/social projects. Deadline: '
             'July 2,2021 (date only). The stated guidelines route leads to the funds login, which '
             'was not accessed; no complete public terms or per-project amount is verified. This '
             'limited historical overview does not promise current funding or use beneficiary '
             'awards as new opportunities.'},
 {'key': 'leaf114',
  'leaf': 'leaf114',
  'identity': 'voluntary-fireworks-factories-2021',
  'title': 'Fund offering financial support to Maltese fireworks factories working on a voluntary '
           'basis',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/fund-offering-financial-support-to-maltese-fireworks-factories-working-on-a-voluntary-basis/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2021-06-18',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2021 fund for voluntary Maltese fireworks factories proposing health/safety '
             'improvements and related eligible work. Own HTML states a maximum €5,000 proposal '
             'and a June 18,2021 noon deadline, without a zone; the €170,000 total is a session '
             'allocation, not an individual award. The archived guidelines return 404. This '
             'limited historical overview retains the stated institutional purpose only, not '
             'unverified eligibility/payment conditions or a new 2025/2026 call.'},
 {'key': 'leaf115',
  'leaf': 'leaf115',
  'identity': 'musical-instrument-purchase-legacy',
  'title': 'Fund for the purchase of musical instruments for the local Musical Societies',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/fund-for-the-purchase-of-musical-instruments-for-the-local-musical-societies/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': None,
  'status': 'unknown',
  'language': 'en',
  'summary': 'Historical musical-society instrument co-financing overview: up to 85% project costs '
             'for the specified eligible instruments and registered band musicians, with an '
             "educational plan, teacher details, audition and two years' same-family or three "
             "years' different-instrument experience. The page says December 3 at noon without a "
             'year/zone; closing date and current availability remain unknown. Archived guidelines '
             'return 404. This is not a general student instrument grant; full terms are '
             'unverified.'},
 {'key': 'leaf116',
  'leaf': 'leaf116',
  'identity': 'band-club-cultural-work-2021',
  'title': 'Fund to strengthen cultural work done by band clubs',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/fund-to-strengthen-cultural-work-done-by-band-clubs/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2021-11-19',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2021 institutional fund for Malta/Gozo band clubs registered as compliant '
             'voluntary organisations and active members of Għaqda Każini tal-Banda. It supports '
             'qualifying cultural/restoration/training/collaborative work; the €160,000 stated '
             'total is a session budget, not an award to each club. Deadline: November 19,2021 at '
             'noon, no zone stated. Archived guidelines return 404, so no unseen per-project cap '
             'or current reopening is claimed; this is a limited historical overview.'},
 {'key': 'leaf118',
  'leaf': 'leaf118',
  'identity': 'young-artist-development-legacy',
  'title': 'Young Artist Development Programme',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/young-artist-development-programme/',
  'categories': ['training'],
  'kind': 'programme-overview',
  'hosts': ['MT'],
  'deadline': None,
  'status': 'unknown',
  'language': 'en',
  'summary': 'Historical Young Artist Development overview for creatives aged 14–17, with '
             'training/mentoring intended to build artistic skills, networks and social '
             'responsibility. The page refers to 2019/2020 participants and a next call in 2021; '
             'that is not a current open call. It describes a two-year programme and 18-month '
             'mentoring, without a verified current deadline or grant amount. Archived guidelines '
             'return 404. This limited overview excludes alumni biographies and does not merge the '
             'age group with Artivisti 18–25.'},
 {'key': 'leaf119',
  'leaf': 'leaf119',
  'identity': 'public-spaces-art-2021',
  'title': 'Public Spaces Art Fund',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/public-spaces-art-fund/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2021-08-31',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2021 Public Spaces Art Fund call for eligible creative '
             'professionals/artists/architects, registered entities, collectives and cooperatives, '
             'with approval from the relevant local council for a pre-set site. It supports '
             'publicly accessible art integrated into public-space development; applicable '
             'state-aid/non-state-aid rules apply. Deadline: August 31,2021 at noon, no zone '
             'stated. Archived guidelines return 404; no unverified project cap or current '
             'reopening is promised in this limited historical overview.'},
 {'key': 'leaf120',
  'leaf': 'leaf120',
  'identity': 'radio-new-talent-2021',
  'title': 'Radio New Talent',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/radio-new-talent/',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2021-09-16',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2021 radio-production/broadcasting development call exclusively for '
             'individual creatives aged 18–30 IN 2021, offering skill development and a platform '
             'for a radio production. Deadline: September 16,2021 at noon, no zone stated. The '
             'cohort age-reference year is fixed, not a rolling present-day eligibility rule. '
             'Archived guidelines return 404; no unverified cash award, citizenship restriction or '
             'new 2025/2026 call is claimed. This is a limited historical programme overview.'},
 {'key': 'leaf121',
  'leaf': 'leaf121',
  'identity': 'president-creativity-2021',
  'title': 'Il-Premju tal-President għall-Kreattività',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/il-premju-tal-president-ghall-kreattivita/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2021-09-02',
  'status': 'expired',
  'language': 'mt',
  'summary': 'Closed 2021 President Creativity framework for qualifying registered entities, '
             'collectives, cooperatives, schools/education/public institutions and voluntary '
             'organisations targeting disadvantaged groups through arts. Strand 2 reopened after '
             'the July 5 deadline and closed September 2,2021 at noon, no zone stated. Both '
             'archived strand-guideline links return 404. This limited historical overview does '
             'not import 2025/2026 benefits or eligibility and does not turn selected '
             'projects/strands into capacity clones.'},
 {'key': 'leaf124',
  'leaf': 'leaf124',
  'identity': 'arts-awards-2025-overview',
  'title': 'Il-Premju għall-Arti 2025',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/il-premju-ghall-arti-2025/',
  'categories': ['competitions'],
  'kind': 'programme-overview',
  'hosts': ['MT'],
  'deadline': None,
  'status': 'unknown',
  'language': 'mt',
  'summary': 'Historical 2025 arts-awards nomination framework covering work in September '
             '2023–August 2025. Competitive categories have distinct eligibility: three '
             'public-cultural-organisation categories versus eligible IP-owning '
             'individuals/entities/NGOs/companies/collectives; noncompetitive honours are '
             'separate. The page sends full nomination terms to another website, not retrieved '
             'here; no exact closing date or cash prize is verified. This limited overview does '
             'not create winner/category clones or a current open award claim.'},
 {'key': 'leaf125',
  'leaf': 'leaf125',
  'identity': 'restoration-2024',
  'title': 'Restoration Funding Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/restoration-funding-scheme/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': '2024-02-06',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed historical restoration call: February 6,2024 at noon, no zone stated. Own '
             'HTML describes qualifying authorised Malta/Gozo voluntary organisations restoring '
             'eligible parish cultural property at least 50 years old. The currently linked PDF '
             'instead specifies a January 2025 edition; that mismatch is retained and its €15,000 '
             'cap is NOT backfilled into this 2024 call. Full 2024 eligibility/benefit terms are '
             'not independently available here. This limited institutional overview is distinct '
             'from the 2025 and 2026 calls.'},
 {'key': 'leaf126',
  'leaf': 'leaf126',
  'identity': 'state-arts-contributions-2024',
  'title': 'The State of the Arts – Malta National Symposium 2024',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/the-state-of-the-arts-malta-national-symposium-2024/',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2024-07-22',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2024 symposium contribution call for artists, researchers, policymakers and '
             'cultural/shared-interest workers proposing workshops, initiatives, artistic '
             'practices or solutions with a clear Malta-creative-sector connection. Deadline: July '
             '22,2024,17:00, no zone stated; selected contributions enter the October 24–25 '
             "programme at University of Malta's Valletta campus. The linked terms use an "
             'unreviewed skipdns.link alias, not retrieved or bypassed. This limited call is '
             'distinct from generic 2022 symposium attendance.'},
 {'key': 'leaf127',
  'leaf': 'leaf127',
  'identity': 'global-connect-2025',
  'title': '2025 Global Connect Open Call',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/2025-global-connect-open-call/',
  'categories': ['competitions'],
  'kind': 'opportunity',
  'hosts': [],
  'deadline': '2024-11-04T22:59:00.000Z',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed Global Connect 2025 call for worldwide-based performing-arts '
             "professionals/leaders with more than five years' experience; live music is excluded. "
             "Benefits include five years' free IETM membership 2025–2029, training/meetings, "
             'online community sessions and support for the 2025 plenary meeting, under full '
             'participation/funding conditions. Ten selected professionals are places in one call. '
             'Deadline: November 4,2024,23:59 CET. The own article is a limited overview; no '
             'universal travel allowance or citizenship filter is invented.'},
 {'key': 'leaf128',
  'leaf': 'leaf128',
  'identity': 'culture-pass-2024',
  'title': 'Culture Pass 2024',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/culture-pass-2024/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2024-08-06',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed 2024 Culture Pass proposal call for creative providers producing artistic '
             'experiences for kindergarten through post-secondary school audiences, with '
             'activities through June 2025. Children/students are beneficiaries, not individual '
             'grant applicants. Deadline: August 6,2024 at noon, no zone stated. Archived '
             'guidelines return 404, so this limited historical overview does not import 2025/2026 '
             'applicant conditions, funding amounts or a current reopening; full 2024 terms remain '
             'unverified.'},
 {'key': 'leaf129',
  'leaf': 'leaf129',
  'identity': 'ex-rampol-bands-2024',
  'title': 'Ex-Rampol, Birkirkara Bands (One-Time) Support Scheme',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/ex-rampol-birkirkara-bands-one-time-support-scheme/',
  'categories': ['grants'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2024-11-05',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed one-time support call for musicians displaced from communal rehearsal spaces '
             'in Birkirkara, covering specified relocation/rehearsal-space costs. Own stated '
             'grant:€1,500 for year 1, then €600 each for years 2 and 3, not €1,500 every year or '
             'a general band-club award. Deadline: November 5,2024 at noon, no zone stated. '
             'Archived guidelines return 404, so this limited historical overview does not promise '
             'current funding or backfill unseen eligibility/payment conditions.'},
 {'key': 'leaf097',
  'leaf': 'leaf097',
  'identity': 'artistic-proposals-acm-anniversary-2025',
  'title': 'Call for Artistic Proposals',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-artistic-proposals/',
  'categories': ['competitions'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2025-06-06T11:00:00.000Z',
  'status': 'expired',
  'language': 'en',
  'summary': "Closed artistic-performance proposal call marking ACM's anniversary, for eligible "
             'creative individuals, registered entities, collectives, cooperatives and voluntary '
             'organisations. Up to three selected performance pieces may receive up to €6,000 '
             'excluding VAT each; places do not create separate calls and funding is not '
             'guaranteed. Deadline: June 6,2025 noon CET. Performances are intended for December '
             '2025. Full artistic/selection, rights and delivery criteria apply; external full '
             'guidelines were not fetched here.'},
 {'key': 'leaf098',
  'leaf': 'leaf098',
  'identity': 'gwangju-malta-artistic-team-2026',
  'title': 'Call for an Artistic Team for the Malta Pavilion at the 16th Gwangju Biennale 2026',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/call-for-an-artistic-team-for-the-malta-pavilion-at-the-16th-gwangju-biennale-2026/',
  'categories': ['competitions'],
  'kind': 'opportunity',
  'hosts': ['KR'],
  'deadline': None,
  'status': 'unknown',
  'language': 'en',
  'summary': "Artistic-team call for Malta's 2026 Gwangju Biennale pavilion in South Korea. Strong "
             'international track record, artistic director/project manager and complete '
             'exhibition delivery required; multinational teams must include Maltese individuals '
             'AND Maltese artists/performers. The €120,000 allocation funds the team project, not '
             'each artist. Stated deadline July 1 noon CET lacks a year: closing date/current '
             'availability remain unknown. Team publicity references require ACM written approval; '
             'full terms apply.'},
 {'key': 'leaf099',
  'leaf': 'leaf099',
  'identity': 'artist-residence-malta-2025',
  'title': 'Artist in Residence Call 2025',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/artist-in-residence-call-2025/',
  'categories': ['competitions'],
  'kind': 'opportunity',
  'hosts': ['MT'],
  'deadline': '2025-04-28',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed international artist residency call for a one-month placement in Malta during '
             'July–September 2025, for artists with at least two years of professional experience. '
             'Up to two selected residents may receive support capped at €10,000 each for '
             'negotiated travel, accommodation, fee and per-diem costs, not unconditional cash. A '
             'Malta-related project/local collaboration, lectures/workshops and exchange '
             'obligations apply. Deadline: April 28,2025 (date only). Full selection and residency '
             'conditions apply.'},
 {'key': 'leaf100',
  'leaf': 'leaf100',
  'identity': 'studio-francis-ebejer-2025',
  'title': 'OPEN CALL Studio Francis Ebejer',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/open-call-studio-francis-ebejer/',
  'categories': ['competitions'],
  'kind': 'programme-overview',
  'hosts': ['MT'],
  'deadline': '2025-06-30',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed Studio Francis Ebejer script-development call, primarily for Maltese-language '
             'theatre/radio work. Radio remuneration €1,500 excluding VAT; theatre €3,500 '
             'excluding VAT for agreed drafts, final script and production consultation. '
             'Selection, mandatory submission materials and research/development duties apply. '
             'Writers retain creative rights but grant exclusive production licensing through one '
             'year after premiere and first-publication rights to producers. Deadline: June '
             '30,2025 at noon, no zone stated; read full category terms.'},
 {'key': 'leaf101',
  'leaf': 'leaf101',
  'identity': 'venice-curatorial-team-2026',
  'title': 'Now open: Call for a Curatorial Team for The Malta Pavilion at the 61st International '
           'Art Exhibition at La Biennale di Venezia 2026.',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/now-open-call-for-a-curatorial-team-for-the-malta-pavilion-at-the-61st-international-art-exhibition-at-la-biennale-di-venezia-2026/',
  'categories': ['competitions'],
  'kind': 'opportunity',
  'hosts': ['IT'],
  'deadline': '2025-05-06T11:00:00.000Z',
  'status': 'expired',
  'language': 'en',
  'summary': "Closed curatorial-team call for Malta's 2026 Venice Art Biennale pavilion in Italy. "
             'Strong international track record and full exhibition delivery required; '
             'multinational teams must include Maltese individuals AND Maltese artists/performers. '
             'The €170,000 allocation is a project delivery budget, not an individual scholarship; '
             'fee caps and reporting/payment obligations apply. Deadline: May 6,2025 noon CET. '
             'Applicant-team publicity references require ACM written approval; full own '
             'eligibility/rights terms apply.'},
 {'key': 'leaf102',
  'leaf': 'leaf102',
  'identity': 'music-cities-fayetteville-2025',
  'title': 'Open Call for Participation in the Music Cities Convention 2025',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/open-call-for-participation-in-the-music-cities-convention-2025/',
  'categories': ['training'],
  'kind': 'opportunity',
  'hosts': ['US'],
  'deadline': '2025-04-09T11:00:00.000Z',
  'status': 'expired',
  'language': 'en',
  'summary': 'Closed delegation call for the 2025 Music Cities Convention in Fayetteville, '
             'Arkansas, USA, for eligible Malta-music-sector professionals with at least three '
             'years of relevant experience and required availability. Two selected places form one '
             'call. ACM supports entry, travel and accommodation under its own guidelines, not an '
             'unrestricted cash award. Deadline: April 9,2025 noon CET. Full applicant, selection, '
             'attendance and expense rules apply; US hosting is not a US-citizenship condition.'},
 {'key': 'leaf117-bstart',
  'leaf': 'leaf117',
  'identity': 'bstart-historical-overview',
  'title': 'B.Start',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/national-opportunities/',
  'categories': ['grants'],
  'kind': 'institutional-grant',
  'hosts': ['MT'],
  'deadline': None,
  'status': 'unknown',
  'language': 'en',
  'summary': 'Historical B.Start overview: a seed fund for small start-up undertakings with a '
             'viable business concept in an early development stage. This own source states '
             'support up to €25,000 for economically viable initiatives, but its external '
             'guidelines were not retrieved and it supplies no verified current deadline or '
             'availability. The amount is an old overview claim, not a confirmed 2026 offer. Full '
             'business, applicant and funding conditions must be checked with the programme '
             'provider.'},
 {'key': 'leaf117-advisory',
  'leaf': 'leaf117',
  'identity': 'business-advisory-historical-overview',
  'title': 'Business Advisory Services',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/national-opportunities/',
  'categories': ['other'],
  'kind': 'programme-overview',
  'hosts': ['MT'],
  'deadline': None,
  'status': 'unknown',
  'language': 'en',
  'summary': 'Historical Business Advisory Services overview for business undertakings operating '
             'in Malta, supporting assessment of strengths/weaknesses and advice on matters such '
             'as funding, market entry, innovation and governance. The benefit is advisory '
             'service, not a stated cash grant. The source links external terms that were not '
             'retrieved, with no verified current deadline or availability. This limited programme '
             'overview does not establish a new 2026 training course or unconditional '
             'entitlement.'},
 {'key': 'leaf117-francis-ebejer',
  'leaf': 'leaf117',
  'identity': 'francis-ebejer-historical-award-overview',
  'title': 'Premju Francis Ebejer',
  'url': 'https://artscouncilmalta.gov.mt/en/opportunitie/national-opportunities/',
  'categories': ['competitions'],
  'kind': 'programme-overview',
  'hosts': ['MT'],
  'deadline': None,
  'status': 'unknown',
  'language': 'mt',
  'summary': 'Historical Premju Francis Ebejer overview for new and established writers of '
             'Maltese-language scripts. The source describes financial recognition plus support to '
             'stage and publish promising work, but gives no verified award amount, current '
             'application deadline or complete eligibility/rights terms. Its historical 1993–2016 '
             'awards context does not establish a current open call. This limited award-programme '
             'overview is distinct from the detailed 2025 Studio Francis Ebejer script-development '
             'call.'}]

if __name__ == "__main__":
    sys.exit(main())
