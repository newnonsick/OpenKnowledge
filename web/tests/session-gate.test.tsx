import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SessionGate, resetCachedMember } from "@/components/auth/session-gate";
import { apiRequest } from "@/lib/api-client";

const replace = vi.fn();
const router = { replace };
let pathname = "/";

vi.mock("next/navigation", () => ({ usePathname: () => pathname, useRouter: () => router }));

describe("SessionGate", () => {
  beforeEach(() => {
    resetCachedMember();
    Object.defineProperty(navigator, "locks", {
      configurable: true,
      value: { request: vi.fn(async (_name: string, callback: () => Promise<unknown>) => callback()) },
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    replace.mockReset();
    pathname = "/";
    localStorage.clear();
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    Object.defineProperty(navigator, "locks", { configurable: true, value: undefined });
  });

  it("keeps an active website session alive without exposing refresh credentials", async () => {
    vi.useFakeTimers();
    document.cookie = "openknowledge-csrf=test-csrf; path=/";
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

    expect(fetchMock.mock.calls.map((call) => call[0] instanceof Request ? new URL(call[0].url).pathname : call[0])).toEqual(["/api/v1/me", "/api/v1/auth/refresh"]);
  });

  it("does not rotate refresh credentials while the tab is hidden", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({
        id: "member-1",
        username: "mai",
        display_name: "Mai",
        status: "active",
        system_role: "member",
        requires_password_change: false,
      }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<SessionGate><div>Private console</div></SessionGate>);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });

    await act(async () => {
      vi.advanceTimersByTime(10 * 60 * 1000);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("disables proactive rotation when cross-tab locking is unavailable", async () => {
    vi.useFakeTimers();
    Object.defineProperty(navigator, "locks", { configurable: true, value: undefined });
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({
        id: "member-1",
        username: "mai",
        display_name: "Mai",
        status: "active",
        system_role: "member",
        requires_password_change: false,
      }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<SessionGate><div>Private console</div></SessionGate>);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    await act(async () => {
      vi.advanceTimersByTime(10 * 60 * 1000);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("guards super-admin routes from regular members", async () => {
    pathname = "/people";
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({
        id: "member-1",
        username: "mai",
        display_name: "Mai",
        status: "active",
        system_role: "member",
        requires_password_change: false,
      }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<SessionGate><div>Private console</div></SessionGate>);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(replace).toHaveBeenCalledWith("/");
    expect(screen.queryByText("Private console")).not.toBeInTheDocument();
  });

  it("coordinates refresh rotation across active tabs", async () => {
    vi.useFakeTimers();
    let lock = Promise.resolve<unknown>(undefined);
    const request = vi.fn((name: string, callback: () => Promise<unknown>) => {
      const execution = lock.then(callback);
      lock = execution.catch(() => undefined);
      return execution;
    });
    Object.defineProperty(navigator, "locks", { configurable: true, value: { request } });
    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      const pathname = input instanceof Request ? new URL(input.url).pathname : String(input);
      if (pathname === "/api/v1/me") {
        return new Response(JSON.stringify({
          id: "member-1",
          username: "mai",
          display_name: "Mai",
          status: "active",
          system_role: "member",
          requires_password_change: false,
        }), { status: 200 });
      }
      return new Response(JSON.stringify({ status: "refreshed", access_expires_at: "2026-08-20T12:30:00Z" }), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<><SessionGate><div>First tab</div></SessionGate><SessionGate><div>Second tab</div></SessionGate></>);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    await act(async () => {
      vi.advanceTimersByTime(10 * 60 * 1000);
      await Promise.resolve();
      await Promise.resolve();
      await lock;
    });

    const refreshCalls = fetchMock.mock.calls.filter(([input]) => {
      const pathname = input instanceof Request ? new URL(input.url).pathname : String(input);
      return pathname === "/api/v1/auth/refresh";
    });
    expect(refreshCalls).toHaveLength(1);
    expect(request).toHaveBeenCalledTimes(1);
  });

  it("marks authenticated requests only after meaningful user activity", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({
        id: "member-1",
        username: "mai",
        display_name: "Mai",
        status: "active",
        system_role: "member",
        requires_password_change: false,
      }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    render(<SessionGate><div>Private console</div></SessionGate>);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    fireEvent.pointerDown(window);
    await apiRequest("/private-activity");

    const options = fetchMock.mock.calls[1][1];
    expect(new Headers(options?.headers).get("X-OpenKnowledge-Meaningful-Activity")).toBe("1");
  });

  it("offers in-context reauthentication without replaying the blocked action", async () => {
    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      const path = input instanceof Request ? new URL(input.url).pathname : String(input);
      if (path === "/api/v1/auth/step-up") {
        return new Response(JSON.stringify({ status: "reauthenticated", step_up_expires_at: "2026-08-20T12:10:00Z" }), { status: 200 });
      }
      return new Response(JSON.stringify({
        id: "member-1",
        username: "mai",
        display_name: "Mai",
        status: "active",
        system_role: "member",
        requires_password_change: false,
      }), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SessionGate><div>Private console</div></SessionGate>);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    window.dispatchEvent(new CustomEvent("openknowledge-step-up-required"));
    fireEvent.change(await screen.findByLabelText("Current password"), { target: { value: "family-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify identity" }));

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.queryByRole("dialog", { name: "Verify your identity" })).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([input]) => {
      const path = input instanceof Request ? new URL(input.url).pathname : String(input);
      return path === "/api/v1/auth/step-up";
    })).toHaveLength(1);
  });

  it("keeps cancellation locked while identity verification is submitting", async () => {
    let resolveStepUp: (response: Response) => void = () => {};
    const pendingStepUp = new Promise<Response>((resolve) => {
      resolveStepUp = resolve;
    });
    const fetchMock = vi.fn<typeof fetch>(async (input) => {
      const path = input instanceof Request ? new URL(input.url).pathname : String(input);
      if (path === "/api/v1/auth/step-up") {
        return pendingStepUp;
      }
      return new Response(JSON.stringify({
        id: "member-1",
        username: "mai",
        display_name: "Mai",
        status: "active",
        system_role: "member",
        requires_password_change: false,
      }), { status: 200 });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<SessionGate><div>Private console</div></SessionGate>);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    window.dispatchEvent(new CustomEvent("openknowledge-step-up-required"));
    fireEvent.change(await screen.findByLabelText("Current password"), { target: { value: "family-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify identity" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled());
    resolveStepUp(new Response(JSON.stringify({ status: "reauthenticated", step_up_expires_at: "2026-08-20T12:10:00Z" }), { status: 200 }));
  });

  function memberResponse(overrides: Record<string, unknown> = {}) {
    return new Response(JSON.stringify({
      id: "member-1",
      username: "mai",
      display_name: "Mai",
      status: "active",
      system_role: "member",
      requires_password_change: false,
      ...overrides,
    }), { status: 200 });
  }

  it("enforces MFA enrollment for a super_admin without mfa_enabled", async () => {
    pathname = "/";
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValue(memberResponse({ mfa_enabled: false, system_role: "super_admin" })));

    render(<SessionGate><div>Private console</div></SessionGate>);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(replace).toHaveBeenCalledWith("/first-use/mfa");
    expect(screen.queryByText("Private console")).not.toBeInTheDocument();
  });

  it("does not enforce MFA enrollment once mfa_enabled is true", async () => {
    pathname = "/";
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValue(memberResponse({ mfa_enabled: true, system_role: "super_admin" })));

    render(<SessionGate><div>Private console</div></SessionGate>);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(replace).not.toHaveBeenCalledWith("/first-use/mfa");
    expect(screen.getByText("Private console")).toBeInTheDocument();
  });

  it("orders password change before MFA enrollment on first use", async () => {
    pathname = "/";
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValue(memberResponse({ mfa_enabled: false, requires_password_change: true, system_role: "super_admin" })));

    render(<SessionGate><div>Private console</div></SessionGate>);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(replace).toHaveBeenCalledWith("/first-use/password");
    expect(replace).not.toHaveBeenCalledWith("/first-use/mfa");
  });

  it("matches /people and /activity exactly without blocking similar prefixes", async () => {
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValue(memberResponse()));

    pathname = "/people-directory";
    const first = render(<SessionGate><div>Private console</div></SessionGate>);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(replace).not.toHaveBeenCalledWith("/");
    expect(screen.getByText("Private console")).toBeInTheDocument();
    first.unmount();
    resetCachedMember();
    replace.mockReset();

    pathname = "/activity-log";
    render(<SessionGate><div>Private console</div></SessionGate>);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(replace).not.toHaveBeenCalledWith("/");
  });

  it("blocks nested /people and /activity routes for regular members", async () => {
    pathname = "/people/member-1";
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockResolvedValue(memberResponse()));

    render(<SessionGate><div>Private console</div></SessionGate>);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(replace).toHaveBeenCalledWith("/");
    expect(screen.queryByText("Private console")).not.toBeInTheDocument();
  });

  it("shows Retry without a forced login redirect when /me is unreachable", async () => {
    pathname = "/";
    vi.stubGlobal("fetch", vi.fn<typeof fetch>().mockRejectedValue(new TypeError("offline")));

    render(<SessionGate><div>Private console</div></SessionGate>);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(await screen.findByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(screen.getByText("The console could not be reached. Check your connection.")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalledWith("/login");
  });
});
