import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DashboardShell } from "@/components/dashboard-shell";

describe("DashboardShell", () => {
  it("leads with unified search and keeps system health visually secondary", () => {
    render(<DashboardShell />);

    expect(screen.getByRole("heading", { name: /everything your family knows/i })).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: /search family knowledge/i })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: /primary navigation/i })).toBeInTheDocument();
    expect(screen.getByText("System pulse")).toBeInTheDocument();
    expect(screen.queryByText(/chat/i)).not.toBeInTheDocument();
  });
});
