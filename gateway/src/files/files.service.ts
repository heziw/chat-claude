import { BadRequestException, Injectable, Logger } from '@nestjs/common';
import * as fs from 'fs/promises';
import { IngestionClient, IngestStatusResponse } from '../common/http/ingestion.client';

export interface UploadResult {
  task_id: string;
  file_id: string;
}

@Injectable()
export class FilesService {
  private readonly logger = new Logger(FilesService.name);

  // file_id -> task_id, populated at upload time so the status endpoint can
  // be called with just the file_id. Falls back to an explicit `taskId`
  // query param (see shared/CONTRACTS.md) when the mapping isn't present —
  // e.g. after a gateway restart, or if a second gateway instance is behind
  // a load balancer. For multi-instance deployments this map should move to
  // Redis (REDIS_URL is already provisioned for this).
  private readonly fileIdToTaskId = new Map<string, string>();

  constructor(private readonly ingestionClient: IngestionClient) {}

  async uploadFile(
    conversationId: string,
    file: Express.Multer.File,
  ): Promise<UploadResult> {
    if (!file) {
      throw new BadRequestException('No file was uploaded (expected multipart field "file")');
    }

    try {
      const result = await this.ingestionClient.ingestFile(
        file.path,
        file.originalname,
        file.mimetype,
        conversationId,
      );
      this.fileIdToTaskId.set(result.file_id, result.task_id);
      return result;
    } finally {
      await fs.unlink(file.path).catch((err) => {
        this.logger.warn(`Failed to remove temp upload ${file.path}: ${err.message}`);
      });
    }
  }

  async getStatus(fileId: string, taskId?: string): Promise<IngestStatusResponse> {
    const resolvedTaskId = taskId ?? this.fileIdToTaskId.get(fileId);
    if (!resolvedTaskId) {
      throw new BadRequestException(
        'Unknown file_id — pass ?taskId=<task_id> returned by POST /conversations/:id/files',
      );
    }
    return this.ingestionClient.getStatus(resolvedTaskId);
  }
}
