"""Collect official Charles University funding opportunities."""

import argparse
import base64
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
from html.parser import HTMLParser
import ipaddress
import io
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET

SOURCE_ID = "cz-charles-university"
SOURCE_URL = "https://cuni.cz/UKEN-927.html"
WEBSITE_URL = "https://cuni.cz/UKEN-1617.html"
LANGUAGE = "en"
PUBLISHER_COUNTRY = "CZ"
PUBLISHER_TYPE = "university"
ATTRIBUTION = "Charles University"


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


def content(root, url):
    """Select reviewed publisher content without navigation or tracking code."""
    host = urllib.parse.urlsplit(url).hostname
    if host == "www.mff.cuni.cz":
        return one(
            [node for node in root.walk() if node.attrs.get("id") == "content"],
            "MFF content",
        )
    if host == "fsv.cuni.cz":
        return one(nodes(root, "div", "main-content"), "FSV content")
    if host == "centrumcarolina.cuni.cz":
        return one(nodes(root, "div", "dright"), "programme content")
    return one(nodes(root, "div", "content"), "CU content")


def programme(
    key,
    title,
    url,
    text,
    facts,
    summary,
    hosts=(),
    eligible=(),
    deadline=None,
    opening=None,
    rolling=False,
    category="scholarships",
):
    """Normalize only directly supported, reviewed funding programme facts."""
    evidence = []
    for fact in facts:
        match = re.search(re.escape(fact), text, re.IGNORECASE)
        if not match:
            raise AdapterError("Changed programme facts: " + key, "parse")
        evidence.append(text[match.start() : match.end()])
    record = make_record(
        title,
        url,
        [category],
        kind="opportunity" if deadline else "programme-overview",
        evidence=evidence,
        host_countries=list(hosts),
    )
    record["id"] = hashlib.sha256(f"{SOURCE_ID}|{key}".encode()).hexdigest()[
        :24
    ]
    record["summary"] = summary
    record["eligible_countries"] = list(eligible)
    record["deadline"] = deadline
    today = datetime.now(timezone.utc).date().isoformat()
    if deadline:
        record["status"] = (
            "expired"
            if deadline < today
            else "unknown" if opening and opening > today else "open"
        )
    elif rolling:
        record["status"] = "open"
    return record


def read_pages():
    """Read the bounded, publicly linked scholarship and mobility inventory."""
    urls = {
        "mobility": SOURCE_URL,
        "hub": WEBSITE_URL,
        "point": "https://cuni.cz/UKEN-924.html",
        "havel": "https://cuni.cz/UKEN-2329.html",
        "international": "https://cuni.cz/UKEN-2497.html",
        "foundations": "https://cuni.cz/UKEN-2528.html",
        "barrande": "https://cuni.cz/UKEN-2330.html",
        "jasso": "https://cuni.cz/UKEN-2338.html",
        "mext": "https://cuni.cz/UKEN-2339.html",
        "germany": "https://cuni.cz/UKEN-2396.html",
        "erasmus": "https://cuni.cz/UKEN-2366.html",
        "rates": "https://cuni.cz/UKEN-2583.html",
        "travel": "https://cuni.cz/UKEN-2584.html",
        "supplement": "https://cuni.cz/UKEN-2585.html",
        "global": "https://cuni.cz/UKEN-2361.html",
        "carolina": "https://centrumcarolina.cuni.cz/CAENG-70.html",
        "acute": "https://cuni.cz/UKEN-1398.html",
        "mff": "https://www.mff.cuni.cz/en/admissions/scholarships",
        "fsv": (
            "https://fsv.cuni.cz/en/admissions/"
            "scholarships-funding-and-fees/development-scholarships"
        ),
    }
    base = "https://www.mff.cuni.cz/en/admissions/scholarships/"
    for key, slug in (
        ("cs", "cstf"),
        ("math", "prague-mathematics-tuition-fee"),
        ("physics", "prague-physics-tuition-fee"),
        ("linguistics", "computational-linguistics"),
    ):
        urls[key] = (
            base
            + slug
            + (
                "-scholarship-2026"
                if key == "linguistics"
                else "-scholarships-2026"
            )
        )
    pages = {key: (url, content(page(url), url)) for key, url in urls.items()}
    pages["german-call"] = german_document(pages["germany"])
    return pages


