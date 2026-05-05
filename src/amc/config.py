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




#########
# PLAYER WEALTH CONTROLS
#########

# RICH_CEILING is the max tax point —
# at/above it, subsidy = 0 and tax = 100% of base. Set this to the wealth
# level at which players should fully self-sustain.
WEALTH_POOR_FLOOR = 500_000      # at/below: established-broke — 100% subsidy / TAX_FLOOR_PCT tax
WEALTH_RICH_CEILING = 5_000_000  # at/above: established-rich — 0% subsidy / 100% tax (break-even)

# Curve warp exponent. 
# Higher value = Protection drops off slower initially, fast later
# Lower value = Protection drops off faster intially, slow later
# 1.0 = linear
WEALTH_EXPONENT = 1.3

# Set to 0 to disable. Once cap exceeded, character subject to full scaling based on treasury
WEALTH_NEW_PLAYER_LIFETIME_INCOME_CUTOFF = 3_000_000

# `driver_level` at/above which a player is considered "experienced" for the net-loss clamp. Affects subsidies for these players to slow endgame prog
EXPERIENCED_DRIVER_LEVEL_THRESHOLD = 200


# Minimum tax amount for established players
WEALTH_TAX_FLOOR_PCT = 0.15

# Multiplier applied to subsidies when the delivering player is sitting in a
# vehicle with detected modded parts. 0.0 = no subsidy, 1.0 = no cut.
MODDED_SUBSIDY_MULTIPLIER = 1.0




#########
# TREASURY RESPONSIVE SCALING
#########

TREASURY_FLOOR = 50_000_000
TREASURY_CEILING = 150_000_000
TREASURY_GOOD_HEALTH_T = 0.9

#   < 1.0  = drop off slower, only drops off heavily near the top/ceiling
#   = 1.0  = linear interpolation
#   > 1.0  = drops off quickly as soon as above floor
TREASURY_CURVE_EXPONENT = 0.7

# Upper clamp on treasury payouts if treasury is above celing
# Higher = more aggressive self-correction
TREASURY_BOOM_CAP = 2.0

# Curve exponent for the veteran subsidy clamp
#   < 1.0  = gentler ramp (subsidy stays high until treasury near floor)
#   = 1.0  = linear
#   > 1.0  = sharper ramp (subsidy collapses fast when treasury low, so the system self-heals faster)
SUBSIDY_HEALTH_EXPONENT = 2.0




#########
# PAYOUT VARIANCE
#########

# Per-job random variance applied at posting time so two otherwise-identical jobs aren't twins. Asymmetric allowed (e.g. UP=0.05, DOWN=0.03 -> +5/-3%).
# Applied INDEPENDENTLY to `bonus_multiplier` and `completion_bonus` AFTER the unified treasury scale above. Set both to 0 to disable jitter.
JOB_BONUS_VARIANCE_UP = 0.05 
JOB_BONUS_VARIANCE_DOWN = 0.05




#########
# /jobs Bonus Multiplier
#########

# Set AT_NEW == AT_VETERAN to disable per-player payout scaling.
JOB_BONUS_PAYOUT_FACTOR_AT_NEW = 1.0      # multiplier for new / broke contributors
JOB_BONUS_PAYOUT_FACTOR_AT_VETERAN = 0.75  # multiplier for veteran / established-rich contributors
# Curve exponent applied to the player's wealth_t before the lerp.
#   < 1.0  = a little wealth/xp already dims payout noticeably
#   = 1.0  = linear
#   > 1.0  = payout stays near full until the player is clearly veteran/rich
JOB_BONUS_PAYOUT_FACTOR_EXPONENT = 3
