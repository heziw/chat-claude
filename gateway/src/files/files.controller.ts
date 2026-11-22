import {
  Controller,
  Get,
  Param,
  ParseUUIDPipe,
  Post,
  Query,
  UploadedFile,
  UseGuards,
  UseInterceptors,
} from '@nestjs/common';
import { FileInterceptor } from '@nestjs/platform-express';
import { CurrentUser } from '../auth/current-user.decorator';
import { JwtAuthGuard } from '../auth/jwt-auth.guard';
import { AuthenticatedUser } from '../auth/jwt-payload.interface';
import { IngestStatusResponse } from '../common/http/ingestion.client';
import { ConversationsService } from '../conversations/conversations.service';
import { FilesService, UploadResult } from './files.service';

@Controller('conversations/:id/files')
@UseGuards(JwtAuthGuard)
export class FilesController {
  constructor(
    private readonly filesService: FilesService,
    private readonly conversationsService: ConversationsService,
  ) {}

  @Post()
  @UseInterceptors(FileInterceptor('file'))
  async upload(
    @CurrentUser() user: AuthenticatedUser,
    @Param('id', new ParseUUIDPipe()) conversationId: string,
    @UploadedFile() file: Express.Multer.File,
  ): Promise<UploadResult> {
    await this.conversationsService.getOwned(conversationId, user.sub);
    const result = await this.filesService.uploadFile(conversationId, file);
    await this.conversationsService.touchLastActive(conversationId);
    return result;
  }

  @Get(':fileId/status')
  async status(
    @CurrentUser() user: AuthenticatedUser,
    @Param('id', new ParseUUIDPipe()) conversationId: string,
    @Param('fileId') fileId: string,
    @Query('taskId') taskId?: string,
  ): Promise<IngestStatusResponse> {
    await this.conversationsService.getOwned(conversationId, user.sub);
    return this.filesService.getStatus(fileId, taskId);
  }
}
