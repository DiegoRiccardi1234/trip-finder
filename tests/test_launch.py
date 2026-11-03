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


def test_packaged_migration_precedes_server_logging_and_state_seed(tmp_path, monkeypatch):
    import app.install_migration
    from app.install_layout import LAYOUT_MARKER
    source = tmp_path / "portable"
    (source / "app").mkdir(parents=True)
    (source / "app" / LAYOUT_MARKER).write_text('{"version":1}')
    target = tmp_path / "legacy"
    calls, messages = [], []
    def migrate(**kwargs):
        calls.append(kwargs)
        return kwargs["backup"]
    def unexpected(*args, **kwargs):
        raise AssertionError("migration must not start logging/server or seed source state")
    monkeypatch.setattr(sys, "argv", ["TripFinder.exe", "--migrate-from", str(target)])
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(source / "app" / "TripFinder.exe"))
    monkeypatch.setattr(app.install_migration, "migrate_installation", migrate)
    monkeypatch.setattr(launch_tray, "_setup_logging", unexpected)
    monkeypatch.setattr(launch_tray, "seed_datasets", unexpected)
    monkeypatch.setattr(launch_tray, "_migration_message", lambda text, **kwargs: messages.append((text, kwargs)))
    assert launch_tray.main() == 0
    assert calls[0]["source"] == source
    assert calls[0]["target"] == target
    assert calls[0]["backup"].parent == target.parent
    assert calls[0]["backup"].name.startswith("legacy.backup-")
    assert "Backup originale" in messages[0][0]


def test_packaged_migration_failure_has_visible_result_and_explicit_backup(tmp_path, monkeypatch):
    import app.install_migration
    target, backup = tmp_path / "legacy", tmp_path / "backup"
    calls, messages = [], []
    def migrate(**kwargs):
        calls.append(kwargs)
        raise RuntimeError("Chiudi Trip Finder")
    monkeypatch.setattr(sys, "argv", ["TripFinder.exe", "--migrate-from", str(target), "--migration-backup", str(backup)])
    monkeypatch.setattr(app.install_migration, "migrate_installation", migrate)
    monkeypatch.setattr(launch_tray, "_migration_message", lambda text, **kwargs: messages.append((text, kwargs)))
    assert launch_tray.main() == 1
    assert calls[0]["backup"] == backup
    assert messages[0][1] == {"failed": True}
    assert "Chiudi Trip Finder" in messages[0][0]


def test_mcp_dispatch_keeps_exact_stdio_without_migration_dialog(monkeypatch):
    from types import SimpleNamespace
    calls = []
    monkeypatch.setitem(sys.modules, "app.mcp_server", SimpleNamespace(main=lambda argv: calls.append(argv)))
    monkeypatch.setattr(sys, "argv", ["TripFinder.exe", "--mcp-stdio", "--token-file", "token path.txt"])
    monkeypatch.setattr(launch_tray, "_migration_message", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("MCP must not display dialogs")))
    assert launch_tray.main() == 0
    assert calls == [["--stdio", "--token-file", "token path.txt"]]


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
