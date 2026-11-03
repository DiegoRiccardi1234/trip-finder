"""Source changes must be parsed or rejected, never erase promotion validity."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def builder(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts" / "build_tessere.py"
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location("build_tessere_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("index", [0, 1])
def test_current_promo_end_and_repeated_effective_footer_are_both_extracted(builder, index):
    text = ("Riduzione del 20%. La promo è valida per viaggi fino al 31 dicembre 2027. "
            "Valida dal 20.10.2026 Pag. 1 a 2. Valida dal 20.10.2026 Pag. 2 a 2.")
    parsed = builder.leggi(text, builder.ESTRATTE[index])
    assert parsed["valido_da"] == "2026-10-20"
    assert parsed["valido_a"] == "2027-12-31"
    assert parsed["valore"] == 20
    assert builder.controlla(parsed) == []


def test_legacy_explicit_travel_window_remains_supported(builder):
    text = "Riduzione del 20%. Valida per viaggi dal 5 aprile al 30 novembre 2026. Valida dal 01.04.2026."
    parsed = builder.leggi(text, builder.ESTRATTE[0])
    assert (parsed["valido_da"], parsed["valido_a"]) == ("2026-04-05", "2026-11-30")
    assert builder.controlla(parsed) == []


@pytest.mark.parametrize("text", [
    "Valida per viaggi fino al 31 dicembre 2027.",
    "Valida per viaggi fino al 31 dicembre 2027. Valida dal 31.02.2026.",
    "Valida per viaggi fino al 31 dicembre 2027. Valida dal 20.10.2026. Valida dal 21.10.2026.",
    "Valida per viaggi fino al 31 dicembre 2027. Valida dal 01.01.2028.",
    "Valida per viaggi fino al 31 febbraio 2027. Valida dal 20.10.2026.",
    "Validità prorogata alla prossima stagione.",
])
def test_missing_invalid_or_ambiguous_dates_cannot_silently_pass(builder, text):
    parsed = builder.leggi("Riduzione del 20%. " + text, builder.ESTRATTE[0])
    assert any("validita'" in problem for problem in builder.controlla(parsed))


def test_unrecognized_validity_cannot_replace_existing_catalogue(builder, monkeypatch, tmp_path):
    target = tmp_path / "catalogue.json"
    original = b'{"preserve":"existing catalogue and curated entries"}'
    target.write_bytes(original)
    monkeypatch.setattr(builder, "USCITA", target)
    monkeypatch.setattr(builder, "precedente", lambda: {})
    monkeypatch.setattr(builder, "ESTRATTE", [builder.ESTRATTE[0]])
    monkeypatch.setattr(builder, "CURATE", [])
    monkeypatch.setattr(builder, "scarica", lambda url: b"public PDF")
    monkeypatch.setattr(builder, "testo_del_pdf", lambda pdf: "Riduzione del 20%. Validità modificata.")
    monkeypatch.setattr(builder.sys, "argv", ["build_tessere.py"])
    with pytest.raises(SystemExit, match="validita'"):
        builder.main()
    assert target.read_bytes() == original


def test_check_ignores_only_verification_date_not_validity_changes(builder, monkeypatch, tmp_path):
    old = {"tessere": [{"id": "promo", "valido_da": "2026-10-20", "valido_a": "2027-12-31", "verificato_il": "2026-10-25"}]}
    target = tmp_path / "catalogue.json"
    target.write_text(json.dumps(old), encoding="utf-8")
    monkeypatch.setattr(builder, "USCITA", target)
    new = {"tessere": [{**old["tessere"][0], "verificato_il": "2026-11-03"}]}
    monkeypatch.setattr(builder, "costruisci", lambda: (new, []))
    monkeypatch.setattr(builder.sys, "argv", ["build_tessere.py", "--check"])
    assert builder.main() == 0
    new["tessere"][0]["valido_a"] = "2028-01-01"
    assert builder.main() == 1
    assert json.loads(target.read_text()) == old
