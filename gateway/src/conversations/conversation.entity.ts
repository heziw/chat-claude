/** Row shape of the `conversations` table (see shared/CONTRACTS.md). */
export interface ConversationEntity {
  id: string;
  user_id: string;
  title: string;
  retention_days: number;
  last_active_at: Date;
  status: 'active' | 'purged';
  /** Custom instruction sent as a system-role message on every /query call
   *  for this conversation (see shared/CONTRACTS.md "system_prompt").
   *  Editable via PATCH /conversations/:id/system-prompt. */
  system_prompt: string | null;
  created_at: Date;
}
