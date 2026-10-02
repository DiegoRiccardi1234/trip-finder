# Geografia: come un nome diventa fermate, e quale città ci finisce sopra

Materiale di lavoro sul resolver e sul gazetteer. Sta fuori da `APPUNTI.md`
perché è un argomento a sé e non serve tutte le mattine; si apre quando si tocca
`app/geo/`.

## Quale città finisce su una fermata

`_nearest_city` sceglie con `weight * 10 - distanza` entro 25 km: «non la più
vicina, la più importante fra le vicine». Prima di dare la colpa alla formula,
**controllare che la città sia in gara**: il dataset è `cities15000`, quindi un
comune sotto i quindicimila abitanti non è fra i candidati e nessun punteggio lo
può far vincere (Canelli, ~10.500, si dichiarava Acqui Terme). Unica strada:
`app/geo/overrides.json`.

Il peso in un override è metà della correzione. Le isole hanno 3.0 perché non
hanno concorrenti entro 25 km; un comune di collina con 3.0 **ruberebbe la città
accanto**. Tenerlo vicino a quello che il gazetteer calcolerebbe,
`log10(abitanti) - 3`. Il metodo per deciderlo: stampare per una dozzina di
casi tutti i candidati con distanza, peso e punteggio, e guardare **cosa viene
scartato**. Casi da tenere nel campione: uno denunciato, uno che la regola
*protegge* (Torino Porta Nuova, che senza il peso diventerebbe San Salvario) e
uno lontano che deve continuare a funzionare (un codice IATA).

Difetto storico corretto il 2026-10-02: le voci di trasporto Trainline entravano
con peso **fisso 2.0**, pur senza dichiarare la popolazione. MXP finiva così
attribuito a «Bellinzago Novarese», un paese a 8 km, invece di Gallarate a 7,3.
Ora il peso Trainline è **neutro 1.0**: GeoNames integra il peso demografico
quando coincidono nome normalizzato e paese. Restano i pesi espliciti degli
override e il bonus di trasporto per risolvere i nomi omonimi.

Con i dataset locali del 2026-10-02 MXP era già Gallarate prima della correzione
(5,93 km, punteggio 11,09, contro Bellinzago a 9,60 km, punteggio 10,40).
Il test sintetico riproduce quindi le distanze storiche senza dipendere dai
cataloghi aggiornati. I test reali continuano a proteggere Canelli/Acqui,
Torino/quartieri, codici IATA e omonime. Non cambia la formula di attribuzione.

L'indice geografico e il resolver sono cache in memoria: dopo una modifica
riavviare il processo. Il bundle include già i tre cataloghi e gli override;
questa correzione non richiede nuovi dati.

Da tenere distinto: il ramo IATA cerca una città vicina alle coordinate senza
usare `airport.city`. Nel campione attuale HND viene attribuito a Kawasaki,
JFK a Queens e FCO ad Acilia-Castel Fusano-Ostia Antica. Sono risultati della
regola geografica; scegliere invece la città commerciale dello scalo richiede
una decisione separata. La correzione del peso Trainline non cambia questi casi.

## Un gazetteer più grande non è solo più copertura

Col gazetteer mondiale sono comparsi tre difetti che quello europeo nascondeva:
**i quartieri diventano città** (da cui la formula qui sopra); **le omonime
scavalcano casa** («Madrid» in Colombia); **gli alias battono i nomi** («Napoli»
rispondeva «Naples»). Gli alias sono 350.000: nel corpus del matching per
somiglianza non entrano, e tagliarne un numero fisso non funziona — «Parigi» è
il ventiduesimo nome di Paris, «Londra» il venticinquesimo.

Vale anche un piano più su, sulla scelta delle **fermate**: un punteggio che non
pesa la distanza prende sempre lo scalo più grande, non il più vicino — Milano
scartava Linate a 7 km. Stesso metodo, stessa domanda: cosa viene scartato.

## Verifiche rapide

- `.\.venv\Scripts\python.exe scripts\try_resolve.py <località>` — in quali
  fermate si traduce un nome. Offline.
- I test dell'attribuzione stanno in `tests/test_geo.py`: Canelli deve restare
  Canelli **e** Acqui Terme restare Acqui Terme, che è la coppia che accorge se
  un override ha rubato la vicina.
