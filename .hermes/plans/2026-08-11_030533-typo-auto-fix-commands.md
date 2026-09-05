# Typo Auto-Fix for In-Game Slash Commands — Implementation Plan

> **For Hermes:** implement this with subagent-driven-development, task-by-task. Ship via a git worktree off `master` + a PR on `amc-backend` (never commit to master directly).

**Goal:** When a player types a slash command with a typo (e.g. `/hepl` instead of `/help`), auto-fix it: **auto-execute** the nearest real command when the edit distance ≤ 2 and the match is unambiguous (**and the command is non-Admin**); otherwise show a "did you mean" popup (a candidate list for ambiguous ties, or a single suggestion for Admin commands). Instead of treating the typo as plain chat.

**Architecture:** Add a small pure-Python Levenshtein (edit distance) helper and hook it into `CommandRegistry.execute()`. When an unmatched message starts with `/`, compare its command token against all registered command aliases, pick the nearest within a distance threshold, and split into two outcomes:

- **dist ≤ 2 AND unambiguous AND non-Admin** → auto-execute the corrected command (re-enter `execute()` with the corrected token + the player's original args), prefixed by a brief "Running `/help` instead of `/hepl`" popup.
- **otherwise** (ambiguous tie, or any Admin command) → popup only.

The message is always suppressed from Discord forwarding when any of these fire (treated as a handled command).

**Tech Stack:** Python 3, Django stdlib only (no new dependency — avoids `uv`/`uv2nix`/Nix churn).

---

## Context / Findings (read before implementing)

**Where commands live:** `amc-backend/src/amc/commands/*.py` (a package — the user's "commands.py" is actually 15 modules). Each command registers via `@registry.register(name_or_aliases, description=..., category=..., deprecated=...)`. `name` = first alias; `aliases` = full list (e.g. `/coords` `/loc` → one command).

**Registry & dispatch:** `src/amc/command_framework.py` → `CommandRegistry.execute(message, ctx)`.
- Called from two places, both async tasks:
  - `src/amc/tasks.py:842` (primary chat processor, `PlayerChatMessageLogEvent`)
  - `src/amc/handlers/chat.py:70` (`ServerSendChat`, non-normal chat categories)
- `execute()` flow today:
  1. Scans for `partial_match_cmd` (message starts with an alias → usage feedback).
  2. Tries each command's compiled regex (command + typed args) — on match, casts types, runs the function, returns `True`.
  3. If `partial_match_cmd` exists → replies with usage → returns `True`.
  4. **Else returns `False`** → message falls through and is forwarded to Discord as normal chat.

**This is the failure mode we're fixing:** a typo'd command (e.g. `/hepl`) returns `False` and gets forwarded to Discord as if it were chat, plus the player gets no feedback.

**Real command inventory (~50 registrations, ~78 aliases). Notable shapes:**
- Very short: `/a`, `/d` — adjacent to many tokens, high ambiguity risk.
- Big family: `/spawn`, `/spawn_asset(s)`, `/spawn_dealerships`, `/spawn_displays`, `/spawn_garage(s)`, `/spawn_vehicle`.
- Prefix pairs: `/tp` & `/tp_player`; `/coords` & `/loc`; `/song_request` & `/songrequest` (these last two are already aliases of the same command).
- Deprecated: `/bill` (`deprecated=True`) — should NOT be suggested.

## Design Decisions

1. **Scope to `/` prefix only.** Only attempt correction when the first token starts with `/`. Plain chat (`helo`) must never be rewritten.
2. **Two-tier auto-fix (safe + frictionless).**
   - **Auto-execute:** when `best_dist <= AUTO_EXECUTE_DIST` (default **2**, same as the overall threshold) **and** the best match is unambiguous **and** the resolved command is **non-Admin** (`category != "Admin"`). Re-run the corrected command with the player's original args (re-enter `execute()` — no infinite recursion, see below), preceded by a brief "Running `/help` instead of `/hepl`" popup.
   - **Popup only:** everything else — ambiguous ties, or any Admin command (even within auto-exec distance). Admin commands NEVER auto-run: a ≤2-edit typo on `/teleport`, `/mute`, `/despawn`, `/spawn*` is a real side-effect footgun with the player's args. **This Admin guard is the one deviation I inserted beyond the literal ask — override if you want admin auto-exec too.**
   - **No infinite recursion:** the corrected token is an exact registered alias, so the recursive `execute()` call matches it via the normal regex path and never re-enters the typo branch. If the player's args don't fit the corrected command, the re-run falls to the existing "Usage" partial-match path (returns `True`) — still no typo recursion.
3. **Correct the command token only, preserve args.** Split `message` into `command_token` + `rest`. Suggest `f"{corrected_token} {rest}"` so the player re-runs with their original arguments. Do not validate args in the suggestion path.
4. **Edit distance = Levenshtein**, computed on the token **without** the leading `/`, **lowercased**.
5. **Threshold default = 2**, configurable via a settings constant (e.g. `COMMAND_FUZZY_THRESHOLD`).
6. **Tie-breaking / ambiguity:** if the minimum distance is shared by **two or more distinct commands**, don't guess — reply listing the candidates and return `True`. (Case: `/b` → equidistant from `/a` and `/d`.)
7. **Skip deprecated commands** as suggestion candidates (`/bill`).
8. **Permission filter (refinement, keep simple):** exclude `category=="Admin"` commands from candidates when `ctx.player_info` says the player isn't an admin, so we never suggest something that'll silently no-op.

## Files Likely to Change

- `src/amc/command_framework.py` — add `levenshtein(a, b)` helper + suggestion logic in `execute()`.
- `src/amc/tests_command_framework.py` — unit tests (primary). `tests_command_framework.py` uses `SimpleTestCase` + a fresh `CommandRegistry`, perfect for this.
- `src/amc_backend/settings.py` (or existing settings) — optional `COMMAND_FUZZY_THRESHOLD` (default 2). Optional.

---

## Tasks

### Task 1: Add a pure-Python Levenshtein helper

**Objective:** Provide edit-distance so distance thresholds are computed on a true edit distance (difflib `SequenceMatcher.ratio` is a similarity ratio, not an edit distance, and is already used only for player-name fuzzy match — do not reuse it here).

**Files:** Modify `src/amc/command_framework.py` (module-level function above `CommandRegistry`).

**Step 1 — Write failing test** in `src/amc/tests_command_framework.py`:

```python
from amc.command_framework import CommandRegistry, CommandContext, levenshtein

class TestLevenshtein(SimpleTestCase):
    def test_identical(self):
        self.assertEqual(levenshtein("help", "help"), 0)
    def test_single_substitution(self):
        self.assertEqual(levenshtein("hepl", "help"), 1)
    def test_single_insertion(self):
        self.assertEqual(levenshtein("hel", "help"), 1)
    def test_single_deletion(self):
        self.assertEqual(levenshtein("help", "hel"), 1)
    def test_transposition_is_two_edits(self):
        self.assertEqual(levenshtein("tp", "pt"), 2)
    def test_disjoint(self):
        self.assertEqual(levenshtein("abc", ""), 3)
    def test_case_sensitive_input_kept(self):
        self.assertEqual(levenshtein("HELP", "help"), 4)
    def test_empty_both(self):
        self.assertEqual(levenshtein("", ""), 0)
```

**Step 2 — Run to verify failure:** `pytest src/amc/tests_command_framework.py::TestLevenshtein -q` → FAIL (`ImportError`/`AttributeError`).

**Step 3 — Implement** (module-level in `command_framework.py`):

```python
def levenshtein(a: str, b: str) -> int:
    """Number of single-char edits (insert/delete/substitute) to turn a into b.
    Cheapest is via two rows — strings here are short (command tokens), O(min(n,m)) memory."""
    if a == b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(
                prev[j] + 1,          # deletion
                cur[j - 1] + 1,       # insertion
                prev[j - 1] + (ca != cb),  # substitution
            ))
        prev = cur
    return prev[-1]
```

**Step 4 — Run to verify pass:** `pytest src/amc/tests_command_framework.py::TestLevenshtein -q` → PASS.

**Step 5 — Commit:** `git add -A && git commit -m "feat: add levenshtein edit-distance helper"`.

---

### Task 2: Register an alias → command index on the registry

**Objective:** Precompute (lazily) a map of lowercase alias (sans leading `/`) → command descriptor, including flags for filtering (deprecated, category).

**Files:** Modify `src/amc/command_framework.py`.

**Step 1 — Write failing test:**

```python
class TestTypoSuggest(SimpleTestCase):
    def setUp(self):
        self.registry = CommandRegistry()
        self.ctx = MagicMock(spec=CommandContext)
        self.ctx.reply = AsyncMock()
        self.ctx.player = MagicMock()
        self.ctx.player.language = "en-gb"
        self.ctx.player_info = None  # not admin

        @self.registry.register(["/help", "/h"])
        async def cmd_help(ctx):
            await ctx.reply("help")

        @self.registry.register("/teleport")
        async def cmd_tp(ctx, target: str):
            await ctx.reply("tpd")

        @self.registry.register("/bill", deprecated=True)
        async def cmd_bill(ctx):
            await ctx.reply("billed")

    def _run(self, s):
        import asyncio
        return asyncio.run(self.registry.execute(s, self.ctx))

    def test_index_built(self):
        aliases = self.registry._alias_index()
        self.assertEqual(sorted(aliases), ["bill", "h", "help", "teleport"])
```

**Step 2 — run → FAIL.** **Step 3 — implement** a lazy index:

```python
def _alias_index(self):
    idx = {}
    for c in self.commands:
        for a in c["aliases"]:
            key = a.lstrip("/").lower()
            idx.setdefault(key, []).append(c)
    return idx
```

Store/refresh on demand in `execute()` (commands are static after autodiscover, so this can be memoized with a dirty flag if desired — simple lazy build per call is fine at this scale, but safer to cache on first use; keep it simple first).

**Step 4 — verify PASS. Step 5 — commit** `feat: add alias index to command registry`.

---

### Task 3: Two-tier auto-fix in `execute()` (auto-run dist ≤ 2, popup otherwise)

**Objective:** Hook typo-fix into the `return False` path of `execute()`: auto-execute unambiguous non-Admin corrections within 2 edits, popup everything else.

**Files:** Modify `src/amc/command_framework.py` (in `execute()`, right before `return False`), plus a `get_typo_suggestion(message, is_admin)` helper.

**Step 1 — Write failing tests:**

```python
    def test_auto_executes_dist1_unambiguous(self):
        # /hepl → dist1 to /help, non-Admin, single match → auto-runs, notice sent
        self.ctx.reply.reset_mock()
        result = self._run("/hepl")
        self.assertTrue(result)
        texts = [a.args[0] for a in self.ctx.reply.call_args_list]
        self.assertIn("help", texts)                        # command actually ran
        self.assertTrue(any("Running" in t and "/help" in t for t in texts))  # notice

    def test_auto_executes_dist2_unambiguous(self):
        # /tleport → /teleport is dist 2 (transposition), non-Admin, single match → auto-runs
        self.ctx.reply.reset_mock()
        result = self._run("/tleport")
        self.assertTrue(result)
        texts = [a.args[0] for a in self.ctx.reply.call_args_list]
        self.assertIn("tpd", texts)                          # command actually ran
        self.assertTrue(any("Running" in t and "/teleport" in t for t in texts))  # notice

    def test_typo_preserves_args_in_auto(self):
        self.ctx.reply.reset_mock()
        result = self._run("/tleport jason")                # dist2 → auto-runs /teleport jason
        self.assertTrue(result)
        texts = [a.args[0] for a in self.ctx.reply.call_args_list]
        self.assertTrue(any("Running" in t and "/teleport jason" in t for t in texts))

    def test_typo_exact_still_runs(self):
        self.ctx.reply.reset_mock()
        self._run("/help")
        self.assertEqual(self.ctx.reply.call_args_list[0].args[0], "help")

    def test_plain_chat_not_touched(self):
        self.ctx.reply.reset_mock()
        result = self._run("helo everyone")
        self.assertFalse(result)

    def test_far_typo_not_suggested(self):
        self.ctx.reply.reset_mock()
        result = self._run("/zzzzzz")
        self.assertFalse(result)

    def test_deprecated_not_suggested(self):
        result = self._run("/bll")                          # /bill deprecated → nothing
        self.assertFalse(result)

    def test_ambiguous_tie_lists_candidates(self):
        @self.registry.register("/a")
        async def cmd_a(ctx): ...
        @self.registry.register("/d")
        async def cmd_d(ctx): ...
        result = self._run("/b")                            # ties /a and /d at dist 1
        self.assertTrue(result)
        text = self.ctx.reply.call_args.args[0]
        self.assertIn("/a", text)
        self.assertIn("/d", text)

    def test_admin_cmd_not_exposed_to_non_admin(self):
        @self.registry.register("/spawn", category="Admin")
        async def cmd_spawn(ctx, vehicle: str = None):
            await ctx.reply("spawned")
        result = self._run("/sedpwn")                       # non-admin → filtered → nothing
        self.assertFalse(result)

    def test_admin_cmd_never_auto_executes(self):
        # admin player, dist1 typo on an Admin command → popup, NOT auto-run
        self.ctx.player_info = {"bIsAdmin": True}
        self.ctx.reply.reset_mock()
        @self.registry.register("/teleport", category="Admin")
        async def cmd_tp(ctx, target: str): ...
        result = self._run("/tleport bob")
        self.assertTrue(result)
        texts = [a.args[0] for a in self.ctx.reply.call_args_list]
        self.assertTrue(any("/teleport" in t for t in texts))     # suggested
        self.assertFalse(any("Running" in t for t in texts))       # not auto-run
```

(For the ambiguity test, `ctx.player_info` is `None` → `is_admin=False`; the TestCase registers extra `/a`/`/d` as General so they remain candidates.)

**Step 2 — run → FAIL.** **Step 3 — implement**:

```python
AUTO_EXECUTE_DIST = 2  # within this distance (and unambiguous & non-Admin) → auto-run
THRESHOLD = getattr(settings, "COMMAND_FUZZY_THRESHOLD", 2)

def _is_admin(self, ctx) -> bool:
    pi = getattr(ctx, "player_info", None) or {}
    return bool(pi.get("bIsAdmin"))

def get_typo_suggestion(self, message: str, is_admin: bool):
    """Return (dist, best_cmds, corrected_full) or None.
    - dist: best edit distance (~ if none within threshold)
    - best_cmds: all distinct commands tied at best dist (filtered/deduped)
    - corrected_full: "cmd rest" string for single unambiguous match
    Caller decides auto-execute vs popup using dist + len(best_cmds) + perm."""

    if not message or not message.startswith("/"):
        return None
    parts = message.split(None, 1)
    token = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""
    query = token.lstrip("/")

    best_dist = None
    best_cmds = []
    for alias, cmds in self._alias_index().items():
        for c in cmds:
            if c.get("deprecated"):
                continue                                   # never suggest/run /bill etc.
            if not is_admin and c.get("category") == "Admin":
                continue                                   # never expose admin cmds to non-admins
            d = levenshtein(query, alias)
            if best_dist is None or d < best_dist:
                best_dist, best_cmds = d, [c]
            elif d == best_dist and c not in best_cmds:
                best_cmds.append(c)                        # dedupe aliases → distinct cmds only

    if best_dist is None or best_dist > THRESHOLD:
        return None
    corrected_full = f"{best_cmds[0]['name']} {rest}".strip() if len(best_cmds) == 1 else None
    return best_dist, best_cmds, corrected_full
```

Then in `execute()`, replace the final `return False`:

```python
        if message.startswith("/"):
            is_admin = self._is_admin(ctx)
            sugg = self.get_typo_suggestion(message, is_admin)
            if sugg:
                best_dist, best_cmds, corrected_full = sugg
                resolved = best_cmds[0]

                # AUTO-EXECUTE: dist<=1, unambiguous, non-Admin only
                if (
                    best_dist <= AUTO_EXECUTE_DIST
                    and len(best_cmds) == 1
                    and resolved.get("category") != "Admin"
                ):
                    await ctx.reply(_("Running {cmd} instead of {typed}.").format(
                        cmd=corrected_full, typed=message.split()[0]))
                    # re-enter with corrected token + original args; exact alias → no typo recursion
                    return await self.execute(corrected_full, ctx)

                # POPUP: ambiguous → list candidates with localized descriptions
                if len(best_cmds) > 1:
                    lines = []
                    for c in best_cmds:
                        desc = str(c.get("description", ""))
                        lines.append(f"<Highlight>{c['name']}</> - {desc}")
                    await ctx.reply(
                        _("<Title>Did you mean one of these?</>\n"
                          "<Secondary>Type the full command to run it.</>\n\n{cmds}")
                        .format(cmds="\n".join(lines)))
                # POPUP: single suggestion (dist 2, or any Admin command)
                else:
                    await ctx.reply(
                        _("<Title>Did you mean</>\n\n{cmd}? "
                          "Type it to run it.").format(cmd=corrected_full))
                return True
        return False
```

Notes for the implementer:
- `return await self.execute(corrected_full, ctx)` — recursion is safe (corrected token is an exact registered alias, so the re-run matches via the normal regex path). The "Running … instead of …" popup is sent before re-entering so the player sees it regardless of what the command itself does.
- Wrap the whole typo branch in the existing `translation.override(player.language)` context so both the notice and any tie-list render in the player's language.
- `THRESHOLD`/`AUTO_EXECUTE_DIST` resolve against `settings` at import; guard so tests without settings default to 2 / 2.

**Step 4 — verify PASS.** **Step 5 — commit** `feat: auto-fix command typos (auto-run ≤2 edits, suggestion popups)`.

---

### Task 4: Threshold + permission sanity tests, ruff & full suite

**Objective:** Tighten edge cases and run the real gates.

**Files:** `src/amc/tests_command_framework.py` (+ optionally `src/amc_backend/settings.py` for the constant).

**Tests to add:**
- Dist boundary: a token at dist 2 is auto-run (non-Admin & unambiguous); a token **at dist 3 (over threshold)** is not suggested at all (`None`/`False`).
- **Recursion-safety guard:** auto-execute path re-enters `execute()` exactly once and does not loop — assert `reply` called a bounded number of times and the command ran once.
- Short-token ambiguity guard is confirmed (Task 3 test).
- Case-insensitivity: `/HEPL` → auto-runs `/help`.
- Message with only a slash `/` → not suggested, returns `False`.

**Run:**
```bash
direnv exec . python -m pytest src/amc/tests_command_framework.py -v --tb=short
# then the broader suite to catch regressions in dispatch:
direnv exec . python -m pytest src/amc/ --tb=short -q
```

`nix flake check .#pytest` also works (spins up temp Postgres+Redis). Note: amc-backend **GitHub CI is known-broken** (malformed `ci.yml` → 0 jobs; only ruff+django-check). Treat CI as noise; rely on local pytest.

**Commit** `test: typo suggestion threshold + permissions`.

---

## Verification / Acceptance

- Player types `/hepl` → dist-1, unambiguous, non-Admin → **auto-runs** `/help`, shows "Running /help instead of /hepl", not forwarded to Discord.
- Player types `/tleport <name>` (dist-2 transpose) → **auto-runs** `/teleport <name>` with the same notice.
- Player types `/b` (ambiguous, ties `/a` `/d`) → both listed with descriptions, nothing auto-run.
- Admin player types `/tleport bob` on Admin `/teleport` → **suggested, never auto-run** (Admin guard).
- Non-admin typing a typo of an Admin command (`/sedpwn`) → nothing (not exposed).
- Player types `helo` (no slash) → **unchanged**, forwarded as normal chat.
- Deprecated `/bill` never suggested or run.
- All existing slash commands still dispatch normally.

## Risks / Tradeoffs / Open Questions

- **Auto-execute scope.** Auto-run fires for **dist ≤ 2, unambiguous, non-Admin** corrections (same 2 as the overall threshold, so effectively "anything unambiguous within threshold auto-runs"). The Admin guard is the one belt-and-suspenders guard I added beyond the literal ask — a ≤2-edit typo on `/teleport`/`/mute`/`/despawn`/`/spawn*` with the player's args is the highest-blast-radius case, and it's deliberately popup-only. If you later want admin auto-exec too, drop the `category != "Admin"` clause — but I'd keep it.
- **Ambiguity ties** (`/b` ↔ `/a`/`/d`, or `/spawn` family) always popup with **localized descriptions** — never guess, never auto-run a tie. This is why suggestion descriptions matter: make sure every command's `description` is meaningful (some are one-liners today).
- **Threshold tuning:** `COMMAND_FUZZY_THRESHOLD` (default 2) doubles as the auto-exec distance (`AUTO_EXECUTE_DIST`, default 2). At 2 the true ambiguity shadow is narrow, but if surprise auto-runs show up the knob is to lower `AUTO_EXECUTE_DIST` below `THRESHOLD` (so dist-2 becomes popup-only) or add a length-dependent cap. Watch `BotInvocationLog` / logs for surprise auto-runs after launch.
- **Non-command slash usage:** players may use other `/`-prefixed chats (RP, other bots). Threshold guard + suggest-not-auto for ambiguous covers most of it, but keep an eye on what gets suggested/auto-run.
- **Cost:** pure-Python Levenshtein over ~78 short aliases per failed slash message is negligible.

## Decided (locked with freeman)
- **Auto-execute** when edit distance ≤ 2, unambiguous, and non-Admin. All else → popup (single suggestion for Admin commands, or tie-list). Tie-list shows descriptions, never guesses. Admin commands never auto-run.
