import { act, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DashboardController } from "@/components/dashboard-controller";

const get = vi.hoisted(() => vi.fn());

vi.mock("next/navigation", () => ({
  usePathname: () => "/",
  useRouter: () => ({ replace: vi.fn() }),
}));

vi.mock("@/components/auth/session-gate", () => ({
  useCurrentMember: () => ({ display_name: "Mai Arun", system_role: "member" }),
}));

vi.mock("@/lib/api-client", () => ({
  contractClient: { GET: get },
  contractData: <T,>(pending: Promise<T>) => pending,
}));

function deferred<T>() {
  let resolve: (value: T) => void;
  const promise = new Promise<T>((complete) => {
    resolve = complete;
  });
  return { promise, resolve: resolve! };
}

describe("DashboardController", () => {
  it("shows an honest loading state and uses the total number of accessible spaces", async () => {
    const spaces = deferred<{
      items: Array<{ created_at: string; id: string; name: string; role: string }>;
      page: number;
      page_size: number;
      total_items: number;
      total_pages: number;
    }>();
    const health = deferred<{ status: "ready" }>();
    const operations = deferred<{
      ingestion: { cancelled: number; cancellation_requested: number; failed: number; queued: number; retry_wait: number; running: number; succeeded: number };
      observed_at: string;
      retrieval: { embedding_generation_active: boolean };
      scope: "accessible_spaces";
      settings_revision: number;
      spaces: number;
      storage: { referenced_bytes: number };
    }>();
    get.mockImplementation((path: string) => {
      if (path === "/api/v1/spaces") {
        return spaces.promise;
      }
      if (path === "/healthz/ready") {
        return health.promise;
      }
      return operations.promise;
    });

    render(<DashboardController />);

    expect(screen.getByRole("status", { name: "Loading dashboard" })).toBeInTheDocument();
    expect(screen.queryByText("No spaces yet")).not.toBeInTheDocument();
    expect(screen.queryByText("Needs attention")).not.toBeInTheDocument();

    await act(async () => {
      spaces.resolve({
        items: [
          { created_at: "2026-08-20T12:00:00Z", id: "global", name: "Family Shared", role: "editor" },
          { created_at: "2026-08-19T12:00:00Z", id: "travel", name: "Travel plans", role: "owner" },
        ],
        page: 1,
        page_size: 3,
        total_items: 4,
        total_pages: 2,
      });
      health.resolve({ status: "ready" });
      operations.resolve({
        ingestion: { cancelled: 0, cancellation_requested: 0, failed: 0, queued: 0, retry_wait: 0, running: 0, succeeded: 0 },
        observed_at: "2026-08-20T12:00:00Z",
        retrieval: { embedding_generation_active: true },
        scope: "accessible_spaces",
        settings_revision: 1,
        spaces: 4,
        storage: { referenced_bytes: 0 },
      });
    });

    await waitFor(() => expect(screen.getByText("4 accessible spaces")).toBeInTheDocument());
    expect(get).toHaveBeenCalledWith("/api/v1/spaces", { params: { query: { page: 1, page_size: 3 } } });
  });
});
