"""Stato di salute di tutti gli adapter, provati dal vivo.

Ogni adapter dichiara una `sample_route`: una tratta che quell'operatore serve
di sicuro. Se non torna nulla li', non e' una giornata senza corse, e' il parser
che si e' rotto. E' l'unico modo per accorgersene prima dell'utente.

    python scripts/check_providers.py
    python scripts/check_providers.py --only itabus --verbose
    python scripts/check_providers.py --save        # rigenera tutte le fixture
    python scripts/check_providers.py --tier 1      # solo i veloci
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timedelta

import _bootstrap  # noqa: F401  (path e codifica dell'uscita)

from app.geo.resolver import PlaceNotFound, get_resolver  # noqa: E402
from app.models import MODE_NODE_KINDS, Node  # noqa: E402
from app.orchestrator import circuit_breaker  # noqa: E402
from app.orchestrator.db import close_db  # noqa: E402
from app.providers import registry  # noqa: E402
from app.providers.base import NotServed, Provider, ProviderError, SearchContext  # noqa: E402
from app.providers.browser_pool import close_browser_pool  # noqa: E402
from app.providers.http_client import Blocked, close_http_client  # noqa: E402

STATUS_ICON = {
    "ok": "ok      ",
    "vuoto": "VUOTO   ",
    "bloccato": "BLOCCATO",
    "errore": "ERRORE  ",
    "saltato": "saltato ",
    "assente": "no rotta",
}


def pick_node(place, provider: Provider) -> Node | None:
    kinds = MODE_NODE_KINDS.get(provider.mode, frozenset())
    usable = [n for n in place.nodes if n.kind in kinds and provider.supports_node(n)]
    usable.sort(key=lambda n: (not n.is_main, -len(n.provider_ids)))
    return usable[0] if usable else None


async def check_one(
    provider: Provider, day: date, save: bool, verbose: bool
) -> tuple[str, int, int, str]:
    if not provider.sample_route:
        return "assente", 0, 0, "nessuna sample_route dichiarata"

    day = _sample_day(provider, day)
    ctx = SearchContext(date=day)

    # Se l'adapter ha un proprio catalogo di fermate, le sue vincono: il
    # controllo deve dire se l'adapter funziona, non se il nostro indice
    # geografico conosce un paese di trecento abitanti.
    try:
        own = await provider.sample_nodes(ctx)
    except Exception as exc:  # noqa: BLE001
        return "errore", 0, 0, f"catalogo dell'adapter non disponibile: {exc}"[:110]

    if own is not None:
        origin, destination = own
    else:
        resolver = get_resolver()
        try:
            origin_place = resolver.resolve(provider.sample_route[0], modes={provider.mode})
            dest_place = resolver.resolve(provider.sample_route[1], modes={provider.mode})
        except PlaceNotFound as exc:
            return "errore", 0, 0, f"localita' non risolta: {exc}"

        origin = pick_node(origin_place, provider)
        destination = pick_node(dest_place, provider)
        if origin is None or destination is None:
            return "errore", 0, 0, "nessuna fermata utilizzabile per la sample_route"

    if not provider.can_serve(origin, destination):
        return "errore", 0, 0, f"can_serve() nega {origin.name} -> {destination.name}"
    started = datetime.now()
    try:
        raw = await provider.fetch(origin, destination, ctx)
        legs = provider.parse(raw, origin, destination, ctx)
    except NotServed as exc:
        return "saltato", 0, _ms(started), str(exc)[:90]
    except Blocked as exc:
        return "bloccato", 0, _ms(started), str(exc)[:90]
    except ProviderError as exc:
        return "errore", 0, _ms(started), str(exc)[:90]
    except Exception as exc:  # noqa: BLE001
        return "errore", 0, _ms(started), f"{type(exc).__name__}: {exc}"[:90]

    elapsed = _ms(started)
    if save and legs:
        from try_provider import save_fixture

        args = argparse.Namespace(
            origin=provider.sample_route[0],
            destination=provider.sample_route[1],
            date=day,
            pax=1,
        )
        save_fixture(provider.id, args, origin, destination, raw)

    if verbose:
        for leg in sorted(legs, key=lambda item: item.depart)[:4]:
            price = f"{leg.fare.amount:7.2f}" if leg.fare else "      -"
            print(
                f"        {leg.depart:%H:%M}-{leg.arrive:%H:%M} {price}  "
                f"{leg.origin.name[:26]} -> {leg.destination.name[:26]} {leg.vehicle or ''}"
            )

    if not legs:
        return "vuoto", 0, elapsed, "nessuna corsa su una tratta che dovrebbe averne"
    return "ok", len(legs), elapsed, ""


def _ms(started: datetime) -> int:
    return int((datetime.now() - started).total_seconds() * 1000)


def _sample_day(provider: Provider, requested: date) -> date:
    """Il giorno su cui provare questo adapter.

    Se l'adapter dichiara la data in cui la sua tratta di prova e' stata vista
    funzionare, si usa quella: molti operatori regionali hanno una corsa al
    giorno o servizi stagionali, e provarli in una data qualunque li farebbe
    sembrare rotti. Quando quella data e' passata non serve piu' a niente e si
    torna al giorno richiesto: sara' il controllo stesso a dire che va rinnovata."""
    if not provider.sample_date:
        return requested
    try:
        pinned = date.fromisoformat(provider.sample_date)
    except ValueError:
        return requested
    return pinned if pinned >= date.today() else requested


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="controlla un solo adapter")
    parser.add_argument("--tier", type=int, help="solo gli adapter di questo livello")
    parser.add_argument("--save", action="store_true", help="rigenera le fixture")
    parser.add_argument("--verbose", action="store_true", help="mostra le prime corse")
    parser.add_argument(
        "--date",
        type=lambda v: datetime.strptime(v, "%Y-%m-%d").date(),
        default=date.today() + timedelta(days=14),
    )
    args = parser.parse_args()

    providers = sorted(registry.all_providers(), key=lambda p: (p.tier, p.mode.value, p.id))
    if args.only:
        providers = [p for p in providers if p.id == args.only]
    if args.tier:
        providers = [p for p in providers if p.tier == args.tier]
    if not providers:
        print("nessun adapter corrisponde ai filtri", file=sys.stderr)
        return 1

    print(f"data di prova: {args.date}   adapter: {len(providers)}\n")
    print(f"{'STATO':9} {'ADAPTER':16} {'MODO':6} {'T':2} {'GAMBE':>6} {'MS':>7}  TRATTA / DETTAGLIO")
    print("-" * 108)

    tally: dict[str, int] = {}
    for provider in providers:
        status, legs, elapsed, detail = await check_one(
            provider, args.date, args.save, args.verbose
        )
        tally[status] = tally.get(status, 0) + 1
        route = " -> ".join(provider.sample_route) if provider.sample_route else "-"
        used = _sample_day(provider, args.date)
        when = "" if used == args.date else f" [{used}]"
        print(
            f"{STATUS_ICON.get(status, status):9} {provider.id:16} {provider.mode.value:6} "
            f"{provider.tier:<2} {legs:>6} {elapsed:>7}  {route}{when}"
            + (f"   {detail}" if detail else "")
        )

    print("-" * 108)
    print("  ".join(f"{k}: {v}" for k, v in sorted(tally.items())))

    health = await circuit_breaker.snapshot()
    aperti = [h for h in health if h["open"]]
    if aperti:
        print("\ncircuiti aperti (l'adapter e' temporaneamente escluso dalle ricerche):")
        for h in aperti:
            print(f"   {h['provider']} [{h['scope']}] ancora {h['open_for_s']}s — {h['last_detail']}")
        print("   per riaprirli subito: POST /api/providers/reset")

    await close_http_client()
    await close_browser_pool()
    await close_db()
    # Un adapter rotto deve far fallire il comando, cosi' e' usabile in un hook.
    return 1 if tally.get("errore") or tally.get("vuoto") else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
