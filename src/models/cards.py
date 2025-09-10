RANKS = {
    '2': 2, '3': 3, '4': 4, '5': 5, '6': 6, '7': 7, '8': 8, '9': 9,
    '10': 10, 'J': 11, 'Q': 12, 'K': 13, 'A': 14
}


def card_rank(card):
    """Return numeric rank of a card dict like {"rank": "A", "suit": "hearts"}."""
    if not card or 'rank' not in card:
        return 0
    return RANKS.get(str(card['rank']).upper(), 0)


def is_pair(hole):
    return len(hole) == 2 and card_rank(hole[0]) == card_rank(hole[1]) and card_rank(hole[0]) > 0


def both_high(hole, threshold=11):
    if len(hole) != 2:
        return False
    r1, r2 = card_rank(hole[0]), card_rank(hole[1])
    return r1 >= threshold and r2 >= threshold


def has_pair_with_board(hole, board):
    hole_ranks = {card_rank(c) for c in hole}
    board_ranks = {card_rank(c) for c in board}
    return len(hole_ranks.intersection(board_ranks)) > 0

def is_straight_flush(hole, board):
    if len(hole) != 2 or len(board) != 3:
        return False
    hole_ranks = sorted([card_rank(c) for c in hole])
    board_ranks = sorted([card_rank(c) for c in board])
    return hole_ranks[0] + 1 == hole_ranks[1] and board_ranks[0] + 1 == board_ranks[1] and board_ranks[1] + 1 == board_ranks[2]

