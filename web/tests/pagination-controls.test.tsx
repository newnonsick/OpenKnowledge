import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { PaginationControls } from "@/components/pagination-controls";

describe("PaginationControls", () => {
  it("shows numeric pages, boundaries, totals, and direct page entry for long lists", () => {
    const onPageChange = vi.fn();
    render(
      <PaginationControls
        loading={false}
        onPageChange={onPageChange}
        page={2}
        pageSize={25}
        totalItems={300}
        totalPages={12}
      />,
    );

    expect(screen.getByRole("navigation", { name: "Pagination" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Page 1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Page 2" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByText("26–50 of 300")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous page" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Next page" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Page 3" }));
    expect(onPageChange).toHaveBeenCalledWith(3);

    const directInput = screen.getByRole("textbox", { name: "Go to page" });
    fireEvent.change(directInput, { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: "Go to page" }));
    expect(onPageChange).toHaveBeenCalledWith(1);
  });

  it("omits direct page entry for short lists", () => {
    render(
      <PaginationControls
        loading={false}
        onPageChange={vi.fn()}
        page={2}
        pageSize={25}
        totalItems={75}
        totalPages={3}
      />,
    );

    expect(screen.getByText("26–50 of 75")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Go to page" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Go to page" })).not.toBeInTheDocument();
  });

  it("renders disabled steps for a single page and nothing for empty results", () => {
    const onPageChange = vi.fn();
    const { rerender } = render(
      <PaginationControls
        loading={false}
        onPageChange={onPageChange}
        page={1}
        pageSize={25}
        totalItems={25}
        totalPages={1}
      />,
    );

    expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();

    rerender(
      <PaginationControls
        loading={false}
        onPageChange={onPageChange}
        page={1}
        pageSize={25}
        totalItems={0}
        totalPages={0}
      />,
    );

    expect(screen.queryByRole("navigation", { name: "Pagination" })).not.toBeInTheDocument();
  });

  it("disables direct page entry while a page transition is pending", () => {
    render(
      <PaginationControls
        loading
        onPageChange={vi.fn()}
        page={2}
        pageSize={25}
        totalItems={300}
        totalPages={12}
      />,
    );

    expect(screen.getByRole("textbox", { name: "Go to page" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Go to page" })).toBeDisabled();
  });

  it("announces the current loading state to assistive technology", () => {
    render(
      <PaginationControls
        loading
        loadingPage={3}
        onPageChange={vi.fn()}
        page={2}
        pageSize={25}
        totalItems={75}
        totalPages={3}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("Loading page 3");
  });
});
