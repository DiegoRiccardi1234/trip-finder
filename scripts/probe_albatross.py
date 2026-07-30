"""Scopre nuovi operatori della piattaforma Albatross e ne verifica una tratta.

Aggiungere un operatore ad `app/providers/bus/albatross.py` costa una riga, ma
quella riga ha bisogno di due cose che non si inventano: **l'host dell'API** e
**una tratta che quell'operatore serve davvero**, con la data in cui e' stata
vista funzionare. Questo script trova entrambe.

Come trova gli host. Il bundle del motore di prenotazione non elenca piu' gli
altri operatori (la versione 8.4 legge `serverUrl` da `apiUrl.js`, che e' per
installazione). Si passa allora dai dati aperti della piattaforma stessa:

    GET {base}/Carriers   anagrafica dei vettori venduti da quell'installazione,
                          con email e PEC: il dominio dell'email e' il dominio
                          del sito, e da li' si tentano gli host consueti
                          `booking.<dominio>/apiUrl.js`, `api.<dominio>`,
                          `albatrossapi.<dominio>`.

`apiUrl.js` e' la conferma piu' pulita: dichiara `serverUrl`, `homepage` e
`environmentReadableName`, cioe' esattamente i campi della riga da scrivere.

Come trova la tratta. Provare coppie di localita' a caso e' inefficiente: un
operatore con trecento fermate ha decine di migliaia di combinazioni e ne serve
poche. Invece:

    GET {base}/Lines      le linee dell'operatore, con la descrizione che quasi
                          sempre contiene i capolinea ("Matera-Potenza-Roma").

Si incrociano quei nomi con il catalogo fermate e si prova la coppia. Le linee
urbane e scolastiche (`type` 4) si saltano: non sono vendibili al pubblico.

    python scripts/probe_albatross.py                    scoperta + sondaggio
    python scripts/probe_albatross.py --host api.tal.it  sonda un host solo
    python scripts/probe_albatross.py --discover-only    solo la ricerca host
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta

import _bootstrap  # noqa: F401  (path e codifica dell'uscita)

from curl_cffi.requests import AsyncSession  # noqa: E402

from app.providers.bus.albatross import (  # noqa: E402
    SEARCH_BODY,
    SEARCH_QUERY,
    OPERATORS,
)

#: Domini di posta che non sono il sito di nessun operatore.
GENERIC_MAIL_DOMAINS = frozenset({
    "pec.it", "legalmail.it", "libero.it", "gmail.com", "virgilio.it",
    "alice.it", "tiscali.it", "hotmail.it", "hotmail.com", "yahoo.it",
    "yahoo.com", "outlook.it", "outlook.com", "arubapec.it", "pecimprese.it",
    "postecert.it", "registerpec.it", "ticertifica.it", "sicurezzapostale.it",
    "cgn.legalmail.it", "pec.confartigianato.it", "pec.aruba.it", "email.it",
    "fastwebnet.it", "inwind.it", "tin.it", "italiamail.it", "cert.legalmail.it",
})

#: Prefissi con cui la piattaforma pubblica l'API, in ordine di frequenza.
API_PREFIXES = ("api", "albatrossapi", "albatross", "booking")

#: Operatori che risultano sulla piattaforma ma che nessun `/Carriers` nomina,
#: perche' non vendono l'uno i biglietti dell'altro. Sono partenze per la
#: ricerca, non host: se il dominio non ha un'installazione, cadono da soli.
SEED_DOMAINS = (
    "interbus.it", "atmmolise.it", "sitbusshuttle.it", "bristolautolinee.it",
    "autolineegobbo.it", "salinabus.it", "romalinee.it", "linea2mari.it",
    "zappalatorrisi.it", "carcoemaugeri.it", "autolineecurcio.it",
    "capuautolinee.it", "flixbus.it", "buscenter.it", "autolineepetruzzi.it",
    "trasportiautolineepugliesi.it", "cotrap.it", "sitasudtrasporti.it",
    "autolineeromano.it", "chiaravallebus.it", "autoservizicerella.it",
    "ferrovieappulolucane.it", "miccolis-spa.it", "sitabus.it",
)

#: Linee di tipo 4 sono urbane o scolastiche: non si vendono a chi viaggia.
SKIP_LINE_TYPES = frozenset({4})

CONCURRENCY = 8
DISCOVERY_TIMEOUT = 12
PROBE_TIMEOUT = 35


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = "".join(ch if ch.isalnum() else " " for ch in stripped.lower())
    return " ".join(cleaned.split())


def known_hosts() -> dict[str, str]:
    """Host gia' presenti in `OPERATORS`, per id."""
    return {row[2].rstrip("/").replace("https://", "").replace("http://", ""): row[0]
            for row in OPERATORS}


