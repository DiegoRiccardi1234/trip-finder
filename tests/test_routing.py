"""Il motore di composizione, costo e classifica: nessuna rete coinvolta."""

from __future__ import annotations

from datetime import time

from app.models import Itinerary, Mode, NodeKind, RiskFlag, SearchQuery
from app.routing import composer, cost, feasibility, ranker, transfers
from app.routing.hub_graph import candidate_hubs

from ._datasets import needs_datasets
from .conftest import (
    BARI_AIR,
    make_node,
    BARI_C,
    MATERA_BUS,
    TORINO_AIR,
    TORINO_PN,
    at,
    make_leg,
)


# ------------------------------------------------------------------ hub graph


def test_bari_e_sempre_fra_gli_hub_per_matera() -> None:
    """Il caso che giustifica la regola del gateway.

    Matera non ha aeroporto ne' ferrovia nazionale: senza Bari fra gli hub, il
    viaggio migliore (volo piu' pullman) non viene mai nemmeno cercato. Per pura
    geometria Bari perde contro citta' come Genova, che stanno sulla linea
    Torino-Matera ma non hanno alcun collegamento con Matera."""
    hubs = candidate_hubs(
        45.0703, 7.6869, 40.6663, 16.6044, modes={Mode.RAIL, Mode.BUS, Mode.AIR}
    )
    assert "Bari" in [hub.name for hub in hubs]


@needs_datasets
def test_fuori_europa_gli_scali_esistono() -> None:
    """Gli hub erano ottantotto citta' europee scritte a mano: su una tratta
    asiatica l'elenco restava vuoto e restavano solo i voli diretti, che fra
    Tokyo e Lima non esistono. Ora le citta' grandi del mondo con un aeroporto
    fanno da scalo — per il solo aereo, perche' fuori Europa non c'e' un adapter
    ferroviario o di pullman che possa servire quel cambio."""
    from app.routing.hub_graph import world_hubs

    hubs = candidate_hubs(35.68, 139.69, -12.04, -77.03, modes={Mode.AIR})
    assert hubs, "nessuno scalo fra Tokyo e Lima"

    mondiali = world_hubs()
    assert len(mondiali) > 100
    assert all(hub.modes == {Mode.AIR} for hub in mondiali)
    # I paesi con la lista curata restano com'erano: li' i pesi scritti a mano
    # dicono cose che la popolazione non sa (Bologna e' uno snodo, non una
    # metropoli).
    assert not any(hub.country == "IT" for hub in mondiali)


def test_nessun_hub_per_tratte_brevi() -> None:
    # Torino-Milano: cambiare non compra mai niente.
    hubs = candidate_hubs(45.0703, 7.6869, 45.4642, 9.19, modes={Mode.RAIL})
    assert hubs == []


def test_hub_troppo_fuori_strada_scartato() -> None:
    hubs = candidate_hubs(
        45.0703, 7.6869, 40.6663, 16.6044, modes={Mode.RAIL, Mode.BUS, Mode.AIR}
    )
    # Amburgo e' nella lista degli hub ma allunga il viaggio di tre volte.
    assert "Hamburg" not in [hub.name for hub in hubs]


# ------------------------------------------------------------------- costo


def test_bagaglio_stiva_solo_dove_si_paga(query: SearchQuery) -> None:
    volo = make_leg("ryanair", Mode.AIR, TORINO_AIR, BARI_AIR, at(8), at(9, 35), 40.0)
    treno = make_leg("trenitalia", Mode.RAIL, TORINO_PN, BARI_C, at(8), at(17), 90.0)

    senza = query.model_copy(update={"with_checked_bag": False})
    con = query.model_copy(update={"with_checked_bag": True})

    assert cost.compute([volo], senza).total == 40.0
    assert cost.compute([volo], con).total == 85.0  # 40 di tariffa + 45 di stiva
    # Sul treno il bagaglio non si paga: il totale non deve muoversi.
    assert cost.compute([treno], con).total == 90.0


