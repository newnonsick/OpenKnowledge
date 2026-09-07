import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ThemeToggle } from "@/components/theme-toggle";

describe("ThemeToggle", () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.dataset.theme = "light";
    vi.restoreAllMocks();
  });

  it("applies the stored theme and toggles with a title attribute", () => {
    localStorage.setItem("openknowledge-theme", "dark");
    render(<ThemeToggle />);

    const button = screen.getByRole("button", { name: "Switch to light theme" });
    expect(button).toHaveAttribute("title", "Switch to light theme");
    expect(document.documentElement.dataset.theme).toBe("dark");

    fireEvent.click(button);
    expect(localStorage.getItem("openknowledge-theme")).toBe("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(screen.getByRole("button", { name: "Switch to dark theme" })).toHaveAttribute("title", "Switch to dark theme");
  });

  it("syncs across tabs through storage events", () => {
    render(<ThemeToggle />);
    expect(screen.getByRole("button", { name: "Switch to dark theme" })).toBeInTheDocument();

    localStorage.setItem("openknowledge-theme", "dark");
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: "openknowledge-theme" }));
    });

    expect(screen.getByRole("button", { name: "Switch to light theme" })).toBeInTheDocument();
  });
});
