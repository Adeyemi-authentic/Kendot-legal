// Shared by the public chat widget and the internal assistant page:
// safe answer formatting and the Server-Sent Events reader.

export type Source = { title: string; url: string | null };
export type ChatEvent =
  | { type: 'token'; text: string }
  | { type: 'done'; handoff: boolean; refused: boolean; sources: Source[] }
  | { type: 'error'; error: string };

/** A non-OK HTTP reply, with the API's own message. */
export class ChatHttpError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export function esc(s: string) {
  return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]!);
}
function inline(s: string) {
  return esc(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
}
/** Minimal, safe formatting: paragraphs, bullet/numbered lists, bold. */
export function format(text: string) {
  return text.trim().split(/\n{2,}/).map((block) => {
    const lines = block.split('\n').filter((l) => l.trim());
    if (lines.length && lines.every((l) => /^\s*(?:[-*•]|\d+[.)])\s+/.test(l))) {
      const ordered = /^\s*\d/.test(lines[0]);
      const items = lines.map((l) => `<li>${inline(l.replace(/^\s*(?:[-*•]|\d+[.)])\s+/, ''))}</li>`).join('');
      return ordered ? `<ol class="list-decimal pl-5 space-y-1">${items}</ol>` : `<ul class="list-disc pl-5 space-y-1">${items}</ul>`;
    }
    return `<p>${lines.map(inline).join('<br>')}</p>`;
  }).join('');
}

/** POST a question and yield the streamed events. A failed fetch throws TypeError
 *  (offline, CORS, server down); a non-OK reply throws ChatHttpError. */
export async function* streamChat(url: string, body: unknown, headers: Record<string, string> = {}): AsyncGenerator<ChatEvent> {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...headers },
    body: JSON.stringify(body),
  });
  if (!res.ok || !res.body) {
    const err = await res.json().catch(() => ({}));
    throw new ChatHttpError(res.status, err.error || 'Sorry, something went wrong. Please try again.');
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let cut;
    while ((cut = buf.indexOf('\n\n')) !== -1) {
      const line = buf.slice(0, cut).trim();
      buf = buf.slice(cut + 2);
      if (line.startsWith('data:')) yield JSON.parse(line.slice(5));
    }
  }
}
