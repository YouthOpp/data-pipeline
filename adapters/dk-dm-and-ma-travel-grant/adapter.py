"""Collect reviewed DM student funding and member training."""

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

SOURCE_ID = "dk-dm-and-ma-travel-grant"
SOURCE_URL = "https://dm.dk/students/membership/travel-grant/"
WEBSITE_URL = "https://dm.dk/"
LANGUAGE = "en"
PUBLISHER_COUNTRY = "DK"
PUBLISHER_TYPE = "non-profit"
ATTRIBUTION = "DM Students — DM and MA student membership programmes"


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
    document = fetch_source(url, min_interval=check_robots(url))
    if not re.search(
        r"</body\s*>\s*</html\s*>\s*(?:<!--[\s\S]*?-->\s*)*$", document, re.I
    ):
        raise AdapterError("Incomplete publisher document", "parse")
    root = PublisherHTML(document).root
    title = " ".join(n.text() for n in nodes(root, "title")).lower()
    if re.match(
        r"just a moment|access denied|attention required|checking your browser",
        title,
    ):
        raise AdapterError("Publisher access challenge", "access")
    return root


def main_content(root):
    return one(nodes(root, "main"), "complete public programme content")


def personal_nodes(key, root):
    content = main_content(root)
    selected = []
    if key in ("grants-hub", "danish-membership"):
        selected = [
            n
            for n in nodes(content, "span")
            if "[writing-mode:vertical-lr]" in n.attrs.get("class", "").split()
        ]
        expected = 1
    elif key == "online-courses":
        selected = [
            n
            for n in nodes(content, "div")
            if n.attrs.get("class") == "p-1.5 text-2xs"
        ]
        expected = 2
    elif key == "sponsorship-danish":
        selected = [
            n
            for n in nodes(content, "blockquote")
            if n.attrs.get("class")
            == "pl-11 pr-7 py-8 sm:px-20 md:pt-16 md:pb-5 md:px-28 xl:pl-32"
        ]
        expected = 1
    else:
        expected = 0
    if len(selected) != expected:
        raise AdapterError("Personal presentation structure changed", "parse")
    for node in selected:
        if re.search(
            r"copyright|permission|repub|reuse|licen|ophavs|gengiv|"
            r"applicants? must|eligib|requires? consent|written consent|"
            r"skal være medlem|skal være indskrevet|automation|scraping",
            node.text(),
            re.I,
        ):
            raise AdapterError("Conditions in personal presentation", "parse")
    return selected


def without_personal(node, excluded):
    if node in excluded or node.tag in ("script", "style", "noscript"):
        return ""
    return " ".join(
        " ".join(
            without_personal(c, excluded) if isinstance(c, Node) else c
            for c in node.children
        ).split()
    )


def safe_route(value):
    parsed = urllib.parse.urlsplit(urllib.parse.urljoin(WEBSITE_URL, value))
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port
        or (parsed.hostname or "").removeprefix("www.") != "dm.dk"
    ):
        raise AdapterError("Invalid own publisher route", "parse")
    return parsed.path


