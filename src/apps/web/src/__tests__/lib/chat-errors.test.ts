import { chatErrorMessage } from "@/lib/chat-errors";

const FALLBACK = "Kria couldn't think that through.";

describe("chatErrorMessage", () => {
  it("passes through a human-readable error message unchanged", () => {
    expect(chatErrorMessage(new Error("Kria is temporarily unavailable"), FALLBACK)).toBe(
      "Kria is temporarily unavailable",
    );
  });

  it("replaces a snake_case backend sentinel with the fallback", () => {
    expect(chatErrorMessage(new Error("edit_guide_failed"), FALLBACK)).toBe(FALLBACK);
    expect(chatErrorMessage(new Error("proposal_dispatch_failed"), FALLBACK)).toBe(FALLBACK);
  });

  it("replaces a raw 'Request failed (NNN)' fallback string with the fallback", () => {
    expect(chatErrorMessage(new Error("Request failed (429)"), FALLBACK)).toBe(FALLBACK);
    expect(chatErrorMessage(new Error("Request failed (500)"), FALLBACK)).toBe(FALLBACK);
  });

  it("replaces a non-Error throw with the fallback", () => {
    expect(chatErrorMessage("plain string", FALLBACK)).toBe(FALLBACK);
    expect(chatErrorMessage(undefined, FALLBACK)).toBe(FALLBACK);
    expect(chatErrorMessage({ message: "not an Error instance" }, FALLBACK)).toBe(FALLBACK);
  });

  it("replaces an Error with an empty message with the fallback", () => {
    expect(chatErrorMessage(new Error(""), FALLBACK)).toBe(FALLBACK);
  });

  it("passes through a mixed-case human message that merely contains an underscore-like word", () => {
    // Not opaque: mixed case + punctuation means it isn't a bare snake_case sentinel.
    expect(chatErrorMessage(new Error("Couldn't reach edit_guide service"), FALLBACK)).toBe(
      "Couldn't reach edit_guide service",
    );
  });
});
