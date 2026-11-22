import {
  Injectable,
  Logger,
  OnModuleDestroy,
  OnModuleInit,
} from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import { Pool, PoolClient, QueryResultRow } from 'pg';

/**
 * Thin wrapper around a `pg.Pool`. All SQL in the gateway goes through this
 * service so pooling/shutdown is handled in one place.
 */
@Injectable()
export class DatabaseService implements OnModuleInit, OnModuleDestroy {
  private readonly logger = new Logger(DatabaseService.name);
  private pool: Pool;

  constructor(private readonly config: ConfigService) {
    this.pool = new Pool({
      connectionString: this.config.get<string>('POSTGRES_URL'),
    });
    this.pool.on('error', (err) => {
      this.logger.error(`Unexpected Postgres pool error: ${err.message}`, err.stack);
    });
  }

  async onModuleInit(): Promise<void> {
    // Fail fast on boot if Postgres is unreachable rather than on first request.
    await this.pool.query('SELECT 1');
    this.logger.log('Connected to Postgres');
  }

  async onModuleDestroy(): Promise<void> {
    await this.pool.end();
  }

  query<T extends QueryResultRow = any>(text: string, params?: any[]) {
    return this.pool.query<T>(text, params);
  }

  async withTransaction<T>(fn: (client: PoolClient) => Promise<T>): Promise<T> {
    const client = await this.pool.connect();
    try {
      await client.query('BEGIN');
      const result = await fn(client);
      await client.query('COMMIT');
      return result;
    } catch (err) {
      await client.query('ROLLBACK');
      throw err;
    } finally {
      client.release();
    }
  }
}
