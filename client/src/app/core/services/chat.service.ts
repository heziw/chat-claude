import { Injectable, signal } from '@angular/core';
import { environment } from '../../../environments/environment';
import { Citation, ReasoningInfo } from '../models';
import { AuthService } from './auth.service';

export interface ChatHistoryTurn {
  role: 'user' | 'assistant';
  content: string;
}

/** "fast"|"standard"|"deep" -- see shared/CONTRACTS.md "thinking_level".
 * User-selectable per question (see chat.component.ts); forwarded as-is
 * through the gateway to retrieval-orchestrator and on to whichever
 * downstream service actually serves the question. */
export type ThinkingLevel = 'fast' | 'standard' | 'deep';

export interface StreamQueryCallbacks {
  onDone?: (finalText: string, citations: Citation[]) => void;
  onError?: (message: string) => void;
}

/**
 * Handles the SSE streaming logic for POST /conversations/:id/query.
 *
 * The gateway proxies this to the retrieval-orchestrator's `/query`, whose
 * response is a `text/event-stream` with events: `token` (delta text),
 * `citation` (source_ref + snippet) and `done` (see shared/CONTRACTS.md).
 *
 * `EventSource` cannot send a POST body, so we use `fetch` +
 * `ReadableStream` and parse the SSE wire format by hand.
 */
@Injectable({ providedIn: 'root' })
export class ChatService {
  /** Current in-flight assistant answer, updated token-by-token. Components
   *  bind directly to this signal to render live streaming text. */
  readonly streamingText = signal<string>('');
  readonly streamingCitations = signal<Citation[]>([]);
  readonly isStreaming = signal<boolean>(false);
  readonly streamError = signal<string | null>(null);
  /** Current `status` event's label (e.g. "מחפש ומנתח מידע רלוונטי..."),
   *  or null once real answer content starts arriving / once done. Not
   *  persisted -- only meaningful during the live stream (see
   *  CONTRACTS.md's `status` event: ephemeral, not stored in MSSQL). */
  readonly processingStatus = signal<string | null>(null);
  /** The current answer's `reasoning` event, if the route emitted one.
   *  Same ephemeral/not-persisted caveat as processingStatus. */
  readonly streamingReasoning = signal<ReasoningInfo | null>(null);

  private abortController: AbortController | null = null;

  constructor(private readonly auth: AuthService) {}

  async sendQuery(
    conversationId: string,
    question: string,
    history: ChatHistoryTurn[] = [],
    thinkingLevel: ThinkingLevel = 'standard',
    callbacks: StreamQueryCallbacks = {}
  ): Promise<void> {
    this.cancel(); // make sure no previous stream is still running

    this.streamingText.set('');
    this.streamingCitations.set([]);
    this.processingStatus.set(null);
    this.streamingReasoning.set(null);
    this.streamError.set(null);
    this.isStreaming.set(true);

    const controller = new AbortController();
    this.abortController = controller;

    const token = this.auth.token();
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream'
    };
    if (token) {
      headers['Authorization'] = `Bearer ${token}`;
    }

