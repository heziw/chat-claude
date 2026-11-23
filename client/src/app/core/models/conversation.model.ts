// Mirrors the `conversations` table from shared/CONTRACTS.md.
export interface Conversation {
  id: string;
  user_id: string;
  title: string;
  retention_days: number;
  last_active_at: string; // ISO timestamp
  status: 'active' | 'purged';
  /** Custom instruction sent as a system-role message on every query in
   *  this conversation (see shared/CONTRACTS.md "system_prompt"). `null`
   *  means no custom instruction is set. */
  system_prompt: string | null;
  created_at: string;
}

export interface CreateConversationRequest {
  title?: string;
}