def test_gamba_senza_prezzo_non_vale_zero(query: SearchQuery) -> None:
    """Una tratta di cui non conosciamo il prezzo non deve sembrare gratis.

    E' il caso di FAL, che pubblica gli orari ma non le tariffe: contarla zero
    la renderebbe automaticamente l'opzione piu' economica di tutte, e vincerebbe
    la classifica proprio perche' ne sappiamo meno."""
    ignota = make_leg("misterioso", Mode.BUS, BARI_C, MATERA_BUS, at(18), at(19, 30))
    breakdown = cost.compute([ignota], query)

    assert breakdown.total > 0, "una tratta senza prezzo e' finita a zero"
    assert breakdown.has_estimates
    # La stima deve restare nell'ordine di grandezza giusto per una tratta
    # di una cinquantina di chilometri: non zero, ma nemmeno un numero a caso.
    assert 2.0 <= breakdown.total <= 30.0
    assert RiskFlag.ESTIMATED_COST in ranker.compute_flags(
        Itinerary(id="x", legs=[ignota], cost=breakdown)
    )


def test_il_prezzo_a_partire_da_e_una_stima(query: SearchQuery) -> None:
    """Trenitalia manda certi prezzi come «a partire da»: e' il minimo del
    giorno, non il prezzo di quella corsa. Finiva nel totale come una cifra
    certa, e l'avvertenza viveva solo dentro «Dettagli» — mentre il totale in
    grande e' proprio quello su cui si decide."""
    leg = make_leg("trenitalia", Mode.RAIL, TORINO_PN, BARI_C, at(8), at(16), price=39.0)
    leg.fare.indicative = True

    breakdown = cost.compute([leg], query)

    assert breakdown.total == 39.0, "il prezzo resta quello, cambia come lo si dice"
    assert breakdown.has_estimates
    assert any("a partire da" in line.label for line in breakdown.lines)
    assert RiskFlag.ESTIMATED_COST in ranker.compute_flags(
        Itinerary(id="x", legs=[leg], cost=breakdown)
    )


def test_un_prezzo_normale_non_diventa_una_stima(query: SearchQuery) -> None:
    """La controprova: senza il segnale dell'operatore la voce resta certa,
    altrimenti il badge comparirebbe ovunque e smetterebbe di dire qualcosa."""
    leg = make_leg("trenitalia", Mode.RAIL, TORINO_PN, BARI_C, at(8), at(16), price=39.0)

    breakdown = cost.compute([leg], query)

    assert not breakdown.has_estimates
    assert all("a partire da" not in line.label for line in breakdown.lines)


def test_un_prezzo_in_sterline_non_diventa_euro_per_finta(query: SearchQuery) -> None:
    """Il totale e' la cifra su cui si decide, e la classifica ci ordina sopra.

    Prima `compute` sommava `leg.fare.amount` senza mai guardare la valuta: 95
    sterline finivano nel totale come 95 euro, stampate «95,00 €», sbagliate del
    venti per cento e senza un avviso."""
    from app.routing import rates

    rates.imposta_per_test({"GBP": 0.85573}, "2026-07-31")
    leg = make_leg("ryanair", Mode.AIR, TORINO_PN, BARI_C, at(8), at(10), price=95.0)
    leg.fare.currency = "GBP"

    breakdown = cost.compute([leg], query)
    riga = next(line for line in breakdown.lines if line.kind == "fare")

    assert riga.amount == 111.02  # 95 / 0,85573
    assert "[GBP]" in riga.label, "la valuta dell'operatore resta in chiaro"
    assert not riga.estimated, "convertita al cambio del giorno, non stimata"


