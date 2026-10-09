"""Collect attributed noncommercial WBI funding opportunities."""

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

SOURCE_ID = "be-wbi"
SOURCE_URL = "https://www.wbi.be/en/bourses"
WEBSITE_URL = "https://www.wbi.be/"
LANGUAGE = "fr"
PUBLISHER_COUNTRY = "BE"
PUBLISHER_TYPE = "government"
ATTRIBUTION = "Wallonie-Bruxelles International (WBI) – www.wbi.be"


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


def content(root):
    return one(
        [
            n
            for n in nodes(root, "div", "field--name-body")
            if not nodes(n, "a", "printButton")
        ],
        "WBI factual article",
    )


def source_facts(key, root):
    if key.startswith("listing") or key.startswith("category"):
        cards = nodes(root, class_name="search-result")
        if not cards:
            raise AdapterError(
                "Missing complete WBI programme inventory", "parse"
            )
        links = sorted(
            {
                n.attrs.get("href", "")
                for card in cards
                for n in nodes(card, "a")
                if n.attrs.get("href")
            }
        )
        pagers = sorted(
            {
                n.attrs.get("href", "")
                for nav in nodes(root, "nav", "pager")
                for n in nodes(nav, "a")
                if n.attrs.get("href")
            }
        )
        return json.dumps([links, pagers], ensure_ascii=False)
    text = (
        one(nodes(root, "h1"), "WBI article title").text()
        + " "
        + content(root).text()
    )
    text = re.sub(
        r"Last edited il y a \d+ (?:minutes?|heures?|jours?)", "", text
    )
    return " ".join(text.split())


