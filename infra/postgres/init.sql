-- Runs once, when the Postgres volume is first created.
CREATE TABLE IF NOT EXISTS orders (
    id           SERIAL PRIMARY KEY,
    item         TEXT        NOT NULL,
    amount_cents INTEGER     NOT NULL CHECK (amount_cents > 0),
    status       TEXT        NOT NULL,
    charge_id    TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- orders connects as a normal (non-superuser) role. If it leaks connections, Postgres still
-- keeps its reserved superuser slots free, so operators can inspect pg_stat_activity.
CREATE ROLE orders_app LOGIN PASSWORD 'orders_app';
GRANT SELECT, INSERT, UPDATE ON orders TO orders_app;
GRANT USAGE ON SEQUENCE orders_id_seq TO orders_app;
