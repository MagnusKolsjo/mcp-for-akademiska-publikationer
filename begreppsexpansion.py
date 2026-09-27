# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""Begreppsexpansion på serversidan: frågan på flera språk, med synonymer.

En fråga på svenska hittar inte forskning som publicerats på engelska,
tyska eller kinesiska om samma sak. Expansionen tar fram frågan som
etablerade fackbegrepp på de språk orkestrering.expansionssprak() väljer
(frågans språk, engelska, och fler när ämnet eller landet motiverar det),
plus ett fåtal synonymer och nyckelord.

Samma arbete kan göras av den anropande assistenten i stället (skillen
sok-vetenskapligt): då skickas q som {"sv": …, "en": …} och synonymer
direkt till discovery_sok, och servern hoppar över sin egen expansion.

Språkmodellen nås via ett OpenAI-kompatibelt chat-API (OpenAI, Anthropic,
Ollama, LM Studio, vLLM m.fl.). Konfiguration i .env:

    DISCOVERY_BEGREPPSEXPANSION_AKTIV       true för att slå på (standard: false)
    DISCOVERY_BEGREPPSEXPANSION_URL         bas-URL, t.ex. http://localhost:11434/v1
    DISCOVERY_BEGREPPSEXPANSION_MODELL      modellnamn
    DISCOVERY_BEGREPPSEXPANSION_API_NYCKEL  nyckel (Bearer), om tjänsten kräver en
    DISCOVERY_BEGREPPSEXPANSION_PROMPTFIL   promptfil (standard: prompts/begreppsexpansion.txt)
    DISCOVERY_BEGREPPSEXPANSION_TIMEOUT     sekunder (standard: 20)

Fail-open: avstängd, felkonfigurerad eller ett svar som inte går att tolka
ger None, och sökningen körs med originalfrågan. En sökning ska aldrig
falla på att expansionen inte fungerar.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

import requests

import kallkonfig

log = logging.getLogger(__name__)

_URL = os.environ.get("DISCOVERY_BEGREPPSEXPANSION_URL", "").strip().rstrip("/")
_MODELL = os.environ.get("DISCOVERY_BEGREPPSEXPANSION_MODELL", "").strip()
_API_NYCKEL = os.environ.get("DISCOVERY_BEGREPPSEXPANSION_API_NYCKEL", "").strip()
_TIMEOUT = float(os.environ.get("DISCOVERY_BEGREPPSEXPANSION_TIMEOUT", "20"))
_PROMPTFIL = os.environ.get("DISCOVERY_BEGREPPSEXPANSION_PROMPTFIL", "prompts/begreppsexpansion.txt")


def _promptfil() -> Path:
    fil = Path(_PROMPTFIL)
    return fil if fil.is_absolute() else Path(__file__).parent / fil


def status() -> dict:
    """Aktiv-status för discovery_kallor."""
    orsak = None
    if not kallkonfig.aktiv("begreppsexpansion", standard=False):
        orsak = "avstängd (DISCOVERY_BEGREPPSEXPANSION_AKTIV=false); expansionen görs då av den anropande assistenten"
    elif not _URL or not _MODELL:
        orsak = "kräver DISCOVERY_BEGREPPSEXPANSION_URL och _MODELL, som inte är satta"
    elif not _promptfil().is_file():
        orsak = f"promptfilen saknas: {_promptfil()}"
    rad = {"aktiv": orsak is None}
    if orsak:
        rad["inaktiverad_orsak"] = orsak
    else:
        rad["modell"] = _MODELL
    return rad


def aktiv() -> bool:
    return status()["aktiv"]


def _tolka(innehall: str, sprak: list[str]) -> dict | None:
    """Plockar ut JSON-objektet ur modellens svar och håller det till schemat."""
    matchning = re.search(r"\{.*\}", innehall or "", re.DOTALL)
    if not matchning:
        return None
    try:
        data = json.loads(matchning.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    def _strang(v) -> str | None:
        return " ".join(v.split()) if isinstance(v, str) and v.strip() else None

    varianter = {
        k.lower(): _strang(v) for k, v in (data.get("varianter") or {}).items()
        if k.lower() in sprak and _strang(v)
    }
    synonymer = {
        k.lower(): [_strang(x) for x in v if _strang(x)][:3]
        for k, v in (data.get("synonymer") or {}).items()
        if k.lower() in sprak and isinstance(v, list)
    }
    nyckelord = [_strang(x) for x in data.get("nyckelord") or [] if _strang(x)][:6]
    if not varianter:
        return None
    return {"varianter": varianter, "synonymer": {k: v for k, v in synonymer.items() if v}, "nyckelord": nyckelord}


def expandera(fraga: str, *, sprak: list[str], amne: str | None = None) -> dict | None:
    """{"varianter": {sprak: fråga}, "synonymer": {sprak: [...]}, "nyckelord": [...]} eller None."""
    if not aktiv():
        return None
    prompt = (
        _promptfil().read_text(encoding="utf-8")
        .replace("{fraga}", fraga)
        .replace("{amne}", amne or "")
        .replace("{sprak}", ", ".join(sprak))
    )
    headers = {"Content-Type": "application/json"}
    if _API_NYCKEL:
        headers["Authorization"] = f"Bearer {_API_NYCKEL}"
    try:
        svar = requests.post(
            f"{_URL}/chat/completions",
            headers=headers,
            json={"model": _MODELL, "messages": [{"role": "user", "content": prompt}], "temperature": 0.2},
            timeout=_TIMEOUT,
        )
        svar.raise_for_status()
        innehall = svar.json()["choices"][0]["message"]["content"]
    except Exception as exc:  # noqa: BLE001 — fail-open
        log.warning("Begreppsexpansionen misslyckades (%s) — söker med originalfrågan", exc)
        return None
    tolkat = _tolka(innehall, sprak)
    if tolkat is None:
        log.warning("Begreppsexpansionen gav ett svar som inte gick att tolka — söker med originalfrågan")
    return tolkat
