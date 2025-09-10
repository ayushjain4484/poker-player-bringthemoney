from abc import ABC, abstractmethod


class Strategy(ABC):
    @abstractmethod
    def decide_bet(self, game_state: dict) -> int:
        """Return the bet amount as a non-negative integer."""
        raise NotImplementedError

    def showdown(self, game_state: dict) -> None:
        """Optional hook to learn/log after hand ends."""
        pass
