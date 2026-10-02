"""Registro dei provider con scoperta automatica dei moduli.

Aggiungere un operatore significa creare un file sotto app/providers/<modo>/ e
decorare la classe con @register. Nessun'altra modifica altrove.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import TypeVar

from app.models import Mode, Node
from app.providers.base import Provider

logger = logging.getLogger(__name__)

_registry: dict[str, Provider] = {}
_loaded = False

P = TypeVar("P", bound=type[Provider])

#: Pacchetti scansionati all'avvio.
PACKAGES = (
    "app.providers.rail",
    "app.providers.bus",
    "app.providers.air",
    "app.providers.ferry",
    "app.providers.aggregator",
)


def register(cls: P) -> P:
    instance = cls()  # type: ignore[call-arg]
    if instance.id in _registry:
        raise ValueError(f"provider duplicato: {instance.id}")
    _registry[instance.id] = instance
    return cls


def _discover() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for package_name in PACKAGES:
        try:
            package = importlib.import_module(package_name)
        except Exception:  # noqa: BLE001 - un pacchetto rotto non blocca gli altri
            logger.exception("pacchetto adapter non caricato: %s", package_name)
            continue
        trovati = 0
        for module in pkgutil.iter_modules(package.__path__):
            if module.name.startswith("_"):
                continue
            trovati += 1
            full_name = f"{package_name}.{module.name}"
            try:
                importlib.import_module(full_name)
            except Exception:  # noqa: BLE001 - un adapter rotto non blocca gli altri
                logger.exception("adapter non caricato: %s", full_name)
        if not trovati:
            # Alcune categorie, come aggregator, sono ancora volutamente vuote.
            logger.debug("nessun adapter in %s", package_name)
    if not _registry:
        logger.error("nessun provider registrato: la ricerca non trovera' niente")
    logger.info("provider registrati: %s", ", ".join(sorted(_registry)) or "NESSUNO")


def all_providers() -> list[Provider]:
    _discover()
    return list(_registry.values())


def get(provider_id: str) -> Provider:
    _discover()
    try:
        return _registry[provider_id]
    except KeyError as exc:
        known = ", ".join(sorted(_registry)) or "(nessuno)"
        raise KeyError(f"provider sconosciuto: {provider_id}. Disponibili: {known}") from exc


def providers_for(
    origin: Node, destination: Node, modes: set[Mode] | None = None
) -> list[Provider]:
    """Provider che dichiarano di poter servire questa tratta, tier 1 per primi."""
    _discover()
    candidates = [
        provider
        for provider in _registry.values()
        if (modes is None or provider.mode in modes)
        and provider.can_serve(origin, destination)
    ]
    candidates.sort(key=lambda provider: (provider.tier, provider.id))
    return candidates
