import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ActivityConsole, AiActionsConsole, ExploreConsole, IngestionConsole, KnowledgeConsole, PeopleConsole, SettingsConsole, SourcesConsole, SpacesConsole } from "@/components/management-console";
import { apiMultipart, apiRequest } from "@/lib/api-client";

const currentMember = vi.hoisted(() => ({ display_name: "Mai", id: "admin-1", mfa_enabled: false, system_role: "member" as "member" | "super_admin" }));
const navigation = vi.hoisted(() => ({ replace: vi.fn(), search: "q=water" }));

vi.mock("next/navigation", () => ({
  usePathname: () => "/spaces",
  useRouter: () => ({ replace: navigation.replace }),
  useSearchParams: () => new URLSearchParams(navigation.search),
}));

vi.mock("@/components/auth/session-gate", () => ({
  useCurrentMember: () => currentMember,
}));

vi.mock("@/lib/api-client", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api-client")>();
  const apiRequest = vi.fn();
  const request = (method: "DELETE" | "GET" | "PATCH" | "POST" | "PUT") => (
    path: string,
    options: {
      body?: unknown;
      params?: {
        header?: Record<string, string>;
        path?: Record<string, string>;
        query?: Record<string, boolean | number | string | null | undefined>;
      };
    } = {},
  ) => {
    let resolvedPath = path;
    for (const [name, value] of Object.entries(options.params?.path || {})) {
      resolvedPath = resolvedPath.replace(`{${name}}`, encodeURIComponent(value));
    }
    const query = new URLSearchParams();
    const queryEntries = Object.entries(options.params?.query || {}).sort(([left], [right]) => {
      const order = ["limit"];
      const leftIndex = order.indexOf(left);
      const rightIndex = order.indexOf(right);
      return (leftIndex < 0 ? order.length : leftIndex) - (rightIndex < 0 ? order.length : rightIndex) || left.localeCompare(right);
    });
    const requestedPage = options.params?.query?.page;
    for (const [name, value] of queryEntries) {
      if (value !== undefined && value !== null) {
        if (name === "page") {
          continue;
        }
        query.set(name === "page_size" ? "limit" : name, String(value));
      }
    }
    if (requestedPage !== undefined && requestedPage !== null && String(requestedPage) !== "1") {
      query.set("page", String(requestedPage));
    }
    if (query.size > 0) {
      resolvedPath += `?${query.toString()}`;
    }
    const legacyOptions: {
      body?: unknown;
      idempotent?: boolean;
      method?: "DELETE" | "PATCH" | "POST" | "PUT";
    } = {};
    if (options.body !== undefined) {
      legacyOptions.body = options.body;
    }
    if (options.params?.header?.["Idempotency-Key"]) {
      legacyOptions.idempotent = true;
    }
    if (method !== "GET") {
      legacyOptions.method = method;
    }
    return apiRequest(resolvedPath, legacyOptions);
  };
  return {
    ...original,
    apiMultipart: vi.fn(),
    apiRequest,
    contractClient: {
      DELETE: request("DELETE"),
      GET: request("GET"),
      PATCH: request("PATCH"),
      POST: request("POST"),
      PUT: request("PUT"),
    },
    contractData: async <T,>(pending: Promise<T>) => {
      const response = await pending;
      if (response && typeof response === "object" && "items" in response && Array.isArray(response.items) && !("page" in response)) {
        return {
          ...response,
          page: 1,
          page_size: 25,
          total_items: response.items.length,
          total_pages: response.items.length ? 1 : 0,
        } as T;
      }
      return response;
    },
  };
});

