"""Collect reviewed Fulbright Austria awards and teaching employment."""

import argparse
import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
from html.parser import HTMLParser
import ipaddress
import json
import io
import zipfile
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

SOURCE_ID = "at-fulbright-austria"
SOURCE_URL = (
    "https://www.fulbright.at/programs/in-austria/students/"
    "full-time-study-research-grants"
)
WEBSITE_URL = "https://www.fulbright.at/"
LANGUAGE = "en"
PUBLISHER_COUNTRY = "AT"
PUBLISHER_TYPE = "non-profit"
ATTRIBUTION = "Fulbright Austria — Austrian-American Educational Commission"


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


PAGES = {
    "artsraising": (
        "https://www.fulbright.at/support/at-donation/artsraising-initi" "ative"
    ),
    "award-01": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/american-studies-lecturing-award"
    ),
    "award-02": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-academy-of-fine-arts-vienna-visiting-professor"
    ),
    "award-03": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-boku-university-visiting-professor"
    ),
    "award-04": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-botstiber-visiting-professor-of-austrian-ameri"
        "can-studies-in-austria"
    ),
    "award-05": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-diplomatic-academy-visiting-professor-of-inter"
        "national-studies"
    ),
    "award-06": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-fh-joanneum-university-of-applied-sciences-gra"
        "z-visiting-professor"
    ),
    "award-07": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-freud-visiting-lecturer-of-psychoanalysis"
    ),
    "award-08": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-graz-university-of-technology-visiting-profess"
        "or"
    ),
    "award-09": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-ifk-senior-fellow-in-cultural-studies"
    ),
    "award-10": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-imc-krems-visiting-professor"
    ),
    "award-11": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-johannes-kepler-university-linz-visiting-profe"
        "ssor"
    ),
    "award-12": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-mci-the-entrepreneurial-school-visiting-profes"
        "sor"
    ),
    "award-13": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-medical-university-of-innsbruck-visiting-profe"
        "ssor"
    ),
    "award-14": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-nawi-graz-visiting-professor"
    ),
    "award-15": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-paracelsus-medical-university-visiting-profess"
        "or"
    ),
    "award-16": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-paris-lodron-university-of-salzburg-visiting-p"
        "rofessor"
    ),
    "award-17": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-salzburg-university-of-applied-sciences-visiti"
        "ng-professor"
    ),
    "award-18": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-schuman-program"
    ),
    "award-19": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-schuman-program-for-us-citizens"
    ),
    "award-20": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-specialist-program"
    ),
    "award-21": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-st-poelten-university-of-applied-sciences-visi"
        "ting-professor"
    ),
    "award-22": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-tu-wien-visiting-professor"
    ),
    "award-23": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-university-of-applied-sciences-burgenland-visi"
        "ting-professor"
    ),
    "award-24": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-university-of-graz-visiting-professor-in-human"
        "ities"
    ),
    "award-25": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-university-of-innsbruck-visiting-professor"
    ),
    "award-26": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-university-of-klagenfurt-visiting-professor"
    ),
    "award-27": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-university-of-vienna-visiting-professor-of-soc"
        "ial-sciences"
    ),
    "award-28": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/fulbright-wu-vienna-university-of-economics-and-business"
        "-visiting-professor"
    ),
    "award-29": (
        "https://www.fulbright.at/programs/in-austria/scholars/grant-de"
        "tails/intercountry-lecture-program"
    ),
    "award-30": (
        "https://www.fulbright.at/programs/in-austria/students/grant-de"
        "tails/combined-grant-study-research-and-teaching-assistantship"
    ),
    "award-31": (
        "https://www.fulbright.at/programs/in-austria/students/grant-de"
        "tails/community-based-combined-grant"
    ),
    "award-32": (
        "https://www.fulbright.at/programs/in-austria/students/grant-de"
        "tails/fulbright-austrian-marshall-plan-foundation-award"
    ),
    "award-33": (
        "https://www.fulbright.at/programs/in-austria/students/grant-de"
        "tails/fulbright-diplomatic-academy-student-award"
    ),
    "award-34": (
        "https://www.fulbright.at/programs/in-austria/students/grant-de"
        "tails/fulbright-ifk-junior-fellowship"
    ),
    "award-35": (
        "https://www.fulbright.at/programs/in-austria/students/grant-de"
        "tails/fulbright-mach-awards-for-doctoral-candidates"
    ),
    "award-36": (
        "https://www.fulbright.at/programs/in-austria/students/grant-de"
        "tails/fulbright-student-award-in-music-performing-arts-and-vis"
        "ual-arts"
    ),
    "award-37": (
        "https://www.fulbright.at/programs/in-austria/teaching-assistan"
        "ts/grant-details/fulbright-foreign-langauge-teaching-assistant"
        "ship"
    ),
    "award-38": (
        "https://www.fulbright.at/programs/in-austria/teaching-assistan"
        "ts/grant-details/us-teaching-assistant-program"
    ),
    "award-39": (
        "https://www.fulbright.at/programs/in-the-usa/scholars/grant-de"
        "tails/fulbright-austria-expert-award"
    ),
    "award-40": (
        "https://www.fulbright.at/programs/in-the-usa/scholars/grant-de"
        "tails/fulbright-botstiber-visiting-professor-of-austrian-ameri"
        "can-studies-in-the-united-states"
    ),
    "award-41": (
        "https://www.fulbright.at/programs/in-the-usa/scholars/grant-de"
        "tails/fulbright-grants-for-teaching-research-career-developmen"
        "t-or-institutional-collaboration"
    ),
    "award-42": (
        "https://www.fulbright.at/programs/in-the-usa/scholars/grant-de"
        "tails/fulbright-schuman-program"
    ),
    "award-43": (
        "https://www.fulbright.at/programs/in-the-usa/scholars/grant-de"
        "tails/fulbright-visiting-professor-at-the-university-of-minnes"
        "ota-social-sciences-humanities-or-fine-arts"
    ),
    "award-44": (
        "https://www.fulbright.at/programs/in-the-usa/students/grant-de"
        "tails/austrian-fulbright-student-program"
    ),
    "download-at": "https://www.fulbright.at/programs/in-austria/download-area",
    "download-us": "https://www.fulbright.at/programs/in-the-usa/download-area",
    "entry": (
        "https://www.fulbright.at/programs/in-austria/students/full-tim"
        "e-study-research-grants"
    ),
    "flta-fund": (
        "https://www.fulbright.at/support/at-donation/flta-empowerment-" "fund"
    ),
    "home": "https://www.fulbright.at/",
    "imprint": "https://www.fulbright.at/about/imprint",
    "intercountry-overview": (
        "https://www.fulbright.at/programs/in-austria/scholars/intercou"
        "ntry-lecture-program"
    ),
    "opportunity-fund": (
        "https://www.fulbright.at/support/at-donation/fulbright-austria"
        "-opportunity-fund"
    ),
    "privacy": "https://www.fulbright.at/about/privacy",
    "prize": (
        "https://www.fulbright.at/awards/fulbright-prize-in-american-st" "udies"
    ),
    "scholars-at": "https://www.fulbright.at/programs/in-austria/scholars",
    "scholars-us": "https://www.fulbright.at/programs/in-the-usa/scholars",
    "sitemap": "https://www.fulbright.at/sitemap",
    "specialist-overview": (
        "https://www.fulbright.at/programs/in-austria/scholars/fulbrigh"
        "t-specialist-program"
    ),
    "student-roadmap": (
        "https://www.fulbright.at/programs/in-austria/students/roadmap-"
        "to-your-application"
    ),
    "students-at": "https://www.fulbright.at/programs/in-austria/students",
    "students-us": "https://www.fulbright.at/programs/in-the-usa/students",
    "ta-background": (
        "https://www.fulbright.at/programs/in-austria/teaching-assistan"
        "ts/about-teaching-in-austria"
    ),
    "ta-roadmap": (
        "https://www.fulbright.at/programs/in-austria/teaching-assistan"
        "ts/roadmap-to-your-application"
    ),
    "teaching-at": (
        "https://www.fulbright.at/programs/in-austria/teaching-assistan" "ts"
    ),
    "teaching-us": (
        "https://www.fulbright.at/programs/in-the-usa/teaching-assistan" "ts"
    ),
    "us-eu": (
        "https://www.fulbright.at/programs/us-eu-grants-for-us-and-eu-c"
        "itizens"
    ),
}

ASSETS = {
    "flta-instructions": {
        "url": (
            "https://www.fulbright.at/fileadmin/website/Download_area/A"
            "ustrian_FLTA_documents/FLTA_2025-2026_Online_Application_I"
            "nstructions.pdf"
        ),
        "sha256": (
            "5be931384affd4ec88eaace36f8ab677e81ca0214a01f8ccbcf08301f2"
            "067403"
        ),
        "type": "pdf",
    },
    "scholar-plagiarism": {
        "url": (
            "https://www.fulbright.at/fileadmin/website/Download_area/A"
            "ustrian_scholar_documents/Fulbright_Plagiarism_Procedure_F"
            "Y22_Visiting_Scholar.pdf"
        ),
        "sha256": (
            "305ce9294081a7d6e5990063899b3e446d9890ca4c66fdb7d8324adf48"
            "0c19fe"
        ),
        "type": "pdf",
    },
    "usta-handbook": {
        "url": (
            "https://www.fulbright.at/fileadmin/website/Download_area/U"
            "STA_documents/Fulbright_Austria_USTA_Handbook_2025-26.pdf"
        ),
        "sha256": (
            "ce5eefa1013167a99e754d08816078221bb604721602644f0e6a3590ca"
            "ffd410"
        ),
        "type": "pdf",
    },
    "student-instructions": {
        "url": (
            "https://www.fulbright.at/fileadmin/website/Download_area/A"
            "ustrian_student_documents/2027-28_Austrian_Fulbright_Stude"
            "nt_application_materials.zip"
        ),
        "sha256": (
            "070e918202a49112104d58d3398d5d6ece6b164ce3ae4f5512930dd946"
            "aabe3f"
        ),
        "type": "zip",
    },
}


