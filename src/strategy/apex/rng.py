import hashlib

def deterministic_rng(game_state: dict, in_action: int) -> float:
    """
    Returns a stable float in [0,1) per decision point, so mixed frequencies are reproducible.
    """
    key = f"{game_state.get('game_id','')}-{game_state.get('round',0)}-{game_state.get('bet_index',0)}-{in_action}"
    h = hashlib.sha256(key.encode('utf-8')).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF
