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
  next_cursor: null,
} satisfies components["schemas"]["Page_SpaceSummary_"];

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

async function mockGateway(page: Page) {
  await page.route("**/healthz/ready", (route) => route.fulfill(json(readyFixture)));
  await page.route("**/api/v1/**", (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (pathname === "/api/v1/auth/login") {
      return route.fulfill(json(loginFixture));
    }
    if (pathname === "/api/v1/me") {
      return route.fulfill(json(memberFixture));
    }
    if (pathname === "/api/v1/spaces") {
      return route.fulfill(json(spacesFixture));
    }
    if (pathname === "/api/v1/operations/summary") {
      return route.fulfill(json(operationsFixture));
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

test("keeps the sign-in action in the first mobile viewport", async ({ page }) => {
  await page.setViewportSize({ height: 844, width: 390 });
  await page.goto("/login");

  await expect(page.getByRole("heading", { name: "Welcome back" })).toBeInViewport();
  await expect(page.getByRole("button", { name: "Sign in" })).toBeInViewport();
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
