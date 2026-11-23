import { Logger, ValidationPipe } from '@nestjs/common';
import { NestFactory } from '@nestjs/core';
import { NestExpressApplication } from '@nestjs/platform-express';
import { AppModule } from './app.module';

async function bootstrap() {
  const app = await NestFactory.create<NestExpressApplication>(AppModule);

  // Default Express body limit (100kb) is too small for a query request
  // carrying base64-encoded image attachments (see QueryDto.images, capped
  // at 4 images client-side) -- 25mb comfortably covers that cap with
  // base64's ~33% overhead.
  app.useBodyParser('json', { limit: '25mb' });

  app.enableCors({
    origin: process.env.CLIENT_APP_URL ?? true,
    credentials: true,
  });

  app.useGlobalPipes(
    new ValidationPipe({
      whitelist: true,
      transform: true,
      forbidNonWhitelisted: false,
    }),
  );

  const port = Number(process.env.PORT ?? 3000);
  await app.listen(port);
  Logger.log(`Gateway listening on :${port} (AUTH_MODE=${process.env.AUTH_MODE ?? 'dev'})`, 'Bootstrap');
}

bootstrap();
