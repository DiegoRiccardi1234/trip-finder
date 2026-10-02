"""Una categoria vuota non implica un motore senza operatori."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.providers import registry


@pytest.fixture
def isolated_registry(monkeypatch):
    monkeypatch.setattr(registry, "_registry", {})
    monkeypatch.setattr(registry, "_loaded", False)
    monkeypatch.setattr(registry, "PACKAGES", ("providers.empty",))
    monkeypatch.setattr(
        registry, "importlib",
        SimpleNamespace(import_module=lambda name: SimpleNamespace(__path__=[])),
    )
    monkeypatch.setattr(registry, "pkgutil", SimpleNamespace(iter_modules=lambda path: []))


def test_categoria_vuota_non_emette_falso_allarme(isolated_registry, caplog):
    registry._registry["esistente"] = object()
    with caplog.at_level(logging.DEBUG, logger=registry.__name__):
        registry._discover()

    assert any("nessun adapter" in record.message for record in caplog.records)
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


@pytest.mark.parametrize("module_present", [False, True])
def test_registry_vuoto_segnala_errore_anche_con_moduli_senza_adapter(
    isolated_registry, monkeypatch, caplog, module_present,
):
    if module_present:
        monkeypatch.setattr(
            registry, "pkgutil",
            SimpleNamespace(iter_modules=lambda path: [SimpleNamespace(name="helper")]),
        )
    registry._discover()

    assert any(
        record.levelno == logging.ERROR and "nessun provider registrato" in record.message
        for record in caplog.records
    )


@pytest.mark.parametrize("package_failure", [False, True])
def test_import_rotto_resta_visibile_senza_bloccare_gli_altri(
    isolated_registry, monkeypatch, caplog, package_failure,
):
    registry._registry["esistente"] = object()

    def broken_import(name):
        if package_failure or name.endswith(".broken"):
            raise ModuleNotFoundError("dipendenza adapter assente")
        return SimpleNamespace(__path__=[])

    monkeypatch.setattr(registry, "importlib", SimpleNamespace(import_module=broken_import))
    monkeypatch.setattr(
        registry, "pkgutil",
        SimpleNamespace(iter_modules=lambda path: [SimpleNamespace(name="broken")]),
    )
    registry._discover()

    assert registry._registry["esistente"] is not None
    assert any(
        record.levelno == logging.ERROR and "non caricato" in record.message and record.exc_info
        for record in caplog.records
    )
