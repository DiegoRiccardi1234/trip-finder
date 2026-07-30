"""Diagnostica della risoluzione geografica.

    python scripts/try_resolve.py Torino Matera "Bari Centrale" BRI
"""

from __future__ import annotations

import sys
import time

import _bootstrap  # noqa: F401  (path e codifica dell'uscita)

from app.geo.resolver import PlaceNotFound, get_resolver  # noqa: E402
from app.models import haversine_km  # noqa: E402


def main(queries: list[str]) -> int:
    started = time.perf_counter()
    resolver = get_resolver()
    print(f"indice caricato in {time.perf_counter() - started:.2f}s\n")

    failures = 0
    for query in queries:
        try:
            place = resolver.resolve(query)
        except PlaceNotFound as exc:
            print(f"{query!r}: NON RISOLTO -> {exc}\n")
            failures += 1
            continue

        print(f"{query!r} -> {place.label}  ({place.lat:.4f}, {place.lon:.4f}) {place.country or '?'}")
        for node in place.nodes:
            distance = haversine_km(place.lat, place.lon, node.lat, node.lon)
            providers = ",".join(sorted(node.provider_ids)) or "-"
            main = "*" if node.is_main else " "
            print(f"   {main} [{node.kind.value:9}] {node.name[:42]:42} {distance:6.1f} km  {providers}")
        print()

    return 1 if failures else 0


if __name__ == "__main__":
    args = sys.argv[1:] or ["Torino", "Matera", "Bari", "Milano", "BRI"]
    raise SystemExit(main(args))
