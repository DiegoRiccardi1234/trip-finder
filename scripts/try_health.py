"""Diagnostica della selezione modelli OpenRouter.

Il controllo di salute usa `GET /models/{slug}/endpoints`, che e' gratuito, non
richiede autenticazione e non esegue inferenza: si puo' lanciare quanto si vuole
senza intaccare la quota giornaliera.

    python scripts/try_health.py
    python scripts/try_health.py --ask     # prova davvero una chiamata
"""

from __future__ import annotations

import argparse
import asyncio

import _bootstrap  # noqa: F401  (path e codifica dell'uscita)

from app.ai import client, endpoint_health, model_selector  # noqa: E402
from app.orchestrator.db import close_db  # noqa: E402
from app.providers.http_client import close_http_client  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ask", action="store_true", help="prova una chiamata vera")
    parser.add_argument("--reset", action="store_true", help="azzera le penalita'")
    args = parser.parse_args()

    try:
        if args.reset:
            await model_selector.clear_penalties()
            print("penalita' azzerate\n")

        penalties = await model_selector.current_penalties()
        if penalties:
            print("penalita' attive:")
            for model, value in sorted(penalties.items(), key=lambda p: -p[1]):
                print(f"   {value:5.1f}  {model}")
            print()

        for task in ("json", "advice"):
            pool = model_selector.DEFAULT_POOLS[task]
            print(f"=== compito: {task} ===")
            health = await endpoint_health.check_many(pool)
            for slug in pool:
                status = health.get(slug, endpoint_health.UNKNOWN)
                mark = "ok  " if status.alive else "MORTO"
                quality = model_selector.score_model_name(slug, task)
                providers = ", ".join(status.providers[:2]) or "-"
                print(
                    f"   {mark} {slug:52} up5m {status.uptime_5m:5.1f}%  "
                    f"qualita' {quality:+.1f}  {providers}  {status.detail}"
                )
            order = await model_selector.rank_models(task)
            print(f"   ordine di prova: {' > '.join(order[:4])}\n")

        if not client.is_configured():
            print("OPENROUTER_API_KEY non impostata: l'IA resta disattivata.")
            print("Il sito funziona lo stesso, senza consigli e senza ricerca in")
            print("linguaggio naturale.")
            return 0

        if args.ask:
            print("=== chiamata di prova ===")
            completion = await client.complete(
                "json",
                "Rispondi solo con JSON.",
                'Restituisci {"ok": true} e nient\'altro.',
                max_tokens=40,
            )
            if completion is None:
                print("   nessun modello ha risposto")
                return 1
            print(f"   {completion.model}: {completion.text[:120]}")
        return 0
    finally:
        await close_http_client()
        await close_db()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
