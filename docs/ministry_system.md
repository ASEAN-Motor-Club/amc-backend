# Ministry of Commerce System Documentation

The Ministry of Commerce is a gamified role within the ASEAN Motor Club (AMC) that allows a player to manage a portion of the server's economy through job funding and subsidies.

## Overview
The system revolves around the **Minister of Commerce**, an elected position with a one-week term. The Minister receives a budget from the Treasury and uses it to:
1.  **Fund Delivery Jobs**: Boost rewards for specific logistics tasks.
2.  **Allocate Subsidies**: Provide bonuses for certain cargo types or routes.

---

## Core Models
Located in `src/amc/models.py`.

- **MinistryTerm**: Represents a Minister's tenure. Tracks `minister` (Player), `current_budget`, `total_spent`, and performance metrics (`created_jobs_count`, `expired_jobs_count`).
- **MinistryElection**: Manages the election lifecycle (Candidacy -> Polling -> Finalized).
- **MinistryCandidacy**: Links players to an election as candidates.
- **MinistryVote**: Records player votes during the polling phase.
- **SubsidyRule**: Modified to include `allocation` and `spent` for Ministry-specific budget tracking.

---

## Financial Lifecycle

### 1. Budget Allocation
At the start of a term, funds are moved from the **Treasury Fund** to the **Ministry of Commerce Budget**.
- **Double Entry**: `Dr. Ministry Budget` / `Cr. Treasury Fund`.

### 2. Job Funding
When `monitor_jobs` generates a job linked to a template, it may be funded by the Ministry if budget allows.
- **Escrow**: The full `Completion Bonus` is moved from the Ministry Budget to **Ministry Escrow**.
- **Completion (Rebate)**: Upon successful delivery, the player gets the bonus. The Ministry receives a **20% Performance Grant** (rebate) back into its `current_budget` from Treasury Revenue.
- **Expiration (Refund/Burn)**: If a job expires, 50% of the escrowed amount is refunded to the Ministry Budget, and 50% is "burned" (recorded as a Ministry Expense).

### 3. Subsidies
The Minister can allocate budget to `SubsidyRule`s via the Admin interface.
- **Spending**: When a player delivers cargo matching a rule, the subsidy is paid out only if the rule has remaining allocation.
- **Recording**: Spending is recorded by deducting from the rule's allocation and the term's budget, moving funds to **Ministry Expenses**.

---

## Election Workflow
Managed by the `CommerceCog` in `src/amc_cogs/commerce.py`.

1.  **Candidacy**: Players run for the position.
2.  **Polling**: The community votes for the registered candidates.
3.  **Finalization**: The winner is determined, a new `MinistryTerm` is created, and the budget is allocated.

---

## Discord Commands
- `/run_for_minister`: Enter the current election.
- `/vote`: Vote for a candidate during polling.
- `/election_status`: Check the status/phase of the current election.
- `/ministry_budget`: View the current Minister's budget, spending, and subsidy allocations.

---

## Admin Management
Administrators can:
- **Configure Subsidies**: Set `allocation` amounts on `SubsidyRule` objects.
- **Audit Terms**: View `MinistryTerm` records to see historical performance.
- **Manage Elections**: Manages `MinistryElection` phases if manual intervention is needed.
