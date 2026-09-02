import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DashboardShell } from "@/components/dashboard-shell";

vi.mock("next/navigation", () => ({
  usePathname: () => "/",
  useRouter: () => ({ replace: vi.fn() }),
}));

describe("DashboardShell", () => {
  it("leads with unified search and keeps system health visually secondary", () => {
    render(
      <DashboardShell
        member={{ displayName: "Mai", role: "Member", systemRole: "member" }}
        operations={{
          ingestion: { cancelled: 0, cancellation_requested: 0, failed: 0, queued: 1, retry_wait: 0, running: 2, succeeded: 8 },
          observed_at: "2026-08-20T12:01:00Z",
          retrieval: { embedding_generation_active: true },
          scope: "accessible_spaces",
          settings_revision: 3,
          spaces: 1,
          storage: { referenced_bytes: 42 },
        }}
        ready
        spaces={[{ id: "global", name: "Family Shared", role: "editor", createdAt: "2026-08-20T12:00:00Z" }]}
      />,
    );

    expect(screen.getByRole("heading", { name: /everything your family knows/i })).toBeInTheDocument();
    expect(screen.getAllByText("OpenKnowledge").length).toBeGreaterThan(0);
    expect(screen.getByRole("searchbox", { name: /search family knowledge/i })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: /primary navigation/i })).toBeInTheDocument();
    expect(screen.getByText("System pulse")).toBeInTheDocument();
    expect(screen.getByText("1 queued")).toBeInTheDocument();
    expect(screen.getByText("2 running")).toBeInTheDocument();
    expect(screen.getByText("42 B referenced")).toBeInTheDocument();
    expect(screen.getByText("Revision 3")).toBeInTheDocument();
    expect(screen.queryByText("100")).not.toBeInTheDocument();
    expect(screen.queryByText(/chat/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "People & access" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Activity" })).not.toBeInTheDocument();
  });

  it("routes every quick action to a working management surface", () => {
    render(<DashboardShell member={{ displayName: "Mai", role: "Member", systemRole: "member" }} operations={null} ready spaces={[]} />);

    expect(screen.getByRole("link", { name: /upload source/i })).toHaveAttribute("href", "/sources");
    expect(screen.getByRole("link", { name: /capture knowledge/i })).toHaveAttribute("href", "/knowledge");
    expect(screen.getByRole("link", { name: /create space/i })).toHaveAttribute("href", "/spaces");
    expect(screen.getByRole("link", { name: /test retrieval/i })).toHaveAttribute("href", "/explore");
    for (const searchLink of screen.getAllByRole("link", { name: /search/i })) {
      expect(searchLink).toHaveAttribute("href", "/explore");
    }
    for (const profileLink of screen.getAllByRole("link", { name: "Open profile" })) {
      expect(profileLink).toHaveAttribute("href", "/settings");
    }
  });

  it("shows administration destinations to a super admin", () => {
    render(<DashboardShell member={{ displayName: "Admin", role: "Super admin", systemRole: "super_admin" }} operations={null} ready spaces={[]} />);

    expect(screen.getByRole("link", { name: "People & access" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Activity" })).toBeInTheDocument();
  });
});
