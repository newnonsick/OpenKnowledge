import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PasswordSecurityPanel } from "@/components/auth/password-security-panel";

function sessionResponse() {
  return new Response(JSON.stringify({
    access_expires_at: "2026-09-01T12:15:00Z",
    member_id: "member-1",
    requires_mfa_enrollment: false,
    requires_password_change: false,
    system_role: "member",
  }), { headers: { "Content-Type": "application/json" }, status: 200 });
}

function fillPasswords() {
  fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "Current-Password-934!" } });
  fireEvent.change(screen.getByLabelText("New password"), { target: { value: "Replacement-Password-935!" } });
  fireEvent.change(screen.getByLabelText("Confirm password"), { target: { value: "Replacement-Password-935!" } });
}

async function submittedBody() {
  await waitFor(() => expect(fetch).toHaveBeenCalled());
  const [input, init] = vi.mocked(fetch).mock.calls[0];
  return new Request(input, init).json();
}

afterEach(() => vi.unstubAllGlobals());

describe("PasswordSecurityPanel", () => {
  it("changes a member password, clears secrets, and reports other-session sign-out", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sessionResponse()));
    render(<PasswordSecurityPanel systemRole="member" />);
    fillPasswords();
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Other website sessions were signed out"));
    expect(await submittedBody()).toEqual({
      confirmation: "Replacement-Password-935!",
      current_password: "Current-Password-934!",
      password: "Replacement-Password-935!",
    });
    expect(screen.getByLabelText("Current password")).toHaveValue("");
    expect(screen.getByLabelText("New password")).toHaveValue("");
    expect(screen.getByLabelText("Confirm password")).toHaveValue("");
  });

  it("requires and sends the selected Super Admin factor", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sessionResponse()));
    render(<PasswordSecurityPanel systemRole="super_admin" />);
    fillPasswords();
    expect(screen.getByRole("button", { name: "Change password" })).toBeDisabled();

    fireEvent.change(screen.getByLabelText("Authentication code"), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));

    expect(await submittedBody()).toEqual({
      confirmation: "Replacement-Password-935!",
      current_password: "Current-Password-934!",
      current_totp_code: "123456",
      password: "Replacement-Password-935!",
    });
  });

  it("sends a recovery code instead of an authenticator code when selected", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(sessionResponse()));
    render(<PasswordSecurityPanel systemRole="super_admin" />);
    fillPasswords();
    fireEvent.click(screen.getByRole("button", { name: "Recovery code" }));
    fireEvent.change(screen.getByLabelText("Recovery code"), { target: { value: "  recover-once  " } });
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));

    expect(await submittedBody()).toEqual({
      confirmation: "Replacement-Password-935!",
      current_password: "Current-Password-934!",
      password: "Replacement-Password-935!",
      recovery_code: "recover-once",
    });
  });

  it("renders the server error without a success status", async () => {
    const errorResponse = () => new Response(JSON.stringify({
        error: {
          code: "authentication_failed",
          message: "Invalid current credentials.",
          type: "authentication_error",
        },
        request_id: "request-1",
      }), { headers: { "Content-Type": "application/json" }, status: 401 });
    vi.stubGlobal("navigator", {
      locks: { request: (_name: string, callback: () => Promise<unknown>) => callback() },
    });
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(errorResponse())
      .mockResolvedValueOnce(new Response(JSON.stringify({
        access_expires_at: "2026-09-01T12:15:00Z",
        status: "refreshed",
      }), { headers: { "Content-Type": "application/json" }, status: 200 }))
      .mockResolvedValueOnce(errorResponse()));
    render(<PasswordSecurityPanel systemRole="member" />);
    fillPasswords();
    fireEvent.click(screen.getByRole("button", { name: "Change password" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid current credentials.");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
