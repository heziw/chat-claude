/** A single parsed Server-Sent-Events frame. */
export interface SseFrame {
  event: string;
  data: string;
}

/**
 * Incremental SSE frame parser. Upstream HTTP chunks don't align with SSE
 * frame boundaries (a chunk can end mid-frame), so frames are buffered and
 * emitted only once a full `\n\n`-terminated block has arrived.
 */
export class SseParser {
  private buffer = '';

  /** Feed a raw chunk (as received from the upstream response) and get back
   * any complete frames it completed. */
  push(chunk: string): SseFrame[] {
    this.buffer += chunk;
    const frames: SseFrame[] = [];

    let separatorIndex: number;
    // SSE frames are separated by a blank line; tolerate both \n\n and \r\n\r\n.
    while ((separatorIndex = this.buffer.indexOf('\n\n')) !== -1) {
      const rawFrame = this.buffer.slice(0, separatorIndex);
      this.buffer = this.buffer.slice(separatorIndex + 2);
      const frame = this.parseFrame(rawFrame);
      if (frame) frames.push(frame);
    }

    return frames;
  }

  private parseFrame(rawFrame: string): SseFrame | null {
    let event = 'message';
    const dataLines: string[] = [];

    for (const line of rawFrame.split('\n')) {
      const trimmed = line.replace(/\r$/, '');
      if (trimmed.startsWith('event:')) {
        event = trimmed.slice('event:'.length).trim();
      } else if (trimmed.startsWith('data:')) {
        // Per the SSE spec, only a single leading space after the `data:`
        // field name is a separator and must be stripped; the rest of the
        // value (including further leading/trailing whitespace, which is
        // meaningful for token-by-token text streaming) must be preserved.
        const value = trimmed.slice('data:'.length);
        dataLines.push(value.startsWith(' ') ? value.slice(1) : value);
      }
    }

    if (dataLines.length === 0) return null;
    return { event, data: dataLines.join('\n') };
  }
}
