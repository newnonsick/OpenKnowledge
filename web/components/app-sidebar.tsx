"use client";

import { ReactNode, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Boxes, KeyRound, LogOut, X } from "lucide-react";
import type { RefObject } from "react";

import { contractClient, contractData } from "@/lib/api-client";
import { resetCachedMember } from "@/components/auth/session-gate";
import { ThemeToggle } from "@/components/theme-toggle";

export type SidebarMember = {
  displayName: string;
  role: string;
  systemRole: "member" | "super_admin";
};

export type SidebarNavigation = {
  items: Array<{ label: string; icon: typeof Boxes; href: string }>;
  label: string;
};

export function AppSidebar({
  closeRef,
  drawerId,
  drawerRef,
  manageGroups,
  menuOpen,
  onMenuClose,
  spaceCount,
  workspaceGroups,
  member,
}: {
  closeRef: RefObject<HTMLButtonElement | null>;
  drawerId: string;
  drawerRef: RefObject<HTMLElement | null>;
  manageGroups: SidebarNavigation[];
  menuOpen: boolean;
  onMenuClose: () => void;
  spaceCount: number | null;
  workspaceGroups: SidebarNavigation[];
  member: SidebarMember;
}) {
  const pathname = usePathname();
  const router = useRouter();
  const [signingOut, setSigningOut] = useState(false);
  const initials = member.displayName.split(/\s+/).map((value) => value[0]).join("").slice(0, 2).toUpperCase();

  const signOut = async () => {
    if (signingOut) {
      return;
    }
    setSigningOut(true);
    try {
      await contractData(contractClient.POST("/api/v1/auth/logout", { body: {} }));
    } finally {
      resetCachedMember();
      router.replace("/login");
    }
  };

  const renderGroup = (group: SidebarNavigation) => (
    <div className="navigation-group" key={group.label}>
      <p className="navigation-label">{group.label}</p>
      <div className="navigation-items">
        {group.items.map((item) => {
          const Icon = item.icon;
          const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
          return (
            <Link aria-current={active ? "page" : undefined} className={`navigation-item${active ? " is-active" : ""}`} href={item.href} key={item.href}>
              <Icon aria-hidden="true" size={18} strokeWidth={1.8} />
              <span>{item.label}</span>
            </Link>
          );
        })}
      </div>
    </div>
  );

  const footer: ReactNode = (
    <>
      <Link className="profile-card static-profile" href="/settings">
        <span className="profile-avatar">{initials || "M"}</span>
        <span><strong>{member.displayName}</strong><small>{member.role}</small></span>
      </Link>
      <Link className="account-link" href="/settings?section=api-keys"><KeyRound aria-hidden="true" size={15} /><span>API keys</span></Link>
      <ThemeToggle />
      <button aria-label="Sign out" className="account-link" disabled={signingOut} onClick={() => void signOut()} type="button"><LogOut aria-hidden="true" size={15} /><span>{signingOut ? "Signing out…" : "Sign out"}</span></button>
    </>
  );

  return (
    <aside className={`sidebar console-sidebar${menuOpen ? " is-open" : ""}`} id={drawerId} ref={drawerRef}>
      <div className="brand-lockup">
        <span className="brand-mark"><Boxes aria-hidden="true" size={17} /></span>
        <span>Kinbase</span>
        <button aria-label="Close navigation" className="mobile-menu-button close" onClick={onMenuClose} ref={closeRef} type="button"><X aria-hidden="true" size={18} /></button>
      </div>

      <div className="family-switcher console-family-card">
        <span className="family-avatar">K</span>
        <span className="family-copy">
          <strong>Family knowledge</strong>
          <small>{spaceCount === null ? "Loading spaces…" : `${spaceCount} accessible ${spaceCount === 1 ? "space" : "spaces"}`}</small>
        </span>
      </div>

      <nav aria-label="Primary navigation" className="primary-navigation">
        {workspaceGroups.map(renderGroup)}
        {manageGroups.map(renderGroup)}
      </nav>

      <div className="sidebar-footer console-account">{footer}</div>
    </aside>
  );
}
