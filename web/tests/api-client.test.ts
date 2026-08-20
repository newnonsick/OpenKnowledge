import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiMultipart, apiRequest } from "@/lib/api-client";

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

  it("uploads multipart bodies without overriding the browser boundary", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ job_state: "queued" }), { status: 202 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set("space_id", "global");
    body.set("file", new File(["family"], "notes.txt", { type: "text/plain" }));

    await apiMultipart("/api/v1/sources/upload", body, { idempotent: true });

    const requestCall = fetchMock.mock.calls[0];
    const requestHeaders = new Headers(requestCall[1]?.headers);
    expect(requestCall[1]).toEqual(expect.objectContaining({ body, credentials: "include", method: "POST" }));
    expect(requestHeaders.has("Content-Type")).toBe(false);
    expect(requestHeaders.get("Idempotency-Key")).toEqual(expect.any(String));
    expect(requestHeaders.get("X-CSRF-Token")).toBe("test-csrf");
  });

  it("keeps the multipart idempotency key while refreshing a session", async () => {
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({ error: { code: "invalid_api_key" } }), { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ status: "refreshed", access_expires_at: "2026-08-20T12:15:00Z" }), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ job_state: "queued" }), { status: 202 }));
    vi.stubGlobal("fetch", fetchMock);
    const body = new FormData();
    body.set("file", new File(["family"], "notes.txt", { type: "text/plain" }));

    await apiMultipart("/api/v1/sources/upload", body, { idempotent: true });

    const firstKey = new Headers(fetchMock.mock.calls[0][1]?.headers).get("Idempotency-Key");
    const retryKey = new Headers(fetchMock.mock.calls[2][1]?.headers).get("Idempotency-Key");
    expect(retryKey).toBe(firstKey);
  });
});
