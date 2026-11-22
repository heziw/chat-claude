/** Row shape of the `messages` table (see shared/CONTRACTS.md). */
export interface MessageEntity {
  id: string;
  conversation_id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  citations: unknown[];
  created_at: Date;
}
