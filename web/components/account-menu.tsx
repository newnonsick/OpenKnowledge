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
  const containerRef = useRef<HTMLDivElement>(null);
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

  const close = () => setOpen(false);

  return (
    <div className="account-menu" ref={containerRef}>
      {open ? (
        <div aria-label="Account" className="account-menu-popover" id={menuId} role="menu">
          <div className="account-menu-identity">
            <strong>{member.displayName}</strong>
            <span>{member.role}</span>
          </div>
          <Link className="account-menu-item" href="/settings" onClick={close}><Settings2 aria-hidden="true" size={16} /><span>Profile and settings</span></Link>
          <Link className="account-menu-item" href="/settings?section=api-keys" onClick={close}><KeyRound aria-hidden="true" size={16} /><span>API keys</span></Link>
          <ThemeToggle />
          <div className="account-menu-separator" />
          <button aria-label="Sign out" className="account-menu-item is-danger" disabled={signingOut} onClick={() => { close(); onSignOut(); }} type="button"><LogOut aria-hidden="true" size={16} /><span>{signingOut ? "Signing out…" : "Sign out"}</span></button>
        </div>
      ) : null}
      <button aria-controls={menuId} aria-expanded={open} aria-haspopup="menu" aria-label="Open account menu" className="profile-card account-menu-trigger" onClick={() => setOpen((current) => !current)} ref={triggerRef} type="button">
        <span className="profile-avatar">{initials || "M"}</span>
        <span><strong>{member.displayName}</strong><small>{member.role}</small></span>
        <ChevronUp aria-hidden="true" className={open ? "is-open" : ""} size={16} />
      </button>
    </div>
  );
}
