import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useEffect, useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { ConfirmationDialog, ModalDialog } from "@/components/confirmation-dialog";
import { modalReturnTargetFor } from "@/lib/focus-management";

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

function StepUpHarness() {
  const [open, setOpen] = useState(true);
  return (
    <ConfirmationDialog
      cancelLabel="Keep item"
      confirmLabel="Archive item"
      description="It leaves search."
      onCancel={() => setOpen(false)}
      onConfirm={vi.fn()}
      open={open}
      title="Archive item?"
    />
  );
}

function HandoffHarness() {
  const [confirmationOpen, setConfirmationOpen] = useState(false);
  const [stepUpOpen, setStepUpOpen] = useState(false);
  const [stepUpReturnTarget, setStepUpReturnTarget] = useState<HTMLElement | null>(null);

  useEffect(() => {
    const openStepUp = (event: Event) => {
      setStepUpReturnTarget(modalReturnTargetFor(event));
      setStepUpOpen(true);
    };
    window.addEventListener("aigw-step-up-required", openStepUp);
    return () => window.removeEventListener("aigw-step-up-required", openStepUp);
  }, []);

  return (
    <>
      <button onClick={() => setConfirmationOpen(true)} type="button">Transfer owner</button>
      <ConfirmationDialog
        cancelLabel="Keep owner"
        confirmLabel="Transfer ownership"
        description="Identity verification is required."
        onCancel={() => setConfirmationOpen(false)}
        onConfirm={vi.fn()}
        open={confirmationOpen}
        title="Transfer ownership?"
      />
      <ModalDialog ariaLabelledBy="step-up-title" onClose={() => setStepUpOpen(false)} open={stepUpOpen} returnFocusTarget={stepUpReturnTarget}>
        <h2 id="step-up-title">Verify identity</h2>
        <button onClick={() => setStepUpOpen(false)} type="button">Finish verification</button>
      </ModalDialog>
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

  it("can disable confirmation without blocking cancellation", () => {
    render(
      <ConfirmationDialog
        cancelLabel="Keep current"
        confirmDisabled
        confirmLabel="Restore revision"
        description="A reason is required."
        onCancel={vi.fn()}
        onConfirm={vi.fn()}
        open
        title="Restore settings"
      />,
    );

    expect(screen.getByRole("button", { name: "Restore revision" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Keep current" })).toBeEnabled();
  });

  it("cancels when identity verification opens", async () => {
    render(<StepUpHarness />);

    window.dispatchEvent(new CustomEvent("aigw-step-up-required"));

    await waitFor(() => expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument());
  });

  it("returns focus to the original action after identity verification", async () => {
    render(<HandoffHarness />);
    const trigger = screen.getByRole("button", { name: "Transfer owner" });
    trigger.focus();
    fireEvent.click(trigger);
    await waitFor(() => expect(screen.getByRole("button", { name: "Keep owner" })).toHaveFocus());

    const stepUpEvent = new CustomEvent("aigw-step-up-required");
    expect(modalReturnTargetFor(stepUpEvent)).toBe(trigger);
    window.dispatchEvent(stepUpEvent);
    expect(modalReturnTargetFor(stepUpEvent)).toBe(trigger);
    await waitFor(() => expect(screen.getByRole("dialog", { name: "Verify identity" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Finish verification" }));

    await waitFor(() => expect(trigger).toHaveFocus());
  });
});
