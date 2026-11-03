from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zipfile import ZipFile

import pytest
from PIL import Image

from app import config, install_layout, update_sync
from app.install_migration import migrate_installation, restore_installation
from scripts.build_windows_icon import SIZES, build
from scripts.package_windows import organize_bundle, write_archive


def organized(path: Path) -> Path:
    (path / "app").mkdir(parents=True)
    (path / "app" / install_layout.LAYOUT_MARKER).write_text('{"version":1}', encoding="utf-8")
    (path / "TripFinder.exe").write_bytes(b"launcher")
    (path / "app" / "TripFinder.exe").write_bytes(b"runtime")
    (path / "LEGGIMI.txt").write_text("guida", encoding="utf-8")
    (path / "state").mkdir()
    return path


def test_layout_marker_distinguishes_sources_and_installations(tmp_path):
    (tmp_path / "app").mkdir()
    assert install_layout.state_dir(tmp_path) == tmp_path / "data"
    marker = tmp_path / "app" / install_layout.LAYOUT_MARKER
    marker.write_text('{"version":1}', encoding="utf-8")
    assert install_layout.state_dir(tmp_path) == tmp_path / "state"
    assert install_layout.installation_root(tmp_path / "app" / "TripFinder.exe") == tmp_path
    marker.write_text('{"version":true}', encoding="utf-8")
    with pytest.raises(ValueError, match="Layout"):
        install_layout.state_dir(tmp_path)


def test_only_one_process_can_own_the_state(tmp_path):
    with install_layout.state_ownership(tmp_path):
        with pytest.raises(RuntimeError, match="già aperta"):
            with install_layout.state_ownership(tmp_path):
                pytest.fail("second owner")
    with install_layout.state_ownership(tmp_path):
        pass


def test_flat_update_into_organized_preserves_launcher_state_vendor(tmp_path):
    source = tmp_path / "flat"
    source.mkdir()
    (source / "TripFinder.exe").write_bytes(b"new runtime")
    (source / "Aggiorna.exe").write_bytes(b"updater")
    (source / "vendor").mkdir()
    (source / "vendor" / "custom.json").write_text("factory")
    (source / "vendor" / "new.exe").write_bytes(b"new")
    (source / "data").mkdir()
    (source / "data" / "trip_finder.db").write_bytes(b"must not copy")
    (source / "data" / "cities15000.txt").write_text("public")
    target = organized(tmp_path / "installed")
    (target / "app" / "vendor").mkdir()
    (target / "app" / "vendor" / "custom.json").write_text("personal")
    (target / "state" / "trip_finder.db").write_bytes(b"personal database")
    update_sync.sincronizza(origine=source, destinazione=target)
    assert (target / "TripFinder.exe").read_bytes() == b"launcher"
    assert (target / "app" / "TripFinder.exe").read_bytes() == b"new runtime"
    assert (target / "app" / "vendor" / "custom.json").read_text() == "personal"
    assert (target / "app" / "vendor" / "new.exe").is_file()
    assert (target / "state" / "trip_finder.db").read_bytes() == b"personal database"
    assert (target / "state" / "cities15000.txt").read_text() == "public"