CONDITIONS = {
    "artsraising": (
        "a843a43b53ac9e92743e618f3cc1e890f6ad7cb55bab230bc6a1e47c80c9d8" "e6"
    ),
    "award-01": (
        "9a15c297ed8c32c9d6c7dc2e8a474228eae1e8f8c592eca8fe00bbd8098d7a" "84"
    ),
    "award-02": (
        "cbc22b2ff7d064c5b8a2f40c97d340f26be4915051eecf541e1b109d660b18" "5b"
    ),
    "award-03": (
        "27116e1990c322d6b09aab8f1ad07f7c24b237ee97f7d6919c8fa7133810c7" "48"
    ),
    "award-04": (
        "f1c0e5d07c3a11110e17682b700a5b7e362e75e9888dc1af309f8522d4119a" "a2"
    ),
    "award-05": (
        "80e601c05df80a488f428a51f648a9455a16f7cf13bb3033a6629f8d783983" "a2"
    ),
    "award-06": (
        "a077d80c1c88b276d61f346cfcf23c8cbecfada6be44febebd7d4afac6cba0" "8a"
    ),
    "award-07": (
        "fd1fb6dc39630dabdb50b55173bcb9bc6a75cdac77f953ec144bea482b3bde" "8f"
    ),
    "award-08": (
        "5e490eaff47f1402b4696c0fbf44116912c821fd3fc35714069619ef2d6865" "c2"
    ),
    "award-09": (
        "9150e4b2536e4a591308669bfdbbccda6f545a01b064d033f4ce4811632922" "77"
    ),
    "award-10": (
        "5dee8e935b880cbbdfcae7e71fcd04d0ac2387f606b215fb366b2a6d96a402" "f8"
    ),
    "award-11": (
        "c45f0f7686b758e6379ade292dee10437d246a25ede19c8c0e0f5897b16a3e" "95"
    ),
    "award-12": (
        "e614e631e23f72780d424a5aa43a2d0080d695140bf7f66f9de0132e926aa9" "f9"
    ),
    "award-13": (
        "e4dc85cd069730746d77568823a197715433fb94fd46eafde41dbc1fcdbcaf" "58"
    ),
    "award-14": (
        "01904b72b6cfd784d80dd65034bf98b281def2a71580cc52e3f0a9beac24f0" "58"
    ),
    "award-15": (
        "c3bce1b9488b16d4880d71c410aaaa4cd35c5ebbfcfe5c8a3e0ebba395ce18" "39"
    ),
    "award-16": (
        "2a8e4808f057f4a326944e0a16243d54821a15d0ec92acf4d2b3586524f828" "09"
    ),
    "award-17": (
        "1d8e264c3ea6363455c77dcf94b958abb32de63d7cb240379d00b53cafc2cc" "5f"
    ),
    "award-18": (
        "e7797950470cb99844042ea53e485c612fd57c6cc4c89aec1c1200edd525da" "af"
    ),
    "award-19": (
        "8fe519bb6627336cdd0efd09635c44b6d95987673e014af55fd38230dbb09e" "fb"
    ),
    "award-20": (
        "aa916a778a99295a2aa8f51e47acfcc4738157267090dc09b4b5ec353ec443" "0e"
    ),
    "award-21": (
        "ba8c900b9b77f65bfc25b203cea6eb6789caeed73a8c61917792568127a7fa" "e3"
    ),
    "award-22": (
        "477a03ed2fbb802841ac3e0a3fedd348c15c7ef95d6271854b5a42ffd10944" "15"
    ),
    "award-23": (
        "6c486ad8ebfaad9366058cee6bc83b1617fd17e34e9a342ffb4eb4dfd1f4b8" "37"
    ),
    "award-24": (
        "a5797b2f972ab63e1f19bd506e320dedfeaf21dc1c8e489c0aa82b630972b7" "66"
    ),
    "award-25": (
        "f903460f30734417180e0f591cd966ef4fe6f0d18c31886dacfe37d53c6048" "e2"
    ),
    "award-26": (
        "bb17ee6cf0d3cf8d36b2d1c7060540539fbc3f6922df113fd005f72325045e" "46"
    ),
    "award-27": (
        "9df6ef40992dedea0e7c5167492bebac8b5a9284d19fe903ed195325495c16" "09"
    ),
    "award-28": (
        "2884a982d071fb51eda0bb3fbfd0829a3017068be5b08fa17dfcb432593e20" "f4"
    ),
    "award-29": (
        "19036f1eeab3a03b85f03948f36decf6dd9e5247e443c133e688de69a97c24" "44"
    ),
    "award-30": (
        "a242e1060f16864fc635094bf2e1f27744b9d3204bb6c7579a550a2e17a0a8" "f7"
    ),
    "award-31": (
        "6e9a83c19f39bc51f87ccab72544a1916c9d7b77f7dffcd84c4de61e0b255e" "08"
    ),
    "award-32": (
        "16025818d404e5334f82aab714e57b85cae31174b30f7d57ede12f01734a80" "b6"
    ),
    "award-33": (
        "c53fe3cfd4222f7b9c714987866da544e8e0ba5ac9536bf69821d5d73e1c47" "97"
    ),
    "award-34": (
        "86782e9cac58725baa0f063fb89823fafebd923c5d40a7ab383b9c69007940" "b6"
    ),
    "award-35": (
        "158b808a437127da558329c3fd91cb2dd1827d2e93c77828a3e8697f70188b" "6a"
    ),
    "award-36": (
        "ab3092e23eaa62a1eeac45476a1b2613cbad4fad35ce3f2efc4eff4af29e12" "18"
    ),
    "award-37": (
        "8b158944f700e6e21c7b55d031dcf0c1b5584702c29d66768daf9d99ab05a0" "ef"
    ),
    "award-38": (
        "2b670d7f8f61ac99d4bdc5b10035b1a34d8dbfddba03151a2cbc14c64fccd3" "37"
    ),
    "award-39": (
        "95213e0461389753db798e888da24168967ee861c107ca001e2f739e83358d" "b7"
    ),
    "award-40": (
        "41607a4c7c74888f4ba9c3f2776680d041e9f348abd31bc484f2282b5e3f13" "b5"
    ),
    "award-41": (
        "50e62be50fa950608544d33cff29d09cfd09938c3382da575cb8560a755853" "6b"
    ),
    "award-42": (
        "7e00efabc35c06ff3ef7118ab6095eca0e20bbebdd490265d112f426a73888" "f1"
    ),
    "award-43": (
        "559faeaad2d5050364725071cd13cbd256340afe0531d19edba4d9c6fb9c8c" "61"
    ),
    "award-44": (
        "61444a0403818ee043b7c9c57b7f8eb8e3e7482f75f1185e0f087899b2938d" "f0"
    ),
    "download-at": (
        "a4b068e8c3fc04f66e01bfc88787c0f57d99baefb6a5720c2ed0aac8484cb4" "24"
    ),
    "download-us": (
        "b19e39ae17648f9dbff1a814c6c5441b0ac9f1356a2f83661af8da266cda6b" "68"
    ),
    "entry": (
        "4a09062adf796b58d46ae01066dec84e2d93656bba6c1a96ca6cac35a23452" "7f"
    ),
    "flta-fund": (
        "0f383bf3764807d5fb04a9d6cda7e370b52ab59de91bb168e4275ed383bf53" "61"
    ),
    "home": (
        "1d5a9944d10fdcd87e0f046a01d2bf487b895344942e630d78363efd3b56f6" "35"
    ),
    "imprint": (
        "3f3dedf7e6a8dce4800eeeb2ea69bc975e2de2afe05a9d7680bcabec52c7b4" "2a"
    ),
    "intercountry-overview": (
        "a690c4860353e87b1562a090cab2c91e62d50403e8530a9b4c04c3489d54e7" "1d"
    ),
    "opportunity-fund": (
        "0d27ba28d76bd4650e66991ee5537cbe59ae36c406dddfad5cf5530149773f" "cc"
    ),
    "privacy": (
        "2545f88ef2dc5b8fd35970e87c630e079c0a0e7feed18e1b7c0367459aa02a" "76"
    ),
    "prize": (
        "3c454e91877fe7953bf56ba7a10665867575c8a31cefed0d14558bb90dd102" "50"
    ),
    "scholars-at": (
        "a690c4860353e87b1562a090cab2c91e62d50403e8530a9b4c04c3489d54e7" "1d"
    ),
    "scholars-us": (
        "6075d3c167551f101a203ef3eb169db0b1c9674f5e99c2184e76c5bc945e17" "ad"
    ),
    "sitemap": (
        "df083946af389355a1d1aaa56c66d58d77f3c6138a339328b0c2c006570e1f" "59"
    ),
    "specialist-overview": (
        "a690c4860353e87b1562a090cab2c91e62d50403e8530a9b4c04c3489d54e7" "1d"
    ),
    "student-roadmap": (
        "9917177a82a9a3fb8397fa7498c03d49b0cd6f12fe40b0a7901bfc846ffc2a" "9a"
    ),
    "students-at": (
        "d21b381a330ecfee707e2ca0cff1a7c5ba26308a5f8c02a011449acba019fa" "80"
    ),
    "students-us": (
        "e29d2c0f73adf69836c78a9b148c835816061f09340584eebb564226e88c6d" "35"
    ),
    "ta-background": (
        "7d5a7921ebfa87758af24cd596afa19f65a9f42c415bedd07e8fb81da1aa6e" "61"
    ),
    "ta-roadmap": (
        "c97dd0b0da45a613f0bc2ed5a1f24c5ca6976998a20176a065b792d7b69e84" "6c"
    ),
    "teaching-at": (
        "00472ea74d8c04ffcd9e6eaa3fa904b49842f50e6ef5694c8c8dae7dafca26" "3e"
    ),
    "teaching-us": (
        "6885705b825b417e4530dbbb81d33de2ed9b240f88ed8d318129ed9eda94c3" "20"
    ),
    "us-eu": (
        "879c71797512329f374acc50f8f91e2a6e51b1d69c0ff5c6193bf3955cb19d" "96"
    ),
}


