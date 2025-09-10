from .base import Strategy
from typing import Any, Dict, Optional
import hashlib

# Reuse your helpers if available; these are expected from your codebase
from src.models.cards import is_pair, both_high, has_pair_with_board


class KillerInstinctStrategy(Strategy):
    """
    KillerInstinctStrategy = balanced GTO-ish core with exploit knobs.
    - Street/position aware
    - Mixed-frequency actions via deterministic RNG (seeded by game_id/round/bet_index)
    - Uses SPR, board texture, pot odds, blockers
    - Always returns legal bets (never <0, never >stack, raises meet min-raise)
    """

    # -------- Tunables (safe defaults; adjust to taste) --------
    # Calling discipline
    POT_ODDS_CALL_THRESHOLD = 0.27      # call if price <= 27%
    CHEAP_CALL_STACK_PCT = 0.02         # ~2% stack qualifies as cheap peel
    CHEAP_CALL_ABS_CAP = 50             # absolute cap for cheap peel

    # Preflop ranges (approx; position-adjusted heuristics)
    PREFLOP_OPEN_MIN_RAISE_MULT = 1.0   # open size baseline (min-raise)
    PREFLOP_PAIR_RAISE_EXTRA = 0.5      # add 0.5x min-raise for pocket pairs when deep

    # Postflop sizings
    CBET_SMALL_FRAC = 0.33              # 1/3 pot c-bet on dry boards
    VALUE_RAISE_FRAC = 0.45             # ~45% pot for thin/merge raises
    POLAR_OVERBET_FRAC = 1.2            # ~120% pot overbet in polar spots (when legal)
    MAX_OVERBET_STACK_FRAC = 1.0        # never exceed all-in

    # Mixed frequencies (probabilities)
    BLUFF_FREQ_DRY = 0.35               # dry boards bluff freq
    BLUFF_FREQ_WET = 0.20               # wet boards bluff freq
    XR_BLUFF_FREQ = 0.18                # check-raise bluff freq when we have key blockers
    THIN_VALUE_FREQ = 0.55              # take thin value with frequency

    # Exploit knobs
    TIGHTEN_VS_BIG_SIZING = True        # fold more when price is bad and we’re capped
    PUNISH_PASSIVES = True              # add extra thin value vs callers (heuristic)

    def decide_bet(self, game_state: Dict[str, Any]) -> int:
        try:
            # --- Defensive parse ---
            players = game_state.get('players') or []
            in_action = int(game_state.get('in_action', 0) or 0)
            me = players[in_action] if 0 <= in_action < len(players) else {}

            hole = me.get('hole_cards') or []
            board = game_state.get('community_cards') or []

            current_buy_in = self._to_int(game_state.get('current_buy_in'))
            minimum_raise = self._to_int(game_state.get('minimum_raise'))
            my_bet = self._to_int(me.get('bet'))
            my_stack = self._to_int(me.get('stack'))
            pot = self._to_int(game_state.get('pot'))

            to_call = max(0, current_buy_in - my_bet)
            if my_stack <= 0:
                return 0

            # Deterministic RNG for mixed strategies (reproducible across reruns)
            rng = self._rng(game_state, in_action)

            # Streets / position / SPR
            street = self._street(board)          # preflop/flop/turn/river
            pos_cat = self._position_category(game_state, in_action)  # EP/MP/LP/BLIND
            spr = self._spr(my_stack, pot, to_call)

            # Hand & board features
            pocket_pair = is_pair(hole)
            decent = both_high(hole, threshold=12) or has_pair_with_board(hole, board)
            texture = self._board_texture(board)
            have_blocker, blocker_type = self._blocker_signal(hole, board, texture)

            # Cheap peel limits & pot odds gate
            cheap_call_limit = self._cheap_call_limit(my_stack)
            price_ok = self._price_ok(to_call, pot)

            # Heuristic opponent model (very light):
            opp_count = sum(1 for p in players if (p or {}).get('status') == 'active') or 2
            multiway = opp_count > 2
            exploit_vs_callers = self.PUNISH_PASSIVES and self._table_looks_passive(players, game_state)

            # ---- Decision skeleton ----
            desired = 0

            if street == "preflop":
                desired = self._preflop_plan(
                    pocket_pair=pocket_pair, decent=decent,
                    pos_cat=pos_cat, to_call=to_call, minimum_raise=minimum_raise,
                    stack=my_stack, pot=pot, spr=spr, rng=rng, multiway=multiway
                )
            else:
                desired = self._postflop_plan(
                    hole, board, texture, pocket_pair, decent,
                    to_call, minimum_raise, my_stack, pot, spr, rng,
                    have_blocker, blocker_type, multiway, exploit_vs_callers
                )

            # --- Safety net: legality + clamps ---
            return self._finalize(desired, to_call, minimum_raise, my_stack)

        except Exception:
            return 0

    # -----------------------------
    # PRE-FLOP
    # -----------------------------
    def _preflop_plan(self, pocket_pair: bool, decent: bool, pos_cat: str,
                      to_call: int, minimum_raise: int, stack: int, pot: int,
                      spr: float, rng: float, multiway: bool) -> int:
        """
        Position-adjusted ranges with mixed frequencies.
        - EP: tight; raise mostly with pairs & strong broadways.
        - MP: add suited broadways, Axs.
        - LP: widen; include suited connectors/gappers at some freq when cheap.
        - Blinds: defend tighter OOP, avoid dominated offsuits unless cheap.
        """
        # Opportunity to open when to_call == 0
        if to_call == 0:
            # EP open only good hands
            if pos_cat == "EP":
                if pocket_pair or rng < 0.40 or decent:
                    return self._open(minimum_raise, stack, extra=(self.PREFLOP_PAIR_RAISE_EXTRA if pocket_pair else 0))
                return 0
            # MP slightly wider
            if pos_cat == "MP":
                if pocket_pair or decent or rng < 0.55:
                    return self._open(minimum_raise, stack, extra=(self.PREFLOP_PAIR_RAISE_EXTRA if pocket_pair else 0))
                return 0
            # LP: widest
            if pos_cat == "LP":
                if pocket_pair or decent or rng < 0.75:
                    return self._open(minimum_raise, stack, extra=(self.PREFLOP_PAIR_RAISE_EXTRA if pocket_pair else 0))
                return 0
            # Blinds: avoid bloating pot OOP unless strong
            if pos_cat == "BLIND":
                if pocket_pair or decent or rng < 0.30:
                    return self._open(minimum_raise, stack, extra=(self.PREFLOP_PAIR_RAISE_EXTRA if pocket_pair else 0))
                return 0

        # Facing a raise
        cheap_call_limit = self._cheap_call_limit(stack)
        price_ok = self._price_ok(to_call, pot)

        # 3-bet policy (tight, position-weighted)
        if pocket_pair and to_call <= stack and minimum_raise > 0:
            # 3-bet more in LP; in EP/MP use RNG gate
            want_3bet = (pos_cat == "LP" and rng < 0.60) or (pos_cat in ("MP", "EP") and rng < 0.35)
            if want_3bet:
                return self._legal_raise(to_call, minimum_raise, stack, bump=minimum_raise)

        # Call policy
        if price_ok and to_call <= min(cheap_call_limit * (2 if pos_cat == "LP" else 1), stack):
            return to_call

        # Default: fold
        return 0

    # -----------------------------
    # POST-FLOP
    # -----------------------------
    def _postflop_plan(self, hole, board, texture, pocket_pair, decent,
                       to_call, minimum_raise, stack, pot, spr, rng,
                       have_blocker, blocker_type, multiway, exploit_vs_callers) -> int:
        """
        Balanced postflop lines:
        - Small c-bets on dry boards with range adv (approx via position & to_call==0).
        - Semi-bluff with draws; blocker-driven bluffs at lower freq.
        - Value bet larger on wet boards; polar overbet when SPR low and board favors nuts.
        - Mixed-frequency thin value.
        """

        # If nobody bet yet (to_call == 0): we are the aggressor/can stab
        if to_call == 0:
            # Value/protection when made hand
            if has_pair_with_board(hole, board) or pocket_pair:
                # Thin value frequency
                if rng < self.THIN_VALUE_FREQ:
                    target = self._size_from_pot(pot, self.VALUE_RAISE_FRAC, stack)
                    return self._legal_raise(0, self._min_raise_or_1(minimum_raise), stack, absolute=target)
                return 0

            # Bluff/semi-bluff: dry boards more often
            bluff_freq = self.BLUFF_FREQ_DRY if texture["dry"] else self.BLUFF_FREQ_WET
            if have_blocker:
                bluff_freq += 0.08  # extra weight with good blocker
            if rng < bluff_freq:
                # Small stab on dry, bigger on wet
                frac = self.CBET_SMALL_FRAC if texture["dry"] else self.VALUE_RAISE_FRAC
                target = self._size_from_pot(pot, frac, stack)
                return self._legal_raise(0, self._min_raise_or_1(minimum_raise), stack, absolute=target)
            return 0

        # Facing a bet: decide call/raise/fold
        price_ok = self._price_ok(to_call, pot)
        has_made = has_pair_with_board(hole, board) or pocket_pair

        # Check-raise value when strong and SPR healthy
        if has_made and rng < 0.35:
            small_value = self._size_from_pot(pot, self.VALUE_RAISE_FRAC, stack)
            return self._promote_raise(to_call, minimum_raise, stack, small_value)

        # Check-raise bluff with blockers at some freq
        if have_blocker and rng < self.XR_BLUFF_FREQ and not has_made:
            # Use board texture to pick size: wet→bigger; dry→smaller
            frac = self.VALUE_RAISE_FRAC if not texture["dry"] else self.CBET_SMALL_FRAC
            blf = self._size_from_pot(pot, frac, stack)
            return self._promote_raise(to_call, minimum_raise, stack, blf)

        # Overbet polarization when SPR is low and board heavily favors nutted region
        if has_made and self._polar_friendly(texture) and spr <= 3 and rng < 0.35:
            over = self._size_from_pot(pot, self.POLAR_OVERBET_FRAC, stack, cap_stack_frac=self.MAX_OVERBET_STACK_FRAC)
            return self._promote_raise(to_call, minimum_raise, stack, over)

        # Calls: priced-in or cheap peels
        cheap_call_limit = self._cheap_call_limit(stack)
        if price_ok or to_call <= cheap_call_limit:
            # Exploit: vs callers, take more calls with equity/marginals
            if exploit_vs_callers and (decent or has_made or texture["wet"]):
                return min(to_call, stack)
            # Base policy
            return min(to_call, stack)

        # Tighten vs big sizing when capped
        if self.TIGHTEN_VS_BIG_SIZING and not has_made and not have_blocker:
            return 0

        return 0

    # -----------------------------
    # Utilities / Features
    # -----------------------------
    @staticmethod
    def _to_int(x: Optional[Any]) -> int:
        try:
            v = int(x or 0)
            return max(0, v)
        except Exception:
            return 0

    @staticmethod
    def _street(board: list) -> str:
        n = len(board or [])
        if n == 0: return "preflop"
        if n <= 3: return "flop"
        if n == 4: return "turn"
        return "river"

    @staticmethod
    def _spr(stack: int, pot: int, to_call: int) -> float:
        eff = max(1, stack - to_call)
        base = max(1, pot + to_call)
        return eff / float(base)

    def _position_category(self, gs: Dict[str, Any], in_action: int) -> str:
        """Approximate: EP/MP/LP/BLIND based on dealer index."""
        dealer = int(gs.get('dealer', 0) or 0)
        players = gs.get('players') or []
        n = len(players)
        if n <= 0: return "EP"
        # relative seat from button
        rel = (in_action - dealer - 1) % n  # 0=Sb,1=BB,2=UTG...
        if rel == 0 or rel == 1:
            return "BLIND"
        # simple split
        if rel <= max(2, n // 3):
            return "EP"
        if rel <= max(3, (2 * n) // 3):
            return "MP"
        return "LP"

    def _board_texture(self, board: list) -> Dict[str, bool]:
        """Classify texture: dry, wet, paired, monotone, straighty."""
        suits = [c.get('suit') for c in (board or [])]
        ranks = [self._rank_to_int(c.get('rank')) for c in (board or []) if c.get('rank')]

        paired = len(ranks) != len(set(ranks))
        suit_counts = {}
        for s in suits:
            if not s: continue
            suit_counts[s] = suit_counts.get(s, 0) + 1
        monotone = any(cnt >= 3 for cnt in suit_counts.values())
        twotone = any(cnt == 2 for cnt in suit_counts.values())

        # crude straightiness: many connected ranks
        ranks = sorted(set(ranks))
        straighty = any(
            all((r + i) in ranks for i in range(3)) for r in ranks
        )  # any 3-seq on board

        dry = (not paired) and (not monotone) and (not straighty) and (not twotone)
        wet = monotone or straighty or (twotone and not paired)

        return {
            "paired": paired,
            "monotone": monotone,
            "twotone": twotone,
            "straighty": straighty,
            "dry": dry,
            "wet": wet
        }

    @staticmethod
    def _rank_to_int(r: Optional[str]) -> int:
        # Supports '2'-'10','J','Q','K','A'
        if not r: return 0
        mapping = {'J':11, 'Q':12, 'K':13, 'A':14}
        try:
            return int(r)
        except Exception:
            return mapping.get(r.upper(), 0)

    def _blocker_signal(self, hole: list, board: list, texture: Dict[str, bool]):
        """Detect classic blocker spots: ace-of-suit on monotone; broadway blockers on straighty."""
        if not hole:
            return False, None
        # Suited ace blocker on monotone boards
        if texture.get("monotone"):
            board_suits = [c.get('suit') for c in (board or [])]
            if len(board_suits) >= 3:
                mono_suit = max(set(board_suits), key=board_suits.count)
                for c in hole:
                    if c.get('suit') == mono_suit and c.get('rank') in ('A', 'K'):
                        return True, "flush_blocker"
        # Broadway blockers on straighty boards
        if texture.get("straighty"):
            hole_ranks = {self._rank_to_int(c.get('rank')) for c in hole}
            if 14 in hole_ranks or 13 in hole_ranks:
                return True, "straight_blocker"
        return False, None

    def _size_from_pot(self, pot: int, frac: float, stack: int, cap_stack_frac: float = 1.0) -> int:
        amt = int(max(1, pot * frac))
        cap = int(stack * cap_stack_frac)
        return max(1, min(amt, cap))

    def _min_raise_or_1(self, minimum_raise: int) -> int:
        return max(1, minimum_raise)

    def _open(self, minimum_raise: int, stack: int, extra: float = 0.0) -> int:
        base = int(self._min_raise_or_1(minimum_raise) * (1.0 + extra))
        return min(max(1, base), stack)

    def _legal_raise(self, to_call: int, minimum_raise: int, stack: int, bump: int = 0, absolute: Optional[int] = None) -> int:
        """Return a legal raise total chips to put in now."""
        if stack <= to_call:
            return min(to_call, stack)  # effectively all-in/call
        if minimum_raise <= 0:
            return min(to_call, stack)  # can't raise, so call/check

        legal_min = to_call + minimum_raise
        target = legal_min + (bump if bump else 0)
        if absolute is not None:
            target = max(legal_min, absolute)
        return min(max(legal_min, target), stack)

    def _promote_raise(self, to_call: int, minimum_raise: int, stack: int, target_total: int) -> int:
        if minimum_raise <= 0:
            return min(to_call, stack)
        legal_min = to_call + minimum_raise
        if stack < legal_min:
            return min(to_call, stack)
        return min(max(legal_min, target_total), stack)

    def _price_ok(self, to_call: int, pot: int) -> bool:
        if to_call <= 0:
            return True
        denom = pot + to_call
        if denom <= 0:
            return to_call <= 1
        return (to_call / float(denom)) <= self.POT_ODDS_CALL_THRESHOLD

    def _cheap_call_limit(self, stack: int) -> int:
        pct_cap = int(stack * self.CHEAP_CALL_STACK_PCT)
        return max(1, min(pct_cap, self.CHEAP_CALL_ABS_CAP))

    def _polar_friendly(self, texture: Dict[str, bool]) -> bool:
        # Paired boards and clear monotone/straighty boards are good for polarized pressure
        return texture.get("paired") or texture.get("monotone") or texture.get("straighty")

    def _table_looks_passive(self, players: list, gs: Dict[str, Any]) -> bool:
        """
        Very rough heuristic: if current_buy_in is still near small blind levels deep in betting,
        and pot sizes remain small multiway, bias towards value vs callers.
        """
        pot = self._to_int(gs.get('pot'))
        current_buy_in = self._to_int(gs.get('current_buy_in'))
        sb = self._to_int(gs.get('small_blind'))
        # passive if current_buy_in is not far above 4x sb and pot is multiway but modest
        return (current_buy_in <= max(4 * sb, 20)) and (pot <= 20 * sb if sb > 0 else pot <= 200)

    def _finalize(self, desired: int, to_call: int, minimum_raise: int, stack: int) -> int:
        desired = max(0, min(int(desired or 0), stack))
        if desired == 0:
            return 0
        if desired < to_call:
            return 0
        if desired == to_call:
            return desired
        if minimum_raise <= 0:
            return min(to_call, stack)
        legal_min = to_call + minimum_raise
        if desired < legal_min:
            return min(to_call, stack)
        return min(desired, stack)

    # Deterministic RNG in [0,1)
    def _rng(self, gs: Dict[str, Any], in_action: int) -> float:
        key = f"{gs.get('game_id','')}-{gs.get('round',0)}-{gs.get('bet_index',0)}-{in_action}"
        h = hashlib.sha256(key.encode('utf-8')).hexdigest()
        return int(h[:8], 16) / 0xFFFFFFFF
