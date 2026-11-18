-- AMC Workshop Reader: read-only, COLUMN-SCOPED role for the workshop site
--
-- The workshop answers exactly one question — "which Steam id and character
-- GUIDs belong to this Discord user" — so this role can read four columns and
-- nothing else. No table-wide SELECT: a mistake in the query layer shows up as
-- a permission error instead of handing an internet-facing service the whole
-- player table.
--
-- For EXISTING databases, run this manually:
--   sudo -u postgres psql -d amc -f sql/setup_workshop_ro.sql
--
-- The password is set separately (never commit it):
--   ALTER ROLE amc_workshop_ro PASSWORD '<generated>';
--
-- Connections are allowed from the Tailscale range only (see the
-- services.postgresql.authentication block in flake.nix).

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'amc_workshop_ro') THEN
    CREATE ROLE amc_workshop_ro LOGIN;
  END IF;
END
$$;

-- Defence in depth: even a compromised client cannot write, and cannot hold a
-- transaction open long enough to matter.
ALTER ROLE amc_workshop_ro SET statement_timeout = '3s';
ALTER ROLE amc_workshop_ro SET default_transaction_read_only = on;
ALTER ROLE amc_workshop_ro SET idle_in_transaction_session_timeout = '5s';

GRANT CONNECT ON DATABASE amc TO amc_workshop_ro;
GRANT USAGE ON SCHEMA public TO amc_workshop_ro;

GRANT SELECT (unique_id, discord_user_id) ON amc_player TO amc_workshop_ro;
GRANT SELECT (guid, player_id) ON amc_character TO amc_workshop_ro;

-- Verification (run manually):
--   SET ROLE amc_workshop_ro;
--   SELECT count(*) FROM amc_character;                -- works
--   SELECT name FROM amc_character LIMIT 1;            -- permission denied
--   UPDATE amc_character SET name = name;              -- permission denied
--   RESET ROLE;
