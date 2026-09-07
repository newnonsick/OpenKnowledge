"use client";

import { useEffect, useId, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, LoaderCircle } from "lucide-react";

type PaginationControlsProps = {
  page: number;
  pageSize: number;
  totalItems: number;
  totalPages: number;
  loading: boolean;
  loadingPage?: number | null;
  onPageChange: (page: number) => void;
};

const jumpThreshold = 10;

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

function rangeSummary(page: number, pageSize: number, totalItems: number): string {
  if (totalItems === 0) {
    return "No results";
  }
  const size = pageSize > 0 ? pageSize : totalItems;
  const start = (page - 1) * size + 1;
  const end = Math.min(page * size, totalItems);
  return `${start}–${end} of ${totalItems}`;
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
  const jumpInputRef = useRef<HTMLInputElement>(null);
  const jumpLabelId = useId();

  useEffect(() => {
    if (document.activeElement !== jumpInputRef.current) {
      setDirectPage(String(page));
    }
  }, [page]);

  if (totalItems === 0 || totalPages <= 0) {
    return null;
  }

  const goToDirectPage = () => {
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

  const summary = rangeSummary(page, pageSize, totalItems);

  if (totalPages <= 1) {
    return (
      <nav aria-label="Pagination" className="pagination-controls pagination-single">
        <p className="pagination-summary">{summary}</p>
      </nav>
    );
  }

  const pendingPage = loading ? (loadingPage ?? page) : null;

  return (
    <nav aria-busy={loading || undefined} aria-label="Pagination" className="pagination-controls">
      <p aria-live="polite" className="pagination-summary">{summary}</p>
      <div className="pagination-actions">
        <button
          aria-label="Previous page"
          className="pagination-button pagination-step"
          disabled={page <= 1}
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
            aria-disabled={entry === pendingPage || undefined}
            aria-label={entry === page ? `Page ${entry}` : `Go to page ${entry}`}
            className={`pagination-button${entry === page ? " is-current" : ""}${entry === pendingPage ? " is-loading" : ""}`}
            disabled={entry === page}
            key={entry}
            onClick={() => {
              if (entry !== pendingPage) {
                onPageChange(entry);
              }
            }}
            type="button"
          >
            {entry === pendingPage ? <LoaderCircle aria-hidden="true" className="spin" size={13} /> : entry}
          </button>
        ))}
        <button
          aria-label="Next page"
          className="pagination-button pagination-step"
          disabled={page >= totalPages}
          onClick={() => onPageChange(page + 1)}
          type="button"
        >
          <ChevronRight aria-hidden="true" size={15} />
        </button>
        {totalPages >= jumpThreshold ? (
          <span className="pagination-jump">
            <label htmlFor={jumpLabelId}>Page</label>
            <input
              aria-label={`Page number, 1 to ${totalPages}`}
              className="pagination-jump-input"
              id={jumpLabelId}
              inputMode="numeric"
              max={totalPages}
              min={1}
              onBlur={() => setDirectPage(String(page))}
              onChange={(event) => setDirectPage(event.target.value.replace(/\D/g, ""))}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  goToDirectPage();
                  jumpInputRef.current?.blur();
                } else if (event.key === "Escape") {
                  setDirectPage(String(page));
                  jumpInputRef.current?.blur();
                }
              }}
              pattern="[0-9]*"
              ref={jumpInputRef}
              type="text"
              value={directPage}
            />
            <button
              className="pagination-button pagination-go"
              onClick={goToDirectPage}
              onMouseDown={(event) => event.preventDefault()}
              type="button"
            >
              Go
            </button>
          </span>
        ) : null}
      </div>
      <span className="visually-hidden" role="status">{loading ? `Loading page ${loadingPage ?? page}` : ""}</span>
    </nav>
  );
}
