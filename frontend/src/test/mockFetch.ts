import { vi } from "vitest";

export interface MockFetchResult {
  status?: number;
  body: unknown;
}

export type FetchHandler = (url: string, method: string, init?: RequestInit) => MockFetchResult | undefined;

// Mocks at the exact boundary api.ts's request() calls (global fetch) --
// never the `api` object's own methods -- per this stage's explicit
// instruction to mock at the API-client boundary, not component internals.
export function mockFetchWith(handler: FetchHandler): ReturnType<typeof vi.fn> {
  const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const method = init?.method ?? "GET";
    const result = handler(url, method, init);
    if (!result) {
      throw new Error(`Unhandled mock fetch: ${method} ${url}`);
    }
    const status = result.status ?? 200;
    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => result.body,
    } as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}
