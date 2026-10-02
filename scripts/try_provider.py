"""Prova un singolo adapter dal vivo, senza avviare l'applicazione.

E' lo strumento di debug principale quando un parser si rompe.

    python scripts/try_provider.py trenitalia Torino Bari 2026-08-14
    python scripts/try_provider.py flixbus Torino Matera 2026-08-14 --save
    python scripts/try_provider.py --list

Con --save la risposta grezza finisce in tests/fixtures/<provider>/ e i test del
parser potranno girare su quella senza toccare la rete.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timedelta

import _bootstrap  # noqa: F401  (path e codifica dell'uscita)

from app.config import FIXTURES_DIR  # noqa: E402
from app.geo.resolver import get_resolver  # noqa: E402
from app.models import Mode, Node  # noqa: E402
from app.providers import registry  # noqa: E402
from app.providers.base import NotServed, ProviderError, SearchContext  # noqa: E402
from app.providers.http_client import Blocked, close_http_client  # noqa: E402
from app.orchestrator.db import close_db  # noqa: E402


def pick_node(place, provider) -> Node | None:
    """Primo nodo della localita' che l'adapter dichiara di poter usare."""
    for node in place.nodes:
        if provider.granularity == "city":
            if node.kind.value in {"bus_stop", "station"}:
                return node
        elif provider.native_id(node):
            return node
    return place.nodes[0] if place.nodes else None


async def run(args: argparse.Namespace) -> int:
    provider = registry.get(args.provider)
    resolver = get_resolver()

    origin_place = resolver.resolve(args.origin, modes={provider.mode})
    dest_place = resolver.resolve(args.destination, modes={provider.mode})

    origin = pick_node(origin_place, provider)
    destination = pick_node(dest_place, provider)
    if origin is None or destination is None:
        print("nessun nodo utilizzabile per questo provider", file=sys.stderr)
        return 1

    print(f"provider : {provider.id} ({provider.name}, tier {provider.tier})")
    print(f"origine  : {origin.name} [{origin.id}] {origin.provider_ids}")
    print(f"destin.  : {destination.name} [{destination.id}] {destination.provider_ids}")
    print(f"data     : {args.date}\n")

    if not provider.can_serve(origin, destination):
        print("can_serve() dice di no: l'adapter non copre questa coppia di fermate")
        return 1

    ctx = SearchContext(date=args.date, pax=args.pax)
    started = datetime.now()
    try:
        await provider.prepare_parse(ctx)
        raw = await provider.fetch(origin, destination, ctx)
        legs = provider._solo_coerenti(provider.parse(raw, origin, destination, ctx), origin, destination)
    except NotServed as exc:
        print(f"non servita: {exc}")
        return 0
    except Blocked as exc:
        print(f"BLOCCATO dall'anti-bot: {exc}", file=sys.stderr)
        return 2
    except ProviderError as exc:
        print(f"ERRORE adapter: {exc}", file=sys.stderr)
        return 2

    elapsed = (datetime.now() - started).total_seconds()
    print(f"{len(legs)} gambe in {elapsed:.1f}s\n")
    for leg in sorted(legs, key=lambda item: item.depart):
        price = f"{leg.fare.amount:7.2f} EUR" if leg.fare else "      n/d"
        changes = f" +{leg.internal_changes} cambi" if leg.internal_changes else ""
        print(
            f"  {leg.depart:%H:%M}-{leg.arrive:%H:%M} "
            f"{leg.duration_min // 60:2}h{leg.duration_min % 60:02}  {price}  "
            f"{leg.operator or ''} {leg.vehicle or ''}{changes}"
        )
        print(f"        {leg.origin.name} -> {leg.destination.name}")
        for note in leg.notes:
            print(f"        nota: {note}")

    if args.save:
        save_fixture(provider.id, args, origin, destination, raw)
    return 0


def save_fixture(provider_id, args, origin: Node, destination: Node, raw) -> None:
    """Congela la risposta grezza piu' il contesto che serve a rigiocarla.

    Salvare le gambe gia' interpretate non servirebbe a niente: il test deve
    poter verificare proprio il passaggio da risposta grezza a gambe."""
    import orjson

    target = FIXTURES_DIR / provider_id
    target.mkdir(parents=True, exist_ok=True)
    slug = f"{args.origin}-{args.destination}".replace(" ", "_").lower()
    path = target / f"{slug}.json"
    payload = {
        "captured_for": {
            "origin_query": args.origin,
            "destination_query": args.destination,
            "date": args.date.isoformat(),
            "pax": args.pax,
        },
        "origin_node": origin.model_dump(mode="json"),
        "destination_node": destination.model_dump(mode="json"),
        "raw": raw,
    }
    path.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
    print(f"\nfixture salvata: {path}")


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", nargs="?", help="id dell'adapter")
    parser.add_argument("origin", nargs="?", help="localita' di partenza")
    parser.add_argument("destination", nargs="?", help="localita' di arrivo")
    parser.add_argument(
        "date",
        nargs="?",
        type=parse_date,
        default=date.today() + timedelta(days=14),
        help="AAAA-MM-GG (default: fra due settimane)",
    )
    parser.add_argument("--pax", type=int, default=1)
    parser.add_argument("--save", action="store_true", help="salva la fixture")
    parser.add_argument("--list", action="store_true", help="elenca gli adapter")
    args = parser.parse_args()

    if args.list or not args.provider:
        for provider in sorted(registry.all_providers(), key=lambda p: (p.mode.value, p.id)):
            print(
                f"{provider.id:16} {provider.mode.value:6} tier{provider.tier} "
                f"{provider.granularity:5} {provider.name}"
            )
        return 0

    if not args.origin or not args.destination:
        parser.error("servono origine e destinazione")

    try:
        return await run(args)
    finally:
        await close_http_client()
        await close_db()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
