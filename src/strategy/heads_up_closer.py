"""
leanpoker_hu_closer.py

Objective
---------
You said we reach heads-up but fail to close. This module provides a refined HU
strategy that toggles between two modes to convert more seconds into wins:

  • PRESS mode (we're chip leader, not covered): raise almost all BTN hands,
    widen 3-bet bluffs when deep, pressure flops/turns more.
  • CLOSE mode (we're even or behind OR covered): adopt a limp-heavy BTN mix
    (lower variance), value bet thinner but reduce big multi-street bluffs,
    and 3-bet bluff less out of position.

The rest of the table (3- or 4-handed) still uses the conservative Adaptive baseline.

Usage
-----
  from leanpoker_hu_closer import HeadsUpCloserStrategy
  s = HeadsUpCloserStrategy()
  bet = s.decide_bet(game_state)
"""

import random
from typing import List, Tuple, Dict, Any

from src.strategy.base import Strategy
from src.strategy.basic import BasicStrategy

# --------- Small helpers ---------
RANK_MAP = {r:i for i, r in enumerate("..23456789TJQKA")}  # '2'->2 ... 'A'->14

def rint(card_rank: str) -> int:
    return RANK_MAP.get(str(card_rank)[0], 0)

def parse_cards(cards: List[dict]) -> List[Tuple[int, str]]:
    out = []
    for c in cards or []:
        out.append((rint(c.get("rank", "")), (c.get("suit", "") or "")[:1]))
    return out

def is_pair(hole: List[dict]) -> bool:
    cs = parse_cards(hole)
    return len(cs) == 2 and cs[0][0] == cs[1][0] and cs[0][0] > 0

def both_high(hole: List[dict], threshold: int = 11) -> bool:
    cs = parse_cards(hole)
    return len(cs) == 2 and min(cs[0][0], cs[1][0]) >= threshold

def has_pair_with_board(hole: List[dict], board: List[dict]) -> bool:
    hs = parse_cards(hole); bs = parse_cards(board)
    if len(hs) != 2 or not bs:
        return False
    hranks = {hs[0][0], hs[1][0]}
    branks = {b[0] for b in bs}
    return len(hranks & branks) > 0


