"""Collect factual metadata from the reviewed publisher programme page."""

from html.parser import HTMLParser
from html import unescape

SOURCE_ID = "at-oead-ernst-mach"
SOURCE_URL = "https://studyinaustria.at/en/news/article/2026/08/ernst-mach-stipendium-weltweit-bewerbung-bis-1-dezember-2026"
WEBSITE_URL = "https://studyinaustria.at/"
LANGUAGE = "en"
PUBLISHER_COUNTRY = "AT"
PUBLISHER_TYPE = "public-institution"
ATTRIBUTION = "© OeAD"

class ProgrammeHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.canonicals = []
        self.headings = []
        self.structured = []
        self.heading = None
        self.script = None
        self.ignore = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "link" and "canonical" in (attributes.get("rel") or "").lower().split():
            self.canonicals.append(attributes.get("href"))
        if tag in ("script", "style"):
            self.ignore += 1
        if tag == "script" and (attributes.get("type") or "").lower() == "application/ld+json":
            self.script = []
        if tag == "h1" and not self.ignore:
            if self.heading is not None:
                raise ValueError("Malformed programme heading")
            self.heading = []

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        if self.heading is not None and not self.ignore:
            self.heading.append(data)

    def handle_endtag(self, tag):
        if tag == "h1" and self.heading is not None:
            self.headings.append(" ".join(unescape("".join(self.heading)).split()))
            self.heading = None
        if tag == "script" and self.script is not None:
            self.structured.append("".join(self.script))
            self.script = None
        if tag in ("script", "style"):
            self.ignore = max(0, self.ignore - 1)

EXPECTED_TITLE = "Ernst Mach Scholarship – Worldwide: Application Deadline 1 December 2026"

def collect():
    delay = check_robots(SOURCE_URL)
    page = ProgrammeHTML()
    page.feed(fetch_source(SOURCE_URL, min_interval=delay))
    page.close()
    if page.canonicals != [SOURCE_URL]:
        raise ValueError("Reviewed programme canonical URL changed or is ambiguous")
    if page.heading is not None or page.headings != [EXPECTED_TITLE]:
        raise ValueError("Expected one exact reviewed programme heading")
    # A calendar date or closed application message does not establish publication or a deadline.
    return [make_record(EXPECTED_TITLE, SOURCE_URL, ["scholarships"],
                        kind="programme-overview", tags=["programme-overview"],
                        method="editorial-review", evidence=[SOURCE_URL])]

# All transport, output and publication code below belongs to this adapter.
# Python standard library only; no imports from another adapter or project module.
import argparse
import base64
import hashlib
import ipaddress
import os
import re
import sys
import time
import json
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

CATEGORIES = ('scholarships', 'internships', 'volunteering', 'fellowships', 'training', 'competitions', 'grants', 'jobs', 'other')
_GITHUB = 'https://api.github.com/repos/YouthOpps/data-source'
_USER_AGENT = 'YouthOpp/1.0 (+https://github.com/YouthOpps/data-pipeline)'
_BUDGETS = {}


class AdapterError(Exception):
    def __init__(self, message, stage='parse', status=None):
        super().__init__(message)
        self.stage, self.status = stage, status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, url):
        return None


_OPENER = urllib.request.build_opener(NoRedirect)


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def safe_error(error):
    text = str(error)
    text = re.sub(r'https?://[^\s<>"\']+', lambda match: urllib.parse.urlunsplit((urllib.parse.urlsplit(match[0]).scheme, urllib.parse.urlsplit(match[0]).hostname or '', urllib.parse.urlsplit(match[0]).path, '', '')), text)
    text = re.sub(r'(?i)\b(?:bearer|basic)\s+\S+', '[redacted]', text)
    text = re.sub(r'(?i)\b(token|password|secret|api[_-]?key|authorization)\s*[:=]\s*\S+', r'\1=[redacted]', text)
    return ' '.join(text.split())[:500]


