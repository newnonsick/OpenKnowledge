"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { Activity, ArrowUpRight, BookOpen, Code2, Copy, FileText, FileUp, FolderKanban, KeyRound, Layers3, LoaderCircle, LockKeyhole, MonitorSmartphone, Plus, Search, Settings2, ShieldCheck, Sparkles, Tag, UserPlus, UsersRound } from "lucide-react";
import { useSearchParams } from "next/navigation";

import { useCurrentMember } from "@/components/auth/session-gate";
import { ConsoleShell } from "@/components/console-shell";
import { ApiError, apiMultipart, apiRequest } from "@/lib/api-client";

type Space = {
  id: string;
  name: string;
  personal?: boolean;
  revision: number;
  role: "editor" | "owner" | "reader";
};

type SpaceListResponse = {
  items: Space[];
  next_cursor: string | null;
};

type KnowledgeSummary = {
  id: string;
  space_id: string;
  tags: string[];
  title: string;
  updated_at: string;
  version: number;
};

type KnowledgeListResponse = {
  items: KnowledgeSummary[];
  next_cursor: string | null;
};

type RetrievalHit = {
  canonical_id: string;
  citation_uri: string | null;
  content_excerpt: string;
  rank: number;
  rank_score: number;
  source_type: string;
  space_id: string;
  title: string;
  version: number | null;
};

type RetrievalResponse = {
  explanation: { abstained: boolean; effective_space_ids: string[] };
  health: { degraded_reasons: string[]; semantic_status: string };
  hits: RetrievalHit[];
};

type SourceSummary = {
  display_name: string;
  id: string;
  original_filename: string | null;
  size_bytes: number | null;
  space_id: string;
  status: string;
  updated_at: string;
};

type SourceListResponse = {
  items: SourceSummary[];
  next_cursor: string | null;
};

type MemberSummary = {
  display_name: string;
  id: string;
  requires_password_change: boolean;
  status: string;
  system_role: string;
  username: string;
};

type MemberListResponse = {
  items: MemberSummary[];
  next_cursor: string | null;
};

type CreatedMember = {
  display_name: string;
  id: string;
  temporary_password: string;
  temporary_password_expires_at: string;
  username: string;
};

type APIKeySummary = {
  created_at: string;
  id: string;
  name: string;
  public_id: string;
  scopes: string[];
  status: string;
};

type APIKeyListResponse = {
  items: APIKeySummary[];
  next_cursor: string | null;
};

type CreatedAPIKey = {
  id: string;
  public_id: string;
  scopes: string[];
  secret: string;
};

type SessionSummary = {
  created_at: string;
  current: boolean;
  id: string;
  last_activity_at: string;
  status: string;
};

type SessionListResponse = {
  items: SessionSummary[];
  next_cursor: string | null;
};

type RuntimeSettings = {
  revision: number;
  state: string;
  values: Record<string, unknown>;
};

type IngestionJob = {
  attempt_count: number;
  created_at: string;
  document_id: string;
  id: string;
  last_error_code?: string | null;
  max_attempts: number;
  progress: number;
  space_id: string;
  state: string;
  updated_at: string;
};

type IngestionListResponse = {
  items: IngestionJob[];
  next_cursor: string | null;
};

type AuditEvent = {
  action: string;
  actor_kind: string;
  id: string;
  occurred_at: string;
  outcome: string;
  request_id: string;
  resource_id: string | null;
  resource_type: string;
};

type AuditListResponse = {
  items: AuditEvent[];
  next_cursor: string | null;
};

function memberView(member: ReturnType<typeof useCurrentMember>) {
  return {
    displayName: member.display_name,
    role: member.system_role === "super_admin" ? "Super admin" : "Member",
  };
}

function message(error: unknown) {
  return error instanceof ApiError ? error.message : "The request could not be completed.";
}

