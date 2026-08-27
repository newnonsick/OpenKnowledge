"use client";

import { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";

type PaginationControlsProps = {
  page: number;
  pageSize: number;
  totalItems: number;
  totalPages: number;
  loading: boolean;
  loadingPage?: number | null;
  onPageChange: (page: number) => void;
};

const jumpMinPages = 8;

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

  if (totalItems === 0 || totalPages <= 1) {
    return null;
  }

  const firstItem = (page - 1) * pageSize + 1;
  const lastItem = Math.min(page * pageSize, totalItems);

  const goToDirectPage = () => {
    if (loading) {
      return;
    }
    const parsed = Number.parseInt(directPage, 10);
    if (!Number.isInteger(parsed)) {
      setDirectPage(String(page));
      return;
    }
    const target = Math.min(Math.max(parsed, 1), totalPages);
    setDirectPage(String(target));
    if (target !== page) {
      onPageChange(target);
    }
  };

  return (
    <nav aria-busy={loading || undefined} aria-label="Pagination" className="pagination-controls">
      <p aria-atomic="true" aria-live="polite" className="pagination-summary" role="status">{loading ? `Loading page ${loadingPage ?? page}…` : `${firstItem}–${lastItem} of ${totalItems}`}</p>
      <div className="pagination-actions">
        <button
          aria-label="Previous page"
          className="pagination-button pagination-step"
          disabled={loading || page <= 1}
          onClick={() => onPageChange(page - 1)}
          type="button"
        >
          <ChevronLeft aria-hidden="true" size={15} />
          <span className="pagination-step-label">Previous</span>
        </button>
        {totalPages >= jumpMinPages ? (
          <label className="pagination-jump-label">
            <span>Page</span>
            <input
              aria-label={`Jump to page of ${totalPages}`}
              className="pagination-jump-input"
              disabled={loading}
              inputMode="numeric"
              max={totalPages}
              min={1}
              onBlur={(event) => {
                if (event.target.value !== String(page)) {
                  goToDirectPage();
                }
              }}
              onChange={(event) => setDirectPage(event.target.value.replace(/\D/g, ""))}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  goToDirectPage();
                }
              }}
              type="text"
              value={directPage}
            />
            <span>of {totalPages}</span>
          </label>
        ) : <span aria-current="page" className="pagination-current">Page {page} of {totalPages}</span>}
        <button
          aria-label="Next page"
          className="pagination-button pagination-step"
          disabled={loading || page >= totalPages}
          onClick={() => onPageChange(page + 1)}
          type="button"
        >
          <span className="pagination-step-label">Next</span>
          <ChevronRight aria-hidden="true" size={15} />
        </button>
      </div>
    </nav>
  );
}
