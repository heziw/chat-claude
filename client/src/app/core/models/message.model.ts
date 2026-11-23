// Mirrors the `messages` table + Milvus `doc_chunks` fields used for citations
// (source_ref / text) from shared/CONTRACTS.md.
export type MessageRole = 'user' | 'assistant' | 'system';

export interface Citation {
  source_ref: string; // e.g. "עמוד 12" / "שורה 45" / "תא B7"
  snippet?: string;
  file_id?: string;
}

/** Judge verdict shape, shared by all three routes that run one (see
 * shared/CONTRACTS.md "reasoning" event) -- only present when
 * thinking_level=="deep" actually ran a judge. */
export interface ReasoningJudge {
  satisfactory: boolean;
  reason: string;
  corrected?: boolean | null;
  missing_file_ids?: string[];
}

/** Payload of the `reasoning` SSE event -- content depends on `route`;
 * only structured/summarize/multi ever emit one (factual/general have no
 * "reasoning trace" beyond the citations already shown). */
export interface ReasoningInfo {
  route: 'structured' | 'summarize' | 'multi';
  steps?: { description: string; code: string }[]; // structured
  sub_questions?: string[]; // multi
  file?: string; // summarize
  chunk_count?: number; // summarize
  judge?: ReasoningJudge;
}

export interface Message {
  id: string;
  conversation_id: string;
  role: MessageRole;
  content: string;
  citations: Citation[];
  created_at: string;
  /** Client-side only, captured from the `reasoning` SSE event when this
   *  message was live-streamed (see ChatService.streamingReasoning /
   *  shared/CONTRACTS.md "reasoning" event). Not part of the server's
   *  message row and not persisted -- a message reloaded from
   *  GET /conversations/:id/messages (an old conversation) will never have
   *  this set, since the reasoning trace only ever existed during that
   *  original live stream. */
  reasoning?: ReasoningInfo;
}
