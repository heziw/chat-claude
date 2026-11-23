import { Injectable, Logger } from '@nestjs/common';
import { Response } from 'express';
import { RetrievalClient } from '../common/http/retrieval.client';
import { SseParser } from '../common/sse/sse-parser';
import { ConversationsService } from '../conversations/conversations.service';
import { MessagesService } from '../conversations/messages.service';

const HISTORY_LIMIT = 20;

@Injectable()
export class QueryService {
  private readonly logger = new Logger(QueryService.name);

  constructor(
    private readonly retrievalClient: RetrievalClient,
    private readonly messagesService: MessagesService,
    private readonly conversationsService: ConversationsService,
  ) {}

  /**
   * Runs the full query flow for `POST /conversations/:id/query`:
   *  1. persist the user's question as a `messages` row
   *  2. open an SSE stream to retrieval-orchestrator and forward it byte-for-byte to `res`
   *  3. accumulate `token` deltas and `citation` events while forwarding
   *  4. on `done`, persist the assistant's reply (content + citations) and end the response
   *
   * Writes directly to `res` (expects the caller to use `@Res({ passthrough: false })`),
   * since this needs full control over SSE framing and can't be expressed as a
   * simple return value.
   */
  async streamAndPersist(
    conversationId: string,
    userId: string,
    question: string,
    thinkingLevel: 'fast' | 'standard' | 'deep',
    res: Response,
    images?: string[],
  ): Promise<void> {
    const conversation = await this.conversationsService.getOwned(conversationId, userId);
    await this.messagesService.addMessage(conversationId, 'user', question, [], images ?? []);

    const priorMessages = await this.messagesService.listByConversation(conversationId);
    const history = priorMessages
      .slice(-HISTORY_LIMIT - 1, -1) // exclude the question we just inserted
      .filter((m) => m.role === 'user' || m.role === 'assistant')
      .map((m) => ({ role: m.role, content: m.content }));

    res.setHeader('Content-Type', 'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('Connection', 'keep-alive');
    res.setHeader('X-Accel-Buffering', 'no');
    (res as any).flushHeaders?.();

    let upstream;
    try {
      upstream = await this.retrievalClient.streamQuery(
        conversationId,
        question,
        history,
        thinkingLevel,
        conversation.system_prompt,
        images,
      );
    } catch (err: any) {
      this.logger.error(`Failed to reach retrieval-orchestrator: ${err.message}`);
      res.write(`event: error\ndata: ${JSON.stringify({ message: 'retrieval service unavailable' })}\n\n`);
      res.end();
      return;
    }

    const parser = new SseParser();
    let accumulatedText = '';
    const citations: unknown[] = [];
    let settled = false;

    const finalize = async () => {
      if (settled) return;
      settled = true;
      try {
        await this.messagesService.addMessage(
          conversationId,
          'assistant',
          accumulatedText,
          citations,
        );
        await this.conversationsService.touchLastActive(conversationId);
      } catch (err: any) {
        this.logger.error(`Failed to persist assistant message: ${err.message}`);
      }
    };

    upstream.on('data', (chunk: Buffer) => {
      // Forward raw bytes untouched so SSE framing seen by the client exactly
      // matches what retrieval-orchestrator sent.
      res.write(chunk);

      const text = chunk.toString('utf-8');
      for (const frame of parser.push(text)) {
        this.applyFrame(frame, {
          onToken: (delta) => {
            accumulatedText += delta;
          },
          onCitation: (citation) => {
            citations.push(citation);
          },
        });
      }
    });

    upstream.on('end', async () => {
      await finalize();
      res.end();
    });

    upstream.on('error', async (err: Error) => {
      this.logger.error(`Upstream SSE stream error: ${err.message}`);
      await finalize();
      if (!res.writableEnded) {
        res.write(`event: error\ndata: ${JSON.stringify({ message: 'stream interrupted' })}\n\n`);
        res.end();
      }
    });

    res.on('close', () => {
      if (!settled) {
        upstream.destroy();
      }
    });
  }

  private applyFrame(
    frame: { event: string; data: string },
    handlers: {
      onToken: (delta: string) => void;
      onCitation: (citation: unknown) => void;
    },
  ): void {
    switch (frame.event) {
      case 'token': {
        handlers.onToken(this.extractTokenDelta(frame.data));
        break;
      }
      case 'citation': {
        handlers.onCitation(this.tryParseJson(frame.data) ?? frame.data);
        break;
      }
      case 'done':
        // no payload handling needed beyond closing out the stream
        break;
      default:
        break;
    }
  }

  /** Token events may carry either a bare text delta or a small JSON envelope
   * like `{"text": "..."}` / `{"delta": "..."}`, depending on the
   * retrieval-orchestrator implementation — handle both. */
  private extractTokenDelta(data: string): string {
    const parsed = this.tryParseJson(data);
    if (parsed && typeof parsed === 'object') {
      const obj = parsed as Record<string, unknown>;
      if (typeof obj.text === 'string') return obj.text;
      if (typeof obj.delta === 'string') return obj.delta;
      if (typeof obj.token === 'string') return obj.token;
      return '';
    }
    return data;
  }

  private tryParseJson(data: string): unknown {
    try {
      return JSON.parse(data);
    } catch {
      return null;
    }
  }
}
