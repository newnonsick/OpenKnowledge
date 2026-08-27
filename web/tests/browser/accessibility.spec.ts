import AxeBuilder from "@axe-core/playwright";
import { expect, Page, test } from "@playwright/test";
import type { components } from "@/lib/generated/openapi";

const readyFixture = {
  request_id: "browser-test",
  status: "ready",
} satisfies components["schemas"]["HealthResponse"];

const loginFixture = {
  access_expires_at: "2026-08-22T14:00:00Z",
  member_id: "member-1",
  requires_mfa_enrollment: false,
  requires_password_change: false,
  system_role: "member",
} satisfies components["schemas"]["SessionAuthentication"];

const memberFixture = {
  display_name: "Mai Arun",
  id: "member-1",
  requires_password_change: false,
  status: "active",
  system_role: "member",
  username: "mai",
} satisfies components["schemas"]["CurrentMember"];

const spacesFixture = {
  items: [
    { created_at: "2026-08-21T12:00:00Z", id: "global", name: "Family Shared", personal: false, revision: 1, role: "editor" },
    { created_at: "2026-08-20T12:00:00Z", id: "travel", name: "Travel plans", personal: false, revision: 1, role: "owner" },
  ],
  page: 1,
  page_size: 100,
  total_items: 2,
  total_pages: 1,
} satisfies components["schemas"]["Page_SpaceSummary_"];

function listPage<T>(items: T[], pageSize = 25) {
  return {
    items,
    page: 1,
    page_size: pageSize,
    total_items: items.length,
    total_pages: items.length ? 1 : 0,
  };
}

const operationsFixture = {
  ingestion: { cancelled: 0, cancellation_requested: 0, failed: 0, queued: 1, retry_wait: 0, running: 1, succeeded: 8 },
  observed_at: "2026-08-22T13:00:00Z",
  retrieval: { embedding_generation_active: false },
  scope: "accessible_spaces",
  settings_revision: 4,
  spaces: 2,
  storage: { referenced_bytes: 524288 },
} satisfies components["schemas"]["OperationSummary"];

const notFoundFixture = {
  error: { code: "resource_not_found", message: "Not found", type: "not_found_error" },
  request_id: "browser-test",
} satisfies components["schemas"]["ErrorEnvelope"];

function json<T>(body: T, status = 200) {
  return {
    body: JSON.stringify(body),
    contentType: "application/json",
    status,
  };
}

