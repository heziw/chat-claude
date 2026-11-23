import { Injectable } from '@nestjs/common';
import { randomUUID } from 'crypto';
import { DatabaseService } from '../common/database/database.service';
import { MessageEntity } from './message.entity';

@Injectable()
export class MessagesService {
  constructor(private readonly db: DatabaseService) {}

  async listByConversation(conversationId: string): Promise<MessageEntity[]> {
    const result = await this.db.query<MessageEntity>(
      `SELECT * FROM messages WHERE conversation_id = $1 ORDER BY created_at ASC`,
      [conversationId],
    );
    // citations is stored as raw JSON text (NVARCHAR(MAX)) in MSSQL — there
    // is no native JSON type, so it must be parsed explicitly here (`pg`
    // used to do this automatically for a `jsonb` column).
    return result.rows.map((row) => ({
      ...row,
      citations: this.parseCitations(row.citations),
    }));
  }

  async addMessage(
    conversationId: string,
    role: 'user' | 'assistant' | 'system',
    content: string,
    citations: unknown[] = [],
  ): Promise<MessageEntity> {
    const result = await this.db.query<MessageEntity>(
      `INSERT INTO messages (id, conversation_id, role, content, citations)
       OUTPUT INSERTED.*
       VALUES ($1, $2, $3, $4, $5)`,
      [randomUUID(), conversationId, role, content, JSON.stringify(citations)],
    );
    const row = result.rows[0];
    return { ...row, citations: this.parseCitations(row.citations) };
  }

  private parseCitations(value: unknown): unknown[] {
    if (Array.isArray(value)) {
      return value;
    }
    if (typeof value !== 'string' || value.length === 0) {
      return [];
    }
    try {
      return JSON.parse(value);
    } catch {
      return [];
    }
  }
}
