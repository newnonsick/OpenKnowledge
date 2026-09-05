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

function pageWindow(page: number, totalPages: number): (number | "gap")[] {
  if (totalPages <= 7) {
    return Array.from({ length: totalPages }, (_, index) => index + 1);
  }
  const siblings = new Set<number>([1, 2, page - 1, page, page + 1, totalPages - 1, totalPages]);
  const ordered = [...siblings].filter((value) => value >= 1 && value <= totalPages).sort((left, right) => left - right);
  const windowed: (number | "gap")[] = [];
  for (const value of ordered) {
    const last = windowed[windowed.length - 1];
    if (typeof last === "number" && value - last > 1) {
      windowed.push("gap");
    }
    windowed.push(value);
  }
  return windowed;
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

  if (totalItems === 0 || totalPages <= 1) {
    return null;
  }

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
      <p aria-atomic="true" aria-live="polite" className="pagination-summary" role="status">
        {loading ? `Loading page ${loadingPage ?? page}…` : `Page ${page} of ${totalPages} · ${totalItems} items`}
      </p>
      <div className="pagination-actions">
        <button
          aria-label="Previous page"
          className="pagination-button pagination-step"
          disabled={loading || page <= 1}
          onClick={() => onPageChange(page - 1)}
          type="button"
        >
          <ChevronLeft aria-hidden="true" size={15} />
        </button>
        {pageWindow(page, totalPages).map((entry, index) => entry === "gap" ? (
          <span aria-hidden="true" className="pagination-ellipsis" key={`gap-${index}`}>…</span>
        ) : (
          <button
            aria-current={entry === page ? "page" : undefined}
            aria-label={entry === page ? `Page ${entry}, current page` : `Go to page ${entry}`}
            className={`pagination-button${entry === page ? " is-current" : ""}`}
            disabled={loading || entry === page}
            key={entry}
            onClick={() => onPageChange(entry)}
            type="button"
          >
            {entry}
          </button>
        ))}
        <button
          aria-label="Next page"
          className="pagination-button pagination-step"
          disabled={loading || page >= totalPages}
          onClick={() => onPageChange(page + 1)}
          type="button"
        >
          <ChevronRight aria-hidden="true" size={15} />
        </button>
        {totalPages >= 8 ? (
          <label className="pagination-jump">
            <span>Go to</span>
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
          </label>
        ) : null}
      </div>
    </nav>
  );
}
