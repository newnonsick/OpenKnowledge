import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PaginationControls } from "@/components/pagination-controls";

const defaults = { loading: false, page: 2, pageSize: 25, totalItems: 300, totalPages: 12 };

describe("PaginationControls", () => {
  it("submits a typed page after keyboard focus moves to Go", () => {
    const onPageChange = vi.fn();
    render(<PaginationControls {...defaults} onPageChange={onPageChange} />);
    const input = screen.getByRole("textbox", { name: "Page number, 1 to 12" });
    fireEvent.change(input, { target: { value: "5" } });
    fireEvent.blur(input);
    fireEvent.click(screen.getByRole("button", { name: "Go" }));
    expect(onPageChange).toHaveBeenCalledWith(5);
  });

  it("navigates relative to the displayed page", () => {
    const onPageChange = vi.fn();
    render(<PaginationControls {...defaults} onPageChange={onPageChange} />);
    expect(screen.getByText("26–50 of 300")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Previous page" }));
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(onPageChange.mock.calls).toEqual([[1], [3]]);
  });

  it.each([["99", 12], ["0", 1], ["5", 5]])("clamps direct page %s to %s", (value, expected) => {
    const onPageChange = vi.fn();
    render(<PaginationControls {...defaults} onPageChange={onPageChange} />);
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onPageChange).toHaveBeenCalledWith(expected);
  });

  it("restores the current page on Escape without navigating", () => {
    const onPageChange = vi.fn();
    render(<PaginationControls {...defaults} onPageChange={onPageChange} />);
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "5" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(input).toHaveValue("2");
    expect(onPageChange).not.toHaveBeenCalled();
  });

  it("blocks duplicate pending requests while allowing a different target", () => {
    const onPageChange = vi.fn();
    render(<PaginationControls {...defaults} loading loadingPage={3} onPageChange={onPageChange} />);
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Previous page" })).toBeEnabled();
    expect(screen.getByText("26–50 of 300")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("Loading page 3");
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "4" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onPageChange).toHaveBeenCalledWith(4);
  });

  it("reflects a completed page change and a partial last page", () => {
    const onPageChange = vi.fn();
    const { rerender } = render(<PaginationControls {...defaults} onPageChange={onPageChange} />);
    rerender(<PaginationControls {...defaults} page={3} totalItems={62} totalPages={3} onPageChange={onPageChange} />);
    expect(screen.getByRole("textbox")).toHaveValue("3");
    expect(screen.getByText("51–62 of 62")).toBeVisible();
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
  });

  it("omits navigation controls for a single page and hides empty pagination", () => {
    const onPageChange = vi.fn();
    const { rerender } = render(<PaginationControls {...defaults} page={1} totalItems={20} totalPages={1} onPageChange={onPageChange} />);
    expect(screen.getByText("1–20 of 20")).toBeVisible();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    rerender(<PaginationControls {...defaults} page={1} totalItems={0} totalPages={0} onPageChange={onPageChange} />);
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });
});
