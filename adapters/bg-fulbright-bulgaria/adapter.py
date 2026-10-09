"""Collect reviewed Fulbright Bulgaria awards and actual training editions."""

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

SOURCE_ID = "bg-fulbright-bulgaria"
SOURCE_URL = "https://www.fulbright.bg/en/research-grants-for-bulgarians/"
WEBSITE_URL = "https://www.fulbright.bg/"
LANGUAGE = "en"
PUBLISHER_COUNTRY = "BG"
PUBLISHER_TYPE = "government"
ATTRIBUTION = (
    "Fulbright Bulgaria — Bulgarian-American Commission for "
    "Educational Exchange"
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
    document = fetch_source(url, min_interval=check_robots(url))
    if not re.search(
        r"</body\s*>\s*</html\s*>\s*(?:<!--[\s\S]*?-->\s*)*$",
        document,
        re.IGNORECASE,
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
    return one(
        [n for n in nodes(root, "div") if n.attrs.get("id") == "main-content"],
        "complete public Fulbright Bulgaria article",
    )


def reviewed_url(value):
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower()
        not in ("fulbright.bg", "www.fulbright.bg")
        or parsed.username
        or parsed.password
        or parsed.port
        or (parsed.query and parsed.query != "et_blog")
    ):
        raise AdapterError("Unexpected source URL", "parse")
    return urllib.parse.urlunsplit(
        ("https", "www.fulbright.bg", parsed.path or "/", parsed.query, "")
    )


def substantive_text(key, root):
    """Keep programme facts while excluding reviewed personal presentation."""
    content = article(root)
    parts = []
    selected = []
    for node in nodes(content, "div"):
        classes = node.attrs.get("class", "").split()
        if "et_pb_module" not in classes:
            continue
        if any(
            c in classes
            for c in (
                "et_pb_team_member",
                "et_pb_testimonial",
                "et_pb_gallery",
                "et_pb_image",
                "et_pb_video",
                "et_pb_code",
            )
        ):
            continue
        if any(c in classes for c in EXCLUDED_MODULES.get(key, [])):
            continue
        if key == "court" and any(
            c in classes for c in ("et_pb_accordion", "et_pb_accordion_item")
        ):
            # These ten accordions are personal internship testimonials.
            continue
        if key == "eta" and "et_pb_slider" in classes:
            continue
        if any(node in list(parent.walk())[1:] for parent in selected):
            continue
        text = node.text()
        if text:
            parts.append(text)
            selected.append(node)

    def outside_modules(node):
        if node.tag in ("script", "style", "noscript") or (
            "et_pb_module" in node.attrs.get("class", "").split()
        ):
            return []
        result = []
        for child in node.children:
            if isinstance(child, Node):
                result.extend(outside_modules(child))
            elif child.strip():
                result.append(child)
        return result

    parts.extend(outside_modules(content))
    if key in ("news", "conferences", "conference2"):
        # Discovery card headings identify the finite frontier; author names
        # and excerpts are impact-story presentation, not funding conditions.
        parts = [
            n.text() for n in nodes(content) if n.tag in ("h1", "h2", "h3")
        ]
    if not parts:
        raise AdapterError("Missing substantive source modules", "parse")
    return " ".join(parts).replace("\xad", "").replace("\u200b", "")


def source_facts(key, root):
    observed = [
        n.attrs.get("href", "")
        for n in nodes(root, "link")
        if n.attrs.get("rel") == "canonical"
    ]
    if observed != CANONICALS[key]:
        raise AdapterError("Publisher canonical changed: " + key, "parse")
    routes, policies = set(), set()
    personal_anchors = set()
    for node in nodes(article(root), "div"):
        classes = node.attrs.get("class", "").split()
        if (
            any(
                c in classes
                for c in (
                    "et_pb_team_member",
                    "et_pb_testimonial",
                    "et_pb_gallery",
                    "et_pb_image",
                    "et_pb_video",
                )
            )
            or any(c in classes for c in EXCLUDED_MODULES.get(key, []))
            or (
                key == "court"
                and any(
                    c in classes
                    for c in ("et_pb_accordion", "et_pb_accordion_item")
                )
            )
        ):
            personal_anchors.update(nodes(node, "a"))
    for anchor in nodes(root, "a"):
        if anchor in personal_anchors:
            continue
        href = anchor.attrs.get("href", "")
        if not href or href.startswith(("mailto:", "tel:")):
            continue
        absolute = urllib.parse.urljoin(PAGES[key], href)
        parsed = urllib.parse.urlsplit(absolute)
        label = anchor.text().lower()
        if re.search(
            r"privacy|terms|copyright|licen[cs]e|политика|условия", label
        ):
            policies.add(absolute)
        if (parsed.hostname or "").lower() in (
            "fulbright.bg",
            "www.fulbright.bg",
        ):
            if parsed.path.startswith(("/wp-content/", "/wp-includes/")):
                # Media is not requested; robots remains independently checked.
                continue
            routes.add(reviewed_url(absolute))
    # Footer privacy anchors sometimes have no visible label.
    for route in routes:
        if "privacy" in route or "terms" in route:
            policies.add(route)
    footer = one(
        [
            n
            for n in nodes(root, "div")
            if FOOTER_MODULES[key] in n.attrs.get("class", "").split()
        ],
        "reviewed publisher legal footer",
    ).text()
    # Only the copyright end year is presentation; new legal wording is not.
    footer = re.sub(r"©\s*1993[-–]\d{4}", "© 1993-CURRENT_YEAR", footer)
    notices = []
    for section in nodes(root, "div"):
        if not any(
            "_tb_footer" in c and "section" in c
            for c in section.attrs.get("class", "").split()
        ):
            continue
        for paragraph in nodes(section, "p"):
            if re.search(
                r"re.?publi|permission|copyright|rights reserved|licen[cs]e"
                r"|reuse|reproduction|разрешение|авторск",
                paragraph.text(),
                re.IGNORECASE,
            ):
                notices.append(paragraph.text())
    return json.dumps(
        {
            "legal_footer": footer,
            "legal_notices": sorted(set(notices)),
            "text": substantive_text(key, root),
            "routes": sorted(routes),
            "policies": sorted(policies),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


FOOTER_MODULES = {
    "agile": "et_pb_text_9_tb_footer",
    "alumni": "et_pb_text_9_tb_footer",
    "conference2": "et_pb_text_9_tb_footer",
    "conferences": "et_pb_text_9_tb_footer",
    "court": "et_pb_text_9_tb_footer",
    "digital2020": "et_pb_text_9_tb_footer",
    "doctoral": "et_pb_text_9_tb_footer",
    "entry": "et_pb_text_9_tb_footer",
    "eta-host": "et_pb_text_9_tb_footer",
    "eta": "et_pb_text_9_tb_footer",
    "fis2019": "et_pb_text_9_tb_footer",
    "fis2021": "et_pb_text_9_tb_footer",
    "fis2022-review": "et_pb_text_9_tb_footer",
    "fis2022": "et_pb_text_9_tb_footer",
    "ftea": "et_pb_text_9_tb_footer",
    "graduate-bg": "et_pb_text_9_tb_footer",
    "graduate-us": "et_pb_text_9_tb_footer",
    "home": "et_pb_text_9_tb_footer",
    "humphrey": "et_pb_text_9_tb_footer",
    "incoming": "et_pb_text_9_tb_footer",
    "info-sessions": "et_pb_text_9_tb_footer",
    "journalists": "et_pb_text_9_tb_footer",
    "law": "et_pb_text_9_tb_footer",
    "leaders": "et_pb_text_9_tb_footer",
    "math": "et_pb_text_9_tb_footer",
    "mba-alias": "et_pb_text_9_tb_footer",
    "media2023": "et_pb_text_9_tb_footer",
    "news": "et_pb_text_9_tb_footer",
    "ngo": "et_pb_text_9_tb_footer",
    "open2023": "et_pb_text_9_tb_footer",
    "open2024": "et_pb_text_9_tb_footer",
    "other": "et_pb_text_9_tb_footer",
    "outgoing": "et_pb_text_9_tb_footer",
    "pmp": "et_pb_text_9_tb_footer",
    "privacy": "et_pb_text_9_tb_footer",
    "scholar-us": "et_pb_text_9_tb_footer",
    "scholar": "et_pb_text_9_tb_footer",
    "schuman": "et_pb_text_9_tb_footer",
    "specialist": "et_pb_text_9_tb_footer",
    "thanks": "et_pb_text_9_tb_footer",
}

PAGES = {
    "agile": (
        "https://www.fulbright.bg/en/agile-certified-practitioner-pmi-a"
        "cp-training/"
    ),
    "alumni": "https://www.fulbright.bg/en/alumni-grants/",
    "conference2": (
        "https://www.fulbright.bg/en/conferences-events/page/2/?et_blog"
    ),
    "conferences": "https://www.fulbright.bg/en/conferences-events/",
    "court": "https://www.fulbright.bg/sofijski-rajonen-sad/",
    "digital2020": (
        "https://www.fulbright.bg/en/digital-presence-online-conference" "/"
    ),
    "doctoral": (
        "https://www.fulbright.bg/en/non-degree-research-programs-bg/"
    ),
    "entry": ("https://www.fulbright.bg/en/research-grants-for-bulgarians/"),
    "eta-host": ("https://www.fulbright.bg/programa-za-uchiteli-po-anglijski/"),
    "eta": ("https://www.fulbright.bg/en/english-teaching-assistantship/"),
    "fis2019": (
        "https://www.fulbright.bg/en/fulbright-international-seminar-20" "19/"
    ),
    "fis2021": (
        "https://www.fulbright.bg/en/fulbright-international-seminar-20" "21/"
    ),
    "fis2022-review": "https://www.fulbright.bg/en/fis-2022-round-up/",
    "fis2022": (
        "https://www.fulbright.bg/en/fulbright-international-seminar-20" "22/"
    ),
    "ftea": "https://www.fulbright.bg/en/grants-for-teachers/",
    "graduate-bg": "https://www.fulbright.bg/en/graduate-students-bg/",
    "graduate-us": "https://www.fulbright.bg/en/graduate-students-usa-3/",
    "home": "https://www.fulbright.bg/en/",
    "humphrey": "https://www.fulbright.bg/en/hubert-humphrey/",
    "incoming": "https://www.fulbright.bg/en/grants-for-us-citizens/",
    "info-sessions": (
        "https://www.fulbright.bg/en/info-sessions-scholar-phd-grants/"
    ),
    "journalists": "https://www.fulbright.bg/zhurnalisti/",
    "law": "https://www.fulbright.bg/en/pravo/",
    "leaders": "https://www.fulbright.bg/en/fulbright-bulgarian-leaders/",
    "math": "https://www.fulbright.bg/en/math-scholars/",
    "mba-alias": "https://www.fulbright.bg/en/biznes-administraciq/",
    "media2023": "https://www.fulbright.bg/en/eta-medialit-seminar-2023/",
    "news": "https://www.fulbright.bg/en/news-and-events/",
    "ngo": "https://www.fulbright.bg/npo-research/",
    "open2023": "https://www.fulbright.bg/en/open-lectures-2023/",
    "open2024": "https://www.fulbright.bg/en/open-lectures-2024/",
    "other": "https://www.fulbright.bg/en/other/",
    "outgoing": "https://www.fulbright.bg/en/grants-for-bulgarian-citizens/",
    "pmp": (
        "https://www.fulbright.bg/en/project-management-professional-pm"
        "p-capm/"
    ),
    "privacy": "https://www.fulbright.bg/en/privacy-policy/",
    "scholar-us": (
        "https://www.fulbright.bg/en/grants-for-us-scholars-and-profess"
        "ionals/"
    ),
    "scholar": (
        "https://www.fulbright.bg/en/visiting-scholar-grants-research/"
    ),
    "schuman": "https://www.fulbright.bg/en/fulbright-schuman/",
    "specialist": "https://www.fulbright.bg/en/fulbright-specialist-program/",
    "thanks": "https://www.fulbright.bg/en/thanks-to-scandinavia/",
}

CANONICALS = {
    "agile": [
        (
            "https://www.fulbright.bg/en/agile-certified-practitioner-p"
            "mi-acp-training/"
        ),
    ],
    "alumni": ["https://www.fulbright.bg/en/alumni-grants/"],
    "conference2": ["https://www.fulbright.bg/en/conferences-events/"],
    "conferences": ["https://www.fulbright.bg/en/conferences-events/"],
    "court": ["https://www.fulbright.bg/sofijski-rajonen-sad/"],
    "digital2020": [
        ("https://www.fulbright.bg/en/digital-presence-online-confer" "ence/"),
    ],
    "doctoral": [
        "https://www.fulbright.bg/en/non-degree-research-programs-bg/"
    ],
    "entry": ["https://www.fulbright.bg/en/research-grants-for-bulgarians/"],
    "eta-host": ["https://www.fulbright.bg/programa-za-uchiteli-po-anglijski/"],
    "eta": ["https://www.fulbright.bg/en/english-teaching-assistantship/"],
    "fis2019": [],
    "fis2021": [
        (
            "https://www.fulbright.bg/en/fulbright-international-semina"
            "r-2021/"
        ),
    ],
    "fis2022-review": ["https://www.fulbright.bg/en/fis-2022-round-up/"],
    "fis2022": [
        (
            "https://www.fulbright.bg/en/fulbright-international-semina"
            "r-2022/"
        ),
    ],
    "ftea": ["https://www.fulbright.bg/en/grants-for-teachers/"],
    "graduate-bg": ["https://www.fulbright.bg/en/graduate-students-bg/"],
    "graduate-us": ["https://www.fulbright.bg/en/graduate-students-usa-3/"],
    "home": ["https://www.fulbright.bg/en"],
    "humphrey": ["https://fulbright.bg/en"],
    "incoming": ["https://www.fulbright.bg/en/grants-for-us-citizens/"],
    "info-sessions": [
        "https://www.fulbright.bg/en/info-sessions-scholar-phd-grants/"
    ],
    "journalists": ["https://www.fulbright.bg/zhurnalisti/"],
    "law": ["https://www.fulbright.bg/en/pravo/"],
    "leaders": ["https://www.fulbright.bg/en/fulbright-bulgarian-leaders/"],
    "math": ["https://www.fulbright.bg/en/math-scholars/"],
    "mba-alias": ["https://www.fulbright.bg/en/biznes-administraciq/"],
    "media2023": ["https://www.fulbright.bg/en/eta-medialit-seminar-2023/"],
    "news": ["https://www.fulbright.bg/en/news-and-events/"],
    "ngo": ["https://www.fulbright.bg/npo-research/"],
    "open2023": ["https://www.fulbright.bg/en/open-lectures-2023/"],
    "open2024": ["https://www.fulbright.bg/en/open-lectures-2024/"],
    "other": ["https://www.fulbright.bg/en/other/"],
    "outgoing": ["https://www.fulbright.bg/en/grants-for-bulgarian-citizens/"],
    "pmp": [
        (
            "https://www.fulbright.bg/en/project-management-professiona"
            "l-pmp-capm/"
        ),
    ],
    "privacy": ["https://www.fulbright.bg/en/privacy-policy/"],
    "scholar-us": [
        (
            "https://www.fulbright.bg/en/grants-for-us-scholars-and-pro"
            "fessionals/"
        ),
    ],
    "scholar": [
        "https://www.fulbright.bg/en/visiting-scholar-grants-research/"
    ],
    "schuman": ["https://www.fulbright.bg/en/fulbright-schuman/"],
    "specialist": ["https://www.fulbright.bg/en/fulbright-specialist-program/"],
    "thanks": ["https://www.fulbright.bg/en/thanks-to-scandinavia/"],
}

EXCLUDED_MODULES = {
    "agile": [
        "et_pb_blurb_15",
        "et_pb_blurb_17",
        "et_pb_toggle_4",
        "et_pb_toggle_6",
    ],
    "alumni": ["et_pb_text_6"],
    "court": [
        "et_pb_text_1",
        "et_pb_text_3",
        "et_pb_text_4",
        "et_pb_text_5",
        "et_pb_text_7",
        "et_pb_text_9",
    ],
    "doctoral": ["et_pb_text_7"],
    "entry": ["et_pb_text_5"],
    "eta": ["et_pb_text_11", "et_pb_text_12"],
    "eta-host": ["et_pb_text_8", "et_pb_text_9"],
    "ftea": ["et_pb_text_5"],
    "graduate-bg": ["et_pb_text_6"],
    "graduate-us": ["et_pb_text_7"],
    "humphrey": ["et_pb_text_6"],
    "leaders": ["et_pb_text_6"],
    "math": ["et_pb_text_5"],
    "pmp": [
        "et_pb_blurb_10",
        "et_pb_blurb_8",
        "et_pb_blurb_9",
        "et_pb_text_6",
        "et_pb_toggle_0",
        "et_pb_toggle_1",
        "et_pb_toggle_2",
    ],
    "scholar": ["et_pb_text_7"],
    "scholar-us": ["et_pb_text_7"],
    "specialist": ["et_pb_text_8"],
    "thanks": ["et_pb_text_5"],
}

CONDITIONS = {
    "agile": (
        "a3d9e45ec79a4d1d057c057a14a75e7ee49efe58ef2eb1ad0970c1dab24826" "26"
    ),
    "alumni": (
        "2a2be4ee194e1f249a8d1edc739c09b55e3161522805a907390b54e9b3f11f" "57"
    ),
    "conference2": (
        "90cd3e98586c00b170caa6a865584cbb222a5641d416680be973fd4a78948a" "a9"
    ),
    "conferences": (
        "7ab73199e91bee17ca5ad366d637a7326f9224558a61e53e9dff28790b166e" "37"
    ),
    "court": (
        "8ee27543bf86de6ae1f097eddd11b893aeedd0f367849936a1aca06c11683b" "41"
    ),
    "digital2020": (
        "8559b283f3183364b41e753185da0077805550a5030417ca6bb5e1daee79f9" "e8"
    ),
    "doctoral": (
        "10aa462c2da2be8f15ac7f1a36d15c6279cb6a7b0c91581a9eff9b5a583424" "6a"
    ),
    "entry": (
        "ab876db59b7a3763e9a36b05516e8ea0591a9702e54e9191d7bd1eefd6a762" "25"
    ),
    "eta-host": (
        "e63dfb219d0ba05580d2704bd74bef3da79a86cf937be6acf9aaf28fefb42c" "83"
    ),
    "eta": (
        "3e2d1a8b1336b7dcefb58f28beeccab3098397d9453a8bd10d5e15058f1e5e" "21"
    ),
    "fis2019": (
        "f54f3eabdfd35481af6204cc69946582584ef1a9866e92ba90f3879586bab5" "1f"
    ),
    "fis2021": (
        "64b07a4200d2fe19eb9fe3df4a358f33e5f953f4dc9e7cca73081a42cbdd3f" "cf"
    ),
    "fis2022-review": (
        "c9c6f7f805fa16407648ab24c9bb5120ea6087505bb6585ddd16b720e889da" "6a"
    ),
    "fis2022": (
        "22252c73457a8cc6013164e9e55064e8d8186ddaf4349625c2922caea26851" "1d"
    ),
    "ftea": (
        "393f6b28b4945021d720118a55d9787ddf63f5519760ae4fc6e9a7a76ecc3e" "8c"
    ),
    "graduate-bg": (
        "e6050ec0d8e481f712be995b4b6519c93e4c328e43e94d78edfcad18f1ed69" "6c"
    ),
    "graduate-us": (
        "29aaf0f940f561dde4e83a83c4e8ecdf7d1b3484071dd92581b699de84e3a5" "f9"
    ),
    "home": (
        "1992acc819032652ad59235cbad02f7b41eae29ed7a1fc432eea21cba281ea" "97"
    ),
    "humphrey": (
        "6ded6af39d2c8c040bd5ffae1170e91d5d088ac5e86714543b1de6146953d1" "0e"
    ),
    "incoming": (
        "11fc053e5853842f429fccbaf019ec808d9e45a99b42cc02c606bea535be17" "77"
    ),
    "info-sessions": (
        "2fa37863b01261319d448a2469700f96968b58a0fcb8469b3a85956700922d" "74"
    ),
    "journalists": (
        "cd805aefc16ee5f6cc922cf45a2d424b7567c95b1fedb457d8cbe9b005e822" "c4"
    ),
    "law": (
        "0ba26cd2a2e2d1dec9e17f7d1241b621586cc5f26c7ddb49f0a85674dc370d" "e2"
    ),
    "leaders": (
        "6cfb5f41015aa6cd7a357ab282dd3ec7bb9e975283bcbf3e524b404ec22fed" "a2"
    ),
    "math": (
        "2c8e33c04b1b8f51106d8220d16590c86335336ec616d26d8fb62dd8bbb8c6" "32"
    ),
    "mba-alias": (
        "70225186dcf24faf7d79cf70aa236a701d2f3a0c29fbafbc42a281eddaacf5" "40"
    ),
    "media2023": (
        "6ae2d944e8a20cb1d7b3f9f53274926fb8c96753f02ab04e02ef4f7e04b57a" "ff"
    ),
    "news": (
        "20e67f21e5d35abb79f92b7c888330836b2d20e7a13f8f0f19da980558f1a0" "a1"
    ),
    "ngo": (
        "d081d1f8682bd660e4f7b27a036d8792d9026e353694d7197df416804f4667" "d9"
    ),
    "open2023": (
        "24d595b3b70cc72d31b910635ad4d4f95ca49a211c32346cd31b56068ec800" "3d"
    ),
    "open2024": (
        "dbffbad66bee75051d3c56fcaa04a5ae50e58396d4de0cf2a4264549b7ea80" "bc"
    ),
    "other": (
        "9e28081a9e0b825b50db87f04069680f1c19bc8b0b41c61d66b17a551c20ee" "ce"
    ),
    "outgoing": (
        "0c038652802aab7dba87eefb5a1ecbc7a4bad5d1182792caa61e2ad037f861" "20"
    ),
    "pmp": (
        "8f6db522cfeadff933244bf1ab92f05ad18e3f276cfe539eced417013926e0" "26"
    ),
    "privacy": (
        "a4308ff46138cb4ea930beebb438030be636eee231b14014f47e2ad0c1eb6e" "75"
    ),
    "scholar-us": (
        "0413a5a1843fbafb25df1e2452bae05318b66764e87437b12d102b15712093" "04"
    ),
    "scholar": (
        "5d6d9904dee742469cacc151d2eeac1706f4c70eab0056faff0b4598cdf8a3" "64"
    ),
    "schuman": (
        "06e9455f13d412e813087cb1bee118ae5b2f41bb00d5b6ac94a10f32dde07a" "53"
    ),
    "specialist": (
        "f136ff869d544eac5d0d2f39e51e71f71996a88eea73a3981a69c228c90777" "cd"
    ),
    "thanks": (
        "8c7bb66e777ec1b0eea1602e587790529fe4449a554b7197f408542a907f08" "40"
    ),
}

PROFILES = [
    {
        "track": "graduate-study",
        "key": "graduate-bg",
        "title": "Fulbright graduate study grants for Bulgarians",
        "categories": ["scholarships"],
        "hosts": ["US"],
        "eligible": ["BG"],
        "deadline": None,
        "closed": True,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Closed degree-study competition for Bulgarian citizens: "
            "one academic year in the US, up to USD66,000 total toward "
            "fees and living costs, plus travel and health coverage. A "
            "bachelor’s degree and relevant language/test evidence are "
            "required; US dual citizens, green-card holders and "
            "certain US residents/students are excluded. The published "
            "May deadline has no year."
        ),
        "evidence": (
            "graduate-bg; US; explicit BG citizenship, exclude BG/US "
            "dual and green-card holders, current US students and >=5 "
            "consecutive US years during preceding 6. AY 2027–28 "
            "closed, deadline unspecified (May yearless). Accredited "
            "US university degree fields except clinical "
            "medicine/dentistry; BA diploma by Sept 1,2026 proof date. "
            "Up to USD 66,000 total for one 10-month academic year, "
            "round-trip/J 1/health up to 100 k; capacity 4 vs FAQ 5 "
            "and obsolete table 7 documented, not cloned. IIE "
            "placement, no prior US admission needed. TOEFL expected "
            "with possible English-taught-degree waiver and "
            "postnomination vouchers; GRE most fields except law/MBA, "
            "GMAT business subject school alternative; desirable 81 "
            "science/101 other and 600 school score are not common "
            "hard minima. J 1 two-year home return."
        ),
    },
    {
        "track": "doctoral-research",
        "key": "doctoral",
        "title": "Fulbright doctoral research grants 2027–28",
        "categories": ["grants", "fellowships"],
        "hosts": ["US"],
        "eligible": ["BG"],
        "deadline": "2026-12-04",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Four to six months of nondegree US research for Bulgarian "
            "doctoral students, with destination-based monthly "
            "maintenance, flights and health coverage. A bachelor’s "
            "degree, PhD enrollment and US host invitation are "
            "required. TOEFL may follow nomination; above81 is "
            "advisable. Visiting researchers cannot enroll in courses. "
            "Apply by December4,2026; no closing time is published."
        ),
        "evidence": (
            "doctoral; US; explicit BG and dual US/green-card "
            "exclusion; BA and full/part-time advanced Ph D enrolment, "
            "current US-study and>=5 consecutive/6 residence "
            "exclusion. AY 2027–28 actual December 4,2026 deadline. "
            "Four–six months, three–four grants capacity; monthly "
            "maintenance amount unspecified, round-trip/health. "
            "Official US host invitation/access/supervisor required. "
            "TOEFL can be after nomination free voucher; above 81 "
            "ADVISABLE, not mandatory score. No clinical "
            "medicine/dentistry. Visiting researcher CANNOT enrol "
            "courses despite generic tuition promotional line; "
            "preserve contradiction without claiming tuition "
            "entitlement."
        ),
    },
    {
        "track": "visiting-scholar",
        "key": "scholar",
        "title": "Fulbright visiting scholar grants 2027–28",
        "categories": ["grants", "fellowships"],
        "hosts": ["US"],
        "eligible": ["BG"],
        "deadline": "2026-12-04",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Three to five months of US research or lecturing for "
            "Bulgarian PhD holders. Monthly maintenance is "
            "USD3,200–4,800, with travel, settling support and health "
            "coverage. A US host invitation, project-appropriate "
            "English and good health are required; no English test is "
            "required. Clinical medicine/dentistry and US dual "
            "citizens or green-card holders are excluded. Apply by "
            "December4,2026."
        ),
        "evidence": (
            "scholar; US; BG citizenship, exclude dual US/green-card; "
            "Ph D, project English via interview (NO language-test "
            "requirement), good health and medical report after "
            "selection, official US host invitation. AY 2027–28 "
            "December 4,2026 actual date. Three–five months; USD "
            "3,200–4,800 per month plus flight/settling/health; "
            "capacity 5–7 main versus obsolete related 5 not "
            "entitlement. Nonclinical research/lecturing all other "
            "fields, prior US opportunity preference not absolute "
            "exclusion."
        ),
    },
    {
        "track": "bulgarian-leaders",
        "key": "leaders",
        "title": "Fulbright Bulgarian Leaders awards",
        "categories": ["scholarships"],
        "hosts": ["US"],
        "eligible": [],
        "deadline": None,
        "closed": True,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Closed self-placement programme for Bulgarian "
            "master’s/doctoral students accepted into a top15 US "
            "graduate programme in their field. Funding may total up "
            "to USD100,000 across several years, with a three-year "
            "return-to-Bulgaria agreement. Applicants arrange "
            "university admission and pay application/test costs. "
            "Clinical medicine/dentistry are excluded; no complete "
            "current closing date is published."
        ),
        "evidence": (
            "leaders; US; Bulgarian students label, no country "
            "whitelist pending no direct passport proof. Selfplacement "
            "into top 15 field-ranked US programme, no IIE assistance, "
            "pay own applications/tests; accepted MA/Ph D, nonclinical "
            "fields. Closed AY 2026–27; unspecified deadline (February "
            "2025 lacks day and external university January deadlines "
            "not programme deadline). Up to USD 100,000 total across "
            "possibly several years; <=7 capacity, three-year "
            "Bulgarian return agreement. Cannot parallel independently "
            "if already core IIE finalist; conditional top 15 "
            "admission funding review not automatic entitlement. Core "
            "FAQ 60 k/comparison 66 k not Leaders cap."
        ),
    },
    {
        "track": "humphrey-fellowship",
        "key": "humphrey",
        "title": "Hubert H. Humphrey fellowships 2027–28",
        "categories": ["fellowships", "training"],
        "hosts": ["US"],
        "eligible": ["BG"],
        "deadline": None,
        "closed": True,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Closed ten-month nondegree US leadership fellowship for "
            "Bulgarian mid-career professionals. A "
            "four-year-equivalent bachelor’s degree, five years of "
            "postdegree full-time experience and managerial/policy "
            "responsibilities are required. Minimum English scores are "
            "TOEFL72, Duolingo100 or IELTS6.0. Includes a professional "
            "affiliation of at least six weeks; prior US experience "
            "has specific restrictions."
        ),
        "evidence": (
            "humphrey; US; explicit BG citizenship; "
            "bachelor’s-equivalent 4 years, >=5 full-time years "
            "AFTERdegree prior August 2026 (published anchor despite "
            "AY 2027–28), managerial/policy role, public "
            "service/leadership, valid BGpassport. Mandatory TOEFL 72 "
            "ORDuolingo 100 ORIELTS 6. Closed AY 2027–28, unspecified "
            "deadline. Ten-month nondegree, no conversion to degree, "
            ">=6 weeks professional affiliation; campus placement no "
            "choice, no numeric benefit invented. Prior US experience "
            "allowed only total<=3 years and return BGwork>=4 years; "
            "first-time/limited experience preference distinct. "
            "Exclude BG/USdual, US permanent residents, "
            "recentgraduates/nonmanagerial researchers or teachers, "
            "mission/selection-affiliate staff/family. Misleading own "
            "SEO-home canonical flagged."
        ),
    },
    {
        "track": "thanks-to-scandinavia",
        "key": "thanks",
        "title": "Thanks to Scandinavia graduate awards 2027–28",
        "categories": ["scholarships"],
        "hosts": ["US"],
        "eligible": ["BG"],
        "deadline": "2026-05-08",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Historical May8,2026 closing date for a US master’s-year "
            "award for Bulgarian graduates. Candidates first need core "
            "Fulbright nomination and then separate Foundation "
            "selection. Additional funding is not quantified; the "
            "generic graduate FAQ cap is not the top-up amount. "
            "Preferred fields include human rights, history and "
            "intercultural communication. The page’s open banner "
            "conflicts with the past deadline."
        ),
        "evidence": (
            "thanks; US; explicit BG citizen/BAproof Sept 1,2026, dual "
            "US/greencard/current USstudy/5 consecutive-in 6 "
            "exclusion. AY 2027–28 actual May 8,2026 EXPIRED despite "
            "stale open banner. Oneacademic year MA, two grants "
            "capacity; core Fulbright nomination then separate "
            "Foundation approval, actual additional funding UNKNOWN, "
            "do not borrow core 60 k FAQ or current 66 k. "
            "Human-rights/history/sociology/Jewish/intercultural "
            "fields preferred not exclusive; interest sameapplication "
            "and anti-intolerance essay for principal nominees; "
            "postnomination testvouchers/possiblewaiver, J 1 return 2 "
            "years."
        ),
    },
    {
        "track": "fordham-law",
        "key": "law",
        "title": "Fulbright–Fordham law tuition scholarship",
        "categories": ["scholarships"],
        "hosts": ["US"],
        "eligible": [],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "bg",
        "summary": (
            "One Bulgarian student can receive 40% of tuition for a "
            "full-time, one-academic-year LLM at Fordham Law in New "
            "York. A bachelor’s degree is required. Apply directly to "
            "Fordham Law and mention learning about the opportunity "
            "through Fulbright Bulgaria. The seven specializations are "
            "choices within one scholarship; no programme-specific "
            "application deadline is published."
        ),
        "evidence": (
            "law; US; one Bulgarian student label, BA, direct faculty "
            "LLM application mention Fulbright origin. 40% tuition one "
            "FULLTIMEacademic year at Fordham NY; seven law "
            "specializations not seven identities. deadline and "
            "opening unknown; sidebar May 20,2022 belongs "
            "othergraduate grant, never transferred. Eligible[] "
            "pending adjective versus passport scope; actual Bulgarian "
            "article."
        ),
    },
    {
        "track": "mathematics-research",
        "key": "math",
        "title": "Fulbright mathematics research scholarship 2026–27",
        "categories": ["grants", "fellowships"],
        "hosts": ["US"],
        "eligible": [],
        "deadline": None,
        "closed": True,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Closed five-month research placement at the University of "
            "Miami/IMSA for early-career Bulgarian mathematicians from "
            "Bulgarian educational institutions. The PhD must have "
            "been awarded no more than ten years before the "
            "application deadline. Maintenance is USD4,800 monthly, "
            "plus flights, settling support and health coverage. "
            "Geometry, combinatorics and applied mathematics are "
            "preferred fields."
        ),
        "evidence": (
            "math; US; early-career Bulgarian mathematicians, Ph D "
            "<=10 years before deadline and all BGeducational "
            "institutions; no country whitelist nationality "
            "adjective/affiliation. Closed AY 2026–27, unspecified "
            "deadline. Five months University Miami/IMSA; USD 4,800 "
            "per month , flight/settling/health. "
            "Geometry/combinatorics/applied preference not exclusion. "
            "No capacity/year clones."
        ),
    },
    {
        "track": "court-internship",
        "key": "court",
        "title": "Fulbright Sofia Regional Court overseas internship",
        "categories": ["internships"],
        "hosts": ["US"],
        "eligible": [],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "bg",
        "summary": (
            "Two selected Sofia Regional Court internship participants "
            "receive a funded two-month placement at the federal "
            "district court in New Orleans, Louisiana. Excellent "
            "English is required. Fulbright covers travel, two-month "
            "maintenance and visa costs. The ongoing programme page "
            "publishes no complete application deadline; historical "
            "grantee names are not application calls."
        ),
        "evidence": (
            "court; US; two selected Sofia Regional Court internship "
            "participants, excellent English, annual two-month New "
            "Orleans Louisiana federaldistrictcourt placement funded "
            "flight/maintenance/visa. No direct passport/degree "
            "requirement from alumni biographies; no country "
            "whitelist. deadline and opening unknown; current grantee "
            "2025–26 names and 2010–24 stories not calls. Actual BG "
            "canonical, no copied names/stories. Initial institutional "
            "internship BG is route, funded overseas host US. The "
            "reviewed2022 seminar round-up describes the court "
            "participants as young Bulgarian law students; citizenship "
            "remains unproven."
        ),
    },
    {
        "track": "teacher-excellence-achievement",
        "key": "ftea",
        "title": "Fulbright Teaching Excellence and Achievement 2027",
        "categories": ["training", "fellowships"],
        "hosts": ["US"],
        "eligible": [],
        "deadline": "2026-03-25",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Historical March25,2026 closing date for six-week US "
            "teacher development in2027. Full-time secondary teachers "
            "must live/work in Bulgaria, hold a bachelor’s degree, "
            "have three teaching years and meet TOEFL45. Training "
            "includes teaching methods, a40-hour practicum and "
            "cultural exchange, with flights, housing/meals, academic "
            "costs and health coverage. Prior TEA participants and "
            "several noneligible teaching roles are excluded."
        ),
        "evidence": (
            "ftea; US; fulltime BGsecondary teachers age 12–18 "
            "students, eligible subjects English/math/science/socialstu"
            "dies/journalism, BA, >=3 teachingyears, live/work BG "
            "throughapplication+programme; no country whitelist "
            "affiliation not passport. Mandatory TOEFL 45; "
            "interviewtest or valid May 2025+results, returnteaching "
            "commitment. Exclude prior TEA, administrators, "
            "parttime/university/private tutors/expatriate schools. "
            "Actual March 25,2026 EXPIRED, 2027 Jan–Mar/Sep–Oct "
            "cohorts are eventperiods. Six weeks, 40 hour practicum; "
            "international/USairfare, visa, fees, "
            "lodging/meals/incidentals, ASPE, "
            "conditionaltransport/baggage/workshops/alumni "
            "minigrants;180 international capacity not 180 BGrecords."
        ),
    },
    {
        "track": "schuman-pre-doctoral",
        "key": "schuman",
        "title": ("Fulbright-Schuman predoctoral awards — historical2022 call"),
        "categories": ["grants", "fellowships"],
        "hosts": ["US"],
        "eligible": [],
        "deadline": "2022-12-01",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Historical December1,2022 closing date for four to nine "
            "months of US predoctoral study/research. Projects concern "
            "EU–US relations, EU policy or institutions and must "
            "relate to at least two member states. A US host "
            "affiliation letter is required. Topic countries are not "
            "separate placements; funding amount and an explicit "
            "passport test are not stated."
        ),
        "evidence": (
            "`schuman-pre-doctoral`, `schuman-postdoctoral`, "
            "`schuman-innovation`, schuman; actual December 1,2022 "
            "EXPIRED for all four (historical source edition, no "
            "invented newer round). Source candidates from 27 EUstates "
            "is country-origin wording, no country whitelist pending "
            "explicit citizenship proof; don't import external URL "
            "claim. US affiliation letter required: predoctoral 4–9 "
            "months accredited US institution, postdoc 3–9 months "
            "USuniversity/nonprofit; both EU-US/policy/institutions "
            "topic relevant >=2 EUmemberstates (topic countries not "
            "host clones). Innovation policy/technology nexus, same "
            "USaffiliation but source also says research tenable "
            "one/two EUmemberstates; propose known US host and "
            "explicitly incomplete/contradictory venue scope, no EU 27 "
            "projection/duration guess. Educator 3–9 months "
            "professional at EUuniversity and "
            "USHEIglobal/international highered research. No own "
            "quantified funding, parent<=9 months not silently min 4/3 "
            "innovation. Distinct criteria, not one all-type clone."
        ),
    },
    {
        "track": "schuman-postdoctoral",
        "key": "schuman",
        "title": (
            "Fulbright-Schuman postdoctoral awards — historical2022 " "call"
        ),
        "categories": ["grants", "fellowships"],
        "hosts": ["US"],
        "eligible": [],
        "deadline": "2022-12-01",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Historical December1,2022 closing date for three to nine "
            "months of US postdoctoral research or lecturing. Projects "
            "concern EU–US relations, EU policy or institutions and "
            "must relate to at least two member states. A US host "
            "affiliation letter is required. Topic countries are not "
            "separate placements; funding amount and an explicit "
            "passport test are not stated."
        ),
        "evidence": (
            "`schuman-pre-doctoral`, `schuman-postdoctoral`, "
            "`schuman-innovation`, schuman; actual December 1,2022 "
            "EXPIRED for all four (historical source edition, no "
            "invented newer round). Source candidates from 27 EUstates "
            "is country-origin wording, no country whitelist pending "
            "explicit citizenship proof; don't import external URL "
            "claim. US affiliation letter required: predoctoral 4–9 "
            "months accredited US institution, postdoc 3–9 months "
            "USuniversity/nonprofit; both EU-US/policy/institutions "
            "topic relevant >=2 EUmemberstates (topic countries not "
            "host clones). Innovation policy/technology nexus, same "
            "USaffiliation but source also says research tenable "
            "one/two EUmemberstates; propose known US host and "
            "explicitly incomplete/contradictory venue scope, no EU 27 "
            "projection/duration guess. Educator 3–9 months "
            "professional at EUuniversity and "
            "USHEIglobal/international highered research. No own "
            "quantified funding, parent<=9 months not silently min 4/3 "
            "innovation. Distinct criteria, not one all-type clone."
        ),
    },
    {
        "track": "schuman-innovation",
        "key": "schuman",
        "title": ("Fulbright-Schuman Innovation awards — historical2022 call"),
        "categories": ["grants", "fellowships"],
        "hosts": ["US"],
        "eligible": [],
        "deadline": "2022-12-01",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Historical December1,2022 closing date for "
            "policy/technology research at the EU–US nexus. A US "
            "university or nonprofit affiliation must be arranged. The "
            "publisher also describes research tenable in one or two "
            "EU member states; the complete venue scope is unresolved. "
            "Funding amount and track-specific minimum duration are "
            "not stated. No current competition is inferred."
        ),
        "evidence": (
            "`schuman-pre-doctoral`, `schuman-postdoctoral`, "
            "`schuman-innovation`, schuman; actual December 1,2022 "
            "EXPIRED for all four (historical source edition, no "
            "invented newer round). Source candidates from 27 EUstates "
            "is country-origin wording, no country whitelist pending "
            "explicit citizenship proof; don't import external URL "
            "claim. US affiliation letter required: predoctoral 4–9 "
            "months accredited US institution, postdoc 3–9 months "
            "USuniversity/nonprofit; both EU-US/policy/institutions "
            "topic relevant >=2 EUmemberstates (topic countries not "
            "host clones). Innovation policy/technology nexus, same "
            "USaffiliation but source also says research tenable "
            "one/two EUmemberstates; propose known US host and "
            "explicitly incomplete/contradictory venue scope, no EU 27 "
            "projection/duration guess. Educator 3–9 months "
            "professional at EUuniversity and "
            "USHEIglobal/international highered research. No own "
            "quantified funding, parent<=9 months not silently min 4/3 "
            "innovation. Distinct criteria, not one all-type clone."
        ),
    },
    {
        "track": "schuman-international-educator",
        "key": "schuman",
        "title": (
            "Fulbright-Schuman international educator awards — "
            "historical2022 call"
        ),
        "categories": ["grants", "fellowships"],
        "hosts": ["US"],
        "eligible": [],
        "deadline": "2022-12-01",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Historical December1,2022 closing date for "
            "three-to-nine-month international higher-education "
            "research at US institutions. Applicants are professionals "
            "at European universities; projects compare EU–US/global "
            "practice and require a US affiliation letter. The own "
            "page does not publish a funding amount or an explicit "
            "passport test."
        ),
        "evidence": (
            "`schuman-pre-doctoral`, `schuman-postdoctoral`, "
            "`schuman-innovation`, schuman; actual December 1,2022 "
            "EXPIRED for all four (historical source edition, no "
            "invented newer round). Source candidates from 27 EUstates "
            "is country-origin wording, no country whitelist pending "
            "explicit citizenship proof; don't import external URL "
            "claim. US affiliation letter required: predoctoral 4–9 "
            "months accredited US institution, postdoc 3–9 months "
            "USuniversity/nonprofit; both EU-US/policy/institutions "
            "topic relevant >=2 EUmemberstates (topic countries not "
            "host clones). Innovation policy/technology nexus, same "
            "USaffiliation but source also says research tenable "
            "one/two EUmemberstates; propose known US host and "
            "explicitly incomplete/contradictory venue scope, no EU 27 "
            "projection/duration guess. Educator 3–9 months "
            "professional at EUuniversity and "
            "USHEIglobal/international highered research. No own "
            "quantified funding, parent<=9 months not silently min 4/3 "
            "innovation. Distinct criteria, not one all-type clone."
        ),
    },
    {
        "track": "english-teaching-assistant",
        "key": "eta",
        "title": "Fulbright/ABF English Teaching Assistantship",
        "categories": ["fellowships", "training"],
        "hosts": ["BG"],
        "eligible": ["US"],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "One academic year of English teaching in Bulgarian "
            "schools or universities, with USD1,000 monthly, USD2,000 "
            "travel, USD1,000 relocation, USD500 language support, "
            "health coverage and free housing. Applicants need a "
            "bachelor’s degree by start, native English and no PhD at "
            "application. The citizenship banner’s dual-citizen "
            "wording is ambiguous; US citizenship is expressly named. "
            "No application deadline is published."
        ),
        "evidence": (
            "eta; BG; ownbanner exact “U.S. or dual (Bulgarian, "
            "European) citizens at the time of application”; known "
            "positive US only and ambiguous dual scope retained, no "
            "independent BG/EU passport-only claim; native English, BA "
            "bystart, NOPh D atapplication, health. Academic year, "
            "Septorientation/event dates yearless not deadline. USD "
            "1,000 per month ; travel 2,000+relocation 1,000 (combined "
            "3,000 not additional), language 500 one-time, ASPE and "
            "mental-health, freehousing utilities grantee, no "
            "dependentincrease. English leaf 14–18 teaching hours vs "
            "BGhost 14–20 conflict preserved; "
            "mentor/co-teaching/extracurricular/community duties, "
            "Fridayfree and 4 leave days; limitedsecond-year "
            "competitive extension not fresh record. deadline and "
            "opening unknown. Prior teaching/region experience "
            "advantageous not required."
        ),
    },
    {
        "track": "eta-host-institution",
        "key": "eta-host",
        "title": ("Fulbright/ABF English assistant host-institution programme"),
        "categories": ["grants", "training"],
        "hosts": ["BG"],
        "eligible": [],
        "deadline": None,
        "closed": False,
        "kind": "institutional-grant",
        "language": "bg",
        "summary": (
            "Bulgarian schools and universities can request an "
            "American English assistant for an academic year. Hosts "
            "provide a14–20-hour weekly teaching plan, materials, free "
            "furnished housing and mentoring, and support adjustment "
            "and extracurricular activity. The institution is the "
            "applicant; the assistant’s citizenship is not an "
            "institutional eligibility rule. Annual selection timing "
            "is yearless."
        ),
        "evidence": (
            "eta-host; BG; separate actual BGschool/university "
            "institutional request, not applicant-USnationality; no "
            "country whitelist. Oneacademic-year placement for "
            "suitablelanguage/profile/general/vocational "
            "schools/state/municipal/universities; corporate sponsor "
            "funding route no new campusclone. Host 14–20 h/weekly "
            "plan, materials, free safe furnishedhousing, "
            "mentoring/adjustment/extracurricularsupport, "
            "nondiscrimination/innovative supportive vision. Selection "
            "endfirstschoolterm yearless, deadline and opening "
            "unknown. Actual programmebeneficiary USassistant not "
            "institutional citizenship; don't duplicate merely "
            "translated individualgrant."
        ),
    },
    {
        "track": "us-graduate-study",
        "key": "graduate-us",
        "title": ("Fulbright US graduate study/research — archived overview"),
        "categories": ["scholarships", "grants"],
        "hosts": ["BG"],
        "eligible": ["US"],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "Archived ten-month study/research award in Bulgaria for "
            "US citizens with a bachelor’s-equivalent degree before "
            "start. Maintenance is USD1,200 monthly plus travel, "
            "relocation, research/books, incidentals, health coverage "
            "and USD500 language support. University course tuition is "
            "conditional on Board approval. Current enrollment is not "
            "required; a host invitation is encouraged. No current "
            "opening or deadline is inferred."
        ),
        "evidence": (
            "`us-graduate-study`, `us-graduate-bg-romania`, "
            "`us-graduate-bg-greece`, graduate-us ARCHIVED ownpage; US "
            "citizen, PRinsufficient, bachelor’s-equivalentbystart, "
            "MA/Ph Dpreferredwhere stated not compulsory, "
            "independentunstructuredprojectskills/project-specific "
            "Bulgarian, no patient-contact/licensure. deadline and "
            "opening unknown, explicitly archival titles, no "
            "current-open claim. Core 10 months BG up to 3 capacity; "
            "BG-RO 10 months 5 each, BG-GR 9 months 5 BG+4 GR, "
            "archaeology/history BHFupto 10 months BG with "
            "relateddiscipline/background, fieldschool/excavation/conse"
            "rvation/study visits and publishable paper/presentation. "
            "Region experience notdisadvantage, "
            "currentstudentenrolment notrequired; previous Fulbright "
            "preferencefirsttimers, former ETA>=2 yearsgap. "
            "Invitationencouraged not compulsory. ALL USD 1,200 per "
            "month plus flight/relocation/researchbooks/incidentals/hea"
            "lth grantee only, nodependentincrease; language 500 "
            "core+arch versus 250 joint. Corecoursetuition conditional "
            "Boardapproval; archaeology optionalone appropriateuni "
            "course, no borrowed courseconditions toall. Distinct "
            "joint durations/hosts/entitlements not countryclones."
        ),
    },
    {
        "track": "us-graduate-bg-romania",
        "key": "graduate-us",
        "title": (
            "Fulbright Bulgaria–Romania graduate award — archived " "overview"
        ),
        "categories": ["scholarships", "grants"],
        "hosts": ["BG", "RO"],
        "eligible": ["US"],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "Archived joint research award: ten months, five each in "
            "Bulgaria and Romania, with country order arranged by the "
            "grantee. US citizenship and a bachelor’s-equivalent "
            "degree are required; master’s/doctoral candidates are "
            "preferred. Maintenance is USD1,200 monthly plus programme "
            "travel/research/health support and USD250 language "
            "support. No dependent increase or current deadline is "
            "published."
        ),
        "evidence": (
            "`us-graduate-study`, `us-graduate-bg-romania`, "
            "`us-graduate-bg-greece`, graduate-us ARCHIVED ownpage; US "
            "citizen, PRinsufficient, bachelor’s-equivalentbystart, "
            "MA/Ph Dpreferredwhere stated not compulsory, "
            "independentunstructuredprojectskills/project-specific "
            "Bulgarian, no patient-contact/licensure. deadline and "
            "opening unknown, explicitly archival titles, no "
            "current-open claim. Core 10 months BG up to 3 capacity; "
            "BG-RO 10 months 5 each, BG-GR 9 months 5 BG+4 GR, "
            "archaeology/history BHFupto 10 months BG with "
            "relateddiscipline/background, fieldschool/excavation/conse"
            "rvation/study visits and publishable paper/presentation. "
            "Region experience notdisadvantage, "
            "currentstudentenrolment notrequired; previous Fulbright "
            "preferencefirsttimers, former ETA>=2 yearsgap. "
            "Invitationencouraged not compulsory. ALL USD 1,200 per "
            "month plus flight/relocation/researchbooks/incidentals/hea"
            "lth grantee only, nodependentincrease; language 500 "
            "core+arch versus 250 joint. Corecoursetuition conditional "
            "Boardapproval; archaeology optionalone appropriateuni "
            "course, no borrowed courseconditions toall. Distinct "
            "joint durations/hosts/entitlements not countryclones."
        ),
    },
    {
        "track": "us-graduate-bg-greece",
        "key": "graduate-us",
        "title": (
            "Fulbright Bulgaria–Greece graduate award — archived " "overview"
        ),
        "categories": ["scholarships", "grants"],
        "hosts": ["BG", "GR"],
        "eligible": ["US"],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "Archived joint research award: nine months, five in "
            "Bulgaria and four in Greece. US citizenship and a "
            "bachelor’s-equivalent degree are required; "
            "master’s/doctoral candidates are preferred. Maintenance "
            "is USD1,200 monthly plus programme travel/research/health "
            "support and USD250 language support. Topic examples do "
            "not create extra awards. No dependent increase or current "
            "deadline is published."
        ),
        "evidence": (
            "`us-graduate-study`, `us-graduate-bg-romania`, "
            "`us-graduate-bg-greece`, graduate-us ARCHIVED ownpage; US "
            "citizen, PRinsufficient, bachelor’s-equivalentbystart, "
            "MA/Ph Dpreferredwhere stated not compulsory, "
            "independentunstructuredprojectskills/project-specific "
            "Bulgarian, no patient-contact/licensure. deadline and "
            "opening unknown, explicitly archival titles, no "
            "current-open claim. Core 10 months BG up to 3 capacity; "
            "BG-RO 10 months 5 each, BG-GR 9 months 5 BG+4 GR, "
            "archaeology/history BHFupto 10 months BG with "
            "relateddiscipline/background, fieldschool/excavation/conse"
            "rvation/study visits and publishable paper/presentation. "
            "Region experience notdisadvantage, "
            "currentstudentenrolment notrequired; previous Fulbright "
            "preferencefirsttimers, former ETA>=2 yearsgap. "
            "Invitationencouraged not compulsory. ALL USD 1,200 per "
            "month plus flight/relocation/researchbooks/incidentals/hea"
            "lth grantee only, nodependentincrease; language 500 "
            "core+arch versus 250 joint. Corecoursetuition conditional "
            "Boardapproval; archaeology optionalone appropriateuni "
            "course, no borrowed courseconditions toall. Distinct "
            "joint durations/hosts/entitlements not countryclones."
        ),
    },
    {
        "track": "us-graduate-archaeology",
        "key": "graduate-us",
        "title": (
            "Fulbright/Balkan Heritage archaeology award — archived " "overview"
        ),
        "categories": ["scholarships", "grants", "training"],
        "hosts": ["BG"],
        "eligible": ["US"],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "Archived Bulgaria archaeology/history placement of up to "
            "ten months with the Balkan Heritage Foundation. US "
            "citizens need a bachelor’s-equivalent degree and relevant "
            "disciplinary interest/background. Field schools, "
            "excavation/conservation and study visits support a paper "
            "or conference presentation. Maintenance is USD1,200 "
            "monthly with programme support and USD500 language "
            "support; one suitable university course may be taken."
        ),
        "evidence": (
            "`us-graduate-study`, `us-graduate-bg-romania`, "
            "`us-graduate-bg-greece`, graduate-us ARCHIVED ownpage; US "
            "citizen, PRinsufficient, bachelor’s-equivalentbystart, "
            "MA/Ph Dpreferredwhere stated not compulsory, "
            "independentunstructuredprojectskills/project-specific "
            "Bulgarian, no patient-contact/licensure. deadline and "
            "opening unknown, explicitly archival titles, no "
            "current-open claim. Core 10 months BG up to 3 capacity; "
            "BG-RO 10 months 5 each, BG-GR 9 months 5 BG+4 GR, "
            "archaeology/history BHFupto 10 months BG with "
            "relateddiscipline/background, fieldschool/excavation/conse"
            "rvation/study visits and publishable paper/presentation. "
            "Region experience notdisadvantage, "
            "currentstudentenrolment notrequired; previous Fulbright "
            "preferencefirsttimers, former ETA>=2 yearsgap. "
            "Invitationencouraged not compulsory. ALL USD 1,200 per "
            "month plus flight/relocation/researchbooks/incidentals/hea"
            "lth grantee only, nodependentincrease; language 500 "
            "core+arch versus 250 joint. Corecoursetuition conditional "
            "Boardapproval; archaeology optionalone appropriateuni "
            "course, no borrowed courseconditions toall. Distinct "
            "joint durations/hosts/entitlements not countryclones."
        ),
    },
    {
        "track": "us-scholar-core",
        "key": "scholar-us",
        "title": "Fulbright US scholar and professional grants",
        "categories": ["grants", "fellowships"],
        "hosts": ["BG"],
        "eligible": ["US"],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "Three to five months in Bulgaria for US scholars, "
            "professionals or artists with the appropriate PhD, "
            "terminal qualification or professional standing. "
            "Maintenance is USD3,800 monthly, with USD1,700 travel and "
            "USD1,300 relocation. Dependent supplements require at "
            "least80% presence. A host invitation is optional, with "
            "science-placement considerations. Clinical "
            "medicine/dentistry are excluded; no complete deadline is "
            "published."
        ),
        "evidence": (
            "scholar-us; BG; US citizen, PRinsufficient; Ph D or "
            "appropriate fieldterminaldegree/recognizedprofessionals/ar"
            "tists; no clinicalmedicine/dentistry. Three–five months, "
            "up to 5 capacity, USD 3,800 per month ; flight 1,700 "
            "andrelocation 1,300, dependents travel 1,500 one/3,000 "
            "two+ and monthly 100 one/200 two+ ONLY>=80%presence. "
            "Invite optional, preferredpuresciences; prior Fulbright "
            "not blanketban. deadline and opening unknown."
        ),
    },
    {
        "track": "us-scholar-flex",
        "key": "scholar-us",
        "title": "Fulbright US scholar Flex grants",
        "categories": ["grants", "fellowships"],
        "hosts": ["BG"],
        "eligible": ["US"],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "Two short Bulgaria visits over up to two years, totaling "
            "two to four months, for eligible US "
            "scholars/professionals. Choose the actual Flex option and "
            "provide a timeline. Maintenance is USD3,800 monthly; "
            "USD4,000 travel covers all segments together. No "
            "dependent benefits are provided. Core award "
            "travel/relocation and dependent allowances do not carry "
            "over; no complete deadline is published."
        ),
        "evidence": (
            "scholar-us; same actual USeligibility, BG; two "
            "SHORTsegmentswithin<=2 years,total 2–4 months and actual "
            "Flexcheckbox/timeline. USD 3,800 per month , USD 4,000 "
            "total travel ALLsegments, NO dependentbenefits. No "
            "borrowing core 1,700/1,300 or dependentbenefit. deadline "
            "and opening unknown."
        ),
    },
    {
        "track": "specialist-host-project",
        "key": "specialist",
        "title": "Fulbright Specialist institutional host projects",
        "categories": ["grants", "training"],
        "hosts": ["BG"],
        "eligible": [],
        "deadline": None,
        "closed": False,
        "kind": "institutional-grant",
        "language": "en",
        "summary": (
            "Bulgarian institutions can propose education/training "
            "projects with US specialists lasting14–42days. Hosts "
            "cover lodging, meals and local transport; federal support "
            "covers international flights, limited health insurance "
            "and a daily honorarium. Personal/clinical research is "
            "excluded. Conditional MultiVisit projects share the "
            "same42-day ceiling. January10, June10 and October10 "
            "deadlines have no year."
        ),
        "evidence": (
            "specialist; BG; no country whitelist institutional "
            "applicants HEI/government/courts/cultural/NGO/thinktank/me"
            "dical-publichealth/teachinghospital. Actual 14–42 days "
            "alltraveldays included, lectures/workshops/curriculum/need"
            "sassessment institutionaloutputs; NO personalresearch "
            "includingclinicalpatientcontact. "
            "Onegrant/institution/year, oneexpert, onecountry; "
            "conditional Multi Visit<=3 trips/12 months each>=14 days "
            "total<=42. Jan 10/Jun 10/Oct 10 YEARLESS deadline and "
            "opening unknown; planningnamed 3 months/open 4 months. "
            "Hostlodging/meals/localtransportcostshare; "
            "federalinternationalflight/limitedhealth/DAILYhonorarium "
            "amountunknown. Namedexpertexistsapproval or World "
            "Learningroster match, no guaranteedslot."
        ),
    },
    {
        "track": "specialist-us-professional",
        "key": "specialist",
        "title": ("Fulbright Specialist US professional assignment overview"),
        "categories": ["fellowships", "training"],
        "hosts": ["BG"],
        "eligible": ["US"],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "Experienced US citizens may apply for the Fulbright "
            "Specialist roster and, if matched and approved, deliver "
            "institutional training assignments lasting14–42days. "
            "Roster acceptance does not guarantee a funded placement. "
            "Bulgaria is a documented host, while the roster has "
            "broader international scope. Assignments support "
            "institutional education rather than personal research; no "
            "individual closing date is published."
        ),
        "evidence": (
            "specialist (scope decision): actual own US "
            "citizenexperienced academic/professional/artistic "
            "specialist individual rosterroute and funded 14–42 "
            "dayinstitutional assignments. BGplacement known, "
            "globalroster not BGexclusive; deadline and opening "
            "unknown; rosteracceptance not guaranteedassignment, no "
            "source-fixed honorarium invented. Distinct applicant role "
            "substantive owntext; do not manufacture separate record "
            "if gate deems only host-project description sufficient."
        ),
    },
    {
        "track": "alumni-travel",
        "key": "alumni",
        "title": "Fulbright Bulgaria alumni travel awards",
        "categories": ["grants"],
        "hosts": [],
        "eligible": ["BG", "US"],
        "deadline": "2026-02-28",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Historical February28,2026 closing date for Fulbright "
            "Bulgaria alumni travel proposals, with a maximum EUR1,500 "
            "per project. Bulgarian or US citizen alumni may apply for "
            "one award type per year; joint proposals share the same "
            "total ceiling. Projects/reports must finish within a "
            "year, and recipients cannot reapply for two years. The "
            "published rolling-open banner conflicts with the dated "
            "round; no2027 deadline is inferred."
        ),
        "evidence": (
            "`alumni-travel`, `alumni-publication`, alumni; explicit "
            "BG/US CITIZENS Fulbright Bulgaria alumni, "
            "projectsbased/carriedout BG. Each EUR 1,500 total maximum "
            ", not shared doublepayment; travel 1/year "
            "conference/training/follow-upresearch, publication 1/year "
            "monograph/high-indexinternationaljournal, open 1–2/year "
            "actualnontravel/nonpublication "
            "community/art/research/education impact. Actual February "
            "28,2026 EXPIRED despite stale rolling-open/upcominglabel; "
            "preserveconflict without Feb 2027 inference. One "
            "TYPE/alum/year, joint total maximum 1,500, "
            "reports/projectcomplete<=1 year, no reapply 2 years, "
            "laterfirsttimerpreference notabsolute lifetimeban. Not "
            "official Fulbright grants; owntravel/visa. "
            "Travelactualdestination UNKNOWN host[] despiteprojectbase "
            "BG; publication/openproject BGknown "
            "notpublisherinference. Capacities 2–4 overall not records."
        ),
    },
    {
        "track": "alumni-publication",
        "key": "alumni",
        "title": "Fulbright Bulgaria alumni publication awards",
        "categories": ["grants"],
        "hosts": ["BG"],
        "eligible": ["BG", "US"],
        "deadline": "2026-02-28",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Historical February28,2026 closing date for Fulbright "
            "Bulgaria alumni publication proposals, with a maximum "
            "EUR1,500 per project. Bulgarian or US citizen alumni may "
            "apply for one award type per year; joint proposals share "
            "the same total ceiling. Projects/reports must finish "
            "within a year, and recipients cannot reapply for two "
            "years. The published rolling-open banner conflicts with "
            "the dated round; no2027 deadline is inferred."
        ),
        "evidence": (
            "`alumni-travel`, `alumni-publication`, alumni; explicit "
            "BG/US CITIZENS Fulbright Bulgaria alumni, "
            "projectsbased/carriedout BG. Each EUR 1,500 total maximum "
            ", not shared doublepayment; travel 1/year "
            "conference/training/follow-upresearch, publication 1/year "
            "monograph/high-indexinternationaljournal, open 1–2/year "
            "actualnontravel/nonpublication "
            "community/art/research/education impact. Actual February "
            "28,2026 EXPIRED despite stale rolling-open/upcominglabel; "
            "preserveconflict without Feb 2027 inference. One "
            "TYPE/alum/year, joint total maximum 1,500, "
            "reports/projectcomplete<=1 year, no reapply 2 years, "
            "laterfirsttimerpreference notabsolute lifetimeban. Not "
            "official Fulbright grants; owntravel/visa. "
            "Travelactualdestination UNKNOWN host[] despiteprojectbase "
            "BG; publication/openproject BGknown "
            "notpublisherinference. Capacities 2–4 overall not records."
        ),
    },
    {
        "track": "alumni-open-project",
        "key": "alumni",
        "title": "Fulbright Bulgaria alumni open project awards",
        "categories": ["grants"],
        "hosts": ["BG"],
        "eligible": ["BG", "US"],
        "deadline": "2026-02-28",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Historical February28,2026 closing date for Fulbright "
            "Bulgaria alumni open project proposals, with a maximum "
            "EUR1,500 per project. Bulgarian or US citizen alumni may "
            "apply for one award type per year; joint proposals share "
            "the same total ceiling. Projects/reports must finish "
            "within a year, and recipients cannot reapply for two "
            "years. The published rolling-open banner conflicts with "
            "the dated round; no2027 deadline is inferred."
        ),
        "evidence": (
            "`alumni-travel`, `alumni-publication`, alumni; explicit "
            "BG/US CITIZENS Fulbright Bulgaria alumni, "
            "projectsbased/carriedout BG. Each EUR 1,500 total maximum "
            ", not shared doublepayment; travel 1/year "
            "conference/training/follow-upresearch, publication 1/year "
            "monograph/high-indexinternationaljournal, open 1–2/year "
            "actualnontravel/nonpublication "
            "community/art/research/education impact. Actual February "
            "28,2026 EXPIRED despite stale rolling-open/upcominglabel; "
            "preserveconflict without Feb 2027 inference. One "
            "TYPE/alum/year, joint total maximum 1,500, "
            "reports/projectcomplete<=1 year, no reapply 2 years, "
            "laterfirsttimerpreference notabsolute lifetimeban. Not "
            "official Fulbright grants; owntravel/visa. "
            "Travelactualdestination UNKNOWN host[] despiteprojectbase "
            "BG; publication/openproject BGknown "
            "notpublisherinference. Capacities 2–4 overall not records."
        ),
    },
    {
        "track": "pmp-course-2026-september",
        "key": "pmp",
        "title": "PMP/CAPM course — September/October2026 edition",
        "categories": ["training"],
        "hosts": ["BG"],
        "eligible": [],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "Historical paid English PMP/CAPM preparation in Sofia on "
            "September25–27 and October3–4,2026: five "
            "days,35hours/PDUs; this edition’s fee is not specified. "
            "Curriculum covers teams, governance, performance and "
            "business value. The past edition still has an ongoing "
            "banner, so application status is unresolved. "
            "Certification-exam experience/degree requirements are not "
            "proven course-admission rules."
        ),
        "evidence": (
            "`pmp-course-2026-september`, pmp; paid English five-day "
            "35 hours/PDUs course Sofia BG, October/November edition "
            "price EUR960 including VAT; September edition price "
            "unspecified. Actual editions Sep 25–27/Oct 3–4,2026 (past "
            "despite ONGOINGbanner) and Oct 30–31/Nov 1/Nov 7–8,2026 "
            "upcoming. unspecifiedapplicationdeadlines, "
            "statusunknownwhere enrollment conflicts/no actualclose. "
            "Training curriculum teams/governance/performance/value, "
            "materials/prep not issuedcredential, nofundedgrantclaim. "
            "Examdegree/36 or 60 month PMexperience/35 education "
            "prerequisites are EXAM not mandatory courseadmission; "
            "exam/membershipfees not award benefits. July 2026 "
            "updatedexam header versus January 2021 footer retained as "
            "sourceconflict, no guaranteedcurrentcertification. No "
            "PMPvs CAPM/course-module clones or evergreenduplicate. "
            "Cancellation source 15/7 day boundaries preserved not "
            "guessed. The €960 VAT-included fee occurs only inside the "
            "October/November edition card. The separate "
            "September/October edition has no published fee; no common "
            "price is inferred."
        ),
    },
    {
        "track": "pmp-course-2026-october",
        "key": "pmp",
        "title": "PMP/CAPM course — October/November2026 edition",
        "categories": ["training"],
        "hosts": ["BG"],
        "eligible": [],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "Paid English PMP/CAPM preparation in Sofia on "
            "October30–31 and November1/7/8,2026: five "
            "days,35hours/PDUs, EUR960 including VAT. Curriculum "
            "covers teams, governance, performance and business value, "
            "with preparation materials. No application deadline is "
            "published. This is course preparation, not a funded "
            "scholarship or automatic professional certification."
        ),
        "evidence": (
            "`pmp-course-2026-september`, pmp; paid English five-day "
            "35 hours/PDUs course Sofia BG, EUR 960 VAT INCLUDED. "
            "Actual editions Sep 25–27/Oct 3–4,2026 (past despite "
            "ONGOINGbanner) and Oct 30–31/Nov 1/Nov 7–8,2026 upcoming. "
            "unspecifiedapplicationdeadlines, statusunknownwhere "
            "enrollment conflicts/no actualclose. Training curriculum "
            "teams/governance/performance/value, materials/prep not "
            "issuedcredential, nofundedgrantclaim. Examdegree/36 or 60 "
            "month PMexperience/35 education prerequisites are EXAM "
            "not mandatory courseadmission; exam/membershipfees not "
            "award benefits. July 2026 updatedexam header versus "
            "January 2021 footer retained as sourceconflict, no "
            "guaranteedcurrentcertification. No PMPvs "
            "CAPM/course-module clones or evergreenduplicate. "
            "Cancellation source 15/7 day boundaries preserved not "
            "guessed."
        ),
    },
    {
        "track": "agile-course-2023-october",
        "key": "agile",
        "title": "PMI-ACP preparation — October2023 completed edition",
        "categories": ["training"],
        "hosts": ["BG"],
        "eligible": [],
        "deadline": None,
        "closed": True,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Completed historical English agile-certification "
            "preparation in Sofia, October20–22,2023. Three days "
            "provide21contact hours/PDUs; the published course fee is "
            "EUR500 excluding VAT. Topics include agile mindset, "
            "value, stakeholders, teams, planning, risk and "
            "improvement. Exam eligibility is separate from course "
            "enrollment; the remaining registration form does not "
            "establish a current opening."
        ),
        "evidence": (
            "`agile-course-2023-october`, agile; paid English Sofia BG "
            "three-day 21 hours/PDUs course EUR 500 VAT EXCLUDED. "
            "Actual Oct 20–22,2023 completed and Feb 9–11,2024 "
            "fullybooked; unspecifiedclosingdates, expired "
            "explicitcompleted/fullybooked rather than applicationdate "
            "derivedfrom event. Curriculum agilemindset/value/stakehold"
            "ers/teams/planning/risk/improvement, "
            "materials/preparation and consultationsuponrequest. "
            "Certificationexperience/degree/21 educationhours not "
            "courseadmission; examfees notstipend. Form stillpresent "
            "despite oldeditions, no futurecourse/openstatus invented, "
            "no evergreenclone. Supplemental PDFredirectpolicy denied; "
            "no bypass."
        ),
    },
    {
        "track": "agile-course-2024-february",
        "key": "agile",
        "title": ("PMI-ACP preparation — February2024 fully booked edition"),
        "categories": ["training"],
        "hosts": ["BG"],
        "eligible": [],
        "deadline": None,
        "closed": True,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Fully booked historical English agile-certification "
            "preparation in Sofia, February9–11,2024. Three days "
            "provide21contact hours/PDUs; the published course fee is "
            "EUR500 excluding VAT. Topics include agile mindset, "
            "value, stakeholders, teams, planning, risk and "
            "improvement. Exam eligibility is separate from course "
            "enrollment; no future course or closing date is inferred."
        ),
        "evidence": (
            "`agile-course-2023-october`, agile; paid English Sofia BG "
            "three-day 21 hours/PDUs course EUR 500 VAT EXCLUDED. "
            "Actual Oct 20–22,2023 completed and Feb 9–11,2024 "
            "fullybooked; unspecifiedclosingdates, expired "
            "explicitcompleted/fullybooked rather than applicationdate "
            "derivedfrom event. Curriculum agilemindset/value/stakehold"
            "ers/teams/planning/risk/improvement, "
            "materials/preparation and consultationsuponrequest. "
            "Certificationexperience/degree/21 educationhours not "
            "courseadmission; examfees notstipend. Form stillpresent "
            "despite oldeditions, no futurecourse/openstatus invented, "
            "no evergreenclone. Supplemental PDFredirectpolicy denied; "
            "no bypass."
        ),
    },
    {
        "track": "civil-society-research",
        "key": "ngo",
        "title": "Fulbright civil-society research grants 2026–27",
        "categories": ["grants", "fellowships"],
        "hosts": ["US"],
        "eligible": ["BG"],
        "deadline": None,
        "closed": True,
        "kind": "opportunity",
        "language": "bg",
        "summary": (
            "Closed three-to-five-month US research competition for "
            "Bulgarian NGO leaders/representatives developing civil "
            "society. A bachelor’s degree, excellent English, good "
            "health and an official US NGO/university invitation are "
            "required. Maintenance is USD3,200–4,800 monthly plus "
            "flights and health coverage. US dual citizens and "
            "work-visa/green-card holders are excluded. The "
            "early-December deadline has no year."
        ),
        "evidence": (
            "ngo actual /npo-research/ BGarticle; US; explicit "
            "BGcitizenship, BA, excellent English via interview "
            "NOtest; exclude BG/USdual andworkvisa/greencard, "
            "goodhealth. Bulgarian NGOleaders/representatives "
            "developingcivil society BG, official USNGO/uni invitation "
            "(all recognized NGOs/accrediteduniv/researchinst). Closed "
            "AY 2026–27; early December yearless unspecified. "
            "Three–five months, USD 3,200–4,800 per month "
            "flight/health,2–3 capacity, ownhostletterdates/workspace/a"
            "ccess/contact required, August-to Julyprogrammebounds "
            "notdeadline. No Ph Drequirement borrowedfromscholar."
        ),
    },
    {
        "track": "oklahoma-mba",
        "key": "mba-alias",
        "title": (
            "Fulbright/University of Oklahoma MBA — historical2025–26 " "call"
        ),
        "categories": ["scholarships"],
        "hosts": ["US"],
        "eligible": ["BG"],
        "deadline": "2024-05-10",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Historical May10,2024 closing date for the Oklahoma Price "
            "College MBA scholarship, covering the programme’s "
            "duration. Bulgarian citizens need a four-year bachelor’s "
            "degree and the stated TOEFL/GMAT evidence; no prior "
            "business training is required. The page’s generic "
            "graduate FAQ says up to USD60,000, but its exact "
            "MBA-specific scope is unclear. The old open banner does "
            "not establish a current call."
        ),
        "evidence": (
            "mba-alias actual distinctnamed University Oklahoma Price "
            "College Norman MBA; US; explicit BGcitizenship, BA 4 "
            "yearproof Sept 1,2024; dual US/workvisa/greencard/current "
            "USstudy/5 consecutive-in 6 excluded. AY 2025–26 actual "
            "May 10,2024 EXPIRED despite staleopenbanner. Funding "
            "entireprogramme, own FAQup to USD 60,000 total belongs "
            "generic graduate wording—flag scope uncertainty/no "
            "assertguaranteed entire MBAcost; travel/health own FAQ. "
            "TOEFL>81 and GMAT>=650 owneligibility bullet (score "
            "submission advantage not required by deadline, nominees "
            "vouchers); latergeneric FAQ 101 desirable and 600 "
            "schoolscore conflictnot silently replace 650. No "
            "priorbusinesstraining required, J 1 home 2 years."
        ),
    },
    {
        "track": "journalist-training",
        "key": "journalists",
        "title": "Fulbright explanatory-journalism training overview",
        "categories": ["training", "fellowships"],
        "hosts": ["US"],
        "eligible": [],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "bg",
        "summary": (
            "Funded two-week US explanatory-journalism training for "
            "practicing journalists with at least one year of "
            "experience and B2 English. Flights, US travel, visa, "
            "insurance, accommodation and meals are covered; "
            "personal/home-country costs remain participants’ "
            "responsibility. The Bulgarian-citizen banner conflicts "
            "with broader country-origin eligibility text. The "
            "programme is not annual; no complete current deadline is "
            "published."
        ),
        "evidence": (
            "journalists; US; fundedtwo-week English "
            "explanatory/evidence-basedjournalism, >=1 "
            "yrpractisingjournalist anymedia, B 2, workexamples, "
            "vaccinationsbeforedeparture perhistoricalpandemicrequireme"
            "nts source maychange. Bannerexplicit BGcitizens "
            "versusbodyjournalistsfrom BG,AL,XK,MD,PL,MK,RO,SK,RS,ME,CZ"
            ",HU; no country whitelist to retain the unresolved "
            "explicit BGcitizenship banner AND broader 12 "
            "country-origin eligibility without projecting a "
            "contradictory partial or exclusive passport list. Dual "
            "BG/US/workvisa/greencard excluded; firsttimer/limited "
            "USexperience preference. Nonannual, deadline and opening "
            "unknown; interviewsfirsthalf June/nominations July "
            "yearless NOTdeadline. Free airfare/USlocaltravel/insurance"
            "/visa/lodging/meals; participantowncountrytransport/hotel/"
            "personalexpenses. Washington/NY/Poynter/Herald "
            "workshops/complex-topicethics/disinformation, no "
            "guaranteedall namedvisits/no providerclone."
        ),
    },
    {
        "track": "international-seminar-2021",
        "key": "fis2021",
        "title": ("Fulbright International Seminar2021 — historical edition"),
        "categories": ["training"],
        "hosts": ["BG"],
        "eligible": [],
        "deadline": "2021-09-15",
        "closed": False,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Historical participant deadline September15,2021 for the "
            "September24–26 democracy/representation seminar in Sofia "
            "and online. Bachelor’s juniors/seniors, graduate "
            "students, academics, NGO representatives and young "
            "professionals worldwide could apply. Full participants "
            "attend workshops and complete projects for certification. "
            "Participation was free; partial travel/accommodation aid "
            "was conditional and capacity was25."
        ),
        "evidence": (
            "fis 2021; BG +online; explicitglobalparticipants, no "
            "citizenshippositive whitelist no country whitelist. "
            "BAjunior/senior, MA/Ph D, academics, "
            "NGO/youngprofessionals; actual Sep 15,2021 "
            "APPLICATIONdeadline EXPIRED. Sep 24–26,2021 events Sofia "
            "Launchee/Zoom, freefullprogrammeworkshops/projectwork/cert"
            "ification,<=25 capacity andconditionalpartialtravel/accomm"
            "odationcasebycase. Democracyrepresentation/inclusion/digit"
            "alchange, no genericevergreenclone."
        ),
    },
    {
        "track": "international-seminar-2022",
        "key": "fis2022",
        "title": ("Fulbright International Seminar2022 — historical edition"),
        "categories": ["training"],
        "hosts": ["BG"],
        "eligible": [],
        "deadline": None,
        "closed": False,
        "kind": "programme-overview",
        "language": "en",
        "summary": (
            "Historical four-day hybrid rule-of-law seminar in Sofia, "
            "October20–23,2022, with workshops on governance, justice "
            "and international legal order. The own invitation "
            "addresses third/fourth-year bachelor’s students, graduate "
            "students, civic activists and professionals. In-person "
            "places were limited, with separate online registration. "
            "No application closing date or guaranteed travel "
            "allowance is published."
        ),
        "evidence": (
            "fis 2022+review; genuine ownparticipantinvitation BAyear "
            "3/4,MA/Ph D,civicactivists/professionals, "
            "hybridin-personlimited/Zoomregistration, "
            "legal/democraticinstitutions/EU-USBGexpert workshops. "
            "unspecifiedclosingdate/unknown (past event not "
            "applicationdeadline); actual eventdates/location "
            "corroboration by ownroundup appended below, no PDF. "
            "Noawardamount/guaranteedtravel inferredfrom 2021. "
            "Dates/location corroboration: https://www.fulbright.bg/en/"
            "fis-2022-round-up/."
        ),
    },
    {
        "track": "international-seminar-2019",
        "key": "fis2019",
        "title": ("Fulbright International Seminar2019 — historical edition"),
        "categories": ["training"],
        "hosts": ["BG"],
        "eligible": [],
        "deadline": None,
        "closed": True,
        "kind": "opportunity",
        "language": "en",
        "summary": (
            "Completed historical seven-day democracy seminar in Sofia "
            "and Plovdiv, September24–30,2019. Structured lectures, "
            "practical workshops, policy projects and graduation "
            "addressed judicial reform, information security, social "
            "cohesion and inclusion. The source describes "
            "international academic/civic participants; their "
            "attendance countries do not prove a passport whitelist. "
            "No application deadline is published."
        ),
        "evidence": (
            "Historical 2019 ECA-sponsored participant seminar: Sept "
            "24–30,2019 Sofia/Plovdiv, structured workshops, policy "
            "projects and graduation; no application deadline, no "
            "citizenship whitelist inferred from participant origins."
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
        record["eligible_countries"] = profile["eligible"]
        record["language"] = profile["language"]
        record["deadline"] = profile["deadline"]
        if profile["deadline"]:
            today = utc_now()[:10]
            record["status"] = (
                "open"
                if today < profile["deadline"]
                else "expired" if today > profile["deadline"] else "unknown"
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
                f"Collected {len(records)} validated Fulbright Bulgaria "
                "awards and training editions"
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
