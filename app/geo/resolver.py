"""Da testo libero ("Torino", "Matera", "BRI") ai nodi di viaggio concreti.

La risoluzione avviene in due tempi:

1. **Ancora**: si trova il punto sulla mappa a cui si riferisce il testo. Puo'
   venire da una citta' del gazetteer, dal nome di una stazione, da un codice
   IATA o da coordinate esplicite.
2. **Nodi**: attorno all'ancora si raccolgono le fermate utilizzabili. Il raggio
   e' diverso per modo: una stazione a 80 km non e' "Torino", un aeroporto a
   80 km spesso si' (Bergamo per Milano, Bari Palese per Matera).

La selezione e' volutamente stretta. Restituire trenta stazioni intorno a
Torino significa moltiplicare per trenta le richieste ai provider e ottenere le
stesse soluzioni: si tengono i nodi principali e si taglia la coda.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from rapidfuzz import fuzz, process

from app.config import get_settings
from app.geo.datasets import CityEntry, load_index, normalize
from app.models import MODE_NODE_KINDS, Mode, Node, NodeKind, Place, haversine_km

logger = logging.getLogger(__name__)

#: Quanti nodi tenere per tipo attorno a un'ancora.
#:
#: Gli aeroporti fanno storia a se'. Due stazioni della stessa citta' danno
#: quasi sempre gli stessi treni, quindi tenerne tante e' spreco; due aeroporti
#: no: da Cuneo si vola dove da Torino non si vola, e ogni scalo escluso e'
#: un'occasione tolta senza che nessuno lo sappia. Costano poco anche quando
#: non servono, perche' la copertura per scalo e' in cache e una coppia non
#: servita esce senza nemmeno una richiesta di rete (misurato: fra +0,6% e
#: +5,6% di interrogazioni sulle tratte peggiori). Il numero qui sotto non e'
#: quindi una selezione ma una rete di sicurezza contro un caso patologico:
#: nel raggio di 120 km, in Italia, il massimo osservato e' sei (Milano).
MAX_NODES_PER_KIND: dict[NodeKind, int] = {
    NodeKind.STATION: 4,
    NodeKind.AIRPORT: 8,
    NodeKind.BUS_STOP: 3,
    NodeKind.PORT: 2,
}

#: L'ordine in cui si scandiscono i tipi di fermata. Era l'ordine di iterazione
#: di un `set` di Enum, cioe' dipendente da `PYTHONHASHSEED`: **cambiava a ogni
#: riavvio del server**. Misurato il 2026-08-22 su sei esecuzioni, sei ordini
#: diversi. Si vedeva a schermo — cercando «Canelli» a volte usciva prima
#: l'aeroporto di Genova — ma non era solo estetica: a parita' di punteggio
#: questo ordine fa da spareggio anche su quali fermate vengono davvero
#: interrogate, e uno spareggio casuale rende irriproducibile una ricerca.
KIND_SCAN_ORDER: tuple[NodeKind, ...] = (
    NodeKind.STATION,
    NodeKind.BUS_STOP,
    NodeKind.AIRPORT,
    NodeKind.PORT,
)

#: Quanto pesa un chilometro di distanza dall'ancora nel punteggio di un nodo.
#: Senza, la distanza contava solo a parita' di punteggio, e vinceva sempre
#: l'aeroporto piu' grande: Milano scartava Linate (7 km) per Malpensa e
#: Bergamo, Firenze scartava Peretola (6 km) per Pisa e Bologna, Venezia
#: metteva Trieste (98 km) davanti a Treviso (26 km). Con 0,35 cento
#: chilometri valgono piu' del bonus di stazione principale (+30), che e'
#: esattamente il compromesso voluto: uno scalo grande merita un po' di strada,
#: non un'ora e mezza.
DISTANCE_PENALTY_PER_KM = 0.35


IATA_RE = re.compile(r"^[A-Za-z]{3}$")
COORDS_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,;]\s*(-?\d+(?:\.\d+)?)\s*$")
#: "Matera, IT" / "Torino (Italia)" -> si scarta il suffisso di paese.
COUNTRY_SUFFIX_RE = re.compile(r"[,(]\s*[A-Za-z ]{2,20}\s*\)?$")

#: Quanto lontano puo' stare la citta' a cui attribuire un'ancora che non ne
#: dichiara una. Oltre, meglio nessuna citta' che quella sbagliata.
NEAREST_CITY_KM = 25.0

MIN_FUZZY_SCORE = 82.0
#: Rapporto minimo fra le lunghezze di due nomi perche' possano essere lo stesso
#: posto. Serve a impedire che una sigla corta vinca dentro un nome lungo.
MIN_LENGTH_RATIO = 0.6


class PlaceNotFound(ValueError):
    """Il testo non corrisponde a nessuna localita' conosciuta."""


