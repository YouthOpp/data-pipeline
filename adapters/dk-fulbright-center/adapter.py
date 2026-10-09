"""Collect reviewed Fulbright Denmark funding and educational programmes."""

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
import xml.etree.ElementTree as ET

SOURCE_ID = "dk-fulbright-center"
SOURCE_URL = (
    "https://fulbrightcenter.dk/go-to-the-us/"
    "fulbright-grants-for-danish-students/apply/"
)
WEBSITE_URL = "https://fulbrightcenter.dk/"
LANGUAGE = "en"
PUBLISHER_COUNTRY = "DK"
PUBLISHER_TYPE = "government"
ATTRIBUTION = "Fulbright Denmark — Danish-American Fulbright Commission"


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


def article(root):
    return one(nodes(root, "article"), "complete publisher article")


def personal_sections(key, root):
    ids = {"crown": ["row-unique-4"], "book-scholar": ["row-unique-2"]}.get(
        key, []
    )
    result = [
        one(
            [
                n
                for n in nodes(article(root), "div")
                if n.attrs.get("id") == identity
            ],
            "reviewed personal presentation section",
        )
        for identity in ids
    ]
    if key == "arctic":
        container = one(
            [
                n
                for n in nodes(article(root), "div")
                if n.attrs.get("id") == "row-unique-1"
            ],
            "Arctic own discovery section",
        )
        rows = nodes(container, "div", "row-internal")
        if len(rows) != 4 or any(len(nodes(n, "h4")) != 1 for n in rows[1:4]):
            raise AdapterError("Arctic biography structure changed", "parse")
        result.extend(rows[1:4])
    return result


def recommendations(key, root):
    identity = RECOMMENDATIONS.get(key)
    if identity is None:
        return []
    container = one(
        [n for n in nodes(root, "div") if n.attrs.get("id") == identity],
        "reviewed recommendation container",
    )
    if (
        container.attrs.get("data-parent") != "true"
        or "style-color-gyho-bg" not in container.attrs.get("class", "").split()
        or not any(n.text() == "Related grants" for n in nodes(container, "h2"))
    ):
        raise AdapterError("Recommendation structure changed", "parse")
    return [container]


def text_without(node, excluded):
    if node in excluded or node.tag in ("script", "style", "noscript"):
        return ""
    return " ".join(
        " ".join(
            text_without(c, excluded) if isinstance(c, Node) else c
            for c in node.children
        ).split()
    )


def substantive_text(key, root):
    content = article(root)
    return (
        text_without(
            content, personal_sections(key, root) + recommendations(key, root)
        )
        .replace("\xad", "")
        .replace("\u200b", "")
    )


def reviewed_url(value):
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower()
        not in ("fulbrightcenter.dk", "www.fulbrightcenter.dk")
        or parsed.username
        or parsed.password
        or parsed.port
        or parsed.query
    ):
        raise AdapterError("Unexpected publisher URL", "parse")
    return urllib.parse.urlunsplit(
        ("https", "fulbrightcenter.dk", parsed.path or "/", "", "")
    )


