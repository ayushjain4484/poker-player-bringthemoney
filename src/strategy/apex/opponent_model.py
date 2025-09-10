from typing import Dict, Any

class OpponentModel:
    """
    Extremely lightweight per-player tracker.
    Tracks avg preflop bucket (1=strong ... 8=trash) and a crude WTSD-ish proxy (sd_seen).
    """

    def __init__(self):
        self.villains: Dict[str, Dict[str, float]] = {}

    def update_showdown(self, players: list) -> None:
        for p in players or []:
            hv = (p or {}).get("hole_cards") or []
            if not hv:
                continue
            pid = str((p or {}).get("name") or (p or {}).get("id") or "?")
            bucket = self._coarse_bucket(hv)
            v = self.villains.setdefault(pid, {"samples": 0.0, "avg_bucket": 6.5, "sd_seen": 0.0})
            n = v["samples"]
            v["avg_bucket"] = (v["avg_bucket"] * n + float(bucket)) / (n + 1.0)
            v["samples"] = n + 1.0
            v["sd_seen"] = min(1.0, v["sd_seen"] + 1.0)

    # Keep identical to strategy bucket mapping if you want consistency; simplified here:
    def _coarse_bucket(self, hole_cards: list) -> int:
        from .cards import hand_bucket
        return hand_bucket(hole_cards)
