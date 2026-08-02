"""Chiamate ai modelli con failover fra fornitori e apprendimento dai difetti.

Regole che nascono dall'uso, quasi tutte dal piano gratuito:

  - Un 429 su un modello gratuito quasi mai significa quota esaurita: e' il
    throttle dell'host, condiviso fra tutti gli utenti. Si passa al modello
    successivo, e se il fornitore e' esaurito si passa al fornitore successivo:
    e' il motivo per cui questo modulo gira su coppie (fornitore, modello) e non
    su un elenco di modelli.
  - `finish_reason == "length"` va trattato come fallimento anche se il testo
    sembra completo. Su una risposta JSON e' il caso peggiore: l'oggetto esterno
    puo' risultare chiuso mentre un array dentro e' tagliato, e
    l'interpretazione riesce restituendo dati incompleti senza segnalare nulla.
  - Ogni difetto osservato diventa una penalita' per **quella coppia**, non per
    il modello: lo stesso slug puo' troncare su un host e funzionare su un
    altro, e spegnerlo ovunque per colpa di uno solo sarebbe uno spreco.

Quando non esce niente si restituisce comunque un `Answer`, che porta il
**motivo**. Le funzioni che usano l'IA restano facoltative e il sito funziona
senza, ma «non configurata» e «ha fallito» sono due cose diverse per chi
guarda lo schermo, e prima erano lo stesso `None`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import orjson

from app.ai import model_selector, providers
from app.ai.providers import CallFailed, Provider
from app.config import get_settings
from app.orchestrator import cache
from app.providers.http_client import Blocked, HttpError, get_http_client

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 4

#: Attese prima di riprovare **lo stesso** modello dopo un 429. Misurato: il
#: throttle dei piani gratuiti dura secondi, non minuti — una chiamata isolata
#: subito dopo un burst che aveva bruciato cinque modelli passa al primo colpo.
#: Aspettare costa meno che scartare il pool intero e restare senza risposta.
RETRY_WAITS = (1.0, 3.0, 8.0)

#: Tetto complessivo alle attese dentro **una sola** richiesta. Oltre questo,
#: chi guarda la pagina preferisce un "non disponibile" a un'attesa muta.
RETRY_BUDGET = 12.0

#: E un tetto al tempo totale speso su un compito, attese comprese. Senza,
#: quattro modelli lenti col timeout dei modelli farebbero aspettare minuti una
#: pagina che ha gia' finito di cercare: il consiglio arriva dopo i risultati e
#: ne trattiene la chiusura.
TASK_DEADLINE = 60.0

#: Motivi per cui un `Answer` puo' non avere testo. Arrivano fino a schermo:
#: sono cose diverse e vanno dette con parole diverse.
NOT_CONFIGURED = "not_configured"
NO_MODELS = "no_models"
RATE_LIMITED = "rate_limited"
TRUNCATED = "truncated"
GARBLED = "garbled"
INVENTED = "invented"
UNANCHORED = "unanchored"
FAILED = "failed"
#: L'eccezione: non c'era niente da dire. Nessun itinerario da consigliare, o
#: una sola possibilita' da confrontare. Non riguarda l'IA, e la pagina lo
#: riconosce per tacere invece di scrivere "consiglio non disponibile"
#: (`motivoDaDire` in `app/static/app.js`).
NOTHING = "nothing"

#: Gli stessi motivi, detti a chi legge. Stanno qui e non nell'interfaccia
#: perche' li usa anche `/api/parse`, che risponde con una frase e non con un
#: codice.
WHY = {
    NOT_CONFIGURED: "nessuna chiave IA configurata",
    NO_MODELS: "nessun modello disponibile per questo compito",
    RATE_LIMITED: "il fornitore ha rifiutato la richiesta (429)",
    TRUNCATED: "il modello ha troncato la risposta",
    GARBLED: "il modello ha risposto con il suo ragionamento invece che con il consiglio",
    INVENTED: "il modello ha citato soluzioni che non esistono",
    UNANCHORED: "il modello non ha detto di quale soluzione parla",
    FAILED: "nessun modello ha risposto",
}


def why(reason: str) -> str:
    return WHY.get(reason, WHY[FAILED])


#: Un lock per fornitore. Le chiamate all'IA di una ricerca partono insieme (un
#: consiglio per combinazione, il confronto, gli scali) e su un host solo
#: diventano un burst: nel log del 2026-08-01 sono cinque modelli bruciati con
#: `HTTP 429` in sei secondi, e nessun consiglio a schermo. L'alternanza fra
#: fornitori scritta in `model_selector.rank_candidates` non aiuta quando di
#: fornitore configurato ce n'e' uno: quello che serve li' e' una fila.
_LOCKS: dict[str, asyncio.Lock] = {}


#: Come comincia un ragionamento sfuggito al posto della risposta. Sono aperture
#: inglesi, e il prompt chiede italiano: la coincidenza non e' un caso, e' il
#: modello che sta pensando ad alta voce invece di rispondere.
_APERTURE_DI_RAGIONAMENTO = (
    "we need", "we should", "we must", "we have to", "we can",
    "let me", "let's", "first, i", "first i", "i need to", "i should",
    "the user", "okay,", "ok,", "alright,", "sure,", "here's",
    "analysis", "thinking:", "reasoning:",
)

#: Parole che in tre righe di italiano ci sono per forza.
_PAROLE_ITALIANE = (
    " il ", " la ", " di ", " che ", " per ", " un ", " una ", " non ",
    " con ", " sono ", " piu' ", " più ", " ma ", " se ", " da ", " e ",
)


def sembra_ragionamento(text: str) -> bool:
    """Vero se la risposta e' il ragionamento del modello, non la risposta.

    Visto sul campo: «We need to produce indicates ,, has is,,., and,» al posto
    di un consiglio di viaggio. I modelli con ragionamento esteso a volte
    scaricano la catena di pensiero nel testo, e il risultato passa ogni
    controllo — non e' vuoto, non e' troncato, e nemmeno il JSON c'entra. Va
    riconosciuto qui o finisce a schermo come se fosse un parere.

    Due segnali, e ne basta uno: un'apertura tipica del pensiero ad alta voce,
    oppure un testo lungo in cui non compare una sola parola italiana."""
    pulito = text.strip().lower()
    if not pulito:
        return False
    if pulito.startswith(_APERTURE_DI_RAGIONAMENTO):
        return True
    # Sotto le duecento battute il campione e' troppo corto per dire qualcosa:
    # "Prendi il pullman." e' italiano legittimo e cortissimo.
    inizio = f" {pulito[:400]} "
    return len(pulito) > 200 and not any(parola in inizio for parola in _PAROLE_ITALIANE)


def _lock_for(name: str) -> asyncio.Lock:
    lock = _LOCKS.get(name)
    if lock is None:
        lock = _LOCKS[name] = asyncio.Lock()
    return lock


class AIUnavailable(RuntimeError):
    """Nessun modello ha risposto in modo utilizzabile."""


@dataclass
class Completion:
    text: str
    model: str
    finish_reason: str | None
    provider: str = "openrouter"


@dataclass(frozen=True)
class Answer:
    """La risposta dell'IA, oppure il motivo per cui non c'e'.

    Prima qui tornava `None` in entrambi i casi, e da fuori «nessuna chiave» e
    «tutti i modelli hanno fallito» erano indistinguibili: l'interfaccia non
    poteva far altro che tacere. E' il difetto per cui il consiglio era sparito
    senza che nessuno lo dicesse."""

    completion: Completion | None = None
    #: Popolato solo da `complete_json`.
    data: dict | None = None
    #: Vuoto quando la risposta c'e'.
    reason: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.completion is not None

    @property
    def text(self) -> str:
        return self.completion.text if self.completion else ""


def is_configured() -> bool:
    """Vero se almeno un fornitore e' utilizzabile."""
    return bool(providers.configured())


