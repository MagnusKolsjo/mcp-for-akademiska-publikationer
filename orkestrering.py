# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Sökorkestrering: vilka källor en fråga skickas till, och i vilket språk.

Att skicka varje fråga till alla källor kostar API-anrop (och pengar hos
OpenAlex), tid och tokens i svaret — och ger sämre träffar, eftersom en
matematikdatabas svar på en fråga om äldres ensamhet bara är brus. Här
väljs i stället en kärna av källor utifrån frågans språk, ämne, dokumenttyp
och land; övriga källor frågas bara om kärnan ger för få träffar.

Reglerna är deterministiska och synliga: varje svar redovisar vilka källor
som frågades och varför, och vilka som inte frågades. Den anropande
assistenten kan då bredda själv, med strategi="bred" eller en uttrycklig
källista.

Här finns också kretsbrytaren (en källa som nyss svarat 429 eller inte
svarat i tid pausas en stund), statistik per källa sedan serverstart, och
fortsättningslagret för att bläddra i ett sammanslaget resultat utan att
fråga källorna igen.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from collections import OrderedDict

# ---------------------------------------------------------------------------
# Källprofiler
# ---------------------------------------------------------------------------

# Kontrollerat ämnesordförråd för parametern amne. Nycklarna är ASCII så att
# de går att skriva utan svenska tecken; värdet är en läsbar etikett.
AMNEN = {
    "medicin": "medicin och hälsa",
    "biologi": "biologi och life science",
    "psykologi": "psykologi",
    "matematik": "matematik",
    "statistik": "statistik",
    "fysik": "fysik och astronomi",
    "datavetenskap": "datavetenskap och AI",
    "teknik": "teknik",
    "ekonomi": "ekonomi",
    "samhallsvetenskap": "samhällsvetenskap",
    "juridik": "juridik",
    "utbildning": "utbildningsvetenskap",
    "humaniora": "humaniora",
    "miljo": "miljö och klimat",
}

TYPER = ("artikel", "bok", "avhandling", "rapport", "konferens", "preprint", "dataset", "programvara")

# roll: "bred" frågas alltid i auto-läget; "special" bara när språk, ämne,
# typ eller land matchar; "komplement" bara vid breddning.
# sprak: språkkoder källan har tyngdpunkt i (tomt = alla). En källa med
# språk får frågan på det språket om den finns (q som dict per språk).
PROFILER: dict[str, dict] = {
    "openalex": {"roll": "bred", "sprak": [], "amnen": [], "typer": [], "land": []},
    "crossref": {"roll": "bred", "sprak": [], "amnen": [], "typer": [], "land": []},
    "libris": {"roll": "special", "sprak": ["sv"], "amnen": [], "typer": ["bok", "avhandling", "rapport"], "land": ["SE"]},
    "swepub": {"roll": "special", "sprak": ["sv"], "amnen": [], "typer": ["artikel", "avhandling", "rapport", "konferens"], "land": ["SE"]},
    "diva": {"roll": "special", "sprak": ["sv"], "amnen": [], "typer": ["avhandling", "rapport"], "land": ["SE"]},
    "publicera": {"roll": "special", "sprak": ["sv"], "amnen": [], "typer": [], "land": ["SE"]},
    "nva": {"roll": "special", "sprak": ["no", "nb", "nn"], "amnen": [], "typer": [], "land": ["NO"]},
    "datacite": {"roll": "special", "sprak": [], "amnen": [], "typer": ["dataset", "programvara"], "land": []},
    "arxiv": {"roll": "special", "sprak": [], "amnen": ["fysik", "matematik", "datavetenskap", "statistik"], "typer": ["preprint"], "land": []},
    "osf": {"roll": "special", "sprak": [], "amnen": ["samhallsvetenskap", "psykologi", "juridik", "utbildning"], "typer": ["preprint"], "land": []},
    "europepmc": {"roll": "special", "sprak": [], "amnen": ["medicin", "biologi", "psykologi"], "typer": [], "land": []},
    "zbmath": {"roll": "special", "sprak": [], "amnen": ["matematik", "statistik"], "typer": [], "land": []},
    "econbiz": {"roll": "special", "sprak": [], "amnen": ["ekonomi"], "typer": [], "land": []},
    "hal": {"roll": "special", "sprak": ["fr"], "amnen": ["humaniora", "samhallsvetenskap"], "typer": [], "land": ["FR"]},
    "doaj": {"roll": "komplement", "sprak": [], "amnen": [], "typer": [], "land": []},
    "core": {"roll": "komplement", "sprak": [], "amnen": [], "typer": [], "land": []},
}

_STANDARDPROFIL = {"roll": "komplement", "sprak": [], "amnen": [], "typer": [], "land": []}


def profil(kalla: str) -> dict:
    return PROFILER.get(kalla, _STANDARDPROFIL)


