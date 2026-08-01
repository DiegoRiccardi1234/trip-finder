"""Costruisce il catalogo delle tessere leggendo le fonti degli operatori.

    python scripts/build_tessere.py            rigenera app/routing/data/tessere.json
    python scripts/build_tessere.py --check    non scrive: dice se il file e' invecchiato

Perche' estrarre invece di scrivere. Una tessera e' un dato con una scadenza
dentro, e su nove voci raccolte ad agosto 2026 cinque scadono entro l'anno. Un
elenco digitato sbaglia in primavera e non lo dice a nessuno.

Le Condizioni Generali di Trasporto sono la fonte giusta: PDF pubblici,
strutturati in paragrafi, che portano la percentuale, i requisiti d'eta' e le
date di validita', e soprattutto sono il documento che **vincola l'operatore**.
Vale piu' delle pagine riassuntive: quelle dicevano che la Carta Verde e' sparita
il 1 aprile 2026, il PDF dice il 4.

Quello che nessuno pubblica in forma leggibile resta scritto qui sotto, in
`CURATE`, ma con la fonte e la data in cui e' stato guardato: cosi' invecchia ad
alta voce. `--check` e' quello che gira ogni settimana in CI.

Come `build_fal_schedule.py`, valida prima di scrivere e **fallisce senza
scrivere**: un catalogo mezzo estratto e' peggio di uno vecchio, perche' quello
vecchio almeno lo sai.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import _bootstrap  # noqa: E402, F401

USCITA = Path(__file__).resolve().parents[1] / "app" / "routing" / "data" / "tessere.json"

MESI = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6,
    "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}

# ── Fonti che si leggono a macchina ──────────────────────────────────────────
#
# Ogni voce dice cosa cercare nel PDF. Il testo cambia poco fra un allegato e
# l'altro perche' e' materiale normativo, ed e' per questo che si puo' leggere.

PDF_TRENITALIA = "https://www.trenitalia.com/content/dam/trenitalia/allegati/info/condizioni-generali-di-trasporto"

ESTRATTE: list[dict[str, Any]] = [
    {
        "id": "trenitalia-promo-young",
        "nome": "Promo Young (Trenitalia)",
        "fonte": f"{PDF_TRENITALIA}/parte-iii-trasporto-regionale/promo/Promo_YOUNG.pdf",
        "ambito": "provider:trenitalia",
        "tipo": "percent",
        "richiede": "Carta X-GO (gratuita)",
        # I nomi con cui l'offerta compare nella risposta di ricerca. Quando
        # combaciano, il totale lo dice Trenitalia e la percentuale qui sopra
        # non viene nemmeno usata.
        "offerte": ["YOUNG", "FrecciaYOUNG"],
        "note": "Solo su alcuni regionali, regionali veloci e metropolitani, "
                "e solo su un numero limitato di treni.",
    },
    {
        "id": "trenitalia-promo-senior",
        "nome": "Promo Senior (Trenitalia)",
        "fonte": f"{PDF_TRENITALIA}/parte-iii-trasporto-regionale/promo/Promo_SENIOR.pdf",
        "ambito": "provider:trenitalia",
        "tipo": "percent",
        "richiede": "Carta X-GO (gratuita)",
        "offerte": ["SENIOR", "FrecciaSENIOR"],
        "note": "Come la Young, ma dai 60 anni in su.",
    },
    {
        "id": "trenitalia-carta-verde",
        "nome": "Carta Verde (Trenitalia)",
        "fonte": f"{PDF_TRENITALIA}/parte-i-norme-comuni/Allegato_5-Carta_Verde.pdf",
        "ambito": "provider:trenitalia",
        "tipo": "percent",
        "note": "Non piu' acquistabile. Le carte gia' emesse valgono fino alla loro scadenza.",
    },
    {
        "id": "trenitalia-carta-argento",
        "nome": "Carta Argento (Trenitalia)",
        "fonte": f"{PDF_TRENITALIA}/parte-i-norme-comuni/Allegato_4-Carta_Argento.pdf",
        "ambito": "provider:trenitalia",
        "tipo": "percent",
        "note": "Non piu' acquistabile. Le carte gia' emesse valgono fino alla loro scadenza.",
    },
]

# ── Quello che nessuno pubblica in forma leggibile ───────────────────────────
#
# Curato a mano, con la fonte e la data in cui e' stato guardato. `verificato_il`
# non e' un ornamento: `app/routing/tessere.py` marca stantia una voce che
# nessuno riguarda da sei mesi, e l'interfaccia lo dice.

VERIFICATO = "2026-08-01"

CURATE: list[dict[str, Any]] = [
    {
        "id": "italo-young",
        "nome": "Italo Young (14-29 anni)",
        "ambito": "provider:italo",
        "tipo": "percent",
        "valore": 40.0,
        "eta_min": 14, "eta_max": 29,
        "fonte": "https://www.italotreno.com/it/offerte-treno/italo-young",
        "note": "Non e' una tessera ma una tariffa: si compra in anticipo e i posti "
                "sono contingentati, quindi la riduzione vera cambia da corsa a corsa. "
                "Qui si usa un valore prudente.",
    },
    {
        "id": "italo-senior",
        "nome": "Italo Senior (over 60)",
        "ambito": "provider:italo",
        "tipo": "percent",
        "valore": 40.0,
        "eta_min": 60,
        "fonte": "https://www.italotreno.com/it/offerte-treno/italo-senior",
        "note": "Come la Young: tariffa contingentata, non tessera.",
    },
    {
        "id": "isic-flixbus",
        "nome": "ISIC su FlixBus",
        "ambito": "provider:flixbus",
        "tipo": "percent",
        "valore": 10.0,
        "richiede": "Carta ISIC",
        "valido_a": "2026-12-15",
        "fonte": "https://isic.de/en/benefits/flixbus/8154/",
        "note": "Codice sconto legato alla carta studenti internazionale.",
    },
    {
        "id": "esncard-flixbus",
        "nome": "ESNcard su FlixBus",
        "ambito": "provider:flixbus",
        "tipo": "percent",
        "valore": 10.0,
        "richiede": "ESNcard (studenti Erasmus)",
        "fonte": "https://www.esncard.org/flixbus",
    },
    {
        "id": "continuita-sardegna-aereo",
        "nome": "Continuita' territoriale Sardegna (voli)",
        "ambito": "mode:air",
        "tipo": "percent",
        "valore": 50.0,
        "richiede": "residenza in Sardegna, oppure studente fino a 27 anni, "
                    "under 21, over 70, lavoratore, disabile o accompagnatore",
        "valido_da": "2026-03-29",
        "rotte": [
            "Cagliari-Roma", "Cagliari-Milano",
            "Olbia-Roma", "Olbia-Milano",
            "Alghero-Roma", "Alghero-Milano",
        ],
        "fonte": "https://www.regione.sardegna.it/argomenti/argomenti-speciali/"
                 "continuita-territoriale-2026/domande-e-risposte-continuita-territoriale",
        "note": "Non e' una tessera ma una tariffa regolata dalla Regione: il prezzo e' "
                "imposto, non scontato, e cambia la classifica piu' di qualunque sconto. "
                "Operata da Aeroitalia e ITA Airways su Fiumicino e Linate.",
    },
    {
        "id": "residenti-isole-traghetti",
        "nome": "Tariffa residenti isole (traghetti)",
        "ambito": "mode:ferry",
        "tipo": "percent",
        "valore": 30.0,
        "richiede": "residenza o nascita in Sardegna o Sicilia",
        "fonte": "https://www.traghetti.com/blog/tariffe-residenti-nativi-traghetti-sicilia-sardegna/",
        "note": "Ogni compagnia la applica a modo suo: Grimaldi la estende a tutti i "
                "passeggeri della prenotazione, Tirrenia in Sicilia ai soli residenti e "
                "familiari al seguito. Il valore qui e' prudente.",
    },
    {
        "id": "interrail-youth",
        "nome": "Interrail Youth (under 28)",
        "ambito": "mode:rail",
        "tipo": "percent",
        "valore": 25.0,
        "eta_max": 27,
        "fonte": "https://www.interrail.eu/en/interrail-passes/deals",
        "note": "Sconto sul prezzo del pass, non sul singolo biglietto: vale se viaggi "
                "gia' con un Interrail, non si somma a una tariffa a corsa.",
    },
    {
        "id": "io-studio",
        "nome": "Carta dello Studente «IoStudio»",
        "ambito": "mode:rail",
        "tipo": "percent",
        "valore": 10.0,
        "eta_min": 14, "eta_max": 26,
        "fonte": "https://www.trenitalia.com/it/informazioni/la-guida-del-viaggiatore/"
                 "altre-riduzioni-e-carte-sconto-a-tua-disposizione.html",
        "note": "Riduzioni sui regionali per studenti delle superiori. Le condizioni "
                "cambiano per regione.",
    },
    {
        "id": "trenitalia-x-go",
        "nome": "X-GO (Trenitalia, gratuita)",
        "ambito": "provider:trenitalia",
        "tipo": "percent",
        "valore": 0.0,
        "fonte": "https://www.trenitalia.com/it/x-go/il-programma.html",
        "note": "Non sconta il biglietto: accumula 2 punti per euro su Regionale e "
                "Intercity, e 150 punti valgono 3 euro sul viaggio dopo. Serve pero' "
                "per avere le promo Young e Senior, ed e' gratuita.",
    },
]


# ── Lettura dei PDF ──────────────────────────────────────────────────────────


def scarica(url: str) -> bytes:
    richiesta = urllib.request.Request(url, headers={"User-Agent": "TripFinder/0.2 (+github)"})
    with urllib.request.urlopen(richiesta, timeout=90) as risposta:
        return risposta.read()


def testo_del_pdf(grezzo: bytes) -> str:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(grezzo)) as pdf:
        pagine = [pagina.extract_text() or "" for pagina in pdf.pages]
    # Le righe del PDF spezzano le frasi a meta': unendo tutto con spazi le
    # espressioni regolari trovano "dal 5 aprile al 30 novembre 2026" anche
    # quando sta su due righe.
    return " ".join(" ".join(pagine).split())


def _giorno(giorno: str, mese: str, anno: str) -> str | None:
    numero = MESI.get(mese.lower())
    if not numero:
        return None
    try:
        return date(int(anno), numero, int(giorno)).isoformat()
    except ValueError:
        return None


VALIDITA = re.compile(
    r"valid[ao]\s+per\s+viaggi\s+dal\s+(\d{1,2})\s+(\w+)(?:\s+(\d{4}))?\s+al\s+(\d{1,2})\s+(\w+)\s+(\d{4})",
    re.IGNORECASE,
)
RITIRO = re.compile(
    r"non\s+(?:e['’]|è)\s+pi(?:u['’]|ù)\s+acquistabile\s+dal\s+(\d{1,2})\s+(\w+)\s+(\d{4})",
    re.IGNORECASE,
)
RIDUZIONE = re.compile(r"riduzione\s+del\s+(\d{1,2})\s*%", re.IGNORECASE)
ETA_FINO = re.compile(r"fino\s+ai\s+(\d{1,2})\s+anni\s+non\s+compiuti", re.IGNORECASE)
ETA_FRA = re.compile(
    r"tra\s+i\s+(\d{1,2})\s+e\s+i\s+(\d{1,2})\s+anni\s+non\s+compiuti", re.IGNORECASE
)
ETA_DA = re.compile(r"(?:et[aà]\s+(?:pari\s+o\s+)?superiore\s+ai?|dai)\s+(\d{1,2})\s+anni", re.IGNORECASE)


def leggi(testo: str, base: dict[str, Any]) -> dict[str, Any]:
    """Quello che il documento dice davvero, sopra quello che sappiamo gia'."""
    voce = dict(base)

    if (m := VALIDITA.search(testo)) is not None:
        anno_fine = m.group(6)
        inizio = _giorno(m.group(1), m.group(2), m.group(3) or anno_fine)
        fine = _giorno(m.group(4), m.group(5), anno_fine)
        if inizio and fine:
            voce["valido_da"], voce["valido_a"] = inizio, fine

    if (m := RITIRO.search(testo)) is not None:
        ritirata = _giorno(m.group(1), m.group(2), m.group(3))
        if ritirata:
            # Ritirata non vuol dire inutile: chi ce l'ha la usa fino alla
            # scadenza sua, che il documento non conosce. Si dice e basta.
            voce["ritirata_dal"] = ritirata
            voce["acquistabile"] = False

    if (m := RIDUZIONE.search(testo)) is not None:
        voce["valore"] = float(m.group(1))

    if (m := ETA_FRA.search(testo)) is not None:
        voce["eta_min"], voce["eta_max"] = int(m.group(1)), int(m.group(2)) - 1
    elif (m := ETA_FINO.search(testo)) is not None:
        voce["eta_max"] = int(m.group(1)) - 1
    elif (m := ETA_DA.search(testo)) is not None:
        voce["eta_min"] = int(m.group(1))

    voce["estratto"] = True
    voce["verificato_il"] = date.today().isoformat()
    return voce


