# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Provider-register, enad sökning och berikning för discovery-hubben.

Det här är inkopplingssömmen. Varje källa är en självständig klientmodul.
Två sorters källor registreras här:

  PROVIDERS     - sökbara källor som ingår i discovery_sok och kan hämtas
                  via discovery_hamta(kalla, id). "sok" tar
                  (q, limit, fran_ar, till_ar, oppen_tillgang, land,
                  kalla_filter) och returnerar {"traffar": [...], "totalt": ...};
                  "hamta" tar ett enda id och returnerar en enhetlig träff.
  BERIKNING     - källor som bara svarar på en specifik fråga om en redan
                  känd post (Unpaywalls open access-status, Semantic
                  Scholars citeringsgraf) och inte deltar i discovery_sok.
                  Används av discovery_oa_lank/discovery_citeringar.

Lägga till en ny sökbar källa:
  1. Skriv <namn>_client.py med sok(...) och gärna hamta(id), som returnerar
     dictar med minst kalla/doi/titel (se openalex_client för mönstret; äldre
     klienter som crossref_client normaliseras här i providers.py i stället,
     se _till_enhetligt).
  2. Lägg till en rad i PROVIDERS nedan, med "aktiv" kopplat till
     kallkonfig.aktiv(namn) och "krav_saknas" om källan kräver nyckel/e-post.
  3. Dokumentera i README och config.example.env.

