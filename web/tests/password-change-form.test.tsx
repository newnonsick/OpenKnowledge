import { fireEvent, render, screen } from "@testing-library/react";
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
});
