"use client";

import { ReactNode, useRef, useState } from "react";
import {
  Activity,
  BookOpen,
  Boxes,
  FileStack,
  FolderKanban,
  Grid2X2,
  Layers3,
  Menu,
  Search,
  Settings2,
  UsersRound,
  WandSparkles,
} from "lucide-react";

import { AppSidebar, SidebarMember, SidebarNavigation } from "@/components/app-sidebar";
import { useAlertDialogFocus, useDrawerFocus } from "@/lib/focus-management";

const workspaceNavigation: SidebarNavigation[] = [
  {
    items: [
      { label: "For you", icon: Grid2X2, href: "/" },
      { label: "Explore", icon: Search, href: "/explore" },
      { label: "Spaces", icon: FolderKanban, href: "/spaces" },
      { label: "Knowledge", icon: BookOpen, href: "/knowledge" },
      { label: "Sources", icon: FileStack, href: "/sources" },
      { label: "Ingestion", icon: Layers3, href: "/ingestion" },
      { label: "AI actions", icon: WandSparkles, href: "/ai-actions" },
    ],
    label: "Workspace",
  },
];

const manageNavigation: SidebarNavigation[] = [
  {
    items: [
      { label: "People & access", icon: UsersRound, href: "/people" },
      { label: "Activity", icon: Activity, href: "/activity" },
      { label: "Settings", icon: Settings2, href: "/settings" },
    ],
    label: "Manage",
  },
];

type ConsoleShellProps = {
  actions?: ReactNode;
  children: ReactNode;
  description: string;
  eyebrow: string;
  member: SidebarMember;
  spaceCount: number | null;
  title: string;
};

export function ConsoleShell({ actions, children, description, eyebrow, member, spaceCount, title }: ConsoleShellProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const contentRef = useRef<HTMLDivElement>(null);
  const { closeRef, drawerRef, triggerRef } = useDrawerFocus(menuOpen, setMenuOpen);
  useAlertDialogFocus(contentRef);
  const visibleManageNavigation = member.systemRole === "super_admin"
    ? manageNavigation
    : manageNavigation.map((group) => ({ ...group, items: group.items.filter((item) => item.href === "/settings") })).filter((group) => group.items.length > 0);

  return (
    <div className={`app-frame console-frame${menuOpen ? " menu-open" : ""}`}>
      <AppSidebar
        closeRef={closeRef}
        drawerId="console-navigation"
        drawerRef={drawerRef}
        manageGroups={visibleManageNavigation}
        member={member}
        menuOpen={menuOpen}
        onMenuClose={() => setMenuOpen(false)}
        spaceCount={spaceCount}
        workspaceGroups={workspaceNavigation}
      />

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