# --------------------------------------------------------------- scoperta host


def domains_from_carriers(carriers: list[dict]) -> set[str]:
    """Domini plausibili di un sito, ricavati dalle email dei vettori."""
    domains: set[str] = set()
    for carrier in carriers:
        if not isinstance(carrier, dict):
            continue
        for field in ("email", "pec"):
            value = str(carrier.get(field) or "").strip().lower()
            if "@" not in value:
                continue
            domain = value.rsplit("@", 1)[1].strip(" .;,")
            if not domain or domain in GENERIC_MAIL_DOMAINS:
                continue
            if domain.startswith("pec.") or ".pec." in domain:
                domain = domain[4:] if domain.startswith("pec.") else domain
            if domain.count(".") >= 1 and domain not in GENERIC_MAIL_DOMAINS:
                domains.add(domain)
        # Anche l'identificatore del vettore e' spesso il dominio del sito.
        slug = str(carrier.get("id") or "").strip().lower()
        if slug.isalnum() and 4 <= len(slug) <= 24:
            domains.add(f"{slug}.it")
            domains.add(f"{slug}.com")
    return domains


async def fetch_json(session: AsyncSession, url: str, timeout: int):
    response = await session.get(
        url, headers={"Accept": "application/json"}, timeout=timeout
    )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")
    return response.json()


async def collect_carrier_domains(session: AsyncSession, hosts: list[str]) -> set[str]:
    """Unione dei domini ricavati dai `/Carriers` degli host gia' noti."""
    gate = asyncio.Semaphore(CONCURRENCY)
    domains: set[str] = set()

    async def one(host: str) -> None:
        async with gate:
            try:
                carriers = await fetch_json(
                    session, f"https://{host}/Carriers", DISCOVERY_TIMEOUT
                )
            except Exception:  # noqa: BLE001 - un host muto non blocca la scoperta
                return
        if isinstance(carriers, list):
            found = domains_from_carriers(carriers)
            domains.update(found)
            print(f"   {host:44} {len(carriers):3} vettori -> {len(found)} domini")

    await asyncio.gather(*(one(host) for host in hosts))
    return domains


async def resolve_installation(session: AsyncSession, domain: str) -> dict | None:
    """Cerca l'installazione Albatross di un dominio.

    `apiUrl.js` e' la fonte migliore: dichiara l'host dell'API, la home e il
    nome commerciale. Se non c'e', si tentano gli host API consueti."""
    for prefix in ("booking", "shop", "biglietti", "www"):
        url = f"https://{prefix}.{domain}/apiUrl.js"
        try:
            response = await session.get(url, timeout=DISCOVERY_TIMEOUT)
        except Exception:  # noqa: BLE001
            continue
        if response.status_code != 200 or "serverUrl" not in response.text:
            continue
        match = re.search(r"window\.apiUrl\s*=\s*(\{.*?\});", response.text, re.S)
        if not match:
            continue
        try:
            config = json.loads(match.group(1))
        except ValueError:
            continue
        server = str(config.get("serverUrl") or "").strip()
        if not server:
            continue
        return {
            "api_base": server if server.endswith("/") else server + "/",
            "booking_base": f"https://{prefix}.{domain}/",
            "website": str(config.get("homepage") or f"https://www.{domain}/"),
            "name": str(config.get("environmentReadableName") or domain),
            "env": str(config.get("environmentName") or ""),
            "source": "apiUrl.js",
        }

    for prefix in API_PREFIXES:
        base = f"https://{prefix}.{domain}/"
        try:
            entries = await fetch_json(session, base + "Stops", DISCOVERY_TIMEOUT)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(entries, list) and entries and isinstance(entries[0], dict):
            if entries[0].get("id"):
                return {
                    "api_base": base,
                    "booking_base": "",
                    "website": f"https://www.{domain}/",
                    "name": domain,
                    "env": "",
                    "source": "Stops",
                }
    return None


