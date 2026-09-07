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
      : null;
  if (!connectedTarget || connectedTarget.closest("[inert], [data-modal-suspended]")) {
    return false;
  }
  connectedTarget.focus();
  return document.activeElement === connectedTarget;
}

function focusableElements(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(focusableSelector)).filter((element) => {
    if (element.hidden) {
      return false;
    }
    if (element instanceof HTMLDetailsElement) {
      return element.open;
    }
    const style = typeof element.checkVisibility === "function" ? null : getComputedStyle(element);
    if (typeof element.checkVisibility === "function") {
      try {
        if (!element.checkVisibility({ checkOpacity: false, checkVisibilityCSS: true })) {
          return false;
        }
      } catch {
        return false;
      }
    } else if (style && (style.display === "none" || style.visibility === "hidden")) {
      return false;
    }
    if (element.closest("[inert], details:not([open])")) {
      return false;
    }
    return true;
  });
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
        const trigger = triggerRef.current;
        queueMicrotask(() => {
          if (trigger?.isConnected && trigger.offsetParent !== null) {
            trigger.focus();
          } else {
            document.querySelector<HTMLElement>("main h1")?.setAttribute("tabindex", "-1");
            const heading = document.querySelector<HTMLElement>("main h1");
            heading?.focus({ preventScroll: true });
          }
        });
      }
      wasOpen.current = false;
      return;
    }
    wasOpen.current = true;
    const overflowBefore = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const closeButton = closeRef.current || drawerRef.current?.querySelector<HTMLButtonElement>("button[aria-label='Close navigation']");
    let frame = 0;
    let attempts = 0;
    const focusClose = () => {
      const target = closeButton?.isConnected
        ? closeButton
        : drawerRef.current?.querySelector<HTMLButtonElement>("button[aria-label='Close navigation']");
      target?.focus({ preventScroll: true });
      attempts += 1;
      const drawer = drawerRef.current;
      if (attempts < 12 && drawer && !drawer.contains(document.activeElement)) {
        frame = requestAnimationFrame(focusClose);
      }
    };
    focusClose();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        setOpen(false);
        return;
      }
      if (drawerRef.current) {
        containFocus(event, drawerRef.current);
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      cancelAnimationFrame(frame);
      document.body.style.overflow = overflowBefore;
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [open, setOpen]);

  return { closeRef, drawerRef, triggerRef };
}

export function useModalFocus(open: boolean, onClose: () => void, explicitReturnTarget?: HTMLElement | null, initialFocusSelector?: string) {
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
      const preferred = initialFocusSelector ? dialog.querySelector<HTMLElement>(initialFocusSelector) : null;
      if (preferred && !preferred.hasAttribute("disabled")) {
        preferred.focus();
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
        event.stopPropagation();
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
