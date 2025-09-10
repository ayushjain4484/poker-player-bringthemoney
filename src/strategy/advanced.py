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

class AdvancedStrategy(Strategy):
    """
    Auto-adjusts for table size. On 4-max:
      - Wider opens (UTG plays closer to CO), SB steals more
      - Wider BB defend; more 3-bet (esp. blinds vs BTN)
      - Higher HU stab frequency on dry boards; thinner value with top pair
      - Slightly lighter call-down thresholds
    Always clamps to stack and respects minimum_raise.
    """

    def decide_bet(self, game_state: Dict[str, Any]) -> int:
        try:
            G = self._g(game_state)
            if G["my_stack"] <= 0:
                return 0
            if G["effective_bb"] <= 10 and G["street"] == 0:
                return self._push_fold_preflop(G)
            if G["street"] == 0:
                return self._preflop_decision(G)
            else:
                return self._postflop_decision(G)
        except Exception:
            try:
                return BasicStrategy().decide_bet(game_state)
            except Exception:
                return 0

    def showdown(self, game_state: Dict[str, Any]) -> None:
        pass

    # ------------ Preflop ------------
    def _preflop_decision(self, G: Dict[str, Any]) -> int:
        bucket = self._hand_bucket(G["hole"])
        pos = self._norm_pos(G["position"], G["n_seats"])
        to_call = G["to_call"]

        facing_raise = to_call > G["bb"]
        ip = pos in ("CO", "BTN")  # 4-max: UTG normalized to CO below
        open_size_bb = 2.2 if ip else 2.5

        # Unopened or limped pot
        if not facing_raise:
            if self._should_open(pos, bucket, G["is_4max"]):
                desired_total = int(round(open_size_bb * G["bb"]))
                return self._raise_to_amount(G, desired_total)
            # BB defend vs limp (check) / tiny price
            if G["position"] == "BB" and self._bb_should_defend(bucket, G["is_4max"]):
                return min(to_call, G["my_stack"])
            return 0

        # Facing an open
        if self._should_value_3bet(pos, bucket, G["is_4max"]):
            factor = 3.0 if ip else 4.0
            desired_total = int(round(factor * G["current_buy_in"]))
            return self._raise_to_amount(G, desired_total)

        if self._should_bluff_3bet(pos, bucket, G["is_4max"], G["effective_bb"]):
            factor = 3.0 if ip else 4.0
            desired_total = int(round(factor * G["current_buy_in"]))
            return self._raise_to_amount(G, desired_total)

        if self._should_cold_call(pos, bucket, to_call, G):
            return min(to_call, G["my_stack"])

        cheap = min(G["my_stack"] // 45 if G["is_4max"] else G["my_stack"] // 50, max(1, G["bb"]))
        return min(to_call, G["my_stack"]) if to_call <= cheap else 0

    # ------------ Postflop ------------
    def _postflop_decision(self, G: Dict[str, Any]) -> int:
        hs = self._hand_strength_vs_board(G["hole"], G["board"], G["is_4max"])
        tex = self._board_texture(G["board"])
        to_call = G["to_call"]

        # Size guides (converted to legal bets later)
        small_bb = 1.4 if G["is_4max"] else 1.2  # stab a touch more HU
        mid_bb   = 3.0
        big_bb   = 4.2

        if to_call == 0:
            if hs["two_pair_plus"] or hs["overpair"] or hs["top_pair_value"]:
                want = big_bb if tex in ("wet", "dynamic") else mid_bb
                return self._bet_bb(G, want)
            if hs["strong_draw"]:
                want = mid_bb if tex != "dry" else small_bb
                return self._bet_bb(G, want)
            # HU stab more on dry boards 4-max
            if tex == "dry" and G["n_alive"] <= 2 and self._mix(G, 0.70 if G["is_4max"] else 0.55):
                return self._bet_bb(G, small_bb)
            return 0

        # Facing a bet
        call_cap = max(G["bb"], G["my_stack"] // (8 if G["is_4max"] else 10))  # 12.5% vs 10% stack
        if hs["two_pair_plus"] or hs["overpair"] or (hs["top_pair_value"] and tex != "wet"):
            desired_total = G["current_buy_in"] + int(round((2.7 if tex != "dry" else 2.2) * G["bb"]))
            raise_amt = self._raise_to_amount(G, desired_total)
            return raise_amt if raise_amt > to_call and raise_amt <= G["my_stack"] else min(to_call, G["my_stack"])

        if hs["strong_draw"]:
            if self._mix(G, 0.50 if G["is_4max"] else 0.45) and G["effective_bb"] > 22:
                desired_total = G["current_buy_in"] + int(round(2.6 * G["bb"]))
                r = self._raise_to_amount(G, desired_total)
                if r > to_call and r <= G["my_stack"]:
                    return r
            return min(to_call, G["my_stack"]) if to_call <= max(call_cap, 2 * G["bb"]) else 0

        if hs["middle_pair"] or hs["weak_pair"]:
            thresh = max(G["bb"], G["my_stack"] // (16 if G["is_4max"] else 20))  # 6.25–5% stack
            return min(to_call, G["my_stack"]) if (to_call <= thresh and tex != "wet") else 0

        cheap = max(1, G["bb"] // 2)
        return min(to_call, G["my_stack"]) if (tex == "dry" and to_call <= cheap and G["n_alive"] <= 2) else 0

    # ------------ Push/Fold (≤10bb preflop) ------------
    def _push_fold_preflop(self, G: Dict[str, Any]) -> int:
        bucket = self._hand_bucket(G["hole"])
        pos = self._norm_pos(G["position"], G["n_seats"])
        to_call = G["to_call"]

        premium = (bucket == 1); strong = (bucket == 2)
        small_pairs = (bucket == 4); suited_ace = (bucket == 5); broad_mid = (bucket == 3)

        jam_ok = False
        if pos in ("EP", "UTG", "MP", "CO"):   # 4-max: UTG≈CO
            jam_ok = premium or strong or small_pairs or suited_ace or (G["is_4max"] and broad_mid)
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

        # Broadways with Ace
        if {hi, lo} == {14, 13}: return 1
        if {hi, lo} == {14, 12}: return 2
        if {hi, lo} == {14, 11}: return 2

        if suited and ((hi == 13 and lo in (12,11)) or (hi == 12 and lo == 11)):
            return 2
        if suited and hi == 14 and 2 <= lo <= 9:
            return 5

        # Suited connectors/gappers (mid+)
        if suited and (({hi, lo} in [{10,9},{9,8},{8,7}]) or abs(hi - lo) == 1 and hi >= 8):
            return 3
        if suited and hi >= 7 and 2 <= abs(hi - lo) <= 3:
            return 6

        # Offsuit broadway
        if offsuit and ((hi in (13,12) and lo >= 10) or (hi == 11 and lo == 10)):
            return 3
        if offsuit and ((hi in (13,12,11)) and 2 <= lo <= 9):
            return 7
        return 8

    # ------------ Range knobs (4-max aware) ------------
    def _should_open(self, pos: str, bucket: int, is4: bool) -> bool:
        # Normalize: in 4-max, UTG plays similar to CO
        if pos in ("UTG", "EP") and is4:
            # Open most playable hands; mix some weak offsuit
            return bucket <= 6 or (bucket == 7 and self._mix_seed(0.35))

        if pos in ("UTG", "EP"):
            return bucket in (1, 2) or (bucket in (3,4,5) and self._mix_seed(0.35))
        if pos in ("MP", "CO"):
            return bucket <= 6 or (bucket == 7 and self._mix_seed(0.25))
        if pos == "BTN":
            # 4-max BTN very wide
            return True if is4 else (bucket <= 7)
        if pos == "SB":
            # 4-max SB steals more
            return bucket <= 7 or (is4 and bucket == 8 and self._mix_seed(0.15))
        return False

    def _bb_should_defend(self, bucket: int, is4: bool) -> bool:
        return (bucket <= 7) or (is4 and self._mix_seed(0.5)) or (not is4 and self._mix_seed(0.25))

    def _should_value_3bet(self, pos: str, bucket: int, is4: bool) -> bool:
        return bucket == 1 or (bucket == 2 and pos in ("CO", "BTN", "SB"))

    def _should_bluff_3bet(self, pos: str, bucket: int, is4: bool, eff_bb: int) -> bool:
        if eff_bb <= 20:  # don’t punt shallow
            return False
        # 4-max: add a bit more bluffing from blinds & BTN
        if pos in ("BTN", "SB") and is4 and bucket in (5,6,3):
            return self._mix_seed(0.55)
        if pos == "BB" and is4 and bucket in (5,6,3,7):
            return self._mix_seed(0.45)
        return (bucket in (5,6,3) and pos in ("CO", "BTN", "SB") and self._mix_seed(0.40))

    def _should_cold_call(self, pos: str, bucket: int, to_call: int, G: Dict[str, Any]) -> bool:
        if to_call > G["my_stack"]:
            return False
        if pos in ("CO", "BTN"):
            return bucket in (2, 3, 4, 5, 6)
        if pos == "BB":
            return bucket <= 7  # still wide
        if pos == "SB":
            # 4-max: less flatting, more 3-bet or fold
            return (bucket in (2, 3, 4, 5) and not G["is_4max"] and self._mix_seed(0.4))
        return bucket in (2, 3, 4) and self._mix_seed(0.3)

    # ------------ Hand strength & board texture ------------
    def _hand_strength_vs_board(self, hole: List[dict], board: List[dict], is4: bool) -> Dict[str, bool]:
        hs = parse_cards(hole); bs = parse_cards(board)
        r1, r2 = (hs + [(0, ""), (0, "")])[:2]
        b_ranks = [b[0] for b in bs]
        top_b = max(b_ranks) if b_ranks else 0

        pair = r1[0] == r2[0] and r1[0] > 0
        overpair = pair and r1[0] > top_b and len(bs) >= 3

        pair_with_board = has_pair_with_board(hole, board)

        # Top pair value threshold: 4-max values top pair with **T+ kicker**
        top_pair_value = False
        if pair_with_board and b_ranks:
            my_high = max(r1[0], r2[0])
            kicker_ok = my_high >= (10 if is4 else 11)
            top_pair_value = (my_high == max(b_ranks)) and kicker_ok

        # Two pair + (crude)
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

        middle_pair = pair_with_board and not top_pair_value
        weak_pair = (pair and not overpair and len(bs) == 0) or (pair_with_board and max(r1[0], r2[0]) < (10 if is4 else 11))

        return {
            "overpair": overpair,
            "top_pair_value": top_pair_value,
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
            n_seats=n_seats, is_4max=(n_seats <= 4),
            position=position, street=street, dealer=S.get("dealer", 0) or 0
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
        # First to act preflop (4-max: this is UTG and plays like CO)
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