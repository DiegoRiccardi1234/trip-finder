"""Scali suggeriti dall'IA, a integrazione del filtro geometrico.

La geometria sa dire quali citta' stanno sulla direttrice; non sa che Matera si
raggiunge da Bari, che per le isole si passa da un porto preciso, o che una
certa tratta ha un unico nodo di scambio reale. Qui si chiede al modello di
aggiungere i nomi che la distanza non cattura.

Il contributo e' additivo e facoltativo: i suggerimenti si sommano agli hub
geometrici, non li sostituiscono, e un errore del modello al massimo fa provare
una citta' in piu'.
"""

from __future__ import annotations

import logging

from app.ai import client
from app.models import Place, SearchQuery
from app.orchestrator import cache

logger = logging.getLogger(__name__)

MAX_SUGGESTIONS = 4

SYSTEM = """Sei un esperto di trasporti pubblici europei.
Ricevi un'origine e una destinazione e indichi le citta' di scambio realmente
usate per quel collegamento: nodi ferroviari, aeroporti di riferimento, porti.
Conta cosa si usa davvero, non cosa sta a meta' strada sulla mappa.
Se il collegamento diretto e' la norma, restituisci una lista vuota.
Rispondi solo con JSON: {"hubs": ["Citta1", "Citta2"]}
Usa i nomi delle citta' nella lingua locale, senza specificare la stazione."""


async def suggest_hubs(
    origin: Place, destination: Place, query: SearchQuery
) -> list[str]:
    if not client.is_configured():
        return []

    key = cache.make_key("ai:hubs", origin.label, destination.label, sorted(m.value for m in query.modes))
    cached = await cache.get(key)
    if cached is not None:
        return list(cached)[:MAX_SUGGESTIONS]

    modes = ", ".join(sorted(mode.value for mode in query.modes))
    prompt = (
        f"Origine: {origin.label} ({origin.country or '?'})\n"
        f"Destinazione: {destination.label} ({destination.country or '?'})\n"
        f"Mezzi ammessi: {modes}\n"
        f"Quali citta' di scambio conviene provare? Al massimo {MAX_SUGGESTIONS}."
    )

    # Qui il motivo non serve: gli scali sono additivi e la loro assenza non e'
    # un fatto da riportare a schermo, a differenza del consiglio finale.
    parsed = (await client.complete_json("json", SYSTEM, prompt, max_tokens=200)).data
    if not parsed:
        return []

    hubs = [
        str(name).strip()
        for name in parsed.get("hubs") or []
        if isinstance(name, str) and name.strip()
    ]
    # Un modello puo' ripetere origine o destinazione: non sono scali.
    excluded = {origin.label.lower(), destination.label.lower()}
    hubs = [hub for hub in hubs if hub.lower() not in excluded][:MAX_SUGGESTIONS]

    await cache.set(key, hubs, kind="ai")
    logger.debug("hub suggeriti per %s-%s: %s", origin.label, destination.label, hubs)
    return hubs
