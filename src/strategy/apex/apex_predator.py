from typing import Any, Dict
from src.strategy.base import Strategy
from .config import ApexConfig
from .rng import deterministic_rng
from .sizing import (
    size_from_pot, legal_raise, promote_raise, raise_to_amount, finalize
)
from .opponent_model import OpponentModel
from .cards import hand_bucket
from .board import (
    board_texture, blocker_signal, hand_strength_vs_board
)

class ApexPredatorStrategy(Strategy):
    """
    ApexPredatorStrategy:
    - GTO-ish backbone: pot-odds checks, small c-bets on dry boards, mixed frequencies (deterministic).
    - Exploits: passive-table detection → more calls/thin value; tighten vs huge sizing when capped.
    - Texture-aware: monotone/paired/straighty; multiway bluff dampening (configurable).
    - SPR-aware overbets in polar-friendly spots.
    - Full legality clamps (never > stack; legal raises only).
    """

    def __init__(self, config: ApexConfig | None = None):
        self.cfg = config or ApexConfig()
        self.model = OpponentModel()

    # --- Public API ---
    def decide_bet(self, game_state: Dict[str, Any]) -> int:
        try:
            G = self._g(game_state)
            if G["my_stack"] <= 0:
                return 0

            # Push/fold mode ≤10bb and preflop
            if G["effective_bb"] <= 10 and G["street"] == 0:
                return finalize(self._push_fold_preflop(G), G["to_call"], G["minimum_raise"], G["my_stack"])

            rng = deterministic_rng(game_state, G["in_action"])
            if G["street"] == 0:
                bet = self._preflop_decision(G, rng)
            else:
                bet = self._postflop_decision(G, rng)
            return finalize(bet, G["to_call"], G["minimum_raise"], G["my_stack"])
        except Exception:
            return 0

    def showdown(self, game_state: Dict[str, Any]) -> None:
        try:
            self.model.update_showdown(game_state.get("players", []) or [])
        except Exception:
            pass

    # --- Preflop ---
    def _preflop_decision(self, G: Dict[str, Any], rng: float) -> int:
        bucket = hand_bucket(G["hole"])
        pos = self._norm_pos(G["position"], G["n_seats"])
        to_call = G["to_call"]
        facing_raise = to_call > G["bb"]
        ip = pos in ("CO", "BTN")
        open_mult = self.cfg.PREFLOP_OPEN_MIN_MULT_IP if ip else self.cfg.PREFLOP_OPEN_MIN_MULT_OOP

        if not facing_raise:
            if self._should_open(pos, bucket, G["is_4max"]):
                desired_total = int(round(open_mult * G["bb"]))
                return raise_to_amount(G["current_buy_in"], to_call, G["minimum_raise"], desired_total, G["my_stack"])
            if G["position"] == "BB" and self._bb_should_defend(bucket, G["is_4max"]):
                return min(to_call, G["my_stack"])
            return 0

        # Facing open → 3-bet/value/bluff/call/fold
        factor = self.cfg.PREFLOP_3BET_FACTOR_IP if ip else self.cfg.PREFLOP_3BET_FACTOR_OOP
        base_size = max(G["bb"], G["current_buy_in"])

        if self._should_value_3bet(pos, bucket, G["is_4max"]):
            return raise_to_amount(G["current_buy_in"], to_call, G["minimum_raise"], int(round(factor * base_size)), G["my_stack"])

        if self._should_bluff_3bet(pos, bucket, G["is_4max"], G["effective_bb"], rng):
            return raise_to_amount(G["current_buy_in"], to_call, G["minimum_raise"], int(round(factor * base_size)), G["my_stack"])

        if self._should_cold_call(pos, bucket, to_call, G, rng):
            return min(to_call, G["my_stack"])

        cheap = min(G["my_stack"] // (45 if G["is_4max"] else 50), max(1, G["bb"]))
        return min(to_call, G["my_stack"]) if to_call <= cheap else 0

    # --- Postflop ---
    def _postflop_decision(self, G: Dict[str, Any], rng: float) -> int:
        hs = hand_strength_vs_board(G["hole"], G["board"], G["is_4max"])
        tex = board_texture(G["board"])
        to_call = G["to_call"]
        spr = self._spr(G["my_stack"], G["pot"], to_call)
        multiway = G["n_alive"] > 2

        have_blocker, _ = blocker_signal(G["hole"], G["board"], tex)
        exploit_vs_callers = self.cfg.PUNISH_PASSIVES and self._table_looks_passive(G)

        # Bluff dampening in multiway pots
        dry_bf = self.cfg.DRY_BLUFF_FREQ * (0.6 if (self.cfg.BLUFF_DAMPEN_MULTIWAY and multiway) else 1.0)
        wet_bf = self.cfg.WET_BLUFF_FREQ * (0.6 if (self.cfg.BLUFF_DAMPEN_MULTIWAY and multiway) else 1.0)

        if to_call == 0:
            if hs["two_pair_plus"] or hs["overpair"] or hs["top_pair_value"]:
                frac = self.cfg.VALUE_RAISE_FRAC if spr > 4 or tex["dry"] else 0.60
                target = size_from_pot(G["pot"], frac, G["my_stack"])
                return legal_raise(0, max(1, G["minimum_raise"]), G["my_stack"], absolute=target)
            if hs["strong_draw"]:
                frac = self.cfg.VALUE_RAISE_FRAC if not tex["dry"] else self.cfg.CBET_SMALL_FRAC
                target = size_from_pot(G["pot"], frac, G["my_stack"])
                return legal_raise(0, max(1, G["minimum_raise"]), G["my_stack"], absolute=target)

            bf = (dry_bf if tex["dry"] else wet_bf) + (0.08 if have_blocker else 0.0)
            if rng < bf:
                frac = self.cfg.CBET_SMALL_FRAC if tex["dry"] else self.cfg.VALUE_RAISE_FRAC
                target = size_from_pot(G["pot"], frac, G["my_stack"])
                return legal_raise(0, max(1, G["minimum_raise"]), G["my_stack"], absolute=target)
            return 0

        # Facing a bet
        price_ok = self._price_ok(to_call, G["pot"])
        cheap_call_limit = self._cheap_call_limit(G["my_stack"])

        if (hs["two_pair_plus"] or hs["overpair"] or (hs["top_pair_value"] and not tex["wet"])) and rng < 0.40:
            target = size_from_pot(G["pot"], self.cfg.VALUE_RAISE_FRAC, G["my_stack"])
            r = promote_raise(to_call, G["minimum_raise"], G["my_stack"], target)
            if r > to_call:
                return r
            return min(to_call, G["my_stack"])

        if have_blocker and rng < self.cfg.XR_BLUFF_FREQ and not (hs["two_pair_plus"] or hs["overpair"] or hs["top_pair_value"]):
            frac = self.cfg.VALUE_RAISE_FRAC if not tex["dry"] else self.cfg.CBET_SMALL_FRAC
            target = size_from_pot(G["pot"], frac, G["my_stack"])
            r = promote_raise(to_call, G["minimum_raise"], G["my_stack"], target)
            if r > to_call:
                return r

        if (hs["two_pair_plus"] or hs["overpair"]) and self._polar_friendly(tex) and spr <= 3 and rng < 0.35:
            over = size_from_pot(G["pot"], self.cfg.POLAR_OVERBET_FRAC, G["my_stack"], cap_stack_frac=self.cfg.MAX_STACK_OVERBET_FRAC)
            r = promote_raise(to_call, G["minimum_raise"], G["my_stack"], over)
            if r > to_call:
                return r

        if price_ok or to_call <= cheap_call_limit:
            if exploit_vs_callers and (hs["top_pair_value"] or tex["wet"] or hs["strong_draw"]):
                return min(to_call, G["my_stack"])
            return min(to_call, G["my_stack"])

        if self.cfg.TIGHTEN_VS_HUGE_SIZING and not have_blocker and not (hs["middle_pair"] or hs["weak_pair"]):
            return 0

        return 0

    # --- Helpers ---
    def _spr(self, stack: int, pot: int, to_call: int) -> float:
        eff = max(1, stack - to_call)
        base = max(1, pot + to_call)
        return eff / float(base)

    def _price_ok(self, to_call: int, pot: int) -> bool:
        if to_call <= 0:
            return True
        denom = pot + to_call
        if denom <= 0:
            return to_call <= 1
        return (to_call / float(denom)) <= self.cfg.POT_ODDS_CALL_THRESHOLD

    def _cheap_call_limit(self, stack: int) -> int:
        pct_cap = int(stack * self.cfg.CHEAP_CALL_STACK_PCT)
        return max(1, min(pct_cap, self.cfg.CHEAP_CALL_ABS_CAP))

    def _polar_friendly(self, texture: Dict[str, bool]) -> bool:
        return texture.get("paired") or texture.get("monotone") or texture.get("straighty")

    def _table_looks_passive(self, G: Dict[str, Any]) -> bool:
        pot = G["pot"]; current = G["current_buy_in"]; sb = max(1, G["small_blind"])
        multiway = G["n_alive"] > 2
        is_smallish = current <= max(4 * sb, 24)
        return is_smallish and (multiway or pot <= 20 * sb)

    def _position(self, S: Dict[str, Any], me_idx: int) -> str:
        n = len(S.get("players", []) or [])
        if n == 0: return "EP"
        dealer = int(S.get("dealer", 0) or 0)
        sb = (dealer + 1) % n
        bb = (dealer + 2) % n
        if me_idx == dealer: return "BTN"
        if me_idx == sb:     return "SB"
        if me_idx == bb:     return "BB"
        return "UTG" if n <= 6 else "MP"

    def _norm_pos(self, pos: str, n_seats: int) -> str:
        if n_seats <= 4 and pos == "UTG":
            return "CO"
        return pos

    # Range knobs (same logic as prior, using deterministic mixing through rng in preflop call/3bet)
    def _should_open(self, pos: str, bucket: int, is4: bool) -> bool:
        import random
        if pos in ("UTG", "EP") and is4:
            return bucket <= 6 or (bucket == 7 and random.random() < 0.35)
        if pos in ("UTG", "EP"):
            return bucket in (1, 2) or (bucket in (3,4,5) and random.random() < 0.35)
        if pos in ("MP", "CO"):
            return bucket <= 6 or (bucket == 7 and random.random() < 0.25)
        if pos == "BTN":
            return True if is4 else (bucket <= 7)
        if pos == "SB":
            return bucket <= 7 or (is4 and bucket == 8 and random.random() < 0.15)
        return False

    def _bb_should_defend(self, bucket: int, is4: bool) -> bool:
        import random
        return (bucket <= 7) or (is4 and random.random() < 0.5) or (not is4 and random.random() < 0.25)

    def _should_value_3bet(self, pos: str, bucket: int, is4: bool) -> bool:
        return bucket == 1 or (bucket == 2 and pos in ("CO", "BTN", "SB"))

    def _should_bluff_3bet(self, pos: str, bucket: int, is4: bool, eff_bb: int, rng: float) -> bool:
        if eff_bb <= 20:
            return False
        if pos in ("BTN", "SB") and is4 and bucket in (5,6,3):
            return rng < 0.55
        if pos == "BB" and is4 and bucket in (5,6,3,7):
            return rng < 0.45
        return (bucket in (5,6,3) and pos in ("CO", "BTN", "SB") and rng < 0.40)

    def _should_cold_call(self, pos: str, bucket: int, to_call: int, G: Dict[str, Any], rng: float) -> bool:
        if to_call > G["my_stack"]:
            return False
        if pos in ("CO", "BTN"):
            return bucket in (2,3,4,5,6)
        if pos == "BB":
            return bucket <= 7
        if pos == "SB":
            return (bucket in (2,3,4,5) and not G["is_4max"] and rng < 0.4)
        return bucket in (2,3,4) and rng < 0.3

    # Push/fold (≤10bb)
    def _push_fold_preflop(self, G: Dict[str, Any]) -> int:
        bucket = hand_bucket(G["hole"])
        pos = self._norm_pos(G["position"], G["n_seats"])
        to_call = G["to_call"]

        premium = (bucket == 1); strong = (bucket == 2)
        small_pairs = (bucket == 4); suited_ace = (bucket == 5); broad_mid = (bucket == 3)

        jam_ok = False
        if pos in ("EP", "UTG", "MP", "CO"):
            jam_ok = premium or strong or small_pairs or suited_ace or (self.cfg.PUSHFOLD_ENABLE_BROADEN_4MAX and G["is_4max"] and broad_mid)
        elif pos == "BTN":
            jam_ok = bucket <= 6 or (G["is_4max"] and bucket == 7)
        elif pos == "SB":
            jam_ok = True
        elif pos == "BB":
            jam_ok = premium or strong or small_pairs or suited_ace or broad_mid

        facing_raise = to_call > G["bb"]
        if facing_raise and not (premium or strong):
            jam_ok = jam_ok and (G["effective_bb"] <= (9 if G["is_4max"] else 8))

        return G["my_stack"] if jam_ok else (min(to_call, G["my_stack"]) if to_call <= G["bb"] else 0)

    # Parse game_state into a convenient struct
    def _g(self, S: Dict[str, Any]) -> Dict[str, Any]:
        players = S.get("players", []) or []
        in_action = int(S.get("in_action", 0) or 0)
        me = players[in_action] if 0 <= in_action < len(players) else {}
        hole = me.get("hole_cards", []) or []
        board = S.get("community_cards", []) or []
        current_buy_in = int(S.get("current_buy_in", 0) or 0)
        minimum_raise = int(S.get("minimum_raise", 0) or 0)
        my_bet = int(me.get("bet", 0) or 0)
        my_stack = int(me.get("stack", 0) or 0)
        small_blind = int(S.get("small_blind", 0) or 0)
        to_call = max(0, current_buy_in - my_bet)
        bb_guess = max(2 * small_blind, minimum_raise, 1)

        opp_stacks = [int(p.get("stack", 0) or 0) for i, p in enumerate(players) if i != in_action]
        covered = max(opp_stacks) if opp_stacks else my_stack
        effective_stack = min(my_stack, covered)
        effective_bb = max(1, effective_stack // max(1, bb_guess))

        n_alive = sum(1 for p in players if (p or {}).get("status", "active") == "active")
        n_seats = len(players)

        position = self._position(S, in_action)
        street = len(board)

        return dict(
            players=players, me=me, hole=hole, board=board,
            current_buy_in=current_buy_in, minimum_raise=minimum_raise,
            my_bet=my_bet, my_stack=my_stack, to_call=to_call,
            bb=bb_guess, effective_bb=effective_bb, n_alive=n_alive,
            n_seats=n_seats, is_4max=(n_seats <= 4),
            position=position, street=street, dealer=S.get("dealer", 0) or 0,
            pot=int(S.get("pot", 0) or 0), small_blind=small_blind, in_action=in_action
        )