async def _call_openai_dialect(
    provider: Provider,
    model: str,
    system: str,
    user: str,
    *,
    max_tokens: int,
    temperature: float,
    json_mode: bool,
) -> tuple[str, str | None]:
    """Il formato Chat Completions, che sei fornitori su sette parlano uguale."""
    base = providers.base_url(provider)
    if not base:
        raise CallFailed("error", "indirizzo non configurato")

    headers = {"Content-Type": "application/json"}
    key = providers.api_key(provider)
    if key:
        headers["Authorization"] = f"Bearer {key}"
    if provider.name == "openrouter":
        # Facoltativi: OpenRouter li usa per attribuire il traffico.
        headers["HTTP-Referer"] = "http://localhost:8010/"
        headers["X-Title"] = "Trip Finder"

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    try:
        data = await get_http_client().post_json(
            f"{base}/chat/completions",
            json=payload,
            headers=headers,
            retries=0,
            # Non il timeout dei vettori: quello vale due secondi di API, questo
            # aspetta un modello che scrive.
            timeout=get_settings().llm_timeout,
        )
    except Blocked as exc:
        raise CallFailed("rate_limited", str(exc)) from exc
    except HttpError as exc:
        raise CallFailed("error", str(exc)) from exc

    choice = (data.get("choices") or [{}])[0]
    text = ((choice.get("message") or {}).get("content") or "").strip()
    return text, choice.get("finish_reason")


