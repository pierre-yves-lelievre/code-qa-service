-- 0003_feedback: a thumbs up or down on an answer; null until someone rates it.

ALTER TABLE queries ADD COLUMN feedback text CHECK (feedback IN ('up', 'down'));