def test_una_valuta_senza_cambio_non_passa_per_certa(query: SearchQuery) -> None:
    """Senza tasso non si inventa: la cifra resta quella e la riga si dichiara
    incerta, perche' e' l'unica cosa onesta da fare con un numero che non e'
    confrontabile con gli altri."""
    from app.routing import rates

    rates.imposta_per_test({"GBP": 0.85573}, "2026-07-31")
    leg = make_leg("misterioso", Mode.AIR, TORINO_PN, BARI_C, at(8), at(10), price=50.0)
    leg.fare.currency = "AUD"

    breakdown = cost.compute([leg], query)
    riga = next(line for line in breakdown.lines if line.kind == "fare")

    assert riga.amount == 50.0
    assert riga.estimated
    assert breakdown.has_estimates


def test_ultimo_miglio_curato_bari_matera() -> None:
    """Il collegamento Bari aeroporto - Matera e' quello che ribalta i conti su
    un volo low cost, e va preso dalla tabella curata, non stimato."""
    estimate = transfers.estimate(BARI_AIR, MATERA_BUS, destination_label="Matera")
    assert not estimate.estimated
    assert 60 <= estimate.duration_min <= 90
    assert 4.0 <= estimate.price_eur <= 9.0


def test_stima_generica_cresce_con_la_distanza() -> None:
    vicino = transfers.estimate(BARI_AIR, BARI_C)
    lontano = transfers.estimate(TORINO_AIR, BARI_C)
    assert vicino.duration_min < lontano.duration_min
    assert vicino.price_eur < lontano.price_eur


# -------------------------------------------------------------- coincidenze


def test_biglietti_separati_richiedono_piu_margine() -> None:
    treno = make_leg("trenitalia", Mode.RAIL, TORINO_PN, BARI_C, at(8), at(17))
    stesso = make_leg("trenitalia", Mode.RAIL, BARI_C, MATERA_BUS, at(18), at(19))
    altro = make_leg("flixbus", Mode.BUS, BARI_C, MATERA_BUS, at(18), at(19))

    assert feasibility.required_margin(treno, altro) > feasibility.required_margin(
        treno, stesso
    )
    assert feasibility.separate_tickets(treno, altro)
    assert not feasibility.separate_tickets(treno, stesso)


def test_prendere_un_aereo_richiede_il_tempo_del_check_in() -> None:
    treno = make_leg("trenitalia", Mode.RAIL, TORINO_PN, TORINO_AIR, at(6), at(6, 45))
    volo = make_leg("ryanair", Mode.AIR, TORINO_AIR, BARI_AIR, at(7, 30), at(9))
    # 45 minuti fra treno e volo non bastano: il vincolo e' il check-in.
    assert not feasibility.connection_ok(treno, volo)

    volo_tardi = volo.model_copy(update={"depart": at(9, 30), "arrive": at(11)})
    assert feasibility.connection_ok(treno, volo_tardi)


def test_attesa_infinita_non_e_una_coincidenza() -> None:
    treno = make_leg("trenitalia", Mode.RAIL, TORINO_PN, BARI_C, at(8), at(12))
    domani = make_leg("flixbus", Mode.BUS, BARI_C, MATERA_BUS, at(9, day_offset=1), at(10, day_offset=1))
    assert not feasibility.connection_ok(treno, domani)


def test_trasferimento_generato_fra_fermate_diverse() -> None:
    volo = make_leg("ryanair", Mode.AIR, TORINO_AIR, BARI_AIR, at(8), at(9, 35))
    bus = make_leg("flixbus", Mode.BUS, BARI_C, MATERA_BUS, at(12), at(13, 15))
    transfer = feasibility.transfer_between(volo, bus)
    assert transfer is not None
    assert transfer.mode is Mode.TRANSFER
    assert transfer.origin.id == BARI_AIR.id and transfer.destination.id == BARI_C.id


# ------------------------------------------------------------------ classifica


def _itinerary(legs, query: SearchQuery) -> Itinerary:
    return Itinerary(id="x", legs=legs, cost=cost.compute(legs, query))


