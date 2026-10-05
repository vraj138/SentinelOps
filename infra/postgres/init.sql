-- Runs once, when the Postgres volume is first created.
CREATE TABLE IF NOT EXISTS orders (
    id           SERIAL PRIMARY KEY,
    item         TEXT        NOT NULL,
    amount_cents INTEGER     NOT NULL CHECK (amount_cents > 0),
    status       TEXT        NOT NULL,
    charge_id    TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
