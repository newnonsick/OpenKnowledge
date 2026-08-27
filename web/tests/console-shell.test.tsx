import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ConfirmationDialog } from "@/components/confirmation-dialog";
import { ConsoleShell } from "@/components/console-shell";
import { contractClient } from "@/lib/api-client";

const replace = vi.fn();

function DialogHarness() {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button onClick={() => setOpen(true)} type="button">Review change</button>
      <ConfirmationDialog cancelLabel="Not now" confirmLabel="Confirm" description="Confirm the requested change." onCancel={() => setOpen(false)} onConfirm={vi.fn()} open={open} title="Confirm change" />
    </div>
  );
}

vi.mock("next/navigation", () => ({
  usePathname: () => "/spaces",
  useRouter: () => ({ replace }),
}));

vi.mock("@/lib/api-client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api-client")>();
  return { ...original, contractClient: { ...original.contractClient, POST: vi.fn() } };
});

describe("ConsoleShell", () => {
  beforeEach(() => {
    replace.mockReset();
    vi.mocked(contractClient.POST).mockReset();
  });

  it("shows the active destination and page context", () => {
    render(
      <ConsoleShell
        description="Create focused spaces without losing unified discovery."
        eyebrow="Knowledge boundaries"
        member={{ displayName: "Mai", role: "Member", systemRole: "member" }}
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
    expect(screen.queryByRole("link", { name: "People & access" })).not.toBeInTheDocument();
  });

  it("ends the website session from the account action", async () => {
    vi.mocked(contractClient.POST).mockResolvedValue({ data: { status: "signed_out" } } as never);
    render(
      <ConsoleShell
        description="Private account controls."
        eyebrow="Account"
        member={{ displayName: "Mai", role: "Member", systemRole: "member" }}
        spaceCount={1}
        title="Settings"
      >
        <p>Settings content</p>
      </ConsoleShell>,
    );

    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));

    await waitFor(() => expect(contractClient.POST).toHaveBeenCalledWith("/api/v1/auth/logout", { body: {} }));
    expect(replace).toHaveBeenCalledWith("/login");
  });

  it("moves focus into confirmations and restores it on Escape", async () => {
    render(
      <ConsoleShell
        description="Private account controls."
        eyebrow="Account"
        member={{ displayName: "Mai", role: "Member", systemRole: "member" }}
        spaceCount={1}
        title="Settings"
      >
        <DialogHarness />
      </ConsoleShell>,
    );

    screen.getByRole("button", { name: "Review change" }).focus();
    fireEvent.click(screen.getByRole("button", { name: "Review change" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Not now" })).toHaveFocus());
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Review change" })).toHaveFocus());
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });
});