# ---------------------------------------------------------------------------
# Språk
# ---------------------------------------------------------------------------

_SV_ORD = frozenset(
    "och att det som en på är för med av till den har inte om ett de var "
    "vid kan eller från hur vad mellan inom bland under efter".split()
)
_NO_ORD = frozenset(
    "og ikke ei hvordan hva hvor hvilke også blant eldre gjennom mellom "
    "etter fra unge kvinner menn".split()
)


def gissa_sprak(q: str) -> str:
    """Grov språkgissning för källvalet: "sv", "no" eller "en".

    Räcker för att avgöra om de svenska och norska källorna ska frågas; den
    anropande assistenten kan alltid ange språket själv genom att skicka q
    som {"sv": …, "en": …}."""
    text = (q or "").lower()
    ord_ = set(re.findall(r"\w+", text))
    # ä/ö finns bara i svenska och æ/ø bara i norska (och danska); å och
    # många småord delas, så orden avgör först när bokstäverna inte gör det.
    if re.search(r"[äö]", text):
        return "sv"
    if re.search(r"[æø]", text):
        return "no"
    sv, no = len(ord_ & _SV_ORD), len(ord_ & _NO_ORD)
    if no > sv:
        return "no"
    if sv or "å" in text:
        return "sv"
    return "en"


def fraga_for_kalla(q: str | dict, kalla: str) -> str:
    """Frågan på källans språk när q är en dict per språk.

    Källor med språktyngdpunkt får sitt språk om det finns; övriga får
    engelska, annars den första varianten."""
    if isinstance(q, str):
        return q
    for sprak in profil(kalla)["sprak"]:
        if q.get(sprak):
            return q[sprak]
    return q.get("en") or next(iter(q.values()))


def fragans_sprak(q: str | dict) -> set[str]:
    if isinstance(q, dict):
        return {k for k, v in q.items() if v}
    return {gissa_sprak(q)}


# ---------------------------------------------------------------------------
# Källval
# ---------------------------------------------------------------------------

