"""Copia una cartella sopra l'altra, senza toccare quello che e' dell'utente.

Sta in un modulo suo, senza effetti all'import, per due motivi: lo importa
`scripts/updater.py`, che gira come programma a parte, e cosi' si puo' provare
senza costruire un eseguibile.

Le regole scritte qui vengono da un aggiornamento fallito davvero, il
2026-08-01: `PermissionError [WinError 32] Il file e' utilizzato da un altro
processo`. Su Windows i file restano bloccati per qualche secondo dopo che il
processo che li teneva e' uscito, e l'antivirus ne blocca altri mentre scansiona
un archivio appena scompattato. Un aggiornatore che si arrende al primo file
lascia l'installazione a meta', che e' peggio di non aver aggiornato.
"""

from __future__ import annotations

import shutil
import json
import os
import secrets
import sys
import tempfile
from contextlib import contextmanager
import time
from pathlib import Path
from app.install_layout import DATASETS, LAYOUT_MARKER, is_organized, validate_layout

#: Roba dell'utente, mai sovrascritta: il database, il profilo, le chiavi, i
#: log, i profili del browser. Il confronto e' sulla prima parte del percorso,
#: quindi `data/qualunque/cosa` e' protetto perche' lo e' `data`.
DA_NON_TOCCARE = ("data", "state", "vendor", ".env", ".env.local", ".browser-profiles")

#: Quanto riprovare prima di arrendersi su un file bloccato. In tutto una
#: trentina di secondi: Defender su un archivio appena scompattato puo'
#: tenerselo dieci o venti secondi, e arrendersi prima vuol dire fallire per
#: una scansione.
ATTESE = (1.0, 2.0, 4.0, 8.0, 16.0)


def _protetto(relativo: Path, arrivo: Path) -> bool:
    """Vero se questo file e' dell'utente e non va scritto.

    Dentro `data/` c'e' roba di due proprietari diversi: il database, le chiavi,
    i cookie e i log sono suoi; i dataset geografici sono nostri e viaggiano
    nello zip. Proteggere la cartella per intero proteggeva anche **da noi**: la
    0.5.1 metteva il gazetteer mondiale nel pacchetto, ma chi aggiornava non lo
    riceveva mai, e su un'installazione aggiornata alla 0.6.0 «Londra»
    rispondeva ancora Ondara, in Spagna. La correzione valeva solo per chi
    installava da zero.

    La regola: dentro `data/` si scrive **solo quello che ancora non c'e'**. Un
    file nuovo arriva, uno esistente non viene toccato — e siccome il pacchetto
    non contiene nessun file dell'utente, non c'e' modo che questo ne
    sovrascriva uno. Per rinfrescare un dataset gia' presente c'e'
    `scripts/fetch_datasets.py`, che e' un gesto voluto."""
    parti = relativo.parts
    if not parti or parti[0] not in DA_NON_TOCCARE:
        return False
    if parti[0] == "data" and len(parti) == 2 and parti[1] in DATASETS and not arrivo.exists():
        return False
    return True


def _e_questo_programma(destinazione: Path) -> bool:
    """Vero se il file di arrivo e' l'eseguibile che sta girando adesso.

    Cintura e bretelle: chi lancia l'aggiornatore ne mette una copia nel
    temporaneo proprio perche' quello installato resti libero, ma se quella
    copia non riesce non si deve comunque provare a riscrivere sé stessi
    mentre si e' in esecuzione — che e' il primo modo in cui questo
    aggiornamento e' fallito."""
    try:
        return destinazione.resolve() == Path(sys.executable).resolve()
    except OSError:
        return False


def _copia_insistendo(origine: Path, destinazione: Path) -> None:
    for attesa in ATTESE:
        try:
            shutil.copy2(origine, destinazione)
            return
        except PermissionError:
            time.sleep(attesa)
    try:
        shutil.copy2(origine, destinazione)
    except PermissionError as exc:
        # Il nome del file che e' rimasto bloccato vale piu' del messaggio
        # generico: dice se e' l'eseguibile, una libreria o roba dell'antivirus.
        raise PermissionError(
            exc.errno,
            f"{exc.strerror} (ancora bloccato dopo {len(ATTESE)} tentativi): {destinazione}",
        ) from exc