describe("management console", () => {
  beforeEach(() => {
    currentMember.display_name = "Mai";
    currentMember.id = "admin-1";
    currentMember.mfa_enabled = false;
    currentMember.system_role = "member";
    navigation.replace.mockReset();
    navigation.search = "q=water";
    vi.mocked(apiRequest).mockReset();
    vi.mocked(apiMultipart).mockReset();
  });

  it("creates a private space and refreshes the accessible list", async () => {
    let created = false;
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (options?.method === "POST" && path === "/api/v1/spaces") {
        created = true;
        return { id: "travel", name: "Travel plans", role: "owner", revision: 1 } as never;
      }
      if (path === "/api/v1/spaces?limit=1") {
        return { items: [], page: 1, page_size: 1, total_items: created ? 2 : 1, total_pages: 2 } as never;
      }
      if (path === "/api/v1/spaces?limit=25") {
        return {
          items: created
            ? [
                { id: "global", name: "Family Shared", role: "editor", revision: 1 },
                { id: "travel", name: "Travel plans", role: "owner", revision: 1 },
              ]
            : [{ id: "global", name: "Family Shared", role: "editor", revision: 1 }],
          page: 1,
          page_size: 25,
          total_items: created ? 2 : 1,
          total_pages: 1,
        } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
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

  it("shows an unavailable state instead of an empty knowledge library after the initial page request fails", async () => {
    vi.mocked(apiRequest).mockImplementation(async (path) => {
      if (path === "/api/v1/spaces?limit=100") {
        return { items: [{ id: "global", name: "Family Shared", role: "editor", revision: 1 }] } as never;
      }
      if (path === "/api/v1/knowledge?limit=25") {
        throw new Error("Gateway unavailable");
      }
      throw new Error(`Unexpected path ${path}`);
    });

    render(<KnowledgeConsole />);

    expect(await screen.findByText("Unable to load knowledge")).toBeInTheDocument();
    expect(screen.queryByText("Nothing captured yet")).not.toBeInTheDocument();
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Retry knowledge" })).toBeEnabled();
  });

  it("does not switch to an empty knowledge library while retrying a failed initial page", async () => {
    let knowledgeAttempts = 0;
    let resolveRetry: ((value: unknown) => void) | undefined;
    vi.mocked(apiRequest).mockImplementation((path) => {
      if (path === "/api/v1/spaces?limit=100") {
        return Promise.resolve({ items: [{ id: "global", name: "Family Shared", role: "editor", revision: 1 }] }) as never;
      }
      if (path === "/api/v1/knowledge?limit=25") {
        knowledgeAttempts += 1;
        if (knowledgeAttempts === 1) {
          return Promise.reject(new Error("Gateway unavailable")) as never;
        }
        return new Promise((resolve) => {
          resolveRetry = resolve;
        }) as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });

    render(<KnowledgeConsole />);

    fireEvent.click(await screen.findByRole("button", { name: "Retry knowledge" }));

    expect(screen.queryByText("Nothing captured yet")).not.toBeInTheDocument();
    resolveRetry?.({ items: [{ id: "note-1", space_id: "global", title: "Water valve", tags: ["home"], version: 1, updated_at: "2026-08-20T12:00:00Z" }] });
    expect(await screen.findByText("Water valve")).toBeInTheDocument();
  });

  it("lets a space owner add and remove family members with explicit confirmation", async () => {
    let added = false;
    let removed = false;
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path === "/api/v1/spaces?limit=25") {
        return { items: [{ id: "travel", name: "Travel plans", role: "owner", revision: 1 }] } as never;
      }
      if (path === "/api/v1/spaces/travel/member-candidates?limit=25") {
        return { items: added ? [] : [{ member_id: "member-2", username: "nana", display_name: "Nana" }] } as never;
      }
      if (path === "/api/v1/spaces/travel/members?limit=25") {
        return {
          items: [
            { member_id: "member-1", username: "mai", display_name: "Mai", status: "active", role: "owner" },
            ...(!removed && added ? [{ member_id: "member-2", username: "nana", display_name: "Nana", status: "active", role: "reader" }] : []),
          ],
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

    const accessTrigger = await screen.findByRole("button", { name: "Manage access for Travel plans" });
    accessTrigger.focus();
    fireEvent.click(accessTrigger);
    expect(await screen.findByRole("dialog", { name: "Travel plans access" })).toHaveAttribute("aria-modal", "true");
    expect(await screen.findByRole("option", { name: "Nana (@nana)" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add member" }));

    await waitFor(() => expect(vi.mocked(apiRequest).mock.calls.filter(([path]) => path === "/api/v1/spaces/travel/members?limit=25")).toHaveLength(2));
    expect(await screen.findByText("@nana")).toBeInTheDocument();
    expect(apiRequest).toHaveBeenCalledWith("/api/v1/spaces/travel/members/member-2", {
      body: { role: "reader" },
      idempotent: true,
      method: "PUT",
    });

    fireEvent.click(screen.getByRole("button", { name: "Remove Nana" }));
    expect(screen.getByRole("alertdialog", { name: "Remove Nana from Travel plans" })).toHaveAttribute("aria-modal", "true");
    fireEvent.click(screen.getByRole("button", { name: "Confirm removal" }));

    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/spaces/travel/members/member-2", {
      idempotent: true,
      method: "DELETE",
    }));
    await waitFor(() => expect(screen.queryByText("@nana")).not.toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Close access manager" }));
    await waitFor(() => expect(accessTrigger).toHaveFocus());
  });

  it("uses the step-up ownership command instead of an ordinary role change", async () => {
    vi.mocked(apiRequest).mockImplementation(async (path) => {
      if (path === "/api/v1/spaces?limit=25") {
        return { items: [{ id: "travel", name: "Travel plans", role: "owner", revision: 2 }] } as never;
      }
      if (path === "/api/v1/spaces/travel/member-candidates?limit=25") {
        return { items: [] } as never;
      }
      if (path === "/api/v1/spaces/travel/members?limit=25") {
        return { items: [
          { member_id: "member-1", username: "mai", display_name: "Mai", status: "active", role: "owner" },
          { member_id: "member-2", username: "nana", display_name: "Nana", status: "active", role: "editor" },
        ] } as never;
      }
      if (path === "/api/v1/spaces/travel/ownership") {
        return { member_id: "member-2", role: "owner", space_id: "travel" } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<SpacesConsole />);

    fireEvent.click(await screen.findByRole("button", { name: "Manage access for Travel plans" }));
    fireEvent.click(await screen.findByRole("button", { name: "Transfer ownership to Nana" }));
    expect(screen.getByRole("alertdialog", { name: "Transfer ownership to Nana" })).toHaveAttribute("aria-modal", "true");
    fireEvent.click(screen.getByRole("button", { name: "Confirm ownership transfer" }));

    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/spaces/travel/ownership", {
      body: { expected_revision: 2, target_member_id: "member-2" },
      idempotent: true,
      method: "PUT",
    }));
  });

  it("archives an owned space only after showing its impact", async () => {
    let archived = false;
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path === "/api/v1/spaces?limit=25") {
        return { items: archived ? [] : [{ id: "travel", name: "Travel plans", role: "owner", revision: 1 }] } as never;
      }
      if (path === "/api/v1/spaces/travel/members?limit=25") {
        return { items: [{ member_id: "member-1", username: "mai", display_name: "Mai", status: "active", role: "owner" }] } as never;
      }
      if (path === "/api/v1/spaces/travel/member-candidates?limit=25") {
        return { items: [] } as never;
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
    expect(screen.getByRole("alertdialog", { name: "Archive Travel plans" })).toHaveAttribute("aria-modal", "true");
    expect(screen.getByText(/leave unified search immediately/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm archive space" }));

    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/spaces/travel", { idempotent: true, method: "DELETE" }));
    await waitFor(() => expect(screen.queryByText("Travel plans")).not.toBeInTheDocument());
  });

  it.each([
    ["Remove Nana", "Confirm removal", "Remove Nana from Travel plans"],
    ["Transfer ownership to Nana", "Confirm ownership transfer", "Transfer ownership to Nana"],
    ["Archive Travel plans", "Confirm archive space", "Archive Travel plans"],
  ])("keeps a failed %s action visible in its confirmation", async (actionName, confirmName, dialogName) => {
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path === "/api/v1/spaces?limit=25") {
        return { items: [{ id: "travel", name: "Travel plans", role: "owner", revision: 2 }] } as never;
      }
      if (path === "/api/v1/spaces/travel/member-candidates?limit=25") {
        return { items: [] } as never;
      }
      if (path === "/api/v1/spaces/travel/members?limit=25") {
        return { items: [
          { member_id: "member-1", username: "mai", display_name: "Mai", status: "active", role: "owner" },
          { member_id: "member-2", username: "nana", display_name: "Nana", status: "active", role: "editor" },
        ] } as never;
      }
      if (options?.method === "DELETE" || options?.method === "PUT") {
        throw new Error("The requested space change could not be saved.");
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<SpacesConsole />);

    fireEvent.click(await screen.findByRole("button", { name: "Manage access for Travel plans" }));
    fireEvent.click(await screen.findByRole("button", { name: actionName }));
    fireEvent.click(screen.getByRole("button", { name: confirmName }));

    const dialog = screen.getByRole("alertdialog", { name: dialogName });
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("The request could not be completed.");
  });

  it("captures a versioned knowledge item in the selected space", async () => {
    let created = false;
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [{ id: "global", name: "Family Shared", role: "editor", revision: 1 }] } as never;
      }
      if (path.startsWith("/api/v1/knowledge") && options?.method === "POST") {
        created = true;
        return { id: "note-1", title: "Water valve", version: 1 } as never;
      }
      if (path.startsWith("/api/v1/knowledge")) {
        return { items: created ? [{ id: "note-1", space_id: "global", title: "Water valve", tags: ["home"], version: 1, updated_at: "2026-08-20T12:00:00Z" }] : [] } as never;
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

  it("loads every accessible-space page before offering native space selectors", async () => {
    vi.mocked(apiRequest).mockImplementation(async (path) => {
      if (path === "/api/v1/spaces?limit=100") {
        return {
          items: [{ id: "global", name: "Family Shared", role: "editor", revision: 1 }],
          page: 1,
          page_size: 100,
          total_items: 101,
          total_pages: 2,
        } as never;
      }
      if (path === "/api/v1/spaces?limit=100&page=2") {
        return {
          items: [{ id: "later", name: "Later space", role: "reader", revision: 1 }],
          page: 2,
          page_size: 100,
          total_items: 101,
          total_pages: 2,
        } as never;
      }
      if (path === "/api/v1/knowledge?limit=25") {
        return { items: [] } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<KnowledgeConsole />);

    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/spaces?limit=100&page=2", {}));
    expect(await screen.findByRole("option", { name: "Later space" })).toBeInTheDocument();
  });

  it("paginates knowledge and resets after filtering", async () => {
    vi.mocked(apiRequest).mockImplementation(async (path) => {
      if (path === "/api/v1/spaces?limit=100") {
        return { items: [{ id: "global", name: "Family Shared", role: "editor", revision: 1 }] } as never;
      }
      if (path === "/api/v1/knowledge?limit=25") {
        return {
          items: [{ id: "note-1", space_id: "global", title: "First note", tags: [], version: 1, updated_at: "2026-08-20T12:00:00Z" }],
          page: 1,
          page_size: 25,
          total_items: 2,
          total_pages: 2,
        } as never;
      }
      if (path === "/api/v1/knowledge?limit=25&page=2") {
        return {
          items: [{ id: "note-2", space_id: "global", title: "Second note", tags: [], version: 1, updated_at: "2026-08-20T11:00:00Z" }],
          page: 2,
          page_size: 25,
          total_items: 2,
          total_pages: 2,
        } as never;
      }
      if (path === "/api/v1/knowledge?limit=25&q=budget&tag=finance") {
        return {
          items: [{ id: "note-3", space_id: "global", title: "Budget plan", tags: ["finance"], version: 1, updated_at: "2026-08-20T10:00:00Z" }],
          page: 1,
          page_size: 25,
          total_items: 1,
          total_pages: 1,
        } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<KnowledgeConsole />);

    expect(await screen.findByText("First note")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(await screen.findByText("Second note")).toBeInTheDocument();
    expect(screen.queryByText("First note")).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Search knowledge list"), { target: { value: "budget" } });
    fireEvent.change(screen.getByLabelText("Filter by tag"), { target: { value: "finance" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply knowledge filters" }));

    expect(await screen.findByText("Budget plan")).toBeInTheDocument();
    expect(screen.queryByText("First note")).not.toBeInTheDocument();
  });

  it("keeps the current knowledge page stable and non-interactive while the next page loads", async () => {
    let resolveSecondPage: ((value: unknown) => void) | undefined;
    vi.mocked(apiRequest).mockImplementation((path) => {
      if (path === "/api/v1/spaces?limit=100") {
        return Promise.resolve({ items: [{ id: "global", name: "Family Shared", role: "editor", revision: 1 }] }) as never;
      }
      if (path === "/api/v1/knowledge?limit=25") {
        return Promise.resolve({ items: [{ id: "note-1", space_id: "global", title: "First note", tags: [], version: 1, updated_at: "2026-08-20T12:00:00Z" }], page: 1, page_size: 25, total_items: 2, total_pages: 2 }) as never;
      }
      if (path === "/api/v1/knowledge?limit=25&page=2") {
        return new Promise((resolve) => { resolveSecondPage = resolve; }) as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<KnowledgeConsole />);

    const firstTitle = await screen.findByText("First note");
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(firstTitle.closest(".data-list")).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("button", { name: "Edit First note" })).toBeDisabled();

    resolveSecondPage?.({ items: [{ id: "note-2", space_id: "global", title: "Second note", tags: [], version: 1, updated_at: "2026-08-20T11:00:00Z" }], page: 2, page_size: 25, total_items: 2, total_pages: 2 });
    expect(await screen.findByText("Second note")).toBeInTheDocument();
  });

  it("edits and archives knowledge with optimistic concurrency and confirmation", async () => {
    let version = 1;
    let archived = false;
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [{ id: "global", name: "Family Shared", role: "editor", revision: 1 }] } as never;
      }
      if (path === "/api/v1/knowledge?limit=25") {
        return { items: archived ? [] : [{ id: "note-1", space_id: "global", title: "Water valve", tags: ["home"], version, updated_at: "2026-08-20T12:00:00Z" }] } as never;
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
    expect(await screen.findByRole("dialog", { name: "Edit Water valve" })).toHaveAttribute("aria-modal", "true");
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
    expect(screen.getByRole("alertdialog", { name: "Archive Water valve" })).toHaveAttribute("aria-modal", "true");
    fireEvent.click(screen.getByRole("button", { name: "Confirm archive" }));

    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/knowledge/note-1?expected_version=2", {
      idempotent: true,
      method: "DELETE",
    }));
    await waitFor(() => expect(screen.queryByText("Version 2 is active")).not.toBeInTheDocument());
  });

  it("returns focus through knowledge editor confirmation handoffs", async () => {
    vi.mocked(apiRequest).mockImplementation(async (path) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [{ id: "global", name: "Family Shared", role: "editor", revision: 1 }] } as never;
      }
      if (path === "/api/v1/knowledge?limit=25") {
        return { items: [{ id: "note-1", space_id: "global", title: "Water valve", tags: ["home"], version: 1, updated_at: "2026-08-20T12:00:00Z" }] } as never;
      }
      if (path === "/api/v1/knowledge/note-1") {
        return { id: "note-1", space_id: "global", title: "Water valve", content: "Turn clockwise.", tags: ["home"], version: 1, updated_at: "2026-08-20T12:00:00Z" } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<KnowledgeConsole />);

    const editButton = await screen.findByRole("button", { name: "Edit Water valve" });
    editButton.focus();
    fireEvent.click(editButton);
    await screen.findByRole("dialog", { name: "Edit Water valve" });
    fireEvent.click(screen.getByRole("button", { name: "Archive Water valve" }));
    fireEvent.click(screen.getByRole("button", { name: "Keep active" }));

    const closeButton = await screen.findByRole("button", { name: "Close knowledge editor" });
    await waitFor(() => expect(closeButton).toHaveFocus());
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(editButton).toHaveFocus());

    fireEvent.click(editButton);
    fireEvent.change(await screen.findByLabelText("Edit content"), { target: { value: "Unsaved change" } });
    fireEvent.click(screen.getByRole("button", { name: "Close knowledge editor" }));
    fireEvent.click(screen.getByRole("button", { name: "Discard changes" }));
    await waitFor(() => expect(editButton).toHaveFocus());
  });

  it("searches every accessible space and exposes degraded semantic health", async () => {
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [{ id: "global", name: "Family Shared", role: "reader", revision: 1 }] } as never;
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
    expect(apiRequest).toHaveBeenCalledWith("/api/v1/retrieval/search", {
      body: { limit: 20, query: "water", semantic_policy: "prefer" },
      method: "POST",
    });
  });

  it("uploads a source into the durable ingestion queue", async () => {
    let uploaded = false;
    vi.mocked(apiRequest).mockImplementation(async (path) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [{ id: "global", name: "Family Shared", role: "editor", revision: 1 }] } as never;
      }
      if (path.startsWith("/api/v1/sources")) {
        return { items: uploaded ? [{ id: "source-1", space_id: "global", display_name: "Procedures", status: "pending", original_filename: "procedures.txt", size_bytes: 32, updated_at: "2026-08-20T12:00:00Z" }] : [] } as never;
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

  it("archives a source only after explicit confirmation", async () => {
    let archived = false;
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [{ id: "global", name: "Family Shared", role: "editor", revision: 1 }] } as never;
      }
      if (path === "/api/v1/sources?limit=25") {
        return { items: archived ? [] : [{ id: "source-1", space_id: "global", display_name: "Procedures", status: "active", original_filename: "procedures.txt", size_bytes: 32, revision: 3, updated_at: "2026-08-20T12:00:00Z" }] } as never;
      }
      if (path === "/api/v1/sources/source-1?expected_revision=3" && options?.method === "DELETE") {
        archived = true;
        return undefined as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<SourcesConsole />);

    fireEvent.click(await screen.findByRole("button", { name: "Archive Procedures" }));
    expect(screen.getByRole("alertdialog", { name: "Archive Procedures" })).toHaveAttribute("aria-modal", "true");
    expect(screen.getByText(/leaves unified search immediately/i)).toBeInTheDocument();
    expect(apiRequest).not.toHaveBeenCalledWith(expect.stringContaining("source-1?expected_revision"), expect.anything());
    fireEvent.click(screen.getByRole("button", { name: "Confirm archive source" }));

    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/sources/source-1?expected_revision=3", { idempotent: true, method: "DELETE" }));
    await waitFor(() => expect(screen.queryByText("Procedures")).not.toBeInTheDocument());
  });

  it("loads the next source page and replaces the page contents", async () => {
    vi.mocked(apiRequest).mockImplementation(async (path) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [{ id: "global", name: "Family Shared", role: "editor", revision: 1 }] } as never;
      }
      if (path === "/api/v1/sources?limit=25") {
        return { items: [{ id: "source-2", space_id: "global", display_name: "Second", status: "active", original_filename: "second.txt", size_bytes: 12, revision: 1, updated_at: "2026-08-20T12:00:00Z" }], page: 1, page_size: 25, total_items: 2, total_pages: 2 } as never;
      }
      if (path === "/api/v1/sources?limit=25&page=2") {
        return { items: [{ id: "source-1", space_id: "global", display_name: "First", status: "active", original_filename: "first.txt", size_bytes: 10, revision: 1, updated_at: "2026-08-20T11:00:00Z" }], page: 2, page_size: 25, total_items: 2, total_pages: 2 } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<SourcesConsole />);

    expect(await screen.findByText("Second")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next page" }));

    expect(await screen.findByText("First")).toBeInTheDocument();
    expect(screen.queryByText("Second")).not.toBeInTheDocument();
  });

  it("reveals a generated member password exactly in the creation result", async () => {
    currentMember.system_role = "super_admin";
    let created = false;
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [{ id: "global", name: "Family Shared", role: "owner", revision: 1 }] } as never;
      }
      if (path.startsWith("/api/v1/members") && options?.method === "POST") {
        created = true;
        return { id: "member-1", username: "nana", display_name: "Nana", temporary_password: "Temp-Only-Once!42", temporary_password_expires_at: "2026-08-21T12:00:00Z", requires_password_change: true } as never;
      }
      if (path.startsWith("/api/v1/members")) {
        return { items: created ? [{ id: "member-1", username: "nana", display_name: "Nana", status: "active", system_role: "member", requires_password_change: true }] : [] } as never;
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

  it("keeps a newly created member discoverable when the directory spans multiple pages", async () => {
    currentMember.system_role = "super_admin";
    const memberPaths: string[] = [];
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [{ id: "global", name: "Family Shared", role: "owner", revision: 1 }] } as never;
      }
      if (path.startsWith("/api/v1/members") && options?.method === "POST") {
        return { id: "member-new", username: "nana", display_name: "Nana", temporary_password: "Temp-Only-Once!42", temporary_password_expires_at: "2026-08-21T12:00:00Z", requires_password_change: true } as never;
      }
      if (path.startsWith("/api/v1/members")) {
        memberPaths.push(path);
      }
      if (path.includes("q=nana")) {
        const incompatibleFiltersRemain = path.includes("status=") || path.includes("system_role=") || path.includes("page=");
        return { items: incompatibleFiltersRemain ? [] : [{ id: "member-new", username: "nana", display_name: "Nana", status: "pending", system_role: "member", requires_password_change: true }], page: 1, page_size: 25, total_items: incompatibleFiltersRemain ? 0 : 1, total_pages: incompatibleFiltersRemain ? 0 : 1 } as never;
      }
      if (path.startsWith("/api/v1/members")) {
        const page = path.includes("page=3") ? 3 : path.includes("page=2") ? 2 : 1;
        return { items: [{ id: `member-${page}`, username: `member-${page}`, display_name: `Member ${page}`, status: "disabled", system_role: "super_admin", requires_password_change: false }], page, page_size: 25, total_items: 51, total_pages: 3 } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<PeopleConsole />);

    await screen.findByRole("heading", { name: "Create a member" });
    fireEvent.change(screen.getByLabelText("Filter member status"), { target: { value: "disabled" } });
    fireEvent.change(screen.getByLabelText("Filter system role"), { target: { value: "super_admin" } });
    fireEvent.click(await screen.findByRole("button", { name: "Next page" }));
    fireEvent.click(await screen.findByRole("button", { name: "Next page" }));
    expect(await screen.findByText("Member 3")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "nana" } });
    fireEvent.change(screen.getByLabelText("Display name"), { target: { value: "Nana" } });
    fireEvent.click(screen.getByRole("button", { name: "Generate member" }));

    expect(await screen.findByText("Temp-Only-Once!42")).toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "Search members" })).toHaveValue("nana");
    expect(await screen.findByRole("button", { name: "Edit Nana" })).toBeInTheDocument();
    const createdMemberPath = memberPaths.findLast((path) => path.includes("q=nana"));
    expect(createdMemberPath).not.toContain("status=");
    expect(createdMemberPath).not.toContain("system_role=");
    expect(createdMemberPath).not.toContain("page=");
  });

  it("routes self password changes to Security and requires enabling a disabled target", async () => {
    currentMember.system_role = "super_admin";
    vi.mocked(apiRequest).mockImplementation(async (path) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [] } as never;
      }
      if (path === "/api/v1/members?limit=25") {
        return { items: [
          { id: "admin-1", username: "mai", display_name: "Mai", mfa_enabled: true, status: "active", system_role: "super_admin", requires_password_change: false },
          { id: "member-disabled", username: "nok", display_name: "Nok", mfa_enabled: false, status: "disabled", system_role: "member", requires_password_change: false },
          { id: "member-pending", username: "jo", display_name: "Jo", mfa_enabled: false, status: "pending", system_role: "member", requires_password_change: true },
        ] } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<PeopleConsole />);

    fireEvent.click(await screen.findByRole("button", { name: "Edit Mai" }));
    expect(screen.queryByRole("button", { name: "Reset Mai password" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Change your password in Security settings" })).toHaveAttribute("href", "/settings?section=security");
    fireEvent.click(screen.getByRole("button", { name: "Close member editor" }));

    fireEvent.click(screen.getByRole("button", { name: "Edit Nok" }));
    expect(screen.getByRole("button", { name: "Reset Nok password" })).toBeDisabled();
    expect(screen.getByText("Enable this member before resetting their password.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close member editor" }));

    fireEvent.click(screen.getByRole("button", { name: "Edit Jo" }));
    expect(screen.getByRole("button", { name: "Reset Jo password" })).toBeEnabled();
  });

  it("resets and disables a member only after explicit confirmation", async () => {
    currentMember.system_role = "super_admin";
    let disabled = false;
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [{ id: "global", name: "Family Shared", role: "owner", revision: 1 }] } as never;
      }
      if (path === "/api/v1/members?limit=25") {
        return { items: [{ id: "member-1", username: "nana", display_name: "Nana", mfa_enabled: true, status: disabled ? "disabled" : "active", system_role: "member", requires_password_change: false }] } as never;
      }
      if (path === "/api/v1/members/member-1/password-reset" && options?.method === "POST") {
        return { id: "member-1", temporary_password: "Reset-Only-Once!42", temporary_password_expires_at: "2026-08-21T12:00:00Z", requires_password_change: true } as never;
      }
      if (path === "/api/v1/members/member-1" && options?.method === "PATCH") {
        disabled = true;
        return { id: "member-1", username: "nana", display_name: "Nana", mfa_enabled: false, status: "disabled", system_role: "member", requires_password_change: true } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<PeopleConsole />);

    fireEvent.click(await screen.findByRole("button", { name: "Edit Nana" }));
    expect(screen.getByRole("dialog", { name: "Edit Nana" })).toHaveAttribute("aria-modal", "true");
    fireEvent.click(screen.getByRole("button", { name: "Reset Nana password" }));
    expect(screen.getByRole("alertdialog", { name: "Reset Nana password" })).toHaveAttribute("aria-modal", "true");
    expect(screen.getByText(/signs out every session/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm password reset" }));
    expect(await screen.findByText("Reset-Only-Once!42")).toBeInTheDocument();

    const memberDialog = screen.getByRole("dialog", { name: "Edit Nana" });
    fireEvent.change(within(memberDialog).getByLabelText("Display name"), { target: { value: "Unsaved Nana" } });
    fireEvent.change(within(memberDialog).getByLabelText("System role"), { target: { value: "super_admin" } });
    fireEvent.click(screen.getByRole("button", { name: "Disable Nana" }));
    expect(screen.getByRole("alertdialog", { name: "Disable Nana" })).toHaveAttribute("aria-modal", "true");
    expect(screen.getByText(/immediately revokes sessions and api keys/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm disable member" }));
    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/members/member-1", {
      body: { display_name: "Nana", status: "disabled", system_role: "member" },
      idempotent: true,
      method: "PATCH",
    }));
    await waitFor(() => expect(screen.getAllByText("disabled").length).toBeGreaterThan(0));
    expect(within(memberDialog).getByLabelText("Display name")).toHaveValue("Unsaved Nana");
    expect(within(memberDialog).getByLabelText("System role")).toHaveValue("member");
    fireEvent.click(within(memberDialog).getByRole("button", { name: "Save account" }));
    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/members/member-1", {
      body: { display_name: "Unsaved Nana", status: "disabled", system_role: "member" },
      idempotent: true,
      method: "PATCH",
    }));
    const disabledPromotionCalls = vi.mocked(apiRequest).mock.calls.filter(([, options]) => {
      const body = options?.body as { status?: string; system_role?: string } | undefined;
      return options?.method === "PATCH" && body?.status === "disabled" && body.system_role === "super_admin";
    });
    expect(disabledPromotionCalls).toHaveLength(0);
  });

  it("explains existing MFA and clears a Super Admin reset secret when the editor closes", async () => {
    currentMember.system_role = "super_admin";
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [] } as never;
      }
      if (path === "/api/v1/members?limit=25") {
        return { items: [
          { id: "admin-2", username: "nana", display_name: "Nana", mfa_enabled: true, status: "active", system_role: "super_admin", requires_password_change: false },
        ] } as never;
      }
      if (path === "/api/v1/members/admin-2/password-reset" && options?.method === "POST") {
        return {
          id: "admin-2",
          requires_password_change: true,
          temporary_password: "Reset-Only-Once!42",
          temporary_password_expires_at: "2026-09-02T12:00:00Z",
        } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<PeopleConsole />);

    fireEvent.click(await screen.findByRole("button", { name: "Edit Nana" }));
    fireEvent.click(screen.getByRole("button", { name: "Reset Nana password" }));
    expect(screen.getByText("Their existing MFA remains required.", { exact: false })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm password reset" }));
    expect(await screen.findByText("Reset-Only-Once!42")).toBeInTheDocument();
    expect(screen.getByText(/expires/i)).toBeInTheDocument();

    const memberDialog = screen.getByRole("dialog", { name: "Edit Nana" });
    fireEvent.click(within(memberDialog).getByRole("button", { name: "Close member editor" }));
    expect(screen.queryByText("Reset-Only-Once!42")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Edit Nana" }));
    expect(screen.queryByText("Reset-Only-Once!42")).not.toBeInTheDocument();
  });

  it.each([
    ["Reset Nana password", "Confirm password reset", "Reset Nana password"],
    ["Disable Nana", "Confirm disable member", "Disable Nana"],
  ])("keeps a failed %s action visible in its confirmation", async (actionName, confirmName, dialogName) => {
    currentMember.system_role = "super_admin";
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [{ id: "global", name: "Family Shared", role: "owner", revision: 1 }] } as never;
      }
      if (path === "/api/v1/members?limit=25") {
        return { items: [{ id: "member-1", username: "nana", display_name: "Nana", mfa_enabled: false, status: "active", system_role: "member", requires_password_change: false }] } as never;
      }
      if (options?.method === "POST" || options?.method === "PATCH") {
        throw new Error("The requested member change could not be saved.");
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<PeopleConsole />);

    fireEvent.click(await screen.findByRole("button", { name: "Edit Nana" }));
    fireEvent.click(screen.getByRole("button", { name: actionName }));
    fireEvent.click(screen.getByRole("button", { name: confirmName }));

    const dialog = screen.getByRole("alertdialog", { name: dialogName });
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("The request could not be completed.");
  });

  it("gates super-admin promotion on confirmed member MFA inside the edit dialog", async () => {
    currentMember.system_role = "super_admin";
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [] } as never;
      }
      if (path === "/api/v1/members?limit=25") {
        return { items: [
          { id: "member-1", username: "nana", display_name: "Nana", mfa_enabled: false, status: "active", system_role: "member", requires_password_change: false },
          { id: "member-2", username: "jo", display_name: "Jo", mfa_enabled: true, status: "active", system_role: "member", requires_password_change: false },
        ] } as never;
      }
      if (path === "/api/v1/members/member-2" && options?.method === "PATCH") {
        return { id: "member-2", username: "jo", display_name: "Jo", mfa_enabled: true, status: "active", system_role: "super_admin", requires_password_change: false } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<PeopleConsole />);

    const nanaTrigger = await screen.findByRole("button", { name: "Edit Nana" });
    nanaTrigger.focus();
    fireEvent.click(nanaTrigger);
    const nanaDialog = screen.getByRole("dialog", { name: "Edit Nana" });
    expect(nanaDialog).toBeInTheDocument();
    expect(nanaDialog.querySelector('option[value="super_admin"]')).toBeDisabled();
    expect(screen.getByText(/set up MFA in Settings.*Security/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close member editor" }));
    await waitFor(() => expect(nanaTrigger).toHaveFocus());

    fireEvent.click(screen.getByRole("button", { name: "Edit Jo" }));
    expect(screen.getByRole("dialog", { name: "Edit Jo" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("System role"), { target: { value: "super_admin" } });
    fireEvent.click(screen.getByRole("button", { name: "Save account" }));

    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/members/member-2", {
      body: { display_name: "Jo", status: "active", system_role: "super_admin" },
      idempotent: true,
      method: "PATCH",
    }));
    expect(screen.queryByRole("dialog", { name: "Edit Jo" })).not.toBeInTheDocument();
  });

  it("lets a super admin repair ownership without granting themselves space access", async () => {
    currentMember.system_role = "super_admin";
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path === "/api/v1/spaces?limit=100") {
        return { items: [{ id: "global", name: "Family Shared", role: "reader", revision: 1 }] } as never;
      }
      if (path === "/api/v1/members?limit=25" || path === "/api/v1/members?limit=25&status=active") {
        return { items: [
          { id: "member-1", username: "old-owner", display_name: "Old Owner", status: "active", system_role: "member", requires_password_change: false },
          { id: "member-2", username: "new-owner", display_name: "New Owner", status: "active", system_role: "member", requires_password_change: false },
        ] } as never;
      }
      if (path === "/api/v1/admin/spaces?limit=25") {
        return { items: [{ id: "private", name: "Private records", revision: 4, owner_member_id: "member-1", owner_username: "old-owner", owner_display_name: "Old Owner", created_at: "2026-08-20T12:00:00Z" }] } as never;
      }
      if (path === "/api/v1/admin/spaces/private/ownership" && options?.method === "PUT") {
        return { space_id: "private", member_id: "member-2", role: "owner" } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<PeopleConsole />);

    fireEvent.click(await screen.findByRole("button", { name: "Open ownership recovery" }));
    expect(await screen.findByRole("option", { name: "Private records · Old Owner" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("New owner"), { target: { value: "member-2" } });
    fireEvent.change(screen.getByLabelText("Recovery reason"), { target: { value: "Restore after account recovery" } });
    fireEvent.click(screen.getByRole("button", { name: "Review ownership repair" }));
    expect(screen.getByRole("alertdialog", { name: "Confirm emergency ownership transfer" })).toHaveAttribute("aria-modal", "true");
    fireEvent.click(screen.getByRole("button", { name: "Confirm emergency transfer" }));

    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/admin/spaces/private/ownership", {
      body: { expected_revision: 4, reason: "Restore after account recovery", target_member_id: "member-2" },
      idempotent: true,
      method: "PUT",
    }));
    expect(screen.getByText(/does not grant the super admin access/i)).toBeInTheDocument();
  });

  it("applies ownership recovery searches explicitly instead of requesting on every keystroke", async () => {
    currentMember.system_role = "super_admin";
    vi.mocked(apiRequest).mockImplementation(async (path) => {
      if (path === "/api/v1/spaces?limit=100") {
        return { items: [{ id: "global", name: "Family Shared", role: "reader", revision: 1 }] } as never;
      }
      if (path.startsWith("/api/v1/members")) {
        return { items: [{ id: "member-1", username: "owner", display_name: "Owner", status: "active", system_role: "member", requires_password_change: false }] } as never;
      }
      if (path.startsWith("/api/v1/admin/spaces")) {
        return { items: [{ id: "private", name: "Private records", revision: 4, owner_member_id: "member-1", owner_username: "owner", owner_display_name: "Owner", created_at: "2026-08-20T12:00:00Z" }] } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<PeopleConsole />);

    fireEvent.click(await screen.findByRole("button", { name: "Open ownership recovery" }));
    await screen.findByRole("option", { name: "Private records · Owner" });
    vi.mocked(apiRequest).mockClear();

    fireEvent.change(screen.getByLabelText("Find space"), { target: { value: "private" } });
    fireEvent.change(screen.getByLabelText("Find new owner"), { target: { value: "nana" } });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(apiRequest).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Apply space search" }));
    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/admin/spaces?limit=25&q=private", {}));
    fireEvent.click(screen.getByRole("button", { name: "Apply owner search" }));
    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/members?limit=25&q=nana&status=active", {}));
  });

  it("creates an individually scoped API key and reveals the secret once", async () => {
    let created = false;
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [{ id: "global", name: "Family Shared", role: "editor", revision: 1 }] } as never;
      }
      if (path === "/api/v1/api-keys" && options?.method === "POST") {
        created = true;
        return { id: "key-1", public_id: "pk_live_1", secret: "aigw_v1_once_only", scopes: ["knowledge:read"] } as never;
      }
      if (path.startsWith("/api/v1/api-keys")) {
        return { items: created ? [{ id: "key-1", public_id: "pk_live_1", name: "Laptop", status: "active", scopes: ["knowledge:read"], created_at: "2026-08-20T12:00:00Z" }] : [] } as never;
      }
      if (path.startsWith("/api/v1/sessions")) {
        return { items: [] } as never;
      }
      if (path === "/api/v1/settings") {
        return { revision: 0, state: "active", values: { retrieval: { limit: 20 } } } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<SettingsConsole />);

    await screen.findByRole("heading", { name: "API keys" });
    fireEvent.change(screen.getByLabelText("Key name"), { target: { value: "Laptop" } });
    fireEvent.click(screen.getByRole("checkbox", { name: "Write knowledge" }));
    fireEvent.click(screen.getByRole("button", { name: "Create API key" }));

    expect(await screen.findByText("aigw_v1_once_only")).toBeInTheDocument();
    expect(screen.getByText(/copy this key now/i)).toBeInTheDocument();
    expect(apiRequest).toHaveBeenCalledWith("/api/v1/api-keys", {
      body: { name: "Laptop", scopes: ["knowledge:read", "knowledge:write"] },
      idempotent: true,
      method: "POST",
    });
  });

  it("shows one URL-addressable settings section and defaults credential lists to active records", async () => {
    vi.mocked(apiRequest).mockImplementation(async (path) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [] } as never;
      }
      if (path === "/api/v1/api-keys?limit=25&status=active" || path === "/api/v1/sessions?limit=25&status=active") {
        return { items: [] } as never;
      }
      if (path === "/api/v1/settings") {
        return { revision: 0, state: "active", values: { retrieval: { limit: 20 } } } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<SettingsConsole />);

    expect(await screen.findByRole("tab", { name: "API keys" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("heading", { name: "API keys" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Sessions" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Safe runtime settings" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Security" }));
    const passwordHeading = screen.getByRole("heading", { name: "Password" });
    const mfaHeading = screen.getByRole("heading", { name: "Multi-factor authentication" });
    expect(passwordHeading).toBeInTheDocument();
    expect(mfaHeading).toBeInTheDocument();
    expect(passwordHeading.compareDocumentPosition(mfaHeading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(navigation.replace).toHaveBeenCalledWith("/settings?section=security", { scroll: false });
    fireEvent.click(screen.getByRole("tab", { name: "Sessions" }));
    expect(screen.getByRole("heading", { name: "Sessions" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "API keys" })).not.toBeInTheDocument();
    expect(navigation.replace).toHaveBeenCalledWith("/settings?section=sessions", { scroll: false });
  });

  it("opens a directly linked settings section", async () => {
    currentMember.system_role = "super_admin";
    navigation.search = "section=runtime";
    vi.mocked(apiRequest).mockImplementation(async (path) => {
      if (path.startsWith("/api/v1/spaces") || path.startsWith("/api/v1/api-keys") || path.startsWith("/api/v1/sessions") || path.startsWith("/api/v1/settings/history")) {
        return { items: [] } as never;
      }
      if (path === "/api/v1/settings") {
        return { revision: 2, state: "active", values: { retrieval: { limit: 20 } } } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<SettingsConsole />);

    expect(screen.getByRole("tab", { name: "Runtime" })).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByRole("heading", { name: "Safe runtime settings" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "API keys" })).not.toBeInTheDocument();
  });

  it("revokes API keys and other website sessions only after confirmation", async () => {
    let keyRevoked = false;
    let sessionRevoked = false;
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [] } as never;
      }
      if (path === "/api/v1/api-keys?limit=25&status=active" && !options?.method) {
        return { items: keyRevoked ? [] : [{ id: "key-1", public_id: "pk_live_1", name: "Laptop", status: "active", scopes: ["knowledge:read"], created_at: "2026-08-20T12:00:00Z" }] } as never;
      }
      if (path === "/api/v1/api-keys/key-1" && options?.method === "DELETE") {
        keyRevoked = true;
        return undefined as never;
      }
      if (path === "/api/v1/sessions?limit=25&status=active" && !options?.method) {
        return { items: [
          { id: "session-current", current: true, status: "active", created_at: "2026-08-20T12:00:00Z", last_activity_at: "2026-08-20T12:00:00Z" },
          ...(!sessionRevoked ? [{ id: "session-other", current: false, status: "active", created_at: "2026-08-19T12:00:00Z", last_activity_at: "2026-08-19T13:00:00Z" }] : []),
        ] } as never;
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
    expect(screen.getByRole("alertdialog", { name: "Revoke Laptop" })).toHaveAttribute("aria-modal", "true");
    fireEvent.click(screen.getByRole("button", { name: "Confirm revoke API key" }));
    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/api-keys/key-1", { idempotent: true, method: "DELETE" }));
    await waitFor(() => expect(screen.queryByText("pk_live_1 · knowledge:read")).not.toBeInTheDocument());

    fireEvent.click(screen.getByRole("tab", { name: "Sessions" }));
    fireEvent.click(screen.getByRole("button", { name: "Sign out website session" }));
    expect(screen.getByRole("alertdialog", { name: "Sign out website session" })).toHaveAttribute("aria-modal", "true");
    fireEvent.click(screen.getByRole("button", { name: "Confirm sign out" }));
    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/sessions/session-other", { idempotent: true, method: "DELETE" }));
    expect(screen.getByText("This session")).toBeInTheDocument();
  });

  it("creates and activates a validated safe runtime settings draft", async () => {
    currentMember.system_role = "super_admin";
    const activeValues = { retrieval: { limit: 20, lexical_weight: 1, vector_weight: 1 } };
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [] } as never;
      }
      if (path === "/api/v1/api-keys?limit=25&status=active") {
        return { items: [] } as never;
      }
      if (path === "/api/v1/sessions?limit=25&status=active") {
        return { items: [] } as never;
      }
      if (path === "/api/v1/settings" && !options?.method) {
        return { id: null, revision: 0, base_revision: 0, state: "active", values: activeValues } as never;
      }
      if (path === "/api/v1/settings/history?limit=20") {
        return { items: [] } as never;
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

    fireEvent.click(screen.getByRole("tab", { name: "Runtime" }));
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

  it("restores a historical safe setting as a new revision after confirmation", async () => {
    currentMember.system_role = "super_admin";
    const revisionOne = { id: "revision-1", revision: 1, base_revision: 0, state: "superseded", values: { retrieval: { limit: 15 } } };
    const revisionTwo = { id: "revision-2", revision: 2, base_revision: 1, state: "active", values: { retrieval: { limit: 25 } } };
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [] } as never;
      }
      if (path === "/api/v1/api-keys?limit=25&status=active") {
        return { items: [] } as never;
      }
      if (path === "/api/v1/sessions?limit=25&status=active") {
        return { items: [] } as never;
      }
      if (path === "/api/v1/settings" && !options?.method) {
        return revisionTwo as never;
      }
      if (path === "/api/v1/settings/history?limit=20") {
        return { items: [revisionTwo, revisionOne] } as never;
      }
      if (path === "/api/v1/settings/rollback/1" && options?.method === "POST") {
        return { ...revisionOne, id: "revision-3", revision: 3, base_revision: 2, state: "active" } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<SettingsConsole />);

    fireEvent.click(screen.getByRole("tab", { name: "Runtime" }));
    fireEvent.change(await screen.findByLabelText("Change reason"), { target: { value: "Restore the proven focused profile" } });
    fireEvent.click(await screen.findByRole("button", { name: "Restore revision 1" }));
    expect(screen.getByRole("alertdialog", { name: "Restore revision 1" })).toHaveAttribute("aria-modal", "true");
    expect(screen.getByText(/creates a new active revision/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm restore revision 1" }));

    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/settings/rollback/1", {
      body: { expected_active_revision: 2, reason: "Restore the proven focused profile" },
      idempotent: true,
      method: "POST",
    }));
    expect(await screen.findByText("Revision 3 is active")).toBeInTheDocument();
  });

  it("shows durable ingestion state without inventing progress", async () => {
    const jobId = "job-1-with-a-very-long-durable-identifier-that-must-not-overflow";
    const documentId = "doc-1-with-a-very-long-document-identifier-that-must-not-overflow";
    vi.mocked(apiRequest).mockImplementation(async (path) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [{ id: "global", name: "Family Shared", role: "editor", revision: 1 }] } as never;
      }
      if (path.startsWith("/api/v1/ingestion-jobs")) {
        return { items: [{ id: jobId, space_id: "global", document_id: documentId, state: "queued", progress: 0, attempt_count: 0, max_attempts: 5, created_at: "2026-08-20T12:00:00Z", updated_at: "2026-08-20T12:00:00Z" }] } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<IngestionConsole />);

    expect(await screen.findByLabelText(`Job ID ${jobId}`)).toHaveAttribute("title", jobId);
    expect(screen.getByLabelText(`Document ID ${documentId}`)).toHaveAttribute("title", documentId);
    expect(screen.getByText("0%")) .toBeInTheDocument();
    expect(screen.getByText("queued")).toBeInTheDocument();
  });

  it("requests ingestion cancellation and retry only after confirmation", async () => {
    let state = "queued";
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [{ id: "global", name: "Family Shared", role: "editor", revision: 1 }] } as never;
      }
      if (path === "/api/v1/ingestion-jobs?limit=25") {
        return { items: [{ id: "job-1", space_id: "global", document_id: "doc-1", state, progress: 0, attempt_count: 1, max_attempts: 5, created_at: "2026-08-20T12:00:00Z", updated_at: "2026-08-20T12:00:00Z" }] } as never;
      }
      if (path === "/api/v1/ingestion-jobs/job-1/cancel" && options?.method === "POST") {
        state = "cancelled";
        return { id: "job-1", state: "cancellation_requested" } as never;
      }
      if (path === "/api/v1/ingestion-jobs/job-1/retry" && options?.method === "POST") {
        state = "queued";
        return { id: "job-1", state: "retry_requested" } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<IngestionConsole />);

    fireEvent.click(await screen.findByRole("button", { name: "Cancel job job-1" }));
    expect(screen.getByRole("alertdialog", { name: "Cancel job job-1" })).toHaveAttribute("aria-modal", "true");
    expect(screen.getByText(/worker will stop at a safe boundary/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm cancel job" }));
    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/ingestion-jobs/job-1/cancel", { idempotent: true, method: "POST" }));

    fireEvent.click(await screen.findByRole("button", { name: "Retry job job-1" }));
    expect(screen.getByRole("alertdialog", { name: "Retry job job-1" })).toHaveAttribute("aria-modal", "true");
    expect(screen.getByText(/starts a new durable attempt/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm retry job" }));
    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/ingestion-jobs/job-1/retry", { idempotent: true, method: "POST" }));
  });

  it("shows immutable audit activity to super admins", async () => {
    currentMember.system_role = "super_admin";
    const requestId = "request-1-with-a-very-long-correlation-identifier-that-must-not-overflow";
    const resourceId = "note-1-with-a-very-long-resource-identifier-that-must-not-overflow";
    vi.mocked(apiRequest).mockImplementation(async (path) => {
      if (path.startsWith("/api/v1/spaces")) {
        return { items: [] } as never;
      }
      if (path.startsWith("/api/v1/audit-events")) {
        return { items: [{ id: "event-1", occurred_at: "2026-08-20T12:00:00Z", actor_kind: "session", actor_member_id: "member-1", action: "knowledge.create", resource_type: "knowledge_item", resource_id: resourceId, outcome: "success", request_id: requestId }] } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<ActivityConsole />);

    expect(await screen.findByText("knowledge.create")).toBeInTheDocument();
    expect(screen.getByLabelText(`Request ID ${requestId}`)).toHaveAttribute("title", requestId);
    expect(screen.getByLabelText(`Resource knowledge_item ${resourceId}`)).toHaveAttribute("title", `${resourceId}`);
    expect(screen.getByText("session · member-1")).toBeInTheDocument();
  });

  it("presents AI management tools as deliberate actions without a chat surface", async () => {
    let confirmed = false;
    vi.mocked(apiRequest).mockImplementation(async (path, options) => {
      if (path === "/api/v1/spaces?limit=100") {
        return { items: [{ id: "private", name: "Private", role: "owner", revision: 1 }] } as never;
      }
      if (path === "/api/v1/ai-tools") {
        return { items: [{ name: "spaces.archive.v1", description: "Archive an owned space.", confirmation: "required", parameters: {} }] } as never;
      }
      if (path === "/api/v1/ai-actions?limit=25") {
        return { items: confirmed ? [] : [{ id: "action-1", tool_name: "spaces.archive.v1", target_ids: ["private"], expected_revision: 1, status: "pending", created_at: "2026-08-20T12:00:00Z", expires_at: "2026-08-20T12:10:00Z" }] } as never;
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
    expect(screen.getByRole("alertdialog", { name: "Confirm spaces.archive.v1" })).toHaveAttribute("aria-modal", "true");
    expect(screen.getByText(/permanently removes it from unified search/i)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm AI action" }));
    await waitFor(() => expect(apiRequest).toHaveBeenCalledWith("/api/v1/ai-actions/action-1/confirm", { idempotent: true, method: "POST" }));
    await waitFor(() => expect(screen.queryByText("Pending confirmation")).not.toBeInTheDocument());
  });

  it("explains the exact impact of each pending AI action", async () => {
    vi.mocked(apiRequest).mockImplementation(async (path) => {
      if (path === "/api/v1/spaces?limit=100") {
        return { items: [{ id: "private", name: "Private", role: "owner", revision: 1 }] } as never;
      }
      if (path === "/api/v1/ai-tools") {
        return { items: [] } as never;
      }
      if (path === "/api/v1/ai-actions?limit=25") {
        return { items: [{ id: "action-2", tool_name: "spaces.members.set.v1", target_ids: ["private", "member-2"], expected_revision: 1, status: "pending", created_at: "2026-08-20T12:00:00Z", expires_at: "2026-08-20T12:10:00Z" }] } as never;
      }
      throw new Error(`Unexpected path ${path}`);
    });
    render(<AiActionsConsole />);

    fireEvent.click(await screen.findByRole("button", { name: "Review spaces.members.set.v1 for private, member-2" }));
    expect(screen.getByText(/changes who can access this space/i)).toBeInTheDocument();
    expect(screen.queryByText(/permanently removes it from unified search/i)).not.toBeInTheDocument();
  });
});
