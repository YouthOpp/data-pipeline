"""Collect the reviewed finite FMFI public opportunity inventory."""

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

SOURCE_ID = "sk-comenius-university"
SOURCE_URL = "https://zona.fmph.uniba.sk/detail-novinky/back_to_page/fmfi-uk-zona/article/vyzva-na-podavanie-ziadosti-o-jednorazove-mimoriadne-stipendium-call-for-applications-for-ex-9/"
WEBSITE_URL = "https://zona.fmph.uniba.sk/"
LANGUAGE = "sk"
PUBLISHER_COUNTRY = "SK"
PUBLISHER_TYPE = "university"
ATTRIBUTION = (
    "Comenius University, Faculty of Mathematics, Physics and Informatics "
    "(FMFI); SAIA for the National Scholarship Programme; FIRST Global "
    "Slovakia/RoboSkillz Academy and Nadácia ESET for their own notices. "
    "Coverage: FMFI current public funding, mobility, educational and job "
    "navigation, not the whole university."
)
_GITHUB = "https://api.github.com/repos/YouthOpps/data-source"
_WORKFLOW_GITHUB = "https://api.github.com/repos/YouthOpps/data-pipeline"
_USER_AGENT = (
    "YouthOpps/1.0 (+https://github.com/YouthOpps/data-pipeline; "
    "public opportunity metadata)"
)
CATEGORIES = {"scholarships", "grants", "internships", "jobs", "training"}
FAMILY = "youthopps-uniba-publisher-v1"
FAMILY_SOURCES = {"sk-comenius-university"}
COLLECTION_TIMEOUT = 900
RUN_TIMEOUT = 1290
EXPORT_TIMEOUT = 30
_STATE_SCHEMA = 1
_ARTIFACT_NAME = "uniba-pacing-state"
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
            body = response.read(byte_limit + 1)
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
        raw = os.environ.get("UNIBA_PACING_BOOTSTRAP", "")
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
    path = os.environ.get("UNIBA_PACING_ARTIFACT_PATH")
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
    allowed = {info["url"] for info in INPUTS.values()}
    if (
        clean not in allowed
        or parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port
    ):
        raise AdapterError("Unreviewed public source route", "access")
    return clean


