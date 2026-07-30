# Trip Finder

[![tests](https://github.com/DiegoRiccardi1234/trip-finder/actions/workflows/tests.yml/badge.svg)](https://github.com/DiegoRiccardi1234/trip-finder/actions/workflows/tests.yml)

Motore di ricerca viaggi multimodale, da usare in locale. Confronta treni,
pullman, aerei e traghetti sul **costo reale porta a porta**, non sulla sola
tariffa, e compone itinerari che nessun singolo operatore ti mostra.

![Torino → Matera, 25 soluzioni confrontate sul costo porta a porta](docs/img/comparison.png)

**English summary** — Trip Finder is a local-first, multimodal trip search
engine for Italy and its neighbours: 42 operator adapters across trains,
coaches, flights and ferries, queried in parallel and merged into a single
ranking. What makes it different from an aggregator is the unit of comparison:
not the fare, but the **real door-to-door cost** — fare plus checked bag, plus
the ride to the first stop and from the last one, plus transfers between legs,
with every estimated item labelled as such. It also separates *changes* from
*tickets*: two separate tickets mean nobody rebooks you if the first leg is
late, so that risk is weighted in the ranking instead of hidden. This lets it
compose journeys no single operator sells — the case that drove the project is
Turin → Matera, a city with neither an airport nor a national-rail station.
Stack: Python, FastAPI, SQLite, `curl_cffi` and Playwright for the harder
operators, and an OpenRouter LLM for the comparative advice, picked at runtime
by live endpoint health. The test suite runs offline on saved fixtures. Please
read the legal note at the bottom before using it.

---

Il caso che ha guidato il progetto è Torino → Matera. Matera non ha né aeroporto
né stazione delle Ferrovie dello Stato: ci si arriva solo con la ferrovia locale
FAL da Bari, con un pullman a lunga percorrenza, o volando su Bari e prendendo la
navetta. Nessun operatore ti dice tutto questo, e gli aggregatori sbagliano il
confronto perché ignorano bagaglio e ultimo miglio.

Cosa restituisce, per il 14 agosto 2026 (25 itinerari, i primi cinque — è la
ricerca nell'immagine qui sopra):

| | Soluzione | Porta a porta | Totale a persona |
|---|---|---|---|
| 1 | Marino Autolinee, pullman notturno diretto | 14h 15 | 66,40 € |
| 2 | Itabus, un cambio — **il più economico** | 20h 25 | **56,98 €** |
| 3 | Volo Ryanair su Bari + navetta — **il più rapido** | 5h 40 | 100,73 € |
| 4 | Volo Ryanair notturno su Bari + navetta | 8h 11 | 83,74 € |
| 5 | FlixBus, un cambio | 18h 15 | 86,97 € |

I prezzi sono quelli di una ricerca vera, non un esempio scritto a mano: cambiano
ogni giorno, e in agosto salgono man mano che i pullman si riempiono.

I primi risultati non sono semplicemente i più alti in classifica: sono **il
migliore di ogni famiglia di soluzione**, più il più economico e il più rapido
in assoluto. Una classifica per solo punteggio mostrerebbe sei varianti di
pullman e nasconderebbe il volo.

Il volo "da 91,73 €" diventa 100,73 € perché include i 3 € del bus per Caselle e
i 6 € della navetta da Bari a Matera. È il tipo di differenza che ribalta una
scelta — e il pullman Itabus a 56,98 € nessun aggregatore te lo mette accanto a
un volo.

---

## Avvio

Serve Python 3.11 o superiore.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\fetch_datasets.py
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8010
```

Poi apri <http://127.0.0.1:8010/>. Oppure, più corto: `.\start.ps1`.

I due dataset (circa 29 MB) si scaricano una volta sola e non stanno nel
repository. Il primo avvio impiega un paio di secondi a costruire l'indice
geografico: 62.000 fermate e 10.000 località.

I comandi sono in PowerShell perché l'avvio senza console, con l'icona nell'area
di notifica, è pensato per Windows (`pystray`). Su Linux e macOS funziona tutto
il resto identico: stesse dipendenze, `uvicorn app.main:app --port 8010`.

### Facoltativo: l'intelligenza artificiale

Copia `.env.example` in `.env` e metti una chiave OpenRouter in
`OPENROUTER_API_KEY`. Senza chiave il sito funziona identico, solo senza il
consiglio finale e senza la ricerca in linguaggio naturale.

```powershell
.\.venv\Scripts\python.exe scripts\try_health.py        # quali modelli sono vivi
.\.venv\Scripts\python.exe scripts\try_health.py --ask  # prova una chiamata vera
```

---

## Come è fatto

```
app/
  geo/          da "Torino" alle fermate reali, con gli ID di ogni operatore
  providers/    un file per operatore, isolati fra loro
  routing/      percorsi candidati, coincidenze, costo, classifica
  orchestrator/ esecuzione in parallelo, budget di tempo, cache, circuit breaker
  ai/           OpenRouter con selezione modelli consapevole della salute
  static/       una pagina, niente build
```

### Il pezzo che conta: la composizione

Interrogare un operatore è la parte facile. Il valore sta nel decidere **quali
tratte vale la pena chiedere** e nel **ricomporre** le risposte.

Su scala europea non si può provare ogni città come scalo. Il filtro è
geometrico: un hub è ammissibile solo se non allunga il viaggio oltre il 40%.
Ma la geometria da sola sbaglia proprio i casi che contano — per pura distanza
Genova batte Bari sulla Torino-Matera, pur non avendo alcun collegamento con
Matera. Per questo **i due hub più vicini alla destinazione entrano sempre**,
qualunque sia il loro punteggio. È la regola che rende trovabile questo viaggio.

### Il modello di costo

Il totale di ogni itinerario comprende:

- la tariffa di ogni tratta;
- il **bagaglio in stiva** dove si paga (45 € su Ryanair, zero sui treni): con o
  senza valigia vince un'opzione diversa;
- l'**avvicinamento** alla prima fermata e l'**ultimo miglio** dall'ultima. I
  collegamenti principali sono curati a mano in `routing/transfers.py`
  (Pugliairbus da Bari a Matera: 75 minuti, 6 €), gli altri sono stimati per
  distanza e marcati come tali;
- i **trasferimenti** fra una tratta e l'altra quando cambia la stazione.

Ogni voce stimata è dichiarata. Una tratta di cui non conosciamo il prezzo non
vale zero: verrebbe premiata proprio perché ne sappiamo meno.

### Il rischio, che è un criterio come gli altri

Torino → Bari con Trenitalia più Bari → Matera con FlixBus sono **due contratti**.
Se il treno ritarda, nessuno ti riprotegge sul pullman e il biglietto è perso.
Il motore lo segnala in rosso e lo pesa nella classifica, separando due cose che
si confondono di continuo:

- **cambi**: quante volte scendi e risali, inclusi quelli interni a un biglietto
  unico (un Frecciarossa con cambio a Bologna è un cambio, ma senza rischio);
- **biglietti**: quanti contratti separati compri. È questo che genera il rischio.

I margini minimi di coincidenza sono per modo: dieci minuti fra due treni, ma
110 minuti per prendere un aereo, perché il vincolo è il check-in.

---

## Gli operatori

Ogni adapter è un plugin isolato: se il suo parser si rompe, gli altri
continuano a funzionare e la ricerca degrada invece di fallire.

**42 adapter, tutti verdi** su `scripts/check_providers.py`.

Otto sono autonomi:

| Operatore | Cosa dà |
|---|---|
| **Trenitalia** (backend di lefrecce.it) | orari e prezzi |
| **FlixBus** | orari e prezzi |
| **Itabus** | orari e prezzi |
| **Ryanair** (Fare Finder) | la tariffa più bassa del giorno |
| **FAL** | l'unico treno che arriva a Matera: orari **e tariffe** dai PDF ufficiali |
| **Grimaldi Lines** | il primo traghetto di linea vero, con il prezzo |
| **ÖBB** | l'orario austriaco, compresi i diretti Vienna–Venezia. Senza prezzi |
| **SBB CFF FFS** | l'orario svizzero, compreso Zurigo–Milano. Senza prezzi |

Gli altri trentaquattro sono **un solo adapter**. Molti operatori di pullman
italiani non hanno un sito ciascuno: usano lo stesso motore di prenotazione,
distribuito su un host per operatore. Stessa API, stesso formato. Da lì
arrivano Marino, Marozzi, Autolinee Federico, Liscio, SAIS, InterSAJ, Ferrovie
del Gargano, Cortina Express, Tiemme, Giuntabus e una ventina d'altri —
**inclusi tre traghetti** (Liberty Lines per Egadi ed Eolie, Blu Navy per
l'Elba, Ichnusa per la Corsica).

Aggiungerne un altro costa una riga nella tabella `OPERATORS` di
`providers/bus/albatross.py`, più una tratta di prova verificata. Per trovare
sia l'una sia l'altra c'è `scripts/probe_albatross.py`, che parte dagli
operatori già noti, ne legge i vettori collegati e prova le coppie di località
ricavate dalle **linee reali** invece di tentare a caso.

### Quelli che non ci sono, e perché

**Parte degli operatori europei è murato.** Verificato sondando gli endpoint
reali, con l'evidenza in [`docs/operatori.md`](docs/operatori.md):

| Operatore | Cosa risponde |
|---|---|
| Italo | `401 Invalid token x`; il browser automatizzato non carica nemmeno il sito (`ERR_HTTP2_PROTOCOL_ERROR`) |
| Deutsche Bahn | `403 OPS_BLOCKED`, anche chiamando da dentro una pagina del sito |
| Trainline | captcha DataDome |
| easyJet / Wizz Air | sfida anti-bot / endpoint spostato |
| GNV, SNAV | API mappate nei loro bundle, ma ogni percorso risponde `401`: manca la sessione |
| Volotea | il token anonimo si ottiene, la ricerca risponde `500` per uno stato di sessione mancante |
| Tirrenia, Direct Ferries | rispondono, ma con dati parziali: partenze senza orario di arrivo né prezzo |
| Omio | percorsi API cambiati |

L'infrastruttura per superarli c'è: `providers/browser_base.py` offre due classi
base, una che chiama un'API **da dentro** una pagina del sito ereditandone
cookie e token, una che compila il modulo di ricerca e legge il DOM. Restano da
usare, sito per sito.

**Un operatore murato non viene però taciuto.** Chi cerca Torino–Roma e non
vede Italo può concludere che non esista. Quelli di cui sappiamo con certezza
che coprono una tratta stanno in `providers/known_routes.json`, e la ricerca li
mostra a parte, fuori dalla classifica e con il link al loro sito: *"anche
questi collegano le due città, ma i loro orari vanno visti da loro"*. La
copertura non è mai dichiarata a occhio — o l'operatore è nei dataset con i
propri identificatori di stazione, o le sue rotte vengono dai suoi documenti
pubblici.

### FAL: quando l'unica fonte è un PDF

Ferrovie Appulo Lucane non ha API. `scripts/build_fal_schedule.py` scarica il
manifesto ufficiale e ne ricava un orario versionato, lavorando sulle coordinate
dei caratteri (il rilevamento tabelle su quel documento produce celle fuse).

Conta più degli orari quello che ci sta intorno, e viene dal manifesto stesso:
il servizio **non circola la domenica né nei festivi**, i treni marcati `(1)`
sono **soppressi dal 27 luglio al 29 agosto** — cioè in pieno agosto — e la
tratta Bari–Gravina è **interrotta**, coperta da autobus sostitutivi. L'adapter
applica queste condizioni prima di produrre qualunque risultato: proporre un
treno che quel giorno non parte è peggio che non proporlo.

Lo script valida prima di scrivere (orari crescenti, durate plausibili,
capolinea attesi) e in caso di dubbio **fallisce senza scrivere niente**.

Dallo stesso sito arriva anche il **listino**, che è un secondo PDF. FAL non
vende a tratta ma a fascia chilometrica: servono insieme la tabella delle
distanze tassabili e quella delle fasce. Bari–Matera sono 70 km tassabili, cioè
6,20 €. Se il listino non regge i suoi controlli, lo script scrive gli orari
**senza** i prezzi: l'adapter lo dichiara e il costo torna a essere una stima,
invece di diventare un numero inventato.

### Aggiungere un operatore

Un file sotto `app/providers/<modo>/`, una classe con `@register`, due metodi:

```python
@register
class MioOperatore(Provider):
    id = "mio"
    name = "Mio Operatore"
    mode = Mode.BUS
    tier = 1                       # 2 se serve il browser

    async def fetch(self, origin, destination, ctx):
        return await ctx.http.get_json(URL, params={...})

    def parse(self, raw, origin, destination, ctx):
        return [Leg(...) for ... in raw["results"]]
```

`fetch` e `parse` sono separati apposta: `parse` è sincrono, non tocca la rete e
gira sulle fixture salvate. È la parte che si rompe quando il sito cambia, e
deve essere verificabile senza internet.

Regole: gli orari devono avere il fuso (un orario senza fuso è un bug
dell'adapter, non un caso da gestire a valle); non aggiungere supplementi ai
prezzi, ci pensa `routing/cost.py` in modo uniforme; una lista vuota significa
"ho cercato e non c'è niente", che è diverso da `NotServed`.

---

## Diagnostica

```powershell
# dove si traduce una località
.\.venv\Scripts\python.exe scripts\try_resolve.py Torino Matera

# un singolo adapter dal vivo, e --save congela la risposta come fixture
.\.venv\Scripts\python.exe scripts\try_provider.py trenitalia Torino Bari 2026-08-14
.\.venv\Scripts\python.exe scripts\try_provider.py --list

# cerca altri operatori sulla piattaforma Albatross, con la loro tratta di prova
.\.venv\Scripts\python.exe scripts\probe_albatross.py --discover-only

# una ricerca completa senza avviare il server
.\.venv\Scripts\python.exe scripts\try_search.py Torino Matera 2026-08-14 --bag

# i test: parser su fixture, motore su dati sintetici, nessuna rete
.\.venv\Scripts\python.exe -m pytest tests -q
```

`try_provider.py` è lo strumento principale quando qualcosa si rompe. Con
`--save` la risposta grezza finisce in `tests/fixtures/` e il test del parser
gira su quella: quando un operatore cambia formato, il test dice esattamente
cosa è cambiato.

Dalla API: `/api/providers` mostra lo stato dei circuiti, `/api/ai/status` i
modelli utilizzabili, `/api/resolve?q=Matera` la risoluzione di una località.

Se un adapter si rompe, il suo circuito si apre per qualche minuto e viene
segnalato nella UI. Per riaprirlo dopo averlo sistemato:
`POST /api/providers/reset`.

---

## Prestazioni

Una ricerca Torino → Matera genera 8 percorsi candidati e circa 520
interrogazioni verso gli operatori. Misurata con i valori di default: i primi
**25 itinerari sono a schermo dopo 6-7 secondi**, e la ricerca si dichiara
completata intorno ai 30 secondi a cache fredda, 16 con la cache calda. La coda
non sono i risultati, sono gli operatori più lenti che chiudono e il consiglio
dell'IA, che arriva per ultimo: i risultati intanto sono già in classifica, che
si riordina in streaming man mano che arrivano.

Tre manopole in `.env` se serve muoverle:

- `SEARCH_TIME_BUDGET` (90 s) — tetto rigido: chi non risponde entro viene
  dichiarato in timeout nella UI, mai nascosto. I primi risultati arrivano molto
  prima, perché a chiudere la prima ondata è `FAST_BUDGET` (20 s);
- `DOMAIN_RATE_LIMIT` (4/s per dominio) — il vero collo di bottiglia: a 1/s la
  stessa ricerca dura circa quattro volte tanto;
- `SEARCH_MAX_CONCURRENCY` (20) — richieste in volo in tutta la ricerca. Può
  essere alta senza far danno, perché quello che protegge i singoli operatori è
  il limite per dominio, non questo.

La cache SQLite usa scadenze diverse per tipo di dato: 15 minuti per i prezzi,
6 ore per gli orari, 30 giorni per la mappatura fra una fermata e l'ID di un
operatore.

---

## Nota legale

I dati vengono letti dagli endpoint pubblici dei siti degli operatori, e i
termini di servizio di molti di loro limitano l'accesso automatico. È uno
strumento personale e locale, a basso volume, con un limite di richieste per
dominio e una cache aggressiva proprio per non pesare sui loro server: nessuna
credenziale, nessun acquisto automatizzato, nessun aggiramento di protezioni —
dove c'è un captcha o una sessione autenticata l'operatore resta fuori, e in
`docs/operatori.md` sta scritto quale e perché. I dati non vengono
redistribuiti, e non esiste alcun servizio pubblico costruito su questo codice.

Se rappresenti un operatore e vuoi che il suo adapter venga rimosso, apri una
issue: lo togliamo.

I prezzi vanno sempre riverificati sul sito dell'operatore prima di comprare:
cambiano fra la ricerca e l'acquisto, e le tariffe più basse hanno spesso
condizioni restrittive.
