# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Arbetsbibliotek: publikationer som sparats för analys och citering.

Sökverktygen hittar och beskriver; biblioteket behåller. En sparad post får
metadata, fullständigt abstract, en färdig referens och — när en öppen
kopia finns — fulltexten, uppdelad i numrerade stycken med teckenpositioner
och sidnummer. Varje citat har därmed en stabil adress (post, stycke,
position) som går att kontrollera i efterhand, även om källans PDF flyttas.

Biblioteket fylls bara med det användaren väljer att spara, och posterna
kan märkas med projekt (en rapport, en presentation). Sökning i biblioteket
kombinerar fulltextsökning med semantisk sökning (embeddings), sammanvägda
med reciprocal rank fusion.

Konfiguration i .env:

    DISCOVERY_BIBLIOTEK_AKTIV        true/false (standard: true; kräver DATABASE_URL)
    DISCOVERY_BIBLIOTEK_SEMANTISK    true/false (standard: true)
    DISCOVERY_EMBEDDING_MODELL       standard: intfloat/multilingual-e5-base (768 dim)
    DISCOVERY_STYCKE_MAX_TECKEN      standard: 800
    DISCOVERY_STYCKE_MIN_TECKEN      standard: 100
    DISCOVERY_STYCKE_OVERLAPP_TECKEN standard: 200

Avstängt (eller utan databas) registreras inga biblioteksverktyg, och
PDF- och embeddingpaketen importeras aldrig.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

import db
import kallkonfig
import orkestrering

log = logging.getLogger(__name__)

AKTIV_ENLIGT_ENV = kallkonfig.aktiv("bibliotek")
SEMANTISK_ENLIGT_ENV = os.environ.get("DISCOVERY_BIBLIOTEK_SEMANTISK", "true").strip().lower() not in ("false", "0", "nej", "av")
EMBEDDING_MODELL = os.environ.get("DISCOVERY_EMBEDDING_MODELL", "intfloat/multilingual-e5-base")
STYCKE_MAX = int(os.environ.get("DISCOVERY_STYCKE_MAX_TECKEN", "800"))
STYCKE_MIN = int(os.environ.get("DISCOVERY_STYCKE_MIN_TECKEN", "100"))
STYCKE_OVERLAPP = int(os.environ.get("DISCOVERY_STYCKE_OVERLAPP_TECKEN", "200"))

# Över http med flera användare får texter utan öppen licens bara visas som
# utdrag: servern ska inte fungera som vidaredistribution av upphovsrättsligt
# skyddade texter. Lokalt (stdio) är biblioteket användarens eget.
_DELAD_DRIFT = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower() == "http"
_UTDRAG_TECKEN = 500


class BibliotekFel(Exception):
    """Förväntade fel i biblioteket (okänd post, avstängd funktion)."""


# ---------------------------------------------------------------------------
# Start och status
# ---------------------------------------------------------------------------

_start_las = threading.Lock()
_startad = False
_avstangd_orsak: str | None = None
_semantisk_orsak: str | None = None


def konfigurerat() -> bool:
    """Ska biblioteksverktygen registreras? (Avgörs vid serverstart.)"""
    return AKTIV_ENLIGT_ENV and db.konfigurerad()


def _starta() -> None:
    global _startad, _avstangd_orsak, _semantisk_orsak
    if _startad:
        return
    with _start_las:
        if _startad:
            return
        if not AKTIV_ENLIGT_ENV:
            _avstangd_orsak = "avstängt med DISCOVERY_BIBLIOTEK_AKTIV=false"
        elif not db.konfigurerad():
            _avstangd_orsak = "kräver DATABASE_URL (postgresql://… eller sqlite:///…), som inte är satt"
        else:
            try:
                db.initiera_schema()
            except Exception as exc:
                _avstangd_orsak = f"databasen gick inte att initiera: {type(exc).__name__}: {' '.join(str(exc).split())}"
        if _avstangd_orsak is None:
            if not SEMANTISK_ENLIGT_ENV:
                _semantisk_orsak = "avstängd med DISCOVERY_BIBLIOTEK_SEMANTISK=false"
            else:
                try:
                    import sentence_transformers  # noqa: F401 — finns paketet?
                    db.initiera_vektor()
                except Exception as exc:
                    _semantisk_orsak = (
                        f"{type(exc).__name__}: {' '.join(str(exc).split())[:200]} — kräver "
                        "sentence-transformers och pgvector (Postgres) eller sqlite-vec (SQLite)"
                    )
                    log.warning("Bibliotekets semantiska sökning avstängd: %s", _semantisk_orsak)
        _startad = True


def _kontrollera() -> None:
    _starta()
    if _avstangd_orsak:
        raise BibliotekFel(f"Biblioteket är inte tillgängligt: {_avstangd_orsak}.")


def semantisk() -> bool:
    _starta()
    return _avstangd_orsak is None and _semantisk_orsak is None


def status() -> dict:
    _starta()
    rad: dict = {"aktiv": _avstangd_orsak is None}
    if _avstangd_orsak:
        rad["inaktiverad_orsak"] = _avstangd_orsak
        return rad
    rad["semantisk_sokning"] = _semantisk_orsak is None
    if _semantisk_orsak:
        rad["semantisk_orsak"] = _semantisk_orsak
    else:
        rad["embedding_modell"] = EMBEDDING_MODELL
    rad["delad_drift"] = _DELAD_DRIFT
    return rad


# ---------------------------------------------------------------------------
# Stycken och embeddings
# ---------------------------------------------------------------------------

def dela_i_stycken(text: str, sidor: list[int] | None = None) -> list[dict]:
    """Överlappande stycken med teckenpositioner och sidnummer.

    Bryter helst vid styckegräns, annars vid meningsslut, annars vid
    STYCKE_MAX. Överlappet gör att en mening vid en gräns finns hel i
    minst ett stycke."""
    ut: list[dict] = []
    n = len(text)
    start = 0
    while start < n:
        slut = min(start + STYCKE_MAX, n)
        if slut < n:
            for skiljetecken in ("\n\n", ". ", "? ", "! ", ".\n", "; "):
                pos = text.rfind(skiljetecken, start + STYCKE_MIN, slut)
                if pos != -1:
                    slut = pos + len(skiljetecken)
                    break
        bit = text[start:slut]
        forsta = len(bit) - len(bit.lstrip())
        bit = bit.strip()
        if len(bit) >= STYCKE_MIN or (bit and slut >= n):
            borjan = start + forsta
            sida = None
            if sidor:
                sida = max(i for i, s in enumerate(sidor) if s <= borjan) + 1
            ut.append({"nr": len(ut) + 1, "text": bit, "tecken_start": borjan,
                       "tecken_slut": borjan + len(bit), "sida": sida})
        if slut >= n:
            break
        # Överlappet får aldrig vara större än halva stycket — annars kryper
        # indelningen fram några tecken i taget — och nästa stycke börjar
        # vid en ordgräns, inte mitt i ett ord.
        nasta = slut - min(STYCKE_OVERLAPP, (slut - start) // 2)
        mellanslag = text.find(" ", nasta, slut)
        start = mellanslag + 1 if mellanslag != -1 else slut
    return ut


_modell = None
_modell_las = threading.Lock()


def _embedda(texter: list[str], *, fraga: bool) -> list[list[float]]:
    global _modell
    with _modell_las:
        if _modell is None:
            from sentence_transformers import SentenceTransformer
            # Modellnedladdningens HTTP-loggning dränker annars serverns egen.
            logging.getLogger("httpx").setLevel(logging.WARNING)
            log.info("Laddar embeddingmodell %s", EMBEDDING_MODELL)
            _modell = SentenceTransformer(EMBEDDING_MODELL)
    # E5-modellerna är tränade med prefixen "query: " och "passage: ".
    if "e5" in EMBEDDING_MODELL.lower():
        texter = [("query: " if fraga else "passage: ") + t for t in texter]
    return _modell.encode(texter, batch_size=32, normalize_embeddings=True).tolist()


def _vektor_param(vektor: list[float]):
    if db.ar_postgres():
        return "[" + ",".join(f"{x:.6f}" for x in vektor) + "]"
    import sqlite_vec
    return sqlite_vec.serialize_float32(vektor)


# ---------------------------------------------------------------------------
# Spara
# ---------------------------------------------------------------------------

def har_fulltext(pid: str) -> bool:
    """Har posten redan en sparad fulltext? (Då hämtas den inte om.)"""
    _kontrollera()
    with db.hamta_db() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT fulltext_status FROM {db.prefix()}bibliotek_poster WHERE id = {db.ph()}", (pid,))
        rad = cur.fetchone()
    return rad is not None and rad[0] == "hämtad"


def post_id(post: dict) -> str:
    return f"doi:{post['doi']}" if post.get("doi") else f"{post['kalla']}:{post['kalla_id']}"


def oppen_licens(licens: str | None) -> bool:
    l = (licens or "").lower().replace(" ", "-")
    return l.startswith("cc") or "public-domain" in l or l == "pd"


def spara(post: dict, *, projekt: str | None, referens: str | None, fulltext_resultat) -> dict:
    """Sparar eller uppdaterar en post. fulltext_resultat är (Fulltext|None, [försök])
    eller None om fulltext inte ska hämtas."""
    _kontrollera()
    pid = post_id(post)
    nu = time.time()
    p, pre = db.ph(), db.prefix()
    ft, forsok = fulltext_resultat if fulltext_resultat is not None else (None, [])

    if ft is not None:
        status_ = "hämtad"
    elif fulltext_resultat is None:
        status_ = "ej begärd"
    else:
        status_ = "ingen öppen fulltext hittades"
    stycken = dela_i_stycken(ft.text, ft.sidor) if ft else []

    with db.hamta_db(vektor=semantisk()) as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT fulltext_status FROM {pre}bibliotek_poster WHERE id = {p}", (pid,))
        befintlig = cur.fetchone()
        behall_fulltext = befintlig is not None and befintlig[0] == "hämtad" and ft is None
        if behall_fulltext:
            status_ = "hämtad"

        falt = {
            "kalla": post.get("kalla"), "kalla_id": post.get("kalla_id"), "doi": post.get("doi"),
            "titel": post.get("titel"), "forfattare": json.dumps(post.get("forfattare") or [], ensure_ascii=False),
            "ar": post.get("ar"), "typ": post.get("typ"), "url": post.get("url"), "sprak": post.get("sprak"),
            "sammanfattning": post.get("sammanfattning"), "referens": referens,
            "fulltext_status": status_, "uppdaterad": nu,
        }
        if not behall_fulltext:
            falt.update({
                "fulltext": ft.text if ft else None,
                "fulltext_url": ft.url if ft else None,
                "fulltext_metod": ft.metod if ft else None,
                "licens": ft.licens if ft else None,
                "sidor": json.dumps(ft.sidor) if ft and ft.sidor else None,
                "tecken_totalt": len(ft.text) if ft else 0,
            })
        if befintlig is None:
            falt["sparad"] = nu
            kolumner = ["id", *falt]
            cur.execute(
                f"INSERT INTO {pre}bibliotek_poster ({', '.join(kolumner)}) VALUES ({', '.join([p] * len(kolumner))})",
                (pid, *falt.values()),
            )
        else:
            cur.execute(
                f"UPDATE {pre}bibliotek_poster SET {', '.join(f'{k} = {p}' for k in falt)} WHERE id = {p}",
                (*falt.values(), pid),
            )

        if not behall_fulltext:
            if not db.ar_postgres() and semantisk():
                # vec0 följer inte främmande nycklar; rensa vektorerna först.
                cur.execute(
                    f"DELETE FROM bibliotek_embeddings WHERE stycke_id IN "
                    f"(SELECT id FROM bibliotek_stycken WHERE post_id = {p})", (pid,))
            cur.execute(f"DELETE FROM {pre}bibliotek_stycken WHERE post_id = {p}", (pid,))
            stycke_ids = []
            for s in stycken:
                if db.ar_postgres():
                    cur.execute(
                        f"INSERT INTO {pre}bibliotek_stycken (post_id, nr, text, tecken_start, tecken_slut, sida) "
                        f"VALUES ({p}, {p}, {p}, {p}, {p}, {p}) RETURNING id",
                        (pid, s["nr"], s["text"], s["tecken_start"], s["tecken_slut"], s["sida"]))
                    stycke_ids.append(cur.fetchone()[0])
                else:
                    cur.execute(
                        "INSERT INTO bibliotek_stycken (post_id, nr, text, tecken_start, tecken_slut, sida) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (pid, s["nr"], s["text"], s["tecken_start"], s["tecken_slut"], s["sida"]))
                    stycke_ids.append(cur.lastrowid)
            if stycken and semantisk():
                vektorer = _embedda([s["text"] for s in stycken], fraga=False)
                for sid, v in zip(stycke_ids, vektorer):
                    cur.execute(
                        f"INSERT INTO {pre}bibliotek_embeddings (stycke_id, vektor) VALUES ({p}, {p}"
                        f"{'::vector' if db.ar_postgres() else ''})",
                        (sid, _vektor_param(v)))

        if projekt:
            konflikt = "ON CONFLICT (projekt, post_id) DO NOTHING"
            cur.execute(
                f"INSERT INTO {pre}bibliotek_projekt (projekt, post_id, tillagd) VALUES ({p}, {p}, {p}) {konflikt}",
                (projekt, pid, nu))

        cur.execute(f"SELECT tecken_totalt, licens, fulltext_url FROM {pre}bibliotek_poster WHERE id = {p}", (pid,))
        tecken, licens, ft_url = cur.fetchone()
        cur.execute(f"SELECT count(*) FROM {pre}bibliotek_stycken WHERE post_id = {p}", (pid,))
        antal_stycken = cur.fetchone()[0]

    ut = {
        "id": pid, "titel": post.get("titel"), "ny": befintlig is None, "projekt": projekt,
        "fulltext_status": status_, "fulltext_url": ft_url, "licens": licens,
        "oppen_licens": oppen_licens(licens), "tecken_totalt": tecken, "stycken": antal_stycken,
        "referens": referens,
    }
    if ft is not None:
        ut["fulltext_metod"] = ft.metod
        if ft.ocr_sidor:
            ut["ocr_sidor"] = ft.ocr_sidor
            ut["ocr_anmarkning"] = ("Texten på dessa sidor är maskinläst (OCR) och kan innehålla "
                                    "felläsningar; kontrollera citat mot originalet.")
        if ft.ej_ocr_sidor:
            ut["sidor_utan_text"] = ft.ej_ocr_sidor
    if status_ != "hämtad" and forsok:
        ut["fulltext_forsok"] = forsok[:6]
    return ut


# ---------------------------------------------------------------------------
# Sök
# ---------------------------------------------------------------------------

_KOLUMNER = "s.id, s.post_id, s.nr, s.text, s.tecken_start, s.tecken_slut, s.sida"


def _projektvillkor(projekt: str | None) -> tuple[str, tuple]:
    if not projekt:
        return "", ()
    return (f" AND s.post_id IN (SELECT post_id FROM {db.prefix()}bibliotek_projekt WHERE projekt = {db.ph()})",
            (projekt,))


def _sokord(fraga: str) -> list[str]:
    return [o for o in orkestrering._termer(fraga)] or re.findall(r"\w+", fraga.lower())


def _fts(cur, fraga: str, projekt: str | None, antal: int) -> list[tuple]:
    ord_ = _sokord(fraga)
    if not ord_:
        return []
    villkor, param = _projektvillkor(projekt)
    if db.ar_postgres():
        # OR mellan orden; ts_rank belönar stycken som har fler av dem.
        cur.execute(
            f"SELECT {_KOLUMNER} FROM discovery.bibliotek_stycken s, to_tsquery('simple', %s) q "
            f"WHERE s.text_tsv @@ q{villkor} ORDER BY ts_rank(s.text_tsv, q) DESC LIMIT %s",
            (" | ".join(re.sub(r"\W", "", o) + ":*" for o in ord_), *param, antal))
    else:
        match = " OR ".join('"' + o.replace('"', "") + '"*' for o in ord_)
        cur.execute(
            f"SELECT {_KOLUMNER} FROM bibliotek_stycken_fts f JOIN bibliotek_stycken s ON s.id = f.rowid "
            f"WHERE bibliotek_stycken_fts MATCH ?{villkor} ORDER BY bm25(bibliotek_stycken_fts) LIMIT ?",
            (match, *param, antal))
    return cur.fetchall()


def _semantiskt(cur, fraga: str, projekt: str | None, antal: int) -> list[tuple]:
    vektor = _vektor_param(_embedda([fraga], fraga=True)[0])
    villkor, param = _projektvillkor(projekt)
    if db.ar_postgres():
        cur.execute(
            f"SELECT {_KOLUMNER} FROM discovery.bibliotek_embeddings e "
            f"JOIN discovery.bibliotek_stycken s ON s.id = e.stycke_id WHERE TRUE{villkor} "
            f"ORDER BY e.vektor <=> %s::vector LIMIT %s",
            (*param, vektor, antal))
        return cur.fetchall()
    # vec0 gör k-närmaste-sökningen före projektfiltret; hämta fler och filtrera.
    k = antal * (10 if projekt else 1)
    cur.execute(
        f"SELECT {_KOLUMNER} FROM (SELECT stycke_id, distance FROM bibliotek_embeddings "
        f"WHERE vektor MATCH ? AND k = ?) e JOIN bibliotek_stycken s ON s.id = e.stycke_id "
        f"WHERE 1{villkor} ORDER BY e.distance LIMIT ?",
        (vektor, k, *param, antal))
    return cur.fetchall()


def sok(fraga: str, *, projekt: str | None = None, lage: str = "hybrid", limit: int = 10) -> dict:
    """Söker bland de sparade styckena: "hybrid", "fulltext" eller "semantisk"."""
    _kontrollera()
    if lage not in ("hybrid", "fulltext", "semantisk"):
        raise BibliotekFel(f"Okänt läge '{lage}'. Välj hybrid, fulltext eller semantisk.")
    if lage == "semantisk" and not semantisk():
        raise BibliotekFel(f"Semantisk sökning är avstängd: {_semantisk_orsak}.")
    kandidater = max(30, limit * 3)
    listor: dict[str, list[tuple]] = {}
    with db.hamta_db(vektor=semantisk()) as conn:
        cur = conn.cursor()
        if lage in ("hybrid", "fulltext"):
            listor["fulltext"] = _fts(cur, fraga, projekt, kandidater)
        if lage in ("hybrid", "semantisk") and semantisk():
            listor["semantisk"] = _semantiskt(cur, fraga, projekt, kandidater)

        poang: dict[int, float] = {}
        rader: dict[int, tuple] = {}
        traffad_av: dict[int, list[str]] = {}
        for namn, lista in listor.items():
            for plats, rad in enumerate(lista, start=1):
                poang[rad[0]] = poang.get(rad[0], 0) + 1 / (60 + plats)
                rader[rad[0]] = rad
                traffad_av.setdefault(rad[0], []).append(namn)
        ordning = sorted(poang, key=poang.get, reverse=True)[:limit]

        post_ids = sorted({rader[i][1] for i in ordning})
        poster = {}
        if post_ids:
            cur.execute(
                f"SELECT id, titel, forfattare, ar, sprak, licens, referens FROM {db.prefix()}bibliotek_poster "
                f"WHERE id IN ({', '.join([db.ph()] * len(post_ids))})", post_ids)
            poster = {r[0]: r for r in cur.fetchall()}

    traffar = []
    for sid in ordning:
        _, pid, nr, text, t0, t1, sida = rader[sid]
        post = poster.get(pid)
        forfattare = json.loads(post[2] or "[]") if post else []
        text_ut, begransad = _visa(text, post[5] if post else None)
        traffar.append({
            "post_id": pid, "titel": post[1] if post else None,
            "forfattare": [f.get("namn") for f in forfattare[:3]], "ar": post[3] if post else None,
            "sprak": post[4] if post else None, "stycke": nr, "sida": sida,
            "tecken_start": t0, "tecken_slut": t1, "text": text_ut, "begransad": begransad,
            "traffad_av": traffad_av[sid], "referens": post[6] if post else None,
        })
    return {"fraga": fraga, "lage": lage, "semantisk_sokning": semantisk(), "antal": len(traffar), "traffar": traffar}


# ---------------------------------------------------------------------------
# Läs
# ---------------------------------------------------------------------------

def _visa(text: str, licens: str | None) -> tuple[str, bool]:
    if _DELAD_DRIFT and not oppen_licens(licens) and len(text) > _UTDRAG_TECKEN:
        return text[:_UTDRAG_TECKEN].rsplit(" ", 1)[0] + "…", True
    return text, False


def las(pid: str, *, stycke: int | None = None, kontext: int = 0,
        fran_tecken: int = 0, max_tecken: int = 8000) -> dict:
    """Läser en sparad post: ett stycke med omgivning, eller fulltexten från en position."""
    _kontrollera()
    p, pre = db.ph(), db.prefix()
    with db.hamta_db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT titel, forfattare, ar, sprak, licens, referens, fulltext, fulltext_status, "
            f"tecken_totalt, sidor, sammanfattning, doi, url, fulltext_url, fulltext_metod "
            f"FROM {pre}bibliotek_poster WHERE id = {p}",
            (pid,))
        rad = cur.fetchone()
        if rad is None:
            raise BibliotekFel(f"Posten '{pid}' finns inte i biblioteket. Se discovery_lista_bibliotek.")
        (titel, forfattare, ar, sprak, licens, referens, fulltext, ft_status,
         tecken_totalt, sidor, sammanfattning, doi, url, ft_url, ft_metod) = rad
        ut = {
            "post_id": pid, "titel": titel, "ar": ar, "sprak": sprak, "licens": licens,
            "referens": referens, "fulltext_status": ft_status, "tecken_totalt": tecken_totalt,
            "doi": doi, "url": url, "fulltext_url": ft_url, "fulltext_metod": ft_metod,
        }
        if stycke is not None:
            cur.execute(
                f"SELECT nr, text, tecken_start, tecken_slut, sida FROM {pre}bibliotek_stycken "
                f"WHERE post_id = {p} AND nr BETWEEN {p} AND {p} ORDER BY nr",
                (pid, stycke - kontext, stycke + kontext))
            rader = cur.fetchall()
            if not rader:
                raise BibliotekFel(f"Posten '{pid}' har inget stycke {stycke}.")
            # Stycken överlappar; läs sammanhängande text ur fulltexten i
            # stället för att skarva styckena, så att inget upprepas.
            t0, t1 = rader[0][2], rader[-1][3]
            text, begransad = _visa(fulltext[t0:t1], licens)
            ut.update({"stycken": [r[0] for r in rader], "sida": rader[0][4], "tecken_start": t0,
                       "tecken_slut": t1, "text": text, "begransad": begransad})
            return ut

    if not fulltext:
        ut["sammanfattning"] = sammanfattning
        ut["meddelande"] = "Posten saknar sparad fulltext; sammanfattningen visas."
        return ut
    fran = max(0, fran_tecken)
    till = min(len(fulltext), fran + max(1, max_tecken))
    text, begransad = _visa(fulltext[fran:till], licens)
    ut.update({"tecken_start": fran, "tecken_slut": till, "text": text, "begransad": begransad,
               "trunkerad": till < len(fulltext)})
    if till < len(fulltext):
        ut["fortsatt_fran_tecken"] = till
    if sidor:
        sid = json.loads(sidor)
        ut["sida"] = max(i for i, s in enumerate(sid) if s <= fran) + 1
    return ut


# ---------------------------------------------------------------------------
# Lista och ta bort
# ---------------------------------------------------------------------------

def lista(projekt: str | None = None) -> dict:
    _kontrollera()
    p, pre = db.ph(), db.prefix()
    with db.hamta_db() as conn:
        cur = conn.cursor()
        villkor = f"WHERE id IN (SELECT post_id FROM {pre}bibliotek_projekt WHERE projekt = {p})" if projekt else ""
        cur.execute(
            f"SELECT id, titel, forfattare, ar, sprak, fulltext_status, licens, tecken_totalt, referens "
            f"FROM {pre}bibliotek_poster {villkor} ORDER BY sparad DESC", (projekt,) if projekt else ())
        poster = cur.fetchall()
        cur.execute(f"SELECT projekt, post_id FROM {pre}bibliotek_projekt")
        koppling = cur.fetchall()
    projekt_per_post: dict[str, list[str]] = {}
    antal_per_projekt: dict[str, int] = {}
    for proj, pid in koppling:
        projekt_per_post.setdefault(pid, []).append(proj)
        antal_per_projekt[proj] = antal_per_projekt.get(proj, 0) + 1
    return {
        "projekt": antal_per_projekt,
        "antal": len(poster),
        "poster": [{
            "post_id": r[0], "titel": r[1], "forfattare": [f.get("namn") for f in json.loads(r[2] or "[]")[:3]],
            "ar": r[3], "sprak": r[4], "fulltext_status": r[5], "licens": r[6], "tecken_totalt": r[7],
            "referens": r[8], "projekt": sorted(projekt_per_post.get(r[0], [])),
        } for r in poster],
    }


def ta_bort(pid: str, *, projekt: str | None = None) -> dict:
    """Tar bort posten ur ett projekt, eller helt ur biblioteket om projekt utelämnas."""
    _kontrollera()
    p, pre = db.ph(), db.prefix()
    with db.hamta_db(vektor=semantisk()) as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT 1 FROM {pre}bibliotek_poster WHERE id = {p}", (pid,))
        if cur.fetchone() is None:
            raise BibliotekFel(f"Posten '{pid}' finns inte i biblioteket.")
        if projekt:
            cur.execute(f"DELETE FROM {pre}bibliotek_projekt WHERE projekt = {p} AND post_id = {p}", (projekt, pid))
            return {"post_id": pid, "borttagen_ur_projekt": projekt, "posten_finns_kvar": True}
        if not db.ar_postgres() and semantisk():
            cur.execute(
                "DELETE FROM bibliotek_embeddings WHERE stycke_id IN "
                "(SELECT id FROM bibliotek_stycken WHERE post_id = ?)", (pid,))
        cur.execute(f"DELETE FROM {pre}bibliotek_poster WHERE id = {p}", (pid,))
    return {"post_id": pid, "borttagen": True}