async def discover(session: AsyncSession, rounds: int = 3) -> list[dict]:
    """Installazioni Albatross non ancora presenti in `OPERATORS`.

    La ricerca e' transitiva: ogni installazione trovata ha il suo `/Carriers`,
    che nomina vettori che a loro volta possono avere un'installazione. Si
    ripete finche' un giro non aggiunge piu' niente, perche' gli operatori si
    conoscono a catena e non tutti compaiono nel primo anello."""
    known = known_hosts()
    print(f"host gia' noti: {len(known)}")

    hosts_to_mine = sorted(known)
    tried_domains: set[str] = set()
    found: list[dict] = []
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(domain: str) -> dict | None:
        async with gate:
            try:
                installation = await resolve_installation(session, domain)
            except Exception:  # noqa: BLE001
                return None
        if installation is None:
            return None
        host = installation["api_base"].replace("https://", "").rstrip("/")
        if host in known or any(f["api_base"] == installation["api_base"] for f in found):
            return None
        installation["domain"] = domain
        print(f"   TROVATO {domain:34} -> {installation['api_base']} "
              f"({installation['source']}, {installation['name']})")
        return installation

    extra_seeds = set(SEED_DOMAINS)
    for round_number in range(1, rounds + 1):
        print(f"\n--- giro {round_number}: /Carriers di {len(hosts_to_mine)} host ---")
        domains = await collect_carrier_domains(session, hosts_to_mine)
        domains |= extra_seeds
        extra_seeds = set()
        known_domains = {host.split(".", 1)[1] for host in known}
        candidates = sorted(domains - known_domains - tried_domains)
        tried_domains.update(candidates)
        print(f"domini candidati nuovi: {len(candidates)}")
        if not candidates:
            break

        results = await asyncio.gather(*(one(domain) for domain in candidates))
        fresh = [r for r in results if r is not None]
        found.extend(fresh)
        if not fresh:
            break
        hosts_to_mine = [f["api_base"].replace("https://", "").rstrip("/") for f in fresh]

    return found


# ------------------------------------------------------------ tratta di prova


def catalog_from(entries: list[dict]) -> dict:
    stops: dict[str, dict] = {}
    localities: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        locality = entry.get("localityShortDescription") or ""
        stops[str(entry["id"])] = {"locality": locality}
        if locality:
            localities.setdefault(_normalize(str(locality)), str(locality))
    return {"stops": stops, "localities": localities, "count": len(stops)}


def pairs_from_lines(lines: list[dict], localities: dict[str, str]) -> list[tuple[str, str]]:
    """Coppie di capolinea ricavate dalle descrizioni delle linee attive.

    La descrizione di una linea e' scritta da un umano ("Matera-Potenza-Roma",
    "ROMA - AVELLINO 2^ corsa"): i nomi che compaiono anche nel catalogo fermate
    sono capolinea o fermate intermedie vere, e una coppia presa di li' ha
    corse molto piu' spesso di due localita' scelte a caso."""
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for line in lines:
        if not isinstance(line, dict) or line.get("dismissed"):
            continue
        if line.get("type") in SKIP_LINE_TYPES:
            continue
        description = str(line.get("description") or "")
        tokens = [t.strip() for t in re.split(r"[-–—/>|]+", description) if t.strip()]
        names: list[str] = []
        for token in tokens:
            key = _normalize(token)
            if key in localities:
                names.append(localities[key])
                continue
            # "Linea int. Sicignano Napoli-Roma": il token porta parole in piu'.
            words = key.split()
            for size in (3, 2, 1):
                for start in range(0, max(1, len(words) - size + 1)):
                    candidate = " ".join(words[start:start + size])
                    if candidate in localities and localities[candidate] not in names:
                        names.append(localities[candidate])
                        break
                else:
                    continue
                break
        for first, second in zip(names, names[1:]):
            key = (first, second)
            if first != second and key not in seen:
                seen.add(key)
                pairs.append(key)
        if len(names) > 2:
            key = (names[0], names[-1])
            if key not in seen and names[0] != names[-1]:
                seen.add(key)
                pairs.append(key)
    return pairs


