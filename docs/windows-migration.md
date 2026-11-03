# Migrazione Windows al layout app/state

La migrazione è disponibile nel pacchetto compilato: non richiede Python.
Gli aggiornamenti automatici continuano a usare il trasporto storico flat;
questa operazione parte solo dal comando esplicito dell'utente.

1. Chiudi Trip Finder scegliendo **Esci** dal menu della bussola.
2. Scarica `TripFinder-portable.zip` ed estrailo in una cartella nuova,
   esterna alla vecchia installazione. Conserva entrambe le copie.
3. In PowerShell esegui, sostituendo i percorsi:

```powershell
& 'C:\Pacchetti\TripFinder\TripFinder.exe' --migrate-from 'C:\Applicazioni\TripFinder'
```

Il primo eseguibile è quello del nuovo portable. Il secondo percorso è la
cartella dell'installazione precedente, a programma chiuso. Puoi scegliere
un backup diverso con una destinazione nuova ed esterna:

```powershell
& 'C:\Pacchetti\TripFinder\TripFinder.exe' --migrate-from 'C:\Applicazioni\TripFinder' --migration-backup 'C:\Backup\TripFinder-prima-migrazione'
```

Una finestra mostra successo o errore anche senza console. Il backup predefinito
è una cartella sorella con suffisso `.backup-AAAAMMGG-HHMMSS`; un backup già
esistente non viene sovrascritto. La copia originale rimane disponibile come
checkpoint; l'installazione conserva il proprio percorso e passa a:

```text
TripFinder.exe
LEGGIMI.txt
app/
state/
```

Il database viene copiato tramite snapshot SQLite coerente; preferenze,
credenziali, cataloghi e personalizzazioni vendor sono conservati. Lo stato
eventualmente presente nel nuovo portable non viene mescolato a quello della
vecchia installazione. Symlink e junction bloccano la migrazione con un errore
esplicito anziché essere seguiti.

Dopo il successo apri `TripFinder.exe` nel percorso originale e verifica
profilo, ricerche salvate, tessere e assistenti. Rigenera i blocchi MCP dalle
Impostazioni, perché il percorso della capability passa da `data/` a `state/`.
Non eliminare il backup prima di questi controlli.

Per tornare indietro, chiudi l'app, rinomina l'installazione nuova per
conservarla e rimetti la cartella backup al nome e al percorso originali.
Mantieni entrambe le copie: non unire cartelle e non ripristinare soltanto il
database. Per automazioni da sorgente è disponibile anche
`scripts/migrate_installation.py` e la funzione di rollback
`app.install_migration.restore_installation`.
