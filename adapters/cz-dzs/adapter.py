"""Collect the reviewed current European Solidarity Corps programme frontier."""

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
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

SOURCE_ID = "cz-dzs"
SOURCE_URL = (
    "https://www.dzs.cz/program/evropsky-sbor-solidarity/projekty-granty"
)
WEBSITE_URL = "https://www.dzs.cz/en"
LANGUAGE = "cs"
PUBLISHER_COUNTRY = "CZ"
PUBLISHER_TYPE = "government"
ATTRIBUTION = (
    (
        'Dům zahraniční spolupráce (DZS), Czech National Agency '
        'for International Education and Research; European Comm'
        'ission/EACEA; SALTO-YOUTH https://www.salto-youth.net/'
        ' and the named training organisers'
    )
)
_GITHUB = "https://api.github.com/repos/YouthOpps/data-source"
_WORKFLOW_GITHUB = "https://api.github.com/repos/YouthOpps/data-pipeline"
_USER_AGENT = (
    "YouthOpps/1.0 (+https://github.com/YouthOpps/data-pipeline; "
    "public opportunity metadata)"
)
CATEGORIES = {"grants", "training"}
FAMILY = "youthopps-dzs-publisher-v1"
FAMILY_SOURCES = {"cz-study-in-czechia", "cz-dzs"}
COLLECTION_TIMEOUT = 900
RUN_TIMEOUT = 1290
EXPORT_TIMEOUT = 30
_STATE_SCHEMA = 1
_ARTIFACT_NAME = "dzs-pacing-state"
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
            type(value) in (int, float)
            and math.isfinite(value) and value >= 0
        )
    except OverflowError:
        return False


def validate_budget(state):
    """Validate inert state without age-expiring a future publisher embargo."""
    if not isinstance(state, dict) or set(state) != {
        "schema", "family", "observed_at", "not_before", "starts", "blocked",
    }:
        raise AdapterError("Invalid publisher pacing state", "access")
    if (type(state["schema"]) is not int or state["schema"] != _STATE_SCHEMA
            or state["family"] != FAMILY
            or not numeric(state["observed_at"])
            or not numeric(state["not_before"])
            or type(state["blocked"]) is not bool
            or not isinstance(state["starts"], list)
            or len(state["starts"]) > 10
            or any(not numeric(t) for t in state["starts"])
            or state["starts"] != sorted(state["starts"])
            or len(set(state["starts"])) != len(state["starts"])
            or any(t > state["observed_at"] for t in state["starts"])):
        raise AdapterError("Invalid publisher pacing history", "access")
    if time.time() + 1 < state["observed_at"]:
        raise AdapterError("Publisher pacing clock moved backwards", "access")
    return state


def empty_budget(now):
    return {
        "schema": _STATE_SCHEMA, "family": FAMILY, "observed_at": now,
        "not_before": now, "starts": [], "blocked": False,
    }


def budget_file():
    global _STATE_PATH, _STATE_LOCK
    if _STATE_PATH:
        return
    directory = os.path.join(tempfile.gettempdir(), FAMILY + (
        "-"
    ) + str(os.getuid()))
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
    if (info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077 or info.st_nlink != 1):
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
        if (info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077 or info.st_nlink != 1):
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
    fd, name = tempfile.mkstemp(prefix=(
        "pending-"
    ), dir=os.path.dirname(_STATE_PATH))
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
            raise AdapterError((
                "Publisher backoff requires reviewed recovery"
            ), (
                "access"
            ))
        now = time.time()
        starts = [t for t in state["starts"] if t > now - 60]
        target = max(now, state["not_before"])
        if starts:
            target = max(target, starts[-1] + 6)
        if len(starts) == 10:
            target = max(target, starts[0] + 60)
        if target + 120 > time.time() + (
            _PHASE_END - time.monotonic()
        ):
            raise AdapterError("Publisher backoff exceeds phase budget",
                               "access")
        if _COLLECTION_STARTED is not None and (
            target + 120 > _COLLECTION_STARTED + COLLECTION_TIMEOUT
        ):
            raise AdapterError((
                "Publisher backoff exceeds collection budget"
            ), (
                "access"
            ))
        if target > now:
            time.sleep(target - now)
        now = time.time()
        if now + 0.001 < target:
            raise AdapterError((
                "Publisher pacing clock moved backwards"
            ), (
                "access"
            ))
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


def request_bytes(url, method="GET", headers=None, payload=None,
                  publisher=False, byte_limit=30000000):
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
                raise AdapterError((
                    "Oversized or invalid response length"
                ), (
                    "fetch"
                ))
            body = response.read(byte_limit + 1)
            if (len(body) > byte_limit
                    or declared and len(body) != int(declared)):
                raise AdapterError("Incomplete or oversized response", "fetch")
            encoding = response.headers.get("Content-Encoding", "identity")
            if encoding != "identity":
                raise AdapterError((
                    "Unsupported response content encoding"
                ), (
                    "fetch"
                ))
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
                    raise AdapterError((
                        "Publisher redirect transport downgrade"
                    ), (
                        "access"
                    ))
                check_robots(destination)
            else:
                if new.scheme != "https" or new.username or new.password:
                    raise AdapterError((
                        "Unsafe authenticated redirect"
                    ), (
                        "publish"
                    ))
                if new.netloc != old.netloc:
                    headers = {key: value for key, value in headers.items()
                               if key.lower() != "authorization"}
                if urllib.parse.urlsplit(url).hostname == "api.github.com":
                    raise AdapterError((
                        "Unexpected GitHub API redirect"
                    ), (
                        "publish"
                    ))
            current = destination
            continue
        if publisher and status in (401, 403, 429):
            refused = urllib.parse.urlsplit(current)
            provenance = urllib.parse.urlunsplit((
                refused.scheme, refused.hostname or "", refused.path, "", ""))
            raise AdapterError(
                f"Publisher refused access: HTTP {status}: " + provenance,
                "access", status)
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
            raise AdapterError((
                "Publisher robots policy unavailable"
            ), (
                "access"
            ), status)
    parser = _ROBOTS[origin]
    if parser is not None and not parser.can_fetch(_USER_AGENT, url):
        raise AdapterError("Publisher robots excludes required input", "access")
    if parser is not None:
        delay = parser.crawl_delay(_USER_AGENT)
        if delay and delay > 6:
            with locked_budget():
                state = load_budget()
                if state["starts"]:
                    state[(
                        "not_before"
                    )] = max(state[(
                        "not_before"
                    )], state[(
                        "starts"
                    )][-1] + delay)
                    save_budget(state)
        rate = parser.request_rate(_USER_AGENT)
        if rate and rate.seconds / rate.requests > 6:
            raise AdapterError((
                "Stricter publisher request-rate requires review"
            ), (
                "access"
            ))


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
        key = next((key for key, info in INPUTS.items()
                    if info["url"] == url), "unmapped-public-input")
        parsed = urllib.parse.urlsplit(reviewed)
        provenance = parsed.scheme + "://" + parsed.netloc + parsed.path
        raise AdapterError(
            f"Required publisher input {key} returned HTTP {status}: "
            + provenance, "fetch", status)