def pace(url, interval=6):
    host = (urllib.parse.urlsplit(url).hostname or '').lower().removeprefix('www.')
    budget = _BUDGETS.setdefault(host, {'starts': deque(), 'interval': 6, 'until': 0})
    budget['interval'] = max(budget['interval'], interval, 6)
    while True:
        now = time.monotonic()
        while budget['starts'] and now - budget['starts'][0] >= 60:
            budget['starts'].popleft()
        ready = max(budget['until'], budget['starts'][-1] + budget['interval'] if budget['starts'] else now,
                    budget['starts'][0] + 60 if len(budget['starts']) >= 10 else now)
        if ready <= now:
            budget['starts'].append(now)
            return budget
        time.sleep(ready - now)


def request_bytes(url, *, interval=6, method='GET', headers=None, payload=None):
    budget = pace(url, interval)
    request = urllib.request.Request(url, data=payload, method=method, headers={'User-Agent': _USER_AGENT, **(headers or {})})
    try:
        response = _OPENER.open(request, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        retry = response.headers.get('Retry-After')
        if retry:
            try:
                seconds = float(retry) if re.fullmatch(r'\d+(?:\.\d+)?', retry) else parsedate_to_datetime(retry).timestamp() - time.time()
                budget['until'] = max(budget['until'], time.monotonic() + max(0, seconds))
            except (ValueError, TypeError, OverflowError):
                pass
        body = response.read(5_000_001)
        if len(body) > 5_000_000:
            raise AdapterError('Response exceeds 5 MB', 'fetch')
        return response.status, response.headers, body


def fetch_source(url, min_interval=6):
    original = urllib.parse.urlsplit(url)
    def checked(value):
        parsed = urllib.parse.urlsplit(value)
        host = (parsed.hostname or '').lower().removeprefix('www.')
        if parsed.scheme != 'https' or parsed.username or parsed.password or host != (original.hostname or '').lower().removeprefix('www.') or parsed.port != original.port:
            raise AdapterError('Source redirect leaves the reviewed HTTPS host', 'fetch')
        if host == 'localhost' or host.endswith('.localhost'):
            raise AdapterError('Public HTTPS source required', 'fetch')
        try:
            if not ipaddress.ip_address(host).is_global:
                raise AdapterError('Public HTTPS source required', 'fetch')
        except ValueError:
            pass
    for attempt in range(2):
        current = url
        try:
            for hop in range(4):
                checked(current)
                status, headers, body = request_bytes(current, interval=min_interval)
                if status in (301, 302, 303, 307, 308):
                    if hop == 3 or not headers.get('Location'):
                        raise AdapterError('Invalid or excessive source redirects', 'fetch')
                    current = urllib.parse.urljoin(current, headers['Location'])
                    continue
                if status != 200:
                    raise AdapterError(f'Publisher returned HTTP {status}', 'fetch', status)
                return body.decode(headers.get_content_charset() or 'utf-8')
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if attempt:
                raise AdapterError('Publisher connection failed: ' + safe_error(error), 'fetch') from error
    raise AdapterError('Publisher could not be read', 'fetch')


def check_robots(url):
    parsed = urllib.parse.urlsplit(url)
    try:
        text = fetch_source(urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, '/robots.txt', '', '')))
    except AdapterError as error:
        if error.status == 404:
            return 6
        raise
    groups, group = [], {'agents': [], 'rules': [], 'delay': 0}
    for line in text.splitlines():
        key, separator, value = line.split('#', 1)[0].strip().partition(':')
        if not separator:
            continue
        key, value = key.lower().strip(), value.strip()
        if key == 'user-agent':
            if group['rules'] or group['delay']:
                groups.append(group)
                group = {'agents': [], 'rules': [], 'delay': 0}
            group['agents'].append(value.lower())
        elif key in ('allow', 'disallow'):
            group['rules'].append((key, value))
        elif key == 'crawl-delay':
            try:
                group['delay'] = float(value)
                if not 0 <= group['delay'] <= 60:
                    raise ValueError()
            except ValueError:
                raise AdapterError('Unsupported robots crawl delay', 'access')
    groups.append(group)
    directives = [line.split('#', 1)[0].strip().lower() for line in text.splitlines() if line.split('#', 1)[0].strip()]
    if directives and not any(g['agents'] for g in groups) and not all(line.startswith('sitemap:') for line in directives):
        raise AdapterError('Robots policy is unavailable or invalid', 'access')
    specific = [g for g in groups if 'youthopp' in g['agents']]
    selected = specific or [g for g in groups if '*' in g['agents']]
    path = parsed.path + ('?' + parsed.query if parsed.query else '')
    matches = []
    for g in selected:
        for directive, pattern in g['rules']:
            if not pattern:
                continue
            expression = '^' + '.*'.join(re.escape(part) for part in pattern.removesuffix('$').split('*')) + ('$' if pattern.endswith('$') else '')
            if re.search(expression, path):
                matches.append((len(pattern.replace('*', '').removesuffix('$')), directive == 'allow'))
    if matches and not max(matches)[1]:
        raise AdapterError('Robots policy disallows this source endpoint', 'access')
    return max([6] + [g['delay'] for g in selected])


