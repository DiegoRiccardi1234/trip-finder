"""Exercise the compiled migration command with synthetic, isolated state.

The portable and legacy runtimes are copied into TEMP. No installed application
or personal state is changed. Only the migration dialog owned by the launched
process tree may be dismissed; all other windows are ignored.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from contextlib import closing
from ctypes import wintypes
from pathlib import Path


def _descendants(root_pid: int) -> set[int]:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]
    kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise OSError("Process inventory unavailable; no dialog will be dismissed")
    parents: dict[int, int] = {}
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(entry)
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    owned = {root_pid}
    while True:
        expanded = owned | {pid for pid, parent in parents.items() if parent in owned}
        if expanded == owned:
            return owned
        owned = expanded


def _dismiss_owned_migration_dialog(root_pid: int) -> list[dict]:
    owned = _descendants(root_pid)
    user = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    user.EnumChildWindows.argtypes = (wintypes.HWND, callback_type, wintypes.LPARAM)
    user.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    user.GetDlgItem.argtypes = (wintypes.HWND, ctypes.c_int)
    user.GetDlgItem.restype = wintypes.HWND
    user.GetDlgCtrlID.argtypes = (wintypes.HWND,)
    user.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    dismissed: list[dict] = []

    @callback_type
    def inspect(window, unused):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if pid.value not in owned:
            return True
        title, class_name = ctypes.create_unicode_buffer(300), ctypes.create_unicode_buffer(100)
        user.GetWindowTextW(window, title, len(title))
        user.GetClassNameW(window, class_name, len(class_name))
        if title.value != "Migrazione Trip Finder" or class_name.value != "#32770":
            return True
        text: list[str] = []
        buttons: list[tuple[int, int]] = []
        @callback_type
        def child(control, unused_child):
            caption, control_class = ctypes.create_unicode_buffer(4000), ctypes.create_unicode_buffer(100)
            user.GetClassNameW(control, control_class, len(control_class))
            if control_class.value.lower() == "static":
                user.GetWindowTextW(control, caption, len(caption))
                if caption.value:
                    text.append(caption.value)
            elif control_class.value.lower() == "button":
                user.GetWindowTextW(control, caption, len(caption))
                if caption.value.replace("&", "").strip().upper() == "OK":
                    buttons.append((control, user.GetDlgCtrlID(control)))
            return True
        user.EnumChildWindows(window, child, 0)
        if len(buttons) != 1:
            return True
        button, command = buttons[0]
        # Recheck ownership immediately before the sole allowed interaction.
        if pid.value not in _descendants(root_pid):
            return True
        # BM_CLICK can be ignored when the dialog is not foreground. Send
        # the same IDOK command to this verified modal window without moving
        # focus or interacting with any other application.
        user.PostMessageW(window, 0x0111, command, button)  # WM_COMMAND / verified OK
        dismissed.append({"title": title.value, "pid": pid.value, "text": "\n".join(text)})
        return True
    user.EnumWindows(inspect, 0)
    return dismissed


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(portable: Path, legacy_runtime: Path, output: Path) -> dict:
    if os.name != "nt":
        raise RuntimeError("Compiled migration verification requires Windows")
    portable, legacy_runtime = portable.resolve(), legacy_runtime.resolve()
    if not (portable / "app" / ".tripfinder-layout.json").is_file():
        raise ValueError("Expected an organized portable bundle")
    if not (legacy_runtime / "TripFinder.exe").is_file() or not (legacy_runtime / "_internal").is_dir():
        raise ValueError("Expected an extracted historical runtime")
    workspace = Path(tempfile.mkdtemp(prefix="tripfinder-compiled-migration-"))
    source, target, backup = workspace / "portable", workspace / "legacy", workspace / "backup"
    shutil.copytree(portable, source, ignore=lambda directory, names: {"state"} & set(names) if Path(directory).resolve() == portable else set())
    (source / "state").mkdir()
    (source / "state" / "other-installation.txt").write_text("must not merge", encoding="utf-8")
    target.mkdir()
    for entry in legacy_runtime.iterdir():
        if entry.is_file() and entry.suffix.lower() == ".exe":
            shutil.copy2(entry, target / entry.name)
    shutil.copytree(legacy_runtime / "_internal", target / "_internal")
    (target / "data").mkdir()
    (target / "vendor").mkdir()
    markers = {".env": "ALLOW_PAID_PROVIDERS=false\n", "data/local_secrets.json": "{}\n",
               "data/profile-marker.txt": "legacy profile\n", "vendor/custom.json": '{"synthetic":"preserve"}\n'}
    for name, value in markers.items():
        (target / name).write_text(value, encoding="utf-8")
    with closing(sqlite3.connect(target / "data" / "trip_finder.db")) as database:
        database.execute("CREATE TABLE migration_marker(value TEXT)")
        database.executemany("INSERT INTO migration_marker VALUES (?)", [("first",), ("second",)])
        database.commit()
    original_hashes = {name: _hash(target / name) for name in markers}
    old_executable = _hash(target / "TripFinder.exe")
    env = {key: value for key, value in os.environ.items() if not key.endswith("_API_KEY") and key not in {"TRIPFINDER_WORKSPACE", "LLM_PROVIDER", "LLM_BASE_URL", "TRIPFINDER_PORT"}}
    report = {"workspace": str(workspace), "portable": str(portable), "historical_runtime": str(legacy_runtime),
              "dialogs_dismissed": [], "started_server": False, "inferences": 0}
    process = subprocess.Popen([str(source / "TripFinder.exe"), "--migrate-from", str(target), "--migration-backup", str(backup)],
                               cwd=workspace, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        deadline = time.monotonic() + 180
        while process.poll() is None and time.monotonic() < deadline:
            report["dialogs_dismissed"].extend(_dismiss_owned_migration_dialog(process.pid))
            time.sleep(0.2)
        if process.poll() is None:
            raise TimeoutError("Compiled migration did not finish; no unrelated dialog was touched")
        report["exit_code"] = process.returncode
        assert process.returncode == 0, "Compiled migration failed; inspect its captured migration dialog"
        assert report["dialogs_dismissed"], "No owned migration result dialog was observed"
        assert {entry.name for entry in target.iterdir()} == {"TripFinder.exe", "LEGGIMI.txt", "app", "state"}
        assert _hash(target / "TripFinder.exe") == _hash(source / "TripFinder.exe")
        assert _hash(backup / "TripFinder.exe") == old_executable
        destinations = {".env": "state/.env", "data/local_secrets.json": "state/local_secrets.json",
                        "data/profile-marker.txt": "state/profile-marker.txt", "vendor/custom.json": "app/vendor/custom.json"}
        for original, destination in destinations.items():
            assert _hash(target / destination) == original_hashes[original]
            assert _hash(backup / original) == original_hashes[original]
        with closing(sqlite3.connect(target / "state" / "trip_finder.db")) as database:
            assert database.execute("SELECT value FROM migration_marker ORDER BY rowid").fetchall() == [("first",), ("second",)]
            assert database.execute("PRAGMA quick_check").fetchone() == ("ok",)
        with closing(sqlite3.connect(backup / "data" / "trip_finder.db")) as database:
            assert database.execute("SELECT COUNT(*) FROM migration_marker").fetchone() == (2,)
        assert not (target / "state" / "other-installation.txt").exists()
        assert not (target / "state" / "runtime.json").exists()
        assert not (source / "state" / "runtime.json").exists()
        report.update(root_layout=True, database_preserved=True, vendor_preserved=True,
                      secrets_preserved=True, backup_preserved=True, source_state_not_merged=True)
    finally:
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            process.wait(timeout=15)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--portable-dir", type=Path, required=True)
    parser.add_argument("--legacy-runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.portable_dir, args.legacy_runtime, args.output), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
