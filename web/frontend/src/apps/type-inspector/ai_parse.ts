/**
 * Heuristics for extracting a single column-type literal from a free-text
 * AI response. The local CLIs (claude / codex / gemini) and hosted Kai
 * both occasionally wrap the answer in backticks, a code fence, or
 * surrounding prose, even when the prompt says "reply with only the
 * type". This parser tolerates all four shapes.
 *
 * Strategy, in order:
 *   1. Whole response is already a valid type literal -> use as-is.
 *   2. Triple-backtick code fence -> take its contents.
 *   3. Single-backtick inline code -> take that.
 *   4. First uppercase token (with optional length) anywhere in the reply.
 *   5. Last resort: first 64 chars of the first line, or "STRING".
 */
export function extractTypeFromAiResponse(text: string): string {
  const trimmed = text.trim();
  // Common shape: just the type. Pass through.
  if (/^[A-Z][A-Z0-9_]*(\(\d+(\s*,\s*\d+)?\))?$/.test(trimmed)) return trimmed;
  // Fenced code block: ```VARCHAR(64)```
  const fence = trimmed.match(/```(?:\w+)?\n?(.+?)\n?```/s);
  if (fence) return fence[1].trim();
  // Backticked inline
  const inline = trimmed.match(/`([A-Z][A-Z0-9_]*(?:\(\d+(?:\s*,\s*\d+)?\))?)`/);
  if (inline) return inline[1];
  // Bare type token anywhere in the reply. No trailing \b -- it would
  // reject "VARCHAR(128)" because both ")" and the following space are
  // non-word characters, so the regex would fall back to "VARCHAR".
  const bare = trimmed.match(/\b([A-Z][A-Z0-9_]{1,32}(?:\(\d+(?:\s*,\s*\d+)?\))?)/);
  if (bare) return bare[1];
  return trimmed.split("\n")[0].slice(0, 64) || "STRING";
}
