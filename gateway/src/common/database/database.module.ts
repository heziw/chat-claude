import { Global, Module } from '@nestjs/common';
import { ConfigModule } from '@nestjs/config';
import { DatabaseService } from './database.service';

/**
 * Global module exposing a single `mssql.ConnectionPool`-backed DatabaseService.
 *
 * We deliberately use the raw `mssql` driver instead of an ORM (TypeORM/Prisma):
 * the schema already exists (see infra/docker/mssql-init.sql) and is owned
 * by shared/CONTRACTS.md, the gateway only needs to read/write a handful of
 * well-known tables, and plain parameterized SQL keeps the mapping between
 * this service and the contract explicit and auditable (important for an
 * air-gapped, security-reviewed deployment). See README.md for the full
 * rationale.
 */
@Global()
@Module({
  imports: [ConfigModule],
  providers: [DatabaseService],
  exports: [DatabaseService],
})
export class DatabaseModule {}
