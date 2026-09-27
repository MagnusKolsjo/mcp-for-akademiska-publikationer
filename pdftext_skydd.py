# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö
"""PDF till markdown med skydd mot minnesslut, rätt OCR-språk och en OCR-kö.

pymupdf4llm kör en layoutmodell och OCR:ar sidor som saknar textlager.
Ett bildtungt dokument kan då kräva tiotals GB minne, eftersom hela
dokumentets analys hålls i minnet. Därför:

1. Extraktionen körs i en egen process, i block om några sidor. En vakt
   i föräldraprocessen läser processens minne (RSS) och avbryter den om
   den passerar gränsen eller tidsgränsen. Då kan ett enskilt dokument
   aldrig fälla datorn eller servern.
2. OCR-språket anges uttryckligen. pymupdf4llm använder annars engelska,
   och svenska, danska, norska och isländska tecken blir fel.
3. Ett block som inte klarar gränserna läses med ren textutvinning
   (utan layout och OCR), så att dokumentet ändå blir sökbart.
4. Dokument där sidor saknar textlager, eller där ett block fick läsas
   med ren textutvinning, noteras i en OCR-kö och PDF:en sparas lokalt.
   Då kan de köras genom en bättre OCR senare utan att laddas ned igen.

Miljövariabler (prefixet sätts av anroparen, t.ex. "GOV" → GOV_OCR_SPRAK):
    <PREFIX>_OCR_SPRAK        Tesseract-språk, t.ex. "swe+eng" (standard: anroparens)
    <PREFIX>_PDF_MAX_MINNE_MB minnesgräns per extraktion (standard 3000)
    <PREFIX>_PDF_TIDSGRANS_S  tidsgräns per sidblock i sekunder (standard 300)
    <PREFIX>_PDF_SIDBLOCK     antal sidor per block (standard 20)
    <PREFIX>_OCR_KO_MAPP      mapp för OCR-kön (standard: ocr_ko/ bredvid modulen)

Ingångspunkt:
    extrahera_pdf(pdf, *, prefix, standardsprak, kalla_id, kalla_url="",
                  sidor=None) -> PdfResultat

Barnprocesserna startas med "spawn", som läser in anroparens huvudmodul på
nytt. Kod på översta nivån i ett anropande skript måste därför ligga under
`if __name__ == "__main__":`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import multiprocessing as mp
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_MAPP = Path(__file__).resolve().parent


@dataclass
class PdfResultat:
    """Resultatet av en extraktion.

    metod: "text" (bara textlager), "ocr" (minst en sida OCR:ad),
           "reserv" (minst ett block lästes med ren textutvinning)
    """
    text: str
    metod: str
    sidor: int
    sidor_utan_textlager: list[int] = field(default_factory=list)
    block_med_reserv: list[tuple[int, int]] = field(default_factory=list)
    i_ocr_ko: bool = False
    orsak: str = ""


def _env(prefix: str, namn: str, standard: str) -> str:
    return os.environ.get(f"{prefix}_{namn}", "").strip() or standard


def _rss_mb(pid: int) -> float:
    """Processens residenta minne i MB, läst med ps (fungerar på macOS och Linux)."""
    try:
        ut = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                            capture_output=True, text=True, timeout=5).stdout.strip()
        return int(ut) / 1024 if ut else 0.0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def _block_arbetare(sokvag: str, sidor: list[int], sprak: str, ko: mp.Queue) -> None:
    """Körs i en egen process: layout + OCR för ett block sidor."""
    # Processen ärver förälderns fd 1 och 2. pymupdf4llm och Tesseract skriver
    # statusrader dit, och i en MCP-server över stdio är fd 1 protokollkanalen.
    # Barnprocessens egna utskrifter skickas därför till /dev/null; resultatet
    # går tillbaka via kön.
    tom = os.open(os.devnull, os.O_WRONLY)
    os.dup2(tom, 1)
    os.dup2(tom, 2)
    os.close(tom)
    try:
        import pymupdf  # noqa: F401  (laddar biblioteket i barnprocessen)
        import pymupdf4llm
        md = pymupdf4llm.to_markdown(sokvag, pages=sidor, ocr_language=sprak,
                                     show_progress=False)
        ko.put(("ok", md))
    except Exception as fel:  # noqa: BLE001 – rapporteras till föräldern
        ko.put(("fel", f"{type(fel).__name__}: {fel}"))


def _kor_block(sokvag: str, sidor: list[int], sprak: str,
               max_minne_mb: float, tidsgrans_s: float) -> tuple[str | None, str]:
    """Kör ett block i en egen process under minnes- och tidsvakt."""
    ctx = mp.get_context("spawn")
    ko: mp.Queue = ctx.Queue()
    proc = ctx.Process(target=_block_arbetare, args=(sokvag, sidor, sprak, ko), daemon=True)
    proc.start()
    start = time.monotonic()
    orsak = ""
    try:
        while True:
            try:
                status, varde = ko.get(timeout=0.5)
                proc.join(timeout=10)
                return (varde, "") if status == "ok" else (None, varde)
            except Exception:  # noqa: BLE001 – kön är tom, fortsätt vakta
                pass
            if not proc.is_alive():
                return None, f"processen avslutades (kod {proc.exitcode})"
            if _rss_mb(proc.pid) > max_minne_mb:
                orsak = f"minnesgränsen {max_minne_mb:.0f} MB passerades"
                break
            if time.monotonic() - start > tidsgrans_s:
                orsak = f"tidsgränsen {tidsgrans_s:.0f} s passerades"
                break
    finally:
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=10)
    return None, orsak


def _ren_text(sokvag: str, sidor: list[int]) -> str:
    """Ren textutvinning utan layout och OCR. Snål med minne."""
    import pymupdf
    delar = []
    with pymupdf.open(sokvag) as dok:
        for i in sidor:
            delar.append(dok[i].get_text("text"))
    return "\n\n".join(delar)


def _sidor_utan_textlager(sokvag: str, sidlista: list[int] | None = None) -> tuple[int, list[int]]:
    """Sidor utan textlager. Med sidlista begränsas kontrollen till dessa sidor."""
    import pymupdf
    utan = []
    urval = set(sidlista) if sidlista is not None else None
    with pymupdf.open(sokvag) as dok:
        antal = dok.page_count
        for i, sida in enumerate(dok):
            if urval is not None and i not in urval:
                continue
            if not sida.get_text("text").strip() and sida.get_images(full=False):
                utan.append(i)
    return antal, utan


def _lagg_i_ocr_ko(prefix: str, sokvag: str, kalla_id: str, kalla_url: str,
                   res: PdfResultat, sprak: str) -> None:
    mapp = Path(_env(prefix, "OCR_KO_MAPP", str(_MAPP / "ocr_ko")))
    (mapp / "filer").mkdir(parents=True, exist_ok=True)
    data = Path(sokvag).read_bytes()
    namn = hashlib.sha256(data).hexdigest()[:20] + ".pdf"
    mal = mapp / "filer" / namn
    if not mal.exists():
        shutil.copyfile(sokvag, mal)
    post = {
        "tid": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "kalla_id": kalla_id,
        "kalla_url": kalla_url,
        "fil": f"filer/{namn}",
        "storlek_bytes": len(data),
        "sidor": res.sidor,
        "sidor_utan_textlager": res.sidor_utan_textlager,
        "block_med_reserv": res.block_med_reserv,
        "metod": res.metod,
        "ocr_sprak": sprak,
        "orsak": res.orsak,
    }
    with open(mapp / "ko.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(post, ensure_ascii=False) + "\n")


def extrahera_pdf(pdf: bytes | str | Path, *, prefix: str, standardsprak: str,
                  kalla_id: str, kalla_url: str = "",
                  sidor: list[int] | None = None) -> PdfResultat:
    """Extraherar en PDF till markdown under minnes- och tidsvakt.

    pdf: PDF:ens innehåll eller en sökväg till filen.
    prefix: miljövariablernas prefix för servern, t.ex. "GOV".
    standardsprak: Tesseract-språk om <PREFIX>_OCR_SPRAK saknas, t.ex. "swe+eng".
    kalla_id, kalla_url: identifierar dokumentet i OCR-kön.
    sidor: valfri lista med sidnummer att begränsa extraktionen till, 0-indexerat
        (samma indexering som pymupdf/pymupdf4llm, dvs. sida 1 i dokumentet är
        index 0). Utelämna (None) för hela dokumentet. Sidor utanför dokumentets
        längd ignoreras tyst. `PdfResultat.sidor` anger ändå dokumentets totala
        sidantal, inte antalet begärda sidor — `sidor_utan_textlager` och
        `block_med_reserv` avser bara de begärda sidorna.
    """
    sprak = _env(prefix, "OCR_SPRAK", standardsprak)
    max_minne = float(_env(prefix, "PDF_MAX_MINNE_MB", "3000"))
    tidsgrans = float(_env(prefix, "PDF_TIDSGRANS_S", "300"))
    blockstorlek = max(1, int(_env(prefix, "PDF_SIDBLOCK", "20")))

    with tempfile.TemporaryDirectory(prefix="pdftext_") as tmp:
        if isinstance(pdf, (bytes, bytearray)):
            sokvag = str(Path(tmp) / "dok.pdf")
            Path(sokvag).write_bytes(pdf)
        else:
            sokvag = str(pdf)

        antal, utan = _sidor_utan_textlager(sokvag, sidor)
        if sidor is not None:
            sidlista = sorted({i for i in dict.fromkeys(sidor) if 0 <= i < antal})
        else:
            sidlista = list(range(antal))

        delar: list[str] = []
        reserv: list[tuple[int, int]] = []
        orsaker: list[str] = []
        for start in range(0, len(sidlista), blockstorlek):
            block = sidlista[start:start + blockstorlek]
            md, orsak = _kor_block(sokvag, block, sprak, max_minne, tidsgrans)
            if md is None:
                logger.warning("Sidorna %d–%d i %s lästes med ren textutvinning: %s",
                               block[0] + 1, block[-1] + 1, kalla_id, orsak)
                md = _ren_text(sokvag, block)
                reserv.append((block[0] + 1, block[-1] + 1))
                orsaker.append(orsak)
            delar.append(md)

        metod = "reserv" if reserv else ("ocr" if utan else "text")
        res = PdfResultat(
            text="\n\n".join(d for d in delar if d),
            metod=metod,
            sidor=antal,
            sidor_utan_textlager=[i + 1 for i in utan],
            block_med_reserv=reserv,
            orsak="; ".join(dict.fromkeys(orsaker)),
        )
        if utan or reserv:
            try:
                _lagg_i_ocr_ko(prefix, sokvag, kalla_id, kalla_url, res, sprak)
                res.i_ocr_ko = True
            except OSError as fel:
                logger.warning("Kunde inte lägga %s i OCR-kön: %s", kalla_id, fel)
        return res


def som_dict(res: PdfResultat) -> dict:
    return asdict(res)
