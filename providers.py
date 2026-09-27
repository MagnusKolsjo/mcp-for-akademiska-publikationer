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
import osf_client
import publicera_client
import semanticscholar_client
import svarscache
import swepub_client
import unpaywall_client
import zbmath_client
from kallhjalp import DiscoveryKallaFel, kapa_text

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
        }
        if p.get("krav_saknas"):
            rad["inaktiverad_orsak"] = p["krav_saknas"]
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

    return {"kallor": sokbara, "berikningskallor": berikning, "svarscache": svarscache.status()}


def _aktiva_providers() -> dict[str, dict]:
    return {namn: p for namn, p in PROVIDERS.items() if p["aktiv"]}


def sok_alla(
    q: str,
    *,
    kallor: list[str] | None = None,
    limit_per_kalla: int = 10,
    fran_ar: int | None = None,
    till_ar: int | None = None,
    oppen_tillgang: bool | None = None,
    land: str | None = None,
    filter: dict[str, dict] | None = None,
    sammanfattning_max: int = 300,
) -> dict:
    """Slår mot flera källor parallellt och slår ihop de normaliserade träffarna.

    q               - fritextfråga som skickas till varje vald källa.
    kallor          - lista med källnamn (se lista_kallor). Utelämnad = alla
                      aktiva källor. En avstängd källa kan inte väljas
                      explicit heller — den finns inte i sökrummet.
    limit_per_kalla - max antal träffar per källa innan sammanslagning.
    fran_ar/till_ar - utgivningsårsintervall som skickas till varje källa.
    oppen_tillgang  - filtrera på öppen tillgång (stöds inte av alla källor).
    land            - ISO-landskod för författarnas institutioner (samma).
    filter          - källspecifika råfilter: {"openalex": {...}, ...}.
    sammanfattning_max - abstractet i varje träff kapas till så många tecken
                      (markerat med "…" och sammanfattning_kapad=True);
                      0 utelämnar det. discovery_hamta ger hela texten.

    Varje källa frågas i en egen tråd med en gemensam tidsgräns
    (_PER_KALLA_TIDSGRANS_S) — en långsam eller nedgången källa fördröjer
    inte svaret och fäller inte hela anropet, utan redovisas under "fel".
    Träfflistorna slås ihop på DOI och rangordnas efter relevans med
    reciprocal rank fusion (se _sla_ihop_och_rangordna).
    """
    aktiva = _aktiva_providers()
    valda = kallor or list(aktiva)
    okanda = [k for k in valda if k not in aktiva]
    if okanda:
        tillgangliga = ", ".join(aktiva) or "(inga aktiva källor)"
        raise ValueError(f"Okänd eller avstängd källa/källor: {', '.join(okanda)}. Tillgängliga: {tillgangliga}.")

    def _fraga_en(namn: str):
        provider = aktiva[namn]
        kalla_filter = (filter or {}).get(namn)
        t0 = time.monotonic()
        try:
            svar, fran_cache = svarscache.hamta_eller_kor(
                namn, svarscache.SOK,
                {"q": q, "limit": limit_per_kalla, "fran_ar": fran_ar, "till_ar": till_ar,
                 "oppen_tillgang": oppen_tillgang, "land": land, "filter": kalla_filter},
                lambda: provider["sok"](q, limit_per_kalla, fran_ar, till_ar, oppen_tillgang, land, kalla_filter),
            )
            traffar = [_till_enhetligt(t) for t in svar.get("traffar", [])]
            return namn, {
                "traffar": traffar,
                "totalt": svar.get("totalt"),
                "antal": svar.get("antal", len(traffar)),
                "tid_s": round(time.monotonic() - t0, 2),
                "fran_cache": fran_cache,
            }, None
        except provider["fel"] as exc:
            return namn, None, str(exc)
        except DiscoveryKallaFel as exc:  # källor som ärver basklassen
            return namn, None, str(exc)
        except Exception as exc:  # oväntat fel i en källa får inte fälla helheten
            return namn, None, f"Oväntat fel: {exc}"

    per_kalla: dict[str, dict] = {}
    fel: dict[str, str] = {}
    listor: dict[str, list[dict]] = {}

    # Poolen stängs utan att vänta in källor som överskrider tidsgränsen:
    # deras trådar får löpa klart i bakgrunden (klienternas egen timeout
    # sätter taket), men svaret väntar inte på dem.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(valda)))
    try:
        framtider = {pool.submit(_fraga_en, namn): namn for namn in valda}
        klara, ej_klara = concurrent.futures.wait(framtider, timeout=_PER_KALLA_TIDSGRANS_S)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    for framtid in ej_klara:
        fel[framtider[framtid]] = f"Källan svarade inte inom {_PER_KALLA_TIDSGRANS_S:.0f} s."
    for framtid in klara:
        namn, resultat, felmeddelande = framtid.result()
        if felmeddelande is not None:
            fel[namn] = felmeddelande
            continue
        listor[namn] = resultat["traffar"]
        per_kalla[namn] = {
            "totalt": resultat["totalt"],
            "antal": resultat["antal"],
            "tid_s": resultat["tid_s"],
            "fran_cache": resultat["fran_cache"],
        }

    # Ordning i valda-listan, inte i färdigordning — annars styr svarstiden
    # vilken källas fält som vinner vid sammanslagning.
    deduplicerade = _sla_ihop_och_rangordna([(namn, listor[namn]) for namn in valda if namn in listor])

    # Kapningen sker efter sammanslagningen, så att en dubblett med längre
    # abstract hos en annan källa inte förlorar texten i förväg.
    for t in deduplicerade:
        t["sammanfattning"], t["sammanfattning_kapad"] = kapa_text(t.get("sammanfattning"), sammanfattning_max)

    resultat = {
        "fraga": q,
        "kallor": list(valda),
        "per_kalla": per_kalla,
        "antal": len(deduplicerade),
        "traffar": deduplicerade,
    }
    if fel:
        resultat["fel"] = fel
    return resultat


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
