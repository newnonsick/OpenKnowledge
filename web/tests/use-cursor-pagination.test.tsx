import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { useCursorPagination } from "@/lib/use-cursor-pagination";

type Item = { id: string; label: string };

describe("useCursorPagination", () => {
  it("loads the first page and appends deduplicated cursor pages without hiding current items", async () => {
    let resolveSecond: ((value: { items: Item[]; next_cursor: string | null }) => void) | undefined;
    const loadPage = vi.fn((cursor: string | null) => {
      if (cursor === null) {
        return Promise.resolve({ items: [{ id: "one", label: "One" }], next_cursor: "next" });
      }
      return new Promise<{ items: Item[]; next_cursor: string | null }>((resolve) => {
        resolveSecond = resolve;
      });
    });
    const { result } = renderHook(() => useCursorPagination({ keyOf: (item: Item) => item.id, loadPage, queryKey: "all" }));

    await waitFor(() => expect(result.current.items).toHaveLength(1));

    act(() => {
      void result.current.loadMore();
    });

    expect(result.current.items.map((item) => item.id)).toEqual(["one"]);
    expect(result.current.loadingMore).toBe(true);

    await act(async () => {
      resolveSecond?.({
        items: [
          { id: "one", label: "One duplicate" },
          { id: "two", label: "Two" },
        ],
        next_cursor: null,
      });
    });

    expect(result.current.items.map((item) => item.id)).toEqual(["one", "two"]);
    expect(result.current.loadingMore).toBe(false);
    expect(result.current.hasMore).toBe(false);
  });

  it("resets results when the server-side query changes", async () => {
    const loadPage = vi.fn((_: string | null, __: AbortSignal, query: string) => Promise.resolve({
      items: [{ id: query, label: query }],
      next_cursor: null,
    }));
    const { result, rerender } = renderHook(
      ({ query }) => useCursorPagination({
        keyOf: (item: Item) => item.id,
        loadPage: (cursor, signal) => loadPage(cursor, signal, query),
        queryKey: query,
      }),
      { initialProps: { query: "finance" } },
    );

    await waitFor(() => expect(result.current.items[0]?.id).toBe("finance"));
    rerender({ query: "engineering" });
    await waitFor(() => expect(result.current.items[0]?.id).toBe("engineering"));

    expect(result.current.items).toHaveLength(1);
  });
});
