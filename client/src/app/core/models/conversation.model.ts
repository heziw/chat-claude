// Mirrors the `conversations` table from shared/CONTRACTS.md.
export interface Conversation {
  id: string;
  user_id: string;
  title: string;
  retention_days: number;
  last_active_at: string; // ISO timestamp
  status: 'active' | 'purged';
  created_at: string;
}

export interface CreateConversationRequest {
  title?: string;
}
