"""Anthropic, che non parla il formato OpenAI e quindi ha bisogno di suo.

Le differenze che contano, tutte verificate sulla documentazione ufficiale e non
dedotte dal formato degli altri:

  - il prompt di sistema e' un campo a parte, non un messaggio con `role`;
  - la risposta e' un elenco di blocchi, e il testo sta in quelli di tipo
    `text`: leggere `content[0]` alla cieca funziona finche' non arriva un
    blocco di pensiero davanti;
  - il troncamento si chiama `stop_reason == "max_tokens"`, non
    `finish_reason == "length"`. Va tradotto, altrimenti la penalita' piu'
    importante di questo progetto non scatterebbe mai su questo fornitore;
  - **il pensiero e' attivo di default e `max_tokens` limita pensiero piu'
    testo insieme.** Con i 700 token che il progetto passa per il JSON la
    risposta arriverebbe tagliata a meta': qui il budget viene alzato, perche'
    un tetto pensato per il solo testo su questo modello e' un tetto sbagliato.

Si usa l'SDK ufficiale, non una POST scritta a mano: le regole qui sopra sono
esattamente il genere di dettaglio che si sbaglia scrivendolo a memoria. Le
riprove restano a carico del failover in `client.py` (`max_retries=0`), come per
gli altri fornitori: due strati di riprove sullo stesso 429 raddoppiano l'attesa
senza aumentare le probabilita'.
"""

from __future__ import annotations

import logging
from typing import Any

from app.ai.providers import CallFailed, Provider, api_key

logger = logging.getLogger(__name__)

#: Quanto spazio dare al pensiero oltre al testo chiesto dal chiamante. Il
#: fattore e' generoso di proposito: sforare significa una risposta tagliata,
#: mentre avanzare non costa niente (si paga quello che si genera, non il tetto).
THINKING_HEADROOM = 6
MIN_MAX_TOKENS = 4096

#: Lo sforzo per compito. Sul JSON serve una risposta ordinata e corta, non una
#: riflessione: e' il compito in cui il ragionamento esteso fa danno.
EFFORT = {"json": "low", "advice": "medium"}


def _client(provider: Provider):
    try:
        from anthropic import AsyncAnthropic
    except ImportError as exc:  # pragma: no cover - dipendenza facoltativa
        raise CallFailed("error", f"pacchetto anthropic non installato: {exc}") from exc

    return AsyncAnthropic(api_key=api_key(provider), max_retries=0)


def _text(message: Any) -> str:
    """Il testo della risposta, che sta nei blocchi di tipo `text`.

    Gli altri blocchi (pensiero, uso di strumenti) non sono testo per l'utente e
    vanno saltati, non concatenati."""
    parts = [
        block.text
        for block in getattr(message, "content", None) or []
        if getattr(block, "type", None) == "text" and getattr(block, "text", None)
    ]
    return "\n".join(parts).strip()


async def call(
    provider: Provider,
    model: str,
    system: str,
    user: str,
    *,
    max_tokens: int,
    task: str,
) -> tuple[str, str | None]:
    """Una chiamata sola. Restituisce (testo, motivo di fine) o solleva `CallFailed`."""
    import anthropic

    client = _client(provider)
    budget = max(MIN_MAX_TOKENS, max_tokens * THINKING_HEADROOM)

    try:
        message = await client.messages.create(
            model=model,
            max_tokens=budget,
            system=system,
            messages=[{"role": "user", "content": user}],
            thinking={"type": "adaptive"},
            output_config={"effort": EFFORT.get(task, "low")},
        )
    except anthropic.RateLimitError as exc:
        raise CallFailed("rate_limited", str(exc)) from exc
    except anthropic.APIConnectionError as exc:
        raise CallFailed("error", str(exc)) from exc
    except anthropic.APIStatusError as exc:
        raise CallFailed("error", f"{exc.status_code}: {exc.message}") from exc
    finally:
        await client.close()

    # Il rifiuto va guardato prima del contenuto: e' una risposta riuscita
    # (HTTP 200) con i blocchi vuoti, e leggerli senza controllare darebbe un
    # testo vuoto senza spiegazione.
    if message.stop_reason == "refusal":
        raise CallFailed("error", "richiesta rifiutata dai filtri del modello")

    return _text(message), finish_reason(message.stop_reason)


def finish_reason(stop_reason: str | None) -> str | None:
    """Traduce il motivo di fine nel vocabolario del resto del progetto.

    `max_tokens` qui e' lo stesso difetto che altrove si chiama `length`: la
    risposta esiste ma e' tagliata. Senza questa riga la penalita' piu'
    importante del progetto non scatterebbe mai su questo fornitore, e un JSON
    troncato passerebbe per buono."""
    return "length" if stop_reason == "max_tokens" else stop_reason
