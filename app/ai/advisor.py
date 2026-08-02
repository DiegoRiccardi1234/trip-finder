"""Il consiglio finale: quale soluzione prendere e perche'.

Una classifica ordinata non e' una decisione. Le prime tre soluzioni sono spesso
incommensurabili fra loro: il volo costa quaranta euro in piu' ma restituisce
una giornata, il pullman notturno risparmia un albergo ma ti scarica alle sei
del mattino, il treno costa di piu' ma e' un biglietto solo. Qui si chiede al
modello di dire il compromesso ad alta voce.

Al modello si passano numeri gia' calcolati, mai dati grezzi: costo totale,
durata porta a porta, numero di biglietti, margine sulla coincidenza piu'
stretta. Non deve fare aritmetica, deve fare da consigliere.

Due regole imparate a schermo, che valgono per tutti e tre i consigli:

  - **Ogni soluzione ha un riferimento, ed e' quello scritto sulla scheda.**
    Prima l'elenco era numerato solo qui dentro: il modello scriveva «la 4» e a
    schermo nessun 4 esisteva, oppure ripiegava sul nome dell'operatore e con
    due corse Itabus non si capiva quale fosse. Il riferimento arriva dalla
    pagina insieme alla soluzione e torna indietro dentro il testo, dove la
    pagina lo rende cliccabile.
  - **Quello che il modello legge deve essere quello che l'utente vede.** Se la
    lista e' fuori vincolo va detto, se le soluzioni sono venticinque e non sei
    va detto, se un prezzo e' condizionato a una tessera va detto. Un consiglio
    che ragiona su una realta' diversa da quella sullo schermo e' peggio di
    nessun consiglio: sembra giusto.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable

from app.ai import client
from app.models import (
    AdviceOption,
    AdviceRequest,
    CompareRequest,
    RelaxedConstraint,
    TripAdviceRequest,
    TripLeg,
    risk_text,
)
from app.orchestrator import cache

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 6

#: Come si scrive un riferimento: `[3]` per una classifica sola, `[B2]` quando
#: le classifiche sono piu' d'una e la lettera dice la colonna.
RIFERIMENTO_RE = re.compile(r"\[([A-Za-z]{0,2}\d{1,3})\]")

#: La regola comune ai prompt che parlano di schede numerate. Sta scritta una
#: volta sola perche' le tre versioni divergevano gia' su cose piu' piccole.
REGOLA_RIFERIMENTI = """- Nomina ogni soluzione con il suo riferimento fra parentesi quadre, esattamente
  come te lo do: «prenderei la [3]». E' il numero scritto sulla scheda che chi
  legge ha davanti, ed e' l'unico modo che ha per capire di quale parli.
- Non citare riferimenti che non sono nell'elenco."""

#: Il modello sbaglia le sottrazioni. Visto a schermo: «la [B1] ti fa
#: risparmiare quasi due ore rispetto alla [B3]», con 12h35 contro 12h30 — i
#: dati nel prompt erano giusti, il conto no. Le differenze gliele diamo fatte.
REGOLA_CONFRONTI = """- Le differenze di prezzo e di durata sono gia' calcolate in fondo a ogni riga:
  usa quelle parole cosi' come sono, non rifare le sottrazioni per conto tuo."""


def _durata(minuti: int) -> str:
    return f"{minuti // 60}h{minuti % 60:02}"


def _euro(valore: float) -> str:
    """Con la virgola, come sulla scheda.

    Il modello ricopia le cifre come gliele si danno: con il punto scriveva
    «32.82 euro» sotto una scheda che diceva «43,98 €», e due notazioni diverse
    per la stessa cosa nella stessa schermata sembrano due dati diversi."""
    return f"{valore:.2f}".replace(".", ",")


def _durata_totale(option: AdviceOption) -> int:
    """Quanto si sta in viaggio: andata piu' ritorno, se c'e' il ritorno.

    E' l'unico punto in cui sommare le due tratte ha senso. Nella riga della
    soluzione stanno separate, perche' sommate producevano un numero che a
    schermo non compariva da nessuna parte."""
    return option.duration_min + (option.return_duration_min or 0)


