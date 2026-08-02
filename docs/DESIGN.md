# Trip Finder — sistema visivo

Ricavato dal codice reale (`app/static/style.css`). Chi tocca un valore qui lo
tocca lì, e viceversa: due elenchi che dicono la stessa cosa divergono sempre.

## Tema

Chiaro **e** scuro, entrambi di prima classe. Il chiaro è il caso comune (si
pianifica un viaggio di giorno, a schermo acceso, spesso in una scheda fra le
altre); lo scuro serve a chi tiene la scheda aperta la sera. La scelta manuale
scavalca il sistema e vive in `localStorage`, applicata **prima** del CSS per non
mostrare un lampo del tema sbagliato.

I due blocchi di variabili sono duplicati apposta: senza preprocessore le
proprietà custom non si riusano fra selettori. Chi cambia una riga la cambia in
entrambi.

## Colore

Strategia **restrained**: neutri tinti verso l'accento, un solo accento sotto il
10% della superficie. È un prodotto in cui si legge una classifica, non una
pagina che deve farsi guardare.

| Ruolo | Chiaro | Scuro | Dove |
|---|---|---|---|
| `--bg` | `#f7f7f5` | `#14171a` | fondo pagina |
| `--panel` | `#ffffff` | `#1c2024` | schede, pannelli, linguetta attiva |
| `--ink` | `#1b1d1e` | `#e8eaec` | testo |
| `--muted` | `#6b7076` | `#9aa2a9` | etichette, note, stati secondari |
| `--line` | `#dcdedf` | `#2e343a` | bordi e separatori |
| `--accent` | `#12604d` | `#5fc9a6` | azione primaria, selezione, stato attivo |
| `--accent-soft` | `#e4f0ec` | `#17322a` | fondo del consiglio, evidenziazioni |
| `--on-accent` | `#ffffff` | `#0d1512` | testo sopra l'accento — **non è sempre bianco** |
| `--warn` / `--warn-soft` | `#8a5300` / `#fdf0dc` | `#e0a95a` / `#33280f` | conferme, vincoli rilassati |
| `--bad` / `--bad-soft` | `#96261f` / `#fbe6e4` | `#e88b83` / `#3a1d1a` | errori |
| `--ok` | `#1d6b3f` | `#6fd08c` | sconti applicati, chiave configurata |

Il verde non è decorativo: è l'unico colore pieno della pagina e marca sempre
la stessa cosa — «questa è l'azione», «questo è selezionato», «questo è attivo».

## Tipografia

Una famiglia sola, di sistema: `system-ui, -apple-system, "Segoe UI", Roboto,
sans-serif`. Nessun font remoto — l'applicazione gira anche offline, dentro un
bundle Windows.

Base 15px/1.5. Scala stretta, rapporto ~1.15: 12.5 (note) · 13 (etichette) ·
13.5 · 15 (corpo) · 17 (titoli pannello) · 19 (orari e prezzi). Gerarchia da
peso e colore prima che da dimensione.

`--mono` (`ui-monospace, "SF Mono", "Cascadia Mono", Consolas`) per **i dati che
si confrontano incolonnati**: orari, importi, nomi degli operatori, slug dei
modelli. Mai per il testo discorsivo.

`font-variant-numeric: tabular-nums` su ogni prezzo e durata: senza, le cifre
ballano fra una riga e l'altra e la colonna non si legge più.

## Forma e spazio

- `--radius: 10px` per pannelli e schede; 7px per i campi; il resto non ha raggi.
- Bordo `1px solid var(--line)`. Nessuna ombra: la separazione la fa il bordo.
- Spaziatura a passi di 4: 4 · 6 · 8 · 10 · 12 · 14 · 18. Il ritmo cambia fra
  denso (righe di classifica) e arioso (impostazioni).
- Corpo del testo entro 76ch. Le tabelle e le classifiche possono correre di più.

## Componenti

- **Linguette**: `.tabs` + `.tab`, la attiva sul colore del pannello con il
  bordo inferiore che scompare dentro la vista. Le viste sono `.view`, ne vive
  una sola (`display: none` / `block`), e lo stato sta nell'hash.
- **Bottoni**: pieno accento per l'azione primaria, `.link` (testo accento senza
  bordo) per le azioni secondarie e distruttive.
- **Chip** delle fermate: bordo sottile, selezionabili; selezionato = accento.
- **Consiglio** (`.advice`): riquadro `--accent-soft` con bordo accento quando
  c'è; `.advice--muto` — trasparente, bordo `--line`, testo `--muted` — quando
  invece si sta dicendo **perché non c'è**. Un'assenza non deve gridare più
  forte del contenuto. Quando manca porta anche un «Riprova» (`.link`): il
  colore più smorto è un segnale, non un'informazione, e da solo non dice cosa
  fare.
- **Numero della scheda** (`.ref`): un quadratino in monospazio nell'angolo alto
  a sinistra della scheda, in grigio, accento quando la scheda è la migliore.
  Non è decorazione: è il nome con cui il consiglio la chiama. Lo spazio se lo
  prende davvero (`.itinerary[data-ref] .tratta { padding-left }`) invece di
  stare sopra gli orari. Compare a ricerca finita — durante, la classifica si
  rimescola e un numero ballerino non è un riferimento.
- **Riferimento nel testo** (`.ref-chip`): lo stesso segno, dentro il consiglio,
  con il bordo accento perché si può premere. Porta alla scheda e la fa notare
  per due secondi (`.itinerary.puntata`). È un `<button>` e non un link: non
  porta altrove, porta più in basso.
- **Elenco modelli**: una riga per modello, con un pallino colorato come unico
  segnale di stato (verde vivo, rosso morto, arancio penalizzato) e il resto in
  grigio. Il colore qui è informazione: tre righe colorate accanto non si
  distinguono più. Il modello in uso è in grassetto e marcato «in uso».
- **Schede fornitore**: griglia con i separatori disegnati da `box-shadow` sulle
  schede, non da un fondo colorato dietro la griglia — con quello una riga
  incompleta lasciava una cella vuota grigia, cioè un riquadro che annuncia il
  nulla.
- **Stato vuoto**: mai «nessun risultato» e basta. Si dice cosa è stato provato.

## Movimento

Quasi nessuno, e sempre di stato: 150–200 ms, `ease-out`. Niente sequenze
d'ingresso, niente rimbalzi. L'unica cosa che si muove davvero è la riga di
stato durante la ricerca, perché lì il movimento è informazione.

## Divieti, appresi sul campo

- **Non fidarsi di `[hidden]` per nascondere.** Una regola con `display` lo batte,
  ed è già costato due «funziona» falsi. Se un elemento deve sparire, la regola
  che lo nasconde e quella che lo mostra devono essere la stessa proprietà.
- **Niente riquadri annidati.** Un pannello dentro un pannello non aggiunge
  gerarchia, aggiunge bordi.
- **Nessun numero gigante senza il suo perché.** Il totale del viaggio è grande
  perché lo si confronta, e accanto porta sempre da cosa è fatto.