@dataclass
class _Anchor:
    label: str
    lat: float
    lon: float
    country: str | None
    #: Quanto e' affidabile questa ancora, per scegliere fra piu' candidati.
    score: float
    #: La citta' a cui l'ancora appartiene, quando e' diversa dall'etichetta.
    #: Un'ancora puo' essere una fermata ("Torino Porta Nuova") e la sua
    #: etichetta non e' un nome di citta': usarla come tale rompe gli operatori
    #: che cercano per citta'. Vedi `_collect`.
    city: str | None = None


#: Quanto vale essere una citta' in cui **sappiamo interrogare qualcuno**.
#: «Valencia» in Venezuela ha il doppio degli abitanti di quella spagnola, e
#: senza questo la ricerca ci finiva dentro; ma per la spagnola conosciamo le
#: stazioni e gli operatori, e quella e' la risposta utile. Vale quanto un
#: fattore dieci di popolazione, non di piu': una metropoli mondiale resta
#: raggiungibile.
BONUS_TRASPORTO = 12.0

#: Quanto scala un'ancora trovata attraverso un nome in un'altra lingua invece
#: che col nome proprio. Cercando «napoli» esistono due candidate: la voce
#: italiana che si chiama cosi', e «Naples» che ha «napoli» fra i suoi alias.
#: Senza questo vinceva la seconda per popolazione, e chi cercava Napoli si
#: vedeva rispondere «Naples».
PENALITA_ALIAS = 0.90


def _city_priority(city: CityEntry) -> float:
    return 100.0 + city.weight * 10.0 + (BONUS_TRASPORTO if city.transport else 0.0)


def _node_priority(node: Node) -> float:
    """Proxy di importanza: le stazioni principali e quelle servite da piu'
    operatori sono quasi sempre quelle che l'utente intende."""
    priority = 60.0
    if node.is_main:
        priority += 30.0
    priority += min(20.0, len(node.provider_ids) * 4.0)
    if node.kind is NodeKind.AIRPORT:
        priority += 5.0
    return priority


#: Distanze che separano un'isola dalla terraferma nei dati. Su un'isola il
#: porto e' addosso al paese e la stazione piu' vicina e' lontana e oltre il
#: mare; in una citta' portuale di terraferma sono entrambi vicini.
ISLAND_PORT_KM = 5.0
ISLAND_GROUND_KM = 8.0


def _drop_mainland_if_island(anchor: "_Anchor", nodes: list[Node]) -> list[Node]:
    """Toglie le fermate di terraferma quando la localita' e' un'isola.

    Il raggio di ricerca non conosce il mare: attorno a Favignana finivano le
    stazioni di Trapani e Marsala, a quindici e trenta chilometri, e il motore
    proponeva di raggiungere l'isola in treno. Il segnale che distingue i due
    casi e' nei dati: su un'isola il porto e' a ridosso del paese e non c'e'
    nessuna fermata di terra vicina."""
    ports = [n for n in nodes if n.kind is NodeKind.PORT]
    if not ports:
        return nodes

    def near(node: Node) -> float:
        return haversine_km(anchor.lat, anchor.lon, node.lat, node.lon)

    if min(near(port) for port in ports) > ISLAND_PORT_KM:
        return nodes
    ground = [n for n in nodes if n.kind is not NodeKind.PORT]
    if any(near(node) <= ISLAND_GROUND_KM for node in ground):
        return nodes
    return ports


