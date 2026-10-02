# Cronologia

Le versioni seguono [semver](https://semver.org/lang/it/). Il testo di ogni
sezione finisce nelle note della release: si scrive per chi usa il programma,
non per chi lo scrive.

## [7.0.2]

**Le preferenze valgono per tutto il viaggio.** «Niente notturni» considera
anche i trasferimenti verso l'aeroporto e le tratte brevi, compreso il cambio
dell'ora di ottobre. Prima poteva restare una partenza alle 02:49 pur avendo
soluzioni diurne disponibili.

- Aggiornati gli orari FAL dal manifesto del 31 agosto, con Pasquetta e
  limitazioni estive riferite al documento giusto.
- Ricontrollate le agevolazioni: tariffe residenti, pass Interrail e convenzioni
  non confermate restano informazioni, senza percentuali inventate nel totale.
  Un catalogo scaricato vecchio non sostituisce piu' quello aggiornato nell'app.
- Migliorata l'attribuzione delle fermate alle citta', mantenendo le correzioni
  per Canelli e Torino.
- Corrette le fermate e gli orari locali Albatross; rinnovate le tratte di prova
  effettivamente verificate. La diagnostica distingue guasti e prove inconclusive.
- Il controllo salute IA distingue misure mancanti da disponibilita' zero;
  un modello scelto manualmente rispetta i limiti dei fornitori a pagamento.
- Corretto l'avvio concorrente del database. Il pacchetto Windows viene
  costruito soltanto con tutti i cataloghi geografici obbligatori.

## [7.0.1]

**«Interpreta» chiede invece di indovinare.** Scrivendo «da Torino a Matera»
senza dire quando, il modello riempiva il buco da solo — quasi sempre con la
data di oggi — e la ricerca partiva su un giorno che nessuno aveva chiesto. Una
supposizione sbagliata non si vede da nessuna parte: sembra una risposta.

Adesso, se manca uno dei tre dati senza cui non si puo' cercare — da dove, dove,
quando — te lo chiede, una domanda per volta, e la risposta la scrivi nello
stesso campo: «Per quando?» → «venerdi 28». Quello che avevi gia' detto resta
valido, quindi non devi riscrivere la frase intera. Sui dati facoltativi non
chiede niente: budget, cambi e valigia hanno un default, e domandarli
rallenterebbe chi aveva gia' scritto tutto.

## [7.0.0]

**Una ricerca poteva rispondere con il viaggio sbagliato, e sembrava giusto.**
Asti → Canelli, ventun chilometri: l'unica soluzione proposta era un pullman di
**diciotto ore** fra Castiglione della Pescaia e Lamezia Terme — Toscana e
Calabria, seicento chilometri più in là. Non era un errore di orario: era la
corsa di un'altra tratta, entrata in classifica come se fosse la risposta.

Succedeva così: Canelli non ha una città nel nostro dataset, il motore gliene
attribuiva una vicina — Acqui Terme — e l'autocompletamento di FlixBus, che è
approssimato e risponde comunque qualcosa, traduceva «asti» in Castiglione
della Pescaia e «acqui terme» in Lamezia Terme. Nessuno confrontava il nome
trovato con quello chiesto. E niente se ne accorgeva a valle, perché quella
corsa portava **le coordinate della tratta chiesta** con i nomi di un'altra.

Tre correzioni, dalla più profonda:

- FlixBus non indovina più: la città proposta si accetta solo se è **dove hai
  chiesto**, e le coordinate lo dicono senza ambiguità — zero chilometri quando
  è giusta, trecento quando non lo è. Le traduzioni sbagliate già salvate non
  sopravvivono all'aggiornamento.
- **Ogni** operatore, anche quelli scritti domani, ora passa da un controllo:
  una corsa che parte o arriva lontano da quello che hai chiesto, o che
  impiegherebbe più tempo di una persona che va a piedi, non entra in
  classifica. Durante le prove ha già preso un secondo caso, di un altro
  operatore: una corsa per **Sibari** dentro una ricerca per Bari.
- Una corsa di FlixBus poteva anche portare il nome di una fermata e la
  posizione di un'altra. Ora non più.

**Il trasporto pubblico locale c'è.** Asti → Canelli adesso risponde:
quarantacinque minuti, linea 41, due euro e cinquanta. Arriva da
[Transitous](https://transitous.org/), che raccoglie gli orari aperti che gli
enti pubblicano — l'attribuzione delle fonti è in fondo alla pagina. È il pezzo
che mancava fuori dalle grandi città, e Matera e Canelli erano esattamente i
posti che ne avevano più bisogno.

**Al consiglio si può rispondere.** Sotto il testo c'è un campo: «perché non la
2? io i cambi li evito», «e a che ora arriva quella che mi hai consigliato?».
Vede la classifica che stai guardando e le preferenze del Profilo, e i numeri
delle soluzioni restano cliccabili anche nelle risposte. Non lancia ricerche e
non tocca il modulo: parla solo di quello che hai davanti, quindi non può
inventare un collegamento che non è stato trovato.

**Le fermate si spiegano da sole.** Cercando «Canelli» la prima pastiglia
proposta era l'aeroporto di **Genova**, a 55 km, e Canelli era la nona — e
l'ordine cambiava a ogni riavvio del programma. Ora le fermate vicine vengono
prima degli aeroporti, dentro ogni gruppo si va per distanza, e su ogni
pastiglia c'è scritto quanto dista. L'ordine con cui il motore sceglie chi
interrogare è rimasto quello di prima, che era tarato: sono due cose diverse e
ora sono separate.

**Le fermate compaiono mentre scrivi.** Prima bisognava premere Invio o
cliccare fuori dal campo, anche avendo scritto il nome per intero: sembrava che
non avesse capito.

## [0.6.1]

**Il mondo arriva anche a chi aggiorna.** Il gazetteer delle città viaggiava nel
pacchetto dalla 0.5.1, ma sta nella cartella `data/` — quella che
l'aggiornamento non tocca mai, per non portarti via database e chiavi. Risultato:
la correzione raggiungeva solo chi installava da zero. Su un'installazione
aggiornata «Londra» rispondeva ancora **Ondara, in Spagna**. Ora dentro `data/`
si scrive quello che manca e si lascia stare quello che c'è già: un dato nuovo
arriva, il tuo non si tocca.

**Il pulsante «Aggiorna e riavvia» non compare più quando non può funzionare.**
Chi fa girare il programma dal codice sorgente lo vedeva, ci cliccava, e solo
allora leggeva che l'aggiornamento automatico vale solo per il pacchetto. Adesso
è scritto prima.

## [0.6.0]

**I treni non spariscono più.** La 0.5.1 chiedeva a Trenitalia l'intera
giornata su **ogni** coppia di stazioni, una richiesta dopo l'altra: venti
coppie, cinque pagine a testa, cento richieste allo stesso sito, ventinove
secondi di coda contro i diciotto che ogni operatore ha per rispondere. Le
ultime non ce la facevano, e dopo tre volte il motore spegneva Trenitalia per
cinque minuti — cioè una ricerca di treni poteva rispondere **senza un treno**.
Adesso la giornata intera si chiede solo sulla tratta che stai cercando, le
pagine partono insieme invece che in fila, e quelle di scorta hanno un tempo
massimo: nel caso peggiore vedi qualche corsa in meno, mai zero. Da cento
richieste a trentasei, e la ricerca dura la metà.

**La ricerca si toglie di mezzo quando hai cercato.** Il modulo restava aperto e
alto, e con le fermate risolte spingeva il primo risultato a ottocento pixel di
scorrimento. Ora si chiude su una riga — «Prato → Torino, dal gio 27 ago» — e
«Modifica» lo riapre esattamente com'era, fermate scelte comprese.

**Quando un operatore non risponde, adesso c'è scritto.** Prima lo diceva solo
il colore di una pastiglia fra tredici, con una spiegazione in gergo inglese
dentro un suggerimento che sul telefono non esiste. Se un operatore cade, sopra
la classifica compare una riga che lo dice: la classifica è meno completa di
come sembra, ed è il genere di cosa che questo programma dichiara.

**«Nessuna soluzione rispetta il tuo vincolo» non si dice più a metà ricerca.**
Compariva mentre gli operatori stavano ancora rispondendo, e a ricerca finita le
soluzioni che lo rispettavano erano due. Finché cerca, adesso è scritto come
provvisorio.

**Il punteggio si legge.** In fondo ai dettagli c'era una fila di decimali senza
etichette. Adesso è un prospetto con gli stessi nomi dei cursori di «Priorità»,
e risponde alla domanda che conta: perché questa soluzione è la prima.

**«Interpreta» non perde più i vincoli per strada.** Scrivendo «massimo due
cambi» il limite restava a tre e il riepilogo non lo nominava: al modello quel
dato non era mai stato chiesto. Ora lo chiede, lo applica e lo scrive.

**Il Profilo ricorda quello che ripeti davvero:** niente notturni, massimo
cambi, budget e valigia da stiva, oltre alla partenza abituale.

**E i prezzi dicono di quando sono.** Si muovono: la stessa corsa è passata da
28,98 a 36,18 euro in diciassette minuti. Accanto ai risultati c'è l'ora in cui
sono stati letti.

## [0.5.1]

**Il pomeriggio esiste.** Su una tratta ferroviaria frequentata la ricerca
mostrava solo le corse del mattino, e taceva sul resto: Prato → Torino si
fermava all'ultima partenza delle 8:43, come se dopo non ci fossero treni.
Trenitalia risponde dieci soluzioni per richiesta a partire dall'ora che le
indichi, e le si chiedeva sempre dalla mezzanotte: quelle dieci finivano prima
di mezzogiorno. Ora la giornata si chiede per intero, e la stessa ricerca arriva
alle 18:40 con sei soluzioni pomeridiane che prima non comparivano. Sulle tratte
con pochi treni non cambia niente e non costa niente: si smette di chiedere
quando l'operatore non ha altro da dire.

**«Parti dopo» non ti riporta più al giorno prima.** Chiedendo un viaggio del 27
in partenza dopo le 13:30, arrivavano corse che partivano alle 23:50 del **26**:
il vincolo confrontava le ore e non le date, quindi la sera prima passava per
«più tardi delle 13:30». Chi cercava il ritorno dopo un impegno si vedeva
proporre, con sicurezza, la notte che lo precedeva. Ora l'ora di partenza guarda
anche il giorno, come già faceva l'ora di arrivo.

**Il mondo arriva anche a chi scarica.** Il gazetteer di trentaquattromila città
introdotto con la 0.5.0 era nel progetto ma non nel pacchetto Windows: nell'app
scaricata «Tokyo» e «Parigi» continuavano a non esistere e «Londra» risolveva su
Ondara, in Spagna. Adesso è dentro. Il file da scaricare pesa di più.

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
