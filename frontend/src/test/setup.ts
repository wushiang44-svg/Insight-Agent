import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

// globals: false in vite.config.ts means every test file imports its own
// describe/it/expect explicitly -- this is the one piece of global test
// wiring that still needs to run automatically for every test: unmounting
// whatever the previous test rendered, and un-stubbing whatever test-local
// fetch mock (see test/mockFetch.ts) the previous test installed, so tests
// never leak state into each other.
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