export function SpacesConsole() {
  const member = useCurrentMember();
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const response = await apiRequest<SpaceListResponse>("/api/v1/spaces?limit=100");
      setSpaces(response.items);
      setError(null);
    } catch (loadError) {
      setError(message(loadError));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const create = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const value = name.trim();
    if (!value || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await apiRequest("/api/v1/spaces", { body: { name: value }, idempotent: true, method: "POST" });
      setName("");
      await load();
    } catch (createError) {
      setError(message(createError));
    } finally {
      setSaving(false);
    }
  };

  return (
    <ConsoleShell
      description="Create focused spaces for private projects while unified discovery searches everything you can access."
      eyebrow="Knowledge boundaries"
      member={memberView(member)}
      spaceCount={spaces.length}
      title="Spaces"
    >
      <div className="console-grid console-grid-spaces">
        <section className="console-panel">
          <div className="panel-heading">
            <div><span>Accessible now</span><h2>Your spaces</h2></div>
            <span className="count-pill">{spaces.length}</span>
          </div>
          {loading ? <div className="console-loading"><LoaderCircle className="spin" size={18} /> Loading spaces…</div> : null}
          {!loading && spaces.length === 0 ? <div className="console-empty"><FolderKanban size={23} /><strong>No spaces yet</strong><span>Create one for a project or keep using shared knowledge.</span></div> : null}
          <div className="space-card-grid">
            {spaces.map((space, index) => (
              <article className="space-card" key={space.id}>
                <div className={`space-card-mark accent-${index % 4}`}><FolderKanban aria-hidden="true" size={19} /></div>
                <div><h3>{space.name}</h3><p>{space.id}</p></div>
                <span className={`role-pill role-${space.role}`}>{space.role}</span>
                <p className="space-card-note"><ShieldCheck aria-hidden="true" size={14} /> Search access follows this membership</p>
              </article>
            ))}
          </div>
        </section>
        <aside className="console-panel action-panel">
          <span className="action-panel-icon"><Plus aria-hidden="true" size={20} /></span>
          <p className="console-eyebrow">New boundary</p>
          <h2>Create a space</h2>
          <p>Best for a trip, home project, personal archive, or anything with a smaller access list.</p>
          <form className="console-form" onSubmit={create}>
            <label htmlFor="space-name">Space name</label>
            <input id="space-name" maxLength={120} onChange={(event) => setName(event.target.value)} placeholder="e.g. Japan trip" required value={name} />
            <button className="primary-button" disabled={saving} type="submit">{saving ? <LoaderCircle className="spin" size={16} /> : <Plus size={16} />} Create space</button>
          </form>
          {error ? <p className="inline-error" role="alert">{error}</p> : null}
        </aside>
      </div>
    </ConsoleShell>
  );
}

export function KnowledgeConsole() {
  const member = useCurrentMember();
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [items, setItems] = useState<KnowledgeSummary[]>([]);
  const [spaceId, setSpaceId] = useState("");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [tags, setTags] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadItems = useCallback(async () => {
    const response = await apiRequest<KnowledgeListResponse>("/api/v1/knowledge?limit=100");
    setItems(response.items);
  }, []);

  useEffect(() => {
    let active = true;
    Promise.all([
      apiRequest<SpaceListResponse>("/api/v1/spaces?limit=100"),
      apiRequest<KnowledgeListResponse>("/api/v1/knowledge?limit=100"),
    ]).then(([spaceResponse, knowledgeResponse]) => {
      if (!active) {
        return;
      }
      setSpaces(spaceResponse.items);
      setSpaceId((current) => current || spaceResponse.items[0]?.id || "");
      setItems(knowledgeResponse.items);
      setError(null);
    }).catch((loadError) => {
      if (active) {
        setError(message(loadError));
      }
    }).finally(() => {
      if (active) {
        setLoading(false);
      }
    });
    return () => {
      active = false;
    };
  }, []);

  const create = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!spaceId || !title.trim() || !content.trim() || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await apiRequest("/api/v1/knowledge", {
        body: {
          content: content.trim(),
          space_id: spaceId,
          tags: tags.split(",").map((value) => value.trim()).filter(Boolean),
          title: title.trim(),
        },
        idempotent: true,
        method: "POST",
      });
      setTitle("");
      setContent("");
      setTags("");
      await loadItems();
    } catch (createError) {
      setError(message(createError));
    } finally {
      setSaving(false);
    }
  };

  return (
    <ConsoleShell
      description="Capture durable notes with immutable revisions, clear provenance, and permission-aware retrieval."
      eyebrow="Canonical knowledge"
      member={memberView(member)}
      spaceCount={spaces.length}
      title="Knowledge"
    >
      <div className="console-grid console-grid-knowledge">
        <section className="console-panel">
          <div className="panel-heading">
            <div><span>Living library</span><h2>Knowledge items</h2></div>
            <span className="count-pill">{items.length}</span>
          </div>
          {loading ? <div className="console-loading"><LoaderCircle className="spin" size={18} /> Loading knowledge…</div> : null}
          {!loading && items.length === 0 ? <div className="console-empty"><BookOpen size={23} /><strong>Nothing captured yet</strong><span>Add the first durable answer, procedure, or family detail.</span></div> : null}
          <div className="data-list">
            {items.map((item) => (
              <article className="data-row knowledge-row" key={item.id}>
                <span className="row-leading violet"><BookOpen aria-hidden="true" size={18} /></span>
                <div className="row-copy"><h3>{item.title}</h3><p>{item.space_id} · version {item.version}</p></div>
                <div className="tag-list">{item.tags.slice(0, 3).map((value) => <span key={value}><Tag size={11} />{value}</span>)}</div>
              </article>
            ))}
          </div>
        </section>
        <aside className="console-panel action-panel capture-panel">
          <span className="action-panel-icon violet"><BookOpen aria-hidden="true" size={20} /></span>
          <p className="console-eyebrow">Manual capture</p>
          <h2>Capture knowledge</h2>
          <p>Use plain language. Kinbase keeps the revision history and makes the active version searchable.</p>
          <form className="console-form" onSubmit={create}>
            <label htmlFor="knowledge-space">Space</label>
            <select id="knowledge-space" onChange={(event) => setSpaceId(event.target.value)} required value={spaceId}>
              {spaces.map((space) => <option key={space.id} value={space.id}>{space.name}</option>)}
            </select>
            <label htmlFor="knowledge-title">Title</label>
            <input id="knowledge-title" maxLength={500} onChange={(event) => setTitle(event.target.value)} required value={title} />
            <label htmlFor="knowledge-content">Knowledge content</label>
            <textarea id="knowledge-content" maxLength={1000000} onChange={(event) => setContent(event.target.value)} required rows={7} value={content} />
            <label htmlFor="knowledge-tags">Tags</label>
            <input id="knowledge-tags" onChange={(event) => setTags(event.target.value)} placeholder="home, safety" value={tags} />
            <button className="primary-button" disabled={saving || spaces.length === 0} type="submit">{saving ? <LoaderCircle className="spin" size={16} /> : <Plus size={16} />} Capture knowledge</button>
          </form>
          {error ? <p className="inline-error" role="alert">{error}</p> : null}
        </aside>
      </div>
    </ConsoleShell>
  );
}

