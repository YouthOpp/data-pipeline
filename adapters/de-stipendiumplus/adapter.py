"""Collect genuine StipendiumPlus funding frameworks and training editions."""

import argparse
import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
from html.parser import HTMLParser
import ipaddress
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

SOURCE_ID = "de-stipendiumplus"
SOURCE_URL = "https://stipendiumplus.de/"
WEBSITE_URL = SOURCE_URL
LANGUAGE = "de"
PUBLISHER_COUNTRY = "DE"
PUBLISHER_TYPE = "nonprofit"
ATTRIBUTION = (
    "StipendiumPlus — Arbeitsgemeinschaft der Begabtenförderungswerke "
    "der Bundesrepublik Deutschland"
)


class Node:
    """Minimal HTML tree retaining official links and structural boundaries."""

    def __init__(self, tag="", attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []

    def text(self):
        if self.tag in ("script", "style", "noscript"):
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


def page(url):
    delay = check_robots(url)
    document = fetch_source(url, min_interval=delay)
    if not re.search(
        r"</body\s*>\s*(?:</html\s*>\s*)?$", document, re.IGNORECASE
    ):
        raise AdapterError("Incomplete publisher document", "parse")
    return PublisherHTML(document).root


def article(root):
    return one(
        [n for n in nodes(root, "main") if n.attrs.get("id") == "main-content"],
        "complete public StipendiumPlus article",
    )


def reviewed_url(value):
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "stipendiumplus.de"
        or parsed.query
        or parsed.username
        or parsed.password
    ):
        raise AdapterError("Unexpected source URL", "parse")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path or "/", "", "")
    )


