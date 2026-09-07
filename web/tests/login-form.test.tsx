import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LoginForm } from "@/components/auth/login-form";

const replace = vi.fn();

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));

function sessionResponse(overrides: Record<string, unknown> = {}) {
  return new Response(
    JSON.stringify({
      member_id: "member-1",
      system_role: "member",
      requires_password_change: false,
      requires_mfa_enrollment: false,
      access_expires_at: "2026-08-20T12:15:00Z",
      ...overrides,
    }),
    { status: 200 },
  );
}

describe("LoginForm", () => {
  beforeEach(() => {
    replace.mockReset();
    vi.restoreAllMocks();
  });

  it("hides second-factor fields until the gateway reports they are required", () => {
    render(<LoginForm />);

    expect(screen.getByRole("textbox", { name: "Username" })).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByLabelText("Authentication code").closest(".auth-step-pane")).toHaveAttribute("hidden");
    expect(screen.queryByLabelText("Recovery code")).not.toBeInTheDocument();
    expect(screen.queryByText(/google|microsoft/i)).not.toBeInTheDocument();
  });

  it("routes a first-use account to mandatory password change", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        sessionResponse({ requires_password_change: true }),
      ),
    );
    render(<LoginForm />);

    fireEvent.change(screen.getByRole("textbox", { name: "Username" }), { target: { value: "mai" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "temporary-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/first-use/password"));
  });

  it("reveals the second-factor step when the server answers mfa_code_required", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            error: { message: "Second-factor authentication code required.", type: "authentication_error", code: "mfa_code_required" },
          }),
          { status: 401 },
        ),
      )
      .mockResolvedValueOnce(sessionResponse({ system_role: "super_admin" }));
    vi.stubGlobal("fetch", fetchMock);
    render(<LoginForm />);

    fireEvent.change(screen.getByRole("textbox", { name: "Username" }), { target: { value: "mai" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "family-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(screen.getByLabelText("Authentication code")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /Authenticator code/ })).toHaveAttribute("aria-pressed", "true");

    fireEvent.change(screen.getByLabelText("Authentication code"), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify and sign in" }));

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
    const secondCall = new Request(fetchMock.mock.calls[1][0] as RequestInfo, fetchMock.mock.calls[1][1]);
    expect(await secondCall.json()).toMatchObject({ totp_code: "123456", recovery_code: null, username: "mai" });
  });

  it("submits a recovery code instead of the authenticator code when switched", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            error: { message: "Second-factor authentication code required.", type: "authentication_error", code: "mfa_code_required" },
          }),
          { status: 401 },
        ),
      )
      .mockResolvedValueOnce(sessionResponse({ system_role: "super_admin" }));
    vi.stubGlobal("fetch", fetchMock);
    render(<LoginForm />);

    fireEvent.change(screen.getByRole("textbox", { name: "Username" }), { target: { value: "mai" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "family-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(screen.getByLabelText("Authentication code")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: /Recovery code/ }));
    fireEvent.change(screen.getByLabelText("Recovery code"), { target: { value: "abcd-efgh-ijkl" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify and sign in" }));

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
    const secondCall = new Request(fetchMock.mock.calls[1][0] as RequestInfo, fetchMock.mock.calls[1][1]);
    expect(await secondCall.json()).toMatchObject({ recovery_code: "abcd-efgh-ijkl", totp_code: null });
  });

  it("keeps the entered credentials when moving to the second factor and back", async () => {    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          error: { message: "Second-factor authentication code required.", type: "authentication_error", code: "mfa_code_required" },
        }),
        { status: 401 },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<LoginForm />);

    fireEvent.change(screen.getByRole("textbox", { name: "Username" }), { target: { value: "mai" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "family-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Back to sign in" })).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Back to sign in" }));
    expect((screen.getByRole("textbox", { name: "Username" }) as HTMLInputElement).value).toBe("mai");
    expect((screen.getByLabelText("Password") as HTMLInputElement).value).toBe("family-password");
  });

  function fillCredentials() {
    fireEvent.change(screen.getByRole("textbox", { name: "Username" }), { target: { value: "mai" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "family-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  }

  function errorResponse(status: number, code = "request_failed", message = "Something went wrong.") {
    return new Response(JSON.stringify({ error: { code, message, type: "api_error" } }), { status });
  }

  it("moves focus to the code input when the MFA step appears", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      error: { message: "Second-factor authentication code required.", type: "authentication_error", code: "mfa_code_required" },
    }), { status: 401 })));
    render(<LoginForm />);

    fillCredentials();

    const code = await screen.findByLabelText("Authentication code");
    await waitFor(() => expect(code).toHaveFocus());
  });

  it("disables the code input while the second factor submits", async () => {
    let release: (response: Response) => void = () => {};
    const pending = new Promise<Response>((resolve) => { release = resolve; });
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        error: { message: "Second-factor authentication code required.", type: "authentication_error", code: "mfa_code_required" },
      }), { status: 401 }))
      .mockReturnValueOnce(pending);
    vi.stubGlobal("fetch", fetchMock);
    render(<LoginForm />);

    fillCredentials();
    const code = await screen.findByLabelText("Authentication code");
    fireEvent.change(code, { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify and sign in" }));

    await waitFor(() => expect(screen.getByLabelText("Authentication code")).toBeDisabled());
    release(sessionResponse({ system_role: "super_admin" }));
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
  });

  it("reports rate limiting distinctly from other failures", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(errorResponse(429, "rate_limited", "Slow down.")));
    render(<LoginForm />);

    fillCredentials();

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Too many attempts. Please wait a moment before trying again."));
  });

  it("reports gateway trouble distinctly on 5xx failures", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(errorResponse(503, "upstream_unavailable", "Downstream failed.")));
    render(<LoginForm />);

    fillCredentials();

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("The gateway is having trouble. Please wait a moment and try again."));
  });

  it("reports a connection problem distinctly on network failures", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    render(<LoginForm />);

    fillCredentials();

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Could not reach the gateway. Check your connection and try again."));
  });
});
