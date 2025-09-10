from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional

@dataclass
class ApexConfig:
    """
    ApexPredatorStrategy configuration

    ---- Core discipline / economics ----
    POT_ODDS_CALL_THRESHOLD: Call if to_call/(pot+to_call) <= this number. Lower = tighter calls.
    CHEAP_CALL_STACK_PCT:    'Cheap peel' threshold as % of our stack (e.g., 0.02 = 2%).
    CHEAP_CALL_ABS_CAP:      Absolute ceiling for cheap peels in chips. Min() with pct cap.

    ---- Bluff/value mixing ----
    XR_BLUFF_FREQ:           Base frequency to check-raise bluff when we hold blockers.
    THIN_VALUE_FREQ:         Frequency to take thin value when to_call == 0 (stabs with pair).
    DRY_BLUFF_FREQ:          Bluff stab frequency on dry boards (first to act).
    WET_BLUFF_FREQ:          Bluff stab frequency on wet/dynamic boards (first to act).

    ---- Postflop sizes (fractions of current pot) ----
    POLAR_OVERBET_FRAC:      Polar overbet size for nut/air spots at low SPR (e.g., 1.25 = 125% pot).
    VALUE_RAISE_FRAC:        Default raise/bet fraction for value/merge spots (~0.45 = 45% pot).
    CBET_SMALL_FRAC:         Small c-bet/stab size used on dry boards (~0.33 = 33% pot).
    MAX_STACK_OVERBET_FRAC:  Cap any bet to this fraction of our remaining stack (1.0 = all-in OK).

    ---- Preflop sizing ----
    PREFLOP_OPEN_MIN_MULT_IP:  Open size in BB-equivalent when In Position (IP).
    PREFLOP_OPEN_MIN_MULT_OOP: Open size in BB-equivalent when Out Of Position (OOP).
    PREFLOP_PAIR_RAISE_EXTRA:  Extra min-raise multiples for pocket pairs when deep (adds to open).
    PREFLOP_3BET_FACTOR_IP:    3-bet total size factor vs open when IP (multiplies current_buy_in or BB guess).
    PREFLOP_3BET_FACTOR_OOP:   3-bet total size factor when OOP.

    ---- Push/fold mode (≤10bb) ----
    PUSHFOLD_ENABLE_BROADEN_4MAX: If True, adds a few extra jams 4-max with mid buckets.

    ---- Exploit toggles ----
    TIGHTEN_VS_HUGE_SIZING:   If True, fold more often when villain uses big sizes and we’re capped.
    PUNISH_PASSIVES:          If True, call/value-bet more vs passive tables (WTSD-ish proxy).
    BLUFF_DAMPEN_MULTIWAY:    If True, reduce bluff frequencies in multiway pots.
    """

    POT_ODDS_CALL_THRESHOLD: float = 0.27
    CHEAP_CALL_STACK_PCT:    float = 0.02
    CHEAP_CALL_ABS_CAP:      int   = 60

    XR_BLUFF_FREQ:     float = 0.20
    THIN_VALUE_FREQ:   float = 0.58
    DRY_BLUFF_FREQ:    float = 0.38
    WET_BLUFF_FREQ:    float = 0.22

    POLAR_OVERBET_FRAC:     float = 1.25
    VALUE_RAISE_FRAC:       float = 0.45
    CBET_SMALL_FRAC:        float = 0.33
    MAX_STACK_OVERBET_FRAC: float = 1.00

    PREFLOP_OPEN_MIN_MULT_IP:  float = 2.2
    PREFLOP_OPEN_MIN_MULT_OOP: float = 2.5
    PREFLOP_PAIR_RAISE_EXTRA:  float = 0.50
    PREFLOP_3BET_FACTOR_IP:    float = 3.0
    PREFLOP_3BET_FACTOR_OOP:   float = 4.0

    PUSHFOLD_ENABLE_BROADEN_4MAX: bool = True

    TIGHTEN_VS_HUGE_SIZING:  bool = True
    PUNISH_PASSIVES:         bool = True
    BLUFF_DAMPEN_MULTIWAY:   bool = True

    # Factory helpers
    @classmethod
    def from_dict(cls, overrides: Optional[Dict[str, Any]] = None) -> "ApexConfig":
        cfg = cls()
        if overrides:
            for k, v in overrides.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        return cfg

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
