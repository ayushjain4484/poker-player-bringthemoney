from typing import Optional

from src.strategy.adaptive_strategy import AdaptiveStrategy
from src.strategy.advanced import AdvancedStrategy
from src.strategy.base import Strategy
from src.strategy.killer_instinct_strategy import KillerInstinctStrategy


class Player:
    VERSION = "v0.2 modular"  # keep a public version for the service

    def __init__(self, strategy: Optional[Strategy] = None):
        # default to BasicStrategy if none supplied
        self._strategy = strategy or AdaptiveStrategy()

    def bet_request(self, game_state: dict) -> int:
        return int(self._strategy.decide_bet(game_state))

    # Compatibility for the HTTP service (legacy camelCase)
    def betRequest(self, game_state: dict) -> int:
        # Delegate to the snake_case method to keep one implementation
        return self.bet_request(game_state)

    def showdown(self, game_state: dict) -> None:
        self._strategy.showdown(game_state)