# These data are reviewed factual summaries, not copied publisher descriptions.
INTERNATIONAL = (
    (
        "aktion",
        "Aktion",
        (
            "MA/PhD final-thesis mobility to Austria for 1–5 months; "
            "EUR1300 monthly. Application months March/October have no "
            "stated year."
        ),
        ("MA and PhD students", "EUR 1,300 per month"),
        ("AT",),
    ),
    (
        "daad-summer",
        "DAAD summer-course scholarship",
        (
            "Approximately one-month course in Germany with "
            "course/accommodation covered and a travel allowance; "
            "yearless October application deadline."
        ),
        ("participation in a summer course", "accommodation covered"),
        ("DE",),
    ),
    (
        "daad-masters",
        "DAAD Study Scholarships: Master’s Degree Programmes in All Fields",
        (
            "Full/partial Master’s study in Germany for 10–24 months; "
            "EUR992 monthly plus travel/study allowance; November has "
            "no stated application year."
        ),
        ("10–24 months", "EUR 992 per month"),
        ("DE",),
    ),
    (
        "daad-research",
        "DAAD Research Grants",
        (
            "Research projects in Germany for doctoral candidates "
            "(2–12 months) or postdoctoral researchers (2–6 months); "
            "EUR1400 monthly plus travel allowance, yearless November "
            "deadline."
        ),
        ("2–12 months", "2–6 months", "EUR 1,400 per month"),
        ("DE",),
    ),
    (
        "daad-cotutelle",
        "DAAD Research Grants for Doctoral Candidates within COTUTELLE",
        (
            "Doctoral cotutelle research in Germany for 7–24 months, "
            "EUR1400 monthly; yearless November application deadline."
        ),
        ("within COTUTELLE", "7–24 months"),
        ("DE",),
    ),
    (
        "future-fund",
        "Czech-German Future Fund study scholarship",
        (
            "BA/MA/PhD humanities/social-science Czech–German research "
            "in Germany for 1–2 semesters. BA EUR650/month, MA/PhD "
            "EUR900/month plus matching one-time materials/travel "
            "contribution; January deadline is yearless."
        ),
        ("Czech-German topics", "EUR 900 per month", "EUR 650 per month"),
        ("DE",),
    ),
    (
        "btha-postgraduate",
        "BTHA postgraduate study funding",
        (
            "Consecutive Master’s, doctoral/postdoctoral study at "
            "Bavarian public universities; EUR992/month for one "
            "academic year, renewable twice (maximum three years); "
            "December deadline has no year."
        ),
        (
            "Funding for Postgraduate Study Programmes",
            "EUR 992 per month",
            "maximum total of 3 years",
        ),
        ("DE",),
    ),
    (
        "btha-summer",
        "BTHA language-course and summer-school funding",
        (
            "Approximately one-month Bavarian summer/language course "
            "with participation and accommodation covered; March "
            "deadline has no year."
        ),
        (
            "Language Courses and Summer Schools in Bavaria",
            "accommodation covered",
        ),
        ("DE",),
    ),
    (
        "gfps",
        "GFPS study and internship scholarship",
        (
            "BA/MA/PhD applicants no older than30; German "
            "study-project stay or internship (three work days/week, "
            "remaining time study). EUR650/month up to five months; "
            "May/October application months are yearless."
        ),
        (
            "not be older than 30 years",
            "EUR 650 per month",
            "maximum of 5 months",
        ),
        ("DE",),
    ),
    (
        "kaad",
        "KAAD scholarship",
        (
            "MA/PhD study/research in Germany requires church/social "
            "engagement, interreligious openness and B1 German. "
            "Master’s up to24 months, doctorate up to36, doctoral "
            "research up to12; actual-cost funding, amount "
            "unspecified; recommended six-month application lead."
        ),
        (
            "church and social engagement",
            "minimum level of B1",
            "up to 36 months",
        ),
        ("DE",),
    ),
    (
        "cern",
        "CERN Doctoral Student Programme",
        (
            "Doctoral dissertation/employment in Switzerland for12–36 "
            "months in applied physics, engineering or computing; "
            "publisher indicative CHF3891/month plus benefits. No "
            "current-cycle deadline stated."
        ),
        ("Applied Physics, Engineering, or Computing", "CHF 3,891 per month"),
        ("CH",),
    ),
    (
        "barrande",
        "Barrande Fellowship",
        (
            "PhD mobility to France: cotutelle up to three years (five "
            "funded months/year), or1–3-month research stay. "
            "Non-Francophone applicants may participate with "
            "supervisor agreement to English. Current hub quotes "
            "EUR1690/1704 monthly, older linked detail quotes EUR1500; "
            "both claims retained, no definitive amount selected. "
            "January deadline is yearless."
        ),
        ("EUR 1,690 per month", "EUR 1,704 per month", "non-Francophone"),
        ("FR",),
    ),
    (
        "ceepus",
        "CEEPUS mobility scholarship",
        (
            "BA/MA/PhD exchange in Central/South-Eastern Europe: "
            "thesis1–2 months, semester3–10 months, study visit3–5 "
            "days or summer school6–30 days. Host-paid amounts and "
            "deadlines vary; no exact country list inferred."
        ),
        ("Central and South-Eastern Europe", "amounts vary"),
        (),
    ),
    (
        "fulbright",
        "Fulbright study and research funding",
        (
            "MA/PhD study, internship or research in the United "
            "States; full degrees or short stays up to12 months. "
            "Academic excellence and civic engagement emphasized; "
            "programme-specific amounts/deadlines unstated."
        ),
        ("civic engagement", "up to 12 months"),
        ("US",),
    ),
)
FOUNDATIONS = (
    (
        "sophia",
        "Sophia Foundation study funding",
        (
            "BA/MA study abroad, primarily economics/law but other "
            "disciplines eligible. Up to CZK15000 one-time "
            "expense-based funding; Czech application, multiple "
            "application rounds with no dates stated."
        ),
        ("Sophia Foundation",),
        (),
    ),
    (
        "education-foundation",
        "Foundation for the Development of Education scholarship",
        (
            "BA/MA applicants up to25 whose Erasmus/other-exchange "
            "funding is insufficient; technical/medical fields "
            "preferred. CZK10000–20000, apply two months before "
            "departure; no fixed date stated."
        ),
        ("Foundation for the Development of Education",),
        (),
    ),
    (
        "bakala",
        "Bakala Foundation scholarship",
        (
            "Excellent BA/MA applicants under34; amount depends on "
            "family finances and destination. Yearless November "
            "application month."
        ),
        ("Bakala Foundation",),
        (),
    ),
    (
        "bader",
        "Bader Scholarship",
        (
            "PhD history/theory of art/architecture support for "
            "international study, internship, travel, equipment or "
            "literature; USD12500 award, usually January without a "
            "stated year. Three awards are capacity, not separate "
            "opportunities."
        ),
        ("Bader Scholarship",),
        (),
    ),
    (
        "experentia",
        "Experentia chemistry scholarship",
        (
            "Organic/bioorganic/medicinal chemistry overseas research "
            "up to12 months for applicants up to35 with recent "
            "doctoral qualification. Publisher groups PhD/postdoctoral "
            "applicants but also states recent-PhD completion; "
            "qualification ambiguity retained. March has no stated "
            "year."
        ),
        ("Experentia",),
        (),
    ),
    (
        "sylff",
        "SYLFF doctoral scholarship",
        (
            "Full-time social-science/humanities PhD applicants "
            "through third year, with societal-impact leadership "
            "potential. USD12500/year includes international mobility; "
            "usually late April without a year, apply through faculty "
            "coordinators."
        ),
        ("SYLFF",),
        (),
    ),
    (
        "visegrad",
        "Visegrad scholarship",
        (
            "Master’s/postgraduate citizens of "
            "Czechia/Hungary/Poland/Slovakia studying in another "
            "Visegrad country or explicitly listed "
            "eastern/southeastern partners. EUR3500 per semester "
            "for1–2 semesters; April has no stated year, own-country "
            "study excluded."
        ),
        ("Visegrad Fund",),
        (),
    ),
)


