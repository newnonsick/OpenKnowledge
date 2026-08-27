import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { ConfirmationDialog } from "@/components/confirmation-dialog";

function EscapeHarness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOpen(true)} type="button">Archive trigger</button>
      <ConfirmationDialog
        cancelLabel="Keep item"
        confirmLabel="Archive item"
        description="It leaves search."
        onCancel={() => setOpen(false)}
        onConfirm={vi.fn()}
        open={open}
        title="Archive item?"
      />
    </>
  );
}

describe("ConfirmationDialog", () => {
  it("renders as a modal alertdialog and confirms once", () => {
    const confirm = vi.fn();
    render(
      <ConfirmationDialog
        cancelLabel="Keep item"
        confirmLabel="Archive item"
        description="It leaves search."
        onCancel={vi.fn()}
        onConfirm={confirm}
        open
        title="Archive item?"
        tone="danger"
      />,
    );

    const dialog = screen.getByRole("alertdialog", { name: "Archive item?" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveTextContent("It leaves search.");
    fireEvent.click(screen.getByRole("button", { name: "Archive item" }));
    expect(confirm).toHaveBeenCalledTimes(1);
  });

  it("cancels on Escape and restores focus to the trigger", async () => {
    render(<EscapeHarness />);
    const trigger = screen.getByRole("button", { name: "Archive trigger" });
    trigger.focus();
    fireEvent.click(trigger);

    await waitFor(() => expect(screen.getByRole("button", { name: "Keep item" })).toHaveFocus());
    fireEvent.keyDown(document, { key: "Escape" });

    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("locks duplicate actions while busy", () => {
    render(
      <ConfirmationDialog
        busy
        busyLabel="Archiving…"
        cancelLabel="Keep item"
        confirmLabel="Archive item"
        description="It leaves search."
        onCancel={vi.fn()}
        onConfirm={vi.fn()}
        open
        title="Archive item?"
      />,
    );

    expect(screen.getByRole("button", { name: "Archiving…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Keep item" })).toBeDisabled();
  });
});
