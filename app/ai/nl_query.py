"""Da una frase in italiano ai parametri di ricerca.

"Devo essere a Matera venerdi' sera, parto da Torino, massimo 120 euro, niente
notturni" contiene origine, destinazione, data, budget, un vincolo di orario di
arrivo e una preferenza. Scriverlo in sei campi e' noioso; farlo interpretare a
un modello costa pochi token ed e' esattamente il tipo di compito in cui i
modelli sono affidabili.

Il risultato e' sempre un **viaggio**, anche quando ha una tappa sola: "da
Matera a Roma per tre giorni, poi a Torino" e "da Torino a Matera venerdi'" sono
la stessa cosa con un numero diverso di tappe, e tenerle sulla stessa strada
evita il ramo che si aggiorna solo per meta'.

Le date relative si risolvono qui, non nel modello: al modello si passa la data
di oggi e gli si chiede una data assoluta, e comunque si valida il risultato.
Un modello che sbaglia l'anno e' un errore silenzioso che manda la ricerca su
un giorno sbagliato.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from app.ai import client
from app.models import BOOKABLE_MODES, Mode, TripPlan, TripStage, chain_dates

logger = logging.getLogger(__name__)

#: Oltre un anno avanti nessun operatore vende, ed e' quasi certo un errore
#: di interpretazione piuttosto che una richiesta vera.
MAX_DAYS_AHEAD = 365

SYSTEM = """Interpreti richieste di viaggio in italiano e le converti in parametri.
Un viaggio puo' avere piu' tappe in fila: "da Matera a Roma per tre giorni, poi a
Torino" sono due tappe, non due viaggi separati.
Rispondi SOLO con JSON, senza commenti, con queste chiavi:
{
  "stages": [
    {
      "origin": "citta di partenza della tappa",
      "destination": "citta di arrivo della tappa",
      "stay_days": 0,
      "arrive_by": null,
      "depart_after": null
    }
  ],
  "date": "AAAA-MM-GG",
  "pax": 1,
  "modes": ["rail","bus","air","ferry"],
  "with_checked_bag": false,
  "max_budget": null,
  "allow_night": true
}
Regole:
- "stages" ha almeno una tappa. La partenza di una tappa e' l'arrivo della
  precedente: non saltare pezzi e non invertire l'ordine.
- "stay_days" sono i giorni di permanenza PRIMA di ripartire per la tappa
  successiva ("per tre giorni" = 3). Sull'ultima tappa vale 0.
- "date" e' la partenza della PRIMA tappa, sempre assoluta, calcolata dalla data
  di oggi che ti viene fornita. Le altre si ricavano dalle soste: non calcolarle.
- "arrive_by" e "depart_after" sono della singola tappa, in formato "HH:MM"
  oppure null. "entro sera" vale "21:00", "in mattinata" vale "12:00",
  "entro pranzo" "13:00".
- "modes" contiene solo i mezzi ammessi; se l'utente non li nomina, mettili tutti.
- "allow_night" false solo se l'utente rifiuta esplicitamente i viaggi notturni.
- "max_budget" e' un numero in euro per persona, oppure null.
- Se un dato non c'e', usa il valore di default: non inventare."""


class ParseFailed(ValueError):
    """La frase non contiene abbastanza informazioni per una ricerca."""


async def parse(text: str, today: date | None = None) -> TripPlan:
    """Da una frase al viaggio, di una tappa o di quattro.

    Il caso a una tappa non e' un caso speciale: e' un viaggio con una tappa
    sola, e tenerlo sulla stessa strada evita il ramo che si aggiorna solo
    per meta'."""
    if not client.is_configured():
        raise ParseFailed("interpretazione del linguaggio naturale non configurata")

    today = today or date.today()
    prompt = f"Oggi e' {today.isoformat()} ({today.strftime('%A')}).\nRichiesta: {text}"
    answer = await client.complete_json("json", SYSTEM, prompt, max_tokens=600)
    parsed = answer.data
    if not parsed:
        # Il motivo arriva fino al messaggio: "riprova fra poco" e "metti una
        # chiave" sono due azioni diverse, e prima erano la stessa frase.
        raise ParseFailed(f"non ho potuto interpretare la richiesta: {client.why(answer.reason)}")

    partenza = _date(parsed.get("date"), today)
    stages = _stages(parsed, partenza)
    if not stages:
        raise ParseFailed("non ho capito da dove a dove vuoi andare")

    return TripPlan(
        stages=chain_dates(stages),
        pax=_int(parsed.get("pax"), default=1, low=1, high=9),
        modes=_modes(parsed.get("modes")),
        with_checked_bag=bool(parsed.get("with_checked_bag")),
        max_budget=_float(parsed.get("max_budget")),
        allow_night=parsed.get("allow_night") is not False,
        raw_text=text,
    )