def make_record(title, url, categories, kind='opportunity', published_at=None, tags=None, host_countries=None, method='editorial-review', evidence=None):
    now = utc_now()
    return {'id': hashlib.sha256(f'{SOURCE_ID}|{url}'.encode()).hexdigest()[:24], 'title': title,
            'url': url, 'source': SOURCE_ID, 'source_url': SOURCE_URL, 'published_at': published_at,
            'summary': '', 'tags': tags or [], 'location': None, 'deadline': None, 'language': LANGUAGE,
            'category': categories[0], 'categories': categories, 'kind': kind, 'host_countries': host_countries or [],
            'eligible_countries': [], 'publisher_country': PUBLISHER_COUNTRY,
            'created_at': now, 'updated_at': now, 'first_seen_at': now, 'last_seen_at': now, 'last_checked_at': now,
            'status': 'unknown', 'classification': {'method': method, 'status': 'unknown' if categories == ['other'] else 'classified', 'evidence': evidence or []}}


def validate_records(records):
    if not isinstance(records, list) or not records:
        raise AdapterError('Empty or invalid opportunity collection', 'validate')
    ids = set()
    for record in records:
        if not isinstance(record, dict) or record.get('source') != SOURCE_ID or record.get('source_url') != SOURCE_URL:
            raise AdapterError('Record source identity mismatch', 'validate')
        for field in ('id', 'title', 'url', 'created_at', 'updated_at', 'first_seen_at', 'last_seen_at', 'last_checked_at'):
            if not isinstance(record.get(field), str) or not record[field].strip():
                raise AdapterError('Missing record field: ' + field, 'validate')
        if record['id'] in ids:
            raise AdapterError('Duplicate opportunity identifier', 'validate')
        ids.add(record['id'])
        url = urllib.parse.urlsplit(record['url'])
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
            raise AdapterError('Unsafe opportunity URL', 'validate')
        for field in ('created_at', 'updated_at', 'first_seen_at', 'last_seen_at', 'last_checked_at', 'published_at', 'deadline'):
            value = record.get(field)
            if field not in record:
                raise AdapterError('Missing record field: ' + field, 'validate')
            if value is not None:
                try:
                    if datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is None:
                        raise ValueError()
                except (ValueError, TypeError, AttributeError):
                    raise AdapterError('Invalid record timestamp: ' + field, 'validate')
        for field in ('tags', 'host_countries', 'eligible_countries'):
            values = record.get(field)
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise AdapterError('Invalid record list: ' + field, 'validate')
            if field != 'tags' and any(not re.fullmatch('[A-Z]{2}', value) for value in values):
                raise AdapterError('Invalid country code', 'validate')
        categories = record.get('categories')
        if not isinstance(categories, list) or not categories or any(value not in CATEGORIES for value in categories) or len(set(categories)) != len(categories) or record.get('category') != categories[0]:
            raise AdapterError('Invalid opportunity categories', 'validate')
        if record.get('kind') not in ('opportunity', 'programme-overview', 'institutional-grant', 'unknown') or record.get('status') not in ('open', 'expired', 'unknown'):
            raise AdapterError('Invalid opportunity kind or status', 'validate')
        if not isinstance(record.get('summary'), str) or len(record['summary']) > 600 or re.search('<[^>]+>', record['summary']):
            raise AdapterError('Invalid plain summary', 'validate')
        for field in ('location', 'language', 'publisher_country'):
            if field not in record or record[field] is not None and not isinstance(record[field], str):
                raise AdapterError('Invalid record field: ' + field, 'validate')
        classification = record.get('classification')
        if not isinstance(classification, dict) or not isinstance(classification.get('method'), str) or classification.get('status') not in ('classified', 'unknown') or not isinstance(classification.get('evidence'), list) or any(not isinstance(value, str) for value in classification['evidence']):
            raise AdapterError('Invalid classification evidence', 'validate')