def test_biglietti_separati_segnalati_e_penalizzati(query: SearchQuery) -> None:
    unico = _itinerary(
        [make_leg("flixbus", Mode.BUS, TORINO_PN, MATERA_BUS, at(22), at(16, day_offset=1), 80.0)],
        query,
    )
    spezzato = _itinerary(
        [
            make_leg("trenitalia", Mode.RAIL, TORINO_PN, BARI_C, at(8), at(17), 60.0),
            make_leg("flixbus", Mode.BUS, BARI_C, MATERA_BUS, at(18), at(19, 15), 20.0),
        ],
        query,
    )
    assert RiskFlag.SEPARATE_TICKETS not in ranker.compute_flags(unico)
    assert RiskFlag.SEPARATE_TICKETS in ranker.compute_flags(spezzato)
    assert ranker.risk_score(spezzato) > ranker.risk_score(unico)


def test_notturno_premiato_arrivo_notturno_penalizzato(query: SearchQuery) -> None:
    notturno = _itinerary(
        [make_leg("flixbus", Mode.BUS, TORINO_PN, MATERA_BUS, at(21), at(11, day_offset=1), 80.0)],
        query,
    )
    diurno = _itinerary(
        [make_leg("flixbus", Mode.BUS, TORINO_PN, MATERA_BUS, at(8), at(22), 80.0)],
        query,
    )
    assert notturno.overnight and not diurno.overnight
    assert ranker.night_bonus(notturno) > ranker.night_bonus(diurno)

    arrivo_alle_tre = _itinerary(
        [make_leg("flixbus", Mode.BUS, TORINO_PN, MATERA_BUS, at(12), at(3, day_offset=1), 80.0)],
        query,
    )
    assert ranker.arrival_penalty(arrivo_alle_tre) == 1.0
    assert ranker.arrival_penalty(diurno) == 0.0


def test_cambi_interni_contano_ma_non_come_biglietti(query: SearchQuery) -> None:
    itinerary = _itinerary(
        [
            make_leg(
                "trenitalia", Mode.RAIL, TORINO_PN, BARI_C, at(8), at(17), 90.0,
                internal_changes=2,
            )
        ],
        query,
    )
    assert itinerary.n_tickets == 1
    assert itinerary.n_changes == 2


def test_il_peso_a_zero_annulla_il_criterio(query: SearchQuery) -> None:
    caro_e_veloce = _itinerary(
        [make_leg("a", Mode.AIR, TORINO_AIR, BARI_AIR, at(8), at(9, 30), 200.0)], query
    )
    economico_e_lento = _itinerary(
        [make_leg("b", Mode.BUS, TORINO_PN, MATERA_BUS, at(8), at(23), 30.0)], query
    )

    solo_prezzo = query.model_copy(
        update={"weights": query.weights.model_copy(update={"duration": 0.0, "risk": 0.0,
                                                           "night_bonus": 0.0,
                                                           "arrival_penalty": 0.0})}
    )
    classifica = ranker.rank([caro_e_veloce, economico_e_lento], solo_prezzo)
    assert classifica[0].cost.total == 30.0

    solo_durata = query.model_copy(
        update={"weights": query.weights.model_copy(update={"price": 0.0, "risk": 0.0,
                                                           "night_bonus": 0.0,
                                                           "arrival_penalty": 0.0})}
    )
    classifica = ranker.rank([caro_e_veloce, economico_e_lento], solo_durata)
    assert classifica[0].cost.total == 200.0


def test_doppioni_quasi_identici_collassati(query: SearchQuery) -> None:
    """Lo stesso pullman preso a due fermate della stessa citta' non deve
    occupare due posti in classifica per dire la stessa cosa."""
    uno = _itinerary(
        [make_leg("marino", Mode.BUS, TORINO_PN, MATERA_BUS, at(18, 30), at(8, 45, 1), 83.0)],
        query,
    )
    quasi_uguale = _itinerary(
        [make_leg("marino", Mode.BUS, TORINO_AIR, MATERA_BUS, at(18, 55), at(8, 45, 1), 83.0)],
        query,
    )
    diverso = _itinerary(
        [make_leg("marino", Mode.BUS, TORINO_PN, MATERA_BUS, at(6, 30), at(21, 35), 75.0)],
        query,
    )

    collassati = ranker.collapse_similar([uno, quasi_uguale, diverso])
    assert len(collassati) == 2
    assert uno in collassati and diverso in collassati