export function ExploreConsole() {
  const member = useCurrentMember();
  const searchParams = useSearchParams();
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [query, setQuery] = useState(searchParams.get("q") || "");
  const [result, setResult] = useState<RetrievalResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    apiRequest<SpaceListResponse>("/api/v1/spaces?limit=100")
      .then((response) => {
        if (active) {
          setSpaces(response.items);
        }
      })
      .catch((loadError) => {
        if (active) {
          setError(message(loadError));
        }
      })
      .finally(() => {
        if (active) {
          setLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, []);

  const search = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!query.trim() || searching) {
      return;
    }
    setSearching(true);
    setError(null);
    try {
      const response = await apiRequest<RetrievalResponse>("/api/v1/retrieval/search", {
        body: {
          limit: 20,
          query: query.trim(),
          semantic_policy: "best_effort",
          space_ids: spaces.map((space) => space.id),
        },
        method: "POST",
      });
      setResult(response);
      const params = new URLSearchParams(searchParams.toString());
      params.set("q", query.trim());
      window.history.replaceState(null, "", `/explore?${params.toString()}`);
    } catch (searchError) {
      setError(message(searchError));
    } finally {
      setSearching(false);
    }
  };

  return (
    <ConsoleShell
      description="Search once across every space you can access. Results remain permission-aware even with a large collection of spaces."
      eyebrow="Unified discovery"
      member={memberView(member)}
      spaceCount={spaces.length}
      title="Explore"
    >
      <section className="explore-hero console-panel">
        <div className="search-scope"><ShieldCheck size={15} /> {loading ? "Resolving access…" : `${spaces.length} spaces in scope`}</div>
        <form className="console-search" onSubmit={search} role="search">
          <Search aria-hidden="true" size={21} />
          <input aria-label="Search query" autoFocus onChange={(event) => setQuery(event.target.value)} placeholder="Ask for a detail, process, place, or decision…" type="search" value={query} />
          <button aria-label="Search knowledge" disabled={searching || loading} type="submit">{searching ? <LoaderCircle className="spin" size={17} /> : <ArrowUpRight size={17} />}</button>
        </form>
        <p>Kinbase fans the query out only to authorized spaces, merges the candidates, and returns a single ranked result set.</p>
      </section>
      {error ? <p className="inline-error wide" role="alert">{error}</p> : null}
      {result ? (
        <section className="search-results-section">
          <div className="results-toolbar">
            <div><p className="console-eyebrow">Ranked results</p><h2>{result.hits.length} matches</h2></div>
            <span className={`health-chip ${result.health.semantic_status}`}>{result.health.semantic_status === "degraded" ? "Semantic layer unavailable · lexical results remain active" : "Semantic + lexical retrieval active"}</span>
          </div>
          {result.hits.length === 0 ? <div className="console-empty result-empty"><Search size={23} /><strong>No confident match</strong><span>Try a more specific phrase or add the missing knowledge.</span></div> : null}
          <div className="result-list">
            {result.hits.map((hit) => (
              <article className="result-card" key={`${hit.source_type}-${hit.canonical_id}`}>
                <div className="result-rank">{String(hit.rank).padStart(2, "0")}</div>
                <div className="result-copy">
                  <div className="result-meta"><span>{hit.space_id}</span><span>{hit.source_type.replaceAll("_", " ")}</span>{hit.version ? <span>v{hit.version}</span> : null}</div>
                  <h3>{hit.title}</h3>
                  <p>{hit.content_excerpt}</p>
                  {hit.citation_uri ? <code>{hit.citation_uri}</code> : null}
                </div>
                <span className="score-pill">{Math.round(hit.rank_score * 100)}%</span>
              </article>
            ))}
          </div>
        </section>
      ) : (
        <section className="explore-prompt-grid">
          <article><span>01</span><h3>Search once</h3><p>No need to guess which space contains the answer.</p></article>
          <article><span>02</span><h3>Respect access</h3><p>Spaces outside your membership never enter the candidate set.</p></article>
          <article><span>03</span><h3>Stay useful</h3><p>Lexical retrieval continues safely when embedding is offline.</p></article>
        </section>
      )}
    </ConsoleShell>
  );
}

