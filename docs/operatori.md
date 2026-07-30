# Stato degli operatori

Verifiche fatte sugli endpoint reali. Serve a non rifare le stesse ricerche fra
sei mesi, e a sapere dove riprendere.

Ultimo aggiornamento: **27 luglio 2026** (sera: aggiunti Grimaldi, OBB, SBB, le
tariffe FAL e un operatore Albatross).

---

## Funzionanti

**42 adapter, tutti verdi** su `scripts/check_providers.py`.

I sette autonomi:

| Operatore | Modo | Endpoint | Note |
|---|---|---|---|
| Trenitalia | treno | `www.lefrecce.it/Channels.Website.BFF.WEB/website/ticket/solutions` | Un `400` con `type: ERROR` significa "nessuna soluzione", non guasto |
| FlixBus | pullman | `global.api.flixbus.com/search/service/v4/search` | Nessuna chiave. Le fermate non hanno coordinate: si recuperano dal `legacy_id` incrociato con Trainline |
| Itabus | pullman | `www.itabus.it/on/demandware.store/Sites-ITABUS-Site/it/Api-Travels` | Il percorso locale e' `/it/`, **non** `/it_IT/`: con quello risponde 500. L'endpoint `Api-Stations` esiste ma pubblica `destinations` sempre vuoto, quindi la copertura non e' ricavabile |
| Ryanair | aereo | `services-api.ryanair.com/farfnd/v4/oneWayFares` | Solo la tariffa piu' bassa del giorno. L'elenco rotte per scalo va in cache |
| FAL | treno | nessuno: PDF ufficiali | Orari **e tariffe** da manifesto, vedi in fondo |
| Grimaldi Lines | nave | `booking.grimaldi-lines.com/ajax.php` | Con prezzo. HTML, non JSON. Dettagli sotto |
| OBB | treno | `fahrplan.oebb.at/bin/mgate.exe` | Orari senza prezzo. Copre i diretti Vienna-Venezia |
| SBB CFF FFS | treno | `transport.opendata.ch/v1/connections` | Orari senza prezzo. Copre Zurigo-Milano |

I trentaquattro della piattaforma Albatross, in `app/providers/bus/albatross.py`:

- **Lunga percorrenza**: Marino Autolinee, Marozzi, Autolinee Federico,
  Autolinee Liscio (copre Roma–Matera), InterSAJ, Ferrovie del Gargano, SATAM,
  Pronto Bus Italia
- **Sicilia**: SAIS Autolinee, Autoservizi Salemi, Giuntabus, Giuntabus
  Trasporti, Magtour, Prestia e Comande, OneBus, Sberna Viaggi, Autolinee
  Sommatinese, Autonoleggio Di Paola
- **Centro e sud**: Consorzio Autolinee, Gruppo Di Maio, Tiemme, Acierno,
  Freccia Roma, Gaspari Lines
- **Nord**: Barzi Service
- **Aeroporti**: Fiumicino Express, Bus4Fly
- **Montagna**: Cortina Express, Livigno Express, Alta Badia Bus
- **Traghetti**: Liberty Lines (Egadi ed Eolie), Blu Navy (Elba),
  Ichnusa Lines (Sardegna–Corsica)

Averne tanti non rallenta la ricerca: se una localita' non e' nel catalogo di
un operatore, l'adapter esce in poche decine di millisecondi senza nemmeno una
richiesta di rete, perche' il catalogo e' in cache per un mese.

### La piattaforma Albatross

Gli ultimi cinque non sono cinque adapter: sono **uno**. Molti operatori di
pullman italiani usano lo stesso motore di prenotazione ("Albatross", di
Sitrap), distribuito su un host per operatore. Stessa API, stessi percorsi,
stesso formato di risposta.

```
GET  {base}/Stops
     catalogo completo: id, nome, coordinate, fuso, localita' di appartenenza

POST {base}/search/s/{localitaPartenza}/{localitaArrivo}/{dal}/{al}
     ?channel=1&reservationId=&inStaging=false&isReturn=false
     &excludeExternals=false&partial=false&caller=&changeDate=false
     &locale=it&coupon=
     corpo: [{"fId": null, "extra": {}, "subGroupId": 0}]
```

Prende i **nomi** delle localita', non gli identificatori. Nessuna
autenticazione. Aggiungere un operatore costa una riga in
`app/providers/bus/albatross.py`.

### Come si trovano gli altri operatori della piattaforma

**La vecchia strada e' chiusa.** Il bundle del motore di prenotazione elencava
gli host di tutti gli operatori; dalla versione 8.4 non piu': ogni
installazione legge il proprio `serverUrl` da un `apiUrl.js` suo, e nel bundle
non resta traccia delle altre. Verificato sul bundle attuale e su tre copie
archiviate (maggio 2025, ottobre 2025, maggio 2026): nessuna contiene la lista.

La strada nuova e' `scripts/probe_albatross.py`, che usa due endpoint aperti
della piattaforma stessa, entrambi scoperti sondando `api.marinobus.it`:

```
GET {base}/Carriers   anagrafica dei vettori venduti da quell'installazione,
                      con email e PEC. Il dominio dell'email e' il dominio del
                      sito, e da li' si tenta `booking.<dominio>/apiUrl.js`,
                      che dichiara host API, home e nome commerciale.
GET {base}/Lines      le linee, con la descrizione che contiene i capolinea
                      ("Matera-Potenza-Roma"). Da li' escono le coppie da
                      provare, invece di tentare a caso: un operatore con
                      trecento fermate ha decine di migliaia di combinazioni.
```

La ricerca e' transitiva (i vettori si nominano a catena) e si esaurisce da
sola. Girata il 27 luglio 2026 ha dato **sette installazioni nuove**:
`consorzioautolineetpl.it`, `dicarlobus.com`, `marinobusurbano.it`,
`romalinee.it`, `saj.it`, `salinabus.it`, `silvestribus.it`.

Di queste **una sola vende corse punto-punto**: MarinoBus Urbano, ora in
`OPERATORS`. Le altre sei rispondono `200` con zero soluzioni su ogni coppia
provata (fino a venti coppie ciascuna, sia a meta' agosto sia a meta' ottobre,
quindi non e' stagionalita'): sono installazioni di trasporto pubblico locale
con linee numerate 141-173, che usano la piattaforma per gli abbonamenti e non
per la vendita al viaggiatore. Riprovarle non serve finche' non cambia il loro
modello di vendita.

Nota sul numero "74 host" che compariva qui prima: non e' piu' verificabile,
perche' derivava dalla lista nel bundle che oggi non esiste. Il conto onesto e'
quello sopra: trentaquattro operatori attivi nel progetto, sette installazioni
nuove sondate, sei senza vendita p2p.

**Attenzione agli orari.** Le date arrivano in UTC (offset `+00:00`) con il
fuso di lettura allegato a parte, per soluzione (`Europe/Rome`). Vanno
**convertite**, non reinterpretate: `_local` in `albatross.py` fa
`astimezone(zone)`.

Verificato riga per riga contro `booking.marinobus.it` sul 14 agosto 2026,
Torino-Matera: l'API riporta `16:30Z` e il sito mostra `18:30`. Senza la
conversione ogni corsa risulta due ore prima, e con lei sbagliano il
riconoscimento del viaggio notturno e la penalita' sull'ora di arrivo. Il test
`test_albatross_converte_gli_orari_utc_in_locali` blocca questo comportamento
sulla fixture salvata.

---

## Bloccati

Verificato che non funzionano. Non vanno riprovati alla cieca: qui c'e' gia'
l'evidenza e il punto da cui ripartire.

