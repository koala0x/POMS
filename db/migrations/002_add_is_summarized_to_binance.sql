ALTER TABLE binance_square_posts
ADD COLUMN IF NOT EXISTS is_summarized BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_binance_square_posts_is_summarized_created_at
ON binance_square_posts (is_summarized, created_at);
