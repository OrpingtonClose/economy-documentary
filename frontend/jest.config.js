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
  transformIgnorePatterns: ["/node_modules/(?!(zustand)/)"],
};
