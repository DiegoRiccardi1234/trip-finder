# Note tecniche

Metodo e trappole raccolti costruendo Trip Finder. Non è documentazione
dell'API: è quello che non si trova leggendo il codice, e che costa una serata
riscoprire. Lo stato di ogni singolo operatore sta in
[`operatori.md`](operatori.md).

---

## Aprire un'API che non vuole essere aperta

Quasi nessun operatore di trasporto pubblica un'API. Quasi tutti ne hanno una,
perché il loro sito la usa. Questa è la procedura che ha funzionato ogni volta,
**in quest'ordine**: il primo passo che riesce è quello giusto, e i successivi
costano molto di più.

1. **Prova diretta con `curl_cffi`** (`impersonate="chrome"`). Passa dove
   `requests` e `fetch` prendono 403, perché il blocco è sul fingerprint TLS e
   non sullo `User-Agent`. È il passo che risolve la maggioranza dei casi, ed è
   il più economico: una richiesta.
2. **Se torna 403, 401 o 404** → scarica la pagina di ricerca e fai il **grep
   dei bundle JavaScript** cercando host (`api.`, `booking.`, `/api/v`) e
   percorsi REST. Spesso l'host vero è diverso da quello che serve la pagina,
   che è solo storage statico.
3. **Se il base URL è iniettato a runtime** → cerca un file di configurazione
   accanto al bundle: `apiUrl.js`, `config.json`, `assets/env.json`. Su una
   piattaforma bus italiana, `window.apiUrl.serverUrl` conteneva tutto.
4. **Se ancora niente** → apri il sito col browser e **registra le richieste**:

   ```python
   async with pool.page() as page:
       page.on("request", lambda r: seen.append((r.method, r.url, r.post_data)))
       await page.goto(URL, wait_until="domcontentloaded")
       await page.wait_for_timeout(15000)   # lascia partire la ricerca
   ```

   Serve un **deep link che faccia partire la ricerca da solo**: altrimenti la
   pagina si carica e non chiama niente, e si registra il vuoto.
5. **Riprova la chiamata trovata con `curl_cffi`.** Se passa, l'adapter è di
   tier 1 e non serve un browser. Se dà 401 perché il token nasce dentro la
   pagina, serve un provider che chiami l'API **da dentro** il sito ereditandone
   cookie e token.

**Time-box: un solo giro.** Se non si apre, l'evidenza va scritta e si passa
oltre. La regola che tiene in piedi tutto il resto è questa:

> Un adapter che restituisce zero risultati in silenzio è peggio di un adapter
> assente. Il primo fa credere che quella tratta non esista; il secondo almeno
> si vede che manca.

### Trovare gli operatori gemelli di una piattaforma

Molti operatori piccoli condividono lo stesso motore di prenotazione, uno per
host. Trovato uno, si trovano gli altri — ma non tutte le strade durano:

- il **bundle della pagina** a volte elenca gli host gemelli, ma è una fonte che
  si esaurisce: quando ogni installazione legge il proprio indirizzo da un file
  di configurazione suo, nel bundle non resta traccia delle altre;
- gli **endpoint anagrafici aperti** (`/Carriers`, `/Companies`, `/Lines`) sono
  la fonte che dura. Rispondono spesso senza autenticazione, e i vettori portano
  l'email: il dominio dell'email è il dominio del sito;
- **la ricerca è transitiva** e si esaurisce da sola in due giri.

**Per la tratta di prova non tentare a caso.** Con trecento fermate le
combinazioni sono decine di migliaia. Se c'è un endpoint delle linee, le
descrizioni contengono i capolinea scritti da un umano («Matera-Potenza-Roma»):
da lì escono le coppie da provare. E **rispondere non significa vendere**: molte
installazioni rispondono `200` con zero soluzioni su qualunque coppia e mese,
perché sono aziende di trasporto locale che usano la piattaforma per gli
abbonamenti. Non è un guasto.

---

## Le trappole che sono costate di più

Sono tutte silenziose. Nessuna di queste ha mai prodotto un errore.

**Qualunque `display` d'autore batte `[hidden]`.** L'attributo `hidden` del
browser è una regola CSS come le altre, e perde contro qualsiasi `display`
dichiarato dopo: `.row`, `label`, `.cards`, e persino `display: contents`. Il
risultato è un blocco che resta visibile pur avendo `hidden` a `true`.
Rincorrerle una per una non funziona: dove ci sono figli che si nascondono, si
mette **una regola sola sul contenitore**.

**aiosqlite si pianta in due modi diversi, entrambi muti.** La connessione è
legata al loop che l'ha creata: riusarla da un altro loop non dà errore, si
blocca. E dalla 0.20 vive su un thread interno **non demone**, quindi finché
resta aperta il processo non muore — tutti i test passano e `pytest` non esce
più. Servono un hook che chiuda la connessione a fine test e una chiusura
esplicita negli script.

