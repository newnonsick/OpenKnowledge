"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { Activity, Archive, ArrowUpRight, BookOpen, CircleAlert, Code2, FileText, FileUp, FolderKanban, KeyRound, Layers3, LoaderCircle, LockKeyhole, MonitorSmartphone, Plus, Save, Search, Settings2, ShieldCheck, Sparkles, Tag, UserMinus, UserPlus, UsersRound, X } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";

import { useCurrentMember } from "@/components/auth/session-gate";
import { ConsoleShell } from "@/components/console-shell";
import { CopyButton } from "@/components/copy-button";
import { PaginationControls } from "@/components/pagination-controls";
import { ApiError, apiMultipart, contractClient, contractData, idempotencyKey } from "@/lib/api-client";
import type { components } from "@/lib/generated/openapi";
import { usePagePagination } from "@/lib/use-page-pagination";
import { loadAccessibleSpaces } from "@/lib/space-options";

type Space = components["schemas"]["SpaceSummary"];
type SpaceListResponse = components["schemas"]["Page_SpaceSummary_"];
type AdminSpace = components["schemas"]["AdminSpaceSummary"];
type SpaceMember = components["schemas"]["SpaceMember"];
type SpaceMemberCandidate = components["schemas"]["SpaceMemberCandidate"];
type SpaceMemberListResponse = components["schemas"]["Page_SpaceMember_"];
type SpaceMemberCandidateListResponse = components["schemas"]["Page_SpaceMemberCandidate_"];
type KnowledgeSummary = components["schemas"]["KnowledgeSummary"];
type KnowledgeListResponse = components["schemas"]["Page_KnowledgeSummary_"];
type KnowledgeDetail = components["schemas"]["KnowledgeDetail"];
type RetrievalResponse = components["schemas"]["RetrievalResult"];
type SourceSummary = components["schemas"]["SourceSummary"];
type SourceListResponse = components["schemas"]["Page_SourceSummary_"];
type MemberSummary = components["schemas"]["MemberSummary"];
type MemberListResponse = components["schemas"]["Page_MemberSummary_"];
type CreatedMember = components["schemas"]["CreatedMember"];
type ResetMemberPassword = components["schemas"]["ResetMemberPassword"];
type APIKeySummary = components["schemas"]["APIKeySummary"];
type APIKeyListResponse = components["schemas"]["Page_APIKeySummary_"];
type CreatedAPIKey = components["schemas"]["CreatedAPIKey"];

const API_KEY_SCOPE_OPTIONS = [
  { description: "Search and read knowledge you can access", label: "Read knowledge", value: "knowledge:read" },
  { description: "Create and edit knowledge in writable spaces", label: "Write knowledge", value: "knowledge:write" },
  { description: "Use model and assistant endpoints", label: "Use AI APIs", value: "chat:write" },
  { description: "List spaces available to this member", label: "Read spaces", value: "spaces:read" },
  { description: "Create and archive owned spaces", label: "Write spaces", value: "spaces:write" },
  { description: "Manage access in owned spaces", label: "Manage space access", value: "spaces:members" },
  { description: "Inspect safe runtime settings", label: "Read settings", value: "settings:read" },
  { description: "Propose safe runtime setting drafts", label: "Write settings", superAdminOnly: true, value: "settings:write" },
] as const;

const DEFAULT_API_KEY_SCOPES = ["knowledge:read"];

type SessionSummary = components["schemas"]["SessionSummary"];
type SessionListResponse = components["schemas"]["Page_SessionSummary_"];
type RuntimeSettings = components["schemas"]["RuntimeSettings"];
type RuntimeSettingsHistoryResponse = components["schemas"]["Page_RuntimeSettings_"];
type IngestionJob = components["schemas"]["IngestionJob"];
type IngestionListResponse = components["schemas"]["Page_IngestionJob_"];
type AuditEvent = components["schemas"]["AuditEvent"];
type AuditListResponse = components["schemas"]["Page_AuditEvent_"];

const dateTimeFormat = new Intl.DateTimeFormat(undefined, { day: "numeric", hour: "2-digit", minute: "2-digit", month: "short" });

function formatDateTime(value: string) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : dateTimeFormat.format(parsed);
}

function memberView(member: ReturnType<typeof useCurrentMember>) {
  return {
    displayName: member.display_name,
    role: member.system_role === "super_admin" ? "Super admin" : "Member",
    systemRole: member.system_role,
  };
}

function message(error: unknown) {
  if (error instanceof ApiError) {
    if (error.code === "recent_authentication_required") {
      return "Verify your identity, then run that action again.";
    }
    return error.message;
  }
  return "The request could not be completed.";
}

function spaceLabel(spaces: Space[], spaceId: string) {
  return spaces.find((space) => space.id === spaceId)?.name ?? spaceId;
}

export function ListSkeleton({ compact = false, rows = 5 }: { compact?: boolean; rows?: number }) {
  return (
    <div aria-hidden="true" className={`list-skeleton${compact ? " compact" : ""}`}>
      {Array.from({ length: rows }).map((_, index) => (
        <div className="skeleton-row" key={index}>
          <span className="skeleton-glyph" />
          <span className="skeleton-lines"><i /><i className="short" /></span>
          <span className="skeleton-pill" />
        </div>
      ))}
    </div>
  );
}

export function ListUnavailable({ label, onRetry }: { label: string; onRetry: () => void }) {
  return (
    <div className="console-unavailable" role="alert">
      <CircleAlert aria-hidden="true" size={22} />
      <div><strong>Unable to load {label}</strong><span>Check your connection and try again.</span></div>
      <button aria-label={`Retry ${label}`} className="secondary-button" onClick={onRetry} type="button">Retry</button>
    </div>
  );
}