COUNTRY_NAMES = {
    "United Arab Emirates": "AE",
    "Andorra": "AD",
    "Afghanistan": "AF",
    "Antigua and Barbuda": "AG",
    "Albania": "AL",
    "Armenia": "AM",
    "Angola": "AO",
    "Argentina": "AR",
    "Australia": "AU",
    "Azerbaijan": "AZ",
    "Bosnia and Herzegovina": "BA",
    "Barbados": "BB",
    "Bangaldesh": "BD",
    "Belgium": "BE",
    "Burkina Faso": "BF",
    "Bulgaria": "BG",
    "Bahrain": "BH",
    "Burundi": "BI",
    "Benin": "BJ",
    "Brunei": "BN",
    "Bolivia": "BO",
    "Brazil": "BR",
    "Bahamas": "BS",
    "Bhutan": "BT",
    "Botswana": "BW",
    "Belarus": "BY",
    "Belize": "BZ",
    "Canada": "CA",
    "Democratic Republic of Congo": "CD",
    "Democratic Republic of the Congo": "CD",
    "Central African Republic": "CF",
    "Republic of Congo": "CG",
    "Switzerland": "CH",
    "Republic of Côte d'Ivoire": "CI",
    "Cook Islands": "CK",
    "Chile": "CL",
    "Cameroon": "CM",
    "China": "CN",
    "Colombia": "CO",
    "Costa Rica": "CR",
    "Cuba": "CU",
    "Cape Verde": "CV",
    "Cyprus": "CY",
    "Czech Republic": "CZ",
    "Germany": "DE",
    "Djibouti": "DJ",
    "Denmark": "DK",
    "Dominica": "DM",
    "Algeria": "DZ",
    "Ecuador": "EC",
    "Estonia": "EE",
    "Egypt": "EG",
    "Eritrea": "ER",
    "Spain": "ES",
    "Ethiopia": "ET",
    "Finland": "FI",
    "Fiji": "FJ",
    "Federated States of Micronesia": "FM",
    "Faroe Islands": "FO",
    "France": "FR",
    "Gabon": "GA",
    "United Kingdom": "GB",
    "Grenada": "GD",
    "Georgia": "GE",
    "Ghana": "GH",
    "Gambia": "GM",
    "Guinea": "GN",
    "Equatorial Guinea": "GQ",
    "Greece": "GR",
    "Guatemala": "GT",
    "Guinea-Bissau": "GW",
    "Guyana": "GY",
    "Hong Kong": "HK",
    "Honduras": "HN",
    "Croatia": "HR",
    "Haiti": "HT",
    "Hungary": "HU",
    "Indonesia": "ID",
    "Ireland": "IE",
    "Israel": "IL",
    "India": "IN",
    "Iraq": "IQ",
    "Iran": "IR",
    "Iceland": "IS",
    "Italy": "IT",
    "Jamaica": "JM",
    "Jordan": "JO",
    "Japan": "JP",
    "Kenya": "KE",
    "Kyrgyzstan": "KG",
    "Cambodia": "KH",
    "Kiribati": "KI",
    "Comoros": "KM",
    "Saint Kitts and Nevis": "KN",
    "Democratic People's Republic of Korea": "KP",
    "Kuwait": "KW",
    "Lao People's Democratic Republic": "LA",
    "Lebanon": "LB",
    "Saint Lucia": "LC",
    "Liechtenstein": "LI",
    "Sri Lanka": "LK",
    "Liberia": "LR",
    "Lesotho": "LS",
    "Lithuania": "LT",
    "Luxembourg": "LU",
    "Latvia": "LV",
    "Libya": "LY",
    "Morocco": "MA",
    "Monaco": "MC",
    "Moldova": "MD",
    "Montenegro": "ME",
    "Madagascar": "MG",
    "Marshall Islands": "MH",
    "North Macedonia": "MK",
    "Mali": "ML",
    "Myanmar": "MM",
    "Mongolia": "MN",
    "Macao": "MO",
    "Mauritania": "MR",
    "Malta": "MT",
    "Mauritius": "MU",
    "Maldives": "MV",
    "Malawi": "MW",
    "Mexico": "MX",
    "Malaysia": "MY",
    "Mozambique": "MZ",
    "Namibia": "NA",
    "Niger": "NE",
    "Nigeria": "NG",
    "Nicaragua": "NI",
    "Netherlands": "NL",
    "Norway": "NO",
    "Nepal": "NP",
    "Nauru": "NR",
    "Niue": "NU",
    "New Zealand": "NZ",
    "Oman": "OM",
    "Panama": "PA",
    "Peru": "PE",
    "Papua New Guinea": "PG",
    "Philippines": "PH",
    "Pakistan": "PK",
    "Poland": "PL",
    "Palestine": "PS",
    "Portugal": "PT",
    "Palau": "PW",
    "Paraguay": "PY",
    "Qatar": "QA",
    "Romania": "RO",
    "Serbia": "RS",
    "Territory of Russia recognized under international law": "RU",
    "Rwanda": "RW",
    "Saudi Arabia": "SA",
    "Solomon Islands": "SB",
    "Seychelles": "SC",
    "Sudan": "SD",
    "Sweden": "SE",
    "Singapore": "SG",
    "Slovenia": "SI",
    "Slovakia": "SK",
    "Sierra Leone": "SL",
    "San Marino": "SM",
    "Senegal": "SN",
    "Somalia": "SO",
    "Suriname": "SR",
    "South Sudan": "SS",
    "Saint Thomas and Principe": "ST",
    "El Salvador": "SV",
    "Syria": "SY",
    "Swaziland": "SZ",
    "Chad": "TD",
    "Togo": "TG",
    "Thailand": "TH",
    "Tajikistan": "TJ",
    "East Timor": "TL",
    "Turkmenistan": "TM",
    "Tunisia": "TN",
    "Tonga": "TO",
    "Turkey": "TR",
    "Trinidad and Tobago": "TT",
    "Tuvalu": "TV",
    "Taiwan": "TW",
    "Tanzania": "TZ",
    "Territory of Ukraine recognized under international law": "UA",
    "Uganda": "UG",
    "United States of America": "US",
    "Uruguay": "UY",
    "Uzbekistan": "UZ",
    "Vatican City State": "VA",
    "Saint Vincent and the Grenadines": "VC",
    "Venezuela": "VE",
    "Vietnam": "VN",
    "Vanuatu": "VU",
    "Samoa": "WS",
    "Kosovo": "XK",
    "Yemen": "YE",
    "South Africa": "ZA",
    "Zambia": "ZM",
    "Zimbabwe": "ZW",
    "Austria": "AT",
}


