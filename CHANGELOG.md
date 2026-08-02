# Cronologia

Le versioni seguono [semver](https://semver.org/lang/it/). Il testo di ogni
sezione finisce nelle note della release: si scrive per chi usa il programma,
non per chi lo scrive.

## [0.5.0]

**Il mondo si trova.** Prima, fuori Europa, la ricerca non falliva: rispondeva
un'altra cosa. «Tokyo» e «New York» non esistevano affatto, e «Londra» — che nel
dataset europeo non c'è — finiva per somiglianza su **Ondara**, in Spagna, con
otto fermate vere di una città che nessuno aveva chiesto. Adesso ci sono
trentaquattromila città di tutto il pianeta, con i loro nomi in italiano:
Parigi, Londra, Il Cairo, Monaco di Baviera, Lisbona.

**E si trova all'ora giusta.** Gli aeroporti fuori dai paesi europei non avevano
fuso orario e ripiegavano su quello di Roma: un volo giapponese sarebbe
comparso con sette ore di scarto, senza che niente lo segnalasse. Ora il fuso
arriva dal gazetteer, e sono giusti Tokyo, New York, San Paolo, Sydney, Dubai.

**I prezzi non si sommano più fra valute diverse.** Il totale metteva insieme
gli importi senza mai guardare la valuta: novantacinque sterline diventavano
novantacinque euro, stampate col simbolo dell'euro, sbagliate del venti per
cento — e siccome la classifica ordina sul prezzo, cambiava anche *quale*
viaggio veniva consigliato. Adesso si convertono al cambio del giorno della
Banca centrale europea, e quello che non si sa convertire si dichiara incerto
invece di passare per una cifra certa.

**Gli scali esistono anche fuori Europa.** Erano ottantotto città scritte a
mano, tutte europee: su una tratta asiatica non ne restava nessuno e c'erano
solo i voli diretti, che fra Tokyo e Lima non esistono.

**E dove non arriviamo, lo diciamo.** Tokyo → Osaka trovava la città, non
trovava nessun operatore e mostrava una pagina vuota. Ora spiega che la ricerca
copre l'Europa, e soprattutto **nomina chi quella tratta la fa davvero**: lo
Shinkansen, Amtrak, Renfe, Eurostar, il KTX coreano, con il link per comprare.
È la stessa cosa che il programma faceva già per Italo e Tirrenia, estesa al
mondo. Di Volotea, che pubblica le sue rotte ma non la ricerca, sono dichiarate
tutte e centocinquantacinque le tratte da e per l'Italia.

**Le fermate delle tappe.** In un viaggio a tappe, «poi vado a Torino» non
lasciava scegliere fra Porta Nuova, Porta Susa e Caselle come fanno i campi
«Da» e «A». Adesso sì, e la scelta vale anche per la ripartenza: chi dice «a
Torino solo Porta Susa» da lì riparte.

**Il consiglio conta anche i cambi.** Prezzo e durata gli arrivavano già
calcolati; da adesso anche i cambi e i biglietti in più.

Sotto il cofano, due cose che si vedono solo se mancano: la pagina ha i suoi
primi test — un browser vero che verifica numeri, riferimenti e ordinamenti —
e le trentacinque autolinee italiane hanno smesso di dichiararsi disponibili su
Parigi → Berlino, dove si prendevano una richiesta a testa per scoprire di no.

## [0.4.0]

**Il consiglio dice di quale soluzione parla.** Prima diceva «l'opzione con
Ryanair» — e di voli Ryanair ce n'erano due — oppure citava «la Torino → Matera
4» mentre a schermo nessun 4 esisteva da nessuna parte: la numerazione viveva
solo dentro la domanda fatta al modello. Adesso ogni scheda porta il suo numero,
in alto a sinistra, e nel testo del consiglio quel numero è un bottone: si
clicca e si finisce sulla scheda giusta, evidenziata per un attimo. Il numero
resta attaccato alla soluzione anche riordinando per prezzo o filtrando il
mezzo, quindi un consiglio già scritto continua a puntare dove deve.

**E dice il vero.** Su andata e ritorno commentava i prezzi della sola andata,
sotto schede che mostravano il totale di entrambe; non sapeva che le soluzioni
erano venticinque e non sei, e poteva scrivere «è l'unica sotto i 50 euro»
essendo falso; non sapeva che la lista era **fuori** dai vincoli quando nessuna
soluzione li rispettava, e poteva consigliare un viaggio oltre il budget senza
avvisare. Ora il consiglio nasce dalla pagina e non dal motore, quindi il
modello vede esattamente quello che vedi tu.

**I conti non li fa più lui.** «Ti fa risparmiare quasi due ore» fra 12h35 e
12h30: i dati erano giusti, la sottrazione no, dentro una frase che si legge
benissimo. Le differenze di prezzo e di durata ora arrivano già calcolate, e al
modello resta il giudizio.

**Quando non arriva, si riprova.** Il messaggio diceva «le chiavi si mettono
nelle impostazioni» anche quando le chiavi c'erano e a sbagliare era stato il
modello. Adesso ogni motivo porta il suo rimedio, un secondo tentativo parte da
solo — il server ha appena penalizzato il modello che ha fallito, e ne tocca un
altro — e poi resta il bottone «Riprova». Un consiglio che cita soluzioni
inesistenti, o che non ne cita nessuna, viene scartato prima di arrivare a
schermo.

**Il prezzo «a partire da» adesso si chiama stima.** Trenitalia manda certe
tariffe come minimo del giorno, non come prezzo di quella corsa: l'avvertenza
c'era, ma solo dentro «Dettagli», e il totale in grande sembrava una cifra
certa. Ora la scheda lo dichiara come tutte le altre voci stimate. La classifica
non cambia: quel segnale non ha mai pesato sul punteggio.

La ricerca si chiude anche un po' prima, perché non aspetta più l'IA: il
consiglio se lo prende la pagina quando i risultati sono già lì.

## [0.3.2]

**L'aggiornamento arriva in fondo davvero.** La 0.3.1 aveva corretto quattro
difetti su cinque. Il quinto si vedeva solo premendo il bottone per davvero: il
programma non si spegneva, e teneva bloccato il proprio eseguibile finché
l'aggiornatore non si arrendeva. Chiudeva chiedendo al proprio thread di
aspettare sé stesso, cosa che sul momento non dà errore — l'eccezione la ingoia
il ciclo di eventi — e da fuori sembrava soltanto un aggiornamento che non
finisce.

Misurato adesso, dal bottone: il programma vecchio si spegne in sei secondi,
855 file vengono sostituiti senza toccare i dati, e quello nuovo risponde
diciassette secondi dopo aver premuto.

## [0.3.1]

**L'aggiornamento automatico ora arriva in fondo.** Diceva «sostituzione in
corso» e non finiva mai. Quattro difetti, ciascuno sufficiente da solo a
fermarlo, e tutti scritti nel log che nessuno aveva ancora letto.

L'aggiornatore non aspettava davvero la chiusura del programma: su Windows
`os.kill(pid, 0)` non è una domanda, e tornava subito. Poi copiava i file
mentre Windows non aveva ancora rilasciato gli handle, e si prendeva un «file
utilizzato da un altro processo». Stava anche riscrivendo sé stesso mentre
girava — cosa che Windows non permette — e ora gira da una copia nel
temporaneo, portandosi dietro le librerie che gli servono per partire. Infine
riapriva il programma staccandolo del tutto, e quello moriva alla prima riga
che scriveva, prima ancora di aprire la porta: da fuori sembrava un
aggiornamento che non finisce.

Adesso un file bloccato si riprova per una trentina di secondi prima di
arrendersi (l'antivirus su un archivio appena scompattato se lo tiene anche
venti), e se proprio non cede il log dice **quale** file era. La pagina intanto
non dice più «riaprilo a mano»: aspetta che risponda la versione nuova, e se
non arriva dice dove guardare.

Chi ha installato la 0.2.0 o la 0.3.0 deve scaricare questa a mano una volta:
l'aggiornamento lo fa il programma installato, e quelli hanno ancora la
versione rotta. Dalla 0.3.1 in poi funziona dal bottone.

## [0.3.0]

**Le tessere, trattate come un dato che scade.** Perché lo sono: delle tredici
voci raccolte ad agosto 2026 cinque portano una data dentro, e la Carta Verde e
la Carta Argento hanno smesso di esistere il 4 aprile. Il catalogo quindi non è
scritto a mano: lo estrae uno script dalle Condizioni Generali di Trasporto —
i PDF pubblici che vincolano l'operatore — leggendone percentuale, limiti d'età
e finestra di validità. Le pagine riassuntive dicevano che la Carta Verde è
sparita il 1° aprile; il documento dice il 4.

**E il prezzo lo dice l'operatore, non una nostra stima.** Trenitalia manda già
le sue tariffe ridotte dentro la risposta di ricerca. Dichiarando la tessera, la
classifica usa quella cifra per quella corsa e quel giorno, e la riga di costo
lo scrive: su Torino → Roma il più economico passa da 60,90 € a 39,00 €. È
l'unico strato che non invecchia. Dove l'operatore non espone niente resta la
percentuale del catalogo, segnata «dichiarato da te»: due livelli di fiducia,
due etichette diverse.

**Si aggiorna da solo.** Il programma riscarica il catalogo una volta al giorno,
così chi ha installato ad agosto vede le scadenze di dicembre senza aggiornare
niente. Se non c'è rete resta quello che c'era, e un file scaricato male non
sostituisce mai uno buono. In più, ogni lunedì un controllo rilegge le fonti e
apre un issue quando qualcosa si è mosso — con una distinzione che è costata un
falso allarme per impararla: una fonte che non risponde non è una fonte che è
cambiata.

Nel profilo le tessere si scelgono da un menu invece di ricordarsele, con
accanto cosa serve per averle e il link al documento da cui vengono.

## [0.2.0]

**Un'applicazione da scaricare.** Dalla release si prende `TripFinder-windows.zip`,
si scompatta e si fa doppio click: niente Python, niente terminale, niente
installazione. I cataloghi delle stazioni e degli aeroporti sono gia' dentro,
quindi il primo avvio funziona anche senza rete. Il programma vive nell'area di
notifica e si aggiorna da solo dalle impostazioni.

**Le chiavi dell'IA si mettono dal sito.** C'e' una scheda Impostazioni con una
scheda per fornitore, il link dove procurarsi la chiave e cosa da' il piano
gratuito. Sono undici, cinque dei quali gratuiti e senza carta. Restano su
questo computer, in `data/local_secrets.json`, e valgono subito: niente
riavvio. I fornitori a pagamento restano spenti finche' non li si accende — non
basta che la chiave ci sia.

**Il consiglio dell'IA non sparisce piu' in silenzio.** Quando manca, adesso la
pagina dice perche': nessuna chiave, fornitore che ha rifiutato, risposta
troncata. Prima il blocco spariva e basta, e l'IA rotta era indistinguibile
dall'IA assente.

E funziona di nuovo. Le chiamate allo stesso fornitore vanno in fila invece di
partire tutte insieme — erano decine nello stesso secondo, e si prendevano un
`429` dietro l'altro finche' non restava un modello utilizzabile. Un rifiuto
temporaneo ora fa aspettare e riprovare, non scartare il modello; e la penalita'
per un `429` dura minuti invece di mezz'ora, perche' e' il throttle dell'host e
non un difetto del modello. Corretto anche il tempo massimo di attesa, che era
quello delle API dei vettori (venti secondi) e tagliava le risposte a meta'.

**Viaggi a tappe.** «Da Matera a Roma per tre giorni, poi a Torino» adesso e' una
cosa sola: le tappe si cercano in fila, la data di ognuna nasce dall'**arrivo**
della precedente piu' la sosta — un notturno arriva il giorno dopo, e conta — e
in fondo c'e' il totale del viaggio e un consiglio su come stanno insieme le
tappe, non solo su ciascuna.

**Quattro schede.** Ricerca, Profilo, Impostazioni, Info. Il profilo non e' piu'
una fisarmonica sopra il modulo di ricerca.

## [0.1.0]

Prima pubblicazione. Motore di ricerca multimodale con 42 adapter di operatori,
classifica sul costo reale porta-a-porta, profilo e ricerche salvate su SQLite,
avvio con icona nell'area di notifica.