def test_in_testa_le_famiglie_e_i_primati(query: SearchQuery) -> None:
    """I primi risultati devono dire **quali strade esistono**, e non nascondere
    il piu' economico dietro sei varianti della stessa."""
    caro_ma_buono = _itinerary(
        [make_leg("marino", Mode.BUS, TORINO_PN, MATERA_BUS, at(18), at(8, day_offset=1), 83.0)],
        query,
    )
    economico = _itinerary(
        [make_leg("itabus", Mode.BUS, TORINO_PN, MATERA_BUS, at(22), at(18, day_offset=1), 39.99)],
        query,
    )
    veloce = _itinerary(
        [make_leg("ryanair", Mode.AIR, TORINO_AIR, BARI_AIR, at(7), at(8, 35), 120.0)],
        query,
    )
    ordinati = ranker.rank([caro_ma_buono, economico, veloce], query)
    testa = ranker.diversify(ordinati)[:3]

    assert economico in testa, "il piu' economico non deve sparire dalla testa"
    assert veloce in testa, "il piu' rapido non deve sparire dalla testa"
    assert caro_ma_buono in testa


def test_filtri_espliciti_rispettati(query: SearchQuery) -> None:
    caro = _itinerary(
        [make_leg("a", Mode.AIR, TORINO_AIR, BARI_AIR, at(8), at(9, 30), 200.0)], query
    )
    economico = _itinerary(
        [make_leg("b", Mode.BUS, TORINO_PN, MATERA_BUS, at(8), at(20), 30.0)], query
    )
    con_budget = query.model_copy(update={"max_budget": 100.0})
    assert ranker.filter_by_query([caro, economico], con_budget) == [economico]


def test_arrivare_il_giorno_dopo_sfora_l_ora_di_arrivo(query: SearchQuery) -> None:
    """"Entro le 23:59" non vuol dire la notte seguente. E' il caso che si
    incontra su Torino-Matera: quasi tutto quello che parte nel pomeriggio
    arriva il giorno dopo."""
    domani = _itinerary(
        [make_leg("a", Mode.BUS, TORINO_PN, MATERA_BUS, at(18, 30), at(8, 45, day_offset=1), 66.0)],
        query,
    )
    oggi = _itinerary(
        [make_leg("b", Mode.AIR, TORINO_AIR, BARI_AIR, at(5, 40), at(7, 20), 90.0)], query
    )
    entro_mezzanotte = query.model_copy(update={"arrive_by": time(23, 59)})

    assert ranker.filter_by_query([domani, oggi], entro_mezzanotte) == [oggi]


def test_partire_la_sera_prima_non_rispetta_l_ora_di_partenza(query: SearchQuery) -> None:
    """"Dopo le 13:30" e' un'ora **del giorno chiesto**, come "entro le 23:59".

    E' il caso di chi ha un impegno la mattina e cerca il ritorno: confrontando
    le sole ore, un viaggio che parte alle 23:50 della sera prima passa il
    vincolo a pieni voti e si presenta come la risposta migliore. Non e' tardi
    abbastanza, e' **presto di un giorno**, e chi legge lo scopre all'arrivo."""
    sera_prima = _itinerary(
        [
            make_leg(
                "a",
                Mode.BUS,
                TORINO_PN,
                MATERA_BUS,
                at(23, 50, day_offset=-1),
                at(8, 10),
                41.0,
            )
        ],
        query,
    )
    pomeriggio = _itinerary(
        [make_leg("b", Mode.BUS, TORINO_PN, MATERA_BUS, at(14, 5), at(22, 30), 45.0)], query
    )
    dopo_pranzo = query.model_copy(update={"depart_after": time(13, 30)})

    assert ranker.filter_by_query([sera_prima, pomeriggio], dopo_pranzo) == [pomeriggio]
    # E se resta solo quella, va detto quale vincolo la sta tenendo fuori.
    assert [v["kind"] for v in ranker.unmet_constraints([sera_prima], dopo_pranzo)] == [
        "depart_after"
    ]


