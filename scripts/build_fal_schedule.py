"""Estrae l'orario delle Ferrovie Appulo Lucane dai manifesti ufficiali in PDF.

FAL e' l'unico servizio ferroviario che arriva a Matera, e non pubblica nessuna
API: solo dei PDF sul proprio sito. Questo script li scarica, li interpreta e
scrive `app/providers/rail/data/fal_schedule.json`, che l'adapter legge.

L'interpretazione **non** usa il rilevamento tabelle di pdfplumber: su questi
manifesti le celle risultano fuse e disallineate. Si lavora sulle posizioni dei
singoli caratteri: le ore che stanno alla stessa ascissa appartengono allo
stesso treno, quelle sulla stessa ordinata alla stessa stazione. E' il modo in
cui il documento e' fatto davvero.

Il file prodotto e' versionato apposta: se un giorno il parsing sbaglia, la
differenza si vede nel diff invece di finire in silenzio nei risultati.

    python scripts/build_fal_schedule.py
    python scripts/build_fal_schedule.py --dump   # mostra cosa ha capito
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import _bootstrap  # noqa: F401  (path e codifica dell'uscita)

import orjson  # noqa: E402

from app.config import DATA_DIR, ROOT  # noqa: E402
from app.providers.rail.fal import price_for  # noqa: E402

PDF_URL = (
    "https://ferrovieappulolucane.it/wp-content/uploads/2026/08/"
    "ORARIO-TRENI-DA-E-PER-BA-DAL-31-AGO-2026-1.pdf"
)
TIMETABLE_PAGE = "https://ferrovieappulolucane.it/tratta/orari/"
OUTPUT = ROOT / "app" / "providers" / "rail" / "data" / "fal_schedule.json"

#: Il nome del PDF tariffe porta la data dell'ultimo aggiornamento, quindi
#: cambia: si cerca sulla pagina degli orari invece di fissarlo qui.
TARIFF_HINT = "TARIFFE"
TARIFF_FALLBACK = (
    "https://ferrovieappulolucane.it/wp-content/uploads/2026/03/"
    "TARIFFE-PUGLIA-E-BASILICATA-09-MAR-2026-1.pdf"
)

TIME_RE = re.compile(r"^([0-9]{1,2})[.,]([0-9]{2})$")
#: Le intestazioni di sezione dicono il verso di marcia.
SECTION_FROM_BARI = "PARTENZE DA BARI"
SECTION_TO_BARI = "PARTENZE PER BARI"

#: Tolleranza in punti per considerare due ore incolonnate sotto lo stesso treno.
COLUMN_TOLERANCE = 14.0
#: Tolleranza in punti per considerare due parole sulla stessa riga.
ROW_TOLERANCE = 4.0

#: Soglie di plausibilita'. Se il risultato non le rispetta lo script fallisce e
#: non scrive niente: meglio nessun orario che un orario inventato.
MIN_RUNS_PER_DIRECTION = 8
MIN_STOPS_PER_RUN = 3
REQUIRED_STATIONS = ("bari", "matera", "altamura")
#: Bari-Matera si percorre in circa due ore e quaranta, ma la stessa linea
#: prosegue per Potenza e li' si arriva a quattro ore. La soglia serve solo a
#: intercettare due treni diversi finiti nella stessa colonna, caso in cui le
#: durate risultano assurde (sei ore o piu'), quindi si tiene larga.
MAX_RUN_MINUTES = 300

# ------------------------------------------------------------------- tariffe

#: Le stazioni come sono scritte nella tabella delle distanze, tradotte nei
#: nodi del progetto. Nel listino Bari e' solo "BARI", senza "C.le".
TARIFF_STATIONS: tuple[tuple[str, str], ...] = (
    ("matera c le", "ov:fal-matera-centrale"),
    ("matera villa longo", "ov:fal-matera-villa-longo"),
    ("altamura", "ov:fal-altamura"),
    ("gravina", "ov:fal-gravina"),
    ("bari", "ov:fal-bari-centrale"),
)

PRICE_RE = re.compile(r"^\d+,\d{2}$")
INT_RE = re.compile(r"^\d{1,3}$")

#: Parole che appartengono alle intestazioni del modulo, non a una stazione.
#: Senza questo filtro la scaletta della tabella si aggancia a "REGIONE PUGLIA"
#: e a "MOD. 07 TARIFFE" (che porta con se' un 07 scambiato per distanza), e da
#: li' in poi tutti gli indici scalano di due: Bari sparisce e le distanze
#: finiscono attribuite alla stazione sbagliata.
HEADING_WORDS = frozenset({
    "regione", "tariffe", "trasporto", "tabella", "mod", "pubblico", "fasce",
    "abbonamento", "tassazione", "forze", "maggiorazione", "ferrovie",
    "numero", "www", "speciali", "ordinarie", "distanze", "vigore", "km",
    "interregionale", "verde", "settimanale", "mensile", "corsa", "semplice",
})

#: Controlli di plausibilita' del listino. Bari-Matera e' 70 km tassabili da
#: anni: se ne uscisse un numero fuori da questa forbice, la tabella e' stata
#: letta male e il prezzo sarebbe sbagliato su ogni ricerca.
MIN_BANDS = 10
BARI_MATERA_KM_RANGE = (50, 100)


@dataclass
class Run:
    """Una corsa: il numero e le sue fermate in ordine."""

    direction: str
    label: str
    #: Marcatore di validita' del manifesto: "(1)" significa soppresso in estate,
    #: "(2)" garantito in caso di sciopero. Vanno tenuti: la nota "(1) treni
    #: soppressi dal 27 luglio al 29 agosto" cade in pieno agosto, e proporre
    #: un treno che non parte e' peggio che non proporlo.
    flag: str | None = None
    stops: list[tuple[str, int]] = field(default_factory=list)  # (stazione, minuti)

    def as_dict(self) -> dict:
        return {
            "direction": self.direction,
            "label": self.label,
            "flag": self.flag,
            "stops": [{"station": name, "minutes": minutes} for name, minutes in self.stops],
        }


def to_minutes(text: str) -> int | None:
    match = TIME_RE.match(text.strip())
    if not match:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 27 or minutes > 59:
        return None
    return hours * 60 + minutes


def download() -> bytes:
    from curl_cffi import requests

    with requests.Session(impersonate="chrome", timeout=90) as session:
        response = session.get(PDF_URL)
        response.raise_for_status()
        return response.content


def _rows(words: list[dict]) -> list[list[dict]]:
    """Raggruppa le parole in righe usando l'ordinata."""
    ordered = sorted(words, key=lambda w: (round(w["top"], 1), w["x0"]))
    rows: list[list[dict]] = []
    for word in ordered:
        if rows and abs(rows[-1][0]["top"] - word["top"]) <= ROW_TOLERANCE:
            rows[-1].append(word)
        else:
            rows.append([word])
    for row in rows:
        row.sort(key=lambda w: w["x0"])
    return rows