export function SourcesConsole() {
  const member = useCurrentMember();
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [sources, setSources] = useState<SourceSummary[]>([]);
  const [spaceId, setSpaceId] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [queued, setQueued] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadSources = useCallback(async () => {
    const response = await apiRequest<SourceListResponse>("/api/v1/sources?limit=100");
    setSources(response.items);
  }, []);

  useEffect(() => {
    let active = true;
    Promise.all([
      apiRequest<SpaceListResponse>("/api/v1/spaces?limit=100"),
      apiRequest<SourceListResponse>("/api/v1/sources?limit=100"),
    ]).then(([spaceResponse, sourceResponse]) => {
      if (!active) {
        return;
      }
      setSpaces(spaceResponse.items);
      setSpaceId(spaceResponse.items[0]?.id || "");
      setSources(sourceResponse.items);
    }).catch((loadError) => {
      if (active) {
        setError(message(loadError));
      }
    }).finally(() => {
      if (active) {
        setLoading(false);
      }
    });
    return () => {
      active = false;
    };
  }, []);

  const upload = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!file || !spaceId || saving) {
      return;
    }
    const body = new FormData();
    body.set("file", file);
    body.set("space_id", spaceId);
    body.set("display_name", displayName.trim() || file.name);
    setSaving(true);
    setQueued(false);
    setError(null);
    try {
      await apiMultipart("/api/v1/sources/upload", body, { idempotent: true });
      setQueued(true);
      setDisplayName("");
      setFile(null);
      await loadSources();
    } catch (uploadError) {
      setError(message(uploadError));
    } finally {
      setSaving(false);
    }
  };

  return (
    <ConsoleShell
      description="Upload original files into versioned object storage and track every revision through the durable ingestion pipeline."
      eyebrow="Source library"
      member={memberView(member)}
      spaceCount={spaces.length}
      title="Sources"
    >
      <div className="console-grid console-grid-sources">
        <section className="console-panel">
          <div className="panel-heading">
            <div><span>Original material</span><h2>Source files</h2></div>
            <span className="count-pill">{sources.length}</span>
          </div>
          {loading ? <div className="console-loading"><LoaderCircle className="spin" size={18} /> Loading sources…</div> : null}
          {!loading && sources.length === 0 ? <div className="console-empty"><FileText size={23} /><strong>No source files</strong><span>Upload a document without changing its meaning or filtering its contents.</span></div> : null}
          <div className="data-list">
            {sources.map((source) => (
              <article className="data-row source-row" key={source.id}>
                <span className="row-leading cyan"><FileText aria-hidden="true" size={18} /></span>
                <div className="row-copy"><h3>{source.display_name}</h3><p>{source.original_filename || "Unnamed file"} · {source.space_id}</p></div>
                <div className="row-stats"><strong>{source.size_bytes === null ? "—" : `${Math.max(1, Math.round(source.size_bytes / 1024))} KB`}</strong><span className={`status-pill status-${source.status}`}>{source.status}</span></div>
              </article>
            ))}
          </div>
        </section>
        <aside className="console-panel action-panel upload-panel">
          <span className="action-panel-icon cyan"><FileUp aria-hidden="true" size={20} /></span>
          <p className="console-eyebrow">Durable upload</p>
          <h2>Add a source</h2>
          <p>The website stores the original bytes first, then queues parsing and retrieval activation separately.</p>
          <form className="console-form" onSubmit={upload}>
            <label htmlFor="source-space">Space</label>
            <select id="source-space" onChange={(event) => setSpaceId(event.target.value)} required value={spaceId}>{spaces.map((space) => <option key={space.id} value={space.id}>{space.name}</option>)}</select>
            <label htmlFor="source-name">Display name</label>
            <input id="source-name" maxLength={500} onChange={(event) => setDisplayName(event.target.value)} placeholder="Defaults to filename" value={displayName} />
            <label className="file-drop" htmlFor="source-file"><FileUp size={20} /><strong>{file ? file.name : "Choose a source file"}</strong><span>{file ? `${Math.max(1, Math.round(file.size / 1024))} KB` : "The configured server upload limit applies"}</span></label>
            <input aria-label="Source file" className="visually-hidden" id="source-file" onChange={(event) => setFile(event.target.files?.[0] || null)} required type="file" />
            <button className="primary-button" disabled={saving || !file || spaces.length === 0} type="submit">{saving ? <LoaderCircle className="spin" size={16} /> : <FileUp size={16} />} Queue source</button>
          </form>
          {queued ? <p className="inline-success"><ShieldCheck size={14} /> Queued for durable ingestion</p> : null}
          {error ? <p className="inline-error" role="alert">{error}</p> : null}
        </aside>
      </div>
    </ConsoleShell>
  );
}

