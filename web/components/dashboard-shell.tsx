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
  { label: "For you", icon: Grid2X2, active: true },
  { label: "Explore", icon: Search },
  { label: "Spaces", icon: FolderKanban },
  { label: "Knowledge", icon: BookOpen },
  { label: "Sources", icon: FileStack },
  { label: "Ingestion", icon: Layers3, count: 2 },
  { label: "AI actions", icon: WandSparkles },
];

const administration = [
  { label: "People & access", icon: UsersRound },
  { label: "Activity", icon: Activity },
  { label: "Settings", icon: Settings2 },
];

const recentItems = [
  { title: "Family travel playbook", meta: "Family Shared · 12 min ago", color: "violet" },
  { title: "Home network inventory", meta: "Home Systems · Yesterday", color: "cyan" },
  { title: "Annual health checklist", meta: "Private · 3 days ago", color: "mint" },
];

function NavigationGroup({ label, items }: { label: string; items: typeof navigation }) {
  return (
    <div className="navigation-group">
      <p className="navigation-label">{label}</p>
      <div className="navigation-items">
        {items.map((item) => {
          const Icon = item.icon;
          return (
            <Link className={`navigation-item${item.active ? " is-active" : ""}`} href="#" key={item.label}>
              <Icon aria-hidden="true" size={18} strokeWidth={1.8} />
              <span>{item.label}</span>
              {item.count ? <span className="navigation-count">{item.count}</span> : null}
            </Link>
          );
        })}
      </div>
    </div>
  );
}

export function DashboardShell() {
  return (
    <div className="app-frame">
      <aside className="sidebar">
        <div className="brand-lockup">
          <span className="brand-mark"><Boxes aria-hidden="true" size={18} /></span>
          <span>Kinbase</span>
          <span className="brand-edition">HOME</span>
        </div>

        <button className="family-switcher" type="button">
          <span className="family-avatar">K</span>
          <span className="family-copy">
            <strong>Kittivath family</strong>
            <small>4 members · 6 spaces</small>
          </span>
          <ChevronDown aria-hidden="true" size={16} />
        </button>

        <nav aria-label="Primary navigation" className="primary-navigation">
          <NavigationGroup items={navigation} label="Workspace" />
          <NavigationGroup items={administration} label="Manage" />
        </nav>

        <div className="sidebar-footer">
          <div className="storage-meter">
            <div className="storage-heading"><span>Storage</span><span>38%</span></div>
            <div className="storage-track"><span /></div>
            <p>7.6 GB of 20 GB</p>
          </div>
          <button className="profile-card" type="button">
            <span className="profile-avatar">NT</span>
            <span><strong>Nok Thitivath</strong><small>Super admin</small></span>
            <ChevronDown aria-hidden="true" size={15} />
          </button>
        </div>
      </aside>

      <main className="main-canvas">
        <header className="topbar">
          <div className="scope-indicator"><span className="scope-dot" />Searching all accessible spaces</div>
          <div className="topbar-actions">
            <button className="command-button" type="button"><Command aria-hidden="true" size={15} /> Command <kbd>⌘ K</kbd></button>
            <button aria-label="Open profile" className="icon-button" type="button"><CircleUserRound aria-hidden="true" size={20} /></button>
          </div>
        </header>

        <div className="content-wrap">
          <section className="hero-section">
            <div className="eyebrow"><Sparkles aria-hidden="true" size={15} /> Your family knowledge, in one place</div>
            <h1>Everything your family knows.<br /><span>Ready when you need it.</span></h1>
            <p className="hero-description">Find a detail, return to a project, or add something worth remembering.</p>

            <form className="knowledge-search" role="search">
              <Search aria-hidden="true" size={23} strokeWidth={1.8} />
              <input aria-label="Search family knowledge" placeholder="Search across every space…" type="search" />
              <button type="submit">Search <ArrowRight aria-hidden="true" size={16} /></button>
            </form>

            <div className="quick-actions" aria-label="Quick actions">
              <button type="button"><span className="action-icon violet"><Upload aria-hidden="true" size={17} /></span>Upload source</button>
              <button type="button"><span className="action-icon coral"><Plus aria-hidden="true" size={17} /></span>Capture knowledge</button>
              <button type="button"><span className="action-icon cyan"><FolderKanban aria-hidden="true" size={17} /></span>Create space</button>
              <button type="button"><span className="action-icon mint"><Gauge aria-hidden="true" size={17} /></span>Test retrieval</button>
            </div>
          </section>

          <section className="dashboard-grid">
            <article className="continue-panel">
              <div className="section-heading">
                <div><p className="section-kicker">PICK UP THE THREAD</p><h2>Continue where you left off</h2></div>
                <Link href="#">View all <ArrowRight aria-hidden="true" size={15} /></Link>
              </div>
              <div className="recent-list">
                {recentItems.map((item) => (
                  <Link className="recent-row" href="#" key={item.title}>
                    <span className={`document-glyph ${item.color}`}><BookOpen aria-hidden="true" size={18} /></span>
                    <span className="recent-copy"><strong>{item.title}</strong><small>{item.meta}</small></span>
                    <ArrowRight aria-hidden="true" className="row-arrow" size={17} />
                  </Link>
                ))}
              </div>
            </article>

            <article className="pulse-panel">
              <div className="pulse-topline"><span><span className="live-dot" /> System pulse</span><span className="pulse-status">All systems normal</span></div>
              <div className="pulse-score"><strong>98.7</strong><span>%</span></div>
              <p>Search readiness across your active knowledge</p>
              <div className="pulse-bars" aria-hidden="true">
                {[62, 78, 70, 91, 83, 94, 87, 96, 92, 98].map((height, index) => <span key={index} style={{ height: `${height}%` }} />)}
              </div>
              <div className="pulse-metrics">
                <div><small>Indexed</small><strong>1,842</strong></div>
                <div><small>Coverage</small><strong>96%</strong></div>
                <div><small>Queue</small><strong>2</strong></div>
              </div>
            </article>
          </section>

          <section className="attention-strip">
            <div className="attention-icon"><Clock3 aria-hidden="true" size={19} /></div>
            <div><strong>2 sources need your attention</strong><p>One import is waiting for review and one source is ready to retry.</p></div>
            <button type="button">Review now</button>
          </section>
        </div>
      </main>
    </div>
  );
}
