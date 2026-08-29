const nextJest = require("next/jest");

const createJestConfig = nextJest({ dir: "./" });

/** @type {import('jest').Config} */
const config = {
  testEnvironment: "jsdom",
  maxWorkers: "50%",
  // The full JSDOM suite grows past 1.5 GB in one process. GitHub's smaller
  // runners can swap heavily when multiple long-lived workers do that at once,
  // starving interaction timers and invalidating performance tests. Recycle
  // only CI workers at a fixed absolute ceiling; local runs keep warm workers.
  ...(process.env.CI ? { workerIdleMemoryLimit: "512MB" } : {}),
  moduleNameMapper: {
    "^@/(.*)$": "<rootDir>/src/$1",
    "^@nova/motion-runtime$": "<rootDir>/../../packages/motion-runtime/src/index.ts",
    "^@nova/motion-runtime/canvaskit$":
      "<rootDir>/../../packages/motion-runtime/src/canvaskit.ts",
    "^@nova/motion-runtime/schema$":
      "<rootDir>/../../packages/motion-runtime/motion-scene.schema.json",
    "^@nova/motion-runtime/catalog$":
      "<rootDir>/../../packages/motion-runtime/creator-blocks.catalog.json",
    "^@nova/motion-runtime/ai-catalog$":
      "<rootDir>/../../packages/motion-runtime/src/ai-catalog.ts",
  },
  // Runs after the test environment is set up — polyfills + jest-dom matchers.
  setupFilesAfterEnv: ["<rootDir>/jest.setup.ts"],
  testMatch: [
    "<rootDir>/src/__tests__/**/*.test.{ts,tsx}",
    // Co-located unit tests for lib/ modules (bar-position, drag-zone, etc.)
    "<rootDir>/src/lib/**/__tests__/**/*.test.{ts,tsx}",
  ],
};

module.exports = createJestConfig(config);