async function mockGateway(page: Page, systemRole: "member" | "super_admin" = "member") {
  await page.route("**/healthz/ready", (route) => route.fulfill(json(readyFixture)));
  await page.route("**/api/v1/**", (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (pathname === "/api/v1/auth/login") {
      return route.fulfill(json(loginFixture));
    }
    if (pathname === "/api/v1/auth/mfa/totp/enroll") {
      return route.fulfill(json({ factor_id: "factor-1", secret: "JBSWY3DPEHPK3PXP" }));
    }
    if (pathname === "/api/v1/me") {
      return route.fulfill(json({ ...memberFixture, system_role: systemRole }));
    }
    if (pathname === "/api/v1/spaces") {
      return route.fulfill(json(spacesFixture));
    }
    if (pathname === "/api/v1/operations/summary") {
      return route.fulfill(json(operationsFixture));
    }
    if (pathname === "/api/v1/knowledge") {
      return route.fulfill(json(listPage([{ content: "Passport renewal instructions and the emergency contact process for every family member.", created_at: "2026-08-20T12:00:00Z", id: "knowledge-1", revision: 2, source_id: "source-1", space_id: "global", tags: ["travel", "important"], title: "International travel document checklist", updated_at: "2026-08-21T12:00:00Z" }])));
    }
    if (pathname === "/api/v1/sources") {
      return route.fulfill(json(listPage([{ display_name: "Family procedures and emergency contacts", id: "source-1", original_filename: "family-procedures-and-emergency-contacts.pdf", revision: 3, size_bytes: 524288, space_id: "global", status: "active", updated_at: "2026-08-20T12:00:00Z" }])));
    }
    if (pathname === "/api/v1/ingestion-jobs") {
      return route.fulfill(json(listPage([{ attempt_count: 1, created_at: "2026-08-20T12:00:00Z", document_id: "source-1", id: "job-with-a-deliberately-long-identifier-123456789", max_attempts: 5, progress: 0, space_id: "global", state: "queued", updated_at: "2026-08-20T12:00:00Z" }])));
    }
    if (pathname === "/api/v1/audit-events") {
      return route.fulfill(json(listPage([{ action: "knowledge.create.with.a.deliberately.long.action.name", actor_kind: "member", actor_member_id: "member-1", id: "audit-1", occurred_at: "2026-08-20T12:00:00Z", outcome: "success", request_id: "request-123456789", resource_id: "knowledge-1", resource_type: "knowledge" }])));
    }
    if (pathname === "/api/v1/api-keys") {
      return route.fulfill(json(listPage([{ created_at: "2026-08-20T12:00:00Z", id: "key-1", name: "Family automation laptop with a long descriptive name", public_id: "pk_live_12345678901234567890", scopes: ["knowledge:read", "knowledge:write"], status: "active" }])));
    }
    if (pathname === "/api/v1/sessions") {
      return route.fulfill(json(listPage([{ created_at: "2026-08-20T12:00:00Z", current: true, id: "session-current-123456789", last_activity_at: "2026-08-21T12:00:00Z", status: "active" }])));
    }
    if (pathname === "/api/v1/settings/history") {
      return route.fulfill(json(listPage([{ base_revision: 3, id: "revision-4", revision: 4, state: "active", values: { retrieval: { lexical_weight: 1, limit: 20, vector_weight: 1 } } }])));
    }
    if (pathname === "/api/v1/settings") {
      return route.fulfill(json({ base_revision: 3, id: "revision-4", revision: 4, state: "active", values: { retrieval: { lexical_weight: 1, limit: 20, vector_weight: 1 } } }));
    }
    if (pathname === "/api/v1/ai-actions") {
      return route.fulfill(json(listPage([{ created_at: "2026-08-20T12:00:00Z", expected_revision: 3, expires_at: "2026-08-20T12:10:00Z", id: "action-1", status: "pending", target_ids: ["private-family-archive-123456789"], tool_name: "spaces.archive.v1" }])));
    }
    if (pathname === "/api/v1/ai-tools") {
      return route.fulfill(json({ items: [{ description: "Archive a private family space after an explicit confirmation.", name: "spaces.archive.v1", risk: "high" }] }));
    }
    if (pathname === "/api/v1/members") {
      return route.fulfill(json(listPage([{ display_name: "Nana Arun with a long family display name", id: "member-2", requires_password_change: false, status: "active", system_role: "member", username: "nana-with-a-long-username" }])));
    }
    if (pathname === "/api/v1/admin/spaces") {
      return route.fulfill(json(listPage([{ created_at: "2026-08-20T12:00:00Z", id: "private", name: "Private records with a very long household name", owner_display_name: "Nana Arun", owner_member_id: "member-2", owner_username: "nana", revision: 4 }])));
    }
    if (/^\/api\/v1\/spaces\/[^/]+\/members$/.test(pathname)) {
      return route.fulfill(json(listPage([{ display_name: "Mai Arun", member_id: "member-1", role: "owner", status: "active", username: "mai" }, { display_name: "Nana Arun", member_id: "member-2", role: "reader", status: "active", username: "nana" }])));
    }
    if (/^\/api\/v1\/spaces\/[^/]+\/member-candidates$/.test(pathname)) {
      return route.fulfill(json(listPage([])));
    }
    return route.fulfill(json(notFoundFixture, 404));
  });
}

async function expectAccessible(page: Page) {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  expect(results.violations).toEqual([]);
}

test("signs in from a keyboard-accessible page and opens the scoped dashboard", async ({ page }) => {
  await mockGateway(page);
  await page.goto("/login");

  await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();
  await expectAccessible(page);

  await page.getByLabel("Username").focus();
  await expect(page.getByLabel("Username")).toBeFocused();
  await page.keyboard.type("mai");
  await page.keyboard.press("Tab");
  await expect(page.getByLabel("Password", { exact: true })).toBeFocused();
  await page.keyboard.type("test-password-only");
  await page.getByRole("button", { name: "Sign in" }).focus();
  await page.keyboard.press("Enter");

  await expect(page).toHaveURL("/");
  await expect(page.getByRole("heading", { name: /Everything your family knows/ })).toBeVisible();
  await expect(page.getByText("Family Shared")).toBeVisible();
  await expect(page.getByText("Permission-aware search is active")).toBeVisible();
  await expect(page.getByRole("link", { name: "People & access" })).toHaveCount(0);
  await expectAccessible(page);

  await page.goto("/people");
  await expect(page).toHaveURL("/");
});