def _confronti(options: list[AdviceOption]) -> Callable[[AdviceOption], str]:
    """Quanto costa e quanto dura ogni soluzione **in piu' della migliore**.

    Il consiglio vive di paragoni («costa 30 euro meno», «ti salva due ore») e
    il paragone e' una sottrazione: farla fare al modello significa vederla
    sbagliata ogni tanto, in una frase che sembra perfetta. Qui la si calcola
    una volta e gliela si passa gia' in parole.

    I pari merito prendono l'etichetta anche loro: si confrontano i valori, non
    l'identita' degli oggetti, altrimenti la seconda soluzione da 24,98 euro
    risulterebbe «piu' cara di zero euro» della prima."""
    if len(options) < 2:
        return lambda _option: ""
    minimo_prezzo = min(option.total for option in options)
    minima_durata = min(_durata_totale(option) for option in options)
    minimi_cambi = min(option.n_changes for option in options)
    minimi_biglietti = min(option.n_tickets for option in options)

    def nota(option: AdviceOption) -> str:
        # Prezzo e durata si dicono sempre tutti e due: la più economica che
        # dura cinque ore di più è la stessa soluzione di prima, ma detta così
        # si capisce cosa costa risparmiare.
        #
        # Accenti veri, qui: il resto del modulo scrive «piu'» perché sono
        # commenti e istruzioni, ma queste parole il modello le ricopia di peso
        # nel consiglio, e a schermo si è letto «è la piu' rapida».
        euro = option.total - minimo_prezzo
        minuti = _durata_totale(option) - minima_durata
        if euro < 0.01 and minuti < 1:
            pezzi = ["la più economica e la più rapida"]
        else:
            pezzi = [
                "la più economica"
                if euro < 0.01
                else f"+{_euro(euro)} euro della più economica",
                "la più rapida"
                if minuti < 1
                else f"+{_durata(minuti)} della più rapida",
            ]

        # Cambi e biglietti solo quando **aggiungono** qualcosa: dirli su ogni
        # riga li trasformerebbe in rumore, e la riga porta già i numeri assoluti.
        cambi = option.n_changes - minimi_cambi
        if cambi > 0:
            pezzi.append(f"+{cambi} camb{'io' if cambi == 1 else 'i'} della più diretta")
        biglietti = option.n_tickets - minimi_biglietti
        if biglietti > 0:
            pezzi.append(
                f"+{biglietti} bigliett{'o' if biglietti == 1 else 'i'} da comprare a parte"
            )
        return ", ".join(pezzi)

    return nota


def _describe_option(
    option: AdviceOption, fallback: str | None, confronto: str = ""
) -> str:
    """Una soluzione in una riga, con davanti il suo riferimento.

    E' l'unica descrizione per tutti e tre i consigli: prima ce n'erano due
    quasi identiche, e le differenze erano difetti — una scriveva la data
    dell'arrivo e l'altra la sola ora, cosi' un arrivo del giorno dopo (che la
    scheda segna `+1`) al modello arrivava come un arrivo in giornata.

    Il riferimento e' quello che porta la soluzione (`option.ref`, cioe' il
    numero scritto sulla scheda); `fallback` serve solo quando chi chiama non
    ne ha uno — nei test, o in una chiamata costruita a mano."""
    ref = option.ref or fallback
    orari = (
        f"{option.depart:%d/%m %H:%M} - {option.arrive:%d/%m %H:%M}"
        f" ({_durata(option.duration_min)})"
    )
    parts = [f"[{ref}] {orari}" if ref else orari]
    if option.return_depart and option.return_arrive:
        ritorno = f"ritorno {option.return_depart:%d/%m %H:%M} - {option.return_arrive:%d/%m %H:%M}"
        if option.return_duration_min is not None:
            ritorno += f" ({_durata(option.return_duration_min)})"
        parts.append(ritorno)
        parts.append(f"{_euro(option.total)} euro a persona, andata e ritorno insieme")
    else:
        parts.append(f"{_euro(option.total)} euro a persona, tutto incluso")
    parts.append(f"mezzi {'+'.join(option.modes)} ({', '.join(option.operators)})")
    parts.append(f"{option.n_changes} cambi, {option.n_tickets} biglietti")
    if option.flags:
        parts.append("avvisi: " + ", ".join(risk_text(flag) for flag in option.flags))
    if option.notes:
        parts.append("da sapere: " + "; ".join(option.notes))
    if confronto:
        parts.append(confronto)
    return " | ".join(parts)


