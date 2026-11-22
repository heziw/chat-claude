import { Global, Module } from '@nestjs/common';
import { ConfigModule } from '@nestjs/config';
import { IngestionClient } from './ingestion.client';
import { RetrievalClient } from './retrieval.client';

/**
 * Thin HTTP clients for the two Python services the gateway talks to:
 * ingestion-service (file upload/status/delete) and retrieval-orchestrator
 * (SSE query streaming). Kept separate from NestJS's own HttpModule so we
 * have full control over streaming (multipart upload, SSE passthrough).
 */
@Global()
@Module({
  imports: [ConfigModule],
  providers: [IngestionClient, RetrievalClient],
  exports: [IngestionClient, RetrievalClient],
})
export class ServiceClientsModule {}