test("keeps dashboard navigation discoverable on a mobile viewport", async ({ page }) => {
  await page.setViewportSize({ height: 844, width: 390 });
  await mockGateway(page);
  await page.goto("/");

  await expect(page.getByRole("heading", { name: /Everything your family knows/ })).toBeVisible();
  await expect(page).toHaveScreenshot("dashboard-mobile-light.png", {
    animations: "disabled",
    fullPage: true,
    maxDiffPixelRatio: 0.005,
  });
  const opener = page.getByRole("button", { name: "Open navigation" });
  await opener.focus();
  await page.keyboard.press("Enter");
  await expect(opener).toHaveAttribute("aria-expanded", "true");
  await expect(page.getByRole("navigation", { name: "Primary navigation" })).toBeVisible();
  await expect(page.getByRole("button", { exact: true, name: "Close navigation" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("navigation", { name: "Primary navigation" })).toBeHidden();
  await expect(opener).toBeFocused();
  await expect(opener).toHaveAttribute("aria-expanded", "false");
  await expectAccessible(page);
});

test("opens discovery with the advertised keyboard shortcut", async ({ page }) => {
  await mockGateway(page);
  await page.goto("/");

  await expect(page.getByRole("link", { name: /search/i })).toBeVisible();

  await page.keyboard.press("Control+k");

  await expect(page).toHaveURL("/explore");
  await expect(page.getByRole("heading", { name: "Explore" })).toBeVisible();
});

test("keeps the sign-in action in the first mobile viewport", async ({ page }) => {
  await page.setViewportSize({ height: 844, width: 390 });
  await page.goto("/login");

  await expect(page.getByText("One trusted place")).toBeInViewport();
  await expect(page.getByRole("heading", { name: "Welcome back" })).toBeInViewport();
  await expect(page.getByRole("button", { name: "Sign in" })).toBeInViewport();
  const [storyBox, formBox] = await Promise.all([
    page.getByText("One trusted place").boundingBox(),
    page.getByRole("heading", { name: "Welcome back" }).boundingBox(),
  ]);
  expect(storyBox?.y).toBeLessThan(formBox?.y ?? Number.POSITIVE_INFINITY);
});

test("preserves approved light and dark-ready dashboard visuals", async ({ page }) => {
  await mockGateway(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: /Everything your family knows/ })).toBeVisible();

  await expect(page).toHaveScreenshot("dashboard-light.png", {
    animations: "disabled",
    fullPage: true,
    maxDiffPixelRatio: 0.005,
  });

  await page.evaluate(() => { document.documentElement.dataset.theme = "dark"; });
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await expectAccessible(page);
  await expect(page).toHaveScreenshot("dashboard-dark-ready.png", {
    animations: "disabled",
    fullPage: true,
    maxDiffPixelRatio: 0.005,
  });
});

test("uploads a real multipart source through the production interface", async ({ page }) => {
  await mockGateway(page, "super_admin");
  let uploadedBody = "";
  let uploadedContentType = "";
  await page.route("**/api/v1/sources/upload", async (route) => {
    uploadedBody = (await route.request().postDataBuffer())?.toString("utf8") || "";
    uploadedContentType = route.request().headers()["content-type"] || "";
    await route.fulfill(json({
      document_id: "source-browser-upload",
      duplicate_candidate_revision_id: null,
      job_id: "job-browser-upload",
      job_state: "queued",
      revision_id: "revision-browser-upload",
    }, 202));
  });
  await page.goto("/sources");
  await page.getByLabel("Display name").fill("Browser upload");
  await page.locator("#source-file").setInputFiles({
    buffer: Buffer.from("Browser multipart upload content."),
    mimeType: "text/plain",
    name: "browser-upload.txt",
  });
  await page.getByRole("button", { name: "Queue source" }).click();

  await expect(page.getByText("Queued for durable ingestion")).toBeVisible();
  expect(uploadedContentType).toContain("multipart/form-data; boundary=");
  expect(uploadedBody).toContain('name="space_id"');
  expect(uploadedBody).toContain("global");
  expect(uploadedBody).toContain('name="display_name"');
  expect(uploadedBody).toContain("Browser upload");
  expect(uploadedBody).toContain('filename="browser-upload.txt"');
  expect(uploadedBody).toContain("Browser multipart upload content.");
});

