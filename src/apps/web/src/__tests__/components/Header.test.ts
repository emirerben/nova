import { isChatFirstPlanPath } from "@/components/Header";

describe("isChatFirstPlanPath", () => {
  it.each(["/plan", "/plan/abc123", "/plan/550e8400-e29b-41d4-a716-446655440000"]) (
    "recognizes %s as a chat-first route",
    (pathname) => expect(isChatFirstPlanPath(pathname)).toBe(true),
  );

  it.each(["/plan/items", "/plan/items/item-1", "/plan/new", "/plan/persona", "/plan/style"]) (
    "keeps %s on its existing route",
    (pathname) => expect(isChatFirstPlanPath(pathname)).toBe(false),
  );
});
