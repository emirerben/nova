/**
 * Guard test for the dev-only email login provider.
 *
 * The provider must exist ONLY when ALLOW_DEV_LOGIN === "true". This is the
 * safety net that keeps a session-minting backdoor out of production, where the
 * flag is never set. authOptions reads process.env at module-eval time, so each
 * case re-imports the module under jest.resetModules with the env pre-set.
 */

export {};

const originalEnv = process.env;

function loadProviders(): { id: string; type: string; clientId?: string }[] {
  const { authOptions } = require("@/lib/auth") as typeof import("@/lib/auth");
  return authOptions.providers.map((p) => ({
    id: (p as { id: string }).id,
    type: (p as { type: string }).type,
    clientId: (p as { options?: { clientId?: string } }).options?.clientId,
  }));
}

beforeEach(() => {
  jest.resetModules();
  process.env = { ...originalEnv };
});

afterEach(() => {
  process.env = originalEnv;
});

describe("dev-login provider gating", () => {
  test("only the google oauth provider is registered when ALLOW_DEV_LOGIN is unset", () => {
    delete process.env.ALLOW_DEV_LOGIN;
    const providers = loadProviders();
    expect(providers).toHaveLength(1);
    expect(providers[0].type).toBe("oauth");
    expect(providers.some((p) => p.type === "credentials")).toBe(false);
  });

  test("the credentials provider is absent when ALLOW_DEV_LOGIN is not exactly 'true'", () => {
    process.env.ALLOW_DEV_LOGIN = "1";
    const providers = loadProviders();
    expect(providers).toHaveLength(1);
    expect(providers.some((p) => p.type === "credentials")).toBe(false);
  });

  test("a credentials provider is added only when ALLOW_DEV_LOGIN === 'true'", () => {
    process.env.ALLOW_DEV_LOGIN = "true";
    const providers = loadProviders();
    expect(providers).toHaveLength(2);
    expect(providers.some((p) => p.type === "credentials")).toBe(true);
  });

  test("legacy preview client ID remains a fallback for Google sign-in", () => {
    delete process.env.GOOGLE_CLIENT_ID;
    delete process.env.YOUTUBE_CLIENT_ID;
    process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID = "preview-client-id\n";

    expect(loadProviders()[0].clientId).toBe("preview-client-id");
  });

  test("canonical server-side client ID wins over legacy fallbacks", () => {
    process.env.GOOGLE_CLIENT_ID = "canonical-client-id";
    process.env.YOUTUBE_CLIENT_ID = "youtube-client-id";
    process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID = "preview-client-id";

    expect(loadProviders()[0].clientId).toBe("canonical-client-id");
  });
});