Libris, Crossref, DataCite och arXiv har dessutom egna MCP-verktyg med
rikare frågespråk (libris_sok, cr_sok, dc_sok, arxiv_sok). De deltar i den
enade sökningen via PROVIDERS precis som övriga källor, och samma
DISCOVERY_<KALLA>_AKTIV styr både deras rad här och om deras egna verktyg
registreras (se mcp_server.py).
"""

from __future__ import annotations

import concurrent.futures
import re
import time

import arxiv_client
import begreppsexpansion
import bibliotek
import fulltext
import citering
import crossref_client
import datacite_client
import core_client
import diva_client
import doaj_client
import econbiz_client
import europepmc_client
import hal_client
import kallkonfig
import libris_client
import nva_client
import openalex_client
import orkestrering
import osf_client
import publicera_client
import semanticscholar_client
import svarscache
import swepub_client
import unpaywall_client
import zbmath_client
from kallhjalp import DiscoveryKallaFel, kapa_text, sprakkod

# En källas anrop i discovery_sok får högst så här lång tid, oavsett dess
# egen strypningstakt — annars kan en enda långsam eller nedgången källa
# göra den enade sökningen orimligt trög. Källor som svarar långsammare än
# så redovisas som ett fel för just den källan, övriga källor är opåverkade.
_PER_KALLA_TIDSGRANS_S = 15.0


# ---------------------------------------------------------------------------
# Enhetligt träffschema
# ---------------------------------------------------------------------------

def _som_ar(varde) -> int | None:
    """Tvingar ett källfält till int|None. Källor levererar utgivningsår
    omväxlande som int och sträng (t.ex. zbMATH Open) — sorteringen i
    sok_alla kraschar hela anropet på en oväntad typ om det inte städas här,
    en gång, i stället för i varje enskild klientmodul."""
    if varde is None:
        return None
    if isinstance(varde, bool):
        return None
    if isinstance(varde, int):
        return varde
    text = str(varde).strip()
    return int(text) if text.isdigit() else None


def normalisera_doi(doi) -> str | None:
    """Naken DOI med gemener: källorna levererar omväxlande
    https://doi.org/-adresser, doi:-prefix och versaler, och DOI:er är
    skiftlägesokänsliga. Utan normalisering missar dedupliceringen dubbletter."""
    if not doi:
        return None
    text = str(doi).strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        if text.lower().startswith(prefix):
            text = text[len(prefix):]
            break
    return text.strip().lower() or None


def _till_enhetligt(traff: dict) -> dict:
    """Formar en källas egen trafform till discoveryhubbens gemensamma schema.

    Källor som redan lämnar exakt det här schemat (OpenAlex) passerar
    oförändrade. Äldre källor (Crossref, DataCite, arXiv) saknar ett eller
    flera av de nyare fälten (kalla_id, oa_lank) — de fylls i här med bästa
    tillgängliga ersättning i stället för att ändra de klienterna.
    """
    return {
        "kalla": traff.get("kalla"),
        "kalla_id": traff.get("kalla_id") or traff.get("doi") or traff.get("id"),
        "doi": normalisera_doi(traff.get("doi")),
        "titel": traff.get("titel"),
        "forfattare": traff.get("forfattare") or [],
        "ar": _som_ar(traff.get("ar")),
        "typ": traff.get("typ"),
        "url": traff.get("url"),
        "oa_lank": traff.get("oa_lank") or traff.get("url_pdf"),
        "citeringar": traff.get("citeringar"),
        "sammanfattning": traff.get("sammanfattning"),
        "sprak": traff.get("sprak"),
    }


# ---------------------------------------------------------------------------
# Adaptrar för källor med ett rikare eller äldre eget schema
# ---------------------------------------------------------------------------

def _arxiv_doi(arxiv_id: str | None) -> str | None:
    if not arxiv_id:
        return None
    utan_version = re.sub(r"v\d+$", "", arxiv_id)
    return f"10.48550/arxiv.{utan_version}"


def _arxiv_normaliserad(traff: dict) -> dict:
    """Formar en arxiv_client-träff (eget, rikare fältschema) till skalet
    _till_enhetligt() sedan fyller i. arXiv saknar utgivningsår i egentlig
    mening — "ar" sätts från inskickningsdatumet (publicerad)."""
    publicerad = traff.get("publicerad") or ""
    ar = int(publicerad[:4]) if publicerad[:4].isdigit() else None
    return {
        "kalla": arxiv_client.KALLA,
        "kalla_id": traff.get("id"),
        # Utan förlags-DOI används arXivs egen DataCite-DOI (10.48550/arXiv.<id>),
        # som varje preprint har — annars kan träffen inte slås ihop med
        # samma preprint från DataCite eller OpenAlex.
        "doi": traff.get("doi") or _arxiv_doi(traff.get("id")),
        "titel": traff.get("titel"),
        "forfattare": traff.get("forfattare", []),
        "ar": ar,
        "typ": "preprint",
        "url": traff.get("url_abs"),
        "oa_lank": traff.get("url_pdf"),
        "citeringar": None,
        "sammanfattning": traff.get("sammanfattning"),
    }


_ARXIV_SYNTAX = re.compile(r'\b(AND|OR|ANDNOT)\b|\w+:|["()]')


_ARXIV_STOPPORD = frozenset(
    "a an and are as at be but by for from has have in into is it its of on "
    "or that the their this to was were which with without".split()
)


def _arxiv_och_fraga(q: str) -> str:
    """Gör en fri flerordsfråga till all:a AND all:b.

    arXiv kombinerar annars orden med OR, vilket i den enade sökningen gav
    hundratusentals träffar där den sökta artikeln inte fanns bland de
    första. En fråga som redan använder arXivs syntax (fältprefix,
    operatorer, citattecken, parenteser) skickas oförändrad."""
    if not q or _ARXIV_SYNTAX.search(q):
        return q
    # arXivs index saknar stoppord, så all:with matchar ingenting och fäller
    # hela AND-kedjan (verifierat: noll träffar). De rensas bort först.
    ord_ = [o for o in q.split() if o.lower() not in _ARXIV_STOPPORD]
    if len(ord_) < 2:
        return q
    return " AND ".join(f"all:{o}" for o in ord_)


def _arxiv_sok(q, limit, fran_ar, till_ar, oppen_tillgang, land, kalla_filter):
    """arXiv stödjer varken oppen_tillgang- eller land-filtrering — allt är
    fritt tillgängligt och källan saknar institutionsdata; parametrarna tas
    emot för att matcha den gemensamma sok-signaturen men ignoreras."""
    svar = arxiv_client.sok(_arxiv_och_fraga(q), limit=limit, fran_ar=fran_ar, till_ar=till_ar)
    return {
        "totalt": svar.get("totalt"),
        "antal": svar.get("antal"),
        "traffar": [_arxiv_normaliserad(t) for t in svar.get("traffar", [])],
    }


def _arxiv_hamta(id_: str) -> dict:
    return _arxiv_normaliserad(arxiv_client.hamta(id_, format="full"))


def _doi_kalla_sok(client, *, kalla_namn):
    """Bygger en sok-funktion för Crossref/DataCite: samma gamla anropsform
    (q, limit, fran_ar, till_ar), oppen_tillgang/land/kalla_filter ignoreras
    — ingen av dem stödjer den filtreringen i den kompakta sökformen."""

    def _sok(q, limit, fran_ar, till_ar, oppen_tillgang, land, kalla_filter):
        return client.sok(q, limit=limit, fran_ar=fran_ar, till_ar=till_ar)

    return _sok


def _doi_kalla_hamta(client):
    def _hamta(id_: str) -> dict:
        # "full" för att få hela abstractet; _till_enhetligt plockar sedan
        # bara det gemensamma schemats fält.
        return _till_enhetligt(client.hamta(id_, format="full"))

    return _hamta


def _libris_normaliserad(traff: dict) -> dict:
    """Formar en libris_client-träff (katalogpost med eget fältschema) till
    det gemensamma schemat. DOI hämtas ur postens identifierare när den har
    en — det gäller främst e-böcker och rapporter."""
    doi = next(
        (i.get("varde") for i in traff.get("identifierare") or []
         if (i.get("typ") or "").upper() == "DOI"),
        None,
    )
    return {
        "kalla": "libris",
        "kalla_id": traff.get("libris_id"),
        "doi": doi,
        "titel": traff.get("titel"),
        "forfattare": [{"namn": u["namn"]} for u in traff.get("upphov") or [] if u.get("namn")],
        "ar": traff.get("ar"),
        "typ": traff.get("verk_typ") or traff.get("instans_typ"),
        "url": traff.get("uri"),
        "oa_lank": None,
        "citeringar": None,
        "sammanfattning": traff.get("sammanfattning"),
        "sprak": sprakkod(traff.get("sprak")),
    }


def _libris_sok(q, limit, fran_ar, till_ar, oppen_tillgang, land, kalla_filter):
    """Libris frågespråk (mellanslag = OCH, | = ELLER m.m.) tar emot q som
    det är. kalla_filter är råa Libris-filter, samma som libris_sok(filter=)."""
    filt = dict(kalla_filter or {})
    if fran_ar:
        filt.setdefault("min-publication.year", fran_ar)
    if till_ar:
        filt.setdefault("max-publication.year", till_ar)
    svar = libris_client.sok(q, limit=limit, filter=filt or None)
    traffar = [_libris_normaliserad(t) for t in svar.get("traffar", [])]
    return {"totalt": svar.get("totalt"), "antal": len(traffar), "traffar": traffar}


def _libris_hamta(id_: str) -> dict:
    return _libris_normaliserad(libris_client.sammanfatta_post(id_))


def _swepub_sok(q, limit, fran_ar, till_ar, oppen_tillgang, land, kalla_filter):
    """SwePub/Xsearch stödjer bara fritext — övriga parametrar ignoreras."""
    return swepub_client.sok(q, limit=limit, fran_ar=fran_ar, till_ar=till_ar)


def _diva_sok(q, limit, fran_ar, till_ar, oppen_tillgang, land, kalla_filter):
    """DiVA stödjer inte land-filtrering i den här klienten."""
    return diva_client.sok(q, limit=limit, fran_ar=fran_ar, till_ar=till_ar, oppen_tillgang=oppen_tillgang)


def _diva_hamta(id_: str) -> dict:
    return diva_client.hamta(id_)


def _publicera_sok(q, limit, fran_ar, till_ar, oppen_tillgang, land, kalla_filter):
    """Publicera stödjer inte oppen_tillgang/land/kalla_filter i den här
    klienten — sökningen är redan en begränsad OpenAlex-fråga."""
    return publicera_client.sok(q, limit=limit, fran_ar=fran_ar, till_ar=till_ar)


def _publicera_hamta(id_: str) -> dict:
    """discovery_hamta("publicera", id) förväntar sig en DOI — se
    publicera_client.hamta()."""
    return publicera_client.hamta(id_)


def _nva_sok(q, limit, fran_ar, till_ar, oppen_tillgang, land, kalla_filter):
    return nva_client.sok(q, limit=limit, fran_ar=fran_ar, till_ar=till_ar)


def _osf_sok(q, limit, fran_ar, till_ar, oppen_tillgang, land, kalla_filter):
    """kalla_filter kan innehålla {"leverantor": "lawarxiv"} för att begränsa
    till en preprintserver — se osf_client.sok()."""
    leverantor = (kalla_filter or {}).get("leverantor")
    return osf_client.sok(q, limit=limit, fran_ar=fran_ar, till_ar=till_ar, leverantor=leverantor)


def _europepmc_sok(q, limit, fran_ar, till_ar, oppen_tillgang, land, kalla_filter):
    return europepmc_client.sok(q, limit=limit, fran_ar=fran_ar, till_ar=till_ar)


def _zbmath_sok(q, limit, fran_ar, till_ar, oppen_tillgang, land, kalla_filter):
    return zbmath_client.sok(q, limit=limit, fran_ar=fran_ar, till_ar=till_ar)


def _econbiz_sok(q, limit, fran_ar, till_ar, oppen_tillgang, land, kalla_filter):
    return econbiz_client.sok(q, limit=limit, fran_ar=fran_ar, till_ar=till_ar)


def _hal_sok(q, limit, fran_ar, till_ar, oppen_tillgang, land, kalla_filter):
    return hal_client.sok(q, limit=limit, fran_ar=fran_ar, till_ar=till_ar)


def _doaj_sok(q, limit, fran_ar, till_ar, oppen_tillgang, land, kalla_filter):
    return doaj_client.sok(q, limit=limit, fran_ar=fran_ar, till_ar=till_ar)


def _core_sok(q, limit, fran_ar, till_ar, oppen_tillgang, land, kalla_filter):
    return core_client.sok(q, limit=limit, fran_ar=fran_ar, till_ar=till_ar)


def _openalex_sprak_sok(sprak: str):
    """sok-funktion mot OpenAlex begränsad till ett publiceringsspråk."""
    def _sok(q, limit, fran_ar, till_ar, oppen_tillgang, land, kalla_filter):
        return _openalex_sok(q, limit, fran_ar, till_ar, oppen_tillgang, land,
                             {**(kalla_filter or {}), "language": sprak})
    return _sok


def _openalex_sok(q, limit, fran_ar, till_ar, oppen_tillgang, land, kalla_filter):
    return openalex_client.sok(
        q,
        limit=limit,
        fran_ar=fran_ar,
        till_ar=till_ar,
        oppen_tillgang=oppen_tillgang,
        land=land,
        filter=kalla_filter,
    )


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------

# Sökbara källor: deltar i discovery_sok och kan hämtas via discovery_hamta.
# "aktiv" avgörs vid modulinläsning (miljön ändras inte under körning).
# "krav_saknas" är en förklarande text när "aktiv" är False av det skälet.
PROVIDERS: dict[str, dict] = {
    "crossref": {
        "etikett": "Crossref",
        "beskrivning": "DOI:er för vetenskapliga artiklar, böcker och konferensbidrag.",
        "sok": _doi_kalla_sok(crossref_client, kalla_namn="crossref"),
        "hamta": _doi_kalla_hamta(crossref_client),
        "fel": crossref_client.CrossrefFel,
        "aktiv": kallkonfig.aktiv("crossref"),
        "krav_saknas": None,
        "filterstod": [],
    },
    "datacite": {
        "etikett": "DataCite",
        "beskrivning": "DOI:er för forskningsdata, programvara och preprints.",
        "sok": _doi_kalla_sok(datacite_client, kalla_namn="datacite"),
        "hamta": _doi_kalla_hamta(datacite_client),
        "fel": datacite_client.DataCiteFel,
        "aktiv": kallkonfig.aktiv("datacite"),
        "krav_saknas": None,
        "filterstod": [],
    },
    "arxiv": {
        "etikett": "arXiv",
        "beskrivning": "Preprints inom fysik, matematik, datavetenskap m.fl.",
        "sok": _arxiv_sok,
        "hamta": _arxiv_hamta,
        "fel": arxiv_client.ArxivFel,
        "aktiv": kallkonfig.aktiv("arxiv"),
        "krav_saknas": None,
        "filterstod": [],
    },
    "libris": {
        "etikett": "Libris",
        "beskrivning": (
            "Sveriges nationella bibliotekskatalog (KB): böcker, avhandlingar, "
            "rapporter och tidskrifter. libris_sok har rikare filter."
        ),
        "sok": _libris_sok,
        "hamta": _libris_hamta,
        "fel": libris_client.LibrisFel,
        "aktiv": kallkonfig.aktiv("libris"),
        "krav_saknas": None,
        "filterstod": ["råa Libris-filter, t.ex. instanceOf.language.@id"],
    },
    "openalex": {
        "etikett": "OpenAlex",
        "beskrivning": (
            "Brett index över vetenskapliga verk, samtliga ämnesfält. "
            "Kostnadsbaserat sedan februari 2026 — se kostnad_usd i svaret."
        ),
        "sok": _openalex_sok,
        "hamta": lambda id_: openalex_client.hamta(id_),
        "fel": openalex_client.OpenAlexFel,
        "aktiv": kallkonfig.aktiv("openalex"),
        "krav_saknas": None,
        "filterstod": [
            "primary_topic.field.id", "topics.id",
            "authorships.institutions.country_code",
            "open_access.is_oa", "open_access.oa_status", "publication_year",
        ],
    },
    "swepub": {
        "etikett": "SwePub",
        "beskrivning": "Publikationer från svenska lärosäten och myndigheter (Libris Xsearch).",
        "sok": _swepub_sok,
        "hamta": None,  # Xsearch saknar dokumenterad hämtning av en enskild känd post.
        "fel": swepub_client.SwePubFel,
        "aktiv": kallkonfig.aktiv("swepub"),
        "krav_saknas": None,
        "filterstod": [],
    },
    "diva": {
        "etikett": "DiVA",
        "beskrivning": "~50 svenska lärosäten/myndigheter: avhandlingar, artiklar, rapporter, examensarbeten.",
        "sok": _diva_sok,
        "hamta": _diva_hamta,
        "fel": diva_client.DivaFel,
        "aktiv": kallkonfig.aktiv("diva"),
        "krav_saknas": None,
        "filterstod": ["oppen_tillgang"],
    },
    "publicera": {
        "etikett": "Publicera (KB)",
        "beskrivning": (
            "Svenska vetenskapliga tidskrifter på KB:s OJS-plattform. Sök via "
            "OpenAlex begränsat till 46 av 55 identifierade tidskrifts-ISSN; "
            "hamta() går äkta mot källan via OAI-PMH."
        ),
        "sok": _publicera_sok,
        "hamta": _publicera_hamta,
        "fel": publicera_client.PubliceraFel,
        "aktiv": kallkonfig.aktiv("publicera"),
        "krav_saknas": None,
        "filterstod": [],
    },
    "nva": {
        "etikett": "NVA",
        "beskrivning": "Norges nationella forskningsarkiv (publikationer från norska lärosäten).",
        "sok": _nva_sok,
        "hamta": lambda id_: nva_client.hamta(id_),
        "fel": nva_client.NvaFel,
        "aktiv": kallkonfig.aktiv("nva"),
        "krav_saknas": None,
        "filterstod": [],
    },
    "osf": {
        "etikett": "OSF Preprints",
        "beskrivning": "Ämnesinriktade preprintservrar (SocArXiv, LawArXiv, EdArXiv, PsyArXiv m.fl.), fritext via SHARE.",
        "sok": _osf_sok,
        "hamta": lambda id_: osf_client.hamta(id_),
        "fel": osf_client.OsfFel,
        "aktiv": kallkonfig.aktiv("osf"),
        "krav_saknas": None,
        "filterstod": ["leverantor"],
    },
    "europepmc": {
        "etikett": "Europe PMC",
        "beskrivning": "Biomedicin och life science (PubMed/MEDLINE, PMC, preprints, patent m.m.).",
        "sok": _europepmc_sok,
        "hamta": lambda id_: europepmc_client.hamta(id_),
        "fel": europepmc_client.EuropePmcFel,
        "aktiv": kallkonfig.aktiv("europepmc"),
        "krav_saknas": None,
        "filterstod": [],
    },
    "zbmath": {
        "etikett": "zbMATH Open",
        "beskrivning": "Matematisk bibliografidatabas med MSC-klassificering.",
        "sok": _zbmath_sok,
        "hamta": lambda id_: zbmath_client.hamta(id_),
        "fel": zbmath_client.ZbmathFel,
        "aktiv": kallkonfig.aktiv("zbmath"),
        "krav_saknas": None,
        "filterstod": [],
    },
    "econbiz": {
        "etikett": "EconBiz",
        "beskrivning": "ZBW:s ekonomiska litteraturdatabas (working papers, grå litteratur).",
        "sok": _econbiz_sok,
        "hamta": lambda id_: econbiz_client.hamta(id_),
        "fel": econbiz_client.EconBizFel,
        "aktiv": kallkonfig.aktiv("econbiz"),
        "krav_saknas": None,
        "filterstod": [],
    },
    "hal": {
        "etikett": "HAL",
        "beskrivning": "Frankrikes öppna arkiv, starkt inom humaniora/samhällsvetenskap (HAL-SHS: bl.a. arkeologi).",
        "sok": _hal_sok,
        "hamta": lambda id_: hal_client.hamta(id_),
        "fel": hal_client.HalFel,
        "aktiv": kallkonfig.aktiv("hal"),
        "krav_saknas": None,
        "filterstod": [],
    },
    "doaj": {
        "etikett": "DOAJ",
        "beskrivning": "Granskade open access-tidskrifter, alla ämnesfält.",
        "sok": _doaj_sok,
        "hamta": lambda id_: doaj_client.hamta(id_),
        "fel": doaj_client.DoajFel,
        "aktiv": kallkonfig.aktiv("doaj"),
        "krav_saknas": None,
        "filterstod": [],
    },
    "core": {
        "etikett": "CORE",
        "beskrivning": "Aggregerad fulltext/metadata från open access-arkiv världen över. Avstängd som standard.",
        "sok": _core_sok,
        "hamta": lambda id_: core_client.hamta(id_),
        "fel": core_client.CoreFel,
        "aktiv": kallkonfig.aktiv("core", standard=False),
        "krav_saknas": None,
        "filterstod": [],
    },
}


def _unpaywall_krav() -> str | None:
    return kallkonfig.krav_saknas("unpaywall", kravs_epost=True)


# Berikningskällor: svarar bara på en fråga om en redan känd post. Deltar
# inte i discovery_sok. Används av discovery_oa_lank/discovery_citeringar.
BERIKNING: dict[str, dict] = {
    "unpaywall": {
        "etikett": "Unpaywall",
        "beskrivning": "Open access-status och fulltextlänk för en DOI.",
        "aktiv": kallkonfig.aktiv("unpaywall") and _unpaywall_krav() is None,
        "krav_saknas": _unpaywall_krav(),
        "anvands_av": ["discovery_oa_lank"],
    },
    "semanticscholar": {
        "etikett": "Semantic Scholar",
        "beskrivning": "Citeringsgraf (citerande verk och referenser).",
        "aktiv": kallkonfig.aktiv("semanticscholar"),
        "krav_saknas": None,
        "anvands_av": ["discovery_citeringar"],
    },
}


def lista_kallor() -> dict:
    """Returnerar alla registrerade källor — sökbara och berikande — med
    aktiv-status, filterstöd och en förklaring när en källa är avstängd
    på grund av ett saknat krav (nyckel eller kontakt-e-post)."""
    sokbara = []
    for namn, p in PROVIDERS.items():
        rad = {
            "namn": namn,
            "etikett": p["etikett"],
            "beskrivning": p["beskrivning"],
            "aktiv": p["aktiv"],
            "filterstod": p.get("filterstod", []),
            "profil": orkestrering.profil(namn),
        }
        if p.get("krav_saknas"):
            rad["inaktiverad_orsak"] = p["krav_saknas"]
        stat = orkestrering.statistik().get(namn)
        if stat:
            rad["statistik_sedan_start"] = stat
        sokbara.append(rad)

    berikning = []
    for namn, p in BERIKNING.items():
        rad = {
            "namn": namn,
            "etikett": p["etikett"],
            "beskrivning": p["beskrivning"],
            "aktiv": p["aktiv"],
            "anvands_av": p["anvands_av"],
        }
        if p.get("krav_saknas"):
            rad["inaktiverad_orsak"] = p["krav_saknas"]
        berikning.append(rad)

    return {
        "kallor": sokbara,
        "berikningskallor": berikning,
        "svarscache": svarscache.status(),
        "begreppsexpansion": begreppsexpansion.status(),
        "bibliotek": bibliotek.status(),
        "amnen": orkestrering.AMNEN,
        "typer": list(orkestrering.TYPER),
    }


def _aktiva_providers() -> dict[str, dict]:
    return {namn: p for namn, p in PROVIDERS.items() if p["aktiv"]}


# Delar av böcker och tidskriftsnummer som förlagen registrerar med egen
# DOI men som inte är publikationer i sig (Crossref: "Copyright", "Index",
# "Front Matter" …). De matchar frågan bara via bokens titel och tränger
# undan riktiga träffar.
_KRINGMATERIAL = frozenset({
    "copyright", "copyright page", "dedication", "front matter", "frontmatter",
    "back matter", "backmatter", "index", "subject index", "author index",
    "references", "bibliography", "contents", "table of contents",
    "contributors", "list of contributors", "notes on contributors",
    "about the authors", "about the author", "acknowledgments",
    "acknowledgements", "preface", "foreword", "title page", "half title",
    "list of figures", "list of tables", "abbreviations", "cover",
    "editorial board", "masthead", "series page", "blank page",
})


def _ar_kringmaterial(traff: dict) -> bool:
    """Kringmaterial, eller en post utan titel (går varken att bedöma eller citera)."""
    titel = " ".join(str(traff.get("titel") or "").lower().split()).strip(" .:")
    return not titel or titel in _KRINGMATERIAL


def _fraga_kallor(
    namn_lista: list[str],
    aktiva: dict[str, dict],
    *,
    q: str | dict,
    limit_per_kalla: int,
    fran_ar: int | None,
    till_ar: int | None,
    oppen_tillgang: bool | None,
    land: str | None,
    filter: dict[str, dict] | None,
    synonymer: dict[str, list[str]] | None = None,
) -> tuple[dict[str, list[dict]], dict[str, dict], dict[str, str]]:
    """Frågar källorna parallellt: (träfflistor, per_kalla, fel).

    Varje källa frågas i en egen tråd med en gemensam tidsgräns
    (_PER_KALLA_TIDSGRANS_S) — en långsam eller nedgången källa fördröjer
    inte svaret och fäller inte hela anropet, utan redovisas under fel.
    """
    def _fraga_en(namn: str):
        provider = aktiva[namn]
        kalla_filter = (filter or {}).get(namn)
        fraga = orkestrering.fraga_for_kalla(q, namn, synonymer)
        t0 = time.monotonic()
        try:
            svar, fran_cache = svarscache.hamta_eller_kor(
                namn, svarscache.SOK,
                {"q": fraga, "limit": limit_per_kalla, "fran_ar": fran_ar, "till_ar": till_ar,
                 "oppen_tillgang": oppen_tillgang, "land": land, "filter": kalla_filter},
                lambda: provider["sok"](fraga, limit_per_kalla, fran_ar, till_ar, oppen_tillgang, land, kalla_filter),
            )
            traffar = [_till_enhetligt(t) for t in svar.get("traffar", []) if not _ar_kringmaterial(t)]
            return namn, {
                "traffar": traffar,
                "totalt": svar.get("totalt"),
                "antal": len(traffar),
                "tid_s": round(time.monotonic() - t0, 2),
                "fran_cache": fran_cache,
                "fraga": fraga,
            }, None
        except provider["fel"] as exc:
            return namn, None, str(exc)
        except DiscoveryKallaFel as exc:  # källor som ärver basklassen
            return namn, None, str(exc)
        except Exception as exc:  # oväntat fel i en källa får inte fälla helheten
            return namn, None, f"Oväntat fel: {exc}"

    listor: dict[str, list[dict]] = {}
    per_kalla: dict[str, dict] = {}
    fel: dict[str, str] = {}
    if not namn_lista:
        return listor, per_kalla, fel

    # Poolen stängs utan att vänta in källor som överskrider tidsgränsen:
    # deras trådar får löpa klart i bakgrunden (klienternas egen timeout
    # sätter taket), men svaret väntar inte på dem.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=len(namn_lista))
    try:
        framtider = {pool.submit(_fraga_en, namn): namn for namn in namn_lista}
        klara, ej_klara = concurrent.futures.wait(framtider, timeout=_PER_KALLA_TIDSGRANS_S)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    for framtid in ej_klara:
        namn = framtider[framtid]
        fel[namn] = f"Källan svarade inte inom {_PER_KALLA_TIDSGRANS_S:.0f} s."
        orkestrering.registrera(namn, ok=False, fel=fel[namn], tidsgrans=True)
    for framtid in klara:
        namn, resultat, felmeddelande = framtid.result()
        if felmeddelande is not None:
            fel[namn] = felmeddelande
            orkestrering.registrera(
                namn, ok=False, fel=felmeddelande,
                overbelastad=orkestrering.ar_overbelastning(felmeddelande),
            )
            continue
        orkestrering.registrera(namn, ok=True, tid_s=resultat["tid_s"], fran_cache=resultat["fran_cache"])
        listor[namn] = resultat["traffar"]
        per_kalla[namn] = {k: resultat[k] for k in ("totalt", "antal", "tid_s", "fran_cache")}
        if isinstance(q, dict) or synonymer:
            per_kalla[namn]["fraga"] = resultat["fraga"]
    return listor, per_kalla, fel


def _kompakt(traff: dict, sammanfattning_max: int) -> dict:
    """Träffen i svarsform: kapat abstract och högst tre författare."""
    ut = dict(traff)
    ut["sammanfattning"], ut["sammanfattning_kapad"] = kapa_text(traff.get("sammanfattning"), sammanfattning_max)
    forfattare = traff.get("forfattare") or []
    if len(forfattare) > 3:
        ut["forfattare"] = forfattare[:3]
        ut["forfattare_antal"] = len(forfattare)
    return ut


def _sida(resultat: dict, start: int) -> dict:
    """En sida ur ett sparat, sammanslaget resultat."""
    alla = resultat["_alla"]
    limit = resultat["_limit"]
    sida = alla[start:start + limit]
    ut = {k: v for k, v in resultat.items() if not k.startswith("_")}
    ut["traffar"] = [_kompakt(t, resultat["_sammanfattning_max"]) for t in sida]
    ut["antal"] = len(sida)
    ut["antal_sammanslagna"] = len(alla)
    ut["fran_plats"] = start + 1
    if start + limit < len(alla):
        ut["fortsattning"] = orkestrering.spara_fortsattning(dict(resultat, _start=start + limit))
    return ut


def sok_alla(
    q: str | dict[str, str],
    *,
    kallor: list[str] | None = None,
    strategi: str = "auto",
    amne: str | None = None,
    typ: str | None = None,
    limit: int = 20,
    limit_per_kalla: int = 10,
    fran_ar: int | None = None,
    till_ar: int | None = None,
    oppen_tillgang: bool | None = None,
    land: str | None = None,
    filter: dict[str, dict] | None = None,
    sammanfattning_max: int = 300,
    expandera: str = "auto",
    synonymer: dict[str, list[str]] | None = None,
    sprak: list[str] | None = None,
) -> dict:
    """Orkestrerad sökning över flera källor, sammanslagen och rangordnad.

    q         - fritextfråga, eller en fråga per språk {"sv": …, "en": …};
                källor med språktyngdpunkt (Libris, DiVA, NVA …) får sitt
                språk, övriga engelska.
    kallor    - uttrycklig källista; går före strategi.
    strategi  - "auto": en kärna av källor efter frågans språk och
                amne/typ/land, breddad till allmänna källor och språkkällor
                om kärnan ger färre än hälften av limit. Ämnes- och
                typkällor som inte passar frågan frågas aldrig i auto.
                "bred": alla aktiva källor på en gång.
    amne/typ  - styr källvalet (se orkestrering.AMNEN/TYPER), filtrerar inte.
    limit     - antal träffar i svaret, efter sammanslagning. Resten nås
                med fortsattning-token (sok_fortsattning).
    limit_per_kalla, fran_ar, till_ar, oppen_tillgang, land, filter -
                skickas till varje källa (land styr även källvalet).
    sammanfattning_max - abstractet kapas till så många tecken; 0 utelämnar det.
    expandera - "auto": servern expanderar en fråga given som sträng till
                fler språk och synonymer om begreppsexpansionen är på (se
                begreppsexpansion.py). "av": ingen expansion. En fråga given
                som dict per språk expanderas aldrig av servern.
    synonymer - {språk: [termer]} från anroparen; läggs som ELLER-termer
                hos källor som stöder det.
    sprak     - extra språk att expandera till, utöver de som väljs efter
                frågans språk, ämne och land.

    En källa som nyss varit överbelastad (429 eller tidsgräns) är pausad
    en stund och redovisas under ej_fragade i stället för att fördröja svaret.
    """
    if expandera not in ("auto", "av"):
        raise ValueError(f"Okänt värde för expandera: '{expandera}'. Välj 'auto' eller 'av'.")
    if strategi not in ("auto", "bred"):
        raise ValueError(f"Okänd strategi '{strategi}'. Välj 'auto' eller 'bred'.")
    if amne and amne not in orkestrering.AMNEN:
        raise ValueError(f"Okänt ämne '{amne}'. Giltiga: {', '.join(orkestrering.AMNEN)}.")
    if typ and typ not in orkestrering.TYPER:
        raise ValueError(f"Okänd typ '{typ}'. Giltiga: {', '.join(orkestrering.TYPER)}.")
    if isinstance(q, dict):
        q = {k.strip().lower(): v for k, v in q.items() if v and v.strip()}
    if not q:
        raise ValueError("Ange en fråga (q).")

    q, synonymer, expansion = _expandera(q, amne=amne, land=land, sprak=sprak,
                                         expandera=expandera, synonymer=synonymer)

    aktiva = _aktiva_providers()
    ej_fragade: dict[str, str] = {}

    if kallor:
        okanda = [k for k in kallor if k not in aktiva]
        if okanda:
            tillgangliga = ", ".join(aktiva) or "(inga aktiva källor)"
            raise ValueError(f"Okänd eller avstängd källa/källor: {', '.join(okanda)}. Tillgängliga: {tillgangliga}.")
        karna = {k: "uttryckligen vald" for k in kallor}
    elif strategi == "bred":
        karna = {k: "strategi: bred" for k in aktiva}
    else:
        karna, ej_fragade = orkestrering.valj_kallor(list(aktiva), q=q, amne=amne, typ=typ, land=land)

    def _utan_pausade(kandidater: dict[str, str]) -> dict[str, str]:
        kvar = {}
        for namn, skal in kandidater.items():
            pausad = orkestrering.ar_pausad(namn)
            if pausad:
                ej_fragade[namn] = f"pausad i {pausad:.0f} s till efter överbelastning"
            else:
                kvar[namn] = skal
        return kvar

    # Språk som ingen vald källa täcker får ett språkfiltrerat OpenAlex-anrop.
    if "openalex" in aktiva and not kallor:
        for s in orkestrering.sprak_utan_egen_kalla(q, list(karna)):
            namn = f"openalex ({s})"
            aktiva[namn] = dict(aktiva["openalex"], sok=_openalex_sprak_sok(s))
            karna[namn] = f"språk: {s} (OpenAlex filtrerat på språket)"

    karna = _utan_pausade(karna)
    # Ett limit större än vad kärnan kan ge med limit_per_kalla höjer
    # antalet per källa, i stället för att fler källor frågas i onödan.
    limit_per_kalla = min(50, max(limit_per_kalla, -(-limit // max(1, len(karna)))))
    parametrar = dict(q=q, limit_per_kalla=limit_per_kalla, fran_ar=fran_ar, till_ar=till_ar,
                      oppen_tillgang=oppen_tillgang, land=land, filter=filter, synonymer=synonymer)
    listor, per_kalla, fel = _fraga_kallor(list(karna), aktiva, **parametrar)
    fragade = dict(karna)

    # Breddning: bara i auto-läget, bara när kärnan gav för få unika träffar.
    breddad = False
    if strategi == "auto" and not kallor:
        unika = sum(
            orkestrering.ar_traffsaker(t, q) for t in _sla_ihop_och_rangordna(list(listor.items()))
        )
        if orkestrering.behover_breddning(unika, limit):
            extra = _utan_pausade({
                namn: f"breddning: kärnan gav bara {unika} träffar med frågans ord"
                for namn, skal in ej_fragade.items() if skal == "frågas vid breddning"
            })
            if extra:
                breddad = True
                for namn in extra:
                    ej_fragade.pop(namn, None)
                l2, p2, f2 = _fraga_kallor(list(extra), aktiva, **parametrar)
                listor.update(l2)
                per_kalla.update(p2)
                fel.update(f2)
                fragade.update(extra)

    # Ordning i fragade (kärnan först), inte i färdigordning — annars styr
    # svarstiden vilken källas fält som vinner vid sammanslagning.
    alla = _sla_ihop_och_rangordna([(namn, listor[namn]) for namn in fragade if namn in listor])
    # Träffar med frågans ord i titel eller abstract före övriga, med
    # rangordningen bevarad inom båda grupperna. Övriga är ofta källor som
    # matchat enstaka ord (Crossref) och hamnar annars högt bara för att de
    # var först i sin källas lista.
    alla.sort(key=lambda t: not orkestrering.ar_traffsaker(t, q))
    alla = orkestrering.sakra_sprak(alla, q, max(1, limit))
    if (isinstance(q, dict) and len(q) > 1) or synonymer:
        for t in alla:
            t["matchade_termer"] = orkestrering.matchade_termer(t, q, synonymer)

    resultat = {
        "fraga": q,
        "strategi": "uttrycklig källista" if kallor else strategi,
        "breddad": breddad,
        "fragade_kallor": fragade,
        "ej_fragade": ej_fragade,
        "per_kalla": per_kalla,
        "begreppsexpansion": expansion,
        "_alla": alla,
        "_limit": max(1, limit),
        "_sammanfattning_max": sammanfattning_max,
    }
    if fel:
        resultat["fel"] = fel
    return _sida(resultat, 0)


def _expandera(
    q: str | dict,
    *,
    amne: str | None,
    land: str | None,
    sprak: list[str] | None,
    expandera: str,
    synonymer: dict[str, list[str]] | None,
) -> tuple[str | dict, dict[str, list[str]] | None, dict]:
    """Frågan efter begreppsexpansion: (q, synonymer, redovisning).

    Anroparens egna varianter och synonymer går alltid före; servern
    expanderar bara en fråga given som sträng, och bara om expansionen är
    påslagen. Redovisningen säger vilka språk som valdes och varför, vem
    som expanderade, vilka språk som saknas och kända täckningsluckor."""
    sprak_lista, sprak_skal = orkestrering.expansionssprak(q, amne=amne, land=land, sprak=sprak)
    redovisning: dict = {"sprak": sprak_skal, "gjord_av": None}
    synonymer = {k.lower(): v for k, v in (synonymer or {}).items() if v} or None

    if isinstance(q, dict):
        redovisning["gjord_av"] = "anroparen"
    elif expandera == "auto" and begreppsexpansion.aktiv():
        original = q
        exp, _ = svarscache.hamta_eller_kor(
            "begreppsexpansion", svarscache.POST,
            {"q": original, "sprak": sprak_lista, "amne": amne},
            lambda: begreppsexpansion.expandera(original, sprak=sprak_lista, amne=amne),
        )
        if exp:
            # Användarens egen formulering går före modellens på frågans språk.
            q = {**exp["varianter"], orkestrering.gissa_sprak(original): original}
            synonymer = {**exp["synonymer"], **(synonymer or {})} or None
            redovisning.update(gjord_av="servern", nyckelord=exp.get("nyckelord", []))

    finns = set(q) if isinstance(q, dict) else orkestrering.fragans_sprak(q)
    saknas = [s for s in sprak_lista if s not in finns]
    if saknas:
        redovisning["saknade_sprak"] = saknas
        redovisning["rad"] = (
            "Frågan söktes inte på " + ", ".join(saknas) + ". Skicka q per språk, "
            "t.ex. {\"sv\": …, \"en\": …}, för att nå litteratur på dem."
        )
    varningar = {s: orkestrering.TACKNINGSVARNINGAR[s] for s in sprak_lista if s in orkestrering.TACKNINGSVARNINGAR}
    if varningar:
        redovisning["tackningsvarningar"] = varningar
    if isinstance(q, dict):
        redovisning["varianter"] = q
    if synonymer:
        redovisning["synonymer"] = synonymer
    return q, synonymer, redovisning


def expandera_fraga(q: str | dict, *, amne: str | None = None, land: str | None = None,
                    sprak: list[str] | None = None) -> dict:
    """Begreppsexpansionen utan sökning, för discovery_expandera."""
    if amne and amne not in orkestrering.AMNEN:
        raise ValueError(f"Okänt ämne '{amne}'. Giltiga: {', '.join(orkestrering.AMNEN)}.")
    if not q:
        raise ValueError("Ange en fråga (q).")
    _, _, redovisning = _expandera(q, amne=amne, land=land, sprak=sprak, expandera="auto", synonymer=None)
    redovisning["server_expansion"] = begreppsexpansion.status()
    return redovisning


def sok_fortsattning(token: str) -> dict:
    """Nästa sida ur ett tidigare discovery_sok-resultat, utan nya källanrop."""
    sparat = orkestrering.hamta_fortsattning(token)
    if sparat is None:
        raise ValueError("Fortsättningen finns inte längre (den gäller en timme). Sök igen.")
    return _sida(sparat, sparat["_start"])


# Konstanten i reciprocal rank fusion. 60 är standardvärdet i litteraturen
# (Cormack m.fl. 2009) och dämpar skillnaden mellan plats 1 och 2 lagom.
_RRF_K = 60


def _sla_ihop_och_rangordna(listor: list[tuple[str, list[dict]]]) -> list[dict]:
    """Slår ihop källornas träfflistor till en, rangordnad efter relevans.

    Varje källa har redan rangordnat sina träffar efter relevans för frågan;
    den ordningen bevaras med reciprocal rank fusion: en träff får
    1/(k + plats) från varje källa som hittade den. En DOI som flera källor
    hittar får därmed poäng från alla — samstämmighet mellan oberoende
    index är en stark relevanssignal. Poster utan DOI kan inte jämföras
    säkert och står var för sig.

    Vid sammanslagning fylls tomma fält från senare källor i (t.ex.
    oa_lank eller citeringar som bara en av källorna har), och
    "hittad_i" visar alla källor som hittade posten.
    """
    poster: dict[str, dict] = {}
    poang: dict[str, float] = {}
    for namn, traffar in listor:
        for plats, traff in enumerate(traffar, start=1):
            nyckel = f"doi:{traff['doi']}" if traff.get("doi") else f"{namn}:{traff.get('kalla_id') or plats}"
            if nyckel in poster:
                befintlig = poster[nyckel]
                for falt, varde in traff.items():
                    if befintlig.get(falt) in (None, "", []) and varde not in (None, "", []):
                        befintlig[falt] = varde
                if namn not in befintlig["hittad_i"]:
                    befintlig["hittad_i"].append(namn)
            else:
                poster[nyckel] = dict(traff, hittad_i=[namn])
            poang[nyckel] = poang.get(nyckel, 0.0) + 1.0 / (_RRF_K + plats)
    ordning = sorted(poster, key=lambda n: poang[n], reverse=True)
    return [poster[n] for n in ordning]


def hamta_fran_kalla(kalla: str, id_: str) -> dict:
    """Hämtar en enskild post från en namngiven källa via discovery_hamta.

    Fångar källans egen felklass och lyfter den vidare oförändrad — MCP-
    lagret (mcp_server.py) gör om den till ToolError.
    """
    aktiva = _aktiva_providers()
    if kalla not in aktiva:
        tillgangliga = ", ".join(aktiva) or "(inga aktiva källor)"
        raise ValueError(f"Okänd eller avstängd källa '{kalla}'. Tillgängliga: {tillgangliga}.")
    provider = aktiva[kalla]
    if "hamta" not in provider or provider["hamta"] is None:
        raise ValueError(f"Källan '{kalla}' stödjer inte hämtning av en enskild post ännu.")
    svar, _ = svarscache.hamta_eller_kor(kalla, svarscache.POST, {"id": id_}, lambda: provider["hamta"](id_))
    return _till_enhetligt(svar)


def oa_lank(doi: str) -> dict:
    """Öppen tillgång-länk för en DOI: OpenAlex, kompletterad med Unpaywall
    om den källan är aktiv. Används av mcp_server.discovery_oa_lank."""
    resultat: dict = {"doi": doi, "kallor": {}}

    if PROVIDERS["openalex"]["aktiv"]:
        try:
            post, _ = svarscache.hamta_eller_kor(
                "openalex", svarscache.POST, {"id": doi}, lambda: openalex_client.hamta(doi)
            )
            resultat["kallor"]["openalex"] = {"oa_lank": post.get("oa_lank")}
        except openalex_client.OpenAlexFel as exc:
            resultat["kallor"]["openalex"] = {"fel": str(exc)}

    if BERIKNING["unpaywall"]["aktiv"]:
        try:
            post, _ = svarscache.hamta_eller_kor(
                "unpaywall", svarscache.POST, {"doi": doi}, lambda: unpaywall_client.hamta(doi)
            )
            resultat["kallor"]["unpaywall"] = {
                "oa_lank": post.get("oa_lank"),
                "is_oa": post.get("is_oa"),
                "oa_status": post.get("oa_status"),
            }
        except unpaywall_client.UnpaywallFel as exc:
            resultat["kallor"]["unpaywall"] = {"fel": str(exc)}

    # Bästa sammanfattande länk: första källan (i registreringsordning) som
    # faktiskt har en.
    resultat["oa_lank"] = next(
        (v.get("oa_lank") for v in resultat["kallor"].values() if v.get("oa_lank")), None
    )
    return resultat


def citeringar(id_eller_doi: str, *, riktning: str = "citerande", limit: int = 20) -> dict:
    """Citeringsgraf för en post: OpenAlex, kompletterad med Semantic
    Scholar om den källan är aktiv. Används av mcp_server.discovery_citeringar."""
    resultat: dict = {"id": id_eller_doi, "riktning": riktning, "kallor": {}}

    if PROVIDERS["openalex"]["aktiv"]:
        try:
            svar, _ = svarscache.hamta_eller_kor(
                "openalex", svarscache.SOK,
                {"citeringar": id_eller_doi, "riktning": riktning, "limit": limit},
                lambda: openalex_client.citeringar(id_eller_doi, riktning=riktning, limit=limit),
            )
            resultat["kallor"]["openalex"] = {
                "totalt": svar.get("totalt"),
                "antal": svar.get("antal"),
                "traffar": svar.get("traffar", []),
            }
        except openalex_client.OpenAlexFel as exc:
            resultat["kallor"]["openalex"] = {"fel": str(exc)}

    if BERIKNING["semanticscholar"]["aktiv"]:
        try:
            svar = semanticscholar_client.citeringar(id_eller_doi, riktning=riktning, limit=limit)
            resultat["kallor"]["semanticscholar"] = {
                "antal": svar.get("antal"),
                "traffar": svar.get("traffar", []),
            }
        except semanticscholar_client.SemanticScholarFel as exc:
            resultat["kallor"]["semanticscholar"] = {"fel": str(exc)}

    if not resultat["kallor"]:
        raise ValueError("Ingen citeringskälla är aktiv (OpenAlex och Semantic Scholar är avstängda).")

    return resultat


def citera(
    *,
    doi: str | None = None,
    kalla: str | None = None,
    id_: str | None = None,
    format: str = "apa",
    sprak: str = "sv-SE",
) -> dict:
    """Formaterad referens för en post, via DOI eller via (kalla, id).

    Har posten en DOI formateras referensen av doi.org (se citering.py);
    annars ur postens egna metadata. Används av mcp_server.discovery_citera.
    """
    post = None
    doi = normalisera_doi(doi)
    if not doi:
        if not (kalla and id_):
            raise ValueError("Ange doi, eller både kalla och id.")
        post = hamta_fran_kalla(kalla, id_)
        doi = post.get("doi")

    if doi:
        referens, _ = svarscache.hamta_eller_kor(
            "doi.org", svarscache.POST, {"doi": doi, "format": format, "sprak": sprak},
            lambda: citering.via_doi(doi, format=format, sprak=sprak),
        )
        return {"doi": doi, "format": format, "referens": referens, "kalla": "doi.org"}

    return {
        "doi": None,
        "format": format,
        "referens": citering.ur_post(post, format=format),
        "kalla": f"{kalla} (postens egna metadata)",
    }


# ---------------------------------------------------------------------------
# Arbetsbibliotek
# ---------------------------------------------------------------------------

def _oa_platser(doi: str) -> list[tuple[str, str | None]]:
    """Öppna kopior för en DOI från Unpaywall (om aktiv) och OpenAlex."""
    platser: list[tuple[str, str | None]] = []
    if BERIKNING["unpaywall"]["aktiv"]:
        try:
            post, _ = svarscache.hamta_eller_kor(
                "unpaywall", svarscache.POST, {"doi": doi}, lambda: unpaywall_client.hamta(doi))
            platser += [(p["url"], p.get("licens")) for p in post.get("oa_platser", [])]
        except DiscoveryKallaFel:
            pass
    if PROVIDERS["openalex"]["aktiv"] and not orkestrering.ar_pausad("openalex"):
        try:
            lista, _ = svarscache.hamta_eller_kor(
                "openalex", svarscache.POST, {"oa_platser": doi}, lambda: openalex_client.oa_platser(doi))
            platser += [(p["url"], p.get("licens")) for p in lista]
        except DiscoveryKallaFel:
            pass
    return platser


def spara_i_bibliotek(
    *,
    kalla: str | None = None,
    id_: str | None = None,
    doi: str | None = None,
    projekt: str | None = None,
    hamta_fulltext: bool = True,
) -> dict:
    """Sparar en post i arbetsbiblioteket med abstract, referens och fulltext.

    Posten läses från den namngivna källan (kalla + id), eller via DOI från
    OpenAlex och i andra hand Crossref."""
    doi = normalisera_doi(doi)
    if kalla and id_:
        post = hamta_fran_kalla(kalla, id_)
    elif doi:
        post = None
        fel = []
        for kandidat in ("openalex", "crossref", "datacite"):
            if kandidat in _aktiva_providers():
                try:
                    post = hamta_fran_kalla(kandidat, doi)
                    break
                except (ValueError, DiscoveryKallaFel, *(p["fel"] for p in PROVIDERS.values())) as exc:
                    fel.append(f"{kandidat}: {exc}")
        if post is None:
            raise ValueError(f"DOI:n '{doi}' hittades inte: " + "; ".join(fel))
    else:
        raise ValueError("Ange kalla och id, eller doi.")
    if not post.get("titel"):
        raise ValueError("Posten saknar titel och kan inte sparas.")

    try:
        referens = citera(doi=post.get("doi"), kalla=post["kalla"], id_=post["kalla_id"])["referens"]
    except Exception:  # noqa: BLE001 — en post utan färdig referens sparas ändå
        referens = None

    hamta = hamta_fulltext and not bibliotek.har_fulltext(bibliotek.post_id(post))
    resultat = fulltext.hitta(post, oa_uppslag=_oa_platser) if hamta else None
    return bibliotek.spara(post, projekt=projekt, referens=referens, fulltext_resultat=resultat)
