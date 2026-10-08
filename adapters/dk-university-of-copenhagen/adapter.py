"""Collect UCPH scholarship programmes and funded doctoral vacancies."""

import argparse
import base64
from datetime import datetime, timedelta, timezone
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

SOURCE_ID = "dk-university-of-copenhagen"
SOURCE_URL = (
    "https://www.ku.dk/studies/admission/"
    "scholarships-for-full-degree-students"
)
WEBSITE_URL = SOURCE_URL
VACANCIES_URL = "https://employment.ku.dk/phd/"
POLICY_URL = "https://about.ku.dk/cookies-and-privacy-policy/"
LANGUAGE = "en"
PUBLISHER_COUNTRY = "DK"
PUBLISHER_TYPE = "university"
ATTRIBUTION = "University of Copenhagen"


class Node:
    """Minimal HTML tree retaining official links and structural boundaries."""

    def __init__(self, tag="", attrs=()):
        self.tag = tag
        self.attrs = dict(attrs)
        self.children = []

    def text(self):
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
    return PublisherHTML(fetch_source(url, min_interval=delay)).root


def clipped(text, limit=240):
    """Bound factual excerpts at a word boundary without adding HTML."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"


def programme(title, fragment, summary, evidence, countries=None):
    record = make_record(
        title,
        SOURCE_URL + "#" + fragment,
        ["scholarships"],
        kind="programme-overview",
        host_countries=["DK"],
        method="official-programme-conditions",
        evidence=evidence,
    )
    record["summary"] = summary
    record["location"] = "University of Copenhagen, Denmark"
    record["eligible_countries"] = countries or []
    return record


# Each reviewed programme is supported by the university's own funding page.
# Conditions are checked against its current paragraph before using own prose.
_FUNDING_PROGRAMMES = {
    "General Sir John Monash Scholarships": (
        "John Monash postgraduate scholarships for Australian citizens",
        "Australian citizens may seek support for taught master's or doctoral "
        "study at leading international universities. UCPH lists this named "
        "postgraduate award, but does not state an amount or current deadline.",
        ["Australian citizens", "master", "doctorates"],
        ["AU"],
        [],
    ),
    "Mackenzie King Open Scholarship": (
        "Mackenzie King Open Scholarship for Canadian graduate students",
        "Canadian students may apply for graduate study in Canada or abroad. "
        "The UCPH programme description directs applicants to the "
        "administrator "
        "for detailed eligibility and dates; no current deadline is stated.",
        ["Canadian students", "graduate studies"],
        ["CA"],
        [],
    ),
    "Becas CONICYT": (
        "CONICYT Becas Chile postgraduate scholarship",
        "Chilean citizens may seek support to start or continue postgraduate "
        "study at highly ranked international universities. The university "
        "page supplies no current application date or award amount.",
        ["Chilean citizens", "post-graduate"],
        ["CL"],
        [],
    ),
    "COLFUTURO loan and scholarship program": (
        "COLFUTURO master's loan with conditional scholarship conversion",
        "Colombian citizens may receive a postgraduate-study loan up to "
        "USD 50,000. A scholarship may cover 50% of the loan subject to "
        "conditions, including returning to Colombia after study and remaining "
        "there for three to five years. This is not an unconditional free "
        "grant; "
        "UCPH gives no current application deadline.",
        ["Colombian citizens", "USD 50,000", "50%", "three to five years"],
        ["CO"],
        ["loan", "conditional scholarship"],
    ),
    "Citadel Capital Scholarship Foundation": (
        "Citadel Capital postgraduate scholarship for Egyptian residents",
        "Egyptian citizens residing in Egypt and younger than 35 may seek "
        "master's-study support abroad in any discipline. Recipients must "
        "return to work in Egypt for at least two years after the programme. "
        "UCPH states no current deadline or monetary amount.",
        ["age of 35", "Egyptian citizen", "residing in Egypt", "two years"],
        ["EG"],
        [],
    ),
    "DAAD Annual Scholarships for Graduates of All Disciplines": (
        "DAAD Annual Scholarships for Graduates of All Disciplines",
        "This named DAAD graduate scholarship supports international study "
        "experience across academic disciplines. UCPH does not publish a "
        "current deadline, amount or nationality condition in this paragraph; "
        "the Germany accordion is not treated as proof of citizenship rules.",
        ["graduates", "all disciplines", "international study"],
        [],
        [],
    ),
    "Friedrich- Ebert-Stiftung (FES)": (
        "Friedrich Ebert Foundation postgraduate scholarships",
        "German citizens pursuing postgraduate study in Germany or at European "
        "universities are described as eligible. Selection considers academic "
        "achievement, social and political involvement, and commitment to "
        "social democratic values. No current application date is published.",
        ["German citizens", "postgraduate", "social and political"],
        ["DE"],
        [],
    ),
    "Inlaks Shivdasani Foundation": (
        "Inlaks Shivdasani postgraduate scholarship",
        "Indian citizens with an undergraduate degree and high achievement may "
        "seek support for postgraduate study at a highly ranked overseas "
        "university. The described award covers tuition, living costs and "
        "one-way travel. UCPH states no current deadline or exact amount.",
        ["Indian citizenship", "undergraduate degree", "one-way travel"],
        ["IN"],
        [],
    ),
    "JN Tata Endowment Loan Scholarships": (
        "J. N. Tata Endowment one-time postgraduate loan scholarship",
        "Indian nationals may apply for a one-time loan scholarship at the "
        "start of full-time postgraduate, PhD or postdoctoral study abroad in "
        "any discipline. This is repayable loan support, not an unconditional "
        "grant. UCPH refers repayment terms and dates to the administrator.",
        [
            "one-time loan",
            "Indian nationals",
            "Postgraduate/Ph.D./Postdoctoral",
        ],
        ["IN"],
        ["repayable loan"],
    ),
    "Lady Meherbai D. Tata Education Trust Scholarships": (
        "Lady Meherbai D. Tata Education Trust scholarship",
        "Female Indian graduates of a recognised Indian university may apply "
        "to continue their education at a recognised institution abroad. "
        "Applications may be made while admission is still pending. UCPH "
        "provides no exact award amount or current application deadline.",
        [
            "female Indian graduates",
            "recognized Indian university",
            "awaiting admission",
        ],
        ["IN"],
        [],
    ),
    "The Indonesian Education Scholarship": (
        "Indonesian Education Scholarship for overseas master's study",
        "Indonesian citizens seeking master's study abroad are described as "
        "eligible, and UCPH is explicitly a listed target university. The "
        "university page directs applicants to the administrator for dates "
        "and further conditions; no current deadline is stated here.",
        ["Indonesian citizens", "Master", "listed target university"],
        ["ID"],
        [],
    ),
    "The CONACYT Mexican Scholarship": (
        "CONACYT Mexican postgraduate scholarship",
        "Mexican citizens may seek the Mexican government's monthly award "
        "for graduate study at an international university. UCPH states "
        "neither a monthly monetary amount nor a current application deadline.",
        ["monthly award", "Mexican citizens", "Mexican government"],
        ["MX"],
        [],
    ),
    "FUNED": (
        "FUNED support for Mexican master's students abroad",
        "Mexican citizens may seek financial support for master's study at "
        "leading international universities. UCPH lists information "
        "technology, "
        "international relations, political science, economics and law among "
        "the subject areas. It does not establish an amount, repayment terms "
        "or current deadline; the support is not asserted to be a free grant.",
        ["Mexican citizens", "Master", "information technology", "law"],
        ["MX"],
        ["funding terms unspecified"],
    ),
    "The Tamim bin Hamad Scholarship": (
        "Tamim bin Hamad overseas scholarship",
        "Qatari citizens may seek Qatar-government scholarship support to "
        "study at a recognised university abroad. UCPH is explicitly on the "
        "approved university list. No award amount or dated current call is "
        "stated on the university page.",
        ["Qatari citizens", "approved universities"],
        ["QA"],
        [],
    ),
    "Global Education Programme": (
        "Global Education Programme postgraduate study funding",
        "The named programme supports full-time overseas postgraduate study "
        "in science, engineering, medicine, education and management. It "
        "covers tuition, travel, insurance, accommodation, meals and "
        "literature. "
        "Recipients must return to Russia and work in their qualification "
        "for at least three years. No nationality rule or current date is "
        "published here; the Russia accordion alone proves neither.",
        ["full-time post-graduate", "three years", "return to Russia"],
        [],
        [],
    ),
    "The Singapore Food Agency (SFA) Postgraduate Scholarships": (
        "Singapore Food Agency postgraduate scholarship",
        "The SFA postgraduate award covers full-term or mid-term master's/PhD "
        "coursework or research at international universities. Listed fields "
        "include agricultural and biological sciences, chemistry, food and "
        "plant science, and toxicology. UCPH provides no award amount, dated "
        "call or explicit citizenship rule; country heading is not "
        "eligibility.",
        ["Master", "PhD", "full-term/mid-term", "Toxicology"],
        [],
        [],
    ),
    "Firstrand Laurie Dippenaar Scholarship": (
        "FirstRand Laurie Dippenaar postgraduate scholarship",
        "South African citizens may seek this named postgraduate scholarship "
        "for study at a recognised university outside South Africa. UCPH "
        "states no current deadline or monetary award amount.",
        ["South African citizens", "postgraduate", "outside of South Africa"],
        ["ZA"],
        [],
    ),
    "The Jean Monnet Scholarship Programme": (
        "Jean Monnet Scholarship Programme for EU-harmonisation study",
        "This programme funds graduate-level academic study connected with "
        "Turkey's EU harmonisation process and the EU acquis. UCPH gives no "
        "current deadline, amount or explicit citizenship rule; the Turkey "
        "accordion is not used to infer applicant nationality.",
        ["graduate level", "EU harmonisation", "EU acquis"],
        [],
        [],
    ),
    "The Aga Khan Foundation": (
        "Aga Khan Foundation postgraduate scholarship programme",
        "Outstanding postgraduate applicants from selected developing "
        "countries who lack other study financing may seek this competitive "
        "award. Support is 50% grant and 50% loan, not a wholly non-repayable "
        "scholarship. UCPH does not identify the complete eligible-country "
        "list or a current application deadline.",
        [
            "select developing countries",
            "no other means",
            "50% grant",
            "50% loan",
        ],
        [],
        ["partial loan"],
    ),
    "Women Techmakers Scholars Program": (
        "Women Techmakers Scholars Program",
        "Applicants identifying as female and studying computer science, "
        "computer engineering or a closely related technical subject are "
        "described as eligible. Selection considers academic performance, "
        "leadership and impact on the women-in-tech community. UCPH supplies "
        "no current application date or award amount.",
        ["identify as female", "computer science", "leadership"],
        [],
        [],
    ),
    "The OFID Scholarship Award": (
        "OFID Scholarship Award for development-related master's study",
        "Students from developing countries pursuing a development-related "
        "master's degree at an internationally recognised university are "
        "described as eligible. The award covers tuition and a monthly "
        "allowance. UCPH supplies no complete country list, monetary amount "
        "or current application date.",
        [
            "developing countries",
            "Master",
            "development related",
            "monthly allowance",
        ],
        [],
        [],
    ),
    "Rotary Scholarship": (
        "Rotary Foundation Global Grants scholarship",
        "Applicants require nomination by a local Rotary club. The source "
        "describes generally rolling applications, with possible local club "
        "deadlines. Club presence in 70 countries is not an applicant-country "
        "list. No exact current deadline or award amount is stated by UCPH.",
        ["Global Grants", "nominated by a local Rotary club", "rolling basis"],
        [],
        [],
    ),
}

_EXCLUDED_FUNDING = {
    "The International Scholarships Program (ISP)": "programme database",
    "Fulbright Commission": "duplicate of the detailed UCPH Fulbright route",
    "The American-Scandinavian Foundation (ASF)": (
        "directory of multiple awards"
    ),
    "Federal Student Loans – Title IV": (
        "UCPH explicitly no longer participates"
    ),
    "Private Lenders": "generic lending and certification advice",
    "Veteran Aid – VA": "UCPH explicitly no longer services this funding",
    "The Gen Foundation": "trust/funder profile without a specific named award",
}


def additional_programmes(main):
    """Cover substantive same-page named programmes without external
    requests.
    """
    sections = [
        n
        for n in nodes(main, "section")
        if n.text().startswith(
            (
                "Scholarships and loans opportunities",
                "Scholarships from external institutions",
            )
        )
    ]
    if len(sections) != 2:
        raise AdapterError("External funding sections changed", "parse")
    found = set()
    records = []
    for section in sections:
        for item in nodes(section, "div", "accordion-item"):
            heading = one(nodes(item, "h2"), "funding accordion heading")
            body = one(nodes(item, "div", "accordion-body"), "funding body")
            blocks = []
            if nodes(body, "h3"):
                current = None
                for child in body.walk():
                    if child.tag == "h3":
                        current = [child.text(), []]
                        blocks.append(current)
                    elif child.tag == "p" and current is not None:
                        current[1].append(child.text())
            else:
                blocks.append([heading.text(), [body.text()]])
            for name, paragraphs in blocks:
                if name in found:
                    raise AdapterError("Repeated funding identity", "parse")
                found.add(name)
                if name in _EXCLUDED_FUNDING:
                    continue
                if name not in _FUNDING_PROGRAMMES:
                    raise AdapterError("Unreviewed named funding programme")
                title, summary, required, countries, tags = _FUNDING_PROGRAMMES[
                    name
                ]
                text = " ".join(paragraphs)
                if any(fact not in text for fact in required):
                    raise AdapterError(
                        "Reviewed funding conditions changed: " + name
                    )
                record = programme(
                    title,
                    heading.attrs["id"],
                    summary,
                    [
                        "UCPH-published programme identity: " + name,
                        "External administrator; only this university's "
                        "published conditions are collected.",
                        "No current dated application call is given here.",
                    ],
                    countries=countries,
                )
                record["id"] = hashlib.sha256(
                    (SOURCE_ID + "|programme|" + name).encode()
                ).hexdigest()[:24]
                record["tags"] = tags
                record["host_countries"] = []
                record["location"] = None
                if "University of Copenhagen" in text:
                    record["host_countries"] = ["DK"]
                    record["location"] = "University of Copenhagen, Denmark"
                elif name == "Mackenzie King Open Scholarship":
                    record["host_countries"] = ["CA"]
                    record["classification"]["evidence"].append(
                        "Canada is an explicit possible study destination; "
                        "other overseas destinations are unspecified."
                    )
                elif name == "Friedrich- Ebert-Stiftung (FES)":
                    record["host_countries"] = ["DE"]
                    record["classification"]["evidence"].append(
                        "Germany is an explicit possible study destination; "
                        "other European destinations are unspecified."
                    )
                records.append(record)
    if found != set(_FUNDING_PROGRAMMES) | set(_EXCLUDED_FUNDING):
        raise AdapterError("Reviewed funding inventory changed", "parse")
    return records


def scholarships(root):
    """Keep actual university study awards, not external funding directories."""
    main = one(nodes(root, "main"), "scholarship main content")
    government = one(
        [
            n
            for n in nodes(main, "section")
            if n.text().startswith("Danish government Scholarships")
        ],
        "Danish Government programme",
    )
    text = government.text()
    required = (
        "automatically",
        "non-EU/EEA",
        "time-limited residence permit",
        "Faculty of Health and Medical Sciences",
        "Faculty of Humanities",
        "Faculty of Social Sciences",
        "Faculty of Science",
        "Faculty of Theology",
        "22 months",
        "90%",
        "60 ECTS",
        "GPA of 10 or higher",
        "maximum three with the same nationality",
        "less than four per year",
        "generally 2-3 per year",
        "September intake",
    )
    if any(term not in text for term in required):
        raise AdapterError("Reviewed scholarship conditions changed", "parse")
    health = one(
        [
            n
            for n in nodes(government, "div", "accordion-item")
            if n.text().startswith("Faculty of Health and Medical Sciences")
        ],
        "Health scholarship conditions",
    )
    science = one(
        [
            n
            for n in nodes(government, "div", "accordion-item")
            if n.text().startswith("Faculty of Science")
        ],
        "Science scholarship conditions",
    )
    if not all(
        "Scholarship for 2" in n.text() and "year students" in n.text()
        for n in (health, science)
    ):
        raise AdapterError("Second-year scholarship cohorts missing", "parse")
    records = [
        programme(
            "UCPH Danish Government Scholarship for MA/MSc admission",
            "danish-government-scholarships",
            "Automatic academic-merit consideration for eligible master's "
            "applicants from outside the EU, EEA and Switzerland with a "
            "time-limited study residence permit. "
            "Awards provide tuition waivers and/or living support. Faculty "
            "rules "
            "vary; SCIENCE requires a first-priority application and "
            "bachelor's "
            "GPA above 90%. No separate scholarship application or dated call "
            "is "
            "published here; financial need does not determine selection.",
            [
                "Eligibility: citizenship outside EU, EEA and Switzerland; "
                "UCPH master's admission and temporary study residence permit.",
                "Exclusions: Danish-citizen rights, SU eligibility and the "
                "specified Aliens Act section 9c/9m residence exception.",
                "HEALTH: academically outstanding applicants; either tuition "
                "alone or tuition and living costs; living support may be "
                "taxed.",
                "HUMANITIES: automatic consideration of eligible MA "
                "applicants.",
                "SOCIAL SCIENCES: September intake only; usually 2–3 "
                "awards/year.",
                "SCIENCE: first-priority MSc admission; bachelor's GPA above "
                "90%; "
                "tuition and living expenses for 22 months. 2025 minimum 98% "
                "is historical.",
                "THEOLOGY: partial tuition waiver only, usually fewer than "
                "4/year; "
                "fees must be paid before possible later reimbursement.",
                "Yearless admission calendars are not scholarship deadlines.",
            ],
        ),
        programme(
            "UCPH HEALTH second-year master's scholarship",
            one(nodes(health, "h2"), "Health heading").attrs["id"],
            "Automatic annual consideration for fee-paying master's students "
            "who completed their first year at UCPH HEALTH. Very limited "
            "one-year awards; the best student may receive tuition and living "
            "support. The faculty expects GPA 10 or higher on the Danish scale "
            "and notifies recipients around mid-September/early October. "
            "No exact current deadline is stated.",
            [
                "Distinct continuing-student cohort, not an admission award.",
                "Publisher expects approximately 2 second-year awards/year; "
                "capacity "
                "does not create separate opportunities.",
                "Source conditions: " + SOURCE_URL + "#" + health.attrs["id"],
            ],
        ),
        programme(
            "UCPH SCIENCE second-year master's scholarship",
            one(nodes(science, "h2"), "Science heading").attrs["id"],
            "Fee-paying UCPH SCIENCE students may receive second-year tuition "
            "and living support after excellent first-year results. At least "
            "60 ECTS must be registered by 1 September; exchange grades "
            "receive "
            "individual assessment. No more than 3 recipients share a "
            "nationality. "
            "Selected students are contacted in early September; fees already "
            "paid for semester 3 are refunded. No dated application call is "
            "stated.",
            [
                "Distinct continuing-student cohort, not an admission award.",
                "2025 recipient GPA 11 or higher is historical, not a current "
                "cutoff.",
                "Source conditions: " + SOURCE_URL + "#" + science.attrs["id"],
            ],
        ),
    ]
    fulbright = one(
        [
            n
            for n in nodes(main, "section")
            if n.text().startswith(
                "Fulbright Scholarship for US full-degree master's students"
            )
        ],
        "UCPH Fulbright study programme",
    )
    if not all(
        term in fulbright.text()
        for term in (
            "US students",
            "1-2 semesters",
            "not given to exchange students",
            "tuition fee",
            "letter of affiliation",
        )
    ):
        raise AdapterError("Reviewed Fulbright study conditions changed")
    records.append(
        programme(
            "Fulbright scholarship for US full-degree or guest students at "
            "UCPH",
            "fulbright-scholarship",
            "UCPH describes Fulbright support for US students taking a full "
            "master's degree or 1–2 semesters as a guest student; exchange "
            "students "
            "are excluded. Apply separately for the award and UCPH study "
            "place. "
            "Full-degree tuition remains payable. The relevant department "
            "supplies an affiliation letter. No current award deadline or "
            "amount "
            "is published on this university page.",
            [
                "Externally administered Fulbright award; only UCPH-published "
                "study-route conditions are collected, not external provider "
                "data.",
                "UCPH International Education cannot sign affiliation letters.",
            ],
            countries=["US"],
        )
    )
    for record in records:
        fragment = urllib.parse.urlsplit(record["url"]).fragment
        if not any(n.attrs.get("id") == fragment for n in nodes(main)):
            raise AdapterError("Programme anchor is missing", "parse")
    records.extend(additional_programmes(main))
    return records


def vacancy_rows(root, url):
    """Read the complete server-rendered official vacancy table."""
    table = one(nodes(root, "table", "vacancies"), "PhD vacancy table")
    rows = []
    for row in nodes(table, "tr"):
        cells = nodes(row, "td")
        if not cells:
            continue
        if len(cells) != 4:
            raise AdapterError("Unknown vacancy table layout", "parse")
        link = one(nodes(cells[0], "a"), "vacancy detail link")
        target = urllib.parse.urljoin(url, link.attrs.get("href", ""))
        parsed = urllib.parse.urlsplit(target)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "employment.ku.dk"
            or parsed.path != "/phd/"
            or parsed.fragment
            or not re.fullmatch(r"show=\d+", parsed.query)
        ):
            raise AdapterError("Unreviewed vacancy detail URL", "parse")
        try:
            date = datetime.strptime(cells[3].text(), "%d-%m-%Y").date()
        except ValueError as error:
            raise AdapterError("Invalid vacancy calendar date") from error
        rows.append(
            (target, link.text(), cells[1].text(), cells[2].text(), date)
        )
    if not rows:
        raise AdapterError("Empty official doctoral inventory", "parse")
    return rows


_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_DATE = (
    r"(?:\d{1,2}(?:st|nd|rd|th)?\s+(?:"
    + "|".join(_MONTHS)
    + r")\s+20\d{2}|(?:"
    + "|".join(_MONTHS)
    + r")\s+\d{1,2}(?:st|nd|rd|th)?[,]?\s+20\d{2}|"
    + r"\d{1,2}\.\d{1,2}\.20\d{2})"
)


def stated_date(value):
    cleaned = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", value)
    cleaned = cleaned.replace(",", "")
    for pattern in ("%d %B %Y", "%B %d %Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(cleaned, pattern).date()
        except ValueError:
            pass
    raise AdapterError("Unsupported explicit application date", "parse")


def application_deadline(body, listing_date):
    """Keep publisher clock/offset; flag competing dates instead of guessing."""
    text = body.text()
    found = []
    for match in re.finditer(
        r"(?:deadline for applications?|application deadline)\s*"
        r"(?:is\s*)?[:]?\s*(?:Sunday\s*)?(" + _DATE + ")",
        text,
        re.I,
    ):
        date = stated_date(match[1])
        remainder = text[match.end() : match.end() + 65]
        clock = re.match(
            r"\s*[,\s]*(?:at\s*)?(\d{1,2})[:.](\d{2})"
            r"\s*(?:pm|PM)?\s*[([]?\s*"
            r"(CET|CEST|GMT\s*\+\s*[12]|UTC)",
            remainder,
        )
        instant = None
        if clock:
            hour, minute = int(clock[1]), int(clock[2])
            zone = re.sub(r"\s", "", clock[3].upper())
            offset = {"CET": 1, "CEST": 2, "GMT+1": 1, "GMT+2": 2, "UTC": 0}[
                zone
            ]
            instant = (
                datetime(
                    date.year,
                    date.month,
                    date.day,
                    hour,
                    minute,
                    tzinfo=timezone(timedelta(hours=offset)),
                )
                .astimezone(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
        found.append((date, instant, clipped(match[0] + remainder, 150)))
    dates = {entry[0] for entry in found}
    evidence = list(dict.fromkeys(entry[2] for entry in found))
    if len(dates | {listing_date}) != 1:
        return (
            None,
            "unknown",
            [
                "Conflicting official listing date: "
                + listing_date.isoformat(),
                *evidence,
            ],
        )
    instants = {entry[1] for entry in found if entry[1] is not None}
    if len(instants) > 1:
        return None, "unknown", ["Conflicting publisher clocks", *evidence]
    deadline = next(iter(instants), listing_date.isoformat())
    if instants:
        expired = datetime.fromisoformat(
            deadline.replace("Z", "+00:00")
        ) < datetime.now(timezone.utc)
    else:
        expired = listing_date < datetime.now(timezone.utc).date()
    return (
        deadline,
        "expired" if expired else "open",
        evidence
        or [
            "Official listing application date: "
            + listing_date.isoformat()
            + "; no supported clock/offset.",
        ],
    )


def vacancy(row, root):
    url, listing_title, faculty, department, date = row
    main = one(nodes(root, "main"), "vacancy main content")
    title = one(nodes(main, "h1"), "vacancy heading").text()
    body = one(nodes(main, "div", "vacancy_details_area"), "vacancy body")
    text = body.text()
    if (
        title != listing_title
        or not re.search(r"\bPhD\b", title, re.I)
        or not re.search(
            r"salary|stipend|scholarship|appointment and payment", text, re.I
        )
    ):
        raise AdapterError("Unverified funded doctoral vacancy", "parse")
    deadline, status, dates = application_deadline(body, date)
    evidence = [
        "Official current PhD listing: " + VACANCIES_URL,
        "Listing faculty: " + faculty,
        *dates,
    ]
    facts = []
    for node in nodes(body):
        if node.tag not in ("p", "li"):
            continue
        paragraph = node.text()
        if (
            re.search(
                r"salary.*(?:starts|approximately)|"
                r"qualifications.*(?:degree|master)|"
                r"(?:must|requires?|required).*degree|"
                r"(?:excellent|fluency|proficiency).*English|"
                r"must not have resided|must not.*doctoral degree",
                paragraph,
                re.I,
            )
            and "tax reductions" not in paragraph
        ):
            facts.append(clipped(paragraph))
    evidence.extend(dict.fromkeys(facts))
    detail_faculties = set(
        re.findall(
            r"Faculty of (?:Science|Health and Medical Sciences|Humanities)",
            text,
            re.I,
        )
    )
    if detail_faculties and faculty.lower() not in {
        value.lower() for value in detail_faculties
    }:
        evidence.append(
            "Faculty discrepancy: detail says "
            + "; ".join(sorted(detail_faculties))
            + "; listing says "
            + faculty
            + "."
        )
    integrated = re.search(r"integrated MSc and PhD programme", text, re.I)
    route = (
        "The notice describes regular and integrated MSc/PhD routes; "
        "their degree and enrolment conditions differ. "
        if integrated
        else "The notice specifies relevant degree and academic requirements. "
    )
    summary = (
        "Funded doctoral recruitment at "
        + department
        + ", UCPH. "
        + route
        + "Application is through the official notice; "
        "consult its subject, language and employment conditions."
    )
    if "MSCA" in text and "must not have resided" in text:
        if not all(term in text for term in ("12 months", "3 years")):
            raise AdapterError("Reviewed MSCA mobility limits changed")
        summary += (
            " MSCA mobility: no more than 12 months of main activity "
            "in Denmark during the 3 years before recruitment."
        )
        if "not already in possession of a doctoral degree" in text:
            evidence.append(
                "MSCA: doctoral degree must not already be held on the first "
                "employment day; mobility is assessed at recruitment."
            )
        if "successfully defended their doctoral thesis" in text:
            evidence.append(
                "MSCA: a successfully defended doctorate also excludes "
                "eligibility before formal award."
            )
        if "Short stays as holidays and compulsory military service" in text:
            evidence.append(
                "MSCA mobility exclusions: short holidays, compulsory "
                "military service and Refugee Convention 1951 circumstances, "
                "as stated in the notice."
            )
    if (
        integrated
        and "without such qualifications will not be considered" in text
    ):
        evidence.append(
            "Publisher describes an integrated route but also says "
            "candidates without regular-route qualifications are not "
            "considered; applicants should clarify that inconsistency."
        )
    record = make_record(
        title,
        url,
        ["fellowships", "jobs"],
        host_countries=["DK"],
        method="official-funded-doctoral-vacancy",
        evidence=evidence,
    )
    record.update(
        summary=clipped(summary, 600),
        deadline=deadline,
        status=status,
        location=department + ", Denmark",
    )
    record["tags"] = ["PhD", "University of Copenhagen"]
    return record


def collect():
    """Collect actual university programmes plus every current PhD advert."""
    _ROBOTS.clear()
    records = scholarships(page(SOURCE_URL))
    pending, seen, rows, urls = [VACANCIES_URL], set(), [], set()
    while pending:
        url = pending.pop(0)
        if url in seen:
            continue
        seen.add(url)
        root = page(url)
        for row in vacancy_rows(root, url):
            if row[0] in urls:
                raise AdapterError("Repeated vacancy across listing pages")
            urls.add(row[0])
            rows.append(row)
        for link in nodes(root, "a"):
            relation = link.attrs.get("rel", "").split()
            if "next" not in relation:
                continue
            target = urllib.parse.urljoin(url, link.attrs.get("href", ""))
            parsed = urllib.parse.urlsplit(target)
            if (
                parsed.scheme != "https"
                or parsed.netloc != "employment.ku.dk"
                or parsed.path != "/phd/"
                or parsed.fragment
            ):
                raise AdapterError("Unreviewed vacancy pagination", "parse")
            if target not in seen:
                pending.append(target)
        if len(seen) > 30:
            raise AdapterError("Excessive vacancy pagination", "parse")
    for index, row in enumerate(rows, 1):
        records.append(vacancy(row, page(row[0])))
        print(
            f"UCPH: inspected doctoral advert {index}/{len(rows)}",
            file=sys.stderr,
        )
    validate_records(records)
    print(
        f"UCPH: {len(records) - len(rows)} award programmes, "
        f"{len(rows)} doctoral adverts, "
        f"{len(seen)} listing pages",
        file=sys.stderr,
    )
    return records


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
    publisher = "ku.dk" if host == "ku.dk" or host.endswith(".ku.dk") else host
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
                        f"Publisher returned HTTP {status}", "fetch", status
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
            or len(record["summary"]) > 600
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
            else f"Collected and validated {len(records)} records"
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
