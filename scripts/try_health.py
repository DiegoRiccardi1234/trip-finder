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

from app.ai import client, endpoint_health, model_selector, providers  # noqa: E402
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

        usable = providers.configured()
        print("=== fornitori ===")
        for provider in providers.PROVIDERS:
            mark = "ok  " if provider in usable else "--  "
            kind = "gratuito" if provider.free else "a pagamento"
            note = provider.label or ("chiave assente" if provider not in usable else "")
            print(f"   {mark} {provider.name:12} {kind:12} {note}")
        print()

        for provider in usable:
            penalties = await model_selector.current_penalties(provider.name)
            if penalties:
                print(f"penalita' attive su {provider.name}:")
                for model, value in sorted(penalties.items(), key=lambda p: -p[1]):
                    print(f"   {value:5.1f}  {model}")
                print()

        for task in ("json", "advice"):
            print(f"=== compito: {task} ===")
            pool = model_selector.DEFAULT_POOLS[task]
            health = await endpoint_health.check_many(pool)
            for slug in pool:
                status = health.get(slug, endpoint_health.UNKNOWN)
                mark = "ok  " if status.alive else "MORTO"
                quality = model_selector.score_model_name(slug, task)
                hosts = ", ".join(status.providers[:2]) or "-"
                print(
                    f"   {mark} openrouter/{slug:44} up5m {status.uptime_5m:5.1f}%  "
                    f"qualita' {quality:+.1f}  {hosts}  {status.detail}"
                )
            order = [
                f"{provider.name}/{model}"
                for provider, model in await model_selector.rank_candidates(task)
            ]
            print(f"   ordine di prova: {' > '.join(order[:4]) or '(nessuno)'}\n")

        if not client.is_configured():
            print("Nessuna chiave impostata: l'IA resta disattivata.")
            print("Ne basta una qualsiasi fra quelle di .env.example.")
            print("Il sito funziona lo stesso, senza consigli e senza ricerca in")
            print("linguaggio naturale.")
            return 0

        if args.ask:
            print("=== chiamata di prova ===")
            answer = await client.complete(
                "json",
                "Rispondi solo con JSON.",
                'Restituisci {"ok": true} e nient\'altro.',
                max_tokens=40,
            )
            if answer.completion is None:
                print(f"   niente: {client.why(answer.reason)}")
                if answer.detail:
                    print(f"   dettaglio: {answer.detail}")
                return 1
            done = answer.completion
            print(f"   {done.provider}/{done.model}: {done.text[:120]}")
        return 0
    finally:
        await close_http_client()
        await close_db()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
