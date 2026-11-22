/** Row shape of the `conversations` table (see shared/CONTRACTS.md). */
export interface ConversationEntity {
  id: string;
  user_id: string;
  title: string;
  retention_days: number;
  last_active_at: Date;
  status: 'active' | 'purged';
  created_at: Date;
}
