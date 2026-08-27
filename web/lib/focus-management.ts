"use client";

import { useLayoutEffect, useRef } from "react";
import type { Dispatch, SetStateAction } from "react";

const focusableSelector = "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";

function focusableElements(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(focusableSelector)).filter((element) => !element.hidden);
}

function containFocus(event: KeyboardEvent, root: HTMLElement): void {
  if (event.key !== "Tab") {
    return;
  }
  const elements = focusableElements(root);
  if (elements.length === 0) {
    event.preventDefault();
    root.focus();
    return;
  }
  const first = elements[0];
  const last = elements[elements.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

export function useDrawerFocus(open: boolean, setOpen: Dispatch<SetStateAction<boolean>>) {
  const drawerRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const wasOpen = useRef(false);

  useLayoutEffect(() => {
    if (!open) {
      if (wasOpen.current) {
        queueMicrotask(() => triggerRef.current?.focus());
      }
      wasOpen.current = false;
      return;
    }
    wasOpen.current = true;
    const focusTimer = window.setTimeout(() => {
      const closeButton = closeRef.current || drawerRef.current?.querySelector<HTMLButtonElement>("button[aria-label='Close navigation']");
      closeButton?.focus();
    }, 240);
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setOpen(false);
        return;
      }
      if (drawerRef.current) {
        containFocus(event, drawerRef.current);
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.clearTimeout(focusTimer);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open, setOpen]);

  return { closeRef, drawerRef, triggerRef };
}

export function useModalFocus(open: boolean, onClose: () => void) {
  const dialogRef = useRef<HTMLElement>(null);
  const returnRef = useRef<HTMLElement | null>(null);
  const closeRef = useRef(onClose);

  useLayoutEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);

  useLayoutEffect(() => {
    if (!open) {
      if (returnRef.current?.isConnected) {
        const target = returnRef.current;
        queueMicrotask(() => target.focus());
      }
      returnRef.current = null;
      return;
    }

    returnRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const background = Array.from(document.body.children).filter((element) => !element.hasAttribute("data-modal-root")) as HTMLElement[];
    const priorOverflow = document.body.style.overflow;
    background.forEach((element) => {
      element.inert = true;
    });
    document.body.style.overflow = "hidden";

    queueMicrotask(() => {
      const dialog = dialogRef.current;
      if (!dialog) {
        return;
      }
      const initial = focusableElements(dialog)[0];
      if (initial) {
        initial.focus();
      } else {
        dialog.focus();
      }
    });

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeRef.current();
        return;
      }
      if (dialogRef.current) {
        containFocus(event, dialogRef.current);
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      background.forEach((element) => {
        element.inert = false;
      });
      document.body.style.overflow = priorOverflow;
    };
  }, [open]);

  return dialogRef;
}