# ---------- Adaptive baseline ----------
class AdaptiveStrategy(Strategy):
    """
    Winner-takes-all chip-EV baseline; adapts to players-in-pot (n_in_pot)
    and players left in match (n_left). HU Closer overrides behavior at n_left == 2.
    """
    def decide_bet(self, game_state: Dict[str, Any]) -> int:
        try:
            G = self._g(game_state)
            if G["my_stack"] <= 0: return 0
            if G["effective_bb"] <= 10 and G["street"] == 0:
                return self._push_fold_preflop(G)
            return self._preflop_decision(G) if G["street"] == 0 else self._postflop_decision(G)
        except Exception:
            try: return BasicStrategy().decide_bet(game_state)
            except Exception: return 0

    def showdown(self, game_state: Dict[str, Any]) -> None:
        pass

    # ---- Preflop ----
    def _preflop_decision(self, G: Dict[str, Any]) -> int:
        K = self._knobs(G)
        bucket = self._hand_bucket(G["hole"])
        pos = self._norm_pos(G["position"], G["n_seats"])
        to_call = G["to_call"]
        ip = pos in ("CO", "BTN")
        facing_raise = to_call > G["bb"]

        # Limp isolation
        limpers = 0
        if G["street"] == 0 and G["current_buy_in"] == G["bb"]:
            for p in G["players"]:
                st = (p or {}).get("status", "active")
                if st == "active" and int((p or {}).get("bet", 0) or 0) == G["bb"]:
                    limpers += 1
            if pos != "BB" and limpers > 0:
                limpers -= 1

        open_size_bb = 2.1 if ip else 2.4
        iso_size_bb  = (3.5 if ip else 4.0) + 1.0 * max(0, limpers)

        if not facing_raise:
            if self._should_open(pos, bucket, K):
                desired_total = int(round((iso_size_bb if limpers > 0 else open_size_bb) * G["bb"]))
                return self._raise_to_amount(G, desired_total)
            if G["position"] == "BB" and self._bb_should_defend(bucket, K):
                return min(to_call, G["my_stack"])
            return 0

        if self._should_value_3bet(pos, bucket, K):
            factor = 2.8 if ip else 3.6
            desired_total = int(round(factor * G["current_buy_in"]))
            return self._raise_to_amount(G, desired_total)

        if self._should_bluff_3bet(pos, bucket, K, G["effective_bb"]):
            factor = 2.8 if ip else 3.6
            desired_total = int(round(factor * G["current_buy_in"]))
            return self._raise_to_amount(G, desired_total)

        if self._should_cold_call(pos, bucket, to_call, G, K):
            return min(to_call, G["my_stack"])

        cheap = min(G["my_stack"] // K["cheap_div"], max(1, G["bb"]))
        return min(to_call, G["my_stack"]) if to_call <= cheap else 0

    # ---- Postflop ----
    def _postflop_decision(self, G: Dict[str, Any]) -> int:
        K = self._knobs(G)
        hs = self._hand_strength_vs_board(G["hole"], G["board"], K)
        tex = self._board_texture(G["board"])
        to_call = G["to_call"]

        small_bb = 1.1 + 0.3 * K["AF"]
        mid_bb   = 2.5 + 0.6 * K["AF"]
        big_bb   = 3.6 + 0.6 * K["AF"]

        if to_call == 0:
            if hs["two_pair_plus"] or hs["overpair"] or hs["top_pair_for_value"]:
                want = big_bb if tex in ("wet", "dynamic") else mid_bb
                return self._bet_bb(G, want)
            if hs["strong_draw"]:
                if G["n_in_pot"] <= 2 or self._mix(G, K["draw_bet_freq"]):
                    want = mid_bb if tex != "dry" else small_bb
                    return self._bet_bb(G, want)
                return 0
            if tex == "dry" and G["n_in_pot"] == 2 and self._mix(G, K["stab_freq_hu"]):
                return self._bet_bb(G, small_bb)
            return 0

        call_cap = max(G["bb"], int(G["my_stack"] * K["call_cap_frac"]))
        if hs["two_pair_plus"] or hs["overpair"] or (hs["top_pair_for_value"] and tex != "wet"):
            desired_total = G["current_buy_in"] + int(round((2.3 if tex == "dry" else 2.6) * G["bb"]))
            r = self._raise_to_amount(G, desired_total)
            if r > to_call and r <= G["my_stack"] and (G["n_in_pot"] <= 3 or hs["two_pair_plus"] or hs["overpair"]):
                return r
            return min(to_call, G["my_stack"])
        if hs["strong_draw"]:
            if (G["n_in_pot"] == 2 and G["effective_bb"] > 22 and self._mix(G, K["draw_raise_freq"])) or                    (tex == "dry" and self._mix(G, K["draw_raise_freq"] * 0.6)):
                desired_total = G["current_buy_in"] + int(round(2.3 * G["bb"]))
                r = self._raise_to_amount(G, desired_total)
                if r > to_call and r <= G["my_stack"]:
                    return r
            return min(to_call, G["my_stack"]) if to_call <= max(call_cap, 2 * G["bb"]) else 0
        if hs["middle_pair"] or hs["weak_pair"]:
            thresh = max(G["bb"], int(G["my_stack"] * K["mpair_cap_frac"]))
            return min(to_call, G["my_stack"]) if (to_call <= thresh and tex != "wet" and G["n_in_pot"] <= 3) else 0
        cheap = max(1, G["bb"] // 2)
        return min(to_call, G["my_stack"]) if (tex == "dry" and to_call <= cheap and G["n_in_pot"] == 2) else 0

    # ---- Push/Fold (≤10bb in baseline) ----
    def _push_fold_preflop(self, G: Dict[str, Any]) -> int:
        K = self._knobs(G)
        bucket = self._hand_bucket(G["hole"])
        pos = self._norm_pos(G["position"], G["n_seats"])
        to_call = G["to_call"]

        premium = (bucket == 1); strong = (bucket == 2)
        small_pairs = (bucket == 4); suited_ace = (bucket == 5); broad_mid = (bucket == 3)

        jam_ok = False
        if pos in ("EP","UTG","MP","CO"):
            jam_ok = premium or strong or small_pairs or (suited_ace and G["n_left"] <= 3) or (broad_mid and G["n_left"] == 2)
        elif pos == "BTN":
            jam_ok = (bucket <= 6) or (bucket == 7 and G["n_left"] == 2)
        elif pos == "SB":
            jam_ok = True if G["n_left"] <= 3 else (premium or strong or small_pairs or suited_ace)
        elif pos == "BB":
            jam_ok = premium or strong or small_pairs or suited_ace or (broad_mid and G["n_left"] <= 3)

        facing_raise = to_call > G["bb"]
        if facing_raise and not (premium or strong):
            jam_ok = jam_ok and (G["effective_bb"] <= K["jam_face_raise_bb"])

        return G["my_stack"] if jam_ok else (min(to_call, G["my_stack"]) if to_call <= G["bb"] else 0)

    # ---- Buckets ----
    def _hand_bucket(self, hole: List[dict]) -> int:
        """
        1 Premium: AA–TT, AKs, AKo
        2 Strong: 99–77, AQs–ATs, AQo–AJo, KQs–KJs, QJs, JTs
        3 Broadway/Mid: KQo, KTo+, QTo+, JTo, T9s, 98s, 87s
        4 Small pairs: 66–22
        5 Suited aces: A9s–A2s
        6 Suited gappers/connectors lower: 97s–54s, 86s–64s, T8s, etc.
        7 Weak offsuit: K9o–K2o, Q9o–Q2o, J9o–J2o, etc.
        8 Trash
        """
        cs = parse_cards(hole)
        if len(cs) != 2 or min(cs[0][0], cs[1][0]) == 0:
            return 8
        r1, s1 = cs[0]; r2, s2 = cs[1]
        suited = s1 == s2; offsuit = not suited; pair = r1 == r2
        hi, lo = (max(r1, r2), min(r1, r2))

        if pair:
            if hi >= 10: return 1
            if 7 <= hi <= 9: return 2
            return 4
        if {hi, lo} == {14, 13}: return 1
        if {hi, lo} == {14, 12}: return 2
        if {hi, lo} == {14, 11}: return 2
        if suited and ((hi == 13 and lo in (12,11)) or (hi == 12 and lo == 11)):
            return 2
        if suited and hi == 14 and 2 <= lo <= 9:
            return 5
        if suited and (({hi, lo} in [{10,9},{9,8},{8,7}]) or abs(hi - lo) == 1 and hi >= 8):
            return 3
        if suited and hi >= 7 and 2 <= abs(hi - lo) <= 3:
            return 6
        if offsuit and ((hi in (13,12) and lo >= 10) or (hi == 11 and lo == 10)):
            return 3
        if offsuit and ((hi in (13,12,11)) and 2 <= lo <= 9):
            return 7
        return 8

    # ---- Knobs ----
    def _knobs(self, G: Dict[str, Any]) -> Dict[str, Any]:
        n_in = max(2, G["n_in_pot"])
        n_left = max(2, G["n_left"])
        AF = 1.0 if n_in == 2 else (0.8 if n_in == 3 else 0.65)
        hu = (n_left == 2); three = (n_left == 3)
        leader_bump = 0.10 if (G["am_chipleader"] and not G["am_covered"]) else 0.0
        return dict(
            AF=AF,
            p_open_loose = (0.42 if hu else 0.30 if three else 0.22) + leader_bump,
            p_3bet_bluff = (0.40 if hu else 0.22 if three else 0.15) + leader_bump/2,
            bb_defend_max_bucket = 7 if hu else (7 if three else 6),
            cheap_div = 45 if hu else (50 if three else 55),
            top_pair_kicker = 10 if hu else (11 if three else 12),
            stab_freq_hu = 0.62,
            draw_bet_freq = 0.55 if hu else (0.45 if three else 0.35),
            draw_raise_freq = 0.40 if hu else (0.28 if three else 0.18),
            call_cap_frac = 0.13 if hu else (0.10 if three else 0.08),
            mpair_cap_frac = 0.0625 if hu else (0.06 if three else 0.05),
            jam_face_raise_bb = 10 if hu else (8 if three else 7),
        )

    # ---- Utilities ----
    def _bet_bb(self, G: Dict[str, Any], bb_mult: float) -> int:
        target = int(round(bb_mult * G["bb"]))
        minr = max(1, G["minimum_raise"])
        amt = max(minr, target)
        return max(0, min(amt, G["my_stack"]))

    def _raise_to_amount(self, G: Dict[str, Any], desired_total: int) -> int:
        to_call = G["to_call"]
        minr = max(1, G["minimum_raise"])
        desired_extra = max(minr, desired_total - G["current_buy_in"])
        bet = to_call + desired_extra
        return max(0, min(bet, G["my_stack"]))

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

        big_blind = int(S.get("big_blind", 0) or 0)
        small_blind = int(S.get("small_blind", 0) or 0)
        bb_guess = big_blind if big_blind > 0 else max(2 * small_blind, 1)

        to_call = max(0, current_buy_in - my_bet)

        opp_stacks = [int(p.get("stack", 0) or 0) for i, p in enumerate(players) if i != in_action]
        covered = max(opp_stacks) if opp_stacks else my_stack
        effective_stack = min(my_stack, covered)
        effective_bb = max(1, effective_stack // max(1, bb_guess))

        status = lambda p: (p or {}).get("status", "active")
        n_in_pot = sum(1 for p in players if status(p) == "active")
        n_left   = sum(1 for p in players if status(p) != "out")
        n_seats  = len(players)

        position = self._position(S, in_action)
        street = len(board)

        am_chipleader = my_stack >= max([my_stack] + opp_stacks)
        am_covered = any(os > my_stack for os in opp_stacks)

        seed = S.get("round", None)
        if seed is None:
            seed = (S.get("dealer", 0) or 0) * 131 + in_action * 17
        random.seed(seed)

        return dict(
            players=players, me=me, hole=hole, board=board,
            current_buy_in=current_buy_in, minimum_raise=minimum_raise,
            my_bet=my_bet, my_stack=my_stack, to_call=to_call,
            bb=bb_guess, effective_bb=effective_bb,
            n_in_pot=n_in_pot, n_left=n_left, n_seats=n_seats,
            position=position, street=street,
            am_chipleader=am_chipleader, am_covered=am_covered,
            dealer=S.get("dealer", 0) or 0
        )

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
        if n_seats <= 4 and pos == "UTG": return "CO"
        return pos

    def _mix_seed(self, p: float) -> bool:
        return random.random() < p

    def _mix(self, G: Dict[str, Any], p: float) -> bool:
        random.seed(G.get("dealer", 0) * 1337 + G.get("current_buy_in", 0) * 7 + G.get("my_bet", 0))
        return random.random() < p

# ---------- Heads-Up Closer ----------
class HeadsUpCloserStrategy(AdaptiveStrategy):
    """
    HU finisher with PRESS/CLOSE modes.
    - PRESS: when stack_lead >= 1.25x and not covered.
    - CLOSE: otherwise (trailing/even/covered). Uses BTN limp-heavy plan at <=25bb.

    Also fixes HU mapping: dealer=BTN, opponent=BB.
    """
    def _position(self, S: Dict[str, Any], me_idx: int) -> str:
        n = len(S.get("players", []) or [])
        if n == 0: return "EP"
        dealer = int(S.get("dealer", 0) or 0)
        if n == 2:
            return "BTN" if me_idx == dealer else "BB"
        return super()._position(S, me_idx)

    # Role inference
    def _role(self, G: Dict[str, Any]) -> str:
        # Find opponent stack in HU
        opp_stack = 0
        for p in G["players"]:
            if p is not G["me"] and (p or {}).get("status","active") != "out":
                opp_stack = int((p or {}).get("stack", 0) or 0)
                break
        lead_ratio = (G["my_stack"] / max(1, opp_stack)) if opp_stack else 1.0
        press = (lead_ratio >= 1.25) and (not G["am_covered"])
        return "PRESS" if press else "CLOSE"

    # Tighter/looser HU knobs by role
    def _knobs(self, G: Dict[str, Any]) -> Dict[str, Any]:
        K = super()._knobs(G)
        if G["n_left"] != 2:
            return K
        role = self._role(G)
        K = dict(K)
        if role == "PRESS":
            K["p_open_loose"] = 0.92
            K["p_3bet_bluff"] = 0.60 if G["effective_bb"] >= 20 else 0.42
            K["bb_defend_max_bucket"] = 7
            K["cheap_div"] = 44
            K["AF"] = 1.08
            K["stab_freq_hu"] = 0.70
            K["draw_bet_freq"] = 0.62
            K["draw_raise_freq"] = 0.50 if G["effective_bb"] >= 22 else 0.35
            K["top_pair_kicker"] = 9
            K["call_cap_frac"] = 0.15
            K["mpair_cap_frac"] = 0.07
            K["jam_face_raise_bb"] = 11
        else:  # CLOSE
            K["p_open_loose"] = 0.55  # still opening a lot, but not auto-raising trash
            K["p_3bet_bluff"] = 0.25 if G["effective_bb"] >= 20 else 0.12
            K["bb_defend_max_bucket"] = 7
            K["cheap_div"] = 42
            K["AF"] = 0.96
            K["stab_freq_hu"] = 0.64
            K["draw_bet_freq"] = 0.55
            K["draw_raise_freq"] = 0.28
            K["top_pair_kicker"] = 10  # a touch tighter value cutoffs
            K["call_cap_frac"] = 0.13
            K["mpair_cap_frac"] = 0.062
            K["jam_face_raise_bb"] = 10
        return K

    # Decide bet with HU-specific short-stack extension (≤15bb)
    def decide_bet(self, game_state: Dict[str, Any]) -> int:
        G = self._g(game_state)
        if G["my_stack"] <= 0:
            return 0
        if G["n_left"] == 2 and G["street"] == 0 and G["effective_bb"] <= 15:
            return self._hu_push_fold_preflop(G)
        # Otherwise, use our customized pre/postflop including BTN limp logic
        return self._preflop_decision(G) if G["street"] == 0 else self._postflop_decision(G)

    # HU Preflop: add BTN limp-heavy behavior in CLOSE mode at <=25bb
    def _preflop_decision(self, G: Dict[str, Any]) -> int:
        if G["n_left"] == 2:
            pos = self._position({"players": G["players"], "dealer": G["dealer"]}, G["players"].index(G["me"]) if G["me"] in G["players"] else 0)
            role = self._role(G)
            bucket = self._hand_bucket(G["hole"])
            to_call = G["to_call"]
            bb = G["bb"]

            # BTN logic
            if pos == "BTN":
                # Facing no raise (first to act HU): choose raise or limp
                if to_call <= bb:  # normal HU preflop state
                    # PRESS: raise almost everything to 2.0bb
                    if role == "PRESS" or G["effective_bb"] > 25:
                        desired_total = int(round(2.0 * bb))
                        return self._raise_to_amount(G, desired_total)
                    # CLOSE: mixed strategy at <=25bb to cut variance
                    # Raise strong & playable; limp the rest; occasional limp-raise trap
                    strong = bucket in (1,2,3) or (bucket == 4 and G["effective_bb"] <= 25)
                    if strong:
                        desired_total = int(round(2.0 * bb))
                        return self._raise_to_amount(G, desired_total)
                    else:
                        # Limp (call) most others
                        return min(to_call, G["my_stack"])
                # If villain limped (rare in this engine), iso raise small
                else:
                    desired_total = int(round(3.5 * bb))
                    return self._raise_to_amount(G, desired_total)

            # BB logic
            else:  # pos == "BB"
                # If BTN min-raises (to 2bb): to_call == bb
                if to_call == bb:
                    # Defend extremely wide; 3-bet bluff some in PRESS, less in CLOSE
                    if self._mix_seed(0.18 if role == "CLOSE" else 0.28) and G["effective_bb"] >= 20 and bucket in (5,6,3):
                        # 3-bet to ~6bb
                        desired_total = int(round(6.0 * bb))
                        return self._raise_to_amount(G, desired_total)
                    return min(to_call, G["my_stack"])
                # Larger open sizes: tighten
                if to_call > bb:
                    # Value 3-bet premiums; otherwise call if hand decent and price ok
                    if bucket in (1,2):
                        desired_total = int(round((3.2 if to_call <= 2*bb else 3.8) * G["current_buy_in"]))
                        return self._raise_to_amount(G, desired_total)
                    cheap = min(G["my_stack"] // (48 if role == "PRESS" else 52), max(1, bb))
                    return min(to_call, G["my_stack"]) if to_call <= cheap else 0

        # Non-HU fallback to baseline
        return super()._preflop_decision(G)

    # HU-specific push/fold up to 15bb
    def _hu_push_fold_preflop(self, G: Dict[str, Any]) -> int:
        bucket = self._hand_bucket(G["hole"])
        pos = self._position({"players": G["players"], "dealer": G["dealer"]}, G["players"].index(G["me"]) if G["me"] in G["players"] else 0)
        to_call = G["to_call"]
        eff = G["effective_bb"]

        premium = (bucket == 1); strong = (bucket == 2)
        small_pairs = (bucket == 4); suited_ace = (bucket == 5); broad_mid = (bucket == 3)

        jam_ok = False
        if pos == "BTN":
            jam_ok = premium or strong or small_pairs or suited_ace or (broad_mid and eff <= 12)
        else:  # BB
            jam_ok = premium or strong or small_pairs or suited_ace or (broad_mid and eff <= 12)

        facing_raise = to_call > G["bb"]
        if facing_raise and not (premium or strong):
            jam_ok = jam_ok and (eff <= 11)

        return G["my_stack"] if jam_ok else (min(to_call, G["my_stack"]) if to_call <= G["bb"] else 0)

    # Postflop: modest tweaks by role
    def _postflop_decision(self, G: Dict[str, Any]) -> int:
        if G["n_left"] == 2:
            K = self._knobs(G)
            hs = self._hand_strength_vs_board(G["hole"], G["board"], K)
            tex = self._board_texture(G["board"])
            to_call = G["to_call"]
            role = self._role(G)

            # Size bands
            small_bb = 1.0 + 0.3 * K["AF"]
            mid_bb   = 2.3 + 0.6 * K["AF"]
            big_bb   = 3.4 + 0.6 * K["AF"]

            if to_call == 0:
                # Value bets (thinner in PRESS, slightly tighter in CLOSE)
                if hs["two_pair_plus"] or hs["overpair"] or hs["top_pair_for_value"]:
                    want = big_bb if tex in ("wet", "dynamic") else mid_bb
                    return self._bet_bb(G, want)

                # Draws: PRESS bets more often; CLOSE checks more IP
                if hs["strong_draw"]:
                    freq = 0.68 if role == "PRESS" else 0.52
                    if self._mix(G, freq):
                        want = mid_bb if tex != "dry" else small_bb
                        return self._bet_bb(G, want)
                    return 0

                # Stabs: PRESS higher; CLOSE moderate
                stab_freq = 0.72 if role == "PRESS" else 0.60
                if tex == "dry" and self._mix(G, stab_freq):
                    return self._bet_bb(G, small_bb)
                return 0

            # Facing a bet
            call_cap = max(G["bb"], int(G["my_stack"] * (0.16 if role == "PRESS" else 0.12)))

            if hs["two_pair_plus"] or hs["overpair"] or (hs["top_pair_for_value"] and tex != "wet"):
                desired_total = G["current_buy_in"] + int(round((2.2 if tex == "dry" else 2.5) * G["bb"]))
                r = self._raise_to_amount(G, desired_total)
                if r > to_call and r <= G["my_stack"]:
                    return r
                return min(to_call, G["my_stack"])

            if hs["strong_draw"]:
                # Semi-bluff raises: PRESS sometimes; CLOSE mostly call
                if role == "PRESS" and G["effective_bb"] > 20 and self._mix(G, 0.42):
                    desired_total = G["current_buy_in"] + int(round(2.2 * G["bb"]))
                    r = self._raise_to_amount(G, desired_total)
                    if r > to_call and r <= G["my_stack"]:
                        return r
                return min(to_call, G["my_stack"]) if to_call <= max(call_cap, 2 * G["bb"]) else 0

            if hs["middle_pair"] or hs["weak_pair"]:
                thresh = max(G["bb"], int(G["my_stack"] * (0.07 if role == "PRESS" else 0.06)))
                return min(to_call, G["my_stack"]) if (to_call <= thresh and tex != "wet") else 0

            # Air: peel cheap only
            cheap = max(1, G["bb"] // 2)
            return min(to_call, G["my_stack"]) if (tex == "dry" and to_call <= cheap) else 0

        # Non-HU fallback
        return super()._postflop_decision(G)


__all__ = ["Strategy", "BasicStrategy", "AdaptiveStrategy", "HeadsUpCloserStrategy"]

if __name__ == "__main__":
    s = HeadsUpCloserStrategy()
    # Quick HU demo
    demo = {
        "players": [
            {"id": 0, "name": "villain", "status": "active", "stack": 850, "bet": 0},
            {"id": 1, "name": "me", "status": "active", "stack": 1150, "bet": 0,
             "hole_cards": [{"rank":"9","suit":"spades"}, {"rank":"8","suit":"spades"}]},
        ],
        "in_action": 1,
        "dealer": 1,
        "small_blind": 5,
        "big_blind": 10,
        "current_buy_in": 10,
        "minimum_raise": 10,
        "community_cards": [],
        "round": 101,
    }
    print("HU Closer demo bet:", s.decide_bet(demo))