| Operatore | Cosa risponde | Dove riprendere |
|---|---|---|
| **Italo** | `POST api-biglietti.italotreno.com/api/v1/booking` -> `401 Invalid token x`. Il browser automatizzato non riesce nemmeno a caricare il sito: `ERR_HTTP2_PROTOCOL_ERROR` su `biglietti.italotreno.com`, mentre curl_cffi passa | I bundle mostrano il flusso: `Authorization: Basic` verso `{base}api/v1/users/authorization` per ottenere un token, poi `Authorization: Bearer`. Il percorso `users/authorization` risponde 404 dall'esterno: probabilmente il WAF lo espone solo con l'origine giusta. **`/api/v1/stations` e' invece aperto**: 2537 stazioni con codici identici a Trainline piu' i flag `isPort`, `isItabusStation`, `isTrenitalia` |
| **Deutsche Bahn** | `403 OPS_BLOCKED` sia da HTTP diretto sia **da dentro una pagina del sito** | Resta solo compilare davvero il modulo su `bahn.de` con `BrowserFormProvider` e leggere il DOM. `int.bahn.de/web/api/reiseloesung/orte` per le stazioni e' aperto |
| **Trainline** | captcha DataDome su `/api/journey-search/` | Coprirebbe SNCF, DB, Renfe e OBB in un colpo solo, ma un captcha e' un no detto in modo esplicito: chiuso qui, non si tenta |
| **easyJet** | `429` con sfida (`cpr_chlge`) su `routepricing/v3/searchfares`; `www.easyjet.com/it` risponde `403` anche con impersonazione TLS | |
| **Wizz Air** | `wizzair.com/static/metadata.json` non e' piu' JSON. Nei bundle compare solo un host di staging (`wizz09-api.staging7.mito.hu`) | Trovare la base vera osservando le richieste della pagina durante una ricerca |
| **Volotea** | Molto piu' vicino di prima, ma non ancora aperto. Il token si ottiene **senza browser**: `POST api.volotea.com/api/voe/v1/account/login` con la `x-api-key` statica che la loro SPA porta nel bundle (qui non riportata), `x-client-type: spa` e un corpo di login vuoto risponde `200` con un JWT anonimo (scade in 10 minuti). Il percorso di ricerca e' `POST /api/voe/v1/flights/search`: senza token da `401`, **col token da `500` `Value cannot be null. (Parameter 'source')`**. Il corpo giusto e' in camelCase (`origin, destination, departureDate, adults, ...`): con quello il model fa il bind (500 interno), con la forma Navitaire no (`400 :ModelIsNull`) | Manca solo uno stato di sessione lato server: probabile che vada rifatta, sulla stessa sessione, la sequenza che la SPA esegue prima di cercare (`culture`, `misc/gtmdata`, `stations/stations`, `trackSearch`). In alternativa scrivere il cookie `searchFlightsModel` su `.volotea.com` e caricare `book.volotea.com/booking/flights`, che lancia la ricerca da solo. **Gia' utilizzabile intanto**: `GET json.volotea.com/dist/stations/stations.json` e' aperto (902 KB, 275 scali) e porta il grafo delle rotte con finestre operative e prezzo indicativo per mercato |
| **Aeroitalia** | `book.aeroitalia.com` e' solo lo storage statico della SPA (405 dal blob) | Stessa strada di Volotea |
| **GNV** | API mappata nei chunk Angular: base `www.gnv.it/integrationlayer/api/v1`, con `sailing/available-per-day`, `entities/routes`, `cart/quote`. Tutto risponde `401 {"error":"unauthorized","error_description":"Session cookie or header not found"}`, **anche dopo warm-up** su `/it` e `/it/booking` che pure depositano `ASP.NET_SessionId` e `__RequestVerificationToken` | Nei chunk compaiono `X-XSRF-TOKEN` e `withCredentials`: manca la chiamata che apre la sessione di prenotazione, probabilmente `cart/initialize`. `booking.gnv.it` e' una pagina morta |
| **SNAV** | App Angular su `booking.snav.it`, backend `travel-service`/`user-service`. Un solo endpoint aperto: `GET /travel-service/setting/get-public-setting` (200). Tutto il resto `401 {"errorCode":"Unauthorized"}` | Percorsi di autenticazione noti dal bundle: `/auth/sign-in`, `/auth/refresh-token`, `/auth/validate-otp`. Che `get-public-setting` sia aperto suggerisce una whitelist: varrebbe enumerare gli altri endpoint pubblici |
| **ITA Airways** | Nessuna API di prenotazione esposta; l'unico host e' `api.travelid.ita-airways.com`, che e' l'identita' Lufthansa | |
| **Vueling** | Nessun host di prenotazione nei bundle oltre a `tickets.vueling.com` | |
| **Omio** | `GoEuroAPI/rest/api/v5/position_suggestions` -> 404, percorsi cambiati | |
| **Direct Ferries** | Il backend non e' su `directferries.it` ma su `ssl.directferries.com`, ed e' un ASMX legacy trovato in `static.directferries.co.uk/dealfinder/main.js`. Due endpoint rispondono `200`: `dealpicker.asmx/SearchDataGetAB` (JSONP: da un nome di porto ricava i `routeId` interni) e `dealpicker.asmx/RouteDataGet` (per una rotta, le date in cui si viaggia). Serve un codice affiliato nel parametro `stdc`, che sta nel loro JS (qui non riportato). **Ne' orari ne' prezzi** | Il preventivo vero sta dietro `ferry/secure/booking_redirect_df.aspx`, che e' un POST di form (`deal_finder1`). Da li' ripartirebbe una fase 2 |
| **Tirrenia (CIN)** | Semiaperto e per questo inutilizzabile come adapter: `GET www.tirrenia.it/mds/disponibilita.ElencoCorseHP.json?lingua=it&compagnia=tirrenia&data=2026/08/20` risponde `200` con voci tipo `CO083020260820` = linea + ora di partenza + data. **Manca l'orario di arrivo e manca il prezzo**, quindi non se ne ricava una gamba senza inventare la durata. Il parametro `codiceLinea` viene ignorato; `compagnia` vuole la stringa `tirrenia`, non la sigla | I prezzi passano da `POST /mds/web/booking.app`, che risponde HTML server-rendered. Intanto le sue rotte stanno in `known_routes.json`, cosi' chi cerca Civitavecchia-Olbia sa che Tirrenia c'e' |

