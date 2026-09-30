"""
AMC feature flags / static configuration.

Lightweight switches that don't need to live in the database.
Toggle these in code (and redeploy) rather than at runtime.
"""

# When False, process_treasury_expiration_penalty() becomes a no-op so the treasury is not charged the 50% penalty for expired non-Ministry jobs.
TREASURY_EXPIRATION_PENALTY_ENABLED = False

# Per-cargo multiplier applied when crediting a delivery toward a job fulfillment counter. Defaults to 1 for any cargo not listed.
CARGO_FULFILLMENT_WEIGHTS: dict[str, int] = {
    # "CARGO_KEY": multiplier"
    "Container_40ft_01": 2,
}

# Depot restock subsidy amount. Set to 0 to disable.
DEPOT_RESTOCK_SUBSIDY_AMOUNT = 10_000

# Minimum RP mode duration in hours. Players cannot toggle RP mode off before this elapses.
RP_MIN_DURATION_HOURS = 1

# --- Jeju Underground Street Racing rewards (Yuuka 2026-09-29) ---
UNDERGROUND_CHAMPIONSHIP_NAME = "Jeju Underground Street Racing"

# Blood Money is paid at rotation end, degraded by finish position:
# 1st = checkpoints x rate, then each place halves (2nd half of 1st,
# 3rd half of 2nd, 4th half of 3rd); 5th and beyond = half of 4th, flat.
# Rate 0 = payouts disabled — pending community discussion
# (Yuuka 2026-09-30: "Set blood money to 0 as well for now. We'll put
# that to discussion."). Set back to 4000 (or another rate) when decided.
# Payouts are treasury-funded either way: each paid racer also posts
# Dr Treasury Expenses / Cr Treasury Fund so the government loses the
# money (skipped entirely while the Treasury is at its floor).
BLOOD_MONEY_PER_CHECKPOINT = 0
RESPECT_PER_CHECKPOINT = 0  # rate undecided — structure only for now

# Vehicle types allowed in underground races (start-line DQ, fail-closed).
UNDERGROUND_VEHICLE_TYPES = ["Small", "Pickup"]


def underground_blood_money(checkpoints: int, position: int, rate: int | None = None) -> int:
    """Position ladder for Blood Money payouts.

    pos1 = checkpoints*rate; pos2 = half of pos1; pos3 = half of pos2;
    pos4 = half of pos3; pos5 AND BEYOND = half of pos4 (flat). Floored to
    whole coins. Position 0 / negatives read as 1st. rate defaults to
    BLOOD_MONEY_PER_CHECKPOINT (0 = payouts disabled pending discussion).
    """
    position = max(position, 1)
    amount = checkpoints * (BLOOD_MONEY_PER_CHECKPOINT if rate is None else rate)
    halvings = min(position - 1, 4)  # 1..5 halve; 5th+ flatten
    return amount // (2**halvings)