def _numero(value: object) -> str:
    """`120.0` scritto come «120». JSON non distingue interi e decimali, e un
    budget di «120.0 euro» sembra una cifra calcolata da qualcuno."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


#: I vincoli messi da parte, detti a parole. Gemella di `detto` in
#: `showRelaxed` (`app/static/app.js`): la pagina lo scrive all'utente, qui lo
#: si scrive al modello, e devono dire la stessa cosa.
_VINCOLO = {
    "max_budget": lambda v: f"budget massimo {_numero(v)} euro a persona",
    "max_changes": lambda v: f"non piu' di {_numero(v)} cambi",
    "allow_night": lambda _: "niente viaggi di notte",
    "depart_after": lambda v: f"partenza dopo le {v}",
    "arrive_by": lambda v: f"arrivo entro le {v}",
}


def _riga_vincoli_saltati(relaxed: Iterable[RelaxedConstraint]) -> str | None:
    """L'avvertenza piu' importante di tutte, e prima non arrivava mai.

    Quando nessuna soluzione rispetta i vincoli il motore mostra le migliori
    **fuori** vincolo invece di una pagina vuota, e la pagina lo dichiara. Il
    prompt invece continuava a ripetere «budget massimo 120 euro» sopra una
    lista che lo sforava tutta: il modello poteva consigliare in tutta calma
    una soluzione fuori budget, e non aveva modo di accorgersene."""
    voci = [_VINCOLO[r.kind](r.value) for r in relaxed if r.kind in _VINCOLO]
    if not voci:
        return None
    return (
        "ATTENZIONE: nessuna soluzione rispetta questi vincoli — "
        + "; ".join(voci)
        + ". Le soluzioni qui sotto li violano tutte: dillo a chi legge invece "
        "di presentarle come se andassero bene."
    )


def _controllo_riferimenti(options: list[AdviceOption]) -> Callable[[str], str]:
    """Scarta la risposta che cita soluzioni inesistenti, o nessuna.

    E' la rete che prende il difetto per cui e' nato tutto questo: «la Torino →
    Matera 4 costa 120,80 euro» mentre a schermo la quarta scheda ne costava
    altri. Un riferimento inventato non e' un dettaglio di stile: manda a
    cercare una scheda che non c'e'. Un consiglio senza nessun riferimento non
    e' seguibile, e con una sola opzione non serve invece nominarla."""
    validi = {(option.ref or "").upper() for option in options if option.ref}

    def check(text: str) -> str:
        citati = {match.group(1).upper() for match in RIFERIMENTO_RE.finditer(text)}
        if citati - validi:
            return client.INVENTED
        if len(validi) > 1 and not citati:
            return client.UNANCHORED
        return ""

    return check


SYSTEM = """Sei un amico che si intende di viaggi e aiuta a scegliere fra alternative
gia' trovate. Ti vengono dati itinerari con costo totale a persona (bagagli e
trasferimenti inclusi), durata porta a porta, numero di biglietti e avvisi.

Scrivi in italiano, al massimo tre paragrafi brevi:
1. quale prenderesti e per quale motivo concreto;
2. in quale situazione converrebbe invece un'altra opzione;
3. l'avvertenza pratica piu' importante, se ce n'e' una.

Regole:
{riferimenti}
{confronti}
- Non inventare orari, prezzi o collegamenti che non sono nell'elenco.
- Non ripetere la tabella: chi legge la vede gia'.
- Se un itinerario ha biglietti separati, dillo: e' un rischio reale, perche'
  saltando una coincidenza nessuno riprotegge il passeggero.
- Se un prezzo e' condizionato (indicativo, o valido con una tessera), non
  darlo per acquisito.
- Niente elenchi puntati, niente titoli, niente formule di cortesia.
- Niente asterischi e niente grassetto: il testo va a schermo cosi' com'e'.""".format(
    riferimenti=REGOLA_RIFERIMENTI, confronti=REGOLA_CONFRONTI
)


