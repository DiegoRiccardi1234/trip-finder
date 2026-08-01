# Cronologia

Le versioni seguono [semver](https://semver.org/lang/it/). Il testo di ogni
sezione finisce nelle note della release: si scrive per chi usa il programma,
non per chi lo scrive.

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
