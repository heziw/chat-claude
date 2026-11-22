import { Injectable, Logger } from '@nestjs/common';
import { ConfigService } from '@nestjs/config';
import axios, { AxiosInstance } from 'axios';
import FormData from 'form-data';
import * as fs from 'fs';

export interface IngestResponse {
  task_id: string;
  file_id: string;
}

export interface IngestStatusResponse {
  status: 'pending' | 'indexing' | 'ready' | 'failed';
  file_id: string;
  error?: string | null;
}

/**
 * Client for ingestion-service (:8000). See shared/CONTRACTS.md.
 */
@Injectable()
export class IngestionClient {
  private readonly logger = new Logger(IngestionClient.name);
  private readonly http: AxiosInstance;

  constructor(private readonly config: ConfigService) {
    const baseURL = this.config.get<string>(
      'INGESTION_SERVICE_URL',
      'http://localhost:8000',
    );
    this.http = axios.create({ baseURL });
  }

  /**
   * Streams a file from disk to `POST /ingest` as multipart/form-data.
   * `filePath` is the path written to disk by multer's diskStorage; the
   * caller owns cleanup of that temp file.
   */
  async ingestFile(
    filePath: string,
    originalName: string,
    mimeType: string,
    conversationId: string,
  ): Promise<IngestResponse> {
    const form = new FormData();
    form.append('file', fs.createReadStream(filePath), {
      filename: originalName,
      contentType: mimeType,
    });
    form.append('conversation_id', conversationId);

    const response = await this.http.post<IngestResponse>('/ingest', form, {
      headers: form.getHeaders(),
      maxContentLength: Infinity,
      maxBodyLength: Infinity,
    });
    return response.data;
  }

  async getStatus(taskId: string): Promise<IngestStatusResponse> {
    const response = await this.http.get<IngestStatusResponse>(
      `/ingest/${taskId}/status`,
    );
    return response.data;
  }

  async deleteConversationFiles(conversationId: string): Promise<void> {
    try {
      await this.http.delete(`/conversations/${conversationId}/files`);
    } catch (err: any) {
      // The conversation row is already gone from Postgres by the time this
      // runs (or is about to be) — log and continue rather than blocking the
      // user-facing delete on the ingestion service being reachable. Orphaned
      // Milvus/NFS data will still be swept by the retention-sweeper.
      this.logger.error(
        `Failed to clean up ingestion-service data for conversation ${conversationId}: ${err.message}`,
      );
    }
  }
}
