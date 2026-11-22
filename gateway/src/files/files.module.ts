import { Module } from '@nestjs/common';
import { ConfigModule, ConfigService } from '@nestjs/config';
import { MulterModule } from '@nestjs/platform-express';
import * as fs from 'fs';
import { diskStorage } from 'multer';
import * as os from 'os';
import * as path from 'path';
import { randomUUID } from 'crypto';
import { AuthModule } from '../auth/auth.module';
import { ConversationsModule } from '../conversations/conversations.module';
import { FilesController } from './files.controller';
import { FilesService } from './files.service';

@Module({
  imports: [
    AuthModule,
    ConversationsModule,
    MulterModule.registerAsync({
      imports: [ConfigModule],
      inject: [ConfigService],
      useFactory: (config: ConfigService) => {
        const uploadDir = config.get<string>('UPLOAD_TMP_DIR', os.tmpdir());
        fs.mkdirSync(uploadDir, { recursive: true });
        return {
          storage: diskStorage({
            destination: uploadDir,
            filename: (_req, file, callback) => {
              const ext = path.extname(file.originalname);
              callback(null, `${randomUUID()}${ext}`);
            },
          }),
          limits: {
            fileSize: Number(config.get<string>('MAX_UPLOAD_SIZE_BYTES', `${512 * 1024 * 1024}`)),
          },
        };
      },
    }),
  ],
  controllers: [FilesController],
  providers: [FilesService],
})
export class FilesModule {}
