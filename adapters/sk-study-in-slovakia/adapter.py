"""Collect the complete, reviewed SAIA scholarship catalogue.

The two actual directional inventories remain distinct except for reviewed
programme aliases. Public ASP.NET navigation uses only observed controls.
SAIA's noncommercial terms require attribution and completed retrieval times.
"""

import argparse
import base64
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
import sys
import stat
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import contextlib
import http.cookiejar
import signal
from datetime import timedelta

SOURCE_ID = "sk-study-in-slovakia"
SOURCE_URL = "https://www.studyinslovakia.saia.sk/en/main/scholarships"
WEBSITE_URL = "https://www.studyinslovakia.saia.sk/"
LANGUAGE = "en"
PUBLISHER_COUNTRY = "SK"
PUBLISHER_TYPE = "nonprofit"
ATTRIBUTION = "SAIA, n. o. – www.saia.sk"
_GITHUB = "https://api.github.com/repos/YouthOpps/data-source"
_WORKFLOW_GITHUB = "https://api.github.com/repos/YouthOpps/data-pipeline"
_USER_AGENT = "YouthOpp/1.0 (+https://github.com/YouthOpps/data-pipeline)"
CATEGORIES = {"scholarships", "internships", "volunteering", "training", "jobs", "competitions", "grants", "fellowships", "other"}
FAMILY = "saia-publisher-pacing"
COLLECTION_TIMEOUT = 270 * 60
LIFECYCLE_TIMEOUT = 300 * 60
REQUEST_CEILING = 500
_ARTIFACT_NAME = "saia-pacing-state"
_STATE_SCHEMA = 1
_COOKIES = http.cookiejar.CookieJar()
_OPENER = None
_ROBOTS = {}
_RESTORED = False
_STATE_TRUSTED = False
_STATE_PATH = None
_STATE_LOCK = None
_COLLECTION_STARTED = None
_LIFECYCLE_STARTED = None
_REQUESTS = 0
_PUBLISHING = False
_COLLECTION_LOCK_FD = None
_APP_TOKEN = None
_APP_EXPIRY = 0
_APP_CONFIGURATION = None
INPUTS = {}

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

def numeric(value):
    try:
        return (
            type(value) in (int, float)
            and math.isfinite(value) and value >= 0
        )
    except OverflowError:
        return False

def request_bytes(url, method="GET", headers=None, payload=None,
                  publisher=False, byte_limit=30000000):
    """Bound response bytes; redirects are explicit and credential-safe."""
    headers = {"User-Agent": _USER_AGENT, **(headers or {})}
    current = url
    for redirect in range(6):
        if publisher:
            public_url(current)
            pace()
        request = urllib.request.Request(
            current, data=payload, headers=headers, method=method
        )
        try:
            response = _OPENER.open(request, timeout=min(60, lifecycle_remaining(collection=publisher)))
        except urllib.error.HTTPError as error:
            response = error
        with response:
            status = response.status
            response_headers = response.headers
            retry = response.headers.get("Retry-After")
            if publisher:
                record_retry_after(retry)
            if publisher and status in (401, 403, 429):
                block_publisher()
                raise AdapterError("Publisher refused access: HTTP " + str(status), "access", status)
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
            if status in (301, 302, 303) and method == "POST":
                method, payload = "GET", None
                headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
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
            response = _OPENER.open(request, timeout=min(60, lifecycle_remaining()))
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
            "schema", "repository", "run_id", "run_attempt", "source", "head_sha", "state",
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
        attempt_metadata = workflow_api(f"/actions/runs/{run_id}/attempts/{attempt}")
        completed_identity(attempt_metadata, identity)
        if not re.fullmatch(r"[0-9a-f]{40}", envelope.get("head_sha", "")) or envelope["head_sha"] != attempt_metadata.get("head_sha"):
            raise ValueError("Artifact source-head mismatch")
        return validate_budget(envelope["state"])
    except Exception:
        raise AdapterError((
            "Invalid or unavailable family pacing artifact"
        ), (
            "access"
        )) from None

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
        token = os.environ.get("DATA_SOURCE_TOKEN")
        if not token:
            raise AdapterError("Publication credentials missing", "publish")
        return token
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
    result = {
        "source": SOURCE_ID,
        "name": "Study in Slovakia",
        "source_url": SOURCE_URL,
        "website_url": WEBSITE_URL,
        "language": LANGUAGE,
        "publisher_country": PUBLISHER_COUNTRY,
        "publisher_type": PUBLISHER_TYPE,
        "status": "fail" if error else "success",
        "last_attempt_at": attempt,
        "last_success_at": (
            previous.get("last_success_at") if error else attempt
        ),
        "last_checked_at": (
            previous.get("last_checked_at")
            if error
            else checked_at
            or max(
                (record["last_checked_at"] for record in records),
                default=attempt,
            )
        ),
        "record_count": len(records),
        "message": (
            "Collection failed; last-good data preserved"
            if error
            else (
                f"Collected {len(records)} validated Study in Slovakia "
                "funding programmes and placements"
            )
        ),
        "error": safe_error(error) if error else None,
    }
    if ATTRIBUTION:
        result["attribution"] = ATTRIBUTION
    if error:
        result["failure_stage"] = getattr(error, "stage", "parse")
    return result

# SAIA legacy files are the shared transport budget, not a new source bucket.
_PACING_KEYS = sorted(hashlib.sha256(key.encode()).hexdigest()
                      for key in ("saia.sk", "scholarships.sk"))
_CANONICAL_KEY = hashlib.sha256(b"scholarships.sk").hexdigest()
FAMILY_SOURCES = {SOURCE_ID}
_LEGACY_SOURCE = "sk-national-scholarship-programme"
_LEGACY_RUN = 37866043357
_LEGACY_HEAD = "08df07fee129bbe1c416e0f417df2c727ea423b3"
_LEGACY_END = "2026-10-09T00:41:52Z"


def lifecycle_remaining(collection=False):
    if _LIFECYCLE_STARTED is None:
        raise AdapterError("Lifecycle clock not initialized", "access")
    limit = COLLECTION_TIMEOUT if collection else LIFECYCLE_TIMEOUT
    remaining = limit - (time.monotonic() - _LIFECYCLE_STARTED)
    if _PHASE_END is not None:
        remaining = min(remaining, _PHASE_END - time.monotonic())
    if remaining <= 0:
        raise AdapterError("Finite lifecycle budget exhausted", "access")
    return remaining


def budget_file():
    global _STATE_PATH, _STATE_LOCK
    directory = os.path.join(tempfile.gettempdir(), FAMILY + "-" + str(os.getuid()))
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(directory)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise AdapterError("Unsafe shared SAIA pacing directory", "access")
    _STATE_PATH = os.path.join(directory, _CANONICAL_KEY)
    _STATE_LOCK = None


def validate_budget(state):
    """Legacy keys stay readable by #17; optional refusal metadata is inert."""
    allowed = {"starts", "interval", "until", "blocked", "observed_at"}
    if (not isinstance(state, dict) or not {"starts", "interval", "until"} <= set(state)
            or set(state) - allowed or not isinstance(state["starts"], list)
            or len(state["starts"]) > 1000
            or any(not numeric(t) for t in state["starts"])
            or state["starts"] != sorted(set(state["starts"]))
            or not numeric(state["interval"]) or state["interval"] < 6
            or not numeric(state["until"])
            or type(state.get("blocked", False)) is not bool
            or not numeric(state.get("observed_at", 0))
            or state.get("observed_at", 0) > time.time() + 1
            or any(t > time.time() + 1 for t in state["starts"])):
        raise AdapterError("Corrupt shared SAIA pacing history", "access")
    return state


def empty_budget(now):
    return {"starts": [], "interval": 6, "until": 0,
            "blocked": False, "observed_at": now}


@contextlib.contextmanager
def locked_budget():
    """Lock both actual legacy inodes in deterministic order; never replace them."""
    budget_file()
    opened = []
    try:
        for key in _PACING_KEYS:
            fd = os.open(os.path.join(os.path.dirname(_STATE_PATH), key),
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                os.close(fd)
                raise AdapterError("Unsafe shared pacing state inode", "access")
            file = os.fdopen(fd, "r+", encoding="utf-8")
            opened.append(file)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if lifecycle_remaining() < 1:
                        raise AdapterError("Shared pacing lock budget exhausted", "access")
                    time.sleep(min(0.25, lifecycle_remaining()))
        yield opened
    finally:
        for file in reversed(opened):
            file.close()


def load_budget(files):
    combined = empty_budget(time.time())
    for file in files:
        file.seek(0)
        raw = file.read(16385)
        if len(raw) > 16384:
            raise AdapterError("Oversized shared pacing state", "access")
        try:
            state = validate_budget(json.loads(raw)) if raw else empty_budget(time.time())
        except (ValueError, TypeError):
            raise AdapterError("Corrupt shared pacing JSON", "access") from None
        combined["starts"] = sorted(set(combined["starts"] + state["starts"]))
        combined["interval"] = max(combined["interval"], state["interval"])
        combined["until"] = max(combined["until"], state["until"])
        combined["blocked"] |= state.get("blocked", False)
        combined["observed_at"] = max(combined["observed_at"], state.get("observed_at", 0))
    return validate_budget(combined)


def save_budget(files, state):
    validate_budget(state)
    for file in files:
        file.seek(0)
        json.dump(state, file, separators=(",", ":"))
        file.truncate()
        file.flush()
        os.fsync(file.fileno())


def pace():
    global _REQUESTS
    if _REQUESTS >= REQUEST_CEILING:
        raise AdapterError("Publisher request ceiling exhausted", "access")
    with locked_budget() as files:
        state = load_budget(files)
        if state["blocked"]:
            raise AdapterError("Publisher refusal requires reviewed recovery", "access")
        now = time.time()
        starts = [t for t in state["starts"] if t > now - 60]
        target = max(now, state["until"],
                     starts[-1] + state["interval"] if starts else now,
                     starts[-10] + 60 if len(starts) >= 10 else now)
        delay = max(0, target - now)
        if delay + 60 > lifecycle_remaining(collection=True):
            raise AdapterError("Publisher embargo exceeds collection budget", "access")
        # Persist the validated union before any network request or waiting.
        save_budget(files, state)
        while delay > 0:
            time.sleep(min(delay, 30))
            delay = max(0, target - time.time())
            lifecycle_remaining(collection=True)
        now = time.time()
        if now + 0.001 < target:
            raise AdapterError("Publisher clock moved backwards", "access")
        state["starts"] = [t for t in starts if t > now - 60] + [now]
        state["observed_at"] = now
        save_budget(files, state)
        _REQUESTS += 1


def record_retry_after(value):
    if value is None:
        return
    with locked_budget() as files:
        state = load_budget(files)
        now = time.time()
        try:
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value.strip()):
                until = now + float(value)
            else:
                parsed = parsedate_to_datetime(value)
                if parsed.utcoffset() is None:
                    raise ValueError("Missing timezone")
                until = parsed.timestamp()
            if not numeric(until):
                raise ValueError("Invalid embargo")
            state["until"] = max(state["until"], until)
        except Exception:
            state["blocked"] = True
        state["observed_at"] = now
        save_budget(files, state)


def block_publisher():
    with locked_budget() as files:
        state = load_budget(files)
        state["blocked"] = True
        state["observed_at"] = time.time()
        save_budget(files, state)


def public_url(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port or parsed.fragment
            or parsed.hostname not in {"www.studyinslovakia.saia.sk", "www.saia.sk",
                                       "grants.saia.sk", "granty.saia.sk", "www.stipendia.sk"}):
        raise AdapterError("Unreviewed publisher URL", "access")
    if parsed.path.startswith(("/admin", "/Account", "/Login")):
        raise AdapterError("Private publisher route", "access")
    return value


def family_source(run):
    path = run.get("path", "")
    if path == ".github/workflows/fetch-" + SOURCE_ID + ".yml":
        return SOURCE_ID
    return None


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


def legacy_migration_budget():
    """Explicit one-time migration, not a claim that unknown old backoff is zero."""
    workflow = workflow_api("/actions/workflows/fetch-" + _LEGACY_SOURCE + ".yml")
    if workflow.get("state") != "disabled_manually":
        raise AdapterError("Legacy SAIA workflow must remain disabled", "access")
    result = workflow_api("/actions/workflows/fetch-" + _LEGACY_SOURCE
                          + ".yml/runs?per_page=100")
    runs = result.get("workflow_runs")
    if (not isinstance(runs, list) or result.get("total_count") != 3 or len(runs) != 3
            or any(run.get("status") != "completed" or run.get("run_attempt") != 1 for run in runs)):
        raise AdapterError("Legacy migration history changed or active", "access")
    latest = max(runs, key=lambda run: prior_timestamp(run.get("updated_at")))
    if (latest.get("id") != _LEGACY_RUN or latest.get("run_attempt") != 1
            or latest.get("head_sha") != _LEGACY_HEAD
            or latest.get("conclusion") != "failure"
            or latest.get("repository", {}).get("full_name") != "YouthOpps/data-pipeline"
            or latest.get("head_branch") != "main"):
        raise AdapterError("Pinned legacy migration evidence changed", "access")
    jobs = workflow_api(f"/actions/runs/{_LEGACY_RUN}/attempts/1/jobs?per_page=100")
    entries = jobs.get("jobs")
    if (not isinstance(entries, list) or len(entries) != jobs.get("total_count")
            or not entries or any(j.get("status") != "completed" for j in entries)
            or max(j.get("completed_at", "") for j in entries) != _LEGACY_END):
        raise AdapterError("Pinned legacy job completion changed", "access")
    end = datetime.fromisoformat(_LEGACY_END.replace("Z", "+00:00")).timestamp()
    if time.time() < end + 86400:
        raise AdapterError("Legacy publisher quiet period incomplete", "access")
    incoming = empty_budget(time.time())
    incoming["until"] = end + 86400
    # Prior arbitrary Retry-After was never exported by legacy17. This reviewed
    # migration acknowledges that limitation; subsequent18 always requires state.
    return incoming


def restore_family_artifact():
    global _RESTORED, _STATE_TRUSTED
    if _RESTORED:
        return
    legacy = workflow_api("/actions/workflows/fetch-" + _LEGACY_SOURCE + ".yml")
    legacy_runs = workflow_api("/actions/workflows/fetch-" + _LEGACY_SOURCE + ".yml/runs?per_page=100")
    if (legacy.get("state") != "disabled_manually"
            or legacy_runs.get("total_count") != 3
            or len(legacy_runs.get("workflow_runs", [])) != 3
            or any(run.get("status") != "completed" or run.get("run_attempt") != 1
                   for run in legacy_runs.get("workflow_runs", []))):
        raise AdapterError("Legacy source17 reactivation/activity requires reviewed migration", "access")
    if {run.get("id") for run in legacy_runs["workflow_runs"]} != {37866043357, 37865230903, 37865212036}:
        raise AdapterError("Pinned legacy run identities changed", "access")
    latest = latest_family_run()
    if latest is None:
        incoming = legacy_migration_budget()
    else:
        run_id, attempt, source = latest
        result = workflow_api(f"/actions/runs/{run_id}/artifacts?per_page=100")
        artifacts = result.get("artifacts")
        if (not isinstance(artifacts, list) or len(artifacts) > 100
                or result.get("total_count") != len(artifacts)):
            raise AdapterError("Incomplete latest pacing artifact inventory", "access")
        matches = [a for a in artifacts if a.get("name") == _ARTIFACT_NAME + "-" + str(attempt)]
        if len(matches) != 1 or matches[0].get("expired") is not False:
            raise AdapterError("Latest completed SAIA attempt lacks its state artifact", "access")
        artifact = matches[0]
        if (type(artifact.get("id")) is not int or artifact["id"] <= 0
                or artifact.get("workflow_run", {}).get("id") != run_id
                or not numeric(artifact.get("size_in_bytes"))
                or artifact["size_in_bytes"] > 32768):
            raise AdapterError("Invalid latest pacing artifact binding", "access")
        incoming = download_inert_artifact(latest, artifact["id"], artifact.get("digest"))
    with locked_budget() as files:
        local = load_budget(files)
        local["starts"] = sorted(set(local["starts"] + incoming["starts"]))
        local["interval"] = max(local["interval"], incoming["interval"])
        local["until"] = max(local["until"], incoming["until"], time.time() + 60)
        local["blocked"] |= incoming.get("blocked", False)
        local["observed_at"] = time.time()
        save_budget(files, local)
    _RESTORED, _STATE_TRUSTED = True, True


def export_family_artifact():
    path = os.environ.get("SAIA_PACING_ARTIFACT_PATH")
    if not path or not _STATE_TRUSTED:
        return
    lifecycle_remaining()
    run_id, attempt = current_run_identity()
    with locked_budget() as files:
        state = load_budget(files)
    envelope = {"schema": 1, "repository": "YouthOpps/data-pipeline",
                "run_id": run_id, "run_attempt": attempt, "source": SOURCE_ID,
                "head_sha": os.environ.get("GITHUB_SHA", ""), "state": state}
    if not re.fullmatch(r"[0-9a-f]{40}", envelope["head_sha"]):
        raise AdapterError("Source artifact head binding missing", "access")
    configured = os.environ.get("RUNNER_TEMP", "")
    if (not configured or not os.path.isabs(configured)
            or os.path.dirname(os.path.abspath(path)) != os.path.realpath(configured)):
        raise AdapterError("Unsafe pacing artifact path", "access")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
            raise AdapterError("Unsafe pacing artifact inode", "access")
        json.dump(envelope, file, separators=(",", ":"))
        file.flush()
        os.fsync(file.fileno())


def prepare_collection():
    global _STATE_TRUSTED, _COLLECTION_LOCK_FD, _COLLECTION_STARTED
    _COLLECTION_STARTED = time.time()
    budget_file()
    if _COLLECTION_LOCK_FD is None:
        path = os.path.join(os.path.dirname(_STATE_PATH), "collection.lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            os.close(fd)
            raise AdapterError("Unsafe collection lock", "access")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise AdapterError("Another local SAIA collector is active", "access") from None
        _COLLECTION_LOCK_FD = fd
    if os.environ.get("GITHUB_ACTIONS") == "true":
        restore_family_artifact()
    _STATE_TRUSTED = True


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
            raise AdapterError("Required publisher robots unavailable", "access", status)
    parser = _ROBOTS[origin]
    if parser is None:
        return
    if not parser.can_fetch(_USER_AGENT, url):
        block_publisher()
        raise AdapterError("Publisher robots excludes required route", "access")
    delay = parser.crawl_delay(_USER_AGENT) or 6
    rate = parser.request_rate(_USER_AGENT)
    if rate:
        if rate.requests <= 0:
            raise AdapterError("Invalid robots rate", "access")
        delay = max(delay, rate.seconds / rate.requests)
    with locked_budget() as files:
        state = load_budget(files)
        state["interval"] = max(state["interval"], delay, 6)
        save_budget(files, state)


def fetch_public(url, method="GET", payload=None, headers=None):
    check_robots(url)
    status, response_headers, body = request_bytes(
        public_url(url), method=method, payload=payload, headers=headers, publisher=True)
    if status != 200:
        raise AdapterError("Required publisher input returned HTTP " + str(status), "fetch", status)
    return body, utc_now(), response_headers

# Hard POSIX alarms bound reads, parsing, crypto, CAS and output as well as starts.
EXPORT_TIMEOUT = 120
_RUN_END = None
_PHASE_END = None
_PHASE_NAME = None

class PhaseExpired(BaseException):
    """A non-renewable whole-phase deadline, never an ordinary HTTP error."""

def check_deadline():
    if _PHASE_END is None or _RUN_END is None:
        raise AdapterError("Bounded execution context required", "access")
    if time.monotonic() >= min(_PHASE_END, _RUN_END):
        raise PhaseExpired()

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
    global _RUN_END, _COLLECTION_LOCK_FD, _LIFECYCLE_STARTED
    if _RUN_END is not None:
        raise AdapterError("Nested execution context refused", "access")
    previous = signal.getsignal(signal.SIGALRM)
    if signal.getitimer(signal.ITIMER_REAL) != (0.0, 0.0):
        raise AdapterError("Existing process alarm refused", "access")
    signal.signal(signal.SIGALRM, alarm_expired)
    _LIFECYCLE_STARTED = time.monotonic()
    _RUN_END = _LIFECYCLE_STARTED + LIFECYCLE_TIMEOUT
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
    if name == "prepublication":
        _PHASE_END = min(_PHASE_END, _LIFECYCLE_STARTED + COLLECTION_TIMEOUT)
    try:
        check_deadline()
        arm_alarm(_PHASE_END)
        yield
        check_deadline()
    finally:
        _PHASE_END = _PHASE_NAME = None
        arm_alarm(limit)

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
                    validate_prior_pair(candidate_records, candidate_metadata)
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
            with phase("publication", 900):
                publish_files(snapshot, desired)
        except PhaseExpired:
            failure = phase_error("publication")
        except Exception as error:
            failure = error
    if failure is not None:
        outcome = metadata(attempt, previous, old, failure)
        if publishing:
            try:
                with phase("failure", 600):
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
            try:
                return run_lifecycle(args.publish)
            except Exception:
                return 1
    except PhaseExpired:
        return 1

def prior_timestamp(value):
    if not isinstance(value, str):
        raise AdapterError("Prior source timestamp missing", "publish")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() != timedelta(0) or parsed.timestamp() > time.time() + 1:
            raise ValueError()
        return parsed.timestamp()
    except (ValueError, OverflowError):
        raise AdapterError("Invalid prior source UTC timestamp", "publish") from None


def validate_prior_pair(records, previous):
    """Only an entirely validated owned pair can be retained after a failure."""
    validate_records(records)
    identities = {"source": SOURCE_ID, "source_url": SOURCE_URL,
                  "website_url": WEBSITE_URL, "language": LANGUAGE,
                  "publisher_country": PUBLISHER_COUNTRY,
                  "publisher_type": PUBLISHER_TYPE, "attribution": ATTRIBUTION,
                  "name": "Study in Slovakia"}
    if (not isinstance(previous, dict)
            or any(previous.get(k) != v for k, v in identities.items())
            or type(previous.get("record_count")) is not int
            or previous["record_count"] != len(records)
            or previous.get("status") not in {"success", "fail"}
            or not isinstance(previous.get("message"), str)):
        raise AdapterError("Invalid owned prior source metadata", "publish")
    attempt = prior_timestamp(previous.get("last_attempt_at"))
    success = prior_timestamp(previous.get("last_success_at"))
    checked = prior_timestamp(previous.get("last_checked_at"))
    if not success <= min(attempt, checked):
        raise AdapterError("Prior source chronology mismatch", "publish")
    if previous["status"] == "success":
        if attempt != success or previous.get("error") is not None:
            raise AdapterError("Prior success outcome inconsistent", "publish")
    elif (checked > attempt or not isinstance(previous.get("error"), str) or not previous["error"]
          or not isinstance(previous.get("failure_stage"), str) or not previous["failure_stage"]):
        raise AdapterError("Prior failure outcome inconsistent", "publish")
    for record in records:
        parsed = urllib.parse.urlsplit(record["url"])
        if (parsed.scheme != "https" or parsed.hostname not in {"grants.saia.sk", "granty.saia.sk"}
                or parsed.path != "/Pages/ProgramDetail.aspx"
                or not re.fullmatch(r"Program=[0-9]+", parsed.query)
                or record["id"] != hashlib.sha256((SOURCE_ID + "|" + record["url"]).encode()).hexdigest()[:24]
                or record.get("language") not in {"en", "sk"}
                or record.get("summary_language") != "en"
                or record.get("publisher_country") != PUBLISHER_COUNTRY):
            raise AdapterError("Prior record identity/language mismatch", "publish")
        stamps = {key: prior_timestamp(record[key]) for key in
                  ("created_at", "updated_at", "first_seen_at", "last_seen_at", "last_checked_at")}
        if (not stamps["created_at"] <= stamps["updated_at"] <= stamps["last_seen_at"]
                or not stamps["first_seen_at"] <= stamps["last_seen_at"] <= stamps["last_checked_at"] == checked):
            raise AdapterError("Prior record chronology mismatch", "publish")
        copied = re.search(re.escape(ATTRIBUTION) + r"; copied ([0-9T:.Z+-]+)\.$", record["summary"])
        if not copied or prior_timestamp(copied[1]) > stamps["last_checked_at"]:
            raise AdapterError("Prior record source-copy provenance missing", "publish")


# Language refers to the displayed title, not the host or applicant nationality.
EN_LIST_URL = "https://grants.saia.sk/Pages/ProgramZoznam.aspx?Type=ALL"
SK_LIST_URL = "https://granty.saia.sk/Pages/ProgramZoznam.aspx?Lang=sk"
EN_FIELDS = (
    "Home country", "Host country", "Field of study/research", "Type of support",
    "Support category", "Target group (when applying for the scholarship)",
    "Duration", "Finance source", "Quota", "Offered support", "Deadline",
    "Submission of applications", "Application form and supporting documents",
    "Other relevant information", "Programme administrator", "Selection procedure", "Attachments",
)
SK_FIELDS = (
    "Domovská krajina", "Krajina pobytu", "Študijný/vedný odbor", "Typ podpory",
    "Kategória podpory", "Cieľová skupina (v čase realizácie)", "Trvanie",
    "Zdroj financovania", "Kvóta", "Poskytovaná podpora", "Uzávierka pre podávanie žiadostí",
    "Podávanie žiadosti", "Formulár žiadosti a podkladové materiály",
    "Ďalšie dôležité informácie", "Administrátor programu", "Priebeh výberu", "Prílohy",
)
ALIASES = {"567": "587", "576": "581", "577": "582", "578": "583",
           "579": "585", "661": "780", "762": "808", "675": "807",
           "663": "686", "664": "665", "672": "674", "792": "106"}
VISEGRAD_MENUS = {"842": ("702", "703", "704", "821"),
                  "843": ("223", "779", "443", "822", "823", "824"),
                  "844": ("827", "176", "826", "481", "825")}


def html_root(body, headers=None):
    charset = headers.get_content_charset() if headers is not None else None
    return PublisherHTML(body.decode(charset or "utf-8", "strict")).root


def detail_facts(body, lang, headers=None):
    root = html_root(body, headers)
    table = one([n for n in root.walk() if n.attrs.get("id") == "ctl00_CphContent_programDetail"],
                "programme detail table")
    title = one(nodes(root, "title"), "programme title").text()
    fields = {}
    for row in nodes(table, "tr"):
        cells = [c for c in row.children if isinstance(c, Node) and c.tag == "td"]
        if len(cells) != 2:
            continue
        labels = [n for n in cells[0].walk() if n.tag == "span"
                  and n.attrs.get("id", "").startswith("ctl00_CphContent_label_ProgramDetail_")]
        # Nested contacts are not programme dimensions; use only actual labels.
        if not labels:
            continue
        label = labels[0].text()
        if label in fields:
            raise AdapterError("Repeated programme dimension", "parse")
        fields[label] = cells[1].text()
    expected = EN_FIELDS if lang == "en" else SK_FIELDS
    if tuple(fields) != expected:
        raise AdapterError("Programme dimensions changed", "parse")
    return title, fields, root


def facts_hash(title, fields):
    return hashlib.sha256(json.dumps([title, fields], ensure_ascii=False,
                                   separators=(",", ":")).encode()).hexdigest()


def listing_page(body, url, headers=None):
    root = html_root(body, headers)
    table = one([n for n in root.walk() if n.tag == "table"
                 and n.attrs.get("id") == "ctl00_CphContent_gvProgramy"], "programme listing")
    entries, pager = [], []
    for link in nodes(table, "a"):
        href = link.attrs.get("href", "")
        match = re.fullmatch(r"ProgramDetail\.aspx\?Program=(\d+)", href)
        if match:
            entries.append({"id": match[1], "title": link.text(),
                            "url": urllib.parse.urljoin(url, href)})
        elif "Page$" in href:
            match = re.fullmatch(r"javascript:__doPostBack\('([^']+)','Page\$(\d+)'\)", href)
            if not match:
                raise AdapterError("Unreviewed public pager control", "parse")
            pager.append((match[1], int(match[2])))
    if not entries or len({x["id"] for x in entries}) != len(entries):
        raise AdapterError("Empty or duplicate catalogue page", "parse")
    return root, entries, pager


def public_form(root, url):
    form = one([n for n in root.walk() if n.tag == "form"
                and n.attrs.get("id") == "aspnetForm"], "public listing form")
    action = urllib.parse.urljoin(url, form.attrs.get("action", ""))
    parsed, origin = urllib.parse.urlsplit(action), urllib.parse.urlsplit(url)
    if parsed.hostname != origin.hostname or parsed.path != "/Pages/ProgramZoznam.aspx":
        raise AdapterError("Public listing form action changed", "access")
    fields = []
    for node in form.walk():
        name = node.attrs.get("name")
        if not name or "disabled" in node.attrs:
            continue
        if node.tag == "input":
            kind = node.attrs.get("type", "text").lower()
            if kind in ("button", "submit", "image", "reset", "file"):
                continue
            if kind in ("checkbox", "radio") and "checked" not in node.attrs:
                continue
            fields.append((name, node.attrs.get("value", "")))
        elif node.tag == "select":
            options = nodes(node, "option")
            selected = [x for x in options if "selected" in x.attrs] or options[:1]
            fields.extend((name, x.attrs.get("value", x.text())) for x in selected)
        elif node.tag == "textarea":
            fields.append((name, node.text()))
    names = [key for key, _ in fields]
    if "__VIEWSTATE" not in names or "__EVENTVALIDATION" not in names:
        raise AdapterError("Public navigation integrity fields missing", "parse")
    return form, action, fields


def navigate(root, url, replacements):
    _, action, fields = public_form(root, url)
    fields = [(key, value) for key, value in fields if key not in replacements]
    fields.extend(replacements.items())
    body, completed, headers = fetch_public(
        action, method="POST", payload=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": url})
    return action, body, completed, headers


def read_inventory(lang, initial=None):
    url = EN_LIST_URL if lang == "en" else SK_LIST_URL
    body, completed, headers = initial if initial is not None else fetch_public(url)
    root, first, pager = listing_page(body, url, headers)
    if lang == "en":
        if not any(n.attrs.get("href") == SK_LIST_URL for n in nodes(root, "a")):
            raise AdapterError("Actual Slovak directional link changed", "parse")
        select = one([n for n in root.walk() if n.tag == "select"
                      and n.attrs.get("id") == "ctl00_CphContent_filterDdlPocetPonuk"], "actual All selector")
        option = one([n for n in nodes(select, "option")
                      if n.attrs.get("value") == "999" and n.text() == "All"], "actual All option")
        button = one([n for n in root.walk() if n.attrs.get("id") == "ctl00_CphContent_btnFilter"],
                     "actual filter submission control")
        if not select.attrs.get("name") or not button.attrs.get("name"):
            raise AdapterError("Actual All control names missing", "parse")
        url, body, completed, headers = navigate(root, url, {
            select.attrs["name"]: option.attrs["value"],
            button.attrs["name"]: button.attrs.get("value", ""),
            "__EVENTTARGET": "", "__EVENTARGUMENT": ""})
        _, entries, pager = listing_page(body, url, headers)
        if pager or len(entries) >= 999:
            raise AdapterError("All view is not an authoritative finite inventory", "parse")
    else:
        entries, seen = [], set()
        for current in range(1, 101):
            root, batch, pager = listing_page(body, url, headers)
            if any(x["id"] in seen for x in batch):
                raise AdapterError("Repeated public catalogue members", "parse")
            seen.update(x["id"] for x in batch)
            entries.extend(batch)
            next_controls = [(target, number) for target, number in pager if number == current + 1]
            if not next_controls:
                if any(number >= current for _, number in pager):
                    raise AdapterError("Catalogue terminal not authoritative", "parse")
                break
            target, number = one(next_controls, "actual next-page control")
            url, body, completed, headers = navigate(root, url, {
                "__EVENTTARGET": target, "__EVENTARGUMENT": "Page$" + str(number)})
        else:
            raise AdapterError("Public catalogue exceeded reviewed finite bound", "parse")
    expected = EXPECTED_INVENTORIES[lang]
    if [(x["id"], x["title"]) for x in entries] != expected:
        raise AdapterError("Complete directional inventory changed; editorial review required", "parse")
    return entries

LEGAL_BASE = "https://www.saia.sk/"
LEGAL_EN = "https://www.saia.sk/en/main/about-us-main/legal-information-and-data-protection/"
LEGAL_SK = "https://www.saia.sk/sk/main/o-nas/pravne-informacie-a-ochrana-udajov/"
TERMS_PDF = "https://www.saia.sk/_user/documents/SAIA/pravne-info/VZP-SAIA-weby2018-SK.pdf"
TERMS_SHA256 = "79426e09e2fa247e7a3e7dcfee669ad298b9eace24576c0e198582a90a8bb826"
AMOUNTS_URL = "https://www.stipendia.sk/sk/main/podmienky-pre-predkladanie-ziadosti/uchadzaci-zo-slovenska/vyska-stipendia"
AMOUNTS_SHA256 = "774d505560b85f17718faa49142c4902399b2a3b33f705e23fc7c0f23aba16a9"


def verify_current_terms():
    for url in (LEGAL_EN, LEGAL_SK):
        body, _, headers = fetch_public(url)
        root = html_root(body, headers)
        base = one(nodes(root, "base"), "actual licence document base").attrs.get("href")
        if base != LEGAL_BASE:
            raise AdapterError("Publisher licence document base changed", "access")
        if not any(urllib.parse.urljoin(base, n.attrs.get("href", "")) == TERMS_PDF
                   for n in nodes(root, "a")):
            raise AdapterError("Applicable publisher licence link changed", "access")
    body, _, _ = fetch_public(TERMS_PDF)
    if hashlib.sha256(body).hexdigest() != TERMS_SHA256:
        raise AdapterError("Binding publisher reuse terms changed", "access")


def reconcile_own_study_pages():
    for url, guard in STUDY_CONTENT_GUARDS.items():
        body, _, headers = fetch_public(url)
        root = html_root(body, headers)
        content = one([n for n in root.walk() if n.attrs.get("id") == "contentcol"],
                      "own study programme content")
        if hashlib.sha256(content.text().encode()).hexdigest() != guard["content_sha256"]:
            raise AdapterError("Own study-level reconciliation changed", "parse")


def deadline_date(literal):
    """A supplied year is required. A local clock does not imply a timezone."""
    match = re.fullmatch(r"\s*(\d{1,2})\.(\d{1,2})\.(\d{4})(?:\s+\d{1,2}:\d{2})?\s*", literal)
    if not match:
        return None
    day, month, year = (int(x) for x in match.groups())
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        raise AdapterError("Invalid literal source deadline", "parse") from None


def construct_records(source_facts):
    """Original reviewed projections, with full physical-source frontier checks."""
    if set(source_facts) != set(REVIEWED_FACTS):
        raise AdapterError("Incomplete physical programme frontier", "validate")
    ids = set(REVIEWED_FACTS) - set(ALIASES) - set(VISEGRAD_MENUS) - {"835"}
    if ids != set(PROGRAMME_PROFILES) or len(ids) != 348:
        raise AdapterError("Incomplete reviewed semantic programme profiles", "validate")
    records = []
    for pid in sorted(ids, key=int):
        detail = source_facts[pid]
        profile = PROGRAMME_PROFILES[pid]
        related = [pid] + [alias for alias, canonical in ALIASES.items() if canonical == pid]
        menus = [menu for menu, members in VISEGRAD_MENUS.items() if pid in members]
        related += menus
        completed = max(source_facts[key]["completed_at"] for key in related)
        source = REVIEWED_FACTS[pid]
        lang = source["source_language"]
        deadline_key = "Deadline" if lang == "en" else "Uzávierka pre podávanie žiadostí"
        deadline = deadline_date(detail["fields"][deadline_key])
        # Literal CET is supplied by each of these same incoming leaves; no
        # inference from source geography, outgoing translations, or source17.
        if pid in {"296", "298", "299"} and deadline == "2026-10-31":
            application = detail["fields"]["Submission of applications"]
            if "31 October at 16:00 CET" in application:
                deadline = "2026-10-31T15:00:00Z"
        if profile.get("deadline_evidence"):
            proof = profile["deadline_evidence"]
            if proof["literal"] not in detail["fields"].get(proof["field"], ""):
                raise AdapterError("Explicit same-page closing evidence changed", "parse")
            deadline = proof["date"]
        if pid == "778":
            # Own study hub and DB disagree on two already-past2026 closings.
            deadline = None
        record = make_record(
            source["title"], detail["url"], profile["categories"],
            kind=profile.get("kind", "programme-overview"),
            host_countries=profile.get("host_countries", []),
            evidence=list(profile.get("evidence", [])), method="editorial-review")
        record.update(language=profile.get("title_language", lang), summary_language="en",
                      summary=profile["summary"] + " " + ATTRIBUTION + "; copied " + completed + ".",
                      deadline=deadline, status=profile.get("status", "unknown"),
                      last_checked_at=completed)
        record["classification"]["evidence"] += [
            "SAIA processed public facts, noncommercial reuse under current terms §5; "
            + ATTRIBUTION + "; completed source retrieval " + completed + ".",
            "Actual catalogue programme " + pid + "; " + ", ".join(related)
            + "; home-country headings are not a universal nationality whitelist.",
        ]
        if deadline:
            day = datetime.fromisoformat(deadline.replace("Z", "+00:00")).date()
            if day < datetime.now(timezone.utc).date():
                record["status"] = "expired"
            # Programme overview remains unknown until an actual current call is
            # established; a future standing deadline alone is insufficient.
        if len(record["summary"].encode("utf-16-le")) // 2 > 580:
            raise AdapterError("Original summary exceeds consumer budget", "validate")
        records.append(record)
    validate_records(records)
    return records


def collect():
    prepare_collection()
    entry_body, _, headers = fetch_public(SOURCE_URL)
    root = html_root(entry_body, headers)
    base = one(nodes(root, "base"), "actual publisher document base").attrs.get("href")
    if base != WEBSITE_URL:
        raise AdapterError("Publisher document base changed", "parse")
    database_urls = sorted({urllib.parse.urljoin(base, n.attrs.get("href", ""))
                            for n in nodes(root, "a")
                            if urllib.parse.urljoin(base, n.attrs.get("href", ""))
                            == SOURCE_URL + "/database"})
    database_url = one(database_urls, "own database handoff")
    initial_en = fetch_public(database_url)
    verify_current_terms()
    reconcile_own_study_pages()
    inventories = {"en": read_inventory("en", initial=initial_en),
                   "sk": read_inventory("sk")}
    # The SK directional link must come from the actual EN catalogue response.
    # It is independently retained in the reviewed inventory, not a guessed locale.
    source_facts = {}
    for lang, entries in inventories.items():
        for entry in entries:
            pid = entry["id"]
            body, completed, headers = fetch_public(entry["url"])
            title, fields, _ = detail_facts(body, lang, headers)
            guard = REVIEWED_FACTS[pid]
            if title != entry["title"] or facts_hash(title, fields) != guard["facts_sha256"]:
                raise AdapterError("Material programme facts changed; review required: " + pid, "parse")
            source_facts[pid] = {"fields": fields, "url": entry["url"], "completed_at": completed}
    body, amounts_completed, _ = fetch_public(AMOUNTS_URL)
    if hashlib.sha256(body).hexdigest() != AMOUNTS_SHA256:
        raise AdapterError("Outgoing rate table changed; review required", "parse")
    for pid in ("295", "300", "682"):
        source_facts[pid]["completed_at"] = max(source_facts[pid]["completed_at"], amounts_completed)
    lifecycle_remaining(collection=True)
    return construct_records(source_facts)

# Complete reviewed physical frontier, not normalized records.
EXPECTED_INVENTORIES = {'en': [('489',
         'BELGIUM - French Community - research stay for university graduates, PhD '
         'students, researchers and university teachers (1 - 10 months) - scholarship '
         'based on bilateral intergovernmental agreement'),
        ('488',
         'BELGIUM - French Community - Summer School of Slovak Language and Culture - '
         'scholarship based on bilateral intergovernmental agreement'),
        ('503',
         'BULGARIA - study stay for university students (5 months) - scholarship based '
         'on bilateral intergovernmental agreement'),
        ('504',
         'BULGARIA - study/research stay for PhD students, university teachers and '
         'researchers (1 - 10 months) - scholarship based on bilateral '
         'intergovernmental agreement'),
        ('505',
         'BULGARIA - Summer School of Slovak Language and Culture - scholarship based '
         'on bilateral intergovernmental agreement'),
        ('491', 'CEEPUS - lecture stay for foreign university teachers (1 month)'),
        ('810', 'CEEPUS - short-term stay for university staff'),
        ('490',
         'CEEPUS - study/research stay for foreign university students and PhD '
         'students (1 - 10 months)'),
        ('587', 'COST - European Cooperation in Science and Technology'),
        ('459',
         'CROATIA - lecture stay for university teachers of Croatian language and '
         'literature (max. 30 days) - scholarship based on bilateral agreement'),
        ('457',
         'CROATIA - study stay for universtity students and PhD students (1 - 10 '
         'months) - scholarship based on bilateral intergovernmental agreement'),
        ('458',
         'CROATIA - Summer School of Slovak Language and Culture - scholarship based '
         'on bilateral intergovernmental agreement'),
        ('456',
         'CZECH REPUBLIC - short-term research/lecture stay for researchers and '
         'university teachers (max. 14 days) - scholarship based on bilateral '
         'intergovernmental agreement'),
        ('453',
         'CZECH REPUBLIC - study stay for university students (3 - 10 months) - '
         'scholarship based on bilateral intergovernmental agreement'),
        ('454',
         'CZECH REPUBLIC - study/research stay for PhD students (3 - 10 months) - '
         'scholarship based on bilateral intergovernmental agreement'),
        ('455',
         'CZECH REPUBLIC - Summer School of Slovak Language and Culture - scholarship '
         'based on bilateral intergovernmental agreement'),
        ('424',
         'EGYPT - short-term research stay (max. 10 days) for university teachers and '
         'researchers - scholarship based on bilateral intergovernmental agreement'),
        ('482',
         'EGYPT - study stay for university students (3 - 5 months) - scholarship '
         'based on bilateral intergovernmental agreement'),
        ('421',
         'EGYPT - Summer School of Slovak Language and Culture - scholarship based on '
         'bilateral intergovernmental agreement'),
        ('423',
         'EGYPT- research stay for university teachers and researchers (2 - 6 months) '
         '- scholarship based on bilateral intergovernmental agreement'),
        ('583',
         'ERC Advanced Grant - European Research Council grants supporting outstanding '
         'research leaders'),
        ('582',
         'ERC Consolidator Grant - European Research Council grants supporting '
         'researchers consolidating their independent career'),
        ('780',
         'ERC Proof of Concept - European Research Council grant supporting ERC grant '
         'holders'),
        ('581',
         'ERC Starting Grant - European Research Council grants supporting outstanding '
         'early career researchers'),
        ('585',
         'ERC Synergy Grant - European Research Council grants supporting small groups '
         'of researchers collaborating on projects'),
        ('588', 'EUREKA - cooperation in research and development'),
        ('616',
         'European Molecular Biology Organisation - long-term fellowships (2 years) '
         'for post-doctoral research visits'),
        ('617',
         'European Molecular Biology Organisation - short-term fellowships (max. 3 '
         'months) for pre-doctoral and post-doctoral research visits'),
        ('701',
         'FINLAND - short-term lecture/research stay (10 days) for education '
         'specialists - scholarship based on bilateral intergovernmental agreement'),
        ('445',
         'FINLAND - study/research stay for university students, PhD students and '
         'university teachers (3 - 9 months) - scholarship based on bilateral '
         'intergovernmental agreement'),
        ('442',
         'FINLAND - Summer School of Slovak Language and Culture - scholarship based '
         'on bilateral intergovernmental agreement'),
        ('460',
         'GERMANY - Summer School of Slovak Language and Culture - scholarship based '
         'on bilateral intergovernmental agreement'),
        ('435',
         'GREECE - short-term lecture/research stay (10 days) for university teachers '
         'or researchers - scholarship based on bilateral intergovernmental agreement'),
        ('436',
         'GREECE - study/research stay for PhD students (5 or 10 months) - scholarship '
         'based on bilateral intergovernmental agreement'),
        ('434',
         'GREECE - Summer School of Slovak Language and Culture - scholarship based on '
         'bilateral intergovernmental agreement'),
        ('509',
         'HUNGARY - research stay for university teachers and researchers (1 - 3 '
         'months) - scholarship based on bilateral intergovernmental agreement'),
        ('510',
         'HUNGARY - short-term research stay for university teachers and researchers '
         '(5 - 20 days) - scholarship based on bilateral intergovernmental agreement'),
        ('507',
         'HUNGARY - study stay for university students (5 or 10 months) - scholarship '
         'based on bilateral intergovernmental agreement'),
        ('508',
         'HUNGARY - study/research stay for PhD students (1 - 10 months) - scholarship '
         'based on bilateral intergovernmental agreement'),
        ('511',
         'HUNGARY - Summer School of Slovak Language and Culture - scholarship based '
         'on bilateral intergovernmental agreement'),
        ('506',
         'CHINA - study/research stay for university students, PhD students, '
         'university teachers and researchers - scholarship based on bilateral '
         'intergovernmental agreement'),
        ('700',
         'CHINA - Summer School of Slovak Language and Culture - scholarship based on '
         'bilateral intergovernmental agreement'),
        ('842', 'International Visegrad Fund – Grants'),
        ('844', 'International Visegrad Fund – Residencies'),
        ('843', 'International Visegrad Fund – Scholarships and Fellowships'),
        ('426',
         'ISRAEL - study stay for university students (10 months) - scholarship based '
         'on bilateral intergovernmental agreement'),
        ('430',
         'ISRAEL - study/research stay for PhD students (10 months) - scholarship '
         'based on bilateral intergovernmental agreement'),
        ('425',
         'ISRAEL - Summer School of Slovak Language and Culture - scholarship based on '
         'bilateral intergovernmental agreement'),
        ('450',
         'ITALY - study/research stay for university students, PhD students, '
         'univeristy teachers, researchers and university graduates (1 - 10 months) - '
         'bilateral intergovernmental agreement'),
        ('451',
         'ITALY - Summer School of Slovak Language and Culture - scholarship based on '
         'bilateral intergovernmental agreement'),
        ('495',
         'KAZAKHSTAN - study stay for university students (3 - 10 months) - '
         'scholarship based on bilateral intergovernmental agreement'),
        ('496',
         'KAZAKHSTAN - study/research stay for PhD students (3 - 10 months) - '
         'scholarship based on bilateral intergovernmental agreement'),
        ('674',
         'KOREA - DUO-KOREA - DUO-Korea Fellowship Program - students exchange in the '
         'framework of a cooperative project between educational institutions'),
        ('699',
         "MEXICO – SECIHTI Scholarships for Master's and Doctoral Studies Abroad"),
        ('695',
         'MOLDOVA - study stay for university students (5 - 10 months) - scholarship '
         'based on bilateral intergovernmental agreement'),
        ('512',
         'MOLDOVA - study/research stay for PhD students, university teachers and '
         'researchers (3 - 10 months) - scholarship based on bilateral '
         'intergovernmental agreement'),
        ('784',
         'MONTENEGRO - study/research stay for university students and PhD students '
         '(minimum 3 months stay) - scholarship based on bilateral intergovernmental '
         'agreement'),
        ('785',
         'MONTENEGRO - Summer School of Slovak Language and Culture - scholarship '
         'based on bilateral intergovernmental agreement'),
        ('808', 'MSCA Postdoctoral Fellowships'),
        ('807',
         'MSCA COFUND - Co-funding of regional, national and international programmes'),
        ('686', 'MSCA Doctoral Networks'),
        ('665', 'MSCA Staff Exchanges'),
        ('296',
         'National Scholarship Programme of the Slovak Republic - study stay for '
         'university students (1 or 2 semesters)'),
        ('298',
         'National Scholarship Programme of the Slovak Republic - study/research stay '
         'for PhD students (1 - 10 months)'),
        ('299',
         'National Scholarship Programme of the Slovak Republic - '
         'teaching/research/artistic stay for universtity teachers, researchers and '
         'artists (1 - 10 months)'),
        ('452',
         'NORTH MACEDONIA - study/research stay for university students and PhD '
         'students (3 - 10 months) - scholarship based on bilateral intergovernmental '
         'agreement'),
        ('447',
         'NORWAY - short-term research/lecture stay for university teachers or '
         'researchers (7 - 21 days) - scholarship based on bilateral intergovernmental '
         'agreement'),
        ('446',
         'NORWAY - study/research stay for university students and PhD students (3 - 9 '
         'months) - scholarship based on bilateral intergovernmental agreement'),
        ('440',
         'NORWAY - Summer School of Slovak Language and Culture - scholarship based on '
         'bilateral intergovernmental agreement'),
        ('517',
         'POLAND - research stay for university teachers and researchers (1 - 10 '
         'months) - scholarship based on bilateral intergovernmental agreement'),
        ('515',
         'POLAND - study stay for university students (5 months) - scholarship based '
         'on bilateral intergovernmental agreement'),
        ('514',
         'POLAND - study stay for university students of Slovak and Slavonic studies '
         '(5 months) - scholarship based on bilateral intergovernmental agreement'),
        ('516',
         'POLAND - study/research stay for PhD students (1 - 3 months) - scholarship '
         'based on bilateral intergovernmental agreement'),
        ('518',
         'POLAND - Summer School of Slovak Languge and Culture - scholarship based on '
         'bilateral intergovernmental agreement'),
        ('519',
         'ROMANIA - study stay for university students (5 - 10 months) - scholarship '
         'based on bilateral intergovernmental agreement'),
        ('520',
         'ROMANIA - study/research stay for PhD students, university teachers and '
         'researchers (3 - 10 months) - scholarship based on bilateral '
         'intergovernmental agreement'),
        ('521',
         'ROMANIA - Summer School of Slovak Language and Culture - scholarship based '
         'on bilateral intergovernmental agreement'),
        ('525',
         'SERBIA - study/research stay for university students and PhD students (1 - 9 '
         'months) - scholarship based on bilateral intergovernmental agreement'),
        ('524',
         'SERBIA - Summer School of Slovak Language and Culture - scholarship based on '
         'bilateral intergovernmental agreement'),
        ('724', 'Scholarship Programme Vertical for full bachelor studies'),
        ('778',
         'Scholarships for Talented International Students from abroad for full-time '
         'study (bachelor’s degree programme or joint bachelor’s and master’s study '
         'programme)'),
        ('531',
         "Scholarships of the Government of the Slovak Republic - full Bachelor's, "
         "Master's and PhD degree study"),
        ('610',
         'SLOVAKIA - Fulbright English Teaching Assistant (ETA) Program - teaching '
         'stay for U.S. citizens (10 months)'),
        ('605',
         'SLOVAKIA – Fulbright Schuman Program – study, research or lecture in the EU '
         'for U.S. Citizens'),
        ('608',
         'SLOVAKIA – Fulbright Specialist Program – short-term expert/teaching stay '
         'for U.S. citizens (2–6 weeks)'),
        ('609',
         'SLOVAKIA - Fulbright U.S. Student Program – study/research stay for U.S. '
         'citizens (9 months)'),
        ('603',
         'SLOVAKIA– Fulbright U.S. Scholar Program – research and/or teaching stay for '
         'U.S. citizens'),
        ('522',
         'SLOVENIA - study/research stay for university students and PhD students (3 - '
         '10 months) - scholarship based on bilateral intergovernmental agreement'),
        ('523',
         'SLOVENIA - Summer School of Slovak Language and Culture - scholarship based '
         'on bilateral intergovernmental agreement'),
        ('618',
         'Society in Science – The Branco Weiss Fellowship - post-doctoral research '
         'fellowships'),
        ('730',
         "UKRAINE - Penta Scholarship for Ukrainians - full Master's degree study"),
        ('500',
         'UKRAINE - study stay for university students (1 - 10 months) - scholarship '
         'based on bilateral intergovernmental agreement'),
        ('501',
         'UKRAINE - study/research stay for PhD students, university teachers and '
         'researchers (1 - 10 months) - scholarship based on bilateral '
         'intergovernmental agreement'),
        ('502',
         'UKRAINE - Summer School of Slovak Language and Culture - scholarship based '
         'on bilateral intergovernmental agreement'),
        ('606',
         'USA – Fulbright Inter-Country Travel for U.S. Lecturers in Europe (up to 5 '
         'days)'),
        ('604',
         'USA – Fulbright Scholar-in-Residence (S-I-R) Program – teaching stay for '
         'Slovak scholars')],
 'sk': [('731', 'AUSTRÁLIA - magisterské a doktorandské štúdium na Univerzite Monash'),
        ('322', 'BELGICKO - NATO - interné platené stáže pre študentov a absolventov'),
        ('813',
         'BELGICKO – Platené stáže „Consilium“ na Generálnom sekretariáte Rady EÚ'),
        ('345',
         'BELGICKO – Štipendium excelentnosti WBI na postdoktorandský výskumný pobyt '
         'vo Valónsku alebo Bruseli'),
        ('88',
         'BELGICKO – Štipendium na letnú stáž didaktiky francúzskeho jazyka na základe '
         'medzivládnej bilaterálnej dohody'),
        ('90',
         'BELGICKO – Štipendium na letnú stáž francúzskeho jazyka v oblasti '
         'medzinárodných vzťahov na základe medzivládnej bilaterálnej dohody'),
        ('89',
         'BELGICKO – Štipendium na letný kurz francúzskeho jazyka a literatúry na '
         'základe medzivládnej bilaterálnej dohody'),
        ('259', 'BELGICKO / POĽSKO – Postgraduálne štúdium na College of Europe'),
        ('759',
         'Boehringer Ingelheim Fonds – príspevok na výskumné cesty a praktické kurzy v '
         'oblasti biomedicíny do celého sveta'),
        ('758',
         'Boehringer Ingelheim Fonds – výskumné štipendiá na doktorandské štúdium v '
         'oblasti biomedicíny do celého sveta'),
        ('156',
         'BULHARSKO - štipendium na 1- až 10-mesačný postgraduálny a výskumný pobyt na '
         'základe medzivládnej bilaterálnej dohody'),
        ('151',
         'BULHARSKO - štipendium na 5- až 10-mesačný študijný pobyt počas '
         'vysokoškolského štúdia na základe medzivládnej bilaterálnej dohody'),
        ('161',
         'BULHARSKO - štipendium na letný kurz bulharského jazyka na základe '
         'medzivládnej bilaterálnej dohody'),
        ('115',
         'CEEPUS - štipendium pre študentov, doktorandov do krajín strednej a '
         'juhovýchodnej Európy'),
        ('327',
         'CEEPUS - štipendium pre učiteľov VŠ do krajín strednej a juhovýchodnej '
         'Európy'),
        ('799',
         'CEEPUS - štipendium pre zamestnancov vysokých škôl do krajín strednej a '
         'juhovýchodnej Európy'),
        ('712', 'CERN - letná škola pre študentov bakalárskeho a magisterského štúdia'),
        ('711', 'CERN - Post Career Break Fellowship Programme'),
        ('710', 'CERN - pracovná stáž pre študentov - Technical Student Programme'),
        ('572', 'CERN - štipendium pre doktorandov - Doctoral Student Programme'),
        ('569',
         'CERN - štipendium pre skúsených vedeckých pracovníkov - The Scientific '
         'Associateship'),
        ('567', 'COST - Európska spolupráca vo vede a technike'),
        ('347',
         'ČESKO - letný kurz v oblasti politickej ekonómie - The American Institute on '
         'Political and Economic Systems (AIPES)'),
        ('109',
         'ČESKO - postgraduálne štúdium v Centre pre ekonomický výskum a doktorandské '
         'štúdium UK v Prahe - CERGE-EI'),
        ('106',
         'ČESKO – štipendiá na podporu slovenských študentov prijatých do 1. ročníka v '
         'Českej republike a českých študentov prijatých do 1. ročníka na Slovensku'),
        ('56',
         'ČESKO - štipendium na letnú školu slovanských štúdií pre študentov, '
         'doktorandov a vysokoškolských učiteľov na základe medzivládnej bilaterálnej '
         'dohody vlád'),
        ('380',
         'ČESKO - štipendium na prednáškové a výskumné pobyty akademických, vedeckých '
         'a výskumných pracovníkov na základe medzivládnej bilaterálnej dohody (v '
         'dĺžke 1-14 dní)'),
        ('55',
         'ČESKO - štipendium na štipendijný pobyt na vysokej škole pre študentov 2. '
         'stupňa VŠ na základe medzivládnej bilaterálnej dohody vlád'),
        ('467',
         'ČESKO - štipendium na štipendijný pobyt pre doktorandov na základe '
         'medzivládnej bilaterálnej dohody vlád'),
        ('783',
         'ČIERNA HORA - študijný alebo výskumný pobyt na základe medzivládnej '
         'bilaterálnej dohody pre študentov a doktorandov'),
        ('98',
         'ČÍNA - ročný pobyt pre vedeckých a pedagogických pracovníkov vysokých škôl '
         'na základe medzivládnej bilaterálnej dohody'),
        ('96',
         'ČÍNA - ročný študijný pobyt na základe medzivládnej bilaterálnej dohody'),
        ('634', 'ČÍNA - štipendium čínskej vlády - EU Program'),
        ('108',
         'EGYPT - 2- až 6-mesačný dlhodobý vedecký pobyt na základe medzivládnej '
         'bilaterálnej dohody'),
        ('101',
         'EGYPT - 3- až 5-mesačný študijný pobyt na základe medzivládnej bilaterálnej '
         'dohody'),
        ('103',
         'EGYPT - 3- až 5-mesačný výskumný pobyt na základe medzivládnej bilaterálnej '
         'dohody'),
        ('105',
         'EGYPT - krátkodobý vedecký pobyt na základe medzivládnej bilaterálnej '
         'dohody'),
        ('110',
         'EGYPT - kurz arabského jazyka v Arabskom vzdelávacom centre na základe '
         'medzivládnej bilaterálnej dohody'),
        ('774',
         'Erasmus Mundus Joint Masters scholarships - štipendiá na magisterské štúdium '
         'do rôznych krajín sveta'),
        ('578',
         'ERC Advanced Grants - Granty Európskej výskumnej rady pre lídrov vo výskume'),
        ('577',
         'ERC Consolidator Grants - Granty Európskej výskumnej rady pre skúsených '
         'samostatných výskumníkov'),
        ('661',
         'ERC Proof of Concept - Granty Európskej výskumnej rady pre držiteľov iných '
         'ERC grantov'),
        ('576',
         'ERC Starting Grant - Granty Európskej výskumnej rady pre vynikajúcich '
         'mladých výskumníkov'),
        ('579',
         'ERC Synergy Grants - Granty Európskej výskumnej rady pre skupinovú prácu '
         'výskumníkov'),
        ('806', 'ESTÓNSKO – granty pre výskumných a akademických pracovníkov'),
        ('121',
         'ESTÓNSKO – štipendium Estophilus pre študentov, doktorandov a výskumníkov na '
         'výskum tém súvisiacich s Estónskom'),
        ('841', 'ESTÓNSKO – štipendium na letné a zimné školy'),
        ('757',
         'EURÓPSKA ÚNIA – Vedecké stáže v Spoločnom výskumnom centre Európskej komisie '
         '(JRC)'),
        ('741', 'FÍNSKO - letné kurzy fínskeho jazyka a kultúry'),
        ('214',
         'Fond na podporu vzdelávania - finančná pomoc pre študentov a začínajúcich '
         'pedagógov'),
        ('359',
         'FRANCÚZSKO – Odborné stáže a pobyty pre pracovníkov v oblasti kultúry'),
        ('358', 'FRANCÚZSKO – Štipendium ENS Paris-Saclay pre študentov a doktorandov'),
        ('750',
         'FRANCÚZSKO – Štipendium excelentnosti Université de Lille na magisterské '
         'štúdium'),
        ('355',
         'FRANCÚZSKO – Štipendium France Excellence Eiffel na magisterské a '
         'doktorandské štúdium'),
        ('350',
         'FRANCÚZSKO – Štipendium France Excellence na 2. rok magisterského štúdia'),
        ('817', 'FRANCÚZSKO – Štipendium France Excellence na bakalárske štúdium'),
        ('352',
         'FRANCÚZSKO – Štipendium France Excellence na doktorát pod dvojitým vedením '
         '(Cotutelle de thèse)'),
        ('351',
         'FRANCÚZSKO – Štipendium France Excellence na vedecko-výskumné stáže pre '
         'doktorandov a postdoktorandov (SSHN)'),
        ('828',
         'FRANCÚZSKO – Štipendium Université Paris-Saclay na magisterské štúdium'),
        ('374',
         'FRANCÚZSKO – Výskumné štipendium Mesta Paríž pre rodové štúdiá – Cena '
         'Margaret Maruani'),
        ('112',
         'GRÉCKO - 5- alebo 10-mesačný výskumný pobyt na základe medzivládnej '
         'bilaterálnej dohody'),
        ('815',
         'GRÉCKO - krátkodobý prednáškový alebo výskumný pobyt (10 dní) na základe '
         'medzivládnej bilaterálnej dohody'),
        ('559',
         'GRÉCKO – kurzy a semináre moderného gréckeho jazyka a kultúry - I. K. Y.'),
        ('114',
         'GRÉCKO - letný kurz gréckeho jazyka na základe medzivládnej bilaterálnej '
         'dohody'),
        ('273',
         'GRÉCKO – štipendiá nadácie Alexander S. Onassis Public Benefit Foundation'),
        ('140',
         'HOLANDSKO - letná/zimná škola medzinárodného verejného alebo súkromného '
         'práva pre študentov vyšších ročníkov a právnikov'),
        ('256', 'HOLANDSKO - letný kurz pre právnikov v oblasti medzinárodného práva'),
        ('141', 'HOLANDSKO - stáže na Medzinárodnom trestnom súde v Haagu'),
        ('733',
         'HOLANDSKO - Výskumné štipendiá Únie holandského jazyka (Nederlandse '
         'Taalunie)'),
        ('541', 'HONGKONG - štipendium na Lingnan University'),
        ('820',
         'Chemistry Europe Travel Grant – podpora zahraničných mobilít pre mladých '
         'chemikov'),
        ('71',
         'CHORVÁTSKO - štipendium na letný kurz chorvátskeho jazyka a literatúry na '
         'základe medzivládnej bilaterálnej dohody'),
        ('378',
         'CHORVÁTSKO - štipendium na prednáškový pobyt vysokoškolských učiteľov na '
         'základe medzivládnej bilaterálnej dohody'),
        ('70',
         'CHORVÁTSKO - štipendium pre doktorandov na študijný alebo výskumný pobyt na '
         'základe medzivládnej bilaterálnej dohody'),
        ('63',
         'CHORVÁTSKO - štipendium pre študentov na semestrálny študijný pobyt na '
         'základe medzivládnej bilaterálnej dohody'),
        ('147', 'INDIA - odborné kurzy - štipendium ITEC'),
        ('146',
         'INDIA - ročný kurz hindského jazyka - štipendium Central Institute of Hindi '
         'v Agre'),
        ('390', 'INDIA - štipendium na štúdium tradičného indického tanca a hudby'),
        ('272', 'INDONÉZIA - štipendium indonézskej vlády DARMASISWA'),
        ('751', 'INDONÉZIA - Štipendium indonézskej vlády KNB na celé štúdium'),
        ('666', 'ÍRSKO - Hardiman Research Scholarships - doktorandské štúdium'),
        ('274', 'ÍRSKO - Spoločnosť pre výskum rakoviny - celé doktorandské štúdium'),
        ('667', 'ÍRSKO - štipendium írskej vlády'),
        ('142',
         'ÍRSKO - Walsh Fellowships Scheme - magisterské a doktorandské štúdium'),
        ('148',
         'ISLAND - štipendiá islandskej vlády na štúdium islandského jazyka ako '
         'druhého jazyka'),
        ('251',
         'ISLAND - The Snorri Sturluson Icelandic Fellowship pre spisovateľov, '
         'prekladateľov a výskumníkov v oblasti humanitných vied na zdokonalenie v '
         'islandskom jazyku a kultúre'),
        ('320',
         'IZRAEL - štipendium pre profesorov na pobyt na Weizmanov inštitút vedy'),
        ('152',
         'JAPONSKO - 5-ročné vysokoškolské štúdium - štipendium japonskej vlády pre '
         'absolventov stredných škôl - Monbukagakusho Undergraduate Student Program'),
        ('468',
         'JAPONSKO - dlhodobé postdoktorandské štipendium poskytované Japonskou '
         'spoločnosťou pre propagáciu vedy (JSPV - Japan Society for the Promotion of '
         'Science)'),
        ('470',
         'JAPONSKO - Matsumaeho medzinarodná nadácia - Matsumae International '
         'Foundation (MIF)'),
        ('689',
         'JAPONSKO - MEXT - Young Leaders Program (YLP) - ročné štipendium pre '
         'zamestnancov verejnoprávnych inštitúcií na inštitúte GRIPS (The National '
         'Graduate Institute for Policy Studies) v Tokiu'),
        ('344', 'JAPONSKO - Nadácia CANON - program pre mladých výskumníkov'),
        ('471',
         'JAPONSKO - Štipendiá na výskumné pobyty v Slovanskom výskumnom centre '
         'Hokkaidskej univerzity'),
        ('527', 'JAPONSKO - štipendium pre študentov japonológie MONBUKAGAKUSHO'),
        ('478',
         'JAPONSKO - VULCANUS tréningový program pre študentov z Európskej únie'),
        ('163',
         'JAPONSKO - výskumný pobyt (možnosť absolvovať celé magisterské/doktorandské '
         'štúdium) - štipendium japonskej vlády MONBUKAGAKUSHO Research Student '
         'Program'),
        ('365', 'JORDÁNSKO - štipendiá pre študentov a výskumných pracovníkov'),
        ('265', 'KANADA - doktorandské štúdium'),
        ('178',
         'KAZACHSTAN - štipendium na 3- až 10-mesačný študijný pobyt počas '
         'vysokoškolského štúdia na základe medzivládnej bilaterálnej dohody'),
        ('180',
         'KAZACHSTAN - štipendium na 3- až 10-mesačný výskumný pobyt počas '
         'vysokoškolského štúdia na základe medzivládnej bilaterálnej dohody'),
        ('592',
         'Kórea - celé magisterské a doktorandské štúdium na KDI School of Public '
         'Policy and management'),
        ('168',
         'KÓREA - celé magisterské/doktorandské štúdium - Štipendijný program '
         'kórejskej vlády (GKS - Global Korea Scholarship)'),
        ('672',
         'KÓREA - DUO-Korea - štipendijný pobyt pre študentov na základe projektovej '
         'spolupráce vysokoškolských inštitúcií'),
        ('683',
         'KÓREA - výskumný pobyt - Štipendijný program kórejskej vlády (GKS - Global '
         'Korea Scholarship)'),
        ('271', 'Letná škola Stredoeurópskej univerzity (CEU) v Budapešti'),
        ('234',
         'Literárny fond - príspevok na tvorivú cestu vedeckým a výskumným '
         'pracovníkom'),
        ('685', 'LITVA - jazykový kurz - štipendium vlády Litovskej republiky'),
        ('381', 'LOTYŠSKO - letná škola - štipendium Vlády Lotyšskej republiky'),
        ('382',
         'LOTYŠSKO - študijný/výskumný pobyt pre vysokoškolských študentov a '
         'doktorandov - štipendium Vlády Lotyšskej republiky'),
        ('324',
         'LUXEMBURSKO - platené stáže v EP všeobecného zamerania alebo stáže '
         'žurnalistického zamerania (Schumanove štipendium)'),
        ('128',
         'MAĎARSKO - 1– až 10-mesačný doktorandský pobyt na základe medzivládnej '
         'bilaterálnej dohody'),
        ('131',
         'MAĎARSKO - 1– až 3-mesačný výskumný pobyt na základe medzivládnej '
         'bilaterálnej dohody'),
        ('127',
         'MAĎARSKO - 5– až 10-mesačný študijný pobyt na základe medzivládnej '
         'bilaterálnej dohody'),
        ('133',
         'MAĎARSKO - 5- až 20-dňový krátkodobý výskumný pobyt na základe medzivládnej '
         'bilaterálnej dohody'),
        ('134',
         'MAĎARSKO - letný kurz maďarského jazyka/letný odborný kurz na základe '
         'medzivládnej bilaterálnej dohody'),
        ('702', 'Medzinárodný vyšehradský fond – Granty Vyšehrad'),
        ('703', 'Medzinárodný vyšehradský fond – Granty Vyšehrad+'),
        ('826', 'Medzinárodný vyšehradský fond – Literárny rezidenčný program'),
        ('176',
         'Medzinárodný vyšehradský fond – Rezidenčný pobyt pre umelcov scénického '
         'umenia'),
        ('827',
         'Medzinárodný vyšehradský fond – Rezidenčný pobyt pre vizuálnych a zvukových '
         'umelcov'),
        ('481',
         'Medzinárodný vyšehradský fond – Rezidenčný pobyt pre vizuálnych umelcov v '
         'New Yorku'),
        ('825',
         'Medzinárodný vyšehradský fond – Rezidenčný pobyt v oblasti módneho dizajnu v '
         'Miláne'),
        ('704', 'Medzinárodný vyšehradský fond – Strategické granty'),
        ('443',
         'Medzinárodný vyšehradský fond – Štipendijný program Vyšehradská skupina – '
         'Taiwan'),
        ('822',
         'Medzinárodný vyšehradský fond – Štipendijný program Západný Balkán – '
         'Vyšehradská skupina'),
        ('824', 'Medzinárodný vyšehradský fond – Štipendium v Open Society Archives'),
        ('821', 'Medzinárodný vyšehradský fond – V4 Gen Mini Granty'),
        ('823',
         'Medzinárodný vyšehradský fond – Výskumné granty v Historických archívoch EÚ'),
        ('779',
         'Medzinárodný vyšehradský fond – Vyšehradský program pre výskumných '
         'pracovníkov'),
        ('223', 'Medzinárodný vyšehradský fond – Vyšehradský štipendijný program'),
        ('336', 'MEXIKO – Štipendium mexickej vlády na magisterské štúdium'),
        ('340', 'MEXIKO – Štipendium mexickej vlády na mobilitu na bakalárskom stupni'),
        ('771',
         'MEXIKO – Štipendium mexickej vlády na mobilitu na magisterskom stupni'),
        ('337', 'MEXIKO – Štipendium mexickej vlády na výskumný pobyt pre doktorandov'),
        ('339',
         'MEXIKO – Štipendium mexickej vlády na výskumný pobyt pre postdoktorandov'),
        ('187',
         'MOLDAVSKO - štipendium na 3- až 10- mesačný stážový a študijný pobyt na '
         'základe medzivládnej bilaterálnej dohody'),
        ('694',
         'MOLDAVSKO - štipendium na 5- až 10- mesačný študijný pobyt na základe '
         'medzivládnej bilaterálnej dohody'),
        ('762', 'MSCA Postdoctoral Fellowships'),
        ('675',
         'MSCA COFUND - Co-funding of regional, national and international programmes'),
        ('663', 'MSCA Doctoral Networks'),
        ('664', 'MSCA Staff Exchanges'),
        ('792', 'Nadace pro rozvoj vzdělání - Ľehčí rozběh'),
        ('655', 'Nadácia provida / Krídla - štipendium pre podporu štúdia v zahraničí'),
        ('300',
         'Národný štipendijný program SR - doktorandi - štipendium na 1- až 10-mesačný '
         'výskumný pobyt (vrátane cestovného grantu)'),
        ('682',
         'Národný štipendijný program SR - postdoktorandi - štipendium na 2- až '
         '6-mesačný výskumný pobyt (vrátane cestovného grantu)'),
        ('295',
         'Národný štipendijný program SR - študenti VŠ - štipendium na študijný pobyt '
         'v trvaní 1-2 semestre, resp. 1-3 trimestre (vrátane cestovného grantu)'),
        ('775', 'NEMECKO - BAFöG: podpora pre študentov študujúcich v Nemecku'),
        ('189', 'NEMECKO - Berlínsky program DAAD pre umelcov'),
        ('755', 'NEMECKO - ceny Nadácie Alexandra von Humboldta v oblasti výskumu'),
        ('196',
         'NEMECKO - Copernicus - študijné štipendiá s praxou pre študentov vysokých '
         'škôl'),
        ('794',
         'NEMECKO - Deutschlandstipendium - podpora pre študentov študujúcich v '
         'Nemecku'),
        ('743',
         'NEMECKO - Einsteinovo fórum - Štipendium Alberta Einsteina pre výskumníkov'),
        ('190', 'NEMECKO - medzinárodné stáže v parlamente'),
        ('191',
         'NEMECKO - Program výmeny osôb pracujúcich na spoločných projektoch medzi SR '
         'a SRN'),
        ('756',
         'NEMECKO - stáže v Európskej centrálnej banke vo Frankfurte nad Mohanom'),
        ('752',
         'NEMECKO - štipendiá Bayerovej nadácie pre študentov a doktorandov v oblasti '
         'biovied'),
        ('742', 'NEMECKO - štipendiá DAAD na letné semináre'),
        ('192',
         'NEMECKO - štipendiá Európskeho prekladateľského centra v Straelene pre '
         'prekladateľov umeleckej literatúry'),
        ('811',
         'NEMECKO - štipendiá Herderovho inštitútu na výskum v oblasti histórie '
         'strednej a východnej Európy'),
        ('193',
         'NEMECKO - štipendiá Katolíckej akademickej výmennej služby (KAAD) na '
         'študijné a výskumné pobyty'),
        ('769',
         'NEMECKO - štipendiá Leibnizovho inštitútu európskych dejín pre doktorandov a '
         'postdoktorandov'),
        ('797',
         'NEMECKO - štipendiá na hospitačný pobyt v Bavorsku pre učiteľov nemeckého '
         'jazyka na základných a stredných školách'),
        ('796',
         'NEMECKO - štipendiá na letný vzdelávací kurz pre učiteľov nemeckého jazyka v '
         'Dillingen an der Donau'),
        ('195',
         'NEMECKO - štipendiá Nadácie Alexandra von Humboldta na výskumné pobyty pre '
         'postdoktorandov a vedeckých pracovníkov'),
        ('760',
         'NEMECKO - štipendiá Nadácie Friedricha Eberta pre študentov a doktorandov'),
        ('839',
         'NEMECKO - štipendiá Nadácie Friedricha Naumanna na študijné a výskumné '
         'pobyty'),
        ('761',
         'NEMECKO - štipendiá Nadácie Fritza Thyssena na výskumné pobyty pre mladých '
         'vedeckých pracovníkov'),
        ('767',
         'NEMECKO - Štipendiá nadácie Hansa Seidela na vysokoškolské a doktorandské '
         'štúdium a na výskumné pobyty pre doktorandov'),
        ('837',
         'NEMECKO – štipendiá Nadácie Heinricha Bölla pre študentov a doktorandov'),
        ('770',
         'NEMECKO - štipendiá nadácie Klassik Stiftung Weimar pre výskumných a '
         'kultúrnych pracovníkov, publicistov a umelcov'),
        ('738',
         'NEMECKO - štipendiá Nadácie Konrada Adenauera na bakalárske, magisterské a '
         'doktorandské štúdium v Nemecku'),
        ('768',
         'NEMECKO - štipendiá Nadácie pruského kultúrneho dedičstva na výskumné pobyty '
         'pre doktorandov a výskumných pracovníkov'),
        ('838',
         'NEMECKO – štipendiá Nadácie Rosy Luxemburgovej pre študentov a doktorandov'),
        ('812',
         'NEMECKO - štipendiá pre doktorandov a výskumných pracovníkov v knižnici '
         'Herzoga Augusta vo Wolfenbütteli'),
        ('392',
         'NEMECKO - štipendijný program Bavorského slobodného štátu pre absolventov VŠ '
         'zo strednej, východnej a juhovýchodnej Európy (magisterské, doktorandské '
         'štúdium a výskumné pobyty)'),
        ('623',
         'NEMECKO - štipendijný program Nemeckej spolkovej nadácie pre životné '
         'prostredie'),
        ('790',
         'NEMECKO - štipendium DAAD - DLR na výskumné pobyty v oblasti aeronautiky a '
         'vesmíru'),
        ('49', 'NEMECKO - štipendium DAAD na letný jazykový kurz nemeckého jazyka'),
        ('64',
         'NEMECKO - štipendium DAAD na magisterské štúdium pre absolventov VŠ všetkých '
         'vedných disciplín'),
        ('716',
         'NEMECKO - štipendium DAAD na magisterské/doplňujúce štúdium v divadelnom '
         'odbore'),
        ('676',
         'NEMECKO - štipendium DAAD na magisterské/doplňujúce štúdium v odbore '
         'architektúra'),
        ('227',
         'NEMECKO - štipendium DAAD na magisterské/doplňujúce štúdium v odbore hudba'),
        ('687',
         'NEMECKO - štipendium DAAD na magisterské/doplňujúce štúdium v odboroch '
         'výtvarné umenie, dizajn, vizuálna komunikácia a film'),
        ('735',
         'NEMECKO - štipendium DAAD na pobyty pre VŠ učiteľov umeleckých smerov a '
         'architektúry (1 - 3 mesiace)'),
        ('52',
         'NEMECKO - štipendium DAAD na výskumné pobyty pre bývalých štipendistov DAAD '
         '(1 - 3 mesiace)'),
        ('717',
         'NEMECKO - štipendium DAAD na výskumné pobyty v rámci doktorátov pod dvojitým '
         'vedením alebo spoločných doktorandských programov (cotutelle)'),
        ('789',
         'NEMECKO - štipendium DAAD na výskumný pobyt pre doktorandov a '
         'postdoktorandov (2 - 12 mesiacov)'),
        ('840',
         'NEMECKO - štipendium Medzinárodnej knižnice pre mládež na výskum detskej a '
         'mládežníckej literatúry a ilustrácie'),
        ('197',
         'NEMECKO - študijné a výskumné štipendiá pre germanistov nadácie Hermanna '
         'Niermanna'),
        ('528',
         'NÓRSKO - granty na mobility v oblasti nórskeho jazyka, literatúry a kultúry'),
        ('270', 'NÓRSKO - medzinárodná letná škola (ISS) v Oslo'),
        ('746',
         'Platené prekladateľské a administratívne stáže Blue Book v Európskej '
         'komisii'),
        ('319', 'POĽSKO - THESAURUS POLONIAE - trojmesačný štipendijný pobyt'),
        ('726',
         'POĽSKO - Program Stanislawa Ulama – Granty na výskumné pobyty pre '
         'postdoktorandov a vedeckých pracovníkov'),
        ('729', 'POĽSKO - Štipendijný program My First Choice'),
        ('73',
         'POĽSKO - štipendium na letný jazykový kurz poľského jazyka na základe '
         'medzivládnej bilaterálnej dohody'),
        ('222',
         'POĽSKO - štipendium na semestrálny študijný pobyt pre študentov na základe '
         'medzivládnej bilaterálnej dohody'),
        ('72',
         'POĽSKO - štipendium na semestrálny študijný pobyt pre študentov polonistiky, '
         'slavistiky a slovakistiky na základe medzivládnej bilaterálnej dohody'),
        ('75',
         'POĽSKO - štipendium na výskumný pobyt pre doktorandov na základe '
         'medzivládnej bilaterálnej dohody'),
        ('76',
         'POĽSKO - štipendium na výskumný pobyt pre vysokoškolských učiteľov a '
         'výskumníkov na základe medzivládnej bilaterálnej dohody'),
        ('830',
         'PORTUGALSKO – Štipendium Camões na letný kurz portugalského jazyka a '
         'kultúry'),
        ('829',
         'PORTUGALSKO – Štipendium Camões na ročný kurz portugalského jazyka a '
         'kultúry'),
        ('831',
         'PORTUGALSKO – Štipendium Fernão Mendes Pinto na odbornú prípravu v oblasti '
         'výučby portugalčiny ako cudzieho jazyka'),
        ('833',
         'PORTUGALSKO – Štipendium Pessoa na vzdelávacie a výskumné projekty v oblasti '
         'portugalského jazyka a kultúry'),
        ('834',
         'PORTUGALSKO – Štipendium Vieira na odbornú prípravu a zdokonaľovanie v '
         'oblasti prekladu a konferenčného tlmočenia'),
        ('832',
         'PORTUGALSKO – Výskumné štipendium Camões na výskum, magisterské a '
         'doktorandské štúdium v oblasti portugalského jazyka a kultúry'),
        ('181', 'RAKÚSKO - Cena Ignaza Liebena Rakúskej akadémie vied'),
        ('183', 'RAKÚSKO - Európske fórum Alpbach – štipendiá na účasť na seminároch'),
        ('753', 'RAKÚSKO - program ESPRIT pre kvalifikovaných postdoktorandov'),
        ('818',
         'RAKÚSKO - štipendiá Clubu Alpbach Czechia & Slovakia na účasť na seminároch '
         'Európskeho fóra Alpbach'),
        ('165',
         'RAKÚSKO - Štipendium Ernsta Macha pre uchádzačov zo všetkých krajín sveta'),
        ('166',
         'RAKÚSKO - Štipendium Franza Werfela pre mladých vysokoškolských učiteľov so '
         'zameraním na nemecký jazyk a rakúsku literatúru'),
        ('167',
         'RAKÚSKO - Štipendium Richarda Plaschku na výskumné pobyty v odbore história '
         'a archeológia'),
        ('117',
         'RUMUNSKO - 1- až 10-mesačný vedecký alebo výskumný pobyt na základe '
         'medzivládnej bilaterálnej dohody'),
        ('116',
         'RUMUNSKO - 5- až 10-mesačný študijný pobyt počas vysokoškolského štúdia na '
         'základe medzivládnej bilaterálnej dohody'),
        ('118',
         'RUMUNSKO - letný kurz rumunského jazyka, kultúry a literatúry na základe '
         'medzivládnej bilaterálnej dohody'),
        ('636',
         'SEVERNÉ MACEDÓNSKO - štipendium na bakalárske štúdium v macedónskom jazyku'),
        ('333',
         'SEVERNÉ MACEDÓNSKO - štipendium na študijný alebo výskumný pobyt pre '
         'študentov a doktorandov na základe medzivládnej bilaterálnej dohody'),
        ('307',
         'SEVERNÉ MACEDÓNSKO - štipendium na vysokoškolské štúdium na Univerzite '
         'informačných vied a technológií Sv. Pavla Apoštola v Ohride'),
        ('656',
         'SINGAPUR - Program SINGA (The Singapore International Graduate Award) - '
         'doktorandské štúdium'),
        ('707',
         'SLOVENSKO - GROWNi – mentoring pre študentov a začínajúcich profesionálov'),
        ('804',
         'SLOVENSKO - štipendiá pre talentovaných domácich študentov na I. a II. '
         'stupni vysokoškolského štúdia'),
        ('211',
         'SLOVENSKO - U. S. Steel Košice - štipendijný program pre talentovaných '
         'študentov vysokých škôl'),
        ('212',
         'SLOVENSKO - USSTÁŽ v U. S. Steel Košice pre vysokoškolských študentov'),
        ('78',
         'SLOVINSKO - štipendium na letnú školu a seminár slovinského jazyka na '
         'základe medzivládnej bilaterálnej dohody'),
        ('77',
         'SLOVINSKO - štipendium na študijný alebo výskumný pobyt pre študentov a '
         'doktorandov na základe medzivládnej bilaterálnej dohody'),
        ('678',
         'SRBSKO - štipendium na prednáškový pobyt vysokoškolských učiteľov na základe '
         'medzivládnej bilaterálnej dohody'),
        ('81',
         'SRBSKO - štipendium na letné semináre srbského jazyka a kultúry v Srbskej '
         'republike pre študentov, doktorandov a vysokoškolských učiteľov na základe '
         'medzivládnej bilaterálnej dohody'),
        ('79',
         'SRBSKO - študijný alebo výskumný pobyt pre študentov a doktorandov na '
         'základe medzivládnej bilaterálnej dohody'),
        ('705',
         'ŠPANIELSKO / PORTUGALSKO – Štipendium Nadácie „la Caixa“ INPhINIT pre '
         'výskumníkov na doktorandské štúdium'),
        ('693',
         'ŠPANIELSKO / PORTUGALSKO – Štipendium Nadácie „la Caixa“ Junior Leader pre '
         'postdoktorandských výskumníkov v STEM odboroch'),
        ('289',
         'Štipendijná ponuka pre Rómov na prípravný kurz na štúdium na Stredoeurópskej '
         'univerzite v Budapešti alebo inej akreditovanej univerzite'),
        ('631',
         'Štipendium Martina Filka - mobility na prestížnych zahraničných vysokých '
         'školách pre študentov 2. a 3. stupňa vysokoškolského vzdelávania'),
        ('414',
         'ŠVAJČIARSKO - štipendium ESKAS na doktorandské štúdium na základe štip. '
         'ponuky švajčiarskej vlády (Swiss Government Excellence Scholarship)'),
        ('48',
         'ŠVAJČIARSKO - štipendium ESKAS na štúdium v oblasti umenia na základe štip. '
         'ponuky švajčiarskej vlády (Swiss Government Excellence Scholarship)'),
        ('469',
         'ŠVAJČIARSKO - štipendium ESKAS na výskumný pobyt pre doktorandov na základe '
         'štip. ponuky švajčiarskej vlády (Swiss Government Excellence Scholarship)'),
        ('376',
         'TAIWAN - štipendiá ministerstva školstva na štúdium čínskeho (mandarínskeho) '
         'jazyka'),
        ('292',
         'TAIWAN - štipendiá Ministerstva školstva na Taiwane na celé bakalárske '
         'štúdium'),
        ('293',
         'TAIWAN - štipendiá Ministerstva školstva na Taiwane na celé magisterské a '
         'doktorandské štúdium'),
        ('748',
         'TALIANSKO – Granty na krátkodobé pobyty pre vedcov, vysokoškolských '
         'pedagógov, odborníkov a pracovníkov v oblasti kultúry'),
        ('361',
         'TALIANSKO – Max Weber výskumné štipendium pre postdoktorandov spoločenských '
         'a humanitných vied na Európskom univerzitnom inštitúte'),
        ('362',
         'TALIANSKO – Rezidenčný program Nadácie Bogliasco pre umelcov a výskumníkov v '
         'oblasti umenia a humanitných vied'),
        ('835',
         'TALIANSKO – Štipendiá talianskych univerzít pre študentov, doktorandov a '
         'absolventov vysokých škôl'),
        ('836',
         'TALIANSKO – Štipendium talianskej vlády na štúdium operného spevu a '
         'manažmentu scénických umení na Accademia Teatro alla Scala'),
        ('215',
         'TALIANSKO – Štipendium talianskej vlády pre študentov, doktorandov a '
         'výskumných pracovníkov'),
        ('285',
         'THAJSKO - štipendium pre stredoškolákov na štúdium na Regent´s School - '
         'Global Connect Scholarships'),
        ('555',
         'TURECKO - štipendium pre hosťujúcich vedeckých pracovníkov a vedeckých '
         'pracovníkov na tvorivom voľne - štipendium Vedeckej a technickej výskumnej '
         'rady Turecka (TÜBİTAK)'),
        ('388',
         'TURECKO - celé bakalárske štúdium - štipendium Vlády Tureckej republiky'),
        ('543',
         'TURECKO - celé štúdium 2. a 3. stupňa vysokoškolského vzdelávania - '
         'štipendium Vlády Tureckej republiky'),
        ('781',
         'TURECKO - KATÍP - 8- mesačný štipendijný program pre zamestnancov štátnej '
         'správy, diplomatov, akademických a výskumných pracovníkov'),
        ('389',
         'TURECKO - výskumný pobyt pre doktorandov, vysokoškolských učiteľov a '
         'výskumných pracovníkov - štipendium Vlády Tureckej republiky'),
        ('204',
         'UKRAJINA - štipendium na 1- až 10- mesačný výskumný pobyt na základe '
         'medzivládnej bilaterálnej dohody'),
        ('201',
         'UKRAJINA - štipendium na 1- až 10-mesačný študijný pobyt počas '
         'vysokoškolského štúdia na základe medzivládnej bilaterálnej dohody'),
        ('798',
         'UKRAJINA - štipendium na celé vysokoškolské štúdium pre občanov SR '
         'ukrajinskej národnosti na základe medzivládnej bilaterálnej dohody'),
        ('205',
         'UKRAJINA - štipendium na seminár ukrajinského jazyka na základe medzivládnej '
         'bilaterálnej dohody'),
        ('210',
         'United World Colleges - stredoškolské štúdium s medzinárodným bakalaureátom '
         '(IB)'),
        ('725', 'USA - FLEX - ročný študijný pobyt pre stredoškolákov'),
        ('602',
         'USA - Fulbright - Schuman Program - granty na študijné, prednáškové a '
         'výskumné pobyty pre občanov EÚ'),
        ('160',
         'USA - Fulbright Slovak Scholar Program - granty pre vysokoškolských '
         'pedagógov, výskumných pracovníkov a odborníkov z praxe'),
        ('158',
         'USA - Fulbright Slovak Student Program – granty na výskumné pobyty pre '
         'doktorandov a absolventov 2. stupňa VŠ'),
        ('171', 'USA - Hubert H. Humphrey program pre odborníkov z praxe'),
        ('734',
         'USA - Obamova nadácia - štipendiný program na Univerzite Columbia, New York'),
        ('172',
         'USA - Study of the U.S. Institutes (SUSIs) - letné semináre pre '
         'vysokoškolských pedagógov a odborníkov z praxe'),
        ('736',
         'USA – Study of the U.S. Institutes (SUSIs) – letné semináre pre '
         'vysokoškolských študentov'),
        ('765',
         'USA - štipendium Paula Robitscheka - dvojsemestrálny študijný pobyt na '
         'University of Nebraska – Lincoln'),
        ('800', 'VEĽKÁ BRITÁNIA - UK Royal Society - mobility výskumných pracovníkov'),
        ('243',
         'VEĽKÁ BRITÁNIA , USA - Secondary School Scholarship Program - stredoškolské '
         'študijné pobyty'),
        ('627',
         'Za vzdelaním do zahraničia - Za vzděláním do zahraničí - Nadace pro rozvoj '
         'vzdělání')]}

REVIEWED_FACTS = {'296': {'title': 'National Scholarship Programme of the Slovak Republic - study stay '
                  'for university students (1 or 2 semesters)',
         'source_language': 'en',
         'facts_sha256': 'd823a6135c7310b79c18b6e6ce0493a978f0ed9dbd07645cf940414a2758e558'},
 '298': {'title': 'National Scholarship Programme of the Slovak Republic - '
                  'study/research stay for PhD students (1 - 10 months)',
         'source_language': 'en',
         'facts_sha256': '33b42a3c9d6fcbea3a8269a1fe5d027811afe31614202da2d52e5a5b60a903b9'},
 '299': {'title': 'National Scholarship Programme of the Slovak Republic - '
                  'teaching/research/artistic stay for universtity teachers, '
                  'researchers and artists (1 - 10 months)',
         'source_language': 'en',
         'facts_sha256': '6a33cc6f13a5135011b1b38698b4643a5ca9b26d84ee5d6d08c313860c7bad9f'},
 '421': {'title': 'EGYPT - Summer School of Slovak Language and Culture - scholarship '
                  'based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '9778218ed03bd9d5985db0308b021c7664ba83bb1571ab92193b5793ba9a9377'},
 '423': {'title': 'EGYPT- research stay for university teachers and researchers (2 - 6 '
                  'months) - scholarship based on bilateral intergovernmental '
                  'agreement',
         'source_language': 'en',
         'facts_sha256': '81f95b38a5ddd7f3ba8dbfae8c7dcfd2b19c06d69848675c43648a3522075bcf'},
 '424': {'title': 'EGYPT - short-term research stay (max. 10 days) for university '
                  'teachers and researchers - scholarship based on bilateral '
                  'intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': 'c54546d59330fc43f96b709817ec0d9f815c8c63c36c2cf100f82465280a7202'},
 '425': {'title': 'ISRAEL - Summer School of Slovak Language and Culture - scholarship '
                  'based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '74b60b3943093a7ab906ccacafca630af1d1118445382acf305a5944d16e740a'},
 '426': {'title': 'ISRAEL - study stay for university students (10 months) - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': 'a343aa67f4f5ac5ddc1754c86d187d9ab67829acd438fda289372595a967691c'},
 '430': {'title': 'ISRAEL - study/research stay for PhD students (10 months) - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '464eb431d0b031e5fed564e92c91b608d0be552a07c943874b706b1cb3126eb0'},
 '434': {'title': 'GREECE - Summer School of Slovak Language and Culture - scholarship '
                  'based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '52c666082265c696bc4f3fc9081425ede127b41ba21aff12c7d64be2afdcaeae'},
 '435': {'title': 'GREECE - short-term lecture/research stay (10 days) for university '
                  'teachers or researchers - scholarship based on bilateral '
                  'intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '23044ae78b5b0d6fa6a8e1f9d13ab3d18521ab703cc3f50e96b587f23fb59e96'},
 '436': {'title': 'GREECE - study/research stay for PhD students (5 or 10 months) - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '564540bf0bb0c561eb0e92b0798020a17a27be70193d5490eed914f21189c3b5'},
 '440': {'title': 'NORWAY - Summer School of Slovak Language and Culture - scholarship '
                  'based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '0c836c0ad1db4b4a1514669dcadca6c97856af225abe721d1689242b05f91911'},
 '442': {'title': 'FINLAND - Summer School of Slovak Language and Culture - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '9cc0526c59886173b041b30ecaea20faff1553e84ab77fcf21f278c7fb40d211'},
 '445': {'title': 'FINLAND - study/research stay for university students, PhD students '
                  'and university teachers (3 - 9 months) - scholarship based on '
                  'bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': 'd6745e67095414a5f31b2e2e7ff88fc5c243cc0a2074ce36b8b55aca744bf64d'},
 '446': {'title': 'NORWAY - study/research stay for university students and PhD '
                  'students (3 - 9 months) - scholarship based on bilateral '
                  'intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '9beae8a4a8060da3d89ad42595bb035cfac2dafbc3858608e02accc0a7e1612f'},
 '447': {'title': 'NORWAY - short-term research/lecture stay for university teachers '
                  'or researchers (7 - 21 days) - scholarship based on bilateral '
                  'intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '0c6781a22c7a08e915eaac28a46b63d46efe384740532584f8efb722906fc1f8'},
 '450': {'title': 'ITALY - study/research stay for university students, PhD students, '
                  'univeristy teachers, researchers and university graduates (1 - 10 '
                  'months) - bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': 'b30e298a499af5320cf8adee1f55a7792dd82a84649cb9d0a83e4f32a430111e'},
 '451': {'title': 'ITALY - Summer School of Slovak Language and Culture - scholarship '
                  'based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '2a0232797389ab90ca98ae96efbf284d3b4a0026f09edcffd2bc8ed7603850d3'},
 '452': {'title': 'NORTH MACEDONIA - study/research stay for university students and '
                  'PhD students (3 - 10 months) - scholarship based on bilateral '
                  'intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '0748180aad5750471449d71a4f65e8bcd511dd9da380d13a4389b404e044f06e'},
 '453': {'title': 'CZECH REPUBLIC - study stay for university students (3 - 10 months) '
                  '- scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '1c5a877478ed165e0d964158e51f2453ae61c14c8e5db6b4dcaf56db64ccc682'},
 '454': {'title': 'CZECH REPUBLIC - study/research stay for PhD students (3 - 10 '
                  'months) - scholarship based on bilateral intergovernmental '
                  'agreement',
         'source_language': 'en',
         'facts_sha256': '6b53b4ea1f80c5c8b4115f0339b2d9284eee5b00dbee84dad0ae6603c79562a4'},
 '455': {'title': 'CZECH REPUBLIC - Summer School of Slovak Language and Culture - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '8c87ccf1ff2890bc424c902ea9aafd3bec65bfde8d88e06de6526a5ab5adfda4'},
 '456': {'title': 'CZECH REPUBLIC - short-term research/lecture stay for researchers '
                  'and university teachers (max. 14 days) - scholarship based on '
                  'bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '22bd544756f6bfeafad1e4a656d5d25b5f8beb400e418a2a22e1f2fb516ce1c8'},
 '457': {'title': 'CROATIA - study stay for universtity students and PhD students (1 - '
                  '10 months) - scholarship based on bilateral intergovernmental '
                  'agreement',
         'source_language': 'en',
         'facts_sha256': '8128669222e2846279099dbe2af492e1304c45ef7500841ba1770179fa74b3e7'},
 '458': {'title': 'CROATIA - Summer School of Slovak Language and Culture - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '1c9578fedc571c68f92967c81a21038177f2580f20114221a80c8874288b9d08'},
 '459': {'title': 'CROATIA - lecture stay for university teachers of Croatian language '
                  'and literature (max. 30 days) - scholarship based on bilateral '
                  'agreement',
         'source_language': 'en',
         'facts_sha256': '49738a0f8ec5e7402c182a06e78b184bbfce0f5b8ac35b99b7162bcaa218d2c9'},
 '460': {'title': 'GERMANY - Summer School of Slovak Language and Culture - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '73f93de7a9a4020a439f2d3301d3e1ef248f549a47d39c39884e3f904a45efe9'},
 '482': {'title': 'EGYPT - study stay for university students (3 - 5 months) - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '80d46735d55a30acbbc08ff4b1ccc828318dbfdd3c46584407cedfc76cb0bf4f'},
 '488': {'title': 'BELGIUM - French Community - Summer School of Slovak Language and '
                  'Culture - scholarship based on bilateral intergovernmental '
                  'agreement',
         'source_language': 'en',
         'facts_sha256': 'c7ca10bead0ead5587435d9413b34aab80e92d44e81299c6bc2f4797684e0fbc'},
 '489': {'title': 'BELGIUM - French Community - research stay for university '
                  'graduates, PhD students, researchers and university teachers (1 - '
                  '10 months) - scholarship based on bilateral intergovernmental '
                  'agreement',
         'source_language': 'en',
         'facts_sha256': '3601ec87db228541ca585242c5416c533293e167746b5cc1d4de0f555a2f3f94'},
 '490': {'title': 'CEEPUS - study/research stay for foreign university students and '
                  'PhD students (1 - 10 months)',
         'source_language': 'en',
         'facts_sha256': '90a3abf41c7c91ed6e3f95b31f841ce3ad93b903adf7e79fd423bc91f39c7800'},
 '491': {'title': 'CEEPUS - lecture stay for foreign university teachers (1 month)',
         'source_language': 'en',
         'facts_sha256': '07deae127952890e99918b088adaee91f600ce2bb2a7af93040abd2a03868728'},
 '495': {'title': 'KAZAKHSTAN - study stay for university students (3 - 10 months) - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '7a3519fa6373a117c9c9b80ee5740d87dcf4ead6fdb47b375558f60ad8abfab6'},
 '496': {'title': 'KAZAKHSTAN - study/research stay for PhD students (3 - 10 months) - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '44963063c2225bdfc79e04103664c980b594f74f12d956f2879e73c597a74b93'},
 '500': {'title': 'UKRAINE - study stay for university students (1 - 10 months) - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '9ae8bace682f17014a72eed1d7f4b5d0c0abf1c1b8e52bb5e759397dda0c8853'},
 '501': {'title': 'UKRAINE - study/research stay for PhD students, university teachers '
                  'and researchers (1 - 10 months) - scholarship based on bilateral '
                  'intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': 'a35c6adccc1e19e0924f413e1870dee32d36a3824a31414ac3eec4370ee9fb6d'},
 '502': {'title': 'UKRAINE - Summer School of Slovak Language and Culture - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '1d22df2af8ac9fdbeda425db1335c453208ae88721965407e577e3dfbc502144'},
 '503': {'title': 'BULGARIA - study stay for university students (5 months) - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '738f2344cf9b012896b341b8803823ae49821784995b9c9c8932b88c3c321a89'},
 '504': {'title': 'BULGARIA - study/research stay for PhD students, university '
                  'teachers and researchers (1 - 10 months) - scholarship based on '
                  'bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '9dc95c48e8fb91f2f2dd3cc25b4ea8ebc6711a9458e972df0d53c1acd863dfdf'},
 '505': {'title': 'BULGARIA - Summer School of Slovak Language and Culture - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': 'b5bf9e5aa9cc30352fc7c3120e91dc0e656a4762a768c315699257088caeb589'},
 '506': {'title': 'CHINA - study/research stay for university students, PhD students, '
                  'university teachers and researchers - scholarship based on '
                  'bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '0ed91bc3c2c5b52a74ebf874ff63f647faf6d694f3241a1847be88ee914ca479'},
 '507': {'title': 'HUNGARY - study stay for university students (5 or 10 months) - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': 'fb8ea02aa7002302ca17bd7ef944ab954359744c0186c7797aa846aa94ed585c'},
 '508': {'title': 'HUNGARY - study/research stay for PhD students (1 - 10 months) - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': 'a114b5dc5e11191a5bcabacc02d07649cd0b7d0e3f7a4172fe23ff92996757fe'},
 '509': {'title': 'HUNGARY - research stay for university teachers and researchers (1 '
                  '- 3 months) - scholarship based on bilateral intergovernmental '
                  'agreement',
         'source_language': 'en',
         'facts_sha256': '580c9deaf52733ddcce913b0636d3b451368b1d0b10276f3a00a2dd013522119'},
 '510': {'title': 'HUNGARY - short-term research stay for university teachers and '
                  'researchers (5 - 20 days) - scholarship based on bilateral '
                  'intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': 'dc8eda5b3710799a36d4f7e75d8c04fa5b05b5a5df165e6cea79e274dd51bd75'},
 '511': {'title': 'HUNGARY - Summer School of Slovak Language and Culture - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': 'e6ffe44f9a7bacc6d8ad5e1140416a44d91e565bbea7d0037316626af1d232be'},
 '512': {'title': 'MOLDOVA - study/research stay for PhD students, university teachers '
                  'and researchers (3 - 10 months) - scholarship based on bilateral '
                  'intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '416fbd9a55cdb88c548a81c7c7e9d0fa833b5a110d566d2f049011404a73a5ee'},
 '514': {'title': 'POLAND - study stay for university students of Slovak and Slavonic '
                  'studies (5 months) - scholarship based on bilateral '
                  'intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '15c5b212822fdc33551894d97fda269f2d3d985a465e2ff064b2d8fdabfaa6ab'},
 '515': {'title': 'POLAND - study stay for university students (5 months) - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': 'a4c788df5271b12ba70dd9096e8cd681985e2989965174f0933c7df12ebca9ec'},
 '516': {'title': 'POLAND - study/research stay for PhD students (1 - 3 months) - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': 'd436727b8066a417ec6e2e5e04b33d423d248e781c55c13ade1917651df6d766'},
 '517': {'title': 'POLAND - research stay for university teachers and researchers (1 - '
                  '10 months) - scholarship based on bilateral intergovernmental '
                  'agreement',
         'source_language': 'en',
         'facts_sha256': '754cb63c9bc59d790f00520c7fcd26aab8349d26c56d5a626b9ec39136b905e2'},
 '518': {'title': 'POLAND - Summer School of Slovak Languge and Culture - scholarship '
                  'based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '9e6c6b51ff12b52e5c76b54ca5be20e3252d27c02c07fcf8b7a4a972492ea116'},
 '519': {'title': 'ROMANIA - study stay for university students (5 - 10 months) - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': 'd88549818dcfe57d8f9e1548495344f8a99ef6c148997edacf7ba79e89e17010'},
 '520': {'title': 'ROMANIA - study/research stay for PhD students, university teachers '
                  'and researchers (3 - 10 months) - scholarship based on bilateral '
                  'intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '242970f50aab2aa2b8f121b6185df516593cca1a14f4e65af000d46f086ad93b'},
 '521': {'title': 'ROMANIA - Summer School of Slovak Language and Culture - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '35c88a476ca794cdc1cbacb5bce60c9f11936b74331bb04520972fb15ba7c253'},
 '522': {'title': 'SLOVENIA - study/research stay for university students and PhD '
                  'students (3 - 10 months) - scholarship based on bilateral '
                  'intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '67a80ca61b3710fd6a880ee2dd09e845d3c48abb8c59a511617979d91b04326b'},
 '523': {'title': 'SLOVENIA - Summer School of Slovak Language and Culture - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '93de313222d8f83aeb7546c6dadc6e977307d957e8cedd2d8b61f396f0f802d5'},
 '524': {'title': 'SERBIA - Summer School of Slovak Language and Culture - scholarship '
                  'based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': 'efbc955712365dd1570d760e1a74877312536a3804534070d13d52f8e278606d'},
 '525': {'title': 'SERBIA - study/research stay for university students and PhD '
                  'students (1 - 9 months) - scholarship based on bilateral '
                  'intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '9336b9f765465f10889841dcd1ae5f46f678f97a0b9483a3f7e4fc1de6b057da'},
 '531': {'title': 'Scholarships of the Government of the Slovak Republic - full '
                  "Bachelor's, Master's and PhD degree study",
         'source_language': 'en',
         'facts_sha256': '610bef1a29e2314751eb508a67cf3af8fd0bf5994b25a2fd3c4a75be34d1ac50'},
 '581': {'title': 'ERC Starting Grant - European Research Council grants supporting '
                  'outstanding early career researchers',
         'source_language': 'en',
         'facts_sha256': 'f2b7a405cd2526651680325f72d937f130e7282a68e025eecf614b27db21c3ea'},
 '582': {'title': 'ERC Consolidator Grant - European Research Council grants '
                  'supporting researchers consolidating their independent career',
         'source_language': 'en',
         'facts_sha256': 'f9d38b35dbf3bfb8f2f6aab71204c08ffd6f91c62ac3b3e03a58703acbf9431f'},
 '583': {'title': 'ERC Advanced Grant - European Research Council grants supporting '
                  'outstanding research leaders',
         'source_language': 'en',
         'facts_sha256': 'c567c0d0383731f38bfabfd95da59a74240b629175e9c4450f7f960acced00b0'},
 '585': {'title': 'ERC Synergy Grant - European Research Council grants supporting '
                  'small groups of researchers collaborating on projects',
         'source_language': 'en',
         'facts_sha256': '389cc7a319a37f98080410d59565a89a7ae86ecd14c78be736e3299159b39a83'},
 '587': {'title': 'COST - European Cooperation in Science and Technology',
         'source_language': 'en',
         'facts_sha256': '41edd69351f2604cb38e10ef03dbca9b97b3c816d10d615ed7ffc5882b09cab3'},
 '588': {'title': 'EUREKA - cooperation in research and development',
         'source_language': 'en',
         'facts_sha256': 'ceeb248ed47ecd2c1c27f8b00057ef93f56ce9a2aef3b33709feff853621191e'},
 '603': {'title': 'SLOVAKIA– Fulbright U.S. Scholar Program – research and/or teaching '
                  'stay for U.S. citizens',
         'source_language': 'en',
         'facts_sha256': '289a99ce2176c5195d87ffb763a9b4c1b164c28139367a8f9c85ee1eb686487b'},
 '604': {'title': 'USA – Fulbright Scholar-in-Residence (S-I-R) Program – teaching '
                  'stay for Slovak scholars',
         'source_language': 'en',
         'facts_sha256': '80b1d9a5233eb071858cd215c83b6788cd7d2e4d1bf7f100d4d7b7f81ccbb48b'},
 '605': {'title': 'SLOVAKIA – Fulbright Schuman Program – study, research or lecture '
                  'in the EU for U.S. Citizens',
         'source_language': 'en',
         'facts_sha256': '8dcbc899112687c13f7892ec0374bab0098cbbd4d2a1889eb1b787b31d61089f'},
 '606': {'title': 'USA – Fulbright Inter-Country Travel for U.S. Lecturers in Europe '
                  '(up to 5 days)',
         'source_language': 'en',
         'facts_sha256': '7361700a4344b11982ab86d2bbac567f8a03d25e741c707269e9f94df313c0e5'},
 '608': {'title': 'SLOVAKIA – Fulbright Specialist Program – short-term '
                  'expert/teaching stay for U.S. citizens (2–6 weeks)',
         'source_language': 'en',
         'facts_sha256': '112519d7369e622be4caec7d1825c405c832b11ef8ce7a7705cd80879755c81a'},
 '609': {'title': 'SLOVAKIA - Fulbright U.S. Student Program – study/research stay for '
                  'U.S. citizens (9 months)',
         'source_language': 'en',
         'facts_sha256': 'c3065555810c1d181c2c6e4b50ee505d6814011c2ef3e30a1a40544d1ab590d6'},
 '610': {'title': 'SLOVAKIA - Fulbright English Teaching Assistant (ETA) Program - '
                  'teaching stay for U.S. citizens (10 months)',
         'source_language': 'en',
         'facts_sha256': '2cc3d6fd7358eff33b48703015202cc158199b221671533d9a086d6ebe38c6d5'},
 '616': {'title': 'European Molecular Biology Organisation - long-term fellowships (2 '
                  'years) for post-doctoral research visits',
         'source_language': 'en',
         'facts_sha256': '8684e0c5225f78fab1c8499bd0efb15a481c0e3d9a31643d4da4b84f94e8c04e'},
 '617': {'title': 'European Molecular Biology Organisation - short-term fellowships '
                  '(max. 3 months) for pre-doctoral and post-doctoral research visits',
         'source_language': 'en',
         'facts_sha256': 'd075e3d0a8ceb3d5cf77c7d3617b2c118ff468967b340afcece4543d6de6def7'},
 '618': {'title': 'Society in Science – The Branco Weiss Fellowship - post-doctoral '
                  'research fellowships',
         'source_language': 'en',
         'facts_sha256': '3b8fa1b68d5b01d74f280b2a51428bb6a02299534ac2a8e76465f72b69b6567a'},
 '665': {'title': 'MSCA Staff Exchanges',
         'source_language': 'en',
         'facts_sha256': '3b2ffe49d5b058d4b207d02e748963b32f3ff2dbfadfc7e26646c352f4e247ab'},
 '674': {'title': 'KOREA - DUO-KOREA - DUO-Korea Fellowship Program - students '
                  'exchange in the framework of a cooperative project between '
                  'educational institutions',
         'source_language': 'en',
         'facts_sha256': 'a07252e1c37d32035b55fe4cd53cfd0f072ec54f35615a9eb71a6b46f00075f7'},
 '686': {'title': 'MSCA Doctoral Networks',
         'source_language': 'en',
         'facts_sha256': '37d7a6d9543ae40c7d909782cd2d72e202b6da9a6e6f68b5ab2fce6f3bdb97c2'},
 '695': {'title': 'MOLDOVA - study stay for university students (5 - 10 months) - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '8a468fb3e6738fdad9b9bf1c52562d4ffec464f5e5521ef726386b7ab489b67f'},
 '699': {'title': "MEXICO – SECIHTI Scholarships for Master's and Doctoral Studies "
                  'Abroad',
         'source_language': 'en',
         'facts_sha256': '0f1bd30408c2bc6b0a4eec1b256b4a583746f27a0ab27960d8e155a16cf38f5e'},
 '700': {'title': 'CHINA - Summer School of Slovak Language and Culture - scholarship '
                  'based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': 'ed666f1936d2226c21f05d55b207c6181eed1db53eb0790765e73839b6755588'},
 '701': {'title': 'FINLAND - short-term lecture/research stay (10 days) for education '
                  'specialists - scholarship based on bilateral intergovernmental '
                  'agreement',
         'source_language': 'en',
         'facts_sha256': '62c9cc6a8fe469f21a94d4d507855ec4c731c968f6ed4d4e5d73e68c75378583'},
 '724': {'title': 'Scholarship Programme Vertical for full bachelor studies',
         'source_language': 'en',
         'facts_sha256': 'fd929741c16aa336bf445cb47249b88553d1c057348862c073ab5c9c96b8ac51'},
 '730': {'title': "UKRAINE - Penta Scholarship for Ukrainians - full Master's degree "
                  'study',
         'source_language': 'en',
         'facts_sha256': 'bd586d2528b3f1ead7f5916b27222cf6977652786f63e38dbe7b0537c24001a2'},
 '778': {'title': 'Scholarships for Talented International Students from abroad for '
                  'full-time study (bachelor’s degree programme or joint bachelor’s '
                  'and master’s study programme)',
         'source_language': 'en',
         'facts_sha256': 'c31a78fcb8fb81a609986896455383b3346d94295840040ae64c9047756741c0'},
 '780': {'title': 'ERC Proof of Concept - European Research Council grant supporting '
                  'ERC grant holders',
         'source_language': 'en',
         'facts_sha256': '983e73aa72ed479a03e641e651afcb37e90f7c45431b8940a54f963e297cc843'},
 '784': {'title': 'MONTENEGRO - study/research stay for university students and PhD '
                  'students (minimum 3 months stay) - scholarship based on bilateral '
                  'intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': '7ed453c1a7b58e75386fef33138a4364c0aa49dd9b8dd0d8cc0bdfc06af32448'},
 '785': {'title': 'MONTENEGRO - Summer School of Slovak Language and Culture - '
                  'scholarship based on bilateral intergovernmental agreement',
         'source_language': 'en',
         'facts_sha256': 'a44c918a3351ca84f89e4694a071ab25da4e6be9f9e97882b31b045e0c6f3e9f'},
 '807': {'title': 'MSCA COFUND - Co-funding of regional, national and international '
                  'programmes',
         'source_language': 'en',
         'facts_sha256': '58e63700e7b2ab662b5a7a2435995b8fe49f79c1475301a2bca7158fd7854219'},
 '808': {'title': 'MSCA Postdoctoral Fellowships',
         'source_language': 'en',
         'facts_sha256': '9bb7831e4528c6b6b79d847f9a7b1de54226503084c4ee04687e18870829b9ab'},
 '810': {'title': 'CEEPUS - short-term stay for university staff',
         'source_language': 'en',
         'facts_sha256': '76e92a799ceeb12a12f61b4f137c9fefc106d22a9aeaefe7415f678f7af015e1'},
 '842': {'title': 'International Visegrad Fund – Grants',
         'source_language': 'en',
         'facts_sha256': '868071b5cfd4993c05d4da4bed4ecf56fc3d907ad15558ebf39f234331487294'},
 '843': {'title': 'International Visegrad Fund – Scholarships and Fellowships',
         'source_language': 'en',
         'facts_sha256': '5ac4a66360ef43f5ef742d39ff5ff10c843bb4d0e66754d87dbc7496fcc51614'},
 '844': {'title': 'International Visegrad Fund – Residencies',
         'source_language': 'en',
         'facts_sha256': 'd7018bfe92453118691460679960c633b4ae28b1ad84b183592cd9c6ef7e2ea9'},
 '101': {'title': 'EGYPT - 3- až 5-mesačný študijný pobyt na základe medzivládnej '
                  'bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': 'ad155026a707d1fb1b6886dd7c83bbff8c53e713ec4d213d20f2b71a1b91b728'},
 '103': {'title': 'EGYPT - 3- až 5-mesačný výskumný pobyt na základe medzivládnej '
                  'bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': '67dee5dda9da8303eb97883abde63eb1d867bd2968e00569bf1134279f9c15a4'},
 '105': {'title': 'EGYPT - krátkodobý vedecký pobyt na základe medzivládnej '
                  'bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': '1cd3ea0779f7974ef52113cfc03564ccb7536df9ceccae2fd2aa15f84fa63b05'},
 '106': {'title': 'ČESKO – štipendiá na podporu slovenských študentov prijatých do 1. '
                  'ročníka v Českej republike a českých študentov prijatých do 1. '
                  'ročníka na Slovensku',
         'source_language': 'sk',
         'facts_sha256': '27eb2b9dbc60fa3f0c3bc3229b80d8dd45f367364c184dccc659dfb3264beec4'},
 '108': {'title': 'EGYPT - 2- až 6-mesačný dlhodobý vedecký pobyt na základe '
                  'medzivládnej bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': '52f0539d249cc66c42976c3c4bfa2f67a1429bb18eb8d41b65b2f911c907eaef'},
 '109': {'title': 'ČESKO - postgraduálne štúdium v Centre pre ekonomický výskum a '
                  'doktorandské štúdium UK v Prahe - CERGE-EI',
         'source_language': 'sk',
         'facts_sha256': '5e0b0e025c66a4829143e914034edcd7f9bec19e087ef893dd4b8aa658112788'},
 '110': {'title': 'EGYPT - kurz arabského jazyka v Arabskom vzdelávacom centre na '
                  'základe medzivládnej bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': 'c65f82bafe84ca4198cda479b5352ec8a43ee68536ea4fbbd8b056b38e1b67af'},
 '112': {'title': 'GRÉCKO - 5- alebo 10-mesačný výskumný pobyt na základe medzivládnej '
                  'bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': '93321b1ec4ba459707911a643f284595210b6e3d0ceff88790ec886ffd857935'},
 '114': {'title': 'GRÉCKO - letný kurz gréckeho jazyka na základe medzivládnej '
                  'bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': '93c28dc4c4f4a4d03f67988e0b7a51a5d32502ae29f2fa28e1c0e2199be1f61d'},
 '115': {'title': 'CEEPUS - štipendium pre študentov, doktorandov do krajín strednej a '
                  'juhovýchodnej Európy',
         'source_language': 'sk',
         'facts_sha256': '497a26e131a1e6f7e2292f3101c85b10f9e931b6c5ff51072e1771b439292d51'},
 '116': {'title': 'RUMUNSKO - 5- až 10-mesačný študijný pobyt počas vysokoškolského '
                  'štúdia na základe medzivládnej bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': '7cd0a65457935c74c3e54a7a26ee7a75f9854f743dc72ffaa1285e9a41d35545'},
 '117': {'title': 'RUMUNSKO - 1- až 10-mesačný vedecký alebo výskumný pobyt na základe '
                  'medzivládnej bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': '1ebdc64e15533092438e128d56ff2dccefc4b079ce5bbd0eac502c2e59f17425'},
 '118': {'title': 'RUMUNSKO - letný kurz rumunského jazyka, kultúry a literatúry na '
                  'základe medzivládnej bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': '93365fd3672b0dd7780418914a0b680389a694ce3a80260d03a4280327480514'},
 '121': {'title': 'ESTÓNSKO – štipendium Estophilus pre študentov, doktorandov a '
                  'výskumníkov na výskum tém súvisiacich s Estónskom',
         'source_language': 'sk',
         'facts_sha256': '667adee2cd2878c614c58f996e64643720b7abb19fc5102b122a125db30e4861'},
 '127': {'title': 'MAĎARSKO - 5– až 10-mesačný študijný pobyt na základe medzivládnej '
                  'bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': 'a4dce5ac1e11eb29d4b12f2ac2e52998ab2a23992488aa0c455660fc46c11328'},
 '128': {'title': 'MAĎARSKO - 1– až 10-mesačný doktorandský pobyt na základe '
                  'medzivládnej bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': '9abffabd98c193943b51898bab5b9df2eeb5fa3dbacca50d9fe83a4f41456701'},
 '131': {'title': 'MAĎARSKO - 1– až 3-mesačný výskumný pobyt na základe medzivládnej '
                  'bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': '38d45fd3c742225a50d2642dfb83b29bdc916d7b8c13d08775bca9af5b61437a'},
 '133': {'title': 'MAĎARSKO - 5- až 20-dňový krátkodobý výskumný pobyt na základe '
                  'medzivládnej bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': '94b7ec77c2f1491071a3e32ed670f4858b8f7595fd2ed206463734a48eb18afe'},
 '134': {'title': 'MAĎARSKO - letný kurz maďarského jazyka/letný odborný kurz na '
                  'základe medzivládnej bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': '9571e3ff21c475a8fa9abf6d16129cc8a8e3a69f3a713ddc7ca9ba21e925a40b'},
 '140': {'title': 'HOLANDSKO - letná/zimná škola medzinárodného verejného alebo '
                  'súkromného práva pre študentov vyšších ročníkov a právnikov',
         'source_language': 'sk',
         'facts_sha256': 'c86e592fa8587750c8233eb93683cc12bbf9a2bbad2bcc869e8379e1570c4ac2'},
 '141': {'title': 'HOLANDSKO - stáže na Medzinárodnom trestnom súde v Haagu',
         'source_language': 'sk',
         'facts_sha256': '4352301cdb10250c60b37219bcdd7f9fc71ba10c3cb09f37743d991fd92d6d89'},
 '142': {'title': 'ÍRSKO - Walsh Fellowships Scheme - magisterské a doktorandské '
                  'štúdium',
         'source_language': 'sk',
         'facts_sha256': 'e0b532775fd4eb9cfb70cc2f552a6a146db6efa205635c744a4040bc3e656cb0'},
 '146': {'title': 'INDIA - ročný kurz hindského jazyka - štipendium Central Institute '
                  'of Hindi v Agre',
         'source_language': 'sk',
         'facts_sha256': 'ab0982aaa21b11bc38d92dc366010d4188d1b8bb4524e555be2949c5318a01c2'},
 '147': {'title': 'INDIA - odborné kurzy - štipendium ITEC',
         'source_language': 'sk',
         'facts_sha256': '388afc740cfeeda41d56f4f1f0d790d925f7cde3ba18953ddfdebbc357615166'},
 '148': {'title': 'ISLAND - štipendiá islandskej vlády na štúdium islandského jazyka '
                  'ako druhého jazyka',
         'source_language': 'sk',
         'facts_sha256': '81c4eacaa0a7db15f28b65067380a0ff014b47862fa3d5401bc478ca6c072015'},
 '151': {'title': 'BULHARSKO - štipendium na 5- až 10-mesačný študijný pobyt počas '
                  'vysokoškolského štúdia na základe medzivládnej bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': 'b95618fdbbc0442f4978f9edc50b4a831cb87f1aa3ac4800bcc19a40bdf6410f'},
 '152': {'title': 'JAPONSKO - 5-ročné vysokoškolské štúdium - štipendium japonskej '
                  'vlády pre absolventov stredných škôl - Monbukagakusho Undergraduate '
                  'Student Program',
         'source_language': 'sk',
         'facts_sha256': 'b5d3adeb9e2ac5bfa1d15a22a94d2f92eef2f82544d3341f2b6f1aa14871b375'},
 '156': {'title': 'BULHARSKO - štipendium na 1- až 10-mesačný postgraduálny a výskumný '
                  'pobyt na základe medzivládnej bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': '3f649f644ff0637fe64b67600e8981281e0f79e72745f1d23e83a6a3ae50ad08'},
 '158': {'title': 'USA - Fulbright Slovak Student Program – granty na výskumné pobyty '
                  'pre doktorandov a absolventov 2. stupňa VŠ',
         'source_language': 'sk',
         'facts_sha256': '7275d3617916cce357af923539a8728d629bc41ea25d5a6d58fe9f0d8b2c8b9d'},
 '160': {'title': 'USA - Fulbright Slovak Scholar Program - granty pre vysokoškolských '
                  'pedagógov, výskumných pracovníkov a odborníkov z praxe',
         'source_language': 'sk',
         'facts_sha256': 'b09c770f76cb5d2835362ed66f3196d2687c8b81bafaf67c5dee3e95924cc813'},
 '161': {'title': 'BULHARSKO - štipendium na letný kurz bulharského jazyka na základe '
                  'medzivládnej bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': '942bde17a1910ae4705657bd4b7d4c09544a6e12204414ca663cf92dd779b152'},
 '163': {'title': 'JAPONSKO - výskumný pobyt (možnosť absolvovať celé '
                  'magisterské/doktorandské štúdium) - štipendium japonskej vlády '
                  'MONBUKAGAKUSHO Research Student Program',
         'source_language': 'sk',
         'facts_sha256': '2184682e8c61618026db82bc28190ac0b080a9163cce2e5b7eaf444aaa2f49be'},
 '165': {'title': 'RAKÚSKO - Štipendium Ernsta Macha pre uchádzačov zo všetkých krajín '
                  'sveta',
         'source_language': 'sk',
         'facts_sha256': '7253a961171aab3a7893e8f3287dbdbbf0f67baa211746cd1f6b92f4526a1321'},
 '166': {'title': 'RAKÚSKO - Štipendium Franza Werfela pre mladých vysokoškolských '
                  'učiteľov so zameraním na nemecký jazyk a rakúsku literatúru',
         'source_language': 'sk',
         'facts_sha256': 'b34df10ba33cc5162f6f1b6c2046b1620679d4ae6606449e0fa771ba3441ecff'},
 '167': {'title': 'RAKÚSKO - Štipendium Richarda Plaschku na výskumné pobyty v odbore '
                  'história a archeológia',
         'source_language': 'sk',
         'facts_sha256': '52dcaeb610bf9918a9142c002f4f952e4c7676dc5bfd156e5dd014f5bce9e694'},
 '168': {'title': 'KÓREA - celé magisterské/doktorandské štúdium - Štipendijný program '
                  'kórejskej vlády (GKS - Global Korea Scholarship)',
         'source_language': 'sk',
         'facts_sha256': '07550aa4ac89db394f473f4a43926fa6a1efc0669d0d875102f2945401937496'},
 '171': {'title': 'USA - Hubert H. Humphrey program pre odborníkov z praxe',
         'source_language': 'sk',
         'facts_sha256': 'f6c004f5d929dd23b6f4f66758d87b74c6ebfb93deb71111cdefd73782bd0667'},
 '172': {'title': 'USA - Study of the U.S. Institutes (SUSIs) - letné semináre pre '
                  'vysokoškolských pedagógov a odborníkov z praxe',
         'source_language': 'sk',
         'facts_sha256': 'f4fc1e25462f9ebb835242baf39202af7545de525c6629babb1f89e305b9c511'},
 '176': {'title': 'Medzinárodný vyšehradský fond – Rezidenčný pobyt pre umelcov '
                  'scénického umenia',
         'source_language': 'sk',
         'facts_sha256': '20204b6b6343bdf17428ab78e4bdd283e86587ccf15bc8cad474bd6a1e9d9e59'},
 '178': {'title': 'KAZACHSTAN - štipendium na 3- až 10-mesačný študijný pobyt počas '
                  'vysokoškolského štúdia na základe medzivládnej bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': '093f93d192e60961c7ff3733652958f49b5ecb5e3f2e52b12cb70b68ba0167ef'},
 '180': {'title': 'KAZACHSTAN - štipendium na 3- až 10-mesačný výskumný pobyt počas '
                  'vysokoškolského štúdia na základe medzivládnej bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': '6555e98af1c5d66c2a7628a6ae3faa48025e0b74c128153016c352ca047a6e0f'},
 '181': {'title': 'RAKÚSKO - Cena Ignaza Liebena Rakúskej akadémie vied',
         'source_language': 'sk',
         'facts_sha256': 'e5712f783529a21e1dec12204ea831c50b8279e017dd4f1c4d336a548be6e5cc'},
 '183': {'title': 'RAKÚSKO - Európske fórum Alpbach – štipendiá na účasť na seminároch',
         'source_language': 'sk',
         'facts_sha256': 'cfbbbc21a27a560b1483f0ac1f08a10d2567b33b06f70dd5fac3d9bd5adb50e5'},
 '187': {'title': 'MOLDAVSKO - štipendium na 3- až 10- mesačný stážový a študijný '
                  'pobyt na základe medzivládnej bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': 'ced7f97a3346b049e359c69e03ecb0526e1c6ec9ef75cfccbd66ed68b8903280'},
 '189': {'title': 'NEMECKO - Berlínsky program DAAD pre umelcov',
         'source_language': 'sk',
         'facts_sha256': 'a3aca81ac91b42aaf512dbb204099bf1a91986e06ab12582d580d8f712ce4e80'},
 '190': {'title': 'NEMECKO - medzinárodné stáže v parlamente',
         'source_language': 'sk',
         'facts_sha256': '0031c99ba2fbc0437e27c7dee41ede6b09e2ca5ba29044543521b2dae2cbfb5c'},
 '191': {'title': 'NEMECKO - Program výmeny osôb pracujúcich na spoločných projektoch '
                  'medzi SR a SRN',
         'source_language': 'sk',
         'facts_sha256': '42ec55e2ae216eaed46ee1506eac0c68ae00d08b4753175ffc53cf0361126162'},
 '192': {'title': 'NEMECKO - štipendiá Európskeho prekladateľského centra v Straelene '
                  'pre prekladateľov umeleckej literatúry',
         'source_language': 'sk',
         'facts_sha256': '1749ed65ef10b038702eb7412a9253be9d1a6b4f24488cafc52047db20ba7b49'},
 '193': {'title': 'NEMECKO - štipendiá Katolíckej akademickej výmennej služby (KAAD) '
                  'na študijné a výskumné pobyty',
         'source_language': 'sk',
         'facts_sha256': 'a44c65479ab6d315d5a145f89ac6cde206e52d90602452f2e74955e90f03b3ae'},
 '195': {'title': 'NEMECKO - štipendiá Nadácie Alexandra von Humboldta na výskumné '
                  'pobyty pre postdoktorandov a vedeckých pracovníkov',
         'source_language': 'sk',
         'facts_sha256': '5bdbf6296625e2de0acf3460feeb9bdd9e4c9e3bf1b433a2b5abf2524915eac2'},
 '196': {'title': 'NEMECKO - Copernicus - študijné štipendiá s praxou pre študentov '
                  'vysokých škôl',
         'source_language': 'sk',
         'facts_sha256': '778e358b84792428c41143846840b2b2e20bbe1c7b16ddc2be76419a4c0ec87d'},
 '197': {'title': 'NEMECKO - študijné a výskumné štipendiá pre germanistov nadácie '
                  'Hermanna Niermanna',
         'source_language': 'sk',
         'facts_sha256': '276ef91cc2fed86c233c52b02d4e7c372772ba5a679def25e86474326793f5eb'},
 '201': {'title': 'UKRAJINA - štipendium na 1- až 10-mesačný študijný pobyt počas '
                  'vysokoškolského štúdia na základe medzivládnej bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': 'd97f7e9da935089e20585d603c7229e4a30e810d3c4f31bcedaec58f99778955'},
 '204': {'title': 'UKRAJINA - štipendium na 1- až 10- mesačný výskumný pobyt na '
                  'základe medzivládnej bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': 'd24f7175d142ea7466ead8032f9f9ebab283c5c873787bd6c5b5d7c9e1e1e6ca'},
 '205': {'title': 'UKRAJINA - štipendium na seminár ukrajinského jazyka na základe '
                  'medzivládnej bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': '4ac0b27fceb2119215f1b0e3e34179dca30e572ad17e9e462248b01a55fedd1f'},
 '210': {'title': 'United World Colleges - stredoškolské štúdium s medzinárodným '
                  'bakalaureátom (IB)',
         'source_language': 'sk',
         'facts_sha256': 'd8d4893de9ceea43bdd316e031acbdf136ab798d34dc1005ffae0eea9834b9fa'},
 '211': {'title': 'SLOVENSKO - U. S. Steel Košice - štipendijný program pre '
                  'talentovaných študentov vysokých škôl',
         'source_language': 'sk',
         'facts_sha256': '56993df4dc1c04e6d60bdbb10fd5582e0129d61f579b636b18a91a41603d253f'},
 '212': {'title': 'SLOVENSKO - USSTÁŽ v U. S. Steel Košice pre vysokoškolských '
                  'študentov',
         'source_language': 'sk',
         'facts_sha256': 'e0b60b78b8d24308f1174b8ad5a12984074c9058ab69133591a7ed2f693e95b9'},
 '214': {'title': 'Fond na podporu vzdelávania - finančná pomoc pre študentov a '
                  'začínajúcich pedagógov',
         'source_language': 'sk',
         'facts_sha256': '33e7e6ce836b3bba6862bed455801371aed76bc0c26cec9c8d19d212e3fbff1d'},
 '215': {'title': 'TALIANSKO – Štipendium talianskej vlády pre študentov, doktorandov '
                  'a výskumných pracovníkov',
         'source_language': 'sk',
         'facts_sha256': 'bfc84417f7b25d1d5d1df03a755982c96d4a51128319e65c3287f48b83155023'},
 '222': {'title': 'POĽSKO - štipendium na semestrálny študijný pobyt pre študentov na '
                  'základe medzivládnej bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': 'c4a9bf465ddfe06b3467fc30273e80c5753e209d18f54945ba8d9c65dddd9ac1'},
 '223': {'title': 'Medzinárodný vyšehradský fond – Vyšehradský štipendijný program',
         'source_language': 'sk',
         'facts_sha256': '9303a0fa5a38a43b6b5b1ab474bd81eecd06286f7d9a2498ee812292e2aebc1f'},
 '227': {'title': 'NEMECKO - štipendium DAAD na magisterské/doplňujúce štúdium v '
                  'odbore hudba',
         'source_language': 'sk',
         'facts_sha256': '51bed3e4b9ee5cc6a8e7768bbbb3fb01d33b3bd634b32fecddb63900f2e9477c'},
 '234': {'title': 'Literárny fond - príspevok na tvorivú cestu vedeckým a výskumným '
                  'pracovníkom',
         'source_language': 'sk',
         'facts_sha256': '1d9efefcebb6a427ff672ec0c462b2d94da5ad61bb3399fa8cf9f6c11e69dfa7'},
 '243': {'title': 'VEĽKÁ BRITÁNIA , USA - Secondary School Scholarship Program - '
                  'stredoškolské študijné pobyty',
         'source_language': 'sk',
         'facts_sha256': '83402a9e3530d3a11d6f5f72a75f7d1b9d614ddd0b78fe5a1862c480ad530a54'},
 '251': {'title': 'ISLAND - The Snorri Sturluson Icelandic Fellowship pre '
                  'spisovateľov, prekladateľov a výskumníkov v oblasti humanitných '
                  'vied na zdokonalenie v islandskom jazyku a kultúre',
         'source_language': 'sk',
         'facts_sha256': '5e68e1efce12d5c01d4ad865ef58010f2593e28345931af656b4240b27bb8fa6'},
 '256': {'title': 'HOLANDSKO - letný kurz pre právnikov v oblasti medzinárodného práva',
         'source_language': 'sk',
         'facts_sha256': 'cb9cef1a7305fb6898c0ec717d4c4a0c64c312f99116eae7d1332dc7a4897bd1'},
 '259': {'title': 'BELGICKO / POĽSKO – Postgraduálne štúdium na College of Europe',
         'source_language': 'sk',
         'facts_sha256': '846dd92950c059c14a5b558a071b77b63d2c5b4c94062a99b46b278bd63fe112'},
 '265': {'title': 'KANADA - doktorandské štúdium',
         'source_language': 'sk',
         'facts_sha256': '5dbe95f2ece4eae2463c637006eaff7c52559c41b7d2e6235954c99a2fa20593'},
 '270': {'title': 'NÓRSKO - medzinárodná letná škola (ISS) v Oslo',
         'source_language': 'sk',
         'facts_sha256': 'dca5dd3db9e8c6dfa34d2a799d620b15bbf0a0405f6036d31bcb869e221cf3a8'},
 '271': {'title': 'Letná škola Stredoeurópskej univerzity (CEU) v Budapešti',
         'source_language': 'sk',
         'facts_sha256': '27e17a8f18bb8046e3e16691e5576b9c8129d96c4eb75742c078800b292eeee2'},
 '272': {'title': 'INDONÉZIA - štipendium indonézskej vlády DARMASISWA',
         'source_language': 'sk',
         'facts_sha256': '67d241493cd06fa9c16e51b05123c0ec87758e4704882a4f20cdb64339f948ba'},
 '273': {'title': 'GRÉCKO – štipendiá nadácie Alexander S. Onassis Public Benefit '
                  'Foundation',
         'source_language': 'sk',
         'facts_sha256': '15508f63fb71d36511eff6e1b09f099b2a08a2668f1e719bcdc2e2814d979904'},
 '274': {'title': 'ÍRSKO - Spoločnosť pre výskum rakoviny - celé doktorandské štúdium',
         'source_language': 'sk',
         'facts_sha256': 'bcfaf70ae02c9573177d4daa0913d340dbdd1e7eb34fbd7f38e6e3c2103d6e12'},
 '285': {'title': 'THAJSKO - štipendium pre stredoškolákov na štúdium na Regent´s '
                  'School - Global Connect Scholarships',
         'source_language': 'sk',
         'facts_sha256': 'd3590d4b4368134db40744a8fa1888f0eb81f95ece5364108c6c5d1e99110082'},
 '289': {'title': 'Štipendijná ponuka pre Rómov na prípravný kurz na štúdium na '
                  'Stredoeurópskej univerzite v Budapešti alebo inej akreditovanej '
                  'univerzite',
         'source_language': 'sk',
         'facts_sha256': 'c5d960a8a96d421fdf72841ad8d14561c8331848cdeabd4918d325a44611240d'},
 '292': {'title': 'TAIWAN - štipendiá Ministerstva školstva na Taiwane na celé '
                  'bakalárske štúdium',
         'source_language': 'sk',
         'facts_sha256': 'a72c19dc4e0b77c7c2be56db8ed589b8cb7049d8978819c7169d7c050a935a5d'},
 '293': {'title': 'TAIWAN - štipendiá Ministerstva školstva na Taiwane na celé '
                  'magisterské a doktorandské štúdium',
         'source_language': 'sk',
         'facts_sha256': '88fea1033215f12e4a8499304ec37e6b87a6a81a7b3228473cf1fdf9ed29227b'},
 '295': {'title': 'Národný štipendijný program SR - študenti VŠ - štipendium na '
                  'študijný pobyt v trvaní 1-2 semestre, resp. 1-3 trimestre (vrátane '
                  'cestovného grantu)',
         'source_language': 'sk',
         'facts_sha256': 'c5f7e52ce3fbd98ebd36019a89ca590baebe7e81c1a76ef7b7918a9688833831'},
 '300': {'title': 'Národný štipendijný program SR - doktorandi - štipendium na 1- až '
                  '10-mesačný výskumný pobyt (vrátane cestovného grantu)',
         'source_language': 'sk',
         'facts_sha256': '02e82ec7de98e82c502c0d17ac61fc7467a97c4693a51cdc6e715f2d5cc84fba'},
 '307': {'title': 'SEVERNÉ MACEDÓNSKO - štipendium na vysokoškolské štúdium na '
                  'Univerzite informačných vied a technológií Sv. Pavla Apoštola v '
                  'Ohride',
         'source_language': 'sk',
         'facts_sha256': '4bd3b676a9d64bd43a57d6f0f30af216701d4465753b8bf90e6aef819d89087e'},
 '319': {'title': 'POĽSKO - THESAURUS POLONIAE - trojmesačný štipendijný pobyt',
         'source_language': 'sk',
         'facts_sha256': '3ccc256ecc850fb928f7ff1f79b42a8f000d7da48b2c1deb6222e09028ad9f4d'},
 '320': {'title': 'IZRAEL - štipendium pre profesorov na pobyt na Weizmanov inštitút '
                  'vedy',
         'source_language': 'sk',
         'facts_sha256': 'f42bdab807a97f497f41ce7c20fd48748970bfb09f736f49d2f0988e1bef97db'},
 '322': {'title': 'BELGICKO - NATO - interné platené stáže pre študentov a absolventov',
         'source_language': 'sk',
         'facts_sha256': '187b1cbec2428ef8a10aa704a8d6374f646478d62247110ecfed1de2021e1e2e'},
 '324': {'title': 'LUXEMBURSKO - platené stáže v EP všeobecného zamerania alebo stáže '
                  'žurnalistického zamerania (Schumanove štipendium)',
         'source_language': 'sk',
         'facts_sha256': '8cfd65f92672c1f03f01ce7fbf9d3675ed316934c592b40c15ea0e80375b060d'},
 '327': {'title': 'CEEPUS - štipendium pre učiteľov VŠ do krajín strednej a '
                  'juhovýchodnej Európy',
         'source_language': 'sk',
         'facts_sha256': '3d340ea0429cf0357c01033edcd56b90cae73bc31a922ac45a94763f30461524'},
 '333': {'title': 'SEVERNÉ MACEDÓNSKO - štipendium na študijný alebo výskumný pobyt '
                  'pre študentov a doktorandov na základe medzivládnej bilaterálnej '
                  'dohody',
         'source_language': 'sk',
         'facts_sha256': '70bf2ada991c7ea5a6f8a2dc8d2f083386705bd79d21ee8efe265f599ac960ae'},
 '336': {'title': 'MEXIKO – Štipendium mexickej vlády na magisterské štúdium',
         'source_language': 'sk',
         'facts_sha256': '09ed02dd081e906eac6e2edb856941869ecfe566d0f4aeae4d139a64234304b7'},
 '337': {'title': 'MEXIKO – Štipendium mexickej vlády na výskumný pobyt pre '
                  'doktorandov',
         'source_language': 'sk',
         'facts_sha256': 'b7abd601423db01123cb2f024c877a77f96e05bd4b5f51ece9f43c6abcd9e7bd'},
 '339': {'title': 'MEXIKO – Štipendium mexickej vlády na výskumný pobyt pre '
                  'postdoktorandov',
         'source_language': 'sk',
         'facts_sha256': '13a87164040e2d4c8bfce0d6b10a059ec88bdcbb69920ddde0a13f2456528def'},
 '340': {'title': 'MEXIKO – Štipendium mexickej vlády na mobilitu na bakalárskom '
                  'stupni',
         'source_language': 'sk',
         'facts_sha256': 'f3a2b76c6ed27dbb07452d852a4e4e143a78ef56212238d0d4fb2a34e878afb3'},
 '344': {'title': 'JAPONSKO - Nadácia CANON - program pre mladých výskumníkov',
         'source_language': 'sk',
         'facts_sha256': '69fe949d36431ca4e1ef3dda7de46ad4db12e7ede91be3c4ae6eb2eb545d95f7'},
 '345': {'title': 'BELGICKO – Štipendium excelentnosti WBI na postdoktorandský '
                  'výskumný pobyt vo Valónsku alebo Bruseli',
         'source_language': 'sk',
         'facts_sha256': 'f2d705e46ca6e5e638186c81695cc69a1818f743d1bb7ee00a1b296887088764'},
 '347': {'title': 'ČESKO - letný kurz v oblasti politickej ekonómie - The American '
                  'Institute on Political and Economic Systems (AIPES)',
         'source_language': 'sk',
         'facts_sha256': '85a0e18a0831b8d184917923d11710c83be4257f40d823ae38dc314f5811a26c'},
 '350': {'title': 'FRANCÚZSKO – Štipendium France Excellence na 2. rok magisterského '
                  'štúdia',
         'source_language': 'sk',
         'facts_sha256': '07589011c09e56316aeae41604a373d0b21484d83d2570d8e72d6d060bfb5e48'},
 '351': {'title': 'FRANCÚZSKO – Štipendium France Excellence na vedecko-výskumné stáže '
                  'pre doktorandov a postdoktorandov (SSHN)',
         'source_language': 'sk',
         'facts_sha256': '189cdd4ff3db420caa579a17b995856f38c5a08c714c1d5632e115b272446631'},
 '352': {'title': 'FRANCÚZSKO – Štipendium France Excellence na doktorát pod dvojitým '
                  'vedením (Cotutelle de thèse)',
         'source_language': 'sk',
         'facts_sha256': '4c260d5a6117c6f898c22f4b87d17fb1acc215bc0ce6d01b4d0c5f1faddd710f'},
 '355': {'title': 'FRANCÚZSKO – Štipendium France Excellence Eiffel na magisterské a '
                  'doktorandské štúdium',
         'source_language': 'sk',
         'facts_sha256': '2bfed0d840b90e1ce66e2a186f7e63f08268ec142fd88f33774fabb48e5979fc'},
 '358': {'title': 'FRANCÚZSKO – Štipendium ENS Paris-Saclay pre študentov a '
                  'doktorandov',
         'source_language': 'sk',
         'facts_sha256': 'fc59ff5279aba3beaef5fd1668370df7cbf7338ef515e34f8922a21cbd5644fc'},
 '359': {'title': 'FRANCÚZSKO – Odborné stáže a pobyty pre pracovníkov v oblasti '
                  'kultúry',
         'source_language': 'sk',
         'facts_sha256': '7ff03be0ed8fca29e71f8a7ce52f1e6f9dc51765fad1bcd4dd99b96abded5878'},
 '361': {'title': 'TALIANSKO – Max Weber výskumné štipendium pre postdoktorandov '
                  'spoločenských a humanitných vied na Európskom univerzitnom '
                  'inštitúte',
         'source_language': 'sk',
         'facts_sha256': '37f17112ce21e6965cc9f225d903c74d71b8658488daa29b3a8c1d4e11503b82'},
 '362': {'title': 'TALIANSKO – Rezidenčný program Nadácie Bogliasco pre umelcov a '
                  'výskumníkov v oblasti umenia a humanitných vied',
         'source_language': 'sk',
         'facts_sha256': '75f69a721e69265be22971fbc96e53e6bc44266bce88a18448d7db6a25753ae2'},
 '365': {'title': 'JORDÁNSKO - štipendiá pre študentov a výskumných pracovníkov',
         'source_language': 'sk',
         'facts_sha256': '928562cd1974682cc8711b51c06084668dca1eefd235a86e158381fb99ffb8fd'},
 '374': {'title': 'FRANCÚZSKO – Výskumné štipendium Mesta Paríž pre rodové štúdiá – '
                  'Cena Margaret Maruani',
         'source_language': 'sk',
         'facts_sha256': '599bada75446426da552e00c73d4468ae5221e96dce87dd8fa0d3b319c2ed807'},
 '376': {'title': 'TAIWAN - štipendiá ministerstva školstva na štúdium čínskeho '
                  '(mandarínskeho) jazyka',
         'source_language': 'sk',
         'facts_sha256': 'b9dea653bc387925fd1f90904ce7f3173ac8f7947669dde58b120f535b787f09'},
 '378': {'title': 'CHORVÁTSKO - štipendium na prednáškový pobyt vysokoškolských '
                  'učiteľov na základe medzivládnej bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': '15c4e4452fed7ac0324bd92ae46a7c41a40154089400456ab4244a5fa6124f75'},
 '380': {'title': 'ČESKO - štipendium na prednáškové a výskumné pobyty akademických, '
                  'vedeckých a výskumných pracovníkov na základe medzivládnej '
                  'bilaterálnej dohody (v dĺžke 1-14 dní)',
         'source_language': 'sk',
         'facts_sha256': '15bf2f656ffa88286903604d23472809542b9e4e1af5d7c2f15a7a222ae55901'},
 '381': {'title': 'LOTYŠSKO - letná škola - štipendium Vlády Lotyšskej republiky',
         'source_language': 'sk',
         'facts_sha256': '3db07291ee579d7acb11a15b0bf977a275800661f9f645bc9d67d676a02b2df0'},
 '382': {'title': 'LOTYŠSKO - študijný/výskumný pobyt pre vysokoškolských študentov a '
                  'doktorandov - štipendium Vlády Lotyšskej republiky',
         'source_language': 'sk',
         'facts_sha256': 'b9eea41c58bda40fa6cc2124138ebef9ee780d4bae7249307981e2e5357d764e'},
 '388': {'title': 'TURECKO - celé bakalárske štúdium - štipendium Vlády Tureckej '
                  'republiky',
         'source_language': 'sk',
         'facts_sha256': '814bd49e36b1b02374cbef0c0060a549bf74112b271a6d0d9a6f89a8e5f6207b'},
 '389': {'title': 'TURECKO - výskumný pobyt pre doktorandov, vysokoškolských učiteľov '
                  'a výskumných pracovníkov - štipendium Vlády Tureckej republiky',
         'source_language': 'sk',
         'facts_sha256': '58ce32b3e306cbed56207de6e11c4abf5107e389ee1d84274f86290effcfe5b1'},
 '390': {'title': 'INDIA - štipendium na štúdium tradičného indického tanca a hudby',
         'source_language': 'sk',
         'facts_sha256': '644b0ca18aeea655400a7dcc382df07a5208c30d8e5696636206d4e88678c0b3'},
 '392': {'title': 'NEMECKO - štipendijný program Bavorského slobodného štátu pre '
                  'absolventov VŠ zo strednej, východnej a juhovýchodnej Európy '
                  '(magisterské, doktorandské štúdium a výskumné pobyty)',
         'source_language': 'sk',
         'facts_sha256': '908854b1d70f2d7ad12fb4ee06b977d5bee2ee20aea4bdb7f9b8bc23570d1ab8'},
 '414': {'title': 'ŠVAJČIARSKO - štipendium ESKAS na doktorandské štúdium na základe '
                  'štip. ponuky švajčiarskej vlády (Swiss Government Excellence '
                  'Scholarship)',
         'source_language': 'sk',
         'facts_sha256': '8aee5fd44a12aa107fe599004a4d37432d2a6462ff171e339b9e0ed0c3165578'},
 '443': {'title': 'Medzinárodný vyšehradský fond – Štipendijný program Vyšehradská '
                  'skupina – Taiwan',
         'source_language': 'sk',
         'facts_sha256': '0797fa0e1dd8ea4fc86a333a80aaa8cfd89a4829db00644644cfb43171c12990'},
 '467': {'title': 'ČESKO - štipendium na štipendijný pobyt pre doktorandov na základe '
                  'medzivládnej bilaterálnej dohody vlád',
         'source_language': 'sk',
         'facts_sha256': '3852086cfcb3fa3ee003bb08ee392aa6f5e345c618952dac12384add8cfa8da1'},
 '468': {'title': 'JAPONSKO - dlhodobé postdoktorandské štipendium poskytované '
                  'Japonskou spoločnosťou pre propagáciu vedy (JSPV - Japan Society '
                  'for the Promotion of Science)',
         'source_language': 'sk',
         'facts_sha256': '02665c83e4c5209439c536d9447390c5c8316633cfca8de429a54b350fe74d94'},
 '469': {'title': 'ŠVAJČIARSKO - štipendium ESKAS na výskumný pobyt pre doktorandov na '
                  'základe štip. ponuky švajčiarskej vlády (Swiss Government '
                  'Excellence Scholarship)',
         'source_language': 'sk',
         'facts_sha256': '3a29920bb09427d67f2e207982f976656efbe45bb8617c39b88c126a1a48cd72'},
 '470': {'title': 'JAPONSKO - Matsumaeho medzinarodná nadácia - Matsumae International '
                  'Foundation (MIF)',
         'source_language': 'sk',
         'facts_sha256': 'a7394613c8364fee8695a48cc1945ae03ea867c36ff1ce668ac7e8639c346f59'},
 '471': {'title': 'JAPONSKO - Štipendiá na výskumné pobyty v Slovanskom výskumnom '
                  'centre Hokkaidskej univerzity',
         'source_language': 'sk',
         'facts_sha256': '255b04f691cc171d1e7eddcb6122dce298a3951991e5497da1b7b370fe0481e5'},
 '478': {'title': 'JAPONSKO - VULCANUS tréningový program pre študentov z Európskej '
                  'únie',
         'source_language': 'sk',
         'facts_sha256': '8205a533903c28955f0c8aef5987e22523e4d20f5f6d3df3f39aa932bcce6577'},
 '48': {'title': 'ŠVAJČIARSKO - štipendium ESKAS na štúdium v oblasti umenia na '
                 'základe štip. ponuky švajčiarskej vlády (Swiss Government Excellence '
                 'Scholarship)',
        'source_language': 'sk',
        'facts_sha256': '2a6fac473302aa64e3d2d828bbfc3e8a6fd90297eb6640c7c4d2f539e2d44021'},
 '481': {'title': 'Medzinárodný vyšehradský fond – Rezidenčný pobyt pre vizuálnych '
                  'umelcov v New Yorku',
         'source_language': 'sk',
         'facts_sha256': '812f504a6d589af7defaf8423aab5c7df6ba3b2d7029010ab79a305502f3368a'},
 '49': {'title': 'NEMECKO - štipendium DAAD na letný jazykový kurz nemeckého jazyka',
        'source_language': 'sk',
        'facts_sha256': '445bb63add9a7b60cbdd0e7ccb1d3bd4caee78edec50aebcab14fada4dd43dbc'},
 '52': {'title': 'NEMECKO - štipendium DAAD na výskumné pobyty pre bývalých '
                 'štipendistov DAAD (1 - 3 mesiace)',
        'source_language': 'sk',
        'facts_sha256': '162faefb9d0d1812db1a313dfcb8eb832ead72ee6d9bd37f72a5d95c17de49fd'},
 '527': {'title': 'JAPONSKO - štipendium pre študentov japonológie MONBUKAGAKUSHO',
         'source_language': 'sk',
         'facts_sha256': '4c537aa5b3899b62924794583004e7cde7183fa25578e458546ba5da550c35af'},
 '528': {'title': 'NÓRSKO - granty na mobility v oblasti nórskeho jazyka, literatúry a '
                  'kultúry',
         'source_language': 'sk',
         'facts_sha256': '0c9df0aa10b9fe1ee68d8303dee68726cc8b4a0684343aea1d0dabb6cb92e7bf'},
 '541': {'title': 'HONGKONG - štipendium na Lingnan University',
         'source_language': 'sk',
         'facts_sha256': '87fcde7d08b1a374735f3b9160b7d6bb03a2d4fcbb3feabff85856ceae812b6f'},
 '543': {'title': 'TURECKO - celé štúdium 2. a 3. stupňa vysokoškolského vzdelávania - '
                  'štipendium Vlády Tureckej republiky',
         'source_language': 'sk',
         'facts_sha256': 'c7f58b67845a2670f2a93746fef37dff18a791c8077c6b23f3dd3777ffe521fb'},
 '55': {'title': 'ČESKO - štipendium na štipendijný pobyt na vysokej škole pre '
                 'študentov 2. stupňa VŠ na základe medzivládnej bilaterálnej dohody '
                 'vlád',
        'source_language': 'sk',
        'facts_sha256': 'e9d435c74172128affe1ff8bd3a0ae0fc026a094bccdd1b68568430cfedf997a'},
 '555': {'title': 'TURECKO - štipendium pre hosťujúcich vedeckých pracovníkov a '
                  'vedeckých pracovníkov na tvorivom voľne - štipendium Vedeckej a '
                  'technickej výskumnej rady Turecka (TÜBİTAK)',
         'source_language': 'sk',
         'facts_sha256': '3a71cb03da34db45398237cfe5510151c5b4dd76504470023ece5052a8b01202'},
 '559': {'title': 'GRÉCKO – kurzy a semináre moderného gréckeho jazyka a kultúry - I. '
                  'K. Y.',
         'source_language': 'sk',
         'facts_sha256': '4d79e70c2b162f067b7b629999b42a3223814e4550331d13c6bda43a33d57a6f'},
 '56': {'title': 'ČESKO - štipendium na letnú školu slovanských štúdií pre študentov, '
                 'doktorandov a vysokoškolských učiteľov na základe medzivládnej '
                 'bilaterálnej dohody vlád',
        'source_language': 'sk',
        'facts_sha256': '450e45dd5ca91069b12796da3d2d42d5e5755d49de78f99a337c1d54662ffadb'},
 '567': {'title': 'COST - Európska spolupráca vo vede a technike',
         'source_language': 'sk',
         'facts_sha256': 'a9db5eefb380e8e0dd44f21e3893d3bbcef2d0833123f96a2d5653ef148f8e65'},
 '569': {'title': 'CERN - štipendium pre skúsených vedeckých pracovníkov - The '
                  'Scientific Associateship',
         'source_language': 'sk',
         'facts_sha256': '2e87a40fa0ec535abd47dc9f20973cc6f8b428e59c2ff4db478ccbe92b778d42'},
 '572': {'title': 'CERN - štipendium pre doktorandov - Doctoral Student Programme',
         'source_language': 'sk',
         'facts_sha256': 'c590ddc7337b055b51c47fe76c75687ba6854319bd112ffb50d94ad94daba45e'},
 '576': {'title': 'ERC Starting Grant - Granty Európskej výskumnej rady pre '
                  'vynikajúcich mladých výskumníkov',
         'source_language': 'sk',
         'facts_sha256': '7cf471b1a1b81335503735e621d55b804822cb6cbc40462f07d5659a54e73783'},
 '577': {'title': 'ERC Consolidator Grants - Granty Európskej výskumnej rady pre '
                  'skúsených samostatných výskumníkov',
         'source_language': 'sk',
         'facts_sha256': 'd762e6f5d755d847758ffe57c2399575469a0b2a29d12d8bfc007b82c9d71e41'},
 '578': {'title': 'ERC Advanced Grants - Granty Európskej výskumnej rady pre lídrov vo '
                  'výskume',
         'source_language': 'sk',
         'facts_sha256': '4aaa4cb509b053bb18052f4dfeb3691eb014bd4798f3fcf9774536ee84f2beb8'},
 '579': {'title': 'ERC Synergy Grants - Granty Európskej výskumnej rady pre skupinovú '
                  'prácu výskumníkov',
         'source_language': 'sk',
         'facts_sha256': '659a4ac76fb0750de6f23acced792513a57476ef0e52ca6b2b7709966c745820'},
 '592': {'title': 'Kórea - celé magisterské a doktorandské štúdium na KDI School of '
                  'Public Policy and management',
         'source_language': 'sk',
         'facts_sha256': '9e6a628eb4d9d5837d9a262ff7e496e885f26b5e22fa18ed9dea7d21fbb4eb69'},
 '602': {'title': 'USA - Fulbright - Schuman Program - granty na študijné, prednáškové '
                  'a výskumné pobyty pre občanov EÚ',
         'source_language': 'sk',
         'facts_sha256': '1b1a044552164207cdfaf0cf3e609313df2da9e17cd99b1993eed477b3997b06'},
 '623': {'title': 'NEMECKO - štipendijný program Nemeckej spolkovej nadácie pre '
                  'životné prostredie',
         'source_language': 'sk',
         'facts_sha256': 'd8fe8e43910b5f4e53c7d1b55312615d2f5579880c537a87c377bae5afcb5831'},
 '627': {'title': 'Za vzdelaním do zahraničia - Za vzděláním do zahraničí - Nadace pro '
                  'rozvoj vzdělání',
         'source_language': 'sk',
         'facts_sha256': '09c91259fd7f7fcc938f32e5872f114a1c3a00b520f0c3c69bf86bffff44152e'},
 '63': {'title': 'CHORVÁTSKO - štipendium pre študentov na semestrálny študijný pobyt '
                 'na základe medzivládnej bilaterálnej dohody',
        'source_language': 'sk',
        'facts_sha256': '5e6611b545e337d939718318923110f257706ce89da9c36aa0f68dd6a5a66472'},
 '631': {'title': 'Štipendium Martina Filka - mobility na prestížnych zahraničných '
                  'vysokých školách pre študentov 2. a 3. stupňa vysokoškolského '
                  'vzdelávania',
         'source_language': 'sk',
         'facts_sha256': 'b3c63b44543edef09299acd44b93dc98246fb8f865d65aa9391016a6ef47f166'},
 '634': {'title': 'ČÍNA - štipendium čínskej vlády - EU Program',
         'source_language': 'sk',
         'facts_sha256': '1e377f54534fcc326911da4e55a85154d12cee2b82c000bdb1b634a74206ba45'},
 '636': {'title': 'SEVERNÉ MACEDÓNSKO - štipendium na bakalárske štúdium v macedónskom '
                  'jazyku',
         'source_language': 'sk',
         'facts_sha256': '244505ebed041e522e0bd6cc5badc3992be9f90519fdccdad9cd1bbef037a72e'},
 '64': {'title': 'NEMECKO - štipendium DAAD na magisterské štúdium pre absolventov VŠ '
                 'všetkých vedných disciplín',
        'source_language': 'sk',
        'facts_sha256': '0e3a2c4280e734270d77d1f821fd4d44976bf83f1cb7d0c07aa538b1443aaa86'},
 '655': {'title': 'Nadácia provida / Krídla - štipendium pre podporu štúdia v '
                  'zahraničí',
         'source_language': 'sk',
         'facts_sha256': '132af0fc92b8ee46cf0b54e7692d6e5e8c6b20d83811d150288ccc69d9b6fb0d'},
 '656': {'title': 'SINGAPUR - Program SINGA (The Singapore International Graduate '
                  'Award) - doktorandské štúdium',
         'source_language': 'sk',
         'facts_sha256': 'c5c6d4f41ce039cd63ce0379499f7e0db856758afea9f5ef2e74b548582600e9'},
 '661': {'title': 'ERC Proof of Concept - Granty Európskej výskumnej rady pre '
                  'držiteľov iných ERC grantov',
         'source_language': 'sk',
         'facts_sha256': '11c8a18fa23da230b06df97fb11ec8e57cd7d0a5c15df68306f2fe2fc2e95f47'},
 '663': {'title': 'MSCA Doctoral Networks',
         'source_language': 'sk',
         'facts_sha256': '774534f17bd2c04579af8d56a90d0091c40f00ee497dad9cfbe9814fdd8ff732'},
 '664': {'title': 'MSCA Staff Exchanges',
         'source_language': 'sk',
         'facts_sha256': 'aeafb51ada83677c9415fff33c9903e10e6848eaf1163ea50b40803cba883484'},
 '666': {'title': 'ÍRSKO - Hardiman Research Scholarships - doktorandské štúdium',
         'source_language': 'sk',
         'facts_sha256': '75a9dc7ef99d94c8a73be74b9ef211bf65983e41b5ebb5d5c3502db01ba1a163'},
 '667': {'title': 'ÍRSKO - štipendium írskej vlády',
         'source_language': 'sk',
         'facts_sha256': 'b1eff7f2bcfdaaf3b0637231508c870fed047eafff1938007c18b4d837864f7c'},
 '672': {'title': 'KÓREA - DUO-Korea - štipendijný pobyt pre študentov na základe '
                  'projektovej spolupráce vysokoškolských inštitúcií',
         'source_language': 'sk',
         'facts_sha256': '55cfbdaaebce73b7839ebbc8fe707e98efa2fc3321f2294ef702ae230ba1d1af'},
 '675': {'title': 'MSCA COFUND - Co-funding of regional, national and international '
                  'programmes',
         'source_language': 'sk',
         'facts_sha256': '07c11e490f94e80482947ac3045159627649e1b8c5aac66bc3d0a3efb3b1ff12'},
 '676': {'title': 'NEMECKO - štipendium DAAD na magisterské/doplňujúce štúdium v '
                  'odbore architektúra',
         'source_language': 'sk',
         'facts_sha256': '3fea9b1b707e0ec7f4a9d43063f20b304fe058ad76e689c96594f9e8734ccbee'},
 '678': {'title': 'SRBSKO - štipendium na prednáškový pobyt vysokoškolských učiteľov '
                  'na základe medzivládnej bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': 'c749359cfa8cbb950281d83767e3769ccdc47a53aa5130d9977ead64ac9ec66f'},
 '682': {'title': 'Národný štipendijný program SR - postdoktorandi - štipendium na 2- '
                  'až 6-mesačný výskumný pobyt (vrátane cestovného grantu)',
         'source_language': 'sk',
         'facts_sha256': 'b00f9e930fc879e3729a4819c919bd9576e55b68b3f195b2da03931e8104dfb3'},
 '683': {'title': 'KÓREA - výskumný pobyt - Štipendijný program kórejskej vlády (GKS - '
                  'Global Korea Scholarship)',
         'source_language': 'sk',
         'facts_sha256': 'a5850b249b4c01c696df72980673eecf31ee51a4637223f166e8b9fd577af54a'},
 '685': {'title': 'LITVA - jazykový kurz - štipendium vlády Litovskej republiky',
         'source_language': 'sk',
         'facts_sha256': '3556c42b006bddf724bdef1529094a8109f875e7e0b6e0e0ca66bd58e2863858'},
 '687': {'title': 'NEMECKO - štipendium DAAD na magisterské/doplňujúce štúdium v '
                  'odboroch výtvarné umenie, dizajn, vizuálna komunikácia a film',
         'source_language': 'sk',
         'facts_sha256': '0f3e6d275457bf32b366a7fc05383639393e57ffe93d0194edbc6a970817aca0'},
 '689': {'title': 'JAPONSKO - MEXT - Young Leaders Program (YLP) - ročné štipendium '
                  'pre zamestnancov verejnoprávnych inštitúcií na inštitúte GRIPS (The '
                  'National Graduate Institute for Policy Studies) v Tokiu',
         'source_language': 'sk',
         'facts_sha256': '0565327b76964acbb4d7b63484420911e04d6928354f80540f4ddea6ac6977d7'},
 '693': {'title': 'ŠPANIELSKO / PORTUGALSKO – Štipendium Nadácie „la Caixa“ Junior '
                  'Leader pre postdoktorandských výskumníkov v STEM odboroch',
         'source_language': 'sk',
         'facts_sha256': 'df768ff9b4abd886cf82369a9fa7ec45dcfc6c3c6cc545554d44fc8ef7939236'},
 '694': {'title': 'MOLDAVSKO - štipendium na 5- až 10- mesačný študijný pobyt na '
                  'základe medzivládnej bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': 'b04177d09d5170c4737e6a9fa0b1310e6c5827df417ac60ba3c84a8997cb22f4'},
 '70': {'title': 'CHORVÁTSKO - štipendium pre doktorandov na študijný alebo výskumný '
                 'pobyt na základe medzivládnej bilaterálnej dohody',
        'source_language': 'sk',
        'facts_sha256': 'f4a294ac8b60b11123255a7d487be0e649b79f1e009c7021ed9bc812b5772dd3'},
 '702': {'title': 'Medzinárodný vyšehradský fond – Granty Vyšehrad',
         'source_language': 'sk',
         'facts_sha256': '086c538f57051fbaf5c44c534ce339c56b54befd1fcf2a8c6148d8c1c43006d2'},
 '703': {'title': 'Medzinárodný vyšehradský fond – Granty Vyšehrad+',
         'source_language': 'sk',
         'facts_sha256': 'bc7d12d6ba59276680de6b339ea24a98be016de98fc8649f63d0e135dee7fc77'},
 '704': {'title': 'Medzinárodný vyšehradský fond – Strategické granty',
         'source_language': 'sk',
         'facts_sha256': '468b03e3e81ab822e7112926171a8f8f66f523828173959dbf49c277bb514352'},
 '705': {'title': 'ŠPANIELSKO / PORTUGALSKO – Štipendium Nadácie „la Caixa“ INPhINIT '
                  'pre výskumníkov na doktorandské štúdium',
         'source_language': 'sk',
         'facts_sha256': 'e28ac90e5c900cd11ddc2308f180659be9f56bbbc903e5dedfca93dbcfdb1d34'},
 '707': {'title': 'SLOVENSKO - GROWNi – mentoring pre študentov a začínajúcich '
                  'profesionálov',
         'source_language': 'sk',
         'facts_sha256': '0f0bd11be66bfbf0ec87b6b296a781d2af0542bb8c615847de25984fa3c0eba6'},
 '71': {'title': 'CHORVÁTSKO - štipendium na letný kurz chorvátskeho jazyka a '
                 'literatúry na základe medzivládnej bilaterálnej dohody',
        'source_language': 'sk',
        'facts_sha256': 'f615db3d7c17dc2e0260335332191bd4c9bc408a233b416f178d8467fe20a4ca'},
 '710': {'title': 'CERN - pracovná stáž pre študentov - Technical Student Programme',
         'source_language': 'sk',
         'facts_sha256': 'b418664ec0ab4f78705bf7e3295645ed1c546b77719916da50c4539cda74ab31'},
 '711': {'title': 'CERN - Post Career Break Fellowship Programme',
         'source_language': 'sk',
         'facts_sha256': 'bb0e2c9c6284222f78c390bfa5ae968bae0233c5cb2f75a378c15608c7bc7414'},
 '712': {'title': 'CERN - letná škola pre študentov bakalárskeho a magisterského '
                  'štúdia',
         'source_language': 'sk',
         'facts_sha256': 'df1795906803ada04bf127876fdfb7812f3691fab9eb0acd24881f90e76ecdbe'},
 '716': {'title': 'NEMECKO - štipendium DAAD na magisterské/doplňujúce štúdium v '
                  'divadelnom odbore',
         'source_language': 'sk',
         'facts_sha256': '3fd61b551beb12109a2464ba5e27e9d62c83b0fb8fb658c9ce035c2fff19db98'},
 '717': {'title': 'NEMECKO - štipendium DAAD na výskumné pobyty v rámci doktorátov pod '
                  'dvojitým vedením alebo spoločných doktorandských programov '
                  '(cotutelle)',
         'source_language': 'sk',
         'facts_sha256': 'a9c5424b6e4cb2c40179575323c6967ba92c144f984df36734b2241eaaaf184e'},
 '72': {'title': 'POĽSKO - štipendium na semestrálny študijný pobyt pre študentov '
                 'polonistiky, slavistiky a slovakistiky na základe medzivládnej '
                 'bilaterálnej dohody',
        'source_language': 'sk',
        'facts_sha256': '9faaf0a20a2470231b91a50daf342380226ca65287076b6b749e2c7ad5ee2aa7'},
 '725': {'title': 'USA - FLEX - ročný študijný pobyt pre stredoškolákov',
         'source_language': 'sk',
         'facts_sha256': 'e89131bfabae5fe05222fd74443b34666a90f47aecfc0a7c58fac2f833fdd497'},
 '726': {'title': 'POĽSKO - Program Stanislawa Ulama – Granty na výskumné pobyty pre '
                  'postdoktorandov a vedeckých pracovníkov',
         'source_language': 'sk',
         'facts_sha256': '282b2be8cbb299b1c37487feae47bf8f8039b80cb722c741d43b02bdeedeb5f5'},
 '729': {'title': 'POĽSKO - Štipendijný program My First Choice',
         'source_language': 'sk',
         'facts_sha256': 'bc18d771b9a879bc2490c99528a3035158fffab1177061e3de13e36475e4e1fb'},
 '73': {'title': 'POĽSKO - štipendium na letný jazykový kurz poľského jazyka na '
                 'základe medzivládnej bilaterálnej dohody',
        'source_language': 'sk',
        'facts_sha256': '786225a8eb74a05cf8676119da738189d9195dd1085b17e370db7615576760b5'},
 '731': {'title': 'AUSTRÁLIA - magisterské a doktorandské štúdium na Univerzite Monash',
         'source_language': 'sk',
         'facts_sha256': '3252fa45a4146d3693eb061e4ef555c97acbfa35c806c80a7ddd9670ebe58d18'},
 '733': {'title': 'HOLANDSKO - Výskumné štipendiá Únie holandského jazyka (Nederlandse '
                  'Taalunie)',
         'source_language': 'sk',
         'facts_sha256': '7f92b2b7a5ae85e786a33e67fe45483c41ec4412945edd5932e9966940c0e51b'},
 '734': {'title': 'USA - Obamova nadácia - štipendiný program na Univerzite Columbia, '
                  'New York',
         'source_language': 'sk',
         'facts_sha256': 'd37607a6b82798c305c4fa8b388684da503c671c3ac0d8d23d1f081d17376776'},
 '735': {'title': 'NEMECKO - štipendium DAAD na pobyty pre VŠ učiteľov umeleckých '
                  'smerov a architektúry (1 - 3 mesiace)',
         'source_language': 'sk',
         'facts_sha256': 'e912d06e53f8cbc0852a0458474a31745448ce87ad622e80e2ef1ffaf88aae7c'},
 '736': {'title': 'USA – Study of the U.S. Institutes (SUSIs) – letné semináre pre '
                  'vysokoškolských študentov',
         'source_language': 'sk',
         'facts_sha256': '1c640142fa3cf4f3174ffc1066c96f05ee608ace094646cc0dd371b965cbe852'},
 '738': {'title': 'NEMECKO - štipendiá Nadácie Konrada Adenauera na bakalárske, '
                  'magisterské a doktorandské štúdium v Nemecku',
         'source_language': 'sk',
         'facts_sha256': '397c5b65383c3b61c95842f0135f5ca9271078c4c24107230f7b7b760143c2ae'},
 '741': {'title': 'FÍNSKO - letné kurzy fínskeho jazyka a kultúry',
         'source_language': 'sk',
         'facts_sha256': 'e63c0244f32d47b8bc114f89a74229ad092330de9cd85c1c89162f74dc2421f3'},
 '742': {'title': 'NEMECKO - štipendiá DAAD na letné semináre',
         'source_language': 'sk',
         'facts_sha256': 'a967a0f76bc2276bf629f360802c411ecefc28d5a247d3cfa612e980b0627edf'},
 '743': {'title': 'NEMECKO - Einsteinovo fórum - Štipendium Alberta Einsteina pre '
                  'výskumníkov',
         'source_language': 'sk',
         'facts_sha256': '7b39069b124f2c06a8685f49f7f8b71e35bc92c76794286299baf22b6ff0e518'},
 '746': {'title': 'Platené prekladateľské a administratívne stáže Blue Book v '
                  'Európskej komisii',
         'source_language': 'sk',
         'facts_sha256': 'bda40f0bbf1484101c0659f422b418be42226e8260cafd628c1211fa08d6982e'},
 '748': {'title': 'TALIANSKO – Granty na krátkodobé pobyty pre vedcov, vysokoškolských '
                  'pedagógov, odborníkov a pracovníkov v oblasti kultúry',
         'source_language': 'sk',
         'facts_sha256': 'f565d113df144a58a50b558646963b33f4b866e265ceeddfcc9eaa5f8ee15596'},
 '75': {'title': 'POĽSKO - štipendium na výskumný pobyt pre doktorandov na základe '
                 'medzivládnej bilaterálnej dohody',
        'source_language': 'sk',
        'facts_sha256': '2b67aaefbc385253a455b7d1d28d8bcb8ae82e58b2397d9104361b657035ab2d'},
 '750': {'title': 'FRANCÚZSKO – Štipendium excelentnosti Université de Lille na '
                  'magisterské štúdium',
         'source_language': 'sk',
         'facts_sha256': '988cef247c618d668a3dce9c0d05649c65297bf7ee1be603d803648b9d633f0d'},
 '751': {'title': 'INDONÉZIA - Štipendium indonézskej vlády KNB na celé štúdium',
         'source_language': 'sk',
         'facts_sha256': '15453452141a8997e7c5ba3286c32c458de4cfbe8cdb39f5b4faa356b09b5831'},
 '752': {'title': 'NEMECKO - štipendiá Bayerovej nadácie pre študentov a doktorandov v '
                  'oblasti biovied',
         'source_language': 'sk',
         'facts_sha256': '8d0ac708841ef967e88e61e27ee948ce1f35c8b57ab7ded87069ee52e9a3fb56'},
 '753': {'title': 'RAKÚSKO - program ESPRIT pre kvalifikovaných postdoktorandov',
         'source_language': 'sk',
         'facts_sha256': '83976e2f83b638e55bc93e073fe26cb104353af058eac7de7303d4e4e0f04346'},
 '755': {'title': 'NEMECKO - ceny Nadácie Alexandra von Humboldta v oblasti výskumu',
         'source_language': 'sk',
         'facts_sha256': 'c1647af87b76ab8236f2e096bf2fb0ae4bb1dda809391c00c8e51aeedd410d34'},
 '756': {'title': 'NEMECKO - stáže v Európskej centrálnej banke vo Frankfurte nad '
                  'Mohanom',
         'source_language': 'sk',
         'facts_sha256': '2d94ca4128433b2aab50f78d6853598fc395b01bbc26ed73d4dfe27833fbd046'},
 '757': {'title': 'EURÓPSKA ÚNIA – Vedecké stáže v Spoločnom výskumnom centre '
                  'Európskej komisie (JRC)',
         'source_language': 'sk',
         'facts_sha256': 'f3a31706f023db78ebfcd2631822f4c4f16f95c401d35373e10f4a42c67d40ef'},
 '758': {'title': 'Boehringer Ingelheim Fonds – výskumné štipendiá na doktorandské '
                  'štúdium v oblasti biomedicíny do celého sveta',
         'source_language': 'sk',
         'facts_sha256': 'ecb46b6247d12870627b9bb17da7af6251b13813c2cf57b1ee07e29c46988d47'},
 '759': {'title': 'Boehringer Ingelheim Fonds – príspevok na výskumné cesty a '
                  'praktické kurzy v oblasti biomedicíny do celého sveta',
         'source_language': 'sk',
         'facts_sha256': 'f282d6f5b405a83a769787862b4982af77b51d73bbaf6b21c9fab830ac68bed7'},
 '76': {'title': 'POĽSKO - štipendium na výskumný pobyt pre vysokoškolských učiteľov a '
                 'výskumníkov na základe medzivládnej bilaterálnej dohody',
        'source_language': 'sk',
        'facts_sha256': '9ae070379625910b8f60bd08dc18d2f5ce5e7f663d5c8bfbab3869b4f6be8aec'},
 '760': {'title': 'NEMECKO - štipendiá Nadácie Friedricha Eberta pre študentov a '
                  'doktorandov',
         'source_language': 'sk',
         'facts_sha256': '5f3db35268ab5230b857377bc8ab9578a53b3f2481db9f2971008da9dd7b0588'},
 '761': {'title': 'NEMECKO - štipendiá Nadácie Fritza Thyssena na výskumné pobyty pre '
                  'mladých vedeckých pracovníkov',
         'source_language': 'sk',
         'facts_sha256': '6307b2c56d150dcd7c815e0188c6d498b82365f8fc2bbd83c0423a66a1a23ee0'},
 '762': {'title': 'MSCA Postdoctoral Fellowships',
         'source_language': 'sk',
         'facts_sha256': '583a5b594a6a5aaa74f5980bcc69ade34db927081b7be7c06df03b2d4ae0ba60'},
 '765': {'title': 'USA - štipendium Paula Robitscheka - dvojsemestrálny študijný pobyt '
                  'na University of Nebraska – Lincoln',
         'source_language': 'sk',
         'facts_sha256': '151bb2ee13aee7efc49fdca00bf273f7e5458065b21cc3c92efb046a81167607'},
 '767': {'title': 'NEMECKO - Štipendiá nadácie Hansa Seidela na vysokoškolské a '
                  'doktorandské štúdium a na výskumné pobyty pre doktorandov',
         'source_language': 'sk',
         'facts_sha256': 'a64a5ef02cb17be41f5f69ed6f8bd3f0ba0ac28adc07b172e63f7363818d91f3'},
 '768': {'title': 'NEMECKO - štipendiá Nadácie pruského kultúrneho dedičstva na '
                  'výskumné pobyty pre doktorandov a výskumných pracovníkov',
         'source_language': 'sk',
         'facts_sha256': 'dc771aa3ca64d47a7a8ed18847933ffbb40cec1368d901860bed861352802df4'},
 '769': {'title': 'NEMECKO - štipendiá Leibnizovho inštitútu európskych dejín pre '
                  'doktorandov a postdoktorandov',
         'source_language': 'sk',
         'facts_sha256': '4ff0dafaa5c682f2f57ceebdddcd41b3eac34b819b543e06cce6b20dd4c397ec'},
 '77': {'title': 'SLOVINSKO - štipendium na študijný alebo výskumný pobyt pre '
                 'študentov a doktorandov na základe medzivládnej bilaterálnej dohody',
        'source_language': 'sk',
        'facts_sha256': 'dd8292e2243af778a1f1d3077e6655cd32f1ecc3205cb7ffc2e78c10469be2fb'},
 '770': {'title': 'NEMECKO - štipendiá nadácie Klassik Stiftung Weimar pre výskumných '
                  'a kultúrnych pracovníkov, publicistov a umelcov',
         'source_language': 'sk',
         'facts_sha256': '4c5f64520e7f8937bd8975916bc387e486f7a127dc92ad4ae34880989bca1ee6'},
 '771': {'title': 'MEXIKO – Štipendium mexickej vlády na mobilitu na magisterskom '
                  'stupni',
         'source_language': 'sk',
         'facts_sha256': 'cbb0061a3f8f2a568f24e2cfecee33bb70d40d6084bd97427cbb6fc7b622dc49'},
 '774': {'title': 'Erasmus Mundus Joint Masters scholarships - štipendiá na '
                  'magisterské štúdium do rôznych krajín sveta',
         'source_language': 'sk',
         'facts_sha256': '96381635bbf3b1c15310c1092b01bcbb2ecfb443bf41b760fcc5c061068e4e15'},
 '775': {'title': 'NEMECKO - BAFöG: podpora pre študentov študujúcich v Nemecku',
         'source_language': 'sk',
         'facts_sha256': '46cad4183f0ea52eeee78549dec527dd937f4bdc5c691707de146f70522c8039'},
 '779': {'title': 'Medzinárodný vyšehradský fond – Vyšehradský program pre výskumných '
                  'pracovníkov',
         'source_language': 'sk',
         'facts_sha256': '90d265825425829732b99d898ea6f7b40a9495d7ed56b47b61e92e01da06603e'},
 '78': {'title': 'SLOVINSKO - štipendium na letnú školu a seminár slovinského jazyka '
                 'na základe medzivládnej bilaterálnej dohody',
        'source_language': 'sk',
        'facts_sha256': '97b59c421e8fcedede6bfa177eb064141f0492bec91f66692947671ba51ce755'},
 '781': {'title': 'TURECKO - KATÍP - 8- mesačný štipendijný program pre zamestnancov '
                  'štátnej správy, diplomatov, akademických a výskumných pracovníkov',
         'source_language': 'sk',
         'facts_sha256': 'dbf7fb8e8413339e6437b8064686a2dd8d259000b4161c9434f65de410e5734b'},
 '783': {'title': 'ČIERNA HORA - študijný alebo výskumný pobyt na základe medzivládnej '
                  'bilaterálnej dohody pre študentov a doktorandov',
         'source_language': 'sk',
         'facts_sha256': '5b6bea3b204f63308be55a89ccdafa299ca58cea273cd7364e8be878ae8b18e2'},
 '789': {'title': 'NEMECKO - štipendium DAAD na výskumný pobyt pre doktorandov a '
                  'postdoktorandov (2 - 12 mesiacov)',
         'source_language': 'sk',
         'facts_sha256': 'fdff0612bb95426e703b27105592b62f7a35fb354ca1e683ada67292c77d09b4'},
 '79': {'title': 'SRBSKO - študijný alebo výskumný pobyt pre študentov a doktorandov '
                 'na základe medzivládnej bilaterálnej dohody',
        'source_language': 'sk',
        'facts_sha256': 'f630bd9acd74f6c2fae9161c2e364ad3acf1ff85b08999ae6be426114619031d'},
 '790': {'title': 'NEMECKO - štipendium DAAD - DLR na výskumné pobyty v oblasti '
                  'aeronautiky a vesmíru',
         'source_language': 'sk',
         'facts_sha256': 'c89e6e94ba687459b4d61cee4b58c283d29a370a4e0a3008603845e99561f15a'},
 '792': {'title': 'Nadace pro rozvoj vzdělání - Ľehčí rozběh',
         'source_language': 'sk',
         'facts_sha256': 'c812e5bfaa8927d9cc2b8720cdf9fade13ae198ed9d6b749c1aa23979afc62b7'},
 '794': {'title': 'NEMECKO - Deutschlandstipendium - podpora pre študentov študujúcich '
                  'v Nemecku',
         'source_language': 'sk',
         'facts_sha256': 'b814b78fb09919d1784db4ce69533e01ca7ae946b1e5ddb9fcb6eee26fe5305b'},
 '796': {'title': 'NEMECKO - štipendiá na letný vzdelávací kurz pre učiteľov nemeckého '
                  'jazyka v Dillingen an der Donau',
         'source_language': 'sk',
         'facts_sha256': 'ff791d4845740e3008d5e296a3a9d367624609c4e60f63ec12e14b2c53e70bfc'},
 '797': {'title': 'NEMECKO - štipendiá na hospitačný pobyt v Bavorsku pre učiteľov '
                  'nemeckého jazyka na základných a stredných školách',
         'source_language': 'sk',
         'facts_sha256': 'ca14c58a964425124c109a55ce9b55871db9f9fa727d9296a2ce958330fa82f1'},
 '798': {'title': 'UKRAJINA - štipendium na celé vysokoškolské štúdium pre občanov SR '
                  'ukrajinskej národnosti na základe medzivládnej bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': 'd63de55c06dce88ee0dcd171ac8be09bf49db9e6768a99f108bc7f2501ca364f'},
 '799': {'title': 'CEEPUS - štipendium pre zamestnancov vysokých škôl do krajín '
                  'strednej a juhovýchodnej Európy',
         'source_language': 'sk',
         'facts_sha256': 'a7d0100692299d392622ae7e338ab1bd3c5ca0849cf3b6b7ba1ff2cad547923a'},
 '800': {'title': 'VEĽKÁ BRITÁNIA - UK Royal Society - mobility výskumných pracovníkov',
         'source_language': 'sk',
         'facts_sha256': 'ab1015c8d0b1a002ce06a7b3d7fc1fc0b72ea055fd708138a0ca950c15f885af'},
 '804': {'title': 'SLOVENSKO - štipendiá pre talentovaných domácich študentov na I. a '
                  'II. stupni vysokoškolského štúdia',
         'source_language': 'sk',
         'facts_sha256': '7089d91c6baa2a6c0a3ebfa715aa037a41913f3149823b2e0ec50fcadf78d2ce'},
 '806': {'title': 'ESTÓNSKO – granty pre výskumných a akademických pracovníkov',
         'source_language': 'sk',
         'facts_sha256': '05b9026c1ea548d4b5057595512e10bdb4e223f38eaa6428869a07cc6e926f1d'},
 '81': {'title': 'SRBSKO - štipendium na letné semináre srbského jazyka a kultúry v '
                 'Srbskej republike pre študentov, doktorandov a vysokoškolských '
                 'učiteľov na základe medzivládnej bilaterálnej dohody',
        'source_language': 'sk',
        'facts_sha256': '88fe1bace4ca20b86a8e927f1a8040b26a949114664161eba6e063dd5ecb9852'},
 '811': {'title': 'NEMECKO - štipendiá Herderovho inštitútu na výskum v oblasti '
                  'histórie strednej a východnej Európy',
         'source_language': 'sk',
         'facts_sha256': 'e09356c6f41657f10c91296dad35a8d1bfa7235645c10a645bf5276c4c93c952'},
 '812': {'title': 'NEMECKO - štipendiá pre doktorandov a výskumných pracovníkov v '
                  'knižnici Herzoga Augusta vo Wolfenbütteli',
         'source_language': 'sk',
         'facts_sha256': 'a7d3e81a8874aed507ff471e74095b6572a04e52e6d8b1329b8f38d94f20ef62'},
 '813': {'title': 'BELGICKO – Platené stáže „Consilium“ na Generálnom sekretariáte '
                  'Rady EÚ',
         'source_language': 'sk',
         'facts_sha256': 'f5838c5faaebca8706eb30a85f4c9b822861aa54dffe998f698f4f2af1da7ae5'},
 '815': {'title': 'GRÉCKO - krátkodobý prednáškový alebo výskumný pobyt (10 dní) na '
                  'základe medzivládnej bilaterálnej dohody',
         'source_language': 'sk',
         'facts_sha256': 'ba45ca8dd8890d54feba8f3b878f24d6ee076f046f5e38ec7ac9224458426a0c'},
 '817': {'title': 'FRANCÚZSKO – Štipendium France Excellence na bakalárske štúdium',
         'source_language': 'sk',
         'facts_sha256': '195c360bd78f75b7ea2bea8339f8e39b0a227aaacb786f03b7da4c604577bbb9'},
 '818': {'title': 'RAKÚSKO - štipendiá Clubu Alpbach Czechia & Slovakia na účasť na '
                  'seminároch Európskeho fóra Alpbach',
         'source_language': 'sk',
         'facts_sha256': '8376e3dcfc3751a65c2e4e8103168d0d1e5a6d480e9c32e4aa65a5949782e234'},
 '820': {'title': 'Chemistry Europe Travel Grant – podpora zahraničných mobilít pre '
                  'mladých chemikov',
         'source_language': 'sk',
         'facts_sha256': '12b37a816c0cf5998f01ab2cf82168bcd1d89d9692f46d4c2e86974419821cb5'},
 '821': {'title': 'Medzinárodný vyšehradský fond – V4 Gen Mini Granty',
         'source_language': 'sk',
         'facts_sha256': 'fd5a1be00ef7e78a7303d0b3d0b94754acacd9a4f0c9f88c428f8fad90d023e8'},
 '822': {'title': 'Medzinárodný vyšehradský fond – Štipendijný program Západný Balkán '
                  '– Vyšehradská skupina',
         'source_language': 'sk',
         'facts_sha256': '615875d65de0cfdcb000fb01756199c3dddb906fef11bb334fa084c20971286f'},
 '823': {'title': 'Medzinárodný vyšehradský fond – Výskumné granty v Historických '
                  'archívoch EÚ',
         'source_language': 'sk',
         'facts_sha256': 'a7423759a67f33be2c4c7faafa5592f22bef17e9278a122757469405db61c878'},
 '824': {'title': 'Medzinárodný vyšehradský fond – Štipendium v Open Society Archives',
         'source_language': 'sk',
         'facts_sha256': 'b69b18254deacb9f69300466db8924fe8fa4d9108b15255a07efab351f114a5a'},
 '825': {'title': 'Medzinárodný vyšehradský fond – Rezidenčný pobyt v oblasti módneho '
                  'dizajnu v Miláne',
         'source_language': 'sk',
         'facts_sha256': '8fb807e1bd4b8b0b501508902b55eb1f147c8939ab30fbac0b5cba93793ec78c'},
 '826': {'title': 'Medzinárodný vyšehradský fond – Literárny rezidenčný program',
         'source_language': 'sk',
         'facts_sha256': 'a29da80be159435058d60d003e479da930fc91693e374e9ba925994374e52238'},
 '827': {'title': 'Medzinárodný vyšehradský fond – Rezidenčný pobyt pre vizuálnych a '
                  'zvukových umelcov',
         'source_language': 'sk',
         'facts_sha256': '4ba6fd14e9c89a5e049090f6b64c05c4f2e186a7d3f73950948b20469cd9e0e8'},
 '828': {'title': 'FRANCÚZSKO – Štipendium Université Paris-Saclay na magisterské '
                  'štúdium',
         'source_language': 'sk',
         'facts_sha256': '0beed244e92828c674ddc4625c0d5add24a35fea47e7eff5f24a0440ebea1259'},
 '829': {'title': 'PORTUGALSKO – Štipendium Camões na ročný kurz portugalského jazyka '
                  'a kultúry',
         'source_language': 'sk',
         'facts_sha256': '257813eac3a3535da698ed156a31f5957456d92dce84603b18c8a40269179bc4'},
 '830': {'title': 'PORTUGALSKO – Štipendium Camões na letný kurz portugalského jazyka '
                  'a kultúry',
         'source_language': 'sk',
         'facts_sha256': 'aeade816811c72cb21de8a5ed6eba66893526f9643d5025b58be13ca0f9fd2de'},
 '831': {'title': 'PORTUGALSKO – Štipendium Fernão Mendes Pinto na odbornú prípravu v '
                  'oblasti výučby portugalčiny ako cudzieho jazyka',
         'source_language': 'sk',
         'facts_sha256': '8cb943985c3ea47678be0aa81cc67b4bbdb053f5653d10c8b58b865b30f51d37'},
 '832': {'title': 'PORTUGALSKO – Výskumné štipendium Camões na výskum, magisterské a '
                  'doktorandské štúdium v oblasti portugalského jazyka a kultúry',
         'source_language': 'sk',
         'facts_sha256': '7f8a38768d7ae26653063a1821ec28fcadd85d0f11eb65c3bced8cf27df36441'},
 '833': {'title': 'PORTUGALSKO – Štipendium Pessoa na vzdelávacie a výskumné projekty '
                  'v oblasti portugalského jazyka a kultúry',
         'source_language': 'sk',
         'facts_sha256': '09332977ed22ec62c01d4d002c40e8b877c9060a32c7559ea8708566a257935a'},
 '834': {'title': 'PORTUGALSKO – Štipendium Vieira na odbornú prípravu a '
                  'zdokonaľovanie v oblasti prekladu a konferenčného tlmočenia',
         'source_language': 'sk',
         'facts_sha256': 'cfc22ef6f4d305e2ef47f9ead6f8b225a728ce74ffa717f214ca9caf7fce5bb5'},
 '835': {'title': 'TALIANSKO – Štipendiá talianskych univerzít pre študentov, '
                  'doktorandov a absolventov vysokých škôl',
         'source_language': 'sk',
         'facts_sha256': 'ea2aaaf6d301699a746cf4b6bed7023af7a090a16081dce2d71cae5b8077d126'},
 '836': {'title': 'TALIANSKO – Štipendium talianskej vlády na štúdium operného spevu a '
                  'manažmentu scénických umení na Accademia Teatro alla Scala',
         'source_language': 'sk',
         'facts_sha256': 'c4051c6593dc89532590e061354ab80903b484038b557f7beb090e2286b3ba87'},
 '837': {'title': 'NEMECKO – štipendiá Nadácie Heinricha Bölla pre študentov a '
                  'doktorandov',
         'source_language': 'sk',
         'facts_sha256': '38f63192c79bc488463334372d60efb113d98be70686bcae48a7d35801b07b39'},
 '838': {'title': 'NEMECKO – štipendiá Nadácie Rosy Luxemburgovej pre študentov a '
                  'doktorandov',
         'source_language': 'sk',
         'facts_sha256': 'd06d4ffdb3f9476250fb5ea9135dc258cffa7e218bb64308a1ddd10e8273abbb'},
 '839': {'title': 'NEMECKO - štipendiá Nadácie Friedricha Naumanna na študijné a '
                  'výskumné pobyty',
         'source_language': 'sk',
         'facts_sha256': '58d9cf2e979fe44cfdd58dcdf525d4864c27ab442f31dd5a14bc748c2b0c0ca0'},
 '840': {'title': 'NEMECKO - štipendium Medzinárodnej knižnice pre mládež na výskum '
                  'detskej a mládežníckej literatúry a ilustrácie',
         'source_language': 'sk',
         'facts_sha256': 'ae17757f1365d79958473e708633bd8bfed585236fb3edf7a0afa37b50367908'},
 '841': {'title': 'ESTÓNSKO – štipendium na letné a zimné školy',
         'source_language': 'sk',
         'facts_sha256': '613b97256f9d77cffcfed33a589a1e094273136c280d7406c27a497b49562460'},
 '88': {'title': 'BELGICKO – Štipendium na letnú stáž didaktiky francúzskeho jazyka na '
                 'základe medzivládnej bilaterálnej dohody',
        'source_language': 'sk',
        'facts_sha256': 'b36f263b441b278619f51143792893cf534ebce4ed6ca72f9fd092e4df219e76'},
 '89': {'title': 'BELGICKO – Štipendium na letný kurz francúzskeho jazyka a literatúry '
                 'na základe medzivládnej bilaterálnej dohody',
        'source_language': 'sk',
        'facts_sha256': 'fabf71773e72ef3a150d3518fd076190059e221c4a0e4882b81e8f886df37eab'},
 '90': {'title': 'BELGICKO – Štipendium na letnú stáž francúzskeho jazyka v oblasti '
                 'medzinárodných vzťahov na základe medzivládnej bilaterálnej dohody',
        'source_language': 'sk',
        'facts_sha256': '75edb4bd66e7b80b46f80e64725a3150adf56bf083b63594347b4379a974ac7d'},
 '96': {'title': 'ČÍNA - ročný študijný pobyt na základe medzivládnej bilaterálnej '
                 'dohody',
        'source_language': 'sk',
        'facts_sha256': '5cc6b733fa5d6bb1b3cf040eb731e5acff3ed989d7f737392831614a8423d182'},
 '98': {'title': 'ČÍNA - ročný pobyt pre vedeckých a pedagogických pracovníkov '
                 'vysokých škôl na základe medzivládnej bilaterálnej dohody',
        'source_language': 'sk',
        'facts_sha256': '478c5311283476ab67cb600795923ca6038a2e078a7db3475188ae8825bc15f4'}}

STUDY_CONTENT_GUARDS = {'https://www.studyinslovakia.saia.sk/en/main/scholarships/scholarships-for-bachelor-study/': {'content_sha256': '906774be39839fe0bf4d9b7f204be7d2bb71cccc76e011990a8cfe99b6a93812'},
 'https://www.studyinslovakia.saia.sk/en/main/scholarships/scholarships-for-master-study/': {'content_sha256': 'c134ec813c5b8c31bfe6702f2c3506f07745a78e64b08e2a6162f269ff0a9959'},
 'https://www.studyinslovakia.saia.sk/en/main/scholarships/scholarships-for-phd-study/': {'content_sha256': '2c44a9a05d31adede83c7615faef2a95543187ede0d29420bf5e8f0955e42e1e'},
 'https://www.studyinslovakia.saia.sk/en/main/scholarships/scholarships-for-bachelor-study/full-time-bachelor-study': {'content_sha256': '923ae91b80dfc4fe02b2f1ba8449da784221915328193d091d7e52e54b2abe71'},
 'https://www.studyinslovakia.saia.sk/en/main/scholarships/scholarships-for-bachelor-study/bachelor-study-mobility': {'content_sha256': '6843a6cfcb92a80dfdcbc5a7937a098b90c7a01ad09b4c77447bb365fbdac9e9'},
 'https://www.studyinslovakia.saia.sk/en/main/scholarships/scholarships-for-master-study/full-master-study': {'content_sha256': '28ea31db09757ee59eb986d6e2500961c503c7d18014dbf6bb235c690cbfb11c'},
 'https://www.studyinslovakia.saia.sk/en/main/scholarships/scholarships-for-master-study/master-study-stay-(short-term-mobility)': {'content_sha256': '6c3881929d02e05cfdc16d247c4459ab0cc9c9e094c24c95529b89db18a876b4'},
 'https://www.studyinslovakia.saia.sk/en/main/scholarships/scholarships-for-phd-study/full-time-phd-study': {'content_sha256': '5f89e7a9ae1e92847e829e9cc0b7911dc33c40c2764295803b74a745f22dba18'},
 'https://www.studyinslovakia.saia.sk/en/main/scholarships/scholarships-for-phd-study/phd-study-stay-(short-term-mobility)': {'content_sha256': '2fed9ffe942cf28254bd6a5b5f54957906de6b22dad28743e8b4710e1274af31'}}

# Original factual summaries, not copied publisher descriptions.
PROGRAMME_PROFILES = {'101': {'categories': ['scholarships'],
         'evidence': ["Target during the stay: bachelor's students, master's students. "
                      'Sending-country/institutional affiliation and the actual programme '
                      'conditions are distinct from a universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['EG'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility to Egypt for bachelor's students, master's students: 3–5 "
                    'months. EUR 485/month from Slovakia plus EGP 500/month from Egypt; tuition is '
                    'waived, but accommodation and meals are charged. Slovak-funded travel '
                    'requires the stated sending-sector affiliation.'},
 '103': {'categories': ['scholarships'],
         'evidence': ['Target during the stay: doctoral students. Sending-country/institutional '
                      'affiliation and the actual programme conditions are distinct from a '
                      'universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['EG'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility to Egypt for doctoral students: 3–5 months. EUR 702/month '
                    'from Slovakia plus EGP 600/month from Egypt; tuition is waived, but '
                    'accommodation and meals are charged. Slovak-funded travel requires the stated '
                    'sending-sector affiliation.'},
 '105': {'categories': ['scholarships'],
         'evidence': ['Target during the stay: university teachers, researchers. '
                      'Sending-country/institutional affiliation and the actual programme '
                      'conditions are distinct from a universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['EG'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility to Egypt for university teachers, researchers: up to 10 '
                    'days. Published support combines a Slovak monthly supplement of EUR 702 and '
                    'Egyptian support of EGP 600/month; accommodation and meals are arranged for a '
                    'fee. Slovak-funded travel requires the stated sending-sector affiliation.'},
 '106': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['CZ', 'SK'],
         'kind': 'programme-overview',
         'summary': 'Lehčí rozběh supports Slovak first-year students in Czechia and Czech '
                    'first-year students in Slovakia. Published support is CZK 10,000–20,000 for '
                    'one academic year, split between semesters; the second payment requires proof '
                    'of continuing study. The two SAIA catalogue descriptions of this same '
                    'foundation programme retain reciprocal eligibility rather than a universal '
                    'Slovakia-only host rule.'},
 '108': {'categories': ['scholarships'],
         'evidence': ['Target during the stay: university teachers, researchers. '
                      'Sending-country/institutional affiliation and the actual programme '
                      'conditions are distinct from a universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['EG'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility to Egypt for university teachers, researchers: 2–6 months. '
                    'Published support combines a Slovak monthly supplement of EUR 702 and '
                    'Egyptian support of EGP 600/month; accommodation and meals are arranged for a '
                    'fee. Slovak-funded travel requires the stated sending-sector affiliation.'},
 '109': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['CZ'],
         'kind': 'programme-overview',
         'summary': 'CERGE-EI offers postgraduate economics/econometrics study in Prague with '
                    'English as the working language. Most admitted students may receive a tuition '
                    'waiver and stipend without a separate scholarship application. The source '
                    "describes an initial master's-level course phase followed by doctoral "
                    'research; its duration descriptions vary, so a guaranteed total funding '
                    'period is not inferred. Admission and academic conditions apply.'},
 '110': {'categories': ['scholarships'],
         'evidence': ["Target during the stay: bachelor's students, master's students, doctoral "
                      'students, university teachers. Sending-country/institutional affiliation '
                      'and the actual programme conditions are distinct from a universal '
                      'Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['EG'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility to Egypt for bachelor's students, master's students, "
                    'doctoral students, university teachers: one month. The host covers tuition, '
                    'dormitory accommodation, meals and excursion travel. Slovak-funded travel '
                    'requires the stated sending-sector affiliation.'},
 '112': {'categories': ['scholarships'],
         'evidence': ['Target during the stay: doctoral students, university teachers, '
                      'researchers. Sending-country/institutional affiliation and the actual '
                      'programme conditions are distinct from a universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['GR'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility to Greece for doctoral students, university teachers, '
                    'researchers: 5 or 10 months. EUR 450/month, a location-dependent EUR 500–550 '
                    'settling allowance and conditional EUR 150 domestic research travel; '
                    "fee-charging study programmes remain the participant's cost. Slovak-funded "
                    'travel requires the stated sending-sector affiliation.'},
 '114': {'categories': ['scholarships'],
         'evidence': ["Target during the stay: bachelor's students, master's students, doctoral "
                      'students, university teachers, researchers. Sending-country/institutional '
                      'affiliation and the actual programme conditions are distinct from a '
                      'universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['GR'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility to Greece for bachelor's students, master's students, "
                    'doctoral students, university teachers, researchers: one month. The host '
                    'covers the stay costs; applicants must meet the published prior-study, fixed '
                    'birth-date and prior-Greek-government-award conditions. Slovak-funded travel '
                    'requires the stated sending-sector affiliation.'},
 '115': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'Outgoing CEEPUS students and doctoral students can undertake eligible network '
                    'or freemover mobility, with scholarship amounts determined by the host '
                    'country. Students need two completed semesters and eligible full-time '
                    'enrolment; semester stays normally last 3–10 months and thesis visits 1–3 '
                    'months. Citizenship/equivalent-status, minimum attendance and degree-level '
                    'cumulative limits apply. Incoming Slovak rates are not outgoing rates.'},
 '116': {'categories': ['scholarships'],
         'evidence': ["Target during the stay: bachelor's students, master's students. "
                      'Sending-country/institutional affiliation and the actual programme '
                      'conditions are distinct from a universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['RO'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility to Romania for bachelor's students, master's students: 5 "
                    'or 10 months. EUR 530/month from Slovakia plus Romanian monthly support of '
                    "EUR 65 for bachelor's or EUR 75 for master's students. Slovak-funded travel "
                    'requires the stated sending-sector affiliation.'},
 '117': {'categories': ['scholarships'],
         'evidence': ['Target during the stay: doctoral students, university teachers, '
                      'researchers. Sending-country/institutional affiliation and the actual '
                      'programme conditions are distinct from a universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['RO'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility to Romania for doctoral students, university teachers, '
                    'researchers: 1–10 months. Doctoral students receive a Slovak EUR 615/month '
                    'supplement and Romanian EUR 85/month; academic/research staff receive EUR 725 '
                    'plus EUR 75/month. Slovak-funded travel requires the stated sending-sector '
                    'affiliation.'},
 '118': {'categories': ['scholarships'],
         'evidence': ["Target during the stay: bachelor's students, master's students, doctoral "
                      'students, university teachers, researchers. Sending-country/institutional '
                      'affiliation and the actual programme conditions are distinct from a '
                      'universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['RO'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility to Romania for bachelor's students, master's students, "
                    'doctoral students, university teachers, researchers: 3–4 weeks. The host '
                    'covers meals, accommodation and transport within Romania. Slovak-funded '
                    'travel requires the stated sending-sector affiliation.'},
 '121': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['EE'],
         'kind': 'programme-overview',
         'summary': 'Estophilus supports foreign students and researchers working on '
                    'Estonia-related topics for one to five months. Published support is EUR '
                    '660/month plus return travel. The proposed work must fit the programme and '
                    'host requirements; Estonian-language knowledge is an advantage. This '
                    'research-topic award is separate from an unrestricted grant to study any '
                    'subject in Estonia.'},
 '127': {'categories': ['scholarships'],
         'evidence': ["Target during the stay: bachelor's students, master's students. "
                      'Sending-country/institutional affiliation and the actual programme '
                      'conditions are distinct from a universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['HU'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility to Hungary for bachelor's students, master's students: 5 "
                    'or 10 months. Published Slovak support is EUR 500/month for '
                    "bachelor's/master's students. Slovak-funded travel requires the stated "
                    'sending-sector affiliation.'},
 '128': {'categories': ['scholarships'],
         'evidence': ['Target during the stay: doctoral students. Sending-country/institutional '
                      'affiliation and the actual programme conditions are distinct from a '
                      'universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['HU'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility to Hungary for doctoral students: 1–10 months. Published '
                    'Slovak support is EUR 600/month for doctoral students. Slovak-funded travel '
                    'requires the stated sending-sector affiliation.'},
 '131': {'categories': ['scholarships'],
         'evidence': ['Target during the stay: university teachers, researchers. '
                      'Sending-country/institutional affiliation and the actual programme '
                      'conditions are distinct from a universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['HU'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility to Hungary for university teachers, researchers: 1–3 '
                    'months. Hungarian support is HUF 80,000/month plus HUF 70,000 housing without '
                    'a PhD, or HUF 120,000 plus HUF 80,000 housing for postdoctoral researchers. '
                    'Slovak-funded travel requires the stated sending-sector affiliation.'},
 '133': {'categories': ['scholarships'],
         'evidence': ['Target during the stay: university teachers, researchers. '
                      'Sending-country/institutional affiliation and the actual programme '
                      'conditions are distinct from a universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['HU'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility to Hungary for university teachers, researchers: 5–20 '
                    'days. Hungarian support depends on PhD status and whether the stay exceeds 15 '
                    'days: HUF 75,000/150,000 without a PhD or HUF 100,000/200,000 for '
                    'postdoctoral researchers. Slovak-funded travel requires the stated '
                    'sending-sector affiliation.'},
 '134': {'categories': ['scholarships'],
         'evidence': ["Target during the stay: bachelor's students, master's students, doctoral "
                      'students, university teachers, researchers, other specified applicants. '
                      'Sending-country/institutional affiliation and the actual programme '
                      'conditions are distinct from a universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['HU'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility to Hungary for bachelor's students, master's students, "
                    'doctoral students, university teachers, researchers, other specified '
                    'applicants: 2 or 4 weeks. The host covers the course participation fee, '
                    'study, excursions, accommodation and meals. Slovak-funded travel requires the '
                    'stated sending-sector affiliation.'},
 '140': {'categories': ['scholarships', 'training'],
         'evidence': [],
         'host_countries': ['NL'],
         'kind': 'programme-overview',
         'summary': "The Hague Academy's three-week public/private international-law courses can "
                    'support eligible advanced law students, graduates and doctoral students up to '
                    'age 30. The source requires at least eight completed law-study semesters and '
                    'English or French. Published support is EUR 600 plus EUR 250 travel. Course '
                    'admission and the specific scholarship-selection conditions apply.'},
 '141': {'categories': ['internships'],
         'evidence': [],
         'host_countries': ['NL'],
         'kind': 'programme-overview',
         'summary': 'The International Criminal Court offers three-to-six-month internships in The '
                    'Hague for candidates with strong academic records. Applicants choose among '
                    'actual published roles; eligibility and financial support vary by position. '
                    'The SAIA overview does not guarantee a paid placement or a universal '
                    "scholarship amount. Candidates must satisfy the selected role's academic, "
                    'language and application requirements.'},
 '142': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': "Walsh Scholars support research-based master's study up to two years or PhDs "
                    'up to four years in projects aligned with Teagasc priorities. Published '
                    'support is EUR 25,000/year, with conditional tuition support up to EUR '
                    '6,000/year. Academic staff and Teagasc researchers first propose projects; '
                    'students apply to the resulting advertised places. This is not an '
                    'unrestricted direct award for any taught postgraduate course.'},
 '146': {'categories': ['scholarships', 'training'],
         'evidence': [],
         'host_countries': ['IN'],
         'kind': 'programme-overview',
         'summary': 'The Central Institute of Hindi in Agra offers a one-year Hindi course for '
                    'applicants aged 21–35 with 12 years of schooling, English and A2 Hindi. '
                    'Published support is INR 3,500/month, a one-off INR 1,000 book allowance, '
                    'return airfare and airport transfer. Hostel residence is compulsory and costs '
                    'INR 250/month; vegetarian food is served. Admission and programme eligibility '
                    'apply.'},
 '147': {'categories': ['scholarships', 'training'],
         'evidence': [],
         'host_countries': ['IN'],
         'kind': 'programme-overview',
         'summary': 'ITEC funds eligible professional courses in India for degree-holding '
                    "professionals aged 25–45 with five years' experience, active English and "
                    'medical fitness. Covered items include return travel, tuition, accommodation, '
                    'medical support, pocket money, books and study visits. Applicants must select '
                    'a course actually included in ITEC; electronic and in-person routes have '
                    'different arrangements. Course duration is at least two weeks.'},
 '148': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['IS'],
         'kind': 'programme-overview',
         'summary': 'Icelandic-as-a-second-language scholarships support a nine-month university '
                    'course, potentially renewable. Registration and a monthly living/dormitory '
                    'stipend are covered; the source gives no fixed amount. Eligible '
                    'language/literature students normally need prior university study, English '
                    'and the stated age limit 35; Icelandic/Nordic-language students are '
                    'preferred. Applicants must also apply to the university; maximum award '
                    'frequency applies.'},
 '151': {'categories': ['scholarships'],
         'evidence': ["Target during the stay: bachelor's students, master's students. "
                      'Sending-country/institutional affiliation and the actual programme '
                      'conditions are distinct from a universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['BG'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility to Bulgaria for bachelor's students, master's students: "
                    'five months. The source quotes a Slovak EUR 377/month supplement plus '
                    'Bulgarian EUR 123/month, tuition waiver and help arranging housing/meals, '
                    'explicitly warning that the 2025 rates may change. Slovak-funded travel '
                    'requires the stated sending-sector affiliation.'},
 '152': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['JP'],
         'kind': 'programme-overview',
         'summary': "Japan's undergraduate MEXT route supports eligible Slovak secondary-school "
                    'graduates born after April 2,2002, with 12 years of schooling and willingness '
                    'to study in Japanese. The 2027-start pathway normally lasts five years '
                    'including preparation, with designated longer-degree exceptions. Published '
                    'support is JPY 117,000/month, tuition and economy airfare. Embassy tests, '
                    'health checks and subject restrictions apply.'},
 '156': {'categories': ['scholarships'],
         'evidence': ['Target during the stay: doctoral students, university teachers, '
                      'researchers. Sending-country/institutional affiliation and the actual '
                      'programme conditions are distinct from a universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['BG'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility to Bulgaria for doctoral students, university teachers, '
                    'researchers: 1–10 months. The source quotes Bulgarian EUR 627/month plus a '
                    'Slovak supplement of EUR 0 for doctoral students or EUR 73 for staff; tuition '
                    'is waived. The stated 2025 rates may change. Slovak-funded travel requires '
                    'the stated sending-sector affiliation.'},
 '158': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['US'],
         'kind': 'programme-overview',
         'summary': 'Fulbright Slovak Student grants fund six-to-nine-month U.S. research stays '
                    "for Slovak citizens with a completed master's degree, including doctoral "
                    'candidates and suitable independent creative researchers. Strong '
                    'academic/professional results and English are required; patient-contact '
                    'clinical medicine is excluded. Support includes location-dependent living '
                    'costs, airfare, J-1 assistance and exchange health benefits. Dependants '
                    'receive visa help, not funding.'},
 '160': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['US'],
         'kind': 'programme-overview',
         'summary': 'Fulbright Slovak Scholar grants fund three-to-six-month U.S. '
                    'research/teaching stays for Slovak citizens with a PhD-equivalent academic '
                    'background or extensive nonacademic professional experience. Strong relevant '
                    'achievements and English are required; patient-contact projects are excluded. '
                    'Support includes location-dependent living costs, return airfare, visa/health '
                    'benefits and specified conditional family/material allowances.'},
 '161': {'categories': ['scholarships'],
         'evidence': ["Target during the stay: bachelor's students, master's students, doctoral "
                      'students, university teachers, researchers. Sending-country/institutional '
                      'affiliation and the actual programme conditions are distinct from a '
                      'universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['BG'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility to Bulgaria for bachelor's students, master's students, "
                    'doctoral students, university teachers, researchers: one month. The host '
                    'covers accommodation, meals, cultural activities and domestic transport. '
                    'Slovak-funded travel requires the stated sending-sector affiliation.'},
 '163': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['JP'],
         'kind': 'programme-overview',
         'summary': "MEXT research-student support in Japan can lead into master's/doctoral study. "
                    'Eligible Slovak applicants need the appropriate degree, English, willingness '
                    'to learn Japanese, medical fitness and the fixed post-April 2,1992 birth-date '
                    'condition for the 2027 start. Published monthly support is JPY '
                    "143,000/144,000/145,000 by research/master's/doctoral track, plus tuition and "
                    'airfare. Embassy selection and host acceptance apply.'},
 '165': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['AT'],
         'kind': 'programme-overview',
         'summary': 'Ernst Mach supports doctoral, postdoctoral and eligible recent-graduate '
                    'researchers from all countries on one-to-nine-month stays in Austria. '
                    'Published support is EUR 1,300/month; housing, insurance and other costs come '
                    'from that amount. Applicants need strong German or English, must meet the age '
                    '35 limit and cannot have studied/researched in Austria during the preceding '
                    'six months. Host and research requirements apply.'},
 '166': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['AT'],
         'kind': 'programme-overview',
         'summary': 'Franz Werfel supports young university teachers researching German '
                    'language/Austrian literature in Austria. Published support is EUR 2,500/month '
                    'for 4–9 months, extendable up to 18 months under programme rules. Prior '
                    'contact with an Austrian host researcher is required; recent Austrian '
                    'study/research exclusions apply. Housing, insurance and other costs are paid '
                    'from the stipend rather than promised as additional benefits.'},
 '167': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['AT'],
         'kind': 'programme-overview',
         'status': 'expired',
         'summary': 'Richard Plaschka supported postdoctoral Austria-related '
                    'historical/historiographical research at EUR 2,500/month for 4–12 months, '
                    'conditionally extendable to 18 months. The source explicitly says September '
                    '15,2026 was the final closing and the programme is then suspended. Host '
                    'contact and recent-Austria exclusions applied; housing and insurance were '
                    'paid from the stipend. This catalogue overview is not a currently open call.'},
 '168': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['KR'],
         'kind': 'programme-overview',
         'summary': "GKS funds eligible master's/doctoral study in Korea through the embassy or "
                    'university route. Applicants need the preceding degree, the stated under 40 '
                    'age condition, Korean/English and medical fitness; former GKS students are '
                    'excluded from reapplying under the updated rule. Published support includes '
                    'KRW 900,000/month, tuition, language preparation, travel and conditional '
                    'research/thesis/insurance support. The 2026 embassy closing is past.'},
 '171': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['US'],
         'kind': 'programme-overview',
         'summary': 'Humphrey offers a ten-month nondegree U.S. professional-development '
                    "fellowship for Slovak citizens with at least a bachelor's degree, five years' "
                    'full-time experience, leadership/community-service evidence and English. '
                    'Covered items include host tuition/fees, living support, health benefits, '
                    'airfare, books/computer and professional activities. A roughly six-week '
                    'professional affiliation is part of the programme; it does not award an '
                    'academic degree.'},
 '172': {'categories': ['fellowships', 'training'],
         'evidence': [],
         'host_countries': ['US'],
         'kind': 'programme-overview',
         'summary': 'SUSI Scholars are intensive five-to-six-week U.S. academic programmes for '
                    'eligible foreign university teachers and researchers working on U.S. '
                    'society/institutions. Published support covers international/domestic travel, '
                    'accommodation, meals, basic health insurance, books and programme expenses. '
                    'Selection depends on the thematic institute and professional profile. This '
                    'educator/scholar route is distinct from the undergraduate SUSI programme.'},
 '176': {'categories': ['grants'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'Visegrad performing-arts residencies support professional V4 artists aged 18+ '
                    'developing new work abroad. Individual support is EUR 2,500 for the artist '
                    'plus EUR 2,000 for the host; group support is EUR 5,500 plus EUR 3,500 for '
                    'the host. The residency country must differ from citizenship and permanent '
                    'residence. Finished performances, festivals and established theatres are '
                    'excluded; reporting and one-award-per-year rules apply.'},
 '178': {'categories': ['scholarships'],
         'evidence': ["Target during the stay: master's students. Sending-country/institutional "
                      'affiliation and the actual programme conditions are distinct from a '
                      'universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['KZ'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility to Kazakhstan for master's students: 3–10 months. "
                    'Published support is EUR 262/month from Slovakia and KZT 86,987/month from '
                    'Kazakhstan, with tuition waived and housing on local-student terms. '
                    'Slovak-funded travel requires the stated sending-sector affiliation.'},
 '180': {'categories': ['scholarships'],
         'evidence': ['Target during the stay: doctoral students. Sending-country/institutional '
                      'affiliation and the actual programme conditions are distinct from a '
                      'universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['KZ'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility to Kazakhstan for doctoral students: 3–10 months. The '
                    'source states Slovak support of EUR 166 by doctoral year and Kazakh support '
                    'of KZT 195,000, without consistently explicit payment periods; research is '
                    'free and local housing terms apply. Slovak-funded travel requires the stated '
                    'sending-sector affiliation.'},
 '181': {'categories': ['competitions', 'grants'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'The Ignaz L. Lieben Prize awards USD 36,000 for outstanding '
                    'molecular-biology, chemistry or physics research. Candidates need a PhD, must '
                    'meet the age 40 limit (44 with the specified parental-care interruption) and '
                    'have worked continuously for three years in an eligible listed country. Full '
                    'academy members are excluded; nomination and merit rules apply. This prize is '
                    'not a mobility stipend tied only to Austrian citizenship.'},
 '183': {'categories': ['scholarships', 'training'],
         'evidence': [],
         'host_countries': ['AT'],
         'kind': 'programme-overview',
         'summary': 'European Forum Alpbach scholarships cover the participation fee and '
                    'accommodation for eligible first-time scholars aged 18–30 with B2 English. At '
                    "least 11 days' participation is required in the roughly two-week programme. "
                    'Additional meal, travel or special-needs assistance may be requested when '
                    'justified; it is not automatically guaranteed. Selection and attendance '
                    'conditions apply.'},
 '187': {'categories': ['scholarships'],
         'evidence': ['Target during the stay: doctoral students, university teachers, '
                      'researchers. Sending-country/institutional affiliation and the actual '
                      'programme conditions are distinct from a universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['MD'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility to Moldova for doctoral students, university teachers, '
                    'researchers: 3–10 months. Slovak doctoral supplements vary by year (EUR '
                    '505/496/487 monthly), with Moldovan LEI 1,330/1,470/1,520 monthly; staff '
                    'supplement EUR 700/month. Tuition is waived; housing follows local terms. '
                    'Slovak-funded travel requires the stated sending-sector affiliation.'},
 '189': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': "DAAD's Berlin Artists programme supports eligible professional artists for 12 "
                    'months, or filmmakers for six months, with a stipend and published travel, '
                    'health, workspace and language-course support. Visual artists cannot apply '
                    'individually: their awards are by invitation. Other disciplines follow their '
                    'actual application procedures. The yearless December guidance does not '
                    'establish a dated current call.'},
 '190': {'categories': ['internships', 'scholarships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': "The Bundestag's five-month International Parliamentary Scholarship runs "
                    'March–July. Eligible Slovak citizens need a university degree, at least B2 '
                    'German, political/social engagement and the stated age 30 limit at the '
                    'programme start. Published support includes EUR 700/month, free '
                    'accommodation, travel and insurance/administrative costs. Academic '
                    'participation accompanies parliamentary work; admission and programme '
                    'selection apply.'},
 '191': {'categories': ['grants'],
         'evidence': [],
         'host_countries': ['DE', 'SK'],
         'kind': 'institutional-grant',
         'summary': 'The Slovak–German DAAD exchange programme supports mobility within jointly '
                    'designed research projects involving institutions in both countries. Only '
                    'relevant travel and stay costs are eligible; salaries, core research and '
                    'materials need other funding. Projects normally run two years, with '
                    'second-year support depending on results. Academic/research teams apply under '
                    'the joint programme; no universal personal grant amount is stated.'},
 '192': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': "Straelen's European Translators' College supports literary translators "
                    'working from or into German or other languages. The source requires two '
                    'published book translations and a publishing contract for the proposed work. '
                    'Scholarship duration and amount are not fixed in the catalogue. Applicants '
                    'must meet the actual residency/project selection conditions; this is a '
                    'professional literary-translation programme rather than a general language '
                    'course.'},
 '193': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': "KAAD's Eastern Europe programme supports eligible Catholic applicants, with "
                    'limited recommended Christian exceptions, on German study/research stays. '
                    "Tracks include master's, doctoral, qualification research and short "
                    'university-teacher visits with different maximum durations. Theology is '
                    'excluded. Partner recommendation and programme-specific academic/language '
                    'conditions apply; the source does not state a fixed scholarship amount.'},
 '195': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'Humboldt research fellowships fund eligible postdocs within four years of '
                    'their PhD at EUR 3,000/month for 6–24 months, or experienced researchers '
                    'within 12 years at EUR 3,600/month for 6–18 months. Host acceptance, '
                    'publications, language and six-in-18-month prior-German-residence limits '
                    'apply; completing doctoral candidates have a specified exception. Family, '
                    'insurance, travel and language extras are conditional.'},
 '196': {'categories': ['scholarships', 'internships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': "Copernicus supports eligible bachelor's/master's students on a Hamburg "
                    'semester followed by a compulsory two-to-three-month internship. Support '
                    'includes housing, meals, pocket money, insurance and travel. Applicants need '
                    'at least one completed semester, B2–C1 German and continued study after the '
                    'visit; prior German-study/residence exclusions apply. It covers specified '
                    'disciplines and eligible regional applicants, not only Slovak citizenship.'},
 '197': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'Hermann Niermann scholarships support eligible German-studies applicants on '
                    "five distinct student, master's, doctoral and academic/research routes in "
                    'Würzburg. Rates range from EUR 950/month for specified student stays through '
                    'EUR 1,350 for doctoral visits. The amount field caps support at EUR 2,225, '
                    'while the staff description says EUR 2,250, so the conflicting maximum is not '
                    'presented as settled. Academic and eligible-country conditions apply.'},
 '201': {'categories': ['scholarships'],
         'evidence': ["Target during the stay: bachelor's students, master's students. "
                      'Sending-country/institutional affiliation and the actual programme '
                      'conditions are distinct from a universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['UA'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility to Ukraine for bachelor's students, master's students: "
                    '1–10 months. A Slovak EUR 298/month supplement and tuition waiver apply; '
                    'Ukraine offers either UAH 2,000/month or free dormitory accommodation. '
                    'Slovak-funded travel requires the stated sending-sector affiliation.'},
 '204': {'categories': ['scholarships'],
         'evidence': ['Target during the stay: doctoral students, university teachers, '
                      'researchers. Sending-country/institutional affiliation and the actual '
                      'programme conditions are distinct from a universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['UA'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility to Ukraine for doctoral students, university teachers, '
                    'researchers: 1–10 months. Doctoral support pairs Slovak EUR 262/month with '
                    'UAH 7,264/month; staff support pairs EUR 146 with UAH 11,960/month. Free '
                    'dormitory housing is the stated alternative to Ukrainian cash support. '
                    'Slovak-funded travel requires the stated sending-sector affiliation.'},
 '205': {'categories': ['scholarships'],
         'evidence': ["Target during the stay: bachelor's students, master's students, doctoral "
                      'students, university teachers, researchers. Sending-country/institutional '
                      'affiliation and the actual programme conditions are distinct from a '
                      'universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['UA'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility to Ukraine for bachelor's students, master's students, "
                    'doctoral students, university teachers, researchers: 21 days. The host covers '
                    'tuition, meals, accommodation and pocket money. Slovak-funded travel requires '
                    'the stated sending-sector affiliation.'},
 '210': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': "UWC's 2027–29 IB selection is for eligible secondary-school students who will "
                    'be 16–18 on September 1,2027, with Slovak citizenship or permanent residence. '
                    'Prior school-year completion, English, academic/conduct and '
                    'previous-application limits apply. Funding is needs-based and depends on the '
                    'allocated place: partial, full or comprehensive support is possible, rather '
                    'than guaranteed full funding for everyone.'},
 '211': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'U.S. Steel Košice scholarships provide up to EUR 2,000 per academic year in '
                    "two installments for eligible full-time bachelor's/master's students up to "
                    '26. Nonemployee families face technical-field, Košice-region, grade and '
                    "income conditions; employees' children have a separate eligibility route with "
                    'parental-employment requirements. Amounts vary with study location and '
                    'renewal requires a fresh application and continued eligibility.'},
 '212': {'categories': ['internships'],
         'evidence': [],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'USSTÁŽ offers eligible university students a paid professional internship at '
                    'U.S. Steel Košice, including workplace experience and communication, '
                    'presentation and teamwork training. Pay depends on the actual placement; the '
                    'catalogue does not state a universal remuneration rate. Candidates must meet '
                    "the advertised role's student, academic and selection requirements. This "
                    "internship is separate from the company's student scholarship."},
 '214': {'categories': ['other'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'The Slovak Education Support Fund offers repayable loans, not scholarships. '
                    'Published student loans range from EUR 500 to EUR 2,500 for '
                    "bachelor's/master's study or EUR 5,000 for doctoral study; eligible beginning "
                    'teachers can borrow EUR 1,000–15,000. The actual loan conditions, '
                    'eligibility, interest/repayment obligations and selection rules must be '
                    'checked; these amounts are not nonrepayable personal awards.'},
 '215': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['IT'],
         'kind': 'programme-overview',
         'summary': "MAECI supports eligible Italian master's, doctoral, arts/music/dance, "
                    'supervised-research and language/culture study tracks. Published support is '
                    'EUR 10,800 paid in installments, conditional tuition exemption at '
                    'participating institutions and health/accident insurance during the award. '
                    'Regional and other administrative fees generally remain payable. Admission, '
                    'age/language and track-specific conditions apply; support is not a universal '
                    'fee-free degree offer.'},
 '222': {'categories': ['scholarships'],
         'evidence': ["Target during the stay: bachelor's students, master's students. "
                      'Sending-country/institutional affiliation and the actual programme '
                      'conditions are distinct from a universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['PL'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility to Poland for bachelor's students, master's students: one "
                    'semester. Published Slovak support is EUR 550/month with tuition waived; '
                    'accommodation and meals are charged. Slovak-funded travel requires the stated '
                    'sending-sector affiliation.'},
 '223': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': "Visegrad scholarships support eligible cross-border master's/post-master's "
                    'study or research for one or two semesters. Published support is EUR '
                    '3,500/semester to the scholar plus EUR 2,000 to the host. Citizenship, '
                    'permanent-residence and home-university countries must differ from the host; '
                    'distances must exceed 150 km and prior host-country degrees are excluded. '
                    "Older bachelor's boilerplate conflicts with the stated current postgraduate "
                    'focus.'},
 '227': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': "DAAD music awards support eligible graduates on German master's/postgraduate "
                    'study up to 24 months or a one-year nondegree deepening course. Published '
                    'support is EUR 992/month with specified travel, insurance and study '
                    'allowances; conditional family/rent/accessibility/language extras apply. '
                    'Tuition is generally not covered. Host admission is a separate process; '
                    'recent-degree, prior-German-residence and study-location limits apply.'},
 '234': {'categories': ['grants'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'Literárny fond supports eligible scientific/technical writers and researchers '
                    'on creative overseas travel. Standard assistance is for travel tickets; '
                    'exceptional support for applicants under 35 can reach EUR 850 and include '
                    'stay costs for the relevant event or study/work visit. Published amounts '
                    'range from EUR 35 to EUR 850. Payment follows return, reporting and '
                    'documented settlement, rather than an unrestricted advance award.'},
 '243': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['US', 'GB'],
         'kind': 'programme-overview',
         'summary': 'The Secondary School Scholarship Program offers distinct U.S./UK school '
                    'routes for eligible students studying in Slovakia, including ASSIST, Davis, '
                    'Kemper and HMC. English and the stated grade standard apply; individual '
                    'routes impose different age, citizenship and income rules. School support can '
                    'cover tuition, housing and meals, but family fees, travel, visa, insurance '
                    'and other costs may remain. Published maximum values are award costs, not '
                    'cash.'},
 '251': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['IS'],
         'kind': 'programme-overview',
         'summary': 'Snorri Sturluson fellowships support eligible writers, translators and '
                    'humanities researchers developing Icelandic-language/culture work in Iceland '
                    'for at least three months. Published support covers return airfare and living '
                    'costs; no fixed stipend amount is supplied. Applicants provide a detailed '
                    'proposed stay, background and publication record. The usual December '
                    'application guidance has no stated year and does not establish a dated call.'},
 '256': {'categories': ['fellowships', 'training'],
         'evidence': [],
         'host_countries': ['NL'],
         'kind': 'programme-overview',
         'summary': 'The UN international-law fellowship course in the Netherlands supports '
                    'eligible law graduates aged 24–45 with relevant experience. Published support '
                    'covers programme participation, study materials, registration, accommodation, '
                    'meals, travel and insurance. The catalogue explicitly gives French as the '
                    'language for 2023, so that historic language detail is not asserted as a '
                    'verified current requirement. The actual current course conditions govern '
                    'selection.'},
 '259': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['BE', 'PL'],
         'kind': 'programme-overview',
         'summary': 'College of Europe offers a ten-month postgraduate European-studies programme '
                    'in Bruges or Natolin. Merit/admission-based full or partial scholarships may '
                    'cover study, housing and meals; roughly 70% receive some support. The 2026/27 '
                    'listed totals of EUR 30,000 in Bruges and EUR 29,000 in Natolin are study '
                    "costs, not guaranteed scholarship cash. Programme admission and each funder's "
                    'selection conditions apply.'},
 '265': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['CA'],
         'kind': 'programme-overview',
         'summary': "Canada's doctoral research scholarship overview states CAD 40,000/year for "
                    'three years. International applicants must already be enrolled at an eligible '
                    'Canadian doctoral institution by the application closing. The route through '
                    'an institution or directly through the appropriate research council depends '
                    'on enrolment and institutional quotas. Research-field, academic and '
                    'agency-specific requirements apply; overseas nationality alone is '
                    'insufficient.'},
 '270': {'categories': ['scholarships', 'training'],
         'evidence': [],
         'host_countries': ['NO'],
         'kind': 'programme-overview',
         'summary': "Oslo's International Summer School offers four-to-six-week courses with "
                    'competitively available scholarships. Published support covers the course, '
                    'accommodation and meals; pocket money and a travel grant are explicitly '
                    'excluded. Applicants first submit the actual preliminary application and '
                    'shortlisted candidates receive further instructions. Course admission, '
                    'academic and language conditions apply; the catalogue does not promise every '
                    'applicant funding.'},
 '271': {'categories': ['scholarships', 'training'],
         'evidence': [],
         'host_countries': ['HU'],
         'kind': 'programme-overview',
         'summary': "CEU's Budapest summer-course overview describes tuition-only, "
                    'tuition-plus-housing and fuller scholarship packages for selected specialist '
                    'seminars. Applicants may still need to cover insurance, travel and living '
                    'costs; Slovak applicants are explicitly ineligible for the stated travel '
                    'reimbursement. Funding depends on the course and award type. These '
                    'conditional packages are not one universally fully funded personal '
                    'scholarship.'},
 '272': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['ID'],
         'kind': 'programme-overview',
         'summary': 'Darmasiswa supports eligible foreign secondary-school graduates aged 18–32 on '
                    'Indonesian language, arts and culture study. English, health, relevant '
                    'interest/background and a country with diplomatic relations are required; '
                    'prior Darmasiswa/KNB recipients and current Indonesian workers/students are '
                    'excluded. Published support includes IDR 4.7 million/month, books and '
                    'settling allowances, with housing/transport only during orientation as '
                    'stated.'},
 '273': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['GR'],
         'kind': 'programme-overview',
         'status': 'expired',
         'summary': 'This Onassis overview explicitly describes stays between October 1,2019 and '
                    'September 30,2020, rather than a verified 2026 call. Historic categories '
                    'offered EUR 1,500/month for specified research visits or EUR 850/month for '
                    'postgraduate study, with seasonal payment exclusions and return airfare. '
                    'Track-specific academic, age and Greek-language conditions applied. Its '
                    'yearless February guidance is not converted into a new closing date.'},
 '274': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['IE'],
         'kind': 'programme-overview',
         'summary': 'Irish Cancer Society PhD support requires a relevant project developed with '
                    'supervisors at an eligible Irish institution. The 2026 tracks have total '
                    'project ceilings of EUR 190,000 or EUR 140,000, not personal cash awards; the '
                    'full-time student stipend is EUR 25,000/year with registration support up to '
                    'EUR 8,500/year. Research, patient-involvement and mobility costs have '
                    'separate limits. The 2026 application rules prohibit generative-AI writing '
                    'assistance.'},
 '285': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['TH'],
         'kind': 'programme-overview',
         'summary': "Regent's Bangkok Global Connect offers competitively selected "
                    'secondary-school scholarships for a two-year IB route or a four-year '
                    'IGCSE-to-IB pathway, with progression conditional on strong results. '
                    'Appropriate prior school level and a headteacher recommendation are required. '
                    'Full support is possible under the actual award terms, but the catalogue '
                    'states no universal cash amount. Its specified 2026–28 IB cohort is distinct '
                    'from an inferred new call.'},
 '289': {'categories': ['scholarships', 'training'],
         'evidence': [],
         'host_countries': ['HU'],
         'kind': 'programme-overview',
         'summary': 'Roma Access Programme supports eligible Roma applicants preparing for '
                    'English-medium postgraduate humanities/social-science study at CEU or another '
                    'accredited institution. The nine-month preparation includes subject courses, '
                    'English and academic writing/study skills. Published full support covers '
                    'travel, tuition, accommodation and a living allowance. Academic entry and '
                    'programme-specific selection conditions apply; the catalogue does not '
                    'establish a current closing year.'},
 '292': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['TW'],
         'kind': 'programme-overview',
         'summary': "Taiwan's Ministry of Education undergraduate scholarship states TWD "
                    "15,000/month for up to four years. Applicants must meet the programme's "
                    'degree admission and representative-office requirements and the stated TOCFL '
                    'Level 3 Chinese condition; a prior Mandarin preparation year is recommended. '
                    'Applications and supporting documents go through the specified office route. '
                    'The March 31 guidance is yearless; no tuition benefit absent from this source '
                    'is invented.'},
 '293': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['TW'],
         'kind': 'programme-overview',
         'summary': "Taiwan's Ministry of Education postgraduate overview states TWD 20,000/month "
                    "and a maximum two-year duration, despite its title mentioning both master's "
                    'and PhD study. Degree admission, representative-office conditions and the '
                    'stated TOCFL Level 3 requirement apply; Mandarin preparation is recommended. '
                    'The title does not justify inventing a longer doctoral funding duration. The '
                    'usual March 31 closing has no stated year.'},
 '295': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'Outgoing NSP mobility supports permanent residents of Slovakia enrolled on '
                    "eligible Slovak master's/advanced combined degrees: one or two semesters "
                    'abroad, or 3–6 months of thesis-related research/artistic work. Monthly '
                    'support varies by host country; a separately requested distance-based travel '
                    'grant can reach EUR 1,500. The 69-country rate table is not an eligibility '
                    'whitelist. The October 31,2026 clock has no verified timezone.'},
 '296': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Visiting students from universities outside Slovakia can receive EUR '
                    "620/month for one or two semesters. Applicants must be master's students or "
                    'have completed at least 2.5 years of university study; a Slovak host '
                    'acceptance is required. A separately requested distance-based travel '
                    'allowance can reach EUR 1,500. Full Slovak degrees and overlapping public '
                    'scholarships are excluded.'},
 '298': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Doctoral students enrolled abroad can undertake a one-to-ten-month research '
                    'or study visit in Slovakia with EUR 1,025.50/month. A Slovak host invitation '
                    "and the programme's residence and repeat-award conditions apply. A travel "
                    'allowance, requested with the scholarship, varies by distance up to EUR '
                    '1,500; this is a visiting award rather than funding for a full Slovak PhD.'},
 '299': {'categories': ['scholarships'],
         'evidence': ['Independent full-facts review restored same-source citizenship, subject, '
                      'language, prior-study or qualification/experience restrictions.',
                      'Independent full-facts review restored same-source citizenship, subject, '
                      'language, prior-study or qualification/experience restrictions.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Incoming NSP teaching/research/artistic stays last 1–10 months. Monthly '
                    "rates: EUR 1,025.50 without PhD and normally under four years' experience; "
                    'EUR 1,370 with PhD and under ten years; EUR 1,470 with PhD and over ten '
                    'years. Host invitation is required; travel is excluded. Residence, '
                    'concurrent-public-award and repeat-award rules restrict eligibility. '
                    'Exceptional no-PhD cases need administrator approval; doctoral study counts '
                    'as experience for up to six years.'},
 '300': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'Outgoing NSP supports permanent residents of Slovakia enrolled on Slovak '
                    'doctoral programmes for one-to-ten-month study/research/artistic mobility '
                    "abroad. Monthly support depends on the host country's approved rate, with a "
                    'separately requested distance-based travel grant up to EUR 1,500. Host '
                    'acceptance and doctoral-study conditions apply. Approved rate countries are '
                    'not the complete eligible-host list; no timezone is inferred for the 2026 '
                    'closing.'},
 '307': {'categories': ['scholarships'],
         'evidence': ['Home-country metadata alone is not a citizenship whitelist.',
                      'Use the explicit eligibility and track limitations in this summary; '
                      'preserve any stricter supported field facts.'],
         'host_countries': ['MK'],
         'kind': 'programme-overview',
         'summary': 'Excellent students aged up to 22 can study a computing bachelor programme in '
                    'Ohrid. One funded academic year, renewable for satisfactory progress; '
                    'tuition, visa/residence costs, return flight, dormitory full board and health '
                    'insurance plus MKD 5,000 monthly pocket money. English proficiency and '
                    'English documents required. July 31 is published without a year.'},
 '319': {'categories': ['fellowships'],
         'evidence': ['Home-country metadata alone is not a citizenship whitelist.',
                      'Use the explicit eligibility and track limitations in this summary; '
                      'preserve any stricter supported field facts.'],
         'host_countries': ['PL'],
         'kind': 'programme-overview',
         'summary': 'Three-month cultural/history research in Krakow: Senior applicants are '
                    'professors, associate professors or PhD holders; Junior applicants are '
                    'doctoral students. Listed support is PLN 3,500/month Senior or PLN '
                    '2,500/month Junior, plus PLN 1,500 once for publications. Capacity depends on '
                    'annual funds. Usual January/February and June/July rounds have no dated '
                    'closing.'},
 '320': {'categories': ['fellowships'],
         'evidence': ['Home-country metadata alone is not a citizenship whitelist.',
                      'Use the explicit eligibility and track limitations in this summary; '
                      'preserve any stricter supported field facts.'],
         'host_countries': ['IL'],
         'kind': 'programme-overview',
         'summary': 'Outstanding university or research-institute professors may undertake a '
                    '2–12-month visit at Weizmann. Host acceptance is required, and work produced '
                    'must acknowledge the designated visiting-professor title. The listing '
                    'supplies no stipend amount or covered costs. December 31 is stated without a '
                    'year.'},
 '322': {'categories': ['internships'],
         'evidence': ['Home-country metadata alone is not a citizenship whitelist.',
                      'Use the explicit eligibility and track limitations in this summary; '
                      'preserve any stricter supported field facts.'],
         'host_countries': ['BE'],
         'kind': 'programme-overview',
         'summary': 'Six-month NATO internships for NATO-member citizens aged 21+, with two '
                    'university years completed and third-year enrolment, or a degree received '
                    'less than one year ago. English or French and valid security clearance '
                    'required. EUR 1,335/month, travel reimbursement up to EUR 1,200 and 15 paid '
                    'leave days. Up to three position applications; closings depend on each '
                    'offer.'},
 '324': {'categories': ['internships'],
         'evidence': ['Home-country metadata alone is not a citizenship whitelist.',
                      'Use the explicit eligibility and track limitations in this summary; '
                      'preserve any stricter supported field facts.'],
         'host_countries': ['LU'],
         'kind': 'programme-overview',
         'summary': 'Five-month Schuman internships for graduates aged 18+ with a qualifying '
                    'university diploma and language skills. Prior EU-institution work cannot '
                    'exceed two consecutive months; no study/research visit in the preceding six '
                    'months. General and journalism tracks require relevant written work or '
                    'journalism evidence. About EUR 1,300 is listed without a payment period; '
                    'travel contribution requires distance over 50 km. Usually May/October rounds, '
                    'no dated closing.'},
 '327': {'categories': ['fellowships'],
         'evidence': ['Home-country metadata alone is not a citizenship whitelist.',
                      'Use the explicit eligibility and track limitations in this summary; '
                      'preserve any stricter supported field facts.'],
         'host_countries': ['AT',
                            'CZ',
                            'HU',
                            'PL',
                            'SI',
                            'BG',
                            'RO',
                            'AL',
                            'BA',
                            'HR',
                            'MK',
                            'MD',
                            'RS',
                            'ME'],
         'kind': 'programme-overview',
         'summary': 'CEEPUS teaching mobility for full-time university teachers: at least five '
                    'working days, at most one month and six teaching hours/week. Host-country '
                    'stipend rates vary; an invitation is needed for freemovers. Non-CEEPUS '
                    'citizens need Equal Status proof of full-time Slovak-university employment. '
                    'Slovenia excludes teacher freemovers; Czechia, Poland and Croatia exclude '
                    'winter freemovers. June 1/July 1/November 1 rules are yearless.'},
 '333': {'categories': ['scholarships'],
         'evidence': ['Home-country metadata alone is not a citizenship whitelist.',
                      'Use the explicit eligibility and track limitations in this summary; '
                      'preserve any stricter supported field facts.'],
         'host_countries': ['MK'],
         'kind': 'opportunity',
         'summary': 'Slovak citizens in bachelor, master or doctoral study may spend 3–10 months '
                    'in North Macedonia, with host acceptance and suitable language skills. Slovak '
                    'support: EUR 500/month BA/MA or 530 PhD plus qualifying travel; host support: '
                    'EUR 50/month BA/MA or 70 PhD, tuition, materials, dormitory and public '
                    'transport. Meals exclude PhD students; health insurance is self-funded. '
                    'Closing November 26, 2026, 16:00, no timezone stated.'},
 '336': {'categories': ['scholarships'],
         'evidence': ['Home-country metadata alone is not a citizenship whitelist.',
                      'Use the explicit eligibility and track limitations in this summary; '
                      'preserve any stricter supported field facts.'],
         'host_countries': ['MX'],
         'kind': 'opportunity',
         'summary': 'Mexican-government master study, up to 24 months: MXN 14,264.88/month. '
                    'Tuition depends on host acceptance; IMSS insurance starts in month seven. '
                    'Visa-fee exemption and conditional international/domestic travel apply under '
                    'the listed annexes; self-bought flights are not reimbursed. Eligible-country '
                    'and approved-programme rules apply; one Spanish SIGCA application per person. '
                    'Applications May 21–June 22, 2026.'},
 '337': {'categories': ['fellowships'],
         'evidence': ['Home-country metadata alone is not a citizenship whitelist.',
                      'Use the explicit eligibility and track limitations in this summary; '
                      'preserve any stricter supported field facts.'],
         'host_countries': ['MX'],
         'kind': 'opportunity',
         'summary': 'Doctoral research in Mexico for 6–12 months: MXN 17,831.10/month. '
                    'Host-dependent tuition, IMSS from month seven, visa exemption and '
                    'annex-dependent travel; self-bought flights are not reimbursed. '
                    'Eligible-country and approved-programme rules apply; one Spanish SIGCA '
                    'application per person. Applications May 21–June 22, 2026. The listing '
                    'explicitly excludes funding for dissertation preparation to obtain a degree.'},
 '339': {'categories': ['fellowships'],
         'evidence': ['Home-country metadata alone is not a citizenship whitelist.',
                      'Use the explicit eligibility and track limitations in this summary; '
                      'preserve any stricter supported field facts.'],
         'host_countries': ['MX'],
         'kind': 'opportunity',
         'summary': 'Listed postdoctoral research in Mexico for 6–12 months: MXN 17,831.10/month, '
                    'host-dependent tuition, IMSS from month seven, visa exemption and '
                    'annex-dependent travel; self-bought flights are not reimbursed. One Spanish '
                    'SIGCA application per eligible-country applicant, May 21–June 22, 2026. The '
                    'audience table is broader than the postdoctoral title; exact track '
                    'eligibility requires confirmation. Dissertation preparation for a degree is '
                    'excluded.'},
 '340': {'categories': ['scholarships'],
         'evidence': ['Home-country metadata alone is not a citizenship whitelist.',
                      'Use the explicit eligibility and track limitations in this summary; '
                      'preserve any stricter supported field facts.'],
         'host_countries': ['MX'],
         'kind': 'opportunity',
         'summary': 'Bachelor mobility in Mexico, up to 12 months: MXN 14,264.88/month. '
                    'Host-dependent tuition, IMSS insurance from month seven, visa exemption and '
                    'annex-dependent travel; self-bought flights are not reimbursed. '
                    'Eligible-country and approved-programme rules apply; one Spanish SIGCA '
                    'application per person, May 21–June 22, 2026. The awarded programme, '
                    'institution and period cannot later be changed or extended.'},
 '344': {'categories': ['fellowships'],
         'evidence': ['Home-country metadata alone is not a citizenship whitelist.',
                      'Use the explicit eligibility and track limitations in this summary; '
                      'preserve any stricter supported field facts.'],
         'host_countries': ['JP'],
         'kind': 'programme-overview',
         'summary': 'Canon research visits in Japan last 3–12 months, with host acceptance and an '
                    'agreed project. Applicants need a master or PhD; the listing states age up to '
                    '40 and normally graduation within ten years, with documented exceptions to '
                    'the latter. Listed funding is EUR 22,500–30,000 without an explicit payment '
                    'period. Usually a September round; no dated closing. Visits may begin from '
                    'January of the following year.'},
 '345': {'categories': ['fellowships'],
         'evidence': ['Home-country metadata alone is not a citizenship whitelist.',
                      'Use the explicit eligibility and track limitations in this summary; '
                      'preserve any stricter supported field facts.'],
         'host_countries': ['BE'],
         'kind': 'programme-overview',
         'summary': 'Foreign PhD holders can research in Wallonia or Brussels for 1–3 months or at '
                    'least 12 months. They must not have spent more than 12 months in Belgium '
                    'during the preceding three years. EUR 2,120 net/month plus economy return '
                    'travel and health, liability and repatriation insurance. Host consent and two '
                    'references required. Yearless long-stay February 1 and short-stay April '
                    '1/October 1 rules apply.'},
 '347': {'categories': ['training', 'scholarships'],
         'evidence': ['Home-country metadata alone is not a citizenship whitelist.',
                      'Use the explicit eligibility and track limitations in this summary; '
                      'preserve any stricter supported field facts.'],
         'host_countries': ['CZ'],
         'kind': 'programme-overview',
         'summary': 'English-language political/economic summer study in Prague, accredited by '
                    'Charles University. Listed programme fee EUR 3,750; conditional TFAS '
                    'scholarships still leave at least EUR 700 payable by every student. Fee '
                    'includes accommodation, materials and normally two meals daily; travel and '
                    'extra meals/personal costs are additional. Recommendations, essay and '
                    'interview required. Yearless closing/early-priority dates conflict; intake '
                    'may close early by country.'},
 '350': {'categories': ['scholarships'],
         'evidence': ['Home-country metadata alone is not a citizenship whitelist.',
                      'Use the explicit eligibility and track limitations in this summary; '
                      'preserve any stricter supported field facts.'],
         'host_countries': ['FR'],
         'kind': 'programme-overview',
         'summary': 'One-year French M2 support for Slovak students; dual French–Slovak citizens '
                    'are excluded. EUR 860/month, insurance, public-university registration and '
                    'CVEC exemption; Campus France housing assistance, with possible CAF support, '
                    'is not free housing. Slovak-ministry travel funding requires enrolment at a '
                    'Slovak university when applying. French/English documents and host admission '
                    'evidence required. Usually March, no dated closing.'},
 '351': {'categories': ['fellowships'],
         'evidence': ['Home-country metadata alone is not a citizenship whitelist.',
                      'Use the explicit eligibility and track limitations in this summary; '
                      'preserve any stricter supported field facts.'],
         'host_countries': ['FR'],
         'kind': 'programme-overview',
         'summary': 'French research visits of 1–3 months for doctoral students or researchers '
                    'whose PhD is at most five years old, working at a Slovak university or '
                    'research organisation; Slovak and foreign nationals qualify. EUR 1,704 or '
                    '2,055/month depending on status; housing arrangements and qualifying '
                    'Slovak-university researcher/doctoral travel support. Host collaboration and '
                    'project evidence required. Priority fields do not exclude other disciplines; '
                    'usual November round is yearless.'},
 '352': {'categories': ['scholarships'],
         'evidence': ['Home-country metadata alone is not a citizenship whitelist.',
                      'Use the explicit eligibility and track limitations in this summary; '
                      'preserve any stricter supported field facts.'],
         'host_countries': ['FR'],
         'kind': 'programme-overview',
         'summary': 'Cotutelle PhD for Slovak citizens, excluding dual French–Slovak citizens: '
                    'three years alternating six months in France and six in Slovakia, with both '
                    'supervisors and two diplomas on completion. EUR 1,770/month during three '
                    'French six-month stays, insurance and registration/CVEC exemption. Housing '
                    'arrangements may receive CAF support; first-stay travel requires Slovak study '
                    'at application. French/English documents; usual February round, no dated '
                    'closing.'},
 '355': {'categories': ['scholarships'],
         'evidence': ['Home-country metadata alone is not a citizenship whitelist.',
                      'Use the explicit eligibility and track limitations in this summary; '
                      'preserve any stricter supported field facts.'],
         'host_countries': ['FR'],
         'kind': 'programme-overview',
         'summary': 'France Excellence Eiffel supports qualifying master/engineering degrees or '
                    'doctoral mobility for 12–36 months. French higher-education institutions must '
                    'nominate applicants; direct student applications are not accepted. EUR '
                    '1,200/month master or 2,100/month doctorate, transport, insurance, housing '
                    'assistance and cultural activities. Tuition is not funded. Closing and '
                    'required documents depend on the institutional call.'},
 '358': {'categories': ['scholarships'],
         'evidence': ['Home-country metadata alone is not a citizenship whitelist.',
                      'Use the explicit eligibility and track limitations in this summary; '
                      'preserve any stricter supported field facts.'],
         'host_countries': ['FR'],
         'kind': 'programme-overview',
         'summary': 'Excellent non-French nationals may pursue ENS Paris-Saclay M2 for one year, '
                    'research internships up to six months or doctoral research up to 12 months. '
                    'Host academic nomination is mandatory; direct unendorsed applications are '
                    'refused. Usually EUR 1,000/month for ten months is listed, without proving '
                    'identical funding for every visit length. French/English proficiency evidence '
                    'follows the programme language; joint PhDs preferred. Closing depends on the '
                    'call.'},
 '359': {'categories': ['training'],
         'evidence': ['Home-country metadata alone is not a citizenship whitelist.',
                      'Use the explicit eligibility and track limitations in this summary; '
                      'preserve any stricter supported field facts.'],
         'host_countries': ['FR'],
         'kind': 'programme-overview',
         'summary': 'Courants du Monde provides 10-day to three-month professional visits in '
                    'France for experienced foreign cultural-sector practitioners. Includes '
                    'conferences, institutional visits, meetings and French partnerships; '
                    'financial coverage varies by call. Students and artists without professional '
                    'experience are excluded. Applicants supply their professional profile and '
                    'mobility project; closing depends on the current call.'},
 '361': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['IT'],
         'kind': 'programme-overview',
         'summary': 'Max Weber fellowships at the EUI support postdoctoral '
                    'humanities/social-science researchers of any nationality, normally for 1–2 '
                    'years with specified longer exceptions. Published support is EUR 2,500/month '
                    'plus conditional family, insurance, research and travel support. Recent PhDs '
                    'without prior postdoctoral fellowships are preferred; English requirements '
                    'apply. A second-year teaching role and extra payment are conditional, not '
                    'universal benefits.'},
 '362': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['IT'],
         'kind': 'programme-overview',
         'summary': 'Bogliasco offers a one-month arts/humanities residency for established '
                    'practitioners and scholars developing a public-facing project. Support is in '
                    'kind: housing, workspace and full board, rather than a monthly cash grant. '
                    'The programme provides focused work time rather than formal courses. '
                    'Professional achievement and project selection apply; former fellows must '
                    'wait five years before applying again.'},
 '365': {'categories': ['other'],
         'evidence': [],
         'host_countries': ['JO'],
         'kind': 'unknown',
         'summary': "ACOR's Jordan research-funding overview covers several named archaeological, "
                    'historical and cultural-heritage fellowships, typically lasting 1–6 months. '
                    'Most awards meet only part of the stay costs; amounts and nationality/student '
                    'restrictions vary by individual scheme. Applicants must choose an actual '
                    'eligible fellowship and satisfy its project and selection conditions. This '
                    'directory is not one uniform fully funded personal award.'},
 '374': {'categories': ['competitions', 'grants'],
         'evidence': [],
         'host_countries': ['FR'],
         'kind': 'programme-overview',
         'summary': "Paris's Margaret Maruani gender-studies research prize supports projects on "
                    'gender, feminism, equality and related rights. The published EUR 20,000 is '
                    'the total support: each laureate receives EUR 10,000, rather than EUR 20,000 '
                    "personally. Applicants submit a research project under the city's actual "
                    'selection rules. Duration is not specified in the catalogue; the overview '
                    'does not establish a dated current closing.'},
 '376': {'categories': ['scholarships', 'training'],
         'evidence': [],
         'host_countries': ['TW'],
         'kind': 'programme-overview',
         'summary': "Taiwan's Mandarin-language scholarship overview describes a 6–12-month course "
                    'and support of TWD 25,000. The amount field does not state a payment period, '
                    'so a monthly entitlement is not inferred. Applicants submit the prescribed '
                    'documents through the Taipei representative office and must satisfy the '
                    'language-course and programme eligibility conditions. This route is separate '
                    'from degree-study scholarships.'},
 '378': {'categories': ['scholarships'],
         'evidence': ['Target during the stay: university teachers. Sending-country/institutional '
                      'affiliation and the actual programme conditions are distinct from a '
                      'universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.',
                      'Independent full-facts review restored material eligibility, benefit or '
                      'travel constraints from the same guarded source leaf.'],
         'host_countries': ['HR'],
         'kind': 'programme-overview',
         'summary': 'Croatian lecture mobility supports eligible Slovak citizens with the required '
                    'Slovak institutional link who teach Slovak/Croatian language and literature. '
                    'Stays last 1–30 days, with free hotel-type housing and a statutory '
                    'meal/pocket allowance of about EUR 20/day. A Croatian host invitation and '
                    'relevant lecture plan are required. Slovak-funded travel depends on the '
                    'specified education-sector affiliation; partner acceptance is still needed '
                    'after Slovak selection.'},
 '380': {'categories': ['scholarships'],
         'evidence': ['Target during the stay: university teachers, researchers. '
                      'Sending-country/institutional affiliation and the actual programme '
                      'conditions are distinct from a universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.',
                      'Independent full-facts review restored material eligibility, benefit or '
                      'travel constraints from the same guarded source leaf.'],
         'host_countries': ['CZ'],
         'kind': 'programme-overview',
         'summary': 'Czech lecture/research mobility supports eligible Slovak citizens with the '
                    'required Slovak institutional link on 1–14-day visits. Doctoral students are '
                    'excluded. The host supplies accommodation and CZK 500/day for meals/pocket '
                    'expenses; Slovak-funded travel depends on specified education-sector '
                    'affiliation. Host acceptance and a relevant programme are required, with a '
                    'complete application at least three months before the planned visit.'},
 '381': {'categories': ['scholarships', 'training'],
         'evidence': [],
         'host_countries': ['LV'],
         'kind': 'programme-overview',
         'summary': 'Latvian summer-school scholarships fund eligible participation through the '
                    'actual organising institution. The published EUR 1,000 is transferred to the '
                    'school organiser, not paid as student cash. Participants must cover any costs '
                    'exceeding that contribution; duration and requirements depend on the selected '
                    'school. Applicants apply directly to the eligible 2026 summer-school '
                    'institution and its selection rules.'},
 '382': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['LV'],
         'kind': 'programme-overview',
         'summary': "Latvian study scholarships state EUR 500/month for bachelor's/master's "
                    'students or EUR 700/month for doctoral students, normally up to 10 or 11 '
                    'months respectively. Prior-study and eligible-country conditions apply. '
                    'Tuition, travel and health insurance may remain payable, and the stipend is '
                    'explicitly insufficient for all living costs. Concurrent scholarships are '
                    'excluded; renewal requires another application and consecutive-award limits '
                    'apply.'},
 '388': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['TR'],
         'kind': 'programme-overview',
         'summary': 'Türkiye Scholarships support eligible full undergraduate study with a Turkish '
                    'preparation year. The source states TRY 4,500/month, university placement, '
                    'tuition, insurance, accommodation, language preparation and start/completion '
                    'flights. Applicants must meet the stated maximum age 21, academic and '
                    'degree-admission requirements. Programme length depends on the degree; the '
                    'yearless January/February guidance does not establish a current dated call.'},
 '389': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['TR'],
         'kind': 'programme-overview',
         'summary': "Türkiye's research scholarship overview supports eligible doctoral students, "
                    'university teachers and researchers for 3–12 months. Published support is TRY '
                    '25,000/month; tuition, housing or travel extras are not supplied in this '
                    'amount field and are not assumed. Applicants must meet the selected research '
                    'programme and host requirements. The listed thematic routes have their own '
                    'eligibility and application conditions.'},
 '390': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['IN'],
         'kind': 'programme-overview',
         'summary': 'ICCR supports eligible study of traditional Indian dance/music for 1–3 years. '
                    'The source states INR 4,500–6,500/month by study level, with specified '
                    'housing, tuition, medical, excursion and thesis support; other allowances '
                    'have unclear/repeated wording. Indian approval is required and Slovak '
                    'nomination alone does not secure an award. The source warns that embassy '
                    'updates are pending, so its figures are not independently verified current '
                    'rates.'},
 '392': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'Bavarian postgraduate scholarships support eligible citizens of listed '
                    'Central/Eastern/Southeastern European countries who retain permanent '
                    'residence in their home country. Published support is EUR 992/month or EUR '
                    '1,152 with a child for one year, potentially renewable twice. Age 30 '
                    "master's/35 doctoral, academic and German/English requirements apply. Travel "
                    'is self-funded and fee-charging programmes are generally excluded.'},
 '414': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['CH'],
         'kind': 'programme-overview',
         'summary': 'Swiss Government Excellence doctoral awards state CHF 2,450/month for 12–36 '
                    "months. Applicants need the appropriate master's degree by the specified 2027 "
                    'date, birth after December 31,1991 and an eligible Swiss '
                    'supervisor/admission. Prior-award and Swiss-residence exclusions apply. '
                    "Travel, registration and potentially tuition remain the applicant's "
                    'responsibility; another concurrent scholarship is excluded. The actual 2026 '
                    'application closing is November 27.'},
 '421': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Egypt to Slovakia for bachelor's students, master's "
                    'students, doctoral students, university teachers; duration: 3 weeks (in '
                    'August). The host provides the language course, accommodation, meals and a '
                    'statutory daily allowance. The sending side covers international travel.'},
 '423': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility from Egypt to Slovakia for university teachers, '
                    'researchers and other listed applicants; duration: 2 - 6 months. Published '
                    'support: EUR 1,025.50 or 1,420/month for staff, depending on PhD status. The '
                    'sending side covers international travel.'},
 '424': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility from Egypt to Slovakia for university teachers, '
                    'researchers and other listed applicants; duration: max. 10 days. Host support '
                    'includes accommodation with breakfast plus the statutory daily allowance. The '
                    'sending side covers international travel.'},
 '425': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Israel to Slovakia for bachelor's students, master's "
                    'students, doctoral students, university teachers, researchers and other '
                    'listed applicants; duration: 3 weeks. The host provides the language course, '
                    'accommodation, meals and a statutory daily allowance. The sending side covers '
                    'international travel.'},
 '426': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Israel to Slovakia for master's students; duration: 8 "
                    'months. Published support: EUR 620/month. The sending side covers '
                    'international travel.'},
 '430': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility from Israel to Slovakia for doctoral students; duration: 8 '
                    'months. Published support: EUR 1,025.50/month. The sending side covers '
                    'international travel.'},
 '434': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Greece to Slovakia for bachelor's students, master's "
                    'students, doctoral students, university teachers, researchers and other '
                    'listed applicants; duration: 3 weeks (in August). The host provides the '
                    'language course, accommodation, meals and a statutory daily allowance.'},
 '435': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility from Greece to Slovakia for university teachers; duration: '
                    '10 days. Host support includes accommodation plus the statutory daily '
                    'allowance. The sending side covers international travel.'},
 '436': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility from Greece to Slovakia for doctoral students; duration: 5 '
                    'or 10 months. Published support: EUR 1,025.50/month. The sending side covers '
                    'international travel.'},
 '440': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Norway to Slovakia for bachelor's students, master's "
                    'students, doctoral students, university teachers, researchers and other '
                    'listed applicants; duration: 3 weeks (in August). The host provides the '
                    'language course, accommodation, meals and a statutory daily allowance. The '
                    'sending side covers international travel.'},
 '442': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Finland to Slovakia for bachelor's students, master's "
                    'students, doctoral students, university teachers, researchers and other '
                    'listed applicants; duration: 3 weeks (in August). The host provides the '
                    'language course, accommodation, meals and a statutory daily allowance. The '
                    'sending side covers international travel.'},
 '443': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['TW'],
         'kind': 'programme-overview',
         'summary': 'Visegrad–Taiwan supports eligible V4 doctoral and postdoctoral researchers at '
                    'selected Taiwanese institutions for 1–10 months. Published support is EUR '
                    '1,000/month plus a one-off EUR 1,000 travel allowance; the Taiwanese side '
                    'meets tuition and research costs as stated. Host acceptance, subject and '
                    'programme conditions apply. The usual August–September application dates are '
                    'yearless despite an explicit CET clock, so no year is invented.'},
 '445': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Finland to Slovakia for bachelor's students, master's "
                    'students, doctoral students, university teachers; duration: 3 - 9 months. '
                    'Published support: EUR 620/month for university students; EUR 1,025.50/month '
                    'for doctoral students; EUR 1,025.50 or 1,420/month for staff, depending on '
                    'PhD status. The sending side covers international travel.'},
 '446': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Norway to Slovakia for bachelor's students, master's "
                    'students, doctoral students; duration: 3 - 9 months. Published support: EUR '
                    '620/month for university students; EUR 1,025.50/month for doctoral students. '
                    'The sending side covers international travel.'},
 '447': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility from Norway to Slovakia for researchers; duration: 7 - 21 '
                    'days. Host support includes accommodation and meals plus the statutory daily '
                    'allowance. The sending side covers international travel.'},
 '450': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Italy to Slovakia for bachelor's students, master's "
                    'students, doctoral students, university teachers, researchers and other '
                    'listed applicants; duration: 1 - 10 months. Published support: EUR 620/month '
                    'for university students; EUR 1,025.50/month for doctoral students; EUR '
                    '1,025.50 or 1,420/month for staff, depending on PhD status. The sending side '
                    'covers international travel.'},
 '451': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Italy to Slovakia for bachelor's students, master's "
                    'students, doctoral students; duration: 3 weeks (in August). The host provides '
                    'the language course, accommodation, meals and a statutory daily allowance. '
                    'The sending side covers international travel.'},
 '452': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from North Macedonia to Slovakia for bachelor's students, "
                    "master's students, doctoral students; duration: 3 - 10 months. Published "
                    'support: EUR 620/month for university students; EUR 1,025.50/month for '
                    'doctoral students. The sending side covers international travel.'},
 '453': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Czechia to Slovakia for master's students; duration: "
                    '3 - 10 months. Published support: EUR 620/month for university students. The '
                    'sending side covers international travel.'},
 '454': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility from Czechia to Slovakia for doctoral students; duration: '
                    '3 - 10 months. Published support: EUR 1,025.50/month for doctoral students. '
                    'The sending side covers international travel.'},
 '455': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Czechia to Slovakia for bachelor's students, master's "
                    'students, doctoral students, university teachers, researchers and other '
                    'listed applicants; duration: 3 weeks (in August). The host provides the '
                    'language course, accommodation, meals and a statutory daily allowance. The '
                    'sending side covers international travel.'},
 '456': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility from Czechia to Slovakia for university teachers, '
                    'researchers; duration: max. 14 days. Host support includes accommodation plus '
                    'the statutory daily allowance. The sending side covers international travel.'},
 '457': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Croatia to Slovakia for bachelor's students, master's "
                    'students, doctoral students; duration: 1 - 10 months (university students - '
                    'min. 4 months; PhD students - min. 1 month). Published support: EUR 620/month '
                    'for university students; EUR 1,025.50/month for doctoral students. The '
                    'sending side covers international travel.'},
 '458': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Croatia to Slovakia for bachelor's students, master's "
                    'students, doctoral students, university teachers, researchers and other '
                    'listed applicants; duration: 3 weeks (in August). The host provides the '
                    'language course, accommodation, meals and a statutory daily allowance. The '
                    'sending side covers international travel.'},
 '459': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility from Croatia to Slovakia for university teachers; '
                    'duration: max. 30 days. Host support includes accommodation plus the '
                    'statutory daily allowance. The sending side covers international travel.'},
 '460': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Germany to Slovakia for bachelor's students, master's "
                    'students, doctoral students, university teachers, researchers and other '
                    'listed applicants; duration: 3 weeks (in August). The host provides the '
                    'language course, accommodation, meals and a statutory daily allowance. The '
                    'sending side covers international travel.'},
 '467': {'categories': ['scholarships'],
         'evidence': ['Target during the stay: doctoral students. Sending-country/institutional '
                      'affiliation and the actual programme conditions are distinct from a '
                      'universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['CZ'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility to Czechia for doctoral students: 3–10 months. Published '
                    "host support is CZK 13,000/month for eligible master's-degree holders not "
                    "concurrently on another Czech bachelor's/master's programme; study, dormitory "
                    'and meal terms follow local rules. Slovak-funded travel requires the stated '
                    'sending-sector affiliation.'},
 '468': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['JP'],
         'kind': 'programme-overview',
         'summary': "JSPS's long-term postdoctoral overview supports eligible research in Japan "
                    'for 12–24 months with JPY 362,000/month, JPY 200,000 settling support, return '
                    'airfare and health benefits. A host-approved research programme is required. '
                    "The catalogue's PhD-year condition explicitly refers to 2007 and is "
                    'historical; it is not converted into a new rolling threshold or claimed as '
                    'verified current eligibility. Application route and actual award rules govern '
                    'selection.'},
 '469': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['CH'],
         'kind': 'programme-overview',
         'summary': 'Swiss Government Excellence research visits state CHF 2,450/month for 6–12 '
                    "months without extension. Applicants need the required master's degree by the "
                    'specified 2027 date, birth after December 31,1991 and a Swiss supervisor; '
                    'prior-award/residence exclusions apply. Tuition/registration, travel and '
                    'concurrent-scholarship restrictions remain. Shorter visits have lower '
                    'priority and formal cotutelle projects require 12 months. The 2026 closing is '
                    'November 27.'},
 '470': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['JP'],
         'kind': 'programme-overview',
         'summary': 'Matsumae supports eligible PhD researchers up to 49 on 3–6-month Japanese '
                    'visits. Published support is JPY 220,000/month, JPY 120,000 settling support, '
                    'return airfare and health/travel insurance. English or Japanese and host '
                    'acceptance are required; prior Japanese visits and current Japanese '
                    'employment/study are excluded. Natural science, engineering and medicine are '
                    'preferred. The stated April–June application window has no year.'},
 '471': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['JP'],
         'kind': 'programme-overview',
         'summary': "Hokkaido's Slavic Research Center offers eligible visiting researchers a "
                    '2–3-month fellowship. Published support includes JPY 480,000/month, JPY '
                    '80,000 for Japanese business travel, return airfare, centre transfer and '
                    'office space. Housing is arranged, but rent and utilities remain payable. The '
                    "applicant's scholarly project/profile and actual centre selection rules "
                    'apply; no current deadline year is inferred.'},
 '478': {'categories': ['scholarships', 'training'],
         'evidence': [],
         'host_countries': ['JP'],
         'kind': 'programme-overview',
         'summary': 'Vulcanus in Japan offers an approximately eight-month technical '
                    'training/study-industry pathway for EU citizens at least in their third '
                    'university year. Academic results, recommendations, English, motivation and '
                    'adaptability are assessed. The source states JPY 2 million support without an '
                    'explicit payment period, so it is not labelled a monthly stipend. Applicants '
                    'must satisfy the actual technical-field and programme-selection '
                    'requirements.'},
 '48': {'categories': ['scholarships'],
        'evidence': [],
        'host_countries': ['CH'],
        'kind': 'programme-overview',
        'summary': "Swiss Government Excellence arts awards support eligible master's study for "
                   '12–21 months at CHF 2,450/month. Applicants need the specified recent '
                   "bachelor's degree, birth after December 31,1991 and actual Swiss admission or "
                   'admission consideration. Prior-award/residence and full-time study '
                   'restrictions apply. Travel, registration and potentially tuition remain '
                   'payable; overlapping scholarships are excluded. The actual 2026 closing is '
                   'November 27.'},
 '481': {'categories': ['grants'],
         'evidence': [],
         'host_countries': ['US'],
         'kind': 'programme-overview',
         'summary': "Visegrad's New York visual-arts residency supports eligible V4 artists at "
                    "Brooklyn's ISCP for two months. Published support is EUR 7,000 toward "
                    'housing, travel, living costs and materials, with professional facilities. '
                    'Normally one artist is selected per V4 country annually; written/spoken '
                    'English is required and applicants without a prior New York arts scholarship '
                    'are preferred. Programme and professional-selection conditions apply.'},
 '482': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Egypt to Slovakia for bachelor's students, master's "
                    'students; duration: 3 - 5 months. Published support: EUR 620/month for '
                    'university students. The sending side covers international travel.'},
 '488': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Belgium to Slovakia for bachelor's students, master's "
                    'students, doctoral students, university teachers, researchers and other '
                    'listed applicants; duration: 3 weeks (in August). The host provides the '
                    'language course, accommodation, meals and a statutory daily allowance. The '
                    'sending side covers international travel.'},
 '489': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility from Belgium to Slovakia for doctoral students, university '
                    'teachers, researchers; duration: 1 - 10 months. Published support: EUR '
                    '1,025.50/month for doctoral students; EUR 1,025.50 or 1,420/month for staff, '
                    'depending on PhD status. The sending side covers international travel.'},
 '49': {'categories': ['scholarships', 'training'],
        'evidence': [],
        'host_countries': ['DE'],
        'kind': 'programme-overview',
        'summary': "DAAD's four-week German summer-course award states EUR 475, course fees, "
                   'travel and insurance support; accommodation is assigned rather than '
                   "universally described as free. Eligible bachelor's/master's students need "
                   'continuing enrolment and A2 German; graduates/doctoral students are excluded. '
                   "The source's semester/year completion wording differs, so applicants must "
                   'check the actual prior-study requirement. Only listed eligible courses '
                   'qualify; the award is nonrenewable.'},
 '490': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "CEEPUS supports incoming bachelor's, master's and doctoral students for one "
                    "to ten months through an eligible network or freemover route. Slovakia's "
                    "published rates are EUR 620/month for bachelor's/master's students and EUR "
                    '1,025.50/month for doctoral students. Participating-country citizenship or '
                    'equivalent status, university enrolment and mobility requirements apply.'},
 '491': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Incoming CEEPUS university teachers undertake teaching mobility in Slovakia, '
                    'including at least five working days and six teaching hours. Published '
                    'monthly rates are EUR 1,025.50 without a PhD and EUR 1,420 with a PhD, '
                    'prorated for shorter stays. Eligible participating-country status and '
                    'employment at an eligible university are required; the teacher route has '
                    'different conditions from student mobility.'},
 '495': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.',
                      'Independent full-facts review restored material eligibility, benefit or '
                      'travel constraints from the same guarded source leaf.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Kazakhstan to Slovakia for master's students lasts "
                    '3–10 months, with EUR 620/month for living costs. Scholarship holders pay '
                    'their own international travel to and from Slovakia. Home-authority selection '
                    'and nomination, complete application documents and programme-specific '
                    'institutional requirements apply; the country heading alone is not converted '
                    'into a citizenship whitelist.'},
 '496': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.',
                      'Independent full-facts review restored material eligibility, benefit or '
                      'travel constraints from the same guarded source leaf.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility from Kazakhstan to Slovakia for doctoral students lasts '
                    '3–10 months, with EUR 1,025.50/month for living costs. Scholarship holders '
                    'pay their own international travel to and from Slovakia. Home-authority '
                    'selection and nomination, complete application documents and '
                    'programme-specific institutional requirements apply; the country heading '
                    'alone is not converted into a citizenship whitelist.'},
 '500': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Ukraine to Slovakia for bachelor's students, master's "
                    'students; duration: 1 - 10 months. Published support: EUR 620/month. The '
                    'sending side covers international travel.'},
 '501': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility from Ukraine to Slovakia for doctoral students, university '
                    'teachers, researchers; duration: 1 - 10 months. Published support: EUR '
                    '1,025.50/month for doctoral students; EUR 1,025.50 or 1,420/month for staff, '
                    'depending on PhD status. The sending side covers international travel.'},
 '502': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Ukraine to Slovakia for bachelor's students, master's "
                    'students, doctoral students, university teachers, researchers and other '
                    'listed applicants; duration: 3 weeks. The host provides the language course, '
                    'accommodation, meals and a statutory daily allowance. The sending side covers '
                    'international travel.'},
 '503': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Bulgaria to Slovakia for bachelor's students, "
                    "master's students; duration: 5 months. Published support: EUR 620/month for "
                    'university students. The sending side covers international travel.'},
 '504': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.',
                      'Independent full-facts review restored material eligibility, benefit or '
                      'travel constraints from the same guarded source leaf.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility from Bulgaria to Slovakia supports doctoral students, '
                    'university teachers and researchers for 1–10 months. Support is EUR '
                    '1,025.50/month for doctoral candidates/staff without a PhD or EUR 1,420 for '
                    'PhD-qualified staff; the sending side covers international travel. A Slovak '
                    'host invitation is required, along with home-authority selection/nomination '
                    'and complete documents. Country metadata is not a blanket passport '
                    'restriction.'},
 '505': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Bulgaria to Slovakia for bachelor's students, "
                    "master's students, doctoral students, university teachers; duration: 3 weeks "
                    '(in August). The host provides the language course, accommodation, meals and '
                    'a statutory daily allowance. The sending side covers international travel.'},
 '506': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from China to Slovakia for bachelor's students, master's "
                    'students, doctoral students, university teachers, researchers; duration: 1 - '
                    '12 months (research stays max. 2 years). Published support: EUR 620/month for '
                    'university students; EUR 1,025.50/month for doctoral students; EUR 1,025.50 '
                    'or 1,420/month for staff, depending on PhD status. The sending side covers '
                    'international travel.'},
 '507': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.',
                      'Independent full-facts review restored material eligibility, benefit or '
                      'travel constraints from the same guarded source leaf.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Hungary to Slovakia for bachelor's students, master's "
                    'students; duration: 5 or 10 months. Published support: EUR 620/month for '
                    'university students. The sending side covers international travel. Slovak '
                    'proficiency and a medical check-up certificate are mandatory.'},
 '508': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.',
                      'Independent full-facts review restored material eligibility, benefit or '
                      'travel constraints from the same guarded source leaf.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility from Hungary to Slovakia for doctoral students; duration: '
                    '1 - 10 months. Published support: EUR 1,025.50/month for doctoral students. '
                    'The sending side covers international travel. Slovak proficiency and a '
                    'medical check-up certificate are mandatory.'},
 '509': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.',
                      'Independent full-facts review restored material eligibility, benefit or '
                      'travel constraints from the same guarded source leaf.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility from Hungary to Slovakia for university teachers, '
                    'researchers; duration: 1 - 3 months. Published support: EUR 1,025.50 or '
                    '1,420/month for staff, depending on PhD status. The sending side covers '
                    'international travel. Slovak proficiency and a medical check-up certificate '
                    'are mandatory.'},
 '510': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.',
                      'Independent full-facts review restored material eligibility, benefit or '
                      'travel constraints from the same guarded source leaf.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility from Hungary to Slovakia for university teachers, '
                    'researchers; duration: 5 - 20 days. Host support includes accommodation plus '
                    'the statutory daily allowance. The sending side covers international travel. '
                    'Slovak proficiency and a medical check-up certificate are mandatory.'},
 '511': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Hungary to Slovakia for bachelor's students, master's "
                    'students, doctoral students, university teachers, researchers and other '
                    'listed applicants; duration: 3 weeks (in August). The host provides the '
                    'language course, accommodation, meals and a statutory daily allowance. The '
                    'sending side covers international travel.'},
 '512': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility from Moldova to Slovakia for doctoral students, university '
                    'teachers, researchers; duration: 3 - 10 months. Published support: EUR '
                    '620/month for doctoral students; EUR 1,025.50 or 1,420/month for staff, '
                    'depending on PhD status. The sending side covers international travel.'},
 '514': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.',
                      'Independent full-facts review restored same-source citizenship, subject, '
                      'language, prior-study or qualification/experience restrictions.',
                      'Independent full-facts review restored same-source citizenship, subject, '
                      'language, prior-study or qualification/experience restrictions.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Poland to Slovakia supports bachelor's/master's "
                    'students specifically in Slovak or Slavonic studies for five months. '
                    'Published support is PLN 2,250/month, with sending-side international travel. '
                    'Home-authority selection/nomination and complete programme documents apply. '
                    'The source country heading is not converted into a blanket passport '
                    'restriction; applicants must meet the actual subject and institutional '
                    'requirements.'},
 '515': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Poland to Slovakia for bachelor's students, master's "
                    'students; duration: 5 months. Published support: PLN 2,250/month for '
                    'students. The sending side covers international travel.'},
 '516': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility from Poland to Slovakia for doctoral students; duration: 1 '
                    '- 3 months. Published support: PLN 3,000/month for doctoral students. The '
                    'sending side covers international travel.'},
 '517': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility from Poland to Slovakia for university teachers, '
                    'researchers; duration: 1 - 10 months. Published support: PLN 3,000 or '
                    '4,000/month for staff, depending on PhD status. The sending side covers '
                    'international travel.'},
 '518': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Poland to Slovakia for bachelor's students, master's "
                    'students, doctoral students, university teachers; duration: 3 weeks (in '
                    'August). The host provides the language course, accommodation, meals and a '
                    'statutory daily allowance. The sending side covers international travel.'},
 '519': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Romania to Slovakia for bachelor's students, master's "
                    'students; duration: 5 - 10 months. Published support: EUR 620/month for '
                    'university students. The sending side covers international travel.'},
 '52': {'categories': ['fellowships'],
        'evidence': [],
        'host_countries': ['DE'],
        'kind': 'programme-overview',
        'summary': "DAAD's re-invitation programme supports eligible former long-stay fellows on "
                   '1–3-month German professional/research visits. Published support is EUR '
                   '2,000/month for eligible lecturers or EUR 2,150 for professors, plus stated '
                   'travel and conditional accessibility support. A previous qualifying DAAD stay '
                   'and no German residence in the past three years are required. Renewal is not '
                   'allowed and another award normally requires a three-year interval.'},
 '520': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility from Romania to Slovakia for doctoral students, university '
                    'teachers, researchers; duration: 3 - 10 months. Published support: EUR '
                    '1,025.50/month for doctoral students; EUR 1,025.50 or 1,420/month for staff, '
                    'depending on PhD status. The sending side covers international travel.'},
 '521': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Romania to Slovakia for bachelor's students, master's "
                    'students, doctoral students, university teachers, researchers; duration: 3 '
                    'weeks (in August). The host provides the language course, accommodation, '
                    'meals and a statutory daily allowance. The sending side covers international '
                    'travel.'},
 '522': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Slovenia to Slovakia for bachelor's students, "
                    "master's students, doctoral students; duration: 3 - 10 months. Published "
                    'support: EUR 620/month for university students; EUR 1,025.50/month for '
                    'doctoral students. The sending side covers international travel.'},
 '523': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Slovenia to Slovakia for bachelor's students, "
                    "master's students, doctoral students, university teachers, researchers; "
                    'duration: 3 weeks (in August). The host provides the language course, '
                    'accommodation, meals and a statutory daily allowance. The sending side covers '
                    'international travel.'},
 '524': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Serbia to Slovakia for bachelor's students, master's "
                    'students, doctoral students, university teachers, researchers; duration: 3 '
                    'weeks (in August). The host provides the language course, accommodation, '
                    'meals and a statutory daily allowance. The sending side covers international '
                    'travel.'},
 '525': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.',
                      'Independent full-facts review restored material eligibility, benefit or '
                      'travel constraints from the same guarded source leaf.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Serbia to Slovakia supports bachelor's/master's and "
                    'doctoral students at EUR 620 or EUR 1,025.50/month respectively, with '
                    'sending-side international travel. The title says 1–9 months but the duration '
                    'field requires at least three months; applicants must clarify that '
                    'difference. Home-authority nomination and complete documents apply; the '
                    'country heading alone does not establish a citizenship whitelist.'},
 '527': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['JP'],
         'kind': 'programme-overview',
         'summary': 'MEXT Japanese-studies mobility supports eligible Slovak students aged 18–29 '
                    'majoring in Japanese language/culture outside Japan for one year. Published '
                    'support is JPY 117,000/month, tuition and economy airfare. Strong Japanese '
                    'and health requirements apply; continuing home-degree study and return rules '
                    'are mandatory, with specified joint-degree exceptions. The source labels its '
                    'conditions informational pending embassy updates, rather than verified '
                    'current terms.'},
 '528': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['NO'],
         'kind': 'programme-overview',
         'summary': 'Norwegian language/literature/culture mobility supports eligible '
                    "master's/doctoral thesis work for 1–3 months at NOK 13,700/month plus NOK "
                    '4,000 travel. Applicants study outside Norway, normally in a '
                    'Nordic/Scandinavian department. A clearly relevant thesis can exceptionally '
                    'qualify outside that department if funds permit. Host, research-topic and '
                    "enrolment requirements apply; the catalogue's home-country heading is not a "
                    'universal citizenship whitelist.'},
 '531': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Government scholarships fund supported-country applicants on full-time Slovak '
                    "public-university degrees: EUR 700/month for bachelor's/master's study or "
                    'language preparation, and EUR 1,100/month for doctoral study, with specified '
                    'start/completion allowances. Age, academic-grade and Slovak-language '
                    'requirements apply. The EUR 80 institutional payment is not extra student '
                    'cash; country quotas and study fields depend on the annual programme.'},
 '541': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['HK'],
         'kind': 'programme-overview',
         'summary': "Lingnan's four-year undergraduate scholarship overview describes competitive "
                    'awards for eligible nonlocal students: full, full-tuition or half-tuition '
                    'support. The fuller package covers tuition, hostel and part of living/study '
                    'expenses, rather than guaranteeing every cost. Nonlocal status is defined by '
                    'the appropriate Hong Kong study-entry permission. University admission, '
                    "academic merit and the actual award's conditions apply."},
 '543': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['TR'],
         'kind': 'programme-overview',
         'summary': "Türkiye postgraduate scholarships state TRY 6,500/month for master's students "
                    'or TRY 9,000 for doctoral students, with placement, tuition, housing, '
                    'insurance, Turkish preparation and start/completion flights. Standard '
                    "duration includes one preparation year plus two master's or four doctoral "
                    'years. The source gives age 30/35 maxima by track; applicants can select only '
                    'courses made available for their educational background. Actual admission and '
                    'programme rules apply.'},
 '55': {'categories': ['scholarships'],
        'evidence': ["Target during the stay: master's students. Sending-country/institutional "
                     'affiliation and the actual programme conditions are distinct from a '
                     'universal Slovak-citizenship rule.',
                     'Full published programme-specific application, academic/language and funding '
                     'conditions apply; travel/supplements require the stated Slovak '
                     'education-sector affiliation.'],
        'host_countries': ['CZ'],
        'kind': 'programme-overview',
        'summary': "Bilateral mobility to Czechia for master's students: 3–10 months. Published "
                   'host support is CZK 12,000/month for students who have not yet earned a '
                   "master's-equivalent degree; study, dormitory and meal terms follow local "
                   'rules. Slovak-funded travel requires the stated sending-sector affiliation.'},
 '555': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['TR'],
         'kind': 'programme-overview',
         'summary': 'TÜBİTAK2221 supports invited visiting or sabbatical scientists based abroad '
                    'on 7-day-to 12-month Turkish visits. Published monthly ceilings are TRY '
                    '66,000 for visiting researchers or TRY 84,000 for sabbatical scientists, with '
                    'return-flight and conditional insurance support. A Turkish host scientist '
                    'submits the application; overseas PhD-related employment and the specified '
                    'experience conditions apply. Duration and benefits differ by track.'},
 '559': {'categories': ['scholarships', 'training'],
         'evidence': [],
         'host_countries': ['GR'],
         'kind': 'programme-overview',
         'summary': "SAIA's IKY overview describes an October–May Greek language/culture course "
                    'for non-Greek applicants with relevant humanities/social/political degrees or '
                    'Greek-teaching backgrounds, aged up to 40 with basic Greek. Published support '
                    'is EUR 150/month, EUR 200 per stay, tuition waiver, meals, housing and '
                    "public-hospital care. This is SAIA's processed factual catalogue description; "
                    "IKY's own website is not accessed and the yearless closing remains undated."},
 '56': {'categories': ['scholarships'],
        'evidence': ["Target during the stay: bachelor's students, master's students, doctoral "
                     'students, university teachers, researchers. Sending-country/institutional '
                     'affiliation and the actual programme conditions are distinct from a '
                     'universal Slovak-citizenship rule.',
                     'Full published programme-specific application, academic/language and funding '
                     'conditions apply; travel/supplements require the stated Slovak '
                     'education-sector affiliation.'],
        'host_countries': ['CZ'],
        'kind': 'programme-overview',
        'summary': "Bilateral mobility to Czechia for bachelor's students, master's students, "
                   'doctoral students, university teachers, researchers: one month. The host '
                   'covers summer-school registration/tuition, accommodation, meals and '
                   'excursions; some schools reimburse local public transport. Slovak-funded '
                   'travel requires the stated sending-sector affiliation.'},
 '569': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'CERN Scientific Associateships support established researchers with an '
                    'existing CERN professional relationship, while they remain employed by their '
                    'home institution. Published support is CHF 6,361–9,010 net/month by '
                    'experience, with conditional family/travel assistance. The visit lasts up to '
                    'one year, potentially extended to two; it involves eligible particle-physics '
                    'or related work. This is not an unrestricted direct early-career student '
                    'award.'},
 '572': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': "CERN's Doctoral Student Programme supports eligible member-country doctoral "
                    'students whose 6–36-month CERN work contributes to their thesis. Published '
                    'support is CHF 3,719/month, travel, health insurance and conditional family '
                    'allowances. English or French and relevant doctoral enrolment are required; '
                    'experimental/theoretical particle physics is excluded for this route. Time '
                    'back at the home university can extend the overall calendar period but is not '
                    'funded.'},
 '581': {'categories': ['grants'],
         'evidence': [],
         'host_countries': [],
         'kind': 'institutional-grant',
         'summary': 'ERC Starting Grants support researchers 2–7 years after their PhD, subject to '
                    'permitted extensions, with a host legal entity in an EU or associated '
                    'country. Funding is normally up to EUR 1.5 million over five years, with '
                    'conditional additional funding. This is principal-investigator-led research '
                    "funding, not an individual student scholarship; the catalogue's yearless "
                    'closing does not establish a current dated call.'},
 '582': {'categories': ['grants'],
         'evidence': [],
         'host_countries': [],
         'kind': 'institutional-grant',
         'summary': 'ERC Consolidator Grants fund independent research leaders with the specified '
                    '7–12-year post-PhD track and an eligible EU/associated-country host. Standard '
                    'support is up to EUR 2 million over five years, with conditional additional '
                    'funding. The English and Slovak SAIA entries disagree on the indirect-cost '
                    'percentage, so no single rate is asserted here. Researchers of any '
                    'nationality may qualify under the full call rules.'},
 '583': {'categories': ['grants'],
         'evidence': [],
         'host_countries': [],
         'kind': 'institutional-grant',
         'summary': 'ERC Advanced Grants support established research leaders with an outstanding '
                    'ten-year record and an eligible EU/associated-country host. Funding is '
                    'normally up to EUR 2.5 million over five years, with conditional additional '
                    'funding. This is a competitively assessed research project, not a student '
                    'award; nationality alone does not determine eligibility and the yearless '
                    'catalogue closing is not assigned a year.'},
 '585': {'categories': ['grants'],
         'evidence': [],
         'host_countries': [],
         'kind': 'institutional-grant',
         'summary': 'ERC Synergy Grants support jointly designed frontier research by two to four '
                    'principal investigators. The Slovak SAIA variant states up to EUR 10 million '
                    'over six years, with conditional additional funding; the English entry leaves '
                    'its amount field empty. Host-location and team rules apply. The two variants '
                    'describe one programme, not separate awards or guaranteed personal payments.'},
 '587': {'categories': ['grants'],
         'evidence': [],
         'host_countries': [],
         'kind': 'institutional-grant',
         'summary': 'COST funds international research-network activities rather than the research '
                    'itself. Proposed Actions normally run four years and require at least seven '
                    'participating countries, including the specified inclusion-country and '
                    'young-researcher proportions. Published initial/following-year budgets are '
                    'EUR 150,000/EUR 180,000. Both catalogue variants close on October 28, 2026, '
                    'but disagree on the clock time and supply no timezone.'},
 '588': {'categories': ['grants'],
         'evidence': [],
         'host_countries': [],
         'kind': 'institutional-grant',
         'summary': 'EUREKA is an international market-oriented civilian R&D framework for '
                    'companies and research organisations. Individual projects require partners in '
                    'at least two member countries; other routes include SME-focused Eurostars and '
                    'industrial clusters. Funding, duration and national eligibility depend on the '
                    'chosen instrument and country rules. This institutional cooperation framework '
                    'does not promise a single personal scholarship amount.'},
 '592': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['KR'],
         'kind': 'programme-overview',
         'summary': 'KDI School supports eligible university graduates on public-policy/management '
                    "master's or doctoral study in Korea, with English required and professional "
                    "experience preferred. The source describes approximately 1.5-year master's "
                    'and three-year doctoral programmes. Funding depends on programme resources; a '
                    'fixed stipend, fee waiver or full package is not supplied and is not '
                    "invented. Applicants must meet the actual school's degree and "
                    'scholarship-selection requirements.'},
 '602': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['US'],
         'kind': 'programme-overview',
         'summary': 'Fulbright Schuman supports EU citizens on U.S. projects with a strong EU/U.S. '
                    'policy or institutional dimension. Doctoral support is up to EUR 3,000/month; '
                    'postdoctoral/lecturing and international-educator tracks up to EUR '
                    '4,000/month, plus EUR 2,000 travel. The NATO track instead states EUR 9,000 '
                    'over three months. Degree, host and thematic rules differ; Innovation '
                    'consideration uses the relevant doctoral/postdoctoral application rather than '
                    'a separate award form.'},
 '603': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'U.S. citizens with a terminal degree can undertake three-to-five-month '
                    'teaching/research visits in Slovakia; applicants resident there are excluded. '
                    'Published support includes USD 2,800/month, a location-dependent housing '
                    'allowance and travel/visa allowances. Dependent benefits have residence '
                    'conditions and exclude Flex awards. Host invitation, teaching duties and '
                    'project-specific language requirements apply.'},
 '604': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['US'],
         'kind': 'programme-overview',
         'summary': 'The Fulbright Scholar-in-Residence programme brings Slovak scholars to U.S. '
                    'colleges for one or two semesters of teaching and wider academic/community '
                    'engagement. U.S. institutions submit the application; scholars do not simply '
                    'apply for a universal direct award. Support includes a monthly stipend, '
                    'return airfare, basic exchange health benefits and administrative help, with '
                    'host and programme-specific conditions.'},
 '605': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'Fulbright Schuman supports eligible U.S. citizens studying or researching EU '
                    'affairs in EU member states for three to nine months. Published monthly '
                    'support is EUR 3,000 for students or EUR 4,000 for scholars, plus EUR 2,000 '
                    'travel and health benefits, subject to award terms. Applicants arrange '
                    'appropriate EU host affiliations. This incoming U.S.-citizen track is '
                    'distinct from the EU-citizen-to-U.S. programme.'},
 '606': {'categories': ['grants'],
         'evidence': [],
         'host_countries': ['SK'],
         'kind': 'institutional-grant',
         'summary': 'Slovak institutions can invite a U.S. Fulbright lecturer already based in '
                    'Europe for a visit of up to five days. The Commission covers economy '
                    'international airfare; the Slovak host covers meals, accommodation and local '
                    'travel and makes practical arrangements. The visit supports lectures, '
                    'workshops or other outreach. It is an institutional invitation for an '
                    'existing Fulbright scholar, not an unrestricted student travel grant.'},
 '608': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'The Fulbright Specialist programme brings eligible U.S. experts to Slovakia '
                    'for short academic or professional projects lasting two to six weeks. '
                    'Published support includes airfare, a stipend/per diem, accommodation and '
                    'administrative assistance. Host coordination and the specialist selection '
                    'rules apply; this expert exchange is distinct from a full-degree scholarship '
                    'or the longer U.S. Scholar award.'},
 '609': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Eligible U.S. citizens/nationals can spend nine months on a Slovak study or '
                    "research project. A bachelor's degree normally must be held by the start; "
                    'applicants holding a PhD during the grant are excluded. Published support '
                    'includes USD 1,300/month, USD 1,500 airfare and conditional professional/visa '
                    'support, subject to change. Host affiliation is required; residence '
                    'exclusions and project-specific language needs apply.'},
 '610': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Fulbright ETAs assist with English teaching at Slovak secondary schools for '
                    'ten months, normally 15–18 hours weekly. Eligible U.S. citizens/nationals '
                    "need a bachelor's degree by the start; Slovak-residence exclusions apply. "
                    'Support includes USD 1,300/month, USD 1,500 airfare and conditional '
                    'visa/professional assistance, subject to award terms. Placement is assigned; '
                    'prior teaching experience and Slovak fluency are not required.'},
 '616': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'EMBO postdoctoral fellowships support cross-border molecular-biology '
                    'research, normally for up to 24 months, with host-country-dependent '
                    'subsistence and conditional family, childcare and relocation support. A '
                    'doctorate, recent PhD and first-author publication are required. '
                    'Host/nationality links to an EMBC member state and mobility restrictions '
                    'apply; returning to the PhD country, laboratory or supervisor is excluded '
                    'under the stated rules.'},
 '617': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'EMBO short-term fellowships cover a research visit of one week to three '
                    'months, with travel and host-country-dependent subsistence for the fellow '
                    'only. Eligible doctoral students or researchers within ten years of their PhD '
                    'must move between countries, with one laboratory in an EMBC member state. The '
                    'visit must advance joint research; courses and bridging extensions are '
                    'excluded and return to the home laboratory is required.'},
 '618': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'Branco Weiss fellowships support unusually original postdoctoral research at '
                    'an academic institution worldwide. Published funding is CHF 100,000/year for '
                    'legitimate research costs, for up to five years in two reviewed phases. '
                    'Applicants need a PhD obtained within five years and cannot have held a '
                    "faculty-equivalent position. The source's age wording is internally "
                    'problematic, so it is not presented as a reliable age threshold.'},
 '623': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'DBU supports Slovak citizens permanently resident in Slovakia on 6–12-month '
                    'environmental placements in Germany. Applicants need a university degree '
                    'completed within five years, a relevant environmental proposal and '
                    'German/English; eligible ongoing doctoral students have specified conditions. '
                    'Published support is EUR 1,450/month plus insurance and German preparation. A '
                    'host can be arranged later; repeat applications are excluded.'},
 '627': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'Za vzděláním do zahraničí provides need-based supplementary funding for '
                    'eligible international study stays, usually a semester. Applicants must '
                    'calculate and justify a funding gap after other scholarships or personal '
                    'resources; no universal grant amount is stated. Applications are accepted in '
                    'the first week of a month and at least two months before the stay. This '
                    "programme is distinct from the foundation's first-year Lehčí rozběh award."},
 '63': {'categories': ['scholarships'],
        'evidence': ["Target during the stay: bachelor's students, master's students. "
                     'Sending-country/institutional affiliation and the actual programme '
                     'conditions are distinct from a universal Slovak-citizenship rule.',
                     'Full published programme-specific application, academic/language and funding '
                     'conditions apply; travel/supplements require the stated Slovak '
                     'education-sector affiliation.'],
        'host_countries': ['HR'],
        'kind': 'programme-overview',
        'summary': "Bilateral mobility to Croatia for bachelor's students, master's students: at "
                   'least four months. The host waives tuition and provides EUR 212/month toward '
                   'dormitory, meal and public-transport costs. Slovak-funded travel requires the '
                   'stated sending-sector affiliation.'},
 '631': {'categories': ['scholarships'],
         'deadline_evidence': {'date': '2026-05-12',
                               'field': 'Podávanie žiadosti',
                               'literal': '12. mája 2026'},
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'status': 'expired',
         'summary': "Martin Filko supports eligible Slovak citizens on full-time master's/doctoral "
                    'study at the specified top 200 foreign institutions, for up to two academic '
                    'years. The EUR 154,500 figure is the 2026/27 total programme budget, not one '
                    "student's award. Covered study/living costs and capped travel/books/insurance "
                    'depend on actual expenses; graduates owe a period of public service. The same '
                    'source explicitly states a May 12,2026 application closing, now past.'},
 '634': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['CN'],
         'kind': 'programme-overview',
         'summary': "China's EU Programme supports eligible EU citizens on full bachelor's, "
                    "master's or doctoral degrees, with preceding qualifications, health and age "
                    'limits of 25/35/40 by track. The published package includes '
                    'tuition/registration and specified academic-fee waivers, dormitory housing, '
                    'pocket money, insurance and designated settling/domestic-travel support. No '
                    'fixed stipend rate is provided; applicants must satisfy the actual degree and '
                    'programme selection rules.'},
 '636': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['MK'],
         'kind': 'programme-overview',
         'summary': "North Macedonia's Macedonian-medium bachelor's route supports eligible full "
                    '3–4-year study. Published support includes tuition, visa/residence costs, '
                    'airfare, university housing/meals, MKD 5,000/month and intensive Macedonian '
                    'preparation. Admission, language-preparation and programme-specific '
                    'eligibility apply. This is distinct from the English-medium computing '
                    'programme; country metadata alone does not prove a blanket nationality '
                    'restriction.'},
 '64': {'categories': ['scholarships'],
        'evidence': [],
        'host_countries': ['DE'],
        'kind': 'programme-overview',
        'summary': "DAAD master's funding normally runs 10–24 months at EUR 992/month, with stated "
                   'travel, insurance and EUR 460/year study-material support plus conditional '
                   "extras. A bachelor's degree, separate German host admission and the actual "
                   'language/degree-recency rules apply; tuition is generally excluded. A distinct '
                   "one-year mobility route during Slovak master's study requires home recognition "
                   'and cannot be extended. Existing German study/residence restrictions remain.'},
 '655': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'Krídla supports selected talented students from Slovakia admitted to foreign '
                    'universities who show commitment to positive social change. The published EUR '
                    '12,000 is a total pool divided among the strongest applicants, not an '
                    'individual entitlement. Awards can meet study-related needs including travel, '
                    'housing, books, tuition or loan repayment. Selection and admission conditions '
                    'apply; the October 31,2026 local clock has no verified timezone.'},
 '656': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['SG'],
         'kind': 'programme-overview',
         'summary': "SINGA's catalogue overview supports outstanding international applicants on a "
                    'four-year Singapore PhD at the named participating universities. Published '
                    'support includes a scholarship, tuition and travel assistance, but this '
                    'source supplies no fixed personal rate. Applicants must satisfy '
                    "degree/research admission and programme selection conditions. The source's "
                    'linked programme label differs from its SINGA title, so no unverified details '
                    'are borrowed from that external route.'},
 '665': {'categories': ['grants'],
         'evidence': [],
         'host_countries': [],
         'kind': 'institutional-grant',
         'summary': 'MSCA Staff Exchanges fund consortia and staff secondments, not a universal '
                    'direct personal fellowship. At least three organisations in three countries '
                    'and the applicable EU/associated-country, sector and discipline rules are '
                    'required. Staff selected through their organisations undertake '
                    'one-to-twelve-month secondments within projects up to four years, return to '
                    'the sending organisation, and receive support alongside their continuing '
                    'salary.'},
 '666': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['IE'],
         'kind': 'programme-overview',
         'summary': 'Hardiman funds a four-year full-time Galway PhD at EUR 25,000/year plus fees. '
                    'Applicants need a first/upper-second honours degree and appropriate '
                    'supervisor/reference support; existing doctoral students and PhD holders are '
                    'excluded. Scholarship selection and PhD admission are separate, and the PhD '
                    'application can have a fee even though the scholarship application is free.'},
 '667': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['IE'],
         'kind': 'programme-overview',
         'summary': "Ireland's Government postgraduate scheme supports eligible research master's "
                    'study for one or two years or doctoral study for three or four. Its standard '
                    'maximum EUR 34,000/year comprises EUR 25,000 stipend, EUR 5,750 fee '
                    'contribution and EUR 3,250 direct research costs, rather than personal cash '
                    'of EUR 34,000. An additional eligible fee contribution up to EUR 4,000 is '
                    'conditional for qualifying non-EU candidates; host and academic rules apply.'},
 '674': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'DUO-Korea supports a reciprocal student pair: one Korean national and one '
                    'eligible European citizen, through partner institutions with a cooperation '
                    'agreement. Published support totals EUR 8,000 per pair, or EUR 4,000 per '
                    'participant for four months. The institutional exchange application and '
                    'enrolment rules apply; exchanges already underway are excluded. English and '
                    'Slovak catalogue entries describe this same paired project.'},
 '676': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'DAAD architecture/design/planning funding supports eligible postgraduate or '
                    'complementary study for 10–24 months at EUR 992/month, with travel, insurance '
                    'and conditional extras. Tuition is generally excluded. Applicants need the '
                    'relevant first degree, sufficient design/planning credits and separate German '
                    'admission; degree-recency and German-residence restrictions apply. Degree and '
                    'nondegree routes, and study outside Germany, have distinct limits.'},
 '678': {'categories': ['scholarships'],
         'evidence': ['Target during the stay: university teachers. Sending-country/institutional '
                      'affiliation and the actual programme conditions are distinct from a '
                      'universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.',
                      'Independent full-facts review restored same-source citizenship, subject, '
                      'language, prior-study or qualification/experience restrictions.'],
         'host_countries': ['RS'],
         'kind': 'programme-overview',
         'summary': 'Serbian lecture mobility supports eligible Slovak citizens with the required '
                    'Slovak institutional link on 1–30-day university-teaching visits. The host '
                    'provides a private dormitory room and statutory meal/pocket allowances. A '
                    'Serbian host invitation and relevant lecture programme are required. '
                    'Slovak-funded travel depends on specified education-sector affiliation; '
                    'partner acceptance remains necessary after Slovak selection.'},
 '682': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'NSP supports eligible Slovak permanent residents on 2–6-month postdoctoral '
                    'research abroad, followed by return to their home institution. Applicants '
                    'need eligible Slovak noncommercial research employment and normally a PhD '
                    'within ten years, with specified exceptional extensions. Monthly support '
                    'depends on the approved destination/track rate; the published country table '
                    'is not a nationality whitelist. Conditional travel support up to EUR 1,500 is '
                    'separate.'},
 '683': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['KR'],
         'kind': 'programme-overview',
         'summary': 'GKS research supports eligible postdoctoral, academic-staff and professional '
                    'research in Korea for six months or one year. Published support includes KRW '
                    '1.5 million/month, conditional research allowances, settling, health and '
                    'travel contributions. This route requires an accepting Korean university '
                    'through University Track, not a SAIA application. Degree, language, health '
                    'and age-up-to-45 rules apply; former GKS students are excluded from 2025.'},
 '685': {'categories': ['scholarships', 'training'],
         'evidence': [],
         'host_countries': ['LT'],
         'kind': 'programme-overview',
         'summary': 'Lithuanian summer-language awards provide a free four-week course and EUR 962 '
                    'for eligible foreign students/staff; housing, travel and other expenses come '
                    'from that stipend. Russian/Belarusian citizenship and affiliation to their '
                    'institutions are excluded; native/C1 Lithuanian speakers cannot apply. At '
                    'most three awards since 2017 and one host application are allowed. The source '
                    'lists 2026 courses without proving a currently open call.'},
 '686': {'categories': ['grants'],
         'evidence': [],
         'host_countries': [],
         'kind': 'institutional-grant',
         'summary': 'MSCA Doctoral Networks fund international institutional consortia; the listed '
                    'November 24, 2026 closing is a consortium deadline, not an individual PhD '
                    'vacancy deadline. Recruits of any nationality must not already hold a PhD and '
                    'must satisfy the 12-in-36-month mobility rule. Fellowships normally last 3–36 '
                    'months, with joint-doctorate exceptions. Individual candidates apply later to '
                    "the funded network's actual vacancies."},
 '687': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'DAAD visual arts/design/communication/film funding supports eligible '
                    'practice-based postgraduate or complementary study for 10–24 months at EUR '
                    '992/month, with travel, insurance and conditional extras. Tuition is '
                    'generally excluded; art history/scientific arts study uses other routes. A '
                    'relevant first degree, separate host admission and degree-recency/residence '
                    'rules apply. Degree/nondegree duration and joint-programme exceptions '
                    'differ.'},
 '689': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['JP'],
         'kind': 'programme-overview',
         'summary': 'MEXT Young Leaders supports eligible public-sector employees on a one-year '
                    'English-medium GRIPS programme, with JPY 242,000/month, tuition and return '
                    "airfare. A bachelor's degree, at least three years of relevant "
                    'public-administration experience, English B2 and health requirements apply. '
                    "The catalogue's 2026–27 duration conflicts with its age/experience reference "
                    'date in October 2027; applicants must clarify the actual call instead of '
                    'assuming a corrected year.'},
 '693': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['ES', 'PT'],
         'kind': 'programme-overview',
         'summary': 'La Caixa Junior Leader supports eligible postdoctoral researchers, normally '
                    'two to seven years after PhD, at qualifying Spanish/Portuguese hosts. Up to '
                    'EUR 320,100 funds a three-year project, including employment/research costs, '
                    'rather than that sum being personal cash. Professional training accompanies '
                    'the award. Incoming and Retaining tracks have different mobility rules; '
                    'candidate and host submit jointly and host excellence requirements apply.'},
 '694': {'categories': ['scholarships'],
         'evidence': ["Target during the stay: bachelor's students, master's students. "
                      'Sending-country/institutional affiliation and the actual programme '
                      'conditions are distinct from a universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.',
                      'Independent full-facts review restored same-source citizenship, subject, '
                      'language, prior-study or qualification/experience restrictions.'],
         'host_countries': ['MD'],
         'kind': 'programme-overview',
         'summary': "Moldovan 5–10-month mobility is restricted to Slovak bachelor's/master's "
                    'students with at least one completed semester, Moldovan or English and a '
                    'passport valid at least 18 months after the stay starts. Support includes a '
                    'Slovak EUR 441/month supplement, Moldovan track-dependent stipends, free '
                    'tuition and locally governed dormitory arrangements. Slovak-funded travel '
                    'depends on specified education-sector affiliation; host acceptance and '
                    'complete documents are required.'},
 '695': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Moldova to Slovakia for bachelor's students, master's "
                    'students; duration: 5 - 10 months. Published support: EUR 620/month for '
                    'university students. The sending side covers international travel.'},
 '699': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': "SECIHTI supports eligible Mexican applicants on master's or doctoral study "
                    'abroad. Living-cost support and any additional benefits depend on the '
                    'relevant call and cooperation agreement; the catalogue provides no universal '
                    'fixed amount or duration. Applicants must meet the specific postgraduate and '
                    'national eligibility requirements. This global outgoing Mexican programme is '
                    'not restricted to a Slovak host merely because SAIA publishes it.'},
 '70': {'categories': ['scholarships'],
        'evidence': ['Target during the stay: doctoral students. Sending-country/institutional '
                     'affiliation and the actual programme conditions are distinct from a '
                     'universal Slovak-citizenship rule.',
                     'Full published programme-specific application, academic/language and funding '
                     'conditions apply; travel/supplements require the stated Slovak '
                     'education-sector affiliation.'],
        'host_countries': ['HR'],
        'kind': 'programme-overview',
        'summary': 'Bilateral mobility to Croatia for doctoral students: at least one month. The '
                   'host waives tuition and provides EUR 239/month toward dormitory, meal and '
                   'public-transport costs. Slovak-funded travel requires the stated '
                   'sending-sector affiliation.'},
 '700': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from China to Slovakia for bachelor's students, master's "
                    'students, doctoral students, university teachers, researchers and other '
                    'listed applicants; duration: 3 weeks (in August). The host provides the '
                    'language course, accommodation, meals and a statutory daily allowance. The '
                    'sending side covers international travel.'},
 '701': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Finland to Slovakia for bachelor's students, master's "
                    'students, doctoral students, university teachers; duration: 10 days. Host '
                    'support includes accommodation plus the statutory daily allowance. The '
                    'sending side covers international travel.'},
 '702': {'categories': ['grants'],
         'evidence': [],
         'host_countries': [],
         'kind': 'institutional-grant',
         'summary': 'Visegrad Grants support eligible organisational projects lasting up to 18 '
                    'months. Typical budgets are EUR 25,000–35,000; up to 100% of project costs '
                    'can be supported, including at most 15% overhead, rather than a guaranteed '
                    'individual award. Usually three partners from three V4 countries are needed; '
                    'adjacent-country cross-border projects have a two-partner exception. Eligible '
                    "organisations and the fund's thematic/territorial conditions apply."},
 '703': {'categories': ['grants'],
         'evidence': [],
         'host_countries': [],
         'kind': 'institutional-grant',
         'summary': 'Visegrad+ supports eligible organisational projects benefiting Eastern '
                    'Partnership or Western Balkan communities, lasting up to 18 months. Typical '
                    'budgets are EUR 25,000–35,000, with up to 100% project support and at most '
                    '15% overhead. Normally three V4 organisations plus one regional partner are '
                    'required; regional-applicant and Ukrainian exceptions differ. This is an '
                    'institutional partnership grant, not a personal study scholarship.'},
 '704': {'categories': ['grants'],
         'evidence': [],
         'host_countries': [],
         'kind': 'institutional-grant',
         'summary': 'Visegrad Strategic Grants support eligible organisational partnerships '
                    'involving all four V4 countries for 12–36 months. Typical budgets are EUR '
                    '35,000–45,000; support can cover up to 100% of project costs, including at '
                    'most 15% overhead. Projects must meet the current strategic priorities and '
                    'demonstrate regional impact. Only one application in this scheme is permitted '
                    'while another project is active; the yearless June closing is not a dated '
                    'open call.'},
 '705': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['ES', 'PT'],
         'kind': 'programme-overview',
         'summary': 'La Caixa INPhINIT supports up to four years of doctoral study in Spain or '
                    'Portugal, including employment, research, tuition and professional training '
                    'costs. No fixed personal stipend is given in this source. Applicants of all '
                    'nationalities can compete subject to doctoral admission and academic/language '
                    'requirements. Incoming and Retaining tracks have distinct mobility '
                    'conditions; the published annual quota is 30 awards in each, rather than '
                    'guaranteed admission.'},
 '707': {'categories': ['training'],
         'evidence': [],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'GROWNi offers free individual mentoring for students and early-career '
                    'professionals on education, career, projects or personal development. '
                    'Participants choose a mentor and request support; that mentor decides whether '
                    'to accept. Duration and scope are agreed between them, rather than a fixed '
                    'funded stay. Registration and a platform profile are required. This is '
                    'mentoring support, with no stated cash scholarship.'},
 '71': {'categories': ['scholarships'],
        'evidence': ["Target during the stay: bachelor's students, master's students, doctoral "
                     'students, university teachers, researchers. Sending-country/institutional '
                     'affiliation and the actual programme conditions are distinct from a '
                     'universal Slovak-citizenship rule.',
                     'Full published programme-specific application, academic/language and funding '
                     'conditions apply; travel/supplements require the stated Slovak '
                     'education-sector affiliation.'],
        'host_countries': ['HR'],
        'kind': 'programme-overview',
        'summary': "Bilateral mobility to Croatia for bachelor's students, master's students, "
                   'doctoral students, university teachers, researchers: two weeks. The host '
                   'covers course tuition, accommodation and a meal allowance. Slovak-funded '
                   'travel requires the stated sending-sector affiliation.'},
 '710': {'categories': ['internships'],
         'evidence': [],
         'host_countries': ['CH'],
         'kind': 'programme-overview',
         'summary': 'CERN Technical Studentships support eligible member-country '
                    "bachelor's/master's students with at least 18 months of relevant study on "
                    '4–12-month placements. Published support is CHF 3,407 net/month, travel, '
                    'insurance and conditional family assistance. English or French is required. '
                    'Applied science, engineering and computing qualify; theoretical/experimental '
                    'particle physics is excluded. Complete academic references are mandatory and '
                    'longer stays can be preferred.'},
 '711': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['CH'],
         'kind': 'programme-overview',
         'summary': "CERN's career-break fellowship supports eligible returners to full-time "
                    'Geneva research for 6–36 months at CHF 5,185–7,994/month, with conditional '
                    'travel, settling and family support. A research break of at least two years '
                    'for family or health reasons is required; unemployment or a field change does '
                    "not count. Qualification/experience rules differ for bachelor's, master's and "
                    'physics PhD candidates; degree and research-fit requirements apply.'},
 '712': {'categories': ['training'],
         'evidence': [],
         'host_countries': ['CH'],
         'kind': 'programme-overview',
         'summary': "CERN's three-month summer-school overview offers lectures, facility visits, "
                    'workshops and expert discussions in relevant science/engineering fields. The '
                    "title targets bachelor's/master's students, while the body describes recent "
                    'graduates with at least a first degree; that eligibility conflict must be '
                    'checked. Good English is required and French welcomed. This source states no '
                    'personal funding rate; its usual January closing has no verified year.'},
 '716': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'DAAD performing-arts funding supports relevant first-degree holders on German '
                    'postgraduate or complementary study for 10–24 months at EUR 992/month plus '
                    'travel, insurance and conditional extras. Tuition is generally excluded. '
                    'Practice disciplines qualify; theatre/dance theory uses other routes. '
                    'Separate host admission, portfolio, degree-recency and German-residence rules '
                    'apply; nondegree study and joint programmes have different duration/mobility '
                    'limits.'},
 '717': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'DAAD supports eligible jointly supervised/cotutelle doctoral research in '
                    'Germany for 7–24 months, potentially split into visits. Published support is '
                    'EUR 1,400/month, insurance/travel, research support and conditional extras; '
                    'tuition is generally excluded. Home/host supervision agreements, '
                    'degree-recency and doctoral-start/residence rules apply. Track-specific '
                    'extensions differ, and time at the home institution is not funded. Supervisor '
                    'visits have separate limits.'},
 '72': {'categories': ['scholarships'],
        'evidence': ["Target during the stay: bachelor's students, master's students. "
                     'Sending-country/institutional affiliation and the actual programme '
                     'conditions are distinct from a universal Slovak-citizenship rule.',
                     'Full published programme-specific application, academic/language and funding '
                     'conditions apply; travel/supplements require the stated Slovak '
                     'education-sector affiliation.'],
        'host_countries': ['PL'],
        'kind': 'programme-overview',
        'summary': "Bilateral mobility to Poland for bachelor's students, master's students: one "
                   'semester. Published Slovak support is EUR 550/month with tuition waived; '
                   'accommodation and meals are charged. This route is for the specified '
                   'Polish/Slavic/Slovak language studies. Slovak-funded travel requires the '
                   'stated sending-sector affiliation.'},
 '724': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Vertical supports disadvantaged young applicants, including specified '
                    "orphaned or conflict-zone groups, preparing for a Slovak bachelor's degree. "
                    'The four-year pathway includes one year of Slovak preparation and three '
                    'degree years, with EUR 280/month for basic living needs. Travel, health '
                    'insurance, document translation and visa costs are excluded. Strong '
                    'secondary-school results are required and priority countries can vary '
                    'annually.'},
 '725': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['US'],
         'kind': 'programme-overview',
         'summary': 'FLEX supports eligible Slovak citizens on a U.S. high-school year with a host '
                    'family, school placement, flights, stipend and specified insurance. '
                    'School-year, academic, English and J-1 visa rules apply. Published birth '
                    "dates concern the 2026/27 selection; the next cycle's age range awaits its "
                    'own call. Passport, excess personal spending, baggage and telephone/internet '
                    'costs are excluded; dental and existing chronic-condition care are not '
                    'insured.'},
 '726': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['PL'],
         'kind': 'programme-overview',
         'summary': 'Ulam supports eligible postdoctoral/research visits to Poland for 6–24 months '
                    'at EUR 2,400/month plus travel support. Applicants provide a PhD diploma, '
                    'research publications, employment evidence and host invitation; research '
                    "quality and available funds determine selection. The catalogue's April 15 "
                    'closing has no verified year. This is a research-mobility grant; source '
                    'geography alone does not establish a citizenship restriction.'},
 '729': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['PL'],
         'kind': 'programme-overview',
         'summary': "My First Choice's catalogue overview describes a 24-month Polish master's "
                    'scholarship at PLN 2,000/month. Applicants need the preceding degree, B2 '
                    "Polish/English and no completed or current master's study. Its documents "
                    'refer to degree completion after 2017 and a medical certificate after January '
                    '2019, which are source-specific historical conditions, not refreshed '
                    'requirements. Admission/award rules need checking; the yearless April 6 '
                    'closing remains undated.'},
 '73': {'categories': ['scholarships'],
        'evidence': ["Target during the stay: bachelor's students, master's students, doctoral "
                     'students, university teachers, researchers. Sending-country/institutional '
                     'affiliation and the actual programme conditions are distinct from a '
                     'universal Slovak-citizenship rule.',
                     'Full published programme-specific application, academic/language and funding '
                     'conditions apply; travel/supplements require the stated Slovak '
                     'education-sector affiliation.'],
        'host_countries': ['PL'],
        'kind': 'programme-overview',
        'summary': "Bilateral mobility to Poland for bachelor's students, master's students, "
                   'doctoral students, university teachers, researchers: one month. The host '
                   'covers the summer course, accommodation, meals and pocket money. Slovak-funded '
                   'travel requires the stated sending-sector affiliation.'},
 '730': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Penta scholarships support eligible Ukrainian applicants undertaking a full '
                    "Slovak master's degree, with the source also describing longer medical study. "
                    'Published support covers standard-duration tuition plus EUR 300/month for '
                    'accommodation and other expenses; an intensive Slovak course may be included. '
                    'Academic admission and programme-specific selection conditions apply. The '
                    'catalogue overview does not by itself establish a current application '
                    'window.'},
 '731': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['AU'],
         'kind': 'programme-overview',
         'summary': "Monash research master's/PhD scholarships normally fund two/3.5 years, "
                    'potentially extendable. Basic full-time living stipends are AUD 37,145/year '
                    'in 2026; tuition-offset values of AUD 41,000–59,600 are fee coverage, not '
                    'cash. Tuition awards and AUD 1,000/2,000 relocation support are conditional. '
                    'Research/English admission and competitive scholarship selection are '
                    'separate; faculty supervisor/proposal or prior-assessment requirements '
                    'apply.'},
 '733': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'Nederlandse Taalunie supports eligible Dutch-studies research for up to six '
                    'months; no fixed grant amount is given. The source targets Dutch-origin '
                    'researchers and other relevant academics, ideally in overseas Dutch '
                    'departments, who cannot access other national/international awards. Research '
                    'must contribute to a thesis or habilitation and can take place inside or '
                    'outside the Dutch-speaking region when relevant. Actual research-fit and '
                    'funding rules apply.'},
 '734': {'categories': ['fellowships', 'training'],
         'evidence': [],
         'host_countries': ['US'],
         'kind': 'programme-overview',
         'status': 'expired',
         'summary': 'Obama Foundation Scholars offers a fully funded nine-month Columbia programme '
                    'for emerging leaders with community impact, English fluency and a commitment '
                    'to return and serve their communities. Study, workshops and professional '
                    'development form the programme; no personal rate is stated. The source '
                    'explicitly says applications are currently not accepted and there is no '
                    '2026/27 intake. Participants cannot campaign for elected office during the '
                    'programme.'},
 '735': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'DAAD supports eligible university teachers in practice-based arts and '
                    'architecture on 1–3-month German visits. Published support is EUR 2,000/month '
                    'for eligible lecturers or EUR 2,150 for professors, plus travel and '
                    'conditional accessibility support. Relevant academic employment and a '
                    'suitable professional plan apply. The award cannot be extended and another '
                    "award normally requires a three-year interval; the source's recurring and "
                    'dated closing wording differ.'},
 '736': {'categories': ['scholarships', 'training'],
         'evidence': [],
         'host_countries': ['US'],
         'kind': 'programme-overview',
         'summary': 'SUSI supports eligible undergraduates aged 18–25 who are Slovak citizens or '
                    'permanent residents on five-week U.S. civic-leadership seminars. Support '
                    'includes programme, travel, housing/meals, visas, specified incidentals and '
                    'basic insurance. Strong English/academics/community leadership and at least '
                    'one semester before graduation are required, with return to home study. '
                    'Little prior overseas/U.S. experience is preferred; no fixed personal stipend '
                    'is stated.'},
 '738': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'Konrad Adenauer supports eligible foreign students on German '
                    "bachelor's/master's/PhD study for at least four semesters, at EUR 992/month "
                    'for students or EUR 1,400 for doctoral candidates, with stated conditional '
                    'extras. Applicants need at least two undergraduate semesters, German B2, '
                    'academic excellence and civic/political engagement consistent with foundation '
                    'values. Medicine/dentistry, graduates and completed-PhD applicants are '
                    'excluded; admission rules apply.'},
 '741': {'categories': ['scholarships', 'training'],
         'evidence': [],
         'host_countries': ['FI'],
         'kind': 'programme-overview',
         'summary': "EDUFI's three-week Finnish language/culture courses target students studying "
                    'Finnish at universities outside Finland. Course fees, materials and '
                    "activities are free; accommodation is free subject to the source's "
                    'Sweden/Norway exception, and a EUR 320 travel grant is stated. '
                    'Course-specific selection and quotas apply. The usual March closing has no '
                    'verified year, and no unsupported universal cash or full-cost package is '
                    'inferred.'},
 '742': {'categories': ['training'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': "DAAD's five-day summer seminars support eligible former "
                    'German-study/research/work visitors, normally with a German degree or a '
                    'university stay of at least three months. Travel, hotel accommodation in '
                    'shared rooms and meals are covered; the previous stay need not have been '
                    'DAAD-funded. Applicants provide a CV and a short presentation aligned with '
                    "the year's seminar theme. This differs from general German-language summer "
                    'courses; closing dates are yearless.'},
 '743': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'Einstein Forum/Wittenstein supports researchers under 35 on a 5–6-month '
                    'Caputh stay with EUR 10,000 plus travel. The proposed '
                    'humanities/social/natural-science project must differ clearly from previous '
                    'work; research towards a doctoral degree is excluded. The project need not '
                    'finish during the stay. A proposal and two references are required; the '
                    'yearless May 15 closing does not establish a current intake.'},
 '746': {'categories': ['internships'],
         'evidence': [],
         'host_countries': ['BE', 'LU'],
         'kind': 'programme-overview',
         'summary': 'Blue Book offers five-month Commission administrative/translation placements '
                    'in Brussels or Luxembourg at EUR 1,538.16/month, with stated '
                    'travel/visa/medical reimbursement. Applicants need a completed three-year '
                    "degree and no more than six weeks' prior relevant EU-institution experience. "
                    "Language rules differ by track. Housing is the trainee's responsibility; "
                    'recurring March/August local CET closing clocks have no verified year and '
                    'stay undated.'},
 '748': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'MAECI supports eligible invited academic/cultural visits of up to ten days '
                    'between Italy and abroad. Standard contributions are EUR 250/day for the '
                    'first five days and EUR 50/day thereafter, capped at EUR 1,500; '
                    'distant-priority-country rates differ, capped at EUR 3,000. Grants partly '
                    'cover costs and are paid after documented completion. Residence/direction, '
                    'official invitation and economy-travel rules apply; the host cannot make the '
                    'visit conditional on funding.'},
 '75': {'categories': ['scholarships'],
        'evidence': ['Target during the stay: doctoral students. Sending-country/institutional '
                     'affiliation and the actual programme conditions are distinct from a '
                     'universal Slovak-citizenship rule.',
                     'Full published programme-specific application, academic/language and funding '
                     'conditions apply; travel/supplements require the stated Slovak '
                     'education-sector affiliation.'],
        'host_countries': ['PL'],
        'kind': 'programme-overview',
        'summary': 'Bilateral mobility to Poland for doctoral students: 1–3 months. Published '
                   'Slovak support is EUR 650/month with tuition waived; accommodation and meals '
                   'are charged. Slovak-funded travel requires the stated sending-sector '
                   'affiliation.'},
 '750': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['FR'],
         'kind': 'programme-overview',
         'summary': "Lille's selected Graduate Programmes offer competitive master's excellence "
                    'awards of EUR 8,500/academic year for international students first arriving '
                    'in France or EUR 4,500 for continuing students, paid in ten instalments. '
                    'Renewal for a second year is possible. Admission and scholarship applications '
                    'are separate; relevant academic and programme-language requirements apply. '
                    'Eligible subject programmes and selection rules limit this university award.'},
 '751': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['ID'],
         'kind': 'programme-overview',
         'summary': "KNB supports eligible international bachelor's/master's/PhD study in "
                    'Indonesia after one language-preparation year. Age ceilings are 21/35/40 by '
                    'degree; prior qualifications, English certification, strong academics, health '
                    'and no other scholarship are required. Published support includes specified '
                    'settling, living, research, books, insurance and travel; preparation has a '
                    'narrower benefit package. No fixed rates are given; embassy recommendation '
                    'and doctoral-host rules apply.'},
 '752': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': "Bayer supports eligible master's/doctoral or medical students on 2-week-to "
                    '6-month life-sciences research stays, with a proposed budget up to EUR '
                    '10,000. Four discipline tracks cover drug discovery, agriculture, medicine '
                    "and climate/health. Bachelor's students and postdocs are excluded; doctoral "
                    "defence cannot precede the funded stay's end. Strong results, two references "
                    'and host acceptance are needed. Approved travel/living/childcare/training '
                    'costs differ by proposal.'},
 '753': {'categories': ['fellowships', 'grants'],
         'evidence': [],
         'host_countries': ['AT'],
         'kind': 'programme-overview',
         'summary': 'ESPRIT supports eligible early-career postdocs on nonextendable 36-month '
                    'Austrian research projects with a mentor. A PhD normally within five years '
                    'and suitable research qualifications are required. Funding includes the '
                    "investigator's salary and project costs; the source's annual lump-sum wording "
                    'differs from its total-cost wording for EUR 45,000–75,000, so that amount is '
                    'not described as personal cash or a verified annual entitlement. Candidate '
                    'and host apply jointly.'},
 '755': {'categories': ['other'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'Humboldt research awards require nomination by a German-based researcher. '
                    'Bessel offers EUR 60,000 for eligible internationally recognised researchers '
                    'within 18 years of PhD; Humboldt offers EUR 80,000. Max Planck–Humboldt '
                    'offers EUR 80,000 plus EUR 1.5 million for a German research group, not extra '
                    'personal cash, with a 15-year PhD limit and rotating subject focus. '
                    'Award-specific excellence and nomination rules apply; this is a multi-award '
                    'programme overview.'},
 '756': {'categories': ['internships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'ECB traineeships normally last 3–6 months, extendable to 12, with EUR '
                    '1,170/month or EUR 2,120 for specified doctoral-level placements, plus '
                    'housing-cost support. Applicants aged 18+ need EU/accession-country '
                    "citizenship, a bachelor's degree and English plus another EU language. "
                    'Experience limits are 12 months of work and six months of traineeships; '
                    'former ECB trainees/employees are excluded. Actual vacancy duties and '
                    'deadlines control eligibility.'},
 '757': {'categories': ['internships'],
         'evidence': [],
         'host_countries': ['BE', 'NL', 'DE', 'IT', 'ES'],
         'kind': 'programme-overview',
         'summary': 'JRC offers funded five-month scientific traineeships across its sites in '
                    'Belgium, Netherlands, Germany, Italy and Spain. Training, research '
                    'participation, professional networks and arrival support are described; no '
                    'fixed stipend rate is supplied. Applications and documents go through the '
                    'official recruitment portal, with education and project-fit requirements '
                    'specified by each vacancy. This is a standing traineeship overview rather '
                    'than one uniformly open position.'},
 '758': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'Boehringer Ingelheim Fonds supports eligible biomedical doctoral research '
                    'begun no more than six months before closing, for two years with a possible '
                    '18-month extension. Monthly rates vary by country, e.g. EUR 2,400 Germany, '
                    '2,200 Spain, 2,450 Austria, 3,800 USA or 3,500 Switzerland, with conditional '
                    'family/research/training extras. Relevant qualifications, a recognised '
                    'laboratory and research assessment apply. This is distinct from its short '
                    'travel/course grants.'},
 '759': {'categories': ['fellowships', 'training'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'Boehringer Ingelheim Fonds supports eligible short biomedical research '
                    'visits/practical courses up to three months with travel, accommodation and '
                    'course-fee contributions. Doctoral/postdoctoral limits are 11/13 years since '
                    'secondary-school graduation, not age limits. Courses must be at least 50% '
                    'practical. Applications are due at least six weeks before travel; '
                    'home-research relevance, host acceptance and cost evidence are required. No '
                    'universal grant rate is stated.'},
 '76': {'categories': ['scholarships'],
        'evidence': ['Target during the stay: university teachers, researchers. '
                     'Sending-country/institutional affiliation and the actual programme '
                     'conditions are distinct from a universal Slovak-citizenship rule.',
                     'Full published programme-specific application, academic/language and funding '
                     'conditions apply; travel/supplements require the stated Slovak '
                     'education-sector affiliation.'],
        'host_countries': ['PL'],
        'kind': 'programme-overview',
        'summary': 'Bilateral mobility to Poland for university teachers, researchers: 1–10 '
                   'months. Published Slovak support is EUR 750/month; the host waives the '
                   'educational/research programme fee, while accommodation and meals are charged. '
                   'Slovak-funded travel requires the stated sending-sector affiliation.'},
 '760': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'Friedrich Ebert supports eligible students/doctoral candidates admitted to a '
                    'German state or recognised university, at EUR 992/month plus conditional EUR '
                    '160/month family support. Foreign applicants need adequate German; strong '
                    'academic results, civic/political engagement and commitment to '
                    'social-democratic values apply. Duration is not specified in this source. '
                    'Online screening precedes document requests and interviews; rolling '
                    'submission does not guarantee selection.'},
 '761': {'categories': ['fellowships', 'grants'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': "Fritz Thyssen's overview covers early-postdoctoral support, research "
                    'projects, events, travel and publication assistance with a link to the German '
                    'research environment. Benefits and duration vary by route, with no universal '
                    'personal rate. Conference-attendance travel is excluded; printing support '
                    'requires an associated funded research project. Subject, collaboration and '
                    'route-specific application rules apply; recurring project/event dates have no '
                    'verified year.'},
 '765': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['US'],
         'kind': 'programme-overview',
         'summary': 'Robitschek supports eligible students of Czech or Slovak origin on two '
                    'semesters at Nebraska–Lincoln. Published support covers direct study costs, a '
                    'Prague–Lincoln return flight and up to USD 400/semester for other expenses. '
                    'Academic merit, English, leadership and interest in U.S. economy/culture are '
                    'considered, with intent to use the experience after returning home. Origin is '
                    'not converted into a passport restriction; actual admission/call requirements '
                    'apply.'},
 '767': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'Hanns Seidel supports eligible German university study with EUR 855/month '
                    'plus EUR 300 study support and conditional insurance/child benefits, or '
                    'doctoral study up to three years at EUR 1,650/month plus EUR 100 research and '
                    'conditional family support. Student age is up to 45; academic excellence, '
                    'German, civic engagement and foundation-value alignment apply. Recognised '
                    'host admission is required; doctoral rules differ and yearless closings stay '
                    'undated.'},
 '768': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'Prussian Cultural Heritage supports eligible collection-related research at '
                    'its participating Berlin institutions for 1–3 months. Graduates/doctoral '
                    'students receive EUR 1,300/month; PhD-qualified academics/researchers EUR '
                    '1,600, plus EUR 500 travel. Applicants provide a relevant proposal, '
                    'qualifications and two references directly to the chosen institution. '
                    'International cooperation and use of its collections are central; '
                    'institution-specific selection rules apply.'},
 '769': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'IEG supports eligible European/religious-history research in Mainz for 6–12 '
                    'months, at EUR 1,650/month for doctoral researchers or EUR 2,100 for '
                    'postdocs, with conditional family support. Doctoral applicants normally '
                    'started within three years; good German/English, research fit and two '
                    'references are expected. Participation in colloquia and at least one '
                    'presentation are required. The October 15,2026 local closing clock has no '
                    'verified timezone.'},
 '77': {'categories': ['scholarships'],
        'evidence': ["Target during the stay: bachelor's students, master's students, doctoral "
                     'students. Sending-country/institutional affiliation and the actual programme '
                     'conditions are distinct from a universal Slovak-citizenship rule.',
                     'Full published programme-specific application, academic/language and funding '
                     'conditions apply; travel/supplements require the stated Slovak '
                     'education-sector affiliation.'],
        'host_countries': ['SI'],
        'kind': 'programme-overview',
        'summary': "Bilateral mobility to Slovenia for bachelor's students, master's students, "
                   'doctoral students: 3–10 months. The host provides EUR 360/month, tuition and '
                   'dormitory accommodation, plus EUR 2.63 per working day for meals. '
                   'Slovak-funded travel requires the stated sending-sector affiliation.'},
 '770': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'Klassik Stiftung Weimar offers two-month research/creative residencies with '
                    'accommodation or EUR 500/month housing support, collection access and expert '
                    'assistance. Weimar, Nietzsche and Bauhaus tracks have distinct project '
                    'requirements; a Nietzsche connection is beneficial, not mandatory. Eligible '
                    'work must fit the relevant collections/themes, with public presentation '
                    'required for Bauhaus. No general living stipend is stated; the July 31 '
                    'closing is yearless.'},
 '771': {'categories': ['scholarships'],
         'deadline_evidence': {'date': '2026-06-22',
                               'field': 'Podávanie žiadosti',
                               'literal': '22. jún 2026'},
         'evidence': [],
         'host_countries': ['MX'],
         'kind': 'programme-overview',
         'status': 'expired',
         'summary': "Mexico's master's mobility supports up to 12 months at MXN 14,264.88/month, "
                    'with institution-dependent fees, insurance only from month seven and '
                    'specified visa/travel support. Citizenship, academic offers and airfare '
                    'eligibility depend on actual call annexes; self-purchased flights are not '
                    'reimbursed. The source explicitly closed applications on June 22,2026. '
                    'Degree-thesis preparation is excluded, and awarded '
                    'programme/institution/duration cannot later change.'},
 '774': {'categories': ['scholarships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      '9d3d59c11074cdd3a6bce130f36f079bef669fef36b29c69bd965614de4999d7; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': "Erasmus Mundus joint master's scholarships support applicants worldwide with "
                    "programme participation, travel, visa and living costs. A bachelor's degree "
                    'is required by entry; final-year applicants may apply. Consortium programmes '
                    'involve at least three universities in three countries. Admission, funding '
                    'and deadlines depend on the selected programme.'},
 '775': {'categories': ['scholarships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Half grant/half repayable interest-free loan; residence/integration '
                      'eligibility rather than all EU citizenship automatically.',
                      'Raw receipt SHA256 '
                      'f7f6b1985ba87dfe857689aae84d14b86949f8cee95598c716b0f7507d0e85a3; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'BAfoG support for study in Germany is means-tested, with up to EUR 992 '
                    'monthly. Eligible foreign residents, including some EU citizens, migrants and '
                    'refugees, must meet residence/integration conditions. For university '
                    'students, half is a grant and half an interest-free repayable loan, capped at '
                    'EUR 10,010 repayment. Applications are ongoing through the institution or '
                    'online.'},
 '778': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Talented international students can receive EUR 500/month for ten months '
                    "annually: up to EUR 15,000 over a three-year bachelor's/combined programme or "
                    "EUR 10,000 over a two-year master's programme. Eligible full-time "
                    'public-university programmes, SAT/SCIO and prior residence/study rules apply. '
                    'The DB and study hub give conflicting, already-past 2026 closings, so no '
                    'single verified current closing is claimed.'},
 '779': {'categories': ['fellowships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      '3e16610886dd0550e7b176c70170798a6ef5fdba75d8bf523d049d9bb4103cb0; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['CZ', 'HU', 'PL'],
         'kind': 'programme-overview',
         'summary': 'Visegrad fellowships fund 2-10-week research or lecturing stays at EUR 500 '
                    'weekly, paid 80% at arrival and 20% after completion. Applicants must be PhD '
                    'students or graduates and citizens of CZ, HU, PL, SK or UA; the V4 host must '
                    'be outside their citizenship country. Invitation and recommendation letters '
                    'are required. Listed deadlines: 31 May and 30 November, without a year; '
                    'capacity may close earlier.'},
 '78': {'categories': ['scholarships'],
        'evidence': ["Target during the stay: bachelor's students, master's students, doctoral "
                     'students, university teachers, researchers. Sending-country/institutional '
                     'affiliation and the actual programme conditions are distinct from a '
                     'universal Slovak-citizenship rule.',
                     'Full published programme-specific application, academic/language and funding '
                     'conditions apply; travel/supplements require the stated Slovak '
                     'education-sector affiliation.'],
        'host_countries': ['SI'],
        'kind': 'programme-overview',
        'summary': "Bilateral mobility to Slovenia for bachelor's students, master's students, "
                   'doctoral students, university teachers, researchers: two weeks. The host '
                   'covers tuition, accommodation, meals, pocket money and domestic transport. '
                   'Slovak-funded travel requires the stated sending-sector affiliation.'},
 '780': {'categories': ['grants'],
         'evidence': [],
         'host_countries': [],
         'kind': 'institutional-grant',
         'summary': 'ERC Proof of Concept supports eligible current or recent ERC grant holders in '
                    'exploring the innovation potential of their ERC-funded research. Prior ERC '
                    'funding and the specified recent-ending and non-overlap rules are required. '
                    'The English catalogue amount is empty and the Slovak variant gives no fixed '
                    'award, so none is invented. This supplementary principal-investigator grant '
                    'is not an open scholarship for all researchers.'},
 '781': {'categories': ['scholarships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      '766d4a09f9947cad48ece7ff0e71b2470697ac67e25222aed0c92d78fa1c29e0; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['TR'],
         'kind': 'programme-overview',
         'summary': 'KATIP offers eight months of Turkish language and cultural study for foreign '
                    'public officials, diplomats and academics/researchers under 40 with '
                    "professional English. Officials need three years' experience; researchers "
                    'need university/research employment and PhD study or a PhD. Five places are '
                    'listed, with TRY 20,000 monthly, accommodation, return flights and '
                    'activities. Applications usually close in June; no exact year is stated.'},
 '783': {'categories': ['scholarships'],
         'evidence': ["Target during the stay: bachelor's students, master's students, doctoral "
                      'students. Sending-country/institutional affiliation and the actual '
                      'programme conditions are distinct from a universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.',
                      'Independent full-facts review restored same-source citizenship, subject, '
                      'language, prior-study or qualification/experience restrictions.'],
         'host_countries': ['ME'],
         'kind': 'programme-overview',
         'summary': "Montenegrin mobility supports eligible Slovak bachelor's/master's/doctoral "
                    'students with the required Slovak institutional link for at least three '
                    'months. Host scholarship support follows national rules; no fixed rate is '
                    'stated. Serbian, Croatian, Montenegrin or another agreed language is '
                    'required; host acceptance is welcomed, not mandatory in the document list. '
                    'Slovak-funded travel depends on specified education-sector affiliation; this '
                    'is partial-degree mobility.'},
 '784': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Montenegro to Slovakia for bachelor's students, "
                    "master's students, doctoral students; duration: min. 3 months. Published "
                    'support: EUR 620/month for university students; EUR 1,025.50/month for '
                    'doctoral students. The sending side covers international travel.'},
 '785': {'categories': ['scholarships'],
         'evidence': ['Bilateral agreement: sending-country affiliation, nomination and full '
                      'programme-specific conditions remain distinct from a blanket passport '
                      'whitelist.',
                      'Exact target-group and duration fields are used; conflicting title/field '
                      'durations are not silently harmonized.'],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility from Montenegro to Slovakia for bachelor's students, "
                    "master's students, doctoral students, university teachers, researchers; "
                    'duration: 3 weeks (in August). The host provides the language course, '
                    'accommodation, meals and a statutory daily allowance. The sending side covers '
                    'international travel.'},
 '789': {'categories': ['fellowships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Bounded actual call: explicit 2026 deadline (789) or 2026/27 intake with '
                      '2025/26 school-leaving cohort (804); future closing alone does not '
                      'establish open status.',
                      'Raw receipt SHA256 '
                      '0d2214d36bd0a1a6cce362cb9bae429f4c8ce16f25f90665264b0a72c9d29686; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['DE'],
         'kind': 'opportunity',
         'summary': 'DAAD research stays in Germany offer EUR 1,400 monthly plus travel/insurance '
                    'and conditional supplements. PhD stays last 2-12 months; postdoctoral stays '
                    '2-6 months. Applicants need a host-approved research plan, language evidence '
                    'and qualifying recent doctoral status; residence in Germany over 15 months '
                    'excludes applicants. Deadline: 16 November 2026, 23:59; timezone unstated. '
                    'The stay cannot be extended.'},
 '79': {'categories': ['scholarships'],
        'evidence': ["Target during the stay: bachelor's students, master's students, doctoral "
                     'students. Sending-country/institutional affiliation and the actual programme '
                     'conditions are distinct from a universal Slovak-citizenship rule.',
                     'Full published programme-specific application, academic/language and funding '
                     'conditions apply; travel/supplements require the stated Slovak '
                     'education-sector affiliation.'],
        'host_countries': ['RS'],
        'kind': 'programme-overview',
        'summary': "Bilateral mobility to Serbia for bachelor's students, master's students, "
                   'doctoral students: at least three months. The source quotes a host stipend of '
                   'RSD 15,000/month under national rules. Slovak-funded travel requires the '
                   'stated sending-sector affiliation.'},
 '790': {'categories': ['fellowships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Five A-E tracks distinguished; trackE doctoral/postdoctoral stipend follows '
                      'respective1760/2400 rate, not a fifth universal rate.',
                      'Raw receipt SHA256 '
                      '0be4a67c9c61cdb957f4c288efbcb415ad99d59ed2a0f55e904e3e5f0f8388aa; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'DAAD-DLR aeronautics, space, transport and energy research has five tracks: '
                    "master's students, nine months/EUR 992 monthly; PhD, 36 months/EUR 1,760; "
                    'postdoc, 6-24 months/EUR 2,400; senior researcher, 1-3 months/EUR 2,760; '
                    'visiting PhD/postdoc, 1-6 months at the matching rate. Excellent English and '
                    'track-specific qualifications are required. Some supplements are conditional; '
                    'positions and deadlines vary.'},
 '794': {'categories': ['scholarships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      'bfe0b3fb9073c9cdee6697905f7123da58098c7b7858a2d38763da81bf043f8a; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': "Deutschlandstipendium supports talented undergraduate and master's students "
                    'studying in Germany with EUR 300 monthly, jointly funded by government and '
                    'private sponsors. Participating universities select recipients and set '
                    'documents and application procedures. Support may continue through the '
                    'standard study period and can accompany BAfoG. The source lists ongoing '
                    'applications; institution-specific terms apply.'},
 '796': {'categories': ['scholarships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      '19863f20d4916b720406c2a16b27cddddd1ce243463b9708955f81fc53c863b5; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'A one-week summer course in Dillingen an der Donau supports German-language '
                    'teachers working at Slovak primary or secondary schools. Bavaria pays '
                    'accommodation, meals and pocket money; the Slovak ministry contributes to '
                    'travel. The course usually runs in the last week of June. Applications are '
                    'handled through the Slovak education ministry; the listed 31 January deadline '
                    'has no year.'},
 '797': {'categories': ['scholarships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      '9ff92d41042dcf0c7e75dc066b20bd8cebc826bac6a2d2499b133fdd2b3bbd72; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'German-language teachers at Slovak primary and secondary schools can '
                    'undertake a two-week observation placement in Bavaria, arranged with an '
                    'assigned host school. The source describes accommodation, meals, pocket money '
                    'and travel support, without a cash amount. Applications are handled through '
                    'the Slovak education ministry. Listed deadline: 31 January, with no year.'},
 '798': {'categories': ['scholarships'],
         'evidence': ["Target during the stay: bachelor's students, master's students, "
                      'secondary-school students. Sending-country/institutional affiliation and '
                      'the actual programme conditions are distinct from a universal '
                      'Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['UA'],
         'kind': 'programme-overview',
         'summary': "Bilateral mobility to Ukraine for bachelor's students, master's students, "
                    "secondary-school students: a full bachelor's/master's degree. This "
                    'full-degree route is specifically for Slovak citizens of Ukrainian ethnicity. '
                    'Tuition is waived; dormitory fees follow Ukrainian-student terms and the '
                    'stipend follows national legislation.'},
 '799': {'categories': ['scholarships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      '7ac3a97964ecfe047479b6a280f39a0b2c6a4f891712d1153cda1a04dc234161; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['AT',
                            'CZ',
                            'HU',
                            'PL',
                            'SI',
                            'BG',
                            'RO',
                            'AL',
                            'BA',
                            'HR',
                            'MK',
                            'MD',
                            'RS',
                            'ME'],
         'kind': 'programme-overview',
         'summary': 'CEEPUS offers 2-5-working-day mobility for university administrative and '
                    'other staff within approved networks. Freemover applications are excluded. '
                    'Host-country scholarship rates vary and home-institution nomination and host '
                    'approval are required. Listed network deadlines are 1 June for winter and 1 '
                    'November for summer semesters, without a year. Country listings do not alone '
                    'establish passport eligibility.'},
 '800': {'categories': ['scholarships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      'e47f30cf795cd0c0a65397be5e1c94927811402f5bb6c32f769c5937cb6c74fc; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['GB', 'IE'],
         'kind': 'programme-overview',
         'summary': 'Royal Society schemes support international research exchanges and UK-based '
                    'research careers; University Research Fellowships also allow Ireland. '
                    'Exchange ceilings are GBP 3,000/6,000/12,000 for differing visit schedules; '
                    'Wolfson awards up to GBP 300,000 over five years or GBP 125,000 for a '
                    '12-month visit. Eight-year URF and ten-year Faraday grants have separate '
                    'conditions; Faraday may reach GBP 8 million. Awards and deadlines vary by '
                    'scheme.'},
 '804': {'categories': ['scholarships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Bounded actual call: explicit 2026 deadline (789) or 2026/27 intake with '
                      '2025/26 school-leaving cohort (804); future closing alone does not '
                      'establish open status.',
                      'Raw receipt SHA256 '
                      '8e0045e81a6c985971895827a2ba1cbd80d6c5fb9fa08197d8de192f4194c979; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['SK'],
         'kind': 'opportunity',
         'summary': "Slovakia's 2026/27 domestic talent scheme pays EUR 400 monthly: EUR 12,000 "
                    "over three undergraduate years or EUR 8,000 over two master's years. "
                    'Undergraduate applicants must finish Slovak secondary school in 2025/26 and '
                    "enter an eligible full-time RIS3 programme. Master's support is for eligible "
                    'continuing recipients. The listed 1,206 places are programme capacity, not '
                    'separate awards. Applications usually close in July; no exact deadline is '
                    'given.'},
 '806': {'categories': ['fellowships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      '52ea3c6e328661b2f4f00d905b83a1b924040d5d70d5c552cf284999352c1f20; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['EE'],
         'kind': 'programme-overview',
         'summary': 'Estonian grants support researchers and academic staff for short visits of '
                    '1-9 days or longer stays of 10 days-10 months. The source lists EUR 45 daily '
                    'and EUR 660 monthly. A host acceptance confirming dates is required before '
                    'the grant decision; funding does not itself ensure admission. Applications '
                    'with an academic CV go to HARNO. Closing is usually in May, with no exact '
                    'year or deadline stated.'},
 '807': {'categories': ['grants'],
         'evidence': [],
         'host_countries': [],
         'kind': 'institutional-grant',
         'summary': 'MSCA COFUND supports an eligible single legal entity establishing doctoral or '
                    'postdoctoral funding programmes in an EU/associated country. Projects can run '
                    'up to five years and receive up to EUR 10 million, with at least three '
                    'recruited researchers and minimum-duration conditions. Individuals apply '
                    'later to programme vacancies. The English/Slovak allowance descriptions '
                    'differ and are not combined into a fabricated individual rate.'},
 '808': {'categories': ['fellowships'],
         'evidence': [],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'MSCA Postdoctoral Fellowships require a PhD and the adjusted eight-year '
                    'research-experience and 12-in-36-month mobility conditions. European '
                    'fellowships can support any nationality for one to two years; Global '
                    'fellowships require eligible nationality/long-term residence, an outgoing '
                    'phase and a mandatory one-year return. Researcher and host apply jointly. The '
                    'two catalogue variants give different yearless deadline guidance, so no date '
                    'is inferred.'},
 '81': {'categories': ['scholarships'],
        'evidence': ["Target during the stay: bachelor's students, master's students, doctoral "
                     'students, university teachers, researchers. Sending-country/institutional '
                     'affiliation and the actual programme conditions are distinct from a '
                     'universal Slovak-citizenship rule.',
                     'Full published programme-specific application, academic/language and funding '
                     'conditions apply; travel/supplements require the stated Slovak '
                     'education-sector affiliation.'],
        'host_countries': ['RS'],
        'kind': 'programme-overview',
        'summary': "Bilateral mobility to Serbia for bachelor's students, master's students, "
                   'doctoral students, university teachers, researchers: three weeks. The host '
                   'covers tuition and student-residence accommodation; the source states RSD '
                   '10,000 cash support without an explicit recurring payment period. '
                   'Slovak-funded travel requires the stated sending-sector affiliation.'},
 '810': {'categories': ['scholarships'],
         'evidence': [],
         'host_countries': ['SK'],
         'kind': 'programme-overview',
         'summary': 'Incoming CEEPUS university staff can undertake a two-to-five-working-day '
                    'visit in Slovakia at the published EUR 73/day. Employment at a participating '
                    'higher-education institution, the applicable participating-country status and '
                    'institutional nomination rules apply. This short staff-mobility route is '
                    'distinct from student and teaching scholarships; teacher-freemover '
                    'boilerplate does not create a staff freemover entitlement.'},
 '811': {'categories': ['fellowships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      '060e2c8c064dcf9e5e7a14afa310512a99c6c71ff189a57b3bdf76a3cdbf61f1; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'Herder Institute fellowships support researchers at any career stage studying '
                    'East-Central European history in Marburg. A one-month award of EUR 1,900 is '
                    'intended for living and travel costs; guest accommodation may be available. '
                    'Around ten awards yearly are listed. Applications require a project, CV, '
                    "publications and qualifications; doctoral applicants also need a supervisor's "
                    'reference. Deadline: 15 August, without a year.'},
 '812': {'categories': ['fellowships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      '9cf29c7e5f0551538b8a31d19077c482035308886e8d403f8e451486850f28ae; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'Herzog August Library fellowships in Wolfenbuettel support 1-10 months of '
                    'research requiring its collections. Doctoral funding is EUR 1,300 monthly '
                    'plus EUR 100 for materials and a currently listed EUR 150 increase; travel '
                    'may be added. Postdoctoral funding is EUR 2,200 monthly, with possible '
                    'travel/family supplements. Researchers from Germany and abroad may apply. '
                    'Listed dates: 31 January, 1 April and 1 October, without years.'},
 '813': {'categories': ['internships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      'd71ee1b6b6f4ff9a451c327b8977ecbe2b3967f2eafb43c3179c7876d4da56d1; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['BE'],
         'kind': 'programme-overview',
         'summary': 'Consilium offers five-month paid traineeships at the EU Council Secretariat '
                    'in Brussels, around 110 annually. The source lists a 2026 monthly grant of '
                    'EUR 1,538.16, conditional travel support and accident cover if otherwise '
                    'uninsured. Up to six positive-action places are reserved for EU citizens with '
                    'recognised disabilities. Calls open twice yearly; exact eligibility and '
                    'current deadlines are set by the Secretariat.'},
 '815': {'categories': ['scholarships'],
         'evidence': ['Target during the stay: university teachers. Sending-country/institutional '
                      'affiliation and the actual programme conditions are distinct from a '
                      'universal Slovak-citizenship rule.',
                      'Full published programme-specific application, academic/language and '
                      'funding conditions apply; travel/supplements require the stated Slovak '
                      'education-sector affiliation.'],
         'host_countries': ['GR'],
         'kind': 'programme-overview',
         'summary': 'Bilateral mobility to Greece for university teachers: ten days. The Greek '
                    'host pays EUR 110/day for living costs, domestic programme travel and a short '
                    'cultural excursion; the daily allowance is paid only after the participant '
                    'returns. Slovak-funded travel requires the stated sending-sector '
                    'affiliation.'},
 '817': {'categories': ['scholarships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      'c7fd241093fb84419b81ef7c0c5f4f41a5922693100536878ef53a9583e273a8; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['FR'],
         'kind': 'programme-overview',
         'summary': "France Excellence supports Slovak students in years 1-3 of a bachelor's "
                    'programme in France for one academic year. It lists EUR 615 monthly, health '
                    'insurance, exemption from university registration fees and French-government '
                    'scholarship status. Parents co-finance EUR 245 monthly. Applicants must show '
                    'admission or an application to a French institution. Applications usually '
                    'close in February; no exact year is stated.'},
 '818': {'categories': ['scholarships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      '2f3e30d98c4bd8e02f65cf423d39a5a179f1e66afa3c669b57e25cc3ed61c8f7; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['AT'],
         'kind': 'programme-overview',
         'summary': 'Club Alpbach Czechia & Slovakia offers 15 scholarships for two weeks of the '
                    'European Forum Alpbach in Austria. Funding covers participation, '
                    'accommodation and EUR 20 daily for meals, excluding travel. Applicants must '
                    'be 18-30 throughout, have C1 English, no previous EFA scholarship and '
                    'Czech/Slovak permanent residence or a qualifying connection. At least 11 '
                    "days' attendance is required. Applications are usually in March, without a "
                    'year.'},
 '820': {'categories': ['grants'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Exact source eligibility says at least two years after PhD, not at most. '
                      'Quota conflicts one versus about15; summary retains conflict.',
                      'Raw receipt SHA256 '
                      'dfd4140d77887f65c4a8494dbc2fc89dca3f83d549046b4e7d8a2da7330bee3a; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['BE',
                            'FR',
                            'NL',
                            'DE',
                            'IT',
                            'GR',
                            'PT',
                            'ES',
                            'AT',
                            'SE',
                            'CZ',
                            'HU',
                            'PL',
                            'CH'],
         'kind': 'programme-overview',
         'summary': 'Chemistry Europe Travel Grant offers EUR 2,000 for a 1-2-month research visit '
                    "outside the applicant's country, usable within 12 months. The source requires "
                    'at least two years after PhD and membership of a participating national '
                    'chemical society. Apply through the society with CV, motivation and five '
                    'publications. Listed deadline: 15 October, without a year. Capacity '
                    'conflicts: the quota field says one, while the body says around 15 '
                    'internationally.'},
 '821': {'categories': ['grants'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      '64ca77b1144a6c8adb6b39d05f9e082f9fbf2dee42fb5fff3b054a0c7c192522; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['DE', 'AT', 'CZ', 'HU', 'PL'],
         'kind': 'institutional-grant',
         'summary': 'V4 Gen Mini Grants fund legal entities, not individual students, for '
                    'cross-border youth projects lasting at most seven months. Projects need at '
                    'least two eligible-country partners and physical mobility for ages 12-30, '
                    'plus a joint event and public presentation. Up to EUR 10,000 per project '
                    'covers eligible travel, accommodation and organisation costs. A draft '
                    'precedes the full application. Closing is usually in November; no exact year '
                    'is stated.'},
 '822': {'categories': ['fellowships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      '1c14c70bd0a8f544709d360dff47cdb551a961e2e1d727794184f13c7b3a5342; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['AL', 'BA', 'MK', 'RS', 'ME'],
         'kind': 'programme-overview',
         'summary': 'Western Balkans-Visegrad fellowships support PhD students or holders for '
                    '2-10-week academic/research stays between the regions. Funding is EUR 500 '
                    'weekly with possible travel support up to EUR 500. Eligible university, '
                    'research, archive or library hosts must provide an invitation. The Slovak '
                    'listing names five Western Balkan destinations. Applications are online; the '
                    'current deadline depends on the offer.'},
 '823': {'categories': ['fellowships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      'b2f68d7db07fcff6bf6ecadbd7bbebdedb08a890bc31f066cd2e3da13fca17a4; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['IT'],
         'kind': 'programme-overview',
         'summary': 'Visegrad/EUI grants support young Central and Eastern European researchers '
                    'studying European integration at the EU Historical Archives in Florence. Six '
                    'annual grants of EUR 5,000 cover research costs. Applicants need good '
                    'English, basic French and a project requiring the archives, with intended '
                    'publication. Apply in English or French with a project and reference. Closing '
                    'is usually in April; duration and exact year are not given.'},
 '824': {'categories': ['fellowships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      'df8c9c6b5d741050cc74f101163677447677f670b0fbe1145c01378f2904813d; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['HU'],
         'kind': 'programme-overview',
         'summary': 'Visegrad/Open Society Archives fellowships support researchers, journalists '
                    'and artists whose projects fit the Budapest collections and current themes. '
                    'EUR 3,000 covers a two-month stay and travel/living costs; shorter visits '
                    'receive proportionally less. Twenty annual places are listed. Applicants '
                    'submit a project, motivation, CV and two referees under the current call. '
                    'Deadlines depend on the offer.'},
 '825': {'categories': ['fellowships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      '8f5e7dbd95b4a84104c35a0a8e44523a0f18e5bda4528b9744c503d927a755c7; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['IT'],
         'kind': 'programme-overview',
         'summary': 'Visegrad fashion-design residencies offer V4 professionals two months in '
                    'Milan, 1 June-31 July, with EUR 6,000 for living costs. Four annual residents '
                    'access studios, lectures, consultations and cultural resources through '
                    'Accademia Costume & Moda. Applicants must be at least 18 with good English '
                    'and submit a portfolio and project. Listed deadline: 15 October, without a '
                    'year.'},
 '826': {'categories': ['fellowships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      '9d224406c43b4da0f4b750f41b0a8034ce9189a604f2a5a3b9d509daaf87afc1; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['CZ', 'HU', 'PL'],
         'kind': 'programme-overview',
         'summary': 'Visegrad literary residencies support authors, poets, translators, critics '
                    'and journalists working on their own projects and public literary events. '
                    'Spring stays last about six weeks with EUR 1,300; autumn stays about 13 weeks '
                    'with EUR 2,600. Accommodation and host support are included. The programme '
                    'lists 32 annual residents across V4 countries. Apply in English with project '
                    'and publication details. Deadline: 15 October, without a year.'},
 '827': {'categories': ['fellowships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Personal3000 and host3000 separate; total6000 not personal stipend.',
                      'Raw receipt SHA256 '
                      '28b663c9b479516c167ed33cecdba18cdabb9fa405f4336d7a2df3075de9e7cd; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['CZ', 'HU', 'PL'],
         'kind': 'programme-overview',
         'summary': 'Visegrad visual/sound-arts residencies support artists and related creative '
                    'professionals for two months in another V4 country. EUR 3,000 goes to the '
                    'resident and separately EUR 3,000 to the host organisation; the full EUR '
                    '6,000 is not personal funding. A portfolio, identity document and signed host '
                    'acceptance are required. Repeat recipients must choose a different V4 country '
                    'from their prior residency. Deadline: 15 October, without a year.'},
 '828': {'categories': ['scholarships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      '23af658cd8217af392e61d6b01120febcbc27cb437a2ad0fd3c98c85e210c7d7; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['FR'],
         'kind': 'programme-overview',
         'summary': "Paris-Saclay international master's scholarships usually pay EUR 1,000 "
                    'monthly for ten months, plus up to EUR 1,000 once for travel/visa costs '
                    'depending on origin. Support can last one or two academic years, conditional '
                    'on entry level and progression. Applicants must first be admitted to an '
                    "eligible master's programme; outstanding candidates may then be nominated. "
                    'Direct scholarship applications before admission are excluded; deadlines '
                    'vary.'},
 '829': {'categories': ['scholarships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      '9b0850586b6d473adc677013f947540cf553dae10d2272a2924c946ce078625c; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['PT'],
         'kind': 'programme-overview',
         'summary': 'Camões annual Portuguese language/culture scholarships provide EUR 500 '
                    'monthly for eight months at Portuguese universities or recognised '
                    'institutions. Foreign students and Portuguese citizens living abroad may '
                    'apply. Electronic applications require qualifications, CV, motivation and two '
                    'references, in Portuguese or English or certified translation. A similar '
                    'Portuguese-institution scholarship cannot be held simultaneously. Closing '
                    'depends on the call.'},
 '830': {'categories': ['scholarships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      '68e8edf44e32a07717ed3ab580e671991accc733bba173e962af6179d291c1b7; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['PT'],
         'kind': 'programme-overview',
         'summary': 'Camões summer Portuguese language/culture scholarships provide EUR 500 for a '
                    'one-month course, with support for course participation at Portuguese '
                    'universities or recognised institutions. Foreign students and Portuguese '
                    'citizens living abroad may apply through the electronic portal. '
                    'Qualifications, motivation and two references are required. Similar '
                    'Portuguese-institution scholarships cannot be combined; deadlines depend on '
                    'the call.'},
 '831': {'categories': ['scholarships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Country filter says Portugal, but material programme description concerns '
                      'foreign institutions or does not establish a physical placement; host '
                      'country left empty.',
                      'Raw receipt SHA256 '
                      '26571bc2249877bd77867d2df4eb9d8e660781f8206188dd0898581ef3332446; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'Fernão Mendes Pinto supports graduates and final-year students developing '
                    'Portuguese-as-a-foreign-language training projects through Camões centres, '
                    'lectureships or foreign partner institutions. EUR 650 monthly is listed. The '
                    'duration field says three years; the body describes variable, renewable '
                    'support, so a guaranteed three-year award is not established. A Portuguese '
                    'work plan and references accompany online applications; deadlines vary.'},
 '832': {'categories': ['scholarships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      'de9f523ef1e401c047ac112cc3f583abac3efc4e0cd6ca6524c348ac484eef53; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['PT'],
         'kind': 'programme-overview',
         'summary': 'Camões research scholarships support foreign and Portuguese university '
                    'teachers/researchers living outside Portugal for Portuguese language/culture '
                    'research or study in Portugal. Funding is EUR 700 monthly. Listed durations '
                    "are one year for specialisation, two for master's and three for doctorate. "
                    'Applicants submit qualifications, references and a Portuguese work plan '
                    'online. Similar Portuguese-institution support cannot be combined; deadlines '
                    'vary.'},
 '833': {'categories': ['scholarships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Country filter says Portugal, but material programme description concerns '
                      'foreign institutions or does not establish a physical placement; host '
                      'country left empty.',
                      'Raw receipt SHA256 '
                      'f4a5d489b1125a454049af0840005e5de7b577abd6a3452ca98b7e6cc3b2c3d0; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'Pessoa scholarships support heads of Portuguese-studies departments or '
                    'Portuguese-language units at foreign universities/research institutions. The '
                    'source lists six months and EUR 650 monthly for education/research projects '
                    'in Portuguese language and culture. A Portuguese work plan, qualifications '
                    'and references are required via the Camões portal. Similar '
                    'Portuguese-institution scholarships cannot be held simultaneously. Deadlines '
                    'vary by call.'},
 '834': {'categories': ['scholarships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Country filter says Portugal, but material programme description concerns '
                      'foreign institutions or does not establish a physical placement; host '
                      'country left empty.',
                      'Raw receipt SHA256 '
                      'dda4de796d613e45cce4ff8b8ac7e849a85d5e0a7a4ef34e7321a2db721310c1; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': [],
         'kind': 'programme-overview',
         'summary': 'Vieira scholarships support foreign graduates and Portuguese citizens living '
                    'abroad undertaking translation or conference-interpreting training projects. '
                    'The source lists one year and EUR 650 monthly. Applications through Camões '
                    'need qualifications, motivation, references and a Portuguese work plan. '
                    'Similar Portuguese-institution scholarships cannot be combined. Deadlines '
                    'depend on the call; the generic support text does not establish a specific '
                    'host placement.'},
 '836': {'categories': ['scholarships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      '4f8086425054811b20c7f58d86679fd7d53b2088b7f6361e50f9650290969737; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['IT'],
         'kind': 'programme-overview',
         'summary': 'MAECI scholarships support foreign students admitted to Accademia Teatro alla '
                    "Scala's opera-singing or performing-arts-management programmes. The source "
                    'lists nine months/ten awards for singing and six months/seven awards for '
                    'management. Funding amounts and deadlines depend on the relevant call. '
                    'Admission and programme selection are required first, and awards cannot be '
                    'combined with other Italian-government scholarships.'},
 '837': {'categories': ['scholarships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      '6f1168f8c75a78c8391134a70b190298f09759041f59633e304b488fbbb24ff2; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'Heinrich Boell scholarships support outstanding, socially engaged '
                    "international master's and doctoral students at recognised German "
                    "institutions. Master's applicants must have a foreign bachelor's degree and "
                    "study full-time; doctoral applicants need a foreign master's. Listed monthly "
                    'funding is EUR 855 plus EUR 300 materials for students, or EUR 1,650 plus EUR '
                    '100 for doctoral study, with conditional family support. Dates: 1 March and 1 '
                    'September, without years.'},
 '838': {'categories': ['scholarships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      'a33d87ffef1b991b2b36aac15c7c916cf5210c8f0007b765568680aca2699aab; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'Rosa Luxemburg scholarships support strong, socially engaged international '
                    "master's and doctoral candidates in Germany, with living-cost support and "
                    "development activities. Master's applicants need enrolment at a recognised "
                    'institution; doctoral candidates need research supervision, with some medical '
                    "exclusions. German B2 and alignment with the foundation's values are "
                    'required. Listed dates are 1 May and 1 October, without years; no cash amount '
                    'is given.'},
 '839': {'categories': ['scholarships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      'de43521cc073302f02d39f5466215bea01cf937ee9927e881f63387086ea8e70; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': "Friedrich Naumann scholarships support talented undergraduate, master's and "
                    'doctoral students, including qualifying foreign applicants, studying in '
                    'Germany. At least EUR 300 monthly is listed alongside development activities '
                    'and a scholarship network. Online applications need academic records, '
                    'motivation, references and evidence of volunteering/social engagement; '
                    'shortlisted candidates attend an interview. Listed deadlines: 30 April and 31 '
                    'October, without years.'},
 '840': {'categories': ['fellowships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      '9b018f04d31dbd18b5c619bbc2c19de7288bc786acc81a942424f9a6a3255eae; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['DE'],
         'kind': 'programme-overview',
         'summary': 'International Youth Library fellowships in Munich support '
                    "children's/youth-literature and illustration research for six weeks-three "
                    'months. EUR 1,300 monthly and around 15 places annually are listed. '
                    'International researchers submit a project, work plan, CV, qualifications, '
                    'reference and English/German evidence. The programme has Bavarian ministry '
                    'funding from 2025. Listed deadline: 30 September, without a year.'},
 '841': {'categories': ['scholarships'],
         'evidence': ['Read all 17 material fields; original title retained by integrator.',
                      'Home-country filter alone does not establish nationality; no '
                      'eligible-country inference.',
                      'Deadline and timezone must retain source uncertainty.',
                      'Raw receipt SHA256 '
                      'c8be5f584c982c14ab3238e2d64cebad7f1c6a9415c4fc77d25fbfcd6617f854; metadata '
                      'status/UTC reviewed by source author.'],
         'host_countries': ['EE'],
         'kind': 'programme-overview',
         'summary': 'Estonian summer/winter school scholarships support students who currently '
                    'study or previously studied Estonian language/culture. Three-four-week '
                    'courses cover accommodation, course fees and cultural activities; travel '
                    "remains the participant's cost. Applicants choose a course and submit "
                    "motivation and a lecturer's reference or language evidence to HARNO. "
                    'Applications usually close in March/October; no exact year is given.'},
 '88': {'categories': ['scholarships'],
        'evidence': ["Target during the stay: bachelor's students, master's students, doctoral "
                     'students, secondary-school teachers, university teachers. '
                     'Sending-country/institutional affiliation and the actual programme '
                     'conditions are distinct from a universal Slovak-citizenship rule.',
                     'Full published programme-specific application, academic/language and funding '
                     'conditions apply; travel/supplements require the stated Slovak '
                     'education-sector affiliation.'],
        'host_countries': ['BE'],
        'kind': 'programme-overview',
        'summary': "Bilateral mobility to Belgium for bachelor's students, master's students, "
                   'doctoral students, secondary-school teachers, university teachers: three '
                   'weeks. The host covers French-teaching-course registration, accommodation and '
                   'meals. Slovak-funded travel requires the stated sending-sector affiliation.'},
 '89': {'categories': ['scholarships'],
        'evidence': ["Target during the stay: bachelor's students, master's students, doctoral "
                     'students. Sending-country/institutional affiliation and the actual programme '
                     'conditions are distinct from a universal Slovak-citizenship rule.',
                     'Full published programme-specific application, academic/language and funding '
                     'conditions apply; travel/supplements require the stated Slovak '
                     'education-sector affiliation.'],
        'host_countries': ['BE'],
        'kind': 'programme-overview',
        'summary': "Bilateral mobility to Belgium for bachelor's students, master's students, "
                   'doctoral students: three weeks. The host covers French-language/literature '
                   'course registration, campus accommodation and meals. Slovak-funded travel '
                   'requires the stated sending-sector affiliation.'},
 '90': {'categories': ['scholarships'],
        'evidence': ['Target during the stay: other specified applicants. '
                     'Sending-country/institutional affiliation and the actual programme '
                     'conditions are distinct from a universal Slovak-citizenship rule.',
                     'Full published programme-specific application, academic/language and funding '
                     'conditions apply; travel/supplements require the stated Slovak '
                     'education-sector affiliation.'],
        'host_countries': ['BE'],
        'kind': 'programme-overview',
        'summary': 'Bilateral mobility to Belgium for other specified applicants: two weeks. The '
                   'host covers registration, accommodation and meals; international travel is '
                   'paid by the relevant sending sector.'},
 '96': {'categories': ['scholarships'],
        'evidence': ["Target during the stay: bachelor's students, master's students, doctoral "
                     'students. Sending-country/institutional affiliation and the actual programme '
                     'conditions are distinct from a universal Slovak-citizenship rule.',
                     'Full published programme-specific application, academic/language and funding '
                     'conditions apply; travel/supplements require the stated Slovak '
                     'education-sector affiliation.'],
        'host_countries': ['CN'],
        'kind': 'programme-overview',
        'summary': "Bilateral mobility to China for bachelor's students, master's students, "
                   'doctoral students: one year, extendable to at most two years. The source gives '
                   "Slovak monthly supplements of EUR 426/361/446 for bachelor's/master's/doctoral "
                   'study plus Chinese stipends of CNY 2,500/3,000/3,500 monthly. Tuition, '
                   'accommodation and basic study materials are waived; medical support applies. '
                   'Slovak-funded travel requires the stated sending-sector affiliation.'},
 '98': {'categories': ['scholarships'],
        'evidence': ['Target during the stay: university teachers, researchers. '
                     'Sending-country/institutional affiliation and the actual programme '
                     'conditions are distinct from a universal Slovak-citizenship rule.',
                     'Full published programme-specific application, academic/language and funding '
                     'conditions apply; travel/supplements require the stated Slovak '
                     'education-sector affiliation.'],
        'host_countries': ['CN'],
        'kind': 'programme-overview',
        'summary': 'Bilateral mobility to China for university teachers, researchers: one year, '
                   'extendable to at most two years. The source gives a Slovak USD 200/month '
                   'supplement and Chinese support of CNY 1,700–2,000/month, plus tuition, '
                   'accommodation, basic study materials and medical support. Slovak-funded travel '
                   'requires the stated sending-sector affiliation.'}}


_OPENER = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPCookieProcessor(_COOKIES))

if __name__ == "__main__":
    sys.exit(main())
