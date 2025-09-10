import random

from .base import Strategy
from typing import List, Tuple, Dict, Any

from .basic import BasicStrategy

# --------- Small helpers you likely already have somewhere ---------
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
    hs = parse_cards(hole)
    bs = parse_cards(board)
    if len(hs) != 2 or not bs:
        return False
    hranks = {hs[0][0], hs[1][0]}
    branks = {b[0] for b in bs}
    return len(hranks & branks) > 0

class AdvancedStrategy(Strategy):
    """
    LeanPoker Advanced Strategy:
    - Hand buckets (8 classes, all 169 combos covered).
    - Position-aware preflop (EP/MP/CO/BTN/SB/BB) with 40–100bb ranges and short-stack push/fold.
    - Defend/3-bet logic vs opens; simple BTN/SB/BB heuristics.
    - Postflop: basic texture read (dry/wet/paired) + value/draw/air buckets, with safe sizes.
    - Bet sizing: uses BB multiples & 'minimum_raise' to produce legal actions.
    - Always clamps to our stack; falls back conservatively when state is ambiguous.
    """

    # ------------ Public API ------------
    def decide_bet(self, game_state: Dict[str, Any]) -> int:
        try:
            G = self._g(game_state)
            if G["my_stack"] <= 0:
                return 0

            # Short-stack push/fold (≤10bb)
            if G["effective_bb"] <= 10 and G["street"] == 0:
                return self._push_fold_preflop(G)

            if G["street"] == 0:
                return self._preflop_decision(G)
            else:
                return self._postflop_decision(G)

        except Exception:
            # Super-safe fallback: mimic BasicStrategy behavior
            try:
                return BasicStrategy().decide_bet(game_state)
            except Exception:
                return 0

    def showdown(self, game_state: Dict[str, Any]) -> None:
        pass  # hook for logging/learning

    # ------------ Core logic ------------
    def _preflop_decision(self, G: Dict[str, Any]) -> int:
        bucket = self._hand_bucket(G["hole"])
        pos = G["position"]
        to_call = G["to_call"]

        # Estimate whether we're facing an open (to_call > BB) or unopened/limped pot (to_call <= BB)
        facing_raise = to_call > G["bb"]
        ip = pos in ("CO", "BTN")  # crude IP heuristic preflop
        open_size_bb = 2.2 if ip else 2.6

        # Open (first-in or vs limps)
        if not facing_raise:
            if self._should_open(pos, bucket):
                desired_total = int(round(open_size_bb * G["bb"]))
                return self._raise_to_amount(G, desired_total)
            else:
                # BB defend vs min-open/limp: allow cheap checks/calls with playable hands
                if pos == "BB" and self._bb_should_defend(bucket):
                    return min(to_call, G["my_stack"])
                # Otherwise fold/check
                return 0

        # Facing an open: decide 3-bet / call / fold
        if self._should_value_3bet(pos, bucket):
            # Target ~3x IP, ~4x OOP of the open (approx using current_buy_in as open size)
            factor = 3.0 if ip else 4.0
            desired_total = int(round(factor * G["current_buy_in"]))
            return self._raise_to_amount(G, desired_total)

        if self._should_bluff_3bet(pos, bucket):
            factor = 3.0 if ip else 4.0
            desired_total = int(round(factor * G["current_buy_in"]))
            # Don't punt shallow; require >20bb effective to bluff 3-bet
            if G["effective_bb"] > 20:
                return self._raise_to_amount(G, desired_total)

        # Cold-call set / suited connector / AQ–ATs etc mainly IP; BB defends wide vs small opens
        if self._should_cold_call(pos, bucket, to_call, G):
            return min(to_call, G["my_stack"])

        # Overfold small/offsuit trash unless cheap
        cheap = min(G["my_stack"] // 50, max(1, G["bb"]))  # ~2% stack or 1BB
        if to_call <= cheap:
            return min(to_call, G["my_stack"])
        return 0

    def _postflop_decision(self, G: Dict[str, Any]) -> int:
        # Basic hand/texture read
        hs = self._hand_strength_vs_board(G["hole"], G["board"])
        tex = self._board_texture(G["board"])
        to_call = G["to_call"]

        # Choose target "bb" size buckets (translated safely to legal amounts)
        small_bb = 1.2  # "33% pot"-ish stand-in
        mid_bb   = 2.8  # "60% pot"-ish stand-in
        big_bb   = 4.0  # "75–100%" stand-in (cap by stack)

        # When we can BET (no bet to call)
        if to_call == 0:
            if hs["two_pair_plus"] or hs["overpair"] or hs["top_pair_good"]:
                # Value: size up on wet; small on dry
                want = big_bb if tex in ("wet", "dynamic") else mid_bb
                return self._bet_bb(G, want)
            if hs["strong_draw"]:  # NFD / OESD / combo
                want = mid_bb if tex != "dry" else small_bb
                return self._bet_bb(G, want)
            if tex == "dry" and G["n_alive"] <= 2:
                # Small stab as c-bet equivalent
                return self._bet_bb(G, small_bb)
            return 0

        # FACING A BET: call/raise/fold heuristics
        call_cap = max(G["bb"], G["my_stack"] // 10)  # don't make huge loose calls
        if hs["two_pair_plus"] or hs["overpair"] or (hs["top_pair_good"] and tex != "wet"):
            # Prefer a raise with strong value if stacks allow
            desired_total = G["current_buy_in"] + int(round((2.5 if tex != "dry" else 2.0) * G["bb"]))
            raise_amt = self._raise_to_amount(G, desired_total)
            # If we can't legally raise much, at least call
            return raise_amt if raise_amt > to_call and raise_amt <= G["my_stack"] else min(to_call, G["my_stack"])

        if hs["strong_draw"]:
            # Semi-bluff sometimes (mix by seed), otherwise call if affordable
            if self._mix(G, 0.45) and G["effective_bb"] > 25:
                desired_total = G["current_buy_in"] + int(round(2.5 * G["bb"]))
                r = self._raise_to_amount(G, desired_total)
                if r > to_call and r <= G["my_stack"]:
                    return r
            return min(to_call, G["my_stack"]) if to_call <= max(call_cap, 2 * G["bb"]) else 0

        if hs["middle_pair"] or hs["weak_pair"]:
            # Pot control: call small bets IP; fold to big aggression, especially on wet boards
            thresh = max(G["bb"], G["my_stack"] // 20)  # ~5% stack or 1BB
            return min(to_call, G["my_stack"]) if to_call <= thresh and tex != "wet" else 0

        # Air/backdoors: fold unless very cheap and board is super dry
        cheap = max(1, G["bb"] // 2)
        return min(to_call, G["my_stack"]) if (tex == "dry" and to_call <= cheap and G["n_alive"] <= 2) else 0

    # ------------ Push/Fold (≤10bb preflop) ------------
    def _push_fold_preflop(self, G: Dict[str, Any]) -> int:
        bucket = self._hand_bucket(G["hole"])
        pos = G["position"]
        to_call = G["to_call"]

        # Rough jam sets by position (see writeup)
        jam_ok = False
        premium = bucket == 1
        strong  = bucket == 2
        small_pairs = bucket == 4
        suited_ace = bucket == 5
        broad_mid  = bucket == 3

        if pos in ("EP", "MP"):
            jam_ok = premium or strong or small_pairs or suited_ace
        elif pos == "CO":
            jam_ok = premium or strong or small_pairs or suited_ace or broad_mid
        elif pos == "BTN":
            jam_ok = True if bucket <= 6 else False
        elif pos == "SB":
            jam_ok = True  # very wide jamming SB when unopened
        elif pos == "BB":
            jam_ok = premium or strong or small_pairs or suited_ace or broad_mid

        # If someone already opened and we’re covered, be a tad tighter
        facing_raise = to_call > G["bb"]
        if facing_raise and not (premium or strong):
            jam_ok = jam_ok and (G["effective_bb"] <= 8)

        return G["my_stack"] if jam_ok else (min(to_call, G["my_stack"]) if to_call <= G["bb"] else 0)

    # ------------ Buckets & Ranges ------------
    def _hand_bucket(self, hole: List[dict]) -> int:
        """
        1 Premium: AA–TT, AKs, AKo
        2 Strong: 99–77, AQs–ATs, AQo–AJo, KQs–KJs, QJs, JTs
        3 Broadway/Mid: KQo, KTo+, QTo+, JTo, T9s, 98s, 87s
        4 Small pairs: 66–22
        5 Suited aces: A9s–A2s (non-premium portion)
        6 Suited gappers: 97s–54s, 86s–64s, T8s, etc.
        7 Weak offsuit: K9o–K2o, Q9o–Q2o, J9o–J2o, etc.
        8 Trash: rest
        """
        cs = parse_cards(hole)
        if len(cs) != 2 or min(cs[0][0], cs[1][0]) == 0:
            return 8
        r1, s1 = cs[0]
        r2, s2 = cs[1]
        suited = s1 == s2
        high1, high2 = max(r1, r2), min(r1, r2)
        offsuit = not suited
        pair = r1 == r2

        # Pairs
        if pair:
            if high1 >= 10: return 1  # TT+
            if 7 <= high1 <= 9: return 2  # 77-99
            if 2 <= high1 <= 6: return 4  # 22-66

        # AK / AQ / AJ
        if {high1, high2} == {14, 13}:  # AK
            return 1 if suited else 1  # AKs/AKo in premium by spec
        if {high1, high2} == {14, 12}:  # AQ
            return 2
        if {high1, high2} == {14, 11}:  # AJ
            return 2 if suited else 2

        # Strong suited broadways
        if suited and ((high1 == 13 and high2 in (12, 11)) or (high1 == 12 and high2 == 11)):  # KQ/KJ/QJ suited
            return 2

        # Suited aces
        if suited and high1 == 14 and 2 <= high2 <= 9:
            return 5 if high2 <= 9 else 2

        # Mid/Broadway & connectors/gappers
        def is_suited_connector(a, b): return suited and abs(a - b) == 1 and max(a, b) >= 8
        def is_suited_two_gap(a, b): return suited and 2 <= abs(a - b) <= 3 and max(a, b) >= 7
        if is_suited_connector(r1, r2) or (suited and {r1, r2} in [{10,9}, {9,8}, {8,7}]):
            return 3
        if is_suited_two_gap(r1, r2) or (suited and max(r1, r2) >= 8 and abs(r1 - r2) <= 3):
            return 6

        # Offsuit broadway
        if offsuit and ((high1 == 13 and high2 >= 10) or (high1 == 12 and high2 >= 10) or (high1 == 11 and high2 == 10)):
            return 3 if (high2 >= 10) else 7

        # Weak offsuit kings/queens/jacks
        if offsuit and ((high1 == 13 and 2 <= high2 <= 9) or (high1 == 12 and 2 <= high2 <= 9) or (high1 == 11 and 2 <= high2 <= 9)):
            return 7

        return 8

    def _should_open(self, pos: str, bucket: int) -> bool:
        # Conservative, implementable approximation of the table in the writeup
        if pos in ("UTG", "EP"):
            return bucket in (1, 2) or bucket in (4, 5, 3) and self._mix_seed(0.3)
        if pos == "MP":
            return bucket in (1, 2, 3) or (bucket in (4, 5) and self._mix_seed(0.5))
        if pos == "CO":
            return bucket in (1, 2, 3, 4, 5, 6) or (bucket == 7 and self._mix_seed(0.2))
        if pos == "BTN":
            return bucket <= 7  # raise almost everything except pure trash
        if pos == "SB":
            return bucket <= 6  # polarized; fewer weak offsuit opens
        if pos == "BB":
            return False
        return False

    def _bb_should_defend(self, bucket: int) -> bool:
        return bucket <= 6 or self._mix_seed(0.25)

    def _should_value_3bet(self, pos: str, bucket: int) -> bool:
        return bucket == 1 or (bucket == 2 and pos in ("CO", "BTN", "SB"))

    def _should_bluff_3bet(self, pos: str, bucket: int) -> bool:
        return (bucket in (5, 6, 3) and pos in ("CO", "BTN", "SB"))

    def _should_cold_call(self, pos: str, bucket: int, to_call: int, G: Dict[str, Any]) -> bool:
        if to_call > G["my_stack"]:
            return False
        if pos in ("CO", "BTN"):
            return bucket in (2, 3, 4, 5, 6)
        if pos == "BB":
            return bucket <= 7  # defend fairly wide
        if pos == "SB":
            return bucket in (2, 3, 4, 5) and self._mix_seed(0.4)
        return bucket in (2, 3, 4) and self._mix_seed(0.3)

    # ------------ Hand strength & board texture ------------
    def _hand_strength_vs_board(self, hole: List[dict], board: List[dict]) -> Dict[str, bool]:
        hs = parse_cards(hole)
        bs = parse_cards(board)
        r1, r2 = (hs + [(0, ""), (0, "")])[:2]
        b_ranks = [b[0] for b in bs]
        top_b = max(b_ranks) if b_ranks else 0

        pair = r1[0] == r2[0] and r1[0] > 0
        overpair = pair and r1[0] > top_b and len(bs) >= 3
        # Pair with board
        pair_with_board = has_pair_with_board(hole, board)
        top_pair_good = False
        if pair_with_board and b_ranks:
            my_high = max(r1[0], r2[0])
            top_pair_good = my_high == max(b_ranks) and my_high >= 11

        # Two pair+ detection
        two_pair_plus = False
        if len(bs) >= 3:
            if pair and any(b == r1[0] for b in b_ranks):  # set
                two_pair_plus = True
            # distinct ranks from hole pairing separate board ranks -> two pair
            if not pair and pair_with_board:
                # crude: if both hole ranks appear on board at least once
                two_pair_plus = (r1[0] in b_ranks) and (r2[0] in b_ranks)

        # Draws (rough)
        suits = [s for _, s in bs]
        suit_count = {s: suits.count(s) for s in set(suits)}
        board_flush2 = any(c >= 2 for c in suit_count.values())
        strong_fd = False
        if board_flush2:
            hole_suits = [s for _, s in hs]
            for s in set(hole_suits):
                if suit_count.get(s, 0) >= 2:
                    strong_fd = True
        # OESD rough: look for 4 unique within span<=4 including our hole ranks
        unique = sorted(set(b_ranks + [r1[0], r2[0]]))
        strong_oesd = False
        for i in range(len(unique) - 3):
            window = unique[i:i+4]
            if window[-1] - window[0] <= 4:
                strong_oesd = True
                break

        strong_draw = strong_fd or strong_oesd
        middle_pair = pair_with_board and not top_pair_good
        weak_pair = (pair and not overpair and len(bs) == 0) or (pair_with_board and max(r1[0], r2[0]) < 11)

        return {
            "overpair": overpair,
            "top_pair_good": top_pair_good,
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

        if paired:
            return "paired"
        if flush2 or max_gap <= 4:
            return "wet" if flush2 and max_gap <= 3 else "dynamic"
        return "dry"

    # ------------ Sizing & State plumbing ------------
    def _bet_bb(self, G: Dict[str, Any], bb_mult: float) -> int:
        # Convert "bb multiple" into a legal bet size for LeanPoker
        target = int(round(bb_mult * G["bb"]))
        minr = max(1, G["minimum_raise"])
        amt = max(minr, target)
        return max(0, min(amt, G["my_stack"]))

    def _raise_to_amount(self, G: Dict[str, Any], desired_total: int) -> int:
        """
        Raise 'to' desired_total (table stakes), converted into "chips to put in now".
        LeanPoker expects 'bet' = to_call + raise_extra, where raise_extra >= minimum_raise.
        """
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

        # Effective BB (approx using max opponent stack)
        opp_stacks = [int(p.get("stack", 0) or 0) for i, p in enumerate(players) if i != in_action]
        covered = max(opp_stacks) if opp_stacks else my_stack
        effective_stack = min(my_stack, covered)
        effective_bb = max(1, effective_stack // max(1, bb_guess))

        # Count active players
        n_alive = sum(1 for p in players if (p or {}).get("status", "active") == "active")

        # Position mapping
        position = self._position(S, in_action)

        # Street: 0 pre, 3 flop, 4 turn, 5 river
        street = len(board)

        # Seed for mixed strategies
        seed = S.get("round", None)
        if seed is None:
            seed = (S.get("dealer", 0) or 0) * 131 + in_action * 17
        random.seed(seed)

        return dict(
            players=players, me=me, hole=hole, board=board,
            current_buy_in=current_buy_in, minimum_raise=minimum_raise,
            my_bet=my_bet, my_stack=my_stack, to_call=to_call,
            bb=bb_guess, effective_bb=effective_bb, n_alive=n_alive,
            position=position, street=street, dealer=S.get("dealer", 0) or 0
        )

    def _position(self, S: Dict[str, Any], me_idx: int) -> str:
        n = len(S.get("players", []) or [])
        if n == 0:
            return "EP"
        dealer = int(S.get("dealer", 0) or 0)
        sb = (dealer + 1) % n
        bb = (dealer + 2) % n
        if me_idx == dealer:
            return "BTN"
        if me_idx == sb:
            return "SB"
        if me_idx == bb:
            return "BB"
        # Distance from dealer to approximate EP/MP/CO
        dist = (me_idx - dealer) % n
        if dist == 3: return "UTG" if n >= 6 else "EP"
        if dist == 4: return "MP"
        if dist == 5: return "CO"
        return "MP"

    # ------------ Mix helpers ------------
    def _mix_seed(self, p: float) -> bool:
        return random.random() < p

    def _mix(self, G: Dict[str, Any], p: float) -> bool:
        random.seed(G.get("dealer", 0) * 1337 + G.get("current_buy_in", 0) * 7 + G.get("my_bet", 0))
        return random.random() < p