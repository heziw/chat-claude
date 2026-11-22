// Mirrors the `messages` table + Milvus `doc_chunks` fields used for citations
// (source_ref / text) from shared/CONTRACTS.md.
export type MessageRole = 'user' | 'assistant' | 'system';

export interface Citation {
  source_ref: string; // e.g. "עמוד 12" / "שורה 45" / "תא B7"
  snippet?: string;
  file_id?: string;
}

export interface Message {
  id: string;
  conversation_id: string;
  role: MessageRole;
  content: string;
  citations: Citation[];
  created_at: string;
}
