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


# ---------- Adaptive, less-aggressive strategy ----------
class AdaptiveStrategy(Strategy):
    """
    Conservative, player-count–adaptive LeanPoker strategy.

    Key ideas
    - Adjusts *by number of players still in the pot* (n_alive):
        HU (2): normal aggression, thinner value ok, more stabs.
        3-way: tighter opens/defends, fewer 3-bet bluffs, fewer stabs.
        4-way+: clearly conservative: multiway = value heavy, minimal bluffing.
    - Smaller 3-bet bluff frequencies and wider folds when out of position.
    - Postflop: multiway requires stronger value to bet/raise; draws prefer calling.
    - Always clamps to legal sizes and our stack.
    """

    # ------------ Public API ------------
    def decide_bet(self, game_state: Dict[str, Any]) -> int:
        try:
            G = self._g(game_state)
            if G["my_stack"] <= 0:
                return 0

            # ≤10bb: tighter push/fold than before (also scaled by table size)
            if G["effective_bb"] <= 10 and G["street"] == 0:
                return self._push_fold_preflop(G)

            return self._preflop_decision(G) if G["street"] == 0 else self._postflop_decision(G)

        except Exception:
            try:
                return BasicStrategy().decide_bet(game_state)
            except Exception:
                return 0

    def showdown(self, game_state: Dict[str, Any]) -> None:
        pass

    # ------------ Preflop ------------
    def _preflop_decision(self, G: Dict[str, Any]) -> int:
        K = self._knobs(G)  # aggression & thresholds from n_alive
        bucket = self._hand_bucket(G["hole"])
        pos = self._norm_pos(G["position"], G["n_seats"])
        to_call = G["to_call"]

        facing_raise = to_call > G["bb"]
        ip = pos in ("CO", "BTN")  # rough IP preflop
        open_size_bb = 2.1 if ip else 2.4  # slightly smaller than aggressive versions

        # Unopened (or limped) pot
        if not facing_raise:
            if self._should_open(pos, bucket, K):
                desired_total = int(round(open_size_bb * G["bb"]))
                return self._raise_to_amount(G, desired_total)

            # BB: defend only if hand is decent and price is small
            if G["position"] == "BB" and self._bb_should_defend(bucket, K):
                return min(to_call, G["my_stack"])
            return 0

        # Facing an open: value 3-bet; bluff 3-bet much rarer, esp. OOP
        if self._should_value_3bet(pos, bucket, K):
            factor = 2.8 if ip else 3.6
            desired_total = int(round(factor * G["current_buy_in"]))
            return self._raise_to_amount(G, desired_total)

        if self._should_bluff_3bet(pos, bucket, K, G["effective_bb"]):
            factor = 2.8 if ip else 3.6
            desired_total = int(round(factor * G["current_buy_in"]))
            return self._raise_to_amount(G, desired_total)

        # Conservative cold-calls (prefer IP & playable hands)
        if self._should_cold_call(pos, bucket, to_call, G, K):
            return min(to_call, G["my_stack"])

        cheap = min(G["my_stack"] // K["cheap_div"], max(1, G["bb"]))
        return min(to_call, G["my_stack"]) if to_call <= cheap else 0

    # ------------ Postflop ------------
    def _postflop_decision(self, G: Dict[str, Any]) -> int:
        K = self._knobs(G)
        hs = self._hand_strength_vs_board(G["hole"], G["board"], K)
        tex = self._board_texture(G["board"])
        to_call = G["to_call"]

        # Scaled target sizes (converted later to legal amounts)
        small_bb = 1.1 + 0.3 * K["AF"]   # ~1.1–1.4bb
        mid_bb   = 2.5 + 0.6 * K["AF"]   # ~2.5–3.1bb
        big_bb   = 3.6 + 0.6 * K["AF"]   # ~3.6–4.2bb

        # No bet to call
        if to_call == 0:
            # Value: require stronger hands as players increase; size up only on wet/dynamic
            if hs["two_pair_plus"] or hs["overpair"] or hs["top_pair_for_value"]:
                want = big_bb if tex in ("wet", "dynamic") else mid_bb
                return self._bet_bb(G, want)

            # Strong draws: semi-bluff less when multiway; favor check IP multiway on dynamic boards
            if hs["strong_draw"]:
                if G["n_alive"] <= 2 or self._mix(G, K["draw_bet_freq"]):
                    want = mid_bb if tex != "dry" else small_bb
                    return self._bet_bb(G, want)
                return 0

            # Stabs: much rarer multiway; modest HU on dry boards
            if tex == "dry" and G["n_alive"] <= 2 and self._mix(G, K["stab_freq_hu"]):
                return self._bet_bb(G, small_bb)
            return 0

        # Facing a bet: raise value, call more with price HU, fold more multiway
        call_cap = max(G["bb"], int(G["my_stack"] * K["call_cap_frac"]))  # % of stack cap

        # Value: raise more selectively multiway; otherwise at least call affordable
        if hs["two_pair_plus"] or hs["overpair"] or (hs["top_pair_for_value"] and tex != "wet"):
            desired_total = G["current_buy_in"] + int(round((2.3 if tex == "dry" else 2.6) * G["bb"]))
            r = self._raise_to_amount(G, desired_total)
            if r > to_call and r <= G["my_stack"] and (G["n_alive"] <= 3 or hs["two_pair_plus"] or hs["overpair"]):
                return r
            return min(to_call, G["my_stack"])

        # Draws: raise as a semi-bluff only HU and with depth; otherwise prefer call if cheap
        if hs["strong_draw"]:
            if (G["n_alive"] == 2 and G["effective_bb"] > 22 and self._mix(G, K["draw_raise_freq"])) or \
               (tex == "dry" and self._mix(G, K["draw_raise_freq"] * 0.6)):
                desired_total = G["current_buy_in"] + int(round(2.3 * G["bb"]))
                r = self._raise_to_amount(G, desired_total)
                if r > to_call and r <= G["my_stack"]:
                    return r
            return min(to_call, G["my_stack"]) if to_call <= max(call_cap, 2 * G["bb"]) else 0

        # Middle/weak pair: pot-control; fold more often multiway or vs big sizing
        if hs["middle_pair"] or hs["weak_pair"]:
            thresh = max(G["bb"], int(G["my_stack"] * K["mpair_cap_frac"]))
            return min(to_call, G["my_stack"]) if (to_call <= thresh and tex != "wet" and G["n_alive"] <= 3) else 0

        # Air/backdoors: only peel very cheap HU on dry boards
        cheap = max(1, G["bb"] // 2)
        return min(to_call, G["my_stack"]) if (tex == "dry" and to_call <= cheap and G["n_alive"] == 2) else 0

    # ------------ Push/Fold (≤10bb preflop) ------------
    def _push_fold_preflop(self, G: Dict[str, Any]) -> int:
        K = self._knobs(G)
        bucket = self._hand_bucket(G["hole"])
        pos = self._norm_pos(G["position"], G["n_seats"])
        to_call = G["to_call"]

        premium = (bucket == 1); strong = (bucket == 2)
        small_pairs = (bucket == 4); suited_ace = (bucket == 5); broad_mid = (bucket == 3)

        jam_ok = False
        # Slightly tighter with more players: 4-way remove weakest jams
        if pos in ("EP", "UTG", "MP", "CO"):
            jam_ok = premium or strong or small_pairs or (suited_ace and G["n_alive"] <= 3) or (broad_mid and G["n_alive"] == 2)
        elif pos == "BTN":
            jam_ok = (bucket <= 6) or (bucket == 7 and G["n_alive"] == 2)
        elif pos == "SB":
            jam_ok = True if G["n_alive"] <= 3 else (premium or strong or small_pairs or suited_ace)
        elif pos == "BB":
            jam_ok = premium or strong or small_pairs or suited_ace or (broad_mid and G["n_alive"] <= 3)

        facing_raise = to_call > G["bb"]
        if facing_raise and not (premium or strong):
            jam_ok = jam_ok and (G["effective_bb"] <= K["jam_face_raise_bb"])

        return G["my_stack"] if jam_ok else (min(to_call, G["my_stack"]) if to_call <= G["bb"] else 0)

    # ------------ Buckets ------------
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

    # ------------ Range knobs & decisions (use n_alive) ------------
    def _knobs(self, G: Dict[str, Any]) -> Dict[str, Any]:
        """
        Returns conservative parameters keyed off players currently active in the hand.
        """
        n = max(2, G["n_alive"])  # at least HU
        # Aggression factor
        AF = 1.0 if n == 2 else (0.8 if n == 3 else 0.65)

        return dict(
            AF=AF,
            # Preflop conservatism
            p_open_loose = 0.20 if n >= 4 else (0.28 if n == 3 else 0.35),
            p_3bet_bluff = 0.15 if n >= 4 else (0.25 if n == 3 else 0.35),
            bb_defend_max_bucket = 6 if n >= 4 else (7 if n == 3 else 7),
            cheap_div = 55 if n >= 4 else (50 if n == 3 else 45),
            # Postflop frequencies/thresholds
            top_pair_kicker = 12 if n >= 4 else (11 if n == 3 else 10),  # Q/J/T as min kicker
            stab_freq_hu = 0.60,        # only used HU
            draw_bet_freq = 0.35 if n >= 4 else (0.45 if n == 3 else 0.55),
            draw_raise_freq = 0.18 if n >= 4 else (0.28 if n == 3 else 0.40),
            call_cap_frac = 0.08 if n >= 4 else (0.10 if n == 3 else 0.125),
            mpair_cap_frac = 0.05 if n >= 4 else (0.06 if n == 3 else 0.0625),
            # Jam tightening when facing raise at short stacks
            jam_face_raise_bb = 7 if n >= 4 else (8 if n == 3 else 9),
        )

    def _should_open(self, pos: str, bucket: int, K: Dict[str, Any]) -> bool:
        # 4-max UTG ≈ CO ranges, but conservative knobs trim weakest opens
        if pos in ("UTG", "EP"):
            return bucket in (1,2,3) or (bucket in (4,5) and self._mix_seed(K["p_open_loose"]))
        if pos in ("MP", "CO"):
            return bucket <= 5 or (bucket == 6 and self._mix_seed(K["p_open_loose"]))
        if pos == "BTN":
            return bucket <= 6 or (bucket == 7 and self._mix_seed(K["p_open_loose"]))
        if pos == "SB":
            # SB opens trimmed: avoid most weak offsuit
            return bucket <= 6 or (bucket == 7 and self._mix_seed(K["p_open_loose"] * 0.6))
        return False

    def _bb_should_defend(self, bucket: int, K: Dict[str, Any]) -> bool:
        return bucket <= K["bb_defend_max_bucket"]

    def _should_value_3bet(self, pos: str, bucket: int, K: Dict[str, Any]) -> bool:
        return bucket == 1 or (bucket == 2 and pos in ("CO", "BTN"))

    def _should_bluff_3bet(self, pos: str, bucket: int, K: Dict[str, Any], eff_bb: int) -> bool:
        if eff_bb <= 22:  # avoid punting shallow
            return False
        if pos not in ("CO", "BTN", "SB"):  # OOP tighter
            return False
        return (bucket in (5,6,3)) and self._mix_seed(K["p_3bet_bluff"])

    def _should_cold_call(self, pos: str, bucket: int, to_call: int, G: Dict[str, Any], K: Dict[str, Any]) -> bool:
        if to_call > G["my_stack"]:
            return False
        if pos in ("CO", "BTN"):
            return bucket in (2,3,4,5,6)
        if pos == "BB":
            return bucket <= K["bb_defend_max_bucket"]
        if pos == "SB":
            # prefer 3-bet/fold from SB; flat only decent pockets/suited Broadway when cheap
            return bucket in (2,4) and to_call <= 2 * G["bb"] and self._mix_seed(0.35)
        return False

    # ------------ Hand strength & board texture ------------
    def _hand_strength_vs_board(self, hole: List[dict], board: List[dict], K: Dict[str, Any]) -> Dict[str, bool]:
        hs = parse_cards(hole); bs = parse_cards(board)
        r1, r2 = (hs + [(0, ""), (0, "")])[:2]
        b_ranks = [b[0] for b in bs]
        top_b = max(b_ranks) if b_ranks else 0

        pair = r1[0] == r2[0] and r1[0] > 0
        overpair = pair and r1[0] > top_b and len(bs) >= 3

        pair_with_board = has_pair_with_board(hole, board)

        # Top pair considered for value only with decent kicker (tighter multiway via K["top_pair_kicker"])
        top_pair_for_value = False
        if pair_with_board and b_ranks:
            my_high = max(r1[0], r2[0])
            kicker_ok = my_high >= K["top_pair_kicker"]
            top_pair_for_value = (my_high == max(b_ranks)) and kicker_ok

        # Two pair+ (crude)
        two_pair_plus = False
        if len(bs) >= 3:
            if pair and any(b == r1[0] for b in b_ranks):  # set
                two_pair_plus = True
            if not pair and pair_with_board:
                two_pair_plus = (r1[0] in b_ranks) and (r2[0] in b_ranks)

        # Draws
        suits = [s for _, s in bs]
        suit_count = {s: suits.count(s) for s in set(suits)}
        strong_fd = any(suit_count.get(s, 0) >= 2 for s in set([r1[1], r2[1]]))
        unique = sorted(set(b_ranks + [r1[0], r2[0]]))
        strong_oesd = False
        for i in range(len(unique) - 3):
            window = unique[i:i+4]
            if window[-1] - window[0] <= 4:
                strong_oesd = True; break
        strong_draw = strong_fd or strong_oesd

        middle_pair = pair_with_board and not top_pair_for_value
        weak_pair = (pair and not overpair and len(bs) == 0) or (pair_with_board and max(r1[0], r2[0]) < K["top_pair_kicker"])

        return {
            "overpair": overpair,
            "top_pair_for_value": top_pair_for_value,
            "two_pair_plus": two_pair_plus,
            "strong_draw": strong_draw,
            "middle_pair": middle_pair,
            "weak_pair": weak_pair,
        }

    def _board_texture(self, board: List[dict]) -> str:
        bs = parse_cards(board)
        if len(bs) < 3:
            return "dry"
        ranks = sorted({b[0] for b in bs})
        suits = [b[1] for b in bs]
        paired = len(ranks) < len(bs)
        flush2 = any(suits.count(s) >= 2 for s in set(suits))
        max_gap = max(ranks) - min(ranks) if ranks else 0
        if paired: return "paired"
        if flush2 or max_gap <= 4:
            return "wet" if flush2 and max_gap <= 3 else "dynamic"
        return "dry"

    # ------------ Sizing & plumbing ------------
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
        small_blind = int(S.get("small_blind", 0) or 0)

        bb_guess = max(2 * small_blind, minimum_raise, 1)
        to_call = max(0, current_buy_in - my_bet)

        opp_stacks = [int(p.get("stack", 0) or 0) for i, p in enumerate(players) if i != in_action]
        covered = max(opp_stacks) if opp_stacks else my_stack
        effective_stack = min(my_stack, covered)
        effective_bb = max(1, effective_stack // max(1, bb_guess))

        # Players *still in the hand* right now
        n_alive = sum(1 for p in players if (p or {}).get("status", "active") == "active")
        n_seats = len(players)

        position = self._position(S, in_action)
        street = len(board)

        seed = S.get("round", None)
        if seed is None:
            seed = (S.get("dealer", 0) or 0) * 131 + in_action * 17
        random.seed(seed)

        return dict(
            players=players, me=me, hole=hole, board=board,
            current_buy_in=current_buy_in, minimum_raise=minimum_raise,
            my_bet=my_bet, my_stack=my_stack, to_call=to_call,
            bb=bb_guess, effective_bb=effective_bb, n_alive=n_alive,
            n_seats=n_seats, position=position, street=street,
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
        # Map 4-max UTG ≈ CO for range decisions
        if n_seats <= 4 and pos == "UTG":
            return "CO"
        return pos

    # ------------ Mix helpers ------------
    def _mix_seed(self, p: float) -> bool:
        return random.random() < p

    def _mix(self, G: Dict[str, Any], p: float) -> bool:
        random.seed(G.get("dealer", 0) * 1337 + G.get("current_buy_in", 0) * 7 + G.get("my_bet", 0))
        return random.random() < p