---

## Da fare

| Operatore | Stato |
|---|---|
| **Volotea** | Manca solo lo stato di sessione, vedi la riga nella tabella dei bloccati: e' il candidato piu' vicino a diventare un adapter |
| **Aeroitalia** | Non affrontato: stessa forma di Volotea (SPA con storage statico) |
| **GNV, SNAV** | API mappate, entrambe a `401`: serve capire come nasce la sessione |
| **DB** | Resta solo `BrowserFormProvider` su `bahn.de`, mai tentato |
| **SNCF, Renfe** | Non sondati. Trainline li coprirebbe tutti in un colpo, ma ha il captcha DataDome |
| **Trenord** | Da verificare se serve: Trenitalia restituisce gia' molte corse regionali lombarde |

---

## FAL: orario statico da PDF

Ferrovie Appulo Lucane non ha nessuna API ed e' l'unico servizio ferroviario che
arriva a Matera. `scripts/build_fal_schedule.py` scarica il manifesto ufficiale
e ne ricava `app/providers/rail/data/fal_schedule.json`, versionato apposta: se
un giorno il parsing sbaglia, la differenza si vede nel diff.

L'estrazione **non** usa il rilevamento tabelle di pdfplumber, che su questo
documento produce celle fuse e disallineate. Lavora sulle coordinate: le ore
alla stessa ascissa appartengono allo stesso treno, quelle alla stessa ordinata
alla stessa stazione. Due trappole trovate strada facendo:

- il manifesto **impila piu' griglie** sotto la stessa intestazione di verso.
  Trattarle come una sola incolonna un treno del pomeriggio sotto uno del
  mattino, e ne esce una corsa che parte da Bari alle 4.14 e arriva a Matera
  alle 15.52. Si dividono sulle righe "N. Treno";
- la linea prosegue oltre Matera fino a Potenza, quindi la soglia di durata
  plausibile va tarata su quattro ore, non sulle due e quaranta di Bari-Matera.

Lo script valida prima di scrivere: numero minimo di corse per verso, orari
crescenti, durate plausibili, capolinea attesi presenti. Se qualcosa non torna
**fallisce e non scrive niente**.

Le condizioni valgono quanto gli orari, e sono nel manifesto:

- `Il Servizio Ferroviario e' sospeso nei giorni di domenica e festivi`
- `(1) - Treni soppressi dal 27 luglio al 29 agosto 2026` — cade in pieno
  agosto, cioe' quando serve davvero scendere al Sud