PAGES = {
    "louisiana-call": (
        "https://www.wbi.be/fr/actualites/appel-candidatures-devenir-"
        "formateurtrice-enseignante-louisiane"
    ),
    "circ-call": (
        "https://www.wbi.be/fr/actualites/offre-stage-centre-internat"
        "ional-recherche-cancer-circ"
    ),
    "leaf17": (
        "https://www.wbi.be/fr/aide-service/realiser-stage-entreprise"
        "-letranger"
    ),
    "leaf16": (
        "https://www.wbi.be/fr/aide-service/obtenir-bourse-specialisa"
        "tion-letranger-wbi"
    ),
    "leaf15": (
        "https://www.wbi.be/fr/aide-service/obtenir-bourse-effectuer-"
        "sejour-recherche-letranger-wbi"
    ),
    "leaf14": (
        "https://www.wbi.be/fr/aide-service/obtenir-bourse-dexcellenc"
        "e-suivre-formation-haut-niveau-wallonie-ou-bruxelles"
    ),
    "leaf13": (
        "https://www.wbi.be/fr/aide-service/obtenir-bourse-cadre-du-p"
        "rojet-pilote-entre-wbi-centre-international-recherche-cancer"
    ),
    "leaf12": (
        "https://www.wbi.be/fr/aide-service/etre-soutenu-votre-partic"
        "ipation-aux-initiatives-programmes-europeens-financement"
    ),
    "leaf11": (
        "https://www.wbi.be/fr/aide-service/enterprise-europe-network"
        "-votre-porte-dentree-partenariats-europeens"
    ),
    "leaf10": (
        "https://www.wbi.be/fr/aide-service/effectuer-stage-organisat"
        "ion-internationale-intergouvernementale-dinteret-public"
    ),
    "leaf9": (
        "https://www.wbi.be/fr/aide-service/devenir-formatrice-ou-for"
        "mateur-enseignante-louisiane-wbi"
    ),
    "leaf8": (
        "https://www.wbi.be/fr/aide-service/devenir-auxiliaire-conver"
        "sation-langue-francaise-letranger-wbi"
    ),
    "leaf7": (
        "https://www.wbi.be/fr/aide-service/beneficier-dune-formation"
        "-droit-international-lacademie-droit-international-haye"
    ),
    "leaf6": (
        "https://www.wbi.be/fr/aide-service/beneficier-dun-soutien-vo"
        "s-projets-domaines-recherche-linnovation"
    ),
    "leaf5": (
        "https://www.wbi.be/fr/aide-service/beneficier-dun-soutien-pr"
        "ojets-cooperation-scientifique-france-programme-hubert-curie"
        "n"
    ),
    "leaf4": (
        "https://www.wbi.be/fr/aide-service/beneficier-dun-soutien-fr"
        "ais-mobilite-lors-dune-manifestation-allemagne-france-grand"
    ),
    "leaf3": (
        "https://www.wbi.be/fr/aide-service/beneficier-du-fonds-mobil" "ite-wbi"
    ),
    "leaf2": (
        "https://www.wbi.be/fr/aide-service/apprendre-ou-se-perfectio"
        "nner-langue-etrangere-wbi"
    ),
    "leaf1": (
        "https://www.wbi.be/fr/aide-service/apprendre-metiers-lies-le"
        "xportation-au-developpement-business-linternational-explort"
    ),
    "leaf0": "https://www.wbi.be/fr/WBI-World",
    "category847": "https://www.wbi.be/en/taxonomy/term/847",
    "category846": "https://www.wbi.be/en/taxonomy/term/846",
    "category844": "https://www.wbi.be/en/taxonomy/term/844",
    "category843": "https://www.wbi.be/en/taxonomy/term/843",
    "legal": "https://www.wbi.be/fr/mentions-legales-0",
    "listing2": "https://www.wbi.be/en/bourses?page=1",
    "listing": "https://www.wbi.be/en/bourses",
    "entry": (
        "https://www.wbi.be/fr/actualites/offre-bourse-stage-entrepri"
        "se-etudiants"
    ),
}
CONDITIONS = {
    "louisiana-call": "498830cc23309429651a6545e25007cf297c882d4430a0419bccbbf282ef22f9",
    "circ-call": "af6ddc24951880b08181493ea88089ca00aad25742c912a873293cb8fb85e8d7",
    "leaf17": "63fd26abacff59225f4c547a355b699d64264d04730c38e9cf916c8299fdf345",
    "leaf16": "182d40518ec56dca9e81e13562ee5c3ee073a51d8e9bdd3ed6fe840adbb2d3f1",
    "leaf15": "c75acb19c583508598ebaa310f9f625ee7c0cb93f0c5776ec912860c5cb04a8b",
    "leaf14": "53d6b18ddc9e7cbe7b5d992f3d1e93f9578b2a3829c7d19d745ddde36f8265fe",
    "leaf13": "92dd452fa99f6ba64f114625343c5cec0c9afd604910f5634f184ae433c1aab4",
    "leaf12": "01cb89d55426de2d9aaaa8569ca1ecb7194e8a22aa746f3c6ae27ebc06f736d0",
    "leaf11": "e71080f16fadf02a7bfe2b543a44ecc760184c18d6a280766f01067886c6c2db",
    "leaf10": "b5d998a8679978272517360c6939d622759931008b273d9a22c0257c1d0972df",
    "leaf9": "82febbe3c945596b7192d20a8648e7757500e0b40e38777e61dfc00c032ed171",
    "leaf8": "cb0c937be88da27233a64173d38bba017d6e7089ca406d7068addea9f3cde9aa",
    "leaf7": "296cc74c8c8934df0ed680d55dae07d488cf103498436fc6933290109770f2ef",
    "leaf6": "55dcf38a2239387d1ba6ecf64334aea7af5e372024e8d29efa7939f0df821376",
    "leaf5": "b3af3d49f8f8e5befd00cd9b782950f40ab82e1a627816f6195ff9a106e9670e",
    "leaf4": "a5eb1c8b0d6cdd9d43964a576e1790940d38f36066a4f97843ff268bffec3d33",
    "leaf3": "9de10449e97f7d979dd771f7136025ec8df6336b53eb69333f641f411821f0a3",
    "leaf2": "213d4623aa0b7b952b4c0079c3c30bd2e672b4f865bc1d15380f883257537cae",
    "leaf1": "0df63e3d292e0b9dc324bb3608506be0f3d9c1cc05d6d88f5e9399fef6c5a390",
    "leaf0": "df00f9cf5f33a283f2848ecc9872cd666d01236a79cbd2b6f592d75861d2f1db",
    "category847": "a566b5b255d1097afc38fc3b4d6c4cee59328028e1fb9e599e1250ed30c56928",
    "category846": "16dc4e37d861f8e4bbc9703dddec9606be9cf80d0315dfe8e92c64b1dcd45a85",
    "category844": "655504070f0097104dcc9167087bf195897ba900f1fa1539b83b78b7be7a14dd",
    "category843": "16dc4e37d861f8e4bbc9703dddec9606be9cf80d0315dfe8e92c64b1dcd45a85",
    "legal": "562a601d3df8c4d6127959f72fc51504c3435a2768d00073d76789ab1f9a91b7",
    "listing2": "d3dd8a41b1b73c7e60409fe8127847c400b8bf45d96e3316b8735f4a716b3d92",
    "listing": "6e3ed3826a25d77af1829ca45b42bf3cbde8addbbaff82cdd7cc3cef75e68102",
    "entry": "943ddb44c6ff4cd09196361cb8fe89a04cee05b2542009a6ed5c9de8fa3809b9",
}