def named_countries(names, excluded=()):
    """Normalize actual destination names, retaining declared exclusions."""
    countries = set()
    for name in names:
        name = name.strip().rstrip(".")
        if name not in COUNTRY_NAMES:
            raise AdapterError("Unreviewed destination name: " + name, "parse")
        code = COUNTRY_NAMES[name]
        if code not in excluded:
            countries.add(code)
    return sorted(countries)


def erasmus_hosts(pages):
    """Read both the traditional and international actual destination lists."""
    rate_node = pages["rates"][1]
    table = one(
        [
            item
            for item in nodes(rate_node, "table")
            if "Group of countries" in item.text()
        ],
        "Erasmus destination rate table",
    )
    groups = []
    for row in nodes(table, "tr"):
        cells = nodes(row, "td")
        if cells and "," in cells[0].text():
            groups.extend(cells[0].text().split(","))
    standard = named_countries(groups)
    if len(standard) != 33:
        raise AdapterError("Changed Erasmus rate-group inventory", "parse")
    global_text = pages["global"][1].text()
    start = "Partner countries of Region 14: "
    middle = "Partner countries in the region 13: "
    last = "Partner countries from region 1–12: "
    try:
        part = global_text.split(start, 1)[1]
        a, part = part.split(middle, 1)
        b, part = part.split(last, 1)
        c, _ = part.split(" ATTENTION!", 1)
    except (IndexError, ValueError) as error:
        raise AdapterError(
            "Changed international destination list", "parse"
        ) from error
    international = named_countries(
        (a + "," + b + "," + c).split(","), excluded=("BY", "RU")
    )
    return standard, international


