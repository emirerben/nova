export {};

const originalEnv = process.env;
const mockNotFound = jest.fn(() => {
  throw new Error("NOT_FOUND");
});

jest.mock("next/navigation", () => ({
  notFound: () => mockNotFound(),
}));

describe("dev QA fixture route gates", () => {
  beforeEach(() => {
    jest.resetModules();
    mockNotFound.mockClear();
    process.env = { ...originalEnv };
    delete process.env.E2E_FIXTURES;
  });

  afterEach(() => {
    process.env = originalEnv;
  });

  it.each([
    ["clips", "@/app/dev-qa/clips/page"],
    ["overlays", "@/app/dev-qa/overlays/page"],
  ])("%s page 404s when E2E_FIXTURES is unset", async (_name, modulePath) => {
    const { default: Page } = await import(modulePath);

    expect(() => Page()).toThrow("NOT_FOUND");
    expect(mockNotFound).toHaveBeenCalledTimes(1);
  });
});
