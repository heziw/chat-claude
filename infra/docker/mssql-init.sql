-- MSSQL schema for local dev / testing against a disposable container.
-- See /shared/CONTRACTS.md "Relational DB: Microsoft SQL Server" for the
-- authoritative schema + the Postgres->MSSQL conversion rules this follows.
-- Run against database `chatdb` (created by docker-entrypoint-initdb.d
-- tooling below, or manually: CREATE DATABASE chatdb;).

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'users')
CREATE TABLE users (
  id NVARCHAR(36) PRIMARY KEY,
  sso_subject NVARCHAR(255) UNIQUE NOT NULL,
  display_name NVARCHAR(255) NOT NULL,
  role NVARCHAR(50) NOT NULL DEFAULT 'user',
  created_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET()
);

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'conversations')
CREATE TABLE conversations (
  id NVARCHAR(36) PRIMARY KEY,
  user_id NVARCHAR(36) NOT NULL REFERENCES users(id),
  title NVARCHAR(500) NOT NULL DEFAULT N'שיחה חדשה',
  retention_days INT NOT NULL DEFAULT 90,
  last_active_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET(),
  status NVARCHAR(20) NOT NULL DEFAULT 'active',
  system_prompt NVARCHAR(MAX) NULL,
  created_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET()
);

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'messages')
CREATE TABLE messages (
  id NVARCHAR(36) PRIMARY KEY,
  conversation_id NVARCHAR(36) NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role NVARCHAR(20) NOT NULL,
  content NVARCHAR(MAX) NOT NULL,
  citations NVARCHAR(MAX) NOT NULL DEFAULT '[]',
  attachments NVARCHAR(MAX) NOT NULL DEFAULT '[]',
  created_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET()
);

IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'file_metadata')
CREATE TABLE file_metadata (
  id NVARCHAR(36) PRIMARY KEY,
  conversation_id NVARCHAR(36) NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  original_name NVARCHAR(500) NOT NULL,
  file_type NVARCHAR(20) NOT NULL,
  nfs_path NVARCHAR(1000) NOT NULL,
  size_bytes BIGINT NOT NULL,
  index_status NVARCHAR(20) NOT NULL DEFAULT 'pending',
  milvus_partition NVARCHAR(100) NULL,
  structured_store_ref NVARCHAR(1000) NULL,
  error_message NVARCHAR(MAX) NULL,
  created_at DATETIMEOFFSET NOT NULL DEFAULT SYSDATETIMEOFFSET()
);

-- Migrations for columns added after the tables above first shipped --
-- guarded so this script stays idempotent against an already-running DB
-- (docker-entrypoint-initdb.d only runs CREATE DATABASE scripts once, but
-- this file is also re-run manually in dev after a schema change).
IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('conversations') AND name = 'system_prompt')
ALTER TABLE conversations ADD system_prompt NVARCHAR(MAX) NULL;

IF NOT EXISTS (SELECT 1 FROM sys.columns WHERE object_id = OBJECT_ID('messages') AND name = 'attachments')
ALTER TABLE messages ADD attachments NVARCHAR(MAX) NOT NULL DEFAULT '[]';

IF NOT EXISTS (SELECT 1 FROM users WHERE sso_subject = 'dev-user')
INSERT INTO users (id, sso_subject, display_name, role)
VALUES ('00000000-0000-4000-8000-000000000000', 'dev-user', 'Dev User', 'admin');
