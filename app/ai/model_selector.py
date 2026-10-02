"""Scelta del modello in base al compito, non "il piu' grosso disponibile".

Due compiti molto diversi convivono in questo progetto:

  - **JSON ad alto volume** (interpretare una richiesta, proporre scali). Serve
    un modello veloce e ordinato di taglia media. I modelli con ragionamento
    esteso sono la scelta peggiore: bruciano il budget di token in una catena
    di pensiero nascosta e restituiscono JSON troncato. Il segnale da guardare
    e' `finish_reason`: se vale "length" il completamento e' stato tagliato e
    il JSON non e' affidabile nemmeno quando per caso si riesce a interpretarlo.
  - **Testo di sintesi** (il consiglio finale). Qui serve capacita', si accetta
    di essere piu' lenti.

Taglia e qualita' non coincidono. Un 27B pulito batte un 235B con ragionamento
che tronca. La soglia di qualita' sta intorno ai 24-27 miliardi di parametri:
sotto si scende nei modelli giocattolo, sopra si escludono i modelli medi
puliti e restano i giganti che troncano.

Il nome non dice se un modello ragiona: `nemotron-super`, `gpt-oss`, `hy3` non
hanno "reasoning" nello slug. Per questo alla fine conta l'evidenza raccolta a
runtime, non l'euristica sul nome.
"""

from __future__ import annotations

import logging
import re
import time

from app import config
from app.ai import endpoint_health, providers
from app.config import get_settings
from app.orchestrator import cache
from app.orchestrator.db import get_db
from app.providers.http_client import HttpError, get_http_client

logger = logging.getLogger(__name__)

#: Pool di partenza di OpenRouter, verificati a luglio 2026. Il piano gratuito
#: ruota in fretta: modelli che c'erano due mesi fa rispondono `endpoints: []`.
#: Per questo il pool e' solo un punto di partenza; se muore tutto si passa alla
#: scoperta automatica piu' sotto, e .env permette comunque di imporne altri.
#: Vivono nel registro dei fornitori insieme a quelli degli altri sei: qui c'e'
#: un alias, perche' due elenchi che dicono la stessa cosa divergono sempre.
DEFAULT_POOLS: dict[str, list[str]] = dict(providers.BY_NAME["openrouter"].pools)

MODELS_URL = "https://openrouter.ai/api/v1/models"

#: Slug da non proporre mai automaticamente: non sono modelli conversazionali
#: o hanno difetti noti su questi compiti.
DISCOVERY_BLOCKLIST = ("lyria", "content-safety", "-vl", "-code", "whisper", "embed")

#: Sotto questa taglia i modelli sbagliano struttura e istruzioni troppo spesso.
QUALITY_FLOOR_B = 24.0

SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b\b")
REASONING_HINTS = ("thinking", "reason", "-r1", "qwq", "deepthink", "-cot")
INSTRUCT_HINTS = ("instruct", "-it", "chat", "flash")

#: Quanto dura una penalita' raccolta sul campo.
PENALTY_TTL = 30 * 60

#: Tranne il 429, che non e' un difetto del modello. E' il throttle dell'host,
#: condiviso fra tutti quelli che lo usano in quel momento, e passa in secondi:
#: tenerlo mezz'ora spegne un modello sano per il resto della sessione, ed e'
#: quello che era successo — cinque modelli su cinque de-rankati da un burst.
PENALTY_TTL_BY_REASON = {"rate_limited": 3 * 60}

PENALTY_WEIGHT = {
    "truncated": 6.0,  # JSON tagliato: il difetto piu' grave per questi usi
    # Il ragionamento al posto della risposta: grave quanto il troncamento,
    # perche' passa ogni altro controllo e finisce a schermo come un parere.
    "garbled": 6.0,
    # Soluzioni citate che non esistono. Stessa gravita': e' un consiglio che
    # sembra perfetto e manda a cercare a schermo una scheda che non c'e'.
    "invented": 6.0,
    # Un consiglio che non dice di quale soluzione parla: si legge bene e non
    # si puo' seguire. Meno grave, ma sempre da non mostrare.
    "unanchored": 4.0,
    "empty": 4.0,
    "rate_limited": 2.0,
    "error": 3.0,
}