def workflow_api(path):
    """Read authenticated workflow metadata without publication permissions."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise AdapterError("Workflow state credentials missing", "access")
    try:
        status, _, body = request_bytes(
            _WORKFLOW_GITHUB + path,
            headers={"Authorization": "Bearer " + token,
                     "Accept": "application/vnd.github+json"},
        )
        if status != 200:
            raise ValueError("Workflow API status")
        return json.loads(body)
    except Exception:
        raise AdapterError((
            "Workflow pacing state unavailable"
        ), (
            "access"
        )) from None


def current_run_identity():
    if (os.environ.get("GITHUB_REPOSITORY") != "YouthOpps/data-pipeline"
            or os.environ.get("GITHUB_REF") != "refs/heads/main"):
        raise AdapterError("Unexpected workflow repository", "access")
    values = [os.environ.get(key, "") for key in ((
        "GITHUB_RUN_ID"
    ), (
        "GITHUB_RUN_ATTEMPT"
    ))]
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
        if (not source or type(identity) is not int or identity <= 0
                or type(attempt) is not int or attempt <= 0
                or run["status"] != "completed"
                or run["repository"]["full_name"] != "YouthOpps/data-pipeline"
                or run[(
                    "head_repository"
                )][(
                    "full_name"
                )] != (
                    "YouthOpps/data-pipeline"
                )
                or run["head_branch"] != "main"
                or expected and (identity, attempt, source) != expected):
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
        raise AdapterError((
            "Untrusted completed family attempt"
        ), (
            "access"
        )) from None


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
                    "Workflow inventory remained unstable", "access") from None


def scan_family_history():
    """Select newest completed chronology, including older-ID rerun attempts."""
    current_id, current_attempt = current_run_identity()
    candidates = []
    total, scanned = None, 0
    seen_ids = set()
    for page in range(1, 101):
        result = workflow_api(f"/actions/runs?per_page=100&page={page}")
        count, runs = result.get("total_count"), result.get("workflow_runs")
        if (type(count) is not int or count < 0
                or not isinstance(runs, list) or len(runs) > 100):
            raise AdapterError("Invalid workflow run inventory", "access")
        if total is None:
            total = count
        elif total != count:
            raise InventoryRace((
                "Workflow inventory changed during restore"
            ), (
                "access"
            ))
        scanned += len(runs)
        for run in runs:
            if not isinstance(run, dict):
                raise AdapterError("Invalid workflow run entry", "access")
            run_id = run.get("id")
            if (type(run_id) is not int or run_id <= 0
                    or run_id in seen_ids):
                raise AdapterError(
                    "Invalid or duplicate workflow run identity", "access")
            seen_ids.add(run_id)
            source = family_source(run)
            if not source:
                continue
            if (run.get((
                "repository"
            ), {}).get((
                "full_name"
            )) != (
                "YouthOpps/data-pipeline"
            )
                    or run.get("head_repository", {}).get("full_name")
                    != (
                        "YouthOpps/data-pipeline"
                    ) or run.get((
                        "head_branch"
                    )) != (
                        "main"
                    )):
                raise AdapterError((
                    "Workflow repository or branch mismatch"
                ), (
                    "access"
                ))
            identity, attempt = run.get("id"), run.get("run_attempt")
            if type(identity) is not int or type(attempt) is not int:
                raise AdapterError("Invalid family run identity", "access")
            if identity == current_id:
                if attempt != current_attempt:
                    raise AdapterError((
                        "Current workflow attempt mismatch"
                    ), (
                        "access"
                    ))
                if current_attempt > 1:
                    prior = workflow_api((
                        f"/actions/runs/{identity}/attempts/"
                        f"{current_attempt - 1}"
                    ))
                    candidates.append(completed_identity(
                        prior, (identity, current_attempt - 1, source)))
                continue
            if run.get((
                "status"
            )) in ((
                "queued"
            ), (
                "waiting"
            ), (
                "pending"
            ), (
                "requested"
            )):
                continue
            if run.get("status") != "completed":
                raise AdapterError("Another family run is active", "access")
            candidates.append(completed_identity(run))
        if scanned >= total:
            if scanned != total:
                raise AdapterError((
                    "Workflow inventory count mismatch"
                ), (
                    "access"
                ))
            if not candidates:
                return None
            ordered = sorted(candidates)
            if len(ordered) > 1 and ordered[-1][0] == ordered[-2][0]:
                raise AdapterError((
                    "Ambiguous family attempt chronology"
                ), (
                    "access"
                ))
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
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "User-Agent": _USER_AGENT,
        "Accept": "application/vnd.github+json",
    })
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
        if (parsed.scheme != "https" or not parsed.hostname
                or parsed.username or parsed.password or parsed.port):
            raise ValueError("Unsafe artifact URL")
        if ("." not in parsed.hostname
                or parsed.hostname.endswith(((
                    ".local"
                ), (
                    ".internal"
                ), (
                    ".localhost"
                )))):
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
            if (not isinstance(digest, str)
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                    or digest[7:] != hashlib.sha256(body).hexdigest()):
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
            "schema", "repository", "run_id", "run_attempt", "source", "state",
        }:
            raise ValueError("Invalid artifact envelope")
        run_id, attempt, source = identity
        if (type(envelope["schema"]) is not int or envelope["schema"] != 1
                or envelope["repository"] != "YouthOpps/data-pipeline"
                or type(envelope["run_id"]) is not int
                or type(envelope["run_attempt"]) is not int
                or envelope["run_id"] != run_id
                or envelope["run_attempt"] != attempt
                or envelope["source"] != source):
            raise ValueError("Artifact ownership mismatch")
        return validate_budget(envelope["state"])
    except Exception:
        raise AdapterError((
            "Invalid or unavailable family pacing artifact"
        ), (
            "access"
        )) from None


def restore_family_artifact():
    global _RESTORED, _STATE_TRUSTED
    if _RESTORED:
        return
    latest = latest_family_run()
    if latest is None:
        raw = os.environ.get("DZS_PACING_BOOTSTRAP", "")
        try:
            bootstrap = json.loads(raw)
            if (set(bootstrap) != {"schema", "family", "not_before", "evidence"}
                    or type(bootstrap["schema"]) is not int
                    or bootstrap["schema"] != 1 or bootstrap["family"] != FAMILY
                    or not numeric(bootstrap["not_before"])
                    or not isinstance(bootstrap["evidence"], str)
                    or not re.fullmatch((
                        "https://github\\.com/YouthOpps/data-pipeline/(?:is"
                        "sues|pull)/[0-9]+(?:#issuecomment-[0-9]+)?"
                    ), bootstrap[(
                        "evidence"
                    )])):
                raise ValueError("Invalid bootstrap")
            incoming = empty_budget(time.time())
            incoming[(
                "not_before"
            )] = max(time.time() + 60, bootstrap[(
                "not_before"
            )])
        except Exception:
            raise AdapterError((
                "Reviewed first-family bootstrap required"
            ), (
                "access"
            )) from None
    else:
        run_id, attempt, source = latest
        expected = _ARTIFACT_NAME + "-" + str(attempt)
        result = workflow_api(f"/actions/runs/{run_id}/artifacts?per_page=100")
        artifacts = result.get("artifacts")
        if (not isinstance(artifacts, list) or result.get((
            "total_count"
        )) != len(artifacts)
                or len(artifacts) > 100):
            raise AdapterError("Incomplete family artifact inventory", "access")
        matches = [a for a in artifacts if a.get("name") == expected]
        if len(matches) != 1 or matches[0].get("expired") is not False:
            raise AdapterError((
                "Newest family pacing artifact missing or expired"
            ), (
                "access"
            ))
        artifact = matches[0]
        if (type(artifact.get("id")) is not int or artifact["id"] <= 0
                or artifact.get("workflow_run", {}).get("id") != run_id
                or not numeric(artifact.get("size_in_bytes"))
                or artifact["size_in_bytes"] > 32768):
            raise AdapterError("Invalid family artifact binding", "access")
        incoming = download_inert_artifact(latest, artifact[(
            "id"
        )], artifact.get((
            "digest"
        )))
    with locked_budget():
        local = load_budget()
        now = time.time()
        combined = empty_budget(now)
        combined[(
            "not_before"
        )] = max(local[(
            "not_before"
        )], incoming[(
            "not_before"
        )], now + 60)
        combined["blocked"] = local["blocked"] or incoming["blocked"]
        combined[(
            "starts"
        )] = sorted(set(local[(
            "starts"
        )] + incoming[(
            "starts"
        )]))[-10:]
        save_budget(combined)
    _RESTORED, _STATE_TRUSTED = True, True


def export_family_artifact():
    """Best-effort infrastructure state; never contains records or secrets."""
    path = os.environ.get("DZS_PACING_ARTIFACT_PATH")
    if not path or not _STATE_TRUSTED:
        return
    run_id, attempt = current_run_identity()
    with locked_budget():
        state = load_budget()
    envelope = {
        "schema": 1, "repository": "YouthOpps/data-pipeline",
        "run_id": run_id, "run_attempt": attempt, "source": SOURCE_ID,
        "state": state,
    }
    configured = os.environ.get("RUNNER_TEMP", "")
    if not configured or not os.path.isabs(configured):
        raise AdapterError((
            "Explicit runner temporary directory required"
        ), (
            "access"
        ))
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
            raise AdapterError((
                "Another local family collector is active"
            ), (
                "access"
            )) from None
        _COLLECTION_LOCK_FD = descriptor
    if os.environ.get("GITHUB_ACTIONS") == "true":
        restore_family_artifact()
    _STATE_TRUSTED = True


def document_root(text, complete=True):
    if complete and not re.search(
            r"</body\s*>\s*</html\s*>\s*(?:<!--[\s\S]*?-->\s*)*$",
            text, re.I):
        raise AdapterError("Incomplete public HTML", "parse")
    root = PublisherHTML(text).root
    title = " ".join(n.text() for n in nodes(root, "title")).lower()
    if re.match(r"just a moment|access denied|attention required|"
                r"checking your browser|verify you are human", title):
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
            _GITHUB + path, method=method, headers=headers,
            payload=(json.dumps(payload).encode()
                     if payload is not None else None),
        )
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise AdapterError("GitHub connection failed; outcome unconfirmed",
                           "publish") from error
    if status == 404 and missing_ok:
        return None
    if status not in (200, 201):
        raise AdapterError(f"GitHub publication HTTP {status}",
                           "publish", status)
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
    allowed = {info["url"] for info in INPUTS.values()}
    if (clean not in allowed or parsed.scheme != "https"
            or parsed.username or parsed.password or parsed.port):
        raise AdapterError("Unreviewed public source route", "access")
    return clean


def html_fingerprint(body):
    root = document_root(body.decode("utf-8", "strict"))
    links = sorted({
        (node.tag, node.attrs.get("rel", ""), node.attrs["href"])
        for tag in ("a", "link") for node in nodes(root, tag)
        if node.attrs.get("href")
    })
    facts = json.dumps({"text": root.text(), "links": links},
                       ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(facts.encode()).hexdigest()


def read_input(key):
    info = INPUTS[key]
    try:
        status, headers, body = fetch_public(info["url"])
    except AdapterError as error:
        raise AdapterError("Required publisher input " + key + ": "
                           + safe_error(error), error.stage,
                           error.status) from None
    if info["format"] == "html":
        digest = html_fingerprint(body)
    else:
        if info["format"] == "pdf" and not body.startswith(b"%PDF-"):
            raise AdapterError("Invalid material document: " + key, "parse")
        digest = hashlib.sha256(body).hexdigest()
    if digest != info["sha256"]:
        raise AdapterError("Reviewed source facts/frontier changed: " + key,
                           "parse")
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
            profile["title"], profile["url"], [profile["category"]],
            kind=profile["kind"], host_countries=profile["hosts"],
            evidence=[ATTRIBUTION, profile["proof"]],
        )
        record["id"] = hashlib.sha256(
            (SOURCE_ID + "|" + profile["key"]).encode()
        ).hexdigest()[:24]
        record["summary"] = profile["summary"]
        record["deadline"] = profile["deadline"]
        record["language"] = "en" if profile["key"].startswith(
            "delegated-"
        ) else "cs"
        deadline = profile["deadline"]
        if "T" in deadline:
            closed = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            record["status"] = "expired" if now >= closed else "open"
        else:
            record["status"] = (
                "expired" if now.date().isoformat() > deadline
                else "open" if now.date().isoformat() < deadline
                else "unknown"
            )
        records.append(record)
    validate_records(records)
    if len(records) != 16:
        raise AdapterError("Incomplete reviewed identity partition", "validate")
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
        "name": "DZS — European Solidarity Corps",
        "source_url": SOURCE_URL,
        "website_url": WEBSITE_URL,
        "language": LANGUAGE,
        "publisher_country": PUBLISHER_COUNTRY,
        "publisher_type": PUBLISHER_TYPE,
        "attribution": ATTRIBUTION,
        "status": "fail" if error else "success",
        "last_attempt_at": attempt,
        "last_success_at": (previous.get("last_success_at")
                            if error else attempt),
        "last_checked_at": (previous.get("last_checked_at")
                            if error else checked_at),
        "record_count": len(records),
        "message": (
            "Collection failed; last-good data preserved" if error else
            f"Collected {len(records)} reviewed ESC grants and training offers"
        ),
        "error": safe_error(error) if error else None,
    }
    if error:
        outcome["failure_stage"] = getattr(error, "stage", "parse")
    return outcome


def phase_error(name):
    return AdapterError("Whole " + name + " phase deadline exceeded",
                        "publish" if name == "publication" else "access")


def serialize(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def fresh_records(records, old, attempt, checked):
    prior = {record["id"]: record for record in old}
    temporal = {"created_at", "updated_at", "first_seen_at", "last_seen_at",
                "last_checked_at"}
    for record in records:
        before = prior.get(record["id"], {})
        same = {k: v for k, v in before.items() if k not in temporal} == {
            k: v for k, v in record.items() if k not in temporal
        }
        record.update(
            created_at=before.get("created_at", attempt),
            first_seen_at=before.get("first_seen_at", attempt),
            updated_at=before.get("updated_at", attempt) if same else attempt,
            last_seen_at=attempt, last_checked_at=checked,
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
                        candidate["files"]["data.json"])
                    candidate_metadata = json.loads(
                        candidate["files"]["metadata.json"])
                    validate_records(candidate_records)
                    if (not isinstance(candidate_metadata, dict)
                            or candidate_metadata.get("source") != SOURCE_ID):
                        raise AdapterError("Remote metadata source mismatch",
                                           "publish")
                    old, previous = candidate_records, candidate_metadata
                snapshot = candidate
            records = collect()
            checked = utc_now()
            fresh_records(records, old, attempt, checked)
            outcome = metadata(attempt, previous, records, checked_at=checked)
            desired = {"data.json": serialize(records),
                       "metadata.json": serialize(outcome)}
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
                        raise AdapterError("No validated previous source pair",
                                           "publish")
                    latest = read_snapshot()
                    if desired is not None and latest["files"] == desired:
                        outcome = json.loads(desired["metadata.json"])
                        failure = None
                    elif old and latest["files"] == snapshot["files"]:
                        publish_files(latest, {
                            "metadata.json": serialize(outcome),
                        })
                    else:
                        raise AdapterError("No safe unchanged last-good pair",
                                           "publish")
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
                print("Timing artifact export failed; next run fail closed",
                      file=sys.stderr)
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


INPUTS = {'allocation': {'url': (
    'https://www.dzs.cz/sites/default/files/2026-01/Aloka%C4'
    '%8Dn%C3%AD%20krit%C3%A9ria%202026_Evropsk%C3%BD%20sbor%'
    '20solidarity_0.pdf'
),
                'format': 'pdf',
                'sha256': (
                    '78208e95b4c465e7033d1c8f096ff5bae1a7b489e5c1e138b7f6897'
                    '8a8cfb37f'
                )},
 'budget': {'url': (
     'https://www.dzs.cz/sites/default/files/2025-11/ESS_Rozp'
     'ocet_CZ_2026.pdf'
 ),
            'format': 'pdf',
            'sha256': (
                'ec59313837a6df82eddbd2e9afbe52bfe8d7d0609c9ec3161631a0b'
                '9eefc9044'
            )},
 'call-event-1': {'url': (
     'https://www.dzs.cz/udalost/individualni-konzultace-k-so'
     'lidarnim-projektum-esc30'
 ),
                  'format': 'html',
                  'sha256': (
                      '4bb3186fb96291883b8742275af441d5ab5240d75fab6e6053dd47c'
                      '5d4956123'
                  )},
 'call-event-2': {'url': (
     'https://www.dzs.cz/udalost/informacni-webinar-pro-zadat'
     'ele-o-solidarni-projekty-esc30-0'
 ),
                  'format': 'html',
                  'sha256': (
                      'aa0ca2212b21cb2342a8d4914259c9e2d453db4c1c33d83596f4725'
                      '1403eb5f5'
                  )},
 'call-event-3': {'url': (
     'https://www.dzs.cz/udalost/individualni-konzultace-k-so'
     'lidarnim-projektum-esc30-0'
 ),
                  'format': 'html',
                  'sha256': (
                      'a53283540afbd754236b649bab73c9ff2d97c94a518955484662fb2'
                      '0fd35431d'
                  )},
 'call-event-4': {'url': (
     'https://www.dzs.cz/udalost/webinar-tipy-k-zadosti-pro-z'
     'ajemce-o-solidarni-projekty-esc30-0'
 ),
                  'format': 'html',
                  'sha256': (
                      '82183e2c0cbdb6831bc7d6758b75cc2fdca7658858898fd30cf8650'
                      '964fed3e0'
                  )},
 'call-event-5': {'url': (
     'https://www.dzs.cz/udalost/individualni-konzultace-k-so'
     'lidarnim-projektum-esc30-1'
 ),
                  'format': 'html',
                  'sha256': (
                      'e66967c35b6e05272deed4affbdd2000e448a1650f0cbb8ddab36fe'
                      'ac59554ef'
                  )},
 'call-event-6': {'url': (
     'https://www.dzs.cz/udalost/individualni-konzultace-k-so'
     'lidarnim-projektum-esc30-2'
 ),
                  'format': 'html',
                  'sha256': (
                      '257145eb72e135f2f4cd750d89ea5e99c9303cbb3d58ce913e42f73'
                      '0670845f4'
                  )},
 'call-event-7': {'url': (
     'https://www.dzs.cz/udalost/individualni-konzultace-k-so'
     'lidarnim-projektum-esc30-3'
 ),
                  'format': 'html',
                  'sha256': (
                      'e74c62a1998d8258d2bd3b5f8facd7facd5389de26a52fbe87fd86c'
                      '1fd2a401f'
                  )},
 'call-event-8': {'url': (
     'https://www.dzs.cz/udalost/informacni-webinar-k-zadosti'
     '-o-grant-na-dobrovolnicke-projekty-esc51-0'
 ),
                  'format': 'html',
                  'sha256': (
                      '5503ba5c008c067b24509029c87f6e99614bf08f714a7e2a1f067a6'
                      'cd223afe1'
                  )},
 'call': {'url': 'https://www.dzs.cz/vyzva_sbor',
          'format': 'html',
          'sha256': (
              'c87a7484dd2a84fe3a44625c6591bd8c03d7a9dbea2c304701f8461'
              'fcfeddc50'
          )},
 'delegated-1': {'url': (
     'https://www.salto-youth.net/tools/european-training-cal'
     'endar/training/spaces-that-empower-rethinking-environme'
     'nts-for-young-people-in-europe-in-german-language.14430'
     '/'
 ),
                 'format': 'html',
                 'sha256': (
                     '3010e9e172691a492059285503889f223406f1703b06501b2f33a85'
                     '03c890a5b'
                 )},
 'delegated-2': {'url': (
     'https://www.salto-youth.net/tools/european-training-cal'
     'endar/training/massive-open-online-course-on-european-s'
     'olidarity-corps-2026.15280/'
 ),
                 'format': 'html',
                 'sha256': (
                     '8198b0733f3ea497e89e269d13312d76ac8c9b590c9fd693fb53189'
                     'aa361b4de'
                 )},
 'delegated-3': {'url': (
     'https://www.salto-youth.net/tools/european-training-cal'
     'endar/training/co-create-space-with-and-for-youth.15244'
     '/'
 ),
                 'format': 'html',
                 'sha256': (
                     '1a90a8b6570a9573b834aeaaed693ba350fa6aaa8ce8e66d8c432ce'
                     'c60a1454a'
                 )},
 'delegated-4': {'url': (
     'https://www.salto-youth.net/tools/european-training-cal'
     'endar/training/training-for-mentors-2.15320/'
 ),
                 'format': 'html',
                 'sha256': (
                     '5e502eaea11c7e71c1315312db35fb9e95542193c05a08cf1ce5fe1'
                     '40ee44686'
                 )},
 'delegated-5': {'url': (
     'https://www.salto-youth.net/tools/european-training-cal'
     'endar/training/volunteering-for-all-slovakia-december-2'
     '026.15317/'
 ),
                 'format': 'html',
                 'sha256': (
                     'c936c0d1d7684ee0342d4d696045f8165f4c4687dcd8065ce231c9c'
                     '3d3ed2523'
                 )},
 'delegated-6': {'url': (
     'https://www.salto-youth.net/tools/european-training-cal'
     'endar/training/ctrl-alt-solidarity-training-course-on-d'
     'igital-empowerment-in-european-solidarity-corps.15300/'
 ),
                 'format': 'html',
                 'sha256': (
                     'de3a5691c2d5f5037b506c215ad59ebf479e5635b4370765d005a43'
                     '0bc8edc5b'
                 )},
 'delegated-7': {'url': (
     'https://www.salto-youth.net/tools/european-training-cal'
     'endar/training/safeguarding-in-esc-organisations-in-pra'
     'ctice.15356/'
 ),
                 'format': 'html',
                 'sha256': (
                     'ad0b2705859ab70f90bf2563b54106d9392ee02a1176271700331ad'
                     '17ef94c67'
                 )},
 'delegated-8': {'url': (
     'https://www.salto-youth.net/tools/european-training-cal'
     'endar/training/tosca-volunteering-training-and-support-'
     'for-organisations-in-the-european-solidarity-corps.1502'
     '1/'
 ),
                 'format': 'html',
                 'sha256': (
                     '63c30e9931e8192581d0d8c1ada64371270542b17d9d272018b3fb8'
                     '49a1415ec'
                 )},
 'grants-en': {'url': (
     'https://www.dzs.cz/en/program/european-solidarity-corps'
     '/projects-and-grants'
 ),
               'format': 'html',
               'sha256': (
                   'f48b2f56a68d214aa0c16749dcaddf877c93abbfc597f78fa63da34'
                   '1b4d19d3b'
               )},
 'grants': {'url': (
     'https://www.dzs.cz/program/evropsky-sbor-solidarity/pro'
     'jekty-granty'
 ),
            'format': 'html',
            'sha256': (
                'd322c3a9d0aa31e5ef781c5dbf64b9fe712587a6c610e07f737e454'
                'fea6248f6'
            )},
 'guide-en': {'url': (
     'https://www.dzs.cz/sites/default/files/2025-11/european'
     '_solidarity_corps_guide_2026_EN.pdf'
 ),
              'format': 'pdf',
              'sha256': (
                  'aee8797fe578c09e2d7209d4facc36dac8f56b39914fca87210f78e'
                  '17277457e'
              )},
 'idea': {'url': 'https://www.dzs.cz/udalost/od-napadu-k-projektu-5',
          'format': 'html',
          'sha256': (
              '849221761e68846b28f3d09653a602f3090bd233d7208ba9c7b7b7e'
              '487eb84bf'
          )},
 'infokit': {'url': (
     'https://www.dzs.cz/sites/default/files/2025-01/InfoKit-'
     'Volunteers-2024.pdf'
 ),
             'format': 'pdf',
             'sha256': (
                 '7975b70e778e75203e2e0f65e43ef980cf0e64095d8475c1c98f8f5'
                 '4ad3ba02f'
             )},
 'information': {'url': 'https://www.dzs.cz/povinne-zverejnovane-informace',
                 'format': 'html',
                 'sha256': (
                     '97b5fb5d50f57c2970bbe29853570db00f56c5fbd2e67ab8626aeaf'
                     '73ff22018'
                 )},
 'net-support': {'url': (
     'https://www.dzs.cz/sites/default/files/2024-09/Informa%'
     'C4%8Dn%C3%AD%20materi%C3%A1l%20pro%20p%C5%99%C3%ADjemce'
     '_9_2024.pdf'
 ),
                 'format': 'pdf',
                 'sha256': (
                     'f410b10691b062dfc44f01bc669acd067ea67dfb8fc581cdc5e8a25'
                     '79c824b6d'
                 )},
 'net': {'url': 'https://www.dzs.cz/networking-activities',
         'format': 'html',
         'sha256': (
             'd465de205bec389511170762cbadf74b76f25280cce7d6e4320877a'
             '46c6ae2cc'
         )},
 'privacy': {'url': 'https://www.dzs.cz/zpracovani-osobnich-udaju',
             'format': 'html',
             'sha256': (
                 '9978730f8440933a86ef73f8291c049a41a9fbdd55d8d2cb9097f19'
                 '0032de92c'
             )},
 'programme-en': {'url': (
     'https://www.dzs.cz/en/program/european-solidarity-corps'
 ),
                  'format': 'html',
                  'sha256': (
                      '3fa21cb3a3def9b8c5ea1cd3a70d24bd4c72293d9c85824b976d6a0'
                      'bd63a1f84'
                  )},
 'programme': {'url': 'https://www.dzs.cz/program/evropsky-sbor-solidarity',
               'format': 'html',
               'sha256': (
                   'f8a78383bff2c21b5e3c242ef721ff53a8b292755a59d474cdebf49'
                   '320d0b74e'
               )},
 'repeat': {'url': (
     'https://www.dzs.cz/sites/default/files/2024-01/Pravidla'
     '%20pro%20%C3%BA%C4%8Dast%20v%20dobrovolnictv%C3%AD.pdf'
 ),
            'format': 'pdf',
            'sha256': (
                'b62a1bb18ee0e53bd194c7da9d21f6c8507116df6aa4ea70cf72078'
                'd746073d7'
            )},
 'robots': {'url': 'https://www.dzs.cz/robots.txt',
            'format': 'robots',
            'sha256': (
                '8ddfdc6072f037a763a82d6271fca793f2c52e726d3650ebe654230'
                '23455a0e1'
            )},
 'salto-legal': {'url': 'https://www.salto-youth.net/about/legal-notice/',
                 'format': 'html',
                 'sha256': (
                     'd0074a9b164c34186917b04a7c2495f9ff1ccbd5791f2e492cf8782'
                     '5db17a5fc'
                 )},
 'salto-robots': {'url': 'https://www.salto-youth.net/robots.txt',
                  'format': 'robots',
                  'sha256': (
                      '121b801898036dc3ff9daccc435ae53803887aa4565847b7543b2af'
                      'b0ec0c412'
                  )},
 'solid-faq': {'url': (
     'https://www.dzs.cz/sites/default/files/2026-06/FAQ_SOLI'
     'D_NEW_2026%201%20%281%29.pdf'
 ),
               'format': 'pdf',
               'sha256': (
                   '03931786c5fe1d309d8575d10081da870c2f47f6438791bfbc49538'
                   '03764b755'
               )},
 'solid-new-faq': {'url': (
     'https://www.dzs.cz/sites/default/files/2025-01/solpro%2'
     '0FAQ_0.pdf'
 ),
                   'format': 'pdf',
                   'sha256': (
                       '974670331182695e397372d3b5314ce8353ed53a2fa3d8be6eef76e'
                       'fc13d5b82'
                   )},
 'stays': {'url': (
     'https://www.dzs.cz/program/evropsky-sbor-solidarity/vyj'
     'ezdy-pobyty'
 ),
           'format': 'html',
           'sha256': (
               '0122269bf491f801235ba6ea219a29fbab683dc9deb110125460089'
               '700b3b44f'
           )},
 'tosca': {'url': (
     'https://www.dzs.cz/udalost/tosca-volunteering-skoleni-p'
     'odpora-pro-organizace-v-programu-evropsky-sbor-solidari'
     'ty'
 ),
           'format': 'html',
           'sha256': (
               '31587f9422bac90644a0d33460aea1f5fa5331d7876a02a99a88b4f'
               '16b104577'
           )},
 'volunteer-faq': {'url': (
     'https://www.dzs.cz/sites/default/files/2024-05/FAQ%20Do'
     'brovolnictv%C3%AD%202024.pdf'
 ),
                   'format': 'pdf',
                   'sha256': (
                       'b10b1897d215f463d45a5db383d1d730bc3fadb011e1683c8fd275d'
                       'da81b4312'
                   )},
 'volunteer-training': {'url': (
     'https://www.dzs.cz/sites/default/files/2026-06/Registra'
     'ce%20na%20%C5%A1kolen%C3%AD_koordin%C3%A1to%C5%99i_OA3-'
     'MT3-OA4-MT4_2026-2027.pdf'
 ),
                        'format': 'pdf',
                        'sha256': (
                            (
                                '420eb98909f6caee8c4dba41567e11ad8759c5686fa879'
                                '31735d3b35e432a36c'
                            )
                        )}}

PROFILES = [{'key': 'solidarity-2026',
  'title': 'Solidární projekty',
  'url': (
      'https://www.dzs.cz/program/evropsky-sbor-solidarity/pro'
      'jekty-granty#solidarni-projekty'
  ),
  'category': 'grants',
  'kind': 'opportunity',
  'hosts': ['CZ'],
  'deadline': '2026-10-01T10:00:00Z',
  'summary': 'Groups of at least five Czech residents aged 18–30 can run '
             'non-profit 2–12 month community projects, optionally through '
             'an organisation. Funding is EUR 630 per project month plus EUR '
             '227 per coach day, maximum 12 days, with conditional '
             'exceptional costs; this is not a personal stipend. Projects '
             'are primarily in Czechia, with eligible bordering-region '
             'exceptions. The 2026 rounds closed 18 February and 1 October '
             'at noon Brussels time. Older English website amounts and dates '
             'differ.',
  'proof': '2026 Czech grant/call; guide pp60–66; March2026 financial FAQ'},
 {'key': 'volunteering-2026',
  'title': 'Dobrovolnické projekty',
  'url': (
      'https://www.dzs.cz/program/evropsky-sbor-solidarity/pro'
      'jekty-granty#dobrovolnicke-projekty'
  ),
  'category': 'grants',
  'kind': 'institutional-grant',
  'hosts': [],
  'deadline': '2026-10-01T10:00:00Z',
  'summary': 'Czech organisations with a valid lead Quality Label apply for '
             'project funding, not individual paid jobs. Unpaid volunteering '
             'runs 30–38 h/week; the 2026 call/guide allow individual stays '
             '2 weeks–12 months, unlike the older hub. Travel, subsistence, '
             'pocket money and inclusion support are conditional. Ordinary '
             '2026 allocation limits are EUR 15,373–350,000 per applicant '
             'project, not guaranteed grants. National rounds closed 18 '
             'February and 1 October at noon Brussels time. Hosts can be '
             'domestic or abroad.',
  'proof': '2026 national call; guide pp37–51; allocation2026; older hub '
           'conflict'},
 {'key': 'priority-teams-2026',
  'title': 'Dobrovolnické týmy v oblastech s vysokou prioritou',
  'url': (
      'https://www.dzs.cz/program/evropsky-sbor-solidarity/pro'
      'jekty-granty#centralizovane-aktivity'
  ),
  'category': 'grants',
  'kind': 'institutional-grant',
  'hosts': [],
  'deadline': '2026-03-03T16:00:00Z',
  'summary': 'EACEA organisational grants cover 12,24 or 36 month projects '
             'up to EUR 400,000 total, not per volunteer. Consortium: at '
             'least three eligible Quality Label organisations from two '
             'programme countries; coordinator has lead status. Teams are '
             'unpaid 2 week–2 month activities for residents aged 18–30. The '
             'guide targets 40 participants per project in principle; the '
             'DZS hub calls it a minimum, not 40 per team. Three priority '
             'themes are not separate awards. Closed 3 March 2026 at 17:00 '
             'Brussels time.',
  'proof': 'DZS centralised section;2026 guide pp52–59; project-target '
           'discrepancy'},
 {'key': 'humanitarian-2026',
  'title': 'Dobrovolnictví v oblasti humanitární pomoci',
  'url': (
      'https://www.dzs.cz/program/evropsky-sbor-solidarity/pro'
      'jekty-granty#centralizovane-aktivity'
  ),
  'category': 'grants',
  'kind': 'institutional-grant',
  'hosts': [],
  'deadline': '2026-04-23T15:00:00Z',
  'summary': 'EACEA grants up to EUR 650,000 per 12,24 or 36 month project '
             'support humanitarian volunteering, not individual stipends. At '
             'least three eligible Quality Label organisations include two '
             'support bodies from different programme countries and an '
             'independent host. Participants 18–35 must complete Commission '
             'training. The guide requires legal EU/associated-country '
             'residence; DZS says permanent residence, not citizenship. '
             'Hosts are humanitarian-operation countries without armed '
             'conflict. Closed 23 April 2026,17:00 Brussels.',
  'proof': 'DZS humanitarian section;2026 guide pp77–85; residence wording '
           'conflict'},
 {'key': 'call-event-2',
  'title': 'Informační webinář pro žadatele o solidární projekty ESC30',
  'url': (
      'https://www.dzs.cz/udalost/informacni-webinar-pro-zadat'
      'ele-o-solidarni-projekty-esc30-0'
  ),
  'category': 'training',
  'kind': 'opportunity',
  'hosts': [],
  'deadline': '2026-09-01',
  'summary': 'Free online Teams workshop on 2 September 2026 for youth aged '
             '18–30 preparing community projects and staff of supporting '
             'organisations. Covers programme priorities, project design, '
             'budgets and application steps. Registration closed 1 '
             'September; event times are not deadlines. No physical '
             'destination or passport restriction is inferred.',
  'proof': (
      'Actual own/delegated original heading and full conditio'
      'ns: https://www.dzs.cz/udalost/informacni-webinar-pro-z'
      'adatele-o-solidarni-projekty-esc30-0'
  )},
 {'key': 'call-event-4',
  'title': 'Webinář: Tipy k žádosti pro zájemce o solidární projekty ESC30',
  'url': (
      'https://www.dzs.cz/udalost/webinar-tipy-k-zadosti-pro-z'
      'ajemce-o-solidarni-projekty-esc30-0'
  ),
  'category': 'training',
  'kind': 'opportunity',
  'hosts': [],
  'deadline': '2026-09-13',
  'summary': 'Free online Teams workshop on 14 September 2026 for '
             'prospective Solidarity Project applicants. Covers '
             'registration, application sections, quality criteria and '
             'common errors. Registration closed 13 September. The body '
             'weekday conflicts with the calendar footer; both give 14 '
             'September. No physical host or citizenship requirement is '
             'inferred.',
  'proof': (
      'Actual own/delegated original heading and full conditio'
      'ns: https://www.dzs.cz/udalost/webinar-tipy-k-zadosti-p'
      'ro-zajemce-o-solidarni-projekty-esc30-0'
  )},
 {'key': 'call-event-8',
  'title': 'Informační webinář k žádosti o grant na Dobrovolnické projekty '
           'ESC51',
  'url': (
      'https://www.dzs.cz/udalost/informacni-webinar-k-zadosti'
      '-o-grant-na-dobrovolnicke-projekty-esc51-0'
  ),
  'category': 'training',
  'kind': 'opportunity',
  'hosts': [],
  'deadline': '2026-09-15',
  'summary': 'Online Teams training on 17 September 2026 for representatives '
             'of organisations holding a valid lead Quality Label. Explains '
             'the ESC 51 budget request and project period. Registration '
             'closed 15 September 2026. The own page does not specify a '
             'participation fee; this is not advertised as universally free. '
             'No physical host or citizenship restriction is inferred.',
  'proof': (
      'Actual own/delegated original heading and full conditio'
      'ns: https://www.dzs.cz/udalost/informacni-webinar-k-zad'
      'osti-o-grant-na-dobrovolnicke-projekty-esc51-0'
  )},
 {'key': 'tosca',
  'title': 'TOSCA Volunteering – Školení a podpora pro organizace v programu '
           'Evropský sbor solidarity',
  'url': (
      'https://www.dzs.cz/udalost/tosca-volunteering-skoleni-p'
      'odpora-pro-organizace-v-programu-evropsky-sbor-solidari'
      'ty'
  ),
  'category': 'training',
  'kind': 'opportunity',
  'hosts': ['CZ'],
  'deadline': '2026-08-03',
  'summary': 'Prague training 12–16 October 2026, with online onboarding, '
             'for English-speaking staff connected to Quality Label '
             'volunteering organisations. DZS and SALTO support programme '
             'quality, impact and cooperation. Host accommodation and food '
             'are covered; participation fees vary by country and travel '
             'arrangements follow selection. Registration closed 3 August '
             '2026, not the event date. Source: DZS and SALTO-YOUTH.',
  'proof': (
      'Actual own/delegated original heading and full conditio'
      'ns: https://www.dzs.cz/udalost/tosca-volunteering-skole'
      'ni-podpora-pro-organizace-v-programu-evropsky-sbor-soli'
      'darity'
  )},
 {'key': 'idea',
  'title': 'Od nápadu k projektu',
  'url': 'https://www.dzs.cz/udalost/od-napadu-k-projektu-5',
  'category': 'training',
  'kind': 'opportunity',
  'hosts': ['CZ'],
  'deadline': '2026-10-25',
  'summary': 'Free project-development training in Brno 21–22 November 2026 '
             'for people aged 18–30 with a community idea; a completed '
             'project is not required. Accommodation in shared two-person '
             'rooms, food and travel are covered. Registration closes 25 '
             'October 2026; no clock or timezone is stated. Passport '
             'eligibility is not inferred.',
  'proof': 'Actual own/delegated original heading and full conditions: '
           'https://www.dzs.cz/udalost/od-napadu-k-projektu-5'},
 {'key': 'delegated-1',
  'title': 'Spaces that Empower: Rethinking Environments for Young People in '
           'Europe (IN GERMAN LANGUAGE)',
  'url': (
      'https://www.salto-youth.net/tools/european-training-cal'
      'endar/training/spaces-that-empower-rethinking-environme'
      'nts-for-young-people-in-europe-in-german-language.14430'
      '/'
  ),
  'category': 'training',
  'kind': 'opportunity',
  'hosts': ['LU'],
  'deadline': '2026-09-30',
  'summary': 'German-language training in Luxembourg 17–20 November 2026 for '
             'youth workers, project managers and policymakers on youth '
             'spaces. Anefore and the national youth service organise it. '
             'Single-room accommodation and food are covered; national '
             'participation fees and travel support are conditional. '
             'Registration closed 30 September 2026. Country participation '
             'is not a citizenship whitelist. Source: DZS and SALTO-YOUTH.',
  'proof': (
      'Actual own/delegated original heading and full conditio'
      'ns: https://www.salto-youth.net/tools/european-training'
      '-calendar/training/spaces-that-empower-rethinking-envir'
      'onments-for-young-people-in-europe-in-german-language.1'
      '4430/'
  )},
 {'key': 'delegated-2',
  'title': 'Massive Open Online Course on European Solidarity Corps 2026',
  'url': (
      'https://www.salto-youth.net/tools/european-training-cal'
      'endar/training/massive-open-online-course-on-european-s'
      'olidarity-corps-2026.15280/'
  ),
  'category': 'training',
  'kind': 'opportunity',
  'hosts': [],
  'deadline': '2026-09-30',
  'summary': 'Free online ESC course 5 October–30 November 2026 for '
             'interested programme actors, coordinated by Leargas and SALTO. '
             'English content is largely translated into Spanish, Italian, '
             'German and Turkish. Completion offers digital badges and '
             'Youthpass. Registration closed 30 September despite the course '
             'continuing. Ireland is the organiser location, not a physical '
             'destination or travel benefit. Source: DZS and SALTO-YOUTH.',
  'proof': (
      'Actual own/delegated original heading and full conditio'
      'ns: https://www.salto-youth.net/tools/european-training'
      '-calendar/training/massive-open-online-course-on-europe'
      'an-solidarity-corps-2026.15280/'
  )},
 {'key': 'delegated-3',
  'title': 'Co-create space with and for youth',
  'url': (
      'https://www.salto-youth.net/tools/european-training-cal'
      'endar/training/co-create-space-with-and-for-youth.15244'
      '/'
  ),
  'category': 'training',
  'kind': 'opportunity',
  'hosts': ['BE'],
  'deadline': '2026-09-29',
  'summary': 'JINT English-language training in Brussels 14–18 December 2026 '
             'with online preparation in November. Youth-work professionals '
             'explore co-designing a specific local space with young people; '
             'prior spatial-design experience is not required. Hosting '
             'accommodation and food are covered unless otherwise specified; '
             'national fees and travel support are conditional. Registration '
             'closed 29 September 2026. Source: DZS and SALTO-YOUTH.',
  'proof': (
      'Actual own/delegated original heading and full conditio'
      'ns: https://www.salto-youth.net/tools/european-training'
      '-calendar/training/co-create-space-with-and-for-youth.1'
      '5244/'
  )},
 {'key': 'delegated-4',
  'title': 'Training for Mentors 2',
  'url': (
      'https://www.salto-youth.net/tools/european-training-cal'
      'endar/training/training-for-mentors-2.15320/'
  ),
  'category': 'training',
  'kind': 'opportunity',
  'hosts': ['GR'],
  'deadline': '2026-10-12T00:00:00Z',
  'summary': 'English-language mentor training in Ioannina 2–6 November 2026 '
             'for current or prospective ESC mentors; extensive experience '
             'is not required. INEDIVIM covers double-room accommodation and '
             'food. Participation fees vary by country of residence; travel '
             'support needs agency confirmation. The literal closing is 11 '
             'October 2026 at 24 hUTC, meaning 12 October 00:00 UTC. '
             'Programme-country participation is not a passport whitelist. '
             'Source: DZS and SALTO-YOUTH.',
  'proof': (
      'Actual own/delegated original heading and full conditio'
      'ns: https://www.salto-youth.net/tools/european-training'
      '-calendar/training/training-for-mentors-2.15320/'
  )},
 {'key': 'delegated-5',
  'title': 'Volunteering for ALL - Slovakia December 2026',
  'url': (
      'https://www.salto-youth.net/tools/european-training-cal'
      'endar/training/volunteering-for-all-slovakia-december-2'
      '026.15317/'
  ),
  'category': 'training',
  'kind': 'opportunity',
  'hosts': ['SK'],
  'deadline': '2026-10-22T00:00:00Z',
  'summary': 'Slovakia 7–11 December 2026 plus online preparation 23 '
             'November. For newcomers directly working with disadvantaged '
             'youth who have never organised ESC volunteering; experienced '
             'organisers and school-pupil projects are excluded. English '
             'discussion ability is needed. Selected relevant housing, food, '
             'travel and visa costs are covered except a variable national '
             'fee. Closes 21 October at 24 hUTC, meaning 22 October 00:00 '
             'UTC. Source: DZS, SALTO Inclusion and Iuventa.',
  'proof': (
      'Actual own/delegated original heading and full conditio'
      'ns: https://www.salto-youth.net/tools/european-training'
      '-calendar/training/volunteering-for-all-slovakia-decemb'
      'er-2026.15317/'
  )},
 {'key': 'delegated-6',
  'title': 'Ctrl+Alt+Solidarity: training course on digital empowerment in '
           'European Solidarity Corps',
  'url': (
      'https://www.salto-youth.net/tools/european-training-cal'
      'endar/training/ctrl-alt-solidarity-training-course-on-d'
      'igital-empowerment-in-european-solidarity-corps.15300/'
  ),
  'category': 'training',
  'kind': 'opportunity',
  'hosts': ['HR'],
  'deadline': '2026-10-01',
  'summary': 'AMEUP English-language training in Zagreb 1–5 December 2026 '
             'for staff, coordinators, mentors and youth workers in active '
             'ESC organisations. Focuses on digital youth work, not '
             'technical IT. No participation fee; single-room accommodation '
             'and food are covered, travel support is conditional. '
             'Registration closed 1 October 2026. Origin-country lists do '
             'not prove citizenship eligibility. Source: DZS and '
             'SALTO-YOUTH.',
  'proof': (
      'Actual own/delegated original heading and full conditio'
      'ns: https://www.salto-youth.net/tools/european-training'
      '-calendar/training/ctrl-alt-solidarity-training-course-'
      'on-digital-empowerment-in-european-solidarity-corps.153'
      '00/'
  )},
 {'key': 'delegated-7',
  'title': 'Safeguarding in ESC organisations in practice',
  'url': (
      'https://www.salto-youth.net/tools/european-training-cal'
      'endar/training/safeguarding-in-esc-organisations-in-pra'
      'ctice.15356/'
  ),
  'category': 'training',
  'kind': 'opportunity',
  'hosts': ['RS'],
  'deadline': '2026-10-02',
  'summary': 'SALTO SEE safeguarding training in Šabac 2–6 November 2026 for '
             'staff, mentors and tutors of ESC Quality Label organisations. '
             'English; housing and food covered, national fees and travel '
             'support conditional. The body limits programme-country '
             'organisations but its country table is broader, including '
             'Western Balkans; eligibility conflict is retained. '
             'Registration closed 2 October 2026. Source: DZS, SALTO SEE and '
             'Czech/Italian agencies.',
  'proof': (
      'Actual own/delegated original heading and full conditio'
      'ns: https://www.salto-youth.net/tools/european-training'
      '-calendar/training/safeguarding-in-esc-organisations-in'
      '-practice.15356/'
  )}]

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(json.dumps({"source": SOURCE_ID, "status": "fail",
                          "error": safe_error(error),
                          "message": (
                              'Run could not establish a durable outcome'
                          )}))
        sys.exit(1)