def source_facts(key, root):
    """Retain complete substantive facts and finite own-route inventory."""
    canonical = one(
        [
            n.attrs.get("href", "")
            for n in nodes(root, "link")
            if n.attrs.get("rel") == "canonical"
        ],
        "publisher canonical",
    )
    if canonical != PAGES[key]:
        raise AdapterError("Publisher canonical changed", "parse")
    content = article(root)
    text = content.text().replace("\xad", "").replace("\u200b", "")
    if key == "imprint":
        # Rights are material. Personal addresses and contacts are not records.
        marker = "Haftung für Inhalte"
        if marker not in text:
            raise AdapterError("Publisher rights evidence missing", "access")
        publisher = re.search(r"Herausgeber (.*?) Technischer Betreiber:", text)
        if not publisher:
            raise AdapterError("Publisher attribution missing", "access")
        text = publisher[1] + " " + text[text.index(marker) :]
    elif key == "academy-2027":
        text = re.sub(
            r"Kontakt Hauptansprechpartner .*?Weitere Themen",
            "Weitere Themen",
            text,
        )
    elif key == "academy-2025":
        text = re.sub(
            r"Ansprechpartner:innen bei der Studienstiftung .*?"
            r"Warum das zählt",
            "Warum das zählt",
            text,
        )
    text = " ".join(text.split())
    links = set()
    for anchor in nodes(root, "a"):
        href = anchor.attrs.get("href", "")
        if not href or href.startswith(("mailto:", "tel:")):
            continue
        url = urllib.parse.urljoin(PAGES[key], href)
        parsed = urllib.parse.urlsplit(url)
        if (parsed.hostname or "").lower().removeprefix("www.") == (
            "stipendiumplus.de"
        ):
            if parsed.path.startswith("/wp-content/"):
                # Media/PDFs are not copied or fetched as award identities.
                continue
            links.add(reviewed_url(url))
    policies = set()
    for anchor in nodes(root, "a"):
        label = anchor.text().lower().replace("\xad", "")
        if re.search(
            r"impressum|datenschutz|copyright|lizenz|nutzungsbedingungen"
            r"|nutzungsrechte|rechtliche hinweise|agb|terms|licen[cs]e",
            label,
        ):
            policies.add(
                urllib.parse.urljoin(PAGES[key], anchor.attrs.get("href", ""))
            )
    if policies != {WEBSITE_URL + "impressum/", WEBSITE_URL + "datenschutz/"}:
        raise AdapterError("Publisher policy navigation changed", "access")
    return json.dumps(
        {
            "text": text,
            "own_routes": sorted(links),
            "policy_routes": sorted(policies),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


PAGES = {
    "academy-2025": (
        "https://stipendiumplus.de/die-werke/gemeinsame-sommerakademie/"
        "die-gemeinsame-sommerakademie-2025/"
    ),
    "academy-2027": (
        "https://stipendiumplus.de/die-werke/gemeinsame-sommerakademie/"
        "die-gemeinsame-sommerakademie-2027/"
    ),
    "academy": (
        "https://stipendiumplus.de/die-werke/gemeinsame-sommerakademie/"
    ),
    "chin-kobe": (
        "https://stipendiumplus.de/die-werke/china-kompetenz-projekt-ch"
        "in-kobe/"
    ),
    "doctoral-application": (
        "https://stipendiumplus.de/promovierende/wie-und-wann-bewerbe-i"
        "ch-mich/"
    ),
    "doctoral-benefits": (
        "https://stipendiumplus.de/promovierende/was-bietet-mir-ein-sti"
        "pendium/"
    ),
    "doctoral-eligibility": (
        "https://stipendiumplus.de/promovierende/was-sollte-ich-mitbrin" "gen/"
    ),
    "doctoral-faq": (
        "https://stipendiumplus.de/promovierende/haeufig-gestellte-frag" "en/"
    ),
    "doctoral-initiative": (
        "https://stipendiumplus.de/promovierende/die-promovierenden-ini"
        "tiative/"
    ),
    "doctoral": "https://stipendiumplus.de/promovierende/",
    "home": "https://stipendiumplus.de/",
    "imprint": "https://stipendiumplus.de/impressum/",
    "nie-wieder": (
        "https://stipendiumplus.de/die-werke/nie-wieder-gemeinsam-gegen"
        "-antisemitismus-fur-eine-plurale-gesellschaft/"
    ),
    "schools": "https://stipendiumplus.de/fuer-schulen-und-hochschulen/",
    "student-application": (
        "https://stipendiumplus.de/studierende/wie-und-wann-bewerbe-ich"
        "-mich/"
    ),
    "student-benefits": (
        "https://stipendiumplus.de/studierende/was-bietet-mir-ein-stipe"
        "ndium/"
    ),
    "student-eligibility": (
        "https://stipendiumplus.de/studierende/was-sollte-ich-mitbringe" "n/"
    ),
    "student-faq": (
        "https://stipendiumplus.de/studierende/haeufig-gestellte-fragen" "/"
    ),
    "students": "https://stipendiumplus.de/studierende/",
    "trainees": "https://stipendiumplus.de/auszubildende/",
    "works": "https://stipendiumplus.de/die-werke/",
}

CONDITIONS = {
    "academy-2025": (
        "93fe77c950f7b6dd5d15092f9757f90d1343c6fe991e69fc1c9809d846354214"
    ),
    "academy-2027": (
        "8f932e84420cf4e4da35d5e248c10fbeae98e0cfe0699d738c7b375d99da0281"
    ),
    "academy": (
        "b1236f3650396235ec8bde6e84ce697536c2a631ef021aaa65c8ba51754cadea"
    ),
    "chin-kobe": (
        "0fcd20063ce7f5c9904c137aa01514243c0850064a05de5392ed52719a7b56ff"
    ),
    "doctoral-application": (
        "94aa55fec117152f979733b41075bee6e8c88ef351f0fca37f53d86bb6c7cc2a"
    ),
    "doctoral-benefits": (
        "6c1fe7f2cfadacf257ab76d1bdc911ba72e1d2407caa7467d9e0d23d38489300"
    ),
    "doctoral-eligibility": (
        "97d008aa33782ba951786fa885a9747f9c4f1e5bd3db3a024a7fc25d1fb5785c"
    ),
    "doctoral-faq": (
        "933e1fb23fc2da7315295739f32efa129a24d1258414c8d1957bf77f34c4ce34"
    ),
    "doctoral-initiative": (
        "227f49b082067c6a15e1913595054950ab57711210c7981c193a75642d6b127c"
    ),
    "doctoral": (
        "dc03561316696dc7fc86ddaee0690b1c5dc50863e4d9536d4c4ad06ec2206a54"
    ),
    "home": (
        "0e424b2a5796ded0390cac5bbdca7ba0e51d18c40a217bdddbf1fb658236ebdf"
    ),
    "imprint": (
        "4851709bf682479990a3d4df48db8de8177b01c3706240f075f32529db41c151"
    ),
    "nie-wieder": (
        "e7315dc3f88085edb6e0ae13467cc6d1d00bcc1bf1fac8646cbcaf68c79bae2c"
    ),
    "schools": (
        "6e9152d486cbcc3d2660c7a0a85d8280aa96010db2f4b6ec388ba8bfc092f2ed"
    ),
    "student-application": (
        "7ddb49ab744eb6f7bae56d4ed33451e3fea80baf7c5eb4f540cb6f6cc0c67339"
    ),
    "student-benefits": (
        "7d55d5fb437dcc893df4b96e69560acb5c9eddc6bd53a415c39843485c3dbca4"
    ),
    "student-eligibility": (
        "8106b2f2304c6e709f2f15500970227443c176083ed146ac41df2b348a05e945"
    ),
    "student-faq": (
        "e6f8824d5ff30cb9c3f515698ac298e423824784972c93b9e71a1858878be0d6"
    ),
    "students": (
        "26796517e59f6d538126d3aa71e8fcca144806afe4364cc2a6a23f13e5c8b252"
    ),
    "trainees": (
        "4fd361d7f42038ce51adbb3d2d26f8e05a094d230c62916c8ef437c30d2b408a"
    ),
    "works": (
        "56a6c207ceb4397d6fd6fb3504f37af38c76143c74c27c970df73f9a8a38c579"
    ),
}

PROFILES = [
    {
        "track": "student-scholarships",
        "key": "student-benefits",
        "title": "Begabtenförderungswerke student scholarships",
        "categories": ["scholarships"],
        "hosts": [],
        "kind": "programme-overview",
        "summary": (
            "Member-administered study scholarships offer €300 monthly "
            "independent of income plus means-tested maintenance up to "
            "€855 monthly. Recognized HEI enrollment, performance, "
            "social engagement and member values matter. Overseas "
            "support is possible; member deadlines vary, with no "
            "central application."
        ),
        "evidence": (
            "The 13 BMFTR-supported works are administrators, not 13 "
            "cloned awards. Monthly €300 study-cost allowance is "
            "income-independent; maintenance up to €855/month "
            "considers parental and own income under BAföG alignment. "
            "Nonrepayable and taxfree, duration follows BAföG maximum. "
            "Conditional insurance: health up to €102 and care €35 if "
            "not insured through parents; family €155/month and "
            "childcare €160 per child. Overseas study, language "
            "courses and internships, including beginners abroad, may "
            "receive travel/tuition/overseas supplements. "
            "State-recognized university/FH enrollment and formal "
            "admission required; Abitur not mandatory. "
            "Good-to-very-good performance, no mandatory 1.0, social "
            "engagement, curiosity/exchange and democratic member "
            "values; no exclusive discipline or political-party/church "
            "affiliation. Member emphasis varies. Limited side work is "
            "possible with member advice; no doctoral hourly limit "
            "borrowed. Direct member applications may be fixed or "
            "rolling, not one central/open call. No expressed "
            "exclusive study-host country or citizenship whitelist; "
            "German publisher and BAföG basis do not prove actual "
            "venue. Conditions: https://stipendiumplus.de/studierende/w"
            "as-sollte-ich-mitbringen/ ; https://stipendiumplus.de/stud"
            "ierende/wie-und-wann-bewerbe-ich-mich/ ; "
            "https://stipendiumplus.de/studierende/haeufig-gestellte-fr"
            "agen/"
        ),
    },
    {
        "track": "doctoral-scholarships",
        "key": "doctoral-benefits",
        "title": "Begabtenförderungswerke doctoral scholarships",
        "categories": ["scholarships"],
        "hosts": ["DE"],
        "kind": "programme-overview",
        "summary": (
            "Doctoral scholarships provide €1,750 monthly including "
            "€100 research support, normally for 3.5 years. Any "
            "nationality can apply for a doctorate at a German HEI; "
            "overseas exceptions require German citizenship or an "
            "Abitur obtained in Germany. Strong supervised research "
            "and engagement "
            "matter; member deadlines vary."
        ),
        "evidence": (
            "€1750/month consists of €1650 maintenance plus €100 "
            "research, not €1850; taxfree. Conditional health "
            "insurance up to €100/month, child/overseas support. "
            "Normal maximum 3.5 years, possible 4.5 with children; "
            "birth during funding extends by 12 months and mothers may "
            "request 3 additional maternity months, not an "
            "unconditional combined maximum. Side work alternatives: "
            "up to quarter research/teaching post at "
            "HEI/extra-university research institution OR up to "
            "5h/week outside academia. All disciplines, very good "
            "degree performance without fixed top grade/NC, innovative "
            "scientifically grounded project with reliable "
            "supervision/clear affiliation, engagement/interests and "
            "member values. Prior student funding not required. More "
            "than 4 years since last degree needs justification: "
            "pregnancy/childcare, long illness or caring for relative "
            "with at least Pflegegrad3 may count; ordinary job, "
            "Referendariat or Vikariat do not. No purely "
            "educational-only scholarship, double financial or "
            "ideational funding, or extra degree such as "
            "MBA/psychotherapy training. Prior scholarship counts "
            "toward maximum, university employment does not. Medical "
            "doctorate during study excluded; only after second state "
            "examination. Second PhD generally excluded except "
            "medical-study doctorate followed by scientific doctorate "
            "in another field. Multiple member applications allowed "
            "but only one eventual funder; individual deadlines. Any "
            "nationality for German-HEI doctorate; overseas doctorates "
            "only reasoned exceptions with German citizenship OR "
            "Abitur obtained in Germany, not a DE-only overall "
            "citizenship list. Cotutelle generally possible; known DE "
            "host is not exclusive. Conditions: "
            "https://stipendiumplus.de/promovierende/was-sollte-ich-mit"
            "bringen/ ; https://stipendiumplus.de/promovierende/wie-und"
            "-wann-bewerbe-ich-mich/ ; https://stipendiumplus.de/promov"
            "ierende/haeufig-gestellte-fragen/"
        ),
    },
    {
        "track": "summer-academy-2027",
        "key": "academy-2027",
        "title": (
            "Gemeinsame Sommerakademie 2027 — coexistence and common " "good"
        ),
        "categories": ["training"],
        "hosts": ["DE"],
        "kind": "opportunity",
        "summary": (
            "Training in Bad Staffelstein, Germany, on 5–10 September "
            "2027 explores coexistence, individuality and common good "
            "through interdisciplinary dialogue. For funded members of "
            "the 13 works and SBB; ten per organization, 140 places. "
            "These are event dates; an application opening or deadline "
            "is not published."
        ),
        "evidence": (
            "Actual distinct 2027 edition, not an evergreen duplicate "
            "or invented annual call. Bad Staffelstein near Bamberg "
            "establishes DE host. Event dates 5–10 September2027 are "
            "not application dates; no time-of-day/zone or "
            "registration state proved. Existing funded scholars of 13 "
            "works plus SBB, ten each equals 140 capacity, not 140 "
            "grants/provider clones. sdw and Hanns-Seidel organize "
            "with other works/SBB. Scientific and personal "
            "perspectives, interdisciplinary working groups, "
            "democratic responsibility and plural coexistence. No "
            "common payment amount or nationality condition. Parent "
            "edition inventory: https://stipendiumplus.de/die-werke/gem"
            "einsame-sommerakademie/"
        ),
    },
    {
        "track": "summer-academy-2025",
        "key": "academy-2025",
        "title": (
            "Gemeinsame Sommerakademie 2025 — historical "
            "debate-culture training"
        ),
        "categories": ["training"],
        "hosts": ["DE"],
        "kind": "opportunity",
        "summary": (
            "Historical training edition held in Heidelberg, Germany, "
            "on 17–22 August 2025. Funded members of the 13 works and "
            "SBB explored democratic debate and plural worldviews in "
            "eleven working groups. This is historical event timing, "
            "not a current registration offer or an application "
            "deadline."
        ),
        "evidence": (
            "Actually linked separate 2025 edition, not guessed "
            "archive/evergreen clone. Heidelberg DE, "
            "event17–22August2025, explicitly historical; no invented "
            "closed application state or deadline from past event "
            "dates. Studienstiftung organized with 13 works/SBB, ten "
            "funded members per organization. Eleven concrete "
            "participant working groups span democratic "
            "debate/ambiguity, extremism prevention, remembrance, "
            "media/digital change, social partnership/tariff "
            "negotiations, Betzavta/dialogue/political outrage, "
            "working-world debate and courage to speak. Curriculum is "
            "substantive own HTML, not merely a portrait or impact "
            "story; no eleven working-group/provider clones or copied "
            "PDF. No precise registration dates, time/zone, payment or "
            "nationality rule. Parent: https://stipendiumplus.de/die-we"
            "rke/gemeinsame-sommerakademie/"
        ),
    },
    {
        "track": "chin-kobe",
        "key": "chin-kobe",
        "title": "CHIN-KoBe China competence programme",
        "categories": ["training"],
        "hosts": ["DE", "CN", "TW"],
        "kind": "programme-overview",
        "summary": (
            "China competence training for funded members of the 13 "
            "works and SBB, particularly suitable for STEM, economics "
            "and teacher-training students. Modules include language "
            "courses in Bochum, China-in-Europe seminars, a China "
            "study trip and a Taiwan academy. No current module "
            "deadline is published."
        ),
        "evidence": (
            "Hans-Böckler-led funded-member programme; especially "
            "suitable fields are not mandatory exclusive eligibility. "
            "LSI Bochum language courses establish DE, China study "
            "trip CN and Taiwan academy TW as known nonexclusive "
            "module hosts; not every module is hosted in all three and "
            "China-in-Europe does not justify an EU country whitelist. "
            "Language/culture/politics/society and intercultural "
            "professional cooperation. One programme, no "
            "country/module clones or external administrator "
            "catalogue. No own current module date, amount, "
            "nationality or unrestricted public admission."
        ),
    },
    {
        "track": "nie-wieder",
        "key": "nie-wieder",
        "title": (
            "Nie wieder!? — antisemitism awareness and plural society "
            "training"
        ),
        "categories": ["training"],
        "hosts": [],
        "kind": "programme-overview",
        "summary": (
            "ELES-led training invites funded scholars of the 13 works "
            "to multi-day seminars, workshops and reflection on "
            "antisemitism and plural democratic society. Public "
            "evening gatherings are a separate audience. The own "
            "source states no training venue country or current "
            "application deadline."
        ),
        "evidence": (
            "Existing funded scholars of 13 works receive expert "
            "talks, sensitization training, workshops/reflection and "
            "in-person seminars; public evenings allow interested "
            "public/civil society but do not open scholar-only "
            "training to everyone. ELES-led common educational "
            "programme, not a bare external directory. No own venue "
            "country: German publisher/administrator/patron "
            "affiliation is not host evidence. No precise "
            "event/application dates, payment or citizenship "
            "criterion; no external programme catalogue."
        ),
    },
    {
        "track": "aufstiegsstipendium",
        "key": "student-benefits",
        "title": (
            "Aufstiegsstipendium for experienced vocational " "professionals"
        ),
        "categories": ["scholarships"],
        "hosts": [],
        "kind": "programme-overview",
        "summary": (
            "A federal scholarship supports a first academic "
            "higher-education degree for people with vocational "
            "qualifications and several years of practical experience, "
            "full time or alongside work. SBB administers this "
            "separately described programme. Own-source amounts, host "
            "countries and deadlines are unspecified."
        ),
        "evidence": (
            "Own substantive named paragraph establishes actual "
            "federal first-degree funding, not a bare partner "
            "logo/link. Vocational training and several years "
            "practical experience, full time or alongside employment. "
            "SBB cooperation administrator. Shared student-benefits "
            "URL still distinct genuine named identity. Equivalent "
            "apprentice-page paragraph deduplicated: "
            "https://stipendiumplus.de/auszubildende/. No borrowed "
            "undergraduate/doctoral monetary rates, duration, "
            "residence, nationality or host inference, external SBB "
            "collection or admission of all current apprentices "
            "without completed qualifications/experience."
        ),
    },
    {
        "track": "apprentice-scholarships",
        "key": "trainees",
        "title": (
            "Begabtenförderungswerke apprentice scholarships and "
            "educational support"
        ),
        "categories": ["scholarships"],
        "hosts": [],
        "kind": "programme-overview",
        "summary": (
            "Financial and educational support for people in "
            "state-recognized vocational training with social interest "
            "and engagement, across all occupational fields. "
            "Mentoring, workshops, exchange and guidance accompany "
            "member-dependent scholarships or cooperation routes. No "
            "common rates or central deadline are stated."
        ),
        "evidence": (
            "Genuine common funding cohort on substantive own "
            "apprentice page, not an invented central cash entitlement "
            "or 13 directory clones. Current state-recognized "
            "vocational training and social interest/engagement; all "
            "occupations eligible, examples "
            "craft/care/administration/technology are not separate "
            "tracks. Financial and educational support, "
            "mentoring/workshops/exchange/personal guidance; overseas "
            "opportunities possible. Members vary between own "
            "scholarships and cooperation with SBB or "
            "vocational-education providers. Missing common "
            "rate/central application does not erase genuine support; "
            "amount/duration/uniform date unspecified. No "
            "citizenship/ordinary host expressed: German publisher "
            "does not imply hostDE. Do not borrow student/doctoral "
            "rates or distinct Aufstiegsstipendium "
            "completed-vocational/experience/first-degree conditions."
        ),
    },
]


def read_pages():
    return {key: page(url) for key, url in PAGES.items()}


def parse_inventory(pages):
    if set(pages) != set(PAGES):
        raise AdapterError("Incomplete source evidence", "parse")
    for key, root in pages.items():
        digest = hashlib.sha256(source_facts(key, root).encode()).hexdigest()
        if digest != CONDITIONS[key]:
            raise AdapterError(
                "Source facts/inventory changed: " + key, "parse"
            )
    records = []
    for profile in PROFILES:
        record = make_record(
            profile["title"],
            PAGES[profile["key"]],
            profile["categories"],
            kind=profile["kind"],
            host_countries=profile["hosts"],
            evidence=[ATTRIBUTION, profile["evidence"]],
        )
        record["id"] = hashlib.sha256(
            (SOURCE_ID + "|" + profile["track"]).encode()
        ).hexdigest()[:24]
        record["summary"] = profile["summary"]
        records.append(record)
    validate_records(records)
    return records


def collect():
    return parse_inventory(read_pages())


CATEGORIES = (
    "scholarships",
    "internships",
    "volunteering",
    "fellowships",
    "training",
    "competitions",
    "grants",
    "jobs",
    "other",
)
_GITHUB = "https://api.github.com/repos/YouthOpps/data-source"
_USER_AGENT = "YouthOpp/1.0 (+https://github.com/YouthOpps/data-pipeline)"


class AdapterError(Exception):
    def __init__(self, message, stage="parse", status=None):
        super().__init__(message)
        self.stage, self.status = stage, status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, url):
        return None


_OPENER = urllib.request.build_opener(NoRedirect)
_ROBOTS = {}


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


def pace(url, interval=6):
    """Lock shared publisher pacing state across overlapping local processes.

    State lives outside the checkout. The advisory lock stays held until the
    response and any Retry-After are recorded, so parallel runs cannot race.
    Production uses a serialized Action; separate machines must not overlap.
    """
    host = (
        (urllib.parse.urlsplit(url).hostname or "").lower().removeprefix("www.")
    )
    directory = os.path.join(
        tempfile.gettempdir(), "de-stipendiumplus-pacing-" + str(os.getuid())
    )
    os.makedirs(directory, mode=0o700, exist_ok=True)
    information = os.lstat(directory)
    if (
        not os.path.isdir(directory)
        or os.path.islink(directory)
        or information.st_uid != os.getuid()
        or information.st_mode & 0o077
    ):
        raise AdapterError("Unsafe publisher pacing directory", "access")
    publisher = "stipendiumplus.de" if host == "stipendiumplus.de" else host
    key = hashlib.sha256(publisher.encode()).hexdigest()
    descriptor = os.open(
        os.path.join(directory, key),
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
        0o600,
    )
    state = os.fdopen(descriptor, "r+", encoding="utf-8")
    try:
        fcntl.flock(state.fileno(), fcntl.LOCK_EX)
        raw = state.read()
        budget = (
            json.loads(raw)
            if raw
            else {"starts": [], "interval": 6, "until": 0}
        )
        budget["interval"] = max(budget["interval"], interval, 6)
        while True:
            now = time.time()
            budget["starts"] = [
                start for start in budget["starts"] if now - start < 60
            ]
            ready = max(
                budget["until"],
                (
                    budget["starts"][-1] + budget["interval"]
                    if budget["starts"]
                    else now
                ),
                (
                    budget["starts"][0] + 60
                    if len(budget["starts"]) >= 10
                    else now
                ),
            )
            if ready <= now:
                budget["starts"].append(now)
                save_budget(state, budget)
                return state, budget
            time.sleep(min(ready - now, 60))
    except Exception:
        state.close()
        raise


def save_budget(state, budget):
    state.seek(0)
    json.dump(budget, state)
    state.truncate()
    state.flush()
    os.fsync(state.fileno())


def request_bytes(url, *, interval=6, method="GET", headers=None, payload=None):
    state, budget = pace(url, interval)
    request_headers = {"User-Agent": _USER_AGENT, **(headers or {})}
    try:
        request = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers=request_headers,
        )
        try:
            response = _OPENER.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            retry = response.headers.get("Retry-After")
            if retry:
                try:
                    seconds = (
                        float(retry)
                        if re.fullmatch(r"\d+(?:\.\d+)?", retry)
                        else parsedate_to_datetime(retry).timestamp()
                        - time.time()
                    )
                    budget["until"] = max(
                        budget["until"], time.time() + max(0, seconds)
                    )
                    save_budget(state, budget)
                except (ValueError, TypeError, OverflowError):
                    pass
            body = response.read(5_000_001)
            if len(body) > 5_000_000:
                raise AdapterError("Response exceeds 5 MB", "fetch")
            return response.status, response.headers, body
    finally:
        state.close()


