# Verifica Trip Finder 7.1.0

## Suite e applicazione

La suite finale sul workspace temporaneo, con copie dei soli cataloghi pubblici,
supera **525 test**. Comprende regressioni esistenti, runtime/routing, schemi
JSON, cancellazioni, rollback, conferme atomiche con scritture concorrenti,
trasporti MCP e prove browser desktop/mobile.

La prova reale da sorgente ha completato sei richieste Codex: interpretazione
con dati mancanti e secondo turno, viaggio a tappe, consiglio e due chat. Le
API vere e il browser verificano routing Codex-only, proposta e conferma di
uno snapshot sintetico, conversazioni persistenti e nessun errore JavaScript.
Una ricerca Torino–Matera per il 17 novembre, treno/pullman e senza notturni,
ha prodotto 25 itinerari. Non e' una garanzia di disponibilita' futura.

Codex CLI effettivo, avviato da una cartella separata, ha chiamato gli
strumenti Trip Finder con app aperta; ad app chiusa riceve l'errore leggibile
che invita ad aprirla. Il collaudo usa un processo CLI separato dal daemon
condiviso e la capability creata dal setup dell'app. I primi tentativi di
collaudo privi di questi prerequisiti sono conservati nei report locali,
insieme alla successiva verifica riuscita.

## Windows e aggiornamenti

Il pacchetto compilato ha superato i sei controlli nativi dell'app e del
browser. L'helper MCP console e il launcher portabile corretto hanno poi
superato negoziazione SDK con nove strumenti e chiamate Codex CLI ad app
aperta e chiusa. Una regressione interattiva riproduce il problema di
buffering del launcher e verifica risposte prima della chiusura di stdin.

I veri updater compilati installato 7.0.1 e pubblicato 7.0.2 hanno aggiornato
copie isolate alla 7.1.0, conservando SQLite, configurazioni e vendor e
riavviando sulla porta di collaudo 8099. Anche il nuovo updater e' stato
eseguito su un'installazione organizzata: conserva launcher, layout e stato,
rilascia il proprio lock e riavvia sulla stessa porta. Entrambi i formati ZIP
superano integrita', assenza di dati privati e avvio con tutti i 43 operatori.

La migrazione pubblica compilata, senza Python, e' stata eseguita su dati
sintetici con il suo dialogo Windows: backup esterno, snapshot SQLite valido,
stato/vendor/credenziali conservati e stato del portable sorgente escluso.
Le prove degli updater identificano gli hash dei binari storici e degli ZIP.
Dopo l'ultimo aggiornamento dati, entrambi gli ZIP finali hanno superato
nuovamente avvio, privacy e confronto di tutti i campi del catalogo corrente,
incluse le date Young/Senior. Questi collaudi usano solo archivi sintetici.

## Assistenti e limiti delle prove

Codex e' collaudato tramite account nativo esistente, su dati sintetici, senza
fallback API. Claude Code e' implementato come runtime opzionale e coperto
dai test di protocollo, isolamento, schemi ed errori; non e' attivato ne'
provato live con un abbonamento in questo rilascio.

Hermes e altri endpoint compatibili sono configurabili con URL, modello e
chiave dell'utente. Hermes puo' inoltre consumare il MCP. Non e' incluso un
runtime Hermes CLI dedicato e non e' stato installato, configurato o provato
live. La compatibilita' implementata non sostituisce un backend/provider.

Nessun test implica compatibilita' live gia' verificata con tutte le app
desktop degli assistenti. Nessun acquisto, prenotazione, pagamento o contatto
con Transitous e' stato eseguito.

## Manutenzione

Il manifesto FAL del 12 ottobre sostituisce quello del 31 agosto: 76 corse,
74 invariate, una corsa senza Venusio e una anticipata di cinque minuti.
Tariffe ufficiali invariate. Sono state controllate entrambe le pagine del PDF.
I tre operatori bus restano inconclusivi con risposte grezze vuote, senza
cambiare pin in assenza di una corsa verificata. Fonti tessere e limiti sono
descritti nel [report dei dati statici](verification-static-data.md).

Young e Senior conservano il 20%, con documento efficace dal 20 ottobre 2026
e viaggi fino al 31 dicembre 2027. Il controllo automatico dei PDF e' tornato
verde dopo la correzione della nuova formulazione di validita'; la suite
protegge sia il formato precedente sia date mancanti o incoerenti.

I report dettagliati e gli screenshot di collaudo restano nel workspace
locale ignorato da Git. Nessuno stato o configurazione personale entra nei
pacchetti pubblici.