- `Servizio effettuato con bus da Bari C.le a Gravina e viceversa` — la tratta
  ferroviaria e' interrotta

L'adapter le applica prima di produrre qualunque gamba. Un Bari-Matera richiede
quasi sempre un cambio ad Altamura ma resta **un biglietto solo**: diventa una
gamba unica con `internal_changes = 1`, senza il rischio dei biglietti separati.

**Le tariffe ora ci sono**, dal secondo PDF (`TARIFFE-PUGLIA-E-BASILICATA`, il
cui nome porta la data di revisione e va quindi cercato sulla pagina degli
orari, non fissato nel codice). FAL non vende a tratta ma a **fascia
chilometrica**: servono due tabelle affiancate sulla stessa pagina, il listino
delle fasce e la matrice triangolare delle distanze tassabili. Bari-Matera sono
70 km tassabili, cioe' 6,20 euro; Bari-Altamura 45 km, 4,00 euro.

Due dettagli che sono costati un giro a vuoto:

- la matrice si legge da destra, perche' il nome della stazione sta in fondo
  alla riga e le distanze la precedono. Il listino occupa la stessa riga piu' a
  sinistra: ci si ferma al primo importo con la virgola;
- la scaletta triangolare (0, 1, 2, ... valori per riga) e' anche la verifica
  che si stia leggendo la tabella giusta, ma va protetta dalle intestazioni:
  senza il filtro sulle parole di testata la catena si aggancia a "REGIONE
  PUGLIA" e a "MOD. **07** TARIFFE", il cui `07` viene scambiato per una
  distanza, e da li' Bari sparisce e ogni distanza scala di due posizioni.

Se il listino non regge i controlli di plausibilita' (numero di fasce, prezzi
crescenti, Bari-Matera fra 50 e 100 km) lo script scrive gli orari **senza** i
prezzi, invece di rinunciare a tutto: `has_prices` dell'adapter torna falso da
solo e `routing/cost.py` ricomincia a stimare dichiarandolo.

---

## Grimaldi Lines: preventivo in HTML

Un endpoint solo, `POST booking.grimaldi-lines.com/ajax.php`, che cambia
mestiere secondo il campo `f`: `s1tR` da l'elenco delle 38 rotte con i codici
dei porti (`ITNAP`, `ITPMO`, `ESBCN`...), `s1sL` da il preventivo. Nessuna
autenticazione. Tre cose imparate sul campo:

- **il prezzo e' del gruppo**, non della persona: con due adulti la stessa
  partenza costa il doppio (29,80 contro 59,60). Va diviso, altrimenti il
  confronto con gli altri operatori e' falsato;
- **la sistemazione va chiesta al plurale**: `CABIN|PONT` funziona, `PONT` da
  solo o `CABIN` da solo tornano vuoti;
- **il certificato non valida** sulle CA di sistema (`curl(60)`): la richiesta
  va fatta senza verifica. E' un preventivo pubblico, non c'e' nulla di
  riservato in transito.

La risposta e' un calendario di caselle: contiene qualche giorno attorno a
quello chiesto, e le partenze degli altri giorni vanno scartate, altrimenti
finiscono in un itinerario datato diversamente.

---

## OBB e SBB: orari si', prezzi no

**OBB** parla HAFAS: `POST fahrplan.oebb.at/bin/mgate.exe`, metodo `TripSearch`,
con la chiave di client `OWDL4fE4ixNiPBBm` che l'app ufficiale pubblica (senza,
risponde `200` ma con `err: PARSE`). Gli identificatori sono quelli della
colonna `obb_id` del dataset Trainline, quindi non serve risolvere i nomi.
Copre i diretti verso l'Italia: Vienna-Venezia in 7h16 senza cambi.

Attenzione al formato degli orari: sono `HHMMSS` **locali** con l'offset in
minuti in un campo a parte, e diventano `ddHHMMSS` quando la corsa scavalca la
mezzanotte. Letti come `HHMMSS` i notturni sparirebbero.

