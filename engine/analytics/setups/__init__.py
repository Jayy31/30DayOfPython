from engine.analytics.setups.base import REGISTRY, MarketContext, Setup, SetupCandidate, load_setups, register

__all__ = ["REGISTRY", "MarketContext", "Setup", "SetupCandidate", "load_setups", "register"]

from engine.analytics.setups import builtin  # noqa: E402,F401  (registers the built-in setups)