export function PeopleConsole() {
  const member = useCurrentMember();
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [members, setMembers] = useState<MemberSummary[]>([]);
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [created, setCreated] = useState<CreatedMember | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const isAdmin = member.system_role === "super_admin";

  const loadMembers = useCallback(async () => {
    const response = await apiRequest<MemberListResponse>("/api/v1/members?limit=100");
    setMembers(response.items);
  }, []);

  useEffect(() => {
    let active = true;
    const requests: [Promise<SpaceListResponse>, Promise<MemberListResponse | null>] = [
      apiRequest<SpaceListResponse>("/api/v1/spaces?limit=100"),
      isAdmin ? apiRequest<MemberListResponse>("/api/v1/members?limit=100") : Promise.resolve(null),
    ];
    Promise.all(requests).then(([spaceResponse, memberResponse]) => {
      if (!active) {
        return;
      }
      setSpaces(spaceResponse.items);
      setMembers(memberResponse?.items || []);
    }).catch((loadError) => {
      if (active) {
        setError(message(loadError));
      }
    }).finally(() => {
      if (active) {
        setLoading(false);
      }
    });
    return () => {
      active = false;
    };
  }, [isAdmin]);

  const create = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!username.trim() || !displayName.trim() || saving) {
      return;
    }
    setSaving(true);
    setCreated(null);
    setError(null);
    try {
      const response = await apiRequest<CreatedMember>("/api/v1/members", {
        body: { display_name: displayName.trim(), username: username.trim() },
        idempotent: true,
        method: "POST",
      });
      setCreated(response);
      setUsername("");
      setDisplayName("");
      await loadMembers();
    } catch (createError) {
      setError(message(createError));
    } finally {
      setSaving(false);
    }
  };

  return (
    <ConsoleShell
      description="Super admins create family accounts. Space owners grant access separately so identity and knowledge boundaries stay explicit."
      eyebrow="Identity & access"
      member={memberView(member)}
      spaceCount={spaces.length}
      title="People & access"
    >
      {!isAdmin ? (
        <section className="console-panel permission-panel"><ShieldCheck size={25} /><div><h2>Space access remains owner-managed</h2><p>Your account can manage memberships inside spaces you own. New family identities can only be generated by a super admin.</p></div></section>
      ) : (
        <div className="console-grid console-grid-people">
          <section className="console-panel">
            <div className="panel-heading"><div><span>Family directory</span><h2>Members</h2></div><span className="count-pill">{members.length}</span></div>
            {loading ? <div className="console-loading"><LoaderCircle className="spin" size={18} /> Loading members…</div> : null}
            <div className="data-list">
              {members.map((person) => (
                <article className="data-row member-row" key={person.id}>
                  <span className="profile-avatar">{person.display_name.slice(0, 2).toUpperCase()}</span>
                  <div className="row-copy"><h3>{person.display_name}</h3><p>@{person.username}</p></div>
                  <div className="member-state"><span className={`status-pill status-${person.status}`}>{person.status}</span>{person.requires_password_change ? <small>First sign-in pending</small> : null}</div>
                </article>
              ))}
            </div>
          </section>
          <aside className="console-panel action-panel member-create-panel">
            {created ? (
              <div className="secret-reveal">
                <span className="action-panel-icon mint"><ShieldCheck size={20} /></span>
                <p className="console-eyebrow">Member created</p>
                <h2>Share securely</h2>
                <p>This temporary password is shown only once. The member must replace it at first sign-in.</p>
                <div className="secret-value"><code>{created.temporary_password}</code><button aria-label="Copy temporary password" onClick={() => navigator.clipboard?.writeText(created.temporary_password)} type="button"><Copy size={16} /></button></div>
                <dl><div><dt>Username</dt><dd>{created.username}</dd></div><div><dt>Member</dt><dd>{created.display_name}</dd></div></dl>
                <button className="secondary-button" onClick={() => setCreated(null)} type="button"><UserPlus size={15} /> Create another member</button>
              </div>
            ) : (
              <>
                <span className="action-panel-icon violet"><UserPlus aria-hidden="true" size={20} /></span>
                <p className="console-eyebrow">Super admin only</p>
                <h2>Create a member</h2>
                <p>Kinbase generates a strong one-time password. No social login or self-registration is exposed.</p>
                <form className="console-form" onSubmit={create}>
                  <label htmlFor="member-username">Username</label>
                  <input autoComplete="off" id="member-username" maxLength={64} onChange={(event) => setUsername(event.target.value)} required value={username} />
                  <label htmlFor="member-display-name">Display name</label>
                  <input id="member-display-name" maxLength={255} onChange={(event) => setDisplayName(event.target.value)} required value={displayName} />
                  <button className="primary-button" disabled={saving} type="submit">{saving ? <LoaderCircle className="spin" size={16} /> : <UsersRound size={16} />} Generate member</button>
                </form>
              </>
            )}
            {error ? <p className="inline-error" role="alert">{error}</p> : null}
          </aside>
        </div>
      )}
    </ConsoleShell>
  );
}

