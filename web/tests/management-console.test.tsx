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
    vi.mocked(apiRequest).mockResolvedValue({ items: [], next_cursor: null } as never);
    render(<AiActionsConsole />);

    expect(await screen.findByText("list_spaces")).toBeInTheDocument();
    expect(screen.getByText("create_space")).toBeInTheDocument();
    expect(screen.getByText("upload_source")).toBeInTheDocument();
    expect(screen.queryByText(/chat/i)).not.toBeInTheDocument();
  });
});
