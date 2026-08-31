"use client";

import { useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Boxes, X } from "lucide-react";
import type { RefObject } from "react";

import { AccountMenu } from "@/components/account-menu";
import { contractClient, contractData } from "@/lib/api-client";
import { resetCachedMember } from "@/components/auth/session-gate";

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

      <div className="sidebar-footer console-account"><AccountMenu member={member} onSignOut={() => void signOut()} signingOut={signingOut} /></div>
    </aside>
  );
}
