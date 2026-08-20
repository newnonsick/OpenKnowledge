import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ActivityConsole, AiActionsConsole, ExploreConsole, IngestionConsole, KnowledgeConsole, PeopleConsole, SettingsConsole, SourcesConsole, SpacesConsole } from "@/components/management-console";
import { apiMultipart, apiRequest } from "@/lib/api-client";

const currentMember = vi.hoisted(() => ({ display_name: "Mai", system_role: "member" as "member" | "super_admin" }));

vi.mock("next/navigation", () => ({
  usePathname: () => "/spaces",
  useRouter: () => ({ replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams("q=water"),
}));

vi.mock("@/components/auth/session-gate", () => ({
  useCurrentMember: () => currentMember,
}));

vi.mock("@/lib/api-client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api-client")>();
  return { ...original, apiMultipart: vi.fn(), apiRequest: vi.fn() };
});

describe("management console", () => {
  beforeEach(() => {
    currentMember.display_name = "Mai";
    currentMember.system_role = "member";
    vi.mocked(apiRequest).mockReset();
    vi.mocked(apiMultipart).mockReset();
  });

  it("creates a private space and refreshes the accessible list", async () => {
    vi.mocked(apiRequest)
      .mockResolvedValueOnce({ items: [{ id: "global", name: "Family Shared", role: "editor", revision: 1 }], next_cursor: null })
      .mockResolvedValueOnce({ id: "travel", name: "Travel plans", role: "owner", revision: 1 })
      .mockResolvedValueOnce({ items: [
        { id: "global", name: "Family Shared", role: "editor", revision: 1 },
        { id: "travel", name: "Travel plans", role: "owner", revision: 1 },
      ], next_cursor: null });
    render(<SpacesConsole />);

    expect(await screen.findByText("Family Shared")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Space name"), { target: { value: "Travel plans" } });
    fireEvent.click(screen.getByRole("button", { name: "Create space" }));

    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/spaces", {
      body: { name: "Travel plans" },
      idempotent: true,
      method: "POST",
    }));
    expect(await screen.findByText("Travel plans")).toBeInTheDocument();
  });

  it("lets a space owner add and remove family members with explicit confirmation", async () => {
    let added = false;
    let removed = false;
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path === "/api/v1/spaces?limit=100") {
        return { items: [{ id: "travel", name: "Travel plans", role: "owner", revision: 1 }], next_cursor: null } as never;
      }
      if (path === "/api/v1/spaces/travel/member-candidates") {
        return { items: added ? [] : [{ member_id: "member-2", username: "nana", display_name: "Nana" }], next_cursor: null } as never;
      }
      if (path === "/api/v1/spaces/travel/members") {
        return {
          items: [
            { member_id: "member-1", username: "mai", display_name: "Mai", status: "active", role: "owner" },
            ...(!removed && added ? [{ member_id: "member-2", username: "nana", display_name: "Nana", status: "active", role: "reader" }] : []),
          ],
          next_cursor: null,
        } as never;
      }
      if (path === "/api/v1/spaces/travel/members/member-2" && options?.method === "PUT") {
        added = true;
        return { member_id: "member-2", role: "reader", space_id: "travel" } as never;
      }
      if (path === "/api/v1/spaces/travel/members/member-2" && options?.method === "DELETE") {
        removed = true;
        return undefined as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<SpacesConsole />);

    fireEvent.click(await screen.findByRole("button", { name: "Manage access for Travel plans" }));
    expect(await screen.findByRole("option", { name: "Nana (@nana)" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add member" }));

    expect(await screen.findByText("@nana")).toBeInTheDocument();
    expect(apiRequest).toHaveBeenCalledWith("/api/v1/spaces/travel/members/member-2", {
      body: { role: "reader" },
      idempotent: true,
      method: "PUT",
    });

    fireEvent.click(screen.getByRole("button", { name: "Remove Nana" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm removal" }));

    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/spaces/travel/members/member-2", {
      idempotent: true,
      method: "DELETE",
    }));
    await waitFor(() => expect(screen.queryByText("@nana")).not.toBeInTheDocument());
  });

  it("archives an owned space only after showing its impact", async () => {
    let archived = false;
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path === "/api/v1/spaces?limit=100") {
        return { items: archived ? [] : [{ id: "travel", name: "Travel plans", role: "owner", revision: 1 }], next_cursor: null } as never;
      }
      if (path === "/api/v1/spaces/travel/members") {
        return { items: [{ member_id: "member-1", username: "mai", display_name: "Mai", status: "active", role: "owner" }], next_cursor: null } as never;
      }
      if (path === "/api/v1/spaces/travel/member-candidates") {
        return { items: [], next_cursor: null } as never;
      }
      if (path === "/api/v1/spaces/travel" && options?.method === "DELETE") {
        archived = true;
        return undefined as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<SpacesConsole />);

    fireEvent.click(await screen.findByRole("button", { name: "Manage access for Travel plans" }));
    fireEvent.click(await screen.findByRole("button", { name: "Archive Travel plans" }));
    expect(screen.getByText(/leave unified search immediately/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm archive space" }));

    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/spaces/travel", { idempotent: true, method: "DELETE" }));
    await waitFor(() => expect(screen.queryByText("Travel plans")).not.toBeInTheDocument());
  });

  it("captures a versioned knowledge item in the selected space", async () => {
    let created = false;
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [{ id: "global", name: "Family Shared", role: "editor", revision: 1 }], next_cursor: null } as never;
      }
      if (path.startsWith("/api/v1/knowledge") && options?.method === "POST") {
        created = true;
        return { id: "note-1", title: "Water valve", version: 1 } as never;
      }
      if (path.startsWith("/api/v1/knowledge")) {
        return { items: created ? [{ id: "note-1", space_id: "global", title: "Water valve", tags: ["home"], version: 1, updated_at: "2026-08-20T12:00:00Z" }] : [], next_cursor: null } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<KnowledgeConsole />);

    await screen.findByRole("option", { name: "Family Shared" });
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Water valve" } });
    fireEvent.change(screen.getByLabelText("Knowledge content"), { target: { value: "Turn the red handle clockwise." } });
    fireEvent.change(screen.getByLabelText("Tags"), { target: { value: "home" } });
    fireEvent.click(screen.getByRole("button", { name: "Capture knowledge" }));

    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/knowledge", {
      body: {
        content: "Turn the red handle clockwise.",
        space_id: "global",
        tags: ["home"],
        title: "Water valve",
      },
      idempotent: true,
      method: "POST",
    }));
    expect(await screen.findByText("Water valve")).toBeInTheDocument();
  });

  it("edits and archives knowledge with optimistic concurrency and confirmation", async () => {
    let version = 1;
    let archived = false;
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [{ id: "global", name: "Family Shared", role: "editor", revision: 1 }], next_cursor: null } as never;
      }
      if (path === "/api/v1/knowledge?limit=100") {
        return { items: archived ? [] : [{ id: "note-1", space_id: "global", title: "Water valve", tags: ["home"], version, updated_at: "2026-08-20T12:00:00Z" }], next_cursor: null } as never;
      }
      if (path === "/api/v1/knowledge/note-1" && !options?.method) {
        return { id: "note-1", space_id: "global", title: "Water valve", content: "Turn clockwise.", tags: ["home"], version, updated_at: "2026-08-20T12:00:00Z" } as never;
      }
      if (path === "/api/v1/knowledge/note-1" && options?.method === "PUT") {
        version = 2;
        return { id: "note-1", space_id: "global", title: "Water valve", content: "Turn clockwise, then close the main tap.", tags: ["home"], version, updated_at: "2026-08-20T12:05:00Z" } as never;
      }
      if (path === "/api/v1/knowledge/note-1?expected_version=2" && options?.method === "DELETE") {
        archived = true;
        return undefined as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<KnowledgeConsole />);

    fireEvent.click(await screen.findByRole("button", { name: "Edit Water valve" }));
    fireEvent.change(await screen.findByLabelText("Edit content"), { target: { value: "Turn clockwise, then close the main tap." } });
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));

    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/knowledge/note-1", {
      body: {
        change_summary: "Updated through the management console",
        content: "Turn clockwise, then close the main tap.",
        expected_version: 1,
        tags: ["home"],
        title: "Water valve",
      },
      idempotent: true,
      method: "PUT",
    }));
    expect(await screen.findByText("Version 2 is active")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Archive Water valve" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm archive" }));

    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/knowledge/note-1?expected_version=2", {
      idempotent: true,
      method: "DELETE",
    }));
    await waitFor(() => expect(screen.queryByText("Version 2 is active")).not.toBeInTheDocument());
  });

  it("searches every accessible space and exposes degraded semantic health", async () => {
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [{ id: "global", name: "Family Shared", role: "reader", revision: 1 }], next_cursor: null } as never;
      }
      if (path === "/api/v1/retrieval/search" && options?.method === "POST") {
        return {
          hits: [{ canonical_id: "note-1", citation_uri: "knowledge://note-1", content_excerpt: "Turn the water valve clockwise.", rank: 1, rank_score: 0.9, source_type: "knowledge_revision", space_id: "global", title: "Water valve", version: 2 }],
          health: { semantic_status: "degraded", degraded_reasons: ["embedding_unavailable"] },
          explanation: { effective_space_ids: ["global"], abstained: false },
        } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<ExploreConsole />);

    expect(await screen.findByDisplayValue("water")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Search knowledge" }));

    expect(await screen.findByText("Turn the water valve clockwise.")).toBeInTheDocument();
    expect(screen.getByText("Semantic layer unavailable · lexical results remain active")).toBeInTheDocument();
  });

  it("uploads a source into the durable ingestion queue", async () => {
    let uploaded = false;
    vi.mocked(apiRequest).mockImplementation(async (path) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [{ id: "global", name: "Family Shared", role: "editor", revision: 1 }], next_cursor: null } as never;
      }
      if (path.startsWith("/api/v1/sources")) {
        return { items: uploaded ? [{ id: "source-1", space_id: "global", display_name: "Procedures", status: "pending", original_filename: "procedures.txt", size_bytes: 32, updated_at: "2026-08-20T12:00:00Z" }] : [], next_cursor: null } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    vi.mocked(apiMultipart).mockImplementation(async () => {
      uploaded = true;
      return { document_id: "source-1", job_id: "job-1", job_state: "queued" } as never;
    });
    render(<SourcesConsole />);

    await screen.findByRole("option", { name: "Family Shared" });
    const file = new File(["Turn off the water valve."], "procedures.txt", { type: "text/plain" });
    fireEvent.change(screen.getByLabelText("Source file"), { target: { files: [file] } });
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Procedures" } });
    const queueButton = screen.getByRole("button", { name: "Queue source" });
    await waitFor(() => expect(queueButton).toBeEnabled());
    fireEvent.submit(queueButton.closest("form")!);

    await waitFor(() => expect(apiMultipart).toHaveBeenCalledWith("/api/v1/sources/upload", expect.any(FormData), { idempotent: true }));
    expect(await screen.findByText(/procedures\.txt/)).toBeInTheDocument();
    expect(screen.getByText("Queued for durable ingestion")).toBeInTheDocument();
  });

  it("reveals a generated member password exactly in the creation result", async () => {
    currentMember.system_role = "super_admin";
    let created = false;
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [{ id: "global", name: "Family Shared", role: "owner", revision: 1 }], next_cursor: null } as never;
      }
      if (path.startsWith("/api/v1/members") && options?.method === "POST") {
        created = true;
        return { id: "member-1", username: "nana", display_name: "Nana", temporary_password: "Temp-Only-Once!42", temporary_password_expires_at: "2026-08-21T12:00:00Z", requires_password_change: true } as never;
      }
      if (path.startsWith("/api/v1/members")) {
        return { items: created ? [{ id: "member-1", username: "nana", display_name: "Nana", status: "active", system_role: "member", requires_password_change: true }] : [], next_cursor: null } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<PeopleConsole />);

    await screen.findByRole("heading", { name: "Create a member" });
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "nana" } });
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Nana" } });
    fireEvent.click(screen.getByRole("button", { name: "Generate member" }));

    expect(await screen.findByText("Temp-Only-Once!42")).toBeInTheDocument();
    expect(screen.getByText(/shown only once/i)).toBeInTheDocument();
  });

  it("resets and disables a member only after explicit confirmation", async () => {
    currentMember.system_role = "super_admin";
    let disabled = false;
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [{ id: "global", name: "Family Shared", role: "owner", revision: 1 }], next_cursor: null } as never;
      }
      if (path === "/api/v1/members?limit=100") {
        return { items: [{ id: "member-1", username: "nana", display_name: "Nana", status: disabled ? "disabled" : "active", system_role: "member", requires_password_change: false }], next_cursor: null } as never;
      }
      if (path === "/api/v1/members/member-1/password-reset" && options?.method === "POST") {
        return { id: "member-1", temporary_password: "Reset-Only-Once!42", temporary_password_expires_at: "2026-08-21T12:00:00Z", requires_password_change: true } as never;
      }
      if (path === "/api/v1/members/member-1" && options?.method === "PATCH") {
        disabled = true;
        return { id: "member-1", username: "nana", display_name: "Nana", status: "disabled", system_role: "member", requires_password_change: true } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<PeopleConsole />);

    fireEvent.click(await screen.findByRole("button", { name: "Manage Nana" }));
    fireEvent.click(screen.getByRole("button", { name: "Reset Nana password" }));
    expect(screen.getByText(/signs out every session/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm password reset" }));
    expect(await screen.findByText("Reset-Only-Once!42")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Disable Nana" }));
    expect(screen.getByText(/immediately revokes sessions and api keys/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm disable member" }));
    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/members/member-1", {
      body: { display_name: "Nana", status: "disabled", system_role: "member" },
      idempotent: true,
      method: "PATCH",
    }));
    expect(await screen.findByText("disabled")).toBeInTheDocument();
  });

  it("creates an individually scoped API key and reveals the secret once", async () => {
    let created = false;
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [{ id: "global", name: "Family Shared", role: "editor", revision: 1 }], next_cursor: null } as never;
      }
      if (path === "/api/v1/api-keys" && options?.method === "POST") {
        created = true;
        return { id: "key-1", public_id: "pk_live_1", secret: "aigw_v1_once_only", scopes: ["knowledge:read"] } as never;
      }
      if (path.startsWith("/api/v1/api-keys")) {
        return { items: created ? [{ id: "key-1", public_id: "pk_live_1", name: "Laptop", status: "active", scopes: ["knowledge:read"], created_at: "2026-08-20T12:00:00Z" }] : [], next_cursor: null } as never;
      }
      if (path.startsWith("/api/v1/sessions")) {
        return { items: [], next_cursor: null } as never;
      }
      if (path === "/api/v1/settings") {
        return { revision: 0, state: "active", values: { retrieval: { limit: 20 } } } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<SettingsConsole />);

    await screen.findByRole("heading", { name: "API keys" });
    fireEvent.change(screen.getByLabelText("Key name"), { target: { value: "Laptop" } });
    fireEvent.click(screen.getByRole("button", { name: "Create API key" }));

    expect(await screen.findByText("aigw_v1_once_only")).toBeInTheDocument();
    expect(screen.getByText(/copy this key now/i)).toBeInTheDocument();
  });

  it("revokes API keys and other website sessions only after confirmation", async () => {
    let keyRevoked = false;
    let sessionRevoked = false;
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [], next_cursor: null } as never;
      }
      if (path === "/api/v1/api-keys" && !options?.method) {
        return { items: keyRevoked ? [] : [{ id: "key-1", public_id: "pk_live_1", name: "Laptop", status: "active", scopes: ["knowledge:read"], created_at: "2026-08-20T12:00:00Z" }], next_cursor: null } as never;
      }
      if (path === "/api/v1/api-keys/key-1" && options?.method === "DELETE") {
        keyRevoked = true;
        return undefined as never;
      }
      if (path === "/api/v1/sessions" && !options?.method) {
        return { items: [
          { id: "session-current", current: true, status: "active", created_at: "2026-08-20T12:00:00Z", last_activity_at: "2026-08-20T12:00:00Z" },
          ...(!sessionRevoked ? [{ id: "session-other", current: false, status: "active", created_at: "2026-08-19T12:00:00Z", last_activity_at: "2026-08-19T13:00:00Z" }] : []),
        ], next_cursor: null } as never;
      }
      if (path === "/api/v1/sessions/session-other" && options?.method === "DELETE") {
        sessionRevoked = true;
        return undefined as never;
      }
      if (path === "/api/v1/settings") {
        return { revision: 0, state: "active", values: { retrieval: { limit: 20 } } } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<SettingsConsole />);

    fireEvent.click(await screen.findByRole("button", { name: "Revoke Laptop" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm revoke API key" }));
    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/api-keys/key-1", { idempotent: true, method: "DELETE" }));
    await waitFor(() => expect(screen.queryByText("pk_live_1 · knowledge:read")).not.toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Sign out website session" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirm sign out" }));
    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/sessions/session-other", { idempotent: true, method: "DELETE" }));
    expect(screen.getByText("This session")).toBeInTheDocument();
  });

  it("creates and activates a validated safe runtime settings draft", async () => {
    currentMember.system_role = "super_admin";
    const activeValues = { retrieval: { limit: 20, lexical_weight: 1, vector_weight: 1 } };
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [], next_cursor: null } as never;
      }
      if (path === "/api/v1/api-keys") {
        return { items: [], next_cursor: null } as never;
      }
      if (path === "/api/v1/sessions") {
        return { items: [], next_cursor: null } as never;
      }
      if (path === "/api/v1/settings" && !options?.method) {
        return { id: null, revision: 0, base_revision: 0, state: "active", values: activeValues } as never;
      }
      if (path === "/api/v1/settings/drafts" && options?.method === "POST") {
        return { id: "draft-1", revision: 1, base_revision: 0, state: "draft", values: { retrieval: { ...activeValues.retrieval, limit: 15 } } } as never;
      }
      if (path === "/api/v1/settings/drafts/draft-1/activate" && options?.method === "POST") {
        return { id: "draft-1", revision: 1, base_revision: 0, state: "active", values: { retrieval: { ...activeValues.retrieval, limit: 15 } } } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<SettingsConsole />);

    fireEvent.change(await screen.findByLabelText("Retrieval result limit"), { target: { value: "15" } });
    fireEvent.change(screen.getByLabelText("Change reason"), { target: { value: "Improve focused family search" } });
    fireEvent.click(screen.getByRole("button", { name: "Create validated draft" }));

    expect(await screen.findByText("Draft revision 1 ready")).toBeInTheDocument();
    expect(apiRequest).toHaveBeenCalledWith("/api/v1/settings/drafts", {
      body: {
        base_revision: 0,
        reason: "Improve focused family search",
        values: { retrieval: { limit: 15, lexical_weight: 1, vector_weight: 1 } },
      },
      idempotent: true,
      method: "POST",
    });

    fireEvent.click(screen.getByRole("button", { name: "Activate settings" }));
    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/settings/drafts/draft-1/activate", {
      body: { expected_active_revision: 0, reason: "Improve focused family search" },
      idempotent: true,
      method: "POST",
    }));
    expect(await screen.findByText("Revision 1 is active")).toBeInTheDocument();
  });

  it("shows durable ingestion state without inventing progress", async () => {
    vi.mocked(apiRequest).mockImplementation(async (path) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [{ id: "global", name: "Family Shared", role: "editor", revision: 1 }], next_cursor: null } as never;
      }
      if (path.startsWith("/api/v1/ingestion-jobs")) {
        return { items: [{ id: "job-1", space_id: "global", document_id: "doc-1", state: "queued", progress: 0, attempt_count: 0, max_attempts: 5, created_at: "2026-08-20T12:00:00Z", updated_at: "2026-08-20T12:00:00Z" }], next_cursor: null } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<IngestionConsole />);

    expect(await screen.findByText("job-1")).toBeInTheDocument();
    expect(screen.getByText("0%")) .toBeInTheDocument();
    expect(screen.getByText("queued")).toBeInTheDocument();
  });

  it("shows immutable audit activity to super admins", async () => {
    currentMember.system_role = "super_admin";
    vi.mocked(apiRequest).mockImplementation(async (path) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [], next_cursor: null } as never;
      }
      if (path.startsWith("/api/v1/audit-events")) {
        return { items: [{ id: "event-1", occurred_at: "2026-08-20T12:00:00Z", actor_kind: "session", action: "knowledge.create", resource_type: "knowledge_item", resource_id: "note-1", outcome: "success", request_id: "request-1" }], next_cursor: null } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<ActivityConsole />);

    expect(await screen.findByText("knowledge.create")).toBeInTheDocument();
    expect(screen.getByText("request-1")).toBeInTheDocument();
  });

  it("presents AI management tools as deliberate actions without a chat surface", async () => {
    let confirmed = false;
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path === "/api/v1/spaces?limit=100") {
        return { items: [{ id: "private", name: "Private", role: "owner", revision: 1 }], next_cursor: null } as never;
      }
      if (path === "/api/v1/ai-tools") {
        return { items: [{ name: "spaces.archive.v1", description: "Archive an owned space.", confirmation: "required", parameters: {} }] } as never;
      }
      if (path === "/api/v1/ai-actions") {
        return { items: confirmed ? [] : [{ id: "action-1", tool_name: "spaces.archive.v1", target_ids: ["private"], expected_revision: 1, status: "pending", created_at: "2026-08-20T12:00:00Z", expires_at: "2026-08-20T12:10:00Z" }], next_cursor: null } as never;
      }
      if (path === "/api/v1/ai-actions/action-1/confirm" && options?.method === "POST") {
        confirmed = true;
        return { pending_action_id: "action-1", status: "executed", tool_name: "spaces.archive.v1" } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<AiActionsConsole />);

    expect((await screen.findAllByText("spaces.archive.v1")).length).toBe(2);
    expect(screen.queryByText(/chat/i)).not.toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Review spaces.archive.v1 for private" }));
    expect(screen.getByText(/permanently removes it from unified search/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm AI action" }));
    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/ai-actions/action-1/confirm", { idempotent: true, method: "POST" }));
    await waitFor(() => expect(screen.queryByText("Pending confirmation")).not.toBeInTheDocument());
  });
});
