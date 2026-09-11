import { mutationFingerprint } from "@/lib/edit-copilot/mutation-fingerprint";

/** Media delivery URLs expire independently of the editable document. */
function withoutDeliveryUrls(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(withoutDeliveryUrls);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).filter(([key]) =>
      !/(?:^|_)(?:url|urls)$|Url$|Urls$/.test(key),
    ).map(([key, entry]) => [key, withoutDeliveryUrls(entry)]));
  }
  return value;
}

/** Pass full local persistence state, never the capped model snapshot or UI focus. */
export function editorDraftRevision(state: Record<string, unknown>): string {
  return mutationFingerprint([withoutDeliveryUrls(state)]);
}
