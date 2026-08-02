"""La guardia sui dataset geografici, condivisa dai test che ne dipendono.

I tre dataset (`stations.csv`, `airports.csv` e `cities15000.txt`, circa 37 MB)
non stanno nel repository: si scaricano con `scripts/fetch_datasets.py`. La
maggior parte della suite non li tocca, ma quattro punti li richiedono per
forza:

  - il resolver, che dei nomi di localita' non sa niente senza di loro;
  - il parser di FlixBus, perche' FlixBus non pubblica le coordinate delle sue
    fermate e vanno recuperate dal dataset Trainline incrociando il `legacy_id`;
  - il confronto dell'orario FAL col manifesto, che parte dai nodi dell'indice;
  - le citta' del mondo e i loro fusi orari, che vengono dal gazetteer GeoNames.

Chi clona il repository e lancia `pytest` senza aver scaricato niente deve
vedere quei test **saltati**, con scritto come rimediare, non un
`FileNotFoundError` che sembra un bug del codice.
"""

from __future__ import annotations

import pytest

from app.config import DATA_DIR

DATASETS = (
    DATA_DIR / "stations.csv",
    DATA_DIR / "airports.csv",
    # Il gazetteer mondiale: senza, «Tokyo» non si trova e gli aeroporti fuori
    # Europa non hanno fuso. I test che lo verificano devono saltare, non
    # fallire, su un clone in cui nessuno ha scaricato niente.
    DATA_DIR / "cities15000.txt",
)

#: Segna un test che senza i dataset non puo' girare.
needs_datasets = pytest.mark.skipif(
    not all(path.exists() for path in DATASETS),
    reason="dataset geografici non scaricati (scripts/fetch_datasets.py)",
)
