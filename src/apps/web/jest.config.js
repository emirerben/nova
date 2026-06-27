const nextJest = require("next/jest");

const createJestConfig = nextJest({ dir: "./" });

/** @type {import('jest').Config} */
const config = {
  testEnvironment: "jsdom",
  maxWorkers: "50%",
  moduleNameMapper: {
    "^@/(.*)$": "<rootDir>/src/$1",
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
