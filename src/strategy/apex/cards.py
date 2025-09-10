from typing import List, Tuple

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

def hand_bucket(hole: List[dict]) -> int:
    """
    1 Premium: AA–TT, AKs, AKo
    2 Strong:  99–77, AQs–ATs, AQo–AJo, KQs–KJs, QJs, JTs
    3 Mid:     KQo, KTo+, QTo+, JTo, T9s, 98s, 87s
    4 Pairs:   66–22
    5 Axs:     A9s–A2s
    6 SC/SG:   97s–54s, 86s–64s, T8s, etc.
    7 Weak-O:  K9o–K2o, Q9o–Q2o, J9o–J2o, etc.
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
    if {hi, lo} in ({14,12}, {14,11}): return 2

    if suited and ((hi == 13 and lo in (12,11)) or (hi == 12 and lo == 11)):
        return 2
    if suited and hi == 14 and 2 <= lo <= 9:
        return 5

    if suited and (({hi, lo} in [{10,9},{9,8},{8,7}]) or (abs(hi - lo) == 1 and hi >= 8)):
        return 3
    if suited and hi >= 7 and 2 <= abs(hi - lo) <= 3:
        return 6

    if offsuit and ((hi in (13,12) and lo >= 10) or (hi == 11 and lo == 10)):
        return 3
    if offsuit and ((hi in (13,12,11)) and 2 <= lo <= 9):
        return 7
    return 8
