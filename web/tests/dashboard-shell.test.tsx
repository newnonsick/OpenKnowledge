import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DashboardShell } from "@/components/dashboard-shell";

describe("DashboardShell", () => {
  it("leads with unified search and keeps system health visually secondary", () => {
    render(
      <DashboardShell
        member={{ displayName: "Mai", role: "Member" }}
        ready
        spaces={[{ id: "global", name: "Family Shared", role: "editor", createdAt: "2026-08-20T12:00:00Z" }]}
      />,
    );

    expect(screen.getByRole("heading", { name: /everything your family knows/i })).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: /search family knowledge/i })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: /primary navigation/i })).toBeInTheDocument();
    expect(screen.getByText("System pulse")).toBeInTheDocument();
    expect(screen.queryByText(/chat/i)).not.toBeInTheDocument();
  });

  it("routes every quick action to a working management surface", () => {
    render(<DashboardShell member={{ displayName: "Mai", role: "Member" }} ready spaces={[]} />);

    expect(screen.getByRole("link", { name: /upload source/i })).toHaveAttribute("href", "/sources");
    expect(screen.getByRole("link", { name: /capture knowledge/i })).toHaveAttribute("href", "/knowledge");
    expect(screen.getByRole("link", { name: /create space/i })).toHaveAttribute("href", "/spaces");
    expect(screen.getByRole("link", { name: /test retrieval/i })).toHaveAttribute("href", "/explore");
    expect(screen.getByRole("link", { name: /command/i })).toHaveAttribute("href", "/explore");
    expect(screen.getByRole("link", { name: "Open profile" })).toHaveAttribute("href", "/settings");
  });
});