def _station_label(row: list[dict]) -> str | None:
    """Il nome della stazione e' la parte testuale a sinistra della riga."""
    words = [w for w in row if to_minutes(w["text"]) is None]
    if not words:
        return None
    label = " ".join(w["text"] for w in words if w["x0"] < 260)
    label = label.strip(" -–—")
    if len(label) < 3 or not any(ch.isalpha() for ch in label):
        return None
    return label


def _columns(centers: list[float]) -> list[float]:
    """Raggruppa le ascisse in colonne: ogni colonna e' un treno."""
    grouped: list[list[float]] = []
    for value in sorted(centers):
        if grouped and value - grouped[-1][-1] <= COLUMN_TOLERANCE:
            grouped[-1].append(value)
        else:
            grouped.append([value])
    return [sum(group) / len(group) for group in grouped]


def extract_notes(data: bytes) -> list[str]:
    """Le note a pie' di pagina, verbatim.

    Contengono le condizioni che decidono se un treno parte davvero: quali sono
    soppressi d'estate, che il servizio non c'e' la domenica, quali tratte sono
    fatte in autobus. Vanno conservate parola per parola, non riassunte."""
    import io

    import pdfplumber

    notes: list[str] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            for line in (page.extract_text() or "").splitlines():
                clean = line.strip()
                if not clean or len(clean) < 12:
                    continue
                if _is_note(clean) and clean not in notes:
                    notes.append(clean)
            for table in page.extract_tables():
                for row in table:
                    for cell in row:
                        text = " ".join((cell or "").split())
                        if _is_note(text) and text not in notes:
                            notes.append(text)
    return notes


