# Assistenti, Chat viaggi e MCP

Trip Finder funziona anche senza intelligenza artificiale. Il modulo di ricerca
rimane disponibile senza account o chiavi. Ogni utente sceglie le proprie
connessioni in **Impostazioni → Assistenti e connessioni**.

## Scegliere una connessione

- **Codex:** installa il client ufficiale, accedi con il tuo account e abilita la
  connessione nell'app. Le richieste usano App Server in un processo dedicato.
- **Claude Code:** supporto nativo facoltativo mediante il client ufficiale.
  Occorre un accesso attuale idoneo. Un vecchio login non prova che un
  abbonamento sia ancora utilizzabile.
- **Hermes Agent:** crea una connessione OpenAI-compatible con l'indirizzo del
  gateway già configurato, il modello e l'eventuale chiave. Trip Finder non
  installa, avvia o gestisce Hermes; serve comunque un suo backend/provider.
  Hermes può anche usare Trip Finder come client MCP esterno.
- **Altri endpoint compatibili:** sono ammessi più collegamenti distinti. HTTP
  è consentito soltanto su loopback; per endpoint remoti occorre HTTPS.

Le attività sono separate: interpretazione del viaggio, suggerimento facoltativo
degli scali, consigli/confronti e chat. Puoi scegliere motori diversi e una lista
ordinata di fallback espliciti. Lasciare **Provider API esistenti** conserva la
selezione precedente. Una route assegnata non riattiva vecchi provider quando
fallisce: senza fallback configurati mostra l'errore.

Gli scali suggeriti dall'IA hanno un budget breve: se il motore non risponde
in tempo, la ricerca usa la composizione geografica deterministica. Gli
strumenti della chat e del MCP usano direttamente questa composizione, senza
avviare un'altra inferenza mentre il modello sta già organizzando il viaggio.

I runtime sono disattivati finché scelti. I provider API precedenti rimangono
disponibili; i pagamenti restano disabilitati per impostazione predefinita.
Le configurazioni personali non vengono distribuite nel pacchetto pubblico.

La diagnostica non genera una risposta del modello. Il test sintetico di una
connessione, se richiesto, consuma invece la quota del relativo account/provider.
Una quota sconosciuta non vale zero; l'elenco dei modelli non prova il diritto
di usare un modello. Gli errori di accesso o quota non attivano altri servizi.

## Usare Chat viaggi

Puoi iniziare una conversazione oppure premere **Continua in Chat viaggi** da
una classifica. Se manca un dato essenziale la chat lo chiede. Una richiesta
esplicita di ricerca interroga gli operatori e mostra avanzamento e risultati;
**Interrompi** ferma il lavoro della conversazione mantenendo quanto raccolto.

Ricerche, operatori e tessere producono dati verificabili: la chat non prenota,
non compra biglietti e non effettua pagamenti. Stime, operatori non verificati
e rischio dei biglietti separati rimangono dichiarati.

Un salvataggio o una modifica al profilo/tessere prepara una proposta nel
**Profilo**. Controlla il contenuto e premi **Conferma** oppure **Rifiuta**.
L'assistente non può confermare da solo. Se nel frattempo il profilo è cambiato,
serve una proposta aggiornata.

Gli itinerari conservati sono copie storiche: prezzi, orari e disponibilità
possono essere cambiati. Restano disponibili i parametri per ripetere la ricerca.
Conversazioni e salvataggi vivono sul computer, nello stesso stato locale
dell'applicazione, non nel browser.

## Collegare un assistente esterno via MCP

È una direzione distinta dall'IA interna: Codex, Claude o Hermes possono usare
gli strumenti di Trip Finder senza cambiare il motore scelto dentro l'app.

1. Apri Trip Finder e vai nelle Impostazioni.
2. Scegli il client e premi **Mostra configurazione MCP**.
3. Inserisci il blocco nella configurazione del client. Usa i percorsi generati
   per questa installazione, non quelli di un altro computer.
4. Riapri o ricarica il client e verifica gli strumenti `tripfinder_*`.

Il setup contiene il percorso della capability, non il suo valore. Mantieni
privati i file di stato. L'adattatore STDIO deve raggiungere l'app già aperta:
non crea un secondo archivio e segnala di aprirla quando non è disponibile.
La porta effettiva e l'identità dell'installazione evitano di collegarsi ad
un'altra copia di Trip Finder.

Gli strumenti espongono contesto, risoluzione località, operatori, tessere,
ricerche con avanzamento/cancellazione e proposte. Non eseguono altra IA;
l'assistente esterno interpreta e spiega i risultati nella propria conversazione.
Non sono disponibili SQL, accesso a file arbitrari, chiavi, aggiornamenti,
prenotazioni o conferme delle proposte.

La configurazione non certifica una prova reale del client. I report di rilascio
distinguono supporto implementato, configurazione effettiva, test automatici e
collaudi live. Un test Codex CLI non certifica Claude Desktop o Hermes.

## Windows: pacchetto e aggiornamenti

Per una nuova installazione organizzata scegli `TripFinder-portable.zip`:

```text
TripFinder.exe
LEGGIMI.txt
app/
state/
```

`TripFinder-windows.zip` conserva il trasporto storico per gli updater già
installati. Le installazioni precedenti restano utilizzabili nel layout con
`data/` finché migrate esplicitamente. Non spostare manualmente solo il database:
anche credenziali, cataloghi e preferenze devono seguire lo stesso stato.

La migrazione a programma chiuso crea un backup esterno e dispone di rollback.
I client ufficiali Codex e Claude Code rimangono installazioni separate del
singolo utente e non sono redistribuiti dentro Trip Finder.

Riferimenti: [Codex App Server](https://learn.chatgpt.com/docs/app-server),
[Claude Code programmatico](https://code.claude.com/docs/en/headless),
[Hermes API server](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/api-server.md),
[Hermes MCP](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/mcp.md).
