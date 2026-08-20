import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ConsoleShell } from "@/components/console-shell";
import { apiRequest } from "@/lib/api-client";

const replace = vi.fn();

vi.mock("next/navigation", () => ({
  usePathname: () => "/spaces",
  useRouter: () => ({ replace }),
}));

vi.mock("@/lib/api-client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api-client")>();
  return { ...original, apiRequest: vi.fn() };
});

describe("ConsoleShell", () => {
  beforeEach(() => {
    replace.mockReset();
    vi.mocked(apiRequest).mockReset();
  });

  it("shows the active destination and page context", () => {
    render(
      <ConsoleShell
        description="Create focused spaces without losing unified discovery."
        eyebrow="Knowledge boundaries"
        member={{ displayName: "Mai", role: "Member" }}
        spaceCount={12}
        title="Spaces"
      >
        <p>Space content</p>
      </ConsoleShell>,
    );

    expect(screen.getByRole("heading", { name: "Spaces" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Spaces" })).toHaveClass("is-active");
    expect(screen.getByText("12 accessible spaces")).toBeInTheDocument();
    expect(screen.getByText("Space content")).toBeInTheDocument();
  });

  it("ends the website session from the account action", async () => {
    vi.mocked(apiRequest).mockResolvedValue(undefined);
    render(
      <ConsoleShell
        description="Private account controls."
        eyebrow="Account"
        member={{ displayName: "Mai", role: "Member" }}
        spaceCount={1}
        title="Settings"
      >
        <p>Settings content</p>
      </ConsoleShell>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));

    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/auth/logout", { body: {}, method: "POST" }));
    expect(replace).toHaveBeenCalledWith("/login");
  });
});