def _testa_richiesta(request: AdviceRequest) -> list[str]:
    context = [
        f"Viaggio: {request.origin} -> {request.destination} il {request.date.isoformat()}",
        f"Passeggeri: {request.pax}",
        f"Valigia da stiva: {'si' if request.with_checked_bag else 'no'}",
    ]
    if request.return_date:
        context.append(f"Con ritorno il {request.return_date.isoformat()}")
    if request.max_budget:
        # "A persona" e' la meta' che mancava: il motore filtra per persona, e
        # senza dirlo il modello moltiplicava per i passeggeri e annunciava uno
        # sforamento che non c'era.
        context.append(f"Budget massimo: {request.max_budget:.0f} euro a persona")
    if request.depart_after:
        context.append(f"Non puo' partire prima delle {request.depart_after}")
    if request.arrive_by:
        context.append(f"Deve arrivare entro le {request.arrive_by}")
    if not request.allow_night:
        context.append("Non vuole viaggiare di notte")
    if request.raw_text:
        context.append(f"Richiesta originale: {request.raw_text}")
    saltati = _riga_vincoli_saltati(request.relaxed)
    if saltati:
        context.append(saltati)
    return context


def _quante(mostrate: int, trovate: int, partial: bool) -> str:
    """Quante ne sta guardando davvero chi legge.

    Il modello ne vedeva sei e le credeva tutte: «e' l'unica sotto i 50 euro»
    era falso rispetto alle altre diciannove che l'utente poteva scorrere."""
    detto = f"le prime {mostrate} per punteggio, su {max(trovate, mostrate)} trovate"
    if partial:
        detto += ", ricerca interrotta a meta': la classifica non e' definitiva"
    return detto


async def advise(request: AdviceRequest) -> client.Answer:
    """Il consiglio su una classifica sola: quella che si sta guardando.

    Riceve le soluzioni **dalla pagina**, non dal motore. Prima nascevano qui
    dentro la ricerca, e su un'andata e ritorno era un guaio: il motore ha due
    classifiche separate, la pagina una sola scheda per coppia, e il consiglio
    finiva per commentare i prezzi della sola andata sotto schede che
    mostravano il totale."""
    if not client.is_configured():
        return client.Answer(reason=client.NOT_CONFIGURED)
    if not request.options:
        return client.Answer(reason=client.NOTHING)

    candidates = request.options[:MAX_CANDIDATES]
    confronto = _confronti(candidates)
    righe = "\n".join(
        _describe_option(option, str(index), confronto(option))
        for index, option in enumerate(candidates, start=1)
    )
    prompt = (
        "\n".join(_testa_richiesta(request))
        + f"\n\nItinerari ({_quante(len(candidates), request.found, request.partial)}):\n"
        + righe
    )

    # Tre paragrafi brevi stanno in trecento token, ma i modelli con
    # ragionamento nascosto ne bruciano il doppio prima di iniziare a scrivere e
    # con seicento arrivavano tagliati. Sul piano gratuito il tetto non costa
    # niente: alzarlo e' l'unico modo per non perdere un consiglio gia' pronto.
    answer = await client.complete(
        "advice",
        SYSTEM,
        prompt,
        max_tokens=1100,
        temperature=0.4,
        check=_controllo_riferimenti(candidates),
    )
    if answer.completion is not None:
        logger.debug("consiglio prodotto da %s", answer.completion.model)
    return answer


# --- Confronto fra piu' ricerche -------------------------------------------

