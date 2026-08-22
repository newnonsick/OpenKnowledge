"use client";

import { useEffect, useLayoutEffect, useRef } from "react";
import type { Dispatch, RefObject, SetStateAction } from "react";

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

export function useAlertDialogFocus(rootRef: RefObject<HTMLElement | null>): void {
  useEffect(() => {
    const root = rootRef.current;
    if (!root) {
      return;
    }
    let activeDialog: HTMLElement | null = null;
    let returnTarget: HTMLElement | null = null;
    const update = () => {
      const next = root.querySelector<HTMLElement>("[role='alertdialog']");
      if (next === activeDialog) {
        return;
      }
      if (next) {
        returnTarget = document.activeElement instanceof HTMLElement ? document.activeElement : null;
        activeDialog = next;
        queueMicrotask(() => focusableElements(next)[0]?.focus());
        return;
      }
      if (activeDialog && returnTarget?.isConnected) {
        const target = returnTarget;
        queueMicrotask(() => target.focus());
      }
      activeDialog = null;
      returnTarget = null;
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (!activeDialog) {
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        focusableElements(activeDialog)[0]?.click();
        return;
      }
      containFocus(event, activeDialog);
    };
    const observer = new MutationObserver(update);
    observer.observe(root, { childList: true, subtree: true });
    document.addEventListener("keydown", handleKeyDown);
    update();
    return () => {
      observer.disconnect();
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [rootRef]);
}
