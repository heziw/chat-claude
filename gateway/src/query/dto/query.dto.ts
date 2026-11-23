import { ArrayMaxSize, IsArray, IsIn, IsOptional, IsString, Matches, MaxLength } from 'class-validator';

// data:<mime>;base64,<payload> -- client-side encodes via FileReader before
// sending (see client/.../chat.component.ts's image-attach handler).
const DATA_URL_RE = /^data:image\/(png|jpeg|jpg|gif|webp);base64,/;

export class QueryDto {
  // Not @IsNotEmpty() -- an image-only send (no caption text) is valid when
  // `images` is set. The controller rejects the one actually-invalid
  // combination (both empty) explicitly, since that needs cross-field
  // logic class-validator's per-property decorators can't express cleanly.
  @IsString()
  @MaxLength(8192)
  question!: string;

  /** "fast"|"standard"|"deep" -- see shared/CONTRACTS.md "thinking_level".
   * Defaults to "standard" downstream (retrieval-orchestrator) if omitted. */
  @IsOptional()
  @IsIn(['fast', 'standard', 'deep'])
  thinking_level?: 'fast' | 'standard' | 'deep';

  /** Base64 image data URLs attached to this message -- see
   * shared/CONTRACTS.md "images". Only honored by the "general" route
   * (retrieval-orchestrator ignores it otherwise); capped here at 4 images
   * so a single request can't balloon arbitrarily (see also main.ts's
   * raised body size limit, sized for this cap). */
  @IsOptional()
  @IsArray()
  @ArrayMaxSize(4)
  @Matches(DATA_URL_RE, { each: true })
  images?: string[];
}
