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
  testMatch: ["<rootDir>/src/**/__tests__/**/*.test.(ts|tsx)"],
  // UI-01c's chat-tokens parser ships its own node:test suite
  // (run via `node --test --experimental-strip-types`), so jest
  // only picks up files that live under a `__tests__` directory.
  testPathIgnorePatterns: [
    "/node_modules/",
    "<rootDir>/src/lib/chat-tokens.test.ts",
  ],
  transformIgnorePatterns: ["/node_modules/(?!(zustand)/)"],
};
