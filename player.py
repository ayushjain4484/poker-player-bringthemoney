
class Player:
    VERSION = "v0.1 basic strategy"

    RANKS = {
        '2': 2, '3': 3, '4': 4, '5': 5, '6': 6, '7': 7, '8': 8, '9': 9,
        '10': 10, 'J': 11, 'Q': 12, 'K': 13, 'A': 14
    }

    def card_rank(self, card):
        """Return numeric rank of a card dict like {"rank": "A", "suit": "hearts"}."""
        if not card or 'rank' not in card:
            return 0
        return self.RANKS.get(str(card['rank']).upper(), 0)

    def is_pair(self, hole):
        return len(hole) == 2 and self.card_rank(hole[0]) == self.card_rank(hole[1]) and self.card_rank(hole[0]) > 0

    def both_high(self, hole, threshold=11):
        # threshold 12 = Q; 11 = J
        if len(hole) != 2:
            return False
        r1, r2 = self.card_rank(hole[0]), self.card_rank(hole[1])
        return r1 >= threshold and r2 >= threshold

    def has_pair_with_board(self, hole, board):
        hole_ranks = {self.card_rank(c) for c in hole}
        board_ranks = {self.card_rank(c) for c in board}
        return len(hole_ranks.intersection(board_ranks)) > 0

    def betRequest(self, game_state):
        """
        Basic, safe strategy for LeanPoker:
        - Pocket pair: raise minimum.
        - Both hole cards high (J or better) or pair with board: call; raise small if cheap.
        - Otherwise: fold unless the call is very cheap (<= 2% of our stack).
        Always ensures we don't bet more than our stack and never returns negative values.
        """
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

            # If we are all-in or broke, we can't bet
            if my_stack <= 0:
                return 0

            # Heuristics
            strong_start = self.is_pair(hole)
            decent = self.both_high(hole, threshold=12) or self.has_pair_with_board(hole, board)

            # Cap how much we are willing to risk for calling with mediocre hands
            cheap_call_limit = max(1, my_stack // 50)  # ~2% of stack

            # Default decision: fold
            bet = 0

            if strong_start:
                # Raise minimum over the call
                bet = to_call + (minimum_raise if minimum_raise > 0 else 0)
                # If no one has raised yet, we may still want some aggression
                if bet == 0:
                    bet = min(10, my_stack)  # small open raise when free
            elif decent:
                # Call if affordable; occasionally raise a little when very cheap
                if to_call <= my_stack:
                    bet = to_call
                    cheap_bump = min(minimum_raise, max(0, my_stack // 100))  # tiny sweetener
                    if to_call <= cheap_call_limit and cheap_bump > 0:
                        bet += cheap_bump
                else:
                    bet = 0
            else:
                # Weak hand: only limp in if it's very cheap
                if to_call <= cheap_call_limit:
                    bet = to_call
                else:
                    bet = 0

            # Safety: never exceed our stack
            bet = max(0, min(bet, my_stack))
            return int(bet)
        except Exception:
            # Be ultra-safe on any parsing errors
            return 0

    def showdown(self, game_state):
        # No learning yet; could log results here in the future
        pass