def test_i_vincoli_messi_da_parte_sono_solo_quelli_che_mordono(query: SearchQuery) -> None:
    """Quando nessuna soluzione rispetta i vincoli la ricerca mostra quelle fuori
    vincolo, e deve dire quali ha messo da parte. Nominare anche i vincoli che
    non escludono niente sposterebbe l'attenzione da quello che morde davvero."""
    notturno = _itinerary(
        [make_leg("a", Mode.BUS, TORINO_PN, MATERA_BUS, at(6, 0), at(20, 0), 66.0)], query
    )
    vincoli = query.model_copy(
        update={"depart_after": time(13, 30), "max_changes": 3, "max_budget": 500.0}
    )

    messi_da_parte = ranker.unmet_constraints([notturno], vincoli)

    # Parte alle 6: viola l'orario. Un cambio non lo fa e costa 66 €, quindi il
    # tetto dei cambi e quello del budget non c'entrano.
    assert [voce["kind"] for voce in messi_da_parte] == ["depart_after"]
    assert messi_da_parte[0]["value"] == "13:30"
    assert ranker.unmet_constraints([notturno], query) == []


# ------------------------------------------------------------- ricomposizione


def test_assemble_aggiunge_avvicinamento_e_ultimo_miglio(torino, matera, query) -> None:
    """Il volo su Bari non finisce a Bari: senza l'ultimo miglio il confronto
    con il pullman diretto per Matera sarebbe truccato."""
    path = composer.CandidatePath(
        [
            composer.waypoint_from_place(torino, "origin"),
            composer.waypoint_from_place(matera, "dest"),
        ]
    )
    volo = make_leg("ryanair", Mode.AIR, TORINO_AIR, BARI_AIR, at(7, 55), at(9, 30), 94.7)
    legs_by_hop = {("origin", "dest"): [volo]}

    itineraries = composer.assemble(path, legs_by_hop, torino, matera, query)
    assert len(itineraries) == 1
    itinerary = itineraries[0]

    modes = [leg.mode for leg in itinerary.legs]
    assert modes == [Mode.TRANSFER, Mode.AIR, Mode.TRANSFER]
    # La partenza vera e' quando esci di casa, non quando decolli.
    assert itinerary.depart < volo.depart
    assert itinerary.arrive > volo.arrive
    # Tariffa piu' navetta di andata piu' pullman per Matera.
    assert itinerary.cost.total > 94.7
    assert itinerary.cost.subtotal("transfer") > 0


def test_assemble_scarta_le_coincidenze_impossibili(torino, matera, query) -> None:
    hub = composer.Waypoint("hub:Bari", "Bari", 41.117, 16.871, (BARI_C,))
    path = composer.CandidatePath(
        [
            composer.waypoint_from_place(torino, "origin"),
            hub,
            composer.waypoint_from_place(matera, "dest"),
        ]
    )
    treno = make_leg("trenitalia", Mode.RAIL, TORINO_PN, BARI_C, at(8), at(17), 90.0)
    troppo_presto = make_leg("flixbus", Mode.BUS, BARI_C, MATERA_BUS, at(17, 5), at(18, 20), 10.0)
    legs_by_hop = {
        ("origin", "hub:Bari"): [treno],
        ("hub:Bari", "dest"): [troppo_presto],
    }
    assert composer.assemble(path, legs_by_hop, torino, matera, query) == []

    fattibile = make_leg("flixbus", Mode.BUS, BARI_C, MATERA_BUS, at(18, 30), at(19, 45), 10.0)
    legs_by_hop[("hub:Bari", "dest")] = [fattibile]
    itineraries = composer.assemble(path, legs_by_hop, torino, matera, query)
    assert len(itineraries) == 1
    assert itineraries[0].n_tickets == 2


