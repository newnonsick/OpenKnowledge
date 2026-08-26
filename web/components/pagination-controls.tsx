"use client";

import { useEffect, useState } from "react";

type PaginationControlsProps = {
  page: number;
  pageSize: number;
  totalItems: number;
  totalPages: number;
  loading: boolean;
  loadingPage?: number | null;
  onPageChange: (page: number) => void;
};

function pageButtons(page: number, totalPages: number): Array<number | "ellipsis-start" | "ellipsis-end"> {
  if (totalPages <= 5) {
    return Array.from({ length: totalPages }, (_, index) => index + 1);
  }
  if (page <= 3) {
    return [1, 2, 3, 4, "ellipsis-end", totalPages];
  }
  if (page >= totalPages - 2) {
    return [1, "ellipsis-start", totalPages - 3, totalPages - 2, totalPages - 1, totalPages];
  }
  return [1, "ellipsis-start", page - 1, page, page + 1, "ellipsis-end", totalPages];
}

export function PaginationControls({
  page,
  pageSize,
  totalItems,
  totalPages,
  loading,
  loadingPage,
  onPageChange,
}: PaginationControlsProps) {
  const [directPage, setDirectPage] = useState(String(page));

  useEffect(() => {
    setDirectPage(String(page));
  }, [page]);

  const goToDirectPage = () => {
    if (loading || totalPages === 0) {
      return;
    }
    const parsed = Number.parseInt(directPage, 10);
    if (!Number.isInteger(parsed)) {
      setDirectPage(String(page));
      return;
    }
    onPageChange(Math.min(Math.max(parsed, 1), totalPages));
  };

  if (totalItems === 0) {
    return <p className="pagination-empty">No results</p>;
  }

  const firstItem = (page - 1) * pageSize + 1;
  const lastItem = Math.min(page * pageSize, totalItems);

  return (
    <nav aria-busy={loading || undefined} aria-label="Pagination" className="pagination-controls">
      <p aria-atomic="true" aria-live="polite" className="pagination-summary" role="status">{loading ? `Loading page ${loadingPage ?? page}…` : `Showing ${firstItem}–${lastItem} of ${totalItems}`}</p>
      <div className="pagination-actions">
        <button
          aria-label="Previous page"
          className="pagination-button"
          disabled={loading || page <= 1}
          onClick={() => onPageChange(page - 1)}
          type="button"
        >
          Prev
        </button>
        <div aria-label="Page numbers" className="pagination-pages" role="group">
          {pageButtons(page, totalPages).map((buttonPage) => typeof buttonPage === "number" ? (
            <button
              aria-current={buttonPage === page ? "page" : undefined}
              aria-label={`Page ${buttonPage}`}
              className={`pagination-button pagination-number${buttonPage === page ? " active" : ""}`}
              disabled={loading}
              key={buttonPage}
              onClick={() => onPageChange(buttonPage)}
              type="button"
            >
              {buttonPage}
            </button>
          ) : <span aria-hidden="true" className="pagination-ellipsis" key={buttonPage}>…</span>)}
        </div>
        <button
          aria-label="Next page"
          className="pagination-button"
          disabled={loading || page >= totalPages}
          onClick={() => onPageChange(page + 1)}
          type="button"
        >
          Next
        </button>
        <div className="pagination-direct">
          <label className="pagination-direct-label">
            Page
            <input
              aria-label="Go to page"
              className="pagination-direct-input"
              disabled={loading}
              inputMode="numeric"
              min={1}
              max={totalPages}
              onChange={(event) => setDirectPage(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  goToDirectPage();
                }
              }}
              type="number"
              value={directPage}
            />
            of {totalPages}
          </label>
          <button
            aria-label="Go to page"
            className="pagination-button"
            disabled={loading}
            onClick={goToDirectPage}
            type="button"
          >
            Go
          </button>
        </div>
      </div>
    </nav>
  );
}