PROFILES = [
    {
        "track": "world-doctoral",
        "key": "leaf0",
        "title": "WBI Excellence WORLD — doctoral",
        "category": "scholarships",
        "summary": "At least one year, renewable three times. FWB master (120 "
        "credits), or foreign degree with current FWB university "
        "affiliation. €2,120/month mobility allowance, not salary; "
        "return travel and conditional doctoral registration fees up "
        "to €835. Maximum three applications.",
        "evidence": "Postdoctorate normally within five years; parenthood "
        "extensions: 15 months per childbirth, 12 months for "
        "biological fathers/adoption/long-term foster placement. "
        "One doctoral and one postdoctoral long award lifetime "
        "entitlement. FNRS co-tutelle mobility allowance suspended "
        "during return to Belgium. Annual February 1 (UTC+1), or "
        "short October 1/April 1 (UTC+2), without an actual year or "
        "closing clock.",
        "hosts": [],
        "deadline": None,
        "kind": "programme-overview",
        "eligible": [],
    },
    {
        "track": "world-postdoctoral",
        "key": "leaf0",
        "title": "WBI Excellence WORLD — postdoctoral",
        "category": "scholarships",
        "summary": "At least one year, renewable once. FWB doctorate, or "
        "foreign degree with current FWB university affiliation. "
        "€2,120/month mobility allowance, not salary; return travel "
        "and conditional postdoctoral training fees up to €835. "
        "Maximum three applications.",
        "evidence": "Postdoctorate normally within five years; parenthood "
        "extensions: 15 months per childbirth, 12 months for "
        "biological fathers/adoption/long-term foster placement. "
        "One doctoral and one postdoctoral long award lifetime "
        "entitlement. FNRS co-tutelle mobility allowance suspended "
        "during return to Belgium. Annual February 1 (UTC+1), or "
        "short October 1/April 1 (UTC+2), without an actual year or "
        "closing clock.",
        "hosts": [],
        "deadline": None,
        "kind": "programme-overview",
        "eligible": [],
    },
    {
        "track": "world-short",
        "key": "leaf0",
        "title": "WBI Excellence WORLD — short research",
        "category": "scholarships",
        "summary": "One to three months; three years between repeat stays. FWB "
        "master/doctorate, or foreign degree with current FWB "
        "university affiliation. €2,120/month mobility allowance, "
        "not salary; return travel and conditional fee support. "
        "Maximum three applications.",
        "evidence": "Postdoctorate normally within five years; parenthood "
        "extensions: 15 months per childbirth, 12 months for "
        "biological fathers/adoption/long-term foster placement. "
        "One doctoral and one postdoctoral long award lifetime "
        "entitlement. FNRS co-tutelle mobility allowance suspended "
        "during return to Belgium. Annual February 1 (UTC+1), or "
        "short October 1/April 1 (UTC+2), without an actual year or "
        "closing clock. Registration/training fee support up to "
        "€835 is tied to the published doctoral/postdoctoral award "
        "conditions, not a universal short-stay benefit.",
        "hosts": [],
        "deadline": None,
        "kind": "programme-overview",
        "eligible": [],
    },
    {
        "track": "explort",
        "key": "leaf1",
        "title": "EXPLORT international business training",
        "category": "training",
        "summary": "Joint AWEX/Forem training, placement in a Belgian business "
        "and mission abroad for students or jobseekers with suitable "
        "qualifications or relevant experience and language skills. "
        "International business interest required. Funding amount "
        "and dated application deadline are not specified.",
        "evidence": "",
        "hosts": [],
        "deadline": None,
        "kind": "programme-overview",
        "eligible": [],
    },
    {
        "track": "language",
        "key": "leaf2",
        "title": "WBI partner language immersion scholarships",
        "category": "scholarships",
        "summary": "Usually one month of language immersion. Normally at least "
        "two years of FWB higher education; Czech Republic allows "
        "one. WBI funds return travel; partners cover tuition, "
        "accommodation and meals. Other public funding cannot be "
        "combined. Destination-specific conditions apply; no dated "
        "call is established.",
        "evidence": "One programme across twelve explicit destinations; "
        "obsolete linked service alias returns 404 and is not used.",
        "hosts": [
            "BG",
            "EE",
            "GR",
            "HU",
            "LV",
            "LT",
            "MA",
            "PL",
            "CZ",
            "SK",
            "TN",
            "TR",
        ],
        "deadline": None,
        "kind": "programme-overview",
        "eligible": [],
    },
    {
        "track": "mobility-out",
        "key": "leaf3",
        "title": "WBI institutional Mobility Fund — outgoing",
        "category": "grants",
        "summary": "Grant submitted by and paid to an FWB higher education "
        "institution for eligible permanent academic, scientific or "
        "administrative staff travelling to partner institutions. "
        "International travel and essential transfers covered; FNRS "
        "qualified researchers and discretionary exceptions may "
        "qualify.",
        "evidence": "Normally full/part-time indefinite employment; students, "
        "doctoral students, emeritus, non-permanent scientific and "
        "temporary staff excluded. Annual April 10/October 9 have "
        "no published call year. Institutional applicant, not an "
        "individual grant.",
        "hosts": [
            "AR",
            "BR",
            "CL",
            "CO",
            "CU",
            "MX",
            "UY",
            "AU",
            "KH",
            "KR",
            "JP",
            "IN",
            "ID",
            "LA",
            "MY",
            "NZ",
            "PH",
            "CN",
            "SG",
            "TW",
            "TH",
            "VN",
            "DZ",
            "LB",
            "MA",
            "TN",
        ],
        "deadline": None,
        "kind": "institutional-grant",
        "eligible": [],
    },
    {
        "track": "mobility-in",
        "key": "leaf3",
        "title": "WBI institutional Mobility Fund — incoming",
        "category": "grants",
        "summary": "FWB institution applies and receives funding to host "
        "academic, scientific or administrative staff from "
        "accredited Maghreb/Lebanon partner institutions. "
        "Accommodation up to €100/night and subsistence up to "
        "€56/day plus local public transport, for at most 30 days. "
        "International flights excluded.",
        "evidence": (
            "Incoming foreign staff are beneficiaries, not direct grant "
            "applicants. Outgoing indefinite FWB employment conditions "
            "are not imposed as incoming citizenship requirements. "
            "Annual April 10/October 9 have no established call year."
        ),
        "hosts": ["BE"],
        "deadline": None,
        "kind": "institutional-grant",
        "eligible": [],
    },
    {
        "track": "nearby",
        "key": "leaf4",
        "title": "WBI nearby-country cultural mobility support",
        "category": "grants",
        "summary": "Travel support for a manifestation in Germany, France, "
        "Luxembourg or the Netherlands. Individuals/entities "
        "domiciled in Wallonia or Brussels, or a foreign hosting "
        "entity, may apply. French cultural activities require joint "
        "design with CWB Paris. Second-class travel or capped "
        "€0.3751/km reimbursement; funding conditions apply.",
        "evidence": "Relative application windows are not evidence of a dated "
        "open call.",
        "hosts": ["DE", "FR", "LU", "NL"],
        "deadline": None,
        "kind": "programme-overview",
        "eligible": [],
    },
    {
        "track": "tournesol",
        "key": "leaf5",
        "title": "PHC Tournesol scientific cooperation grant",
        "category": "grants",
        "summary": "An FWB university-led research team and French partner "
        "jointly submit a cooperation project; higher education "
        "co-promoters may participate. Mobility grant up to €2,000 "
        "per year for two years; short missions typically one to two "
        "weeks per year.",
        "evidence": "Lodging up to €200 per night and food/local travel up to "
        "€50 per calendar day under published reimbursement "
        "conditions. Both sides must submit their application. "
        "Funding supports institutional cooperation, not a "
        "citizenship-based personal scholarship.",
        "hosts": ["FR"],
        "deadline": None,
        "kind": "institutional-grant",
        "eligible": [],
    },
    {
        "track": "hague",
        "key": "leaf7",
        "title": "WBI Hague Academy international law scholarships 2026",
        "category": "training",
        "summary": "Three annual scholarships for one three-week public or "
        "private international law course. At least four university "
        "study years or a suitable three-year law degree; English or "
        "French proficiency and some study at an FWB-funded "
        "university required. Tuition, capped travel and housing "
        "contribution covered.",
        "evidence": "Courses July 6–24 and July 27–August 14, 2026. Apply by "
        "January 31, 2026. European travel capped at €250; three "
        "available places are one programme, not three records.",
        "hosts": ["NL"],
        "deadline": "2026-01-31",
        "kind": "opportunity",
        "eligible": [],
    },
    {
        "track": "assistants",
        "key": "leaf8",
        "title": "WBI French conversation assistant placements",
        "category": "jobs",
        "summary": "Paid French conversation assistant placements: host salary "
        "or stipend plus WBI travel. Normally under 35 and at least "
        "one year FWB residence; degree and language requirements "
        "vary by destination. No Belgian citizenship restriction is "
        "inferred.",
        "evidence": "Italy under 30; Switzerland 21–30 or under 35 with second "
        "qualification. Spain permits a foreign-degree route; no "
        "2026–27 Catalonia/Andalusia posts. Annual October–February "
        "15 is yearless. Native French or certified C2; FWB "
        "bachelor/master or completed three study years with degree "
        "proof before departure. Host language: UK/Ireland B1+, "
        "German-speaking Switzerland B1, Spain/Germany A2+, Italy "
        "A1+, Austria A2, Taiwan Chinese A1+ and English B1+. "
        "Germany requires measles vaccination/immunity and relevant "
        "experience; Switzerland future teaching interest and "
        "pedagogical experience. WBI travel cap €1,200 Taiwan/€250 "
        "others; Spain travel support only first20 selected, "
        "prioritising first-time assistants. Stays six to eleven "
        "months; one supported renewal in same country. Criminal "
        "record required before arrival.",
        "hosts": ["AT", "IE", "CH", "DE", "ES", "IT", "GB", "TW"],
        "deadline": None,
        "kind": "programme-overview",
        "eligible": [],
    },
    {
        "track": "louisiana",
        "key": "leaf9",
        "title": "WBI teaching placements in Louisiana — current call",
        "category": "jobs",
        "summary": "Ten-month Louisiana teaching placements, renewable twice "
        "with exceptional longer renewals. Teaching qualification, "
        "27 months full-time teaching, current teaching post, French "
        "C2+ and English B1+ required. Application deadline December "
        "31, 2026; interviews in late January 2027.",
        "evidence": "Residence conflict: programme requires at least one year "
        "FWB residence, current article says Belgium; preserve both "
        "and seek publisher clarification. Article quotes 2026–27 "
        "salary stages USD52,443/53,632/53,665/54,583/55,602 and "
        "conditional USD6,000/4,000/4,000 bonuses; not a verified "
        "2027–28 schedule. Raw before midnight has no time zone. "
        "December 1 event registration and December 8 information "
        "event are not award deadlines.",
        "hosts": ["US"],
        "deadline": "2026-12-31",
        "kind": "opportunity",
        "eligible": [],
    },
    {
        "track": "oi",
        "key": "leaf10",
        "title": "WBI intergovernmental organisation internships",
        "category": "internships",
        "summary": "One-to-six-month funded internships for master students or "
        "final-year bachelor students or recent FWB graduates within "
        "two years, at most 30 at placement start. €1,200–1,600 "
        "monthly support and travel up to €500. Previous "
        "organisation experience over six weeks excluded.",
        "evidence": "Other support cannot normally be combined; social "
        "allowance exceptions apply. Annual October 15/April 15 "
        "lack a call year. Nonexhaustive organisation directory is "
        "not a host-country whitelist. NGOs are excluded; the host "
        "must be an actual intergovernmental organisation of public "
        "interest.",
        "hosts": [],
        "deadline": None,
        "kind": "programme-overview",
        "eligible": [],
    },
    {
        "track": "circ-postdoc",
        "key": "leaf13",
        "title": "WBI CIRC pilot postdoctoral scholarship",
        "category": "scholarships",
        "summary": (
            "Research in Lyon for at least one year, renewable once. FWB "
            "doctorate or foreign degree with current FWB university "
            "affiliation; normally within five years of doctorate, "
            "extended one year per childbirth/adoption. €3,000/month "
            "mobility scholarship, not salary; return travel and "
            "conditional insurance/training."
        ),
        "evidence": "Maximum three applications and one award with renewal. "
        "February 1 UTC+1 is yearless; not the distinct 2027 SEE "
        "internship.",
        "hosts": ["FR"],
        "deadline": None,
        "kind": "programme-overview",
        "eligible": [],
    },
    {
        "track": "excellence-in-long",
        "key": "leaf14",
        "title": "WBI Excellence IN — long postdoctoral",
        "category": "scholarships",
        "summary": "At least one year, renewable once. Foreign doctorate "
        "required, or pending doctorate with timely proof, for "
        "research at an FWB higher education institution. At most 12 "
        "months in Belgium during the previous three years. "
        "€2,120/month mobility allowance, not salary, plus return "
        "travel and insurance.",
        "evidence": "Maximum three applications; incompatible with FNRS "
        "funding. Long award only once with renewal; short repeat "
        "entitlements differ. Annual February 1 or October 1/April "
        "1 without actual year. July 15/December 1/July 1 pending "
        "doctorate proof dates are not application deadlines.",
        "hosts": ["BE"],
        "deadline": None,
        "kind": "programme-overview",
        "eligible": [],
    },
    {
        "track": "excellence-in-short",
        "key": "leaf14",
        "title": "WBI Excellence IN — short postdoctoral",
        "category": "scholarships",
        "summary": (
            "One to three months, with three years between repeat stays. "
            "Foreign doctorate required, or pending doctorate with "
            "timely proof, for research at an FWB higher education "
            "institution. At most 12 months in Belgium during the "
            "previous three years. €2,120/month mobility allowance, not "
            "salary, plus return travel and insurance."
        ),
        "evidence": "Maximum three applications; incompatible with FNRS "
        "funding. Long award only once with renewal; short repeat "
        "entitlements differ. Annual February 1 or October 1/April "
        "1 without actual year. July 15/December 1/July 1 pending "
        "doctorate proof dates are not application deadlines.",
        "hosts": ["BE"],
        "deadline": None,
        "kind": "programme-overview",
        "eligible": [],
    },
    {
        "track": "research",
        "key": "leaf15",
        "title": "WBI bilateral research stay scholarships",
        "category": "scholarships",
        "summary": "One-to-six-month research, doctoral/postdoctoral "
        "specialisation or institutional partnership stays. FWB "
        "higher education graduates or final-year students "
        "completing their qualification before departure may apply. "
        "Partner-specific funding and conditions apply; no actual "
        "dated call established.",
        "evidence": "",
        "hosts": [
            "BG",
            "EG",
            "EE",
            "HU",
            "IL",
            "LV",
            "LT",
            "MA",
            "MD",
            "PL",
            "RU",
            "SK",
            "SI",
            "CZ",
        ],
        "deadline": None,
        "kind": "programme-overview",
        "eligible": [],
    },
    {
        "track": "specialisation",
        "key": "leaf16",
        "title": "WBI bilateral academic specialisation scholarships",
        "category": "scholarships",
        "summary": "Academic-year complementary qualification abroad for FWB "
        "higher education graduates or final-year students "
        "completing their qualification before departure. "
        "Destination-specific funding and requirements apply; no "
        "dated call established. The separately named Québec fee "
        "exemption has distinct requirements.",
        "evidence": "",
        "hosts": [
            "BG",
            "EG",
            "EE",
            "HU",
            "IL",
            "JP",
            "LV",
            "LT",
            "PL",
            "CA",
            "CZ",
            "RU",
            "SK",
            "SI",
            "TN",
        ],
        "deadline": None,
        "kind": "programme-overview",
        "eligible": [],
    },
    {
        "track": "quebec-exemption",
        "key": "leaf16",
        "title": "Québec tuition fee exemption",
        "category": "scholarships",
        "summary": "Separately named fee exemption for Belgian citizens with "
        "the required FWB school or degree certification studying in "
        "Québec. No WBI scholarship application is needed; the "
        "Québec-specific procedure applies. A fee exemption is not a "
        "cash stipend.",
        "evidence": "Belgian citizenship is explicit only for this named "
        "subprogramme and is not propagated to other WBI "
        "affiliation/residence schemes.",
        "hosts": ["CA"],
        "deadline": None,
        "kind": "programme-overview",
        "eligible": ["BE"],
    },
    {
        "track": "company",
        "key": "leaf17",
        "title": "WBI company internship scholarship — January–June 2027",
        "category": "internships",
        "summary": "One-to-three-month curricular company internship abroad in "
        "January–June 2027 for final-cycle FWB university or higher "
        "education students. €1,200/1,400/1,600 per month and 50% "
        "travel support capped at €500. No host salary or other "
        "public funding except social allowance. Apply by November "
        "1, 2026.",
        "evidence": "FWB affiliation, not Belgian citizenship. "
        "Doctoral/research placements excluded subject to actual "
        "article exceptions. Source says EU27 priority although "
        "Belgium contradicts abroad requirement: known destination "
        "projection excludes BE (EU26); unspecified border "
        "countries may qualify subject to budget. Raw 23h59 has no "
        "time zone.",
        "hosts": [
            "AT",
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
        "deadline": "2026-11-01",
        "kind": "opportunity",
        "eligible": [],
    },
    {
        "track": "circ-see",
        "key": "circ-call",
        "title": "WBI CIRC SEE internship — March–July 2027",
        "category": "internships",
        "summary": "Five-month funded internship at CIRC in Lyon, March–July "
        "2027, for a relevant FWB higher education master student "
        "enrolled throughout the placement, with English "
        "proficiency. €1,400/month and travel support up to €500. "
        "Apply by November 13, 2026.",
        "evidence": "Distinct named SEE internship call, not the CIRC pilot "
        "postdoctoral award. Raw midnight deadline has no time "
        "zone.",
        "hosts": ["FR"],
        "deadline": "2026-11-13",
        "kind": "opportunity",
        "eligible": [],
    },
]


def read_pages():
    """Read every required hub, leaf, current call and legal article."""
    return {key: page(url) for key, url in PAGES.items()}


def parse_inventory(pages):
    """Normalize reviewed genuine identities; reject changed source facts."""
    if set(pages) != set(PAGES):
        raise AdapterError("Incomplete WBI source evidence", "parse")
    for key, root in pages.items():
        digest = hashlib.sha256(source_facts(key, root).encode()).hexdigest()
        if digest != CONDITIONS[key]:
            raise AdapterError(
                "WBI facts or inventory changed: " + key, "parse"
            )
    records = []
    today = datetime.now(timezone.utc).date().isoformat()
    for profile in PROFILES:
        record = make_record(
            profile["title"],
            PAGES[profile["key"]],
            [profile["category"]],
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
        if profile["deadline"]:
            record["status"] = (
                "open"
                if today < profile["deadline"]
                else "expired" if today > profile["deadline"] else "unknown"
            )
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
        tempfile.gettempdir(), "wbi-publisher-pacing-" + str(os.getuid())
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
    publisher = "wbi.be" if host == "wbi.be" else host
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
                f"Collected {len(records)} validated WBI "
                f"funded opportunities"
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