def test_hop_senza_gambe_non_produce_itinerari(torino, matera, query) -> None:
    """Se una tratta del percorso e' scoperta, l'itinerario non esiste: meglio
    niente che un percorso con un buco."""
    hub = composer.Waypoint("hub:Bari", "Bari", 41.117, 16.871, (BARI_C,))
    path = composer.CandidatePath(
        [
            composer.waypoint_from_place(torino, "origin"),
            hub,
            composer.waypoint_from_place(matera, "dest"),
        ]
    )
    legs_by_hop = {
        ("origin", "hub:Bari"): [
            make_leg("trenitalia", Mode.RAIL, TORINO_PN, BARI_C, at(8), at(17), 90.0)
        ]
    }
    assert composer.assemble(path, legs_by_hop, torino, matera, query) == []


def test_alle_isole_non_si_arriva_su_strada(query: SearchQuery) -> None:
    """Il motore proponeva un autobus da Trapani a Levanzo e un treno fino a
    Marsala con "ultimo miglio" fino a Favignana. Sono isole: qualunque
    collegamento di superficie stimato li' e' inventato."""
    from app.models import Place
    from app.routing import feasibility

    porto_favignana = make_node(
        "ov:port-favignana", "Favignana Porto", 37.933, 12.328, NodeKind.PORT, city="Favignana"
    )
    favignana = Place(
        query="Favignana", label="Favignana", lat=37.933, lon=12.328,
        country="IT", nodes=[porto_favignana],
    )
    trapani = Place(
        query="Trapani", label="Trapani", lat=38.016, lon=12.515, country="IT",
        nodes=[
            make_node("ov:port-trapani", "Trapani Porto", 38.016, 12.515, NodeKind.PORT),
            make_node("t:trapani", "Trapani", 38.017, 12.530),
        ],
    )

    assert feasibility.is_island(favignana)
    assert not feasibility.is_island(trapani)

    # La stazione di Marsala non puo' fare da ultimo miglio per un'isola...
    marsala = make_node("t:marsala", "Marsala", 37.799, 12.437)
    assert not feasibility.land_link_possible(favignana, marsala)
    # ...mentre il molo dell'operatore, che ha un altro identificatore ma e' lo
    # stesso posto, deve essere accettato.
    molo_operatore = make_node(
        "alb:abc-123", "Favignana", 37.932, 12.329, NodeKind.PORT, city="Favignana"
    )
    assert feasibility.land_link_possible(favignana, molo_operatore)


def test_niente_trasferimenti_di_terra_fra_due_porti() -> None:
    from app.routing import transfers

    levanzo = make_node("p:levanzo", "Levanzo", 38.001, 12.337, NodeKind.PORT)
    favignana = make_node("p:favignana", "Favignana", 37.933, 12.328, NodeKind.PORT)
    trapani_porto = make_node("p:trapani", "Trapani Porto", 38.016, 12.515, NodeKind.PORT)
    trapani_staz = make_node("t:trapani", "Trapani", 38.017, 12.530)

    assert not transfers.reachable_by_land(levanzo, favignana)
    assert not transfers.reachable_by_land(trapani_staz, levanzo)
    # Stazione e porto della stessa citta' restano collegati: e' una passeggiata.
    assert transfers.reachable_by_land(trapani_staz, trapani_porto)


def test_deduplica_itinerari_identici(torino, matera, query) -> None:
    path = composer.CandidatePath(
        [
            composer.waypoint_from_place(torino, "origin"),
            composer.waypoint_from_place(matera, "dest"),
        ]
    )
    volo = make_leg("ryanair", Mode.AIR, TORINO_AIR, BARI_AIR, at(7, 55), at(9, 30), 94.7)
    first = composer.assemble(path, {("origin", "dest"): [volo]}, torino, matera, query)
    second = composer.assemble(path, {("origin", "dest"): [volo]}, torino, matera, query)
    assert len(composer.deduplicate(first + second)) == 1
