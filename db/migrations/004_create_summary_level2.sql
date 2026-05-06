CREATE TABLE IF NOT EXISTS summary_level2 (
    id BIGSERIAL PRIMARY KEY,
    source VARCHAR NOT NULL,
    summary TEXT NOT NULL,
    level1_ids BIGINT[] NOT NULL,
    level1_count INT NOT NULL,
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_summary_level2_source_created_at
ON summary_level2 (source, created_at);

CREATE INDEX IF NOT EXISTS idx_summary_level2_source_period
ON summary_level2 (source, period_start, period_end);