ROLLING_WIDGETS = {
    "doctoral": {
        "widget": "c54487",
        "header": "c54489",
        "entries": {
            "/detail-novinky/back_to_page/studium/article/seminar-z-algebraickej-teorie-grafov-yasamin-khaefi-9102026/": {
                "counterpart": "/detail-novinky/back_to_page/doktorandi-a-doktoranske-studium/article/seminar-z-algebraickej-teorie-grafov-yasamin-khaefi-9102026/",
                "title": "Seminár "
                "z "
                "algebraickej "
                "teórie "
                "grafov "
                "- "
                "Yasamin "
                "Khaefi "
                "(9.10.2026)",
                "text": "Seminár "
                "z "
                "algebraickej "
                "teórie "
                "grafov "
                "- "
                "Yasamin "
                "Khaefi...",
            },
            "/detail-novinky/back_to_page/studium/article/doktorandske-kolokvium-kai-zekeri-adams-12102026/": {
                "counterpart": "/detail-novinky/back_to_page/doktorandi-a-doktoranske-studium/article/doktorandske-kolokvium-kai-zekeri-adams-12102026/",
                "title": "Doktorandské "
                "kolokvium "
                "KAI "
                "- "
                "Zekeri "
                "Adams "
                "(12.10.2026)",
                "text": "Doktorandské "
                "kolokvium "
                "KAI "
                "- "
                "Zekeri "
                "Adams "
                "(12.10.2026)",
            },
            "http://fmph.uniba.sk/o-fakulte/akademicky-senat/doplnujuce-volby-do-skas-2027/": {
                "counterpart": "http://fmph.uniba.sk/o-fakulte/akademicky-senat/doplnujuce-volby-do-skas-2027/",
                "title": "Doplňujúce "
                "voľby "
                "do "
                "ŠKAS "
                "FMFI "
                "UK "
                "- "
                "elektronické "
                "hlasovanie",
                "text": "Doplňujúce "
                "voľby "
                "do "
                "ŠKAS "
                "FMFI "
                "UK "
                "- "
                "elektronické "
                "hlasovanie",
            },
            "http://fmph.uniba.sk/o-fakulte/akademicky-senat/doplnovacie-volby-do-as-uk-2027/": {
                "counterpart": None,
                "title": "Doplňovacie "
                "voľby "
                "do "
                "ŠČAS "
                "UK "
                "- "
                "elektronické "
                "hlasovanie",
                "text": "Doplňovacie "
                "voľby "
                "do "
                "ŠČAS "
                "UK "
                "- "
                "elektronické "
                "hlasovanie",
            },
            "/detail-novinky/back_to_page/studium/article/seminar-katedry-teoretickej-fyziky-surajit-das-13102026/": {
                "counterpart": "/detail-novinky/back_to_page/doktorandi-a-doktoranske-studium/article/seminar-katedry-teoretickej-fyziky-surajit-das-13102026/",
                "title": "Seminár "
                "Katedry "
                "teoretickej "
                "fyziky "
                "- "
                "Surajit "
                "Das "
                "(13.10.2026)",
                "text": "Seminár "
                "Katedry "
                "teoretickej "
                "fyziky "
                "- "
                "Surajit "
                "Das...",
            },
            "/detail-novinky/back_to_page/studium/article/obhajoba-dizertacnej-prace-peter-anthony-13102026/": {
                "counterpart": "/detail-novinky/back_to_page/doktorandi-a-doktoranske-studium/article/obhajoba-dizertacnej-prace-peter-anthony-13102026/",
                "title": "Obhajoba "
                "dizertačnej "
                "práce "
                "- "
                "Peter "
                "Anthony "
                "(13.10.2026)",
                "text": "Obhajoba "
                "dizertačnej "
                "práce "
                "- "
                "Peter "
                "Anthony "
                "(13.10.2026)",
            },
        },
    },
    "students": {
        "widget": "c54479",
        "header": "c54478",
        "entries": {
            "/detail-novinky/back_to_page/studium/article/seminar-z-algebraickej-teorie-grafov-yasamin-khaefi-9102026/": {
                "counterpart": "/detail-novinky/back_to_page/studium/article/seminar-z-algebraickej-teorie-grafov-yasamin-khaefi-9102026/",
                "title": "Seminár "
                "z "
                "algebraickej "
                "teórie "
                "grafov "
                "- "
                "Yasamin "
                "Khaefi "
                "(9.10.2026)",
                "text": "Seminár "
                "z "
                "algebraickej "
                "teórie "
                "grafov "
                "- "
                "Yasamin "
                "Khaefi...",
            },
            "/detail-novinky/back_to_page/studium/article/doktorandske-kolokvium-kai-zekeri-adams-12102026/": {
                "counterpart": "/detail-novinky/back_to_page/studium/article/doktorandske-kolokvium-kai-zekeri-adams-12102026/",
                "title": "Doktorandské "
                "kolokvium "
                "KAI "
                "- "
                "Zekeri "
                "Adams "
                "(12.10.2026)",
                "text": "Doktorandské "
                "kolokvium "
                "KAI "
                "- "
                "Zekeri "
                "Adams "
                "(12.10.2026)",
            },
            "http://fmph.uniba.sk/o-fakulte/akademicky-senat/doplnujuce-volby-do-skas-2027/": {
                "counterpart": "http://fmph.uniba.sk/o-fakulte/akademicky-senat/doplnujuce-volby-do-skas-2027/",
                "title": "Doplňujúce "
                "voľby "
                "do "
                "ŠKAS "
                "FMFI "
                "UK "
                "- "
                "elektronické "
                "hlasovanie",
                "text": "Doplňujúce "
                "voľby "
                "do "
                "ŠKAS "
                "FMFI "
                "UK "
                "- "
                "elektronické "
                "hlasovanie",
            },
            "http://fmph.uniba.sk/o-fakulte/akademicky-senat/doplnovacie-volby-do-as-uk-2027/": {
                "counterpart": None,
                "title": "Doplňovacie "
                "voľby "
                "do "
                "ŠČAS "
                "UK "
                "- "
                "elektronické "
                "hlasovanie",
                "text": "Doplňovacie "
                "voľby "
                "do "
                "ŠČAS "
                "UK "
                "- "
                "elektronické "
                "hlasovanie",
            },
            "/detail-novinky/back_to_page/studium/article/seminar-katedry-teoretickej-fyziky-surajit-das-13102026/": {
                "counterpart": "/detail-novinky/back_to_page/studium/article/seminar-katedry-teoretickej-fyziky-surajit-das-13102026/",
                "title": "Seminár "
                "Katedry "
                "teoretickej "
                "fyziky "
                "- "
                "Surajit "
                "Das "
                "(13.10.2026)",
                "text": "Seminár "
                "Katedry "
                "teoretickej "
                "fyziky "
                "- "
                "Surajit "
                "Das...",
            },
            "/detail-novinky/back_to_page/studium/article/obhajoba-dizertacnej-prace-peter-anthony-13102026/": {
                "counterpart": "/detail-novinky/back_to_page/studium/article/obhajoba-dizertacnej-prace-peter-anthony-13102026/",
                "title": "Obhajoba "
                "dizertačnej "
                "práce "
                "- "
                "Peter "
                "Anthony "
                "(13.10.2026)",
                "text": "Obhajoba "
                "dizertačnej "
                "práce "
                "- "
                "Peter "
                "Anthony "
                "(13.10.2026)",
            },
        },
    },
    "scholarships": {
        "widget": "c54479",
        "header": "c54478",
        "entries": {
            "/detail-novinky/back_to_page/studium/article/doktorandske-kolokvium-kai-zekeri-adams-12102026/": {
                "counterpart": "/detail-novinky/back_to_page/studium/article/doktorandske-kolokvium-kai-zekeri-adams-12102026/",
                "title": "Doktorandské "
                "kolokvium "
                "KAI "
                "- "
                "Zekeri "
                "Adams "
                "(12.10.2026)",
                "text": "Doktorandské "
                "kolokvium "
                "KAI "
                "- "
                "Zekeri "
                "Adams "
                "(12.10.2026)",
            },
            "http://fmph.uniba.sk/o-fakulte/akademicky-senat/doplnujuce-volby-do-skas-2027/": {
                "counterpart": "http://fmph.uniba.sk/o-fakulte/akademicky-senat/doplnujuce-volby-do-skas-2027/",
                "title": "Doplňujúce "
                "voľby "
                "do "
                "ŠKAS "
                "FMFI "
                "UK "
                "- "
                "elektronické "
                "hlasovanie",
                "text": "Doplňujúce "
                "voľby "
                "do "
                "ŠKAS "
                "FMFI "
                "UK "
                "- "
                "elektronické "
                "hlasovanie",
            },
            "http://fmph.uniba.sk/o-fakulte/akademicky-senat/doplnovacie-volby-do-as-uk-2027/": {
                "counterpart": None,
                "title": "Doplňovacie "
                "voľby "
                "do "
                "ŠČAS "
                "UK "
                "- "
                "elektronické "
                "hlasovanie",
                "text": "Doplňovacie "
                "voľby "
                "do "
                "ŠČAS "
                "UK "
                "- "
                "elektronické "
                "hlasovanie",
            },
            "/detail-novinky/back_to_page/studium/article/seminar-katedry-teoretickej-fyziky-surajit-das-13102026/": {
                "counterpart": "/detail-novinky/back_to_page/studium/article/seminar-katedry-teoretickej-fyziky-surajit-das-13102026/",
                "title": "Seminár "
                "Katedry "
                "teoretickej "
                "fyziky "
                "- "
                "Surajit "
                "Das "
                "(13.10.2026)",
                "text": "Seminár "
                "Katedry "
                "teoretickej "
                "fyziky "
                "- "
                "Surajit "
                "Das...",
            },
            "/detail-novinky/back_to_page/studium/article/obhajoba-dizertacnej-prace-peter-anthony-13102026/": {
                "counterpart": "/detail-novinky/back_to_page/studium/article/obhajoba-dizertacnej-prace-peter-anthony-13102026/",
                "title": "Obhajoba "
                "dizertačnej "
                "práce "
                "- "
                "Peter "
                "Anthony "
                "(13.10.2026)",
                "text": "Obhajoba "
                "dizertačnej "
                "práce "
                "- "
                "Peter "
                "Anthony "
                "(13.10.2026)",
            },
            "/detail-novinky/back_to_page/studium/article/eset-science-talks-serge-haroche-o-vede-odvahe-a-zodpovednosti-objavovat-nepoznane/": {
                "counterpart": "/detail-novinky/back_to_page/studium/article/eset-science-talks-serge-haroche-o-vede-odvahe-a-zodpovednosti-objavovat-nepoznane/",
                "title": "ESET "
                "Science "
                "Talks "
                "- "
                "Serge "
                "Haroche: "
                "O "
                "vede, "
                "odvahe "
                "a "
                "zodpovednosti "
                "objavovať "
                "nepoznané",
                "text": "ESET "
                "Science "
                "Talks "
                "- "
                "Serge "
                "Haroche: "
                "O "
                "vede, "
                "odvahe "
                "a...",
            },
            "/detail-novinky/back_to_page/studium/article/vyzva-na-podavanie-ziadosti-o-jednorazove-mimoriadne-stipendium-call-for-applications-for-ex-9/": {
                "counterpart": "/detail-novinky/back_to_page/studium/article/vyzva-na-podavanie-ziadosti-o-jednorazove-mimoriadne-stipendium-call-for-applications-for-ex-9/",
                "title": "Výzva "
                "na "
                "podávanie "
                "žiadostí "
                "o "
                "jednorazové "
                "mimoriadne "
                "štipendium "
                "/ "
                "Call "
                "for "
                "applications "
                "for "
                "extraordinary "
                "scholarship",
                "text": "Výzva "
                "na "
                "podávanie "
                "žiadostí "
                "o "
                "jednorazové "
                "mimoriadne...",
            },
            "/detail-novinky/back_to_page/studium/article/narodny-stipendijny-program-vyzva-3/": {
                "counterpart": "/detail-novinky/back_to_page/studium/article/narodny-stipendijny-program-vyzva-3/",
                "title": "Národný " "štipendijný " "program " "- " "výzva",
                "text": "Národný " "štipendijný " "program " "- " "výzva",
            },
        },
    },
}