def fetch_source(url, min_interval=6):
    original = urllib.parse.urlsplit(url)

    def checked(value):
        parsed = urllib.parse.urlsplit(value)
        host = (parsed.hostname or "").lower().removeprefix("www.")
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or host != (original.hostname or "").lower().removeprefix("www.")
            or parsed.port != original.port
        ):
            raise AdapterError(
                "Source redirect leaves the reviewed HTTPS host", "fetch"
            )
        if host == "localhost" or host.endswith(".localhost"):
            raise AdapterError("Public HTTPS source required", "fetch")
        try:
            if not ipaddress.ip_address(host).is_global:
                raise AdapterError("Public HTTPS source required", "fetch")
        except ValueError:
            pass

    for attempt in range(2):
        current = url
        try:
            for hop in range(4):
                checked(current)
                status, headers, body = request_bytes(
                    current, interval=min_interval
                )
                if status in (301, 302, 303, 307, 308):
                    if hop == 3 or not headers.get("Location"):
                        raise AdapterError(
                            "Invalid or excessive source redirects", "fetch"
                        )
                    current = urllib.parse.urljoin(current, headers["Location"])
                    checked(current)
                    if original.path != "/robots.txt":
                        min_interval = max(min_interval, check_robots(current))
                    continue
                if status != 200:
                    raise AdapterError(
                        f"Publisher returned HTTP {status} for "
                        + urllib.parse.urlunsplit(
                            (
                                urllib.parse.urlsplit(current).scheme,
                                urllib.parse.urlsplit(current).hostname or "",
                                urllib.parse.urlsplit(current).path,
                                "",
                                "",
                            )
                        ),
                        "fetch",
                        status,
                    )
                return body.decode(headers.get_content_charset() or "utf-8")
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            if attempt:
                raise AdapterError(
                    "Publisher connection failed: " + safe_error(error), "fetch"
                ) from error
    raise AdapterError("Publisher could not be read", "fetch")