export function SpacesConsole() {
  const member = useCurrentMember();
  const [spaceSearch, setSpaceSearch] = useState("");
  const [appliedSpaceSearch, setAppliedSpaceSearch] = useState("");
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [selectedSpace, setSelectedSpace] = useState<Space | null>(null);
  const [memberSearch, setMemberSearch] = useState("");
  const [appliedMemberSearch, setAppliedMemberSearch] = useState("");
  const [memberRoleFilter, setMemberRoleFilter] = useState<"" | "owner" | "editor" | "reader">("");
  const [candidateSearch, setCandidateSearch] = useState("");
  const [appliedCandidateSearch, setAppliedCandidateSearch] = useState("");
  const [candidateId, setCandidateId] = useState("");
  const [candidateRole, setCandidateRole] = useState<"editor" | "reader">("reader");
  const [pendingRemoval, setPendingRemoval] = useState<SpaceMember | null>(null);
  const [pendingOwnership, setPendingOwnership] = useState<SpaceMember | null>(null);
  const [confirmSpaceArchive, setConfirmSpaceArchive] = useState(false);
  const [createdSpaceName, setCreatedSpaceName] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const accessPanelRef = useRef<HTMLElement>(null);
  const scrolledSpaceRef = useRef<string | null>(null);

  const loadSpacePage = useCallback((page: number, signal: AbortSignal) => contractData(contractClient.GET("/api/v1/spaces", { params: { query: { page, page_size: 25, ...(appliedSpaceSearch ? { q: appliedSpaceSearch } : {}) } }, signal })), [appliedSpaceSearch]);
  const spacePages = usePagePagination<Space>({ loadPage: loadSpacePage, queryKey: appliedSpaceSearch });
  const spaces = spacePages.items;
  const loadMemberPage = useCallback((page: number, signal: AbortSignal) => selectedSpace
    ? contractData(contractClient.GET("/api/v1/spaces/{space_id}/members", { params: { path: { space_id: selectedSpace.id }, query: { page, page_size: 25, ...(appliedMemberSearch ? { q: appliedMemberSearch } : {}), ...(memberRoleFilter ? { role: memberRoleFilter } : {}) } }, signal }))
    : Promise.resolve({ items: [], page: 1, page_size: 25, total_items: 0, total_pages: 0 }), [appliedMemberSearch, memberRoleFilter, selectedSpace]);
  const memberPages = usePagePagination<SpaceMember>({ loadPage: loadMemberPage, queryKey: JSON.stringify([selectedSpace?.id, appliedMemberSearch, memberRoleFilter]) });
  const spaceMembers = memberPages.items;
  const loadCandidatePage = useCallback((page: number, signal: AbortSignal) => selectedSpace
    ? contractData(contractClient.GET("/api/v1/spaces/{space_id}/member-candidates", { params: { path: { space_id: selectedSpace.id }, query: { page, page_size: 25, ...(appliedCandidateSearch ? { q: appliedCandidateSearch } : {}) } }, signal }))
    : Promise.resolve({ items: [], page: 1, page_size: 25, total_items: 0, total_pages: 0 }), [appliedCandidateSearch, selectedSpace]);
  const candidatePages = usePagePagination<SpaceMemberCandidate>({ loadPage: loadCandidatePage, queryKey: JSON.stringify([selectedSpace?.id, appliedCandidateSearch]) });
  const memberCandidates = candidatePages.items;
  const selectedCandidateId = memberCandidates.some((candidate) => candidate.member_id === candidateId) ? candidateId : memberCandidates[0]?.member_id || "";
  const accessLoading = selectedSpace !== null && (memberPages.initialLoading || candidatePages.initialLoading || (memberPages.loading && spaceMembers.length === 0) || (candidatePages.loading && memberCandidates.length === 0));

  useEffect(() => {
    if (!selectedSpace) {
      scrolledSpaceRef.current = null;
      return;
    }
    if (scrolledSpaceRef.current === selectedSpace.id || accessLoading) {
      return;
    }
    const node = accessPanelRef.current;
    if (!node) {
      return;
    }
    scrolledSpaceRef.current = selectedSpace.id;
    node.scrollIntoView({ block: "nearest" });
  }, [selectedSpace, accessLoading]);

  useEffect(() => {
    setCandidateId((current) => memberCandidates.some((candidate) => candidate.member_id === current) ? current : memberCandidates[0]?.member_id || "");
  }, [memberCandidates]);

  const loadAccess = useCallback((space: Space) => {
    setSelectedSpace(space);
    setMemberSearch("");
    setAppliedMemberSearch("");
    setMemberRoleFilter("");
    setCandidateSearch("");
    setAppliedCandidateSearch("");
    setConfirmSpaceArchive(false);
    setError(null);
  }, []);

  const refreshSelectedSpace = useCallback(async () => {
    if (!selectedSpace) {
      return;
    }
    try {
      const response = await contractData(contractClient.GET("/api/v1/spaces", { params: { query: { page: 1, page_size: 5, q: selectedSpace.id } } }));
      const updated = response.items.find((space) => space.id === selectedSpace.id);
      if (updated) {
        setSelectedSpace(updated);
      }
    } catch {
      return;
    }
  }, [selectedSpace]);

  const create = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const value = name.trim();
    if (!value || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await contractData(contractClient.POST("/api/v1/spaces", { body: { name: value }, params: { header: { "Idempotency-Key": idempotencyKey() } } }));
      setCreatedSpaceName(value);
      setName("");
      if (appliedSpaceSearch) {
        setSpaceSearch("");
        setAppliedSpaceSearch("");
      }
      await spacePages.goToPage(1);
    } catch (createError) {
      setError(message(createError));
    } finally {
      setSaving(false);
    }
  };

  const addMember = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedSpace || !selectedCandidateId || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await contractData(contractClient.PUT("/api/v1/spaces/{space_id}/members/{member_id}", {
        body: { role: candidateRole },
        params: { header: { "Idempotency-Key": idempotencyKey() }, path: { member_id: selectedCandidateId, space_id: selectedSpace.id } },
      }));
      memberPages.reload();
      candidatePages.reload();
      await refreshSelectedSpace();
    } catch (updateError) {
      setError(message(updateError));
    } finally {
      setSaving(false);
    }
  };

  const changeRole = async (spaceMember: SpaceMember, role: "editor" | "reader") => {
    if (!selectedSpace || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await contractData(contractClient.PUT("/api/v1/spaces/{space_id}/members/{member_id}", {
        body: { role },
        params: { header: { "Idempotency-Key": idempotencyKey() }, path: { member_id: spaceMember.member_id, space_id: selectedSpace.id } },
      }));
      memberPages.reload();
      await refreshSelectedSpace();
    } catch (updateError) {
      setError(message(updateError));
    } finally {
      setSaving(false);
    }
  };

  const transferOwnership = async () => {
    if (!selectedSpace || !pendingOwnership || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await contractData(contractClient.PUT("/api/v1/spaces/{space_id}/ownership", {
        body: { expected_revision: selectedSpace.revision, target_member_id: pendingOwnership.member_id },
        params: { header: { "Idempotency-Key": idempotencyKey() }, path: { space_id: selectedSpace.id } },
      }));
      setPendingOwnership(null);
      setSelectedSpace(null);
      spacePages.reload();
    } catch (transferError) {
      setError(message(transferError));
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
      await contractData(contractClient.DELETE("/api/v1/spaces/{space_id}/members/{member_id}", {
        params: { header: { "Idempotency-Key": idempotencyKey() }, path: { member_id: pendingRemoval.member_id, space_id: selectedSpace.id } },
      }));
      setPendingRemoval(null);
      memberPages.reload();
      candidatePages.reload();
      await refreshSelectedSpace();
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
      await contractData(contractClient.DELETE("/api/v1/spaces/{space_id}", {
        params: { header: { "Idempotency-Key": idempotencyKey() }, path: { space_id: selectedSpace.id } },
      }));
      setSelectedSpace(null);
      setConfirmSpaceArchive(false);
      spacePages.reload();
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
      spaceCount={spacePages.initialLoading ? null : spacePages.totalItems}
      title="Spaces"
    >
      <div className="console-grid console-grid-spaces">
        <section className="console-panel">
          <div className="panel-heading">
            <div><span>Accessible now</span><h2>Your spaces</h2></div>
            <span className="count-pill">{spacePages.initialLoading ? "…" : spacePages.totalItems}</span>
          </div>
          <form className="list-filter-bar space-filter-bar" onSubmit={(event) => { event.preventDefault(); setAppliedSpaceSearch(spaceSearch.trim()); }} role="search"><label><Search aria-hidden="true" size={15} /><input aria-label="Search spaces" onChange={(event) => setSpaceSearch(event.target.value)} placeholder="Search name or ID" type="search" value={spaceSearch} /></label><button className="secondary-button" type="submit">Apply</button>{appliedSpaceSearch ? <button className="filter-clear-button" onClick={() => { setSpaceSearch(""); setAppliedSpaceSearch(""); }} type="button">Clear</button> : null}</form>
          {selectedSpace && !accessLoading ? (
            <section className="space-access-panel" aria-label={`${selectedSpace.name} access`} ref={accessPanelRef}>
              <div className="space-access-heading">
                <div><span>Owner controls</span><h2>{selectedSpace.name} access</h2></div>
                <div className="space-access-actions">
                  {selectedSpace.id !== "global" ? <button aria-label={`Archive ${selectedSpace.name}`} className="archive-button compact" onClick={() => setConfirmSpaceArchive(true)} type="button"><Archive size={14} /> Archive space</button> : null}
                  <button aria-label="Close access manager" className="icon-button" onClick={() => setSelectedSpace(null)} type="button"><X size={16} /></button>
                </div>
              </div>
              <form className="list-filter-bar access-filter-bar" onSubmit={(event) => { event.preventDefault(); setAppliedCandidateSearch(candidateSearch.trim()); }} role="search"><label><Search aria-hidden="true" size={14} /><input aria-label="Find member to add" onChange={(event) => setCandidateSearch(event.target.value)} placeholder="Find a member to add" type="search" value={candidateSearch} /></label><button className="secondary-button" type="submit">Find member</button></form>
              <form className="membership-add-form" onSubmit={addMember}>
                <div>
                  <label htmlFor="space-member-candidate">Family member</label>
                  <select disabled={memberCandidates.length === 0 || Boolean(candidatePages.error)} id="space-member-candidate" onChange={(event) => setCandidateId(event.target.value)} value={selectedCandidateId}>
                    {candidatePages.error ? <option value="">Members are unavailable</option> : null}
                    {!candidatePages.error && memberCandidates.length === 0 ? <option value="">Everyone already has access</option> : null}
                    {memberCandidates.map((candidate) => <option key={candidate.member_id} value={candidate.member_id}>{candidate.display_name} (@{candidate.username})</option>)}
                  </select>
                </div>
                <div>
                  <label htmlFor="space-member-role">Role</label>
                  <select id="space-member-role" onChange={(event) => setCandidateRole(event.target.value as "editor" | "reader")} value={candidateRole}>
                    <option value="reader">Reader</option>
                    <option value="editor">Editor</option>
                  </select>
                </div>
                <button className="primary-button" disabled={!selectedCandidateId || saving} type="submit"><UserPlus size={15} /> Add member</button>
              </form>
              <form className="list-filter-bar access-member-filter" onSubmit={(event) => { event.preventDefault(); setAppliedMemberSearch(memberSearch.trim()); }} role="search"><label><Search aria-hidden="true" size={14} /><input aria-label="Search space members" onChange={(event) => setMemberSearch(event.target.value)} placeholder="Search current members" type="search" value={memberSearch} /></label><label><select aria-label="Filter space member role" onChange={(event) => setMemberRoleFilter(event.target.value as typeof memberRoleFilter)} value={memberRoleFilter}><option value="">All roles</option><option value="owner">Owner</option><option value="editor">Editor</option><option value="reader">Reader</option></select></label><button className="secondary-button" type="submit">Apply</button></form>
              <div className="membership-list">
                {spaceMembers.map((spaceMember) => (
                  <article className="membership-row" key={spaceMember.member_id}>
                    <span className="profile-avatar">{spaceMember.display_name.slice(0, 2).toUpperCase()}</span>
                    <div className="row-copy"><h3>{spaceMember.display_name}</h3><p>@{spaceMember.username}</p></div>
                    {spaceMember.role === "owner" ? <span className="role-pill role-owner">owner</span> : <><label className="visually-hidden" htmlFor={`role-${spaceMember.member_id}`}>Role for {spaceMember.display_name}</label><select disabled={saving} id={`role-${spaceMember.member_id}`} onChange={(event) => void changeRole(spaceMember, event.target.value as "editor" | "reader")} value={spaceMember.role}><option value="reader">Reader</option><option value="editor">Editor</option></select><button aria-label={`Transfer ownership to ${spaceMember.display_name}`} className="ownership-transfer-button" disabled={saving} onClick={() => setPendingOwnership(spaceMember)} type="button"><KeyRound size={14} /></button></>}
                    <button aria-label={`Remove ${spaceMember.display_name}`} className="membership-remove-button" disabled={saving} onClick={() => setPendingRemoval(spaceMember)} type="button"><UserMinus size={15} /></button>
                  </article>
                ))}
              </div>
              {memberPages.error || candidatePages.error ? <ListUnavailable label="space access" onRetry={() => { void memberPages.reload(); void candidatePages.reload(); }} /> : null}
              {memberPages.totalItems > 0 ? <PaginationControls loading={memberPages.loading} loadingPage={memberPages.loadingPage} onPageChange={(nextPage) => void memberPages.goToPage(nextPage)} page={memberPages.page} pageSize={memberPages.pageSize} totalItems={memberPages.totalItems} totalPages={memberPages.totalPages} /> : null}
              {candidatePages.totalItems > 0 ? <PaginationControls loading={candidatePages.loading} loadingPage={candidatePages.loadingPage} onPageChange={(nextPage) => void candidatePages.goToPage(nextPage)} page={candidatePages.page} pageSize={candidatePages.pageSize} totalItems={candidatePages.totalItems} totalPages={candidatePages.totalPages} /> : null}
              {pendingRemoval ? (
                <div className="confirmation-strip" role="alertdialog" aria-label={`Remove ${pendingRemoval.display_name} from ${selectedSpace.name}`}>
                  <div><strong>Remove {pendingRemoval.display_name}?</strong><span>They will immediately lose access to this space and its search results.</span></div>
                  <button className="secondary-button" onClick={() => setPendingRemoval(null)} type="button">Keep member</button>
                  <button className="danger-button" disabled={saving} onClick={() => void removeMember()} type="button">Confirm removal</button>
                </div>
              ) : null}
              {pendingOwnership ? <div className="confirmation-strip" role="alertdialog" aria-label={`Transfer ownership to ${pendingOwnership.display_name}`}><div><strong>Transfer this space?</strong><span>You will become an editor. A recent identity verification is required, and the blocked action is never replayed automatically.</span></div><button className="secondary-button" onClick={() => setPendingOwnership(null)} type="button">Keep ownership</button><button className="danger-button" disabled={saving} onClick={() => void transferOwnership()} type="button">Confirm ownership transfer</button></div> : null}
              {confirmSpaceArchive ? <div className="confirmation-strip" role="alertdialog" aria-label={`Archive ${selectedSpace.name}`}><div><strong>Archive {selectedSpace.name}?</strong><span>Its knowledge will leave unified search immediately, while history remains preserved.</span></div><button className="secondary-button" onClick={() => setConfirmSpaceArchive(false)} type="button">Keep space</button><button className="danger-button" disabled={saving} onClick={() => void archiveSelectedSpace()} type="button">Confirm archive space</button></div> : null}
            </section>
          ) : null}
          {accessLoading ? <div className="console-loading access-loading"><LoaderCircle className="spin" size={18} /> Loading space access…</div> : null}
          {spacePages.initialLoading || (spacePages.loading && spaces.length === 0) ? <ListSkeleton /> : null}
          {spacePages.error ? <ListUnavailable label="spaces" onRetry={() => void spacePages.reload()} /> : null}
          {!spacePages.initialLoading && !spacePages.loading && !spacePages.error && spaces.length === 0 ? <div className="console-empty"><FolderKanban size={23} /><strong>{appliedSpaceSearch ? "No matching spaces" : "No spaces yet"}</strong><span>{appliedSpaceSearch ? "Try a broader name or clear the search." : "Create one for a project or keep using shared knowledge."}</span></div> : null}
          <div className="space-card-grid">
            {spaces.map((space, index) => (
              <article className={`space-card${selectedSpace?.id === space.id ? " is-selected" : ""}`} key={space.id}>
                <div className={`space-card-mark accent-${index % 4}`}><FolderKanban aria-hidden="true" size={17} /></div>
                <div><h3>{space.name}</h3><p title={space.id}>{space.id}</p></div>
                <span className={`role-pill role-${space.role}`}>{space.role}</span>
                <div className="space-card-footer">
                  <span className="space-card-meta"><ShieldCheck aria-hidden="true" size={13} /> {space.role === "owner" ? "You manage this space" : "Shared with you"}</span>
                  {space.role === "owner" ? <button aria-label={`Manage access for ${space.name}`} className="space-manage-button" onClick={() => void loadAccess(space)} type="button"><UsersRound size={13} /> Manage access</button> : null}
                </div>
              </article>
            ))}
          </div>
          {spacePages.totalItems > 0 ? <PaginationControls loading={spacePages.loading} loadingPage={spacePages.loadingPage} onPageChange={(nextPage) => void spacePages.goToPage(nextPage)} page={spacePages.page} pageSize={spacePages.pageSize} totalItems={spacePages.totalItems} totalPages={spacePages.totalPages} /> : null}
        </section>
        <aside className="console-panel action-panel">
          <span className="action-panel-icon"><Plus aria-hidden="true" size={20} /></span>
          <p className="console-eyebrow">New boundary</p>
          <h2>Create a space</h2>
          <p>Best for a trip, home project, personal archive, or anything with a smaller access list.</p>
          <form className="console-form" onSubmit={create}>
            <label htmlFor="space-name">Space name</label>
            <input id="space-name" maxLength={120} onChange={(event) => { setCreatedSpaceName(null); setName(event.target.value); }} placeholder="e.g. Japan trip" required value={name} />
            <button className="primary-button" disabled={saving} type="submit">{saving ? <LoaderCircle className="spin" size={16} /> : <Plus size={16} />} Create space</button>
          </form>
          {createdSpaceName ? <p className="inline-success" role="status"><ShieldCheck size={14} /> {createdSpaceName} is ready and listed in your spaces.</p> : null}
          {error ? <p className="inline-error" role="alert">{error}</p> : null}
        </aside>
      </div>
    </ConsoleShell>
  );
}