def normalize_reviewed_rolling_duplicates(root, key):
    """Remove only proved rolling/calendar duplicates in three reviewed hubs."""
    rule = ROLLING_WIDGETS.get(key)
    if rule is None:
        return
    widget = one(
        [n for n in root.walk() if n.attrs.get("id") == rule["widget"]],
        "rolling widget",
    )
    header = one(
        [n for n in root.walk() if n.attrs.get("id") == rule["header"]],
        "rolling header",
    )
    if (
        widget.tag != "div"
        or widget.attrs != {"id": rule["widget"], "class": "csc-default"}
        or header.tag != "div"
        or header.attrs != {"id": rule["header"], "class": "csc-default"}
        or header.text() != "Najbližšie udalosti:"
    ):
        raise AdapterError("Changed rolling widget structure", "parse")
    parent = one(
        [n for n in root.walk() if widget in n.children],
        "rolling widget parent",
    )
    siblings = [n for n in parent.children if isinstance(n, Node)]
    position = siblings.index(widget)
    if (
        parent.tag != "aside"
        or parent.attrs != {"class": "span3"}
        or position == 0
        or siblings[position - 1] is not header
    ):
        raise AdapterError("Changed rolling widget boundary", "parse")

    def children(node, tag, attributes):
        values = []
        for child in node.children:
            if isinstance(child, Node):
                if child.tag != tag or child.attrs != attributes:
                    raise AdapterError("Changed rolling markup", "parse")
                values.append(child)
            elif child.strip():
                raise AdapterError("Unexpected rolling facts", "parse")
        return values

    container = one(
        children(widget, "div", {"class": "news-header-simple-list"}),
        "rolling list",
    )
    entries = children(
        container, "div", {"class": "news-header-list-container"}
    )
    seen = set()
    for entry in entries:
        title = one(
            children(entry, "div", {"class": "news-header-list-title"}),
            "rolling title",
        )
        link = one(
            [n for n in title.children if isinstance(n, Node)], "rolling link"
        )
        if any(not isinstance(n, Node) and n.strip() for n in title.children):
            raise AdapterError("Unexpected rolling facts", "parse")
        href = link.attrs.get("href")
        reviewed = rule["entries"].get(href)
        if (
            not reviewed
            or href in seen
            or link.tag != "a"
            or link.attrs != {"href": href, "title": reviewed["title"]}
            or any(isinstance(n, Node) for n in link.children)
            or link.text() != reviewed["text"]
        ):
            raise AdapterError("Unknown or changed rolling entry", "parse")
        seen.add(href)
        if reviewed["counterpart"] is None:
            # A shared href with a different election stage is not a duplicate.
            continue
        counterparts = [
            n
            for tooltip in root.walk()
            if tooltip.tag == "div"
            and re.fullmatch(
                r"toolTipIdMenu(?:[1-9]|[12][0-9]|3[01])",
                tooltip.attrs.get("id", ""),
            )
            and tooltip.attrs.get("class") == "newscalendar_tooltip"
            for n in nodes(tooltip, "a")
            if n.attrs.get("href") == reviewed["counterpart"]
            and n.attrs.get("title", "").strip() == reviewed["title"]
        ]
        counterpart = one(counterparts, "retained monthly counterpart")
        if counterpart.text().partition(" - ")[2] != reviewed["title"]:
            raise AdapterError("Changed monthly duplicate title", "parse")
        container.children.remove(entry)