def valj_kallor(
    aktiva: list[str],
    *,
    q: str | dict,
    amne: str | None,
    typ: str | None,
    land: str | None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Kärnan för strategi="auto": ({källa: skäl}, {ej frågad källa: skäl})."""
    sprak = fragans_sprak(q)
    valda: dict[str, str] = {}
    ej: dict[str, str] = {}
    for namn in aktiva:
        p = profil(namn)
        skal = []
        if p["roll"] == "bred":
            skal.append("bred källa")
        if p["sprak"] and sprak & set(p["sprak"]):
            skal.append("språk: " + ", ".join(sorted(sprak & set(p["sprak"]))))
        if amne and amne in p["amnen"]:
            skal.append(f"ämne: {amne}")
        if typ and typ in p["typer"]:
            skal.append(f"typ: {typ}")
        if land and land.upper() in p["land"]:
            skal.append(f"land: {land.upper()}")
        if skal:
            valda[namn] = ", ".join(skal)
        elif _breddbar(p, amne, typ):
            ej[namn] = "frågas vid breddning"
        elif p["amnen"]:
            ej[namn] = "ämneskälla (" + ", ".join(p["amnen"]) + ") — ange amne eller kallor"
        else:
            ej[namn] = "typkälla (" + ", ".join(p["typer"]) + ") — ange typ eller kallor"
    return valda, ej


def _breddbar(p: dict, amne: str | None, typ: str | None) -> bool:
    """Källor som breddningen får ta till: allmänna källor och språkkällor,
    men inte ämnes- eller typkällor som inte passar frågan — en
    matematikdatabas svar på en fråga om äldres ensamhet är bara brus."""
    if p["amnen"] and amne not in p["amnen"]:
        return False
    if p["typer"] and not p["sprak"] and not p["land"] and typ not in p["typer"]:
        return False
    return True


def _termer(text: str) -> set[str]:
    return {o for o in re.findall(r"\w+", (text or "").lower())
            if len(o) > 2 and o not in _SV_ORD and o not in _NO_ORD and o not in _EN_ORD}


_EN_ORD = frozenset("the and for with from into over under between among about".split())


def ar_traffsaker(traff: dict, q: str | dict) -> bool:
    """Innehåller titel eller abstract minst hälften av frågans ord?

    Används bara för att avgöra om kärnan räcker. Crossref och andra
    källor som rangordnar på enskilda ord svarar nästan alltid med fulla
    listor, även när ingen träff handlar om frågan — att räkna dem som
    träffar skulle aldrig utlösa breddning. Ordet räknas som funnet om det
    förekommer som början av ett ord, så att böjningar (äldre/äldres) matchar."""
    varianter = q.values() if isinstance(q, dict) else [q]
    text = f"{traff.get('titel') or ''} {traff.get('sammanfattning') or ''}".lower()
    ord_i_text = re.findall(r"\w+", text)
    for variant in varianter:
        termer = _termer(variant)
        if not termer:
            return True
        funna = sum(1 for t in termer if any(o.startswith(t[:max(4, len(t) - 2)]) for o in ord_i_text))
        if funna * 2 >= len(termer):
            return True
    return False


def behover_breddning(unika: int, limit: int) -> bool:
    """Kärnan räcker om den ger minst hälften av det begärda antalet unika
    träffar — resten nås då med fortsättning i stället för fler anrop."""
    return unika < max(5, limit // 2)


# ---------------------------------------------------------------------------
# Kretsbrytare och statistik
# ---------------------------------------------------------------------------

PAUS_S = float(os.environ.get("DISCOVERY_PAUS_VID_OVERBELASTNING_S", "300"))

# En enstaka tidsgräns kan vara en tung fråga; två i rad tyder på att
# källan är överbelastad. 429 är däremot källans egen signal och pausar direkt.
_TIDSGRANSER_FORE_PAUS = 2

_las = threading.Lock()
_pausad_till: dict[str, float] = {}
_tidsgranser_i_rad: dict[str, int] = {}
_statistik: dict[str, dict] = {}


def ar_pausad(kalla: str) -> float | None:
    """Sekunder kvar av pausen, eller None om källan inte är pausad."""
    with _las:
        kvar = _pausad_till.get(kalla, 0) - time.time()
    return kvar if kvar > 0 else None


def registrera(kalla: str, *, ok: bool, tid_s: float | None = None, fel: str | None = None,
               fran_cache: bool = False, overbelastad: bool = False, tidsgrans: bool = False) -> None:
    """Uppdaterar statistiken och pausar en överbelastad källa."""
    with _las:
        if tidsgrans:
            _tidsgranser_i_rad[kalla] = _tidsgranser_i_rad.get(kalla, 0) + 1
            overbelastad = overbelastad or _tidsgranser_i_rad[kalla] >= _TIDSGRANSER_FORE_PAUS
        else:
            _tidsgranser_i_rad[kalla] = 0
        s = _statistik.setdefault(kalla, {"anrop": 0, "fran_cache": 0, "fel": 0, "pausningar": 0, "tid_s_summa": 0.0})
        s["anrop"] += 1
        if fran_cache:
            s["fran_cache"] += 1
        elif ok and tid_s is not None:
            s["tid_s_summa"] += tid_s
        if not ok:
            s["fel"] += 1
            s["senaste_fel"] = (fel or "")[:200]
        if overbelastad:
            s["pausningar"] += 1
            _pausad_till[kalla] = time.time() + PAUS_S


def ar_overbelastning(felmeddelande: str) -> bool:
    """Fel som säger att källan är överbelastad, inte att frågan var fel."""
    return bool(re.search(r"\b429\b|för hög (trafik|belastning)", felmeddelande or ""))


def statistik() -> dict[str, dict]:
    """Per källa sedan serverstart, för discovery_kallor."""
    with _las:
        ut = {}
        for kalla, s in _statistik.items():
            live = s["anrop"] - s["fran_cache"] - s["fel"]
            rad = {
                "anrop": s["anrop"],
                "fran_cache": s["fran_cache"],
                "fel": s["fel"],
                "medeltid_s": round(s["tid_s_summa"] / live, 2) if live > 0 else None,
            }
            if s.get("senaste_fel"):
                rad["senaste_fel"] = s["senaste_fel"]
            kvar = _pausad_till.get(kalla, 0) - time.time()
            if kvar > 0:
                rad["pausad_s"] = round(kvar)
            ut[kalla] = rad
        return ut


# ---------------------------------------------------------------------------
# Fortsättning (bläddring i ett sammanslaget resultat)
# ---------------------------------------------------------------------------

_FORTSATTNING_LIVSTID_S = 3600
_FORTSATTNING_MAX = 200
_fortsattningar: OrderedDict[str, tuple[float, dict]] = OrderedDict()


def spara_fortsattning(resultat: dict) -> str:
    """Sparar ett sammanslaget resultat i minnet och returnerar en token.

    Minnet räcker: en fortsättning används inom samma samtal, och
    källornas svar ligger dessutom i svarscachen om den är aktiv."""
    token = secrets.token_urlsafe(9)
    with _las:
        _fortsattningar[token] = (time.time() + _FORTSATTNING_LIVSTID_S, resultat)
        while len(_fortsattningar) > _FORTSATTNING_MAX:
            _fortsattningar.popitem(last=False)
    return token


def hamta_fortsattning(token: str) -> dict | None:
    with _las:
        post = _fortsattningar.get(token)
    if not post or post[0] < time.time():
        return None
    return post[1]
