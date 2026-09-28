-- Phase 1: watchlist, alerts, alert delivery bookkeeping.
-- Single-tenant (this server has one owner, gated by MCP_SECRET), so no user_id columns.

CREATE TABLE IF NOT EXISTS watchlist (
    symbol      text PRIMARY KEY,
    target_buy  numeric,
    target_sell numeric,
    reason      text,
    priority    text NOT NULL DEFAULT 'medium' CHECK (priority IN ('low', 'medium', 'high')),
    notes       text,
    tags        text[] NOT NULL DEFAULT '{}',
    status      text NOT NULL DEFAULT 'watching' CHECK (status IN ('watching', 'paused', 'hit', 'archived')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS alerts (
    id              bigserial PRIMARY KEY,
    symbol          text NOT NULL,
    type            text NOT NULL CHECK (type IN ('price_above', 'price_below', 'volume_spike', 'new_announcement')),
    threshold       numeric,
    status          text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'triggered', 'disabled')),
    last_checked_at timestamptz,
    last_fired_at   timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (symbol, type)
);

CREATE TABLE IF NOT EXISTS alert_events (
    id           bigserial PRIMARY KEY,
    alert_id     bigint REFERENCES alerts(id) ON DELETE CASCADE,
    symbol       text NOT NULL,
    type         text NOT NULL,
    message      text NOT NULL,
    payload      jsonb,
    triggered_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS alert_events_triggered_at_idx ON alert_events (triggered_at DESC);

-- Surrogate ID (PDF url, or a hash when there is none) of the newest
-- announcement we've already told the user about, per symbol.
CREATE TABLE IF NOT EXISTS last_seen_announcement (
    symbol          text PRIMARY KEY,
    last_id         text,
    last_checked_at timestamptz NOT NULL DEFAULT now()
);

-- One row, updated by the /api/cron/check-alerts endpoint every run, so
-- psx_selftest can tell whether the scheduled job is actually firing.
CREATE TABLE IF NOT EXISTS cron_state (
    id                     smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_run_at            timestamptz,
    last_alerts_evaluated  int,
    last_events_created    int,
    last_error             text
);
INSERT INTO cron_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