def _is_note(text: str) -> bool:
    """Una nota vera, non un frammento di tabella.

    Fra le righe estratte finiscono anche colonne di orari e file di marcatori:
    si tengono solo le frasi, cioe' quelle con abbastanza parole vere."""
    if len(text) < 20 or "\n" in text:
        return False
    if not (text.startswith("-") or text.startswith("(")):
        return False
    words = [w for w in text.split() if len(w) > 2 and any(c.isalpha() for c in w)]
    return len(words) >= 4


def extract_calendar(data: bytes) -> dict:
    """Date del manifesto: mai replicare una sospensione datata ogni anno."""
    import io

    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        text = " ".join(p.extract_text() or "" for p in pdf.pages)
    return calendar_from_text(text)


def calendar_from_text(text: str) -> dict:
    from datetime import date

    months = {
        "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
        "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
        "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
    }
    text = " ".join(text.split()).lower()
    validity = re.search(r"in vigore dal (\d{1,2}) (\w+) (\d{4})", text)
    if not validity or validity[2] not in months:
        raise ValueError("data di entrata in vigore del manifesto non riconosciuta")
    year = int(validity[3])
    result = {"valid_from": date(year, months[validity[2]], int(validity[1])).isoformat()}
    window = re.search(
        r"treni soppressi dal (\d{1,2}) (\w+) al (\d{1,2}) (\w+) (\d{4})", text
    )
    if window:
        start = date(int(window[5]), months[window[2]], int(window[1]))
        end = date(int(window[5]), months[window[4]], int(window[3]))
    elif "treni soppressi nel mese di agosto" in text:
        start, end = date(year, 8, 1), date(year, 8, 31)
    else:
        raise ValueError("finestra estiva dei treni marcati (1) non riconosciuta")
    result["suppressed_window"] = {"from": start.isoformat(), "to": end.isoformat()}
    return result


def parse_pdf(data: bytes, dump: bool = False) -> list[Run]:
    import io

    import pdfplumber

    runs: list[Run] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
            rows = _rows(words)

            # Le intestazioni dividono la pagina in due blocchi, uno per verso.
            sections: list[tuple[str, float]] = []
            for row in rows:
                text = " ".join(w["text"] for w in row).upper()
                if SECTION_FROM_BARI in text:
                    sections.append(("da_bari", row[0]["top"]))
                elif SECTION_TO_BARI in text:
                    sections.append(("per_bari", row[0]["top"]))
            if not sections:
                continue
            sections.sort(key=lambda item: item[1])

            for index, (direction, start) in enumerate(sections):
                end = sections[index + 1][1] if index + 1 < len(sections) else 1e9
                section_rows = [r for r in rows if start < r[0]["top"] < end]
                for header, block in _split_blocks(section_rows):
                    runs.extend(_parse_block(direction, header, block, dump))
    return runs


