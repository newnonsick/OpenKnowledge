"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import {
  Activity,
  ArrowRight,
  BookOpen,
  Boxes,
  CircleAlert,
  CircleUserRound,
  Clock3,
  Command,
  FileStack,
  FolderKanban,
  Grid2X2,
  Layers3,
  Menu,
  Plus,
  Search,
  Settings2,
  ShieldCheck,
  Sparkles,
  Upload,
  UsersRound,
  WandSparkles,
} from "lucide-react";

import { AppSidebar, SidebarMember, SidebarNavigation } from "@/components/app-sidebar";
import { useDrawerFocus } from "@/lib/focus-management";

const navigation: SidebarNavigation[] = [
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

const administration: SidebarNavigation[] = [
  {
    items: [
      { label: "People & access", icon: UsersRound, href: "/people" },
      { label: "Activity", icon: Activity, href: "/activity" },
      { label: "Settings", icon: Settings2, href: "/settings" },
    ],
    label: "Manage",
  },
];

export type DashboardSpace = {
  createdAt: string;
  id: string;
  name: string;
  role: string;
};

export type DashboardOperations = {
  ingestion: {
    cancelled: number;
    cancellation_requested: number;
    failed: number;
    queued: number;
    retry_wait: number;
    running: number;
    succeeded: number;
  };
  observed_at: string;
  retrieval: { embedding_generation_active: boolean };
  scope: "accessible_spaces";
  settings_revision: number;
  spaces: number;
  storage: { referenced_bytes: number };
};

type DashboardShellProps = {
  loading?: boolean;
  member: SidebarMember;
  operations: DashboardOperations | null;
  ready: boolean;
  spaceCount?: number | null;
  spaceCountFailed?: boolean;
  spaces: DashboardSpace[];
  spacesLoaded?: boolean;
};

function formatClock(value: string): string {
  const observed = new Date(value);
  return `${String(observed.getHours()).padStart(2, "0")}:${String(observed.getMinutes()).padStart(2, "0")}`;
}

function formatBytes(value: number): string {
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  if (value < 1024 * 1024 * 1024) {
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  }
  return `${(value / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

export function DashboardShell({
  loading = false,
  member,
  operations,
  ready,
  spaces,
  spaceCount = spaces.length,
  spaceCountFailed = false,
  spacesLoaded = true,
}: DashboardShellProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [shortcutHint, setShortcutHint] = useState("⌘ K");
  const commandLinkRef = useRef<HTMLAnchorElement>(null);
  const { closeRef, drawerRef, triggerRef } = useDrawerFocus(menuOpen, setMenuOpen);
  const visibleAdministration = member.systemRole === "super_admin"
    ? administration
    : administration.map((group) => ({ ...group, items: group.items.filter((item) => item.href === "/settings") })).filter((group) => group.items.length > 0);
  const queued = operations ? operations.ingestion.queued + operations.ingestion.retry_wait : 0;
  const pulseUnavailable = !loading && !ready;

  useEffect(() => {
    const platform = typeof navigator === "undefined" ? "" : navigator.platform.toLowerCase();
    setShortcutHint(platform.includes("mac") ? "⌘ K" : "Ctrl K");
  }, []);

  useEffect(() => {
    const handleShortcut = (event: KeyboardEvent) => {
      const target = event.target;
      if (
        event.defaultPrevented
        || event.altKey
        || !(event.metaKey || event.ctrlKey)
        || event.key.toLowerCase() !== "k"
        || (target instanceof HTMLElement && (
          target.isContentEditable
          || target.tagName === "INPUT"
          || target.tagName === "TEXTAREA"
          || target.tagName === "SELECT"
        ))
      ) {
        return;
      }
      event.preventDefault();
      commandLinkRef.current?.click();
    };
    window.addEventListener("keydown", handleShortcut);
    return () => window.removeEventListener("keydown", handleShortcut);
  }, []);

  return (
    <div className={`app-frame${menuOpen ? " menu-open" : ""}`}>
      <AppSidebar
        closeRef={closeRef}
        drawerId="dashboard-navigation"
        drawerRef={drawerRef}
        manageGroups={visibleAdministration}
        member={member}
        menuOpen={menuOpen}
        onMenuClose={() => setMenuOpen(false)}
        spaceCount={loading ? null : spaceCount}
        spaceCountFailed={spaceCountFailed}
        workspaceGroups={navigation}
      />

      <main aria-busy={loading} className="main-canvas">
        <header className="console-mobile-bar">
          <button aria-controls="dashboard-navigation" aria-expanded={menuOpen} aria-label="Open navigation" className="mobile-menu-button" onClick={() => setMenuOpen(true)} ref={triggerRef} type="button"><Menu aria-hidden="true" size={19} /></button>
          <span><Boxes aria-hidden="true" size={17} /> OpenKnowledge</span>
          <div className="mobile-top-actions">
            <Link aria-label="Search knowledge" className="icon-button" href="/explore"><Search aria-hidden="true" size={19} /></Link>
            <Link aria-label="Open profile" className="icon-button" href="/settings"><CircleUserRound aria-hidden="true" size={20} /></Link>
          </div>
        </header>
        <header className="topbar">
          <div className="scope-indicator"><span className="scope-dot" />Searching all accessible spaces</div>
          <div className="topbar-actions">
            <Link className="command-button" href="/explore" ref={commandLinkRef}><Command aria-hidden="true" size={15} /> Search <kbd>{shortcutHint}</kbd></Link>
            <Link aria-label="Open profile" className="icon-button" href="/settings"><CircleUserRound aria-hidden="true" size={20} /></Link>
          </div>
        </header>

        <div className="content-wrap">
          {loading ? <p aria-label="Loading dashboard" aria-live="polite" className="visually-hidden" role="status">Loading dashboard</p> : null}
          <section className="hero-section">
            <div className="eyebrow"><Sparkles aria-hidden="true" size={15} /> Your family knowledge, in one place</div>
            <h1>Everything your family knows.<br /><span>Ready when you need it.</span></h1>
            <p className="hero-description">Find a detail, return to a project, or add something worth remembering.</p>

            <form action="/explore" className="knowledge-search" method="get" role="search">
              <Search aria-hidden="true" size={23} strokeWidth={1.8} />
              <input aria-label="Search family knowledge" name="q" placeholder="Search across every space…" type="search" />
              <button type="submit">Search <ArrowRight aria-hidden="true" size={16} /></button>
            </form>

            <div className="quick-actions" aria-label="Quick actions">
              <Link href="/sources"><span className="action-icon"><Upload aria-hidden="true" size={17} /></span>Upload source</Link>
              <Link href="/knowledge"><span className="action-icon"><Plus aria-hidden="true" size={17} /></span>Capture knowledge</Link>
              <Link href="/spaces"><span className="action-icon"><FolderKanban aria-hidden="true" size={17} /></span>Create space</Link>
            </div>
          </section>

          <section className="dashboard-grid">
            <article className="continue-panel">
              <div className="section-heading">
                <div><p className="section-kicker">PICK UP THE THREAD</p><h2>Continue where you left off</h2></div>
                <Link href="/spaces">View all <ArrowRight aria-hidden="true" size={15} /></Link>
              </div>
              <div className="recent-list">
                {loading ? <div aria-hidden="true" className="dashboard-list-skeleton">
                  {Array.from({ length: 3 }).map((_, index) => <span className="dashboard-skeleton-row" key={index}><i /><b /><em /></span>)}
                </div> : spaces.slice(0, 3).map((space) => (
                  <Link className="recent-row" href={`/knowledge?space=${encodeURIComponent(space.id)}`} key={space.id}>
                    <span className="document-glyph"><BookOpen aria-hidden="true" size={18} /></span>
                    <span className="recent-copy"><strong>{space.name}</strong><small>{space.role} · available now</small></span>
                    <ArrowRight aria-hidden="true" className="row-arrow" size={17} />
                  </Link>
                ))}
                {!loading && spacesLoaded && spaces.length === 0 ? <div className="dashboard-empty"><FolderKanban aria-hidden="true" size={19} /><span><strong>No spaces yet</strong><small>Create the first private working space.</small></span></div> : null}
                {!loading && !spacesLoaded ? <div className="dashboard-empty"><FolderKanban aria-hidden="true" size={19} /><span><strong>Spaces are unavailable</strong><small>Try refreshing this page before managing access.</small></span></div> : null}
              </div>
            </article>

            <article className="pulse-panel">
              <div className="pulse-topline"><span><span className={`live-dot${pulseUnavailable ? " is-down" : ""}`} /> System pulse</span><span className={`pulse-status${pulseUnavailable ? " is-down" : ""}`}>{loading ? "Checking" : ready ? "Ready" : "Needs attention"}</span></div>
              {loading ? <div aria-hidden="true" className="dashboard-pulse-skeleton"><i /><b /><span><em /><em /><em /><em /><em /><em /></span></div> : <>
                <div className="pulse-summary"><strong>{operations ? operations.ingestion.running : "—"}</strong><span>jobs processing now</span></div>
                <p>{operations ? `Observed across ${operations.spaces} accessible ${operations.spaces === 1 ? "space" : "spaces"}` : "Operational snapshot is temporarily unavailable"}</p>
                <div className="pulse-operational-grid">
                  <div><small>Queue</small><strong>{operations ? `${queued} queued` : "Unavailable"}</strong></div>
                  <div><small>Processing</small><strong>{operations ? `${operations.ingestion.running} running` : "Unavailable"}</strong></div>
                  <div><small>Storage</small><strong>{operations ? `${formatBytes(operations.storage.referenced_bytes)} referenced` : "Unavailable"}</strong></div>
                  <div><small>Retrieval</small><strong>{operations ? (operations.retrieval.embedding_generation_active ? "Generation active" : "Lexical only") : "Unavailable"}</strong></div>
                  <div><small>Settings</small><strong>{operations ? `Revision ${operations.settings_revision}` : "Unavailable"}</strong></div>
                  <div><small>Failures</small><strong>{operations ? `${operations.ingestion.failed} terminal` : "Unavailable"}</strong></div>
                </div>
                {operations ? <time className="pulse-observed" dateTime={operations.observed_at}>Snapshot {formatClock(operations.observed_at)}</time> : null}
              </>}
            </article>
          </section>

          <section className={`attention-strip${loading || ready ? "" : " is-warning"}`}>
            <div className="attention-icon">{loading ? <Clock3 aria-hidden="true" size={19} /> : ready ? <ShieldCheck aria-hidden="true" size={19} /> : <CircleAlert aria-hidden="true" size={19} />}</div>
            <div><strong>{loading ? "Checking system status" : ready ? "Permission-aware search is active" : "The gateway is not ready"}</strong><p>{loading ? "Loading access and operational status." : ready ? "Every search is limited to spaces this account can access." : "Check database readiness and schema compatibility before continuing."}</p></div>
            {loading ? <span className="attention-loading">Checking…</span> : <Link href={ready ? "/spaces" : "/activity"}>{ready ? "Manage spaces" : "View activity"}</Link>}
          </section>
        </div>
      </main>
      {menuOpen ? <button aria-label="Close navigation overlay" className="navigation-overlay" onClick={() => setMenuOpen(false)} type="button" /> : null}
    </div>
  );
}