**SBB** passa da `transport.opendata.ch/v1/connections`, che non chiede nessuna
chiave (l'API ufficiale `journey-service.api.sbb.ch` invece si', e risponde
401). Gli identificatori sono i codici UIC, cioe' la colonna `cff_id`. Zurigo-
Milano esce correttamente. Due avvertenze: gli errori sono **silenziosi** (200
con liste vuote e `from`/`to` a `null`, mai un 4xx), e il matching dei nomi e'
svizzero-centrico, motivo in piu' per usare gli UIC.

Nessuno dei due pubblica prezzi: le gambe escono senza tariffa e il costo viene
stimato, dichiarandolo. Per la Svizzera la stima e' quasi certamente bassa, e
la nota sulla gamba lo dice.

---

## Tessere e sconti: nessuno li espone in ricerca

Sondati il 28 luglio 2026, un giro per operatore. **Nessuno dei due si apre**:
lo sconto esiste, ma vive piu' avanti nel flusso di acquisto, dopo che si e'
scelta la corsa e si e' identificato il passeggero. Non va riprovato alla cieca.

| Operatore | Cosa si e' provato | Risposta |
|---|---|---|
| **Albatross** (Marino e gli altri 33) | `subGroupId` da 0 a 10 nel corpo; `extra` con `passengerType`, `category`, `discount`, `fareType`; `coupon` con `STUDENT`, `UNIVERSITA`, `TEST` | Sempre `200` con le stesse quattro soluzioni e gli stessi prezzi (83,00 e 75,00 su Torino-Matera). Nessuna variazione di un centesimo. Gli endpoint anagrafici che avrebbero elencato le categorie (`/SubGroups`, `/Categories`, `/PassengerTypes`, `/Discounts`, `/Fares`, `/Tariffs`) rispondono tutti **404** |
| **Itabus** | `membership=true`; `code=STUDENT`; `code=UNDER26` | Sempre le stesse 13 corse con gli stessi totali. I due parametri esistono nella richiesta ma in ricerca non fanno niente: servono al carrello, con l'account collegato |

Dove riprendere, se un giorno serve davvero: registrare le richieste del sito
mentre si completa un acquisto con la tessera, non mentre si cerca. E' l'unico
punto in cui la riduzione compare, ed e' anche quello che richiede credenziali
vere — per questo il time-box si e' fermato qui.

**Nel frattempo le tessere ci sono lo stesso**, dichiarate dall'utente nel
profilo (`app/routing/discounts.py`): una percentuale, un importo a corsa o un
abbonamento che azzera la tratta, con vincoli di operatore, tratta, giorni e
scadenza. Entrano nel totale porta-a-porta e quindi nella classifica, e la voce
resta marcata **«dichiarato da te»** finche' il prezzo ridotto non arriva
dall'operatore. Non e' un ripiego elegante ma e' onesto: un totale che ignora
la tessera che uno ha in tasca sbaglia il confronto, non solo la cifra.

**Trenitalia e' il candidato piu' promettente e non e' stato sondato.** Nella
risposta compaiono gia' i nomi delle offerte (`FrecciaYOUNG`, `YOUNG`,
`isCartaFrecciaProgram`) e un array `discounts` che arriva vuoto: la strada e'
capire quale parametro della richiesta lo riempie.

---

## Regole che valgono per tutti

**Il vuoto non e' un no.** Se l'elenco rotte di un operatore non arriva, la
copertura resta *ignota* e la tratta si prova lo stesso. Memorizzare un elenco
vuoto significherebbe dichiarare che quell'operatore non serve piu' niente, e
sparirebbe da ogni ricerca senza un solo errore visibile. In
`providers/coverage.py` questo e' esplicito: `allow_empty` va passato solo dove
il vuoto e' una risposta vera (Ryanair risponde 404 per gli scali che non serve).

**Niente adapter che fingono.** Un adapter che restituisce zero gambe in
silenzio e' peggio di un adapter assente: sporca la diagnostica e fa credere
che una tratta non esista. Se un operatore non si riesce a interrogare, va in
questa pagina, non nel codice.

**Ogni adapter dichiara una `sample_route`**, una tratta che quell'operatore
serve di sicuro. `scripts/check_providers.py` la usa per accorgersi che un
parser si e' rotto: zero corse li' non e' una giornata vuota, e' un guasto.
