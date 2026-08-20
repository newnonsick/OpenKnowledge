import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiRequest } from "@/lib/api-client";

describe("apiRequest", () => {
  beforeEach(() => {
    document.cookie = "aigw-csrf=test-csrf; path=/";
    vi.restoreAllMocks();
  });

  it("rotates the refresh token and retries one failed authenticated request", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: { code: "invalid_api_key" } }), { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ status: "refreshed", access_expires_at: "2026-08-20T12:15:00Z" }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ items: [] }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    const response = await apiRequest<{ items: unknown[] }>("/api/v1/spaces");

    expect(response.items).toEqual([]);
    const refreshCall = fetchMock.mock.calls[1];
    expect(refreshCall[0]).toBe("/api/v1/auth/refresh");
    expect(refreshCall[1]).toEqual(expect.objectContaining({ body: "{}", credentials: "include", method: "POST" }));
    expect(new Headers(refreshCall[1]?.headers).get("X-CSRF-Token")).toBe("test-csrf");
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("adds CSRF and idempotency headers to mutations without exposing tokens", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ id: "space-1" }), { status: 201 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await apiRequest("/api/v1/spaces", { method: "POST", body: { name: "Home" }, idempotent: true });

    const requestCall = fetchMock.mock.calls[0];
    const requestHeaders = new Headers(requestCall[1]?.headers);
    expect(requestCall[0]).toBe("/api/v1/spaces");
    expect(requestCall[1]).toEqual(expect.objectContaining({ credentials: "include", method: "POST" }));
    expect(requestHeaders.get("Content-Type")).toBe("application/json");
    expect(requestHeaders.get("Idempotency-Key")).toEqual(expect.any(String));
    expect(requestHeaders.get("X-CSRF-Token")).toBe("test-csrf");
  });
});
