# Verifica dati statici — 2 ottobre 2026

## Ricontrollo tessere del 3 novembre 2026

Riletti integralmente i quattro PDF Trenitalia. Le percentuali e i requisiti
restano invariati, ma [Promo Young](https://www.trenitalia.com/content/dam/trenitalia/allegati/info/condizioni-generali-di-trasporto/parte-iii-trasporto-regionale/promo/Promo_YOUNG.pdf)
e [Promo Senior](https://www.trenitalia.com/content/dam/trenitalia/allegati/info/condizioni-generali-di-trasporto/parte-iii-trasporto-regionale/promo/Promo_SENIOR.pdf)
riportano ora validita' del documento dal 20 ottobre 2026 e viaggi fino al
31 dicembre 2027. Il confronto settimanale segnalava correttamente una differenza:
il parser riconosceva soltanto la formulazione dal/al e perdeva la nuova
scadenza fino al. Parser e catalogo includono entrambe le formulazioni e
bloccano la rigenerazione delle promo se le date non sono leggibili. Verde e
Argento sono invariate; rinnovate le relative date di verifica. Le altre
voci conservano i propri timestamp: ESNcard restituisce ancora HTTP 403 e la
pagina Grimaldi non ha completato la risposta. Il dettaglio convenzione
IoStudio continua a dire che la convenzione non e' stata trovata; l'annuncio
MIM citato nel report riguarda Frecce/Intercity e non comprova il 10% sui
regionali. Rimangono riduzioni informative senza percentuali inventate.

## FAL

La [pagina ufficiale degli orari](https://ferrovieappulolucane.it/tratta/orari/) collega il [manifesto in vigore dal 31 agosto 2026](https://ferrovieappulolucane.it/wp-content/uploads/2026/08/ORARIO-TRENI-DA-E-PER-BA-DAL-31-AGO-2026-1.pdf). Il parser riconosce 76 corse e supera i controlli di plausibilita'. Il documento precedente era quello del 22 giugno. Il collegamento Bari 04:14 → Matera 06:50 resta ricostruibile: arrivo ad Altamura 05:26, ripartenza 06:21.

Il [listino del 9 marzo 2026](https://ferrovieappulolucane.it/wp-content/uploads/2026/03/TARIFFE-PUGLIA-E-BASILICATA-09-MAR-2026-1.pdf) e' ancora quello pubblicato: fasce e distanze estratte coincidono con il catalogo precedente. Bari–Matera: 70 km tassabili, 6,20 euro. La provenienza delle tariffe ora riporta l'URL ufficiale.

L'orario generato contiene `valid_from` e `suppressed_window`: il manifesto attuale dice agosto e la finestra e' riferita al 2026. Non si ricicla automaticamente la soppressione nel 2027. Le date anteriori all'entrata in vigore non producono corse del nuovo documento. Il lunedi' di Pasqua e' escluso come festivo. Le informazioni restano statiche e richiedono verifica prima della partenza.

## Catalogo tessere

Tutte le 13 voci sono state esaminate. La data di verifica si aggiorna soltanto quando la fonte e le condizioni sono riscontrabili.

| Voci | Riscontro | Esito |
| --- | --- | --- |
| Promo Young/Senior Trenitalia | PDF delle CGT collegati nelle singole voci, letti integralmente | 20%, 5 aprile–30 novembre 2026; date di verifica aggiornate |
| Carta Verde/Argento Trenitalia | PDF delle CGT collegati nelle singole voci | 10%/15%, ritiro dal 4 aprile 2026 confermato; restano per i possessori |
| Italo Giovani | [Pagina ufficiale attuale](https://www.italotreno.com/it/offerte-treno/italo-giovani) | 14–29 anni, 40–70% sulla Flex; URL corretto e condizioni esplicite |
| Italo Senior | [Pagina ufficiale](https://www.italotreno.com/it/offerte-treno/italo-senior) | 40–60% sulla Flex; la pagina riporta anticipi discordanti, da verificare nell'acquisto |
| ISIC FlixBus | [Condizioni ufficiali ISIC](https://isic.de/en/discounts/germany/flixbus/8154) | 10%, fino al 15 dicembre 2026, esclusi extra e servizi; nessuna cumulabilita' |
| ESNcard FlixBus | [Pagina ESNcard](https://www.esncard.org/flixbus) restituisce 403 | Condizioni non verificabili; timestamp precedente e limite dichiarato |
| Continuita' Sardegna | [FAQ della Regione](https://www.regione.sardegna.it/argomenti/argomenti-speciali/continuita-territoriale-2026/domande-e-risposte-continuita-territoriale) | Tariffe regolate per rotta/categoria, nessun -50% universale; voce informativa con valore zero |
| Residenti isole, traghetti | [Fonte Grimaldi](https://www.grimaldi-lines.com/en/offers-for-ferries/offers-for-residents-and-native-inhabitants-from-sardinia-and-sicily/) indicizzata ma non leggibile integralmente (certificato HTTPS/timeout) | Percentuale generale del 30% non confermata; valore zero e timestamp precedente |
| Interrail Youth | [Fonte ufficiale Interrail](https://www.interrail.eu/en/interrail-passes/deals) | Fino al 25% sul pass, non sul singolo biglietto; valore zero per evitare riduzioni improprie |
| IoStudio | [Annuncio MIM](https://iostudio.pubblica.istruzione.it/web/guest/home/-/asset_publisher/5qsx90af8N5T/content/con-la-tua-carta-dello-studente-iostudio-e-trenitalia-puoi-viaggiare-sulle-frecce-a-prezzi-scontati-) cita 20% su Frecce/IC; [dettaglio convenzione](https://iostudio.pubblica.istruzione.it/web/studenti/dettaglio-convenzione/-/dettaglioconvenzioneview/dettaglio/19/0) non trovato | Il 10% sui regionali non e' comprovato: valore zero, ambito Trenitalia, validita' ancora da verificare |
| X-GO | [Programma ufficiale](https://www.trenitalia.com/it/x-go/il-programma.html) | 150 punti = 3 euro cashback; raccolta fino al 31 dicembre 2026, utilizzo fino al 28 febbraio 2027 |

I valori zero mantengono le agevolazioni nell'elenco senza inventare un risparmio sulle ricerche. Gli sconti Italo restano stime dichiarate: la percentuale pubblicata si riferisce alla Flex, e non dimostra un ulteriore sconto sul prezzo promozionale restituito da una ricerca. ESNcard, IoStudio e tariffe traghetti restano limiti di fonte espliciti, non guasti risolti.

## Verifiche automatiche

`tests/test_fal.py` e `tests/test_tessere.py`: 27 test superati, inclusa la ricostruzione Bari–Matera sui dataset geografici presenti (sola lettura). Nel workspace isolato privo di dataset: 26 superati, un test geografico saltato. I test verificano Pasquetta 2026/2027/2028, finestre del vecchio e nuovo manifesto, assenza di ricorrenza annuale, decorrenza e sconti informativi a zero. Le prove del catalogo versionato ignorano la copia eventualmente scaricata nel workspace dell'utente. `build_tessere.py --check` termina con codice zero sulle quattro fonti PDF; i limiti delle voci curate restano quelli della tabella.

Nessuna chiave o database personale e' stato letto o modificato per queste verifiche. Nessun contatto con Transitous.

## Aggiornamento FAL - 3 novembre 2026

La [pagina ufficiale degli orari](https://ferrovieappulolucane.it/tratta/orari/) collega ora il [manifesto in vigore dal 12 ottobre 2026](https://ferrovieappulolucane.it/wp-content/uploads/2026/10/ORARIO-TRENI-DA-E-PER-BA-DAL-12-OTT-2026-1.pdf). Entrambe le pagine sono state estratte e controllate anche come immagini. Il dataset e' stato rigenerato con lo script del progetto: 76 corse, 74 identiche alle precedenti. La corsa 1 non riporta piu' la fermata di Venusio; la corsa 6 anticipa di cinque minuti (Matera Centrale 05:40, Altamura 06:06). Bari 04:14 - Matera Centrale 06:50 rimane ricostruibile su una data feriale successiva alla nuova decorrenza.

Il nuovo manifesto segnala il bus sostitutivo tra Matera Sud e Altamura e viceversa e ammette sui bus soltanto le biciclette pieghevoli. Le note affiancate della legenda vengono conservate separatamente; anche la nota sui collegamenti Gravina-Potenza con bus sostitutivo rimane esplicita. Le gambe FAL mostrano queste condizioni come avvertenze del manifesto: non attribuiscono il mezzo bus a tutte le corse. La sospensione indicata per agosto resta datata al 2026 e non viene ripetuta automaticamente nel 2027.

Il [listino ufficiale del 9 marzo 2026](https://ferrovieappulolucane.it/wp-content/uploads/2026/03/TARIFFE-PUGLIA-E-BASILICATA-09-MAR-2026-1.pdf) e' ancora collegato dalla pagina FAL. Riscaricato e interpretato: 15 fasce e 10 coppie di stazioni riconosciute, identiche al dataset precedente; Bari-Matera resta 70 km tassabili e 6,20 euro. Il confronto non e' basato soltanto sul nome del file.

Verifica mirata: 105 test superati fra FAL, fusi orari e parser. I nuovi casi proteggono la decorrenza del 12 ottobre, l'anticipo della corsa 6, le condizioni bus e la separazione delle note affiancate. PDF e immagini di controllo restano fra i file locali ignorati, fuori da Git e dai bundle. Nessuna nuova inferenza o accesso a dati personali per questo aggiornamento.
