import {
  Body,
  Controller,
  Delete,
  Get,
  HttpCode,
  Param,
  ParseUUIDPipe,
  Post,
  UseGuards,
} from '@nestjs/common';
import { CurrentUser } from '../auth/current-user.decorator';
import { JwtAuthGuard } from '../auth/jwt-auth.guard';
import { AuthenticatedUser } from '../auth/jwt-payload.interface';
import { ConversationEntity } from './conversation.entity';
import { ConversationsService } from './conversations.service';
import { CreateConversationDto } from './dto/create-conversation.dto';
import { MessageEntity } from './message.entity';
import { MessagesService } from './messages.service';

@Controller('conversations')
@UseGuards(JwtAuthGuard)
export class ConversationsController {
  constructor(
    private readonly conversationsService: ConversationsService,
    private readonly messagesService: MessagesService,
  ) {}

  @Get()
  async list(@CurrentUser() user: AuthenticatedUser): Promise<ConversationEntity[]> {
    return this.conversationsService.listForUser(user.sub);
  }

  @Post()
  async create(
    @CurrentUser() user: AuthenticatedUser,
    @Body() dto: CreateConversationDto,
  ): Promise<ConversationEntity> {
    return this.conversationsService.create(user.sub, dto.title, dto.retentionDays);
  }

  @Get(':id')
  async getOne(
    @CurrentUser() user: AuthenticatedUser,
    @Param('id', new ParseUUIDPipe()) id: string,
  ): Promise<ConversationEntity> {
    return this.conversationsService.getOwned(id, user.sub);
  }

  @Delete(':id')
  @HttpCode(204)
  async remove(
    @CurrentUser() user: AuthenticatedUser,
    @Param('id', new ParseUUIDPipe()) id: string,
  ): Promise<void> {
    await this.conversationsService.delete(id, user.sub);
  }

  @Get(':id/messages')
  async listMessages(
    @CurrentUser() user: AuthenticatedUser,
    @Param('id', new ParseUUIDPipe()) id: string,
  ): Promise<MessageEntity[]> {
    // Ownership check first so a user can't enumerate another user's messages.
    await this.conversationsService.getOwned(id, user.sub);
    return this.messagesService.listByConversation(id);
  }
}
