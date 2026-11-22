import { Injectable } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import axios, { AxiosInstance } from 'axios';
import { Readable } from 'stream';

export interface QueryHistoryItem {
  role: 'user' | 'assistant' | 'system';
  content: string;
}

/**
 * Client for retrieval-orchestrator (:8001). See shared/CONTRACTS.md.
 * `POST /query` returns a `text/event-stream` response, so we open it with
 * axios's node `stream` response type and hand the raw Readable back to the
 * caller (QueryService) to tee: forward bytes to the client as SSE, and
 * parse events to know when generation is `done` so the assistant message
 * can be persisted.
 */
@Injectable()
export class RetrievalClient {
  private readonly http: AxiosInstance;

  constructor(private readonly config: ConfigService) {
    const baseURL = this.config.get<string>(
      'RETRIEVAL_SERVICE_URL',
      'http://localhost:8001',
    );
    this.http = axios.create({ baseURL });
  }

  async streamQuery(
    conversationId: string,
    question: string,
    history?: QueryHistoryItem[],
    thinkingLevel: 'fast' | 'standard' | 'deep' = 'standard',
  ): Promise<Readable> {
    const response = await this.http.post(
      '/query',
      { conversation_id: conversationId, question, history, thinking_level: thinkingLevel },
      {
        responseType: 'stream',
        headers: { Accept: 'text/event-stream' },
      },
    );
    return response.data as Readable;
  }
}
