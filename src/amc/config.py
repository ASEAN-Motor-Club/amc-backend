"""
AMC feature flags / static configuration.

Lightweight switches that don't need to live in the database.
Toggle these in code (and redeploy) rather than at runtime.
"""

# When False, process_treasury_expiration_penalty() becomes a no-op so the treasury is not charged the 50% penalty for expired non-Ministry jobs.
TREASURY_EXPIRATION_PENALTY_ENABLED = True

# Per-cargo multiplier applied when crediting a delivery toward a job fulfillment counter. Defaults to 1 for any cargo not listed.
CARGO_FULFILLMENT_WEIGHTS: dict[str, int] = {
    # "CARGO_KEY": multiplier"
    "Container_40ft_01": 2,
}

# Depot restock subsidy amount. Set to 0 to disable.
DEPOT_RESTOCK_SUBSIDY_AMOUNT = 10_000
