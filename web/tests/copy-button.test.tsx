import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CopyButton } from "@/components/copy-button";

describe("CopyButton", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("announces a successful copy", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    render(<CopyButton label="Copy API key" value="secret-value" />);

    fireEvent.click(screen.getByRole("button", { name: "Copy API key" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Copy API key copied" })).toBeInTheDocument());
    expect(screen.getByText("Copied")).toBeInTheDocument();
  });

  it("shows failure feedback when the clipboard is unavailable", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("denied"));
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    render(<CopyButton label="Copy API key" value="secret-value" />);

    fireEvent.click(screen.getByRole("button", { name: "Copy API key" }));

    await waitFor(() => expect(screen.getByRole("button", { name: "Copy API key failed to copy" })).toBeInTheDocument());
    expect(screen.getByText("Copy failed")).toBeInTheDocument();
    expect(screen.getByRole("button")).toHaveAttribute("title", "Copy failed — select the text manually");
  });
});
