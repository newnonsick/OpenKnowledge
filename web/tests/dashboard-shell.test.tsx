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
});
