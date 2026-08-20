"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import { Activity, Archive, ArrowUpRight, BookOpen, Code2, Copy, FileText, FileUp, FolderKanban, KeyRound, Layers3, LoaderCircle, LockKeyhole, MonitorSmartphone, Plus, Save, Search, Settings2, ShieldCheck, Sparkles, Tag, UserMinus, UserPlus, UsersRound, X } from "lucide-react";
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

type SpaceMember = {
  display_name: string;
  member_id: string;
  role: "editor" | "owner" | "reader";
  status: string;
  username: string;
};

type SpaceMemberCandidate = {
  display_name: string;
  member_id: string;
  username: string;
};

type SpaceMemberListResponse = {
  items: SpaceMember[];
  next_cursor: string | null;
};

type SpaceMemberCandidateListResponse = {
  items: SpaceMemberCandidate[];
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

type KnowledgeDetail = KnowledgeSummary & {
  content: string;
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

type ResetMemberPassword = {
  id: string;
  requires_password_change: boolean;
  temporary_password: string;
  temporary_password_expires_at: string;
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
  base_revision?: number;
  id?: string | null;
  revision: number;
  state: string;
  values: {
    retrieval: { limit: number; [key: string]: unknown };
    [key: string]: unknown;
  };
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
  const [accessLoading, setAccessLoading] = useState(false);
  const [selectedSpace, setSelectedSpace] = useState<Space | null>(null);
  const [spaceMembers, setSpaceMembers] = useState<SpaceMember[]>([]);
  const [memberCandidates, setMemberCandidates] = useState<SpaceMemberCandidate[]>([]);
  const [candidateId, setCandidateId] = useState("");
  const [candidateRole, setCandidateRole] = useState<SpaceMember["role"]>("reader");
  const [pendingRemoval, setPendingRemoval] = useState<SpaceMember | null>(null);
  const [confirmSpaceArchive, setConfirmSpaceArchive] = useState(false);
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

  const loadAccess = useCallback(async (space: Space) => {
    setAccessLoading(true);
    setError(null);
    try {
      const [membersResponse, candidatesResponse] = await Promise.all([
        apiRequest<SpaceMemberListResponse>(`/api/v1/spaces/${space.id}/members`),
        apiRequest<SpaceMemberCandidateListResponse>(`/api/v1/spaces/${space.id}/member-candidates`),
      ]);
      setSelectedSpace(space);
      setSpaceMembers(membersResponse.items);
      setMemberCandidates(candidatesResponse.items);
      setCandidateId(candidatesResponse.items[0]?.member_id || "");
      setConfirmSpaceArchive(false);
    } catch (loadError) {
      setError(message(loadError));
    } finally {
      setAccessLoading(false);
    }
  }, []);

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

  const addMember = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedSpace || !candidateId || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await apiRequest(`/api/v1/spaces/${selectedSpace.id}/members/${candidateId}`, {
        body: { role: candidateRole },
        idempotent: true,
        method: "PUT",
      });
      await loadAccess(selectedSpace);
    } catch (updateError) {
      setError(message(updateError));
    } finally {
      setSaving(false);
    }
  };

  const changeRole = async (spaceMember: SpaceMember, role: SpaceMember["role"]) => {
    if (!selectedSpace || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await apiRequest(`/api/v1/spaces/${selectedSpace.id}/members/${spaceMember.member_id}`, {
        body: { role },
        idempotent: true,
        method: "PUT",
      });
      await loadAccess(selectedSpace);
    } catch (updateError) {
      setError(message(updateError));
    } finally {
      setSaving(false);
    }
  };

  const removeMember = async () => {
    if (!selectedSpace || !pendingRemoval || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await apiRequest(`/api/v1/spaces/${selectedSpace.id}/members/${pendingRemoval.member_id}`, {
        idempotent: true,
        method: "DELETE",
      });
      setPendingRemoval(null);
      await loadAccess(selectedSpace);
    } catch (removeError) {
      setError(message(removeError));
    } finally {
      setSaving(false);
    }
  };

  const archiveSelectedSpace = async () => {
    if (!selectedSpace || selectedSpace.id === "global" || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await apiRequest(`/api/v1/spaces/${selectedSpace.id}`, {
        idempotent: true,
        method: "DELETE",
      });
      setSelectedSpace(null);
      setConfirmSpaceArchive(false);
      await load();
    } catch (archiveError) {
      setError(message(archiveError));
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
                <div className="space-card-footer">
                  <p className="space-card-note"><ShieldCheck aria-hidden="true" size={14} /> Search access follows this membership</p>
                  {space.role === "owner" ? <button aria-label={`Manage access for ${space.name}`} className="space-manage-button" onClick={() => void loadAccess(space)} type="button"><UsersRound size={14} /> Manage access</button> : null}
                </div>
              </article>
            ))}
          </div>
          {accessLoading ? <div className="console-loading access-loading"><LoaderCircle className="spin" size={18} /> Loading space access…</div> : null}
          {selectedSpace && !accessLoading ? (
            <section className="space-access-panel" aria-label={`${selectedSpace.name} access`}>
              <div className="space-access-heading">
                <div><span>Owner controls</span><h2>{selectedSpace.name} access</h2></div>
                <div className="space-access-actions">
                  {selectedSpace.id !== "global" ? <button aria-label={`Archive ${selectedSpace.name}`} className="archive-button compact" onClick={() => setConfirmSpaceArchive(true)} type="button"><Archive size={14} /> Archive space</button> : null}
                  <button aria-label="Close access manager" className="icon-button" onClick={() => setSelectedSpace(null)} type="button"><X size={16} /></button>
                </div>
              </div>
              <form className="membership-add-form" onSubmit={addMember}>
                <div>
                  <label htmlFor="space-member-candidate">Family member</label>
                  <select disabled={memberCandidates.length === 0} id="space-member-candidate" onChange={(event) => setCandidateId(event.target.value)} value={candidateId}>
                    {memberCandidates.length === 0 ? <option value="">Everyone already has access</option> : null}
                    {memberCandidates.map((candidate) => <option key={candidate.member_id} value={candidate.member_id}>{candidate.display_name} (@{candidate.username})</option>)}
                  </select>
                </div>
                <div>
                  <label htmlFor="space-member-role">Role</label>
                  <select id="space-member-role" onChange={(event) => setCandidateRole(event.target.value as SpaceMember["role"])} value={candidateRole}>
                    <option value="reader">Reader</option>
                    <option value="editor">Editor</option>
                    <option value="owner">Owner</option>
                  </select>
                </div>
                <button className="primary-button" disabled={!candidateId || saving} type="submit"><UserPlus size={15} /> Add member</button>
              </form>
              <div className="membership-list">
                {spaceMembers.map((spaceMember) => (
                  <article className="membership-row" key={spaceMember.member_id}>
                    <span className="profile-avatar">{spaceMember.display_name.slice(0, 2).toUpperCase()}</span>
                    <div className="row-copy"><h3>{spaceMember.display_name}</h3><p>@{spaceMember.username}</p></div>
                    <label className="visually-hidden" htmlFor={`role-${spaceMember.member_id}`}>Role for {spaceMember.display_name}</label>
                    <select disabled={saving} id={`role-${spaceMember.member_id}`} onChange={(event) => void changeRole(spaceMember, event.target.value as SpaceMember["role"])} value={spaceMember.role}>
                      <option value="reader">Reader</option>
                      <option value="editor">Editor</option>
                      <option value="owner">Owner</option>
                    </select>
                    <button aria-label={`Remove ${spaceMember.display_name}`} className="membership-remove-button" disabled={saving} onClick={() => setPendingRemoval(spaceMember)} type="button"><UserMinus size={15} /></button>
                  </article>
                ))}
              </div>
              {pendingRemoval ? (
                <div className="confirmation-strip" role="alertdialog" aria-label={`Remove ${pendingRemoval.display_name} from ${selectedSpace.name}`}>
                  <div><strong>Remove {pendingRemoval.display_name}?</strong><span>They will immediately lose access to this space and its search results.</span></div>
                  <button className="secondary-button" onClick={() => setPendingRemoval(null)} type="button">Keep member</button>
                  <button className="danger-button" disabled={saving} onClick={() => void removeMember()} type="button">Confirm removal</button>
                </div>
              ) : null}
              {confirmSpaceArchive ? <div className="confirmation-strip" role="alertdialog" aria-label={`Archive ${selectedSpace.name}`}><div><strong>Archive {selectedSpace.name}?</strong><span>Its knowledge will leave unified search immediately, while history remains preserved.</span></div><button className="secondary-button" onClick={() => setConfirmSpaceArchive(false)} type="button">Keep space</button><button className="danger-button" disabled={saving} onClick={() => void archiveSelectedSpace()} type="button">Confirm archive space</button></div> : null}
            </section>
          ) : null}
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
  const [editing, setEditing] = useState<KnowledgeDetail | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [editContent, setEditContent] = useState("");
  const [editTags, setEditTags] = useState("");
  const [confirmArchive, setConfirmArchive] = useState(false);
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

  const openEditor = async (item: KnowledgeSummary) => {
    setLoading(true);
    setError(null);
    try {
      const detail = await apiRequest<KnowledgeDetail>(`/api/v1/knowledge/${item.id}`);
      setEditing(detail);
      setEditTitle(detail.title);
      setEditContent(detail.content);
      setEditTags(detail.tags.join(", "));
      setConfirmArchive(false);
    } catch (loadError) {
      setError(message(loadError));
    } finally {
      setLoading(false);
    }
  };

  const saveRevision = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!editing || !editTitle.trim() || !editContent.trim() || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const updated = await apiRequest<KnowledgeDetail>(`/api/v1/knowledge/${editing.id}`, {
        body: {
          change_summary: "Updated through the management console",
          content: editContent.trim(),
          expected_version: editing.version,
          tags: editTags.split(",").map((value) => value.trim()).filter(Boolean),
          title: editTitle.trim(),
        },
        idempotent: true,
        method: "PUT",
      });
      setEditing(updated);
      setEditTitle(updated.title);
      setEditContent(updated.content);
      setEditTags(updated.tags.join(", "));
      await loadItems();
    } catch (updateError) {
      setError(message(updateError));
    } finally {
      setSaving(false);
    }
  };

  const archiveKnowledge = async () => {
    if (!editing || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await apiRequest(`/api/v1/knowledge/${editing.id}?expected_version=${editing.version}`, {
        idempotent: true,
        method: "DELETE",
      });
      setEditing(null);
      setConfirmArchive(false);
      await loadItems();
    } catch (archiveError) {
      setError(message(archiveError));
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
                <button aria-label={`Edit ${item.title}`} className="row-action-button" onClick={() => void openEditor(item)} type="button">Edit</button>
              </article>
            ))}
          </div>
          {editing ? (
            <section className="knowledge-editor" aria-label={`Edit ${editing.title}`}>
              <div className="space-access-heading">
                <div><span>Immutable revision</span><h2>Edit knowledge</h2></div>
                <button aria-label="Close knowledge editor" className="icon-button" onClick={() => setEditing(null)} type="button"><X size={16} /></button>
              </div>
              <p className="revision-state">Version {editing.version} is active</p>
              <form className="console-form" onSubmit={saveRevision}>
                <label htmlFor="edit-knowledge-title">Edit title</label>
                <input id="edit-knowledge-title" maxLength={500} onChange={(event) => setEditTitle(event.target.value)} required value={editTitle} />
                <label htmlFor="edit-knowledge-content">Edit content</label>
                <textarea id="edit-knowledge-content" maxLength={1000000} onChange={(event) => setEditContent(event.target.value)} required rows={7} value={editContent} />
                <label htmlFor="edit-knowledge-tags">Edit tags</label>
                <input id="edit-knowledge-tags" onChange={(event) => setEditTags(event.target.value)} value={editTags} />
                <div className="editor-actions">
                  <button className="primary-button" disabled={saving} type="submit"><Save size={15} /> Save revision</button>
                  <button aria-label={`Archive ${editing.title}`} className="archive-button" disabled={saving} onClick={() => setConfirmArchive(true)} type="button"><Archive size={15} /> Archive</button>
                </div>
              </form>
              {confirmArchive ? (
                <div className="confirmation-strip" role="alertdialog" aria-label={`Archive ${editing.title}`}>
                  <div><strong>Archive {editing.title}?</strong><span>It will leave default retrieval but its revision history remains preserved.</span></div>
                  <button className="secondary-button" onClick={() => setConfirmArchive(false)} type="button">Keep active</button>
                  <button className="danger-button" disabled={saving} onClick={() => void archiveKnowledge()} type="button">Confirm archive</button>
                </div>
              ) : null}
            </section>
          ) : null}
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
  const [selectedMember, setSelectedMember] = useState<MemberSummary | null>(null);
  const [pendingMemberAction, setPendingMemberAction] = useState<"disable" | "enable" | "reset" | null>(null);
  const [resetPassword, setResetPassword] = useState<ResetMemberPassword | null>(null);
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

  const updateSelectedMember = async (status = selectedMember?.status) => {
    if (!selectedMember || !status || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const response = await apiRequest<MemberSummary>(`/api/v1/members/${selectedMember.id}`, {
        body: {
          display_name: selectedMember.display_name,
          status,
          system_role: selectedMember.system_role,
        },
        idempotent: true,
        method: "PATCH",
      });
      setSelectedMember(response);
      setPendingMemberAction(null);
      await loadMembers();
    } catch (updateError) {
      setError(message(updateError));
    } finally {
      setSaving(false);
    }
  };

  const resetSelectedMemberPassword = async () => {
    if (!selectedMember || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const response = await apiRequest<ResetMemberPassword>(`/api/v1/members/${selectedMember.id}/password-reset`, {
        idempotent: true,
        method: "POST",
      });
      setResetPassword(response);
      setPendingMemberAction(null);
      setSelectedMember({ ...selectedMember, requires_password_change: true });
      await loadMembers();
    } catch (resetError) {
      setError(message(resetError));
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
                  <button aria-label={`Manage ${person.display_name}`} className="row-action-button" onClick={() => { setSelectedMember(person); setPendingMemberAction(null); setResetPassword(null); }} type="button"><Settings2 size={14} /> Manage</button>
                </article>
              ))}
            </div>
            {selectedMember ? (
              <section aria-label={`Manage ${selectedMember.display_name}`} className="member-admin-panel">
                <div className="space-access-heading"><div><span>Account controls</span><h2>{selectedMember.display_name}</h2></div><button aria-label="Close member manager" className="icon-button" onClick={() => setSelectedMember(null)} type="button"><X size={16} /></button></div>
                <div className="member-admin-form">
                  <div><label htmlFor="member-admin-display-name">Display name</label><input id="member-admin-display-name" maxLength={255} onChange={(event) => setSelectedMember({ ...selectedMember, display_name: event.target.value })} value={selectedMember.display_name} /></div>
                  <div><label htmlFor="member-admin-system-role">System role</label><select id="member-admin-system-role" onChange={(event) => setSelectedMember({ ...selectedMember, system_role: event.target.value })} value={selectedMember.system_role}><option value="member">Member</option><option value="super_admin">Super admin</option></select></div>
                  <button className="secondary-button" disabled={saving} onClick={() => void updateSelectedMember()} type="button"><Save size={14} /> Save account</button>
                </div>
                <div className="member-security-actions">
                  <button className="secondary-button" disabled={saving} onClick={() => { setResetPassword(null); setPendingMemberAction("reset"); }} type="button"><KeyRound size={14} /> Reset {selectedMember.display_name} password</button>
                  {selectedMember.status === "disabled" ? <button className="secondary-button" disabled={saving} onClick={() => setPendingMemberAction("enable")} type="button">Enable {selectedMember.display_name}</button> : <button className="archive-button compact" disabled={saving} onClick={() => setPendingMemberAction("disable")} type="button">Disable {selectedMember.display_name}</button>}
                </div>
                {pendingMemberAction === "reset" ? <div aria-label={`Reset ${selectedMember.display_name} password`} className="confirmation-strip" role="alertdialog"><div><strong>Reset this password?</strong><span>This signs out every session, revokes active API keys, and creates a one-time password.</span></div><button className="secondary-button" onClick={() => setPendingMemberAction(null)} type="button">Keep password</button><button className="danger-button" disabled={saving} onClick={() => void resetSelectedMemberPassword()} type="button">Confirm password reset</button></div> : null}
                {pendingMemberAction === "disable" ? <div aria-label={`Disable ${selectedMember.display_name}`} className="confirmation-strip" role="alertdialog"><div><strong>Disable this member?</strong><span>This immediately revokes sessions and API keys. Space history remains preserved.</span></div><button className="secondary-button" onClick={() => setPendingMemberAction(null)} type="button">Keep active</button><button className="danger-button" disabled={saving} onClick={() => void updateSelectedMember("disabled")} type="button">Confirm disable member</button></div> : null}
                {pendingMemberAction === "enable" ? <div aria-label={`Enable ${selectedMember.display_name}`} className="confirmation-strip" role="alertdialog"><div><strong>Enable this member?</strong><span>The member can authenticate again, but revoked sessions and keys remain revoked.</span></div><button className="secondary-button" onClick={() => setPendingMemberAction(null)} type="button">Keep disabled</button><button className="primary-button" disabled={saving} onClick={() => void updateSelectedMember("active")} type="button">Confirm enable member</button></div> : null}
                {resetPassword ? <div className="member-reset-secret"><p>This temporary password is shown only once.</p><div className="secret-value"><code>{resetPassword.temporary_password}</code><button aria-label="Copy reset password" onClick={() => navigator.clipboard?.writeText(resetPassword.temporary_password)} type="button"><Copy size={16} /></button></div></div> : null}
              </section>
            ) : null}
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
  const [runtimeDraft, setRuntimeDraft] = useState<RuntimeSettings | null>(null);
  const [runtimeLimit, setRuntimeLimit] = useState(20);
  const [runtimeReason, setRuntimeReason] = useState("");
  const [keyName, setKeyName] = useState("");
  const [createdKey, setCreatedKey] = useState<CreatedAPIKey | null>(null);
  const [pendingKeyRevocation, setPendingKeyRevocation] = useState<APIKeySummary | null>(null);
  const [pendingSessionRevocation, setPendingSessionRevocation] = useState<SessionSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadKeys = useCallback(async () => {
    const response = await apiRequest<APIKeyListResponse>("/api/v1/api-keys");
    setKeys(response.items);
  }, []);

  const loadSessions = useCallback(async () => {
    const response = await apiRequest<SessionListResponse>("/api/v1/sessions");
    setSessions(response.items);
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
      setRuntimeLimit(runtimeResponse.values.retrieval.limit);
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

  const revokeKey = async () => {
    if (!pendingKeyRevocation || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await apiRequest(`/api/v1/api-keys/${pendingKeyRevocation.id}`, {
        idempotent: true,
        method: "DELETE",
      });
      setPendingKeyRevocation(null);
      await loadKeys();
    } catch (revokeError) {
      setError(message(revokeError));
    } finally {
      setSaving(false);
    }
  };

  const revokeSession = async () => {
    if (!pendingSessionRevocation || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await apiRequest(`/api/v1/sessions/${pendingSessionRevocation.id}`, {
        idempotent: true,
        method: "DELETE",
      });
      setPendingSessionRevocation(null);
      await loadSessions();
    } catch (revokeError) {
      setError(message(revokeError));
    } finally {
      setSaving(false);
    }
  };

  const createRuntimeDraft = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!settings || runtimeReason.trim().length < 5 || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const draft = await apiRequest<RuntimeSettings>("/api/v1/settings/drafts", {
        body: {
          base_revision: settings.revision,
          reason: runtimeReason.trim(),
          values: {
            ...settings.values,
            retrieval: { ...settings.values.retrieval, limit: runtimeLimit },
          },
        },
        idempotent: true,
        method: "POST",
      });
      setRuntimeDraft(draft);
    } catch (draftError) {
      setError(message(draftError));
    } finally {
      setSaving(false);
    }
  };

  const activateRuntimeDraft = async () => {
    if (!settings || !runtimeDraft?.id || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const activated = await apiRequest<RuntimeSettings>(`/api/v1/settings/drafts/${runtimeDraft.id}/activate`, {
        body: {
          expected_active_revision: settings.revision,
          reason: runtimeReason.trim(),
        },
        idempotent: true,
        method: "POST",
      });
      setSettings(activated);
      setRuntimeLimit(activated.values.retrieval.limit);
      setRuntimeDraft(null);
    } catch (activationError) {
      setError(message(activationError));
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
            {keys.map((key) => <article className="data-row" key={key.id}><span className="row-leading violet"><KeyRound size={17} /></span><div className="row-copy"><h3>{key.name}</h3><p>{key.public_id} · {key.scopes.join(", ")}</p></div><span className={`status-pill status-${key.status}`}>{key.status}</span>{key.status === "active" ? <button aria-label={`Revoke ${key.name}`} className="membership-remove-button" onClick={() => setPendingKeyRevocation(key)} type="button"><X size={15} /></button> : null}</article>)}
          </div>
          {pendingKeyRevocation ? <div className="confirmation-strip" role="alertdialog" aria-label={`Revoke ${pendingKeyRevocation.name}`}><div><strong>Revoke {pendingKeyRevocation.name}?</strong><span>Clients using this key will lose access immediately.</span></div><button className="secondary-button" onClick={() => setPendingKeyRevocation(null)} type="button">Keep key</button><button className="danger-button" disabled={saving} onClick={() => void revokeKey()} type="button">Confirm revoke API key</button></div> : null}
        </section>

        <section className="console-panel settings-section">
          <div className="panel-heading"><div><span>Website access</span><h2>Sessions</h2></div><MonitorSmartphone size={20} /></div>
          <div className="data-list compact-list">
            {sessions.map((session) => <article className="data-row" key={session.id}><span className="row-leading cyan"><MonitorSmartphone size={17} /></span><div className="row-copy"><h3>{session.current ? "This session" : "Website session"}</h3><p>Last active {new Date(session.last_activity_at).toLocaleString()}</p></div><span className={`status-pill status-${session.status}`}>{session.status}</span>{!session.current && session.status === "active" ? <button aria-label="Sign out website session" className="membership-remove-button" onClick={() => setPendingSessionRevocation(session)} type="button"><X size={15} /></button> : null}</article>)}
            {sessions.length === 0 ? <div className="console-empty small"><MonitorSmartphone size={20} /><strong>No session records returned</strong></div> : null}
          </div>
          {pendingSessionRevocation ? <div className="confirmation-strip" role="alertdialog" aria-label="Sign out website session"><div><strong>Sign out this device?</strong><span>The selected session and all of its credentials will be revoked.</span></div><button className="secondary-button" onClick={() => setPendingSessionRevocation(null)} type="button">Keep signed in</button><button className="danger-button" disabled={saving} onClick={() => void revokeSession()} type="button">Confirm sign out</button></div> : null}
        </section>

        <section className="console-panel settings-section runtime-section">
          <div className="panel-heading"><div><span>Production boundary</span><h2>Safe runtime settings</h2></div><Settings2 size={20} /></div>
          <div className="runtime-summary"><div><span>Active revision</span><strong>{settings?.revision ?? 0}</strong></div><div><span>State</span><strong>{settings?.state || "active"}</strong></div><div><span>Change mode</span><strong>{member.system_role === "super_admin" ? "Draft + activate" : "Read only"}</strong></div></div>
          {settings ? <p className="runtime-active-state">Revision {settings.revision} is active</p> : null}
          {member.system_role === "super_admin" && settings ? (
            <form className="runtime-settings-form" onSubmit={createRuntimeDraft}>
              <div><label htmlFor="runtime-retrieval-limit">Retrieval result limit</label><input id="runtime-retrieval-limit" max={100} min={1} onChange={(event) => setRuntimeLimit(Number(event.target.value))} required type="number" value={runtimeLimit} /></div>
              <div><label htmlFor="runtime-change-reason">Change reason</label><input id="runtime-change-reason" maxLength={500} minLength={5} onChange={(event) => setRuntimeReason(event.target.value)} required value={runtimeReason} /></div>
              <button className="primary-button" disabled={saving || runtimeReason.trim().length < 5} type="submit">Create validated draft</button>
            </form>
          ) : null}
          {runtimeDraft ? <div className="runtime-draft-review"><div><strong>Draft revision {runtimeDraft.revision} ready</strong><span>Validated against the typed safe-setting schema. Activation remains a separate audited step.</span></div><button className="primary-button" disabled={saving} onClick={() => void activateRuntimeDraft()} type="button">Activate settings</button></div> : null}
          <pre>{JSON.stringify(runtimeDraft?.values || settings?.values || {}, null, 2)}</pre>
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
