# Cronologia

Le versioni seguono [semver](https://semver.org/lang/it/). Il testo di ogni
sezione finisce nelle note della release: si scrive per chi usa il programma,
non per chi lo scrive.

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