def source_facts(key, root):
    canonical = [
        n.attrs.get("href", "")
        for n in nodes(root, "link")
        if n.attrs.get("rel") == "canonical"
    ]
    if canonical != CANONICALS[key]:
        raise AdapterError("Publisher canonical changed: " + key, "parse")
    routes, policies, materials = set(), set(), set()
    excluded = personal_sections(key, root)
    personal_anchors = {a for section in excluded for a in nodes(section, "a")}
    card_nodes = {
        n for section in recommendations(key, root) for n in section.walk()
    }
    restriction_notices = [
        n.text()
        for n in card_nodes
        if n.tag in ("p", "small", "span")
        and re.search(
            r"permission|copyright|rights reserved|licen[cs]e|"
            r"re.?publi|reuse|reproduction",
            n.text(),
            re.I,
        )
    ]
    if restriction_notices:
        raise AdapterError("New recommendation legal conditions", "parse")
    for anchor in nodes(root, "a"):
        if anchor in personal_anchors:
            continue
        href = anchor.attrs.get("href", "")
        if not href or href.startswith(("mailto:", "tel:")):
            continue
        absolute = urllib.parse.urljoin(PAGES[key], href)
        if key == "frontier-7" and absolute == "http://www.fulbrightcenter.dk/":
            # The actual legacy guide links this verified homepage alias.
            # No HTTP endpoint is fetched; current HTTPS home is reviewed.
            absolute = WEBSITE_URL
        parsed = urllib.parse.urlsplit(absolute)
        if anchor in card_nodes:
            if re.search(
                r"permission|re.?publi|reuse|reproduction|rights reserved",
                anchor.text(),
                re.I,
            ):
                raise AdapterError(
                    "New recommendation legal restriction", "parse"
                )
            is_policy = re.search(
                r"privacy|terms|copyright|licen[cs]e|data protection|"
                r"processing of personal",
                anchor.text(),
                re.I,
            )
            if is_policy and absolute not in REVIEWED_CARD_POLICIES:
                raise AdapterError(
                    "New recommendation policy: "
                    + key
                    + " "
                    + parsed.path[:200],
                    "parse",
                )
            if (
                parsed.path.lower().endswith(".pdf")
                and absolute not in REVIEWED_CARD_PDFS
            ):
                raise AdapterError(
                    "New recommendation PDF: " + key + " " + parsed.path[:200],
                    "parse",
                )
            if (parsed.hostname or "").lower() in (
                "fulbrightcenter.dk",
                "www.fulbrightcenter.dk",
            ) and not parsed.path.startswith(("/wp-content/", "/wp-includes/")):
                if reviewed_url(absolute) not in REVIEWED_CARD_ROUTES:
                    raise AdapterError(
                        "New recommendation programme route: "
                        + key
                        + " "
                        + parsed.path[:200],
                        "parse",
                    )
            continue
        if re.search(
            r"privacy|terms|copyright|licen[cs]e|data protection|"
            r"processing of personal",
            anchor.text(),
            re.I,
        ):
            policies.add(absolute)
        if (parsed.hostname or "").lower() in (
            "fulbrightcenter.dk",
            "www.fulbrightcenter.dk",
        ):
            if parsed.path.startswith(("/wp-content/", "/wp-includes/")):
                if parsed.path.lower().endswith(".pdf"):
                    materials.add(absolute)
                continue
            routes.add(reviewed_url(absolute))
    footer = one(
        [n for n in nodes(root, "footer") if n.attrs.get("id") == "colophon"],
        "publisher legal footer",
    )
    notices = [
        n.text()
        for n in nodes(footer)
        if n.tag in ("p", "small", "span")
        and re.search(
            r"©|copyright|permission|rights reserved|licen[cs]e|"
            r"re.?publi|reuse|reproduction",
            n.text(),
            re.I,
        )
    ]
    if not notices and key not in NO_LEGAL_NOTICE:
        raise AdapterError("Missing publisher legal notice", "parse")
    notices = [re.sub(r"©\s*\d{4}", "© CURRENT_YEAR", t) for t in notices]
    return json.dumps(
        {
            "text": substantive_text(key, root),
            "routes": sorted(routes),
            "policies": sorted(policies),
            "materials": sorted(materials),
            "legal": notices,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def read_public_pdf(url, expected):
    delay = check_robots(url)
    current = url
    for hop in range(4):
        reviewed_url(current)
        delay = max(delay, check_robots(current))
        status, headers, raw = request_bytes(current, interval=delay)
        if status in (301, 302, 303, 307, 308):
            if hop == 3 or not headers.get("Location"):
                raise AdapterError("Invalid public PDF redirect", "fetch")
            current = urllib.parse.urljoin(current, headers["Location"])
            continue
        if status != 200:
            raise AdapterError(
                "Publisher PDF returned HTTP " + str(status), "fetch", status
            )
        if (
            not raw.startswith(b"%PDF-")
            or not raw.rstrip().endswith(b"%%EOF")
            or hashlib.sha256(raw).hexdigest() != expected
        ):
            raise AdapterError(
                "Reviewed public PDF changed or incomplete", "parse"
            )
        return expected
    raise AdapterError("Unresolved public PDF", "fetch")


RECOMMENDATIONS = {
    "all-disciplines": "row-unique-2",
    "community-college": "row-unique-2",
    "selection": "row-unique-2",
    "thanks": "row-unique-2",
    "gap-6": "row-unique-2",
}

NO_LEGAL_NOTICE = ["selection", "gap-6"]

REVIEWED_CARD_ROUTES = [
    "https://fulbrightcenter.dk/",
    "https://fulbrightcenter.dk/about-fulbright-program/",
    (
        "https://fulbrightcenter.dk/about-fulbright-program/alumni-impa"
        "ct-study/"
    ),
    ("https://fulbrightcenter.dk/about-fulbright-program/board-membe" "rs/"),
    ("https://fulbrightcenter.dk/about-fulbright-program/current-sta" "tus/"),
    ("https://fulbrightcenter.dk/about-fulbright-program/data-protec" "tion/"),
    ("https://fulbrightcenter.dk/about-fulbright-program/partners/"),
    "https://fulbrightcenter.dk/about/",
    "https://fulbrightcenter.dk/advising/",
    ("https://fulbrightcenter.dk/advising/advising-postdocs-scholars" "/"),
    "https://fulbrightcenter.dk/advising/broad-opportunities/",
    ("https://fulbrightcenter.dk/advising/cost-of-living-in-denmark/"),
    "https://fulbrightcenter.dk/advising/financing/",
    ("https://fulbrightcenter.dk/advising/fulbright-is-for-everyone/"),
    "https://fulbrightcenter.dk/advising/graduate-advising/",
    (
        "https://fulbrightcenter.dk/advising/graduate-advising/to-do-li"
        "st-for-master-and-phd-studies-in-usa/"
    ),
    "https://fulbrightcenter.dk/advising/housing-in-the-us/",
    ("https://fulbrightcenter.dk/advising/how-do-we-choose-our-candi" "dates/"),
    ("https://fulbrightcenter.dk/advising/training-internships-in-us" "a/"),
    ("https://fulbrightcenter.dk/advising/travelling-with-family/"),
    "https://fulbrightcenter.dk/advising/u-s-grading-systems/",
    "https://fulbrightcenter.dk/advising/u-s-tests/",
    "https://fulbrightcenter.dk/alumni/",
    "https://fulbrightcenter.dk/book-en-fulbright-forsker/",
    "https://fulbrightcenter.dk/contact/",
    "https://fulbrightcenter.dk/data-protection/",
    "https://fulbrightcenter.dk/go-to-the-us/",
    (
        "https://fulbrightcenter.dk/go-to-the-us/crown-prince-frederik-"
        "fund-2/"
    ),
    (
        "https://fulbrightcenter.dk/go-to-the-us/crown-prince-frederik-"
        "fund-2/alumni/"
    ),
    (
        "https://fulbrightcenter.dk/go-to-the-us/fulbright-arctic-initi"
        "ative-2024-2026/"
    ),
    ("https://fulbrightcenter.dk/go-to-the-us/fulbright-co-funded/"),
    (
        "https://fulbrightcenter.dk/go-to-the-us/fulbright-denmark-inge"
        "r-p-davis-grant-in-social-sciences/"
    ),
    (
        "https://fulbrightcenter.dk/go-to-the-us/fulbright-grants-for-d"
        "anish-students/"
    ),
    (
        "https://fulbrightcenter.dk/go-to-the-us/fulbright-grants-for-d"
        "anish-students/apply"
    ),
    (
        "https://fulbrightcenter.dk/go-to-the-us/fulbright-grants-for-d"
        "anish-students/apply/"
    ),
    "https://fulbrightcenter.dk/go-to-the-us/fulbright-schuman/",
    ("https://fulbrightcenter.dk/go-to-the-us/grants-for-scholars/"),
    ("https://fulbrightcenter.dk/go-to-the-us/grants-for-scholars/ap" "ply"),
    ("https://fulbrightcenter.dk/go-to-the-us/grants-for-scholars/ap" "ply/"),
    "https://fulbrightcenter.dk/go-to-the-us/other-grants/",
    "https://fulbrightcenter.dk/go-to-the-us/students/",
    (
        "https://fulbrightcenter.dk/go-to-the-us/students/fulbright-joi"
        "nt-grants/"
    ),
    ("https://fulbrightcenter.dk/go-to-the-us/thanks-to-scandinavia/"),
    (
        "https://fulbrightcenter.dk/grants-americans/fulbright-for-scho"
        "lars/fulbright-schuman/"
    ),
    "https://fulbrightcenter.dk/grantsforamericans/",
    (
        "https://fulbrightcenter.dk/grantsforamericans/fulbright-for-sc"
        "holars/"
    ),
    (
        "https://fulbrightcenter.dk/grantsforamericans/fulbright-for-sc"
        "holars/fulbright-distinguished-chair-in-american-studies/"
    ),
    (
        "https://fulbrightcenter.dk/grantsforamericans/fulbright-for-sc"
        "holars/fulbright-schuman/"
    ),
    (
        "https://fulbrightcenter.dk/grantsforamericans/fulbright-for-sc"
        "holars/scholar-grants-all-disciplines/"
    ),
    (
        "https://fulbrightcenter.dk/grantsforamericans/fulbright-for-sc"
        "holars/scholar-grants-community-college/"
    ),
    (
        "https://fulbrightcenter.dk/grantsforamericans/fulbright-for-st"
        "udents/"
    ),
    (
        "https://fulbrightcenter.dk/grantsforamericans/fulbright-for-st"
        "udents/current-dk-fulbrighters/"
    ),
    (
        "https://fulbrightcenter.dk/grantsforamericans/fulbright-for-st"
        "udents/current-us-fulbrighters/"
    ),
    (
        "https://fulbrightcenter.dk/grantsforamericans/fulbright-inter-"
        "country-travel-grant/"
    ),
    (
        "https://fulbrightcenter.dk/grantsforamericans/fulbright-inter-"
        "country-travel-grant/apply/"
    ),
    (
        "https://fulbrightcenter.dk/grantsforamericans/fulbright-specia"
        "list-grant-program/"
    ),
    "https://fulbrightcenter.dk/grantsfordanes/",
    ("https://fulbrightcenter.dk/grantsfordanes/fulbright-co-funded/"),
    ("https://fulbrightcenter.dk/grantsfordanes/fulbright-for-schola" "rs/"),
    ("https://fulbrightcenter.dk/grantsfordanes/fulbright-for-studen" "ts/"),
    (
        "https://fulbrightcenter.dk/grantsfordanes/fulbright-grants-for"
        "-danish-students/"
    ),
    (
        "https://fulbrightcenter.dk/grantsfordanes/students/fulbright-f"
        "or-students/"
    ),
    (
        "https://fulbrightcenter.dk/grantsfordanes/students/fulbright-j"
        "oint-grants/"
    ),
    "https://fulbrightcenter.dk/hosts/",
    "https://fulbrightcenter.dk/partners/",
    "https://fulbrightcenter.dk/returning-home/",
    "https://fulbrightcenter.dk/scholars/",
    "https://fulbrightcenter.dk/scholars/arctic-initiative-iii/",
    ("https://fulbrightcenter.dk/scholars/arctic-initiative-iii/appl" "y/"),
    "https://fulbrightcenter.dk/shop/",
    "https://fulbrightcenter.dk/stories/",
    (
        "https://fulbrightcenter.dk/stories/2021/the-life-of-a-fulbrigh"
        "t-specialist-in-denmark/"
    ),
    (
        "https://fulbrightcenter.dk/stories/2022/bliv-vaert-for-en-amer"
        "ikansk-fulbright-specialist-2/"
    ),
    (
        "https://fulbrightcenter.dk/stories/2024/being-a-fulbright-spec"
        "ialist-in-denmark-2/"
    ),
    ("https://fulbrightcenter.dk/stories/2025/webinar-om-legatansogn" "ing/"),
    (
        "https://fulbrightcenter.dk/stories/2026/arctic-dreams-danish-w"
        "ork-life-balance/"
    ),
    (
        "https://fulbrightcenter.dk/stories/2026/can-do-attitude-skaber"
        "-resultater-mit/"
    ),
    (
        "https://fulbrightcenter.dk/stories/2026/fulbright-scholar-univ"
        "ersity-college-copenhagen/"
    ),
    (
        "https://fulbrightcenter.dk/stories/2026/my-danish-fulbright-ex"
        "perience-love-story/"
    ),
    "https://fulbrightcenter.dk/stories/page_category/advising/",
    ("https://fulbrightcenter.dk/stories/page_category/for-dk-citize" "ns/"),
    "https://fulbrightcenter.dk/stories/page_category/general/",
    (
        "https://fulbrightcenter.dk/stories/page_category/grants-for-am"
        "erican-citizens/"
    ),
    (
        "https://fulbrightcenter.dk/stories/page_category/grants-for-sc"
        "holars-us-citizenship/"
    ),
    (
        "https://fulbrightcenter.dk/stories/page_category/other-grants-"
        "for-danish-citizens/"
    ),
    "https://fulbrightcenter.dk/usa-legater/",
    "https://fulbrightcenter.dk/usa-vejledning/",
    ("https://fulbrightcenter.dk/webinar-for-legatsogning-16-novembe" "r/"),
    (
        "https://fulbrightcenter.dk/webinar-om-legatsogning-d-9-novembe"
        "r-2022/"
    ),
    (
        "https://fulbrightcenter.dk/webinar-om-legatsogning-tirsdag-d-2"
        "4-januar-2023/"
    ),
]

REVIEWED_CARD_POLICIES = [
    "https://fulbrightcenter.dk/about-fulbright-program/",
    ("https://fulbrightcenter.dk/about-fulbright-program/data-protec" "tion/"),
    "https://fulbrightcenter.dk/data-protection/",
    (
        "https://fulbrightcenter.dk/wp-content/uploads/2025/05/Data-Pro"
        "tection-Notice_Consent-Form_March-2025.pdf"
    ),
    "https://www.privacyshield.gov/welcome",
]

REVIEWED_CARD_PDFS = [
    (
        "https://fulbrightcenter.dk/wp-content/uploads/2020/02/Instruct"
        "ions-Arctic-Initiative-III.pdf"
    ),
    (
        "https://fulbrightcenter.dk/wp-content/uploads/2023/11/FFSP_Dis"
        "ability-Accommodations_Candidates.pdf"
    ),
    (
        "https://fulbrightcenter.dk/wp-content/uploads/2024/02/Workshee"
        "t-Returning-Home.pdf"
    ),
    (
        "https://fulbrightcenter.dk/wp-content/uploads/2024/10/Named-Pr"
        "ojects-PDF.pdf"
    ),
    (
        "https://fulbrightcenter.dk/wp-content/uploads/2024/10/Open-Pro"
        "jects-PDF.pdf"
    ),
    (
        "https://fulbrightcenter.dk/wp-content/uploads/2025/05/Data-Pro"
        "tection-Notice_Consent-Form_March-2025.pdf"
    ),
    (
        "https://fulbrightcenter.dk/wp-content/uploads/2025/10/Project-"
        "Highlights-Arts.pdf"
    ),
    (
        "https://fulbrightcenter.dk/wp-content/uploads/2025/10/Project-"
        "Highlights-Europe-Eurasia.pdf"
    ),
    (
        "https://fulbrightcenter.dk/wp-content/uploads/2025/10/Project-"
        "Highlights-Non-Academic-Hosts.pdf"
    ),
    (
        "https://fulbrightcenter.dk/wp-content/uploads/2025/10/Project-"
        "Highlights-U.S.-Community-Colleges.pdf"
    ),
    (
        "https://fulbrightcenter.dk/wp-content/uploads/2026/02/Final_Fr"
        "equently-Asked-Questions_Danes-going-to-the-US.pdf"
    ),
    (
        "https://fulbrightcenter.dk/wp-content/uploads/2026/03/Inter-Co"
        "untry-Travel-Grant-Application-Form-Fulbright-Denmark.pdf"
    ),
    (
        "https://fulbrightcenter.dk/wp-content/uploads/2026/07/CPFF-202"
        "7-2028-Ansogningsskema.pdf"
    ),
    (
        "https://fulbrightcenter.dk/wp-content/uploads/2026/08/Applican"
        "t-Commitments-2027-2028-DK-Students-1.pdf"
    ),
    (
        "https://fulbrightcenter.dk/wp-content/uploads/2026/08/Book-en-"
        "Fulbright-forsker.pdf"
    ),
    (
        "https://fulbrightcenter.dk/wp-content/uploads/2026/08/Budget-F"
        "orm-2027-2028-DK-Scholars-1.pdf"
    ),
    (
        "https://fulbrightcenter.dk/wp-content/uploads/2026/08/Budget-F"
        "orm-2027-2028-DK-Students.pdf"
    ),
    (
        "https://fulbrightcenter.dk/wp-content/uploads/2026/08/Instruct"
        "ions-for-applicants-Foreign-Students-2027-2028-DENMARK.pdf"
    ),
    (
        "https://fulbrightcenter.dk/wp-content/uploads/2026/08/Instruct"
        "ions-for-applicants-Visiting-Scholars-2027-2028-DENMARK.pdf"
    ),
    (
        "https://ufm.dk/en/publications/2022/files/the-danish-education"
        "-system.pdf"
    ),
]

PAGES = {
    "about": "https://fulbrightcenter.dk/about-fulbright-program/",
    "all-disciplines": (
        "https://fulbrightcenter.dk/grantsforamericans/fulbright-for-sc"
        "holars/scholar-grants-all-disciplines/"
    ),
    "arctic": (
        "https://fulbrightcenter.dk/go-to-the-us/fulbright-arctic-initi"
        "ative-2024-2026/"
    ),
    "arts": ("https://fulbrightcenter.dk/go-to-the-us/fulbright-co-funded/"),
    "book-scholar": "https://fulbrightcenter.dk/book-en-fulbright-forsker/",
    "community-college": (
        "https://fulbrightcenter.dk/grantsforamericans/fulbright-for-sc"
        "holars/scholar-grants-community-college/"
    ),
    "crown": (
        "https://fulbrightcenter.dk/go-to-the-us/crown-prince-frederik-"
        "fund-2/"
    ),
    "current-status": (
        "https://fulbrightcenter.dk/about-fulbright-program/current-sta" "tus/"
    ),
    "distinguished": (
        "https://fulbrightcenter.dk/grantsforamericans/fulbright-for-sc"
        "holars/fulbright-distinguished-chair-in-american-studies/"
    ),
    "entry": (
        "https://fulbrightcenter.dk/go-to-the-us/fulbright-grants-for-d"
        "anish-students/apply/"
    ),
    "home": "https://fulbrightcenter.dk/",
    "hosts": "https://fulbrightcenter.dk/hosts/",
    "incoming": "https://fulbrightcenter.dk/grantsforamericans/",
    "intercountry-apply": (
        "https://fulbrightcenter.dk/grantsforamericans/fulbright-inter-"
        "country-travel-grant/apply/"
    ),
    "intercountry": (
        "https://fulbrightcenter.dk/grantsforamericans/fulbright-inter-"
        "country-travel-grant/"
    ),
    "joint": (
        "https://fulbrightcenter.dk/go-to-the-us/students/fulbright-joi"
        "nt-grants/"
    ),
    "other": "https://fulbrightcenter.dk/go-to-the-us/other-grants/",
    "outgoing-alias": "https://fulbrightcenter.dk/go-to-the-us/",
    "outgoing": "https://fulbrightcenter.dk/go-to-the-us/",
    "privacy": (
        "https://fulbrightcenter.dk/about-fulbright-program/data-protec" "tion/"
    ),
    "programme": (
        "https://fulbrightcenter.dk/go-to-the-us/fulbright-grants-for-d"
        "anish-students/"
    ),
    "scholar-apply": (
        "https://fulbrightcenter.dk/go-to-the-us/grants-for-scholars/ap" "ply/"
    ),
    "scholar-dk": (
        "https://fulbrightcenter.dk/go-to-the-us/grants-for-scholars/"
    ),
    "scholar-us": (
        "https://fulbrightcenter.dk/grantsforamericans/fulbright-for-sc"
        "holars/"
    ),
    "schuman": "https://fulbrightcenter.dk/go-to-the-us/fulbright-schuman/",
    "selection": (
        "https://fulbrightcenter.dk/advising/how-do-we-choose-our-candi"
        "dates/"
    ),
    "specialist": (
        "https://fulbrightcenter.dk/grantsforamericans/fulbright-specia"
        "list-grant-program/"
    ),
    "student-us": (
        "https://fulbrightcenter.dk/grantsforamericans/fulbright-for-st"
        "udents/"
    ),
    "thanks": (
        "https://fulbrightcenter.dk/go-to-the-us/thanks-to-scandinavia/"
    ),
    "frontier-1": (
        "https://fulbrightcenter.dk/go-to-the-us/fulbright-denmark-inge"
        "r-p-davis-grant-in-social-sciences/"
    ),
    "frontier-2": "https://fulbrightcenter.dk/scholars/",
    "frontier-3": "https://fulbrightcenter.dk/go-to-the-us/students/",
    "frontier-4": (
        "https://fulbrightcenter.dk/grantsforamericans/fulbright-for-sc"
        "holars/fulbright-schuman/"
    ),
    "frontier-5": "https://fulbrightcenter.dk/data-protection/",
    "frontier-6": "https://fulbrightcenter.dk/returning-home/",
    "frontier-7": (
        "https://fulbrightcenter.dk/advising/graduate-advising/to-do-li"
        "st-for-master-and-phd-studies-in-usa/"
    ),
    "frontier-8": (
        "https://fulbrightcenter.dk/webinar-om-legatsogning-tirsdag-d-2"
        "4-januar-2023/"
    ),
    "frontier-9": (
        "https://fulbrightcenter.dk/webinar-om-legatsogning-d-9-novembe"
        "r-2022/"
    ),
    "frontier-10": (
        "https://fulbrightcenter.dk/webinar-for-legatsogning-16-novembe" "r/"
    ),
    "frontier-11": "https://fulbrightcenter.dk/advising/housing-in-the-us/",
    "frontier-12": (
        "https://fulbrightcenter.dk/advising/cost-of-living-in-denmark/"
    ),
    "frontier-13": "https://fulbrightcenter.dk/usa-legater/",
    "frontier-14": "https://fulbrightcenter.dk/usa-vejledning/",
    "gap-1": "https://fulbrightcenter.dk/advising/financing/",
    "gap-2": "https://fulbrightcenter.dk/alumni/",
    "gap-3": (
        "https://fulbrightcenter.dk/advising/training-internships-in-us" "a/"
    ),
    "gap-4": (
        "https://fulbrightcenter.dk/advising/advising-postdocs-scholars" "/"
    ),
    "gap-5": "https://fulbrightcenter.dk/advising/graduate-advising/",
    "gap-6": "https://fulbrightcenter.dk/advising/broad-opportunities/",
    "gap-7": "https://fulbrightcenter.dk/advising/",
    "gap-8": "https://fulbrightcenter.dk/advising/u-s-tests/",
    "gap-9": "https://fulbrightcenter.dk/advising/u-s-grading-systems/",
    "gap-10": ("https://fulbrightcenter.dk/advising/travelling-with-family/"),
    "arctic-iii": "https://fulbrightcenter.dk/scholars/arctic-initiative-iii/",
    "arctic-iii-apply": (
        "https://fulbrightcenter.dk/scholars/arctic-initiative-iii/appl" "y/"
    ),
}

CANONICALS = {
    "about": ["https://fulbrightcenter.dk/about-fulbright-program/"],
    "all-disciplines": [
        (
            "https://fulbrightcenter.dk/grantsforamericans/fulbright-fo"
            "r-scholars/scholar-grants-all-disciplines/"
        ),
    ],
    "arctic": [
        (
            "https://fulbrightcenter.dk/go-to-the-us/fulbright-arctic-i"
            "nitiative-2024-2026/"
        ),
    ],
    "arts": ["https://fulbrightcenter.dk/go-to-the-us/fulbright-co-funded/"],
    "book-scholar": ["https://fulbrightcenter.dk/book-en-fulbright-forsker/"],
    "community-college": [
        (
            "https://fulbrightcenter.dk/grantsforamericans/fulbright-fo"
            "r-scholars/scholar-grants-community-college/"
        ),
    ],
    "crown": [
        (
            "https://fulbrightcenter.dk/go-to-the-us/crown-prince-frede"
            "rik-fund-2/"
        ),
    ],
    "current-status": [
        (
            "https://fulbrightcenter.dk/about-fulbright-program/current"
            "-status/"
        ),
    ],
    "distinguished": [
        (
            "https://fulbrightcenter.dk/grantsforamericans/fulbright-fo"
            "r-scholars/fulbright-distinguished-chair-in-american-studi"
            "es/"
        ),
    ],
    "entry": [
        (
            "https://fulbrightcenter.dk/go-to-the-us/fulbright-grants-f"
            "or-danish-students/apply/"
        ),
    ],
    "home": ["https://fulbrightcenter.dk/"],
    "hosts": ["https://fulbrightcenter.dk/hosts/"],
    "incoming": ["https://fulbrightcenter.dk/grantsforamericans/"],
    "intercountry-apply": [
        (
            "https://fulbrightcenter.dk/grantsforamericans/fulbright-in"
            "ter-country-travel-grant/apply/"
        ),
    ],
    "intercountry": [
        (
            "https://fulbrightcenter.dk/grantsforamericans/fulbright-in"
            "ter-country-travel-grant/"
        ),
    ],
    "joint": [
        (
            "https://fulbrightcenter.dk/go-to-the-us/students/fulbright"
            "-joint-grants/"
        ),
    ],
    "other": ["https://fulbrightcenter.dk/go-to-the-us/other-grants/"],
    "outgoing-alias": ["https://fulbrightcenter.dk/go-to-the-us/"],
    "outgoing": ["https://fulbrightcenter.dk/go-to-the-us/"],
    "privacy": [
        (
            "https://fulbrightcenter.dk/about-fulbright-program/data-pr"
            "otection/"
        ),
    ],
    "programme": [
        (
            "https://fulbrightcenter.dk/go-to-the-us/fulbright-grants-f"
            "or-danish-students/"
        ),
    ],
    "scholar-apply": [
        (
            "https://fulbrightcenter.dk/go-to-the-us/grants-for-scholar"
            "s/apply/"
        ),
    ],
    "scholar-dk": [
        "https://fulbrightcenter.dk/go-to-the-us/grants-for-scholars/"
    ],
    "scholar-us": [
        (
            "https://fulbrightcenter.dk/grantsforamericans/fulbright-fo"
            "r-scholars/"
        ),
    ],
    "schuman": ["https://fulbrightcenter.dk/go-to-the-us/fulbright-schuman/"],
    "selection": [
        (
            "https://fulbrightcenter.dk/advising/how-do-we-choose-our-c"
            "andidates/"
        ),
    ],
    "specialist": [
        (
            "https://fulbrightcenter.dk/grantsforamericans/fulbright-sp"
            "ecialist-grant-program/"
        ),
    ],
    "student-us": [
        (
            "https://fulbrightcenter.dk/grantsforamericans/fulbright-fo"
            "r-students/"
        ),
    ],
    "thanks": [
        ("https://fulbrightcenter.dk/go-to-the-us/thanks-to-scandina" "via/"),
    ],
    "frontier-1": [
        (
            "https://fulbrightcenter.dk/go-to-the-us/fulbright-denmark-"
            "inger-p-davis-grant-in-social-sciences/"
        ),
    ],
    "frontier-2": ["https://fulbrightcenter.dk/scholars/"],
    "frontier-3": ["https://fulbrightcenter.dk/go-to-the-us/students/"],
    "frontier-4": [
        (
            "https://fulbrightcenter.dk/grantsforamericans/fulbright-fo"
            "r-scholars/fulbright-schuman/"
        ),
    ],
    "frontier-5": ["https://fulbrightcenter.dk/data-protection/"],
    "frontier-6": ["https://fulbrightcenter.dk/returning-home/"],
    "frontier-7": [
        (
            "https://fulbrightcenter.dk/advising/graduate-advising/to-d"
            "o-list-for-master-and-phd-studies-in-usa/"
        ),
    ],
    "frontier-8": [
        (
            "https://fulbrightcenter.dk/webinar-om-legatsogning-tirsdag"
            "-d-24-januar-2023/"
        ),
    ],
    "frontier-9": [
        (
            "https://fulbrightcenter.dk/webinar-om-legatsogning-d-9-nov"
            "ember-2022/"
        ),
    ],
    "frontier-10": [
        ("https://fulbrightcenter.dk/webinar-for-legatsogning-16-nov" "ember/"),
    ],
    "frontier-11": ["https://fulbrightcenter.dk/advising/housing-in-the-us/"],
    "frontier-12": [
        ("https://fulbrightcenter.dk/advising/cost-of-living-in-denm" "ark/"),
    ],
    "frontier-13": ["https://fulbrightcenter.dk/usa-legater/"],
    "frontier-14": ["https://fulbrightcenter.dk/usa-vejledning/"],
    "gap-1": ["https://fulbrightcenter.dk/advising/financing/"],
    "gap-2": ["https://fulbrightcenter.dk/alumni/"],
    "gap-3": [
        ("https://fulbrightcenter.dk/advising/training-internships-i" "n-usa/"),
    ],
    "gap-4": [
        ("https://fulbrightcenter.dk/advising/advising-postdocs-scho" "lars/"),
    ],
    "gap-5": ["https://fulbrightcenter.dk/advising/graduate-advising/"],
    "gap-6": ["https://fulbrightcenter.dk/advising/broad-opportunities/"],
    "gap-7": ["https://fulbrightcenter.dk/advising/"],
    "gap-8": ["https://fulbrightcenter.dk/advising/u-s-tests/"],
    "gap-9": ["https://fulbrightcenter.dk/advising/u-s-grading-systems/"],
    "gap-10": ["https://fulbrightcenter.dk/advising/travelling-with-family/"],
    "arctic-iii": [
        "https://fulbrightcenter.dk/scholars/arctic-initiative-iii/"
    ],
    "arctic-iii-apply": [
        ("https://fulbrightcenter.dk/scholars/arctic-initiative-iii/" "apply/"),
    ],
}

CONDITIONS = {
    "about": (
        "2617b8ab45e01d1b26ea42f4744f064e0f61161db1e010ac51e92abd4c1eea" "02"
    ),
    "all-disciplines": (
        "5a4b631e5c40043dabfe7a2cf87db0f81ac23404af6f4b197a0c29b392870f" "53"
    ),
    "arctic": (
        "3a2ce1d6c3aeb14a58a69527aec23b115de6e40a0759634ad571636640824c" "31"
    ),
    "arctic-iii": (
        "bdb860165fe118bbcabe100d6cd889c09d662e9651135b41475b18d22230e4" "4c"
    ),
    "arctic-iii-apply": (
        "b5c828389461cbe6cd4026e2560aebac88addae7a6e0bb17663a4efba93b31" "8b"
    ),
    "arts": (
        "1eb03b57b62dfe6f75333c972656c828643734f1fe71ade250cdb45421dbf4" "26"
    ),
    "book-scholar": (
        "c0bf88a57e4560c3e1de43288f0a2d3eca7f5bab2f9d6ca27f9c2d8bb908d9" "e1"
    ),
    "community-college": (
        "653c15e84aebab18a98454ac43af966acb74a4a57a5eecc99bb7750913343e" "a6"
    ),
    "crown": (
        "451865c3d5fe5452faa4a099143bf41a90fe15b284fb6aa83bce3674b38248" "05"
    ),
    "current-status": (
        "6fa7226d9fbbf971ad06ec66ac30c84efbc17b255c34730e0fcf58275cd679" "a2"
    ),
    "distinguished": (
        "072055e2fc890899a2791d85a857974da9e0f540bb58b14f3fa59a90d9216f" "dc"
    ),
    "entry": (
        "65c537d9055a6d8bf00eb94011969813710bd37c7e951ba2f411083dde9fa3" "34"
    ),
    "frontier-1": (
        "56a0fbac768847995d60012d16550c1a1d626e902dd3e28c40f4118610edb5" "47"
    ),
    "frontier-10": (
        "d454d3995e7380f03f478093238cf0fc34fd6cf455cd8da93783436a4e8661" "b8"
    ),
    "frontier-11": (
        "28f0d444613dbde74daf181ddc7253fcbfbd1bf1a4fc0f8ceee9aad845afa8" "c3"
    ),
    "frontier-12": (
        "63f8e4d6669c821eb915c7c747026cd661c6d43464592b1474b7680b77ae15" "ae"
    ),
    "frontier-13": (
        "a5a3fbbfe64fb09ed52e7505f5ff986ed08ed64d80141877f8d2fbed43cd46" "7d"
    ),
    "frontier-14": (
        "f2f9d4a817ee0b1c4f80f3c44877ce9f550e982f9b78ce6ab92b38013bbbd7" "d5"
    ),
    "frontier-2": (
        "50e13a217af4289b5adf8776c62a5a71d33ae32258390355da62cf9ef5ad59" "7b"
    ),
    "frontier-3": (
        "598fb658f461e87b5cf62b618de501feaaf07454197e6e4a934cf6627de594" "ca"
    ),
    "frontier-4": (
        "b447eecab864e90f9be2b56200bbfe189fa74811f72a831d2ebfc762c34839" "b5"
    ),
    "frontier-5": (
        "1d53b5063c7fd5c55cd64fc157397b64d2cc3c2b3476f2efd451f01662c12e" "d4"
    ),
    "frontier-6": (
        "1206af65571691b6d113789b4bc010e2192d6e2dfc8ec19cf40b7748da06a4" "4e"
    ),
    "frontier-7": (
        "b49c3e3f8c25a004927f24e29ff63a26a99af8f2d145f0833beacbd36b4466" "00"
    ),
    "frontier-8": (
        "c038574e65c373a42c999329f8bdf8761832cfba6109cff070b3c1f3563fef" "a7"
    ),
    "frontier-9": (
        "241187398d656984e57a9a524f88d26e2a58f04cc3167e8885f5572ad56fe1" "fc"
    ),
    "gap-1": (
        "380a81ea6a347bc9f001084df2a9f74e604ace997c9fa4f8a46828e13f03eb" "02"
    ),
    "gap-10": (
        "26b54e930295cdb48841a7f207475225bc563a737a4c52675554ff973708fc" "df"
    ),
    "gap-2": (
        "03ecc0b50e3b1b71ae6149fa0db1f2966c898105310cda8ed80bd157f6cd12" "25"
    ),
    "gap-3": (
        "f06f19d735c93180bea1b0378bcb929ae1f0b9ee07267a5dec87ad820ceb49" "5d"
    ),
    "gap-4": (
        "41ad572e0ca256d04d1200a555aa84cfa2bca06afa712ac9ffb61a4a3a6902" "82"
    ),
    "gap-5": (
        "4a6818306632a0f1ee07ed6ca663eff84badf38e29b4bbd22b3777d0676c5c" "3e"
    ),
    "gap-6": (
        "fd51f193c3359d71ee33a499abfec65a02d59be8d4e45a1ed250f1f1ea1796" "59"
    ),
    "gap-7": (
        "6105f75143024cbcacb90660fc278230067f35447bcf7fa534e7aab0108281" "b1"
    ),
    "gap-8": (
        "c600b57694b4bbd44ac83ef246f3708087fbff6c94d6feaf382efff1d4be2c" "80"
    ),
    "gap-9": (
        "68d3e2ea701a25cb5c1690a8aa8fd92f5b4e28262d5673908d90d293cde1c3" "75"
    ),
    "home": (
        "7c33885f18e3d748af9edf2dc1cfa087999bce1654618422d318f81cf8917f" "30"
    ),
    "hosts": (
        "6492959714c9c45b4241d262ef43bdd04eb6960f3d6f9234011b7e11b6eb7d" "5f"
    ),
    "incoming": (
        "ca83ccc9242ca0a133f3c27ca19e5bdddf9d59c6dcf146a9443e10481476d3" "73"
    ),
    "intercountry": (
        "38f96833eef9eacdd43f00373ad790cbfee9ad19b98990ea66be4c02cfa859" "e6"
    ),
    "intercountry-apply": (
        "9d8a574ee4b53c45e211a6dbcb522c34e78885453e4fdd9251d0ca9b44f085" "81"
    ),
    "joint": (
        "2b30b8db265e261119c75dd208ccef38560ad498c7ea53e02a27e435692a3a" "d2"
    ),
    "other": (
        "4111db76f2b355ff37a4a5afc71f778b7e11d4a28c0008a7d6e8cbe20b04e1" "40"
    ),
    "outgoing": (
        "c8e96c73b7ea4f743f2743dec28c7a0249b2fa205d51db77ecb7bb74ad76e9" "93"
    ),
    "outgoing-alias": (
        "c8e96c73b7ea4f743f2743dec28c7a0249b2fa205d51db77ecb7bb74ad76e9" "93"
    ),
    "privacy": (
        "0e5b585d1e8bcd631e0cc2937b45593714f5cc70b487ba88b4354278329bce" "05"
    ),
    "programme": (
        "fd0fd9a903b055774b7769524e0059168c593ef22c1a1a55926e4d4d6c52ba" "46"
    ),
    "scholar-apply": (
        "f97b7b9f49ee3e803589d1197140000c6d0f9509ecad1d7a596bfbe1117a4e" "4c"
    ),
    "scholar-dk": (
        "fbeaf847ef0d59606d01224a2d1d49e619ff8aec73d59bcf9b1a5e100f92ef" "bd"
    ),
    "scholar-us": (
        "7f5d7769d31760cabfd7a79b7bac03ce2da82fc36135c455742eed5233d70f" "46"
    ),
    "schuman": (
        "558c2ba0a132dc3d80c72c9d6840fb07a0fc39a780bd69529e15caa5259db4" "91"
    ),
    "selection": (
        "1a014b8962a98a4fe96b9866259df9e3df41a9dc9d6bd7bc0ac7e618308e6f" "6b"
    ),
    "specialist": (
        "86fb648da7eebd594b6ecbaaae24a29becf6a51bbeaf5b0161a1b7eff2f99f" "fe"
    ),
    "student-us": (
        "c9e0204cd24e1b776b86d5ed4366cab1844790aad8075b3ea8f4315a881d1b" "84"
    ),
    "thanks": (
        "871ef0133b9065a7f144d9e3d9157d5b222627f3f59f9927e04b5599bf8613" "64"
    ),
}

PROFILES = [
    {
        "track": "student-grant",
        "key": "programme",
        "title": "Fulbright Danish student grants 2027–28",
        "categories": ["scholarships"],
        "hosts": ["US"],
        "eligible": ["DK"],
        "deadline": "2027-02-17T11:00:00.000Z",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Master’s/PhD study or research in the US for Danish "
            "citizens: DKK50,000 total for one semester or DKK100,000 "
            "for two semesters/first degree year. Undergraduate, LLM, "
            "MBA and already-started US studies are excluded. "
            "Tuition-free exchanges generally exclude applicants "
            "except PhD/Arts cases. Apply by February17,2027 at noon "
            "CET."
        ),
        "evidence": (
            "core Danish master's/PhD degree/nondegree US free-mover "
            "grant, US host; Danish citizenship. AY 2027/28 deadline "
            "2027-02-17 T 11:00:00.000 Z (Wednesday Feb 17 noon CET), "
            "open at research clock.50 k DKK TOTAL one semester 4–6 "
            "mo;100 k TOTAL two semesters 8–10 mo/first full-degree "
            "year. No UG/internship/summer-school/conference/LLM/MBA; "
            "not begun USstudy, starts August onward, nondegree "
            "PhD<=12 mo. No exchange/tuition-waiver generally except "
            "PhD/Arts. Master's general graduate admission, no "
            "extension/continuing/MOOC. Remain at US host, limited "
            "foreign travel. Required English two one-page essays/CV 2 "
            "pages/transcripts/two timely recommendations/host "
            "correspondence/passport/commitment+budget; PhD Danish "
            "home academic+financial letter (not contract alone), "
            "artists portfolio. Shared official FAQ conditions and "
            "unresolved exceptions: Core citizenship: Danish passport "
            "mandatory, permanent residence insufficient; dual "
            "Danish/US and US green card excluded. Affiliation/address "
            "in Kingdom of Denmark preferred, close-ties/full-degree "
            "exceptions; do not infer general residence requirement. "
            "Distinct scholar postdoc FAQ DOES require residing in "
            "Denmark despite employment exemption. Official PDF FAQ is "
            "required material source: BA equivalent by grant start, "
            "degree not necessarily by application; no medical degree; "
            "medical research only without direct patient OR animal "
            "contact, conditional acknowledgement. No fixed grade "
            "minimum; previous grantees may reapply with stronger "
            "justification and first-time preference; "
            "recent-US-experience preference (nine months in calendar "
            "year constitutes residence). Student PhD cannot be "
            "completed during student award; expected pre-start degree "
            "requires Scholar/Postdoc competition. J 1 mandatory, "
            "two-year home physical-presence restriction applies to "
            "certain immigration/work visas, not blanket travel "
            "prohibition. Family funding/visas conditional, no "
            "dependent grant; FAQ's spouse J 2 versus later J 1 typo "
            "retained as uncertainty, no promise. No renewal; first "
            "year only, additional funds/visa extension possible not "
            "guaranteed. Main student HTML 50 k 4–6 months and 100 k "
            "8–10 conflicts FAQ 100 k from 7 months onward; retain "
            "both, main stated bands govern summary. Admission may be "
            "pending at application with proof of correspondence; "
            "formal graduate applications versus guest research "
            "invitation requirements differ; PhD initial "
            "correspondence acceptable but final acceptance mandatory. "
            "Payment no earlier two weeks before departure. Additional "
            "own financing guidance requires adequate insurance beyond "
            "minimal ASPE, including liability and repatriation, and "
            "guaranteed officially documented funds for the J1 visa. "
            "Destination/family funding examples are visa "
            "requirements, not scholarship benefits. Graduate advice "
            "describes PhD research stays of six to twelve months, "
            "differing from the main grant semester bands; the source "
            "discrepancy is retained. Postdoc advice allows final host "
            "affiliation after application but before the grant is "
            "received. Spouse/child J2 visas and accompanying-family "
            "permits remain conditional; no dependent cash award is "
            "promised."
        ),
    },
    {
        "track": "joint-florida-poly",
        "key": "joint",
        "title": "Fulbright/Florida Polytechnic joint grants 2027–28",
        "categories": ["scholarships"],
        "hosts": ["US"],
        "eligible": ["DK"],
        "deadline": "2027-02-17T11:00:00.000Z",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Master’s degree/nondegree study in computer/data science "
            "or electrical/mechanical engineering. DKK50,000–100,000 "
            "plus USD4,000 per semester, a 12-credit-hour tuition "
            "waiver and conditional merit aid. Danish applicants apply "
            "separately to the university and identify it in their "
            "Fulbright application."
        ),
        "evidence": (
            "master's degree/nondegree CS/Data Sci/EE/ME only;50 k–100 "
            "k DKK +USD 4000 PER SEMESTER +waiver 12 credit hours "
            "+merit scholarship if available. US host; Danish "
            "citizenship, same core deadline/open. Generic Joint "
            "sidebar 100 k must not erase actual Florida band. Shared "
            "official FAQ conditions and unresolved exceptions: Core "
            "citizenship: Danish passport mandatory, permanent "
            "residence insufficient; dual Danish/US and US green card "
            "excluded. Affiliation/address in Kingdom of Denmark "
            "preferred, close-ties/full-degree exceptions; do not "
            "infer general residence requirement. Distinct scholar "
            "postdoc FAQ DOES require residing in Denmark despite "
            "employment exemption. Official PDF FAQ is required "
            "material source: BA equivalent by grant start, degree not "
            "necessarily by application; no medical degree; medical "
            "research only without direct patient OR animal contact, "
            "conditional acknowledgement. No fixed grade minimum; "
            "previous grantees may reapply with stronger justification "
            "and first-time preference; recent-US-experience "
            "preference (nine months in calendar year constitutes "
            "residence). Student PhD cannot be completed during "
            "student award; expected pre-start degree requires "
            "Scholar/Postdoc competition. J 1 mandatory, two-year home "
            "physical-presence restriction applies to certain "
            "immigration/work visas, not blanket travel prohibition. "
            "Family funding/visas conditional, no dependent grant; "
            "FAQ's spouse J 2 versus later J 1 typo retained as "
            "uncertainty, no promise. No renewal; first year only, "
            "additional funds/visa extension possible not guaranteed. "
            "Main student HTML 50 k 4–6 months and 100 k 8–10 "
            "conflicts FAQ 100 k from 7 months onward; retain both, "
            "main stated bands govern summary. Admission may be "
            "pending at application with proof of correspondence; "
            "formal graduate applications versus guest research "
            "invitation requirements differ; PhD initial "
            "correspondence acceptable but final acceptance mandatory. "
            "Payment no earlier two weeks before departure. Specific "
            "published joint tuition waivers govern; same application "
            "alone does not prove blanket LLM/MBA or no-tuition "
            "exclusions. Universal documented Danish passport, "
            "dual-US/green-card and J 1 rules apply. Additional own "
            "financing guidance requires adequate insurance beyond "
            "minimal ASPE, including liability and repatriation, and "
            "guaranteed officially documented funds for the J1 visa. "
            "Destination/family funding examples are visa "
            "requirements, not scholarship benefits. Graduate advice "
            "describes PhD research stays of six to twelve months, "
            "differing from the main grant semester bands; the source "
            "discrepancy is retained. Postdoc advice allows final host "
            "affiliation after application but before the grant is "
            "received. Spouse/child J2 visas and accompanying-family "
            "permits remain conditional; no dependent cash award is "
            "promised."
        ),
    },
    {
        "track": "joint-columbia-gsas",
        "key": "joint",
        "title": "Fulbright/Columbia GSAS joint grants 2027–28",
        "categories": ["scholarships"],
        "hosts": ["US"],
        "eligible": ["DK"],
        "deadline": "2027-02-17T11:00:00.000Z",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Danish MA/PhD applicants to Columbia GSAS may receive "
            "DKK100,000 plus approximately DKK100,000 in tuition "
            "reduction, capped at 50% of actual tuition. Journalism "
            "School is excluded. University admission and the shared "
            "Fulbright application are separate processes."
        ),
        "evidence": (
            "MA/PhD GSAS, no School of Journalism;100 k DKK +approx "
            "100 k DKK tuition reduction capped 50% actualtuition. US "
            "host; Danish citizenship,same deadline/open. Shared "
            "official FAQ conditions and unresolved exceptions: Core "
            "citizenship: Danish passport mandatory, permanent "
            "residence insufficient; dual Danish/US and US green card "
            "excluded. Affiliation/address in Kingdom of Denmark "
            "preferred, close-ties/full-degree exceptions; do not "
            "infer general residence requirement. Distinct scholar "
            "postdoc FAQ DOES require residing in Denmark despite "
            "employment exemption. Official PDF FAQ is required "
            "material source: BA equivalent by grant start, degree not "
            "necessarily by application; no medical degree; medical "
            "research only without direct patient OR animal contact, "
            "conditional acknowledgement. No fixed grade minimum; "
            "previous grantees may reapply with stronger justification "
            "and first-time preference; recent-US-experience "
            "preference (nine months in calendar year constitutes "
            "residence). Student PhD cannot be completed during "
            "student award; expected pre-start degree requires "
            "Scholar/Postdoc competition. J 1 mandatory, two-year home "
            "physical-presence restriction applies to certain "
            "immigration/work visas, not blanket travel prohibition. "
            "Family funding/visas conditional, no dependent grant; "
            "FAQ's spouse J 2 versus later J 1 typo retained as "
            "uncertainty, no promise. No renewal; first year only, "
            "additional funds/visa extension possible not guaranteed. "
            "Main student HTML 50 k 4–6 months and 100 k 8–10 "
            "conflicts FAQ 100 k from 7 months onward; retain both, "
            "main stated bands govern summary. Admission may be "
            "pending at application with proof of correspondence; "
            "formal graduate applications versus guest research "
            "invitation requirements differ; PhD initial "
            "correspondence acceptable but final acceptance mandatory. "
            "Payment no earlier two weeks before departure. Specific "
            "published joint tuition waivers govern; same application "
            "alone does not prove blanket LLM/MBA or no-tuition "
            "exclusions. Universal documented Danish passport, "
            "dual-US/green-card and J 1 rules apply. Additional own "
            "financing guidance requires adequate insurance beyond "
            "minimal ASPE, including liability and repatriation, and "
            "guaranteed officially documented funds for the J1 visa. "
            "Destination/family funding examples are visa "
            "requirements, not scholarship benefits. Graduate advice "
            "describes PhD research stays of six to twelve months, "
            "differing from the main grant semester bands; the source "
            "discrepancy is retained. Postdoc advice allows final host "
            "affiliation after application but before the grant is "
            "received. Spouse/child J2 visas and accompanying-family "
            "permits remain conditional; no dependent cash award is "
            "promised."
        ),
    },
    {
        "track": "joint-nyu-gsas",
        "key": "joint",
        "title": "Fulbright/NYU GSAS joint grants 2027–28",
        "categories": ["scholarships"],
        "hosts": ["US"],
        "eligible": ["DK"],
        "deadline": "2027-02-17T11:00:00.000Z",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Degree-seeking Danish MA/PhD students at NYU GSAS: "
            "DKK100,000 plus a full first-year tuition waiver. Stern "
            "business, cinema/performance and specified "
            "medical/environmental programmes are excluded. Applicants "
            "apply to the university and identify NYU in their "
            "Fulbright application."
        ),
        "evidence": (
            "DEGREE-seeking MA/PhD,100 k DKK +full "
            "FIRST-YEARtuitionwaiver; excludes Stern "
            "Business/Cinema/Performance/Environmental Health/Basic "
            "Medical/Biomaterials. US host; Danish citizenship,same "
            "deadline/open. Shared official FAQ conditions and "
            "unresolved exceptions: Core citizenship: Danish passport "
            "mandatory, permanent residence insufficient; dual "
            "Danish/US and US green card excluded. Affiliation/address "
            "in Kingdom of Denmark preferred, close-ties/full-degree "
            "exceptions; do not infer general residence requirement. "
            "Distinct scholar postdoc FAQ DOES require residing in "
            "Denmark despite employment exemption. Official PDF FAQ is "
            "required material source: BA equivalent by grant start, "
            "degree not necessarily by application; no medical degree; "
            "medical research only without direct patient OR animal "
            "contact, conditional acknowledgement. No fixed grade "
            "minimum; previous grantees may reapply with stronger "
            "justification and first-time preference; "
            "recent-US-experience preference (nine months in calendar "
            "year constitutes residence). Student PhD cannot be "
            "completed during student award; expected pre-start degree "
            "requires Scholar/Postdoc competition. J 1 mandatory, "
            "two-year home physical-presence restriction applies to "
            "certain immigration/work visas, not blanket travel "
            "prohibition. Family funding/visas conditional, no "
            "dependent grant; FAQ's spouse J 2 versus later J 1 typo "
            "retained as uncertainty, no promise. No renewal; first "
            "year only, additional funds/visa extension possible not "
            "guaranteed. Main student HTML 50 k 4–6 months and 100 k "
            "8–10 conflicts FAQ 100 k from 7 months onward; retain "
            "both, main stated bands govern summary. Admission may be "
            "pending at application with proof of correspondence; "
            "formal graduate applications versus guest research "
            "invitation requirements differ; PhD initial "
            "correspondence acceptable but final acceptance mandatory. "
            "Payment no earlier two weeks before departure. Specific "
            "published joint tuition waivers govern; same application "
            "alone does not prove blanket LLM/MBA or no-tuition "
            "exclusions. Universal documented Danish passport, "
            "dual-US/green-card and J 1 rules apply. Additional own "
            "financing guidance requires adequate insurance beyond "
            "minimal ASPE, including liability and repatriation, and "
            "guaranteed officially documented funds for the J1 visa. "
            "Destination/family funding examples are visa "
            "requirements, not scholarship benefits. Graduate advice "
            "describes PhD research stays of six to twelve months, "
            "differing from the main grant semester bands; the source "
            "discrepancy is retained. Postdoc advice allows final host "
            "affiliation after application but before the grant is "
            "received. Spouse/child J2 visas and accompanying-family "
            "permits remain conditional; no dependent cash award is "
            "promised."
        ),
    },
    {
        "track": "joint-chicago-physical-sciences",
        "key": "joint",
        "title": ("Fulbright/Chicago Physical Sciences joint grants 2027–28"),
        "categories": ["scholarships"],
        "hosts": ["US"],
        "eligible": ["DK"],
        "deadline": "2027-02-17T11:00:00.000Z",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "One year of Danish master’s degree/nondegree study in "
            "Chicago Physical Sciences: DKK100,000 plus reduced "
            "tuition priced at USD21,800 for master’s study or "
            "USD3,170 per quarter for nondegree study. Those figures "
            "are tuition charges, not cash grants; a later PhD route "
            "is conditional."
        ),
        "evidence": (
            "one-year Physical Sciences MAdegree/nondegree;100 k DKK "
            "+reduced TUITION PRICE USD 21800 master's OR USD 3170 PER "
            "QUARTER nondegree (not stipend). Later PhD "
            "possibility/waiver conditional, no conversion guarantee. "
            "US host; Danish citizenship,same deadline/open. Shared "
            "official FAQ conditions and unresolved exceptions: Core "
            "citizenship: Danish passport mandatory, permanent "
            "residence insufficient; dual Danish/US and US green card "
            "excluded. Affiliation/address in Kingdom of Denmark "
            "preferred, close-ties/full-degree exceptions; do not "
            "infer general residence requirement. Distinct scholar "
            "postdoc FAQ DOES require residing in Denmark despite "
            "employment exemption. Official PDF FAQ is required "
            "material source: BA equivalent by grant start, degree not "
            "necessarily by application; no medical degree; medical "
            "research only without direct patient OR animal contact, "
            "conditional acknowledgement. No fixed grade minimum; "
            "previous grantees may reapply with stronger justification "
            "and first-time preference; recent-US-experience "
            "preference (nine months in calendar year constitutes "
            "residence). Student PhD cannot be completed during "
            "student award; expected pre-start degree requires "
            "Scholar/Postdoc competition. J 1 mandatory, two-year home "
            "physical-presence restriction applies to certain "
            "immigration/work visas, not blanket travel prohibition. "
            "Family funding/visas conditional, no dependent grant; "
            "FAQ's spouse J 2 versus later J 1 typo retained as "
            "uncertainty, no promise. No renewal; first year only, "
            "additional funds/visa extension possible not guaranteed. "
            "Main student HTML 50 k 4–6 months and 100 k 8–10 "
            "conflicts FAQ 100 k from 7 months onward; retain both, "
            "main stated bands govern summary. Admission may be "
            "pending at application with proof of correspondence; "
            "formal graduate applications versus guest research "
            "invitation requirements differ; PhD initial "
            "correspondence acceptable but final acceptance mandatory. "
            "Payment no earlier two weeks before departure. Specific "
            "published joint tuition waivers govern; same application "
            "alone does not prove blanket LLM/MBA or no-tuition "
            "exclusions. Universal documented Danish passport, "
            "dual-US/green-card and J 1 rules apply. Additional own "
            "financing guidance requires adequate insurance beyond "
            "minimal ASPE, including liability and repatriation, and "
            "guaranteed officially documented funds for the J1 visa. "
            "Destination/family funding examples are visa "
            "requirements, not scholarship benefits. Graduate advice "
            "describes PhD research stays of six to twelve months, "
            "differing from the main grant semester bands; the source "
            "discrepancy is retained. Postdoc advice allows final host "
            "affiliation after application but before the grant is "
            "received. Spouse/child J2 visas and accompanying-family "
            "permits remain conditional; no dependent cash award is "
            "promised."
        ),
    },
    {
        "track": "joint-pittsburgh-gspia",
        "key": "joint",
        "title": "Fulbright/Pittsburgh GSPIA joint grants 2027–28",
        "categories": ["scholarships"],
        "hosts": ["US"],
        "eligible": ["DK"],
        "deadline": "2027-02-17T11:00:00.000Z",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Danish MA/PhD study in international relations, affairs "
            "or studies at Pittsburgh GSPIA: DKK100,000 plus USD6,500 "
            "tuition reduction. The own agreement describes a "
            "one-year/degree route. University admission is separate "
            "from the shared Fulbright application."
        ),
        "evidence": (
            "MA/PhD Intl Relations/Affairs/Studies;100 k DKK +USD 6500 "
            "tuition reduction; one-year/degree route. US host; Danish "
            "citizenship,same deadline/open. Shared official FAQ "
            "conditions and unresolved exceptions: Core citizenship: "
            "Danish passport mandatory, permanent residence "
            "insufficient; dual Danish/US and US green card excluded. "
            "Affiliation/address in Kingdom of Denmark preferred, "
            "close-ties/full-degree exceptions; do not infer general "
            "residence requirement. Distinct scholar postdoc FAQ DOES "
            "require residing in Denmark despite employment exemption. "
            "Official PDF FAQ is required material source: BA "
            "equivalent by grant start, degree not necessarily by "
            "application; no medical degree; medical research only "
            "without direct patient OR animal contact, conditional "
            "acknowledgement. No fixed grade minimum; previous "
            "grantees may reapply with stronger justification and "
            "first-time preference; recent-US-experience preference "
            "(nine months in calendar year constitutes residence). "
            "Student PhD cannot be completed during student award; "
            "expected pre-start degree requires Scholar/Postdoc "
            "competition. J 1 mandatory, two-year home "
            "physical-presence restriction applies to certain "
            "immigration/work visas, not blanket travel prohibition. "
            "Family funding/visas conditional, no dependent grant; "
            "FAQ's spouse J 2 versus later J 1 typo retained as "
            "uncertainty, no promise. No renewal; first year only, "
            "additional funds/visa extension possible not guaranteed. "
            "Main student HTML 50 k 4–6 months and 100 k 8–10 "
            "conflicts FAQ 100 k from 7 months onward; retain both, "
            "main stated bands govern summary. Admission may be "
            "pending at application with proof of correspondence; "
            "formal graduate applications versus guest research "
            "invitation requirements differ; PhD initial "
            "correspondence acceptable but final acceptance mandatory. "
            "Payment no earlier two weeks before departure. Specific "
            "published joint tuition waivers govern; same application "
            "alone does not prove blanket LLM/MBA or no-tuition "
            "exclusions. Universal documented Danish passport, "
            "dual-US/green-card and J 1 rules apply. Additional own "
            "financing guidance requires adequate insurance beyond "
            "minimal ASPE, including liability and repatriation, and "
            "guaranteed officially documented funds for the J1 visa. "
            "Destination/family funding examples are visa "
            "requirements, not scholarship benefits. Graduate advice "
            "describes PhD research stays of six to twelve months, "
            "differing from the main grant semester bands; the source "
            "discrepancy is retained. Postdoc advice allows final host "
            "affiliation after application but before the grant is "
            "received. Spouse/child J2 visas and accompanying-family "
            "permits remain conditional; no dependent cash award is "
            "promised."
        ),
    },
    {
        "track": "arts-co-funded",
        "key": "arts",
        "title": "Fulbright Denmark co-funded Arts grants 2027–28",
        "categories": ["scholarships"],
        "hosts": ["US"],
        "eligible": ["DK"],
        "deadline": "2027-02-17T11:00:00.000Z",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Nondegree US master’s study for students affiliated with "
            "Danish Ministry of Culture institutions: DKK60,000 total "
            "for one or two semesters. Core student conditions apply, "
            "but institutional exchanges and tuition waivers are "
            "permitted with demonstrated financial need. Apply by "
            "February17,2027 at noon CET."
        ),
        "evidence": (
            "nondegree MASTER'S, Danish Ministry Cultureinstitution "
            "affiliation,60 k DKK TOTAL one ORtwo semesters. Explicit "
            "core conditions apply except institutional "
            "exchange/tuitionwaiver allowed with documented FINANCIAL "
            "NEED. US host; Danish citizenship,same 2027 Feb 17 noon "
            "CET/open. Shared official FAQ conditions and unresolved "
            "exceptions: Core citizenship: Danish passport mandatory, "
            "permanent residence insufficient; dual Danish/US and US "
            "green card excluded. Affiliation/address in Kingdom of "
            "Denmark preferred, close-ties/full-degree exceptions; do "
            "not infer general residence requirement. Distinct scholar "
            "postdoc FAQ DOES require residing in Denmark despite "
            "employment exemption. Official PDF FAQ is required "
            "material source: BA equivalent by grant start, degree not "
            "necessarily by application; no medical degree; medical "
            "research only without direct patient OR animal contact, "
            "conditional acknowledgement. No fixed grade minimum; "
            "previous grantees may reapply with stronger justification "
            "and first-time preference; recent-US-experience "
            "preference (nine months in calendar year constitutes "
            "residence). Student PhD cannot be completed during "
            "student award; expected pre-start degree requires "
            "Scholar/Postdoc competition. J 1 mandatory, two-year home "
            "physical-presence restriction applies to certain "
            "immigration/work visas, not blanket travel prohibition. "
            "Family funding/visas conditional, no dependent grant; "
            "FAQ's spouse J 2 versus later J 1 typo retained as "
            "uncertainty, no promise. No renewal; first year only, "
            "additional funds/visa extension possible not guaranteed. "
            "Main student HTML 50 k 4–6 months and 100 k 8–10 "
            "conflicts FAQ 100 k from 7 months onward; retain both, "
            "main stated bands govern summary. Admission may be "
            "pending at application with proof of correspondence; "
            "formal graduate applications versus guest research "
            "invitation requirements differ; PhD initial "
            "correspondence acceptable but final acceptance mandatory. "
            "Payment no earlier two weeks before departure. Additional "
            "own financing guidance requires adequate insurance beyond "
            "minimal ASPE, including liability and repatriation, and "
            "guaranteed officially documented funds for the J1 visa. "
            "Destination/family funding examples are visa "
            "requirements, not scholarship benefits. Graduate advice "
            "describes PhD research stays of six to twelve months, "
            "differing from the main grant semester bands; the source "
            "discrepancy is retained. Postdoc advice allows final host "
            "affiliation after application but before the grant is "
            "received. Spouse/child J2 visas and accompanying-family "
            "permits remain conditional; no dependent cash award is "
            "promised."
        ),
    },
    {
        "track": "danish-scholar",
        "key": "scholar-dk",
        "title": "Fulbright Danish scholar grants 2027–28",
        "categories": ["fellowships"],
        "hosts": ["US"],
        "eligible": ["DK"],
        "deadline": "2027-02-17T11:00:00.000Z",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Both research and teaching in the US for four to ten "
            "months, with DKK200,000 total. Danish citizens normally "
            "need Kingdom-of-Denmark higher-education employment; "
            "postdocs are exempt from employment but must reside in "
            "Denmark. A US host invitation and strong English are "
            "required. Apply by February17,2027 at noon CET."
        ),
        "evidence": (
            "BOTH research ANDteaching US visit,200 k DKK TOTAL 4–10 "
            "mo,notmonthly. DKcitizens, Kingdom "
            "HEIassistant/associate/professor employment, postdoc "
            "exemptemployment but mandatory DKresidence FAQ. US host; "
            "Danish citizenship,2027 Feb 17 noon CET/open. "
            "Hostinvitation/high English/allfields, Augustonward, no "
            "already-started USresearch/conference/summerschool/grouptr"
            "ip. No dependentgrant. Teaching "
            "co-teaching/workshop/supervision possible. CV 4 "
            "pages/project 3–5+bib 1–3/syllabi<=10/two "
            "timelyrefs/hostletter/passport/homeacademicfinancialsuppor"
            "t notrequired postdocs. Shared official FAQ conditions "
            "and unresolved exceptions: Core citizenship: Danish "
            "passport mandatory, permanent residence insufficient; "
            "dual Danish/US and US green card excluded. "
            "Affiliation/address in Kingdom of Denmark preferred, "
            "close-ties/full-degree exceptions; do not infer general "
            "residence requirement. Distinct scholar postdoc FAQ DOES "
            "require residing in Denmark despite employment exemption. "
            "Official PDF FAQ is required material source: BA "
            "equivalent by grant start, degree not necessarily by "
            "application; no medical degree; medical research only "
            "without direct patient OR animal contact, conditional "
            "acknowledgement. No fixed grade minimum; previous "
            "grantees may reapply with stronger justification and "
            "first-time preference; recent-US-experience preference "
            "(nine months in calendar year constitutes residence). "
            "Student PhD cannot be completed during student award; "
            "expected pre-start degree requires Scholar/Postdoc "
            "competition. J 1 mandatory, two-year home "
            "physical-presence restriction applies to certain "
            "immigration/work visas, not blanket travel prohibition. "
            "Family funding/visas conditional, no dependent grant; "
            "FAQ's spouse J 2 versus later J 1 typo retained as "
            "uncertainty, no promise. No renewal; first year only, "
            "additional funds/visa extension possible not guaranteed. "
            "Main student HTML 50 k 4–6 months and 100 k 8–10 "
            "conflicts FAQ 100 k from 7 months onward; retain both, "
            "main stated bands govern summary. Admission may be "
            "pending at application with proof of correspondence; "
            "formal graduate applications versus guest research "
            "invitation requirements differ; PhD initial "
            "correspondence acceptable but final acceptance mandatory. "
            "Payment no earlier two weeks before departure. Additional "
            "own financing guidance requires adequate insurance beyond "
            "minimal ASPE, including liability and repatriation, and "
            "guaranteed officially documented funds for the J1 visa. "
            "Destination/family funding examples are visa "
            "requirements, not scholarship benefits. Graduate advice "
            "describes PhD research stays of six to twelve months, "
            "differing from the main grant semester bands; the source "
            "discrepancy is retained. Postdoc advice allows final host "
            "affiliation after application but before the grant is "
            "received. Spouse/child J2 visas and accompanying-family "
            "permits remain conditional; no dependent cash award is "
            "promised."
        ),
    },
    {
        "track": "schuman-pre-doctoral",
        "key": "schuman",
        "title": "Fulbright Schuman pre-doctoral research grants 2027–28",
        "categories": ["fellowships"],
        "hosts": ["US"],
        "eligible": [],
        "deadline": "2026-12-01T11:00:00.000Z",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Three to nine months of US research for European "
            "applicants working on EU affairs or US–EU relations, "
            "relevant to at least two EU member states. Students "
            "receive EUR2,000 monthly plus EUR2,000 one-time travel "
            "support. Apply by December1,2026 at noon CET; Fulbright "
            "Belgium administers these awards."
        ),
        "evidence": (
            "EU/European applicants, researchtopic US–EU/EUaffairs "
            "relevant>=2 EUstates, anyfield,US 3–9 mo;EUR 2000 "
            "PERMONTH +EUR 2000 ONETIMEtravel. US host; no explicit "
            "citizenship whitelist (European applicant wording is not "
            "explicit passport whitelist). AY 2027/28 Dec 1,2026 "
            "NOONCET ->2026-12-01 T 11:00:00.000 Z/open."
        ),
    },
    {
        "track": "schuman-postdoctoral",
        "key": "schuman",
        "title": ("Fulbright Schuman postdoctoral research/lecturing 2027–28"),
        "categories": ["fellowships"],
        "hosts": ["US"],
        "eligible": [],
        "deadline": "2026-12-01T11:00:00.000Z",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Three to nine months in the US under the named "
            "postdoctoral research and lecturing track. European "
            "applicants’ projects must address EU affairs or US–EU "
            "relations and at least two EU states. Scholars receive "
            "EUR3,000 monthly plus EUR2,000 one-time travel support. "
            "Apply by December1,2026 at noon CET."
        ),
        "evidence": (
            "sametopic/duration/date, named Post-Doctoral Research and "
            "Lecturing track (the own page does not explicitly require "
            "both activities), scholars EUR 3000/month +EUR 2000 "
            "travel. US host; no explicit citizenship whitelist."
        ),
    },
    {
        "track": "schuman-international-educators",
        "key": "schuman",
        "title": ("Fulbright Schuman International Educators grants 2027–28"),
        "categories": ["fellowships"],
        "hosts": ["US"],
        "eligible": [],
        "deadline": "2026-12-01T11:00:00.000Z",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "The named International Educators track supports three to "
            "nine months in the US, with EU-affairs/US–EU and "
            "two-member-state project conditions. The common page "
            "states EUR2,000 one-time travel support; its "
            "student/scholar monthly rates do not explicitly identify "
            "this track’s rate. Apply by December1,2026 at noon CET."
        ),
        "evidence": (
            "actual third named role, samecommon 3–9 mo/date/topic/US. "
            "Monthly panel labels Students versus Scholars but does "
            "not explicitly map International Educators; monthly "
            "amount is unspecified for this named track;EUR 2000 "
            "travel common. US host; no explicit citizenship "
            "whitelist. Belgium administration, not Denmark. 10–15 "
            "awards available places."
        ),
    },
    {
        "track": "crown-full-mpp-mpa",
        "key": "crown",
        "title": "Crown Prince Frederik Fund full MPP/MPA 2028–29",
        "categories": ["scholarships"],
        "hosts": ["US"],
        "eligible": ["DK"],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "Two-year full MPP/MPA study at Harvard Kennedy School or "
            "an equivalent Ivy League alternative. The scholarship "
            "covers tuition, with no common amount stated. Danish "
            "citizenship and strong Denmark ties are required; US dual "
            "citizens/green-card holders are excluded. The 2028–29 "
            "application round is expected to open in spring2027; no "
            "closing date is published."
        ),
        "evidence": (
            "two-year FULLMPP/MPA Harvard Kennedy or equivalent Ivy "
            "League alternative; tuitiononly amountunknown,1–2 slots "
            "places. AY 2028/29 opens Spring 2027 (NOclosing date),no "
            "complete current deadline is published. US host; Danish "
            "citizenship. Strong DKties mandatory, dual "
            "US/greencardexcluded, publicservice/leadership/nonprofit/w"
            "orkcontext,stronggrades (10/12 preferred),quantproof; "
            "MPPGRE/GMATquant>=70 percent ONLYMPP. Workexperience "
            "useful/typical 2–3 years not blanket minimum. "
            "Applyfundfirst, nomineesapply HKS+1–3 alternatives, "
            "finalhostacceptancecondition."
        ),
    },
    {
        "track": "crown-midcareer-mpa",
        "key": "crown",
        "title": "Crown Prince Frederik Fund Mid-Career MPA 2028–29",
        "categories": ["scholarships"],
        "hosts": ["US"],
        "eligible": ["DK"],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "One-year Mid-Career MPA study at Harvard Kennedy School "
            "or an equivalent Ivy League alternative, covering "
            "tuition. Danish citizenship and strong Denmark ties are "
            "required; leadership/public-service experience matters. "
            "The 2028–29 round is expected to open in spring2027; no "
            "current deadline or guaranteed living allowance is "
            "published."
        ),
        "evidence": (
            "one-year MC/MPA actualdistinctdegree/audience, same "
            "future 2028/29/tuition-only conditions. US host; Danish "
            "citizenship,no complete current deadline is published; "
            "The published future round is separate from passed "
            "2027/28 round status to future."
        ),
    },
    {
        "track": "thanks-general-degree",
        "key": "thanks",
        "title": "Thanks to Scandinavia general-degree grants 2027–28",
        "categories": ["scholarships"],
        "hosts": ["US"],
        "eligible": ["DK"],
        "deadline": "2027-02-17T11:00:00.000Z",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "One year of full-degree US study, with master’s study "
            "preferred: USD20,000 total toward tuition, books and "
            "university fees, excluding living costs. Candidates use "
            "the Danish Fulbright nomination process. Core citizenship "
            "exclusions conflict with this grant’s softer preference "
            "wording; no waiver or automatic stacking is promised."
        ),
        "evidence": (
            "generalfull USdegree (master'spreferred) ONE-year USD "
            "20000 TOTALtuition/books/universityfees NOTliving.2 "
            "master's slots capacity, not two identities; "
            "captionmaster's versus generaldegree wording ambiguity "
            "means no guaranteed PhDeligibility. "
            "Coreapplicationnomination, decisions May June,pay "
            "USafterarrival. US host; Danish citizenship,same 2027 Feb "
            "17 deadline/open. Coremandatorydual US/green exclusion "
            "conflicts specific Thanks preference wording; "
            "preserveboth, NOinferredwaiver/no promised core+20 "
            "kstack. Shared official FAQ conditions and unresolved "
            "exceptions: Core citizenship: Danish passport mandatory, "
            "permanent residence insufficient; dual Danish/US and US "
            "green card excluded. Affiliation/address in Kingdom of "
            "Denmark preferred, close-ties/full-degree exceptions; do "
            "not infer general residence requirement. Distinct scholar "
            "postdoc FAQ DOES require residing in Denmark despite "
            "employment exemption. Official PDF FAQ is required "
            "material source: BA equivalent by grant start, degree not "
            "necessarily by application; no medical degree; medical "
            "research only without direct patient OR animal contact, "
            "conditional acknowledgement. No fixed grade minimum; "
            "previous grantees may reapply with stronger justification "
            "and first-time preference; recent-US-experience "
            "preference (nine months in calendar year constitutes "
            "residence). Student PhD cannot be completed during "
            "student award; expected pre-start degree requires "
            "Scholar/Postdoc competition. J 1 mandatory, two-year home "
            "physical-presence restriction applies to certain "
            "immigration/work visas, not blanket travel prohibition. "
            "Family funding/visas conditional, no dependent grant; "
            "FAQ's spouse J 2 versus later J 1 typo retained as "
            "uncertainty, no promise. No renewal; first year only, "
            "additional funds/visa extension possible not guaranteed. "
            "Main student HTML 50 k 4–6 months and 100 k 8–10 "
            "conflicts FAQ 100 k from 7 months onward; retain both, "
            "main stated bands govern summary. Admission may be "
            "pending at application with proof of correspondence; "
            "formal graduate applications versus guest research "
            "invitation requirements differ; PhD initial "
            "correspondence acceptable but final acceptance mandatory. "
            "Payment no earlier two weeks before departure. Additional "
            "own financing guidance requires adequate insurance beyond "
            "minimal ASPE, including liability and repatriation, and "
            "guaranteed officially documented funds for the J1 visa. "
            "Destination/family funding examples are visa "
            "requirements, not scholarship benefits. Graduate advice "
            "describes PhD research stays of six to twelve months, "
            "differing from the main grant semester bands; the source "
            "discrepancy is retained. Postdoc advice allows final host "
            "affiliation after application but before the grant is "
            "received. Spouse/child J2 visas and accompanying-family "
            "permits remain conditional; no dependent cash award is "
            "promised."
        ),
    },
    {
        "track": "thanks-victor-borge-music",
        "key": "thanks",
        "title": "Victor Borge Music grants 2027–28",
        "categories": ["scholarships"],
        "hosts": ["US"],
        "eligible": ["DK"],
        "deadline": "2027-02-17T11:00:00.000Z",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "A distinct music-field Thanks to Scandinavia award: "
            "USD20,000 for one year of US degree study, toward "
            "tuition, books and university fees rather than living "
            "costs. Danish Fulbright nomination is required. "
            "Conflicting dual-citizenship preference wording does not "
            "establish a waiver of core eligibility rules."
        ),
        "evidence": (
            "one music-field type, sameone-year 20 "
            "kcostscope/commondeadline and conflicts, US host; Danish "
            "citizenship. Actualspecificacademicmusic criteria, no "
            "external TTSuniversity catalogues. Shared official FAQ "
            "conditions and unresolved exceptions: Core citizenship: "
            "Danish passport mandatory, permanent residence "
            "insufficient; dual Danish/US and US green card excluded. "
            "Affiliation/address in Kingdom of Denmark preferred, "
            "close-ties/full-degree exceptions; do not infer general "
            "residence requirement. Distinct scholar postdoc FAQ DOES "
            "require residing in Denmark despite employment exemption. "
            "Official PDF FAQ is required material source: BA "
            "equivalent by grant start, degree not necessarily by "
            "application; no medical degree; medical research only "
            "without direct patient OR animal contact, conditional "
            "acknowledgement. No fixed grade minimum; previous "
            "grantees may reapply with stronger justification and "
            "first-time preference; recent-US-experience preference "
            "(nine months in calendar year constitutes residence). "
            "Student PhD cannot be completed during student award; "
            "expected pre-start degree requires Scholar/Postdoc "
            "competition. J 1 mandatory, two-year home "
            "physical-presence restriction applies to certain "
            "immigration/work visas, not blanket travel prohibition. "
            "Family funding/visas conditional, no dependent grant; "
            "FAQ's spouse J 2 versus later J 1 typo retained as "
            "uncertainty, no promise. No renewal; first year only, "
            "additional funds/visa extension possible not guaranteed. "
            "Main student HTML 50 k 4–6 months and 100 k 8–10 "
            "conflicts FAQ 100 k from 7 months onward; retain both, "
            "main stated bands govern summary. Admission may be "
            "pending at application with proof of correspondence; "
            "formal graduate applications versus guest research "
            "invitation requirements differ; PhD initial "
            "correspondence acceptable but final acceptance mandatory. "
            "Payment no earlier two weeks before departure. Additional "
            "own financing guidance requires adequate insurance beyond "
            "minimal ASPE, including liability and repatriation, and "
            "guaranteed officially documented funds for the J1 visa. "
            "Destination/family funding examples are visa "
            "requirements, not scholarship benefits. Graduate advice "
            "describes PhD research stays of six to twelve months, "
            "differing from the main grant semester bands; the source "
            "discrepancy is retained. Postdoc advice allows final host "
            "affiliation after application but before the grant is "
            "received. Spouse/child J2 visas and accompanying-family "
            "permits remain conditional; no dependent cash award is "
            "promised."
        ),
    },
    {
        "track": "us-student",
        "key": "student-us",
        "title": "Fulbright US student grants in Denmark 2028–29",
        "categories": ["scholarships"],
        "hosts": ["DK"],
        "eligible": ["US"],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "Ten months of graduate study/research in Denmark, with "
            "DKK125,000 stated for 2028–29. US citizens apply "
            "separately to their Danish host; US/Danish dual citizens "
            "are excluded. Guest nondegree study is tuition-free, "
            "while full-degree study requires tuition for all years. "
            "The next round opens in spring2027; no 2028–29 closing "
            "date is published."
        ),
        "evidence": (
            "ONE future AY 2028/29 programmeoverview:125 k DKK,total "
            "10 mo MA/PhD/graduate/research, opens Spring 2027 ->no "
            "complete current deadline is published. Body old AY "
            "2027/28 Oct 6,2026 5 pm'EST(UTC-4)' gives literal 21 UTC "
            "but ESTlabel contradicts; preserve old timestamp/mismatch "
            "in evidence only, NEVER attach old closing date/new 125 k "
            "to historical 2027–28 round. Denmark is a known host; the "
            "incoming hub advertises wider Kingdom administration for "
            "Greenland and the Faroe Islands. US citizenship is "
            "required. Dual US/DKexcluded, otherdualcitizen uses "
            "USpassport/permanent USresidence. No UG; "
            "seniors/recentgrads MAclasses. Independentlyapplyhost "
            "andgrant; invitation/affiliation, finaladmission/permit "
            "conditional. Nondegreeguest NOTtuition; full-degree "
            "FULLtuition ALLyears, laterdegreeconversion may create "
            "retroactivetuition;grantonlyfirstyear. Copenhagen "
            "SUNDguest PhD or degree MAonly, no nondegree MA. Denmark "
            "is a positively known nonexclusive host; the incoming hub "
            "also advertises Kingdom administration for "
            "Greenland/Faroe Islands, without per-award "
            "territory/amount guarantees."
        ),
    },
    {
        "track": "us-scholar-all-disciplines",
        "key": "all-disciplines",
        "title": "Fulbright All Disciplines US scholar grants 2028–29",
        "categories": ["fellowships"],
        "hosts": ["DK"],
        "eligible": ["US"],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "US scholars spend four to five months at a Danish "
            "higher-education institution, with DKK200,000 total and a "
            "strong host invitation required. All fields are eligible; "
            "sustainability, space/Arctic and life-science priorities "
            "are preferences. The 2028–29 round is expected to open in "
            "spring2027; no deadline is published."
        ),
        "evidence": (
            "200 k DKK TOTAL 4–5 mo allfields/Kingdom HEI, "
            "stronghostinvitation; sustainability/space/lifesciences "
            "preferences NOTexclusive. host[DK] "
            "conservative,eligible[US],future AY 2028/29 Spring 2027 "
            "no complete current deadline is published, prior 2027/28 "
            "passed distinctevidence. No extra DKjob. Denmark is a "
            "positively known nonexclusive host; the incoming hub also "
            "advertises Kingdom administration for Greenland/Faroe "
            "Islands, without per-award territory/amount guarantees. "
            "No extra Danish job during the grant. Upcoming AY 2028/29 "
            "opening spring 2027 has no actual closing date; old AY "
            "2027/28 deadline passed. Funding remains subject to "
            "fiscal revisions."
        ),
    },
    {
        "track": "us-scholar-distinguished-american-studies",
        "key": "distinguished",
        "title": ("Fulbright Distinguished Chair in American Studies 2028–29"),
        "categories": ["fellowships"],
        "hosts": ["DK"],
        "eligible": ["US"],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "Research and teaching at the University of Southern "
            "Denmark. The own award page allows four-to-five-month or "
            "eight-to-ten-month stays and lists DKK350,000; the host "
            "overview associates that amount only with the longer "
            "stay. The discrepancy is unresolved. The 2028–29 round "
            "opens in spring2027; no closing date is published."
        ),
        "evidence": (
            "SDUresearch ANDteaching,350 k DKK flatbenefitrow; "
            "actualownleaf 4–5 semester OR 8–10 year, hub 350 k 8–10 "
            "only. Preserve source discrepancy, don't assert 350 "
            "kguaranteedhalfterm. Denmark is a known host; US "
            "citizenship,same future no complete current deadline is "
            "published. Denmark is a positively known nonexclusive "
            "host; the incoming hub also advertises Kingdom "
            "administration for Greenland/Faroe Islands, without "
            "per-award territory/amount guarantees. No extra Danish "
            "job during the grant. Upcoming AY 2028/29 opening spring "
            "2027 has no actual closing date; old AY 2027/28 deadline "
            "passed. Funding remains subject to fiscal revisions."
        ),
    },
    {
        "track": "us-scholar-community-college",
        "key": "community-college",
        "title": "Fulbright US Community College Faculty grants 2028–29",
        "categories": ["fellowships"],
        "hosts": ["DK"],
        "eligible": ["US"],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "US community-college faculty spend four to five months at "
            "a Danish higher-education host, receiving DKK200,000 "
            "total. A strong host invitation is required; no extra "
            "Danish job may be taken during the grant. The next "
            "2028–29 application round is expected in spring2027; no "
            "closing date is published."
        ),
        "evidence": (
            "actual UScommunitycollegefaculty,200 k DKK 4–5 mo, "
            "DKHEIcommunitycollege/uniequiv,stronginvitation. Denmark "
            "is a known host; US citizenship,future no complete "
            "current deadline is published. Denmark is a positively "
            "known nonexclusive host; the incoming hub also advertises "
            "Kingdom administration for Greenland/Faroe Islands, "
            "without per-award territory/amount guarantees. No extra "
            "Danish job during the grant. Upcoming AY 2028/29 opening "
            "spring 2027 has no actual closing date; old AY 2027/28 "
            "deadline passed. Funding remains subject to fiscal "
            "revisions."
        ),
    },
    {
        "track": "us-scholar-maritime-digital-transformation",
        "key": "hosts",
        "title": "Fulbright Digital Transformation in Shipping/Maritime",
        "categories": ["fellowships"],
        "hosts": ["DK"],
        "eligible": ["US"],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "The distinct Digital Transformation in Shipping/Maritime "
            "scholar programme is listed by Fulbright Denmark with "
            "DKK350,000 for four to ten months at a Danish "
            "maritime/shipping institution. The own listing does not "
            "specify a PhD minimum. It belongs to the US scholar "
            "framework; no current closing date is published."
        ),
        "evidence": (
            "actualnamed Digital Transformationin "
            "Shipping/Maritime,350 k DKK 4–10 mo, "
            "Danishmaritime/shippinginstitution. Denmark is a known "
            "host; US citizenship,future no complete current deadline "
            "is published. The own host listing specifies the "
            "programme focus, amount and duration; detailed recipient "
            "qualifications, including any PhD minimum, are "
            "unspecified. Denmark is a positively known nonexclusive "
            "host; the incoming hub also advertises Kingdom "
            "administration for Greenland/Faroe Islands, without "
            "per-award territory/amount guarantees. No extra Danish "
            "job during the grant. Upcoming AY 2028/29 opening spring "
            "2027 has no actual closing date; old AY 2027/28 deadline "
            "passed. Funding remains subject to fiscal revisions."
        ),
    },
    {
        "track": "us-scholar-maritime-research-education-development",
        "key": "hosts",
        "title": "Fulbright Maritime Research, Education and Development",
        "categories": ["fellowships"],
        "hosts": ["DK"],
        "eligible": ["US"],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "The separately named Maritime Research, Education and "
            "Development scholar programme is listed with DKK350,000 "
            "for four to ten months at a Danish maritime/shipping "
            "institution. Its own listing does not state detailed "
            "applicant qualifications. No closing date is published."
        ),
        "evidence": (
            "actualdistinctnamed Maritime Research Education "
            "Development,350 k DKK 4–10 mo, Denmark is a known host; "
            "US citizenship,future no complete current deadline is "
            "published. The publisher separately names this maritime "
            "research, education and development focus. The scholar "
            "hub calls its list three types but separately names five "
            "programmes. Denmark is a positively known nonexclusive "
            "host; the incoming hub also advertises Kingdom "
            "administration for Greenland/Faroe Islands, without "
            "per-award territory/amount guarantees. No extra Danish "
            "job during the grant. Upcoming AY 2028/29 opening spring "
            "2027 has no actual closing date; old AY 2027/28 deadline "
            "passed. Funding remains subject to fiscal revisions."
        ),
    },
    {
        "track": "specialist-institutional-project",
        "key": "specialist",
        "title": "Fulbright Specialist institutional projects in Denmark",
        "categories": ["training"],
        "hosts": ["DK"],
        "eligible": [],
        "deadline": None,
        "closed": False,
        "kind": "institutional-grant",
        "language": "en",
        "summary": (
            "Danish institutions can host US specialists for 14–42 "
            "days, including travel, for workshops, curriculum or "
            "institutional-development projects. Federal support "
            "covers flights, honorarium and limited health coverage; "
            "hosts provide accommodation, meals and local transport. "
            "Named/open matching are application modes, with months of "
            "preparation and final approval required."
        ),
        "evidence": (
            "Kingdom HEI/government/cultural/NGO/medicalpublichealth "
            "institution applies USspecialistproject 14–42 "
            "daysincludingtravel; collaborativecurriculum/workshops/lec"
            "tures/evaluation/needsassessment. "
            "Federalairfare/stipend/limitedhealth, "
            "hostlodging/meals/localtransport costshare. Denmark is a "
            "known host; institutional affiliation (institution "
            "affiliation, not citizenship),no complete current "
            "deadline is published. Named/open modes "
            "NOTseparateawards. Namedlead>=6 mo/4 ifrostered,open 6–9 "
            "mo; one-pageproposal then actualformalapproval "
            "Commission/ECA/FFSB. Multiplevisits combined<=42. "
            "PDFfinalreportcondition for halfhonorariumafter; "
            "hostagreement 10–12 weeks/docs 8–10/flights 2–6; "
            "notapplicationdeadlines. Denmark is a positively known "
            "nonexclusive host; the incoming hub also advertises "
            "Kingdom administration for Greenland/Faroe Islands, "
            "without per-award territory/amount guarantees. Public "
            "named/open timeline PDFs establish conditional final "
            "approvals, health coverage and honorarium split before "
            "departure/after final reports; named lead 6 months or 4 "
            "alreadyrostered, open 6–9 months. Roster entry is not "
            "guaranteed placement."
        ),
    },
    {
        "track": "specialist-individual-roster-assignment",
        "key": "specialist",
        "title": "Fulbright Specialist roster-supported assignments",
        "categories": ["training"],
        "hosts": ["DK"],
        "eligible": ["US"],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "Experienced US academic/professional/artistic specialists "
            "can join a roster for funded 14–42-day international "
            "assignments. Roster approval does not guarantee a match. "
            "Federal support covers airfare, honorarium and limited "
            "health coverage, while hosts share in-country costs. "
            "Denmark is a known destination within the broader "
            "programme; no current closing date is published."
        ),
        "evidence": (
            "USexperiencedacademic/professional/artistic specialist "
            "beneficiary, samefunded 14–42 dayassignment/costscope, "
            "rosteracceptance DOES NOTguarantee placement, Funded "
            "specialist assignments use named or open matching. "
            "host[DK]known NONEXCLUSIVEglobalroster,eligible[US],no "
            "complete current deadline is published. Public timelines "
            "describe the named and open application processes. "
            "Denmark is a positively known nonexclusive host; the "
            "incoming hub also advertises Kingdom administration for "
            "Greenland/Faroe Islands, without per-award "
            "territory/amount guarantees. Public named/open timeline "
            "PDFs establish conditional final approvals, health "
            "coverage and honorarium split before departure/after "
            "final reports; named lead 6 months or 4 alreadyrostered, "
            "open 6–9 months. Roster entry is not guaranteed placement."
        ),
    },
    {
        "track": "intercountry-collaborative-visit",
        "key": "intercountry",
        "title": ("Fulbright Inter-Country collaborative visits to Denmark"),
        "categories": ["grants"],
        "hosts": ["DK"],
        "eligible": ["US"],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "US Fulbright scholars already based in Europe can "
            "collaborate with a Danish host on a visit of about one "
            "week during their existing grant. The commission "
            "reimburses eligible flight/train costs after the visit; "
            "the host covers other expenses. Approval precedes ticket "
            "purchase, with current host commission approval required. "
            "No dated closing is published."
        ),
        "evidence": (
            "already USFulbright Scholarhostedin Europe visits Kingdom "
            "DKHEI up toaboutoneweek duringcurrentgrant. "
            "Scholar+hostcollaborative application, "
            "currenthostcommissionapproval, hostpays "
            "ALLin-countryexpenses; DKcommissionflight/trainreimbursed "
            "AFTERvisitreceipts, ticketbuy ONLYAFTERapproval. Denmark "
            "is a known host; US citizenship,rollingnoactualdatedclosin"
            "g ->no complete current deadline is published. "
            "Danish-hosted scholars visiting other European countries "
            "depend on the destination commission's separate offer; "
            "approximately 200 Europe-based US scholars describes the "
            "cohort, not separate award types. "
            "Signeddataprotectionproof, the application requires "
            "supporting documentation. Denmark is a positively known "
            "nonexclusive host; the incoming hub also advertises "
            "Kingdom administration for Greenland/Faroe Islands, "
            "without per-award territory/amount guarantees."
        ),
    },
    {
        "track": "book-scholar-institutional-lecture",
        "key": "book-scholar",
        "title": ("Book a Fulbright scholar — institutional lecture programme"),
        "categories": ["training"],
        "hosts": ["DK"],
        "eligible": [],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "A Danish institution can request a free one-to-two-hour "
            "lecture by a visiting Fulbright researcher, with speaker "
            "transport funded by the commission. Request at least four "
            "weeks ahead; the host provides the venue, promotion and "
            "suitable event arrangements. This is an institutional "
            "educational service, not a cash scholarship or three "
            "separate speaker awards."
        ),
        "evidence": (
            "ONE free 1–2 hkeynote/lectureservice for "
            "Danishinstitution, commissionfundsspeakertransport, "
            "hostvenue/promotion/cateringasappropriate,request>=4 "
            "WEEKSbefore. Denmark is a known host; institutional "
            "affiliationinstitutions,yearless no complete current "
            "deadline is published. The institutional educational "
            "service provides travel support rather than an individual "
            "cash award. Speakers rotate; current biographies do not "
            "define separate funding programmes. This is one "
            "institutional educational service, with no individual "
            "cash scholarship."
        ),
    },
    {
        "track": "arctic-initiative-iii-2021",
        "key": "arctic-iii",
        "title": "Fulbright Arctic Initiative III — historical 2021–22",
        "categories": ["fellowships", "training"],
        "hosts": ["CA", "US"],
        "eligible": ["DK", "GL", "FO"],
        "deadline": None,
        "closed": True,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Historical closed 18-month Arctic research collaboration, "
            "spring2021–fall2022, for citizens and residents of "
            "Denmark, Greenland or the Faroe Islands. USD40,000 total "
            "supports programme/exchange travel, maintenance and "
            "research, with group meals/accommodation separate. "
            "Individual exchanges last six weeks to three months. No "
            "Danish closing date is stated."
        ),
        "evidence": (
            "Citizenship AND residence in Denmark, Greenland or the "
            "Faroe Islands required. Established/early-career "
            "researchers, Indigenous/local knowledge holders and "
            "arts/professional practitioners need outstanding related "
            "accomplishments, active thematic inquiry, English and "
            "interdisciplinary collaboration. One framework covers "
            "security/cooperation, infrastructure/environment and "
            "community health. USD40,000 is total, for the grantee "
            "only; group accommodation/meals separately covered and "
            "limited accident/sickness benefits plus adequate "
            "additional liability/repatriation insurance required. "
            "Published opening Canada meeting and final Washington DC "
            "meeting are known nonexclusive hosts; Norway meeting is "
            "TBC only. The individual-exchange destination list is not "
            "exhaustive. Eighteen months spring2021–fall2022, "
            "individual exchange six weeks–three months. Explicit "
            "competition closed; September15,2020 is US Scholar "
            "deadline and does not establish Danish closing. English "
            "statement3–5pages, bibliography1–3, CV<=6, two "
            "recommender-uploaded letters, passport; host invitation "
            "recommended, not required. Group policy brief/research "
            "product and individual one-page outcomes required. "
            "J1/two-year physical-presence restrictions affect certain "
            "work/immigration visas, not conference/tourist travel. "
            "This own historical III evidence is independent of "
            "excluded current IV."
        ),
    },
    {
        "track": "inger-danish-student",
        "key": "frontier-1",
        "title": (
            "Inger P. Davis Danish student grants — historical " "framework"
        ),
        "categories": ["scholarships"],
        "hosts": ["US"],
        "eligible": ["DK"],
        "deadline": None,
        "closed": True,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Named social-sciences study/research support focused on "
            "children at risk and their families. Danish students "
            "receive DKK50,000 total for one semester or DKK100,000 "
            "for two. Danish core eligibility and application "
            "requirements apply. The page’s Danish2023–24 competition "
            "has passed; no current closing date is published."
        ),
        "evidence": (
            "Inger P. Davis social-sciences study/research improves "
            "children at risk and families, welcoming "
            "interdisciplinary work. Page historical AY2023/24 Danish "
            "competition explicitly passed; old2024/25 announcement is "
            "not a current deadline. Applications to other relevant "
            "categories are possible without guaranteed combined "
            "awards. Danish core eligibility applies: Danish passport "
            "mandatory, US dual citizenship and green cards excluded; "
            "affiliation/address in the Kingdom preferred with stated "
            "close-ties/full-degree exceptions. Master’s/PhD degree or "
            "nondegree study, excluding undergraduate, LLM, MBA, "
            "internship, conference and summer-school routes; no "
            "already-started US study. General tuition-free exchanges "
            "excluded except documented PhD/Arts cases. PhD degree "
            "cannot be completed during a student grant; if obtained "
            "before departure, the scholar competition applies. Host "
            "application/correspondence required, final admission "
            "conditional; PhD home academic/financial support and "
            "conditional portfolio evidence. English "
            "essays/CV/transcripts/two timely "
            "recommendations/passport/budget required. "
            "Research/medical work cannot involve clinical/direct "
            "patient or animal contact. Funding remains partial and "
            "only for the initial year; no automatic renewal or "
            "dependent award. Additional adequate insurance beyond "
            "minimal ASPE, documented guaranteed J1 funding and "
            "conditional family visas apply; visa-cost examples are "
            "not grant benefits. Named social-sciences study/research "
            "support focused on children at risk and their families. "
            "Danish students receive DKK50,000 total for one semester "
            "or DKK100,000 for two. Danish core eligibility and "
            "application requirements apply. The page’s Danish2023–24 "
            "competition has passed; no current closing date is "
            "published."
        ),
    },
    {
        "track": "inger-danish-scholar",
        "key": "frontier-1",
        "title": (
            "Inger P. Davis Danish scholar grants — historical " "framework"
        ),
        "categories": ["fellowships"],
        "hosts": ["US"],
        "eligible": ["DK"],
        "deadline": None,
        "closed": True,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Named US social-sciences study/research support for "
            "Danish scholars working on children at risk and families, "
            "with interdisciplinary approaches welcomed. Funding is "
            "DKK200,000 total for one or two semesters. Danish core "
            "eligibility and process apply. The published "
            "Danish2023–24 round is closed; no current deadline is "
            "stated."
        ),
        "evidence": (
            "Inger P. Davis social-sciences study/research improves "
            "children at risk and families, welcoming "
            "interdisciplinary work. Page historical AY2023/24 Danish "
            "competition explicitly passed; old2024/25 announcement is "
            "not a current deadline. Applications to other relevant "
            "categories are possible without guaranteed combined "
            "awards. Danish core scholar eligibility applies: Danish "
            "passport, no US dual citizenship or green card; relevant "
            "assistant/associate/full-professor higher-education "
            "employment, with postdocs exempt employment but required "
            "to reside in Denmark. The core scholar programme "
            "expressly requires both research and teaching, high "
            "English proficiency and a US host invitation; no "
            "already-started research, conference/group/summer-school "
            "award. At-large postdoc affiliation can be finalized "
            "after application but before grant. "
            "CV/project/bibliography/syllabus/two "
            "recommendations/passport/support documentation apply; "
            "home employment support letter not required for postdocs. "
            "Medical research excludes clinical/direct patient and "
            "animal contact; no dependent cash award or blanket "
            "universal work visa guarantee. Additional adequate "
            "insurance beyond minimal ASPE, documented guaranteed J1 "
            "funding and conditional family visas apply; visa-cost "
            "examples are not grant benefits. Named US social-sciences "
            "study/research support for Danish scholars working on "
            "children at risk and families, with interdisciplinary "
            "approaches welcomed. Funding is DKK200,000 total for one "
            "or two semesters. Danish core eligibility and process "
            "apply. The published Danish2023–24 round is closed; no "
            "current deadline is stated."
        ),
    },
    {
        "track": "inger-us-student",
        "key": "frontier-1",
        "title": (
            "Inger P. Davis US student grants — historical page " "overview"
        ),
        "categories": ["scholarships"],
        "hosts": ["DK"],
        "eligible": ["US"],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "Named Danish social-sciences study/research support for "
            "American students, focused on improving the lives of "
            "children at risk and families: DKK125,000 total for two "
            "semesters. The application process follows the US core "
            "process. The page refers to2023–24 and delegates US "
            "deadlines elsewhere; no current closing date or opening "
            "is established."
        ),
        "evidence": (
            "Inger P. Davis grant supports social-sciences projects "
            "relevant to improving children at risk and their "
            "families; interdisciplinary applications welcomed. Four "
            "recipient roles have separately stated benefit scopes. "
            "Applicants may also apply to other relevant Fulbright "
            "categories, without guaranteed combined awards. Page "
            "labels AY2023/24; only Danish deadline explicitly passed, "
            "and old upcoming2024/25 announcement is not a current "
            "call. American core application PROCESS applies; the page "
            "does not expressly transfer every eligibility "
            "requirement. US closing dates are referred externally "
            "without a complete date, so opening/deadline remain "
            "unknown. Benefit for this role: Named Danish "
            "social-sciences study/research support for American "
            "students, focused on improving the lives of children at "
            "risk and families: DKK125,000 total for two semesters. "
            "The application process follows the US core process. The "
            "page refers to2023–24 and delegates US deadlines "
            "elsewhere; no current closing date or opening is "
            "established."
        ),
    },
    {
        "track": "inger-us-scholar",
        "key": "frontier-1",
        "title": (
            "Inger P. Davis US scholar grants — historical page " "overview"
        ),
        "categories": ["fellowships"],
        "hosts": ["DK"],
        "eligible": ["US"],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "Named Danish social-sciences study/research support for "
            "American scholars focused on children at risk and "
            "families: DKK200,000 total for one semester. "
            "Interdisciplinary work is welcomed, using the US core "
            "application process. The historical page does not state a "
            "current US closing date or prove that all current core "
            "eligibility conditions apply."
        ),
        "evidence": (
            "Inger P. Davis grant supports social-sciences projects "
            "relevant to improving children at risk and their "
            "families; interdisciplinary applications welcomed. Four "
            "recipient roles have separately stated benefit scopes. "
            "Applicants may also apply to other relevant Fulbright "
            "categories, without guaranteed combined awards. Page "
            "labels AY2023/24; only Danish deadline explicitly passed, "
            "and old upcoming2024/25 announcement is not a current "
            "call. American core application PROCESS applies; the page "
            "does not expressly transfer every eligibility "
            "requirement. US closing dates are referred externally "
            "without a complete date, so opening/deadline remain "
            "unknown. Benefit for this role: Named Danish "
            "social-sciences study/research support for American "
            "scholars focused on children at risk and families: "
            "DKK200,000 total for one semester. Interdisciplinary work "
            "is welcomed, using the US core application process. The "
            "historical page does not state a current US closing date "
            "or prove that all current core eligibility conditions "
            "apply."
        ),
    },
    {
        "track": "schuman-incoming-pre-doctoral",
        "key": "frontier-4",
        "title": "Fulbright Schuman US pre-doctoral research in the EU",
        "categories": ["fellowships"],
        "hosts": [],
        "eligible": ["US"],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "US students undertake pre-doctoral research in the EU on "
            "US–EU relations, EU institutions or EU policy. One year "
            "is preferred; support is EUR2,000 per month plus EUR2,000 "
            "one-time travel. The own page publishes September16 "
            "without a year and an opening from February, so the "
            "current deadline and status are unknown."
        ),
        "evidence": (
            "Fulbright Belgium administers US-citizen/student/professio"
            "nal grants to the European Union, exclusively addressing "
            "US–EU relations, EU institutions or EU policy. Student "
            "one-year and scholar six-to-nine-month durations are "
            "preferences, not hard minimums. Monthly student EUR2,000 "
            "/ scholar EUR3,000 plus one-time EUR2,000 travel. "
            "September16 closing and February opening have no year or "
            "time; no passport whitelist of EU hosts, exact country "
            "guarantee or additional educator type is stated. "
            "Research/lecturing is the published named scholar track, "
            "not an explicit both-components requirement."
        ),
    },
    {
        "track": "schuman-incoming-postdoctoral",
        "key": "frontier-4",
        "title": (
            "Fulbright Schuman US postdoctoral research/lecturing in " "the EU"
        ),
        "categories": ["fellowships"],
        "hosts": [],
        "eligible": ["US"],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "US scholars undertake the named postdoctoral "
            "research/lecturing track in the EU on US–EU relations, EU "
            "institutions or EU policy. Six to nine months are "
            "preferred; support is EUR3,000 monthly plus EUR2,000 "
            "one-time travel. September16 has no year or clock; no "
            "current deadline is inferred."
        ),
        "evidence": (
            "Fulbright Belgium administers US-citizen/student/professio"
            "nal grants to the European Union, exclusively addressing "
            "US–EU relations, EU institutions or EU policy. Student "
            "one-year and scholar six-to-nine-month durations are "
            "preferences, not hard minimums. Monthly student EUR2,000 "
            "/ scholar EUR3,000 plus one-time EUR2,000 travel. "
            "September16 closing and February opening have no year or "
            "time; no passport whitelist of EU hosts, exact country "
            "guarantee or additional educator type is stated. "
            "Research/lecturing is the published named scholar track, "
            "not an explicit both-components requirement."
        ),
    },
]

