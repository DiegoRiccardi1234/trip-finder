"""Stato di salute di tutti gli adapter, provati dal vivo.

Ogni adapter dichiara una `sample_route`: una tratta che quell'operatore serve
di sicuro. Se non torna nulla li', non e' una giornata senza corse, e' il parser
che si e' rotto. E' l'unico modo per accorgersene prima dell'utente.

Quella conclusione vale pero' **solo nel giorno che l'adapter ha pinnato** con
`sample_date`. Passata quella data si prova in un giorno qualunque, e per un
operatore stagionale o con una corsa al giorno `VUOTO` torna a essere ambiguo:
per questo la riga lo dice («pin scaduto»), e per questo, quando non esce
nessuna gamba, il dettaglio riporta **quanto pesava la risposta grezza**. Zero
elementi e' l'operatore che tace; dodici elementi e nessuna gamba e' il parser.

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
        # **Quanto pesava la risposta grezza**, che e' l'unica cosa capace di
        # separare i due guasti che oggi si chiamano tutti e due `VUOTO`:
        # l'operatore che non ha corse quel giorno, e il parser che non legge
        # piu' quello che l'operatore manda. Senza questo numero il rapporto
        # afferma una causa che non ha misurato, e quella riga e' esattamente
        # il posto in cui si guarda quando qualcosa si e' rotto in silenzio.
        return "vuoto", 0, elapsed, f"nessuna corsa: {_dimensione(raw)}"
    return "ok", len(legs), elapsed, ""


def _dimensione(raw: object) -> str:
    """Quanto materiale ha risposto l'operatore, in una frase.

    Volutamente generica: ogni adapter ha la sua forma grezza — chi una lista,
    chi un dict, chi HTML — e qui non si vuole sapere cosa contiene, solo se
    c'era qualcosa da leggere."""
    if raw is None:
        return "risposta assente"
    if isinstance(raw, list):
        return f"la risposta portava {len(raw)} elementi"
    if isinstance(raw, dict):
        pieni = [chiave for chiave, valore in raw.items() if valore]
        return f"la risposta portava {len(raw)} chiavi, {len(pieni)} non vuote"
    if isinstance(raw, (str, bytes)):
        return f"la risposta portava {len(raw)} caratteri"
    return f"la risposta era un {type(raw).__name__}"


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


def _pin_scaduto(provider: Provider) -> bool:
    """Se questo adapter dichiara una data di prova che e' gia' passata.

    Serve a dire ad alta voce quello che `_sample_day` fa in silenzio: da quel
    momento l'adapter viene provato in un giorno qualunque, che e' esattamente
    la condizione in cui il commento della sua tabella avverte che sembrerebbe
    rotto senza esserlo."""
    if not provider.sample_date:
        return False
    try:
        return date.fromisoformat(provider.sample_date) < date.today()
    except ValueError:
        return False


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
        # Tre righe di codice per una bugia per omissione. La data si stampava
        # solo quando **differiva** da quella chiesta, cioe' solo finche' il pin
        # era valido: appena scade, `_sample_day` torna la data richiesta, la
        # colonna resta vuota e l'unico segnale e' un `VUOTO` indistinguibile da
        # un parser rotto. Il codice diceva in tre punti che il controllo
        # avrebbe segnalato la scadenza, e non lo faceva.
        when = "" if used == args.date else f" [{used}]"
        if _pin_scaduto(provider):
            when += f" [pin scaduto il {provider.sample_date}]"
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
