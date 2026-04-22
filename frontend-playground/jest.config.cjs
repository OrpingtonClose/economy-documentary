/**
 * Jest config for the playground frontend.
 *
 * Scope is intentionally narrow for PR 6: ts-jest compiles the
 * hand-written TS contracts + fetch helpers; jsdom is the
 * environment so DOM-touching utilities (fetch, Response) resolve.
 * PR 7 will add @testing-library/react component tests on top of
 * this same config.
 */
module.exports = {
  preset: "ts-jest",
  // Node env ships the WHATWG fetch/Response globals we assert
  // against in the helper tests. When PR 7 adds @testing-library
  // /react tests, those files will override ``testEnvironment`` to
  // ``jsdom`` via a per-file ``/** @jest-environment jsdom */``
  // pragma; the default stays Node so fetch is not silently shimmed.
  testEnvironment: "node",
  testMatch: ["<rootDir>/src/**/__tests__/**/*.test.(ts|tsx)"],
  moduleNameMapper: {
    "^@/(.*)$": "<rootDir>/src/$1",
  },
  transform: {
    "^.+\\.(ts|tsx)$": [
      "ts-jest",
      { tsconfig: "<rootDir>/tsconfig.test.json" },
    ],
  },
};
