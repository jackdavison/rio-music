"""Collectors self-register via @collector(name). load_all() imports every module here."""
import importlib
import logging
import pkgutil

log = logging.getLogger(__name__)
_loaded = False


def load_all():
    global _loaded
    if _loaded:
        return
    for mod in pkgutil.iter_modules(__path__):
        if mod.name in ("base", "util"):
            continue
        try:
            importlib.import_module(f"{__name__}.{mod.name}")
        except Exception as exc:  # a broken module must not take the others down
            log.error("could not import collector module %s: %s", mod.name, exc)
    _loaded = True
