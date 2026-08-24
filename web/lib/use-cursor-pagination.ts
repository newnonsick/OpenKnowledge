"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export type CursorPage<T> = {
  items: T[];
  next_cursor: string | null;
};

type CursorPaginationOptions<T> = {
  keyOf: (item: T) => string;
  loadPage: (cursor: string | null, signal: AbortSignal) => Promise<CursorPage<T>>;
  queryKey: string;
};

export function useCursorPagination<T>({ keyOf, loadPage, queryKey }: CursorPaginationOptions<T>) {
  const [items, setItems] = useState<T[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [reloadVersion, setReloadVersion] = useState(0);
  const activeController = useRef<AbortController | null>(null);
  const requestVersion = useRef(0);
  const loadPageRef = useRef(loadPage);
  const keyOfRef = useRef(keyOf);
  loadPageRef.current = loadPage;
  keyOfRef.current = keyOf;

  useEffect(() => {
    const controller = new AbortController();
    activeController.current?.abort();
    activeController.current = controller;
    const version = ++requestVersion.current;
    setItems([]);
    setCursor(null);
    setError(null);
    setInitialLoading(true);
    setLoadingMore(false);
    void loadPageRef.current(null, controller.signal).then(
      (page) => {
        if (version !== requestVersion.current || controller.signal.aborted) {
          return;
        }
        setItems(page.items);
        setCursor(page.next_cursor);
        setInitialLoading(false);
      },
      (loadError) => {
        if (version !== requestVersion.current || controller.signal.aborted) {
          return;
        }
        setError(loadError);
        setInitialLoading(false);
      },
    );
    return () => controller.abort();
  }, [queryKey, reloadVersion]);

  const loadMore = useCallback(async () => {
    if (!cursor || initialLoading || loadingMore) {
      return;
    }
    const controller = new AbortController();
    activeController.current?.abort();
    activeController.current = controller;
    const version = ++requestVersion.current;
    setLoadingMore(true);
    setError(null);
    try {
      const page = await loadPageRef.current(cursor, controller.signal);
      if (version !== requestVersion.current || controller.signal.aborted) {
        return;
      }
      setItems((current) => {
        const existing = new Set(current.map(keyOfRef.current));
        return [...current, ...page.items.filter((item) => !existing.has(keyOfRef.current(item)))];
      });
      setCursor(page.next_cursor);
    } catch (loadError) {
      if (version === requestVersion.current && !controller.signal.aborted) {
        setError(loadError);
      }
    } finally {
      if (version === requestVersion.current) {
        setLoadingMore(false);
      }
    }
  }, [cursor, initialLoading, loadingMore]);

  const reload = useCallback(() => setReloadVersion((current) => current + 1), []);

  return {
    error,
    hasMore: cursor !== null,
    initialLoading,
    items,
    loadingMore,
    loadMore,
    reload,
  };
}