COMPARE_SYSTEM = """Sei un amico che si intende di viaggi. Chi ti scrive non ha ancora
deciso dove (o quando, o da dove) andare: ti mostra piu' possibilita', ognuna
con le sue prime soluzioni di viaggio gia' trovate, e vuole sapere quale conviene.

Scrivi in italiano, al massimo tre paragrafi brevi:
1. quale possibilita' sceglieresti, dicendo di quanto e in cosa batte le altre
   (euro di differenza, ore di differenza, cambi in meno);
2. a quali condizioni converrebbe invece un'altra;
3. l'avvertenza pratica piu' importante, se ce n'e' una.

Regole:
- Confronta le possibilita' fra loro. Non descriverle una per una: chi legge le
  ha gia' sotto gli occhi, quello che gli manca e' il paragone.
- Chiama ogni possibilita' con la sua etichetta, cosi' come te la do.
{riferimenti}
{confronti}
- Non inventare orari, prezzi o collegamenti che non sono nell'elenco.
- Se una possibilita' obbliga a biglietti separati, dillo: saltando una
  coincidenza nessuno riprotegge il passeggero.
- Se un prezzo e' condizionato (indicativo, o valido con una tessera), non
  darlo per acquisito.
- Niente elenchi puntati, niente titoli, niente formule di cortesia.
- Niente asterischi e niente grassetto: il testo va a schermo cosi' com'e'.""".format(
    riferimenti=REGOLA_RIFERIMENTI, confronti=REGOLA_CONFRONTI
)


async def compare(request: CompareRequest) -> client.Answer:
    """Un consiglio solo su piu' possibilita', per dire quale prendere.

    Non e' `advise` chiamata piu' volte: quella descrive le righe di **una**
    classifica e le sue righe sono anonime rispetto alla meta, quindi due
    consigli affiancati non possono dire "vai a Palermo, costa 30 euro meno".
    Qui il confronto e' il compito, e ogni possibilita' arriva al modello con
    la sua etichetta."""
    if not client.is_configured():
        return client.Answer(reason=client.NOT_CONFIGURED)
    utili = [candidate for candidate in request.candidates if candidate.options]
    if len(utili) < 2:
        return client.Answer(reason=client.NOTHING)

    context = [f"Passeggeri: {request.pax}"]
    if request.with_checked_bag:
        context.append("Con valigia da stiva")
    if request.max_budget:
        context.append(f"Budget massimo: {request.max_budget:.0f} euro a persona")
    if request.raw_text:
        context.append(f"Richiesta originale: {request.raw_text}")

    # Il confronto si calcola su **tutte** le possibilita' insieme: qui il
    # compito e' dire quale conviene fra Torino e Milano, e un "la piu'
    # economica" riferito alla sola colonna non direbbe niente di nuovo.
    tutte: list[AdviceOption] = [
        option for candidate in utili for option in candidate.options
    ]
    confronto = _confronti(tutte)

    blocchi = []
    for candidate in utili:
        testa = f"{candidate.label} ({candidate.origin} -> {candidate.destination}, {candidate.date:%d/%m}"
        testa += f", ritorno il {candidate.return_date:%d/%m})" if candidate.return_date else ")"
        testa += " — " + _quante(
            len(candidate.options), candidate.found, candidate.partial
        )
        saltati = _riga_vincoli_saltati(candidate.relaxed)
        if saltati:
            testa += f"\n{saltati}"
        righe = "\n".join(
            _describe_option(option, f"{index}", confronto(option))
            for index, option in enumerate(candidate.options, start=1)
        )
        blocchi.append(f"{testa}\n{righe}")

    prompt = "\n".join(context) + "\n\nPossibilita':\n\n" + "\n\n".join(blocchi)

    # Stessa chiave a parita' di possibilita' confrontate: rifare la stessa
    # ricerca due volte di fila non deve ricomprare un consiglio identico.
    # Dentro la chiave va tutto cio' che cambia il prompt — la frase originale
    # e la valigia ne erano fuori, e per un'ora si serviva la risposta a una
    # domanda diversa.
    key = cache.make_key(
        "ai:compare",
        request.pax,
        request.max_budget,
        request.with_checked_bag,
        request.raw_text,
        sorted(
            f"{c.label}|{c.date}|{c.return_date}|{c.found}|{c.partial}|"
            + ";".join(
                f"{o.ref}/{o.depart:%d%H%M}/{o.total:.2f}/{o.n_changes}/{o.n_tickets}"
                for o in c.options
            )
            for c in utili
        ),
    )
    answer = await client.complete(
        "advice",
        COMPARE_SYSTEM,
        prompt,
        max_tokens=1100,
        temperature=0.4,
        cache_key=key,
        check=_controllo_riferimenti(tutte),
    )
    if answer.completion is not None:
        logger.debug("confronto prodotto da %s", answer.completion.model)
    return answer