def _split_blocks(rows: list[list[dict]]) -> list[tuple[list[dict], list[list[dict]]]]:
    """Divide una sezione nei suoi blocchi di colonne.

    Il manifesto non mette tutte le corse di un verso in un'unica griglia: le
    impila in blocchi, ognuno con la propria intestazione "N. Treno" e le
    proprie colonne. Trattarli come una griglia sola incolonna l'orario di un
    treno del pomeriggio sotto quello di un treno del mattino, e ne esce una
    corsa che parte da Bari alle 4.14 e arriva a Matera alle 15.52.

    Ogni blocco torna con la riga dei numeri di treno che lo precede: da li'
    escono sia i numeri veri sia i marcatori di validita'."""
    blocks: list[tuple[list[dict], list[list[dict]]]] = []
    header: list[dict] = []
    current: list[list[dict]] = []
    previous: list[dict] = []

    for row in rows:
        text = " ".join(w["text"] for w in row).upper()
        if "TRENO" in text and len(text) < 120:
            if current:
                blocks.append((header, current))
            # La riga dei numeri sta appena sopra "N. Treno", oppure sulla
            # stessa riga a destra dell'etichetta.
            header = [w for w in (previous + row) if not w["text"].upper().startswith("N")]
            current = []
            previous = row
            continue
        current.append(row)
        previous = row

    if current:
        blocks.append((header, current))
    return [(h, b) for h, b in blocks if b]


FLAG_RE = re.compile(r"^\(([0-9](?:-[0-9])*)\)$")


def _labels_and_flags(header: list[dict], rows: list[list[dict]]) -> tuple[
    list[tuple[float, str]], list[tuple[float, str]]
]:
    """Numeri di treno e marcatori di validita', con la loro ascissa."""
    numbers = [
        ((w["x0"] + w["x1"]) / 2, w["text"])
        for w in header
        if re.fullmatch(r"[0-9]{1,4}(?:/[0-9]{1,4})?", w["text"])
    ]
    flags: list[tuple[float, str]] = []
    for row in rows:
        tokens = [w for w in row if FLAG_RE.match(w["text"])]
        # Una riga di marcatori e' fatta quasi solo di quelli.
        if tokens and len(tokens) >= max(1, len(row) - 1):
            flags.extend(((w["x0"] + w["x1"]) / 2, w["text"]) for w in tokens)
    return numbers, flags


def _nearest(items: list[tuple[float, str]], center: float) -> str | None:
    if not items:
        return None
    best = min(items, key=lambda item: abs(item[0] - center))
    return best[1] if abs(best[0] - center) <= COLUMN_TOLERANCE else None


def _parse_block(
    direction: str, header: list[dict], rows: list[list[dict]], dump: bool
) -> list[Run]:
    numbers, flags = _labels_and_flags(header, rows)
    # Prima passata: quali stazioni, e a che ascissa stanno le ore.
    station_rows: list[tuple[str, list[tuple[float, int]]]] = []
    centers: list[float] = []
    for row in rows:
        times = [
            ((w["x0"] + w["x1"]) / 2, to_minutes(w["text"]))
            for w in row
            if to_minutes(w["text"]) is not None
        ]
        if not times:
            continue
        label = _station_label(row)
        if label is None:
            continue
        station_rows.append((label, [(x, m) for x, m in times if m is not None]))
        centers.extend(x for x, _ in times)

    if not station_rows:
        return []

    columns = _columns(centers)
    if dump:
        print(f"   [{direction}] {len(station_rows)} righe stazione, {len(columns)} colonne")

    runs: list[Run] = []
    for column_index, center in enumerate(columns):
        number = _nearest(numbers, center)
        run = Run(
            direction=direction,
            label=number or f"{direction}-{column_index + 1}",
            flag=_nearest(flags, center),
        )
        for label, times in station_rows:
            best = min(times, key=lambda item: abs(item[0] - center), default=None)
            if best is None or abs(best[0] - center) > COLUMN_TOLERANCE:
                continue
            run.stops.append((label, best[1]))
        if len(run.stops) >= MIN_STOPS_PER_RUN:
            runs.append(run)
    return runs


def _norm_name(text: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else " " for ch in (text or "").lower())
    return " ".join(cleaned.split())


def tariff_node_id(name: str) -> str | None:
    """Nodo del progetto per una stazione del listino, se la conosciamo."""
    key = _norm_name(name)
    for label, node_id in TARIFF_STATIONS:
        if key == label:
            return node_id
    return None