def test_update_copy_failure_restores_every_preceding_file(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir(); target.mkdir()
    for name in ("TripFinder.exe", "Aggiorna.exe"):
        (source / name).write_text("new")
        (target / name).write_text("old")
    copied = []
    original = update_sync._copia_insistendo
    def fail_second(origin, destination):
        copied.append(destination)
        if len(copied) == 2:
            destination.write_text("partial")
            raise OSError("synthetic failure")
        original(origin, destination)
    monkeypatch.setattr(update_sync, "_copia_insistendo", fail_second)
    with pytest.raises(OSError, match="synthetic"):
        update_sync.sincronizza(origine=source, destinazione=target)
    assert all((target / name).read_text() == "old" for name in ("TripFinder.exe", "Aggiorna.exe"))


def test_update_lock_is_atomic_and_only_owner_releases(tmp_path):
    lock = tmp_path / "update.lock"
    def claim(_):
        try:
            return update_sync.claim_update_lock(lock, "test")
        except RuntimeError:
            return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        owners = [value for value in pool.map(claim, range(6)) if value]
    assert len(owners) == 1
    assert update_sync.release_update_lock(lock, "another-owner") is False
    assert lock.exists()
    assert update_sync.release_update_lock(lock, owners[0]) is True


def test_stale_lock_requires_expiry_and_dead_owner(tmp_path, monkeypatch):
    lock = tmp_path / "update.lock"
    lock.write_text(json.dumps({"owner": "old", "pid": 123, "created": 0}))
    monkeypatch.setattr(update_sync, "_pid_alive", lambda pid: True)
    with pytest.raises(RuntimeError):
        update_sync.claim_update_lock(lock, "new")
    monkeypatch.setattr(update_sync, "_pid_alive", lambda pid: False)
    owner = update_sync.claim_update_lock(lock, "new")
    update_sync.transfer_update_lock(lock, owner, 456)
    assert json.loads(lock.read_text())["pid"] == 456
    assert update_sync.release_update_lock(lock, owner)


def test_connection_secret_and_legacy_setting_writes_do_not_erase_each_other(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    with ThreadPoolExecutor(max_workers=3) as pool:
        tasks = [pool.submit(config.update_connection_secret, "one", "synthetic-one"),
                 pool.submit(config.update_connection_secret, "two", "synthetic-two"),
                 pool.submit(config.save_local_secrets, {"allow_paid_providers": False})]
        for task in tasks:
            task.result()
    assert config.connection_secret("one") == "synthetic-one"
    assert config.connection_secret("two") == "synthetic-two"
    assert config.read_local_secrets()["allow_paid_providers"] is False
    config.update_connection_secret("one", None)
    assert config.connection_secret("one") == ""
    assert config.connection_secret("two") == "synthetic-two"


def test_migration_preserves_store_vendor_and_original_rollback(tmp_path):
    source = organized(tmp_path / "portable")
    (source / "state" / "other-profile.txt").write_text("must not mix")
    target = tmp_path / "installed"
    (target / "data").mkdir(parents=True)
    (target / "TripFinder.exe").write_bytes(b"old exe")
    (target / ".env").write_text("synthetic settings")
    (target / "data" / "local_secrets.json").write_text('{"synthetic":true}')
    (target / "vendor").mkdir()
    (target / "vendor" / "custom.json").write_text("keep")
    with sqlite3.connect(target / "data" / "trip_finder.db") as database:
        database.execute("CREATE TABLE marker(value TEXT)")
        database.execute("INSERT INTO marker VALUES ('keep')")
    database.close()
    backup = tmp_path / "backup"
    migrate_installation(source=source, target=target, backup=backup)
    assert {entry.name for entry in target.iterdir()} == {"TripFinder.exe", "LEGGIMI.txt", "app", "state"}
    assert (target / "state" / ".env").read_text() == "synthetic settings"
    assert (target / "app" / "vendor" / "custom.json").read_text() == "keep"
    assert not (target / "state" / "other-profile.txt").exists()
    with sqlite3.connect(target / "state" / "trip_finder.db") as database:
        assert database.execute("SELECT value FROM marker").fetchone() == ("keep",)
    database.close()
    assert (backup / "TripFinder.exe").read_bytes() == b"old exe"
    restore_installation(target=target, backup=backup, failed=tmp_path / "failed-new")
    assert (target / "TripFinder.exe").read_bytes() == b"old exe"


def test_existing_state_blocks_migration_without_moving_files(tmp_path):
    source = organized(tmp_path / "portable")
    target = tmp_path / "installed"
    (target / "data").mkdir(parents=True)
    (target / "TripFinder.exe").write_bytes(b"legacy")
    with install_layout.state_ownership(target / "data"):
        with pytest.raises(RuntimeError):
            migrate_installation(source=source, target=target, backup=tmp_path / "backup")
    assert (target / "TripFinder.exe").read_bytes() == b"legacy"
    assert not (tmp_path / "backup").exists()


def test_icon_keeps_all_sizes_and_transparency(tmp_path):
    path = build(tmp_path / "icon.ico")
    first = path.read_bytes()
    assert build(path).read_bytes() == first
    with Image.open(path) as icon:
        assert icon.ico.sizes() == {(size, size) for size in SIZES}
        for size in SIZES:
            frame = icon.ico.getimage((size, size)).convert("RGBA")
            assert frame.getpixel((0, 0))[3] == 0
            assert frame.getpixel((size // 2, size // 2))[3] > 0


def test_archive_never_includes_personal_state(tmp_path):
    source = organized(tmp_path / "bundle")
    (source / "state" / "trip_finder.db").write_bytes(b"private")
    (source / ".env").write_text("private")
    path = write_archive(source, tmp_path / "portable.zip", organized=True)
    with ZipFile(path) as archive:
        assert "TripFinder/state/" in archive.namelist()
        assert not any(name.endswith("trip_finder.db") or name.endswith(".env") for name in archive.namelist())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native launcher")
def test_native_launcher_forwards_quoted_arguments_and_stdio(tmp_path):
    source = tmp_path / "flat"
    source.mkdir()
    (source / "_internal").mkdir()
    (source / "data").mkdir()
    for name in install_layout.DATASETS:
        (source / "data" / name).write_text("public")
    (source / "LEGGIMI.txt").write_text("guida")
    compiler = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    program = tmp_path / "echo.cs"
    program.write_text('using System; class Echo { static int Main(string[] args) { foreach(string arg in args) Console.WriteLine(arg); Console.Write(Console.In.ReadToEnd()); return 7; } }')
    # A GUI runtime would discard Python stdio: only the dedicated console
    # helper may handle MCP. A non-executable placeholder proves the routing.
    (source / "TripFinder.exe").write_bytes(b"windowed runtime must not run for MCP")
    subprocess.run([str(compiler), "/nologo", "/target:exe", "/out:" + str(source / "TripFinderMCP.exe"), str(program)], check=True, capture_output=True)
    target = organize_bundle(source, tmp_path / "portable")
    arguments = ["--mcp-stdio", "space arg", 'quote"arg', "trailing\\"]
    result = subprocess.run([str(target / "TripFinder.exe"), *arguments], input="protocol-input\n", text=True, capture_output=True, timeout=15)
    assert result.returncode == 7
    assert result.stdout.splitlines() == ["--stdio", *arguments[1:], "protocol-input"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native interactive stdio")
def test_native_mcp_launcher_flushes_each_reply_without_stdin_eof(tmp_path):
    import queue
    import threading

    source = tmp_path / "flat"
    source.mkdir()
    (source / "_internal").mkdir()
    (source / "data").mkdir()
    for name in install_layout.DATASETS:
        (source / "data" / name).write_text("public")
    (source / "LEGGIMI.txt").write_text("guida")
    (source / "TripFinder.exe").write_bytes(b"windowed runtime must not handle MCP")
    compiler = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    program = tmp_path / "interactive.cs"
    program.write_text('using System; class Echo { static int Main() { string line; while((line=Console.ReadLine())!=null) { Console.WriteLine("reply:"+line); Console.Out.Flush(); } return 0; } }')
    subprocess.run([str(compiler), "/nologo", "/target:exe", "/out:" + str(source / "TripFinderMCP.exe"), str(program)], check=True, capture_output=True)
    target = organize_bundle(source, tmp_path / "portable")
    process = subprocess.Popen([str(target / "TripFinder.exe"), "--mcp-stdio"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    replies = queue.Queue()

    def read_replies():
        for line in process.stdout:
            replies.put(line.strip())

    reader = threading.Thread(target=read_replies, daemon=True)
    reader.start()
    try:
        for message in ("initialize", "tools/list"):
            process.stdin.write(message + "\n")
            process.stdin.flush()
            assert replies.get(timeout=4) == "reply:" + message
            assert process.poll() is None  # Host keeps stdin open between RPCs.
        process.stdin.close()
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
            process.wait(timeout=5)
        reader.join(timeout=1)
        process.stdout.close()
        process.stderr.close()
