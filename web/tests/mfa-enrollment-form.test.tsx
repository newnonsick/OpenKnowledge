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
        .mockResolvedValueOnce(
          new Response(
            JSON.stringify({
              factor_id: "factor-1",
              secret: "ABCDEFGHIJKLMNOP",
              provisioning_uri: "otpauth://totp/OpenKnowledge:admin?secret=ABCDEFGHIJKLMNOP&issuer=OpenKnowledge",
            }),
            { status: 200 },
          ),
        )
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
    expect(screen.getByRole("img", { name: "Authenticator setup QR code" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("6–8 digit authentication code"), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify and continue" }));

    await waitFor(() => expect(screen.getByText("code-one")).toBeInTheDocument());
    expect(screen.getByText("code-two")).toBeInTheDocument();
    expect(screen.getByText("openknowledge_v1_public-1_secret")).toBeInTheDocument();
    expect(screen.getByText(/shown only once/i)).toBeInTheDocument();
  });

  it("moves focus to the success heading and reports clipboard failures", async () => {
    document.cookie = "openknowledge-csrf=test-csrf; path=/";
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(
          new Response(
            JSON.stringify({
              factor_id: "factor-1",
              secret: "ABCDEFGHIJKLMNOP",
              provisioning_uri: "otpauth://totp/OpenKnowledge:admin?secret=ABCDEFGHIJKLMNOP&issuer=OpenKnowledge",
            }),
            { status: 200 },
          ),
        )
        .mockResolvedValueOnce(
          new Response(
            JSON.stringify({
              member_id: "member-1",
              system_role: "super_admin",
              requires_password_change: false,
              requires_mfa_enrollment: false,
              access_expires_at: "2026-08-20T12:15:00Z",
              recovery_codes: ["code-one", "code-two"],
              initial_api_key: null,
            }),
            { status: 200 },
          ),
        ),
    );
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
    });
    render(<MfaEnrollmentForm />);

    await screen.findByText("ABCDEFGHIJKLMNOP");
    fireEvent.change(screen.getByLabelText("6–8 digit authentication code"), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify and continue" }));

    const heading = await screen.findByRole("heading", { name: "Save your recovery secrets." });
    await waitFor(() => expect(heading).toHaveFocus());

    fireEvent.click(screen.getByRole("button", { name: "Copy all codes" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Copy failed — select the codes manually."));
  });
});