export function SettingsConsole() {
  const member = useCurrentMember();
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [keys, setKeys] = useState<APIKeySummary[]>([]);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [settings, setSettings] = useState<RuntimeSettings | null>(null);
  const [keyName, setKeyName] = useState("");
  const [createdKey, setCreatedKey] = useState<CreatedAPIKey | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadKeys = useCallback(async () => {
    const response = await apiRequest<APIKeyListResponse>("/api/v1/api-keys");
    setKeys(response.items);
  }, []);

  useEffect(() => {
    let active = true;
    Promise.all([
      apiRequest<SpaceListResponse>("/api/v1/spaces?limit=100"),
      apiRequest<APIKeyListResponse>("/api/v1/api-keys"),
      apiRequest<SessionListResponse>("/api/v1/sessions"),
      apiRequest<RuntimeSettings>("/api/v1/settings"),
    ]).then(([spaceResponse, keyResponse, sessionResponse, runtimeResponse]) => {
      if (!active) {
        return;
      }
      setSpaces(spaceResponse.items);
      setKeys(keyResponse.items);
      setSessions(sessionResponse.items);
      setSettings(runtimeResponse);
    }).catch((loadError) => {
      if (active) {
        setError(message(loadError));
      }
    }).finally(() => {
      if (active) {
        setLoading(false);
      }
    });
    return () => {
      active = false;
    };
  }, []);

  const createKey = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!keyName.trim() || saving) {
      return;
    }
    setSaving(true);
    setCreatedKey(null);
    setError(null);
    try {
      const response = await apiRequest<CreatedAPIKey>("/api/v1/api-keys", {
        body: { name: keyName.trim(), scopes: ["knowledge:read"] },
        idempotent: true,
        method: "POST",
      });
      setCreatedKey(response);
      setKeyName("");
      await loadKeys();
    } catch (createError) {
      setError(message(createError));
    } finally {
      setSaving(false);
    }
  };

  return (
    <ConsoleShell
      description="Manage personal API credentials, active website sessions, and the safe runtime configuration boundary."
      eyebrow="Account control"
      member={memberView(member)}
      spaceCount={spaces.length}
      title="Settings"
    >
      {loading ? <div className="console-loading standalone"><LoaderCircle className="spin" size={18} /> Loading secure settings…</div> : null}
      <div className="settings-layout">
        <section className="console-panel settings-section">
          <div className="panel-heading"><div><span>Personal credentials</span><h2>API keys</h2></div><KeyRound size={20} /></div>
          <p className="section-intro">Every member owns separate keys. Start with the narrowest scope and create another key for a different device or automation.</p>
          {createdKey ? <div className="secret-banner"><div><strong>Copy this key now</strong><span>It cannot be displayed again after you leave this result.</span></div><code>{createdKey.secret}</code><button aria-label="Copy API key" onClick={() => navigator.clipboard?.writeText(createdKey.secret)} type="button"><Copy size={16} /></button></div> : null}
          <form className="inline-create-form" onSubmit={createKey}>
            <div><label htmlFor="api-key-name">Key name</label><input id="api-key-name" maxLength={120} onChange={(event) => setKeyName(event.target.value)} placeholder="e.g. Laptop" required value={keyName} /></div>
            <div className="scope-selection"><span>Initial scope</span><strong>knowledge:read</strong></div>
            <button className="primary-button" disabled={saving} type="submit">{saving ? <LoaderCircle className="spin" size={16} /> : <KeyRound size={16} />} Create API key</button>
          </form>
          <div className="data-list compact-list">
            {keys.map((key) => <article className="data-row" key={key.id}><span className="row-leading violet"><KeyRound size={17} /></span><div className="row-copy"><h3>{key.name}</h3><p>{key.public_id} · {key.scopes.join(", ")}</p></div><span className={`status-pill status-${key.status}`}>{key.status}</span></article>)}
          </div>
        </section>

        <section className="console-panel settings-section">
          <div className="panel-heading"><div><span>Website access</span><h2>Sessions</h2></div><MonitorSmartphone size={20} /></div>
          <div className="data-list compact-list">
            {sessions.map((session) => <article className="data-row" key={session.id}><span className="row-leading cyan"><MonitorSmartphone size={17} /></span><div className="row-copy"><h3>{session.current ? "This session" : "Website session"}</h3><p>Last active {new Date(session.last_activity_at).toLocaleString()}</p></div><span className={`status-pill status-${session.status}`}>{session.status}</span></article>)}
            {sessions.length === 0 ? <div className="console-empty small"><MonitorSmartphone size={20} /><strong>No session records returned</strong></div> : null}
          </div>
        </section>

        <section className="console-panel settings-section runtime-section">
          <div className="panel-heading"><div><span>Production boundary</span><h2>Safe runtime settings</h2></div><Settings2 size={20} /></div>
          <div className="runtime-summary"><div><span>Active revision</span><strong>{settings?.revision ?? 0}</strong></div><div><span>State</span><strong>{settings?.state || "active"}</strong></div><div><span>Change mode</span><strong>{member.system_role === "super_admin" ? "Draft + activate" : "Read only"}</strong></div></div>
          <pre>{JSON.stringify(settings?.values || {}, null, 2)}</pre>
        </section>
      </div>
      {error ? <p className="inline-error wide" role="alert">{error}</p> : null}
    </ConsoleShell>
  );
}

