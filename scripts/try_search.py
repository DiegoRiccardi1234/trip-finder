"""Esegue una ricerca completa da riga di comando, senza avviare il server.

    python scripts/try_search.py Torino Matera 2026-08-14
    python scripts/try_search.py Torino Matera 2026-08-14 --bag --modes rail,bus
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timedelta

import _bootstrap  # noqa: F401  (path e codifica dell'uscita)

from app.models import BOOKABLE_MODES, Itinerary, Mode, SearchQuery, Weights  # noqa: E402
from app.orchestrator.db import close_db  # noqa: E402
from app.orchestrator.search_service import SearchService  # noqa: E402
from app.providers.browser_pool import close_browser_pool  # noqa: E402
from app.providers.http_client import close_http_client  # noqa: E402

MODE_ICON = {
    Mode.RAIL: "treno",
    Mode.BUS: "bus",
    Mode.AIR: "aereo",
    Mode.FERRY: "nave",
    Mode.TRANSFER: "trasf",
    Mode.WALK: "piedi",
}


def show(itinerary: Itinerary, index: int) -> None:
    offset = (itinerary.arrive.date() - itinerary.depart.date()).days
    suffix = f"+{offset}" if offset else ""
    modes = "/".join(MODE_ICON.get(mode, mode.value) for mode in itinerary.modes)
    print(
        f"\n{index:2}. {itinerary.depart:%a %d/%m %H:%M} -> {itinerary.arrive:%H:%M}{suffix}  "
        f"{itinerary.duration_min // 60}h{itinerary.duration_min % 60:02}  "
        f"{itinerary.cost.total:7.2f} EUR  [{modes}]  "
        f"punteggio {itinerary.score:.2f}"
    )
    print(
        f"    {itinerary.n_changes} cambi, {itinerary.n_tickets} biglietti"
        + (f"  ATTENZIONE: {', '.join(f.value for f in itinerary.flags)}" if itinerary.flags else "")
    )
    for leg in itinerary.legs:
        price = f"{leg.fare.amount:6.2f}" if leg.fare else "     -"
        marker = "  ~" if leg.is_transfer else "   "
        print(
            f"{marker} {leg.depart:%H:%M}-{leg.arrive:%H:%M} {price}  "
            f"{MODE_ICON.get(leg.mode, ''):5} {leg.origin.name[:28]:28} -> {leg.destination.name[:28]:28}"
            f" {leg.operator or ''} {leg.vehicle or ''}"
        )
    for line in itinerary.cost.lines:
        flag = " (stimato)" if line.estimated else ""
        print(f"       {line.amount:7.2f}  {line.label}{flag}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("origin")
    parser.add_argument("destination")
    parser.add_argument(
        "date",
        nargs="?",
        type=lambda v: datetime.strptime(v, "%Y-%m-%d").date(),
        default=date.today() + timedelta(days=14),
    )
    parser.add_argument("--pax", type=int, default=1)
    parser.add_argument("--bag", action="store_true")
    parser.add_argument("--modes", default=",".join(m.value for m in BOOKABLE_MODES))
    parser.add_argument("--max-changes", type=int, default=3)
    parser.add_argument("--top", type=int, default=8)
    args = parser.parse_args()

    query = SearchQuery(
        origin=args.origin,
        destination=args.destination,
        date=args.date,
        pax=args.pax,
        with_checked_bag=args.bag,
        modes={Mode(m.strip()) for m in args.modes.split(",") if m.strip()},
        max_changes=args.max_changes,
        weights=Weights(),
    )

    started = datetime.now()
    final: list[Itinerary] = []
    try:
        async for event in SearchService().run(query):
            if event.event == "plan":
                print(f"percorsi: {len(event.data['paths'])}, interrogazioni: {event.data['requests']}")
                for path in event.data["paths"]:
                    print(f"   - {path['label']}")
            elif event.event == "provider":
                report = event.data
                print(
                    f"   [{report.status.value:12}] {report.provider:12} "
                    f"{report.legs_found:3} gambe  {report.elapsed_ms:5} ms"
                    f"  {report.detail or ''}"
                )
            elif event.event == "itineraries" and not event.data["partial"]:
                final = event.data["items"]
            elif event.event == "error":
                print(f"ERRORE: {event.data['message']}", file=sys.stderr)
            elif event.event == "done":
                print(
                    f"\n=== {event.data['itineraries']} itinerari da "
                    f"{event.data['legs']} gambe in "
                    f"{(datetime.now() - started).total_seconds():.1f}s ==="
                )
    finally:
        await close_http_client()
        await close_browser_pool()
        await close_db()

    for index, itinerary in enumerate(final[: args.top], start=1):
        show(itinerary, index)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