def controlla(voce: dict[str, Any]) -> list[str]:
    """Cosa manca perche' la voce sia utilizzabile. Vuoto = va bene."""
    problemi = []
    if not voce.get("id") or not voce.get("nome"):
        problemi.append("senza id o nome")
    if not voce.get("fonte"):
        problemi.append(f"{voce.get('id')}: senza fonte")
    valore = voce.get("valore")
    if not isinstance(valore, (int, float)):
        problemi.append(f"{voce.get('id')}: percentuale non trovata nel documento")
    elif voce.get("tipo") == "percent" and not 0 <= float(valore) <= 100:
        problemi.append(f"{voce.get('id')}: percentuale fuori scala ({valore})")
    return problemi


def costruisci() -> dict[str, Any]:
    voci: list[dict[str, Any]] = []
    problemi: list[str] = []

    for base in ESTRATTE:
        print(f"leggo {base['id']}...")
        try:
            testo = testo_del_pdf(scarica(base["fonte"]))
        except Exception as exc:  # noqa: BLE001 - la fonte e' fuori dal nostro controllo
            problemi.append(f"{base['id']}: fonte non leggibile ({exc})")
            continue
        voce = leggi(testo, base)
        problemi += controlla(voce)
        voci.append(voce)
        pezzi = [f"{voce.get('valore')}%"]
        if voce.get("valido_a"):
            pezzi.append(f"fino al {voce['valido_a']}")
        if voce.get("ritirata_dal"):
            pezzi.append(f"ritirata dal {voce['ritirata_dal']}")
        print("   ", ", ".join(pezzi))

    for base in CURATE:
        voce = dict(base)
        voce.setdefault("verificato_il", VERIFICATO)
        voce["estratto"] = False
        problemi += controlla(voce)
        voci.append(voce)

    if problemi:
        raise SystemExit("catalogo non scritto:\n  " + "\n  ".join(problemi))

    voci.sort(key=lambda v: (not v.get("estratto"), v["id"]))
    return {
        "generato_il": date.today().isoformat(),
        "nota": "Generato da scripts/build_tessere.py. Le voci con estratto=true "
                "vengono dai documenti degli operatori, le altre sono curate a mano "
                "e portano la data in cui qualcuno le ha guardate.",
        "tessere": voci,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Catalogo delle tessere e delle riduzioni")
    parser.add_argument(
        "--check", action="store_true",
        help="non scrive: esce diverso da zero se il file versionato non e' piu' quello giusto",
    )
    args = parser.parse_args()

    nuovo = costruisci()

    if not args.check:
        USCITA.parent.mkdir(parents=True, exist_ok=True)
        USCITA.write_text(
            json.dumps(nuovo, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nscritte {len(nuovo['tessere'])} voci in {USCITA}")
        return 0

    if not USCITA.exists():
        print("il catalogo non esiste ancora", file=sys.stderr)
        return 1
    vecchio = json.loads(USCITA.read_text(encoding="utf-8"))

    # La data di generazione cambia a ogni giro e non e' una notizia: si
    # confronta quello che le voci dicono, non quando le abbiamo lette.
    def confrontabile(catalogo: dict[str, Any]) -> dict[str, Any]:
        return {
            v["id"]: {k: val for k, val in v.items() if k != "verificato_il"}
            for v in catalogo["tessere"]
        }

    prima, dopo = confrontabile(vecchio), confrontabile(nuovo)
    cambiate = [i for i in dopo if i in prima and prima[i] != dopo[i]]
    nuove = [i for i in dopo if i not in prima]
    sparite = [i for i in prima if i not in dopo]

    oggi = date.today().isoformat()
    scadute = [
        v["id"] for v in vecchio["tessere"] if v.get("valido_a") and v["valido_a"] < oggi
    ]

    for etichetta, elenco in (
        ("cambiate alla fonte", cambiate), ("nuove", nuove),
        ("sparite", sparite), ("scadute", scadute),
    ):
        if elenco:
            print(f"{etichetta}: {', '.join(elenco)}")

    if cambiate or nuove or sparite or scadute:
        print("\nil catalogo va rigenerato: python scripts/build_tessere.py")
        return 1
    print(f"catalogo allineato alle fonti ({len(dopo)} voci)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
