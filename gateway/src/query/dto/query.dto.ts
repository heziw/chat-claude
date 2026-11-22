import { IsIn, IsNotEmpty, IsOptional, IsString, MaxLength } from 'class-validator';

export class QueryDto {
  @IsString()
  @IsNotEmpty()
  @MaxLength(8192)
  question!: string;

  /** "fast"|"standard"|"deep" -- see shared/CONTRACTS.md "thinking_level".
   * Defaults to "standard" downstream (retrieval-orchestrator) if omitted. */
  @IsOptional()
  @IsIn(['fast', 'standard', 'deep'])
  thinking_level?: 'fast' | 'standard' | 'deep';
}
