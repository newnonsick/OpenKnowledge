import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { NewPasswordFields } from "@/components/auth/new-password-fields";

function Harness() {
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");

  return (
    <NewPasswordFields
      confirmation={confirmation}
      idPrefix="test"
      onConfirmationChange={setConfirmation}
      onPasswordChange={setPassword}
      password={password}
    />
  );
}

describe("NewPasswordFields", () => {
  it("renders controlled password fields, policy state, and one visibility control", () => {
    render(<Harness />);
    const password = screen.getByLabelText("New password");
    const confirmation = screen.getByLabelText("Confirm password");

    fireEvent.change(password, { target: { value: "Permanent-Password-934!" } });
    fireEvent.change(confirmation, { target: { value: "Permanent-Password-934!" } });

    expect(screen.getByText("At least 15 characters")).toHaveAttribute("data-valid", "true");
    expect(screen.getByText("Passwords match")).toHaveAttribute("data-valid", "true");
    expect(password).toHaveAttribute("id", "test-new-password");
    expect(confirmation).toHaveAttribute("id", "test-confirm-password");

    fireEvent.click(screen.getByRole("button", { name: "Show passwords" }));
    expect(password).toHaveAttribute("type", "text");
    expect(confirmation).toHaveAttribute("type", "text");
  });
});