def parse_size_b(slug: str) -> float | None:
    """Miliardi di parametri desunti dallo slug, quando dichiarati."""
    matches = SIZE_RE.findall(slug.lower())
    if not matches:
        return None
    # "llama-4-maverick-17b-128e": vince il numero piu' grande, che e' la taglia.
    return max(float(value) for value in matches)


def score_model_name(slug: str, task: str) -> float:
    """Punteggio a priori, dal solo nome. Grezzo ma sorprendentemente utile."""
    lower = slug.lower()
    score = 0.0

    size = parse_size_b(lower)
    if size is not None:
        if size < QUALITY_FLOOR_B:
            score -= 4.0  # sotto la soglia di qualita'
        elif task == "json":
            # Per il JSON la taglia in eccesso non aiuta e costa latenza.
            score += 2.0 if size <= 80 else 0.5
        else:
            score += 1.0 + min(2.0, size / 100.0)

    if any(hint in lower for hint in INSTRUCT_HINTS):
        score += 1.5
    if any(hint in lower for hint in REASONING_HINTS):
        # Sul JSON e' quasi sempre un problema; sul testo lungo molto meno.
        score -= 3.0 if task == "json" else 0.5
    if lower.endswith(":free"):
        score += 0.5

    return score


async def record_penalty(model: str, reason: str, provider: str = "openrouter") -> None:
    """Registra un difetto osservato davvero, per la durata del compito.

    La penalita' e' per coppia (provider, modello): lo stesso slug puo' troncare
    su un host e funzionare su un altro, e spegnerlo ovunque per colpa di uno
    solo sarebbe uno spreco."""
    db = await get_db()
    weight = PENALTY_WEIGHT.get(reason, 2.0)
    ttl = PENALTY_TTL_BY_REASON.get(reason, PENALTY_TTL)
    await db.execute(
        "INSERT INTO model_penalty(provider, model, reason, penalty, expires_at) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(provider, model) DO UPDATE SET "
        "reason = excluded.reason, penalty = model_penalty.penalty + excluded.penalty, "
        # MAX e non l'ultimo valore: un 429 che arriva dopo un troncamento
        # accorcerebbe la penalita' del troncamento, che invece e' un difetto
        # vero del modello e deve durare la sua mezz'ora.
        "expires_at = MAX(model_penalty.expires_at, excluded.expires_at)",
        (provider, model, reason, weight, time.time() + ttl),
    )
    await db.commit()
    logger.info("penalita' %s per %s (%s, %d min)", weight, model, reason, ttl // 60)


async def current_penalties(provider: str = "openrouter") -> dict[str, float]:
    db = await get_db()
    async with db.execute(
        "SELECT model, penalty FROM model_penalty WHERE provider = ? AND expires_at > ?",
        (provider, time.time()),
    ) as cursor:
        rows = await cursor.fetchall()
    return {model: penalty for model, penalty in rows}


async def clear_penalties() -> None:
    db = await get_db()
    await db.execute("DELETE FROM model_penalty")
    await db.commit()


async def catalogo_openrouter() -> dict[str, list[str]]:
    """Il catalogo vivo di OpenRouter, diviso fra gratuiti e a pagamento.

    Una chiamata sola per entrambi: sono la stessa risposta, e chiederla due
    volte per filtrarla in due modi diversi sarebbe uno spreco. Al momento della
    scrittura sono 17 gratuiti e 320 a pagamento, e i due numeri vanno trattati
    in modo diverso — i primi si possono elencare tutti, i secondi no."""
    key = cache.make_key("openrouter:catalog")
    cached = await cache.get(key)
    if cached is not None:
        return {"free": list(cached.get("free", [])), "paid": list(cached.get("paid", []))}

    try:
        payload = await get_http_client().get_json(
            MODELS_URL, headers={"Accept": "application/json"}, retries=1
        )
    except HttpError as exc:
        logger.debug("catalogo modelli non raggiungibile: %s", exc)
        return {"free": [], "paid": []}

    diviso: dict[str, list[str]] = {"free": [], "paid": []}
    for model in (payload.get("data") if isinstance(payload, dict) else None) or []:
        slug = model.get("id") if isinstance(model, dict) else None
        pricing = model.get("pricing") if isinstance(model, dict) else None
        if not slug or not isinstance(pricing, dict):
            continue
        if any(word in slug.lower() for word in DISCOVERY_BLOCKLIST):
            continue
        try:
            gratuito = not (
                float(pricing.get("prompt", 1)) or float(pricing.get("completion", 1))
            )
        except (TypeError, ValueError):
            continue
        diviso["free" if gratuito else "paid"].append(slug)

    diviso["paid"].sort()
    await cache.set(key, diviso, kind="static", ttl=6 * 3600)
    return diviso


async def discover_free_models(limit: int = 8) -> list[str]:
    """Modelli gratuiti realmente in catalogo adesso.

    Serve quando il pool scritto nel codice e' invecchiato. E' successo gia' una
    volta: cinque slug su cinque rispondevano `endpoints: []` perche' nel
    frattempo erano stati ritirati. Senza questa rete di sicurezza l'IA sarebbe
    semplicemente sparita, e in modo silenzioso."""
    return (await catalogo_openrouter())["free"][:limit]


async def rank_models(task: str, candidates: list[str] | None = None) -> list[str]:
    """Modelli da provare in ordine, i migliori per primi.

    L'ordine finale nasce da tre segnali, in questa gerarchia: la salute live
    (un modello morto non ha appello), la fascia di disponibilita' e infine la
    qualita' attesa per quel compito. La fascia serve proprio a impedire che
    mezzo punto di uptime scavalchi un modello nettamente piu' adatto."""
    settings = get_settings()
    pool = candidates or settings.model_pool(task) or DEFAULT_POOLS.get(task, [])
    if not pool:
        return []

    order = await _rank_pool(pool, task)
    if order:
        return order

    # Pool invecchiato: si ricostruisce dal catalogo vivo invece di arrendersi.
    logger.warning("nessun modello sano nel pool %s, passo alla scoperta", task)
    discovered = [slug for slug in await discover_free_models(limit=10) if slug not in pool]
    order = await _rank_pool(discovered, task)
    if order:
        logger.info("pool %s ricostruito dal catalogo: %s", task, order[:3])
        return order

    # I fetch falliti restano UNKNOWN e passano gia' _rank_pool. Arrivare qui
    # significa che tutti i modelli sono stati bocciati con evidenza: non
    # rimettere in gioco slug ritirati o endpoint non operativi.
    return []


async def _rank_pool(pool: list[str], task: str, provider: str = "openrouter") -> list[str]:
    if not pool:
        return []

    health = await endpoint_health.check_many(pool)
    penalties = await current_penalties(provider)

    alive = [
        (slug, health.get(slug, endpoint_health.UNKNOWN))
        for slug in pool
        if health.get(slug, endpoint_health.UNKNOWN).alive
    ]
    for slug in pool:
        status = health.get(slug, endpoint_health.UNKNOWN)
        if not status.alive:
            logger.debug("scartato %s: %s", slug, status.detail)
    if not alive:
        return []

    tiers = endpoint_health.tiers([status.uptime_5m for _, status in alive])
    ranked = [
        (tier, score_model_name(slug, task) - penalties.get(slug, 0.0), slug)
        for (slug, _), tier in zip(alive, tiers)
    ]
    # Prima la fascia di disponibilita' (0 = migliore), poi la qualita' attesa
    # dentro la fascia: cosi' un modello piu' adatto non viene scavalcato per
    # due decimi di punto percentuale.
    ranked.sort(key=lambda item: (item[0], -item[1]))
    return [slug for _, _, slug in ranked]


# ------------------------------------------------------------- multi-fornitore


async def discover_catalog(provider: providers.Provider, limit: int = 40) -> list[str]:
    """Il catalogo vivo di un fornitore compatibile OpenAI (`GET {base}/models`).

    Serve dove il pool scritto nel codice non basta: i nomi dei modelli cambiano
    e un elenco fissato invecchia. Se la chiamata non riesce non si blocca
    niente, si torna vuoti e restano i pool di partenza."""
    base = providers.base_url(provider)
    if not provider.lists_models or not base:
        return []

    key = cache.make_key("catalog", provider.name)
    cached = await cache.get(key)
    if cached is not None:
        return list(cached)[:limit]

    headers = {"Accept": "application/json"}
    token = providers.api_key(provider)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        payload = await get_http_client().get_json(f"{base}/models", headers=headers, retries=1)
    except HttpError as exc:
        logger.debug("catalogo di %s non raggiungibile: %s", provider.name, exc)
        return []

    found = [
        str(entry.get("id"))
        for entry in (payload.get("data") if isinstance(payload, dict) else None) or []
        if isinstance(entry, dict) and entry.get("id")
    ]
    usable = [slug for slug in found if not any(word in slug.lower() for word in DISCOVERY_BLOCKLIST)]
    await cache.set(key, usable, kind="static", ttl=6 * 3600)
    return usable[:limit]


async def _models_for(provider: providers.Provider, task: str) -> list[str]:
    """I modelli di un fornitore, i migliori per primi.

    Solo OpenRouter ha un controllo di salute per modello (`/endpoints`, gratis
    e senza autenticazione): la' si scarta chi e' morto prima di provarlo. Per
    gli altri la salute non e' interrogabile, quindi contano la qualita' attesa
    dal nome e le penalita' raccolte sul campo."""
    if provider.name == "openrouter":
        return await rank_models(task)

    pool = providers.pool_for(provider, task)
    if not pool:
        pool = await discover_catalog(provider)
    if not pool:
        return []

    penalties = await current_penalties(provider.name)
    ranked = sorted(
        pool,
        key=lambda slug: -(score_model_name(slug, task) - penalties.get(slug, 0.0)),
    )
    return ranked


async def catalogo(task: str) -> list[dict[str, object]]:
    """Ogni modello candidato per un compito, con quello che si sa di lui.

    Serve alla scheda Impostazioni: senza, l'unica cosa visibile era l'ordine
    di prova, che non dice **perche'** un modello e' in fondo — se e' morto, se
    ha appena troncato una risposta, o se semplicemente e' meno adatto. Nessuna
    di queste tre cose si risolve allo stesso modo."""
    a_pagamento_ammessi = get_settings().allow_paid_providers
    voci: list[dict[str, object]] = []

    for provider in providers.configured():
        consigliati = list(providers.pool_for(provider, task))
        gratuiti: list[str] = []
        pagamento: list[str] = []

        if provider.name == "openrouter":
            catalogo_vivo = await catalogo_openrouter()
            gratuiti = catalogo_vivo["free"]
            # I 320 a pagamento si mostrano solo a chi li ha accesi: elencarli
            # comunque vorrebbe dire proporre una spesa a chi ha detto di no.
            pagamento = catalogo_vivo["paid"] if a_pagamento_ammessi else []
        elif not consigliati:
            gratuiti = await discover_catalog(provider, limit=40)

        # L'ordine conta: i consigliati per primi, e nessun doppione.
        elenco: list[str] = []
        for gruppo in (consigliati, gratuiti, pagamento):
            for slug in gruppo:
                if slug not in elenco:
                    elenco.append(slug)

        penalita = await current_penalties(provider.name)
        # La salute per modello la pubblica solo OpenRouter, gratis e senza
        # autenticazione. Per gli altri non esiste: dirlo e' meglio che
        # inventare un pallino verde che non significa niente. E si verificano
        # solo i gratuiti: sono una dozzina ed e' il pool che marcisce, mentre
        # trecentoventi verifiche sarebbero quaranta secondi di attesa per una
        # informazione che li' non serve.
        da_verificare = (
            [slug for slug in elenco if slug not in pagamento]
            if provider.name == "openrouter" else []
        )
        salute = await endpoint_health.check_many(da_verificare) if da_verificare else {}

        for slug in elenco:
            stato = salute.get(slug)
            voci.append({
                "id": f"{provider.name}/{slug}",
                "provider": provider.name,
                "model": slug,
                # Del **modello**, non del fornitore: su OpenRouter convivono.
                "gratuito": provider.free and slug not in pagamento,
                "consigliato": slug in consigliati,
                "verificabile": slug in da_verificare,
                "vivo": stato.alive if stato else None,
                "uptime_5m": round(stato.uptime_5m, 1) if stato else None,
                "hosts": list(stato.providers) if stato else [],
                "detail": stato.detail if stato else "",
                "penalita": round(penalita.get(slug, 0.0), 1),
                "qualita": round(score_model_name(slug, task), 1),
            })
    return voci


async def rank_candidates(task: str) -> list[tuple[providers.Provider, str]]:
    """Le coppie (fornitore, modello) da provare, in ordine.

    L'ordine alterna i fornitori invece di esaurirne uno: prima la scelta
    migliore di ciascuno, poi la seconda di ciascuno. E' il punto: quando un
    fornitore gratuito e' sotto throttle lo sono di solito **tutti** i suoi
    modelli, e provarne quattro di fila vuol dire quattro attese per niente
    mentre accanto c'e' un altro host libero."""
    columns: list[list[tuple[providers.Provider, str]]] = []
    for provider in providers.configured():
        models = await _models_for(provider, task)
        if models:
            columns.append([(provider, model) for model in models])

    candidates: list[tuple[providers.Provider, str]] = []
    for index in range(max((len(column) for column in columns), default=0)):
        for column in columns:
            if index < len(column):
                candidates.append(column[index])

    ordered = _pin_first(candidates, task)
    pinned = config.pinned_model(task)
    if pinned and pinned[0] == "openrouter" and any(
        provider.name == pinned[0] and model == pinned[1] for provider, model in ordered
    ):
        health = await endpoint_health.check(pinned[1])
        if not health.alive:
            ordered = [(provider, model) for provider, model in ordered
                       if (provider.name, model) != pinned]
    return ordered


def _pin_first(
    candidates: list[tuple[providers.Provider, str]], task: str
) -> list[tuple[providers.Provider, str]]:
    """Porta in testa il modello scelto a mano, lasciando gli altri dietro.

    In testa e non da solo: se il modello scelto rifiuta la richiesta, restare
    senza risposta per rispetto della scelta sarebbe un modo curioso di
    rispettarla. La fila esiste per questo. Se il modello scelto non e' fra i
    candidati perche' non e' nel pool, si aggiunge lo stesso. Per OpenRouter
    `rank_candidates` verifica anche il pin: una preferenza non riattiva un
    endpoint morto, mentre una salute non verificabile conserva il fallback."""
    scelto = config.pinned_model(task)
    if not scelto:
        return candidates
    nome_fornitore, modello = scelto

    provider = providers.BY_NAME.get(nome_fornitore)
    if provider is None or not providers.is_configured(provider):
        logger.info("modello scelto su %s ma il fornitore non e' configurato", nome_fornitore)
        return candidates

    if not get_settings().allow_paid_providers and (
        not provider.free or (provider.name == "openrouter" and not modello.endswith(":free"))
    ):
        logger.info("modello scelto a pagamento ignorato: fornitori a pagamento disattivati")
        return candidates

    resto = [
        coppia
        for coppia in candidates
        if not (coppia[0].name == nome_fornitore and coppia[1] == modello)
    ]
    return [(provider, modello), *resto]
