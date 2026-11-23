import {
  Injectable,
  Logger,
  OnModuleDestroy,
  OnModuleInit,
} from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import * as sql from 'mssql';

/**
 * Thin wrapper around an `mssql.ConnectionPool`. All SQL in the gateway goes
 * through this service so pooling/shutdown is handled in one place.
 *
 * The rest of the codebase (auth/conversations/messages services) was
 * originally written against `pg`'s `query(text, params)` -> `{ rows }`
 * shape with `$1, $2, ...` placeholders. To avoid touching every call site,
 * this service keeps that exact external shape and does the Postgres ->
 * MSSQL adaptation internally: `$n` placeholders are rewritten to `@pn` and
 * bound via `mssql.Request#input`.
 */
@Injectable()
export class DatabaseService implements OnModuleInit, OnModuleDestroy {
  private readonly logger = new Logger(DatabaseService.name);
  private pool: sql.ConnectionPool;

  constructor(private readonly config: ConfigService) {
    // These are required for the gateway to function at all, so a missing
    // value should fail fast (connect() below will throw) rather than
    // silently coercing to an empty string.
    this.pool = new sql.ConnectionPool({
      server: this.config.get<string>('MSSQL_HOST')!,
      port: parseInt(this.config.get<string>('MSSQL_PORT', '1433'), 10),
      database: this.config.get<string>('MSSQL_DATABASE')!,
      user: this.config.get<string>('MSSQL_USER')!,
      password: this.config.get<string>('MSSQL_PASSWORD')!,
      options: {
        // Needed against the local dev container, which uses a self-signed
        // cert. In production, pointed at the real enterprise SQL Server,
        // consider removing this (or setting it conditionally on NODE_ENV)
        // if that server presents a certificate signed by a trusted CA.
        trustServerCertificate: true,
        enableArithAbort: true,
      },
    });
    this.pool.on('error', (err) => {
      this.logger.error(`Unexpected MSSQL pool error: ${err.message}`, err.stack);
    });
  }

  async onModuleInit(): Promise<void> {
    // Fail fast on boot if MSSQL is unreachable rather than on first request.
    await this.pool.connect();
    await this.pool.query('SELECT 1');
    this.logger.log('Connected to MSSQL');
  }

  async onModuleDestroy(): Promise<void> {
    await this.pool.close();
  }

  /**
   * Runs a parameterized query and returns rows in the same `{ rows }` shape
   * `pg` used to return, so existing call sites don't need to change.
   *
   * `text` must use Postgres-style positional placeholders (`$1`, `$2`, ...);
   * they are rewritten to mssql's named placeholders (`@p1`, `@p2`, ...)
   * before execution.
   */
  async query<T = any>(
    text: string,
    params: any[] = [],
  ): Promise<{ rows: T[] }> {
    const request = this.pool.request();
    params.forEach((value, index) => {
      request.input(`p${index + 1}`, value);
    });
    const translated = text.replace(/\$(\d+)/g, (_match, n) => `@p${n}`);
    const result = await request.query<T>(translated);
    return { rows: result.recordset as unknown as T[] };
  }

  /**
   * Runs `fn` inside an MSSQL transaction. The transaction object passed to
   * `fn` can be used to build requests via `new sql.Request(transaction)`.
   */
  async withTransaction<T>(fn: (transaction: sql.Transaction) => Promise<T>): Promise<T> {
    const transaction = new sql.Transaction(this.pool);
    await transaction.begin();
    try {
      const result = await fn(transaction);
      await transaction.commit();
      return result;
    } catch (err) {
      await transaction.rollback();
      throw err;
    }
  }
}
