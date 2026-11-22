import { IsInt, IsOptional, IsString, Max, MaxLength, Min } from 'class-validator';

export class CreateConversationDto {
  @IsOptional()
  @IsString()
  @MaxLength(256)
  title?: string;

  @IsOptional()
  @IsInt()
  @Min(1)
  @Max(3650)
  retentionDays?: number;
}