def _is_heading(name: str) -> bool:
    """Vero se quel testo e' un pezzo di intestazione, non una stazione."""
    words = _norm_name(name).split()
    return any(word in HEADING_WORDS for word in words)


def _matrix_tail(row: list[dict]) -> tuple[list[int | None], str]:
    """Coda di una riga della tabella distanze: i valori e il nome di stazione.

    Si legge da destra. Il nome sta in fondo, prima ci sono le distanze verso
    le stazioni gia' elencate, e piu' a sinistra comincia tutt'altro (il
    listino delle fasce, che sulla stessa riga porta gli importi). Fermarsi al
    primo importo con la virgola separa le due tabelle senza doverne conoscere
    le coordinate, che cambiano a ogni riedizione del modulo."""
    name_parts: list[str] = []
    values: list[int | None] = []
    for word in reversed(row):
        text = word["text"]
        if not values and any(ch.isalpha() for ch in text) and not PRICE_RE.match(text):
            name_parts.append(text)
            continue
        if INT_RE.match(text):
            values.append(int(text))
        elif text == "-":
            values.append(None)
        else:
            break
    return list(reversed(values)), " ".join(reversed(name_parts)).strip()


def parse_tariffs(data: bytes, dump: bool = False) -> dict:
    """Listino a fasce e distanze tassabili fra le stazioni della linea.

    FAL non vende a tratta ma a **fascia chilometrica**: la tabella delle
    distanze dice quanti chilometri tassabili separano due stazioni, il listino
    dice quanto costa quella fascia. Le due tabelle stanno affiancate sulla
    stessa pagina, e vanno lette insieme."""
    import io

    import pdfplumber

    bands: list[tuple[int, float]] = []
    distances: dict[str, int] = {}

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            text = (page.extract_text() or "").upper()
            if "DISTANZE FERROVIARIE" not in text or "REGIONE PUGLIA" not in text:
                continue

            page_rows = _rows(page.extract_words(use_text_flow=False, keep_blank_chars=False))

            # --- listino: la prima colonna e' la fascia, la prima cifra con la
            #     virgola sulla stessa riga e' la corsa semplice. Le tabelle
            #     successive (forze dell'ordine, abbonamenti) ripetono le stesse
            #     fasce con importi piu' alti: si tiene solo la prima lettura.
            for row in page_rows:
                texts = [w["text"] for w in row]
                if not texts or not INT_RE.match(texts[0]):
                    continue
                prices = [t for t in texts if PRICE_RE.match(t)]
                if not prices:
                    continue
                km = int(texts[0])
                if any(existing == km for existing, _ in bands):
                    continue
                bands.append((km, float(prices[0].replace(",", "."))))

            # --- distanze: righe consecutive con 0, 1, 2, ... valori. E' la
            #     forma triangolare della tabella, e serve anche da verifica:
            #     se la progressione si rompe, la pagina non e' quella attesa.
            names: list[str] = []
            rows_values: list[list[int | None]] = []
            for row in page_rows:
                values, name = _matrix_tail(row)
                if not name or len(name) < 3 or not any(c.isalpha() for c in name):
                    continue
                if _is_heading(name):
                    continue
                if len(values) != len(names):
                    continue
                names.append(name)
                rows_values.append(values)

            if dump:
                print(f"   listino: {len(bands)} fasce, tabella: {len(names)} stazioni")
                for name, values in zip(names, rows_values):
                    print(f"      {name:32} {values}")

            for index, (name, values) in enumerate(zip(names, rows_values)):
                node_id = tariff_node_id(name)
                if node_id is None:
                    continue
                for other_index, km in enumerate(values):
                    other = tariff_node_id(names[other_index])
                    if other is None or km is None or other == node_id:
                        continue
                    distances[_pair_key(node_id, other)] = km
            break

    bands.sort()
    return {"bands": [[km, price] for km, price in bands], "distances": distances}


def _pair_key(first: str, second: str) -> str:
    return "|".join(sorted((first, second)))


