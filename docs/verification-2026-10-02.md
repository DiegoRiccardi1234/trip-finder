# Verifica Trip Finder 7.0.2 — 2 ottobre 2026

## Risultati

- Suite locale finale: **388 test superati**, nessuno saltato. Un avviso di
  deprecazione riguarda Starlette/TestClient; non e' un errore dell'app.
- Sei richieste al server reale sulla porta 8099: tutte superate con Google
  gratuito, incluso il secondo turno e Torino–Roma–Matera con tre giorni a Roma.
  Il browser ha confermato domanda sulla data, conservazione delle tappe e
  compilazione del modulo.
- Ricerca manuale Torino–Matera del 16 ottobre, senza notturni: 25 soluzioni,
  nessuna partenza nella fascia notturna; il trasferimento alle 02:49 della
  versione precedente e' escluso. Streaming e completamento verificati.
- Asti–Canelli, solo bus, 16 ottobre: due soluzioni coerenti, con prezzo
  esplicitamente stimato. Il contatto con Transitous resta escluso.
- Viaggio Torino–Roma–Matera: entrambe le tappe completate, 25 soluzioni
  ciascuna, seconda partenza il 19 ottobre dopo tre giorni a Roma.
- Diagnostica dei 43 operatori: 39 verificati il 16 ottobre; OneBus verificato
  separatamente il 19 ottobre. Alta Badia Bus, Tiemme e MarinoBus Urbano sono
  inconclusivi, con risposta vuota e vecchi pin conservati. Non dichiarati
  stagionali o guasti senza evidenza.
- Bundle Windows locale: ZIP integro, tre cataloghi geografici, 43 operatori,
  versione 7.0.2, MXP risolto su Gallarate, catalogo tessere del 2 ottobre.
  L'aggiornamento in installazione isolata conserva file utente e database;
  nello ZIP non entrano chiavi, file .env o database personali.

## Correzioni verificate

Preparazione dei parser condivisa fra ricerca e diagnostica; orari Albatross
locali e nodi porto corretti; peso Trainline neutro; FAL aggiornato dal manifesto
ufficiale; calendario con Pasquetta e finestre datate; agevolazioni informative
senza percentuali infondate; catalogo piu' recente protetto dai download
retrogradi. Il dettaglio delle fonti e dei limiti sta nel
[report dei dati statici](verification-static-data.md).

Il filtro notte considera anche trasferimenti e tratte brevi. Durate e margini
di coincidenza confrontano istanti UTC durante entrambi i cambi d'ora. L'apertura
SQLite concorrente usa un unico lock e chiude la connessione dopo errori di
inizializzazione. Il builder si ferma senza cataloghi obbligatori o se le
directory risolte di build escono dal progetto.

## IA: osservazioni dal vivo

OpenRouter ha rifiutato i due Gemma con HTTP 429 **upstream**: il corpo dell'API
lo dichiarava, e l'uso giornaliero della chiave risultava zero. Non e' prova di
quota giornaliera esaurita. Lo slug GLM gratuito senza endpoint e' stato rimosso.
Le misure mancanti di uptime restano non verificate, distinte da uptime zero.
Pin manuali e fallback rispettano salute e disattivazione dei modelli a pagamento.

La nuova chiave Google ha confermato che i 2.5, pur elencati nel catalogo, sono
rifiutati per nuovi utenti. I successori indicati dall'API rispondono sul prompt
reale entro 600 token: Flash Lite 3.5 per JSON (~1.5 s), Flash 3.8 per consigli
(~2 s nella sonda). Nessun fornitore a pagamento usato. Gli undici fornitori
gia' supportati bastano per questa verifica; non aggiunti adapter LLM inutili.

## Ripetere le prove

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe scripts\check_providers.py
# Server reale gia' acceso sulla 8099:
.\.venv\Scripts\python.exe scripts\verify_live.py --output data\logs\verification\parse-live.json
# Server spento sulla 8099, dopo la costruzione dello ZIP:
.\.venv\Scripts\python.exe scripts\verify_bundle.py --output data\logs\verification\bundle.json
```

I report grezzi restano nel workspace locale ignorato da Git. Prezzi e corse
sono osservazioni della data di verifica, non garanzie per ricerche future.
