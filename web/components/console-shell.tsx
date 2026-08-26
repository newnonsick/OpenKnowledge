"use client";

import { ReactNode, useRef, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  Activity,
  BookOpen,
  Boxes,
  FileStack,
  FolderKanban,
  Grid2X2,
  KeyRound,
  Layers3,
  LogOut,
  Menu,
  Search,
  Settings2,
  UsersRound,
  WandSparkles,
  X,
} from "lucide-react";

import { contractClient, contractData } from "@/lib/api-client";
import { resetCachedMember } from "@/components/auth/session-gate";
import { ThemeToggle } from "@/components/theme-toggle";
import { useAlertDialogFocus, useDrawerFocus } from "@/lib/focus-management";

const workspaceNavigation = [
  { label: "For you", icon: Grid2X2, href: "/" },
  { label: "Explore", icon: Search, href: "/explore" },
  { label: "Spaces", icon: FolderKanban, href: "/spaces" },
  { label: "Knowledge", icon: BookOpen, href: "/knowledge" },
  { label: "Sources", icon: FileStack, href: "/sources" },
  { label: "Ingestion", icon: Layers3, href: "/ingestion" },
  { label: "AI actions", icon: WandSparkles, href: "/ai-actions" },
];

const manageNavigation = [
  { label: "People & access", icon: UsersRound, href: "/people" },
  { label: "Activity", icon: Activity, href: "/activity" },
  { label: "Settings", icon: Settings2, href: "/settings" },
];

type ConsoleShellProps = {
  actions?: ReactNode;
  children: ReactNode;
  description: string;
  eyebrow: string;
  member: { displayName: string; role: string; systemRole: "member" | "super_admin" };
  spaceCount: number | null;
  title: string;
};

function NavigationGroup({ label, pathname, items }: {
  items: typeof workspaceNavigation;
  label: string;
  pathname: string;
}) {
  return (
    <div className="navigation-group">
      <p className="navigation-label">{label}</p>
      <div className="navigation-items">
        {items.map((item) => {
          const Icon = item.icon;
          const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
          return (
            <Link className={`navigation-item${active ? " is-active" : ""}`} href={item.href} key={item.href}>
              <Icon aria-hidden="true" size={18} strokeWidth={1.8} />
              <span>{item.label}</span>
            </Link>
          );
        })}
      </div>
    </div>
  );
}

export function ConsoleShell({ actions, children, description, eyebrow, member, spaceCount, title }: ConsoleShellProps) {
  const pathname = usePathname();
  const router = useRouter();
  const [menuOpen, setMenuOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const contentRef = useRef<HTMLDivElement>(null);
  const { closeRef, drawerRef, triggerRef } = useDrawerFocus(menuOpen, setMenuOpen);
  useAlertDialogFocus(contentRef);
  const visibleManageNavigation = member.systemRole === "super_admin"
    ? manageNavigation
    : manageNavigation.filter((item) => item.href === "/settings");
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

  return (
    <div className={`app-frame console-frame${menuOpen ? " menu-open" : ""}`}>
      <aside className="sidebar console-sidebar" id="console-navigation" ref={drawerRef}>
        <div className="brand-lockup">
          <span className="brand-mark"><Boxes aria-hidden="true" size={17} /></span>
          <span>Kinbase</span>
          <button aria-label="Close navigation" className="mobile-menu-button close" onClick={() => setMenuOpen(false)} ref={closeRef} type="button"><X aria-hidden="true" size={18} /></button>
        </div>

        <div className="family-switcher console-family-card">
          <span className="family-avatar">K</span>
          <span className="family-copy">
            <strong>Family knowledge</strong>
            <small>{spaceCount === null ? "Loading spaces…" : `${spaceCount} accessible ${spaceCount === 1 ? "space" : "spaces"}`}</small>
          </span>
        </div>

        <nav aria-label="Primary navigation" className="primary-navigation">
          <NavigationGroup items={workspaceNavigation} label="Workspace" pathname={pathname} />
          <NavigationGroup items={visibleManageNavigation} label="Manage" pathname={pathname} />
        </nav>

        <div className="sidebar-footer console-account">
          <div className="profile-card static-profile">
            <span className="profile-avatar">{initials || "M"}</span>
            <span><strong>{member.displayName}</strong><small>{member.role}</small></span>
          </div>
          <Link className="account-link" href="/settings"><KeyRound aria-hidden="true" size={15} /><span>API keys</span></Link>
          <ThemeToggle />
          <button aria-label="Sign out" className="account-link" disabled={signingOut} onClick={signOut} type="button"><LogOut aria-hidden="true" size={15} /><span>{signingOut ? "Signing out…" : "Sign out"}</span></button>
        </div>
      </aside>

      <main className="main-canvas console-main">
        <header className="console-mobile-bar">
          <button aria-controls="console-navigation" aria-expanded={menuOpen} aria-label="Open navigation" className="mobile-menu-button" onClick={() => setMenuOpen(true)} ref={triggerRef} type="button"><Menu aria-hidden="true" size={19} /></button>
          <span><Boxes aria-hidden="true" size={17} /> Kinbase</span>
        </header>
        <div className="console-content" ref={contentRef}>
          <header className="console-page-header">
            <div>
              <p className="console-eyebrow">{eyebrow}</p>
              <h1>{title}</h1>
              <p>{description}</p>
            </div>
            {actions ? <div className="console-page-actions">{actions}</div> : null}
          </header>
          {children}
        </div>
      </main>
      <button aria-label="Close navigation overlay" className="navigation-overlay" onClick={() => setMenuOpen(false)} type="button" />
    </div>
  );
}
