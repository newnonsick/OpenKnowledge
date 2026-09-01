import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { MfaSecurityPanel } from "@/components/auth/mfa-security-panel";
import { contractClient } from "@/lib/api-client";

vi.mock("@/lib/api-client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api-client")>();
  return {
    ...original,
    contractClient: { ...original.contractClient, POST: vi.fn() },
    contractDataWithSessionRetry: async <T,>(request: () => Promise<{ data?: T }>) => (await request()).data as T,
  };
});

describe("MfaSecurityPanel", () => {
  beforeEach(() => {
    vi.mocked(contractClient.POST).mockReset();
  });

  it("shows a concise protected state when MFA is enabled", () => {
    render(<MfaSecurityPanel enabled onEnabled={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "Multi-factor authentication" })).toBeInTheDocument();
    expect(screen.getByText("Authenticator enabled")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start MFA setup" })).not.toBeInTheDocument();
  });

  it("verifies the current password before setup and reveals recovery codes after confirmation", async () => {
    vi.mocked(contractClient.POST).mockImplementation((path) => {
      if (path === "/api/v1/auth/mfa/totp/enroll") {
        return Promise.resolve({ data: { factor_id: "factor-1", secret: "ABCDEFGHIJKLMNOP" } }) as never;
      }
      if (path === "/api/v1/auth/mfa/totp/confirm") {
        return Promise.resolve({ data: { recovery_codes: ["code-one", "code-two"], initial_api_key: null } }) as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    const onEnabled = vi.fn();
    render(<MfaSecurityPanel enabled={false} onEnabled={onEnabled} />);

    fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "correct horse battery staple" } });
    fireEvent.click(screen.getByRole("button", { name: "Start MFA setup" }));

    expect(await screen.findByText("ABCDEFGHIJKLMNOP")).toBeInTheDocument();
    expect(contractClient.POST).toHaveBeenCalledWith("/api/v1/auth/mfa/totp/enroll", {
      body: { current_password: "correct horse battery staple" },
    });
    fireEvent.change(screen.getByLabelText("Authentication code"), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: "Confirm MFA" }));

    expect(await screen.findByText("code-one")).toBeInTheDocument();
    expect(screen.getByText("code-two")).toBeInTheDocument();
    expect(contractClient.POST).toHaveBeenCalledWith("/api/v1/auth/mfa/totp/confirm", {
      body: { code: "123456", current_password: "correct horse battery staple", factor_id: "factor-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "I saved my recovery codes" }));
    await waitFor(() => expect(onEnabled).toHaveBeenCalledTimes(1));
    expect(screen.getByText("Authenticator enabled")).toBeInTheDocument();
  });

  it("keeps the setup form available after an API error", async () => {
    vi.mocked(contractClient.POST).mockRejectedValue(new Error("offline"));
    render(<MfaSecurityPanel enabled={false} onEnabled={vi.fn()} />);

    fireEvent.change(screen.getByLabelText("Current password"), { target: { value: "password" } });
    fireEvent.click(screen.getByRole("button", { name: "Start MFA setup" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("MFA setup could not start");
    expect(screen.getByLabelText("Current password")).toHaveValue("password");
    expect(screen.getByRole("button", { name: "Start MFA setup" })).toBeEnabled();
  });
});
