import { Injectable } from '@nestjs/common';
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
    return result.rows;
  }

  async addMessage(
    conversationId: string,
    role: 'user' | 'assistant' | 'system',
    content: string,
    citations: unknown[] = [],
  ): Promise<MessageEntity> {
    const result = await this.db.query<MessageEntity>(
      `INSERT INTO messages (conversation_id, role, content, citations)
       VALUES ($1, $2, $3, $4::jsonb)
       RETURNING *`,
      [conversationId, role, content, JSON.stringify(citations)],
    );
    return result.rows[0];
  }
}
