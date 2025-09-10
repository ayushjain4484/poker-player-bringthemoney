from typing import Dict, List, Tuple
from .cards import parse_cards, has_pair_with_board, rint

def board_texture(board: List[dict]) -> Dict[str, bool]:
    bs = parse_cards(board)
    ranks = [b[0] for b in bs]
    suits = [b[1] for b in bs]
    paired = len(set(ranks)) < len(ranks)
    suit_counts = {s: suits.count(s) for s in set(suits)}
    monotone = any(v >= 3 for v in suit_counts.values())
    twotone = any(v == 2 for v in suit_counts.values())
    rset = sorted(set(ranks))
    straighty = any(all((r + i) in rset for i in range(3)) for r in rset)
    dry = (not paired) and (not monotone) and (not straighty) and (not twotone)
    wet = monotone or straighty or (twotone and not paired)
    return {"paired": paired, "monotone": monotone, "twotone": twotone, "straighty": straighty, "dry": dry, "wet": wet}

def blocker_signal(hole: List[dict], board: List[dict], texture: Dict[str, bool]):
    if not hole:
        return False, None
    if texture.get("monotone"):
        suits = [c.get('suit') for c in (board or [])]
        if len(suits) >= 3:
            mono = max(set(suits), key=suits.count)
            for c in hole:
                if c.get('suit') == mono and c.get('rank') in ('A', 'K'):
                    return True, "flush_blocker"
    if texture.get("straighty"):
        hr = {rint(c.get('rank')) for c in hole}
        if 14 in hr or 13 in hr:
            return True, "straight_blocker"
    return False, None

def hand_strength_vs_board(hole: List[dict], board: List[dict], is4: bool) -> Dict[str, bool]:
    hs = parse_cards(hole); bs = parse_cards(board)
    r1, r2 = (hs + [(0, ""), (0, "")])[:2]
    b_ranks = [b[0] for b in bs]

    pair = r1[0] == r2[0] and r1[0] > 0
    top_b = max(b_ranks) if b_ranks else 0
    overpair = pair and r1[0] > top_b and len(bs) >= 3
    pair_with_bd = has_pair_with_board(hole, board)

    top_pair_value = False
    if pair_with_bd and b_ranks:
        my_high = max(r1[0], r2[0])
        kicker_ok = my_high >= (10 if is4 else 11)
        top_pair_value = (my_high == max(b_ranks)) and kicker_ok

    two_pair_plus = False
    if len(bs) >= 3:
        if pair and any(b == r1[0] for b in b_ranks):
            two_pair_plus = True
        if not pair and pair_with_bd:
            two_pair_plus = (r1[0] in b_ranks) and (r2[0] in b_ranks)

    # Draws (crude OESD/FD)
    suits = [s for _, s in bs]
    suit_count = {s: suits.count(s) for s in set(suits)}
    strong_fd = any(suit_count.get(s, 0) >= 2 for s in set([hs[0][1] if hs else "", hs[1][1] if len(hs) > 1 else ""]))
    unique = sorted(set(b_ranks + [r1[0], r2[0]]))
    strong_oesd = False
    for i in range(len(unique) - 3):
        window = unique[i:i+4]
        if window[-1] - window[0] <= 4:
            strong_oesd = True; break
    strong_draw = strong_fd or strong_oesd

    middle_pair = pair_with_bd and not top_pair_value
    weak_pair = (pair and not overpair and len(bs) == 0) or (pair_with_bd and max(r1[0], r2[0]) < (10 if is4 else 11))

    return {
        "overpair": overpair,
        "top_pair_value": top_pair_value,
        "two_pair_plus": two_pair_plus,
        "strong_draw": strong_draw,
        "middle_pair": middle_pair,
        "weak_pair": weak_pair,
    }