REVIEWED_CONDITIONS = {
    "mobility": (
        "e0f3b14bb97b0c52212cbbc4720481c1cb330678f1350f1061ea0a30d22894d4"
    ),
    "hub": ("5255299491cb7b43b89fad7f9e088bb0a914ddbb123cb469f707113afff43883"),
    "point": (
        "12bc51e5a885417939faf9c98f09cc260aab9c9f386f7cdd3bc41646f066c80f"
    ),
    "havel": (
        "34125e878d31e1f1509118599d3f3e6941caf209b400f5849c27d0f4fe0dddee"
    ),
    "international": (
        "01a034298c6997f0a3479f84e6f4b7741817c66d540b87a78e04b5a40e5df034"
    ),
    "foundations": (
        "692497332ce71bbe9476c2501a6d6f2bb7ca9e41a25c476162d027b6e4852835"
    ),
    "barrande": (
        "a4befd9d13543684d5d7ef630a775106a438f4d39e7abae69a97ef1fff00b766"
    ),
    "jasso": (
        "71fec10dce9c6aad65e0347ff6123de309fd40b11c929413da742657e6dde13c"
    ),
    "mext": (
        "3da5a9b61c0562aa00dd8b5a3ebe6bde9bdca64aea3ceace9ca439005fa86f76"
    ),
    "germany": (
        "17a034abc608a556b4f5594999780eea6df072e4283e7e8e93c45d5e242b20aa"
    ),
    "erasmus": (
        "e24f4436caaa6d9fc5bdd28ce2c2fd2b1d5ab318e7f20b729565a8f05a45a7ff"
    ),
    "rates": (
        "1c8f180efbf5a22f1d4f3458cc5cdb6beb4252f24585f4b63d7e3b5c9ffaf8d6"
    ),
    "travel": (
        "d72fb332ba656ffe5b441e8b0595ac7c377051d6ab94209beed81997f5533838"
    ),
    "supplement": (
        "458b944fc51a7de3e23e14e86e61ba2dc5e6d4875dd472d68d066ebde822de2a"
    ),
    "global": (
        "e2071e5eac6dc7ac364f21af523b70fb2f37fe9f6ddab307f36508fca162023e"
    ),
    "carolina": (
        "9f41f062d6153de1832d1b5220a55e746c97753a3a408d7e3127c8b72dbb4042"
    ),
    "acute": (
        "fb39f44d5f9df587f85dd09f1a05651c3492f22c388690293f6d0044d4cd3ab0"
    ),
    "mff": ("13fdac2620fa5c0f29f283a46fa767c6f65d933505d080bdf7dc9d98c3e54624"),
    "fsv": ("81582bed2f125c6dcaea478a2fdf6166c378eb4aa2f4fd8fa7eca4a002cbee3a"),
    "cs": ("e3aba2b8cbe82ad391cde69a2af96d416f3d3d434fe7330b082d16a6032de305"),
    "math": (
        "9f67c4336d1d007b32b78e01e62c8ed2e93d981b2be63a1fe051e97543c39498"
    ),
    "physics": (
        "9e571bf01d72b4de4e4ce78b03d2fb92b5698cac9071b0c71fcd604a112700a3"
    ),
    "linguistics": (
        "75cefe115574bb8f20bb37e113a92819b261583eeb6b065ecdb4831b2025d06b"
    ),
}


