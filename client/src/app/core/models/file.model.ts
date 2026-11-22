// Mirrors the `file_metadata` table and the ingestion-service status contract
// from shared/CONTRACTS.md.
export type IndexStatus = 'pending' | 'indexing' | 'ready' | 'failed';

export interface FileMetadata {
  id: string;
  conversation_id: string;
  original_name: string;
  file_type: 'pdf' | 'docx' | 'xlsx' | 'csv' | 'json' | 'image' | string;
  nfs_path: string;
  size_bytes: number;
  index_status: IndexStatus;
  milvus_partition?: string | null;
  structured_store_ref?: string | null;
  error_message?: string | null;
  created_at: string;
}

// Response from POST /conversations/:id/files (gateway proxies to
// ingestion-service POST /ingest, which returns 202 + {task_id, file_id}).
export interface UploadFileResponse {
  task_id: string;
  file_id: string;
}

// Response from GET /conversations/:id/files/:fileId/status (gateway proxies
// to ingestion-service GET /ingest/{task_id}/status -> {status, file_id, error}).
export interface FileStatusResponse {
  status: IndexStatus;
  file_id: string;
  error?: string | null;
}

// Client-side tracking model for a file being uploaded/indexed in the chat UI.
export interface UploadingFile {
  fileId: string;
  originalName: string;
  status: IndexStatus;
  errorMessage?: string | null;
}