def _mappings(origine: Path, destinazione: Path):
    validate_layout(origine)
    validate_layout(destinazione)
    source_organized = is_organized(origine)
    target_organized = is_organized(destinazione)
    if not destinazione.exists() or not any(destinazione.iterdir()):
        target_organized = source_organized
    for file in origine.rglob("*"):
        if not file.is_file():
            continue
        if file.is_symlink() or not file.resolve().is_relative_to(origine.resolve()):
            raise ValueError("Il pacchetto contiene collegamenti simbolici")
        relativo = file.relative_to(origine)
        if source_organized:
            if relativo.parts[0] == "app":
                runtime_relative = Path(*relativo.parts[1:])
                if runtime_relative.parts[0] == "vendor":
                    target = destinazione / relativo if target_organized else destinazione / runtime_relative
                    if target.exists():
                        continue
                elif _protetto(runtime_relative, destinazione / runtime_relative):
                    continue
                if not target_organized and runtime_relative.name == LAYOUT_MARKER:
                    continue
                if not target_organized and runtime_relative.parts[:2] == ("resources", "data"):
                    if len(runtime_relative.parts) != 3 or runtime_relative.name not in DATASETS:
                        continue
                    target = destinazione / "data" / runtime_relative.name
                    if target.exists():
                        continue
                else:
                    target = destinazione / relativo if target_organized else destinazione / runtime_relative
            else:
                if relativo.parts[0] in DA_NON_TOCCARE or (not target_organized and relativo.name == "TripFinder.exe"):
                    continue
                target = destinazione / relativo
        elif relativo.parts[0] == "data":
            if len(relativo.parts) != 2 or relativo.name not in DATASETS:
                continue
            target = destinazione / ("state" if target_organized else "data") / relativo.name
            if target.exists():
                continue
        else:
            if relativo.parts[0] in DA_NON_TOCCARE:
                if relativo.parts[0] != "vendor":
                    continue
            target = destinazione / "app" / relativo if target_organized else destinazione / relativo
            if relativo.parts[0] == "vendor" and target.exists():
                continue
            if target_organized and relativo.name == "LEGGIMI.txt":
                target = destinazione / relativo
        if not target.resolve().is_relative_to(destinazione.resolve()):
            raise ValueError("Destinazione aggiornamento fuori installazione")
        if not _e_questo_programma(target):
            yield file, target


def sincronizza(*, origine: Path, destinazione: Path) -> int:
    """Copia tutto quello che sta in `origine` dentro `destinazione`.

    Restituisce quanti file ha scritto. Non cancella niente: quello che c'era
    e non c'e' piu' resta, ed e' la scelta prudente — un file di troppo non ha
    mai rotto niente, uno mancante si'."""
    if not origine.exists():
        raise FileNotFoundError(f"non trovo la cartella da installare: {origine}")
    mappings = list(_mappings(origine, destinazione))
    destinazione.mkdir(parents=True, exist_ok=True)
    backups: dict[Path, Path | None] = {}
    written: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="tripfinder-rollback-", dir=destinazione.parent) as temporary:
        for index, (_, target) in enumerate(mappings):
            backup = Path(temporary) / str(index) if target.exists() else None
            if backup is not None:
                shutil.copy2(target, backup)
            backups[target] = backup
        try:
            for source, target in mappings:
                target.parent.mkdir(parents=True, exist_ok=True)
                written.append(target)
                _copia_insistendo(source, target)
        except Exception:
            for target in reversed(written):
                backup = backups[target]
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    os.replace(backup, target)
            raise
    return len(written)


@contextmanager
def _update_guard(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".guard").open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # access denied is not proof of exit
        try:
            code = wintypes.DWORD()
            kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def claim_update_lock(path: Path, version: str, ttl_seconds: float = 180.0) -> str:
    """Atomic claim: a live owner's lock is never stolen based on its age."""
    path.parent.mkdir(parents=True, exist_ok=True)
    owner = secrets.token_hex(24)
    payload = {"owner": owner, "pid": os.getpid(), "version": version, "created": time.time()}
    with _update_guard(path):
        if path.exists():
            try:
                raw = path.read_text(encoding="utf-8")
                record = json.loads(raw)
                pid = int(record.get("pid", 0)) if isinstance(record, dict) else int(raw.strip())
                created = float(record.get("created", path.stat().st_mtime)) if isinstance(record, dict) else path.stat().st_mtime
            except (ValueError, TypeError, OSError):
                raise RuntimeError("Lock aggiornamento non leggibile: verifica il suo esito prima di riprovare") from None
            if time.time() - created <= ttl_seconds or _pid_alive(pid):
                raise RuntimeError("Un aggiornamento è già in corso; verifica il suo esito prima di riprovare")
            path.unlink()
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    return owner


def release_update_lock(path: Path, owner: str | None = None, *, legacy_parent_pid: int | None = None) -> bool:
    with _update_guard(path):
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
        try:
            record = json.loads(raw)
        except ValueError:
            record = None
        matches = isinstance(record, dict) and owner is not None and record.get("owner") == owner
        matches = matches or (legacy_parent_pid is not None and raw.strip() == str(legacy_parent_pid))
        if matches:
            path.unlink(missing_ok=True)
            return True
    return False


def transfer_update_lock(path: Path, owner: str, pid: int) -> None:
    with _update_guard(path):
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("owner") != owner:
            raise RuntimeError("Proprietario del lock aggiornamento cambiato")
        record["pid"] = pid
        temporary = path.with_name(path.name + ".transfer-" + owner)
        try:
            temporary.write_text(json.dumps(record), encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
