"""Come una localita' diventa un elenco di fermate.

Il resolver e' silenzioso quando sbaglia: non solleva niente, restituisce
semplicemente fermate diverse, e gli effetti si vedono tre livelli piu' in
basso come "quell'operatore non copre la tratta". Questi test bloccano i modi
in cui e' gia' successo.
"""

from __future__ import annotations

import pytest

from app.models import Mode
from tests._datasets import needs_datasets

pytestmark = needs_datasets


@pytest.fixture(scope="module")
def resolver():
    from app.geo.resolver import get_resolver

    return get_resolver()


def _cities(place) -> set[str]:
    return {node.city for node in place.nodes if node.kind.value in ("station", "bus_stop")}


def test_una_fermata_come_ancora_non_diventa_una_citta(resolver) -> None:
    """Cercare "Torino Porta Nuova" non deve far sparire mezzi operatori.

    FlixBus, Itabus, Albatross e Grimaldi cercano **per citta'**, con la chiave
    che esce da `city_key(node)`. Alle fermate che non dichiarano la propria
    citta' il resolver attacca quella dell'ancora: se l'ancora e' una stazione
    e si usa la sua etichetta, la citta' diventa "Torino Porta Nuova", nessuno
    di quegli operatori la riconosce e spariscono tutti senza un errore.

    Il risultato deve essere lo stesso che si ottiene scrivendo la citta'."""
    citta = resolver.resolve("Torino", modes={Mode.RAIL, Mode.BUS})
    stazione = resolver.resolve("Torino Porta Nuova", modes={Mode.RAIL, Mode.BUS})

    assert _cities(citta) == {"Torino"}
    assert _cities(stazione) == {"Torino"}
    # L'etichetta invece resta quella chiesta: e' da li' che si misura il primo
    # miglio, e partire da Porta Nuova non e' come partire dal centro.
    assert stazione.label == "Torino Porta Nuova"


def test_un_codice_iata_come_ancora_porta_la_citta_giusta(resolver) -> None:
    """Stesso problema per "TRN": l'ancora e' l'aeroporto, la citta' e' Torino."""
    place = resolver.resolve("TRN", modes={Mode.RAIL, Mode.BUS})
    assert _cities(place) == {"Torino"}


def _iata(place) -> set[str]:
    return {node.iata for node in place.nodes if node.iata}


def test_lo_scalo_sotto_casa_non_perde_contro_uno_grande_e_lontano(resolver) -> None:
    """La distanza pesa nel punteggio, non e' piu' un semplice spareggio.

    Con la sola priorita' (principale + numero di operatori) vinceva sempre lo
    scalo piu' grande: Milano scartava **Linate a 7 km** per Malpensa e
    Bergamo, Firenze scartava **Peretola a 6 km** per Pisa e Bologna, e Venezia
    metteva Trieste (98 km) davanti a Treviso (26 km) perche' nel dataset ha un
    identificativo in piu'. Erano tre modi diversi di far cominciare il viaggio
    a un'ora di strada da casa."""
    def primo(citta: str) -> str:
        place = resolver.resolve(citta, modes={Mode.AIR})
        return place.nodes[0].iata

    # Ora entrano tutti, quindi il test non e' piu' su chi c'e' ma su chi viene
    # per primo: e' l'ordine che decide chi vede l'adapter quando i posti
    # disponibili sono meno degli scali.
    assert primo("Milano") == "LIN"
    assert primo("Firenze") == "FLR"
    assert primo("Venezia") == "VCE"


def test_nessuno_scalo_nel_raggio_viene_escluso(resolver) -> None:
    """Gli aeroporti nel raggio entrano tutti, anche i piccoli.

    Da Cuneo Ryanair vola solo a Palermo e Cagliari, ma su quelle due tratte e'
    un'alternativa vera, e un'occasione tolta in silenzio e' peggio di una
    proposta che perde in classifica: il costo del trasferimento e' gia' nel
    prezzo porta a porta, quindi uno scalo lontano non puo' vincere a
    sproposito. Il tetto resta solo come rete di sicurezza."""
    torino = _iata(resolver.resolve("Torino", modes={Mode.AIR}))
    assert {"TRN", "CUF", "MXP", "GOA"} <= torino

    milano = _iata(resolver.resolve("Milano", modes={Mode.AIR}))
    assert {"LIN", "MXP", "BGY"} <= milano


