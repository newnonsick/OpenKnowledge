import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { MfaEnrollmentForm } from "@/components/auth/mfa-enrollment-form";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn() }) }));

describe("MfaEnrollmentForm", () => {
  it("reveals recovery codes only after a valid authenticator confirmation", async () => {
    document.cookie = "openknowledge-csrf=test-csrf; path=/";
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(new Response(JSON.stringify({ factor_id: "factor-1", secret: "ABCDEFGHIJKLMNOP" }), { status: 200 }))
        .mockResolvedValueOnce(
          new Response(
            JSON.stringify({
              member_id: "member-1",
              system_role: "super_admin",
              requires_password_change: false,
              requires_mfa_enrollment: false,
              access_expires_at: "2026-08-20T12:15:00Z",
              recovery_codes: ["code-one", "code-two"],
              initial_api_key: {
                id: "key-1",
                public_id: "public-1",
                name: "First device",
                secret: "openknowledge_v1_public-1_secret",
                scopes: ["chat:write", "knowledge:read"],
              },
            }),
            { status: 200 },
          ),
        ),
    );
    render(<MfaEnrollmentForm />);

    await screen.findByText("ABCDEFGHIJKLMNOP");
    fireEvent.change(screen.getByLabelText("6-digit authentication code"), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify and continue" }));

    await waitFor(() => expect(screen.getByText("code-one")).toBeInTheDocument());
    expect(screen.getByText("code-two")).toBeInTheDocument();
    expect(screen.getByText("openknowledge_v1_public-1_secret")).toBeInTheDocument();
    expect(screen.getByText(/shown only once/i)).toBeInTheDocument();
  });
});
