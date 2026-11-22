import { Module } from '@nestjs/common';
import { AuthModule } from '../auth/auth.module';
import { ConversationsModule } from '../conversations/conversations.module';
import { QueryController } from './query.controller';
import { QueryService } from './query.service';

@Module({
  imports: [AuthModule, ConversationsModule],
  controllers: [QueryController],
  providers: [QueryService],
})
export class QueryModule {}
