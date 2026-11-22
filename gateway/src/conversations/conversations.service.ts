import {
  ForbiddenException,
  Injectable,
  NotFoundException,
} from '@nestjs/common';
import { DatabaseService } from '../common/database/database.service';
import { IngestionClient } from '../common/http/ingestion.client';
import { ConversationEntity } from './conversation.entity';

@Injectable()
export class ConversationsService {
  constructor(
    private readonly db: DatabaseService,
    private readonly ingestionClient: IngestionClient,
  ) {}

  async listForUser(userId: string): Promise<ConversationEntity[]> {
    const result = await this.db.query<ConversationEntity>(
      `SELECT * FROM conversations WHERE user_id = $1 AND status = 'active'
       ORDER BY last_active_at DESC`,
      [userId],
    );
    return result.rows;
  }

  async create(
    userId: string,
    title?: string,
    retentionDays?: number,
  ): Promise<ConversationEntity> {
    const result = await this.db.query<ConversationEntity>(
      `INSERT INTO conversations (user_id, title, retention_days)
       VALUES ($1, COALESCE($2, 'שיחה חדשה'), COALESCE($3, 90))
       RETURNING *`,
      [userId, title, retentionDays],
    );
    return result.rows[0];
  }

  /** Fetches a conversation and asserts it belongs to `userId`. */
  async getOwned(conversationId: string, userId: string): Promise<ConversationEntity> {
    const result = await this.db.query<ConversationEntity>(
      `SELECT * FROM conversations WHERE id = $1`,
      [conversationId],
    );
    const conversation = result.rows[0];
    if (!conversation) {
      throw new NotFoundException('Conversation not found');
    }
    if (conversation.user_id !== userId) {
      throw new ForbiddenException('You do not have access to this conversation');
    }
    return conversation;
  }

  async touchLastActive(conversationId: string): Promise<void> {
    await this.db.query(
      `UPDATE conversations SET last_active_at = now() WHERE id = $1`,
      [conversationId],
    );
  }

  async delete(conversationId: string, userId: string): Promise<void> {
    // Ensures ownership before anything is deleted.
    await this.getOwned(conversationId, userId);

    // Clean up ingestion-service owned data (Milvus partition, NFS files,
    // side-stores) first; if it fails we still proceed to drop the Postgres
    // row (and log) rather than leaving the conversation un-deletable.
    await this.ingestionClient.deleteConversationFiles(conversationId);

    await this.db.query(`DELETE FROM conversations WHERE id = $1`, [conversationId]);
  }
}
