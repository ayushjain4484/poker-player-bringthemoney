from typing import Optional

def size_from_pot(pot: int, frac: float, stack: int, cap_stack_frac: float = 1.0) -> int:
    amt = int(max(1, pot * frac))
    cap = int(stack * cap_stack_frac)
    return max(1, min(amt, cap))

def legal_raise(to_call: int, minimum_raise: int, stack: int, bump: int = 0, absolute: Optional[int] = None) -> int:
    if stack <= to_call:
        return min(to_call, stack)
    if minimum_raise <= 0:
        return min(to_call, stack)
    legal_min = to_call + minimum_raise
    target = legal_min + (bump if bump else 0)
    if absolute is not None:
        target = max(legal_min, absolute)
    return min(max(legal_min, target), stack)

def promote_raise(to_call: int, minimum_raise: int, stack: int, target_total: int) -> int:
    if minimum_raise <= 0:
        return min(to_call, stack)
    legal_min = to_call + minimum_raise
    if stack < legal_min:
        return min(to_call, stack)
    return min(max(legal_min, target_total), stack)

def raise_to_amount(current_buy_in: int, to_call: int, minimum_raise: int, desired_total: int, stack: int) -> int:
    minr = max(1, minimum_raise)
    desired_extra = max(minr, desired_total - current_buy_in)
    bet = to_call + desired_extra
    return max(0, min(bet, stack))

def finalize(desired: int, to_call: int, minimum_raise: int, stack: int) -> int:
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
