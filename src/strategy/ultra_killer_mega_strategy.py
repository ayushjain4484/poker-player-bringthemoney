from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
import hashlib
import math

from .base import Strategy
from src.models.cards import is_pair, both_high, has_pair_with_board


# In-process opponent model (simple, ephemeral). Keys are f"{name}:{version}".
_OPP_MODEL: Dict[str, Dict[str, Any]] = {}
_HAND_CTX: Dict[str, Any] = {
    "hand_id": None,            # f"{game_id}:{round}"
    "preflop_flags": {},        # opp_key -> {"vpip": bool, "pfr": bool}
    "we_raised": False,         # whether we raised at any street this hand
}


def _opp_key(p: Dict[str, Any]) -> str:
    return f"{(p or {}).get('name','?')}:{(p or {}).get('version','?')}"


def _ewma_update(prev: float, obs: float, window: int = 10) -> float:
    # classic EWMA smoothing; window ~ 10 hands as requested
    alpha = 2.0 / (window + 1.0)
    if math.isnan(prev) or prev < 0:
        return obs
    return prev + alpha * (obs - prev)


class UltraKillerMegaStrategy(Strategy):
    """
    Exploit-forward strategy with lightweight opponent profiling.
    Assumptions:
    - Always 4 players at the table as per spec.
    - Opponent buckets by team name with calibrated thresholds.
    - Board-aware c-bet sizing: 33% on dry, 66% on wet.
    - EWMA tracking for VPIP/PFR/AF over ~10 hands; fold-to-raise tracked heuristically.
    Safety: always returns a non-negative int not exceeding our stack and obeys min-raise.
    """

    # Sizing and price gates
    CBET_DRY_FRAC = 0.33
    CBET_WET_FRAC = 0.66
    VALUE_FRAC = 0.45
    POT_ODDS_CALL_THRESHOLD = 0.27
    CHEAP_CALL_STACK_PCT = 0.02
    CHEAP_CALL_ABS_CAP = 50

    # Mixed frequencies (deterministic via _rng)
    FLOAT_FREQ_BASE = 0.45      # float vs small c-bets (higher vs passive postflop)
    XR_BLUFF_FREQ_BASE = 0.16   # XR bluffs when we have blockers
    THIN_VALUE_FREQ = 0.55

    # Opponent buckets (name contains...)
    NAME_BUCKETS = {
        "the real donkey killers": {
            "style": ("loose", "passive_pre", "aggressive_post"),
            "raise_defense_threshold": 161.55769230769232,  # chips
            "af_baseline": 2.2,
            "float_more": True,
            "trap_more": True,
        },
        "the better donkey killers": {
            "style": ("semi_loose", "passive_pre", "passive_post"),
            "raise_defense_threshold": 175.86363636363637,
            "af_baseline": 0.9,
            "float_more": True,
            "trap_more": False,
        },
        "the donkey killers": {
            "style": ("loose", "passive_pre", "aggressive_post"),
            "raise_defense_threshold": 112.44444444444444,
            "af_baseline": 2.0,
            "float_more": True,
            "trap_more": True,
        },
    }

    def decide_bet(self, game_state: Dict[str, Any]) -> int:
        try:
            players = game_state.get("players") or []
            in_action = int(game_state.get("in_action", 0) or 0)
            me = players[in_action] if 0 <= in_action < len(players) else {}

            hole = me.get("hole_cards") or []
            board = game_state.get("community_cards") or []

            current_buy_in = self._to_int(game_state.get("current_buy_in"))
            minimum_raise = self._to_int(game_state.get("minimum_raise"))
            my_bet = self._to_int(me.get("bet"))
            my_stack = self._to_int(me.get("stack"))
            pot = self._to_int(game_state.get("pot"))
            sb = self._to_int(game_state.get("small_blind"))

            to_call = max(0, current_buy_in - my_bet)
            if my_stack <= 0:
                return 0

            # Deterministic RNG to mix frequencies.
            rng = self._rng(game_state, in_action)

            # Hand features and table state
            street = self._street(board)
            texture = self._board_texture(board)
            pocket_pair = is_pair(hole)
            decent = both_high(hole, threshold=12) or has_pair_with_board(hole, board)
            price_ok = self._price_ok(to_call, pot)
            cheap_call = to_call <= self._cheap_call_limit(my_stack)

            # Hand context & lightweight live tracking (per snapshot)
            self._maybe_reset_hand(game_state)
            self._observe_preflop(players, game_state)

            # Opponent profiling: aggregate active opponents on table now
            opp_infos = self._active_opponents(players, me)
            bucket_info = self._bucket_for_table(opp_infos)

            # Exploit toggles derived from profile
            af_avg = self._avg_stat(opp_infos, "af", default=bucket_info.get("af_baseline", 1.2))
            fold_to_raise = self._avg_stat(opp_infos, "fold_to_raise", default=0.35)
            float_freq = self.FLOAT_FREQ_BASE + (0.15 if bucket_info.get("float_more") else 0.0)
            xr_bluff_freq = self.XR_BLUFF_FREQ_BASE + (0.10 if fold_to_raise > 0.5 else 0.0)

            # Raise defense threshold versus the table mix
            raise_def_thr = bucket_info.get("raise_defense_threshold", 150.0)

            desired = 0

            if street == "preflop":
                # Simple preflop: pocket pairs raise; decent call/raise small; otherwise fold unless cheap
                if to_call == 0:
                    if pocket_pair or decent or rng < 0.6:
                        desired = self._open(minimum_raise, my_stack, extra=(0.5 if pocket_pair else 0.0))
                    else:
                        desired = 0
                else:
                    # 3-bet with pairs sometimes in position; otherwise call if price acceptable
                    if pocket_pair and rng < 0.45 and minimum_raise > 0:
                        desired = self._legal_raise(to_call, minimum_raise, my_stack, bump=minimum_raise)
                    elif price_ok or cheap_call:
                        desired = min(to_call, my_stack)
                    else:
                        desired = 0
            else:
                # Postflop
                has_made = has_pair_with_board(hole, board) or pocket_pair

                if to_call == 0:
                    # We're the aggressor or checked to. Choose c-bet size by texture.
                    frac = self.CBET_DRY_FRAC if texture["dry"] else self.CBET_WET_FRAC
                    # Thin value more often vs passive-callers
                    if has_made and rng < self.THIN_VALUE_FREQ:
                        target = self._size_from_pot(pot, max(frac, self.VALUE_FRAC), my_stack)
                        desired = self._legal_raise(0, self._min_raise_or_1(minimum_raise), my_stack, absolute=target)
                    else:
                        # Bluff frequency increases if fold_to_raise high
                        stab_freq = (0.30 if texture["dry"] else 0.18) + (0.12 if fold_to_raise > 0.5 else 0.0)
                        if rng < stab_freq:
                            target = self._size_from_pot(pot, frac, my_stack)
                            desired = self._legal_raise(0, self._min_raise_or_1(minimum_raise), my_stack, absolute=target)
                        else:
                            desired = 0
                else:
                    # Facing a bet: decide on call/raise/fold
                    if has_made:
                        # Value raise some of the time; size by texture
                        if rng < 0.35:
                            frac = self.VALUE_FRAC if not texture["dry"] else self.CBET_DRY_FRAC
                            target = self._size_from_pot(pot, frac, my_stack)
                            desired = self._promote_raise(to_call, minimum_raise, my_stack, target)
                        else:
                            desired = min(to_call, my_stack) if (price_ok or cheap_call) else min(to_call, my_stack)
                    else:
                        # Bluff-raise when fold_to_raise is high
                        if rng < xr_bluff_freq and self._has_blocker_signal(hole, board, texture):
                            frac = self.CBET_DRY_FRAC if texture["dry"] else self.VALUE_FRAC
                            target = self._size_from_pot(pot, frac, my_stack)
                            desired = self._promote_raise(to_call, minimum_raise, my_stack, target)
                        else:
                            # Float more vs small c-bets and passive fields
                            call_bias = float_freq
                            if af_avg > 2.0:
                                call_bias -= 0.12  # tighten bluff-catch vs high AF
                            if (price_ok or cheap_call) and rng < call_bias:
                                desired = min(to_call, my_stack)
                            else:
                                desired = 0

                    # Defense vs very large raises: cap with threshold guidance
                    if desired == min(to_call, my_stack) or desired == 0:
                        if to_call > raise_def_thr and not has_made and af_avg > 1.5:
                            desired = 0  # overfold to big pressure multiway per guidance

            # Record if we raised this hand for fold-to-raise updates at showdown
            if desired > to_call and minimum_raise > 0:
                _HAND_CTX["we_raised"] = True

            return self._finalize(desired, to_call, minimum_raise, my_stack)
        except Exception:
            return 0

    # Learning hook
    def showdown(self, game_state: Dict[str, Any]) -> None:
        try:
            hand_id = self._hand_id(game_state)
            if _HAND_CTX.get("hand_id") != hand_id:
                # Nothing tracked; just set baselines
                _HAND_CTX["hand_id"] = hand_id

            players = game_state.get("players") or []
            # Update EWMA stats for each opponent
            for p in players:
                if p is None:
                    continue
                if p.get("name") == self._my_name(players, game_state.get("in_action", 0)):
                    continue
                key = _opp_key(p)
                model = _OPP_MODEL.setdefault(key, {"vpip": 0.25, "pfr": 0.12, "af": 1.2, "fold_to_raise": 0.35, "hands": 0})
                flags = (_HAND_CTX.get("preflop_flags") or {}).get(key, {"vpip": False, "pfr": False})

                model["vpip"] = _ewma_update(float(model.get("vpip", 0.25)), 1.0 if flags.get("vpip") else 0.0)
                model["pfr"] = _ewma_update(float(model.get("pfr", 0.12)), 1.0 if flags.get("pfr") else 0.0)

                # Nudge AF toward bucket baseline for that opponent
                bucket = self._bucket_for_name(p.get("name", ""))
                af_base = bucket.get("af_baseline", 1.2)
                model["af"] = _ewma_update(float(model.get("af", 1.2)), float(af_base))

                # Fold-to-raise heuristic: if we raised this hand and they ended folded by showdown
                we_raised = bool(_HAND_CTX.get("we_raised"))
                folded = (p.get("status") == "folded")
                if we_raised and folded:
                    model["fold_to_raise"] = _ewma_update(float(model.get("fold_to_raise", 0.35)), 1.0)
                elif we_raised:
                    model["fold_to_raise"] = _ewma_update(float(model.get("fold_to_raise", 0.35)), 0.0)

                model["hands"] = int(model.get("hands", 0)) + 1

            # Reset per-hand context after showdown
            _HAND_CTX["hand_id"] = None
            _HAND_CTX["preflop_flags"] = {}
            _HAND_CTX["we_raised"] = False
        except Exception:
            # Be silent on learning errors
            pass

    # -----------------------------
    # Small utilities
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
        if n == 0:
            return "preflop"
        if n <= 3:
            return "flop"
        if n == 4:
            return "turn"
        return "river"

    @staticmethod
    def _rank_to_int(r: Optional[str]) -> int:
        if not r:
            return 0
        mapping = {'J': 11, 'Q': 12, 'K': 13, 'A': 14}
        try:
            return int(r)
        except Exception:
            return mapping.get(str(r).upper(), 0)

    def _board_texture(self, board: list) -> Dict[str, bool]:
        suits = [c.get('suit') for c in (board or [])]
        ranks = [self._rank_to_int(c.get('rank')) for c in (board or []) if c.get('rank')]

        paired = len(ranks) != len(set(ranks))
        suit_counts: Dict[str, int] = {}
        for s in suits:
            if not s:
                continue
            suit_counts[s] = suit_counts.get(s, 0) + 1
        monotone = any(cnt >= 3 for cnt in suit_counts.values())
        twotone = any(cnt == 2 for cnt in suit_counts.values())

        ranks = sorted(set(ranks))
        straighty = any(all((r + i) in ranks for i in range(3)) for r in ranks)

        dry = (not paired) and (not monotone) and (not straighty) and (not twotone)
        wet = monotone or straighty or (twotone and not paired)

        return {"paired": paired, "monotone": monotone, "twotone": twotone, "straighty": straighty, "dry": dry, "wet": wet}

    @staticmethod
    def _size_from_pot(pot: int, frac: float, stack: int) -> int:
        amt = int(max(1, pot * float(frac)))
        return max(1, min(amt, stack))

    @staticmethod
    def _min_raise_or_1(minimum_raise: int) -> int:
        return max(1, int(minimum_raise or 0))

    @staticmethod
    def _open(minimum_raise: int, stack: int, extra: float = 0.0) -> int:
        base = int(UltraKillerMegaStrategy._min_raise_or_1(minimum_raise) * (1.0 + float(extra)))
        return min(max(1, base), stack)

    @staticmethod
    def _legal_raise(to_call: int, minimum_raise: int, stack: int, bump: int = 0, absolute: Optional[int] = None) -> int:
        if stack <= to_call:
            return min(to_call, stack)
        if minimum_raise <= 0:
            return min(to_call, stack)
        legal_min = to_call + int(minimum_raise)
        target = legal_min + (int(bump) if bump else 0)
        if absolute is not None:
            target = max(legal_min, int(absolute))
        return min(max(legal_min, target), stack)

    @staticmethod
    def _promote_raise(to_call: int, minimum_raise: int, stack: int, target_total: int) -> int:
        if minimum_raise <= 0:
            return min(to_call, stack)
        legal_min = to_call + int(minimum_raise)
        if stack < legal_min:
            return min(to_call, stack)
        return min(max(legal_min, int(target_total)), stack)

    @staticmethod
    def _price_ok(to_call: int, pot: int) -> bool:
        if to_call <= 0:
            return True
        denom = pot + to_call
        if denom <= 0:
            return to_call <= 1
        return (to_call / float(denom)) <= UltraKillerMegaStrategy.POT_ODDS_CALL_THRESHOLD

    @staticmethod
    def _cheap_call_limit(stack: int) -> int:
        pct_cap = int(stack * UltraKillerMegaStrategy.CHEAP_CALL_STACK_PCT)
        return max(1, min(pct_cap, UltraKillerMegaStrategy.CHEAP_CALL_ABS_CAP))

    @staticmethod
    def _finalize(desired: int, to_call: int, minimum_raise: int, stack: int) -> int:
        desired = max(0, min(int(desired or 0), stack))
        if desired == 0:
            return 0
        if desired < to_call:
            return 0
        if desired == to_call:
            return desired
        if minimum_raise <= 0:
            return min(to_call, stack)
        legal_min = to_call + int(minimum_raise)
        if desired < legal_min:
            return min(to_call, stack)
        return min(desired, stack)

    # ---- Opponent model helpers ----
    def _maybe_reset_hand(self, gs: Dict[str, Any]) -> None:
        hid = self._hand_id(gs)
        if _HAND_CTX.get("hand_id") != hid:
            _HAND_CTX["hand_id"] = hid
            _HAND_CTX["preflop_flags"] = {}
            _HAND_CTX["we_raised"] = False

    @staticmethod
    def _hand_id(gs: Dict[str, Any]) -> str:
        return f"{gs.get('game_id','')}:{gs.get('round',0)}"

    @staticmethod
    def _my_name(players: list, in_action: int) -> str:
        try:
            me = players[in_action] if 0 <= in_action < len(players) else {}
            return str((me or {}).get('name', ''))
        except Exception:
            return ""

    def _observe_preflop(self, players: list, gs: Dict[str, Any]) -> None:
        # Track vpip/pfr flags based on current snapshot when still preflop
        board = gs.get('community_cards') or []
        if len(board) != 0:
            return
        current_buy_in = self._to_int(gs.get('current_buy_in'))
        min_raise = self._to_int(gs.get('minimum_raise'))
        sb = self._to_int(gs.get('small_blind'))
        for p in players:
            if not p:
                continue
            key = _opp_key(p)
            # init flags
            flags = _HAND_CTX.setdefault("preflop_flags", {}).setdefault(key, {"vpip": False, "pfr": False})
            bet = self._to_int(p.get('bet'))
            if bet > 0:
                flags["vpip"] = True
            # Crude PFR detection: someone put the table to a higher buy_in (bet equals current buy_in and current_buy_in > 2*sb)
            if current_buy_in > max(2 * sb, 20) and bet == current_buy_in and min_raise > 0:
                flags["pfr"] = True

    def _active_opponents(self, players: list, me: Dict[str, Any]):
        infos = []
        for p in players:
            if not p or p is me:
                continue
            if (p.get('status') or '') not in ('active', 'out', 'folded'):
                continue
            key = _opp_key(p)
            model = _OPP_MODEL.setdefault(key, {"vpip": 0.25, "pfr": 0.12, "af": 1.2, "fold_to_raise": 0.35, "hands": 0})
            bucket = self._bucket_for_name(p.get('name',''))
            infos.append({
                "key": key,
                "name": p.get('name',''),
                "model": model,
                "bucket": bucket,
            })
        return infos

    def _bucket_for_name(self, name: str) -> Dict[str, Any]:
        nm = (name or '').strip().lower()
        for key, conf in self.NAME_BUCKETS.items():
            if key in nm:
                return conf
        return {"raise_defense_threshold": 150.0, "af_baseline": 1.2, "float_more": False, "trap_more": False}

    def _bucket_for_table(self, opp_infos) -> Dict[str, Any]:
        # If multiple known buckets at table, pick the most aggressive postflop baseline (worst case)
        chosen = None
        for info in opp_infos:
            b = info.get("bucket") or {}
            if not chosen:
                chosen = b
            else:
                if float(b.get("af_baseline", 1.2)) > float(chosen.get("af_baseline", 1.2)):
                    chosen = b
        return chosen or {"raise_defense_threshold": 150.0, "af_baseline": 1.2, "float_more": False, "trap_more": False}

    @staticmethod
    def _avg_stat(opp_infos, field: str, default: float = 0.0) -> float:
        vals = []
        for info in opp_infos:
            v = info.get("model", {}).get(field)
            try:
                vals.append(float(v))
            except Exception:
                pass
        if not vals:
            return float(default)
        return sum(vals) / float(len(vals))

    def _has_blocker_signal(self, hole: list, board: list, texture: Dict[str, bool]) -> bool:
        # Light blocker proxy: Ace or King of board-dominant suit on monotone; broadway on straighty
        if not hole:
            return False
        if texture.get("monotone"):
            suits = [c.get('suit') for c in (board or [])]
            if len(suits) >= 3:
                mono = max(set(suits), key=suits.count)
                for c in hole:
                    if c.get('suit') == mono and (c.get('rank') in ('A','K')):
                        return True
        if texture.get("straighty"):
            ranks = set(self._rank_to_int(c.get('rank')) for c in hole)
            if 14 in ranks or 13 in ranks:
                return True
        return False

    # Deterministic RNG in [0,1)
    def _rng(self, gs: Dict[str, Any], in_action: int) -> float:
        key = f"{gs.get('game_id','')}-{gs.get('round',0)}-{gs.get('bet_index',0)}-{in_action}"
        h = hashlib.sha256(key.encode('utf-8')).hexdigest()
        return int(h[:8], 16) / 0xFFFFFFFF
