ALTER TABLE binance_square_posts
ADD COLUMN IF NOT EXISTS is_summarized BOOLEAN NOT NULL DEFAULT FALSE;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_name = 'binance_square_posts'
      AND column_name = 'created_at'
      AND table_schema = ANY (current_schemas(true))
  ) THEN
    EXECUTE 'CREATE INDEX IF NOT EXISTS idx_binance_square_posts_is_summarized_created_at ON binance_square_posts (is_summarized, created_at);';
  ELSE
    EXECUTE 'CREATE INDEX IF NOT EXISTS idx_binance_square_posts_is_summarized_id ON binance_square_posts (is_summarized, id);';
  END IF;
END $$;
