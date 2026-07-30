"""L'avvio senza finestra: la scelta della porta.

Con l'icona nell'area di notifica non c'e' piu' un terminale dove leggere
"address already in use": se la scelta della porta sbaglia, il programma
sembra semplicemente non partire. Questi test tengono ferma quella logica.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import launch_tray  # noqa: E402


def _porta_occupata() -> tuple[socket.socket, int]:
    """Una porta con qualcuno davvero in ascolto, scelta dal sistema."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    return listener, listener.getsockname()[1]


def test_una_porta_in_ascolto_non_e_libera() -> None:
    listener, port = _porta_occupata()
    try:
        assert launch_tray.is_free(port) is False
    finally:
        listener.close()
    # Chiuso il listener la porta torna disponibile: senza questo, `pick_port`
    # scivolerebbe in avanti a ogni riavvio e l'indirizzo cambierebbe sempre.
    assert launch_tray.is_free(port) is True


def test_pick_port_scavalca_chi_e_gia_in_ascolto() -> None:
    listener, port = _porta_occupata()
    try:
        scelta = launch_tray.pick_port(port, span=5)
        assert scelta != port
        assert launch_tray.is_free(scelta)
    finally:
        listener.close()


def test_pick_port_resta_sulla_prima_se_e_libera() -> None:
    listener, port = _porta_occupata()
    listener.close()
    assert launch_tray.pick_port(port, span=5) == port


def test_nessun_trip_finder_su_una_porta_spenta() -> None:
    """La guardia di istanza singola non deve confondere "chiuso" con "e' lui"."""
    listener, port = _porta_occupata()
    listener.close()
    assert launch_tray.trip_finder_at(port, timeout=0.5) is False