test("moves a management list with the direct numeric page control", async ({ page }) => {
  await mockGateway(page, "super_admin");
  await page.route("**/api/v1/knowledge*", (route) => {
    const requestUrl = new URL(route.request().url());
    const requestedPage = Number(requestUrl.searchParams.get("page") || "1");
    expect(requestUrl.searchParams.get("page_size")).toBe("25");
    return route.fulfill(json({
      items: [{
        content: "Page-specific knowledge content.",
        created_at: "2026-08-20T12:00:00Z",
        id: `knowledge-${requestedPage}`,
        revision: 1,
        source_id: null,
        space_id: "global",
        tags: [],
        title: requestedPage === 2 ? "Second page knowledge" : "First page knowledge",
        updated_at: "2026-08-20T12:00:00Z",
      }],
      page: requestedPage,
      page_size: 25,
      total_items: 500,
      total_pages: 20,
    }));
  });
  await page.goto("/knowledge");

  await expect(page.getByText("First page knowledge")).toBeVisible();
  const jumpInput = page.getByRole("textbox", { name: "Jump to page of 20" });
  await jumpInput.fill("2");
  await jumpInput.press("Enter");

  await expect(page.getByText("Second page knowledge")).toBeVisible();
  await expect(page.getByText("26–50 of 500")).toBeVisible();
});

test("keeps the mobile knowledge error and retry state within the viewport", async ({ page }) => {
  await mockGateway(page, "super_admin");
  await page.route("**/api/v1/knowledge*", (route) => route.fulfill(json(notFoundFixture, 503)));
  await page.setViewportSize({ height: 720, width: 320 });
  await page.goto("/knowledge");

  await expect(page.getByText("Unable to load knowledge")).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry knowledge" })).toBeVisible();
  await expect(page.getByText("Nothing captured yet")).toHaveCount(0);
  const viewportState = await page.evaluate(() => ({ clientWidth: document.documentElement.clientWidth, scrollWidth: document.documentElement.scrollWidth }));
  expect(viewportState.scrollWidth).toBeLessThanOrEqual(viewportState.clientWidth);
});

