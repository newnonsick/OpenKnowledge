"use client";

import { useEffect, useId, useState } from "react";
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

export function PaginationControls({ page, pageSize, totalItems, totalPages, loading, loadingPage, onPageChange }: PaginationControlsProps) {
  const [directPage, setDirectPage] = useState(String(page));
  const inputId = useId();

  useEffect(() => setDirectPage(String(page)), [page]);

  if (totalItems === 0 || totalPages <= 0) return null;

  const size = pageSize > 0 ? pageSize : totalItems;
  const summary = `${(page - 1) * size + 1}–${Math.min(page * size, totalItems)} of ${totalItems}`;
  const targetPage = Math.min(Math.max(directPage ? Number(directPage) : page, 1), totalPages);
  const pendingPage = loading ? loadingPage ?? page : null;

  const go = () => {
    setDirectPage(String(targetPage));
    if (targetPage !== page && targetPage !== pendingPage) onPageChange(targetPage);
  };

  return (
    <nav aria-label="Pagination" className={`pagination-controls${totalPages === 1 ? " pagination-single" : ""}`}>
      <p className="pagination-summary">{summary}</p>
      {totalPages > 1 ? (
        <div className="pagination-actions">
          <button aria-label="Previous page" className="pagination-button pagination-step" disabled={page <= 1 || page - 1 === pendingPage} onClick={() => onPageChange(page - 1)} type="button"><ChevronLeft aria-hidden="true" size={16} /></button>
          <span className="pagination-position">
            <label htmlFor={inputId}>Page</label>
            <input
              aria-label={`Page number, 1 to ${totalPages}`}
              className="pagination-jump-input"
              id={inputId}
              inputMode="numeric"
              onChange={(event) => setDirectPage(event.target.value.replace(/\D/g, ""))}
              onKeyDown={(event) => {
                if (event.key === "Enter") { event.preventDefault(); go(); }
                if (event.key === "Escape") { event.preventDefault(); setDirectPage(String(page)); }
              }}
              pattern="[0-9]*"
              type="text"
              value={directPage}
            />
            <span>of {totalPages}</span>
            <button aria-label="Go" className="pagination-button pagination-go" disabled={targetPage === page || targetPage === pendingPage} onClick={go} type="button">Go</button>
          </span>
          <button aria-label="Next page" className="pagination-button pagination-step" disabled={page >= totalPages || page + 1 === pendingPage} onClick={() => onPageChange(page + 1)} type="button"><ChevronRight aria-hidden="true" size={16} /></button>
        </div>
      ) : null}
      <span className="pagination-progress" role="status">{loading ? <><LoaderCircle aria-hidden="true" className="spin" size={14} /><span className="visually-hidden">Loading page {loadingPage ?? page}</span></> : <span className="visually-hidden">Page {page} of {totalPages}</span>}</span>
    </nav>
  );
}
