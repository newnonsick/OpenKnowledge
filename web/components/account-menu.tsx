"use client";

import Link from "next/link";
import { ChevronUp, KeyRound, LogOut, Settings2 } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import { ThemeToggle } from "@/components/theme-toggle";

type AccountMenuProps = {
  member: {
    displayName: string;
    role: string;
  };
  onSignOut: () => void;
  signingOut: boolean;
};

export function AccountMenu({ member, onSignOut, signingOut }: AccountMenuProps) {
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const containerRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuId = useId();
  const initials = member.displayName.split(/\s+/).map((value) => value[0]).join("").slice(0, 2).toUpperCase();

  useEffect(() => {
    if (!open) {
      return;
    }
    const dismissOutside = (event: PointerEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const dismissWithKeyboard = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener("pointerdown", dismissOutside);
    document.addEventListener("keydown", dismissWithKeyboard);
    return () => {
      document.removeEventListener("pointerdown", dismissOutside);
      document.removeEventListener("keydown", dismissWithKeyboard);
    };
  }, [open]);

  useEffect(() => {
    if (!open) {
      return;
    }
    queueMicrotask(() => {
      const items = menuRef.current?.querySelectorAll<HTMLElement>("[role='menuitem']:not([disabled])");
      items?.[Math.min(activeIndex, (items.length || 1) - 1)]?.focus();
    });
  }, [activeIndex, open]);

  const close = () => setOpen(false);
  const openAt = (index: number) => {
    setActiveIndex(index);
    setOpen(true);
  };
  const closeAndRefocus = () => {
    setOpen(false);
    queueMicrotask(() => triggerRef.current?.focus());
  };
  const handleMenuKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Tab") {
      event.preventDefault();
      closeAndRefocus();
      return;
    }
    const itemCount = menuRef.current?.querySelectorAll("[role='menuitem']").length ?? 0;
    if (!itemCount || !["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      return;
    }
    event.preventDefault();
    if (event.key === "Home") {
      setActiveIndex(0);
    } else if (event.key === "End") {
      setActiveIndex(itemCount - 1);
    } else {
      const direction = event.key === "ArrowDown" ? 1 : -1;
      setActiveIndex((current) => (current + direction + itemCount) % itemCount);
    }
  };

  return (
    <div className="account-menu" ref={containerRef}>
      {open ? (
        <div aria-label="Account" className="account-menu-popover" id={menuId} onKeyDown={handleMenuKeyDown} ref={menuRef} role="menu">
          <div aria-hidden="true" className="account-menu-identity" role="presentation">
            <strong>{member.displayName}</strong>
            <span>{member.role}</span>
          </div>
          <Link className="account-menu-item" href="/settings?section=security" onClick={close} onFocus={() => setActiveIndex(0)} role="menuitem" tabIndex={activeIndex === 0 ? 0 : -1}><Settings2 aria-hidden="true" size={16} /><span>Security settings</span></Link>
          <Link className="account-menu-item" href="/settings?section=api-keys" onClick={close} onFocus={() => setActiveIndex(1)} role="menuitem" tabIndex={activeIndex === 1 ? 0 : -1}><KeyRound aria-hidden="true" size={16} /><span>API keys</span></Link>
          <ThemeToggle menuItem onFocus={() => setActiveIndex(2)} tabIndex={activeIndex === 2 ? 0 : -1} />
          <div className="account-menu-separator" role="separator" />
          <button aria-label="Sign out" className="account-menu-item is-danger" disabled={signingOut} onClick={() => { close(); onSignOut(); }} onFocus={() => setActiveIndex(3)} role="menuitem" tabIndex={activeIndex === 3 ? 0 : -1} type="button"><LogOut aria-hidden="true" size={16} /><span>{signingOut ? "Signing out…" : "Sign out"}</span></button>
        </div>
      ) : null}
      <button aria-controls={menuId} aria-expanded={open} aria-haspopup="menu" aria-label={open ? "Close account menu" : "Open account menu"} className="profile-card account-menu-trigger" onClick={() => { if (open) closeAndRefocus(); else openAt(0); }} onKeyDown={(event) => { if (event.key === "ArrowDown" || event.key === "ArrowUp") { event.preventDefault(); openAt(event.key === "ArrowDown" ? 0 : 3); } }} ref={triggerRef} type="button">
        <span className="profile-avatar">{initials || "M"}</span>
        <span><strong>{member.displayName}</strong><small>{member.role}</small></span>
        <ChevronUp aria-hidden="true" className={open ? "is-open" : ""} size={16} />
      </button>
    </div>
  );
}