def _stages(parsed: dict, partenza: date) -> list[TripStage]:
    """Le tappe leggibili, scartando quelle senza capo o senza coda.

    Un modello che perde una destinazione produrrebbe una tappa verso il nulla:
    meglio una tappa in meno che una ricerca su una meta inventata."""
    grezze = parsed.get("stages")
    if not isinstance(grezze, list):
        # Il modello puo' rispondere nel formato a viaggio singolo, specie i
        # piu' piccoli: si accetta invece di buttare via una risposta buona.
        grezze = [parsed]

    stages: list[TripStage] = []
    for grezza in grezze[:8]:
        if not isinstance(grezza, dict):
            continue
        origin = _text(grezza.get("origin"))
        destination = _text(grezza.get("destination"))
        # Le tappe dopo la prima possono omettere l'origine: e' l'arrivo della
        # precedente, ed e' esattamente cosi' che si racconta a voce.
        if not origin and stages:
            origin = stages[-1].destination
        if not origin or not destination or origin.lower() == destination.lower():
            continue
        stages.append(
            TripStage(
                origin=origin,
                destination=destination,
                date=partenza,
                stay_days=_int(grezza.get("stay_days"), default=0, low=0, high=365),
                arrive_by=_time(grezza.get("arrive_by")),
                depart_after=_time(grezza.get("depart_after")),
            )
        )
    # L'ultima tappa non ha un "dopo": una sosta li' non sposta niente e
    # comparirebbe a schermo come un dato che non serve a nulla.
    if stages:
        stages[-1] = stages[-1].model_copy(update={"stay_days": 0})
    return stages


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _date(value: object, today: date) -> date:
    if isinstance(value, str):
        try:
            parsed = datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
        except ValueError:
            parsed = None
        if parsed is not None:
            # Un modello che sbaglia l'anno manderebbe la ricerca su un giorno
            # gia' passato senza che nessuno se ne accorga.
            if parsed < today:
                logger.info("data nel passato (%s), la sposto all'anno prossimo", parsed)
                try:
                    parsed = parsed.replace(year=today.year + 1)
                except ValueError:  # 29 febbraio
                    parsed = parsed + timedelta(days=365)
            if parsed <= today + timedelta(days=MAX_DAYS_AHEAD):
                return parsed
            logger.info("data troppo lontana (%s), uso il default", parsed)
    return today + timedelta(days=7)


def _int(value: object, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _time(value: object) -> time | None:
    if not isinstance(value, str) or ":" not in value:
        return None
    try:
        hours, minutes = value.strip().split(":")[:2]
        return time(int(hours), int(minutes))
    except ValueError:
        return None


def _modes(value: object) -> set[Mode]:
    valid = {mode.value for mode in BOOKABLE_MODES}
    if isinstance(value, list):
        chosen = {Mode(item) for item in value if isinstance(item, str) and item in valid}
        if chosen:
            return chosen
    return set(BOOKABLE_MODES)
