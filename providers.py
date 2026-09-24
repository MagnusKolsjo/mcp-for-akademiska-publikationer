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

Källor med ett helt annat frågespråk (Libris) eller ett eget dedikerat
MCP-verktyg (Libris, Crossref, DataCite, arXiv — "Befintliga verktyg
BEHÅLLS oförändrade") har kvar sina egna verktyg utanför den här filen,
men deltar ändå i den enade sökningen via PROVIDERS nedan precis som förut.
"""

from __future__ import annotations

import concurrent.futures
import time

import arxiv_client
import crossref_client
import datacite_client
import kallkonfig
import openalex_client
import semanticscholar_client
import unpaywall_client
from kallhjalp import DiscoveryKallaFel

# En källas anrop i discovery_sok får högst så här lång tid, oavsett dess
# egen strypningstakt — annars kan en enda långsam eller nedgången källa
# göra den enade sökningen orimligt trög. Källor som svarar långsammare än
# så redovisas som ett fel för just den källan, övriga källor är opåverkade.
_PER_KALLA_TIDSGRANS_S = 15.0


# ---------------------------------------------------------------------------
# Enhetligt träffschema
# ---------------------------------------------------------------------------

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
        "doi": traff.get("doi"),
        "titel": traff.get("titel"),
        "forfattare": traff.get("forfattare") or [],
        "ar": traff.get("ar"),
        "typ": traff.get("typ"),
        "url": traff.get("url"),
        "oa_lank": traff.get("oa_lank") or traff.get("url_pdf"),
        "citeringar": traff.get("citeringar"),
    }


# ---------------------------------------------------------------------------
# Adaptrar för källor med ett rikare eller äldre eget schema
# ---------------------------------------------------------------------------

def _arxiv_normaliserad(traff: dict) -> dict:
    """Formar en arxiv_client-träff (eget, rikare fältschema) till skalet
    _till_enhetligt() sedan fyller i. arXiv saknar utgivningsår i egentlig
    mening — "ar" sätts från inskickningsdatumet (publicerad)."""
    publicerad = traff.get("publicerad") or ""
    ar = int(publicerad[:4]) if publicerad[:4].isdigit() else None
    return {
        "kalla": arxiv_client.KALLA,
        "kalla_id": traff.get("id"),
        "doi": traff.get("doi"),
        "titel": traff.get("titel"),
        "forfattare": traff.get("forfattare", []),
        "ar": ar,
        "typ": "preprint",
        "url": traff.get("url_abs"),
        "oa_lank": traff.get("url_pdf"),
        "citeringar": None,
    }


def _arxiv_sok(q, limit, fran_ar, till_ar, oppen_tillgang, land, kalla_filter):
    """arXiv stödjer varken oppen_tillgang- eller land-filtrering — allt är
    fritt tillgängligt och källan saknar institutionsdata; parametrarna tas
    emot för att matcha den gemensamma sok-signaturen men ignoreras."""
    svar = arxiv_client.sok(q, limit=limit, fran_ar=fran_ar, till_ar=till_ar)
    return {
        "totalt": svar.get("totalt"),
        "antal": svar.get("antal"),
        "traffar": [_arxiv_normaliserad(t) for t in svar.get("traffar", [])],
    }


def _arxiv_hamta(id_: str) -> dict:
    return _arxiv_normaliserad(arxiv_client.hamta(id_, format="kort"))


def _doi_kalla_sok(client, *, kalla_namn):
    """Bygger en sok-funktion för Crossref/DataCite: samma gamla anropsform
    (q, limit, fran_ar, till_ar), oppen_tillgang/land/kalla_filter ignoreras
    — ingen av dem stödjer den filtreringen i den kompakta sökformen."""

    def _sok(q, limit, fran_ar, till_ar, oppen_tillgang, land, kalla_filter):
        return client.sok(q, limit=limit, fran_ar=fran_ar, till_ar=till_ar)

    return _sok


def _doi_kalla_hamta(client):
    def _hamta(id_: str) -> dict:
        return _till_enhetligt(client.hamta(id_, format="kort"))

    return _hamta


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
        "aktiv": True,
        "krav_saknas": None,
        "filterstod": [],
    },
    "datacite": {
        "etikett": "DataCite",
        "beskrivning": "DOI:er för forskningsdata, programvara och preprints.",
        "sok": _doi_kalla_sok(datacite_client, kalla_namn="datacite"),
        "hamta": _doi_kalla_hamta(datacite_client),
        "fel": datacite_client.DataCiteFel,
        "aktiv": True,
        "krav_saknas": None,
        "filterstod": [],
    },
    "arxiv": {
        "etikett": "arXiv",
        "beskrivning": "Preprints inom fysik, matematik, datavetenskap m.fl.",
        "sok": _arxiv_sok,
        "hamta": _arxiv_hamta,
        "fel": arxiv_client.ArxivFel,
        "aktiv": True,
        "krav_saknas": None,
        "filterstod": [],
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

    return {"kallor": sokbara, "berikningskallor": berikning}


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

    Varje källa frågas i en egen tråd med en delad tidsgräns
    (_PER_KALLA_TIDSGRANS_S) — en långsam eller nedgången källa fördröjer
    inte de andra och fäller inte hela anropet. Resultatet dedupliceras på
    DOI (första träffen för en given DOI vinner) och sorteras nyast först.
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
            svar = provider["sok"](q, limit_per_kalla, fran_ar, till_ar, oppen_tillgang, land, kalla_filter)
            traffar = [_till_enhetligt(t) for t in svar.get("traffar", [])]
            return namn, {
                "traffar": traffar,
                "totalt": svar.get("totalt"),
                "antal": svar.get("antal", len(traffar)),
                "tid_s": round(time.monotonic() - t0, 2),
            }, None
        except provider["fel"] as exc:
            return namn, None, str(exc)
        except DiscoveryKallaFel as exc:  # källor som ärver basklassen
            return namn, None, str(exc)
        except Exception as exc:  # oväntat fel i en källa får inte fälla helheten
            return namn, None, f"Oväntat fel: {exc}"

    traffar: list[dict] = []
    per_kalla: dict[str, dict] = {}
    fel: dict[str, str] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(valda))) as pool:
        framtider = {pool.submit(_fraga_en, namn): namn for namn in valda}
        for framtid in concurrent.futures.as_completed(framtider, timeout=_PER_KALLA_TIDSGRANS_S + 5):
            namn = framtider[framtid]
            try:
                namn, resultat, felmeddelande = framtid.result(timeout=_PER_KALLA_TIDSGRANS_S)
            except concurrent.futures.TimeoutError:
                fel[namn] = f"Källan svarade inte inom {_PER_KALLA_TIDSGRANS_S:.0f} s."
                continue
            if felmeddelande is not None:
                fel[namn] = felmeddelande
                continue
            traffar.extend(resultat["traffar"])
            per_kalla[namn] = {
                "totalt": resultat["totalt"],
                "antal": resultat["antal"],
                "tid_s": resultat["tid_s"],
            }

    # Deduplicera på DOI — första träffen för en given DOI vinner. Träffar
    # utan DOI (vanligt för böcker, rapporter, vissa DiVA/HAL-poster) kan
    # inte jämföras säkert och behålls alla.
    sedda_doi: set[str] = set()
    deduplicerade: list[dict] = []
    for t in traffar:
        doi = t.get("doi")
        if doi:
            if doi in sedda_doi:
                continue
            sedda_doi.add(doi)
        deduplicerade.append(t)

    deduplicerade.sort(key=lambda t: (t.get("ar") is None, -(t.get("ar") or 0)))

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
    return _till_enhetligt(provider["hamta"](id_))


def oa_lank(doi: str) -> dict:
    """Öppen tillgång-länk för en DOI: OpenAlex, kompletterad med Unpaywall
    om den källan är aktiv. Används av mcp_server.discovery_oa_lank."""
    resultat: dict = {"doi": doi, "kallor": {}}

    if PROVIDERS["openalex"]["aktiv"]:
        try:
            post = openalex_client.hamta(doi)
            resultat["kallor"]["openalex"] = {"oa_lank": post.get("oa_lank")}
        except openalex_client.OpenAlexFel as exc:
            resultat["kallor"]["openalex"] = {"fel": str(exc)}

    if BERIKNING["unpaywall"]["aktiv"]:
        try:
            post = unpaywall_client.hamta(doi)
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
            svar = openalex_client.citeringar(id_eller_doi, riktning=riktning, limit=limit)
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