class Resolver:
    def __init__(self) -> None:
        self._index = load_index()
        self._settings = get_settings()

        self._cities_by_name: dict[str, list[CityEntry]] = {}
        #: Le chiavi che sono il **nome** della citta', non un suo alias.
        self._city_own_name: set[tuple[str, int]] = set()
        for city in self._index.cities:
            for key in [city.normalized, *city.aliases]:
                self._cities_by_name.setdefault(key, []).append(city)
            self._city_own_name.add((city.normalized, id(city)))

        self._nodes_by_name: dict[str, list[Node]] = {}
        for node in self._index.nodes:
            self._nodes_by_name.setdefault(normalize(node.name), []).append(node)
            # Anche la citta' dell'aeroporto: il nodo si chiama «Tokyo Haneda
            # International Airport» e chi cerca scrive «Tokyo». Il campo c'era
            # gia' nel dato e non lo leggeva nessuno.
            if node.city:
                chiave = normalize(node.city)
                if chiave and chiave not in self._nodes_by_name:
                    self._nodes_by_name[chiave] = []
                if chiave:
                    self._nodes_by_name[chiave].append(node)

        # Corpus per il matching fuzzy, usato solo quando l'esatto fallisce.
        #
        # I nomi in altre lingue **non** entrano qui, e sono trecentocinquantamila:
        # in una mappa esatta non pesano, in un confronto per somiglianza
        # sarebbero altrettante occasioni di sbagliare. Cercare per somiglianza
        # e' l'ultima spiaggia, e va fatta sul minor numero di candidati
        # possibile.
        self._corpus: list[str] = [
            city.normalized for city in self._index.cities
        ] + list(self._nodes_by_name)

    # ------------------------------------------------------------------ ancore

    def _anchors_exact(self, key: str) -> list[_Anchor]:
        anchors: list[_Anchor] = []
        for city in self._cities_by_name.get(key, []):
            priorita = _city_priority(city)
            if (key, id(city)) not in self._city_own_name:
                priorita *= PENALITA_ALIAS
            anchors.append(
                _Anchor(
                    city.name, city.lat, city.lon, city.country, priorita,
                    city=city.name,
                )
            )
        for node in self._nodes_by_name.get(key, []):
            anchors.append(
                _Anchor(
                    node.name, node.lat, node.lon, node.country, _node_priority(node),
                    city=node.city,
                )
            )
        return anchors

    def _anchors_fuzzy(self, key: str) -> list[_Anchor]:
        matches = process.extract(
            key, self._corpus, scorer=fuzz.WRatio, limit=8, score_cutoff=MIN_FUZZY_SCORE
        )
        anchors: list[_Anchor] = []
        for matched_key, similarity, _ in matches:
            # WRatio premia le sottostringhe: senza questo vincolo "Costadedoi"
            # finisce su "Os" e "Poggibonsi" su "Bo", con punteggi altissimi e
            # risultati assurdi. Due nomi di lunghezza troppo diversa non sono
            # lo stesso posto.
            shorter, longer = sorted((len(matched_key), len(key)))
            if longer and shorter / longer < MIN_LENGTH_RATIO:
                continue
            for anchor in self._anchors_exact(matched_key):
                # La somiglianza testuale abbassa la priorita' in proporzione:
                # un match all'85% non deve battere un esatto.
                anchor.score *= similarity / 100.0
                anchors.append(anchor)
        return anchors

    def _find_anchor(self, query: str) -> _Anchor:
        text = query.strip()
        if not text:
            raise PlaceNotFound("localita' vuota")

        coords = COORDS_RE.match(text)
        if coords:
            lat, lon = float(coords.group(1)), float(coords.group(2))
            return self._with_city(_Anchor(f"{lat:.4f}, {lon:.4f}", lat, lon, None, 1000.0))

        if IATA_RE.match(text):
            airport = self._index.by_iata.get(text.upper())
            if airport is not None:
                return self._with_city(_Anchor(
                    airport.name, airport.lat, airport.lon, airport.country, 1000.0
                ))

        candidates: list[_Anchor] = []
        key = normalize(text)
        candidates += self._anchors_exact(key)

        if not candidates:
            trimmed = normalize(COUNTRY_SUFFIX_RE.sub("", text))
            if trimmed and trimmed != key:
                candidates += self._anchors_exact(trimmed)
                key = trimmed

        if not candidates:
            candidates += self._anchors_fuzzy(key)

        if not candidates:
            raise PlaceNotFound(f"localita' non trovata: {query!r}")

        candidates.sort(key=lambda anchor: anchor.score, reverse=True)
        return self._with_city(candidates[0])

    def _with_city(self, anchor: _Anchor) -> _Anchor:
        """Assicura che l'ancora sappia a quale citta' appartiene."""
        if not anchor.city:
            anchor.city = self._nearest_city(anchor.lat, anchor.lon)
        return anchor

    def _nearest_city(self, lat: float, lon: float) -> str | None:
        """La citta' a cui attribuire un punto.

        Serve quando l'ancora non e' una citta' ma una fermata o un codice
        IATA, e la fermata non dichiara la propria citta' (nel dataset delle
        stazioni succede spesso). Senza, la citta' diventerebbe l'etichetta
        dell'ancora — "Torino Porta Nuova" — e quella stringa e' la chiave con
        cui FlixBus, Itabus, Albatross e Grimaldi cercano: nessuno di loro
        conosce una citta' con quel nome, e sparirebbero tutti in silenzio.

        **Non** la piu' vicina in assoluto: la piu' importante fra le vicine.
        Con il solo gazetteer europeo le due cose coincidevano; da quando ci
        sono le trentaquattromila del mondo, dentro Torino ci stanno anche i
        suoi quartieri sopra i quindicimila abitanti, e Porta Nuova finiva
        attribuita a **San Salvario** — che nessun operatore vende. Un peso
        contro un chilometro: cosi' una fermata in periferia resta della sua
        citta', e una citta' vera a venti chilometri non viene scavalcata dal
        paese accanto."""
        best: CityEntry | None = None
        best_score = float("-inf")
        for city in self._index.cities:
            # Pre-filtro rettangolare, come in `nodes_within`: la scansione e'
            # su tutto il gazetteer e la haversine costa piu' di due sottrazioni.
            if abs(city.lat - lat) > 0.4 or abs(city.lon - lon) > 0.6:
                continue
            distance = haversine_km(lat, lon, city.lat, city.lon)
            if distance >= NEAREST_CITY_KM:
                continue
            score = city.weight * 10.0 - distance
            if score > best_score:
                best, best_score = city, score
        return best.name if best else None

    # -------------------------------------------------------------------- nodi

    def _usable(self, node: Node) -> bool:
        """Un nodo senza modo di essere identificato da qualche operatore e' inutile.

        Porti e aeroporti fanno eccezione: li identifichiamo per nome e IATA,
        gli adapter relativi non hanno bisogno di un ID nel dataset."""
        if node.kind in (NodeKind.PORT, NodeKind.AIRPORT):
            return True
        return bool(node.provider_ids)

    def _collect(self, anchor: _Anchor, modes: set[Mode]) -> list[Node]:
        wanted_kinds: set[NodeKind] = set()
        for mode in modes:
            wanted_kinds |= set(MODE_NODE_KINDS.get(mode, frozenset()))

        settings = self._settings
        selected: list[Node] = []
        radii = {
            NodeKind.AIRPORT: settings.air_radius_km,
            NodeKind.PORT: settings.port_radius_km,
        }
        # I tipi noti nel loro ordine, poi gli altri in ordine alfabetico. La
        # coda non e' pedanteria: senza, un tipo aggiunto domani a
        # `MODE_NODE_KINDS` e dimenticato qui sparirebbe in silenzio, che e'
        # peggio del disordine che questa costante e' venuta a togliere.
        noti = [k for k in KIND_SCAN_ORDER if k in wanted_kinds]
        altri = sorted(wanted_kinds - set(KIND_SCAN_ORDER), key=lambda k: k.value)
        for kind in noti + altri:
            radius = radii.get(kind, settings.ground_radius_km)
            nearby = [
                node
                for node in self._index.nodes_within(
                    anchor.lat, anchor.lon, radius, kinds={kind}
                )
                if self._usable(node)
            ]
            distanze = {
                node.id: haversine_km(anchor.lat, anchor.lon, node.lat, node.lon)
                for node in nearby
            }
            nearby.sort(
                key=lambda node: (
                    -(_node_priority(node) - DISTANCE_PENALTY_PER_KM * distanze[node.id]),
                    distanze[node.id],
                )
            )
            selected.extend(nearby[: MAX_NODES_PER_KIND.get(kind, 4)])

        selected = _drop_mainland_if_island(anchor, selected)

        # Un nodo puo' essere stato scelto per due modi diversi: si deduplica
        # tenendo l'ordine, che e' gia' quello di preferenza.
        seen: set[str] = set()
        unique: list[Node] = []
        for node in selected:
            if node.id in seen:
                continue
            seen.add(node.id)
            if node.city:
                unique.append(node)
                continue
            # Molte fermate del dataset non dichiarano la citta', e il loro nome
            # non la contiene: la stazione di riferimento di Cosenza si chiama
            # "Paola". Gli operatori che ragionano per citta' non la
            # riconoscerebbero mai. Si attacca la localita' che l'utente ha
            # davvero chiesto, su una copia, perche' i nodi dell'indice sono
            # condivisi fra tutte le ricerche.
            #
            # `anchor.city`, non `anchor.label`: se l'ancora e' una fermata
            # l'etichetta e' "Torino Porta Nuova", e quella stringa finirebbe in
            # `city_key()`, cioe' nella chiave con cui FlixBus, Itabus,
            # Albatross e Grimaldi cercano e deduplicano. Cercare "Torino Porta
            # Nuova" invece di "Torino" li faceva sparire tutti, in silenzio.
            unique.append(node.model_copy(update={"city": anchor.city or anchor.label}))
        return unique

    # ------------------------------------------------------------------ public

    def resolve(self, query: str, modes: set[Mode] | None = None) -> Place:
        modes = modes or {Mode.RAIL, Mode.BUS, Mode.AIR, Mode.FERRY}
        anchor = self._find_anchor(query)
        return self._place(query, anchor, modes)

    def resolve_at(
        self,
        lat: float,
        lon: float,
        label: str,
        country: str | None = None,
        modes: set[Mode] | None = None,
    ) -> Place:
        """Risoluzione da coordinate note, senza passare dal testo.

        Serve per gli hub, di cui conosciamo gia' la posizione esatta. Farli
        passare dal matching testuale e' pericoloso: "Ancona" somiglia a "Ona",
        che e' un paese in Spagna, e un solo scambio del genere manda la ricerca
        a interrogare mezza Europa per niente."""
        modes = modes or {Mode.RAIL, Mode.BUS, Mode.AIR, Mode.FERRY}
        anchor = _Anchor(label, lat, lon, country, 1000.0)
        return self._place(label, anchor, modes)

    def _place(self, query: str, anchor: _Anchor, modes: set[Mode]) -> Place:
        nodes = self._collect(anchor, modes)
        if not nodes:
            raise PlaceNotFound(
                f"{anchor.label}: nessuna fermata utilizzabile nel raggio previsto"
            )
        return Place(
            query=query,
            label=anchor.label,
            lat=anchor.lat,
            lon=anchor.lon,
            country=anchor.country,
            nodes=nodes,
        )

    def suggest(self, prefix: str, limit: int = 8) -> list[str]:
        """Suggerimenti per il campo di ricerca della UI."""
        key = normalize(prefix)
        if len(key) < 2:
            return []
        matches = process.extract(
            key, self._corpus, scorer=fuzz.WRatio, limit=limit * 3, score_cutoff=70
        )
        labels: list[str] = []
        for matched_key, _, _ in matches:
            for anchor in self._anchors_exact(matched_key):
                if anchor.label not in labels:
                    labels.append(anchor.label)
        return labels[:limit]


_resolver: Resolver | None = None


def get_resolver() -> Resolver:
    global _resolver
    if _resolver is None:
        _resolver = Resolver()
    return _resolver
