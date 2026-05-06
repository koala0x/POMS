CREATE TABLE IF NOT EXISTS summary_level1 (
    id BIGSERIAL PRIMARY KEY,
    source VARCHAR NOT NULL,
    summary TEXT NOT NULL,
    raw_ids BIGINT[] NOT NULL,
    raw_count INT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    is_summarized_l2 BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_summary_level1_source_created_at
ON summary_level1 (source, created_at);

CREATE INDEX IF NOT EXISTS idx_summary_level1_is_summarized_l2_created_at
ON summary_level1 (is_summarized_l2, created_at);
