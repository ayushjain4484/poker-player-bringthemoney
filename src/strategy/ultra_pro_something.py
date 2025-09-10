from .base import Strategy
from typing import Any, Dict, Optional, Tuple
import hashlib

# Reuse your helpers if available
from src.models.cards import is_pair, both_high, has_pair_with_board


class KillerInstinctStrategy(Strategy):
    """
    KillerInstinctStrategy v2
    - GTO-ish core with exploit knobs; street/position/SPR/texture aware
    - Mixed frequencies via deterministic RNG (seeded by game_id/round/bet_index)
    - Lean Poker rules-correct raises (strictly > to_call + minimum_raise)
    - Always returns legal amounts: 0 (fold/check), call, or (re)raise/all-in
    - Short-stack push/fold layer for ≤ ~10 BB
    """

    # -------- Tunables --------
    # Calling discipline
    POT_ODDS_CALL_THRESHOLD = 0.27       # call if price <= 27%
    CHEAP_CALL_STACK_PCT = 0.02          # ~2% stack qualifies as cheap peel
    CHEAP_CALL_ABS_CAP = 50              # absolute cap for cheap peel (chips)

    # Preflop ranges / sizes (approx; position- & stack-adjusted)
    PREFLOP_OPEN_MIN_RAISE_MULT = 1.0    # open baseline (min-raise * this)
    PREFLOP_PAIR_RAISE_EXTRA = 0.5       # add 0.5x min-raise for pocket pairs when deep
    JAM_BB_THRESHOLD = 10                # push/fold mode if effective stack <= 10 BB

    # Postflop sizings
    CBET_SMALL_FRAC = 0.33               # 1/3 pot c-bet on dry boards
    VALUE_RAISE_FRAC = 0.45              # ~45% pot for thin/merge raises
    POLAR_OVERBET_FRAC = 1.2             # ~120% pot overbet in polar spots (when legal)
    MAX_OVERBET_STACK_FRAC = 1.0         # never exceed all-in

    # Mixed frequencies (probabilities)
    BLUFF_FREQ_DRY = 0.35
    BLUFF_FREQ_WET = 0.20
    XR_BLUFF_FREQ = 0.18                 # check-raise bluff freq with key blockers
    THIN_VALUE_FREQ = 0.55

    # Exploit knobs
    TIGHTEN_VS_BIG_SIZING = True         # fold more when price is bad and we’re capped
    PUNISH_PASSIVES = True               # add extra thin value vs callers (heuristic)

    # -------------- Public entrypoint --------------
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
            small_blind = self._to_int(game_state.get('small_blind'))
            big_blind = max(1, small_blind * 2)

            to_call = max(0, current_buy_in - my_bet)
            if my_stack <= 0:
                return 0  # busted

            # Deterministic RNG (reproducible across reruns of the same state)
            rng = self._rng(game_state, in_action)

            # Streets / position / stacks
            street = self._street(board)                            # preflop/flop/turn/river
            pos_cat = self._position_category(game_state, in_action)  # EP/MP/LP/BLIND
            spr = self._spr(my_stack, pot, to_call)
            eff_bb = self._eff_bb(my_stack, big_blind)

            # Hand & board features
            pocket_pair = is_pair(hole)
            decent = both_high(hole, threshold=12) or has_pair_with_board(hole, board)
            texture = self._board_texture(board)
            have_blocker, blocker_type = self._blocker_signal(hole, board, texture)

            # Opponent model (very light):
            opp_count = sum(1 for p in players if (p or {}).get('status') == 'active') or 2
            multiway = opp_count > 2
            exploit_vs_callers = self.PUNISH_PASSIVES and self._table_looks_passive(players, game_state)

            # ------ Decision skeleton ------
            desired = 0

            if street == "preflop":
                # Short‑stack jam layer first
                if eff_bb <= self.JAM_BB_THRESHOLD:
                    if self._is_preflop_jam_candidate(hole, pos_cat, rng):
                        # all-in (amount to add now is entire stack)
                        desired = my_stack
                    else:
                        # call if cheap / good price, otherwise fold
                        cheap_call_limit = self._cheap_call_limit(my_stack)
                        if self._price_ok(to_call, pot) or to_call <= cheap_call_limit:
                            desired = min(to_call, my_stack)
                        else:
                            desired = 0
                else:
                    desired = self._preflop_plan(
                        pocket_pair=pocket_pair,
                        decent=decent,
                        pos_cat=pos_cat,
                        to_call=to_call,
                        minimum_raise=minimum_raise,
                        stack=my_stack,
                        pot=pot,
                        spr=spr,
                        rng=rng,
                        multiway=multiway,
                        small_blind=small_blind
                    )
            else:
                desired = self._postflop_plan(
                    hole, board, texture, pocket_pair, decent,
                    to_call, minimum_raise, my_stack, pot, spr, rng,
                    have_blocker, blocker_type, multiway, exploit_vs_callers
                )

            # --- Legality + clamps ---
            return self._finalize(desired, to_call, minimum_raise, my_stack)

        except Exception:
            # Fail-safe: never crash the round
            return 0

    # -----------------------------
    # PRE-FLOP
    # -----------------------------
    def _preflop_plan(
        self, pocket_pair: bool, decent: bool, pos_cat: str,
        to_call: int, minimum_raise: int, stack: int, pot: int,
        spr: float, rng: float, multiway: bool, small_blind: int
    ) -> int:
        """
        Position-adjusted ranges with mixed frequencies.
        - EP: tight; raise mostly with pairs & strong broadways.
        - MP: add suited broadways, Axs.
        - LP: widen; include suited connectors/gappers at some freq when cheap.
        - Blinds: defend tighter OOP, avoid dominated offsuits unless cheap.
        - Multiway: fewer bluffs, prefer calls with playable hands when priced.
        """
        # Opening (nobody bet yet)
        if to_call == 0:
            if pos_cat == "EP":
                if pocket_pair or decent or rng < 0.40:
                    return self._open(minimum_raise, stack, extra=(self.PREFLOP_PAIR_RAISE_EXTRA if pocket_pair else 0))
                return 0
            if pos_cat == "MP":
                if pocket_pair or decent or rng < 0.55:
                    return self._open(minimum_raise, stack, extra=(self.PREFLOP_PAIR_RAISE_EXTRA if pocket_pair else 0))
                return 0
            if pos_cat == "LP":
                # Slightly tighter if many players left behind (multiway risk)
                gate = 0.68 if multiway else 0.75
                if pocket_pair or decent or rng < gate:
                    return self._open(minimum_raise, stack, extra=(self.PREFLOP_PAIR_RAISE_EXTRA if pocket_pair else 0))
                return 0
            # Blinds: mostly complete/defend with stronger hands
            if pos_cat == "BLIND":
                if pocket_pair or decent or rng < 0.30:
                    return self._open(minimum_raise, stack, extra=(self.PREFLOP_PAIR_RAISE_EXTRA if pocket_pair else 0))
                return 0

        # Facing a raise: potential 3-bet / squeeze
        cheap_call_limit = self._cheap_call_limit(stack)
        price_ok = self._price_ok(to_call, pot)

        # Tighter 3-bet policy; LP > MP > EP; squeeze less often multiway
        if pocket_pair and to_call <= stack and minimum_raise > 0:
            want_3bet = ((pos_cat == "LP" and rng < 0.58) or
                         (pos_cat == "MP" and rng < 0.35) or
                         (pos_cat == "EP" and rng < 0.28))
            if multiway:
                want_3bet = want_3bet and (rng < 0.25)
            if want_3bet:
                return self._legal_raise_strict(to_call, minimum_raise, stack, bump=minimum_raise)

        # Priced-in or cheap peel
        if price_ok and to_call <= stack:
            # call a bit more liberally in position
            limit = cheap_call_limit * (2 if pos_cat == "LP" else 1)
            if to_call <= max(limit, 1):
                return to_call
        # Default: fold
        return 0

    # -----------------------------
    # POST-FLOP
    # -----------------------------
    def _postflop_plan(
        self, hole, board, texture, pocket_pair, decent,
        to_call, minimum_raise, stack, pot, spr, rng,
        have_blocker, blocker_type, multiway, exploit_vs_callers
    ) -> int:
        """
        Balanced postflop lines:
        - Small c-bets on dry boards with range adv (approx via position & to_call==0).
        - Semi-bluff with draws; blocker-driven bluffs at lower freq.
        - Value bet larger on wet boards; polar overbet when SPR low & board favors nuts.
        - Mixed-frequency thin value.
        """

        # If nobody bet yet (to_call == 0): we can stab
        if to_call == 0:
            # Value/protection when made hand (pair+)
            if has_pair_with_board(hole, board) or pocket_pair:
                if rng < self.THIN_VALUE_FREQ:
                    target = self._size_from_pot(pot, self.VALUE_RAISE_FRAC, stack)
                    return self._legal_raise_strict(0, self._min_raise_or_1(minimum_raise), stack, absolute=target)
                return 0

            # Bluff/semi-bluff: dry boards more often, boost with blockers
            bluff_freq = self.BLUFF_FREQ_DRY if texture["dry"] else self.BLUFF_FREQ_WET
            if have_blocker:
                bluff_freq += 0.08
            if rng < bluff_freq:
                frac = self.CBET_SMALL_FRAC if texture["dry"] else self.VALUE_RAISE_FRAC
                target = self._size_from_pot(pot, frac, stack)
                return self._legal_raise_strict(0, self._min_raise_or_1(minimum_raise), stack, absolute=target)
            return 0

        # Facing a bet: decide call/raise/fold
        price_ok = self._price_ok(to_call, pot)
        has_made = has_pair_with_board(hole, board) or pocket_pair

        # Check-raise value when strong and SPR healthy
        if has_made and rng < 0.35:
            small_value = self._size_from_pot(pot, self.VALUE_RAISE_FRAC, stack)
            return self._promote_raise_strict(to_call, minimum_raise, stack, small_value)

        # Check-raise bluff with blockers at some freq
        if have_blocker and rng < self.XR_BLUFF_FREQ and not has_made:
            frac = self.VALUE_RAISE_FRAC if not texture["dry"] else self.CBET_SMALL_FRAC
            blf = self._size_from_pot(pot, frac, stack)
            return self._promote_raise_strict(to_call, minimum_raise, stack, blf)

        # Polar overbet when board texture allows & SPR low
        if has_made and self._polar_friendly(texture) and spr <= 3 and rng < 0.35:
            over = self._size_from_pot(pot, self.POLAR_OVERBET_FRAC, stack, cap_stack_frac=self.MAX_OVERBET_STACK_FRAC)
            return self._promote_raise_strict(to_call, minimum_raise, stack, over)

        # Calls: priced-in or cheap peels (widen vs passives)
        cheap_call_limit = self._cheap_call_limit(stack)
        if price_ok or to_call <= cheap_call_limit:
            if exploit_vs_callers and (decent or has_made or texture["wet"]):
                return min(to_call, stack)
            return min(to_call, stack)

        # Tighten vs big sizing when capped & no blockers
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

    @staticmethod
    def _eff_bb(stack: int, big_blind: int) -> float:
        if big_blind <= 0:
            return 999.0
        return stack / float(big_blind)

    def _position_category(self, gs: Dict[str, Any], in_action: int) -> str:
        """Approximate: EP/MP/LP/BLIND based on dealer index."""
        dealer = int(gs.get('dealer', 0) or 0)
        players = gs.get('players') or []
        n = len(players)
        if n <= 0: return "EP"
        # relative seat from button
        rel = (in_action - dealer - 1) % n  # 0=SB,1=BB,2=UTG...
        if rel == 0 or rel == 1:
            return "BLIND"
        # coarse buckets
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

        # crude straightiness: any 3-in-a-row on board
        ranks = sorted(set(ranks))
        straighty = any(all((r + i) in ranks for i in range(3)) for r in ranks)

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
        mapping = {'J': 11, 'Q': 12, 'K': 13, 'A': 14}
        if not r: return 0
        try:
            return int(r)
        except Exception:
            return mapping.get(str(r).upper(), 0)

    def _blocker_signal(self, hole: list, board: list, texture: Dict[str, bool]) -> Tuple[bool, Optional[str]]:
        """Detect classic blocker spots: ace-of-suit on monotone; broadway blockers on straighty."""
        if not hole:
            return False, None
        # Suited A/K blocker on monotone boards
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

    def _is_preflop_jam_candidate(self, hole: list, pos_cat: str, rng: float) -> bool:
        """
        Cheap, robust push/fold heuristic:
        - Jam all pocket pairs 9+ always; 7-8 at some freq.
        - Jam AKo/AQs+ always; AJo/KQo some freq (LP/MP more than EP).
        Uses only hole ranks (no external evaluator).
        """
        # Map ranks
        r = sorted([self._rank_to_int(c.get('rank')) for c in (hole or [])], reverse=True)
        if len(r) < 2:
            return False
        hi, lo = r[0], r[1]
        pair = hi == lo

        # Always jam strong pairs & big aces
        if pair and hi >= 9 + 2:  # pair >= 11 -> JJ+; treat 99-TT as frequent jams
            return True
        if pair and hi >= 9:      # 99, TT
            return rng < 0.8
        if hi == 14 and lo >= 12: # AKo, AQo+
            return True
        # Suited broadways & KQ/AJ mixing by position
        if both_high(hole, threshold=12):  # both >= Q
            gate = 0.7 if pos_cat in ("LP", "MP") else 0.45
            return rng < gate
        return False

    # ---------- Sizing helpers ----------
    def _size_from_pot(self, pot: int, frac: float, stack: int, cap_stack_frac: float = 1.0) -> int:
        amt = int(max(1, pot * frac))
        cap = int(stack * cap_stack_frac)
        return max(1, min(amt, cap))

    def _min_raise_or_1(self, minimum_raise: int) -> int:
        return max(1, minimum_raise)

    def _open(self, minimum_raise: int, stack: int, extra: float = 0.0) -> int:
        """
        Opening raise. On Lean Poker, a *raise* must be strictly greater than
        to_call + minimum_raise; when to_call==0 this means > minimum_raise.
        We therefore add +1 to avoid being treated as a call. (Legal clamps later.)
        """
        base = int(self._min_raise_or_1(minimum_raise) * (1.0 + extra))
        # +1 to ensure strictness; _finalize will cap to stack
        return min(max(1, base + 1), stack)

    # ---------- Raise legality (strict) ----------
    def _legal_raise_strict(self, to_call: int, minimum_raise: int, stack: int,
                            bump: int = 0, absolute: Optional[int] = None) -> int:
        """
        Return a *legal* raise amount to add now.
        Lean Poker requires strictly > to_call + minimum_raise for a raise.
        """
        if stack <= to_call:
            return min(to_call, stack)  # effectively call/all-in call
        if minimum_raise <= 0:
            return min(to_call, stack)  # cannot raise; call/check

        legal_min_exclusive = to_call + minimum_raise
        target = legal_min_exclusive + 1 + (bump if bump else 0)  # +1 enforces strictness
        if absolute is not None:
            target = max(legal_min_exclusive + 1, absolute)
        return min(max(legal_min_exclusive + 1, target), stack)

    def _promote_raise_strict(self, to_call: int, minimum_raise: int, stack: int, target_total: int) -> int:
        """
        Promote a call to a strict legal raise towards target_total.
        """
        if minimum_raise <= 0 or stack <= to_call:
            return min(to_call, stack)
        legal_min_exclusive = to_call + minimum_raise
        return min(max(legal_min_exclusive + 1, target_total), stack)

    # ---------- Price / peels ----------
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

    # ---------- Texture helpers ----------
    def _polar_friendly(self, texture: Dict[str, bool]) -> bool:
        # Paired or clearly monotone/straighty boards play well for polarized pressure
        return texture.get("paired") or texture.get("monotone") or texture.get("straighty")

    def _table_looks_passive(self, players: list, gs: Dict[str, Any]) -> bool:
        """
        Rough passive table heuristic: if current_buy_in is near SB levels late in betting
        and pots stay small multiway, bias toward value vs callers.
        """
        pot = self._to_int(gs.get('pot'))
        current_buy_in = self._to_int(gs.get('current_buy_in'))
        sb = self._to_int(gs.get('small_blind'))
        return (current_buy_in <= max(4 * sb, 20)) and (pot <= 20 * sb if sb > 0 else pot <= 200)

    def _finalize(self, desired: int, to_call: int, minimum_raise: int, stack: int) -> int:
        """
        Final guardrail: clamp 0..stack and ensure the engine treats the intent correctly:
        - If desired < to_call → fold (0) or we return 0.
        - If desired == to_call → call.
        - If desired > to_call, ensure it's strictly > to_call + minimum_raise for raises;
          otherwise fall back to a call.
        """
        desired = max(0, min(int(desired or 0), stack))
        if desired == 0:
            return 0
        if desired < to_call:
            return 0
        if desired == to_call:
            return desired

        # We are attempting a raise
        legal_min_exclusive = to_call + max(0, minimum_raise)
        if desired <= legal_min_exclusive:
            # Not a legal raise → degrade to call
            return min(to_call, stack)
        return desired

    # Deterministic RNG in [0,1)
    def _rng(self, gs: Dict[str, Any], in_action: int) -> float:
        key = f"{gs.get('game_id','')}-{gs.get('round',0)}-{gs.get('bet_index',0)}-{in_action}"
        h = hashlib.sha256(key.encode('utf-8')).hexdigest()
        return int(h[:8], 16) / 0xFFFFFFFF
