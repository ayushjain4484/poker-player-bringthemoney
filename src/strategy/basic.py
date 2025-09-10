from .base import Strategy
from src.models.cards import is_pair, both_high, has_pair_with_board


class BasicStrategy(Strategy):
    """
    Basic, safe strategy for LeanPoker:
    - Pocket pair: raise minimum.
    - Both hole cards high (J or better) or pair with board: call; raise small if cheap.
    - Otherwise: fold unless the call is very cheap (<= 2% of our stack).
    Always ensures we don't bet more than our stack and never returns negative values.
    """

    def decide_bet(self, game_state: dict) -> int:
        try:
            players = game_state.get('players', [])
            in_action = game_state.get('in_action', 0)
            me = players[in_action] if 0 <= in_action < len(players) else {}
            hole = me.get('hole_cards', []) or []
            board = game_state.get('community_cards', []) or []

            current_buy_in = int(game_state.get('current_buy_in', 0) or 0)
            minimum_raise = int(game_state.get('minimum_raise', 0) or 0)
            my_bet = int(me.get('bet', 0) or 0)
            my_stack = int(me.get('stack', 0) or 0)

            to_call = max(0, current_buy_in - my_bet)

            if my_stack <= 0:
                return 0

            strong_start = is_pair(hole)
            decent = both_high(hole, threshold=12) or has_pair_with_board(hole, board)

            cheap_call_limit = max(1, my_stack // 50)  # ~2% of stack
            bet = 0

            if strong_start:
                bet = to_call + (minimum_raise if minimum_raise > 0 else 0)
                if bet == 0:
                    bet = min(10, my_stack)
            elif decent:
                if to_call <= my_stack:
                    bet = to_call
                    cheap_bump = min(minimum_raise, max(0, my_stack // 100))
                    if to_call <= cheap_call_limit and cheap_bump > 0:
                        bet += cheap_bump
                else:
                    bet = 0
            else:
                if to_call <= cheap_call_limit:
                    bet = to_call
                else:
                    bet = 0

            bet = max(0, min(bet, my_stack))
            return int(bet)
        except Exception:
            return 0

    def showdown(self, game_state: dict) -> None:
        # no-op for now; could log or learn later
        pass
