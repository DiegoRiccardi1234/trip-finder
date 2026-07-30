"""Preparazione comune agli script di diagnostica.

Due cose che vanno fatte prima di qualunque import del progetto:

  - mettere la radice del repository sul path, cosi' `app` e' importabile
    senza installare il pacchetto;
  - forzare UTF-8 sull'uscita, perche' la console di Windows parte in cp1252 e
    va in errore sul primo nome proprio accentato ("Bari Karol Wojtyla" ha una
    l con barra, e senza questa riga lo script muore li').
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover - stream ridiretto
        pass