**Un prezzo stranamente basso non è un affare, è un bug.** Su una soluzione
esaurita, un operatore ferroviario espone il prezzo di *quel che resta da
vendere*: con il treno veloce pieno, un Torino-Roma risultava **1,50 €** (il
solo regionale finale) contro i 105-141 € delle soluzioni gemelle davvero
acquistabili. Con quella cifra vince ogni classifica. Regola: **prima del prezzo
si guarda lo stato**, e una soluzione non acquistabile non è un'opzione di
viaggio.

**Il prezzo può essere del gruppo, non della persona.** Un traghetto raddoppiava
il totale con due passeggeri (29,80 → 59,60). Si scopre con una richiesta:
cambiare il numero di adulti e vedere se il numero raddoppia. Tutto il progetto
ragiona per passeggero, quindi una gamba che ragiona per gruppo falsa ogni
confronto.

**Il vuoto non è un no.** Se l'elenco delle rotte di un operatore non arriva, la
copertura resta *ignota* e la tratta si prova comunque. Memorizzare un elenco
vuoto significa dichiarare che quell'operatore non serve più niente: sparisce da
ogni ricerca futura, senza un solo errore visibile.

**Fusi orari: non fidarsi dell'offset.** Un operatore serializzava in UTC
(`+00:00`) allegando però il fuso di lettura a parte. Va **convertito**, non
reinterpretato. Letto male, ogni corsa risultava due ore prima, e con lei
sbagliavano il riconoscimento del viaggio notturno e la penalità sull'orario di
arrivo.

**Un elemento con `id="reset"` dentro un form oscura `form.reset()`.** La
proprietà restituisce il bottone invece del metodo, e la chiamata muore con «is
not a function». Vale per qualunque nome che collida con un metodo del form.

---

## Quanto costa davvero una ricerca

Il collo di bottiglia non è la concorrenza, è **il limite di richieste per
dominio**. Misurato su Torino → Matera con i valori di default (4 richieste al
secondo per dominio, 20 in volo in tutto):

| | |
|---|---|
| Percorsi candidati | 8 |
| Interrogazioni agli operatori | ~520 |
| Primi 25 itinerari a schermo | 6-7 secondi |
| Ricerca dichiarata completata, cache fredda | ~31 secondi |
| Idem, cache calda | ~16 secondi |

Due conseguenze non ovvie:

- **contare i task non misura il costo.** Chi esce dalla cache di copertura non
  fa rete: aggiungere coppie costa pochissimo in richieste e tantissimo in
  attesa, se quelle richieste vanno tutte sullo stesso host;
- **il sintomo di un limite troppo stretto è un circuito aperto, non un
  errore.** Togliendo il tetto agli aeroporti, un operatore ha accumulato
  timeout finché il circuit breaker non l'ha escluso: i voli sono spariti dalla
  classifica e non è comparso un solo messaggio. Un'API pubblica e leggera
  merita un ritmo suo.

La coda dei 31 secondi non sono i risultati — quelli ci sono dopo sei — ma gli
operatori più lenti che chiudono e il consiglio dell'IA, che arriva per ultimo.

---

## Verificare un'interfaccia

**Si misura il pixel, non l'attributo che dovrebbe averlo prodotto.** Lo stato
interno e quello che l'utente vede sono due cose diverse, e su un'interfaccia
conta solo il secondo: `getBoundingClientRect().height > 0`, il colore
calcolato, la posizione. Dichiarare verificato qualcosa che a schermo è rotto
costa più di un bug ammesso — ed è così che la trappola `display` contro
`[hidden]` è sopravvissuta a due «funziona».

**Con Playwright non si aspetta a tempo, si aspetta lo stato.** Il bottone che
si disabilita dice che la ricerca è partita; che si riabilita dice che è finita.
Un `wait_for_timeout` tarato a occhio misura la propria pazienza, non il
programma.

**Attenzione alla cache del browser durante le prove.** Una regola CSS nuova può
risultare «non applicata» solo perché il foglio di stile arriva dalla cache
della sessione: prima di dare la colpa al selettore, conviene verificare che la
regola sia davvero nel documento.

---

## Windows, due dettagli che fanno perdere un'ora

- Serve il pacchetto `tzdata`: non c'è un database dei fusi di sistema, e senza
  quello ogni conversione di orario fallisce.
- La console parte in cp1252, quindi gli script muoiono sul primo nome
  accentato. Si forza UTF-8 **prima** di qualunque import.
- `pythonw.exe` più uvicorn è un server che non parte: senza console
  `sys.stdout` è `None` e il logging muore alla prima riga, in silenzio. Lo
  stdio va riparato prima degli import e i log vanno su file.
- In PowerShell, `comando | Select-Object -First N` non mostra niente finché il
  processo non termina: se quel processo è appeso, sembra essersi bloccato prima
  di stampare. Per capire dove si ferma davvero, si redirige su file e si legge
  il file.