PDFS = {
    "faq": (
        (
            "https://fulbrightcenter.dk/wp-content/uploads/2026/02/Fina"
            "l_Frequently-Asked-Questions_Danes-going-to-the-US.pdf"
        ),
        ("e9d0e39ee9d7a27ab30cdf1812e6ef7ad9548de68b4884e74c051275b0" "330853"),
    ),
    "specialist-named-timeline": (
        (
            "https://fulbrightcenter.dk/wp-content/uploads/2024/10/Name"
            "d-Projects-PDF.pdf"
        ),
        ("9bedde239eae580232e2af2f053795106039b7fbe9e097684dc99f9c00" "122ae3"),
    ),
    "specialist-open-timeline": (
        (
            "https://fulbrightcenter.dk/wp-content/uploads/2024/10/Open"
            "-Projects-PDF.pdf"
        ),
        ("cc51421efcbc3536a5b5fcb1da9464652bc2d6c8e0e664c868305a1c11" "49b962"),
    ),
}

SITEMAPS = {
    "sitemap-index": (
        "https://fulbrightcenter.dk/sitemap.xml",
        "sitemapindex",
        [
            "https://fulbrightcenter.dk/category-sitemap.xml",
            "https://fulbrightcenter.dk/page-sitemap.xml",
            ("https://fulbrightcenter.dk/page_category-sitemap.xml"),
            "https://fulbrightcenter.dk/post-sitemap.xml",
            "https://fulbrightcenter.dk/post_tag-sitemap.xml",
        ],
    ),
    "sitemap-pages": (
        "https://fulbrightcenter.dk/page-sitemap.xml",
        "urlset",
        [
            "https://fulbrightcenter.dk/",
            ("https://fulbrightcenter.dk/about-fulbright-program/"),
            (
                "https://fulbrightcenter.dk/about-fulbright-program/alu"
                "mni-impact-study/"
            ),
            (
                "https://fulbrightcenter.dk/about-fulbright-program/boa"
                "rd-members/"
            ),
            (
                "https://fulbrightcenter.dk/about-fulbright-program/cur"
                "rent-status/"
            ),
            (
                "https://fulbrightcenter.dk/about-fulbright-program/dat"
                "a-protection/"
            ),
            ("https://fulbrightcenter.dk/about-fulbright-program/par" "tners/"),
            "https://fulbrightcenter.dk/advising/",
            (
                "https://fulbrightcenter.dk/advising/advising-postdocs-"
                "scholars/"
            ),
            ("https://fulbrightcenter.dk/advising/broad-opportunitie" "s/"),
            (
                "https://fulbrightcenter.dk/advising/cost-of-living-in-"
                "denmark/"
            ),
            "https://fulbrightcenter.dk/advising/financing/",
            ("https://fulbrightcenter.dk/advising/graduate-advising/"),
            (
                "https://fulbrightcenter.dk/advising/graduate-advising/"
                "to-do-list-for-master-and-phd-studies-in-usa/"
            ),
            ("https://fulbrightcenter.dk/advising/housing-in-the-us/"),
            (
                "https://fulbrightcenter.dk/advising/how-do-we-choose-o"
                "ur-candidates/"
            ),
            (
                "https://fulbrightcenter.dk/advising/training-internshi"
                "ps-in-usa/"
            ),
            ("https://fulbrightcenter.dk/advising/travelling-with-fa" "mily/"),
            ("https://fulbrightcenter.dk/advising/u-s-grading-system" "s/"),
            "https://fulbrightcenter.dk/advising/u-s-tests/",
            "https://fulbrightcenter.dk/alumni/",
            ("https://fulbrightcenter.dk/book-en-fulbright-forsker/"),
            "https://fulbrightcenter.dk/contact/",
            "https://fulbrightcenter.dk/data-protection/",
            "https://fulbrightcenter.dk/go-to-the-us/",
            (
                "https://fulbrightcenter.dk/go-to-the-us/crown-prince-f"
                "rederik-fund-2/"
            ),
            (
                "https://fulbrightcenter.dk/go-to-the-us/crown-prince-f"
                "rederik-fund-2/alumni/"
            ),
            (
                "https://fulbrightcenter.dk/go-to-the-us/fulbright-arct"
                "ic-initiative-2024-2026/"
            ),
            ("https://fulbrightcenter.dk/go-to-the-us/fulbright-co-f" "unded/"),
            (
                "https://fulbrightcenter.dk/go-to-the-us/fulbright-denm"
                "ark-inger-p-davis-grant-in-social-sciences/"
            ),
            (
                "https://fulbrightcenter.dk/go-to-the-us/fulbright-gran"
                "ts-for-danish-students/"
            ),
            (
                "https://fulbrightcenter.dk/go-to-the-us/fulbright-gran"
                "ts-for-danish-students/apply/"
            ),
            ("https://fulbrightcenter.dk/go-to-the-us/fulbright-schu" "man/"),
            ("https://fulbrightcenter.dk/go-to-the-us/grants-for-sch" "olars/"),
            (
                "https://fulbrightcenter.dk/go-to-the-us/grants-for-sch"
                "olars/apply/"
            ),
            ("https://fulbrightcenter.dk/go-to-the-us/other-grants/"),
            "https://fulbrightcenter.dk/go-to-the-us/students/",
            (
                "https://fulbrightcenter.dk/go-to-the-us/students/fulbr"
                "ight-joint-grants/"
            ),
            (
                "https://fulbrightcenter.dk/go-to-the-us/thanks-to-scan"
                "dinavia/"
            ),
            "https://fulbrightcenter.dk/grantsforamericans/",
            (
                "https://fulbrightcenter.dk/grantsforamericans/fulbrigh"
                "t-for-scholars/"
            ),
            (
                "https://fulbrightcenter.dk/grantsforamericans/fulbrigh"
                "t-for-scholars/fulbright-distinguished-chair-in-americ"
                "an-studies/"
            ),
            (
                "https://fulbrightcenter.dk/grantsforamericans/fulbrigh"
                "t-for-scholars/fulbright-schuman/"
            ),
            (
                "https://fulbrightcenter.dk/grantsforamericans/fulbrigh"
                "t-for-scholars/scholar-grants-all-disciplines/"
            ),
            (
                "https://fulbrightcenter.dk/grantsforamericans/fulbrigh"
                "t-for-scholars/scholar-grants-community-college/"
            ),
            (
                "https://fulbrightcenter.dk/grantsforamericans/fulbrigh"
                "t-for-students/"
            ),
            (
                "https://fulbrightcenter.dk/grantsforamericans/fulbrigh"
                "t-for-students/current-dk-fulbrighters/"
            ),
            (
                "https://fulbrightcenter.dk/grantsforamericans/fulbrigh"
                "t-for-students/current-us-fulbrighters/"
            ),
            (
                "https://fulbrightcenter.dk/grantsforamericans/fulbrigh"
                "t-inter-country-travel-grant/"
            ),
            (
                "https://fulbrightcenter.dk/grantsforamericans/fulbrigh"
                "t-inter-country-travel-grant/apply/"
            ),
            (
                "https://fulbrightcenter.dk/grantsforamericans/fulbrigh"
                "t-specialist-grant-program/"
            ),
            "https://fulbrightcenter.dk/hosts/",
            "https://fulbrightcenter.dk/returning-home/",
            "https://fulbrightcenter.dk/scholars/",
            ("https://fulbrightcenter.dk/scholars/arctic-initiative-" "iii/"),
            (
                "https://fulbrightcenter.dk/scholars/arctic-initiative-"
                "iii/apply/"
            ),
            "https://fulbrightcenter.dk/shop/",
            "https://fulbrightcenter.dk/stories/",
            "https://fulbrightcenter.dk/usa-legater/",
            "https://fulbrightcenter.dk/usa-vejledning/",
            (
                "https://fulbrightcenter.dk/webinar-for-legatsogning-16"
                "-november/"
            ),
            (
                "https://fulbrightcenter.dk/webinar-om-legatsogning-d-9"
                "-november-2022/"
            ),
            (
                "https://fulbrightcenter.dk/webinar-om-legatsogning-tir"
                "sdag-d-24-januar-2023/"
            ),
        ],
    ),
    "sitemap-categories": (
        "https://fulbrightcenter.dk/page_category-sitemap.xml",
        "urlset",
        [
            ("https://fulbrightcenter.dk/stories/page_category/advis" "ing/"),
            (
                "https://fulbrightcenter.dk/stories/page_category/for-d"
                "k-citizens/"
            ),
            ("https://fulbrightcenter.dk/stories/page_category/gener" "al/"),
            (
                "https://fulbrightcenter.dk/stories/page_category/grant"
                "s-for-american-citizens/"
            ),
            (
                "https://fulbrightcenter.dk/stories/page_category/grant"
                "s-for-scholars-us-citizenship/"
            ),
            (
                "https://fulbrightcenter.dk/stories/page_category/other"
                "-grants-for-danish-citizens/"
            ),
        ],
    ),
}


