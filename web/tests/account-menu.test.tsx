import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AccountMenu } from "@/components/account-menu";

describe("AccountMenu", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.dataset.theme = "light";
  });

  it("opens account destinations above the user trigger with valid menu semantics", async () => {
    render(<AccountMenu member={{ displayName: "Mai Arun", role: "Member" }} onSignOut={vi.fn()} signingOut={false} />);

    const trigger = screen.getByRole("button", { name: "Open account menu" });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("menu", { name: "Account" })).not.toBeInTheDocument();

    fireEvent.click(trigger);

    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("menu", { name: "Account" })).toHaveClass("account-menu-popover");
    const security = screen.getByRole("menuitem", { name: "Security settings" });
    expect(security).toHaveAttribute("href", "/settings?section=security");
    expect(screen.getByRole("menuitem", { name: "API keys" })).toHaveAttribute("href", "/settings?section=api-keys");
    expect(screen.getByRole("menuitem", { name: "Switch to dark theme" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: "Sign out" })).toBeInTheDocument();
    await waitFor(() => expect(security).toHaveFocus());
  });

  it("moves through items with menu keyboard controls", async () => {
    render(<AccountMenu member={{ displayName: "Mai Arun", role: "Member" }} onSignOut={vi.fn()} signingOut={false} />);
    fireEvent.click(screen.getByRole("button", { name: "Open account menu" }));
    const security = screen.getByRole("menuitem", { name: "Security settings" });
    const apiKeys = screen.getByRole("menuitem", { name: "API keys" });
    const signOut = screen.getByRole("menuitem", { name: "Sign out" });
    await waitFor(() => expect(security).toHaveFocus());

    fireEvent.keyDown(security, { key: "ArrowDown" });
    await waitFor(() => expect(apiKeys).toHaveFocus());
    fireEvent.keyDown(apiKeys, { key: "End" });
    await waitFor(() => expect(signOut).toHaveFocus());
    fireEvent.keyDown(signOut, { key: "Home" });
    await waitFor(() => expect(security).toHaveFocus());
  });

  it("dismisses on Escape and outside pointer input", () => {
    render(<AccountMenu member={{ displayName: "Mai Arun", role: "Member" }} onSignOut={vi.fn()} signingOut={false} />);
    const trigger = screen.getByRole("button", { name: "Open account menu" });

    fireEvent.click(trigger);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("menu", { name: "Account" })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();

    fireEvent.click(trigger);
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole("menu", { name: "Account" })).not.toBeInTheDocument();
  });

  it("runs sign out once and closes the menu", () => {
    const onSignOut = vi.fn();
    render(<AccountMenu member={{ displayName: "Mai Arun", role: "Member" }} onSignOut={onSignOut} signingOut={false} />);

    fireEvent.click(screen.getByRole("button", { name: "Open account menu" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Sign out" }));

    expect(onSignOut).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("menu", { name: "Account" })).not.toBeInTheDocument();
  });
});
