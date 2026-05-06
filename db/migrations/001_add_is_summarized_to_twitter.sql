ALTER TABLE twitter_posts
ADD COLUMN IF NOT EXISTS is_summarized BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_twitter_posts_is_summarized_created_at
ON twitter_posts (is_summarized, created_at);