def pairs_from_catalog(entries: list[dict], limit: int = 6) -> list[tuple[str, str]]:
    """Ripiego: le localita' con piu' fermate, incrociate fra loro."""
    counts: dict[str, int] = {}
    for entry in entries:
        locality = str((entry or {}).get("localityShortDescription") or "")
        if locality:
            counts[locality] = counts.get(locality, 0) + 1
    top = [name for name, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:limit]]
    return [(a, b) for a in top for b in top if a != b]


async def try_route(
    session: AsyncSession, base: str, origin: str, destination: str,
    start: date, end: date,
) -> list[dict]:
    url = f"{base}search/s/{origin}/{destination}/{start.isoformat()}/{end.isoformat()}"
    response = await session.post(
        url,
        params=dict(SEARCH_QUERY),
        json=SEARCH_BODY,
        headers={"Accept": "application/json"},
        timeout=PROBE_TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")
    payload = response.json()
    return payload if isinstance(payload, list) else []


def solution_dates(solutions: list[dict]) -> list[str]:
    days: list[str] = []
    for solution in solutions:
        trips = [t for t in (solution.get("trips") or []) if isinstance(t, dict)]
        if not trips:
            continue
        raw = trips[0].get("departureDateTime")
        if isinstance(raw, str) and len(raw) >= 10:
            day = raw[:10]
            if day not in days:
                days.append(day)
    return sorted(days)


async def probe(
    session: AsyncSession, installation: dict, start: date, end: date, attempts: int,
    verbose: bool = False,
) -> dict:
    """Trova una tratta con corse vere per una installazione."""
    base = installation["api_base"]
    result = dict(installation)
    try:
        entries = await fetch_json(session, base + "Stops", PROBE_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        result["status"] = f"catalogo non disponibile: {exc}"
        return result
    if not isinstance(entries, list) or not entries:
        result["status"] = "catalogo vuoto"
        return result

    catalog = catalog_from(entries)
    result["stops"] = catalog["count"]
    result["localities"] = len(catalog["localities"])

    try:
        lines = await fetch_json(session, base + "Lines", PROBE_TIMEOUT)
    except Exception:  # noqa: BLE001 - senza linee si passa al ripiego
        lines = []
    candidates = pairs_from_lines(lines if isinstance(lines, list) else [],
                                  catalog["localities"])
    candidates += [p for p in pairs_from_catalog(entries) if p not in candidates]
    result["candidates"] = len(candidates)

    if verbose:
        live_lines = [
            str(l.get("description"))
            for l in (lines if isinstance(lines, list) else [])
            if isinstance(l, dict) and not l.get("dismissed")
            and l.get("type") not in SKIP_LINE_TYPES
        ]
        print(f"\n   [{base}] {len(live_lines)} linee vendibili, "
              f"{len(candidates)} coppie candidate")
        for description in live_lines[:12]:
            print(f"      linea: {description}")
        for pair in candidates[:attempts]:
            print(f"      coppia: {pair[0]} -> {pair[1]}")

    for origin, destination in candidates[:attempts]:
        try:
            solutions = await try_route(session, base, origin, destination, start, end)
        except Exception as exc:  # noqa: BLE001 - una coppia muta non e' un guasto
            if verbose:
                print(f"      {origin} -> {destination}: {exc}")
            continue
        if verbose:
            print(f"      {origin} -> {destination}: {len(solutions)} soluzioni")
        usable = [
            s for s in solutions
            if isinstance(s, dict) and s.get("trips")
            and not any((t or {}).get("canceled") for t in s["trips"])
        ]
        if not usable:
            continue
        days = solution_dates(usable)
        result["status"] = "ok"
        result["route"] = (origin, destination)
        result["dates"] = days
        result["solutions"] = len(usable)
        return result

    result["status"] = f"nessuna corsa su {min(len(candidates), attempts)} coppie provate"
    return result


def operator_row(result: dict, preferred_day: str) -> str:
    """La riga da incollare in `OPERATORS`."""
    origin, destination = result["route"]
    days = result.get("dates") or []
    day = preferred_day if preferred_day in days else (days[0] if days else preferred_day)
    slug = re.sub(r"[^a-z0-9]", "", (result.get("env") or result["domain"].split(".")[0]).lower())
    booking = result.get("booking_base") or ""
    extra = "" if day == preferred_day else f",\n     \"{day}\""
    return (
        f'    ("{slug}", "{result["name"]}", "{result["api_base"]}",\n'
        f'     "{booking}", "{result["website"]}", Mode.BUS,\n'
        f'     ("{origin}", "{destination}"){extra}),'
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", action="append", help="sonda solo questo host API")
    parser.add_argument("--discover-only", action="store_true", help="solo ricerca host")
    parser.add_argument("--attempts", type=int, default=14,
                        help="coppie da provare per operatore")
    parser.add_argument("--days", type=int, default=8,
                        help="ampiezza in giorni della finestra di ricerca")
    parser.add_argument(
        "--from",
        dest="start",
        type=lambda v: datetime.strptime(v, "%Y-%m-%d").date(),
        default=date.today() + timedelta(days=14),
    )
    parser.add_argument("--prefer", default="", help="giorno da preferire per il sample")
    parser.add_argument("--rounds", type=int, default=3,
                        help="giri di scoperta transitiva sui /Carriers")
    parser.add_argument("--verbose", action="store_true",
                        help="mostra linee, coppie provate e risposte")
    args = parser.parse_args()

    start = args.start
    end = start + timedelta(days=args.days)
    preferred = args.prefer or start.isoformat()

    async with AsyncSession(impersonate="chrome", timeout=PROBE_TIMEOUT) as session:
        if args.host:
            installations = [
                {
                    "api_base": h if h.endswith("/") else h + "/",
                    "booking_base": "",
                    "website": "",
                    "name": h,
                    "env": "",
                    "domain": h.replace("https://", "").split(".", 1)[-1],
                    "source": "manuale",
                }
                for h in (x if x.startswith("http") else f"https://{x}" for x in args.host)
            ]
        else:
            installations = await discover(session, rounds=args.rounds)
            print(f"\ninstallazioni nuove trovate: {len(installations)}")
            if args.discover_only:
                for item in sorted(installations, key=lambda i: i["domain"]):
                    print(f"  {item['domain']:34} {item['api_base']:46} {item['name']}")
                return 0

        if not installations:
            print("niente da sondare")
            return 0

        print(f"\nsondaggio su {start} .. {end}, {args.attempts} coppie per operatore\n")
        gate = asyncio.Semaphore(4)

        async def run(item: dict) -> dict:
            async with gate:
                return await probe(session, item, start, end, args.attempts, args.verbose)

        results = await asyncio.gather(*(run(item) for item in installations))

    ok = [r for r in results if r.get("status") == "ok"]
    ko = [r for r in results if r.get("status") != "ok"]

    print(f"{'ESITO':8} {'HOST':46} {'FERMATE':>8}  TRATTA / MOTIVO")
    print("-" * 110)
    for result in sorted(results, key=lambda r: (r.get("status") != "ok", r["api_base"])):
        host = result["api_base"].replace("https://", "").rstrip("/")
        stops = result.get("stops", 0)
        if result.get("status") == "ok":
            origin, destination = result["route"]
            days = ", ".join(result.get("dates", [])[:3])
            print(f"{'ok':8} {host:46} {stops:>8}  {origin} -> {destination}  [{days}]")
        else:
            print(f"{'-':8} {host:46} {stops:>8}  {result.get('status')}")

    print(f"\nfunzionanti: {len(ok)}   senza tratta: {len(ko)}")
    if ok:
        print("\nrighe da aggiungere a OPERATORS in app/providers/bus/albatross.py:\n")
        for result in sorted(ok, key=lambda r: r["domain"]):
            print(operator_row(result, preferred))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