def github(method, path, payload=None, missing_ok=False):
    token = os.environ.get('DATA_SOURCE_TOKEN')
    if not token:
        raise AdapterError('DATA_SOURCE_TOKEN is required for publication', 'publish')
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2026-03-10'}
    if payload is not None:
        headers['Content-Type'] = 'application/json'
    try:
        status, _, body = request_bytes(_GITHUB + path, method=method, headers=headers,
                                        payload=json.dumps(payload).encode() if payload is not None else None)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise AdapterError('GitHub connection failed; publication is unconfirmed', 'publish') from error
    if status == 404 and missing_ok:
        return None
    if status not in (200, 201):
        raise AdapterError(f'GitHub publication API returned HTTP {status}', 'publish', status)
    return json.loads(body)


def read_snapshot():
    head = github('GET', '/git/ref/heads/main')['object']['sha']
    tree = github('GET', '/git/commits/' + head)['tree']['sha']
    files = {}
    for name in ('data.json', 'metadata.json'):
        file = github('GET', f'/contents/datas/{SOURCE_ID}/{name}?ref={head}', missing_ok=True)
        if file is not None and file.get('encoding') != 'base64':
            raise AdapterError('Unsupported existing source file encoding', 'publish')
        files[name] = None if file is None else base64.b64decode(file['content']).decode('utf-8')
    if (files['data.json'] is None) != (files['metadata.json'] is None):
        raise AdapterError('Incomplete remote source snapshot; no files changed', 'publish')
    return {'head': head, 'tree': tree, 'files': files}


def publish_files(snapshot, files):
    expected = snapshot['files'].copy()
    desired = {**expected, **files}
    for attempt in range(3):
        candidate = None
        try:
            tree = github('POST', '/git/trees', {'base_tree': snapshot['tree'], 'tree': [
                {'path': f'datas/{SOURCE_ID}/{name}', 'mode': '100644', 'type': 'blob', 'content': text}
                for name, text in files.items()]})['sha']
            candidate = github('POST', '/git/commits', {'message': f'Update {SOURCE_ID} collection outcome', 'tree': tree, 'parents': [snapshot['head']]})['sha']
            github('PATCH', '/git/refs/heads/main', {'sha': candidate, 'force': False})
            return
        except AdapterError:
            latest = read_snapshot()
            if candidate and (latest['head'] == candidate or latest['files'] == desired):
                return
            if latest['files'] != expected:
                raise AdapterError('Source changed concurrently; newer remote data was not overwritten', 'publish')
            snapshot = latest
            if attempt == 2:
                raise AdapterError('Publication failed; durable remote outcome could not be updated', 'publish')


