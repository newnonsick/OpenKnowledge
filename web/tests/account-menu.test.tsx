import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AccountMenu } from "@/components/account-menu";

describe("AccountMenu", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.dataset.theme = "light";
  });

  it("opens account destinations above the user trigger", () => {
    render(<AccountMenu member={{ displayName: "Mai Arun", role: "Member" }} onSignOut={vi.fn()} signingOut={false} />);

    const trigger = screen.getByRole("button", { name: "Open account menu" });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("menu", { name: "Account" })).not.toBeInTheDocument();

    fireEvent.click(trigger);

    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("menu", { name: "Account" })).toHaveClass("account-menu-popover");
    expect(screen.getByRole("link", { name: "Profile and settings" })).toHaveAttribute("href", "/settings");
    expect(screen.getByRole("link", { name: "API keys" })).toHaveAttribute("href", "/settings?section=api-keys");
    expect(screen.getByRole("button", { name: "Switch to dark theme" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeInTheDocument();
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
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));

    expect(onSignOut).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("menu", { name: "Account" })).not.toBeInTheDocument();
  });
});