def test_le_fermate_scelte_restringono_la_ricerca() -> None:
    """Il filtro sulle fermate tiene solo quelle chieste, e ignora gli id ignoti.

    Un elenco di id che non esistono piu' (una ricerca salvata mesi fa, un
    dataset aggiornato) non deve trasformarsi in "nessuna soluzione": meglio
    cercare da tutte che da nessuna."""
    from app.orchestrator.search_service import _only_chosen_nodes
    from app.geo.resolver import get_resolver

    place = get_resolver().resolve("Torino", modes={Mode.RAIL})
    primo = place.nodes[0]

    ristretto = _only_chosen_nodes(place, {primo.id})
    assert [node.id for node in ristretto.nodes] == [primo.id]
    # Le coordinate della localita' non si toccano: restano quelle dell'ancora,
    # altrimenti il primo miglio sparirebbe insieme alla penalita' di chi parte
    # da una fermata periferica.
    assert (ristretto.lat, ristretto.lon) == (place.lat, place.lon)

    assert _only_chosen_nodes(place, set()).nodes == place.nodes
    assert _only_chosen_nodes(place, {"non-esiste"}).nodes == place.nodes


# ------------------------------------------------------------------ il mondo
#
# Il gazetteer Trainline copre 43 paesi europei. Fuori di li' il resolver non
# diceva "non trovato": trovava **un'altra cosa**, per somiglianza, e la ricerca
# partiva su una citta' che nessuno aveva chiesto.


@pytest.mark.parametrize(
    ("query", "atteso"),
    [
        ("Tokyo", "JP"),
        ("Osaka", "JP"),
        ("New York", "US"),
        ("Sydney", "AU"),
        ("Cairo", "EG"),
        ("Bangkok", "TH"),
        ("Buenos Aires", "AR"),
    ],
)
def test_le_citta_del_mondo_si_trovano(resolver, query, atteso) -> None:
    place = resolver.resolve(query)
    assert place.country == atteso
    assert place.nodes, "trovata la citta' ma nessuna fermata"


@pytest.mark.parametrize(
    ("italiano", "paese", "contiene"),
    [
        ("Londra", "GB", "London"),
        ("Parigi", "FR", "Paris"),
        ("Il Cairo", "EG", "Cairo"),
        ("Monaco di Baviera", "DE", "nchen"),
        ("Lisbona", "PT", "Lisbo"),
    ],
)
def test_gli_esonimi_italiani_portano_alla_citta_giusta(
    resolver, italiano, paese, contiene
) -> None:
    """«Londra» non e' in nessun dataset di stazioni, e prima finiva per
    somiglianza su **Ondara**, in Spagna: otto fermate vere di una citta' che
    nessuno aveva chiesto. I nomi in altre lingue arrivano da GeoNames."""
    place = resolver.resolve(italiano)
    assert place.country == paese
    assert contiene.lower() in place.label.lower()


def test_gli_orari_fuori_europa_non_sono_ora_italiana() -> None:
    """Il fuso mancante ripiegava su `Europe/Rome`: un volo giapponese sarebbe
    stato mostrato con sette ore di scarto, senza che niente lo dicesse."""
    from app.geo.datasets import load_index

    index = load_index()
    fusi = {code: index.by_iata[code].timezone for code in ("HND", "JFK", "GRU", "SYD", "DXB")}

    assert fusi["HND"] == "Asia/Tokyo"
    assert fusi["JFK"] == "America/New_York"
    assert fusi["GRU"] == "America/Sao_Paulo"
    assert fusi["SYD"] == "Australia/Sydney"
    assert fusi["DXB"] == "Asia/Dubai"


