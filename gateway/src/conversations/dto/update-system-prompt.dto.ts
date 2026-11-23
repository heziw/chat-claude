import { IsOptional, IsString, MaxLength } from 'class-validator';

export class UpdateSystemPromptDto {
  /** Empty string / omitted both mean "clear the custom instruction" --
   *  the controller normalizes an empty string to `null` before saving. */
  @IsOptional()
  @IsString()
  @MaxLength(4000)
  system_prompt?: string;
}
