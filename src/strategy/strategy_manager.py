from __future__ import annotations

import os
from typing import Any, Callable, Dict, Optional

from src.strategy.base import Strategy

# Optional imports for available built-in strategies.
# Keep them optional so the manager can still work even if some aren't present.
try:
    from src.strategy.basic import BasicStrategy  # type: ignore
except Exception:  # pragma: no cover
    BasicStrategy = None  # type: ignore

try:
    from src.strategy.advanced import AdvancedStrategy  # type: ignore
except Exception:  # pragma: no cover
    AdvancedStrategy = None  # type: ignore

# Apex strategy and config (if available)
try:
    from src.strategy.apex.config import ApexConfig  # type: ignore
    from src.strategy.apex.apex_predator import ApexPredatorStrategy  # type: ignore
except Exception:  # pragma: no cover
    ApexConfig = None  # type: ignore
    ApexPredatorStrategy = None  # type: ignore


class StrategyManager:
    """
    StrategyManager that can switch between multiple strategy implementations.

    Selection precedence:
      1) An explicit Strategy instance passed to __init__(strategy=...) is used as-is.
      2) A named strategy (strategy_name) is constructed from the registry.
      3) If neither provided, STRATEGY_NAME env var is used (default: "basic").

    You can switch at runtime via set_strategy(name, overrides).
    """

    def __init__(
        self,
        strategy: Optional[Strategy] = None,
        strategy_name: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
    ):
        self._registry: Dict[str, Callable[[Optional[Dict[str, Any]]], Strategy]] = {}
        self._register_builtins()

        if strategy is not None:
            self._impl: Strategy = strategy
            return

        name = strategy_name or os.getenv("STRATEGY_NAME", "basic").lower()
        self._impl = self._build_strategy(name, overrides)

    def decide_bet(self, game_state: Dict[str, Any]) -> int:
        return int(self._impl.decide_bet(game_state))

    def post_round_update(self, game_state: Dict[str, Any]) -> None:
        # Keep compatibility with strategies using 'showdown' as post-round hook
        self._impl.showdown(game_state)

    def get_strategy(self) -> Strategy:
        return self._impl


    def set_strategy(self, strategy_name: str, overrides: Optional[Dict[str, Any]] = None) -> None:
        """
        Switch the active strategy by name. If the name is not registered or
        its dependencies are missing, raises a ValueError.
        """
        self._impl = self._build_strategy(strategy_name, overrides)

    def available_strategies(self) -> Dict[str, str]:
        """Return a mapping of available strategy names to a short description."""
        return {
            name: fn.__doc__.strip().splitlines()[0] if getattr(fn, "__doc__", None) else ""
            for name, fn in self._registry.items()
        }

    # --- Internal helpers ---
    def _register_builtins(self) -> None:
        """Register built-in strategies present in the environment."""
        if BasicStrategy is not None:
            def _basic_factory(_: Optional[Dict[str, Any]]) -> Strategy:
                """Basic, safe baseline strategy."""
                return BasicStrategy()  # type: ignore
            self._registry["basic"] = _basic_factory

        if AdvancedStrategy is not None:
            def _advanced_factory(_: Optional[Dict[str, Any]]) -> Strategy:
                """Advanced strategy with table-size adjustments."""
                return AdvancedStrategy()  # type: ignore
            self._registry["advanced"] = _advanced_factory

        if ApexPredatorStrategy is not None and ApexConfig is not None:
            def _apex_factory(ovr: Optional[Dict[str, Any]]) -> Strategy:
                """Apex Predator strategy (configurable)."""
                cfg = ApexConfig.from_dict(ovr or {})  # type: ignore
                return ApexPredatorStrategy(cfg)  # type: ignore
            # support multiple aliases
            self._registry["apex"] = _apex_factory
            self._registry["apex_predator"] = _apex_factory

    def _build_strategy(self, name: str, overrides: Optional[Dict[str, Any]]) -> Strategy:
        key = (name or "").lower().strip()
        factory = self._registry.get(key)
        if not factory:
            raise ValueError(f"Unknown strategy '{name}'. Available: {', '.join(sorted(self._registry.keys())) or '(none)'}")
        return factory(overrides)