for (const width of [390, 768]) {
  test(`puts primary management actions before long lists at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ height: 844, width });
    await mockGateway(page, "super_admin");
    for (const route of ["spaces", "knowledge", "sources", "people"]) {
      await page.goto(`/${route}`);
      const grid = page.locator(`.console-grid-${route}`);
      await expect(grid).toBeVisible();
      const [actionBox, listBox] = await Promise.all([
        grid.locator(":scope > .action-panel").boundingBox(),
        grid.locator(":scope > section:not(.action-panel)").first().boundingBox(),
      ]);
      expect(actionBox?.y, `${route} action panel should come first`).toBeLessThan(listBox?.y ?? Number.POSITIVE_INFINITY);
    }
  });
}

test("preserves list-first management columns on desktop", async ({ page }) => {
  await mockGateway(page, "super_admin");
  for (const route of ["spaces", "knowledge", "sources", "people"]) {
    await page.goto(`/${route}`);
    const grid = page.locator(`.console-grid-${route}`);
    const [actionBox, listBox] = await Promise.all([
      grid.locator(":scope > .action-panel").boundingBox(),
      grid.locator(":scope > section:not(.action-panel)").first().boundingBox(),
    ]);
    expect(listBox?.x, `${route} list should remain the first desktop column`).toBeLessThan(actionBox?.x ?? Number.POSITIVE_INFINITY);
  }
});

test("hydrates a stored dark theme without a React mismatch", async ({ page }) => {
  const hydrationErrors: string[] = [];
  page.on("console", (entry) => {
    if (entry.type() === "error" && /hydration|server rendered html/i.test(entry.text())) {
      hydrationErrors.push(entry.text());
    }
  });
  page.on("pageerror", (error) => {
    if (/hydration|server rendered html/i.test(error.message)) {
      hydrationErrors.push(error.message);
    }
  });
  await page.addInitScript(() => localStorage.setItem("aigw-theme", "dark"));
  await page.goto("/login");

  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();
  await expect.poll(() => hydrationErrors).toEqual([]);
});

const responsiveRoutes = ["/", "/spaces", "/explore", "/knowledge", "/sources", "/ingestion", "/people", "/settings", "/activity", "/ai-actions"];
const responsivePublicRoutes = [
  { heading: "Welcome back", route: "/login" },
  { heading: "Make this account yours.", route: "/first-use/password" },
  { heading: "Protect admin access.", route: "/first-use/mfa" },
];
const responsiveViewports = [
  { height: 720, width: 320 },
  { height: 844, width: 390 },
  { height: 1024, width: 768 },
  { height: 900, width: 1024 },
  { height: 1000, width: 1440 },
  { height: 1080, width: 1920 },
];

for (const viewport of responsiveViewports) {
  test(`keeps every console within a ${viewport.width}px viewport`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await mockGateway(page, "super_admin");
    for (const route of responsiveRoutes) {
      await page.goto(route);
      await expect(page.locator("main")).toBeVisible();
      await expect.poll(() => page.evaluate(() => ({ clientWidth: document.documentElement.clientWidth, scrollWidth: document.documentElement.scrollWidth }))).toEqual({ clientWidth: viewport.width, scrollWidth: viewport.width });
      const visibleOverflow = await page.evaluate(() => Array.from(document.querySelectorAll<HTMLElement>("main *")).filter((element) => {
        const style = getComputedStyle(element);
        return style.display !== "none" && style.visibility !== "hidden" && style.overflowX === "visible" && element.scrollWidth > element.clientWidth + 1;
      }).map((element) => `${element.tagName.toLowerCase()}.${element.className}`));
      expect(visibleOverflow, `${route} contains non-scrollable component overflow`).toEqual([]);
      if (process.env.RESPONSIVE_AUDIT_SHOTS === "1") {
        const routeName = route === "/" ? "dashboard" : route.slice(1);
        await page.screenshot({ animations: "disabled", fullPage: true, path: `test-results/responsive-audit/${viewport.width}-${routeName}.png` });
      }
    }
  });
}

for (const viewport of responsiveViewports) {
  test(`keeps every public entry flow within a ${viewport.width}px viewport`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await mockGateway(page);
    for (const { heading, route } of responsivePublicRoutes) {
      await page.goto(route);
      await expect(page.getByRole("heading", { name: heading })).toBeVisible();
      await expect.poll(() => page.evaluate(() => ({ clientWidth: document.documentElement.clientWidth, scrollWidth: document.documentElement.scrollWidth }))).toEqual({ clientWidth: viewport.width, scrollWidth: viewport.width });
      const visibleOverflow = await page.evaluate(() => Array.from(document.querySelectorAll<HTMLElement>("main *")).filter((element) => {
        const style = getComputedStyle(element);
        return style.display !== "none" && style.visibility !== "hidden" && style.overflowX === "visible" && element.scrollWidth > element.clientWidth + 1;
      }).map((element) => `${element.tagName.toLowerCase()}.${element.className}`));
      expect(visibleOverflow, `${route} contains non-scrollable component overflow`).toEqual([]);
    }
  });
}

for (const viewport of [{ height: 844, width: 390 }, { height: 1000, width: 1440 }]) {
  test(`keeps every console accessible at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await mockGateway(page, "super_admin");
    for (const route of responsiveRoutes) {
      await page.goto(route);
      await expect(page.locator("main")).toBeVisible();
      await expectAccessible(page);
    }
  });
}

for (const viewport of [{ height: 844, width: 390 }, { height: 1000, width: 1440 }]) {
  test(`keeps every public entry flow accessible at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await mockGateway(page);
    for (const { heading, route } of responsivePublicRoutes) {
      await page.goto(route);
      await expect(page.getByRole("heading", { name: heading })).toBeVisible();
      await expectAccessible(page);
    }
  });
}