def safe_route(value):
    parsed = urllib.parse.urlsplit(urllib.parse.urljoin(WEBSITE_URL, value))
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port
        or (parsed.hostname or "").removeprefix("www.") != "fulbright.at"
    ):
        raise AdapterError("Invalid own publisher route", "parse")
    return (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")


def source_facts(key, root):
    content = main_content(root)
    canonical = one(
        [
            n.attrs.get("href", "")
            for n in nodes(root, "link")
            if n.attrs.get("rel") == "canonical"
        ],
        "source canonical",
    )
    if canonical != PAGES[key]:
        raise AdapterError("Publisher canonical changed: " + key, "parse")
    routes, rights, external_links = set(), set(), set()
    for node in nodes(root, "a"):
        value = node.attrs.get("href", "")
        parsed = urllib.parse.urlsplit(urllib.parse.urljoin(PAGES[key], value))
        if (parsed.hostname or "").removeprefix("www.") == "fulbright.at":
            route = safe_route(urllib.parse.urlunsplit(parsed))
            routes.add(route)
        elif parsed.scheme in ("http", "https"):
            if parsed.username or parsed.password:
                raise AdapterError("Unsafe source link", "parse")
            external_links.add(
                urllib.parse.urlunsplit(
                    (
                        parsed.scheme,
                        parsed.netloc,
                        parsed.path,
                        parsed.query,
                        "",
                    )
                )
            )
        if re.search(
            r"imprint|privacy|copyright|licen[sc]|permission|terms|"
            r"automation|automated|scraping|republication|datenschutz|"
            r"gengiv|consent",
            node.text() + " " + value,
            re.I,
        ):
            if parsed.scheme in ("http", "https"):
                if parsed.username or parsed.password:
                    raise AdapterError("Unsafe policy link", "parse")
                rights.add(
                    (
                        node.text(),
                        urllib.parse.urlunsplit(
                            (
                                parsed.scheme,
                                parsed.netloc,
                                parsed.path,
                                parsed.query,
                                "",
                            )
                        ),
                    )
                )
    footer = one(nodes(root, "footer"), "publisher footer")
    # The complete visible footer protects new reuse conditions in any tag.
    facts = {
        "text": content.text(),
        "routes": sorted(routes),
        "rights": sorted(rights),
        "external_links": sorted(external_links),
        "footer": footer.text(),
    }
    return json.dumps(facts, ensure_ascii=False, sort_keys=True)


def read_asset(key):
    """Read reviewed public instructions without parsing applicant forms."""
    asset = ASSETS[key]
    current = asset["url"]
    interval = check_robots(current)
    for hop in range(4):
        route = safe_route(current)
        if route not in {safe_route(item["url"]) for item in ASSETS.values()}:
            raise AdapterError("Unreviewed public document redirect", "access")
        status, headers, body = request_bytes(current, interval=interval)
        if status in (301, 302, 303, 307, 308):
            if hop == 3 or not headers.get("Location"):
                raise AdapterError("Invalid public document redirect", "fetch")
            current = urllib.parse.urljoin(current, headers["Location"])
            safe_route(current)
            interval = max(interval, check_robots(current))
            continue
        if status != 200:
            raise AdapterError(
                f"Public document HTTP {status}: {key}", "fetch", status
            )
        if hashlib.sha256(body).hexdigest() != asset["sha256"]:
            raise AdapterError("Public document facts changed: " + key, "parse")
        if asset["type"] == "pdf":
            if not body.startswith(b"%PDF-") or b"%%EOF" not in body[-2048:]:
                raise AdapterError("Incomplete public PDF: " + key, "parse")
        else:
            if not body.startswith(b"PK"):
                raise AdapterError(
                    "Incomplete public instruction archive", "parse"
                )
            try:
                with zipfile.ZipFile(io.BytesIO(body)) as archive:
                    if archive.testzip() is not None:
                        raise ValueError()
            except (ValueError, zipfile.BadZipFile, RuntimeError):
                raise AdapterError(
                    "Invalid public instruction archive", "parse"
                )
        return body
    raise AdapterError("Public document unavailable", "fetch")


def read_pages():
    pages = {key: page(url) for key, url in PAGES.items()}
    # No normalized records exist until every material document is verified.
    for key in ASSETS:
        read_asset(key)
    return pages


def parse_inventory(pages):
    if set(pages) != set(PAGES):
        raise AdapterError("Incomplete Fulbright Austria evidence", "parse")
    for key in PAGES:
        actual = hashlib.sha256(
            source_facts(key, pages[key]).encode()
        ).hexdigest()
        if actual != CONDITIONS[key]:
            raise AdapterError(
                "Fulbright Austria facts/frontier changed: " + key, "parse"
            )
    records = []
    now = utc_now()
    today = now[:10]
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
        record["deadline"] = profile["deadline"]
        opening = profile.get("opening")
        if profile["track"] == "expert" and today > "2026-11-01":
            record["deadline"] = "2027-04-01"
            opening = "2026-11-02"
        deadline = record["deadline"]
        status = profile.get("status", "unknown")
        if deadline and len(deadline) > 10:
            if datetime.fromisoformat(
                now.replace("Z", "+00:00")
            ) >= datetime.fromisoformat(deadline.replace("Z", "+00:00")):
                status = "expired"
            elif opening and today > opening:
                status = "open"
        elif deadline:
            if today > deadline:
                status = "expired"
            elif today == deadline:
                status = "unknown"
            elif opening and today > opening:
                status = "open"
        record["status"] = status
        records.append(record)
    validate_records(records)
    if len(records) != 45:
        raise AdapterError(
            "Incomplete reviewed programme inventory", "validate"
        )
    return records


def collect():
    return parse_inventory(read_pages())


PROFILES = [
    {
        "key": "award-01",
        "track": "american-studies-lecturing-award",
        "title": "American Studies (Lecturing Award)",
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-09-15",
        "opening": "2026-02-15",
        "summary": (
            "Four-month teaching/research award in Austria, with "
            "€5,000 per month and €1,000 travel/relocation. Requires "
            "US citizenship, a PhD, relevant expertise and teaching "
            "experience."
        ),
        "evidence": (
            "Four months; €5,000/month and €1,000 travel/relocation; "
            "two weekly 90-minute American-studies courses plus "
            "negotiable advising. PhD, appropriate teaching experience "
            "and relevant expertise required; all faculty ranks. US "
            "citizenship is mandatory; a green card, permanent "
            "residence or residence permit is insufficient. "
            "Instruction is in English; German is advantageous but not "
            "required. PhD/qualification requirement: a PhD. "
            "Application period: February 15–September 15, 2026."
        ),
    },
    {
        "key": "award-02",
        "track": ("fulbright-academy-of-fine-arts-vienna-visiting-professor"),
        "title": ("Fulbright-Academy of Fine Arts Vienna Visiting Professor"),
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-09-15",
        "opening": "2026-02-15",
        "summary": (
            "Four-month teaching/research award in Austria, with "
            "€5,000 per month and €1,000 travel/relocation. Requires "
            "US citizenship, a PhD or professional experience "
            "appropriate to the assignment, relevant expertise and "
            "teaching experience."
        ),
        "evidence": (
            "Four months; €5,000/month + €1,000; arts "
            "management/economics/culture/entrepreneurship across "
            "Academy and WU; at least two courses. PhD or appropriate "
            "professional qualifications. US citizenship is mandatory; "
            "a green card, permanent residence or residence permit is "
            "insufficient. Instruction is in English; German is "
            "advantageous but not required. PhD/qualification "
            "requirement: a PhD or professional experience appropriate "
            "to the assignment. Application period: February "
            "15–September 15, 2026."
        ),
    },
    {
        "key": "award-03",
        "track": "fulbright-boku-university-visiting-professor",
        "title": "Fulbright-BOKU University Visiting Professor",
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-09-15",
        "opening": "2026-02-15",
        "summary": (
            "Four-month teaching/research award in Austria, with "
            "€5,000 per month and €1,000 travel/relocation. Requires "
            "US citizenship, associate/full professorship or "
            "equivalent researcher qualifications, relevant expertise "
            "and teaching experience."
        ),
        "evidence": (
            "Four months; €5,000/month + €1,000; BOKU advanced "
            "graduate/PhD teaching, research and co-supervision. "
            "Associate/full professor or researcher with equivalent "
            "qualifications. US citizenship is mandatory; a green "
            "card, permanent residence or residence permit is "
            "insufficient. Instruction is in English; German is "
            "advantageous but not required. PhD/qualification "
            "requirement: associate/full professorship or equivalent "
            "researcher qualifications. Application period: February "
            "15–September 15, 2026."
        ),
    },
    {
        "key": "award-04",
        "track": (
            "fulbright-botstiber-visiting-professor-of-austrian-america"
            "n-studies-in-austria"
        ),
        "title": (
            "Fulbright-Botstiber Visiting Professor of "
            "Austrian-American Studies in Austria"
        ),
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-09-15",
        "opening": "2026-02-15",
        "summary": (
            "Four-month Austrian-American history teaching/research "
            "award with USD 5,000 per month and €1,000 "
            "travel/relocation. Requires US citizenship, a PhD, "
            "relevant expertise and teaching experience."
        ),
        "evidence": (
            "Four months; USD 5,000/month + €1,000; historical "
            "Austrian-American relationship mission and "
            "teaching/scholarship; at least one course. PhD. US "
            "citizenship is mandatory; a green card, permanent "
            "residence or residence permit is insufficient. "
            "Instruction is in English; German is advantageous but not "
            "required. PhD/qualification requirement: a PhD. "
            "Application period: February 15–September 15, 2026."
        ),
    },
    {
        "key": "award-05",
        "track": (
            "fulbright-diplomatic-academy-visiting-professor-of-interna"
            "tional-studies"
        ),
        "title": (
            "Fulbright-Diplomatic Academy Visiting Professor of "
            "International Studies"
        ),
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-09-15",
        "opening": "2026-02-15",
        "summary": (
            "Five to six months from January 2028, with €3,300 per "
            "month and €1,000 travel/relocation. Requires US "
            "citizenship, a PhD, relevant expertise and teaching "
            "experience."
        ),
        "evidence": (
            "Five–six months from January 2028; €3,300/month + €1,000; "
            "two graduate diplomatic courses, nine 90-minute sessions "
            "each. PhD. US citizenship is mandatory; a green card, "
            "permanent residence or residence permit is insufficient. "
            "Instruction is in English; German is advantageous but not "
            "required. PhD/qualification requirement: a PhD. "
            "Application period: February 15–September 15, 2026."
        ),
    },
    {
        "key": "award-06",
        "track": (
            "fulbright-fh-joanneum-university-of-applied-sciences-graz-"
            "visiting-professor"
        ),
        "title": (
            "Fulbright-FH Joanneum University of Applied Sciences Graz "
            "Visiting Professor"
        ),
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-09-15",
        "opening": "2026-02-15",
        "summary": (
            "Four-month teaching/research award in Austria, with "
            "€5,000 per month and €1,000 travel/relocation. Requires "
            "US citizenship, a PhD, relevant expertise and teaching "
            "experience."
        ),
        "evidence": (
            "Four months; €5,000/month + €1,000; at least two FH "
            "Joanneum courses and joint research/advising. PhD "
            "required. US citizenship is mandatory; a green card, "
            "permanent residence or residence permit is insufficient. "
            "Instruction is in English; German is advantageous but not "
            "required. PhD/qualification requirement: a PhD. "
            "Application period: February 15–September 15, 2026."
        ),
    },
    {
        "key": "award-07",
        "track": "fulbright-freud-visiting-lecturer-of-psychoanalysis",
        "title": "Fulbright-Freud Visiting Lecturer of Psychoanalysis",
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-09-15",
        "opening": "2026-02-15",
        "summary": (
            "Four-month teaching/research award in Austria, with "
            "€5,000 per month and €1,000 travel/relocation. Requires "
            "US citizenship, associate/full professorship and several "
            "years of psychoanalysis teaching or professional "
            "experience, relevant expertise and teaching experience."
        ),
        "evidence": (
            "Four months; €5,000/month + €1,000; Freud Museum research "
            "and one university course/seminar. Associate/full "
            "professor, several years teaching/professional "
            "psychoanalysis experience; basic German expected. US "
            "citizenship is mandatory; a green card, permanent "
            "residence or residence permit is insufficient. Basic "
            "German is expected. PhD/qualification requirement: "
            "associate/full professorship and several years of "
            "psychoanalysis teaching or professional experience. "
            "Application period: February 15–September 15, 2026."
        ),
    },
    {
        "key": "award-08",
        "track": ("fulbright-graz-university-of-technology-visiting-professor"),
        "title": ("Fulbright-Graz University of Technology Visiting Professor"),
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-09-15",
        "opening": "2026-02-15",
        "summary": (
            "Four-month teaching/research award in Austria, with "
            "€5,000 per month and €1,000 travel/relocation. Requires "
            "US citizenship, an appropriate terminal degree, relevant "
            "expertise and teaching experience."
        ),
        "evidence": (
            "Four months; €5,000/month + €1,000; TU Graz two masters "
            "courses and research. Appropriate terminal degree AND "
            "teaching expertise required. US citizenship is mandatory; "
            "a green card, permanent residence or residence permit is "
            "insufficient. Instruction is in English; German is "
            "advantageous but not required. PhD/qualification "
            "requirement: an appropriate terminal degree. Application "
            "period: February 15–September 15, 2026."
        ),
    },
    {
        "key": "award-09",
        "track": "fulbright-ifk-senior-fellow-in-cultural-studies",
        "title": "Fulbright-ifk Senior Fellow in Cultural Studies",
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-09-15",
        "opening": "2026-02-15",
        "summary": (
            "Four-month cultural-research fellowship. The page states "
            "€5,000 for four months without specifying a monthly "
            "frequency, plus €1,000 travel/relocation. Requires US "
            "citizenship, a PhD, relevant expertise and teaching "
            "experience."
        ),
        "evidence": (
            "Four months; source says €5,000 FOR FOUR MONTHS without "
            "monthly unit, plus €1,000. IFK interdisciplinary cultural "
            "research/fellow meetings and presentation. The payment "
            "frequency is unspecified. US citizenship is mandatory; a green "
            "card, permanent residence or residence permit is "
            "insufficient. Instruction is in English; German is "
            "advantageous but not required. PhD/qualification "
            "requirement: a PhD. Application period: February "
            "15–September 15, 2026."
        ),
    },
    {
        "key": "award-10",
        "track": "fulbright-imc-krems-visiting-professor",
        "title": "Fulbright-IMC Krems Visiting Professor",
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-09-15",
        "opening": "2026-02-15",
        "summary": (
            "Four-month teaching/research award in Austria, with "
            "€5,000 per month and €1,000 travel/relocation. Requires "
            "US citizenship, a PhD or equivalent, relevant expertise "
            "and teaching experience."
        ),
        "evidence": (
            "Four months; €5,000/month + €1,000; IMC Krems three "
            "weekly 90-minute masters sessions in "
            "business/diplomacy/marketing/transformation. PhD or "
            "equivalent. US citizenship is mandatory; a green card, "
            "permanent residence or residence permit is insufficient. "
            "Instruction is in English; German is advantageous but not "
            "required. PhD/qualification requirement: a PhD or "
            "equivalent. Application period: February 15–September 15, "
            "2026."
        ),
    },
    {
        "key": "award-11",
        "track": (
            "fulbright-johannes-kepler-university-linz-visiting-profess" "or"
        ),
        "title": (
            "Fulbright-Johannes Kepler University Linz Visiting " "Professor"
        ),
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-09-15",
        "opening": "2026-02-15",
        "summary": (
            "Four-month teaching/research award in Austria, with "
            "€5,000 per month and €1,000 travel/relocation. Requires "
            "US citizenship, a PhD, relevant expertise and teaching "
            "experience."
        ),
        "evidence": (
            "Four months; €5,000/month + €1,000; JKU at least two "
            "90-minute class sessions weekly/advising, research "
            "welcome; PhD. US citizenship is mandatory; a green card, "
            "permanent residence or residence permit is insufficient. "
            "Instruction is in English; German is advantageous but not "
            "required. PhD/qualification requirement: a PhD. "
            "Application period: February 15–September 15, 2026."
        ),
    },
    {
        "key": "award-12",
        "track": (
            "fulbright-mci-the-entrepreneurial-school-visiting-professo" "r"
        ),
        "title": (
            "Fulbright-MCI | The Entrepreneurial School® Visiting " "Professor"
        ),
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-09-15",
        "opening": "2026-02-15",
        "summary": (
            "Four-month teaching/research award in Austria, with "
            "€5,000 per month and €1,000 travel/relocation. Requires "
            "US citizenship, a PhD, relevant expertise and teaching "
            "experience."
        ),
        "evidence": (
            "Four months; €5,000/month + €1,000; MCI two courses, "
            "advising/research and US-home institutional cooperation; "
            "PhD. US citizenship is mandatory; a green card, permanent "
            "residence or residence permit is insufficient. "
            "Instruction is in English; German is advantageous but not "
            "required. PhD/qualification requirement: a PhD. "
            "Application period: February 15–September 15, 2026."
        ),
    },
    {
        "key": "award-13",
        "track": (
            "fulbright-medical-university-of-innsbruck-visiting-profess" "or"
        ),
        "title": (
            "Fulbright-Medical University of Innsbruck Visiting " "Professor"
        ),
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-09-15",
        "opening": "2026-02-15",
        "summary": (
            "Four-month teaching/research award in Austria, with "
            "€5,000 per month and €1,000 travel/relocation. Requires "
            "US citizenship, a PhD or appropriate professional "
            "experience, relevant expertise and teaching experience."
        ),
        "evidence": (
            "Four months; €5,000/month + €1,000; Medical Innsbruck two "
            "courses/advising/research; PhD or appropriate "
            "professionals. US citizenship is mandatory; a green card, "
            "permanent residence or residence permit is insufficient. "
            "Instruction is in English; German is advantageous but not "
            "required. PhD/qualification requirement: a PhD or "
            "appropriate professional experience. Application period: "
            "February 15–September 15, 2026."
        ),
    },
    {
        "key": "award-14",
        "track": "fulbright-nawi-graz-visiting-professor",
        "title": "Fulbright-NAWI Graz Visiting Professor",
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-09-15",
        "opening": "2026-02-15",
        "summary": (
            "Four-month teaching/research award in Austria, with "
            "€5,000 per month and €1,000 travel/relocation. Requires "
            "US citizenship, a PhD or appropriate professional "
            "experience, relevant expertise and teaching experience."
        ),
        "evidence": (
            "Four months; €5,000/month + €1,000; NAWI two "
            "courses/research/advising; optional extracurricular "
            "networking. PhD or professionals. US citizenship is "
            "mandatory; a green card, permanent residence or residence "
            "permit is insufficient. Instruction is in English; German "
            "is advantageous but not required. PhD/qualification "
            "requirement: a PhD or appropriate professional "
            "experience. Application period: February 15–September 15, "
            "2026."
        ),
    },
    {
        "key": "award-15",
        "track": ("fulbright-paracelsus-medical-university-visiting-professor"),
        "title": ("Fulbright-Paracelsus Medical University Visiting Professor"),
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-09-15",
        "opening": "2026-02-15",
        "summary": (
            "Four-month teaching/research award in Austria, with "
            "€5,000 per month and €1,000 travel/relocation. Requires "
            "US citizenship, a PhD or appropriate professional "
            "experience, relevant expertise and teaching experience."
        ),
        "evidence": (
            "Four months; €5,000/month + €1,000; PMU two "
            "courses/advising/research; PhD or professionals. US "
            "citizenship is mandatory; a green card, permanent "
            "residence or residence permit is insufficient. "
            "Instruction is in English; German is advantageous but not "
            "required. PhD/qualification requirement: a PhD or "
            "appropriate professional experience. Application period: "
            "February 15–September 15, 2026."
        ),
    },
    {
        "key": "award-16",
        "track": (
            "fulbright-paris-lodron-university-of-salzburg-visiting-pro"
            "fessor"
        ),
        "title": (
            "Fulbright-Paris Lodron University of Salzburg Visiting "
            "Professor"
        ),
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-09-15",
        "opening": "2026-02-15",
        "summary": (
            "Four-month teaching/research award in Austria, with "
            "€5,000 per month and €1,000 travel/relocation. Requires "
            "US citizenship, a PhD, relevant expertise and teaching "
            "experience."
        ),
        "evidence": (
            "Four months; €5,000/month + €1,000; Salzburg two courses, "
            "nine 90-minute sessions each; PhD. US citizenship is "
            "mandatory; a green card, permanent residence or residence "
            "permit is insufficient. Instruction is in English; German "
            "is advantageous but not required. PhD/qualification "
            "requirement: a PhD. Application period: February "
            "15–September 15, 2026."
        ),
    },
    {
        "key": "award-17",
        "track": (
            "fulbright-salzburg-university-of-applied-sciences-visiting"
            "-professor"
        ),
        "title": (
            "Fulbright-Salzburg University of Applied Sciences "
            "Visiting Professor"
        ),
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-09-15",
        "opening": "2026-02-15",
        "summary": (
            "Four-month teaching/research award in Austria, with "
            "€5,000 per month and €1,000 travel/relocation. Requires "
            "US citizenship, a PhD, relevant expertise and teaching "
            "experience."
        ),
        "evidence": (
            "Four months; €5,000/month + €1,000; Salzburg UAS at least "
            "two masters courses; PhD. US citizenship is mandatory; a "
            "green card, permanent residence or residence permit is "
            "insufficient. Instruction is in English; German is "
            "advantageous but not required. PhD/qualification "
            "requirement: a PhD. Application period: February "
            "15–September 15, 2026."
        ),
    },
    {
        "key": "award-19",
        "track": "fulbright-schuman-program-for-us-citizens",
        "title": "Fulbright Schuman Program for US citizens",
        "categories": ["scholarships"],
        "kind": "programme-overview",
        "hosts": [],
        "eligible": ["US"],
        "deadline": "2025-12-01T11:00:00Z",
        "opening": "2025-09-15",
        "summary": (
            "US citizens may study, research or lecture on EU affairs "
            "or US–EU relations in Europe through the Schuman "
            "Programme. The page shows a December 1, 2025 noon CET "
            "closing time. Benefits are not specified; header duration "
            "up to nine months differs from the body’s three months to "
            "one academic year."
        ),
        "evidence": (
            "US citizens may study, lecture or research EU affairs or "
            "US–EU relations in Europe. All academic fields may "
            "qualify if they address this focus. The dedicated body "
            "gives the European direction; its hub card instead names "
            "a US institution. Header duration is up to nine months, "
            "while the body says three months to one academic year. "
            "The own page gives December 1, 2025 noon CET (UTC+1), not "
            "a 2026 closing time. Monthly funding and separate "
            "participant-type benefits are not specified."
        ),
    },
    {
        "key": "award-20",
        "track": "fulbright-specialist-program",
        "title": "Fulbright Specialist Program",
        "categories": ["grants"],
        "kind": "institutional-grant",
        "hosts": ["AT"],
        "eligible": [],
        "deadline": "2027-04-01",
        "opening": "2026-04-02",
        "summary": (
            "Austrian institutions may host a US academic or "
            "professional for two to six weeks of educational "
            "capacity-building. The host covers accommodation and "
            "meals; federal support covers travel and a USD 200 daily "
            "honorarium. Competitive roster admission does not "
            "guarantee a match."
        ),
        "evidence": (
            "Austrian institutions submit educational project "
            "proposals for US academics or professionals, with a named "
            "or unnamed specialist. The competitive roster does not "
            "guarantee matching. Projects support seminars, workshops, "
            "mentoring, curriculum and workforce development, rather "
            "than an individual research project. April 2, 2026–April "
            "1, 2027 application period for 2027–28 visits. One "
            "two-to-six-week visit, or three two-week visits within "
            "October 1–September 30; up to five projects may be "
            "funded. The host supplies accommodation and meals; "
            "federal support supplies travel and a USD 200 daily "
            "honorarium."
        ),
    },
    {
        "key": "award-21",
        "track": (
            "fulbright-st-poelten-university-of-applied-sciences-visiti"
            "ng-professor"
        ),
        "title": (
            "Fulbright-St. Pölten University of Applied Sciences "
            "Visiting Professor"
        ),
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": None,
        "opening": None,
        "summary": (
            "No applications are accepted for 2027–28. Four-month "
            "teaching/research award in Austria, with €5,000 per month "
            "and €1,000 travel/relocation. Requires US citizenship, a "
            "PhD or equivalent, relevant expertise and teaching "
            "experience."
        ),
        "evidence": (
            "No applications are accepted for 2027–28. The described "
            "award lasts four months and provides €5,000 per month "
            "plus €1,000 travel/relocation. At least two courses and "
            "research/advising are expected. Faculty need a PhD or "
            "equivalent, appropriate teaching experience and relevant "
            "expertise. US citizenship required; green card or "
            "residence permit insufficient. English instruction; "
            "German advantageous but not required."
        ),
        "status": "expired",
    },
    {
        "key": "award-22",
        "track": "fulbright-tu-wien-visiting-professor",
        "title": "Fulbright-TU Wien Visiting Professor",
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-09-15",
        "opening": "2026-02-15",
        "summary": (
            "Four-month teaching/research award in Austria, with "
            "€5,000 per month and €1,000 travel/relocation. Requires "
            "US citizenship, a PhD or appropriate professional "
            "experience, relevant expertise and teaching experience."
        ),
        "evidence": (
            "Four months; €5,000/month +€1,000; TU Wien at least two "
            "90-minute masters/PhD class sessions per week across the "
            "published engineering/science disciplines; PhD or "
            "professionals. US citizenship is mandatory; a green card, "
            "permanent residence or residence permit is insufficient. "
            "Instruction is in English; German is advantageous but not "
            "required. PhD/qualification requirement: a PhD or "
            "appropriate professional experience. Application period: "
            "February 15–September 15, 2026."
        ),
    },
    {
        "key": "award-23",
        "track": (
            "fulbright-university-of-applied-sciences-burgenland-visiti"
            "ng-professor"
        ),
        "title": (
            "Fulbright-University of Applied Sciences Burgenland "
            "Visiting Professor"
        ),
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-09-15",
        "opening": "2026-02-15",
        "summary": (
            "Four-month teaching/research award in Austria, with "
            "€5,000 per month and €1,000 travel/relocation. Requires "
            "US citizenship, a PhD, relevant expertise and teaching "
            "experience."
        ),
        "evidence": (
            "Four months; €5,000/month +€1,000; Burgenland masters "
            "sustainable energy/environment/buildings; at least two "
            "90-minute class sessions per week; PhD. US citizenship is "
            "mandatory; a green card, permanent residence or residence "
            "permit is insufficient. Instruction is in English; German "
            "is advantageous but not required. PhD/qualification "
            "requirement: a PhD. Application period: February "
            "15–September 15, 2026."
        ),
    },
    {
        "key": "award-24",
        "track": (
            "fulbright-university-of-graz-visiting-professor-in-humanit" "ies"
        ),
        "title": (
            "Fulbright-University of Graz Visiting Professor in " "Humanities"
        ),
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": None,
        "opening": None,
        "summary": (
            "No applications are accepted for 2027–28. Four-month "
            "teaching/research award in Austria, with €5,000 per month "
            "and €1,000 travel/relocation. Requires US citizenship, a "
            "PhD, relevant expertise and teaching experience."
        ),
        "evidence": (
            "No applications are accepted for 2027–28. The described "
            "four-month Humanities teaching/research award provides "
            "€5,000 per month plus €1,000 travel/relocation, with two "
            "cultural-studies courses and advising. Faculty need a "
            "PhD, appropriate teaching experience and relevant "
            "expertise. US citizenship required; green card or "
            "residence permit insufficient. English instruction; "
            "German advantageous but not required."
        ),
        "status": "expired",
    },
    {
        "key": "award-25",
        "track": "fulbright-university-of-innsbruck-visiting-professor",
        "title": "Fulbright-University of Innsbruck Visiting Professor",
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-09-15",
        "opening": "2026-02-15",
        "summary": (
            "Four-month teaching/research award in Austria, with "
            "€5,000 per month and €1,000 travel/relocation. Requires "
            "US citizenship, a PhD or appropriate professional "
            "experience, relevant expertise and teaching experience."
        ),
        "evidence": (
            "Four months; €5,000/month +€1,000; Innsbruck two "
            "courses/advising/research; PhD or professionals. US "
            "citizenship is mandatory; a green card, permanent "
            "residence or residence permit is insufficient. "
            "Instruction is in English; German is advantageous but not "
            "required. PhD/qualification requirement: a PhD or "
            "appropriate professional experience. Application period: "
            "February 15–September 15, 2026."
        ),
    },
    {
        "key": "award-26",
        "track": "fulbright-university-of-klagenfurt-visiting-professor",
        "title": "Fulbright-University of Klagenfurt Visiting Professor",
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-09-15",
        "opening": "2026-02-15",
        "summary": (
            "Four-month teaching/research award in Austria, with "
            "€5,000 per month and €1,000 travel/relocation. Requires "
            "US citizenship, a PhD, relevant expertise and teaching "
            "experience."
        ),
        "evidence": (
            "Four months; €5,000/month +€1,000; Klagenfurt two "
            "courses/advising/research; Research priorities are "
            "referred to a separate catalogue without being specified "
            "on this page. PhD. US citizenship is mandatory; a green "
            "card, permanent residence or residence permit is "
            "insufficient. Instruction is in English; German is "
            "advantageous but not required. PhD/qualification "
            "requirement: a PhD. Application period: February "
            "15–September 15, 2026."
        ),
    },
    {
        "key": "award-27",
        "track": (
            "fulbright-university-of-vienna-visiting-professor-of-socia"
            "l-sciences"
        ),
        "title": (
            "Fulbright-University of Vienna Visiting Professor of "
            "Social Sciences"
        ),
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-09-15",
        "opening": "2026-02-15",
        "summary": (
            "Four-month teaching/research award in Austria, with "
            "€5,000 per month and €1,000 travel/relocation. Requires "
            "US citizenship, a PhD, relevant expertise and teaching "
            "experience."
        ),
        "evidence": (
            "Four months; €5,000/month +€1,000; Vienna Social Sciences "
            "two courses/advising/research; PhD. US citizenship is "
            "mandatory; a green card, permanent residence or residence "
            "permit is insufficient. Instruction is in English; German "
            "is advantageous but not required. PhD/qualification "
            "requirement: a PhD. Application period: February "
            "15–September 15, 2026."
        ),
    },
    {
        "key": "award-28",
        "track": (
            "fulbright-wu-vienna-university-of-economics-and-business-v"
            "isiting-professor"
        ),
        "title": (
            "Fulbright-WU (Vienna University of Economics and "
            "Business) Visiting Professor"
        ),
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-09-15",
        "opening": "2026-02-15",
        "summary": (
            "Four-month teaching/research award in Austria, with "
            "€5,000 per month and €1,000 travel/relocation. Requires "
            "US citizenship, senior academic standing with a PhD, "
            "relevant expertise and teaching experience."
        ),
        "evidence": (
            "Four months; €5,000/month +€1,000; WU two courses PLUS "
            "one Central-European partner guest course. Senior "
            "academics with PhD. Teaching is at WU in Austria, with an "
            "additional guest course at a Central-European partner "
            "university; other countries are not specified. US "
            "citizenship is mandatory; a green card, permanent "
            "residence or residence permit is insufficient. "
            "Instruction is in English; German is advantageous but not "
            "required. PhD/qualification requirement: senior academic "
            "standing with a PhD. Application period: February "
            "15–September 15, 2026."
        ),
    },
    {
        "key": "award-29",
        "track": "intercountry-lecture-program",
        "title": "Intercountry Lecture Program",
        "categories": ["grants"],
        "kind": "institutional-grant",
        "hosts": ["AT"],
        "eligible": [],
        "deadline": None,
        "opening": None,
        "summary": (
            "Austrian institutions may invite a US Fulbright scholar "
            "currently based elsewhere in Europe for a visit of up to "
            "five days. Travel is funded subject to budget; the host "
            "covers room and board. Fulbright Austria and the current "
            "European host commission must approve; request at least "
            "six weeks in advance."
        ),
        "evidence": (
            "An Austrian institution invites a US Fulbright scholar "
            "currently placed in another European country for up to "
            "five days. Fulbright Austria and the scholar’s CURRENT "
            "European host commission must approve. Request at least "
            "six weeks before the visit; funding is budget-contingent. "
            "Travel is reimbursed and the host supplies room and "
            "board. Scholars from bordering countries generally use "
            "second-class rail; non-bordering travel or rail exceeding "
            "five hours one way may use reasonable flights plus "
            "Austrian second-class rail. Ordinary public local "
            "transport with receipts is reimbursable. An Austrian Visa "
            "C-Erwerb is required before travel unless the scholar is "
            "a citizen of a Schengen member country."
        ),
    },
    {
        "key": "award-30",
        "track": ("combined-grant-study-research-and-teaching-assistantship"),
        "title": (
            "Combined Grant: Study, Research, and Teaching " "Assistantship"
        ),
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": [],
        "deadline": "2026-10-07",
        "opening": "2026-03-31",
        "summary": (
            "Nine-month research, part-time study and teaching "
            "placement in Austria. Thirteen classroom hours weekly "
            "plus preparation; public tuition remission, €2,000 "
            "travel, approximately €1,600 net monthly October–May and "
            "€1,300 once in June. Bachelor’s/master’s graduates and "
            "finishing seniors need intermediate German. No "
            "graduate-degree enrolment or dependent allowance."
        ),
        "evidence": (
            "Nine months of research, public university part-time "
            "study and secondary-school teaching. Teaching scheduling "
            "takes priority: thirteen classroom hours weekly plus "
            "preparation/administration. Bachelor’s/master’s graduates "
            "and finishing seniors; intermediate German B1/ACTFL "
            "intermediate-mid/two college years required by grant "
            "start. €2,000 travel, approximately €1,600 NET monthly "
            "October–May (eight months), and €1,300 ONCE in June. ASPE "
            "up to USD 100,000 from late September through June plus "
            "national medical insurance October 1–July 12; public "
            "tuition remission, no dependent allowance. Research "
            "invitations where possible; arts contacts/admission "
            "mandatory, private arts study excluded. Graduate-degree "
            "enrolment is not supported. Two late September "
            "orientation programmes required. Closing October 7, 2026, "
            "literal 17:00 ET; the own page does not resolve its time "
            "zone."
        ),
    },
    {
        "key": "award-31",
        "track": "community-based-combined-grant",
        "title": "Community-Based Combined Grant",
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": [],
        "deadline": "2026-10-07",
        "opening": "2026-03-31",
        "summary": (
            "Nine-month Austrian community-project placement combining "
            "research/study and school teaching. Requires an "
            "organization invitation and intermediate German; thirteen "
            "classroom hours weekly plus preparation. Public tuition "
            "remission, €2,000 travel, approximately €1,600 net "
            "monthly October–May and €1,300 once in June; no dependent "
            "allowance."
        ),
        "evidence": (
            "Nine-month research/study/teaching award with a community "
            "service/capacity project. Organization invitation "
            "letters, coherent feasible collaboration and community "
            "benefit required; underserved communities or bilateral "
            "aims preferred rather than exclusive. Bachelor’s/master’s "
            "graduates and finishing seniors need intermediate German "
            "B1/ACTFL intermediate-mid. Thirteen classroom hours "
            "weekly plus preparation, with teaching scheduling "
            "priority. €2,000 travel, approximately €1,600 NET monthly "
            "October–May (eight months), €1,300 ONCE in June, ASPE up "
            "to USD 100,000 and national medical insurance October "
            "1–July 12. Public tuition remission, no dependent "
            "allowance; graduate-degree goals discouraged. Two late "
            "September orientations required. Closing October 7, 2026, "
            "literal 17:00 ET; the own page does not resolve its time "
            "zone."
        ),
    },
    {
        "key": "award-32",
        "track": "fulbright-austrian-marshall-plan-foundation-award",
        "title": "Fulbright-Austrian Marshall Plan Foundation Award",
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": [],
        "deadline": "2026-10-07",
        "opening": "2026-03-31",
        "summary": (
            "Nine-month STEM research in Austria, with €2,000 travel "
            "and €1,300 monthly maintenance, insurance and public "
            "tuition remission. PhD candidates are preferred; other "
            "strong graduates may qualify. Host invitation and "
            "university matriculation required; German is recommended "
            "according to project needs. Medicine/veterinary medicine "
            "and graduate-degree enrolment excluded."
        ),
        "evidence": (
            "Nine-month STEM research award. PhD candidates preferred, "
            "but strong graduate/recent undergraduate applicants may "
            "qualify. €2,000 travel plus €1,300 monthly maintenance "
            "for nine months, ASPE up to USD 100,000, public tuition "
            "remission, no dependent allowance. Intermediate German is "
            "recommended according to project needs rather than "
            "universally mandatory. Full Austrian public university "
            "matriculation and host invitation/anchored supervision "
            "required. Medicine and veterinary medicine excluded. "
            "Independent research rather than graduate-degree "
            "enrolment; private university fees are not covered. "
            "Closing October 7, 2026, literal 17:00 ET; the own page "
            "does not resolve its time zone."
        ),
    },
    {
        "key": "award-33",
        "track": "fulbright-diplomatic-academy-student-award",
        "title": "Fulbright-Diplomatic Academy Student Award",
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2026-10-07",
        "opening": "2026-03-31",
        "summary": (
            "One year of Diplomatic Academy study, with €15,500 "
            "tuition, €745 monthly room/board supplement and €2,000 "
            "travel. Requires a university degree equivalent to 180 "
            "ECTS; exceptional US/Austrian dual citizens may qualify. "
            "Six or more months of Austrian residence in the preceding "
            "year is disqualifying. No second-year funding for "
            "existing Academy students or dependent allowance."
        ),
        "evidence": (
            "One-year Diplomatic Academy funding: €15,500 tuition, "
            "€745 monthly room/board supplement, €2,000 travel, ASPE "
            "up to USD 100,000; no dependent allowance. Diploma, MAIS "
            "first/direct second year, ETIA first year or DIA first "
            "year options. No funding for the second year of US "
            "students already studying at the Academy. University "
            "degree equivalent to at least 180 ECTS; exceptional "
            "US/Austrian dual citizens may qualify, but Austrian "
            "residence of six or more months in the preceding year "
            "disqualifies. Diploma requires good English and German or "
            "French plus basics of the remaining third language. "
            "English-taught master’s programmes recommend intermediate "
            "German. Independent Academy admission application is "
            "possible but not required. Closing October 7, 2026, "
            "literal 17:00 ET; the own page does not resolve its time "
            "zone."
        ),
    },
    {
        "key": "award-34",
        "track": "fulbright-ifk-junior-fellowship",
        "title": "Fulbright-ifk Junior Fellowship",
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": [],
        "deadline": "2026-10-07",
        "opening": "2026-03-31",
        "summary": (
            "Nine-month IFK cultural-research fellowship for US PhD "
            "candidates with advanced German. €1,300 monthly "
            "maintenance plus up to €600 monthly rent, €2,000 travel, "
            "€300 once for conferences and €200 total phone/post "
            "support; insurance and public tuition remission. Required "
            "fellow meetings and lecture; no dependent allowance."
        ),
        "evidence": (
            "US PhD candidates in interdisciplinary "
            "cultural/humanities/social/art research; advanced German "
            "required with self and qualified-instructor evaluations. "
            "Nine months: €1,300 monthly maintenance plus IFK rent "
            "support up to €600 monthly, €2,000 travel, €300 ONCE for "
            "conferences and €200 all-inclusive phone/post support. "
            "ASPE up to USD 100,000, public tuition remission, no "
            "dependent allowance. Weekly Monday lectures, Tuesday "
            "workshops, fellow meetings and a presentation required; "
            "other activities optional. Shared office/library/computer "
            "resources. Current themes are optional research focuses "
            "rather than citizenship conditions. Closing October 7, "
            "2026, literal 17:00 ET; the own page does not resolve its "
            "time zone."
        ),
    },
    {
        "key": "award-35",
        "track": "fulbright-mach-awards-for-doctoral-candidates",
        "title": "Fulbright-Mach Awards for Doctoral Candidates",
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": [],
        "deadline": "2026-10-07",
        "opening": "2026-03-31",
        "summary": (
            "Nine-month doctoral research in Austria. A master’s "
            "degree is required at application despite a broader "
            "generic candidate paragraph. €2,000 travel and €1,300 "
            "monthly maintenance, insurance and public tuition "
            "remission. Intermediate German, university matriculation "
            "and a host invitation required. Medicine/veterinary "
            "medicine and graduate-degree enrolment excluded."
        ),
        "evidence": (
            "Named Mach doctoral award requires a master’s degree at "
            "APPLICATION. A broader generic candidate paragraph lists "
            "bachelor’s/master’s/PhD applicants, while the Mach "
            "paragraph is narrower. Nine months: €2,000 travel, €1,300 "
            "monthly maintenance, ASPE up to USD 100,000, public "
            "tuition remission, no dependent allowance. Intermediate "
            "German and language evaluations required; full university "
            "matriculation and host invitation/supervision necessary. "
            "Medicine/veterinary medicine and graduate-degree "
            "enrolment excluded. Arts contact, audition/admission "
            "where applicable; private study/tuition excluded. Four "
            "Mach places are within seven full-time research grants. "
            "Unselected candidates may be considered for a combined "
            "award at Fulbright Austria’s discretion. Closing October "
            "7, 2026, literal 17:00 ET; the own page does not resolve "
            "its time zone."
        ),
    },
    {
        "key": "award-36",
        "track": (
            "fulbright-student-award-in-music-performing-arts-and-visua"
            "l-arts"
        ),
        "title": (
            "Fulbright Student Award in Music, Performing Arts and "
            "Visual Arts"
        ),
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": [],
        "deadline": "2026-10-07",
        "opening": "2026-03-31",
        "summary": (
            "Nine-month music, performing-arts or visual-arts study in "
            "Austria. €2,000 travel and €1,300 monthly maintenance "
            "supplement, insurance and public tuition remission; "
            "additional resources are necessary. Intermediate German "
            "and public university acceptance/matriculation required, "
            "with field-specific portfolios, invitations and "
            "auditions. Private study and dependent allowances "
            "excluded."
        ),
        "evidence": (
            "Bachelor’s/master’s/PhD applicants and finishing seniors "
            "may pursue nine-month music, performing-arts or "
            "visual-arts study at an Austrian public university. "
            "€2,000 travel and €1,300 monthly maintenance are "
            "supplemental and require additional resources; ASPE up to "
            "USD 100,000, public tuition remission, no dependent "
            "allowance. Intermediate German with evaluations required. "
            "Relevant contacts, host invitation, audition/admission "
            "and full matriculation required; portfolios according to "
            "field and auditions may involve applicant costs. Private "
            "study/tuition excluded. Both study/research and "
            "graduate-degree enrolment are allowed. Unselected "
            "candidates may be considered for a combined award at "
            "Fulbright Austria’s discretion. Closing October 7, 2026, "
            "literal 17:00 ET; the own page does not resolve its time "
            "zone."
        ),
    },
    {
        "key": "award-37",
        "track": "fulbright-foreign-langauge-teaching-assistantship",
        "title": (
            "Austrian Fulbright Foreign Language Teaching Assistant " "Program"
        ),
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["US"],
        "eligible": ["AT"],
        "deadline": "2027-01-15",
        "opening": "2026-11-15",
        "summary": (
            "Nine-month Austrian German-teaching assistantship at a US "
            "college, with room/board, a small unspecified stipend, "
            "€2,000 travel and insurance. Twenty teaching hours weekly "
            "and two tuition-free courses per semester. Austrian "
            "citizenship and an eligible Austrian degree required; "
            "experience is helpful rather than mandatory. TOEFL is "
            "taken after selection, with a voucher."
        ),
        "evidence": (
            "Austrian citizens teach German at a US institution for "
            "nine months from August 2027, twenty hours weekly. "
            "Early-career applicants need an eligible accredited "
            "Austrian university, university-of-applied-sciences or "
            "teacher-training degree; final-semester exceptions "
            "require completion by June. Language/translation/education"
            " disciplines encouraged, not exclusive. Prior Fulbright "
            "grantees excluded. Native German and strong English; "
            "teaching experience helpful but not mandatory. "
            "Room/board, a small unspecified host stipend, €2,000 "
            "travel, ASPE up to USD 100,000 and two tuition-free "
            "courses per semester; hub mentions two-to-three courses. "
            "TOEFL after selection with a voucher, not at initial "
            "application. J-1 two-year home-residence condition. "
            "Published application period: November 15, "
            "2026–January 15, 2027. Linked file labelled "
            "2025–26 contains generic 2026–27 instructions: US "
            "citizenship/US permanent residence excluded; relevant "
            "prior-year applicant/immediate-family employment "
            "restrictions and plagiarism rules apply, with "
            "country-specific instructions controlling."
        ),
    },
    {
        "key": "award-38",
        "track": "us-teaching-assistant-program",
        "title": "US Teaching Assistantship Program",
        "categories": ["jobs"],
        "kind": "opportunity",
        "hosts": ["AT"],
        "eligible": ["US"],
        "deadline": "2027-01-15",
        "opening": "2026-11-15",
        "summary": (
            "Salaried English-teaching employment in Austrian "
            "secondary schools, not a Fulbright grant. US citizenship "
            "and German B1 required; thirteen classroom hours weekly "
            "plus preparation, October 1–May 31. Approximately €2,012 "
            "gross/€1,631 net monthly with compulsory health/accident "
            "insurance. Travel and housing are self-funded; fees "
            "apply. The page contradicts itself on orientation "
            "attendance and duration."
        ),
        "evidence": (
            "US teaching assistants are salaried BMB/provincial-school "
            "employees, not Fulbright grantees. US citizens including "
            "dual citizens; bachelor’s degree by job start, German "
            "B1/ACTFL intermediate-mid, teaching experience optional. "
            "Thirteen classroom hours weekly plus preparation October "
            "1–May 31. Superior first-year performance may permit a "
            "discretionary second year; maximum two years. "
            "Approximately €2,012 GROSS/€1,631 NET monthly after "
            "tax/compulsory BVAEB health/accident contributions. "
            "Coverage begins with employment and extends through July "
            "12 grace period. No travel allowance or housing; €100 "
            "acceptance fee, €218 residence-permit fee, €130 "
            "orientation fee, police/apostille/airfare/housing/initial "
            "expenses self-funded; first salary mid-November. The body "
            "calls five-day orientation required, whereas its expense "
            "section and 2025–26 handbook describe four days and "
            "optional attendance. Roadmap: residence-permit "
            "application by July 1, acceptance/payment within two "
            "weeks. Current degree-document deadline August 20 "
            "conflicts with dated handbook July 15. Residence/work "
            "permission before duty; extra employment restrictions "
            "unless EU dual citizen. Published application period: "
            "November 15, 2026–January 15, 2027."
        ),
    },
    {
        "key": "award-39",
        "track": "expert",
        "title": "Fulbright Austria Expert Award",
        "categories": ["grants"],
        "kind": "opportunity",
        "hosts": ["US"],
        "eligible": ["AT"],
        "deadline": "2026-11-01",
        "opening": "2026-04-02",
        "summary": (
            "Two-to-six-week collaborative US institutional project "
            "for Austrian academics/professionals. USD 1,000 per week, "
            "€1,500 travel, required university health-plan costs and "
            "visa application fee. Exceptional US/Austrian dual "
            "citizens may qualify; six or more months’ prior-year US "
            "residence is disqualifying. The published rounds close "
            "November 1, 2026 and April 1, 2027, with different "
            "permitted travel windows."
        ),
        "evidence": (
            "Two-to-six-week visit, one visit. Austrian citizenship; "
            "exceptional US/Austrian dual citizens may qualify, while "
            "US residence of six or more months in the prior year is "
            "prohibited. PhD/equivalent academics or professionals "
            "with appropriate expertise/experience; excellent English, "
            "no test. Collaborative institutional educational capacity "
            "rather than individual research. USD 1,000 PER WEEK, "
            "€1,500 travel, required host university health-plan costs "
            "and visa APPLICATION fee covered; host must sponsor J-1, "
            "sponsorship/affiliation/service fees not covered. No "
            "concurrent FWF/ÖAW stipend. Home-institution support, "
            "host invitation confirming visa sponsorship, "
            "project/syllabi and three references required. Round 1 "
            "April 2–November 1, 2026 permits travel March "
            "2027–September 2028; round 2 November 2, 2026–April 1, "
            "2027 permits travel August 2027–September 2028."
        ),
    },
    {
        "key": "award-40",
        "track": (
            "fulbright-botstiber-visiting-professor-of-austrian-america"
            "n-studies-in-the-united-states"
        ),
        "title": (
            "Fulbright-Botstiber Visiting Professor of "
            "Austrian-American Studies in the United States"
        ),
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["US"],
        "eligible": ["AT"],
        "deadline": "2026-10-30",
        "opening": "2026-02-15",
        "summary": (
            "Four-month Austrian-American studies teaching/research "
            "placement in the US, with USD 5,000 per month and €1,500 "
            "travel/relocation. Austrian citizenship, a PhD or "
            "equivalent and teaching expertise required; US/Austrian "
            "dual citizens ineligible. University affiliation and "
            "required health-plan fees are not covered. J-1 "
            "home-residence and 24-month restrictions apply."
        ),
        "evidence": (
            "Four months of teaching/scholarship on the historical "
            "Austrian-American relationship in 2027–28. USD 5,000 PER "
            "MONTH plus €1,500 travel/relocation. Austrian citizenship "
            "required; US/Austrian dual citizens excluded. Faculty all "
            "ranks need PhD/equivalent, appropriate teaching "
            "experience and relevant expertise; English instruction. "
            "Grants may begin September 1 and end June 30. J-1 "
            "home-residence/24-month restrictions. Affiliation/service "
            "fees and required host university health plans not "
            "covered. February 15–October 30, 2026 date-only "
            "application period; current four-month terms differ from "
            "a historical three-month testimonial."
        ),
    },
    {
        "key": "award-41",
        "track": (
            "fulbright-grants-for-teaching-research-career-development-"
            "or-institutional-collaboration"
        ),
        "title": (
            "Fulbright Grants for Teaching, Research, Career "
            "Development, or Institutional Collaboration"
        ),
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["US"],
        "eligible": ["AT"],
        "deadline": "2026-10-30",
        "opening": "2026-02-15",
        "summary": (
            "Three-to-four-month US teaching, research, "
            "career-development or institutional-collaboration visit, "
            "with USD 4,000 per month and €1,500 travel/relocation. "
            "Austrian citizenship and a PhD or equivalent required; "
            "US/Austrian dual citizens ineligible. Projects ideally "
            "combine activities. Affiliation and required university "
            "health fees are not covered; J-1 restrictions apply."
        ),
        "evidence": (
            "Three-to-four-month US teaching, research, "
            "career-development or institutional-collaboration award "
            "in 2027–28; projects ideally combine two or more "
            "activities. USD 4,000 PER MONTH plus €1,500 "
            "travel/relocation. Austrian citizenship, faculty with "
            "PhD/equivalent and appropriate teaching expertise; "
            "US/Austrian dual citizens excluded. Grants may "
            "begin September 1 and end June 30. J-1 "
            "home-residence/24-month restrictions; affiliation/service "
            "and required university health-plan fees not covered. "
            "February 15–October 30, 2026 date-only application period."
        ),
    },
    {
        "key": "award-42",
        "track": "fulbright-schuman-program",
        "title": "Fulbright Schuman Program for EU citizens",
        "categories": ["scholarships"],
        "kind": "programme-overview",
        "hosts": ["US"],
        "eligible": [
            "AT",
            "BE",
            "BG",
            "HR",
            "CY",
            "CZ",
            "DK",
            "EE",
            "FI",
            "FR",
            "DE",
            "GR",
            "HU",
            "IE",
            "IT",
            "LV",
            "LT",
            "LU",
            "MT",
            "NL",
            "PL",
            "PT",
            "RO",
            "SK",
            "SI",
            "ES",
            "SE",
        ],
        "deadline": "2026-12-01T11:00:00Z",
        "opening": "2026-09-15",
        "summary": (
            "EU citizens may study, research or lecture in the US on "
            "EU affairs or US–EU relations through the Schuman "
            "Programme. The closing time is December 1, 2026 noon CET. "
            "The page does not specify funding amounts; header "
            "duration up to nine months differs from the body’s three "
            "months to one academic year."
        ),
        "evidence": (
            "Citizens of EU member states may study, lecture or "
            "research in the US on EU development/policy or US–EU "
            "relations; any academic field with this focus. Joint US "
            "Department of State/European Commission funding "
            "administered by the US-Belgium-Luxembourg commission. "
            "Monthly award amounts and separate track benefits are not "
            "specified. Header up to nine months; body three months to "
            "one academic year. Starts September–end March and must end "
            "within the academic year applied for. September "
            "15–December 1, 2026 at 12:00 CET (UTC+1) application "
            "period. The same programme appears in the incoming "
            "scholar hub."
        ),
    },
    {
        "key": "award-43",
        "track": (
            "fulbright-visiting-professor-at-the-university-of-minnesot"
            "a-social-sciences-humanities-or-fine-arts"
        ),
        "title": (
            "Fulbright Visiting Professor at the University of "
            "Minnesota (Social Sciences, Humanities, or Fine Arts)"
        ),
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["US"],
        "eligible": ["AT"],
        "deadline": "2026-10-30",
        "opening": "2026-02-15",
        "summary": (
            "Minnesota visiting professorship for Austrian academics "
            "in social sciences, humanities or fine arts. USD 2,500 "
            "monthly maintenance for three to four months, USD 10,000 "
            "gross salary and USD 5,000 gross housing support in "
            "total, plus €1,500 travel. Austrian citizenship and PhD "
            "or equivalent required; US/Austrian dual citizens "
            "ineligible. Header and summary differ on duration."
        ),
        "evidence": (
            "Minnesota social-sciences/humanities/fine-arts "
            "professorship, with European studies/human "
            "rights/immigration/refugee/genocide studies encouraged. "
            "Header three-to-four months while summary says four. USD "
            "2,500 MONTHLY maintenance for three-to-four months, USD "
            "10,000 GROSS TOTAL salary, USD 5,000 GROSS TOTAL housing "
            "subsidy/assistance, €1,500 travel/relocation. Austrian "
            "faculty all ranks with PhD/equivalent and teaching "
            "expertise; US/Austrian dual citizens excluded. Host "
            "invitation not required for the named Minnesota portal "
            "selection. J-1 home-residence/24-month restrictions; "
            "affiliation/service and required health-plan fees not "
            "covered. February 15–October 30, 2026 date-only "
            "application period."
        ),
    },
    {
        "key": "award-44",
        "track": "austrian-fulbright-student-program",
        "title": "Austrian Fulbright Student Program",
        "categories": ["scholarships"],
        "kind": "opportunity",
        "hosts": ["US"],
        "eligible": ["AT"],
        "deadline": "2027-05-01",
        "opening": "2027-02-01",
        "summary": (
            "Austrian master’s funding at a US university: up to "
            "USD 50,000 for one-year or USD 75,000 for multi-year "
            "programmes, plus up to €2,000 travel, insurance and "
            "application/test support. Austrian citizenship required; "
            "US dual citizenship/current US residence excluded. "
            "Professional degrees have defined exceptions. The 2027 "
            "application header conflicts with older "
            "roadmap/materials; previous-grant rules also conflict."
        ),
        "evidence": (
            "Master’s funding: up to USD 50,000 for a one-year or "
            "USD 75,000 for a multi-year programme, up to €2,000 travel, "
            "ASPE up to USD 100,000, possible partial tuition waivers, "
            "coaching/four university applications/fees and GRE/TOEFL "
            "vouchers AFTER nomination; law programmes normally "
            "GRE-exempt. Austrian citizenship, no US dual "
            "citizenship/current US residence. Degree by October 1, "
            "October 30 case-by-case; Diplomstudium documentation by "
            "May 1 of the FOLLOWING year. MBA/professional degrees "
            "excluded; LLM exceptions constitutional/human-rights/publi"
            "c-international/theory/philosophy/comparative/history, "
            "and public-health exception to medical restrictions; "
            "veterinary/pharmacy otherwise excluded. No independently "
            "applied/admitted US programme; scholar-qualified "
            "postdocs/professionals excluded. "
            "Previous BMFWF postgraduate grants except internships, "
            "FWF/ÖAW and concurrent funding excluded. Prior Fulbright "
            "student exclusion conflicts with FAQ allowing another "
            "basic grant five years after the previous grant. Header "
            "February 1–May 1, 2027/start August–September 2028 "
            "conflicts with 2026→2027 roadmap and 2027–28 instruction "
            "ZIP. FAQ says 00:00 CET on May 1 without dated-year scope. "
            "Generic linked instructions exclude US permanent "
            "residents and certain applicant/immediate-family "
            "prior-year employment; generic PhD options do not broaden "
            "this master’s programme. J-1 two-year home-residence "
            "condition."
        ),
    },
    {
        "key": "award-20",
        "track": "specialist-arts",
        "title": (
            "Fulbright Specialist in Music, Performing Arts and Visual " "Arts"
        ),
        "categories": ["grants"],
        "kind": "institutional-grant",
        "hosts": ["AT"],
        "eligible": [],
        "deadline": None,
        "opening": None,
        "summary": (
            "Austrian arts institutions may invite a US specialist for "
            "two to six weeks of teaching/advising in music, "
            "performing arts, visual arts or arts management. The "
            "separately funded programme covers travel, "
            "honorarium/teaching remuneration and ASPE insurance. The "
            "page does not give an award amount or application closing "
            "date."
        ),
        "evidence": (
            "Named programme operating since 2024–25; Austrian arts "
            "institution invitation, two-to-six-week educational "
            "assignment for US citizens confirmed by the Artsraising "
            "page. Literary arts and arts management also supported. "
            "Fully field-funded travel, honorarium and teaching "
            "remuneration with ASPE health benefit. The specific track "
            "does not state the generic USD 200/day honorarium or April "
            "1 deadline. Artsraising donation targets are fundraising "
            "budgets, not recipient entitlements."
        ),
    },
    {
        "key": "prize",
        "track": "american-studies-prize",
        "title": "Fulbright Austria Prize in American Studies",
        "categories": ["grants"],
        "kind": "opportunity",
        "hosts": [],
        "eligible": [],
        "deadline": "2026-04-30",
        "opening": None,
        "summary": (
            "Prize for a master’s/doctoral American-studies thesis "
            "completed at an Austrian university in 2024–25, submitted "
            "October 2024–November 2025. Students or supervisors may "
            "nominate; previously submitted theses cannot be "
            "resubmitted. A panel awards one €1,000 prize or two €750 "
            "prizes. April 30, 2026 date-only closing; the November "
            "2026 ceremony is not an application deadline."
        ),
        "evidence": (
            "American studies in the broadest sense includes relevant "
            "ancillary disciplines at Austrian universities, with no "
            "topical or methodological limitation. Thesis affiliation "
            "is not Austrian citizenship or a funded travel "
            "destination. One€1,000 or two €750 awards are alternatives "
            "determined by the panel; masters/doctoral theses, "
            "students/advisors nominate, no prior-year resubmission. "
            "Own dated call: April 30, 2026;2026 AAAS November "
            "conference award ceremony."
        ),
    },
]
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
                    reviewed = set(PAGES.values()) | {
                        value["url"] for value in ASSETS.values()
                    }
                    if urllib.parse.urlsplit(
                        current
                    ).path != "/robots.txt" and safe_route(current) not in {
                        safe_route(value) for value in reviewed
                    }:
                        raise AdapterError(
                            "Redirect reaches an unreviewed route", "access"
                        )
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
        "name": "Fulbright Austria",
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
                f"Collected {len(records)} validated Fulbright Austria "
                "awards and teaching positions"
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
