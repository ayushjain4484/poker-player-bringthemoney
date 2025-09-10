from typing import Dict, Any, Optional
from src.strategy.apex.config import ApexConfig
from src.strategy.apex.apex_predator import ApexPredatorStrategy
from src.strategy.base import Strategy

class StrategyManager:
    def __init__(self, overrides: Optional[Dict[str, Any]] = None):
        cfg = ApexConfig.from_dict(overrides or {})
        self._impl: Strategy = ApexPredatorStrategy(cfg)

    def decide_bet(self, game_state: Dict[str, Any]) -> int:
        return self._impl.decide_bet(game_state)

    def post_round_update(self, game_state: Dict[str, Any]) -> None:
        self._impl.showdown(game_state)

    def get_strategy(self) -> Strategy:
        return self._impl