def parse_inventory(pages):
    """Collect genuine programmes and calls from bounded own-publisher pages."""
    for key, expected in REVIEWED_CONDITIONS.items():
        text = pages[key][1].text().split("Last change:")[0]
        if hashlib.sha256(text.encode()).hexdigest() != expected:
            raise AdapterError(
                "Publisher conditions changed; review source page: " + key,
                "parse",
            )
    records = []

    def add(page_key, key, title, facts, summary, **options):
        url, node = pages[page_key]
        result = programme(
            key, title, url, node.text(), facts, summary, **options
        )
        records.append(result)
        return result

    mobility = add(
        "mobility",
        "mobility-autumn-2026",
        "Charles University Mobility Fund – Autumn 2026",
        ("October 30, 2026, 2:00 p.m.", "October 1, 2026", "Mobility Fund"),
        (
            "Autumn 2026 partial worldwide mobility grant for full-time CU "
            "BA/MA/PhD students within standard duration +1 year. "
            "Study/internship ≥30 days; short PhD stays ≤29 days; medical "
            "Master’s traineeships 21–29 days. Stays 1 Nov 2026–30 Sep 2027; "
            "no retrospective or Erasmus+/GAUK funding. Acceptance/academic "
            "approval required; study category needs B2 with exemptions. "
            "Long stays: CZK 15,000/month Europe or 20,000 outside, "
            "max 70,000; "
            "short rates vary. Deadline 30 Oct 2026 at 14:00 (zone unstated, "
            "date-only); faculty 20–30 Oct."
        ),
        deadline="2026-10-30",
        opening="2026-10-01",
    )
    mobility["classification"]["evidence"].append(
        "Detailed eligibility and benefits: "
        + (
            "Autumn2026 partial mobility grant for full-time CU "
            "BA/MA/PhD students within standard study period plus one "
            "year. One application call covers study/internship of at "
            "least30 days, short PhD mobility up to29 days and medical "
            "Master’s traineeships21–29 days. Stays "
            "November2026–September2027; no retrospective funding or "
            "concurrent Erasmus+/GAUK. Acceptance and academic "
            "approval required; study category requires B2 language "
            "evidence with stated exceptions. Indicative monthly "
            "support CZK15000 Europe/20000 outside, maximum70000 for "
            "long stays; short categories CZK10000–20000 "
            "Europe/15000–25000 outside. Faculty "
            "deadlines20–30October; central deadline October30,2026 "
            "at14:00, timezone unstated; date-only normalized."
        )
    )
    add(
        "hub",
        "doctoral-income",
        "Charles University doctoral studying income",
        ("1.2", "full-time"),
        (
            "Full-time doctoral study during the standard four-year "
            "period qualifies for doctoral studying income of at "
            "least1.2 times the Czech minimum wage. Income may combine "
            "scholarship and salary; exact wage/amount not inferred."
        ),
        hosts=("CZ",),
    )
    add(
        "hub",
        "stars",
        "STARS doctoral scholarship",
        ("STARS scholarship", "talented PhD students"),
        (
            "Charles University’s own scholarship page identifies a "
            "Faculty of Science scholarship for talented PhD students. "
            "Amount, application dates and further conditions are not "
            "stated on this accessible page. Linked STARS detail has a "
            "bot-verification gate and is not accessed by the "
            "collector."
        ),
    )
    add(
        "hub",
        "gi-bill",
        "GI Bill at Charles University Faculty of Social Sciences",
        ("GI Bill", "U.S. veterans only"),
        (
            "Charles University explicitly states that its Faculty of "
            "Social Sciences participates in GI Bill for U.S. veterans "
            "only. Veteran status is the supported condition; "
            "citizenship, funding amount and application dates are "
            "unspecified. This is a limited programme overview, not a "
            "claim of an open application call."
        ),
    )
    add(
        "havel",
        "havel",
        "Václav Havel Bursary",
        ("repression",),
        (
            "International students whose studies are hindered by "
            "oppressive regimes may seek CU financial support. The "
            "detailed page requires admission/enrolment in a CU degree "
            "programme; the general hub also mentions foundation "
            "programmes. Contributions to Czech-language preparation "
            "may be possible. Evidence of repression and translated "
            "documents required. Application dates March31/October31 "
            "have no stated year; amount unknown."
        ),
        hosts=("CZ",),
    )
    add(
        "fsv",
        "fsv-2026-27",
        "FSV UK SCHOLARS scholarship 2026/2027",
        ("April 30, 2026", "75 000"),
        (
            "One-time approximately CZK75000 living-cost support for "
            "financially disadvantaged fee-paying Faculty of Social "
            "Sciences BA/MA students. Apply as incoming student, BA "
            "year1/2 or MA year1 for the next year. No tuition waiver; "
            "renewal needs a new application. English application, "
            "motivation up to1000 words and academic recommendation "
            "required; programme application/payment also due "
            "April30,2026. Payment after winter registration and "
            "tuition settlement, no later than December2026 subject to "
            "conditions."
        ),
        deadline="2026-04-30",
        hosts=("CZ",),
    )
    for key, title, summary, facts, hosts in INTERNATIONAL:
        add("international", key, title, facts, summary, hosts=hosts)
    for key, title, summary, facts, hosts in FOUNDATIONS:
        item = add("foundations", key, title, facts, summary, hosts=hosts)
        if key == "visegrad":
            item["eligible_countries"] = ["CZ", "HU", "PL", "SK"]
            item["host_countries"] = [
                "AL",
                "AM",
                "AZ",
                "BA",
                "BY",
                "CZ",
                "GE",
                "HU",
                "MD",
                "ME",
                "MK",
                "PL",
                "RS",
                "SK",
                "UA",
                "XK",
            ]
    add(
        "jasso",
        "jasso",
        "JASSO scholarship",
        ("80,000",),
        (
            "Students admitted to a Japanese partner university may "
            "receive JPY80000/month for a stay up to12 months. "
            "Selection occurs during the host application process; no "
            "fixed current deadline stated."
        ),
        hosts=("JP",),
    )
    add(
        "mext",
        "mext",
        "MEXT partner-university scholarship",
        ("117,000",),
        (
            "Japanese government support for a one-year stay at "
            "selected partner universities beginning October of the "
            "following academic year. JPY117000/month plus travel "
            "support; December/January annual announcements are "
            "yearless. One-student partner capacity is not a separate "
            "scholarship."
        ),
        hosts=("JP",),
    )
    add(
        "erasmus",
        "erasmus",
        "Erasmus+ student mobility funding",
        ("scholarship",),
        (
            "Partial living-cost funding for approved Erasmus+ student "
            "mobility, subject to a participant agreement. Long "
            "study/traineeship grants depend on destination "
            "(EUR540–660 or690–810/month); short mobility is EUR79/day "
            "for days1–14 and56/day for days15–30. Current2026/27 "
            "distance-based travel rates rangeEUR28–1735 standard "
            "or56–1735 green; green travel needs an environmentally "
            "friendly journey in at least one direction. No generic "
            "application deadline stated."
        ),
    )
    add(
        "supplement",
        "erasmus-supplement",
        "Erasmus+ supplementary scholarship",
        ("limited opportunities",),
        (
            "Additional conditional mobility funding for students with "
            "special needs or limited opportunities, including "
            "disability, financial disadvantage, children, dietary "
            "restrictions or orphan’s pension. Supporting "
            "documentation is required; no fixed amount/deadline "
            "stated."
        ),
    )
    add(
        "global",
        "erasmus-international",
        "Erasmus+ international mobility scholarship",
        ("700 EUR/month", "60 days"),
        (
            "CU-enrolled accredited BA/MA/PhD students may apply "
            "through faculty selection for funded2–12-month mobility "
            "where traditional Erasmus is unavailable. Total Erasmus "
            "participation maximum12 months per cycle, or24 for "
            "undivided Master’s. Region13/14 study EUR660/month and "
            "traineeship810; regions1–12 EUR700/month. No travel to "
            "Belarus/Russia allowed despite their appearance in the "
            "regional list; no fixed application date stated."
        ),
    )
    add(
        "acute",
        "acute",
        "Bursary for Students in Acute Difficulties",
        ("CZK 100,000", "throughout the calendar year"),
        (
            "Discretionary emergency assistance for CU students when "
            "other assistance is unavailable or insufficient. "
            "Circumstances include bereavement, dependent "
            "care/parenthood, health issues, disasters/accidents or "
            "crime. Applications with explanation/evidence may go to "
            "dean and Rector; lump-sum amount depends on need, "
            "combined annual maximumCZK100000. Rector applications "
            "accepted throughout the calendar year; no automatic "
            "entitlement."
        ),
        rolling=True,
    )
    add(
        "carolina",
        "ukraine-emergency",
        "Bursary for Students in Acute Difficulties related to Ukraine",
        ("crisis in Ukraine", "monthly income and expenses"),
        (
            "Emergency support related to the Ukraine crisis requires "
            "a signed posted application describing monthly income, "
            "expenses and intended use. Financial/residence evidence "
            "requested; affidavit possible if documents unavailable. "
            "No amount, fixed application date or citizenship "
            "requirement stated."
        ),
    )
    add(
        "carolina",
        "accommodation",
        "Charles University accommodation bursary",
        ("Accommodation bursaries", "at most two incidents"),
        (
            "Requested support for uninterrupted full-time students "
            "not studying as foreign-language self-payers, with at "
            "most two academic failures and within standard study "
            "duration plus one year. Basic support requires residence "
            "outside the teaching district (outside Prague when "
            "studying there); only one bursary for concurrent "
            "programmes. No amount/current deadline stated."
        ),
    )
    add(
        "carolina",
        "hlavka",
        "Josef, Marie, and Zdenka Hlávka Foundation Scholarship",
        ("not automatically available", "standard period"),
        (
            "Conditional support for students already included in the "
            "foundation’s programme for student-citizens permanently "
            "living abroad. Enrollment in the relevant study unit and "
            "standard programme duration required. Not automatically "
            "available; exact nationality, amount and deadline "
            "unstated."
        ),
    )
    add(
        "carolina",
        "eu4belarus",
        "EU4Belarus scholarship",
        ("Belarusian students", "2020–2022"),
        (
            "For Belarusian students displaced by repression "
            "during2020–2022 who began university study in Lithuania, "
            "Latvia, Czechia or Poland no earlier than2020/2021. No "
            "current application dates or benefit amounts stated on "
            "CU’s page."
        ),
        hosts=("LT", "LV", "CZ", "PL"),
        eligible=("BY",),
    )
    for key, title, field in (
        (
            "cs-ba",
            "Computer Science Tuition Fee Scholarship – Bachelor’s 2026",
            "Bachelor",
        ),
        (
            "cs-ma",
            "Computer Science Tuition Fee Scholarship – Master’s 2026",
            "Master",
        ),
        (
            "math",
            "Prague Mathematics Tuition Fee Scholarship 2026",
            "Mathematics",
        ),
        ("physics", "Prague Physics Tuition Fee Scholarship 2026", "Physics"),
        (
            "linguistics",
            "Computational Linguistics Scholarship 2026",
            "Language Technologies",
        ),
    ):
        source = "cs" if key.startswith("cs-") else key
        add(
            source,
            "mff-" + key,
            title,
            ("2026", field),
            "Academic-excellence award for applicants to the English-taught "
            + field
            + " programme at CU Mathematics and Physics starting2026/27. "
            + "Tuition coverage EUR4200/year for EU students or7100 otherwise, "
            + (
                "for three Bachelor’s years. "
                if key == "cs-ba"
                else "for two Master’s years. "
            )
            + "English application documents include CV, motivation, "
            + "transcripts and two recommendations, due April30,2026. "
            + (
                "No other external living-allowance scholarship/grant; "
                if key == "linguistics"
                else (
                    "New entrants only; no other "
                    "scholarship/maintenance grant; "
                )
            )
            + "continued support requires full-time study and progress."
            + (
                " CZK120000 living allowance/year; first-semester progress "
                "may reduce support."
                if key == "linguistics"
                else ""
            ),
            hosts=("CZ",),
            deadline="2026-04-30",
        )
    add(
        "mff",
        "mff-math-phd",
        "Mathematics PhD financial support programme",
        ("20000 CZK", "25000 CZK"),
        (
            "Active mathematics PhD students are offered guaranteed "
            "monthly tax-exempt incomeCZK20000; projects/teaching may "
            "increase income, typical total approximately25000. "
            "Contact department or vice-dean; no fixed application "
            "date stated."
        ),
        hosts=("CZ",),
    )
    add(
        "mff",
        "mff-cs-phd",
        "Computer Science PhD employment programme",
        ("20,000 CZK", "12,500 CZK", "separate application"),
        (
            "Computer Science PhD students supervised in the faculty "
            "may apply separately for academic-assistant employment. "
            "Minimum salaryCZK20000/month plus tax-free stipend12500; "
            "project participation can increase income. "
            "Published2024/25 median project increment11000 is "
            "historical context, not guaranteed current benefit. No "
            "fixed application deadline stated."
        ),
        hosts=("CZ",),
        category="jobs",
    )
    standard_hosts, global_hosts = erasmus_hosts(pages)
    for record in records:
        if record["title"] == "Erasmus+ student mobility funding":
            record["host_countries"] = standard_hosts
        elif record["title"] == "Erasmus+ international mobility scholarship":
            record["host_countries"] = global_hosts
    records.extend(german_programmes(pages))
    validate_records(records)
    return records