@pytest.mark.parametrize(
    ("query", "paese", "etichetta"),
    [
        # Omonime dove la piu' popolosa non e' quella giusta: per la spagnola
        # sappiamo interrogare gli operatori, per l'altra no.
        ("Madrid", "ES", "Madrid"),
        ("Valencia", "ES", "Valencia"),
        # E il nome proprio batte il nome in un'altra lingua: «Naples» ha
        # «napoli» fra i suoi alias e piu' abitanti della voce italiana.
        ("Napoli", "IT", "Napoli"),
        ("Firenze", "IT", "Firenze"),
    ],
)
def test_le_omonime_del_mondo_non_scavalcano_casa(resolver, query, paese, etichetta) -> None:
    """Il gazetteer mondiale ha portato dentro trentaquattromila citta', e con
    loro le omonime: senza un criterio, «Madrid» finiva in Colombia, «Valencia»
    in Venezuela e Napoli si chiamava «Naples»."""
    place = resolver.resolve(query)
    assert place.country == paese
    assert place.label == etichetta


# ------------------------------------- l'ordine con cui le fermate si mostrano


def test_l_ordine_dei_gruppi_non_dipende_piu_dall_avvio(resolver) -> None:
    """Era l'ordine di iterazione di un `set` di Enum.

    Cioe' dipendeva da `PYTHONHASHSEED`: misurato il 2026-08-22 su sei
    esecuzioni, sei ordini diversi. Si vedeva a schermo — cercando «Canelli» a
    volte usciva prima l'aeroporto di Genova — ma non era solo estetica: a
    parita' di punteggio quell'ordine fa da spareggio anche su quali fermate
    vengono davvero interrogate, e uno spareggio casuale rende una ricerca
    irriproducibile.

    Si prova la causa e non il sintomo: dentro un processo solo il seme non si
    puo' cambiare, quindi si verifica che i gruppi escano nell'ordine
    dichiarato invece che in uno qualsiasi."""
    from app.geo.resolver import KIND_SCAN_ORDER

    place = resolver.resolve("Canelli")
    posizioni = [KIND_SCAN_ORDER.index(node.kind) for node in place.nodes]
    assert posizioni == sorted(posizioni), (
        f"i gruppi non seguono l'ordine dichiarato: "
        f"{[n.kind.value for n in place.nodes]}"
    )


def test_le_fermate_mostrate_vanno_per_gruppo_e_per_distanza(resolver) -> None:
    """Il caso Canelli, che e' quello da cui e' nato tutto.

    A schermo la prima pastiglia era «Genoa Cristoforo Colombo Airport», a 55
    km, e Canelli stessa era la nona. L'ordine del motore e' tarato e resta
    com'e' (vedi il test su Linate qui sopra): quello che cambia e' cosa legge
    una persona."""
    place = resolver.resolve("Canelli")
    mostrate = place.fermate_da_mostrare()

    primo, km_primo = mostrate[0]
    assert primo.name == "Canelli"
    assert km_primo < 1.0

    terra = [km for nodo, km in mostrate if nodo.kind.value in {"station", "bus_stop"}]
    aria = [km for nodo, km in mostrate if nodo.kind.value == "airport"]
    assert terra == sorted(terra), "dentro il gruppo si va per distanza crescente"
    assert aria == sorted(aria)
    indici_aria = [i for i, (n, _) in enumerate(mostrate) if n.kind.value == "airport"]
    indici_terra = [i for i, (n, _) in enumerate(mostrate) if n.kind.value != "airport"]
    assert max(indici_terra) < min(indici_aria), "la terra prima dell'aria"


def test_l_ordine_del_motore_resta_quello_del_punteggio(resolver) -> None:
    """La separazione ha senso solo se le due liste restano diverse.

    Se `nodes` cominciasse a coincidere con l'ordine di presentazione vorrebbe
    dire che il riordino e' arrivato dove non doveva, e la correzione su Linate
    sarebbe da rifare senza che nessun test lo dica."""
    place = resolver.resolve("Canelli")
    motore = [node.id for node in place.nodes]
    schermo = [node.id for node, _ in place.fermate_da_mostrare()]

    assert sorted(motore) == sorted(schermo), "le stesse fermate, in ordine diverso"
    assert motore != schermo, "su Canelli i due ordini devono differire"