    try {
      const response = await fetch(
        `${environment.apiBaseUrl}/conversations/${conversationId}/query`,
        {
          method: 'POST',
          headers,
          body: JSON.stringify({
            conversation_id: conversationId,
            question,
            history,
            thinking_level: thinkingLevel
          }),
          signal: controller.signal
        }
      );

      if (!response.ok || !response.body) {
        throw new Error(`השרת החזיר שגיאה (${response.status})`);
      }

      await this.consumeSseStream(response.body, callbacks);
    } catch (error) {
      if ((error as DOMException)?.name === 'AbortError') {
        return; // cancelled on purpose, not a real error
      }
      const message = error instanceof Error ? error.message : 'שגיאה בלתי צפויה בעת שליחת השאלה';
      this.streamError.set(message);
      callbacks.onError?.(message);
    } finally {
      this.isStreaming.set(false);
      this.abortController = null;
    }
  }

  cancel(): void {
    this.abortController?.abort();
    this.abortController = null;
  }

  private async consumeSseStream(
    body: ReadableStream<Uint8Array>,
    callbacks: StreamQueryCallbacks
  ): Promise<void> {
    const reader = body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });

      // SSE events are separated by a blank line ("\n\n").
      let boundary = buffer.indexOf('\n\n');
      while (boundary !== -1) {
        const rawEvent = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        this.handleRawEvent(rawEvent, callbacks);
        boundary = buffer.indexOf('\n\n');
      }
    }

    // Flush any trailing event without a final blank line.
    if (buffer.trim().length > 0) {
      this.handleRawEvent(buffer, callbacks);
    }
  }

  private handleRawEvent(rawEvent: string, callbacks: StreamQueryCallbacks): void {
    let eventName = 'message';
    const dataLines: string[] = [];

    for (const line of rawEvent.split('\n')) {
      if (line.startsWith('event:')) {
        eventName = line.slice('event:'.length).trim();
      } else if (line.startsWith('data:')) {
        // Per the SSE spec, only a single leading space after `data:` is a
        // separator; the rest of the value (including meaningful leading/
        // trailing whitespace from token-by-token streaming) must be kept.
        const value = line.slice('data:'.length);
        dataLines.push(value.startsWith(' ') ? value.slice(1) : value);
      }
    }

    if (dataLines.length === 0) return;
    const rawData = dataLines.join('\n');

    // Token/status payloads are plain text deltas by contract (e.g.
    // "315000" or " מדומה") and must never be JSON-sniffed: a bare
    // numeric/boolean/null value is itself valid JSON
    // (`JSON.parse("315000") === 315000`), so blindly parsing every one
    // would silently turn a real numeric answer token into a non-string
    // that extractTokenDelta drops. Only `citation`/`reasoning` are genuine
    // JSON object envelopes, so only attempt parsing for those (and still
    // only for object-shaped payloads).
    let payload: unknown = rawData;
    if ((eventName === 'citation' || eventName === 'reasoning') && rawData.trimStart().startsWith('{')) {
      try {
        payload = JSON.parse(rawData);
      } catch {
        // Malformed JSON — keep the raw string; the per-event handler below
        // rejects a non-object rather than throwing.
      }
    }

    switch (eventName) {
      case 'status': {
        this.processingStatus.set(typeof payload === 'string' ? payload : rawData);
        break;
      }
      case 'reasoning': {
        if (payload && typeof payload === 'object') {
          this.streamingReasoning.set(payload as ReasoningInfo);
        }
        break;
      }
      case 'token': {
        const delta = this.extractTokenDelta(payload);
        this.streamingText.update((current) => current + delta);
        // Real content has started arriving -- whatever "still working on
        // it" status was showing is now stale, clear it.
        this.processingStatus.set(null);
        break;
      }
      case 'citation': {
        const citation = this.extractCitation(payload);
        if (citation) {
          this.streamingCitations.update((current) => [...current, citation]);
        }
        break;
      }
      case 'done': {
        this.processingStatus.set(null);
        callbacks.onDone?.(this.streamingText(), this.streamingCitations());
        break;
      }
      default:
        break;
    }
  }

  private extractTokenDelta(payload: unknown): string {
    if (typeof payload === 'string') return payload;
    if (payload && typeof payload === 'object') {
      const obj = payload as Record<string, unknown>;
      const delta = obj['delta'] ?? obj['token'] ?? obj['text'] ?? obj['content'];
      if (typeof delta === 'string') return delta;
    }
    return '';
  }

  private extractCitation(payload: unknown): Citation | null {
    if (!payload || typeof payload !== 'object') return null;
    const obj = payload as Record<string, unknown>;
    const sourceRef = obj['source_ref'];
    if (typeof sourceRef !== 'string') return null;
    return {
      source_ref: sourceRef,
      snippet: typeof obj['snippet'] === 'string' ? (obj['snippet'] as string) : undefined,
      file_id: typeof obj['file_id'] === 'string' ? (obj['file_id'] as string) : undefined
    };
  }
}
