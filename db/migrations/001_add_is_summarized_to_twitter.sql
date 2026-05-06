ALTER TABLE twitter_posts
ADD COLUMN IF NOT EXISTS is_summarized BOOLEAN NOT NULL DEFAULT FALSE;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_name = 'twitter_posts'
      AND column_name = 'created_at'
      AND table_schema = ANY (current_schemas(true))
  ) THEN
    EXECUTE 'CREATE INDEX IF NOT EXISTS idx_twitter_posts_is_summarized_created_at ON twitter_posts (is_summarized, created_at);';
  ELSE
    EXECUTE 'CREATE INDEX IF NOT EXISTS idx_twitter_posts_is_summarized_id ON twitter_posts (is_summarized, id);';
  END IF;
END $$;