async def _call(
    provider: Provider,
    model: str,
    system: str,
    user: str,
    *,
    max_tokens: int,
    temperature: float,
    json_mode: bool,
    task: str,
) -> tuple[str, str | None]:
    if provider.dialect == "anthropic":
        from app.ai import anthropic_client

        return await anthropic_client.call(
            provider, model, system, user, max_tokens=max_tokens, task=task
        )
    return await _call_openai_dialect(
        provider,
        model,
        system,
        user,
        max_tokens=max_tokens,
        temperature=temperature,
        json_mode=json_mode,
    )


async def complete(
    task: str,
    system: str,
    user: str,
    *,
    max_tokens: int = 900,
    temperature: float = 0.2,
    json_mode: bool = False,
    cache_key: str | None = None,
    check: Callable[[str], str] | None = None,
) -> Answer:
    """Prova le coppie (fornitore, modello) in ordine finche' una risponde bene.

    Le chiamate allo stesso fornitore vanno in fila (`_LOCKS`) e un 429 fa
    riaspettare **lo stesso** modello invece di scartarlo subito: sono le due
    correzioni al burst che aveva zittito l'IA.

    `check` e' un controllo che conosce la richiesta e questo modulo no: gli si
    passa il testo e risponde con il motivo dello scarto, o stringa vuota se va
    bene. Serve a chi puo' verificare la risposta contro i dati che ha mandato
    — per esempio che le soluzioni citate esistano davvero. Uno scarto qui vale
    come un troncamento: penalita' alla coppia e modello successivo."""
    if not is_configured():
        return Answer(reason=NOT_CONFIGURED)

    def scartabile(text: str) -> str:
        """Il motivo per cui questo testo non va mostrato, o stringa vuota."""
        if json_mode:
            return ""  # un JSON non e' in italiano e non cita soluzioni
        if sembra_ragionamento(text):
            return GARBLED
        return check(text) if check else ""

    if cache_key:
        cached = await cache.get(cache_key)
        # Anche la cache passa dal controllo. Una risposta sbagliata salvata
        # ieri resta sbagliata oggi, e servirla senza guardarla vuol dire che il
        # controllo non protegge proprio il caso in cui costa di piu': quello
        # che si ripete uguale a ogni ricarica.
        if cached and not scartabile(str(cached.get("text", ""))):
            # Le voci scritte prima del multi-fornitore non hanno il campo:
            # vale la pena leggerle lo stesso invece di buttare la cache.
            return Answer(
                completion=Completion(
                    text=cached.get("text", ""),
                    model=cached.get("model", ""),
                    finish_reason=cached.get("finish_reason"),
                    provider=cached.get("provider", "openrouter"),
                )
            )
        if cached:
            logger.info(
                "scarto una risposta in cache (%s)", scartabile(str(cached.get("text", "")))
            )

    candidates = await model_selector.rank_candidates(task)
    if not candidates:
        return Answer(reason=NO_MODELS, detail="nessun modello disponibile per questo compito")

    last_reason, last_detail = FAILED, ""
    remaining_wait = RETRY_BUDGET
    scadenza = time.monotonic() + TASK_DEADLINE

    for provider, model in candidates[:MAX_ATTEMPTS]:
        if time.monotonic() > scadenza:
            logger.info("compito %s oltre il tempo massimo, mi fermo", task)
            break
        waits = deque(RETRY_WAITS)
        result: tuple[str, str | None] | None = None

        while True:
            try:
                # Una richiesta per volta verso lo stesso host: e' il punto in
                # cui il burst diventa una fila.
                async with _lock_for(provider.name):
                    result = await _call(
                        provider,
                        model,
                        system,
                        user,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        json_mode=json_mode,
                        task=task,
                    )
            except CallFailed as exc:
                if exc.reason == RATE_LIMITED and waits and remaining_wait > 0:
                    pause = min(waits.popleft(), remaining_wait)
                    remaining_wait -= pause
                    logger.info(
                        "%s/%s sotto throttle, riprovo fra %.0fs", provider.name, model, pause
                    )
                    await asyncio.sleep(pause)
                    continue
                await model_selector.record_penalty(model, exc.reason, provider=provider.name)
                # Il motivo da solo non basta a capire cosa e' successo: "error" puo'
                # essere un 401, un 500 o un timeout, e senza il dettaglio si resta a
                # indovinare proprio quando serve saperlo.
                logger.info(
                    "%s/%s non utilizzabile (%s: %s), passo al successivo",
                    provider.name, model, exc.reason, exc,
                )
                last_reason, last_detail = exc.reason, str(exc)
                result = None
            break

        if result is None:
            continue
        text, finish = result

        if finish == "length":
            # Il difetto piu' insidioso: la risposta esiste ma e' tagliata.
            await model_selector.record_penalty(model, "truncated", provider=provider.name)
            logger.info("%s/%s ha troncato la risposta", provider.name, model)
            last_reason, last_detail = TRUNCATED, f"{provider.name}/{model}"
            continue
        if not text:
            await model_selector.record_penalty(model, "empty", provider=provider.name)
            last_reason, last_detail = FAILED, f"{provider.name}/{model} non ha risposto"
            continue
        # Solo sul testo discorsivo: un JSON non e' in italiano e non deve
        # esserlo, e passarlo di qui lo boccerebbe sempre.
        problema = scartabile(text)
        if problema:
            await model_selector.record_penalty(model, problema, provider=provider.name)
            logger.info(
                "%s/%s scartato (%s): %r", provider.name, model, problema, text[:80]
            )
            last_reason, last_detail = problema, f"{provider.name}/{model}"
            continue

        completion = Completion(
            text=text, model=model, finish_reason=finish, provider=provider.name
        )
        if cache_key:
            await cache.set(cache_key, completion.__dict__, kind="ai")
        return Answer(completion=completion)

    logger.warning(
        "nessun modello utilizzabile per il compito %s (ultimo motivo: %s)", task, last_reason
    )
    return Answer(reason=last_reason, detail=last_detail)


async def complete_json(
    task: str, system: str, user: str, *, max_tokens: int = 700
) -> Answer:
    """Come `complete`, ma con l'oggetto gia' interpretato in `Answer.data`."""
    answer = await complete(
        task, system, user, max_tokens=max_tokens, temperature=0.0, json_mode=True
    )
    if not answer.ok:
        return answer

    completion = answer.completion
    assert completion is not None  # garantito da answer.ok
    parsed = _extract_json(completion.text)
    if parsed is None:
        await model_selector.record_penalty(
            completion.model, "error", provider=completion.provider
        )
        logger.info("%s/%s non ha prodotto JSON valido", completion.provider, completion.model)
        return Answer(
            reason=FAILED, detail=f"{completion.provider}/{completion.model}: JSON non valido"
        )
    return Answer(completion=completion, data=parsed)


def _extract_json(text: str) -> dict | None:
    """Recupera l'oggetto anche quando arriva dentro un blocco di codice."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("```")[1] if "```" in candidate[3:] else candidate[3:]
        candidate = candidate.removeprefix("json").strip()

    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = orjson.loads(candidate[start : end + 1])
    except orjson.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
