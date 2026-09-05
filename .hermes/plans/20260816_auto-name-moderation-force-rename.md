# Login-Time Offensive-Name Auto-Moderation → Auto Force-Rename — Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** On every player login, detect display names that violate moderation rules (racist/hate slurs, the n-word, offensive terms — including obfuscated/leetspeak variants) via a deterministic blocklist plus an OpenRouter LLM call with **structured JSON output**, and automatically force-rename the violating account through the existing `forced_name` lock + a neutral in-game announcement.

**Architecture:** Two-stage matcher in the worker's login path. Stage A is a cheap, exact, deterministic blocklist+normalizer that catches known/obfuscated slurs instantly at zero cost and **zero false positives**. Stage B — for names that aren't a blocklisted slur — is an OpenRouter call that returns a **Pydantic-structured verdict** (mirroring `amc-peripheral`'s `beta.chat.completions.parse` pattern), which includes whether the name violates and an **LLM-proposed clean alternative name**. Any confirmed violation (blocklist hit, or LLM `is_violation` at high confidence) triggers the existing account-level `forced_name` lock → `refresh_player_name` live push → `ForcedNameLog`/`NameModerationLog` audit → **in-game `/chat` neutral announcement**. Login is never blocked — the check runs as a non-blocking `asyncio.create_task`.

**Tech Stack:** Python 3.13 / Django async ORM (arq worker), **`openai==1.96.1` SDK → `AsyncOpenAI(api_key=OPENAI_API_KEY_OPENROUTER, base_url="https://openrouter.ai/api/v1")`** (identical to amc-peripheral), Pydantic v2 structured output via `beta.chat.completions.parse`, PostgreSQL (models), Redis (name-verdict cache).

---

## Decisions locked in (freeman, 2026-08-16)

1. **LLM provider = the same openai+openrouter setup as `amc-peripheral`.** `AsyncOpenAI(api_key=OPENAI_API_KEY_OPENROUTER, base_url="https://openrouter.ai/api/v1")`. Structured output via Pydantic v2 + `beta.chat.completions.parse(response_format=<Model>)` exactly like peripheral's WikiSynthesizer / ModerationResponse path.
2. **LLM proposes the alternative name** in the structured output (`suggested_name` field, required when `is_violation == true`). Client re-sanitizes/re-checks it before writing.
3. **Announce.** After auto-rename, send a neutral in-game `/chat` broadcast (per motortown-moderation skill: gratitude/neutral wording, **never echo the slur**, password= required in the query string).
4. **Scope = `PlayerLoginLogEvent` only.** Character switch requires a re-login in Motor Town, so catching the login event covers switching — no separate character-switch hook needed.
5. **Rollout = FULL LLM Phase B (freeman, 2026-08-16).** No review-first burn-in. Ship enabled on prod (`NAMER_ENABLED=1`, `NAMER_AUTO_CONFIDENCE_THRESHOLD=0.9`): blocklist hits AND high-confidence LLM verdicts auto-rename. The sub-threshold `/manual_review` path stays as a lightweight always-on safety net (posts to Discord `1366478091131551834`), not a blocking gate. The code already supports this — no rewrite needed; this governs the deploy env + this record.

---

## Recommended rollout (Yumemi — staged, burn-in first)

Auto-rename is **account-level and lasting**, so I recommend a staged rollout rather than flipping it all the way on at once. This is a single switch away ("strict auto" vs "review + auto on certain cases") and is intentional, not scope creep:

- **Phase 0 — build & staging (no prod harm).** `NAMER_ENABLED=0` app-wide; everything is unit-tested against mocks and validated on `amc-peripheral` staging. Nothing touches a real account.
- **Phase 1 — "auto on blocklist, review on LLM" (recommended first prod run).** Keep two tiers configured:
  - **Stage A blocklist hits auto-rename immediately** — these are certain (a canonical/leetspeak slur). No hesitation needed; they're the clear offenses this feature exists for.
  - **LLM verdicts fire *manually***: post each to Discord channel `1366478091131551834` (`#namer-review`) as `needs review`, and rename after a human thumbs-up. This catches LLM false-positives (the thing I'm most wary of — an innocent name auto-locked forever) before it matters.
  - Run this for ~1–2 weeks while `NameModerationLog` accumulates; tune the blocklist patterns and the LLM prompt on real data.
- **Phase 2 — full auto ("strict").** Flip `NAMER_AUTO_CONFIDENCE_THRESHOLD` to `0.9` and let the LLM auto-rename on high-confidence violations. `NameModerationLog` still records every decision so the rare miss is recoverable; announcements stay neutral.

**Why not skip to Phase 2:** a wrong auto-rename isn't reversible-by-accident (the `forced_name` lock is permanent until an admin clears it). A week of review-first costs a few Discord messages and buys confidence that the LLM's `confidence` + `recommended_action` are trustworthy in production. The blocklist path is safe to trust immediately.

If you'd rather skip straight to full auto, that's a valid call — flip `NAMER_AUTO_PERMISSIVE=1` in Phase 1 and proceed. The plan supports either; the flag is the only difference.

---

## Current context / assumptions (verified 2026-08-16)

- **Implementation target is `amc-backend` `master`, NOT the local checkout.** The submodule pin at `/opt/data/workspace/amc-server/amc-backend` is **stale** (`d097fcb`) and does **not** contain the force-rename feature. Real code is on `origin/master` (`7be0974`, includes PR #31 force-rename + #32 async-safe fix). All work in a fresh worktree off `master`.
- **Login chokepoint (master):** `src/amc/tasks.py:964` `case PlayerLoginLogEvent(...)` → ~line 1007-1008 `asyncio.create_task(send_player_messages(...))` and `await refresh_player_name(character, http.client_mod)`. Both `character` and `player` are in scope here.
- **Name enforcement (master):** `src/amc/player_tags.py:191-212` `refresh_player_name` reads `forced_name` async-safely, `base_name = (forced_name or character.name)`, then `build_display_name()` → `set_character_name` → mod `PUT`. Setting `Player.forced_name` + calling `refresh_player_name` is the entire auto-rename mechanism. **No mod changes needed.**
- **Audit helper (master):** `src/amc/forced_name.py::log_forced_name_change(player, *, action, old_name, new_name, actor_character, actor_player, actor_discord_id)` → `ForcedNameLog`.
- **Announce path:** native game server urllib-`http://127.0.0.1:8080/chat` POST with `password=` (empty ok) + `message` + `type=message`. Success `{"message":"message sent","succeeded":true}`. Neutral wording per moderation skill; never echo the slur.
- **LLM reference (amc-peripheral, mirror it):**
  - `pyproject.toml:20` → `"openai==1.96.1"` (pinned).
  - `radio/radio_cog.py:442-444` → `AsyncOpenAI(api_key=OPENAI_API_KEY_OPENROUTER, base_url="https://openrouter.ai/api/v1")`.
  - `wiki/synthesis.py:93-100` → `await client.chat.completions.create(model=..., messages=[...])`.
  - `radio/radio_cog.py:1072-1091` → structured output: `await client.beta.chat.completions.parse(model=..., response_format=<PydanticModel>, messages=[...])` → `choices[0].message.parsed`.
  - `bot/ai_models.py` → Pydantic v2 response models (e.g. `ModerationResponse`), `from pydantic import BaseModel, Field`.
  - `settings.py:4` → `OPENAI_API_KEY_OPENROUTER = os.environ.get("OPENAI_API_KEY_OPENROUTER")`; `settings.py:37` → `DEFAULT_AI_MODEL = os.environ.get("DEFAULT_AI_MODEL", "qwen/qwen3.6-flash")`.
- **amc-backend today has no `openai`/`anthropic` dep.** Adding `openai==1.96.1` (pulls pydantic v2) is required for parity. This means `uv lock` + uv2nix rebuild churn — unavoidable and accepted.
- **Workflow constraint:** changes ship as a git worktree off `master` → feature branch → PR (CI is gate). Never commit to master; never edit the stale main checkout in place.

---

## Files to change (on master, in a fresh worktree)

- **Create** `src/amc/name_verdict.py` — the Pydantic v2 response model `NameVerdict` (mirrors `ai_models.py`/`ModerationResponse` style).
- **Create** `src/amc/name_moderation.py` — Stage A blocklist + normalizer + `is_offensive_blocklist(name)`.
- **Create** `src/amc/llm_judge.py` — `AsyncOpenAI` client wrapper (same base_url/key as peripheral) + `judge_name(name) -> NameVerdict` via `beta.chat.completions.parse`.
- **Create** `src/amc/models.py` addition — `NameModerationLog` (decision audit: player, character, base_name, verdict_source, is_violation, confidence, categories, action, suggested_name, llm_model, created_at).
- **Create** migration for `NameModerationLog`.
- **Modify** `src/amc/tasks.py:964-1010` — fire non-blocking `asyncio.create_task(run_name_moderation(character, player, http_client_mod))` in the login case; **no other scope** (no character-switch hook).
- **Modify** `src/amc/player_tags.py` — **no change** (enforcement reuses `refresh_player_name`).
- **Modify** `src/amc_backend/settings.py` — `OPENAI_API_KEY_OPENROUTER`, `NAMER_LLM_MODEL` (default `DEFAULT_AI_MODEL`), `NAMER_ENABLED`, `NAMER_AUTO_CONFIDENCE_THRESHOLD`, `NAMER_REVIEW_CHANNEL_ID` (default `1366478091131551834`), `NAMER_CANNED_FALLBACK_NAME`, `NAMER_VERDICT_CACHE_TTL`, `NAMER_ANNOUNCE` (bool).
- **Modify** `pyproject.toml` — add `"openai==1.96.1"`; run `uv lock` (required by uv2nix).
- **Modify** `amc-peripheral`-style NixOS service env (on `asean-mt-server`, machine Nix module for `amc-worker`) — inject `OPENAI_API_KEY_OPENROUTER` from the ragenix secret (same secret name peripheral uses). **Editing the ragenix secret to add the OpenRouter key on `asean-mt-server` requires the user.**
- **Tests** `src/amc/test_name_moderation.py`, `src/amc/test_llm_judge.py`, extend `src/amc/test_tasks.py`.

---

## Implementation tasks

### Task 0 — Clean worktree off master (preflight)

- Fetch `origin/master`, open a git worktree off it for `amc-backend`.
- Verify the feature exists: `git show HEAD:src/amc/forced_name.py | head -5` prints the docstring; `git show HEAD:src/amc/player_tags.py | grep -c forced_name` > 0.
- Create feature branch `feat/login-name-auto-moderation`.
- Commit.

### Task 1 — Add `openai` dependency

- `pyproject.toml` → `"openai==1.96.1"` in `[project].dependencies`.
- `uv lock` (MUST — uv2nix reads the lock; skip = broken Nix build).
- Verify with `nix flake check .#django-check` (or `direnv exec ... python -c "from openai import AsyncOpenAI"`).
- Commit (loicker `pyproject.toml` + `uv.lock`).

### Task 2 — `NameModerationLog` model + migration

- Models: `player = FK(Player)`, `character = FK(Character, null)`, `base_name`, `verdict_source` ("blocklist"|"llm"|"cache"|"error"), `is_violation`, `confidence`, `categories` (ArrayField), `action` ("rename"|"none"|"manual_review"), `suggested_name`, `llm_model`, `created_at`.
- Migration `0227_namemoderationlog.py`.
- Test: create a row, assert persists (in `test_name_moderation.py`, mocked DB).
- Commit.

### Task 3 — Stage A deterministic blocklist

- `name_moderation.py`:
  - `_normalize(name)` → lowercase + leetspeak map (`1→i`, `!→i`, `0→o`, `3→e`, `@→a`, `$→s`, `5→s`, `7→t`, strip separators/spaces).
  - `SLUR_PATTERNS` — regex with word boundaries over the normalized string for the n-word and its obfuscated variants, f-slur, c-slur, etc.
  - `is_offensive_blocklist(name) -> tuple[bool, list[str]]`.
- Tests (read carefully — the false-positive traps):
  - `is_offensive_blocklist("delivryn1gaa") is True`
  - `is_offensive_blocklist("ih8juice") is True`  (may need pattern tune; the old modlog regex was `n[i1!]g[gq9]a` etc.)
  - `is_offensive_blocklist("HappyDriver") is False`
  - `is_offensive_blocklist("Motortown...") is False` (the `nigg` inside "Motortown" — boundary rules required)
  - `is_offensive_blocklist("truckin") is False`, `is_offensive_blocklist("Nigeria") is False`
- Commit.

### Task 4 — Pydantic structured verdict (`NameVerdict`)

- `name_verdict.py` (mirror `ai_models.py` style), Pydantic v2:

```python
from typing import Literal, List, Optional
from pydantic import BaseModel, Field

class NameVerdict(BaseModel):
    name: str
    is_violation: bool
    confidence: float = Field(ge=0, le=1)
    categories: List[str] = Field(default_factory=list)
    reason: str = ""
    suggested_name: Optional[str] = None          # LLM proposes the alt name
    recommended_action: Literal["rename", "none", "manual_review"] = "none"
```

- Commit.

### Step 5 — LLM judge (mirror peripheral)

- `llm_judge.py`:

```python
from openai import AsyncOpenAI
from django.conf import settings

_cache = {}   # or Redis; keyed by normalize(name); TTL=settings.NAMER_VERDICT_CACHE_TTL

_client = None
def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY_OPENROUTER,
                              base_url="https://openrouter.ai/api/v1")
    return _client

async def judge_name(name: str) -> NameVerdict:
    verdict = _cache.get(norm)
    if cached: return cached
    try:
        completion = await _get_client().beta.chat.completions.parse(
            model=settings.NAMER_LLM_MODEL,
            response_format=NameVerdict,
            messages=[
                {"role": "system", "content": (
                    "You are a video-game name moderator. Judge whether a display name "
                    "violates anti-hate rules (racist, homophobic, misogynistic, or slur "
                    "names, including obfuscated/leetspeak spellings). Reply ONLY as the "
                    "given NameVerdict schema. If violation, set is_violation=true and "
                    "propose a single clean, playful replacement in suggested_name.")},
                {"role": "user", "content": f"Name to judge: \"{name}\""},
            ],
        )
        parsed = completion.choices[0].message.parsed
    except Exception:
        # degrade to no-action; never propagate into login path
        return NameVerdict(name=name, is_violation=False,
                           recommended_action="none", categories=[], reason="judge_error")
    ...
```

- Mocked-`AsyncOpenAI` tests: canned parsed `NameVerdict`, malformed parse → fallback verdict "none"; assert `judge_name` never raises.
- Commit.

### Step 6 — Orchestrator + login hook + auto-rename + announce

`run_name_moderation(character, player, http_client, http_client_mod, announce_session)` in `name_moderation.py`:

```python
async def run_name_moderation(character, player, http_client_mod, settings=settings):
    if not settings.NAMER_ENABLED: return
    display = character.custom_name or character.name
    base = strip_all_tags(display)
    if _is_reserved(base): return                      # [GOV]/[M]/[DOT]/staff untouched
    # Stage A
    blocked, cats = is_offensive_blocklist(base)
    if blocked:
        await _apply_rename(character, player, base, suggestion=None, cat=cats,
                            source="blocklist", confidence=1.0, session=http_client_mod)
        return
    # Stage B
    verdict = await judge_name(base)                   # cached by default
    if verdict.is_violation and verdict.confidence >= settings.NAMER_AUTO_CONFIDENCE_THRESHOLD \
            and verdict.recommended_action == "rename":
        await _apply_rename(character, player, base, verdict=verdict, ...)
    elif verdict.is_violation:                        # low confidence
        await _log_manual_review(character, player, base, verdict,
                                 channel_id=settings.NAMER_REVIEW_CHANNEL_ID)  # 1366478091131551834
    else:
        await _log_nonviolation(character, player, base, verdict)
```

`_apply_rename`:

```python
async def _apply_rename(character, player, base, *, suggestion, cats, source, confidence):
    to = suggestion if _safe_suggested_name(suggestion) else settings.NAMER_CANNED_FALLBACK_NAME
    old = character.custom_name or character.name
    await Player.objects.filter(unique_id=player.unique_id).aupdate(forced_name=to)
    await log_forced_name_change(player, action="auto_rename", old_name=old, new_name=to)
    await NameModerationLog.objects.acreate(...)   # decision audit row
    await refresh_player_name(character, http_client_mod)      # live push (already async-safe)
    if settings.NAMER_ANNOUNCE:
        announce = f"The display name was changed to comply with our rules. The player now shows as {to}."
        await _send_in_game_announce(http_client_mod or native /chat, announce)   # neutral, no slur
```

`_send_in_game_announce`: via `http://127.0.0.1:8080/chat?password=&message=...&type=message&color=FFFF00` per moderation skill — **never include the old slur**.

Login hook in `tasks.py` (PlayerLoginLogEvent, only place):

```python
await refresh_player_name(character, http.client_mod)
asyncio.create_task(run_name_moderation(character, player, http.client_mod, settings))
```

- Integration test: login for `delivryn1gaa` → `Player.forced_name` set, `ForcedNameLog` row, `NameMod...` row; login race (LLM down) → no exception, no block.
- Unit test: `_safe_suggested_name` rejects a bad suggested name; fallback applied.
- Commit.

### Task 7 — Config/secrets/deploy (mirror peripheral)

- `settings.py`: `OPENAI_API_KEY_OPENROUTER = os.environ.get("OPENAI_API_KEY_OPENROUTER")`; `NAMER_LLM_MODEL = os.environ.get("NAMER_LLM_MODEL") or DEFAULT_AI_MODEL` (default `qwen/qwen3.6-flash`); `NAMER_ENABLED` (default off); `NAMER_AUTO_CONFIDENCE_THRESHOLD` (default `0.9`); `NAMER_REVIEW_CHANNEL_ID = os.environ.get("NAMER_REVIEW_CHANNEL_ID", "1366478091131551834")`; `NAMER_CANNED_FALLBACK_NAME` (default `"Friendly"`+suffix or configured); `NAMER_VERDICT_CACHE_TTL`; `NAMER_ANNOUNCE=True`.
- Firewall nothing: `OPENAI_KEY... ` env var added to `amc-backend`/`amc-worker` NixOS service on `asean-mt-server`. **ragenix secret on the server is user-editing (add `OPENAI_API_KEY_OPENROUTER` same as peripheral).**
- Confirm ragenix secret already set for peripheral = reuse same name.
- Commit.

### Task 8 — Integration + verification

- Staging (`amc-peripheral` staging backend) first: `NAMER_ENABLED=0` → nothing runs; then set a synthetic offensive name + `NAMER_ENABLED=1`, trigger login event, assert `Player.forced_name` set, mod list shows new name, `NameMod...` + `Forced...Log` rows present, `/chat` neutral announcement sent (no slur).
- `nix flake check .#pytest`, `.#ruff`, `.#django-check`.
- PR `feat/login-name-auto-moderation` → `master`, CI gate (remember: amc-backend CI chronically red from pre-existing malformed `ci.yml` — don't read PR CI as signal; fix the YAML while here if cheap).
- Prod deploy `--skip-checks --migrate --restart-be` after migration, flip `NAMER_ENABLED=1`.

---

## Safety / guardrails

- **Never block a login**: the check is `asyncio.create_task` fire-and-forget; all LLM/HTTP/DB failures degrade to **no action** + log (never throw into login path).
- **Auto-rename is account-level and lasting.** Only auto when blocklist hit (certain) OR `is_violation && confidence >= 0.9 && action=="rename"`. Else → Discord channel `NAMER_REVIEW_CHANNEL_ID` (`1366478091131551834`) for manual review.
- **Review/log posting is via the amc-backend Discord bot** (`NAMER_REVIEW_CHANNEL_ID` = `1366478091131551834`). The worker resolves the channel by ID at runtime (`bot.fetch_channel` / `get_channel`) — the ID is configured, the actual `discord.TextChannel` is looked up when posting, so it works across restarts/bots. If the bot is the one posting, fetch the channel by ID rather than hardcoding the name (channel names can be renamed).
- **LLM-suggested name is re-checksumed** against the blocklist + reserved tags + length (≤20 after `strip_all_tags`) before write; fall back to `NAMER_CANNED_FALLBACK_NAME` if unsafe/missing.
- **Never echo/l broadcast the slur** — announcement is neutral and only names the outcome/new name (per moderation skill).
- **Reserved/staff names untouched** (`[GOV]`, `[M]`, `[DOT]`, staff accounts).
- **Blocklist curated for false positives** (`Motortown`'s internal `nigg` etc. → word-boundary rules).
- **Cost/latency:** verdict cache in Redis by normalized base name (TTL) so a name is judged once; blocklist-first keeps LLM calls low; cap `NAMER_MAX_LLM_CALLS/DAY` optional.
- **Key hygiene:** `OPENAI_API_KEY_OPENROUTER` via ragenix, env-injected, never in repo/logs.

---

## Risks / tradeoffs / open questions

| Risk | Mitigation |
|---|---|
| LLM false-negative (misses a slur) | Stage A blocklist always catches canonical/leetspeak; LLM is additive |
| LLM false-positive (auto-unrenames innocent name) | confidence threshold + `recommended_action` enum + every verdict logged to `NameModerationLog` for review; burn-in below |
| Auto-replace of an undesired name | Only on clear violation; LLM suggestion re-sanitized; announced neutrally, human-seen |
| Adding `openai` dep → `uv lock`/Nix rebuild churn | Accepted (freeman asked for peripheral parity). Keep pinned `==1.96.1` |
| API key secret not on prod host | user must add ragenix `OPENAI_API_KEY_OPENROUTER` on asean-mt-server (editing secrets is user). Workflows can stage with a dev key |
| CI red (pre-existing) | don't gate on it; fix `ci.yml` in same PR if cheap |

Resolved (not blocking): provider ✓, suggested name from LLM ✓, announce ✓, scope = login only ✓, review/log channel = `1366478091131551834` ✓.

**One follow-up I need from you, non-blocking for code:** the exact model string for the moderation call. Peripheral defaults to `qwen/qwen3.6-flash` (cheap/fast, fine for this). I'll default `NAMER_LLM_MODEL` to `qwen/qwen3.6-flash` and make it env-overridable — confirm or pick another on OpenRouter and I'll set it in the deploy env.

---

## Verification checklist

- [ ] worktree off `origin/master`; features present; branch created.
- [ ] `pytest` green: blocklist, LLM-parse (mocked), orchestrator, safe-name, announce.
- [ ] `is_offensive_blocklist` catches `delivryn1gaa`/`ih8juice`, misses `HappyDriver`/`Motortown…`/`Nigeria`.
- [ ] login hook non-blocking (LLM down → no throw, no login delay).
- [ ]  `NameModerationLog` row for every decision; `ForcedNameLog` only for renames.
- [ ] `NAMER_ENABLED=0` runs nothing.
- [ ] reserved/staff names skipped.
- [ ] staging: synthetic bad name → `forced_name` set, live name pushed, `/chat` announcement neutral.
- [ ] prod deploy + flake checks.