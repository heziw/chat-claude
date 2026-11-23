/** Row shape of the `messages` table (see shared/CONTRACTS.md). */
export interface MessageEntity {
  id: string;
  conversation_id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  citations: unknown[];
  /** Base64 data URLs of images attached to this message (general-chat
   *  route only -- see shared/CONTRACTS.md "images"/"attachments"). */
  attachments: unknown[];
  created_at: Date;
}
