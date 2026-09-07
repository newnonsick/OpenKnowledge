import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { PaginationControls } from "@/components/pagination-controls";

describe("PaginationControls", () => {
  it("shows a range summary with compact navigation and clamped direct entry for long lists", () => {
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
    expect(screen.getByText("26–50 of 300")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Go to page 1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Page 2" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous page" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Next page" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Previous page" }));
    expect(onPageChange).toHaveBeenCalledWith(1);
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(onPageChange).toHaveBeenCalledWith(3);

    const directInput = screen.getByRole("textbox", { name: "Page number, 1 to 12" });
    fireEvent.change(directInput, { target: { value: "99" } });
    fireEvent.keyDown(directInput, { key: "Enter" });
    expect(onPageChange).toHaveBeenCalledWith(12);
  });

  it("clamps the range summary on the final partial page", () => {
    render(
      <PaginationControls
        loading={false}
        onPageChange={vi.fn()}
        page={2}
        pageSize={25}
        totalItems={33}
        totalPages={2}
      />,
    );

    expect(screen.getByText("26–33 of 33")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /Go to page/ })).not.toBeInTheDocument();
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
    expect(screen.getByRole("button", { name: "Page 2" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /Go to page/ })).not.toBeInTheDocument();
  });

  it("renders nothing for a single page or empty results", () => {
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

    expect(screen.queryByRole("navigation", { name: "Pagination" })).not.toBeInTheDocument();

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

    expect(screen.getByRole("textbox", { name: "Page number, 1 to 12" })).toBeDisabled();
  });

  it("keeps the visible summary stable while announcing loading separately", () => {
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

    expect(screen.getByText("26–50 of 75")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Loading page 3");
  });

  it("renders a five-button window with edge pages for long lists", () => {
    render(
      <PaginationControls
        loading={false}
        onPageChange={vi.fn()}
        page={6}
        pageSize={25}
        totalItems={300}
        totalPages={12}
      />,
    );

    const numberedButtons = screen.getAllByRole("button", { name: /^(Go to page|Page) \d/ });
    expect(numberedButtons).toHaveLength(5);
    expect(screen.getByRole("button", { name: "Go to page 1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Page 6" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Go to page 12" })).toBeInTheDocument();
  });

  it("shows the jump control only once the list reaches ten pages", () => {
    const onPageChange = vi.fn();
    const { rerender } = render(
      <PaginationControls
        loading={false}
        onPageChange={onPageChange}
        page={1}
        pageSize={25}
        totalItems={225}
        totalPages={9}
      />,
    );

    expect(screen.queryByRole("textbox", { name: /Go to page/ })).not.toBeInTheDocument();

    rerender(
      <PaginationControls
        loading={false}
        onPageChange={onPageChange}
        page={1}
        pageSize={25}
        totalItems={250}
        totalPages={10}
      />,
    );

    expect(screen.getByRole("textbox", { name: "Page number, 1 to 10" })).toBeInTheDocument();
  });
});
