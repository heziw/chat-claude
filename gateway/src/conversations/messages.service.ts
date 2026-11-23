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
    // citations/attachments are stored as raw JSON text (NVARCHAR(MAX)) in
    // MSSQL — there is no native JSON type, so they must be parsed
    // explicitly here (`pg` used to do this automatically for a `jsonb`
    // column).
    return result.rows.map((row) => ({
      ...row,
      citations: this.parseJsonArray(row.citations),
      attachments: this.parseJsonArray(row.attachments),
    }));
  }

  async addMessage(
    conversationId: string,
    role: 'user' | 'assistant' | 'system',
    content: string,
    citations: unknown[] = [],
    attachments: unknown[] = [],
  ): Promise<MessageEntity> {
    const result = await this.db.query<MessageEntity>(
      `INSERT INTO messages (id, conversation_id, role, content, citations, attachments)
       OUTPUT INSERTED.*
       VALUES ($1, $2, $3, $4, $5, $6)`,
      [randomUUID(), conversationId, role, content, JSON.stringify(citations), JSON.stringify(attachments)],
    );
    const row = result.rows[0];
    return {
      ...row,
      citations: this.parseJsonArray(row.citations),
      attachments: this.parseJsonArray(row.attachments),
    };
  }

  private parseJsonArray(value: unknown): unknown[] {
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