export function KnowledgeConsole() {
  const member = useCurrentMember();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [spaces, setSpaces] = useState<Space[]>([]);
  const spaceFilter = searchParams.get("space");
  const [spaceId, setSpaceId] = useState("");
  const [searchText, setSearchText] = useState("");
  const [tagText, setTagText] = useState("");
  const [appliedSearch, setAppliedSearch] = useState("");
  const [appliedTag, setAppliedTag] = useState("");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [tags, setTags] = useState("");
  const [spacesLoading, setSpacesLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editing, setEditing] = useState<KnowledgeDetail | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [editContent, setEditContent] = useState("");
  const [editTags, setEditTags] = useState("");
  const [confirmArchive, setConfirmArchive] = useState(false);
  const [savedRevision, setSavedRevision] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const editorRef = useRef<HTMLElement>(null);
  const editedIdRef = useRef<string | null>(null);

  useEffect(() => {
    if (!editing) {
      editedIdRef.current = null;
      return;
    }
    if (editedIdRef.current === editing.id) {
      return;
    }
    const node = editorRef.current;
    if (!node) {
      return;
    }
    editedIdRef.current = editing.id;
    node.scrollIntoView({ block: "nearest" });
  }, [editing]);

  const loadKnowledgePage = useCallback((page: number, signal: AbortSignal) => contractData(
    contractClient.GET("/api/v1/knowledge", {
      params: {
        query: {
          page,
          page_size: 25,
          ...(spaceFilter ? { space_id: spaceFilter } : {}),
          ...(appliedSearch ? { q: appliedSearch } : {}),
          ...(appliedTag ? { tag: appliedTag } : {}),
        },
      },
      signal,
    }),
  ), [appliedSearch, appliedTag, spaceFilter]);
  const {
    error: paginationError,
    initialLoading,
    items,
    page: knowledgePage,
    pageSize: knowledgePageSize,
    loadingPage: knowledgeLoadingPage,
    totalItems: knowledgeTotalItems,
    totalPages: knowledgeTotalPages,
    loading,
    goToPage: goToKnowledgePage,
    reload: reloadItems,
  } = usePagePagination<KnowledgeSummary>({
    loadPage: loadKnowledgePage,
    queryKey: JSON.stringify([spaceFilter, appliedSearch, appliedTag]),
  });

  useEffect(() => {
    let active = true;
    loadAccessibleSpaces().then((spaceResponse) => {
      if (!active) {
        return;
      }
      setSpaces(spaceResponse);
      setSpaceId((current) => current || spaceResponse[0]?.id || "");
      setError(null);
    }).catch((loadError) => {
      if (active) {
        setError(message(loadError));
      }
    }).finally(() => {
      if (active) {
        setSpacesLoading(false);
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
      await contractData(contractClient.POST("/api/v1/knowledge", {
        body: {
          content: content.trim(),
          space_id: spaceId,
          tags: tags.split(",").map((value) => value.trim()).filter(Boolean),
          title: title.trim(),
        },
        params: { header: { "Idempotency-Key": idempotencyKey() } },
      }));
      setTitle("");
      setContent("");
      setTags("");
      if (appliedSearch || appliedTag) {
        setSearchText("");
        setTagText("");
        setAppliedSearch("");
        setAppliedTag("");
      }
      await goToKnowledgePage(1);
    } catch (createError) {
      setError(message(createError));
    } finally {
      setSaving(false);
    }
  };

  const openEditor = async (item: KnowledgeSummary) => {
    setDetailLoading(true);
    setError(null);
    try {
      const detail = await contractData(contractClient.GET("/api/v1/knowledge/{item_id}", { params: { path: { item_id: item.id } } }));
      setEditing(detail);
      setEditTitle(detail.title);
      setEditContent(detail.content);
      setEditTags(detail.tags.join(", "));
      setConfirmArchive(false);
      setSavedRevision(null);
    } catch (loadError) {
      setError(message(loadError));
    } finally {
      setDetailLoading(false);
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
      const updated = await contractData(contractClient.PUT("/api/v1/knowledge/{item_id}", {
        body: {
          change_summary: "Updated through the management console",
          content: editContent.trim(),
          expected_version: editing.version,
          tags: editTags.split(",").map((value) => value.trim()).filter(Boolean),
          title: editTitle.trim(),
        },
        params: { header: { "Idempotency-Key": idempotencyKey() }, path: { item_id: editing.id } },
      }));
      setEditing(updated);
      setEditTitle(updated.title);
      setEditContent(updated.content);
      setEditTags(updated.tags.join(", "));
      setSavedRevision(updated.version);
      reloadItems();
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
      await contractData(contractClient.DELETE("/api/v1/knowledge/{item_id}", {
        params: {
          header: { "Idempotency-Key": idempotencyKey() },
          path: { item_id: editing.id },
          query: { expected_version: editing.version },
        },
      }));
      setEditing(null);
      setConfirmArchive(false);
      reloadItems();
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
      spaceCount={spacesLoading ? null : spaces.length}
      title="Knowledge"
    >
      <div className="console-grid console-grid-knowledge">
        <section className="console-panel">
          <div className="panel-heading">
            <div><span>Living library</span><h2>{spaceFilter ? "Filtered knowledge" : "Knowledge items"}</h2></div>
            <span className="count-pill" aria-label={`${knowledgeTotalItems} knowledge items`}>{initialLoading ? "…" : knowledgeTotalItems}</span>
          </div>
          {spaceFilter ? <div className="filter-strip"><Search size={13} /> Showing one space only<button onClick={() => router.replace("/knowledge")} type="button">Show all spaces</button></div> : null}
          <form className="list-filter-bar" onSubmit={(event) => {
            event.preventDefault();
            setAppliedSearch(searchText.trim());
            setAppliedTag(tagText.trim());
          }} role="search">
            <label>
              <span className="visually-hidden">Search knowledge list</span>
              <Search aria-hidden="true" size={15} />
              <input aria-label="Search knowledge list" onChange={(event) => setSearchText(event.target.value)} placeholder="Search title or content" type="search" value={searchText} />
            </label>
            <label>
              <span className="visually-hidden">Filter by tag</span>
              <Tag aria-hidden="true" size={14} />
              <input aria-label="Filter by tag" onChange={(event) => setTagText(event.target.value)} placeholder="Tag" value={tagText} />
            </label>
            <button aria-label="Apply knowledge filters" className="secondary-button" type="submit">Apply</button>
            {appliedSearch || appliedTag ? <button className="filter-clear-button" onClick={() => {
              setSearchText("");
              setTagText("");
              setAppliedSearch("");
              setAppliedTag("");
            }} type="button">Clear</button> : null}
          </form>
          {initialLoading || (loading && items.length === 0) ? <ListSkeleton /> : null}
          {paginationError ? <ListUnavailable label="knowledge" onRetry={() => void reloadItems()} /> : null}
          {!initialLoading && !loading && !paginationError && items.length === 0 ? <div className="console-empty"><BookOpen size={23} /><strong>{appliedSearch || appliedTag ? "No matching knowledge" : "Nothing captured yet"}</strong><span>{appliedSearch || appliedTag ? "Try a broader phrase or clear one of the filters." : "Add the first durable answer, procedure, or family detail."}</span></div> : null}
          <div className="data-list">
            {items.map((item) => (
              <article className="data-row knowledge-row" key={item.id}>
                <span className="row-leading violet"><BookOpen aria-hidden="true" size={18} /></span>
                <div className="row-copy"><h3>{item.title}</h3><p>{spaceLabel(spaces, item.space_id)} · version {item.version}</p></div>
                <div className="tag-list">{item.tags.slice(0, 3).map((value) => <span key={value}><Tag size={11} />{value}</span>)}</div>
                <button aria-label={`Edit ${item.title}`} className="row-action-button" onClick={() => void openEditor(item)} type="button">Edit</button>
              </article>
            ))}
          </div>
          {knowledgeTotalItems > 0 ? <PaginationControls loading={loading} loadingPage={knowledgeLoadingPage} onPageChange={(nextPage) => void goToKnowledgePage(nextPage)} page={knowledgePage} pageSize={knowledgePageSize} totalItems={knowledgeTotalItems} totalPages={knowledgeTotalPages} /> : null}
          {detailLoading ? <div className="console-loading access-loading"><LoaderCircle className="spin" size={18} /> Loading knowledge detail…</div> : null}
          {editing ? (
            <section className="knowledge-editor" aria-label={`Edit ${editing.title}`} ref={editorRef}>
              <div className="space-access-heading">
                <div><span>Immutable revision</span><h2>Edit knowledge</h2></div>
                <button aria-label="Close knowledge editor" className="icon-button" onClick={() => setEditing(null)} type="button"><X size={16} /></button>
              </div>
              <p className="revision-state">Version {editing.version} is active</p>
              {savedRevision ? <p className="inline-success" role="status"><ShieldCheck size={14} /> Revision {savedRevision} saved and searchable.</p> : null}
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
            <button className="primary-button" disabled={saving || spacesLoading || spaces.length === 0} type="submit">{saving ? <LoaderCircle className="spin" size={16} /> : <Plus size={16} />} Capture knowledge</button>
          </form>
          {error ? <p className="inline-error" role="alert">{error}</p> : null}
        </aside>
      </div>
    </ConsoleShell>
  );
}

export function ExploreConsole() {
  const member = useCurrentMember();
  const router = useRouter();
  const searchParams = useSearchParams();
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [query, setQuery] = useState(searchParams.get("q") || "");
  const [result, setResult] = useState<RetrievalResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const autoQueryRef = useRef<string | null>(searchParams.get("q"));

  useEffect(() => {
    let active = true;
    loadAccessibleSpaces()
      .then((response) => {
        if (active) {
          setSpaces(response);
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

  const runSearch = useCallback(async (value: string) => {
    const trimmed = value.trim();
    if (!trimmed || searching) {
      return;
    }
    setSearching(true);
    setError(null);
    try {
      const response = await contractData(contractClient.POST("/api/v1/retrieval/search", {
        body: {
          limit: 20,
          query: trimmed,
          semantic_policy: "prefer",
        },
      }));
      setResult(response);
      router.replace(`/explore?q=${encodeURIComponent(trimmed)}`, { scroll: false });
    } catch (searchError) {
      setError(message(searchError));
    } finally {
      setSearching(false);
    }
  }, [router, searching]);

  useEffect(() => {
    const pending = autoQueryRef.current;
    if (pending) {
      autoQueryRef.current = null;
      void runSearch(pending);
    }
  }, [runSearch]);

  return (
    <ConsoleShell
      description="Search once across every space you can access. Results remain permission-aware even with a large collection of spaces."
      eyebrow="Unified discovery"
      member={memberView(member)}
      spaceCount={loading ? null : spaces.length}
      title="Explore"
    >
      <section className="explore-hero console-panel">
        <div className="search-scope"><ShieldCheck size={15} /> All accessible spaces are in scope</div>
        <form className="console-search" onSubmit={(event) => { event.preventDefault(); void runSearch(query); }} role="search">
          <Search aria-hidden="true" size={21} />
          <input aria-label="Search query" autoFocus onChange={(event) => setQuery(event.target.value)} placeholder="Ask for a detail, process, place, or decision…" type="search" value={query} />
          <button aria-label="Search knowledge" disabled={searching} type="submit">{searching ? <LoaderCircle className="spin" size={17} /> : <ArrowUpRight size={17} />}</button>
        </form>
        <p>Kinbase fans the query out only to authorized spaces, merges the candidates, and returns a single ranked result set.</p>
      </section>
      {error ? <p className="inline-error wide" role="alert">{error}</p> : null}
      <p className="visually-hidden" role="status">{searching ? "Searching…" : result ? `${result.hits.length} matches found` : ""}</p>
      {searching ? (
        <section aria-busy="true" className="search-results-section">
          <div className="results-toolbar">
            <div><p className="console-eyebrow">Ranked results</p><h2>Searching…</h2></div>
          </div>
          <ListSkeleton rows={4} />
        </section>
      ) : null}
      {!searching && result ? (
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
                  <div className="result-meta"><span data-chip="space">{spaceLabel(spaces, hit.space_id)}</span><span>{hit.source_type.replaceAll("_", " ")}</span>{hit.version ? <span>v{hit.version}</span> : null}</div>
                  <h3>{hit.title}</h3>
                  <p>{hit.content_excerpt}</p>
                </div>
                <span className="score-pill">{Math.round(hit.rank_score * 100)}%</span>
              </article>
            ))}
          </div>
        </section>
      ) : null}
      {!searching && !result ? (
        <section className="explore-prompt-grid">
          <article><span>01</span><h3>Search once</h3><p>No need to guess which space contains the answer.</p></article>
          <article><span>02</span><h3>Respect access</h3><p>Spaces outside your membership never enter the candidate set.</p></article>
          <article><span>03</span><h3>Stay useful</h3><p>Lexical retrieval continues safely when embedding is offline.</p></article>
        </section>
      ) : null}
    </ConsoleShell>
  );
}

export function SourcesConsole() {
  const member = useCurrentMember();
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [searchText, setSearchText] = useState("");
  const [appliedSearch, setAppliedSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<"" | "pending" | "processing" | "ready" | "active" | "failed" | "quarantined" | "cancelled">("");
  const [spaceId, setSpaceId] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [spacesLoading, setSpacesLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [queued, setQueued] = useState(false);
  const [pendingArchive, setPendingArchive] = useState<SourceSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  const loadSourcePage = useCallback((page: number, signal: AbortSignal) => contractData(
    contractClient.GET("/api/v1/sources", {
      params: { query: {
        page,
        page_size: 25,
        ...(appliedSearch ? { q: appliedSearch } : {}),
        ...(statusFilter ? { status: statusFilter } : {}),
      } },
      signal,
    }),
  ), [appliedSearch, statusFilter]);
  const {
    error: paginationError,
    initialLoading,
    items: sources,
    page: sourcePage,
    pageSize: sourcePageSize,
    totalItems: sourceTotalItems,
    totalPages: sourceTotalPages,
    loading,
    loadingPage: sourceLoadingPage,
    goToPage: goToSourcePage,
    reload: reloadSources,
  } = usePagePagination<SourceSummary>({
    loadPage: loadSourcePage,
    queryKey: JSON.stringify([appliedSearch, statusFilter]),
  });

  useEffect(() => {
    let active = true;
    loadAccessibleSpaces().then((spaceResponse) => {
      if (!active) {
        return;
      }
      setSpaces(spaceResponse);
      setSpaceId(spaceResponse[0]?.id || "");
    }).catch((loadError) => {
      if (active) {
        setError(message(loadError));
      }
    }).finally(() => {
      if (active) {
        setSpacesLoading(false);
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
      await apiMultipart<components["schemas"]["SourceUploadReceipt"]>("/api/v1/sources/upload", body, { idempotent: true });
      setQueued(true);
      setDisplayName("");
      setFile(null);
      reloadSources();
    } catch (uploadError) {
      setError(message(uploadError));
    } finally {
      setSaving(false);
    }
  };

  const archiveSource = async () => {
    if (!pendingArchive || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await contractData(contractClient.DELETE("/api/v1/sources/{document_id}", {
        params: {
          header: { "Idempotency-Key": idempotencyKey() },
          path: { document_id: pendingArchive.id },
          query: { expected_revision: pendingArchive.revision },
        },
      }));
      setPendingArchive(null);
      reloadSources();
    } catch (archiveError) {
      setError(message(archiveError));
    } finally {
      setSaving(false);
    }
  };

  return (
    <ConsoleShell
      description="Upload original files into versioned object storage and track every revision through the durable ingestion pipeline."
      eyebrow="Source library"
      member={memberView(member)}
      spaceCount={spacesLoading ? null : spaces.length}
      title="Sources"
    >
      <div className="console-grid console-grid-sources">
        <section className="console-panel">
          <div className="panel-heading">
            <div><span>Original material</span><h2>Source files</h2></div>
            <span className="count-pill">{initialLoading ? "…" : sourceTotalItems}</span>
          </div>
          <form className="list-filter-bar source-filter-bar" onSubmit={(event) => {
            event.preventDefault();
            setAppliedSearch(searchText.trim());
          }} role="search">
            <label><Search aria-hidden="true" size={15} /><input aria-label="Search source files" onChange={(event) => setSearchText(event.target.value)} placeholder="Search name or filename" type="search" value={searchText} /></label>
            <label><span className="visually-hidden">Filter source status</span><select aria-label="Filter source status" onChange={(event) => setStatusFilter(event.target.value as typeof statusFilter)} value={statusFilter}><option value="">All statuses</option><option value="pending">Pending</option><option value="processing">Processing</option><option value="ready">Ready</option><option value="active">Active</option><option value="failed">Failed</option><option value="quarantined">Quarantined</option><option value="cancelled">Cancelled</option></select></label>
            <button className="secondary-button" type="submit">Apply</button>
            {appliedSearch || statusFilter ? <button className="filter-clear-button" onClick={() => { setSearchText(""); setAppliedSearch(""); setStatusFilter(""); }} type="button">Clear</button> : null}
          </form>
          {initialLoading || (loading && sources.length === 0) ? <ListSkeleton /> : null}
          {paginationError ? <ListUnavailable label="source files" onRetry={() => void reloadSources()} /> : null}
          {!initialLoading && !loading && !paginationError && sources.length === 0 ? <div className="console-empty"><FileText size={23} /><strong>{appliedSearch || statusFilter ? "No matching source files" : "No source files"}</strong><span>{appliedSearch || statusFilter ? "Try a broader search or clear the status filter." : "Upload a document without changing its meaning or filtering its contents."}</span></div> : null}
          <div className="data-list">
            {sources.map((source) => (
              <article className="data-row source-row" key={source.id}>
                <span className="row-leading cyan"><FileText aria-hidden="true" size={18} /></span>
                <div className="row-copy"><h3>{source.display_name}</h3><p>{source.original_filename || "Unnamed file"} · {spaceLabel(spaces, source.space_id)}</p></div>
                <div className="row-stats"><strong>{source.size_bytes === null ? "—" : `${Math.max(1, Math.round(source.size_bytes / 1024))} KB`}</strong><span className={`status-pill status-${source.status}`}>{source.status}</span></div>
                <button aria-label={`Archive ${source.display_name}`} className="archive-button compact" disabled={saving} onClick={() => setPendingArchive(source)} type="button"><Archive size={14} /> Archive</button>
              </article>
            ))}
          </div>
          {sourceTotalItems > 0 ? <PaginationControls loading={loading} loadingPage={sourceLoadingPage} onPageChange={(nextPage) => void goToSourcePage(nextPage)} page={sourcePage} pageSize={sourcePageSize} totalItems={sourceTotalItems} totalPages={sourceTotalPages} /> : null}
          {pendingArchive ? <div aria-label={`Archive ${pendingArchive.display_name}`} className="confirmation-strip" role="alertdialog"><div><strong>Archive {pendingArchive.display_name}?</strong><span>The source leaves unified search immediately while its original bytes, revisions, and audit history remain preserved.</span></div><button className="secondary-button" onClick={() => setPendingArchive(null)} type="button">Keep source</button><button className="danger-button" disabled={saving} onClick={() => void archiveSource()} type="button">Confirm archive source</button></div> : null}
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
            <button className="primary-button" disabled={saving || spacesLoading || !file || spaces.length === 0} type="submit">{saving ? <LoaderCircle className="spin" size={16} /> : <FileUp size={16} />} Queue source</button>
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
  const [spacesReady, setSpacesReady] = useState(false);
  const [memberSearch, setMemberSearch] = useState("");
  const [appliedMemberSearch, setAppliedMemberSearch] = useState("");
  const [memberStatus, setMemberStatus] = useState<"" | "pending" | "active" | "disabled">("");
  const [memberRole, setMemberRole] = useState<"" | "super_admin" | "member">("");
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [created, setCreated] = useState<CreatedMember | null>(null);
  const [selectedMember, setSelectedMember] = useState<MemberSummary | null>(null);
  const [pendingMemberAction, setPendingMemberAction] = useState<"disable" | "enable" | "reset" | null>(null);
  const [resetPassword, setResetPassword] = useState<ResetMemberPassword | null>(null);
  const [recoverySpaceSearch, setRecoverySpaceSearch] = useState("");
  const [recoveryOwnerSearch, setRecoveryOwnerSearch] = useState("");
  const [appliedRecoverySpaceSearch, setAppliedRecoverySpaceSearch] = useState("");
  const [appliedRecoveryOwnerSearch, setAppliedRecoveryOwnerSearch] = useState("");
  const [ownershipRecoveryOpen, setOwnershipRecoveryOpen] = useState(false);
  const [recoverySpaceId, setRecoverySpaceId] = useState("");
  const [recoveryTargetId, setRecoveryTargetId] = useState("");
  const [recoveryReason, setRecoveryReason] = useState("");
  const [reviewOwnershipRecovery, setReviewOwnershipRecovery] = useState(false);
  const [ownershipRecovered, setOwnershipRecovered] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const isAdmin = member.system_role === "super_admin";

  const loadMemberPage = useCallback((page: number, signal: AbortSignal) => isAdmin
    ? contractData(contractClient.GET("/api/v1/members", { params: { query: { page, page_size: 25, ...(appliedMemberSearch ? { q: appliedMemberSearch } : {}), ...(memberStatus ? { status: memberStatus } : {}), ...(memberRole ? { system_role: memberRole } : {}) } }, signal }))
    : Promise.resolve({ items: [], page: 1, page_size: 25, total_items: 0, total_pages: 0 }), [appliedMemberSearch, isAdmin, memberRole, memberStatus]);
  const memberPages = usePagePagination<MemberSummary>({ loadPage: loadMemberPage, queryKey: JSON.stringify([isAdmin, appliedMemberSearch, memberStatus, memberRole]) });
  const members = memberPages.items;
  const loadAdminSpacePage = useCallback((page: number, signal: AbortSignal) => isAdmin && ownershipRecoveryOpen
    ? contractData(contractClient.GET("/api/v1/admin/spaces", { params: { query: { page, page_size: 25, ...(appliedRecoverySpaceSearch ? { q: appliedRecoverySpaceSearch } : {}) } }, signal }))
    : Promise.resolve({ items: [], page: 1, page_size: 25, total_items: 0, total_pages: 0 }), [appliedRecoverySpaceSearch, isAdmin, ownershipRecoveryOpen]);
  const adminSpacePages = usePagePagination<AdminSpace>({ loadPage: loadAdminSpacePage, queryKey: JSON.stringify([isAdmin, ownershipRecoveryOpen, appliedRecoverySpaceSearch]) });
  const adminSpaces = adminSpacePages.items;
  const loadRecoveryOwnerPage = useCallback((page: number, signal: AbortSignal) => isAdmin && ownershipRecoveryOpen
    ? contractData(contractClient.GET("/api/v1/members", { params: { query: { page, page_size: 25, status: "active", ...(appliedRecoveryOwnerSearch ? { q: appliedRecoveryOwnerSearch } : {}) } }, signal }))
    : Promise.resolve({ items: [], page: 1, page_size: 25, total_items: 0, total_pages: 0 }), [appliedRecoveryOwnerSearch, isAdmin, ownershipRecoveryOpen]);
  const recoveryOwnerPages = usePagePagination<MemberSummary>({ loadPage: loadRecoveryOwnerPage, queryKey: JSON.stringify([isAdmin, ownershipRecoveryOpen, appliedRecoveryOwnerSearch]) });
  const recoveryOwners = recoveryOwnerPages.items;

  useEffect(() => {
    if (!ownershipRecoveryOpen) {
      return;
    }
    setRecoverySpaceId((current) => adminSpaces.some((space) => space.id === current) ? current : adminSpaces[0]?.id || "");
  }, [adminSpaces, ownershipRecoveryOpen]);

  useEffect(() => {
    if (!ownershipRecoveryOpen) {
      return;
    }
    const ownerId = adminSpaces.find((space) => space.id === recoverySpaceId)?.owner_member_id;
    setRecoveryTargetId((current) => recoveryOwners.some((person) => person.id === current && person.id !== ownerId)
      ? current
      : recoveryOwners.find((person) => person.id !== ownerId)?.id || "");
  }, [adminSpaces, ownershipRecoveryOpen, recoveryOwners, recoverySpaceId]);

  useEffect(() => {
    let active = true;
    loadAccessibleSpaces().then((spaceResponse) => {
      if (!active) {
        return;
      }
      setSpaces(spaceResponse);
      setSpacesReady(true);
    }).catch((loadError) => {
      if (active) {
        setError(message(loadError));
        setSpacesReady(true);
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
      const response = await contractData(contractClient.POST("/api/v1/members", {
        body: { display_name: displayName.trim(), username: username.trim() },
        params: { header: { "Idempotency-Key": idempotencyKey() } },
      }));
      setCreated(response);
      setUsername("");
      setDisplayName("");
      memberPages.reload();
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
      const response = await contractData(contractClient.PATCH("/api/v1/members/{member_id}", {
        body: {
          display_name: selectedMember.display_name,
          status,
          system_role: selectedMember.system_role,
        },
        params: { header: { "Idempotency-Key": idempotencyKey() }, path: { member_id: selectedMember.id } },
      }));
      setSelectedMember(response);
      setPendingMemberAction(null);
      memberPages.reload();
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
      const response = await contractData(contractClient.POST("/api/v1/members/{member_id}/password-reset", {
        params: { header: { "Idempotency-Key": idempotencyKey() }, path: { member_id: selectedMember.id } },
      }));
      setResetPassword(response);
      setPendingMemberAction(null);
      setSelectedMember({ ...selectedMember, requires_password_change: true });
      memberPages.reload();
    } catch (resetError) {
      setError(message(resetError));
    } finally {
      setSaving(false);
    }
  };

  const openOwnershipRecovery = () => {
    setError(null);
    setOwnershipRecovered(false);
    setRecoverySpaceId(adminSpaces[0]?.id || "");
    const firstOwner = adminSpaces[0]?.owner_member_id;
    setRecoveryTargetId(recoveryOwners.find((candidate) => candidate.id !== firstOwner)?.id || "");
    setOwnershipRecoveryOpen(true);
  };

  const selectRecoverySpace = (spaceId: string) => {
    setRecoverySpaceId(spaceId);
    const ownerId = adminSpaces.find((space) => space.id === spaceId)?.owner_member_id;
    setRecoveryTargetId(recoveryOwners.find((candidate) => candidate.id !== ownerId)?.id || "");
    setReviewOwnershipRecovery(false);
    setOwnershipRecovered(false);
  };

  const transferEmergencyOwnership = async () => {
    const selected = adminSpaces.find((space) => space.id === recoverySpaceId);
    const target = recoveryOwners.find((candidate) => candidate.id === recoveryTargetId);
    if (!selected || !target || recoveryReason.trim().length < 5 || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await contractData(contractClient.PUT("/api/v1/admin/spaces/{space_id}/ownership", {
        body: {
          expected_revision: selected.revision,
          reason: recoveryReason.trim(),
          target_member_id: target.id,
        },
        params: { header: { "Idempotency-Key": idempotencyKey() }, path: { space_id: selected.id } },
      }));
      adminSpacePages.reload();
      setReviewOwnershipRecovery(false);
      setRecoveryReason("");
      setOwnershipRecovered(true);
    } catch (transferError) {
      setError(message(transferError));
    } finally {
      setSaving(false);
    }
  };

  return (
    <ConsoleShell
      description="Super admins create family accounts. Space owners grant access separately so identity and knowledge boundaries stay explicit."
      eyebrow="Identity & access"
      member={memberView(member)}
      spaceCount={spacesReady ? spaces.length : null}
      title="People & access"
    >
      {!isAdmin ? (
        <section className="console-panel permission-panel"><ShieldCheck size={25} /><div><h2>Space access remains owner-managed</h2><p>Your account can manage memberships inside spaces you own. New family identities can only be generated by a super admin.</p></div></section>
      ) : (
        <div className="console-grid console-grid-people">
          <section className="console-panel">
            <div className="panel-heading"><div><span>Family directory</span><h2>Members</h2></div><span className="count-pill">{memberPages.initialLoading ? "…" : memberPages.totalItems}</span></div>
            <form className="list-filter-bar member-filter-bar" onSubmit={(event) => { event.preventDefault(); setAppliedMemberSearch(memberSearch.trim()); }} role="search"><label><Search aria-hidden="true" size={14} /><input aria-label="Search members" onChange={(event) => setMemberSearch(event.target.value)} placeholder="Name or username" type="search" value={memberSearch} /></label><label><select aria-label="Filter member status" onChange={(event) => setMemberStatus(event.target.value as typeof memberStatus)} value={memberStatus}><option value="">All statuses</option><option value="pending">Pending</option><option value="active">Active</option><option value="disabled">Disabled</option></select></label><label><select aria-label="Filter system role" onChange={(event) => setMemberRole(event.target.value as typeof memberRole)} value={memberRole}><option value="">All roles</option><option value="super_admin">Super admin</option><option value="member">Member</option></select></label><button className="secondary-button" type="submit">Apply</button></form>
            {memberPages.initialLoading || (memberPages.loading && members.length === 0) ? <ListSkeleton rows={6} /> : null}
            {memberPages.error ? <ListUnavailable label="members" onRetry={() => void memberPages.reload()} /> : null}
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
            {memberPages.totalItems > 0 ? <PaginationControls loading={memberPages.loading} loadingPage={memberPages.loadingPage} onPageChange={(nextPage) => void memberPages.goToPage(nextPage)} page={memberPages.page} pageSize={memberPages.pageSize} totalItems={memberPages.totalItems} totalPages={memberPages.totalPages} /> : null}
            {selectedMember ? (
              <section aria-label={`Manage ${selectedMember.display_name}`} className="member-admin-panel">
                <div className="space-access-heading"><div><span>Account controls</span><h2>{selectedMember.display_name}</h2></div><button aria-label="Close member manager" className="icon-button" onClick={() => setSelectedMember(null)} type="button"><X size={16} /></button></div>
                <div className="member-admin-form">
                  <div><label htmlFor="member-admin-display-name">Display name</label><input id="member-admin-display-name" maxLength={255} onChange={(event) => setSelectedMember({ ...selectedMember, display_name: event.target.value })} value={selectedMember.display_name} /></div>
                  <div><label htmlFor="member-admin-system-role">System role</label><select id="member-admin-system-role" onChange={(event) => setSelectedMember({ ...selectedMember, system_role: event.target.value as MemberSummary["system_role"] })} value={selectedMember.system_role}><option value="member">Member</option><option value="super_admin">Super admin</option></select></div>
                  <button className="secondary-button" disabled={saving} onClick={() => void updateSelectedMember()} type="button"><Save size={14} /> Save account</button>
                </div>
                <div className="member-security-actions">
                  <button className="secondary-button" disabled={saving} onClick={() => { setResetPassword(null); setPendingMemberAction("reset"); }} type="button"><KeyRound size={14} /> Reset {selectedMember.display_name} password</button>
                  {selectedMember.status === "disabled" ? <button className="secondary-button" disabled={saving} onClick={() => setPendingMemberAction("enable")} type="button">Enable {selectedMember.display_name}</button> : <button className="archive-button compact" disabled={saving} onClick={() => setPendingMemberAction("disable")} type="button">Disable {selectedMember.display_name}</button>}
                </div>
                {pendingMemberAction === "reset" ? <div aria-label={`Reset ${selectedMember.display_name} password`} className="confirmation-strip" role="alertdialog"><div><strong>Reset this password?</strong><span>This signs out every session, revokes active API keys, and creates a one-time password.</span></div><button className="secondary-button" onClick={() => setPendingMemberAction(null)} type="button">Keep password</button><button className="danger-button" disabled={saving} onClick={() => void resetSelectedMemberPassword()} type="button">Confirm password reset</button></div> : null}
                {pendingMemberAction === "disable" ? <div aria-label={`Disable ${selectedMember.display_name}`} className="confirmation-strip" role="alertdialog"><div><strong>Disable this member?</strong><span>This immediately revokes sessions and API keys. Space history remains preserved.</span></div><button className="secondary-button" onClick={() => setPendingMemberAction(null)} type="button">Keep active</button><button className="danger-button" disabled={saving} onClick={() => void updateSelectedMember("disabled")} type="button">Confirm disable member</button></div> : null}
                {pendingMemberAction === "enable" ? <div aria-label={`Enable ${selectedMember.display_name}`} className="confirmation-strip" role="alertdialog"><div><strong>Enable this member?</strong><span>The member can authenticate again, but revoked sessions and keys remain revoked.</span></div><button className="secondary-button" onClick={() => setPendingMemberAction(null)} type="button">Keep disabled</button><button className="primary-button" disabled={saving} onClick={() => void updateSelectedMember("active")} type="button">Confirm enable member</button></div> : null}
                {resetPassword ? <div className="member-reset-secret"><p>This temporary password is shown only once.</p><div className="secret-value"><code>{resetPassword.temporary_password}</code><CopyButton label="Copy reset password" value={resetPassword.temporary_password} /></div></div> : null}
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
                <div className="secret-value"><code>{created.temporary_password}</code><CopyButton label="Copy temporary password" value={created.temporary_password} /></div>
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
            <div className="ownership-recovery">
              <div className="ownership-recovery-heading">
                <div><span>Account recovery</span><strong>Emergency ownership</strong></div>
                {!ownershipRecoveryOpen ? <button className="secondary-button" disabled={saving} onClick={openOwnershipRecovery} type="button"><KeyRound size={14} /> Open ownership recovery</button> : <button aria-label="Close ownership recovery" className="icon-button" onClick={() => { setOwnershipRecoveryOpen(false); setReviewOwnershipRecovery(false); }} type="button"><X size={15} /></button>}
              </div>
              {ownershipRecoveryOpen ? (
                <div className="ownership-recovery-form">
                  <p>Use only when an owner cannot recover their account. Space names and ownership metadata are visible here; content remains inaccessible.</p>
                  {adminSpacePages.initialLoading || recoveryOwnerPages.initialLoading || (adminSpacePages.loading && adminSpaces.length === 0) || (recoveryOwnerPages.loading && recoveryOwners.length === 0) ? <ListSkeleton rows={3} /> : null}
                  {adminSpacePages.error || recoveryOwnerPages.error ? <ListUnavailable label="ownership recovery data" onRetry={() => { void adminSpacePages.reload(); void recoveryOwnerPages.reload(); }} /> : null}
                  <form className="ownership-search-form" onSubmit={(event) => { event.preventDefault(); setAppliedRecoverySpaceSearch(recoverySpaceSearch.trim()); }} role="search">
                    <label htmlFor="recovery-space-search">Find space</label>
                    <div><input id="recovery-space-search" onChange={(event) => setRecoverySpaceSearch(event.target.value)} placeholder="Search space or owner" type="search" value={recoverySpaceSearch} /><button aria-label="Apply space search" className="secondary-button" type="submit"><Search size={14} /> Apply</button></div>
                  </form>
                  <label htmlFor="recovery-space">Recovery space</label>
                  <select disabled={Boolean(adminSpacePages.error)} id="recovery-space" onChange={(event) => selectRecoverySpace(event.target.value)} value={recoverySpaceId}>
                    {adminSpaces.map((space) => <option key={space.id} value={space.id}>{space.name} · {space.owner_display_name}</option>)}
                  </select>
                  {adminSpacePages.totalItems > 0 ? <PaginationControls loading={adminSpacePages.loading} loadingPage={adminSpacePages.loadingPage} onPageChange={(nextPage) => void adminSpacePages.goToPage(nextPage)} page={adminSpacePages.page} pageSize={adminSpacePages.pageSize} totalItems={adminSpacePages.totalItems} totalPages={adminSpacePages.totalPages} /> : null}
                  <form className="ownership-search-form" onSubmit={(event) => { event.preventDefault(); setAppliedRecoveryOwnerSearch(recoveryOwnerSearch.trim()); }} role="search">
                    <label htmlFor="recovery-owner-search">Find new owner</label>
                    <div><input id="recovery-owner-search" onChange={(event) => setRecoveryOwnerSearch(event.target.value)} placeholder="Search active members" type="search" value={recoveryOwnerSearch} /><button aria-label="Apply owner search" className="secondary-button" type="submit"><Search size={14} /> Apply</button></div>
                  </form>
                  <label htmlFor="recovery-owner">New owner</label>
                  <select disabled={Boolean(recoveryOwnerPages.error)} id="recovery-owner" onChange={(event) => { setRecoveryTargetId(event.target.value); setReviewOwnershipRecovery(false); setOwnershipRecovered(false); }} value={recoveryTargetId}>
                    {recoveryOwners.filter((candidate) => candidate.id !== adminSpaces.find((space) => space.id === recoverySpaceId)?.owner_member_id).map((candidate) => <option key={candidate.id} value={candidate.id}>{candidate.display_name} (@{candidate.username})</option>)}
                  </select>
                  {recoveryOwnerPages.totalItems > 0 ? <PaginationControls loading={recoveryOwnerPages.loading} loadingPage={recoveryOwnerPages.loadingPage} onPageChange={(nextPage) => void recoveryOwnerPages.goToPage(nextPage)} page={recoveryOwnerPages.page} pageSize={recoveryOwnerPages.pageSize} totalItems={recoveryOwnerPages.totalItems} totalPages={recoveryOwnerPages.totalPages} /> : null}
                  <label htmlFor="recovery-reason">Recovery reason</label>
                  <textarea id="recovery-reason" maxLength={500} minLength={5} onChange={(event) => { setRecoveryReason(event.target.value); setReviewOwnershipRecovery(false); setOwnershipRecovered(false); }} required value={recoveryReason} />
                  <p className="ownership-boundary"><ShieldCheck size={14} /> This repair is audited and does not grant the Super Admin access to space content.</p>
                  {reviewOwnershipRecovery ? (
                    <div aria-label="Confirm emergency ownership transfer" className="confirmation-strip" role="alertdialog">
                      <div><strong>Transfer ownership now?</strong><span>The current owner becomes an editor. The selected member becomes the sole owner.</span></div>
                      <button className="secondary-button" onClick={() => setReviewOwnershipRecovery(false)} type="button">Go back</button>
                      <button className="danger-button" disabled={saving} onClick={() => void transferEmergencyOwnership()} type="button">Confirm emergency transfer</button>
                    </div>
                  ) : <button className="secondary-button" disabled={!recoverySpaceId || !recoveryTargetId || recoveryReason.trim().length < 5 || saving} onClick={() => setReviewOwnershipRecovery(true)} type="button">Review ownership repair</button>}
                  {ownershipRecovered ? <p className="inline-success"><ShieldCheck size={14} /> Ownership repaired. The action and reason were written to the audit trail.</p> : null}
                </div>
              ) : null}
            </div>
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
  const [settings, setSettings] = useState<RuntimeSettings | null>(null);
  const [keySearch, setKeySearch] = useState("");
  const [appliedKeySearch, setAppliedKeySearch] = useState("");
  const [keyStatus, setKeyStatus] = useState<"" | "active" | "revoked" | "expired">("");
  const [sessionStatus, setSessionStatus] = useState<"" | "active" | "revoked" | "expired">("");
  const [historyState, setHistoryState] = useState<"" | "active" | "superseded">("");
  const [runtimeDraft, setRuntimeDraft] = useState<RuntimeSettings | null>(null);
  const [pendingSettingsRestore, setPendingSettingsRestore] = useState<RuntimeSettings | null>(null);
  const [runtimeLimit, setRuntimeLimit] = useState(20);
  const [runtimeReason, setRuntimeReason] = useState("");
  const [keyName, setKeyName] = useState("");
  const [selectedKeyScopes, setSelectedKeyScopes] = useState<string[]>(DEFAULT_API_KEY_SCOPES);
  const [createdKey, setCreatedKey] = useState<CreatedAPIKey | null>(null);
  const [pendingKeyRevocation, setPendingKeyRevocation] = useState<APIKeySummary | null>(null);
  const [pendingSessionRevocation, setPendingSessionRevocation] = useState<SessionSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadKeyPage = useCallback((page: number, signal: AbortSignal) => contractData(
    contractClient.GET("/api/v1/api-keys", {
      params: { query: { page, page_size: 25, ...(appliedKeySearch ? { q: appliedKeySearch } : {}), ...(keyStatus ? { status: keyStatus } : {}) } },
      signal,
    }),
  ), [appliedKeySearch, keyStatus]);
  const keyPages = usePagePagination<APIKeySummary>({ loadPage: loadKeyPage, queryKey: JSON.stringify([appliedKeySearch, keyStatus]) });
  const loadSessionPage = useCallback((page: number, signal: AbortSignal) => contractData(
    contractClient.GET("/api/v1/sessions", { params: { query: { page, page_size: 25, ...(sessionStatus ? { status: sessionStatus } : {}) } }, signal }),
  ), [sessionStatus]);
  const sessionPages = usePagePagination<SessionSummary>({ loadPage: loadSessionPage, queryKey: sessionStatus });
  const loadHistoryPage = useCallback((page: number, signal: AbortSignal) => {
    if (member.system_role !== "super_admin") {
      return Promise.resolve({ items: [], page: 1, page_size: 20, total_items: 0, total_pages: 0 });
    }
    return contractData(contractClient.GET("/api/v1/settings/history", { params: { query: { page, page_size: 20, ...(historyState ? { state: historyState } : {}) } }, signal }));
  }, [historyState, member.system_role]);
  const historyPages = usePagePagination<RuntimeSettings>({ loadPage: loadHistoryPage, queryKey: `${member.system_role}:${historyState}` });
  const keys = keyPages.items;
  const sessions = sessionPages.items;
  const settingsHistory = historyPages.items;

  useEffect(() => {
    let active = true;
    Promise.all([
      loadAccessibleSpaces(),
      contractData(contractClient.GET("/api/v1/settings")),
    ]).then(([spaceResponse, runtimeResponse]) => {
      if (!active) {
        return;
      }
      setSpaces(spaceResponse);
      setSettings(runtimeResponse);
      setRuntimeLimit(runtimeResponse.values.retrieval?.limit ?? 20);
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
  }, [member.system_role]);

  const createKey = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!keyName.trim() || selectedKeyScopes.length === 0 || saving) {
      return;
    }
    setSaving(true);
    setCreatedKey(null);
    setError(null);
    try {
      const response = await contractData(contractClient.POST("/api/v1/api-keys", {
        body: { name: keyName.trim(), scopes: selectedKeyScopes },
        params: { header: { "Idempotency-Key": idempotencyKey() } },
      }));
      setCreatedKey(response);
      setKeyName("");
      setSelectedKeyScopes(DEFAULT_API_KEY_SCOPES);
      keyPages.reload();
    } catch (createError) {
      setError(message(createError));
    } finally {
      setSaving(false);
    }
  };

  const toggleKeyScope = (scope: string) => {
    setSelectedKeyScopes((current) => API_KEY_SCOPE_OPTIONS
      .map((option) => option.value)
      .filter((value) => value === scope ? !current.includes(value) : current.includes(value)));
  };

  const revokeKey = async () => {
    if (!pendingKeyRevocation || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await contractData(contractClient.DELETE("/api/v1/api-keys/{key_id}", {
        params: { header: { "Idempotency-Key": idempotencyKey() }, path: { key_id: pendingKeyRevocation.id } },
      }));
      setPendingKeyRevocation(null);
      keyPages.reload();
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
      await contractData(contractClient.DELETE("/api/v1/sessions/{family_id}", {
        params: { header: { "Idempotency-Key": idempotencyKey() }, path: { family_id: pendingSessionRevocation.id } },
      }));
      setPendingSessionRevocation(null);
      sessionPages.reload();
    } catch (revokeError) {
      setError(message(revokeError));
    } finally {
      setSaving(false);
    }
  };

  const createRuntimeDraft = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!settings?.values.retrieval || runtimeReason.trim().length < 5 || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const draft = await contractData(contractClient.POST("/api/v1/settings/drafts", {
        body: {
          base_revision: settings.revision,
          reason: runtimeReason.trim(),
          values: {
            ...settings.values,
            retrieval: { ...settings.values.retrieval, limit: runtimeLimit },
          },
        },
        params: { header: { "Idempotency-Key": idempotencyKey() } },
      }));
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
      const activated = await contractData(contractClient.POST("/api/v1/settings/drafts/{draft_id}/activate", {
        body: {
          expected_active_revision: settings.revision,
          reason: runtimeReason.trim(),
        },
        params: { header: { "Idempotency-Key": idempotencyKey() }, path: { draft_id: runtimeDraft.id } },
      }));
      setSettings(activated);
      setRuntimeLimit(activated.values.retrieval?.limit ?? runtimeLimit);
      setRuntimeDraft(null);
      historyPages.reload();
    } catch (activationError) {
      setError(message(activationError));
    } finally {
      setSaving(false);
    }
  };

  const restoreRuntimeSettings = async () => {
    if (!settings || !pendingSettingsRestore || runtimeReason.trim().length < 5 || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const restored = await contractData(contractClient.POST("/api/v1/settings/rollback/{target_revision}", {
        body: {
          expected_active_revision: settings.revision,
          reason: runtimeReason.trim(),
        },
        params: { header: { "Idempotency-Key": idempotencyKey() }, path: { target_revision: pendingSettingsRestore.revision } },
      }));
      setSettings(restored);
      setRuntimeLimit(restored.values.retrieval?.limit ?? runtimeLimit);
      setPendingSettingsRestore(null);
      historyPages.reload();
    } catch (restoreError) {
      setError(message(restoreError));
    } finally {
      setSaving(false);
    }
  };

  return (
    <ConsoleShell
      description="Manage personal API credentials, active website sessions, and the safe runtime configuration boundary."
      eyebrow="Account control"
      member={memberView(member)}
      spaceCount={loading ? null : spaces.length}
      title="Settings"
    >
      <div className="settings-layout">
        <section className="console-panel settings-section">
          <div className="panel-heading"><div><span>Personal credentials</span><h2>API keys</h2></div><KeyRound size={20} /></div>
          <p className="section-intro">Every member owns separate keys. Start with the narrowest scope and create another key for a different device or automation.</p>
          {createdKey ? <div className="secret-banner"><div><strong>Copy this key now</strong><span>It cannot be displayed again after you leave this result.</span></div><code>{createdKey.secret}</code><CopyButton label="Copy API key" value={createdKey.secret} /></div> : null}
          <form className="inline-create-form api-key-create-form" onSubmit={createKey}>
            <div><label htmlFor="api-key-name">Key name</label><input id="api-key-name" maxLength={120} onChange={(event) => setKeyName(event.target.value)} placeholder="e.g. Laptop" required value={keyName} /></div>
            <fieldset className="api-key-scopes">
              <legend>Permissions</legend>
              <div className="api-key-scope-grid">
                {API_KEY_SCOPE_OPTIONS.filter((option) => !("superAdminOnly" in option) || member.system_role === "super_admin").map((option) => (
                  <label className="api-key-scope-option" key={option.value}>
                    <input aria-label={option.label} checked={selectedKeyScopes.includes(option.value)} onChange={() => toggleKeyScope(option.value)} type="checkbox" />
                    <span><strong>{option.label}</strong><small>{option.description}</small></span>
                  </label>
                ))}
              </div>
            </fieldset>
            <button className="primary-button" disabled={saving || selectedKeyScopes.length === 0} type="submit">{saving ? <LoaderCircle className="spin" size={16} /> : <KeyRound size={16} />} Create API key</button>
          </form>
          <form className="list-filter-bar settings-list-filter" onSubmit={(event) => { event.preventDefault(); setAppliedKeySearch(keySearch.trim()); }} role="search">
            <label><Search aria-hidden="true" size={14} /><input aria-label="Search API keys" onChange={(event) => setKeySearch(event.target.value)} placeholder="Name or public ID" type="search" value={keySearch} /></label>
            <label><select aria-label="Filter API key status" onChange={(event) => setKeyStatus(event.target.value as typeof keyStatus)} value={keyStatus}><option value="">All statuses</option><option value="active">Active</option><option value="revoked">Revoked</option><option value="expired">Expired</option></select></label>
            <button className="secondary-button" type="submit">Apply</button>
          </form>
          {keyPages.initialLoading || (keyPages.loading && keys.length === 0) ? <ListSkeleton compact rows={3} /> : null}
          {keyPages.error ? <ListUnavailable label="API keys" onRetry={() => void keyPages.reload()} /> : null}
          <div className="data-list compact-list">
            {keys.map((key) => <article className="data-row" key={key.id}><span className="row-leading violet"><KeyRound size={17} /></span><div className="row-copy"><h3>{key.name}</h3><p>{key.public_id} · {key.scopes.join(", ")}</p></div><span className={`status-pill status-${key.status}`}>{key.status}</span>{key.status === "active" ? <button aria-label={`Revoke ${key.name}`} className="membership-remove-button" onClick={() => setPendingKeyRevocation(key)} type="button"><X size={15} /></button> : null}</article>)}
          </div>
          {keyPages.totalItems > 0 ? <PaginationControls loading={keyPages.loading} loadingPage={keyPages.loadingPage} onPageChange={(nextPage) => void keyPages.goToPage(nextPage)} page={keyPages.page} pageSize={keyPages.pageSize} totalItems={keyPages.totalItems} totalPages={keyPages.totalPages} /> : null}
          {pendingKeyRevocation ? <div className="confirmation-strip" role="alertdialog" aria-label={`Revoke ${pendingKeyRevocation.name}`}><div><strong>Revoke {pendingKeyRevocation.name}?</strong><span>Clients using this key will lose access immediately.</span></div><button className="secondary-button" onClick={() => setPendingKeyRevocation(null)} type="button">Keep key</button><button className="danger-button" disabled={saving} onClick={() => void revokeKey()} type="button">Confirm revoke API key</button></div> : null}
        </section>

        <section className="console-panel settings-section">
          <div className="panel-heading"><div><span>Website access</span><h2>Sessions</h2></div><MonitorSmartphone size={20} /></div>
          <div className="list-filter-bar single-filter-bar"><label><select aria-label="Filter session status" onChange={(event) => setSessionStatus(event.target.value as typeof sessionStatus)} value={sessionStatus}><option value="">All statuses</option><option value="active">Active</option><option value="expired">Expired</option><option value="revoked">Revoked</option></select></label></div>
          {sessionPages.initialLoading || (sessionPages.loading && sessions.length === 0) ? <ListSkeleton compact rows={3} /> : null}
          {sessionPages.error ? <ListUnavailable label="sessions" onRetry={() => void sessionPages.reload()} /> : null}
          <div className="data-list compact-list">
            {sessions.map((session) => <article className="data-row" key={session.id}><span className="row-leading cyan"><MonitorSmartphone size={17} /></span><div className="row-copy"><h3>{session.current ? "This session" : "Website session"}</h3><p>Last active {formatDateTime(session.last_activity_at)}</p></div><span className={`status-pill status-${session.status}`}>{session.status}</span>{!session.current && session.status === "active" ? <button aria-label="Sign out website session" className="membership-remove-button" onClick={() => setPendingSessionRevocation(session)} type="button"><X size={15} /></button> : null}</article>)}
            {!sessionPages.initialLoading && !sessionPages.loading && !sessionPages.error && sessions.length === 0 ? <div className="console-empty small"><MonitorSmartphone size={20} /><strong>No session records returned</strong></div> : null}
          </div>
          {sessionPages.totalItems > 0 ? <PaginationControls loading={sessionPages.loading} loadingPage={sessionPages.loadingPage} onPageChange={(nextPage) => void sessionPages.goToPage(nextPage)} page={sessionPages.page} pageSize={sessionPages.pageSize} totalItems={sessionPages.totalItems} totalPages={sessionPages.totalPages} /> : null}
          {pendingSessionRevocation ? <div className="confirmation-strip" role="alertdialog" aria-label="Sign out website session"><div><strong>Sign out this device?</strong><span>The selected session and all of its credentials will be revoked.</span></div><button className="secondary-button" onClick={() => setPendingSessionRevocation(null)} type="button">Keep signed in</button><button className="danger-button" disabled={saving} onClick={() => void revokeSession()} type="button">Confirm sign out</button></div> : null}
        </section>

        <section className="console-panel settings-section runtime-section">
          <div className="panel-heading"><div><span>Production boundary</span><h2>Safe runtime settings</h2></div><Settings2 size={20} /></div>
          {loading ? <ListSkeleton compact rows={3} /> : <>
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
          {member.system_role === "super_admin" ? <div className="list-filter-bar single-filter-bar"><label><select aria-label="Filter settings history state" onChange={(event) => setHistoryState(event.target.value as typeof historyState)} value={historyState}><option value="">All history</option><option value="active">Active</option><option value="superseded">Superseded</option></select></label></div> : null}
          {(historyPages.initialLoading || (historyPages.loading && settingsHistory.length === 0)) && member.system_role === "super_admin" ? <ListSkeleton compact rows={3} /> : null}
          {historyPages.error ? <ListUnavailable label="settings history" onRetry={() => void historyPages.reload()} /> : null}
          {settingsHistory.length > 0 ? <div className="data-list compact-list">{settingsHistory.map((revision) => <article className="data-row" key={revision.id || revision.revision}><span className="row-leading violet"><Settings2 size={16} /></span><div className="row-copy"><h3>Revision {revision.revision}</h3><p>Retrieval limit {revision.values.retrieval?.limit ?? "default"} · {revision.state}</p></div>{revision.state === "superseded" ? <button aria-label={`Restore revision ${revision.revision}`} className="row-action-button" disabled={saving} onClick={() => setPendingSettingsRestore(revision)} type="button">Restore</button> : <span className="status-pill status-active">active</span>}</article>)}</div> : null}
          {historyPages.totalItems > 0 ? <PaginationControls loading={historyPages.loading} loadingPage={historyPages.loadingPage} onPageChange={(nextPage) => void historyPages.goToPage(nextPage)} page={historyPages.page} pageSize={historyPages.pageSize} totalItems={historyPages.totalItems} totalPages={historyPages.totalPages} /> : null}
          {pendingSettingsRestore ? <div aria-label={`Restore revision ${pendingSettingsRestore.revision}`} className="confirmation-strip" role="alertdialog"><div><strong>Restore revision {pendingSettingsRestore.revision}?</strong><span>This creates a new active revision from the historical values. The current revision remains preserved for audit and future recovery.</span></div><button className="secondary-button" onClick={() => setPendingSettingsRestore(null)} type="button">Keep current</button><button className="danger-button" disabled={saving || runtimeReason.trim().length < 5} onClick={() => void restoreRuntimeSettings()} type="button">Confirm restore revision {pendingSettingsRestore.revision}</button></div> : null}
          <div className="runtime-values">
            {Object.entries((runtimeDraft?.values || settings?.values || {}) as Record<string, Record<string, unknown> | undefined>).map(([section, entries]) => (
              <details className="runtime-value-group" key={section} open={section === "retrieval"}>
                <summary>{section.replaceAll("_", " ")}</summary>
                <dl>
                  {Object.entries(entries || {}).map(([key, value]) => (
                    <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{typeof value === "boolean" ? (value ? "on" : "off") : String(value)}</dd></div>
                  ))}
                </dl>
              </details>
            ))}
          </div>
          </>}
        </section>
      </div>
      {error ? <p className="inline-error wide" role="alert">{error}</p> : null}
    </ConsoleShell>
  );
}

export function IngestionConsole() {
  const member = useCurrentMember();
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [spacesReady, setSpacesReady] = useState(false);
  const [spaceFilter, setSpaceFilter] = useState("");
  const [stateFilter, setStateFilter] = useState<"" | "preparing" | "queued" | "running" | "retry_wait" | "succeeded" | "failed" | "cancelled">("");
  const [pendingJobAction, setPendingJobAction] = useState<{ job: IngestionJob; operation: "cancel" | "retry" } | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadJobPage = useCallback((page: number, signal: AbortSignal) => contractData(
    contractClient.GET("/api/v1/ingestion-jobs", {
      params: { query: {
        page,
        page_size: 25,
        ...(spaceFilter ? { space_id: spaceFilter } : {}),
        ...(stateFilter ? { state: stateFilter } : {}),
      } },
      signal,
    }),
  ), [spaceFilter, stateFilter]);
  const {
    error: paginationError,
    initialLoading,
    items: jobs,
    page: jobPage,
    pageSize: jobPageSize,
    totalItems: jobTotalItems,
    totalPages: jobTotalPages,
    loading,
    loadingPage: jobLoadingPage,
    goToPage: goToJobPage,
    reload: reloadJobs,
  } = usePagePagination<IngestionJob>({
    loadPage: loadJobPage,
    queryKey: JSON.stringify([spaceFilter, stateFilter]),
  });

  useEffect(() => {
    let active = true;
    loadAccessibleSpaces().then((response) => {
      if (active) {
        setSpaces(response);
        setSpacesReady(true);
      }
    }).catch((loadError) => {
      if (active) {
        setError(message(loadError));
        setSpacesReady(true);
      }
    });
    return () => {
      active = false;
    };
  }, []);

  const mutateJob = async () => {
    if (!pendingJobAction || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const params = {
        header: { "Idempotency-Key": idempotencyKey() },
        path: { job_id: pendingJobAction.job.id },
      };
      if (pendingJobAction.operation === "cancel") {
        await contractData(contractClient.POST("/api/v1/ingestion-jobs/{job_id}/cancel", { params }));
      } else {
        await contractData(contractClient.POST("/api/v1/ingestion-jobs/{job_id}/retry", { params }));
      }
      setPendingJobAction(null);
      reloadJobs();
    } catch (mutationError) {
      setError(message(mutationError));
    } finally {
      setSaving(false);
    }
  };

  const activeJobs = jobs.filter((job) => !["succeeded", "failed", "cancelled"].includes(job.state)).length;
  return (
    <ConsoleShell
      description="Follow the durable pipeline from queued source through parsing, activation, retry, or a clear terminal state."
      eyebrow="Operational pipeline"
      member={memberView(member)}
      spaceCount={spacesReady ? spaces.length : null}
      title="Ingestion"
    >
      <div className="metric-strip metric-strip-compact"><article><span>Total jobs</span><strong>{initialLoading ? "…" : jobTotalItems}</strong></article><article><span>Active on this page</span><strong>{initialLoading || loading ? "…" : activeJobs}</strong></article></div>
      <section className="console-panel">
        <div className="panel-heading"><div><span>Live durable state</span><h2>Ingestion jobs</h2></div><button className="metric-refresh-button metric-refresh-inline" onClick={reloadJobs} type="button"><Layers3 size={14} /> Refresh now</button></div>
        <div className="list-filter-bar two-filter-bar">
          <label><span className="visually-hidden">Filter ingestion space</span><select aria-label="Filter ingestion space" onChange={(event) => setSpaceFilter(event.target.value)} value={spaceFilter}><option value="">All spaces</option>{spaces.map((space) => <option key={space.id} value={space.id}>{space.name}</option>)}</select></label>
          <label><span className="visually-hidden">Filter ingestion state</span><select aria-label="Filter ingestion state" onChange={(event) => setStateFilter(event.target.value as typeof stateFilter)} value={stateFilter}><option value="">All states</option><option value="preparing">Preparing</option><option value="queued">Queued</option><option value="running">Running</option><option value="retry_wait">Retry wait</option><option value="succeeded">Succeeded</option><option value="failed">Failed</option><option value="cancelled">Cancelled</option></select></label>
        </div>
        {initialLoading || (loading && jobs.length === 0) ? <ListSkeleton /> : null}
        {paginationError ? <ListUnavailable label="ingestion jobs" onRetry={() => void reloadJobs()} /> : null}
        {!initialLoading && !loading && !paginationError && jobs.length === 0 ? <div className="console-empty"><Layers3 size={23} /><strong>{spaceFilter || stateFilter ? "No matching ingestion jobs" : "No ingestion jobs"}</strong><span>{spaceFilter || stateFilter ? "Choose a different space or state." : "Uploaded sources will appear here as soon as preparation begins."}</span></div> : null}
        <div className="job-list">
          {jobs.map((job) => (
            <article className="job-card" key={job.id}>
              <div className="job-topline"><div><span className={`state-dot state-${job.state}`} /><code>{job.id}</code></div><span className={`status-pill status-${job.state}`}>{job.state}</span></div>
              <div className="job-context"><strong>{spaceLabel(spaces, job.space_id)}</strong><span>document {job.document_id}</span><span>attempt {job.attempt_count}/{job.max_attempts}</span></div>
              <div className="progress-row"><progress max={100} value={Math.max(0, Math.min(100, job.progress))} /><strong>{Math.round(job.progress)}%</strong></div>
              {job.last_error_code ? <p className="job-error">{job.last_error_code}</p> : null}
              {!['succeeded', 'failed', 'cancelled'].includes(job.state) ? <button aria-label={`Cancel job ${job.id}`} className="archive-button compact" disabled={saving} onClick={() => setPendingJobAction({ job, operation: "cancel" })} type="button">Cancel job</button> : null}
              {['failed', 'cancelled'].includes(job.state) ? <button aria-label={`Retry job ${job.id}`} className="secondary-button" disabled={saving} onClick={() => setPendingJobAction({ job, operation: "retry" })} type="button">Retry job</button> : null}
            </article>
          ))}
        </div>
        {jobTotalItems > 0 ? <PaginationControls loading={loading} loadingPage={jobLoadingPage} onPageChange={(nextPage) => void goToJobPage(nextPage)} page={jobPage} pageSize={jobPageSize} totalItems={jobTotalItems} totalPages={jobTotalPages} /> : null}
        {pendingJobAction ? <div aria-label={`${pendingJobAction.operation === "cancel" ? "Cancel" : "Retry"} job ${pendingJobAction.job.id}`} className="confirmation-strip" role="alertdialog"><div><strong>{pendingJobAction.operation === "cancel" ? "Cancel this ingestion job?" : "Retry this ingestion job?"}</strong><span>{pendingJobAction.operation === "cancel" ? "The worker will stop at a safe boundary; completed durable stages and audit history remain preserved." : "This starts a new durable attempt from the preserved original source and records the request in the audit trail."}</span></div><button className="secondary-button" onClick={() => setPendingJobAction(null)} type="button">Not now</button><button className={pendingJobAction.operation === "cancel" ? "danger-button" : "primary-button"} disabled={saving} onClick={() => void mutateJob()} type="button">Confirm {pendingJobAction.operation} job</button></div> : null}
      </section>
      {error ? <p className="inline-error wide" role="alert">{error}</p> : null}
    </ConsoleShell>
  );
}

export function ActivityConsole() {
  const member = useCurrentMember();
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [spacesReady, setSpacesReady] = useState(false);
  const [filterQuery, setFilterQuery] = useState("");
  const [actionFilter, setActionFilter] = useState("");
  const [outcomeFilter, setOutcomeFilter] = useState<"" | "success" | "denied" | "failed">("");
  const [resourceFilter, setResourceFilter] = useState("");
  const [appliedFilters, setAppliedFilters] = useState({ action: "", outcome: "" as "" | "success" | "denied" | "failed", q: "", resourceType: "" });
  const [error, setError] = useState<string | null>(null);
  const isAdmin = member.system_role === "super_admin";
  const loadAuditPage = useCallback((page: number, signal: AbortSignal) => {
    if (!isAdmin) {
      return Promise.resolve({ items: [], page: 1, page_size: 25, total_items: 0, total_pages: 0 });
    }
    return contractData(contractClient.GET("/api/v1/audit-events", {
      params: { query: {
        page,
        page_size: 25,
        ...(appliedFilters.q ? { q: appliedFilters.q } : {}),
        ...(appliedFilters.action ? { action: appliedFilters.action } : {}),
        ...(appliedFilters.outcome ? { outcome: appliedFilters.outcome } : {}),
        ...(appliedFilters.resourceType ? { resource_type: appliedFilters.resourceType } : {}),
      } },
      signal,
    }));
  }, [appliedFilters, isAdmin]);
  const {
    error: paginationError,
    initialLoading,
    items: events,
    page: auditPage,
    pageSize: auditPageSize,
    totalItems: auditTotalItems,
    totalPages: auditTotalPages,
    loading,
    loadingPage: auditLoadingPage,
    goToPage: goToAuditPage,
    reload: reloadAuditEvents,
  } = usePagePagination<AuditEvent>({
    loadPage: loadAuditPage,
    queryKey: JSON.stringify([isAdmin, appliedFilters]),
  });

  useEffect(() => {
    let active = true;
    loadAccessibleSpaces().then((spaceResponse) => {
      if (!active) {
        return;
      }
      setSpaces(spaceResponse);
      setSpacesReady(true);
    }).catch((loadError) => {
      if (active) {
        setError(message(loadError));
        setSpacesReady(true);
      }
    });
    return () => {
      active = false;
    };
  }, []);

  return (
    <ConsoleShell
      description="Review security-sensitive mutations with actor, resource, outcome, timestamp, and request correlation."
      eyebrow="Immutable evidence"
      member={memberView(member)}
      spaceCount={spacesReady ? spaces.length : null}
      title="Activity"
    >
      {!isAdmin ? <section className="console-panel permission-panel"><LockKeyhole size={25} /><div><h2>Audit access is restricted</h2><p>Only super admins can review family-wide audit events. Space membership remains visible to each space owner.</p></div></section> : (
        <section className="console-panel">
          <div className="panel-heading"><div><span>Security chronology</span><h2>Audit events</h2></div><Activity size={20} /></div>
          <form className="list-filter-bar audit-filter-bar" onSubmit={(event) => {
            event.preventDefault();
            setAppliedFilters({ action: actionFilter.trim(), outcome: outcomeFilter, q: filterQuery.trim(), resourceType: resourceFilter.trim() });
          }} role="search">
            <label><Search aria-hidden="true" size={15} /><input aria-label="Search audit events" onChange={(event) => setFilterQuery(event.target.value)} placeholder="Request, resource, action" type="search" value={filterQuery} /></label>
            <label><input aria-label="Filter audit action" onChange={(event) => setActionFilter(event.target.value)} placeholder="Exact action" value={actionFilter} /></label>
            <label><select aria-label="Filter audit outcome" onChange={(event) => setOutcomeFilter(event.target.value as typeof outcomeFilter)} value={outcomeFilter}><option value="">All outcomes</option><option value="success">Success</option><option value="denied">Denied</option><option value="failed">Failed</option></select></label>
            <label><input aria-label="Filter audit resource type" onChange={(event) => setResourceFilter(event.target.value)} placeholder="Resource type" value={resourceFilter} /></label>
            <button className="secondary-button" type="submit">Apply</button>
          </form>
          {initialLoading || (loading && events.length === 0) ? <ListSkeleton /> : null}
          {paginationError ? <ListUnavailable label="audit events" onRetry={() => void reloadAuditEvents()} /> : null}
          <div className="audit-list">
            {events.map((event) => <article className="audit-row" key={event.id}><span className={`audit-outcome outcome-${event.outcome}`} /><time dateTime={event.occurred_at}>{formatDateTime(event.occurred_at)}</time><div><h3>{event.action}</h3><p>{event.resource_type}{event.resource_id ? ` · ${event.resource_id}` : ""}</p></div><code>{event.request_id}</code><span className="status-pill">{event.outcome}</span></article>)}
            {!initialLoading && !loading && !paginationError && events.length === 0 ? <div className="console-empty"><Activity size={23} /><strong>No audit events returned</strong></div> : null}
          </div>
          {auditTotalItems > 0 ? <PaginationControls loading={loading} loadingPage={auditLoadingPage} onPageChange={(nextPage) => void goToAuditPage(nextPage)} page={auditPage} pageSize={auditPageSize} totalItems={auditTotalItems} totalPages={auditTotalPages} /> : null}
        </section>
      )}
      {error ? <p className="inline-error wide" role="alert">{error}</p> : null}
    </ConsoleShell>
  );
}

type AIManagementTool = components["schemas"]["AIToolDescriptor"];
type PendingAIAction = components["schemas"]["PendingAIAction"];

function aiActionImpact(toolName: string) {
  if (toolName === "spaces.members.set.v1") {
    return "This changes who can access this space and which actions that member can perform. Approval remains bound to the exact space, member, role, revision, and expiration.";
  }
  if (toolName === "ingestion_jobs.cancel.v1") {
    return "This requests cancellation at a safe worker boundary while preserving completed stages and audit history.";
  }
  if (toolName === "ingestion_jobs.retry.v1") {
    return "This requests a new durable attempt from the preserved original source after current-state validation.";
  }
  if (toolName === "settings.propose.v1") {
    return "This creates a validated settings draft only. Activation remains a separate recent-step-up action with optimistic concurrency.";
  }
  if (toolName === "knowledge.archive.v1" || toolName === "spaces.archive.v1") {
    return "This permanently removes it from unified search while preserving its version and audit history. Approval is bound to the exact target, revision, member, and expiration.";
  }
  return "Approval is bound to this exact command, target, member, current state, and expiration.";
}

export function AiActionsConsole() {
  const member = useCurrentMember();
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [spacesReady, setSpacesReady] = useState(false);
  const [tools, setTools] = useState<AIManagementTool[]>([]);
  const [reviewing, setReviewing] = useState<PendingAIAction | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadActionPage = useCallback((page: number, signal: AbortSignal) => contractData(
    contractClient.GET("/api/v1/ai-actions", { params: { query: { page, page_size: 25 } }, signal }),
  ), []);
  const actionPages = usePagePagination<PendingAIAction>({ loadPage: loadActionPage, queryKey: "pending" });
  const pendingActions = actionPages.items;

  useEffect(() => {
    let active = true;
    Promise.all([
      loadAccessibleSpaces(),
      contractData(contractClient.GET("/api/v1/ai-tools")),
    ]).then(([spaceResponse, toolResponse]) => {
      if (!active) {
        return;
      }
      setSpaces(spaceResponse);
      setSpacesReady(true);
      setTools(toolResponse.items);
    }).catch((loadError) => {
      if (active) {
        setError(message(loadError));
        setSpacesReady(true);
      }
    });
    return () => {
      active = false;
    };
  }, []);

  const confirmAction = async () => {
    if (!reviewing || saving) {
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await contractData(contractClient.POST("/api/v1/ai-actions/{action_id}/confirm", {
        params: { header: { "Idempotency-Key": idempotencyKey() }, path: { action_id: reviewing.id } },
      }));
      setReviewing(null);
      actionPages.reload();
    } catch (confirmError) {
      setError(message(confirmError));
    } finally {
      setSaving(false);
    }
  };

  return (
    <ConsoleShell
      description="A typed management surface for trusted assistants. Identity, scope, idempotency, audit, and confirmations remain server-enforced."
      eyebrow="Tool contracts"
      member={memberView(member)}
      spaceCount={spacesReady ? spaces.length : null}
      title="AI actions"
    >
      <section className="tool-principle"><Sparkles size={22} /><div><strong>Plain requests in, deliberate operations out</strong><p>Read actions can run directly. High-impact writes require a short-lived confirmation before execution.</p></div></section>
      <div className="tool-grid">
        {tools.map((tool, index) => <article className="tool-card" key={tool.name}><div><span className={`tool-icon accent-${index % 4}`}><Code2 size={17} /></span><span className={`mode-pill mode-${tool.confirmation === "required" ? "confirm" : "read"}`}>{tool.confirmation === "required" ? "Confirm" : "Direct"}</span></div><code>{tool.name}</code><p>{tool.description}</p></article>)}
      </div>
      {actionPages.initialLoading || (actionPages.loading && pendingActions.length === 0) ? <section className="console-panel pending-ai-panel"><ListSkeleton compact rows={3} /></section> : null}
      {actionPages.error ? <section className="console-panel pending-ai-panel"><ListUnavailable label="pending actions" onRetry={() => void actionPages.reload()} /></section> : null}
      {!actionPages.initialLoading && !actionPages.loading && !actionPages.error && pendingActions.length > 0 ? <section className="console-panel pending-ai-panel"><div className="panel-heading"><div><span>Human approval</span><h2>Pending confirmation</h2></div><span className="count-pill">{actionPages.totalItems}</span></div><div className="data-list">{pendingActions.map((action) => <article className="data-row" key={action.id}><span className="row-leading violet"><Sparkles size={17} /></span><div className="row-copy"><h3>{action.tool_name}</h3><p>{action.target_ids.join(", ")} · revision {action.expected_revision ?? "—"}</p></div><button aria-label={`Review ${action.tool_name} for ${action.target_ids.join(", ")}`} className="row-action-button" onClick={() => setReviewing(action)} type="button">Review</button></article>)}</div>{actionPages.totalItems > 0 ? <PaginationControls loading={actionPages.loading} loadingPage={actionPages.loadingPage} onPageChange={(nextPage) => void actionPages.goToPage(nextPage)} page={actionPages.page} pageSize={actionPages.pageSize} totalItems={actionPages.totalItems} totalPages={actionPages.totalPages} /> : null}{reviewing ? <div aria-label={`Confirm ${reviewing.tool_name}`} className="confirmation-strip" role="alertdialog"><div><strong>Execute {reviewing.tool_name}?</strong><span>{aiActionImpact(reviewing.tool_name)}</span></div><button className="secondary-button" onClick={() => setReviewing(null)} type="button">Not now</button><button className="danger-button" disabled={saving} onClick={() => void confirmAction()} type="button">Confirm AI action</button></div> : null}</section> : null}
      {!actionPages.initialLoading && !actionPages.loading && !actionPages.error && pendingActions.length === 0 ? <section className="console-panel pending-ai-panel"><div className="console-empty small"><Sparkles size={20} /><strong>No actions need approval</strong></div></section> : null}
      {error ? <p className="inline-error wide" role="alert">{error}</p> : null}
    </ConsoleShell>
  );
}
