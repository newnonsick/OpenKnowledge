import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LoginForm } from "@/components/auth/login-form";

const replace = vi.fn();

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));

describe("LoginForm", () => {
  beforeEach(() => {
    replace.mockReset();
    vi.restoreAllMocks();
  });

  it("uses local credentials without social login choices", () => {
    render(<LoginForm />);

    expect(screen.getByRole("textbox", { name: "Username" })).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByText(/google|microsoft/i)).not.toBeInTheDocument();
  });

  it("routes a first-use account to mandatory password change", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            member_id: "member-1",
            system_role: "member",
            requires_password_change: true,
            requires_mfa_enrollment: false,
            access_expires_at: "2026-08-20T12:15:00Z",
          }),
          { status: 200 },
        ),
      ),
    );
    render(<LoginForm />);

    fireEvent.change(screen.getByRole("textbox", { name: "Username" }), { target: { value: "mai" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "temporary-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/first-use/password"));
  });
});