def source_facts(key, root):
    content = main_content(root)
    if any(n.attrs.get("rel") == "canonical" for n in nodes(root, "link")):
        raise AdapterError("Publisher canonical format changed", "parse")
    canonical = one(
        [
            n.attrs.get("content", "")
            for n in nodes(root, "meta")
            if n.attrs.get("property") == "og:url"
        ],
        "publisher original URL metadata",
    )
    if safe_route(canonical) != safe_route(PAGES[key]):
        raise AdapterError("Publisher canonical changed: " + key, "parse")
    routes = set()
    for n in nodes(root, "a"):
        value = n.attrs.get("href", "")
        parsed = urllib.parse.urlsplit(urllib.parse.urljoin(PAGES[key], value))
        if (parsed.hostname or "").removeprefix("www.") == "dm.dk":
            route = safe_route(urllib.parse.urlunsplit(parsed))
            # News reports are not the bounded member programme frontier.
            if "/nyheder/" in route or route.startswith("/nyheder/"):
                route = "<news-report>"
            routes.add(route)
    footer = one(nodes(root, "footer"), "publisher footer")
    legal = [
        n.text()
        for n in nodes(footer)
        if n.tag in ("p", "small", "span")
        and re.search(
            r"copyright|©|licen|permission|ophavs|gengiv|repub|reuse|"
            r"scrap|automat|robot|tillad|vilkår|vilkaar",
            n.text(),
            re.I,
        )
    ]
    legal_links = []
    for node in nodes(root, "a"):
        value = node.attrs.get("href", "")
        if value.startswith("mailto:") and not node.text():
            continue
        if re.search(
            r"terms|privacy|cookie|licen|copyright|automation|automated|"
            r"permission|reuse|repub|vilkår|vilkaar|privatliv|ophavs|gengiv",
            node.text() + " " + value,
            re.I,
        ):
            parsed = urllib.parse.urlsplit(
                urllib.parse.urljoin(PAGES[key], value)
            )
            if parsed.scheme == "https" and not (
                parsed.username or parsed.password
            ):
                legal_links.append(
                    [
                        node.text(),
                        urllib.parse.urlunsplit(
                            parsed._replace(query="", fragment="")
                        ),
                    ]
                )
            else:
                raise AdapterError("Unsafe policy dependency", "parse")
    text = without_personal(content, personal_nodes(key, root))
    if key in ("home", "danish-students", "students"):
        # Landing news/event cards change; retain own navigation and new
        # funding, training and rights headings for substantive discovery.
        text = " | ".join(
            n.text()
            for n in nodes(content)
            if (
                n.tag in ("h1", "h2", "h3", "p")
                or any(
                    isinstance(c, str)
                    and re.search(
                        r"permission|repub|reuse|licen|copyright|ophavs|gengiv|"
                        r"scraping|automation|automated|terms of use|"
                        r"scholarship|grant|funding|legat|sponsor",
                        c,
                        re.I,
                    )
                    for c in n.children
                )
            )
            and re.search(
                r"legat|sponsor|fund|grant|kurs|class|mentor|"
                r"copyright|permission|licen|ophavs|gengiv|repub|reuse|"
                r"scraping|automation|automated|terms of use|written consent",
                n.text(),
                re.I,
            )
        )
    return json.dumps(
        {
            "text": text,
            "routes": sorted(routes),
            "canonical": safe_route(canonical),
            "legal": legal,
            "footer_text": footer.text(),
            "legal_links": sorted(legal_links),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


PAGES = {
    "activity": "https://dm.dk/students/membership/activity-fund/",
    "benefits": "https://dm.dk/students/membership/membership-benefits/",
    "classes": (
        "https://dm.dk/students/membership/membership-benefits/come-to-"
        "free-classes-and-events/"
    ),
    "competition": "https://dm.dk/konkurrencebetingelser/",
    "cookies": "https://dm.dk/cookies/",
    "danish-benefits": "https://dm.dk/studerende/medlemskab/medlemsfordele/",
    "danish-classes": (
        "https://dm.dk/studerende/medlemskab/medlemsfordele/gratis-kurs"
        "er-og-arrangementer/"
    ),
    "danish-cookie": "https://dm.dk/studerende/cookies/",
    "danish-membership": "https://dm.dk/studerende/medlemskab/",
    "danish-professional": (
        "https://dm.dk/studerende/medlemskab/medlemsfordele/faglige-med"
        "lemsfordele/"
    ),
    "danish-students": "https://dm.dk/studerende/",
    "events": "https://dm.dk/studerende/arrangementer/",
    "financial": (
        "https://dm.dk/students/membership/membership-benefits/financia"
        "l-member-benefits/"
    ),
    "grants-hub": (
        "https://dm.dk/studerende/medlemskab/legater-og-sponsorater/"
    ),
    "home": "https://dm.dk/",
    "mentor-specific": (
        "https://dm.dk/studerende/medlemskab/medlemsfordele/faglige-med"
        "lemsfordele/mentorordning-for-studerende-ved-sling-uling-og-na"
        "ku/"
    ),
    "online-courses": (
        "https://dm.dk/studerende/medlemskab/medlemsfordele/gratis-onli"
        "ne-kurser/"
    ),
    "privacy": "https://dm.dk/privatlivspolitik/",
    "professional": (
        "https://dm.dk/students/membership/membership-benefits/professi"
        "onal-member-benefits/"
    ),
    "reimbursement": "https://dm.dk/om-dm/refusion/",
    "sponsorship-danish": (
        "https://dm.dk/studerende/medlemskab/legater-og-sponsorater/spo"
        "nsoratpulje/"
    ),
    "student-cookie": "https://dm.dk/students/cookie-policy/",
    "students": "https://dm.dk/students/",
    "terms": "https://dm.dk/handelsvilkaar/",
    "travel-danish": (
        "https://dm.dk/studerende/medlemskab/legater-og-sponsorater/rej"
        "selegat/"
    ),
    "travel": "https://dm.dk/students/membership/travel-grant/",
}

CONDITIONS = {
    "activity": (
        "90a3444993922d8b1a2a341a4bd14dab9d8d04b32530c879ac08703fe09fb2" "a3"
    ),
    "benefits": (
        "92a92c246232c0461e85c50cf34e821d2b220bb2fc46f1f01d98decefb0320" "72"
    ),
    "classes": (
        "7f689769964c1249934ff2fb239eb23aad21f85911c93dff1fff9161a12077" "f4"
    ),
    "competition": (
        "227c0075537520f60b7e59276cff555535efd8d6201f47de3717bfc0c3b9b1" "22"
    ),
    "cookies": (
        "d394dd8254d93f54237731a4896de5f40f8e47883b310d3a9abdb204313f0d" "e3"
    ),
    "danish-benefits": (
        "20f11fdf4155d43f4c8a026a71dc2f33f1e8bb25301089b8688caba6b74653" "c4"
    ),
    "danish-classes": (
        "fb655101fb0a203380d7bb7e4e6700b0fa8042cff9f69e36af6234f4342e4e" "1c"
    ),
    "danish-cookie": (
        "68fcda189f06ad16d5d1f96f9dde617fa00e9754d914299bad8ade7ec2e45c" "3a"
    ),
    "danish-membership": (
        "03ebde57dc2105ba5833d6741512dcecd2767343e58e2dfe9c4c298c939ef4" "91"
    ),
    "danish-professional": (
        "e3ff894f4eecc82d38a557e2233fea8afbc24b34de7ae9030d3246415ed84c" "3b"
    ),
    "danish-students": (
        "af4dc5bd086c309bd63686c1110c140d1d20fff476e91857a93a03c5c1c8dd" "c3"
    ),
    "events": (
        "7f8017b80ea1a74b22cac9960e982d8a30433a15b617d3890a68ea3b326afe" "80"
    ),
    "financial": (
        "020f4f0bbc4dfa922c1f99174e55fb02d7d89a3c2938f6e6769360d0630ea1" "93"
    ),
    "grants-hub": (
        "338042bd8780fa190033459c57c2eb062d48901476d35e9d666a39d8ad29d0" "91"
    ),
    "home": (
        "e47d4cd4112b026ebd5ae63e6e40afd45ea679096a6ad14132a1082957d3b0" "55"
    ),
    "mentor-specific": (
        "07419e69394a399e2ffa753284bf78bff0983b4f58b9a715967175e90d6949" "6a"
    ),
    "online-courses": (
        "61e1c450a90cf2304525d026ec017cab39d1264165e39a13101211ffbfe295" "19"
    ),
    "privacy": (
        "74fe162e321ba0e42cdec5ee5e9316991c116b2c2975966b3d1cdb5e61035d" "dc"
    ),
    "professional": (
        "2df00faa52ea2aa82028ff97c62ebe59c65fcde051ed7f9d5f81a9661e5002" "dc"
    ),
    "reimbursement": (
        "e4defcd5bd7827036fbd27606cdb43814f8bc90697286bfa48ee42559b4834" "dd"
    ),
    "sponsorship-danish": (
        "e35c7400fd48fc2dc7434fa5aa0f3eac25d6d897b8985938fcb281d53f8b9c" "2d"
    ),
    "student-cookie": (
        "b38b856a3ba3304e21d14c4ae94e47a1f0a23ec53d26255d8d6eccc11b109a" "48"
    ),
    "students": (
        "bba55e098b24a128d8c8e4df45bea8980bc30efc494c507641b2c51df16238" "7b"
    ),
    "terms": (
        "52cc4207d4bd88f62319f190557b6270206aede83aaad2361f9aa3ad77ad34" "c7"
    ),
    "travel": (
        "2a18383a91ea385b12d9ab50d438fc332b6832b0248e970716c13cbc0d1acf" "e4"
    ),
    "travel-danish": (
        "4fdf6f2d04fde1655b8eea0a885ab055e4a4892673d3ebab5971e883b62515" "4d"
    ),
}

PROFILES = [
    {
        "track": "travel-grant",
        "key": "travel",
        "title": "DM Students Travel Grant — spring 2027 round",
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": [],
        "deadline": "2027-02-01",
        "language": "en",
        "summary": (
            "Up to DKK 8,000 for DM student members undertaking "
            "optional, academically approved bachelor’s or master’s "
            "study trips, placements or fieldwork in Denmark or "
            "abroad. Trips must last no more than six months; "
            "mandatory or already university-funded activities are "
            "excluded. The Danish page dates the next round November "
            "1, 2026–February 1, 2027; its closing clock and time zone "
            "are unspecified."
        ),
        "evidence": (
            "DM student membership and current relevant "
            "bachelor/master enrolment are required; membership is not "
            "a nationality or residence test. Domestic and overseas "
            "travel are supported, without a complete destination "
            "list. Maximum individual award DKK8000; English 2026 "
            "annual fund DKK150000 and twice-yearly DKK75000 "
            "allocations are budgets, not individual entitlements. "
            "Danish about10 recipients per half-year is capacity; the "
            "student board assesses award size. Optional academically "
            "relevant fieldwork/study trips/exchange/stays/internships;"
            " <=6months, completed within1year of award; apply before "
            "travel starts, cancellation returns funding. Mandatory "
            "curriculum, regular teaching replacement and previously "
            "university-funded activities excluded. Each traveller "
            "applies individually; domestic awards taxable and "
            "recipient must notify Skattestyrelsen, no foreign tax "
            "treatment specified. Online application requires <=1A4 "
            "motivation, academic approval/preapproval by "
            "director/lecturer/supervisor, current enrolment proof, "
            "<=1A4 budget explaining costs/financing and other "
            "scholarships applied/received, combined PDFs<=6pages. No "
            "tickets/booking receipts required at application. "
            "Late/incomplete/noncompliant applications excluded. "
            "English possible request for photos/content is optional; "
            "Danish says preparedness to share experiences and "
            "dissemination enthusiasm are considered, and motivation "
            "may explain proposed communication. English recurring "
            "autumn Jun1–Sep1 for majority Sep–Jan travel; spring "
            "Nov1–Feb1 for majority Feb–Aug travel; those dates alone "
            "lack years. English explicitly says next opening "
            "Nov1,2026; Danish explicitly gives next spring round "
            "Nov1,2026–Feb1,2027, with no closing clock/time zone. No "
            "currently-open claim is inferred from a future opening. "
            "Decisions expected roughly1month after cutoff. Danish "
            "corroboration: https://dm.dk/studerende/medlemskab/legater"
            "-og-sponsorater/rejselegat/."
        ),
    },
    {
        "track": "activity-fund",
        "key": "activity",
        "title": ("DM Students Activity Fund — student-led event sponsorship"),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "hosts": [],
        "deadline": None,
        "language": "en",
        "summary": (
            "DM supports student-member-led academic, social and "
            "career events benefiting student groups. The Danish "
            "sponsorship page requires university-enrolled "
            "participants, a detailed budget and application at least "
            "one month before the event. Funding normally reimburses "
            "costs after the event; no standard award limit or dated "
            "closing deadline is published. Alcohol, mandatory "
            "teaching and already university-funded activities are "
            "excluded."
        ),
        "evidence": (
            "Activity Fund and Danish Sponsoratpulje are the same "
            "student funding scheme: Danish form privacy calls it "
            "aktivitetspulje. Responsible applicant must be a DM "
            "member; group/student-association participants benefit, "
            "not a personal travel scholarship. Danish requires all "
            "participants university-enrolled and a detailed budget; "
            "apply at least1month before the event, a relative lead "
            "time not fixed closing date. No geography or citizenship "
            "restriction stated. Supports professional/social/study/lab"
            "our-market talks, debates, networking, company visits, "
            "conferences; English exceptional social events may "
            "support healthy learning environments across cohorts and "
            "instructors. Evaluation: subject/social/career content, "
            "participants and cost ratio, DM involvement/exposure. DM "
            "logo expected in publicity; materials when possible, "
            "consultant participation suggested, photos welcomed if "
            "appropriate. If DM covers entire event, free DM-member "
            "entry and openness to all are preferences, not "
            "unconditional requirements. Alcohol, teaching "
            "replacement, compulsory and previously university-funded "
            "activities excluded. English additionally excludes "
            "parties/Friday bars; Danish gives "
            "social/revue/running-club examples without the same "
            "blanket wording, a bilingual scope difference retained. "
            "Normally disbursed after event; requested expenses, "
            "responsible contact, group name, "
            "participants/fees/catering, total budget, amount, "
            "purpose/exposure and other funding specified. No common "
            "maximum amount; Danish DKK7500 is a recipient "
            "testimonial, not a grant rate. Linked public "
            "reimbursement guidance requires expense receipts and "
            "permits student associations without CVR to use an "
            "expense form; those with CVR may invoice, no mandatory "
            "CVR requirement inferred. No form/account operation "
            "required here. Danish corroboration: "
            "https://dm.dk/studerende/medlemskab/legater-og-sponsorater"
            "/sponsoratpulje/; reimbursement: "
            "https://dm.dk/om-dm/refusion/."
        ),
    },
    {
        "track": "free-member-classes",
        "key": "classes",
        "title": ("DM and MA — free classes and events for student members"),
        "categories": ["training"],
        "kind": "programme-overview",
        "hosts": ["DK"],
        "deadline": None,
        "language": "en",
        "summary": (
            "Students who are members of DM and MA can attend free "
            "skills classes and events in Aalborg, Aarhus, Odense and "
            "Copenhagen. Examples include InDesign, Excel, LaTeX, R, "
            "Stata and Danish punctuation. This member entitlement "
            "describes training access; individual course dates, "
            "registration availability and application deadlines are "
            "not specified on the programme page."
        ),
        "evidence": (
            "Own English and Danish pages explicitly offer free "
            "classes/events to students and members of DM and MA. "
            "Examples are InDesign, Excel, LaTeX, R, Stata and Danish "
            "punctuation, with study/work and job-search relevance. "
            "Published cities Aalborg, Aarhus, Odense and Copenhagen "
            "support known Danish hosting, not applicant nationality. "
            "No cash award, stipend, certificate guarantee or "
            "course-specific deadline is stated. The public "
            "current-event calendar is JavaScript-rendered and the "
            "reviewed HTML supplies no individual participant "
            "programme terms; this record describes the member "
            "entitlement, not every current event. Danish "
            "corroboration: https://dm.dk/studerende/medlemskab/medlems"
            "fordele/gratis-kurser-og-arrangementer/."
        ),
    },
    {
        "track": "free-online-courses",
        "key": "online-courses",
        "title": "DM — free online-course access for members",
        "categories": ["training"],
        "kind": "programme-overview",
        "hosts": [],
        "deadline": None,
        "language": "da",
        "summary": (
            "DM members receive free access to a published online "
            "skills catalogue via onlinekurser.dk. Topics include "
            "graphics, office software, job search, IT security, "
            "economics and project management. The introductory "
            "wording mentions DM and MA, while the specific access "
            "paragraph names DM membership. A provider profile is "
            "needed on first use; no cash award or closing deadline is "
            "stated."
        ),
        "evidence": (
            "Own page introductory wording says DM and MA membership; "
            "the explicit access entitlement says DM members have free "
            "access to the listed onlinekurser.dk courses. No "
            "mandatory dual-MA condition is inferred from that wording "
            "difference. Published subjects: remote work/Teams/Zoom, "
            "CV/interviews/job search/social media, information search "
            "and negotiation, workplace representation/APV/conflict/str"
            "ess/strategy/voluntary board work, Google tools, "
            "graphics/video/Adobe/Canva/Forms/Sway/AI, communication, "
            "IT security/GDPR/economics, leadership/project "
            "management/Agile-Scrum, Project Libre/Trello/Microsoft "
            "Project, Office/SharePoint/Windows and punctuation. First "
            "provider visit requires a profile used thereafter; no "
            "external catalogue or account operation is needed to "
            "establish this own member entitlement. No "
            "programme-specific duration, certificate guarantee, "
            "stipend or deadline stated. Online delivery has no "
            "published physical host restriction or applicant "
            "citizenship list. Membership fees and consumer discounts "
            "are distinct from the waived course access charge."
        ),
    },
    {
        "track": "sling-uling-naku-mentoring",
        "key": "mentor-specific",
        "title": "DM mentoring for SLING, ULING and NAKU students",
        "categories": ["training"],
        "kind": "programme-overview",
        "hosts": [],
        "deadline": None,
        "language": "da",
        "summary": (
            "DM members studying forest and landscape engineering, "
            "urban landscape engineering, or nature and culture "
            "communication can request a study-to-career mentor. "
            "Meetings support study plans, workplace insight, skills "
            "and job search. A typical arrangement lasts about a year "
            "with four to seven meetings, adapted by agreement. "
            "Matches are not guaranteed; no price, stipend or "
            "application deadline is published."
        ),
        "evidence": (
            "Students in forest/landscape engineering (SLING), urban "
            "landscape engineering (ULING; formerly garden/park "
            "engineering) or nature/culture communication (NAKU) AND "
            "DM membership are eligible. No "
            "nationality/residence/geographic restriction is stated. "
            "Individual mentoring supports study/career plans, insight "
            "into a company/work role, expressing skills, "
            "CV/applications/job search, broader options, networking "
            "and transition to employment. A student may already agree "
            "a mentor with an employer, select/contact one, or ask the "
            "secretariat for help finding a relevant mentor. Notify "
            "the secretariat of an agreed arrangement/start. Explain "
            "motivation for the workplace and planned use; matching is "
            "not guaranteed because applicants may exceed available "
            "mentors. Agree expectations and individual form/duration; "
            "a year is encouraged, typically4–7meetings/year "
            "lasting1–1.5h, usually at the company, with phone/email "
            "possible. These are typical arrangements, not mandatory "
            "fixed attendance quotas. No price/free-fee promise, "
            "stipend or fixed closing date is published. No "
            "last-half-year restriction is stated for this specific "
            "programme."
        ),
    },
]


def read_pages():
    return {key: page(url) for key, url in PAGES.items()}


def parse_inventory(pages):
    if set(pages) != set(PAGES):
        raise AdapterError("Incomplete DM source evidence", "parse")
    for key in PAGES:
        actual = hashlib.sha256(
            source_facts(key, pages[key]).encode()
        ).hexdigest()
        if actual != CONDITIONS[key]:
            raise AdapterError("DM facts/frontier changed: " + key, "parse")
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
        record["deadline"] = profile["deadline"]
        record["language"] = profile["language"]
        # A date-only cutoff does not establish any time zone or clock.
        # A future opening notice is not a presently open application claim.
        if record["deadline"] and utc_now()[:10] > record["deadline"]:
            record["status"] = "expired"
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
        tempfile.gettempdir(), SOURCE_ID + "-pacing-" + str(os.getuid())
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
    publisher = host
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
                f"Collected {len(records)} validated DM student "
                "funding and member training programmes"
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
