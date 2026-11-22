import { Module } from '@nestjs/common';
import { ConfigModule } from '@nestjs/config';
import { AppController } from './app.controller';
import { AuthModule } from './auth/auth.module';
import { DatabaseModule } from './common/database/database.module';
import { ServiceClientsModule } from './common/http/service-clients.module';
import { ConversationsModule } from './conversations/conversations.module';
import { FilesModule } from './files/files.module';
import { QueryModule } from './query/query.module';

@Module({
  imports: [
    ConfigModule.forRoot({
      isGlobal: true,
      // Allow running the gateway from its own directory (./.env) or from a
      // repo-root `.env` shared across all services (see /.env.example).
      envFilePath: ['.env', '../.env'],
    }),
    DatabaseModule,
    ServiceClientsModule,
    AuthModule,
    ConversationsModule,
    FilesModule,
    QueryModule,
  ],
  controllers: [AppController],
})
export class AppModule {}