export function IngestionConsole() {
  const member = useCurrentMember();
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [jobs, setJobs] = useState<IngestionJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadJobs = useCallback(async () => {
    try {
      const response = await apiRequest<IngestionListResponse>("/api/v1/ingestion-jobs?limit=100");
      setJobs(response.items);
      setError(null);
    } catch (loadError) {
      setError(message(loadError));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let active = true;
    apiRequest<SpaceListResponse>("/api/v1/spaces?limit=100").then((response) => {
      if (active) {
        setSpaces(response.items);
      }
    }).catch((loadError) => {
      if (active) {
        setError(message(loadError));
      }
    });
    void loadJobs();
    const timer = window.setInterval(loadJobs, 15000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [loadJobs]);

  const activeJobs = jobs.filter((job) => !["completed", "failed", "cancelled"].includes(job.state)).length;
  return (
    <ConsoleShell
      description="Follow the durable pipeline from queued source through parsing, activation, retry, or a clear terminal state."
      eyebrow="Operational pipeline"
      member={memberView(member)}
      spaceCount={spaces.length}
      title="Ingestion"
    >
      <div className="metric-strip"><article><span>Tracked jobs</span><strong>{jobs.length}</strong></article><article><span>In progress</span><strong>{activeJobs}</strong></article><article><span>Refresh</span><strong>15s</strong></article></div>
      <section className="console-panel">
        <div className="panel-heading"><div><span>Live durable state</span><h2>Ingestion jobs</h2></div><Layers3 size={20} /></div>
        {loading ? <div className="console-loading"><LoaderCircle className="spin" size={18} /> Loading jobs…</div> : null}
        {!loading && jobs.length === 0 ? <div className="console-empty"><Layers3 size={23} /><strong>No ingestion jobs</strong><span>Uploaded sources will appear here as soon as preparation begins.</span></div> : null}
        <div className="job-list">
          {jobs.map((job) => (
            <article className="job-card" key={job.id}>
              <div className="job-topline"><div><span className={`state-dot state-${job.state}`} /><code>{job.id}</code></div><span className={`status-pill status-${job.state}`}>{job.state}</span></div>
              <div className="job-context"><strong>{job.space_id}</strong><span>document {job.document_id}</span><span>attempt {job.attempt_count}/{job.max_attempts}</span></div>
              <div className="progress-row"><progress max={100} value={Math.max(0, Math.min(100, job.progress))} /><strong>{Math.round(job.progress)}%</strong></div>
              {job.last_error_code ? <p className="job-error">{job.last_error_code}</p> : null}
            </article>
          ))}
        </div>
      </section>
      {error ? <p className="inline-error wide" role="alert">{error}</p> : null}
    </ConsoleShell>
  );
}

export function ActivityConsole() {
  const member = useCurrentMember();
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const isAdmin = member.system_role === "super_admin";

  useEffect(() => {
    let active = true;
    Promise.all([
      apiRequest<SpaceListResponse>("/api/v1/spaces?limit=100"),
      isAdmin ? apiRequest<AuditListResponse>("/api/v1/audit-events?limit=100") : Promise.resolve(null),
    ]).then(([spaceResponse, auditResponse]) => {
      if (!active) {
        return;
      }
      setSpaces(spaceResponse.items);
      setEvents(auditResponse?.items || []);
    }).catch((loadError) => {
      if (active) {
        setError(message(loadError));
      }
    }).finally(() => {
      if (active) {
        setLoading(false);
      }
    });
    return () => {
      active = false;
    };
  }, [isAdmin]);

  return (
    <ConsoleShell
      description="Review security-sensitive mutations with actor, resource, outcome, timestamp, and request correlation."
      eyebrow="Immutable evidence"
      member={memberView(member)}
      spaceCount={spaces.length}
      title="Activity"
    >
      {!isAdmin ? <section className="console-panel permission-panel"><LockKeyhole size={25} /><div><h2>Audit access is restricted</h2><p>Only super admins can review family-wide audit events. Space membership remains visible to each space owner.</p></div></section> : (
        <section className="console-panel">
          <div className="panel-heading"><div><span>Security chronology</span><h2>Audit events</h2></div><Activity size={20} /></div>
          {loading ? <div className="console-loading"><LoaderCircle className="spin" size={18} /> Loading activity…</div> : null}
          <div className="audit-list">
            {events.map((event) => <article className="audit-row" key={event.id}><span className={`audit-outcome outcome-${event.outcome}`} /><time>{new Date(event.occurred_at).toLocaleString()}</time><div><h3>{event.action}</h3><p>{event.resource_type}{event.resource_id ? ` · ${event.resource_id}` : ""}</p></div><code>{event.request_id}</code><span className="status-pill">{event.outcome}</span></article>)}
            {!loading && events.length === 0 ? <div className="console-empty"><Activity size={23} /><strong>No audit events returned</strong></div> : null}
          </div>
        </section>
      )}
      {error ? <p className="inline-error wide" role="alert">{error}</p> : null}
    </ConsoleShell>
  );
}

const aiTools = [
  ["list_spaces", "Read", "Return only spaces the current member can access."],
  ["create_space", "Write", "Create a new private space owned by the current member."],
  ["list_space_members", "Read", "Inspect access for a space the current member owns."],
  ["set_space_membership", "Confirm", "Grant or change a member role after explicit confirmation."],
  ["search_knowledge", "Read", "Run permission-aware unified retrieval across authorized spaces."],
  ["create_knowledge", "Write", "Capture a canonical knowledge item with provenance and revision one."],
  ["update_knowledge", "Confirm", "Create an immutable new revision with optimistic concurrency."],
  ["delete_knowledge", "Confirm", "Archive an item and remove its active retrieval projection."],
  ["upload_source", "Write", "Store original bytes and queue the durable ingestion pipeline."],
  ["list_ingestion_jobs", "Read", "Return real job states, retries, progress, and terminal errors."],
  ["create_api_key", "Confirm", "Issue a personal scoped key and reveal its secret once."],
  ["revoke_api_key", "Confirm", "Revoke one of the current member's API keys."],
];

export function AiActionsConsole() {
  const member = useCurrentMember();
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    apiRequest<SpaceListResponse>("/api/v1/spaces?limit=100").then((response) => {
      if (active) {
        setSpaces(response.items);
      }
    }).catch((loadError) => {
      if (active) {
        setError(message(loadError));
      }
    });
    return () => {
      active = false;
    };
  }, []);

  return (
    <ConsoleShell
      description="A typed management surface for trusted assistants. Identity, scope, idempotency, audit, and confirmations remain server-enforced."
      eyebrow="Tool contracts"
      member={memberView(member)}
      spaceCount={spaces.length}
      title="AI actions"
    >
      <section className="tool-principle"><Sparkles size={22} /><div><strong>Plain requests in, deliberate operations out</strong><p>Read actions can run directly. High-impact writes require a short-lived confirmation before execution.</p></div></section>
      <div className="tool-grid">
        {aiTools.map(([name, mode, description], index) => <article className="tool-card" key={name}><div><span className={`tool-icon accent-${index % 4}`}><Code2 size={17} /></span><span className={`mode-pill mode-${mode.toLowerCase()}`}>{mode}</span></div><code>{name}</code><p>{description}</p></article>)}
      </div>
      {error ? <p className="inline-error wide" role="alert">{error}</p> : null}
    </ConsoleShell>
  );
}
