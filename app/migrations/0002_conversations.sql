-- 0002_conversations: every logged query belongs to a conversation; history is read back by it.
-- NOT NULL without a default is safe: nothing writes `queries` before this migration.

ALTER TABLE queries ADD COLUMN conversation_id uuid NOT NULL;

CREATE INDEX queries_conversation ON queries (repo_id, conversation_id, id);