def metadata(attempt, previous, records, error=None):
    result = {'source': SOURCE_ID, 'source_url': SOURCE_URL, 'website_url': WEBSITE_URL,
              'language': LANGUAGE, 'publisher_country': PUBLISHER_COUNTRY, 'publisher_type': PUBLISHER_TYPE,
              'status': 'fail' if error else 'success', 'last_attempt_at': attempt,
              'last_success_at': previous.get('last_success_at') if error else attempt,
              'last_checked_at': previous.get('last_checked_at') if error else attempt,
              'record_count': len(records), 'message': 'Collection failed; last-good data preserved' if error else f'Collected and validated {len(records)} records',
              'error': safe_error(error) if error else None}
    if ATTRIBUTION:
        result['attribution'] = ATTRIBUTION
    if error:
        result['failure_stage'] = getattr(error, 'stage', 'parse')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publish', action='store_true', help='Write only this source to YouthOpps/data-source using DATA_SOURCE_TOKEN')
    args = parser.parse_args()
    snapshot = read_snapshot() if args.publish else None
    old = json.loads(snapshot['files']['data.json']) if snapshot and snapshot['files']['data.json'] is not None else []
    previous = json.loads(snapshot['files']['metadata.json']) if snapshot and snapshot['files']['metadata.json'] is not None else {}
    if snapshot and snapshot['files']['data.json'] is not None:
        validate_records(old)
        if previous.get('source') != SOURCE_ID:
            raise AdapterError('Remote metadata source mismatch; no files changed', 'publish')
    attempt, failure = utc_now(), None
    try:
        records = collect()
        validate_records(records)
        prior = {record['id']: record for record in old}
        temporal = {'created_at', 'updated_at', 'first_seen_at', 'last_seen_at', 'last_checked_at'}
        for record in records:
            before = prior.get(record['id'], {})
            same = {key: value for key, value in before.items() if key not in temporal} == {key: value for key, value in record.items() if key not in temporal}
            record.update(created_at=before.get('created_at', attempt), first_seen_at=before.get('first_seen_at', attempt),
                          updated_at=before.get('updated_at', attempt) if same else attempt, last_seen_at=attempt, last_checked_at=attempt)
        records.sort(key=lambda record: record['id'])
        records.sort(key=lambda record: record['published_at'] or '', reverse=True)
        validate_records(records)
    except Exception as error:
        failure, records = error, old
    outcome = metadata(attempt, previous, records, failure)
    if args.publish and (not failure or old):
        files = {'metadata.json': json.dumps(outcome, ensure_ascii=False, indent=2) + '\n'}
        if not failure:
            files['data.json'] = json.dumps(records, ensure_ascii=False, indent=2) + '\n'
        try:
            publish_files(snapshot, files)
        except Exception as error:
            failure = error
            outcome = metadata(attempt, previous, old, AdapterError('Publication failed: ' + safe_error(error), 'publish'))
            try:
                latest = read_snapshot()
                if not old or latest['files'] != snapshot['files']:
                    raise AdapterError('No safe previous snapshot for failure reporting', 'publish')
                publish_files(latest, {'metadata.json': json.dumps(outcome, ensure_ascii=False, indent=2) + '\n'})
            except Exception:
                outcome['message'] = 'Publication failed; durable failure status could not be recorded. Remote data was not force-overwritten.'
    elif args.publish:
        outcome['message'] = 'First collection failed; no published source folder created'
    print(json.dumps(outcome, ensure_ascii=False))
    return 1 if failure else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print(json.dumps({'source': SOURCE_ID, 'status': 'fail', 'last_attempt_at': utc_now(),
                          'message': 'Run could not complete; durable status unavailable',
                          'error': safe_error(error), 'failure_stage': getattr(error, 'stage', 'publish')}, ensure_ascii=False))
        sys.exit(1)
