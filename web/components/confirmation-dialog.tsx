"use client";

import { ReactNode, useEffect, useId, useState } from "react";
import { createPortal } from "react-dom";
import { AlertTriangle, LoaderCircle } from "lucide-react";

import { useModalFocus } from "@/lib/focus-management";

type ModalDialogProps = {
  ariaDescribedBy?: string;
  ariaLabelledBy: string;
  children: ReactNode;
  className?: string;
  onClose: () => void;
  open: boolean;
  returnFocusTarget?: HTMLElement | null;
  role?: "alertdialog" | "dialog";
};

export function ModalDialog({
  ariaDescribedBy,
  ariaLabelledBy,
  children,
  className = "",
  onClose,
  open,
  returnFocusTarget,
  role = "dialog",
}: ModalDialogProps) {
  const dialogRef = useModalFocus(open, onClose, returnFocusTarget);
  const [portalRoot] = useState<HTMLElement | null>(() => {
    if (typeof document === "undefined") {
      return null;
    }
    const existing = document.querySelector<HTMLElement>("[data-modal-root]");
    if (existing) {
      return existing;
    }
    const root = document.createElement("div");
    root.dataset.modalRoot = "";
    document.body.appendChild(root);
    return root;
  });

  if (!open || !portalRoot) {
    return null;
  }

  return createPortal(
    <div className="modal-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget) {
        onClose();
      }
    }}>
      <section
        aria-describedby={ariaDescribedBy}
        aria-labelledby={ariaLabelledBy}
        aria-modal="true"
        className={`modal-surface${className ? ` ${className}` : ""}`}
        ref={dialogRef}
        role={role}
        tabIndex={-1}
      >
        {children}
      </section>
    </div>,
    portalRoot,
  );
}

type ConfirmationDialogProps = {
  busy?: boolean;
  busyLabel?: string;
  cancelLabel: string;
  confirmDisabled?: boolean;
  confirmLabel: string;
  description: string;
  onCancel: () => void;
  onConfirm: () => void;
  open: boolean;
  title: string;
  tone?: "danger" | "primary" | "neutral";
};

export function ConfirmationDialog({
  busy = false,
  busyLabel,
  cancelLabel,
  confirmDisabled = false,
  confirmLabel,
  description,
  onCancel,
  onConfirm,
  open,
  title,
  tone = "neutral",
}: ConfirmationDialogProps) {
  const titleId = useId();
  const descriptionId = useId();
  const actionClass = tone === "danger" ? "danger-button" : tone === "primary" ? "primary-button" : "secondary-button modal-confirm-button";

  useEffect(() => {
    if (!open) {
      return;
    }
    const closeForStepUp = () => onCancel();
    window.addEventListener("aigw-step-up-required", closeForStepUp);
    return () => window.removeEventListener("aigw-step-up-required", closeForStepUp);
  }, [onCancel, open]);

  return (
    <ModalDialog
      ariaDescribedBy={descriptionId}
      ariaLabelledBy={titleId}
      className={`confirmation-dialog tone-${tone}`}
      onClose={() => {
        if (!busy) {
          onCancel();
        }
      }}
      open={open}
      role="alertdialog"
    >
      <div className="modal-heading">
        <span className="modal-icon"><AlertTriangle aria-hidden="true" size={18} /></span>
        <div>
          <small>Confirm action</small>
          <h2 id={titleId}>{title}</h2>
        </div>
      </div>
      <p className="modal-copy" id={descriptionId}>{description}</p>
      <div className="modal-actions">
        <button className="secondary-button" disabled={busy} onClick={onCancel} type="button">{cancelLabel}</button>
        <button className={actionClass} disabled={busy || confirmDisabled} onClick={onConfirm} type="button">
          {busy ? <LoaderCircle aria-hidden="true" className="spin" size={16} /> : null}
          {busy ? busyLabel || `${confirmLabel}…` : confirmLabel}
        </button>
      </div>
    </ModalDialog>
  );
}
