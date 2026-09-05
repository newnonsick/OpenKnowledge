"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export type PageResult<T> = {
  items: T[];
  page: number;
  page_size: number;
  total_items: number;
  total_pages: number;
};

type PageLoader<T> = (page: number, signal: AbortSignal) => Promise<PageResult<T>>;

type PagePaginationOptions<T> = {
  loadPage: PageLoader<T>;
  queryKey: string;
  initialPage?: number;
};

type PagePaginationState<T> = {
  items: T[];
  page: number;
  pageSize: number;
  totalItems: number;
  totalPages: number;
  initialLoading: boolean;
  loading: boolean;
  loadingPage: number | null;
  retryPage: number | null;
  resolvedQueryKey: string | null;
  error: string | null;
};

export function usePagePagination<T>({ loadPage, queryKey, initialPage = 1 }: PagePaginationOptions<T>) {
  const loadPageRef = useRef(loadPage);
  const queryKeyRef = useRef(queryKey);
  queryKeyRef.current = queryKey;
  const requestIdRef = useRef(0);
  const controllerRef = useRef<AbortController | null>(null);
  const stateRef = useRef<PagePaginationState<T>>({
    items: [],
    page: initialPage,
    pageSize: 0,
    totalItems: 0,
    totalPages: 0,
    initialLoading: true,
    loading: true,
    loadingPage: initialPage,
    retryPage: null,
    resolvedQueryKey: null,
    error: null,
  });
  const [state, setState] = useState(stateRef.current);

  useEffect(() => {
    loadPageRef.current = loadPage;
  }, [loadPage]);

  const updateState = useCallback((next: PagePaginationState<T>) => {
    stateRef.current = next;
    setState(next);
  }, []);

  const fetchPage = useCallback(async (requestedPage: number, clearItems: boolean) => {
    const targetPage = Number.isFinite(requestedPage) ? Math.max(1, Math.trunc(requestedPage)) : 1;
    const requestQueryKey = queryKeyRef.current;
    const requestId = ++requestIdRef.current;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    const previous = stateRef.current;
    const pendingState: PagePaginationState<T> = {
      ...previous,
      ...(clearItems ? {
        items: [],
        page: targetPage,
        pageSize: 0,
        totalItems: 0,
        totalPages: 0,
      } : {}),
      initialLoading: clearItems,
      loading: true,
      loadingPage: targetPage,
      retryPage: null,
      error: null,
    };
    updateState(pendingState);

    try {
      const response = await loadPageRef.current(targetPage, controller.signal);
      if (controller.signal.aborted || requestId !== requestIdRef.current) {
        return;
      }
      updateState({
        items: response.items,
        page: response.page,
        pageSize: response.page_size,
        totalItems: response.total_items,
        totalPages: response.total_pages,
        initialLoading: false,
        loading: false,
        loadingPage: null,
        retryPage: null,
        resolvedQueryKey: requestQueryKey,
        error: null,
      });
    } catch (error) {
      if (controller.signal.aborted || requestId !== requestIdRef.current) {
        return;
      }
      updateState({
        ...pendingState,
        initialLoading: false,
        loading: false,
        loadingPage: null,
        retryPage: targetPage,
        resolvedQueryKey: requestQueryKey,
        error: error instanceof Error ? error.message : "Unable to load this page.",
      });
    }
  }, [updateState]);

  useEffect(() => {
    void fetchPage(initialPage, true);
    return () => {
      requestIdRef.current += 1;
      controllerRef.current?.abort();
    };
  }, [fetchPage, initialPage, queryKey]);

  const goToPage = useCallback((targetPage: number) => fetchPage(targetPage, false), [fetchPage]);
  const reload = useCallback(() => fetchPage(stateRef.current.retryPage ?? stateRef.current.page, false), [fetchPage]);
  const nextPage = useCallback(() => {
    if (stateRef.current.page < stateRef.current.totalPages) {
      return fetchPage(stateRef.current.page + 1, false);
    }
    return Promise.resolve();
  }, [fetchPage]);
  const previousPage = useCallback(() => {
    if (stateRef.current.page > 1) {
      return fetchPage(stateRef.current.page - 1, false);
    }
    return Promise.resolve();
  }, [fetchPage]);

  return {
    ...state,
    hasNext: state.totalPages > 0 && state.page < state.totalPages,
    hasPrevious: state.page > 1,
    isRefreshing: state.loading && !state.initialLoading,
    queryReady: state.resolvedQueryKey === queryKey,
    goToPage,
    nextPage,
    previousPage,
    reload,
  };
}
