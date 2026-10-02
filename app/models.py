"""Modelli di dominio condivisi da provider, motore di composizione e API.

Tutti i `datetime` che rappresentano orari di viaggio sono timezone-aware.
Un orario naive che arriva da un adapter e' un bug dell'adapter, non un caso
da gestire a valle: due gambe in fusi diversi non sono confrontabili senza tz.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field


class Mode(str, Enum):
    RAIL = "rail"
    BUS = "bus"
    AIR = "air"
    FERRY = "ferry"
    TRANSFER = "transfer"  # navetta, metro, taxi: collegamento fra due gambe
    WALK = "walk"


#: Modi che un utente puo' chiedere esplicitamente. TRANSFER e WALK sono
#: generati dal motore, non richiesti.
BOOKABLE_MODES = frozenset({Mode.RAIL, Mode.BUS, Mode.AIR, Mode.FERRY})


class NodeKind(str, Enum):
    STATION = "station"
    AIRPORT = "airport"
    BUS_STOP = "bus_stop"
    PORT = "port"
    CITY = "city"


#: Che tipo di nodo puo' servire ciascun modo.
MODE_NODE_KINDS: dict[Mode, frozenset[NodeKind]] = {
    Mode.RAIL: frozenset({NodeKind.STATION}),
    Mode.BUS: frozenset({NodeKind.BUS_STOP, NodeKind.STATION}),
    Mode.AIR: frozenset({NodeKind.AIRPORT}),
    Mode.FERRY: frozenset({NodeKind.PORT}),
}


class Node(BaseModel):
    """Un punto di partenza/arrivo concreto: stazione, aeroporto, fermata, porto."""

    model_config = ConfigDict(frozen=False)

    id: str  # identificatore interno stabile, es. "tl:8768"
    name: str
    kind: NodeKind
    lat: float
    lon: float
    country: str | None = None
    city: str | None = None
    timezone: str | None = None
    iata: str | None = None
    #: Mappa provider -> identificatore nativo, es. {"trenitalia": "830008409"}.
    provider_ids: dict[str, str] = Field(default_factory=dict)
    #: Stazione/aeroporto principale della citta': preferito come hub.
    is_main: bool = False

    def supports(self, provider_id: str) -> bool:
        return provider_id in self.provider_ids

    def __hash__(self) -> int:  # usato per deduplicare le gambe da interrogare
        return hash(self.id)


#: I gruppi in cui si presentano le fermate, nell'ordine in cui si mostrano.
#: Terra prima dell'aria perche' sono scelte diverse: chi guarda le stazioni
#: sta valutando un viaggio, chi guarda gli aeroporti ne sta valutando un
#: altro, e mescolarli costringe a rileggere la lista due volte.
GRUPPI_DI_PRESENTAZIONE: tuple[frozenset[NodeKind], ...] = (
    frozenset({NodeKind.STATION, NodeKind.BUS_STOP}),
    frozenset({NodeKind.AIRPORT}),
    frozenset({NodeKind.PORT}),
    frozenset({NodeKind.CITY}),
)


class Place(BaseModel):
    """Esito della risoluzione di un testo libero ("Torino", "Matera")."""

    query: str
    label: str
    lat: float
    lon: float
    country: str | None = None
    nodes: list[Node] = Field(default_factory=list)

    def nodes_for(self, mode: Mode) -> list[Node]:
        kinds = MODE_NODE_KINDS.get(mode, frozenset())
        return [n for n in self.nodes if n.kind in kinds]

    def fermate_da_mostrare(self) -> list[tuple[Node, float]]:
        """Le fermate come le legge una persona, con la loro distanza in km.

        **Non e' l'ordine di `nodes`, ed e' voluto.** `nodes` e' ordinata per
        punteggio — importanza meno distanza — ed e' cosi' che il motore decide
        chi interrogare quando i posti sono meno delle fermate: e' un ordine
        tarato, che protegge Linate a 7 km dal venire scavalcato da Malpensa
        (vedi `DISTANCE_PENALTY_PER_KM` nel resolver). Non si tocca.

        Ma a schermo quell'ordine mente. Cercando «Canelli» metteva in cima
        l'aeroporto di Genova, a 55 km, e Canelli stessa nona: chi legge cerca
        per tipo e per vicinanza, il motore no. Da qui due ordini distinti, e
        una regola: **questo non arriva mai al motore**.

        La distanza si ricalcola invece di essere portata dietro in un campo.
        I nodi dell'indice sono condivisi fra tutte le ricerche, e la distanza
        non e' una proprieta' della stazione ma della relazione fra stazione e
        ancora — la stessa fermata sta a 1 km da «Torino» e a 19 da «Ciriè».
        Un campo sul nodo sarebbe un dato duplicato che finirebbe dentro ogni
        gamba serializzata, dove non significa niente."""
        per_gruppo: list[tuple[int, float, str, Node]] = []
        for node in self.nodes:
            gruppo = next(
                (i for i, kinds in enumerate(GRUPPI_DI_PRESENTAZIONE) if node.kind in kinds),
                len(GRUPPI_DI_PRESENTAZIONE),
            )
            km = haversine_km(self.lat, self.lon, node.lat, node.lon)
            # Il nome come terzo criterio: due fermate equidistanti non devono
            # scambiarsi di posto fra una chiamata e l'altra.
            per_gruppo.append((gruppo, km, node.name, node))
        per_gruppo.sort(key=lambda riga: riga[:3])
        return [(node, km) for _, km, _, node in per_gruppo]


class Fare(BaseModel):
    """Tariffa di una singola gamba, cosi' come pubblicata dall'operatore."""

    amount: float
    currency: str = "EUR"
    fare_class: str | None = None  # "Economy", "Base", "Super Economy", "Value"
    refundable: bool | None = None
    changeable: bool | None = None
    #: Bagagli gia' inclusi nella tariffa (piccolo/cabina/stiva).
    included_cabin_bags: int = 0
    included_checked_bags: int = 0
    #: Costo per aggiungere un bagaglio da stiva, se noto.
    checked_bag_price: float | None = None
    seats_left: int | None = None
    #: L'operatore dichiara «a partire da»: la cifra e' un minimo, non il
    #: prezzo di quella corsa. Era solo una nota testuale dentro la gamba, e il
    #: totale la presentava come una cifra certa — mentre il principio del
    #: progetto e' che le voci stimate si chiamino stimate.
    indicative: bool = False


class Leg(BaseModel):
    """Una tratta servita da un singolo operatore, oppure un trasferimento."""

    provider: str  # id dell'adapter che l'ha prodotta, es. "trenitalia"
    mode: Mode
    origin: Node
    destination: Node
    depart: datetime
    arrive: datetime
    operator: str | None = None  # nome commerciale, es. "Trenitalia", "Ryanair"
    vehicle: str | None = None  # "FR 9512", "FR1234", "IC 728"
    fare: Fare | None = None
    booking_url: str | None = None
    co2_kg: float | None = None
    #: Cambi interni alla gamba. Un Torino-Bari con cambio a Bologna venduto da
    #: Trenitalia come biglietto unico e' una gamba sola (nessun rischio di
    #: coincidenza persa) ma con un cambio: le due cose vanno tenute distinte.
    internal_changes: int = 0
    #: Fermate intermedie descritte dall'operatore, per il dettaglio in UI.
    segments: list[str] = Field(default_factory=list)
    #: Note che l'adapter vuole far arrivare all'utente ("solo con Carta Verde").
    notes: list[str] = Field(default_factory=list)
    #: Tariffe ridotte **dichiarate dall'operatore**: nome dell'offerta ->
    #: totale che si pagherebbe avendone diritto. Trenitalia le manda gia' nella
    #: risposta di ricerca; il prezzo esposto le esclude perche' il motore non sa
    #: se chi cerca ha la tessera. Il totale vale solo avendo diritto a **tutte**
    #: le offerte elencate, e vale piu' di qualunque percentuale stimata: viene
    #: da loro, e non invecchia.
    reduced_fares: dict[str, float] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def duration_min(self) -> int:
        elapsed = self.arrive.astimezone(timezone.utc) - self.depart.astimezone(timezone.utc)
        return max(0, int(elapsed.total_seconds() // 60))

    @property
    def price(self) -> float:
        return self.fare.amount if self.fare else 0.0

    @property
    def is_transfer(self) -> bool:
        return self.mode in (Mode.TRANSFER, Mode.WALK)


class CostLine(BaseModel):
    """Una voce del costo totale, cosi' l'utente vede da dove esce il numero."""

    label: str
    amount: float
    kind: str  # "fare" | "bag" | "transfer" | "estimate"
    estimated: bool = False


class CostBreakdown(BaseModel):
    currency: str = "EUR"
    lines: list[CostLine] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total(self) -> float:
        return round(sum(line.amount for line in self.lines), 2)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_estimates(self) -> bool:
        return any(line.estimated for line in self.lines)

    def subtotal(self, kind: str) -> float:
        return round(sum(line.amount for line in self.lines if line.kind == kind), 2)


class RiskFlag(str, Enum):
    SEPARATE_TICKETS = "separate_tickets"  # gambe non protette fra loro
    TIGHT_CONNECTION = "tight_connection"  # margine sotto il minimo consigliato
    STATION_CHANGE = "station_change"  # cambio stazione/aeroporto nella stessa citta'
    NIGHT_ARRIVAL = "night_arrival"  # arrivo fra 00:00 e 06:00
    LAST_LEG_UNVERIFIED = "last_leg_unverified"  # orario da fonte statica, non live
    ESTIMATED_COST = "estimated_cost"  # una voce di costo e' una stima


#: Gli stessi avvisi detti a parole. Al modello arrivavano i valori dell'enum —
#: `last_leg_unverified` dentro un prompt italiano, senza legenda — mentre a
#: schermo l'utente leggeva una frase compiuta: due descrizioni diverse della
#: stessa scheda, e il consiglio poteva parafrasare male proprio l'avviso.
#: Sono volutamente piu' brevi delle frasi della pagina (`FLAG_TEXT` in
#: `app/static/app.js`): li' spiegano, qui devono solo essere capiti.
#: Un test verifica che le due mappe coprano gli stessi codici.
RISK_TEXT: dict[RiskFlag, str] = {
    RiskFlag.SEPARATE_TICKETS: "biglietti separati, nessuna riprotezione fra una tratta e l'altra",
    RiskFlag.TIGHT_CONNECTION: "coincidenza stretta rispetto al margine consigliato",
    RiskFlag.STATION_CHANGE: "cambio di stazione o scalo, serve spostarsi",
    RiskFlag.NIGHT_ARRIVAL: "arrivo nel cuore della notte",
    RiskFlag.LAST_LEG_UNVERIFIED: "ultima tratta da orario statico, non verificata live",
    RiskFlag.ESTIMATED_COST: "alcune voci di costo sono stimate",
}


def risk_text(flag: str) -> str:
    """La frase per un avviso, o il codice se e' nuovo e nessuno l'ha tradotto."""
    try:
        return RISK_TEXT[RiskFlag(flag)]
    except ValueError:
        return flag


class Itinerary(BaseModel):
    id: str
    legs: list[Leg]
    cost: CostBreakdown = Field(default_factory=CostBreakdown)
    flags: list[RiskFlag] = Field(default_factory=list)
    #: Punteggio finale del ranker (piu' alto = meglio). Assegnato a valle.
    score: float = 0.0
    #: Componenti del punteggio, per spiegare la classifica nella UI.
    score_parts: dict[str, float] = Field(default_factory=dict)
    #: Comparso dopo che la prima ondata di risultati era gia' sullo schermo.
    #: Senza questo segnale la classifica si rimescolerebbe sotto gli occhi di
    #: chi legge senza spiegare perche'.
    is_new: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def depart(self) -> datetime:
        return self.legs[0].depart

    @computed_field  # type: ignore[prop-decorator]
    @property
    def arrive(self) -> datetime:
        return self.legs[-1].arrive

    @computed_field  # type: ignore[prop-decorator]
    @property
    def duration_min(self) -> int:
        elapsed = self.arrive.astimezone(timezone.utc) - self.depart.astimezone(timezone.utc)
        return max(0, int(elapsed.total_seconds() // 60))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def n_changes(self) -> int:
        """Cambi totali: fra gambe diverse piu' quelli interni a ciascuna gamba."""
        bookable = [leg for leg in self.legs if not leg.is_transfer]
        return max(0, len(bookable) - 1) + sum(leg.internal_changes for leg in bookable)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def n_tickets(self) -> int:
        """Biglietti distinti da comprare. E' questo che genera il rischio."""
        return len([leg for leg in self.legs if not leg.is_transfer])

    @computed_field  # type: ignore[prop-decorator]
    @property
    def modes(self) -> list[Mode]:
        seen: list[Mode] = []
        for leg in self.legs:
            if not leg.is_transfer and leg.mode not in seen:
                seen.append(leg.mode)
        return seen

    @computed_field  # type: ignore[prop-decorator]
    @property
    def operators(self) -> list[str]:
        seen: list[str] = []
        for leg in self.legs:
            name = leg.operator or leg.provider
            if not leg.is_transfer and name not in seen:
                seen.append(name)
        return seen

    @computed_field  # type: ignore[prop-decorator]
    @property
    def min_connection_min(self) -> int | None:
        """Margine piu' stretto fra due gambe consecutive, trasferimenti esclusi."""
        margins: list[int] = []
        bookable = [leg for leg in self.legs if not leg.is_transfer]
        for prev, nxt in zip(bookable, bookable[1:]):
            arrive = prev.arrive.astimezone(timezone.utc)
            depart = nxt.depart.astimezone(timezone.utc)
            transfer_min = sum(
                leg.duration_min
                for leg in self.legs
                if leg.is_transfer and arrive <= leg.depart.astimezone(timezone.utc) < depart
            )
            gap = int((depart - arrive).total_seconds() // 60)
            margins.append(gap - transfer_min)
        return min(margins) if margins else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def co2_kg(self) -> float | None:
        values = [leg.co2_kg for leg in self.legs if leg.co2_kg is not None]
        return round(sum(values), 1) if values else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def overnight(self) -> bool:
        """Vero se il viaggio copre la fascia 01:00-05:00 in movimento."""
        for leg in self.legs:
            if leg.is_transfer or leg.duration_min < 240:
                continue
            cursor = leg.depart
            while cursor < leg.arrive:
                if 1 <= cursor.hour < 5:
                    return True
                cursor += timedelta(minutes=30)
        return False

    @property
    def refundable(self) -> bool | None:
        values = [
            leg.fare.refundable
            for leg in self.legs
            if leg.fare and leg.fare.refundable is not None
        ]
        if not values:
            return None
        return all(values)


class Weights(BaseModel):
    """Pesi del ranker. Somma non vincolata: le componenti sono gia' normalizzate."""

    price: float = 1.0
    duration: float = 1.0
    risk: float = 0.8
    night_bonus: float = 0.5
    arrival_penalty: float = 0.6
    co2: float = 0.0


class DiscountKind(str, Enum):
    PERCENT = "percent"  # "-20% su Marino"
    AMOUNT = "amount"  # "-5 euro a corsa"
    FREE = "free"  # abbonamento: quella tratta e' gia' pagata


class Discount(BaseModel):
    """Una tessera, un abbonamento o una convenzione dichiarata dall'utente.

    Quasi nessun operatore espone le tariffe ridotte nella ricerca: lo sconto
    universitario di Marino, per dire, si sceglie piu' avanti nel flusso di
    acquisto. Ma un totale porta-a-porta che ignora la tessera che uno ha in
    tasca e' sbagliato quanto uno che ignora il bagaglio: cambia la classifica,
    non solo la cifra finale.

    Finche' `verified` e' falso il valore e' dichiarato dall'utente e non
    verificato con l'operatore, e in interfaccia va detto.
    """

    id: int | None = None
    name: str
    #: A cosa si applica: `all`, `provider:marino`, `mode:rail`, `mode:transfer`.
    scope: str = "all"
    kind: DiscountKind = DiscountKind.PERCENT
    #: Percentuale (20 = -20%) o importo in euro. Ignorato per `free`.
    value: float = 0.0
    #: Localita' fra cui vale, vuote = ovunque. Vale nei due versi.
    routes: list[str] = Field(default_factory=list)
    #: Giorni della settimana in cui vale (0 = lunedi), vuoti = tutti.
    weekdays: list[int] = Field(default_factory=list)
    valid_from: date | None = None
    valid_to: date | None = None
    active: bool = True
    #: Vero solo quando il prezzo ridotto arriva dall'operatore, non da qui.
    verified: bool = False
    #: Nomi delle offerte dell'operatore a cui questa tessera da' diritto
    #: ("FrecciaYOUNG", "SENIOR"). Quando l'operatore manda il prezzo ridotto
    #: nella risposta, si usa il suo invece di stimare: la stima invecchia con la
    #: percentuale, il suo prezzo no.
    offers: list[str] = Field(default_factory=list)
    #: Da quale voce del catalogo viene, se non e' stata scritta a mano.
    catalog_id: str | None = None

    def saving(self, amount: float) -> float:
        """Quanto toglie a una tariffa, senza mai portarla sotto zero."""
        if amount <= 0:
            return 0.0
        if self.kind is DiscountKind.FREE:
            return round(amount, 2)
        if self.kind is DiscountKind.PERCENT:
            return round(min(amount, amount * max(0.0, self.value) / 100.0), 2)
        return round(min(amount, max(0.0, self.value)), 2)


class SearchQuery(BaseModel):
    origin: str
    destination: str
    date: date
    pax: int = 1
    modes: set[Mode] = Field(default_factory=lambda: set(BOOKABLE_MODES))
    #: Se l'utente porta una valigia da stiva il confronto prezzi cambia del tutto.
    with_checked_bag: bool = False
    max_budget: float | None = None
    #: Deve essere a destinazione entro questo orario (giorno di `date`).
    arrive_by: time | None = None
    depart_after: time | None = None
    allow_night: bool = True
    max_changes: int = 3
    weights: Weights = Field(default_factory=Weights)
    #: Fermate scelte a mano fra quelle in cui la localita' si risolve. Vuote =
    #: tutte, che resta il comportamento normale: scrivere "Torino" continua a
    #: cercare da Porta Nuova, Porta Susa, Stura e dall'aeroporto insieme.
    origin_nodes: set[str] = Field(default_factory=set)
    destination_nodes: set[str] = Field(default_factory=set)
    #: Tessere e abbonamenti dell'utente, gia' filtrati per quelli attivi.
    discounts: list[Discount] = Field(default_factory=list)
    #: Testo libero originale, se la query viene dal parser in linguaggio naturale.
    raw_text: str | None = None

    def wants(self, mode: Mode) -> bool:
        return mode in self.modes


# --- Viaggio a tappe --------------------------------------------------------
#
# Le due partenze e le due mete della ricerca generano **combinazioni**: Matera
# verso Roma *e* Matera verso Torino, due possibilita' alternative fra cui
# scegliere. Un viaggio a tappe e' un'altra cosa: Matera, poi Roma, poi Torino,
# tutte e tre, in fila. Quello che le distingue e' che qui la data di una tappa
# **dipende** dalla precedente, e per questo non e' esprimibile ripetendo una
# ricerca: si arriva a Roma il 24, ci si ferma tre giorni, si riparte il 27.


class TripStage(BaseModel):
    """Una tappa: da dove a dove, e quanto ci si ferma prima di ripartire."""

    origin: str
    destination: str
    #: Data di partenza della tappa. Solo la prima ce l'ha per davvero; per le
    #: altre e' una previsione, che diventa certa quando si sceglie l'itinerario
    #: della tappa precedente e se ne conosce l'arrivo.
    date: date
    #: Giorni di sosta a destinazione prima della tappa successiva. Sull'ultima
    #: non significa niente e vale zero.
    stay_days: int = Field(default=0, ge=0, le=365)
    #: I vincoli di orario sono della tappa, non del viaggio: «devo essere a
    #: Roma entro le 21» non dice niente su quando arrivare a Torino tre giorni
    #: dopo.
    arrive_by: time | None = None
    depart_after: time | None = None


class TripPlan(BaseModel):
    """Un viaggio intero, come lo si racconta a voce."""

    stages: list[TripStage] = Field(min_length=1, max_length=8)
    pax: int = Field(default=1, ge=1, le=9)
    modes: set[Mode] = Field(default_factory=lambda: set(BOOKABLE_MODES))
    with_checked_bag: bool = False
    max_budget: float | None = None
    max_changes: int | None = None
    allow_night: bool = True
    raw_text: str | None = None


def chain_dates(stages: list[TripStage], arrivals: list[date] | None = None) -> list[TripStage]:
    """Rimette in fila le date: ogni tappa parte dopo l'arrivo della precedente.

    `arrivals` sono le date di arrivo davvero osservate, una per tappa, quando
    si conoscono: una corsa notturna arriva **il giorno dopo**, e ricalcolare
    sulla data di partenza sposterebbe indietro tutta la coda del viaggio di un
    giorno. Dove l'arrivo non si conosce si assume in giornata, che e' il caso
    normale e resta corretto finche' la tappa non e' notturna."""
    rimesse: list[TripStage] = []
    corrente: date | None = None
    for indice, stage in enumerate(stages):
        partenza = stage.date if corrente is None else max(stage.date, corrente)
        rimesse.append(stage.model_copy(update={"date": partenza}))
        arrivo = (arrivals[indice] if arrivals and indice < len(arrivals) else None) or partenza
        corrente = arrivo + timedelta(days=stage.stay_days)
    return rimesse


class ProviderStatus(str, Enum):
    PENDING = "pending"
    OK = "ok"
    EMPTY = "empty"  # ha risposto ma senza soluzioni
    TIMEOUT = "timeout"
    BLOCKED = "blocked"  # anti-bot, 403/429 non superato
    ERROR = "error"
    CIRCUIT_OPEN = "circuit_open"
    SKIPPED = "skipped"  # non copre questa rotta o questo modo


class ProviderReport(BaseModel):
    """Esito di un provider su una ricerca. Va mostrato in UI, mai nascosto."""

    provider: str
    status: ProviderStatus
    legs_found: int = 0
    elapsed_ms: int = 0
    detail: str | None = None


# --- I consigli ------------------------------------------------------------
#
# Tutti e tre i consigli — la classifica di una ricerca, il confronto fra piu'
# ricerche, il viaggio a tappe — arrivano da una chiamata della pagina con le
# soluzioni che la pagina ha gia'. Il consiglio sulla classifica prima nasceva
# invece dentro la ricerca, lato server: li' un'andata e ritorno era una coppia
# a schermo e due classifiche separate nel motore, e il consiglio parlava dei
# prezzi della sola andata mentre ogni scheda mostrava il totale. Con la pagina
# come sorgente unica, il modello vede per costruzione quello che si vede.
#
# Il corpo e' volutamente **compatto** e non un `Itinerary`: dieci campi
# dell'itinerario (`depart`, `duration_min`, `n_changes`, `co2_kg`...) sono
# `computed_field`, cioe' di sola uscita. Rimandandoli indietro Pydantic li
# ignorerebbe in silenzio e li ricalcolerebbe dalle gambe, quindi o si spedisce
# tutto l'albero delle gambe o si dice esplicitamente cosa serve. Meglio il
# secondo: qui si vede a colpo d'occhio su cosa ragiona il modello.


class AdviceOption(BaseModel):
    """Una soluzione, ridotta a quello che serve per consigliare."""

    #: Il numero scritto sulla scheda. E' l'unico modo che ha il modello di
    #: nominare una soluzione in modo verificabile: senza, diceva "l'opzione
    #: Itabus" e con due Itabus non si capiva quale, o citava un indice che a
    #: schermo non esisteva.
    ref: str = ""
    depart: datetime
    arrive: datetime
    duration_min: int
    total: float
    modes: list[str] = Field(default_factory=list)
    operators: list[str] = Field(default_factory=list)
    n_changes: int = 0
    n_tickets: int = 1
    flags: list[str] = Field(default_factory=list)
    #: Andata e ritorno: gli estremi della tratta di ritorno, se c'e'.
    return_depart: datetime | None = None
    return_arrive: datetime | None = None
    #: E la sua durata. Prima la pagina mandava in `duration_min` la **somma**
    #: di andata e ritorno insieme agli orari della sola andata: un numero che
    #: non compariva in nessun punto dello schermo.
    return_duration_min: int | None = None
    #: Quello che la scheda dice nel dettaglio e il totale da solo non dice:
    #: "prezzo a partire da", la tariffa ridotta dell'operatore, uno sconto
    #: dichiarato dall'utente e non verificato.
    notes: list[str] = Field(default_factory=list, max_length=8)


class RelaxedConstraint(BaseModel):
    """Un vincolo che il motore ha messo da parte per non dare pagina vuota.

    Forma identica a quella prodotta da `ranker.unmet_constraints`."""

    kind: str
    value: str | float | bool | None = None


class AdviceCandidate(BaseModel):
    """Una combinazione partenza-meta-data, con le sue prime opzioni."""

    label: str
    origin: str
    destination: str
    date: date
    return_date: date | None = None
    options: list[AdviceOption] = Field(default_factory=list, max_length=8)
    #: Quante soluzioni ha in tutto la classifica: senza, il modello vede cinque
    #: righe e crede che siano tutte, e scrive "e' l'unica sotto i 50 euro".
    found: int = 0
    #: La ricerca e' stata interrotta a meta': la classifica non e' definitiva.
    partial: bool = False
    relaxed: list[RelaxedConstraint] = Field(default_factory=list, max_length=6)


class AdviceRequest(BaseModel):
    """Una classifica sola da consigliare: quella che si sta guardando."""

    label: str = ""
    origin: str
    destination: str
    date: date
    return_date: date | None = None
    options: list[AdviceOption] = Field(default_factory=list, max_length=8)
    found: int = 0
    partial: bool = False
    relaxed: list[RelaxedConstraint] = Field(default_factory=list, max_length=6)
    pax: int = 1
    with_checked_bag: bool = False
    max_budget: float | None = None
    depart_after: str | None = None
    arrive_by: str | None = None
    allow_night: bool = True
    raw_text: str | None = None


class ChatMessage(BaseModel):
    """Un turno della conversazione, come lo rimanda la pagina."""

    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class ParseRequest(BaseModel):
    """Una frase da interpretare, con i turni che l'hanno preceduta.

    I turni servono a una cosa sola, ed e' quella che mancava: quando un dato
    indispensabile non c'e', il modello lo **chiede** invece di inventarlo, e la
    risposta e' il turno dopo. Senza `messages` il comportamento e' identico a
    prima, che e' il motivo per cui il campo ha un default."""

    text: str = Field(min_length=1, max_length=1000)
    messages: list[ChatMessage] = Field(default_factory=list, max_length=10)


class ChatRequest(AdviceRequest):
    """Il consiglio, ma continuabile.

    E' `AdviceRequest` piu' la storia dei turni, e l'eredita' non e' pigrizia:
    la conversazione deve vedere **esattamente** cio' che vede il consiglio,
    perche' e' lo stesso blocco a schermo. Due strutture separate avrebbero
    cominciato a divergere alla prima aggiunta, e il modello si sarebbe trovato
    a rispondere su una classifica diversa da quella che ha commentato."""

    #: I turni precedenti, dal piu' vecchio. Il tetto e' basso di proposito:
    #: una conversazione su una classifica non ha bisogno di memoria lunga, e
    #: una storia che cresce senza limite finisce per costare piu' della
    #: risposta e per far troncare i modelli piccoli.
    messages: list[ChatMessage] = Field(default_factory=list, max_length=12)
    #: Quello che l'utente ha messo nel Profilo. Oggi nessun prompt lo vede:
    #: arrivava al modello solo di rimbalzo, perche' la pagina lo aveva gia'
    #: scritto nei campi del modulo.
    profilo: dict[str, Any] = Field(default_factory=dict)


class CompareRequest(BaseModel):
    """Le combinazioni da mettere a confronto."""

    candidates: list[AdviceCandidate] = Field(min_length=2, max_length=12)
    pax: int = 1
    with_checked_bag: bool = False
    max_budget: float | None = None
    raw_text: str | None = None


class TripLeg(BaseModel):
    """Una tappa gia' risolta: quella che l'utente sta guardando in classifica."""

    label: str
    origin: str
    destination: str
    date: date
    stay_days: int = 0
    #: La soluzione in cima alla sua classifica, se ne ha trovata almeno una.
    chosen: AdviceOption | None = None
    #: Quante ne ha trovate in tutto, per dire "poche" quando sono poche.
    found: int = 0


class TripAdviceRequest(BaseModel):
    """Il viaggio intero da commentare."""

    legs: list[TripLeg] = Field(min_length=1, max_length=8)
    pax: int = 1
    with_checked_bag: bool = False
    max_budget: float | None = None
    raw_text: str | None = None


class SavedSearchIn(BaseModel):
    """Una ricerca da mettere nel profilo.

    `params` e' la query string della pagina: salvare e condividere sono lo
    stesso gesto, e riaprire non richiede di ricostruire niente."""

    name: str = Field(min_length=1, max_length=80)
    params: str = Field(min_length=1, max_length=4000)
    summary: str | None = Field(default=None, max_length=400)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distanza in linea d'aria in km."""
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def node_distance_km(a: Node, b: Node) -> float:
    return haversine_km(a.lat, a.lon, b.lat, b.lon)


def jsonable(value: Any) -> Any:
    """Serializzazione uniforme per gli eventi SSE."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value