def sitemap_locations(document, kind):
    if "<!DOCTYPE" in document.upper() or "<!ENTITY" in document.upper():
        raise AdapterError("Unsupported sitemap entities", "parse")
    try:
        root = ET.fromstring(document)
    except ET.ParseError as error:
        raise AdapterError("Incomplete publisher sitemap", "parse") from error
    namespace = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    if root.tag != namespace + kind:
        raise AdapterError("Unexpected sitemap type", "parse")
    entry = "sitemap" if kind == "sitemapindex" else "url"
    locations = []
    for node in root:
        if node.tag != namespace + entry:
            raise AdapterError("Unexpected sitemap entry", "parse")
        loc = one(
            [n for n in node if n.tag == namespace + "loc"], "sitemap location"
        )
        locations.append(reviewed_url((loc.text or "").strip()))
    if not locations or len(locations) != len(set(locations)):
        raise AdapterError("Empty or duplicate sitemap", "parse")
    return sorted(locations)


def read_pages():
    result = {key: page(url) for key, url in PAGES.items()}
    for key, (url, expected) in PDFS.items():
        result[key] = read_public_pdf(url, expected)
    for key, (url, kind, expected) in SITEMAPS.items():
        actual = sitemap_locations(fetch_source(url, check_robots(url)), kind)
        if actual != expected:
            raise AdapterError(
                "Source sitemap frontier changed: " + key, "parse"
            )
        result[key] = actual
    return result


def parse_inventory(pages):
    if set(pages) != set(PAGES) | set(PDFS) | set(SITEMAPS):
        raise AdapterError("Incomplete source evidence", "parse")
    for key in PAGES:
        root = pages[key]
        digest = hashlib.sha256(source_facts(key, root).encode()).hexdigest()
        if digest != CONDITIONS[key]:
            raise AdapterError(
                "Source facts/inventory changed: " + key, "parse"
            )
    for key, (_, expected) in PDFS.items():
        if pages[key] != expected:
            raise AdapterError("Missing reviewed public PDF evidence", "parse")
    for key, (_, _, expected) in SITEMAPS.items():
        if pages[key] != expected:
            raise AdapterError("Missing reviewed sitemap frontier", "parse")
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
        record["eligible_countries"] = profile["eligible"]
        record["language"] = profile["language"]
        record["deadline"] = profile["deadline"]
        if profile["deadline"]:
            now = utc_now()
            record["status"] = (
                "open" if now < profile["deadline"] else "expired"
            )
        elif profile["closed"]:
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
                f"Collected {len(records)} validated Fulbright Denmark "
                "funding and educational programmes"
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
