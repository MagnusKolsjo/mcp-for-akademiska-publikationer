"""Provider-register och enad sökning för discovery-hubben.

Det här är inkopplingssömmen. Varje DOI-källa (Crossref, DataCite, ...) är en
självständig klientmodul som exponerar samma sök-yta: en sok()-funktion som
returnerar normaliserade träffar och en egen felklass. Registret nedan binder
ihop dem så att discovery_sok kan slå mot flera källor i ett anrop.

Lägga till en ny discovery-källa:
  1. Skriv <namn>_client.py med sok(q, *, limit, fran_ar, till_ar) som
     returnerar {"traffar": [...]} med normaliserade träffar (se formen i
     crossref_client._forma_traff) och en <Namn>Fel-klass.
  2. Lägg till en rad i PROVIDERS nedan.
  3. Exponera käll-specifika verktyg i mcp_server.py om källan har egna filter.

Biblioteks-/katalogkällor med en annan träffform (som Libris JSON-LD) ingår
inte i den enade DOI-sökningen utan har sina egna verktyg.
"""

import crossref_client
import datacite_client

# Registret över DOI-källor som ingår i den enade sökningen. Nyckeln är det
# källnamn som används i discovery_sok(kallor=[...]) och som taggas på varje
# träff. "sok" tar (q, limit, fran_ar, till_ar) och returnerar källans
# sok()-dict; "fel" är källans felklass för riktad felhantering.
PROVIDERS: dict[str, dict] = {
    "crossref": {
        "etikett": "Crossref",
        "beskrivning": "DOI:er för vetenskapliga artiklar, böcker och konferensbidrag.",
        "sok": lambda q, limit, fran_ar, till_ar: crossref_client.sok(
            q, limit=limit, fran_ar=fran_ar, till_ar=till_ar
        ),
        "fel": crossref_client.CrossrefFel,
    },
    "datacite": {
        "etikett": "DataCite",
        "beskrivning": "DOI:er för forskningsdata, programvara och preprints.",
        "sok": lambda q, limit, fran_ar, till_ar: datacite_client.sok(
            q, limit=limit, fran_ar=fran_ar, till_ar=till_ar
        ),
        "fel": datacite_client.DataCiteFel,
    },
}


def lista_kallor() -> dict:
    """Returnerar de discovery-källor som ingår i den enade sökningen."""
    return {
        "kallor": [
            {"namn": namn, "etikett": p["etikett"], "beskrivning": p["beskrivning"]}
            for namn, p in PROVIDERS.items()
        ]
    }


def sok_alla(
    q: str,
    *,
    kallor: list[str] | None = None,
    limit_per_kalla: int = 10,
    fran_ar: int | None = None,
    till_ar: int | None = None,
) -> dict:
    """Slår mot flera DOI-källor och slår ihop de normaliserade träffarna.

    q               - fritextfråga som skickas till varje vald källa.
    kallor          - lista med källnamn (se lista_kallor). Utelämnad = alla.
    limit_per_kalla - max antal träffar per källa innan sammanslagning.
    fran_ar/till_ar - utgivningsårsintervall som skickas till varje källa.

    En källa som fallerar stoppar inte de andra — dess fel rapporteras under
    "fel" medan övriga källors träffar ändå returneras. Det sammanslagna
    resultatet sorteras med nyast först (okänt år sist).
    """
    valda = kallor or list(PROVIDERS)
    okanda = [k for k in valda if k not in PROVIDERS]
    if okanda:
        raise ValueError(
            f"Okänd(a) källa/källor: {', '.join(okanda)}. "
            f"Tillgängliga: {', '.join(PROVIDERS)}."
        )

    traffar: list[dict] = []
    per_kalla: dict[str, dict] = {}
    fel: dict[str, str] = {}

    for namn in valda:
        provider = PROVIDERS[namn]
        try:
            svar = provider["sok"](q, limit_per_kalla, fran_ar, till_ar)
            traffar.extend(svar.get("traffar", []))
            per_kalla[namn] = {"totalt": svar.get("totalt"), "antal": svar.get("antal")}
        except provider["fel"] as exc:
            fel[namn] = str(exc)
        except Exception as exc:  # oväntat fel i en källa får inte fälla helheten
            fel[namn] = f"Oväntat fel: {exc}"

    traffar.sort(key=lambda t: (t.get("ar") is None, -(t.get("ar") or 0)))

    resultat = {
        "fraga": q,
        "kallor": valda,
        "per_kalla": per_kalla,
        "antal": len(traffar),
        "traffar": traffar,
    }
    if fel:
        resultat["fel"] = fel
    return resultat