def check_robots(url):
    parsed = urllib.parse.urlsplit(url)
    origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    if origin not in _ROBOTS:
        try:
            _ROBOTS[origin] = fetch_source(origin + "/robots.txt")
        except AdapterError as error:
            if error.status == 404:
                _ROBOTS[origin] = ""
            else:
                raise
    text = _ROBOTS[origin]
    groups, group = [], {"agents": [], "rules": [], "delay": 0}
    for line in text.splitlines():
        key, separator, value = line.split("#", 1)[0].strip().partition(":")
        if not separator:
            continue
        key, value = key.lower().strip(), value.strip()
        if key == "user-agent":
            if group["rules"] or group["delay"]:
                groups.append(group)
                group = {"agents": [], "rules": [], "delay": 0}
            group["agents"].append(value.lower())
        elif key in ("allow", "disallow"):
            group["rules"].append((key, value))
        elif key == "crawl-delay":
            try:
                group["delay"] = float(value)
                if not 0 <= group["delay"] <= 60:
                    raise ValueError()
            except ValueError:
                raise AdapterError("Unsupported robots crawl delay", "access")
    groups.append(group)
    directives = [
        line.split("#", 1)[0].strip().lower()
        for line in text.splitlines()
        if line.split("#", 1)[0].strip()
    ]
    if (
        directives
        and not any(g["agents"] for g in groups)
        and not all(line.startswith("sitemap:") for line in directives)
    ):
        raise AdapterError("Robots policy is unavailable or invalid", "access")
    specific = [g for g in groups if "youthopp" in g["agents"]]
    selected = specific or [g for g in groups if "*" in g["agents"]]
    path = parsed.path + ("?" + parsed.query if parsed.query else "")
    matches = []
    for g in selected:
        for directive, pattern in g["rules"]:
            if not pattern:
                continue
            expression = (
                "^"
                + ".*".join(
                    re.escape(part)
                    for part in pattern.removesuffix("$").split("*")
                )
                + ("$" if pattern.endswith("$") else "")
            )
            if re.search(expression, path):
                matches.append(
                    (
                        len(pattern.replace("*", "").removesuffix("$")),
                        directive == "allow",
                    )
                )
    if matches and not max(matches)[1]:
        raise AdapterError(
            "Robots policy disallows this source endpoint", "access"
        )
    return max([6] + [g["delay"] for g in selected])


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
    token = os.environ.get("DATA_SOURCE_TOKEN")
    if not token:
        raise AdapterError(
            "DATA_SOURCE_TOKEN is required for publication", "publish"
        )
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
                f"Collected {len(records)} validated StipendiumPlus "
                "funding frameworks and training editions"
            )
        ),
        "error": safe_error(error) if error else None,
    }
    if ATTRIBUTION:
        result["attribution"] = ATTRIBUTION
    if error:
        result["failure_stage"] = getattr(error, "stage", "parse")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--publish",
        action="store_true",
        help=(
            "Write only this source to YouthOpps/data-source "
            "using DATA_SOURCE_TOKEN"
        ),
    )
    args = parser.parse_args()
    snapshot = read_snapshot() if args.publish else None
    old = (
        json.loads(snapshot["files"]["data.json"])
        if snapshot and snapshot["files"]["data.json"] is not None
        else []
    )
    previous = (
        json.loads(snapshot["files"]["metadata.json"])
        if snapshot and snapshot["files"]["metadata.json"] is not None
        else {}
    )
    if snapshot and snapshot["files"]["data.json"] is not None:
        validate_records(old)
        if previous.get("source") != SOURCE_ID:
            raise AdapterError(
                "Remote metadata source mismatch; no files changed", "publish"
            )
    attempt, failure = utc_now(), None
    try:
        records = collect()
        checked_at = utc_now()
        validate_records(records)
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
            same = {
                key: value
                for key, value in before.items()
                if key not in temporal
            } == {
                key: value
                for key, value in record.items()
                if key not in temporal
            }
            record.update(
                created_at=before.get("created_at", attempt),
                first_seen_at=before.get("first_seen_at", attempt),
                updated_at=(
                    before.get("updated_at", attempt) if same else attempt
                ),
                last_seen_at=attempt,
                last_checked_at=checked_at,
            )
        records.sort(key=lambda record: record["id"])
        records.sort(
            key=lambda record: record["published_at"] or "", reverse=True
        )
        validate_records(records)
    except Exception as error:
        failure, records = error, old
    outcome = metadata(
        attempt,
        previous,
        records,
        failure,
        checked_at=None if failure else checked_at,
    )
    if args.publish and (not failure or old):
        files = {
            "metadata.json": json.dumps(outcome, ensure_ascii=False, indent=2)
            + "\n"
        }
        if not failure:
            files["data.json"] = (
                json.dumps(records, ensure_ascii=False, indent=2) + "\n"
            )
        try:
            publish_files(snapshot, files)
        except Exception as error:
            failure = error
            outcome = metadata(
                attempt,
                previous,
                old,
                AdapterError(
                    "Publication failed: " + safe_error(error), "publish"
                ),
            )
            try:
                latest = read_snapshot()
                if not old or latest["files"] != snapshot["files"]:
                    raise AdapterError(
                        "No safe previous snapshot for failure reporting",
                        "publish",
                    )
                publish_files(
                    latest,
                    {
                        "metadata.json": json.dumps(
                            outcome, ensure_ascii=False, indent=2
                        )
                        + "\n"
                    },
                )
            except Exception:
                outcome["message"] = (
                    "Publication failed; durable failure status could not be "
                    "recorded. Remote data was not force-overwritten."
                )
    elif args.publish:
        outcome["message"] = (
            "First collection failed; no published source folder created"
        )
    print(json.dumps(outcome, ensure_ascii=False))
    return 1 if failure else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(
            json.dumps(
                {
                    "source": SOURCE_ID,
                    "status": "fail",
                    "last_attempt_at": utc_now(),
                    "message": (
                        "Run could not complete; " "durable status unavailable"
                    ),
                    "error": safe_error(error),
                    "failure_stage": getattr(error, "stage", "publish"),
                },
                ensure_ascii=False,
            )
        )
        sys.exit(1)
