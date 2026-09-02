import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { PasswordChangeForm } from "@/components/auth/password-change-form";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }) }));

describe("PasswordChangeForm", () => {
  it("blocks weak or mismatched passwords before sending them", () => {
    render(<PasswordChangeForm />);

    const password = screen.getByLabelText("New password");
    const confirmation = screen.getByLabelText("Confirm password");
    const submit = screen.getByRole("button", { name: "Set new password" });

    fireEvent.change(password, { target: { value: "too-short" } });
    fireEvent.change(confirmation, { target: { value: "different" } });

    expect(screen.getByText("At least 15 characters")).toHaveAttribute("data-valid", "false");
    expect(screen.getByText("Passwords match")).toHaveAttribute("data-valid", "false");
    expect(submit).toBeDisabled();
  });

  it("blocks passwords that miss a required character class", () => {
    render(<PasswordChangeForm />);

    fireEvent.change(screen.getByLabelText("New password"), { target: { value: "all lowercase letters!" } });
    fireEvent.change(screen.getByLabelText("Confirm password"), { target: { value: "all lowercase letters!" } });

    expect(screen.getByText("An uppercase letter")).toHaveAttribute("data-valid", "false");
    expect(screen.getByText("A number")).toHaveAttribute("data-valid", "false");
    expect(screen.getByRole("button", { name: "Set new password" })).toBeDisabled();
  });

  it("reveals the first personal API key once before entering the console", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      requires_mfa_enrollment: false,
      initial_api_key: {
        id: "key-1",
        public_id: "public-1",
        name: "First device",
        secret: "openknowledge_v1_public-1_secret",
        scopes: ["chat:write", "knowledge:read"],
      },
    }), { status: 200 })));
    render(<PasswordChangeForm />);

    fireEvent.change(screen.getByLabelText("New password"), { target: { value: "Permanent-Password-934!" } });
    fireEvent.change(screen.getByLabelText("Confirm password"), { target: { value: "Permanent-Password-934!" } });
    fireEvent.click(screen.getByRole("button", { name: "Set new password" }));

    await waitFor(() => expect(screen.getByText("openknowledge_v1_public-1_secret")).toBeInTheDocument());
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    const [input, init] = vi.mocked(fetch).mock.calls[0];
    const request = new Request(input, init);
    expect(await request.json()).toEqual({
      confirmation: "Permanent-Password-934!",
      password: "Permanent-Password-934!",
    });
    expect(screen.getByText(/shown only once/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "I have saved this API key" })).toBeInTheDocument();
  });
});
