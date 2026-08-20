import Link from "next/link";
import {
  Activity,
  ArrowRight,
  BookOpen,
  Boxes,
  ChevronDown,
  CircleUserRound,
  Clock3,
  Command,
  FileStack,
  FolderKanban,
  Gauge,
  Grid2X2,
  KeyRound,
  Layers3,
  Plus,
  Search,
  Settings2,
  Sparkles,
  Upload,
  UsersRound,
  WandSparkles,
} from "lucide-react";

const navigation = [
  { label: "For you", icon: Grid2X2, active: true, href: "/" },
  { label: "Explore", icon: Search, href: "/explore" },
  { label: "Spaces", icon: FolderKanban, href: "/spaces" },
  { label: "Knowledge", icon: BookOpen, href: "/knowledge" },
  { label: "Sources", icon: FileStack, href: "/sources" },
  { label: "Ingestion", icon: Layers3, href: "/ingestion" },
  { label: "AI actions", icon: WandSparkles, href: "/ai-actions" },
];

const administration = [
  { label: "People & access", icon: UsersRound, href: "/people" },
  { label: "Activity", icon: Activity, href: "/activity" },
  { label: "Settings", icon: Settings2, href: "/settings" },
];

export type DashboardSpace = {
  createdAt: string;
  id: string;
  name: string;
  role: string;
};

type DashboardShellProps = {
  member: { displayName: string; role: string };
  ready: boolean;
  spaces: DashboardSpace[];
};

function NavigationGroup({ label, items }: { label: string; items: typeof navigation }) {
  return (
    <div className="navigation-group">
      <p className="navigation-label">{label}</p>
      <div className="navigation-items">
        {items.map((item) => {
          const Icon = item.icon;
          return (
            <Link className={`navigation-item${item.active ? " is-active" : ""}`} href={item.href} key={item.label}>
              <Icon aria-hidden="true" size={18} strokeWidth={1.8} />
              <span>{item.label}</span>
            </Link>
          );
        })}
      </div>
    </div>
  );
}

export function DashboardShell({ member, ready, spaces }: DashboardShellProps) {
  const initials = member.displayName.split(/\s+/).map((value) => value[0]).join("").slice(0, 2).toUpperCase();
  return (
    <div className="app-frame">
      <aside className="sidebar">
        <div className="brand-lockup">
          <span className="brand-mark"><Boxes aria-hidden="true" size={18} /></span>
          <span>Kinbase</span>
          <span className="brand-edition">HOME</span>
        </div>

        <div className="family-switcher console-family-card">
          <span className="family-avatar">K</span>
          <span className="family-copy">
            <strong>Family knowledge</strong>
            <small>{spaces.length} accessible {spaces.length === 1 ? "space" : "spaces"}</small>
          </span>
        </div>

        <nav aria-label="Primary navigation" className="primary-navigation">
          <NavigationGroup items={navigation} label="Workspace" />
          <NavigationGroup items={administration} label="Manage" />
        </nav>

        <div className="sidebar-footer">
          <div className="storage-meter">
            <div className="storage-heading"><span>Access</span><span>Scoped</span></div>
            <div className="storage-track"><span className="access-track" /></div>
            <p>{spaces.length} spaces available to this account</p>
          </div>
          <Link className="profile-card" href="/settings">
            <span className="profile-avatar">{initials || "M"}</span>
            <span><strong>{member.displayName}</strong><small>{member.role}</small></span>
            <ChevronDown aria-hidden="true" size={15} />
          </Link>
        </div>
      </aside>

      <main className="main-canvas">
        <header className="topbar">
          <div className="scope-indicator"><span className="scope-dot" />Searching all accessible spaces</div>
          <div className="topbar-actions">
            <Link className="command-button" href="/explore"><Command aria-hidden="true" size={15} /> Command <kbd>⌘ K</kbd></Link>
            <Link aria-label="Open profile" className="icon-button" href="/settings"><CircleUserRound aria-hidden="true" size={20} /></Link>
          </div>
        </header>

        <div className="content-wrap">
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
              <Link href="/sources"><span className="action-icon violet"><Upload aria-hidden="true" size={17} /></span>Upload source</Link>
              <Link href="/knowledge"><span className="action-icon coral"><Plus aria-hidden="true" size={17} /></span>Capture knowledge</Link>
              <Link href="/spaces"><span className="action-icon cyan"><FolderKanban aria-hidden="true" size={17} /></span>Create space</Link>
              <Link href="/explore"><span className="action-icon mint"><Gauge aria-hidden="true" size={17} /></span>Test retrieval</Link>
            </div>
          </section>

          <section className="dashboard-grid">
            <article className="continue-panel">
              <div className="section-heading">
                <div><p className="section-kicker">PICK UP THE THREAD</p><h2>Continue where you left off</h2></div>
                <Link href="/spaces">View all <ArrowRight aria-hidden="true" size={15} /></Link>
              </div>
              <div className="recent-list">
                {spaces.slice(0, 3).map((space, index) => (
                  <Link className="recent-row" href={`/spaces/${encodeURIComponent(space.id)}`} key={space.id}>
                    <span className={`document-glyph ${["violet", "cyan", "mint"][index]}`}><BookOpen aria-hidden="true" size={18} /></span>
                    <span className="recent-copy"><strong>{space.name}</strong><small>{space.role} · available now</small></span>
                    <ArrowRight aria-hidden="true" className="row-arrow" size={17} />
                  </Link>
                ))}
                {spaces.length === 0 ? <div className="dashboard-empty"><FolderKanban aria-hidden="true" size={19} /><span><strong>No spaces yet</strong><small>Create the first private working space.</small></span></div> : null}
              </div>
            </article>

            <article className="pulse-panel">
              <div className="pulse-topline"><span><span className={`live-dot${ready ? "" : " is-down"}`} /> System pulse</span><span className={`pulse-status${ready ? "" : " is-down"}`}>{ready ? "Ready" : "Needs attention"}</span></div>
              <div className="pulse-score"><strong>{ready ? "100" : "0"}</strong><span>%</span></div>
              <p>Gateway readiness reported by the live production boundary</p>
              <div className="pulse-bars" aria-hidden="true">
                {[62, 78, 70, 91, 83, 94, 87, 96, 92, 98].map((height) => <span className={`pulse-bar-${height}`} key={height} />)}
              </div>
              <div className="pulse-metrics">
                <div><small>Spaces</small><strong>{spaces.length}</strong></div>
                <div><small>Access</small><strong>Scoped</strong></div>
                <div><small>Gateway</small><strong>{ready ? "Ready" : "Check"}</strong></div>
              </div>
            </article>
          </section>

          <section className="attention-strip">
            <div className="attention-icon"><Clock3 aria-hidden="true" size={19} /></div>
            <div><strong>{ready ? "Permission-aware search is active" : "The gateway is not ready"}</strong><p>{ready ? "Every search is limited to spaces this account can access." : "Check database readiness and schema compatibility before continuing."}</p></div>
            <Link href={ready ? "/spaces" : "/activity"}>{ready ? "Review access" : "View activity"}</Link>
          </section>
        </div>
      </main>
    </div>
  );
}
