const nextJest = require("next/jest");

const createJestConfig = nextJest({ dir: "./" });

/** @type {import('jest').Config} */
const config = {
  testEnvironment: "jsdom",
  maxWorkers: "50%",
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
