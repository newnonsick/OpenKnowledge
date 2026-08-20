import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SessionGate } from "@/components/auth/session-gate";

const replace = vi.fn();
const router = { replace };

vi.mock("next/navigation", () => ({ useRouter: () => router }));

describe("SessionGate", () => {
  afterEach(() => {
    vi.useRealTimers();
    replace.mockReset();
  });

  it("keeps an active website session alive without exposing refresh credentials", async () => {
    vi.useFakeTimers();
    document.cookie = "aigw-csrf=test-csrf; path=/";
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            id: "member-1",
            username: "mai",
            display_name: "Mai",
            status: "active",
            system_role: "member",
            requires_password_change: false,
          }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(new Response(JSON.stringify({ status: "refreshed", access_expires_at: "2026-08-20T12:30:00Z" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    render(<SessionGate><div>Private console</div></SessionGate>);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText("Private console")).toBeInTheDocument();

    await act(async () => {
      vi.advanceTimersByTime(10 * 60 * 1000);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual(["/api/v1/me", "/api/v1/auth/refresh"]);
  });
});
