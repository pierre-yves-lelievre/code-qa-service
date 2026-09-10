-- Test database, separate from the dev `codeqa` so tests never see real jobs or snapshots.
-- Runs from /docker-entrypoint-initdb.d on a fresh volume, and from `make db` on an existing one.
SELECT 'CREATE DATABASE codeqa_test'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'codeqa_test')\gexec
