"use client";

import { useLayoutEffect, useRef } from "react";
import type { Dispatch, SetStateAction } from "react";

const focusableSelector = "a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])";
const modalReturnTargets = new WeakMap<Event, HTMLElement>();
let activeModalReturnTarget: HTMLElement | null = null;
type ModalStackEntry = {
  dialogRef: { current: HTMLElement | null };
  returnTarget: HTMLElement | null;
  token: symbol;
};
const modalStack: ModalStackEntry[] = [];
let backgroundInertState: Map<HTMLElement, boolean> | null = null;
let bodyOverflowBeforeModal = "";

function refreshModalStack() {
  const top = modalStack.at(-1);
  modalStack.forEach((entry) => {
    const backdrop = entry.dialogRef.current?.parentElement;
    if (!backdrop) {
      return;
    }
    if (entry === top) {
      delete backdrop.dataset.modalSuspended;
      entry.dialogRef.current!.inert = false;
    } else {
      backdrop.dataset.modalSuspended = "";
      entry.dialogRef.current!.inert = true;
    }
  });
  activeModalReturnTarget = top?.returnTarget?.isConnected ? top.returnTarget : null;
}

function lockModalBackground() {
  if (backgroundInertState) {
    return;
  }
  const background = Array.from(document.body.children).filter((element) => !element.hasAttribute("data-modal-root")) as HTMLElement[];
  backgroundInertState = new Map(background.map((element) => [element, element.inert]));
  background.forEach((element) => {
    element.inert = true;
  });
  bodyOverflowBeforeModal = document.body.style.overflow;
  document.body.style.overflow = "hidden";
}

function unlockModalBackground() {
  backgroundInertState?.forEach((inert, element) => {
    if (element.isConnected) {
      element.inert = inert;
    }
  });
  backgroundInertState = null;
  document.body.style.overflow = bodyOverflowBeforeModal;
}

export function modalReturnTargetFor(event: Event): HTMLElement | null {
  const mappedTarget = modalReturnTargets.get(event);
  if (mappedTarget?.isConnected) {
    return mappedTarget;
  }
  if (activeModalReturnTarget?.isConnected) {
    modalReturnTargets.set(event, activeModalReturnTarget);
    return activeModalReturnTarget;
  }
  return null;
}

export function focusModalReturnTarget(target: HTMLElement | null): boolean {
  if (!target) {
    return false;
  }
  const connectedTarget = target.isConnected
    ? target
    : target.id
      ? document.getElementById(target.id)
      : Array.from(document.querySelectorAll<HTMLElement>("[aria-label]")).find((candidate) => candidate.getAttribute("aria-label") === target.getAttribute("aria-label"));
  if (!connectedTarget || connectedTarget.closest("[inert], [data-modal-suspended]")) {
    return false;
  }
  connectedTarget.focus();
  return document.activeElement === connectedTarget;
}

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

export function useModalFocus(open: boolean, onClose: () => void, explicitReturnTarget?: HTMLElement | null) {
  const dialogRef = useRef<HTMLElement>(null);
  const returnRef = useRef<HTMLElement | null>(null);
  const closeRef = useRef(onClose);

  useLayoutEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);

  useLayoutEffect(() => {
    if (!open) {
      returnRef.current = null;
      return;
    }

    const activeElement = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    returnRef.current = explicitReturnTarget?.isConnected ? explicitReturnTarget : activeElement;
    const stackEntry: ModalStackEntry = { dialogRef, returnTarget: returnRef.current, token: Symbol("modal") };
    lockModalBackground();
    modalStack.push(stackEntry);
    refreshModalStack();

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
      if (modalStack.at(-1)?.token !== stackEntry.token) {
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        closeRef.current();
        return;
      }
      if (dialogRef.current) {
        containFocus(event, dialogRef.current);
      }
    };
    const preserveReturnTarget = (event: Event) => {
      if (returnRef.current?.isConnected) {
        modalReturnTargets.set(event, returnRef.current);
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    window.addEventListener("openknowledge-step-up-required", preserveReturnTarget, { capture: true });
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("openknowledge-step-up-required", preserveReturnTarget, { capture: true });
      const stackIndex = modalStack.findIndex((entry) => entry.token === stackEntry.token);
      if (stackIndex >= 0) {
        modalStack.splice(stackIndex, 1);
      }
      refreshModalStack();
      if (returnRef.current) {
        const target = returnRef.current;
        queueMicrotask(() => focusModalReturnTarget(target));
      }
      returnRef.current = null;
      if (modalStack.length === 0) {
        unlockModalBackground();
      }
    };
  }, [explicitReturnTarget, open]);

  return dialogRef;
}