def collect():
    """Return one complete validated, non-publishing collection."""
    return parse_inventory(read_pages())


def german_document(germany):
    """Read the actually linked application notice without opening logins."""
    url, node = germany
    link = one(
        [
            link
            for link in nodes(node, "a")
            if link.text().startswith("Announcement of monthly scholarships")
        ],
        "Germany monthly call notice",
    )
    target = urllib.parse.urljoin(url, link.attrs.get("href", ""))
    parsed = urllib.parse.urlsplit(target)
    if parsed.hostname != "cuni.cz" or not parsed.path.endswith(".docx"):
        raise AdapterError("Unexpected Germany application notice", "parse")
    delay = check_robots(target)
    status, _, body = request_bytes(target, interval=delay)
    if status != 200:
        raise AdapterError(
            "Germany application notice unavailable", "fetch", status
        )
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > 2_000_000:
                raise AdapterError("Oversized Germany notice", "parse")
            root = ET.fromstring(archive.read(info))
        namespace = (
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        )
        paragraphs = []
        for paragraph in root.iter(namespace + "p"):
            paragraphs.append(
                "".join(
                    item.text or "" for item in paragraph.iter(namespace + "t")
                )
            )
        plain = " ".join(" ".join(paragraphs).split())
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as error:
        raise AdapterError(
            "Invalid Germany application notice", "parse"
        ) from error
    document = Node()
    document.children = [plain]
    return target, document


