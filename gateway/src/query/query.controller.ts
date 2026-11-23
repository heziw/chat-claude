import { BadRequestException, Body, Controller, Param, ParseUUIDPipe, Post, Res, UseGuards } from '@nestjs/common';
import { Response } from 'express';
import { CurrentUser } from '../auth/current-user.decorator';
import { JwtAuthGuard } from '../auth/jwt-auth.guard';
import { AuthenticatedUser } from '../auth/jwt-payload.interface';
import { QueryDto } from './dto/query.dto';
import { QueryService } from './query.service';

@Controller('conversations/:id/query')
@UseGuards(JwtAuthGuard)
export class QueryController {
  constructor(private readonly queryService: QueryService) {}

  @Post()
  async query(
    @CurrentUser() user: AuthenticatedUser,
    @Param('id', new ParseUUIDPipe()) conversationId: string,
    @Body() dto: QueryDto,
    @Res({ passthrough: false }) res: Response,
  ): Promise<void> {
    if (!dto.question.trim() && !dto.images?.length) {
      throw new BadRequestException('question or images must be provided');
    }
    await this.queryService.streamAndPersist(
      conversationId,
      user.sub,
      dto.question,
      dto.thinking_level ?? 'standard',
      res,
      dto.images,
    );
  }
}
