"""Preflight e contenuto del bundle, senza avviare PyInstaller."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from scripts import build_exe


@pytest.fixture
def build_root(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    for name, value in {
        "ROOT": root,
        "SPEC": root / "TripFinder.spec",
        "DIST": root / "dist",
        "BUILD": root / "build",
        "BUNDLE": root / "dist" / "TripFinder",
    }.items():
        monkeypatch.setattr(build_exe, name, value)
    build_exe.SPEC.write_text("spec simulato", encoding="utf-8")
    (root / "data").mkdir()
    for name in build_exe.DATASETS:
        (root / "data" / name).write_text("catalogo simulato", encoding="utf-8")
    for directory in (build_exe.DIST, build_exe.BUILD):
        directory.mkdir()
        (directory / "precedente.txt").write_text("preservare su errore", encoding="utf-8")
    return root


def _unexpected(*args, **kwargs):
    pytest.fail("preflight fallito: non deve cancellare o avviare build/download")


def test_cataloghi_mancanti_bloccano_build_senza_cancellare(build_root, monkeypatch):
    monkeypatch.setattr(build_exe, "_assicura_datasets", lambda: False)
    monkeypatch.setattr(build_exe, "subprocess", SimpleNamespace(check_call=_unexpected))
    monkeypatch.setattr(build_exe, "shutil", SimpleNamespace(rmtree=_unexpected))

    assert build_exe.main() == 1
    assert (build_exe.DIST / "precedente.txt").exists()
    assert (build_exe.BUILD / "precedente.txt").exists()


@pytest.mark.parametrize("returncode", [0, 1])
def test_download_incompleto_non_soddisfa_preflight(build_root, monkeypatch, returncode):
    (build_root / "data" / "airports.csv").unlink()
    monkeypatch.setattr(
        build_exe, "subprocess",
        SimpleNamespace(run=lambda *args, **kwargs: SimpleNamespace(returncode=returncode)),
    )

    assert build_exe._assicura_datasets() is False


@pytest.mark.parametrize("unsafe_name", ["DIST", "BUILD"])
def test_controlla_tutti_i_path_prima_di_cancellare(build_root, monkeypatch, unsafe_name):
    monkeypatch.setattr(build_exe, unsafe_name, build_root / ".." / "external")
    monkeypatch.setattr(build_exe, "_assicura_datasets", _unexpected)
    monkeypatch.setattr(build_exe, "shutil", SimpleNamespace(rmtree=_unexpected))

    assert build_exe.main() == 1
    assert (build_root / "dist" / "precedente.txt").exists()
    assert (build_root / "build" / "precedente.txt").exists()


def test_destinazione_risolta_fuori_root_blocca_anche_symlink(build_root, monkeypatch):
    # Simula il risultato di resolve su junction/symlink anche su Windows,
    # dove creare un symlink puo' richiedere privilegi non disponibili nei test.
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == build_exe.BUILD:
            return build_root.parent / "external"
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(build_exe, "_assicura_datasets", _unexpected)
    monkeypatch.setattr(build_exe, "shutil", SimpleNamespace(rmtree=_unexpected))

    assert build_exe.main() == 1
    assert (build_root / "dist" / "precedente.txt").exists()
    assert (build_root / "build" / "precedente.txt").exists()


def test_non_cancella_la_root_del_progetto(build_root, monkeypatch):
    monkeypatch.setattr(build_exe, "BUILD", build_root)
    monkeypatch.setattr(build_exe, "_assicura_datasets", _unexpected)
    monkeypatch.setattr(build_exe, "shutil", SimpleNamespace(rmtree=_unexpected))

    assert build_exe.main() == 1
    assert build_exe.SPEC.exists()


@pytest.mark.parametrize("remove_dataset", [False, True])
def test_bundle_contiene_tutti_i_cataloghi_o_non_pubblica_zip(
    build_root, monkeypatch, remove_dataset,
):
    def fake_pyinstaller(*args, **kwargs):
        build_exe.BUNDLE.mkdir(parents=True)
        (build_exe.BUNDLE / "TripFinder.exe").write_bytes(b"build simulata")
        if remove_dataset:
            (build_root / "data" / "airports.csv").unlink()

    monkeypatch.setattr(build_exe, "subprocess", SimpleNamespace(check_call=fake_pyinstaller))
    zip_path = build_exe.DIST / "TripFinder-windows.zip"
    if remove_dataset:
        with pytest.raises(FileNotFoundError):
            build_exe.main()
        assert not zip_path.exists()
    else:
        assert build_exe.main() == 0
        with ZipFile(zip_path) as archive:
            assert {f"TripFinder/data/{name}" for name in build_exe.DATASETS} <= set(archive.namelist())