def validate_tariffs(fares: dict) -> list[str]:
    """Il listino e' utile solo se regge questi controlli."""
    problems: list[str] = []
    bands = fares.get("bands") or []
    if len(bands) < MIN_BANDS:
        problems.append(f"solo {len(bands)} fasce tariffarie, attese almeno {MIN_BANDS}")
    prices = [price for _, price in bands]
    if any(b < a for a, b in zip(prices, prices[1:])):
        problems.append(f"prezzi non crescenti con la distanza: {prices[:8]}")

    key = _pair_key("ov:fal-bari-centrale", "ov:fal-matera-centrale")
    bari_matera = (fares.get("distances") or {}).get(key)
    low, high = BARI_MATERA_KM_RANGE
    if bari_matera is None:
        problems.append("manca la distanza Bari-Matera: la tabella non e' stata letta")
    elif not low <= bari_matera <= high:
        problems.append(
            f"Bari-Matera risulta {bari_matera} km tassabili, fuori da {low}-{high}"
        )
    return problems


def validate(runs: list[Run]) -> list[str]:
    """Controlli di plausibilita'. Un elenco non vuoto significa: non scrivere."""
    problems: list[str] = []

    for direction in ("da_bari", "per_bari"):
        block = [r for r in runs if r.direction == direction]
        if len(block) < MIN_RUNS_PER_DIRECTION:
            problems.append(
                f"solo {len(block)} corse per il verso {direction}, "
                f"attese almeno {MIN_RUNS_PER_DIRECTION}"
            )

    everything = " ".join(name.lower() for run in runs for name, _ in run.stops)
    for required in REQUIRED_STATIONS:
        if required not in everything:
            problems.append(f"nessuna fermata contiene {required!r}: capolinea mancante")

    # Gli orari di una corsa devono crescere lungo il percorso, e l'intera
    # linea Bari-Matera si percorre in poco piu' di due ore: una corsa che
    # "dura" sei ore e' due treni diversi finiti nella stessa colonna.
    for run in runs:
        minutes = [m for _, m in run.stops]
        if any(b < a for a, b in zip(minutes, minutes[1:])):
            problems.append(f"corsa {run.label}: orari non crescenti {minutes[:6]}")
        elif minutes and minutes[-1] - minutes[0] > MAX_RUN_MINUTES:
            fermate = " -> ".join(name[:20] for name, _ in run.stops[:3])
            problems.append(
                f"corsa {run.label}: dura {minutes[-1] - minutes[0]} minuti, "
                f"oltre il massimo plausibile di {MAX_RUN_MINUTES} ({fermate}...)"
            )
        if len(problems) >= 5:
            break

    return problems


def find_tariff_pdf() -> str:
    """URL del listino, cercato sulla pagina degli orari.

    Il nome del file porta la data dell'ultima revisione: fissarlo qui
    significherebbe scaricare un listino vecchio al primo aggiornamento."""
    from curl_cffi import requests

    try:
        with requests.Session(impersonate="chrome", timeout=60) as session:
            html = session.get(TIMETABLE_PAGE).text
        links = re.findall(r'https?://[^"\'\s>]+\.pdf', html, re.I)
        for link in links:
            if TARIFF_HINT in link.upper():
                return link
    except Exception as exc:  # noqa: BLE001 - si ripiega sull'ultimo noto
        print(f"   pagina orari non raggiungibile ({exc}), uso l'ultimo listino noto")
    return TARIFF_FALLBACK


def download_url(url: str) -> bytes:
    from curl_cffi import requests

    with requests.Session(impersonate="chrome", timeout=90) as session:
        response = session.get(url)
        response.raise_for_status()
        return response.content