# --- Il viaggio intero -----------------------------------------------------

TRIP_SYSTEM = """Sei un amico che si intende di viaggi. Chi ti scrive ha gia' un
viaggio a tappe con le date decise e, per ogni tappa, la soluzione migliore
trovata. Non deve scegliere fra alternative: deve capire se il viaggio, cosi'
com'e', regge.

Scrivi in italiano, al massimo tre paragrafi brevi:
1. come sta insieme il viaggio: dove va il grosso della spesa e del tempo, se una
   tappa pesa quanto tutte le altre;
2. cosa cambieresti nell'incastro — spostare una sosta di un giorno, invertire
   l'ordine, tenersi piu' margine — e quanto si guadagna a farlo;
3. l'avvertenza pratica piu' importante, se ce n'e' una.

Regole:
- Ragiona sul viaggio nel suo insieme. Le singole classifiche le ha gia' sotto
  gli occhi, tappa per tappa: qui serve quello che li' non si vede.
- Chiama ogni tappa col suo numero, come te lo do: «la tappa 2».
- Il riferimento fra parentesi quadre accanto a una soluzione — «[B1]» — e' il
  numero scritto sulla sua scheda: puoi citarlo, ma non inventarne altri.
- Non inventare orari, prezzi o collegamenti che non sono nell'elenco.
- Un arrivo a notte fonda seguito da una ripartenza il mattino dopo va detto.
- Se una tappa non ha trovato niente, dillo: il viaggio non si chiude.
- Niente elenchi puntati, niente titoli, niente formule di cortesia.
- Niente asterischi e niente grassetto: il testo va a schermo cosi' com'e'."""


def _describe_leg(leg: TripLeg, index: int) -> str:
    testa = f"Tappa {index}. {leg.origin} -> {leg.destination} il {leg.date:%d/%m}"
    if leg.chosen is None:
        return f"{testa}: nessuna soluzione trovata"
    parts = [
        testa,
        _describe_option(leg.chosen, None),
        f"{leg.found} soluzioni trovate in tutto",
    ]
    if leg.stay_days:
        parts.append(f"poi {leg.stay_days} giorni di sosta")
    return " | ".join(parts)


async def advise_trip(request: TripAdviceRequest) -> client.Answer:
    if not client.is_configured():
        return client.Answer(reason=client.NOT_CONFIGURED)
    # Con una tappa sola non c'e' nessun "insieme" da commentare: quello che si
    # puo' dire lo dice gia' il consiglio in fondo alla sua classifica.
    if len(request.legs) < 2:
        return client.Answer(reason=client.NOTHING)

    totale = sum(leg.chosen.total for leg in request.legs if leg.chosen)
    context = [
        f"Passeggeri: {request.pax}",
        f"Totale del viaggio con le soluzioni migliori: {_euro(totale)} euro a persona",
    ]
    if request.with_checked_bag:
        context.append("Con valigia da stiva")
    if request.max_budget:
        context.append(f"Budget massimo: {request.max_budget:.0f} euro a persona")
    if request.raw_text:
        context.append(f"Richiesta originale: {request.raw_text}")

    prompt = "\n".join(context) + "\n\nTappe:\n" + "\n".join(
        _describe_leg(leg, index) for index, leg in enumerate(request.legs, start=1)
    )

    key = cache.make_key(
        "ai:trip",
        request.pax,
        request.max_budget,
        request.with_checked_bag,
        request.raw_text,
        [
            f"{leg.origin}>{leg.destination}|{leg.date}|{leg.stay_days}|{leg.found}|"
            + (f"{leg.chosen.depart:%d%H%M}/{leg.chosen.total:.2f}" if leg.chosen else "vuota")
            for leg in request.legs
        ],
    )
    answer = await client.complete(
        "advice", TRIP_SYSTEM, prompt, max_tokens=1100, temperature=0.4, cache_key=key
    )
    if answer.completion is not None:
        logger.debug("consiglio sul viaggio prodotto da %s", answer.completion.model)
    return answer