def html_fingerprint(body, key=None):
    root = document_root(body.decode("utf-8", "strict"))
    normalize_reviewed_rolling_duplicates(root, key)
    links = sorted(
        {
            (node.tag, node.attrs.get("rel", ""), node.attrs["href"])
            for tag in ("a", "link")
            for node in nodes(root, tag)
            if node.attrs.get("href")
        }
    )
    facts = json.dumps(
        {"text": root.text(), "links": links},
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
        record["language"] = LANGUAGE
        deadline = profile["deadline"]
        if deadline is not None:
            record["status"] = (
                "expired"
                if now.date().isoformat() > deadline
                else "open" if now.date().isoformat() < deadline else "unknown"
            )
        records.append(record)
    validate_records(records)
    if len(records) != 15:
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
        "name": "FMFI public funding and opportunities — Comenius University",
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
            else f"Collected {len(records)} reviewed FMFI funding and opportunity records"
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


INPUTS = {
    "doctoral-fees": {
        "url": "https://zona.fmph.uniba.sk/doktorandi-a-doktorandske-studium/skolne-a-poplatky/",
        "format": "html",
        "sha256": "5d4b3a9f11b1ebc0bd53a6105949c65ef543ab337f924ff5398d2049c5c69af6",
        "raw_sha256": "0c0a1949b76ca2342bf7c0b59c538a8130c47fd2d081da901afeb6a62e45cd06",
        "bytes": 28936,
    },
    "doctoral-office": {
        "url": "https://zona.fmph.uniba.sk/doktorandi-a-doktorandske-studium/referat-doktorandskeho-studia-postdoktorandskych-pobytov-a-riadenia-kvality/",
        "format": "html",
        "sha256": "3cf7db678b5b031a3d93cb25d492b255f752d61e3e68430100c493f0d60f25f8",
        "raw_sha256": "d23c2e4fd42971330ad3d6b2d33c999c20ad313ea62ab50933c2dca210d5a1e7",
        "bytes": 29198,
    },
    "doctoral-opps": {
        "url": "https://zona.fmph.uniba.sk/doktorandi-a-doktorandske-studium/prilezitosti-a-podujatia/",
        "format": "html",
        "sha256": "38bf39f57bddb58351ce070261cba2cbc92e80f1c0cf1fd836553a7bd82cccec",
        "raw_sha256": "84c2f7c29b05fe361cca411d8af9a364ddb1b51515a883872243d2f332d07f71",
        "bytes": 28265,
    },
    "doctoral": {
        "url": "https://zona.fmph.uniba.sk/doktorandi-a-doktorandske-studium/",
        "format": "html",
        "sha256": "8e7be859cbb8cf1013047c84b7839c1e4659277e4c84ea134cdcaa755624f9f9",
        "raw_sha256": "84959306b5205d817e2a1b4e8857b8c7df656c3c5ea20697e7ead5422ba91b62",
        "bytes": 42256,
    },
    "entry": {
        "url": "https://zona.fmph.uniba.sk/detail-novinky/back_to_page/fmfi-uk-zona/article/vyzva-na-podavanie-ziadosti-o-jednorazove-mimoriadne-stipendium-call-for-applications-for-ex-9/",
        "format": "html",
        "sha256": "f5971c6124e366fd257bb854680e4a24fb1f6ea822543214b105b99b3be18984",
        "raw_sha256": "332cee2a7d07620db3a62d2b158c7bcbad4ed3765ed44edcf0e88013b1f3b420",
        "bytes": 24947,
    },
    "faculty-rules": {
        "url": "https://zona.fmph.uniba.sk/sluzby-a-administrativa/fakultne-predpisy/",
        "format": "html",
        "sha256": "f03cdeb55d84c3af80258f6dc76ad17e4e19b1bde72ccacdcf61c58ded0a10dd",
        "raw_sha256": "2b41e46100cdb960225593692eec56f02f4f40e62382ef338a1c8cb004cb44bf",
        "bytes": 70114,
    },
    "graduate": {
        "url": "https://zona.fmph.uniba.sk/studenti-a-studium/mobility/erasmus-absolventska-staz/",
        "format": "html",
        "sha256": "3ec11590aac1963ae3d1eab0ef47881f4e27a33082103ff442510cf1c7f35a41",
        "raw_sha256": "224e939767cf0db6628966eaccd220d7e31168b11f03596df4844c3486650f4b",
        "bytes": 24627,
    },
    "grant-index-1": {
        "url": "https://zona.fmph.uniba.sk/pre-zamestnancov/grantove-vyzvy/browse/1/",
        "format": "html",
        "sha256": "19e81aa91e8963ecf46a544f2a4dbf54db2c7f46ad5ab374946900771ab15e2e",
        "raw_sha256": "6f8f5ccddc75d13b5cdff4aa74da944d3ee8cd7051f4d6e92e4666de99d1379c",
        "bytes": 38975,
    },
    "grant-index-10": {
        "url": "https://zona.fmph.uniba.sk/pre-zamestnancov/grantove-vyzvy/browse/10/",
        "format": "html",
        "sha256": "68523c4c74c062e933e36e8af03680a141883145679d61cc746cef0ffe7e10d4",
        "raw_sha256": "23fb7155604ac2795629325f528c3256eaaa49a559ac52cfccb26a72fd7d6736",
        "bytes": 35382,
    },
    "grant-index-2": {
        "url": "https://zona.fmph.uniba.sk/pre-zamestnancov/grantove-vyzvy/browse/2/",
        "format": "html",
        "sha256": "99bcf6a3af74b9196b7454345af548f297a16608ffaba737b955095c4e27ae13",
        "raw_sha256": "6dc2310202cfcc136149e4347687211c1d982ac4559d8664f22ebbe2424451e7",
        "bytes": 38969,
    },
    "grant-index-3": {
        "url": "https://zona.fmph.uniba.sk/pre-zamestnancov/grantove-vyzvy/browse/3/",
        "format": "html",
        "sha256": "f85a70c7eea563bed580137302dd619401780526ee5350ffd03f30a8a2e4257a",
        "raw_sha256": "e9ac6c62fa564efd10db0a7365d04a0447ec463a03387a03abe67ef7e4e8a75f",
        "bytes": 39127,
    },
    "grant-index-4": {
        "url": "https://zona.fmph.uniba.sk/pre-zamestnancov/grantove-vyzvy/browse/4/",
        "format": "html",
        "sha256": "93276dc0f91856abc4cab39e5428c40d6cf7f765d89c1cb7da0cface4c71cf8f",
        "raw_sha256": "74dbcf625a6759ef01563a80af968df721a58382c8afd174de4a7513c9cbde3b",
        "bytes": 38988,
    },
    "grant-index-5": {
        "url": "https://zona.fmph.uniba.sk/pre-zamestnancov/grantove-vyzvy/browse/5/",
        "format": "html",
        "sha256": "4dd1ce4df20373576025ba35b50cbd4153966104e2a6872767c295f60e99eed5",
        "raw_sha256": "e01d2655622b8745481d463849dcd6f662982efdfac617f13431731cef7b18a3",
        "bytes": 39560,
    },
    "grant-index-6": {
        "url": "https://zona.fmph.uniba.sk/pre-zamestnancov/grantove-vyzvy/browse/6/",
        "format": "html",
        "sha256": "eac6fd405346dcd7c7b626ca933e71fe507c3ef826d6769595c82b0b6c4bd012",
        "raw_sha256": "722b47713198b2c9553803ebdfecacf334a00eb777e8f5cb92c51dc3eae89ccb",
        "bytes": 39529,
    },
    "grant-index-7": {
        "url": "https://zona.fmph.uniba.sk/pre-zamestnancov/grantove-vyzvy/browse/7/",
        "format": "html",
        "sha256": "4f49fe5b30862ff1dab9bd95936c0279695f86ee4da526205c3f4a1d65a48f8a",
        "raw_sha256": "9c99ff355a9f2a60f74d06e371763be38e3576d2c7893ad586c2f343f5362c43",
        "bytes": 41045,
    },
    "grant-index-8": {
        "url": "https://zona.fmph.uniba.sk/pre-zamestnancov/grantove-vyzvy/browse/8/",
        "format": "html",
        "sha256": "2d18961feb32fa42897415f74b532806bc0e70dd895d124930e10f09d4d29c36",
        "raw_sha256": "df63caf8e8561d25f3d09bcc2c4d3becb459a62c969c4a9d7a1bc53c1e9580c1",
        "bytes": 39774,
    },
    "grant-index-9": {
        "url": "https://zona.fmph.uniba.sk/pre-zamestnancov/grantove-vyzvy/browse/9/",
        "format": "html",
        "sha256": "fba06f712313f4d65b2ec71931d112d8a689ffc457547ae2b5b9b20ca39be6ea",
        "raw_sha256": "dc0ad57c039ae6e68644e8efa28765e65184ab41f738c7c6750f481a404ab092",
        "bytes": 38840,
    },
    "grants": {
        "url": "https://zona.fmph.uniba.sk/pre-zamestnancov/grantove-vyzvy/",
        "format": "html",
        "sha256": "900983d50a48a566d3563114b78173486e977a4be9122ea3c21345227848967f",
        "raw_sha256": "ce3e4766cd2518e21fad931e0dce08ca286742f2a34b27c4976a3be637508ee9",
        "bytes": 38533,
    },
    "incoming": {
        "url": "https://zona.fmph.uniba.sk/studenti-a-studium/mobility/ine-prichadzajuce-mobility/",
        "format": "html",
        "sha256": "681d7ee13d032f607c1edcf12f1ec983b725f885818da96c3a4158f5a7f1bda0",
        "raw_sha256": "3a84677274555421a2ccc4a95c153bec049de971be87cb8cc1ef08c10c9c84f0",
        "bytes": 20014,
    },
    "job": {
        "url": "https://zona.fmph.uniba.sk/detail-novinky/back_to_page/brigady-a-podujatia/article/lektori-it-a-stem-vzdelavania/",
        "format": "html",
        "sha256": "fa452c3b50d2526833e9c437b26c1abc7f386a32f45e3455e52e547671296d72",
        "raw_sha256": "401f6d8c26aaa285f1ba1aeb77a3a845b9f9a8b15bf22c8e06a5ee1a3803f3aa",
        "bytes": 23141,
    },
    "kega": {
        "url": "https://zona.fmph.uniba.sk/detail-novinky/back_to_page/grantove-vyzvy/article/kega-2026/",
        "format": "html",
        "sha256": "f88cc5b6cf0d4e0307a8bd4662cab9fdda7301b54a27afe884dec446490ab284",
        "raw_sha256": "a9978d7f2daa05c8c9de33297c634ece6ae01d7e3082f03119cdb6b52912896c",
        "bytes": 22852,
    },
    "mobility": {
        "url": "https://zona.fmph.uniba.sk/studenti-a-studium/mobility/erasmus-studium-sms/",
        "format": "html",
        "sha256": "5580e12dc4094df68dcd798f9cc3ee92d9b9aa05cba45c2f14fd652793e363cc",
        "raw_sha256": "0249a4c9251c697b46b3842c3910192f448f57e73daff8b50e322895a8090e71",
        "bytes": 26954,
    },
    "nsp": {
        "url": "https://zona.fmph.uniba.sk/detail-novinky/back_to_page/brigady-a-podujatia/article/narodny-stipendijny-program-vyzva-3/",
        "format": "html",
        "sha256": "7268ae434c29746089ef1d8a6f2a3cea2eba059f7da8dca9c67ddf2d2b194747",
        "raw_sha256": "10be6d271c8dbd95cdce8ba58c74d52f02d9720a26d6e8741fb93fe8ca0503ae",
        "bytes": 26704,
    },
    "scholarship-code": {
        "url": "https://zona.fmph.uniba.sk/fileadmin/fmfi/fakulta/legislativa/Stipendijny_poriadok_FMFI_UK_uplne_znenie_jun2025.pdf",
        "format": "pdf",
        "sha256": "d7031da462cb77603d9c0bc8b334f0474fd9dc6543d602240894dec9fce028fa",
        "raw_sha256": "d7031da462cb77603d9c0bc8b334f0474fd9dc6543d602240894dec9fce028fa",
        "bytes": 418552,
    },
    "scholarships": {
        "url": "https://zona.fmph.uniba.sk/studenti-a-studium/stipendia/",
        "format": "html",
        "sha256": "d75fca6d89c614e8462ffda74ba04436eb5f7c08551762d51fa3ec10b8ddf7a1",
        "raw_sha256": "5459e1c744f5173301892b25b259dda22c2ed2ca09fbc56ae7ed0adb052c7c45",
        "bytes": 47808,
    },
    "sitemap": {
        "url": "https://zona.fmph.uniba.sk/mapa-stranky/",
        "format": "html",
        "sha256": "85e01a2cbacda57622e301bfab72c61b7fe8b971b34f670e9f5631ebfbf50119",
        "raw_sha256": "8c7b24d3d02294c6ddc8a7e03d7ce2fe54d9244c61be9d4e60ba1210bc04fe3b",
        "bytes": 68250,
    },
    "sms-selection": {
        "url": "https://zona.fmph.uniba.sk/studenti-a-studium/mobility/erasmus-studium-sms/kriteria-vyberoveho-konania/",
        "format": "html",
        "sha256": "ce85f2bbb94cca7de5cd3bdd8f3460d0864d17fb0c5c9f997525eeed2305db2f",
        "raw_sha256": "cd8891ce129b5f232c1903d243be01401b44492e28b166092c8d160680cbb33e",
        "bytes": 24314,
    },
    "smt": {
        "url": "https://zona.fmph.uniba.sk/studenti-a-studium/mobility/erasmus-staz-smt/",
        "format": "html",
        "sha256": "eb6c47acebbf44d5ba413f14039c1596c3ae14d2f06758956691e356163b2794",
        "raw_sha256": "4f95c0b4b0c3b00d27a50ccf26dfc119b8d8cd1d916357028e928c00aa074e7f",
        "bytes": 22783,
    },
    "staff": {
        "url": "https://zona.fmph.uniba.sk/studenti-a-studium/mobility/erasmus-ucitelska-mobilita-sta/",
        "format": "html",
        "sha256": "5695df16ae9fbab2901477dda11d2f5a72405c726780c3bdeab2c9f46695937b",
        "raw_sha256": "f9300fa6f9db10a833bb55e3209fcd10523dbc9a1ad5a3560adb1335df916680",
        "bytes": 20130,
    },
    "student-opps": {
        "url": "https://zona.fmph.uniba.sk/studenti-a-studium/prilezitosti-a-podujatia/",
        "format": "html",
        "sha256": "870fc40cc4fe5d49f4a3e65f4fe41ac500d8d647e1955a485f74b7a8fa1a51ec",
        "raw_sha256": "f224e86ef14f001ad7ed150fa51ee8b7b05fccf85067dee9b435feee5e7ade91",
        "bytes": 30215,
    },
    "students": {
        "url": "https://zona.fmph.uniba.sk/studenti-a-studium/",
        "format": "html",
        "sha256": "16df7fbd3d3748e91d4b662464940f648bc6b913f4423eb95f853d029bc67ad6",
        "raw_sha256": "d20b08ff3f765dd9b14734eb7aeba6ce682f713f8c45adcfbf7ea02a4d22d439",
        "bytes": 41695,
    },
    "study-fees": {
        "url": "https://zona.fmph.uniba.sk/studenti-a-studium/skolne-a-poplatky/",
        "format": "html",
        "sha256": "eeee06599722210c8a5012df94bc917b4ce99cff445b0f8b37a98cad4a09daae",
        "raw_sha256": "8a2373a627cbda9ac7351b84e6ded970d4ea6e88dfaeb82084c35d5360d93b47",
        "bytes": 30252,
    },
    "study-office": {
        "url": "https://zona.fmph.uniba.sk/studenti-a-studium/studijne-oddelenie/",
        "format": "html",
        "sha256": "e4aae003c0884258c10be1e5e8257b8d9fba6287f49587d8cdb87bfe49f80ed8",
        "raw_sha256": "b8c16614fdfebb6edf9a89df7cea1295f5fdb5935835cbff4ab0d0632dcc071d",
        "bytes": 37498,
    },
    "talk": {
        "url": "https://zona.fmph.uniba.sk/detail-novinky/back_to_page/brigady-a-podujatia/article/eset-science-talks-serge-haroche-o-vede-odvahe-a-zodpovednosti-objavovat-nepoznane/",
        "format": "html",
        "sha256": "3334c0c339c294ec331a3ac44bdd2b755468fd321d1a65ab9a84b40b00b140a7",
        "raw_sha256": "69d5024fe474c354aa70f27c5ebdf23adf1b07b9267a729ee9ce9640d08ac946",
        "bytes": 23316,
    },
    "tuition-2026": {
        "url": "https://zona.fmph.uniba.sk/fileadmin/ruk/legislativa/2025/VP_2025_34_Priloha_c._8.pdf",
        "format": "pdf",
        "sha256": "dc6b6445e0ee094ba50ba4fe9af2a632d0b9e4009942b154489e4c0c39b0e017",
        "raw_sha256": "dc6b6445e0ee094ba50ba4fe9af2a632d0b9e4009942b154489e4c0c39b0e017",
        "bytes": 264682,
    },
    "tuition-amendment": {
        "url": "https://zona.fmph.uniba.sk/fileadmin/ruk/legislativa/2024/VP_2024_36.pdf",
        "format": "pdf",
        "sha256": "0153b505d5f5cee1d37fede42add33b552c3365e9c5a8674d58a56085f7d37f5",
        "raw_sha256": "0153b505d5f5cee1d37fede42add33b552c3365e9c5a8674d58a56085f7d37f5",
        "bytes": 83436,
    },
    "tuition-code": {
        "url": "https://zona.fmph.uniba.sk/fileadmin/ruk/legislativa/2024/VP_2024_26.pdf",
        "format": "pdf",
        "sha256": "e83638f676312daf2b2dbd9d67f3c880a0153f169c805e22e1f725c6bab44ca0",
        "raw_sha256": "e83638f676312daf2b2dbd9d67f3c880a0153f169c805e22e1f725c6bab44ca0",
        "bytes": 218269,
    },
    "vega": {
        "url": "https://zona.fmph.uniba.sk/detail-novinky/back_to_page/grantove-vyzvy/article/vega-2026/",
        "format": "html",
        "sha256": "6778e211ee323e24986858995b4d9df8dfcf7d3486a50cacc02ed7132c47b6d4",
        "raw_sha256": "b34cb63b529a49267460b5e167e06e5d9f5e2263a84212cfe821cf1a571dd43f",
        "bytes": 22591,
    },
    "zona-robots": {
        "url": "https://zona.fmph.uniba.sk/robots.txt",
        "format": "text",
        "sha256": "bda33a2ef8297cab3e9c5fd2977a155cfccd1d552ea1f978b7fe330d31638c3b",
        "raw_sha256": "bda33a2ef8297cab3e9c5fd2977a155cfccd1d552ea1f978b7fe330d31638c3b",
        "bytes": 16,
    },
}


PROFILES = [
    {
        "key": "social",
        "title": "Priznávanie sociálneho štipendia",
        "url": "https://zona.fmph.uniba.sk/fileadmin/fmfi/fakulta/legislativa/Stipendijny_poriadok_FMFI_UK_uplne_znenie_jun2025.pdf#page=4",
        "category": "scholarships",
        "hosts": ["SK"],
        "kind": "programme-overview",
        "deadline": None,
        "summary": "Means-tested monthly support for full-time FMFI bachelor/master "
        "students with permanent Slovak residence or asylum, subsidiary "
        "protection or temporary refuge; not a nationality rule. "
        "Prior-degree, repeated-year and duration exclusions apply, with "
        "specific-needs exceptions. Apply during the academic year after "
        "enrolment; personal amount is unknown.",
        "proof": "Current June2025 faculty scholarship code; formal article "
        "mechanism, not an open application round. Exact published "
        "heading: Priznávanie sociálneho štipendia.",
    },
    {
        "key": "field",
        "title": "Priznávanie odborového štipendia",
        "url": "https://zona.fmph.uniba.sk/fileadmin/fmfi/fakulta/legislativa/Stipendijny_poriadok_FMFI_UK_uplne_znenie_jun2025.pdf#page=6",
        "category": "scholarships",
        "hosts": ["SK"],
        "kind": "programme-overview",
        "deadline": None,
        "summary": "Conditional support for full-time FMFI students in annually "
        "ministry-designated fields; funded fields, allocation and "
        "personal amounts are unverified. Usually automatic; "
        "cohort-specific ranking, prior-year/GPA and duration conditions "
        "apply. First-master entrants with an external bachelor degree "
        "must request it by the last enrolment day. A visible "
        "incompatible-award clause conflicts with a footnote saying it "
        "was deleted; eligibility needs clarification.",
        "proof": "Current June2025 faculty scholarship code; formal article "
        "mechanism, not an open application round. Exact published "
        "heading: Priznávanie odborového štipendia.",
    },
    {
        "key": "merit",
        "title": "Priznávanie prospechového štipendia",
        "url": "https://zona.fmph.uniba.sk/fileadmin/fmfi/fakulta/legislativa/Stipendijny_poriadok_FMFI_UK_uplne_znenie_jun2025.pdf#page=8",
        "category": "scholarships",
        "hosts": ["SK"],
        "kind": "programme-overview",
        "deadline": None,
        "summary": "Full-time FMFI merit award: GPA up to 1.30, top 10%, all marks "
        "A–E and no repeated subject; generally 54 ECTS. The 27-ECTS "
        "first-winter award is only for three-year bachelor programmes; "
        "four-year bachelors start in year two. First-master exceptions "
        "apply; annual amount unknown. Usually automatic; October 15 is "
        "only an external-bachelor first-master request exception, whose "
        "clause wrongly names the field scholarship. November 15 is "
        "award timing, not an application deadline.",
        "proof": "Current June2025 faculty scholarship code; formal article "
        "mechanism, not an open application round. Exact published "
        "heading: Priznávanie prospechového štipendia.",
    },
    {
        "key": "achievement",
        "title": "Priznávanie mimoriadneho štipendia",
        "url": "https://zona.fmph.uniba.sk/fileadmin/fmfi/fakulta/legislativa/Stipendijny_poriadok_FMFI_UK_uplne_znenie_jun2025.pdf#page=11",
        "category": "scholarships",
        "hosts": ["SK"],
        "kind": "programme-overview",
        "deadline": None,
        "summary": "Discretionary dean award for exceptional study, research, art "
        "or sport achievements by FMFI students at all levels/forms. "
        "Granted without application, once for the same result or "
        "competition; no entitlement or verified personal amount. This "
        "state-funded achievement mechanism is distinct from the current "
        "application-based extraordinary scholarship call.",
        "proof": "Current June2025 faculty scholarship code; formal article "
        "mechanism, not an open application round. Exact published "
        "heading: Priznávanie mimoriadneho štipendia.",
    },
    {
        "key": "pregnancy",
        "title": "Priznávanie tehotenského štipendia",
        "url": "https://zona.fmph.uniba.sk/fileadmin/fmfi/fakulta/legislativa/Stipendijny_poriadok_FMFI_UK_uplne_znenie_jun2025.pdf#page=13",
        "category": "scholarships",
        "hosts": ["SK"],
        "kind": "programme-overview",
        "deadline": None,
        "summary": "Pregnant students with permanent Slovak residence and no "
        "entitlement to statutory tehotenské pregnancy benefit may "
        "receive support starting 27 weeks BEFORE expected delivery, "
        "including qualifying pregnancy-related study interruption. "
        "Residence is not citizenship. The rector/education-social "
        "office administers it; statutory monthly euro amount is not "
        "verified.",
        "proof": "Current June2025 faculty scholarship code; formal article "
        "mechanism, not an open application round. Exact published "
        "heading: Priznávanie tehotenského štipendia.",
    },
    {
        "key": "own-oneoff",
        "title": "Jednorazové štipendium",
        "url": "https://zona.fmph.uniba.sk/fileadmin/fmfi/fakulta/legislativa/Stipendijny_poriadok_FMFI_UK_uplne_znenie_jun2025.pdf#page=13",
        "category": "scholarships",
        "hosts": ["SK"],
        "kind": "programme-overview",
        "deadline": None,
        "summary": "Own-fund one-off support for exceptional work, results, civic "
        "service or faculty representation by FMFI students of all "
        "levels/forms and graduates within 90 days of completion. "
        "Discretionary dean nomination, without guaranteed entitlement "
        "or verified amount. Invitations, teaching assistance and "
        "recognition awards are pathways/examples, not separate "
        "scholarship calls.",
        "proof": "Current June2025 faculty scholarship code; formal article "
        "mechanism, not an open application round. Exact published "
        "heading: Jednorazové štipendium.",
    },
    {
        "key": "social-support",
        "title": "Sociálna podpora",
        "url": "https://zona.fmph.uniba.sk/fileadmin/fmfi/fakulta/legislativa/Stipendijny_poriadok_FMFI_UK_uplne_znenie_jun2025.pdf#page=15",
        "category": "scholarships",
        "hosts": ["SK"],
        "kind": "programme-overview",
        "deadline": None,
        "summary": "Own-fund social support for FMFI students at all levels/forms "
        "and graduates within 90 days: unusually adverse social "
        "circumstances, or severe officially recognised disability AND "
        "official registration as a student with specific needs. Written "
        "application through the study office/dean; support can be "
        "one-off or regular but is discretionary, with no guaranteed "
        "entitlement or verified personal amount.",
        "proof": "Current June2025 faculty scholarship code; formal article "
        "mechanism, not an open application round. Exact published "
        "heading: Sociálna podpora.",
    },
    {
        "key": "doctoral",
        "title": "Poskytovanie doktorandského štipendia",
        "url": "https://zona.fmph.uniba.sk/fileadmin/fmfi/fakulta/legislativa/Stipendijny_poriadok_FMFI_UK_uplne_znenie_jun2025.pdf#page=18",
        "category": "scholarships",
        "hosts": ["SK"],
        "kind": "programme-overview",
        "deadline": None,
        "summary": "Monthly support for full-time FMFI doctoral students within "
        "normal study duration and without a prior doctorate. Salary "
        "grade 6 before the doctoral exam and grade 7 afterward are "
        "tariff references, not verified euro amounts. Payment ends on "
        "defence, completion, overrun or the 48 th stipend; study "
        "interruption is unpaid. Conditional top-ups are not guaranteed "
        "or separate offers.",
        "proof": "Current June2025 faculty scholarship code; formal article "
        "mechanism, not an open application round. Exact published "
        "heading: Poskytovanie doktorandského štipendia.",
    },
    {
        "key": "extraordinary-2026",
        "title": "Výzva na podávanie žiadostí o jednorazové mimoriadne štipendium / "
        "Call for applications for extraordinary scholarship",
        "url": "https://zona.fmph.uniba.sk/detail-novinky/back_to_page/fmfi-uk-zona/article/vyzva-na-podavanie-ziadosti-o-jednorazove-mimoriadne-stipendium-call-for-applications-for-ex-9/",
        "category": "scholarships",
        "hosts": [],
        "kind": "opportunity",
        "deadline": "2026-10-18",
        "summary": "Actual 2026/27 FMFI bachelor/master call for study/research "
        "achievements, skills, practice and faculty representation under "
        "Article 12. EUR 10,000 is the presumed TOTAL CALL budget, not "
        "an individual award. Funded activity locations are unspecified. "
        "Closing October 18, 2026; the own calendar says 23:59 without a "
        "timezone, so the deadline remains date-only. Apply via the "
        "linked form; no future round is inferred.",
        "proof": "Reviewed own FMFI literal published heading and substantive "
        "notice.",
    },
    {
        "key": "nsp-october-2026",
        "title": "Národný štipendijný program - výzva",
        "url": "https://zona.fmph.uniba.sk/detail-novinky/back_to_page/brigady-a-podujatia/article/narodny-stipendijny-program-vyzva-3/",
        "category": "scholarships",
        "hosts": [],
        "kind": "opportunity",
        "deadline": "2026-10-31",
        "summary": "SAIA mobility call: outgoing Slovak-HEI students/PhD (including "
        "external research affiliations), and postdocs at Slovak "
        "HEIs/research organisations, need permanent Slovak residence. "
        "Incoming foreign students/PhD/teachers/researchers/artists may "
        "have any citizenship. Living costs and possible travel grant; "
        "amounts/durations/full external rules unverified. October 31, "
        "2026 at 16:00 has no zone. Incoming original acceptance letter "
        "required; not every step online. April 30, 2027 only announced "
        "next round.",
        "proof": "Reviewed own FMFI literal published heading and substantive "
        "notice.",
    },
    {
        "key": "sms-2025",
        "title": "ERASMUS+ štúdium (SMS)",
        "url": "https://zona.fmph.uniba.sk/studenti-a-studium/mobility/erasmus-studium-sms/",
        "category": "scholarships",
        "hosts": [],
        "kind": "opportunity",
        "deadline": "2025-02-15",
        "summary": "Published 2025/26 FMFI outgoing study offer, closed February "
        "15, 2025; not a current 2026/27 call. Edition rates EUR 674/606 "
        "per month by country group, maximum 4 funded months; partial or "
        "zero-grant mobility is possible. Partner agreement, required "
        "credits, academic ranking and language selection apply; C 1 "
        "documentation can waive the English test. Current central terms "
        "are unavailable; destination countries are not established.",
        "proof": "Reviewed own FMFI literal published heading and substantive "
        "notice.",
    },
    {
        "key": "smt",
        "title": "ERASMUS+ stáž (SMT)",
        "url": "https://zona.fmph.uniba.sk/studenti-a-studium/mobility/erasmus-staz-smt/",
        "category": "internships",
        "hosts": [],
        "kind": "programme-overview",
        "deadline": None,
        "summary": "FMFI placement overview for all study levels: at least 2 "
        "months, maximum 12 per cycle; eligible organisations exclude EU "
        "institutions and EU-project managers. Funding may be partial or "
        "zero; host income can coexist. The page says year-round "
        "applications but also prior-spring selection, an unresolved "
        "timing conflict. Its 2023 rates are outdated and current "
        "central terms/rates unavailable; no unrestricted current "
        "enrolment or destination countries are inferred.",
        "proof": "Reviewed own FMFI literal published heading and substantive "
        "notice.",
    },
    {
        "key": "stem-job",
        "title": "Lektori IT a STEM vzdelávania",
        "url": "https://zona.fmph.uniba.sk/detail-novinky/back_to_page/brigady-a-podujatia/article/lektori-it-a-stem-vzdelavania/",
        "category": "jobs",
        "hosts": ["SK"],
        "kind": "opportunity",
        "deadline": None,
        "summary": "FIRST Global Slovakia/RoboSkillz Academy seeks students, "
        "ideally IT/electrical/mechatronics/teaching, for mentoring and "
        "microcontroller-based STEM/IT workshops. Teach in the Slovak "
        "region where studying/living; flexible part-time October "
        "2026–May 2027. Pay FROM EUR 25/hour, not a guaranteed fixed "
        "rate. No application deadline is published in the own FMFI "
        "notice; external job-platform terms/details are not used.",
        "proof": "Reviewed own FMFI literal published heading and substantive "
        "notice.",
    },
    {
        "key": "science-talk",
        "title": "ESET Science Talks - Serge Haroche: O vede, odvahe a "
        "zodpovednosti objavovať nepoznané",
        "url": "https://zona.fmph.uniba.sk/detail-novinky/back_to_page/brigady-a-podujatia/article/eset-science-talks-serge-haroche-o-vede-odvahe-a-zodpovednosti-objavovat-nepoznane/",
        "category": "training",
        "hosts": ["SK"],
        "kind": "opportunity",
        "deadline": None,
        "summary": "Free public educational discussion on quantum science, "
        "discovery and critical thinking with Serge Haroche, moderated "
        "by Daniel Stach; Nadácia ESET organises it at Stará tržnica, "
        "Bratislava. Registration is required via the linked ticket "
        "page. October 16, 2026 at 18:00 is the EVENT wall-clock time, "
        "not a registration deadline; timezone and closing are "
        "unspecified.",
        "proof": "Reviewed own FMFI literal published heading and substantive "
        "notice.",
    },
    {
        "key": "tuition-reduction",
        "title": "Zníženie, resp. odpustenie školného",
        "url": "https://zona.fmph.uniba.sk/studenti-a-studium/skolne-a-poplatky/",
        "category": "grants",
        "hosts": ["SK"],
        "kind": "programme-overview",
        "deadline": None,
        "summary": "LIMITED FMFI recommendations for OVER-STANDARD-DURATION tuition "
        "above 70% of annual tuition, not income or cash. "
        "Ground-specific 25–100% reductions depend on circumstances; "
        "representation, social-hardship and childcare tiers require GPA "
        "up to 2.00. Dean recommends, rector decides; recommendations "
        "are individual, not guaranteed or automatically added. Latest "
        "complete rules unavailable: amendment 10 label links amendment "
        "9. Current timing and statutory-entitlement details unverified.",
        "proof": "Reviewed own FMFI literal published heading and substantive "
        "notice.",
    },
]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(
            json.dumps(
                {
                    "source": SOURCE_ID,
                    "status": "fail",
                    "error": safe_error(error),
                    "message": ("Run could not establish a durable outcome"),
                }
            )
        )
        sys.exit(1)