def _provenance(source: str) -> str:
    """La provenienza da scrivere nel file versionato.

    Un URL resta com'e'. Un percorso locale (`--tariff-pdf`) diventa relativo
    alla radice del progetto, o il solo nome se sta fuori: il file prodotto
    finisce in un repository pubblico, e la struttura di cartelle di chi lo
    rigenera non e' un dato del progetto.
    """
    if source.startswith(("http://", "https://")):
        return source
    path = Path(source)
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.name


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", action="store_true", help="mostra cosa ha capito")
    parser.add_argument("--pdf", help="usa un PDF gia' scaricato invece di riscaricarlo")
    parser.add_argument("--tariff-pdf", help="listino gia' scaricato")
    parser.add_argument("--skip-tariffs", action="store_true",
                        help="rigenera solo gli orari, lasciando il listino com'e'")
    args = parser.parse_args()

    if args.pdf:
        data = open(args.pdf, "rb").read()
        print(f"PDF locale: {args.pdf} ({len(data):,} byte)")
    else:
        print(f"scarico {PDF_URL}")
        data = download()
        (DATA_DIR / "fal_treni.pdf").write_bytes(data)
        print(f"   {len(data):,} byte")

    calendar = extract_calendar(data)
    runs = parse_pdf(data, dump=args.dump)
    print(f"corse riconosciute: {len(runs)}")

    if args.dump:
        for run in runs[:6]:
            fermate = ", ".join(
                f"{name[:26]} {m // 60:02}:{m % 60:02}" for name, m in run.stops[:6]
            )
            print(f"   {run.label}: {fermate} ...")

    problems = validate(runs)
    if problems:
        print("\nORARIO NON PLAUSIBILE, non scrivo niente:", file=sys.stderr)
        for problem in problems:
            print(f"   - {problem}", file=sys.stderr)
        print(
            "\nIl manifesto FAL ha probabilmente cambiato impaginazione. "
            "Rilancia con --dump per vedere cosa e' stato riconosciuto.",
            file=sys.stderr,
        )
        return 1

    # --- listino a fasce, dal PDF delle tariffe -----------------------------
    fares: dict = {}
    if args.skip_tariffs and OUTPUT.exists():
        fares = orjson.loads(OUTPUT.read_bytes()).get("fares") or {}
    if not args.skip_tariffs:
        if args.tariff_pdf:
            tariff_url = args.tariff_pdf
            tariff_data = open(args.tariff_pdf, "rb").read()
            print(f"\nlistino locale: {args.tariff_pdf} ({len(tariff_data):,} byte)")
        else:
            tariff_url = find_tariff_pdf()
            print(f"\nscarico il listino {tariff_url}")
            tariff_data = download_url(tariff_url)
            (DATA_DIR / "fal_tariffe.pdf").write_bytes(tariff_data)
            print(f"   {len(tariff_data):,} byte")

        fares = parse_tariffs(tariff_data, dump=args.dump)
        fares["source"] = _provenance(tariff_url)
        tariff_problems = validate_tariffs(fares)
        print(
            f"listino: {len(fares['bands'])} fasce, "
            f"{len(fares['distances'])} coppie di stazioni"
        )
        if tariff_problems:
            # Un listino illeggibile non deve buttare via anche gli orari: si
            # rinuncia ai prezzi, che l'adapter dichiara come assenti, e si
            # scrive comunque l'orario. Il contrario (prezzi inventati su un
            # orario giusto) sarebbe molto peggio.
            print("\nLISTINO NON PLAUSIBILE, i prezzi non verranno scritti:", file=sys.stderr)
            for problem in tariff_problems:
                print(f"   - {problem}", file=sys.stderr)
            fares = {}

    if fares and args.dump:
        for pair, km in sorted(fares["distances"].items()):
            print(f"   {pair:56} {km:3} km -> {price_for(fares, km):.2f} EUR")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": PDF_URL,
        "source_page": TIMETABLE_PAGE,
        **calendar,
        "note": (
            "Estratto dai documenti ufficiali FAL: orari dal manifesto, prezzi "
            "dal listino a fasce chilometriche. Il servizio ferroviario e' "
            "sospeso la domenica e nei giorni festivi."
        ),
        "notes": extract_notes(data),
        "fares": fares,
        "runs": [run.as_dict() for run in runs],
    }
    OUTPUT.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
    print(f"\nscritto {OUTPUT} ({len(runs)} corse)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