def german_programmes(pages):
    """Preserve individual university funding routes and actual notice dates."""
    url, root = pages["germany"]
    notice_url, notice = pages["german-call"]
    text = notice.text()
    deadline = re.search(r"8\s*th\s+December\s+2025", text)
    if not deadline or "2026" not in text:
        raise AdapterError("Changed Germany monthly application cycle", "parse")
    sections = nodes(root, "div", "overflowHidden")
    if len(sections) != 12:
        raise AdapterError("Changed Germany funding inventory", "parse")
    definitions = (
        (
            4,
            "2",
            "Saarland",
            "semester",
            "934 EUR",
            None,
            "BA/MA semester scholarship EUR934/month; one or two places.",
        ),
        (
            4,
            "2",
            "Saarland",
            "doctoral",
            "1,200 EUR",
            None,
            (
                "Doctoral mobility for several weeks up to three months in "
                "January–December2027, EUR1200/month; one or two places."
            ),
        ),
        (
            5,
            "3",
            "Düsseldorf",
            "semester",
            "934 EUR",
            None,
            (
                "BA/MA semester scholarship EUR934/month; supervisor "
                "confirmation required, one place."
            ),
        ),
        (
            6,
            "5",
            "Hamburg",
            "semester",
            "1,000 EUR",
            None,
            (
                "BA/MA/doctoral semester scholarship EUR1000/month; winter "
                "five months or summer four, three places."
            ),
        ),
        (
            6,
            "5",
            "Hamburg",
            "monthly",
            "1,300 EUR",
            "2025-12-08",
            (
                "One-month scholarships: doctoral EUR1300/month (four "
                "places), academic staff EUR2000 (six places)."
            ),
        ),
        (
            7,
            "13",
            "Heidelberg",
            "semester",
            "700 EUR",
            None,
            (
                "BA/MA/doctoral semester support EUR700/month for five "
                "months, optional pre-semester intensive German; two "
                "places."
            ),
        ),
        (
            7,
            "13",
            "Heidelberg",
            "monthly",
            "1,300 EUR",
            "2025-12-08",
            "Doctoral one-month scholarship EUR1300, one place.",
        ),
        (
            8,
            "7",
            "Cologne",
            "monthly",
            "1,300 EUR",
            "2025-12-08",
            (
                "One-month doctoral EUR1300 or academic staff EUR2000 "
                "scholarship; one place per cohort."
            ),
        ),
        (
            9,
            "14",
            "Bonn",
            "monthly",
            "1,200 EUR",
            "2025-12-08",
            (
                "One-month doctoral EUR1200 scholarship (two places) or "
                "academic staff EUR2000 (one place)."
            ),
        ),
        (
            10,
            "15",
            "Regensburg",
            "monthly",
            "1,300 EUR",
            "2025-12-08",
            (
                "Doctoral analytical-chemistry scholarship for CU Faculty "
                "of Science EUR1300/month, one place."
            ),
        ),
    )
    result = []
    for index, anchor, university, route, amount, due, facts in definitions:
        summary = (
            "Charles University bilateral funded mobility to "
            + university
            + ", Germany. "
            + facts
            + (
                " Medical and Pharmacy faculties excluded. CV, motivation, "
                "academic acceptance/invitation and study-specific "
                "documents required; apply through the university system. "
            )
            + (
                (
                    "Actual attached2026 notice requires applications by "
                    "December8,2025 despite the hub’s stale 'currently open' "
                    "statement; retained expired."
                )
                if due
                else (
                    "The hub states a February2026 call-publication milestone, "
                    "not an application deadline; deadline unknown."
                )
            )
        )
        record = programme(
            "germany-" + university.lower() + "-" + route,
            university + " " + route + " mobility scholarship",
            url + "#" + anchor,
            sections[index].text(),
            [amount],
            summary,
            hosts=("DE",),
            deadline=due,
        )
        if due:
            record["classification"]["evidence"].append(
                "Application notice: " + notice_url + "; " + deadline.group()
            )
        result.append(record)
    return result


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
    publisher = (
        "cuni.cz" if host == "cuni.cz" or host.endswith(".cuni.cz") else host
    )
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
    try:
        request = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers={"User-Agent": _USER_AGENT, **(headers or {})},
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
        if (
            not isinstance(record.get("summary"), str)
            or len(record["summary"].encode("utf-16-le")) // 2 > 600
            or re.search("<[^>]+>", record["summary"])
        ):
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


def metadata(attempt, previous, records, error=None):
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
            previous.get("last_checked_at") if error else attempt
        ),
        "record_count": len(records),
        "message": (
            "Collection failed; last-good data preserved"
            if error
            else (
                f"Collected {len(records)} validated accessible records; "
                "UJOP excluded because its robots policy was unavailable"
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
                last_checked_at=attempt,
            )
        records.sort(key=lambda record: record["id"])
        records.sort(
            key=lambda record: record["published_at"] or "", reverse=True
        )
        validate_records(records)
    except Exception as error:
        failure, records = error, old
    outcome = metadata(attempt, previous, records, failure)
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
