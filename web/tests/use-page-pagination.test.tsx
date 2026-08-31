import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { usePagePagination } from "@/lib/use-page-pagination";

type Item = { id: string; label: string };
type PageResult = {
  items: Item[];
  page: number;
  page_size: number;
  total_items: number;
  total_pages: number;
};

describe("usePagePagination", () => {
  it("replaces the current slice on a direct page jump while retaining it during loading", async () => {
    let resolveSecond: ((value: PageResult) => void) | undefined;
    const loadPage = vi.fn((page: number) => {
      if (page === 1) {
        return Promise.resolve({
          items: [{ id: "one", label: "One" }],
          page: 1,
          page_size: 1,
          total_items: 2,
          total_pages: 2,
        });
      }
      return new Promise<PageResult>((resolve) => {
        resolveSecond = resolve;
      });
    });
    const { result } = renderHook(() => usePagePagination({ loadPage, queryKey: "all" }));

    await waitFor(() => expect(result.current.items.map((item) => item.id)).toEqual(["one"]));

    act(() => {
      void result.current.goToPage(2);
    });

    expect(result.current.items.map((item) => item.id)).toEqual(["one"]);
    expect(result.current.loading).toBe(true);

    await act(async () => {
      resolveSecond?.({
        items: [{ id: "two", label: "Two" }],
        page: 2,
        page_size: 1,
        total_items: 2,
        total_pages: 2,
      });
    });

    expect(result.current.items.map((item) => item.id)).toEqual(["two"]);
    expect(result.current.page).toBe(2);
    expect(result.current.hasPrevious).toBe(true);
    expect(result.current.hasNext).toBe(false);
  });

  it("keeps the last successful page and range when a later page request fails", async () => {
    const loadPage = vi.fn(async (page: number) => {
      if (page === 1) {
        return {
          items: [{ id: "one", label: "One" }],
          page: 1,
          page_size: 25,
          total_items: 50,
          total_pages: 2,
        };
      }
      throw new Error("Page two is unavailable");
    });
    const { result } = renderHook(() => usePagePagination({ loadPage, queryKey: "failed-transition" }));

    await waitFor(() => expect(result.current.items.map((item) => item.id)).toEqual(["one"]));
    await act(async () => {
      await result.current.goToPage(2);
    });

    expect(result.current.error).toBe("Page two is unavailable");
    expect(result.current.items.map((item) => item.id)).toEqual(["one"]);
    expect(result.current.page).toBe(1);
    expect(result.current.pageSize).toBe(25);
    expect(result.current.totalItems).toBe(50);
    expect(result.current.totalPages).toBe(2);
  });

  it("retries the failed target page instead of the last successful page", async () => {
    let pageTwoAttempts = 0;
    const loadPage = vi.fn(async (page: number) => {
      if (page === 1) {
        return {
          items: [{ id: "one", label: "One" }],
          page: 1,
          page_size: 25,
          total_items: 50,
          total_pages: 2,
        };
      }
      pageTwoAttempts += 1;
      if (pageTwoAttempts === 1) {
        throw new Error("Page two is unavailable");
      }
      return {
        items: [{ id: "two", label: "Two" }],
        page: 2,
        page_size: 25,
        total_items: 50,
        total_pages: 2,
      };
    });
    const { result } = renderHook(() => usePagePagination({ loadPage, queryKey: "retry-failed-target" }));

    await waitFor(() => expect(result.current.items.map((item) => item.id)).toEqual(["one"]));
    await act(async () => {
      await result.current.goToPage(2);
    });
    expect(result.current.error).toBe("Page two is unavailable");

    await act(async () => {
      await result.current.reload();
    });

    await waitFor(() => expect(result.current.items.map((item) => item.id)).toEqual(["two"]));
    expect(result.current.page).toBe(2);
    expect(loadPage.mock.calls.map(([page]) => page)).toEqual([1, 2, 2]);
  });

  it("resets to page one when the server-side query changes", async () => {
    const loadPage = vi.fn((_: number, __: AbortSignal, query: string) => Promise.resolve({
      items: [{ id: query, label: query }],
      page: 1,
      page_size: 25,
      total_items: 1,
      total_pages: 1,
    }));
    const { result, rerender } = renderHook(
      ({ query }) => usePagePagination({
        loadPage: (page, signal) => loadPage(page, signal, query),
        queryKey: query,
      }),
      { initialProps: { query: "finance" } },
    );

    await waitFor(() => expect(result.current.items[0]?.id).toBe("finance"));
    rerender({ query: "engineering" });
    await waitFor(() => expect(result.current.items[0]?.id).toBe("engineering"));

    expect(result.current.page).toBe(1);
    expect(result.current.items).toHaveLength(1);
  });

  it("marks changed query data stale until the replacement request settles", async () => {
    let resolveEngineering: ((value: { items: { id: string; label: string }[]; page: number; page_size: number; total_items: number; total_pages: number }) => void) | undefined;
    const { result, rerender } = renderHook(
      ({ query }) => usePagePagination({
        loadPage: () => query === "finance"
          ? Promise.resolve({ items: [{ id: "finance", label: "Finance" }], page: 1, page_size: 25, total_items: 1, total_pages: 1 })
          : new Promise<{ items: { id: string; label: string }[]; page: number; page_size: number; total_items: number; total_pages: number }>((resolve) => { resolveEngineering = resolve; }),
        queryKey: query,
      }),
      { initialProps: { query: "finance" } },
    );

    await waitFor(() => expect(result.current.queryReady).toBe(true));
    rerender({ query: "engineering" });
    expect(result.current.queryReady).toBe(false);

    resolveEngineering?.({ items: [{ id: "engineering", label: "Engineering" }], page: 1, page_size: 25, total_items: 1, total_pages: 1 });
    await waitFor(() => expect(result.current.queryReady).toBe(true));
    expect(result.current.items[0]?.id).toBe("engineering");
  });

  it("exposes errors and can recover with reload", async () => {
    let attempt = 0;
    const loadPage = vi.fn(async () => {
      attempt += 1;
      if (attempt === 1) {
        throw new Error("Gateway unavailable");
      }
      return {
        items: [{ id: "recovered", label: "Recovered" }],
        page: 1,
        page_size: 25,
        total_items: 1,
        total_pages: 1,
      };
    });
    const { result } = renderHook(() => usePagePagination({ loadPage, queryKey: "retry" }));

    await waitFor(() => expect(result.current.error).toBe("Gateway unavailable"));
    await act(async () => {
      await result.current.reload();
    });

    expect(result.current.error).toBeNull();
    expect(result.current.items[0]?.id).toBe("recovered");
  });
});
