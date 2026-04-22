/** @type {import('jest').Config} */
module.exports = {
  testEnvironment: "jsdom",
  setupFilesAfterEnv: ["<rootDir>/jest.setup.ts"],
  moduleNameMapper: {
    "^@/(.*)$": "<rootDir>/src/$1",
  },
  transform: {
    "^.+\\.(ts|tsx)$": [
      "ts-jest",
      {
        tsconfig: {
          jsx: "react-jsx",
          esModuleInterop: true,
          module: "commonjs",
          target: "ES2020",
          moduleResolution: "node",
          strict: true,
          allowJs: true,
          resolveJsonModule: true,
          paths: { "@/*": ["./src/*"] },
        },
      },
    ],
  },
  testMatch: [
    "<rootDir>/src/**/__tests__/**/*.test.(ts|tsx)",
    "<rootDir>/src/**/*.test.(ts|tsx)",
  ],
  // chat-tokens.test.ts is a ``node --test`` file (native Node test
  // runner, ``node:test`` imports) — skip it under Jest so the suite
  // does not trip on ``node:test`` resolution.
  testPathIgnorePatterns: [
    "/node_modules/",
    "<rootDir>/src/lib/chat-tokens.test.ts",
  ],
  transformIgnorePatterns: ["/node_modules/(?!(zustand)/)"],
};
