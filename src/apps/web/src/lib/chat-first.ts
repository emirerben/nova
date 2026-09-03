/** Public rollout gate. Missing env is deliberately default-on for the
 * canonical signed-in creation flow; set false only for an emergency rollback. */
export const CHAT_FIRST_CREATION_ENABLED =
  process.env.NEXT_PUBLIC_CHAT_FIRST_CREATION_ENABLED !== "false";

// The fallback signal is shared in-memory instead of relying solely on a
// one-shot DOM event. This lets the global Header react even when the chat
// workspace reports a very fast 404 before Header's passive effect subscribes.
let chatFirstFallback = false;
const chatFirstFallbackListeners = new Set<() => void>();

export function getChatFirstFallback(): boolean {
  return chatFirstFallback;
}

export function subscribeChatFirstFallback(listener: () => void): () => void {
  chatFirstFallbackListeners.add(listener);
  return () => chatFirstFallbackListeners.delete(listener);
}

export function setChatFirstFallback(value: boolean): void {
  if (chatFirstFallback === value) return;
  chatFirstFallback = value;
  chatFirstFallbackListeners.forEach((listener) => listener());
}
