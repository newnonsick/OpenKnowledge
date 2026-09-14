import { expect, Page, test } from "@playwright/test";
import type { components } from "@/lib/generated/openapi";

const LONG_TOKEN = `supercalifragilisticexpialidocious-${"antidisestablishmentarianism".repeat(12)}-0123456789`;

const memberFixture = {
  display_name: "Mai Arun",
  id: "member-1",
  mfa_enabled: false,
  requires_password_change: false,
  status: "active",
  system_role: "member",
  username: "mai",
} satisfies components["schemas"]["CurrentMember"];

const spacesFixture = {
  items: [
    { created_at: "2026-08-21T12:00:00Z", id: "global", name: "Family Shared", personal: false, revision: 1, role: "editor" },
  ],
  page: 1,
  page_size: 100,
  total_items: 1,
  total_pages: 1,
} satisfies components["schemas"]["Page_SpaceSummary_"];

const hitFixture = {
  canonical_id: "knowledge-1",
  citation_uri: "openknowledge://global/knowledge/knowledge-1",
  content_excerpt: `Excerpt carrying an unbroken token ${LONG_TOKEN}`,
  language: "en",
  rank: 1,
  rank_score: 0.92,
  revision_id: "revision-1",
  source_filename: null,
  source_type: "knowledge",
  space_id: "global",
  title: `Valve checklist ${LONG_TOKEN}`,
  version: 2,
} satisfies components["schemas"]["RetrievalHit"];

const searchFixture = {
  explanation: { abstained: false, active_space_id: null, effective_space_ids: ["global"] },
  health: { degraded_reasons: [], embedding_coverage: null, embedding_generation_id: null, semantic_status: "disabled" },
  hits: [hitFixture],
  query: "valve",
} satisfies components["schemas"]["RetrievalResult"];

const evidenceFixture = {
  canonical_id: "knowledge-1",
  chunk_id: null,
  citation_uri: "openknowledge://global/knowledge/knowledge-1?revision=revision-1",
  content: `Before content carrying an unbroken token ${LONG_TOKEN} for overflow pressure.`,
  kind: "knowledge_revision",
  revision_id: "revision-1",
  space_id: "global",
  superseded: true,
  title: `Before title ${LONG_TOKEN}`,
  version: 1,
} satisfies components["schemas"]["EvidenceDetail"];

const knowledgeFixture = {
  content: `After content carrying an unbroken token ${LONG_TOKEN} for overflow pressure.`,
  content_excerpt: `After excerpt ${LONG_TOKEN}`,
  created_at: "2026-08-20T12:00:00Z",
  id: "knowledge-1",
  lifecycle_status: "accepted",
  space_id: "global",
  tags: [],
  title: `After title ${LONG_TOKEN}`,
  updated_at: "2026-08-21T12:00:00Z",
  version: 2,
} satisfies components["schemas"]["KnowledgeDetail"];

function json<T>(body: T, status = 200) {
  return {
    body: JSON.stringify(body),
    contentType: "application/json",
    status,
  };
}

async function mockExplore(page: Page) {
  await page.route("**/api/v1/**", (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (pathname === "/api/v1/me") {
      return route.fulfill(json(memberFixture));
    }
    if (pathname === "/api/v1/spaces") {
      return route.fulfill(json(spacesFixture));
    }
    if (pathname === "/api/v1/retrieval/search" && request.method() === "POST") {
      return route.fulfill(json(searchFixture));
    }
    if (pathname === "/api/v1/evidence/knowledge/knowledge-1/revisions/revision-1") {
      return route.fulfill(json(evidenceFixture));
    }
    if (pathname === "/api/v1/knowledge/knowledge-1") {
      return route.fulfill(json(knowledgeFixture));
    }
    return route.fulfill(json({ error: { code: "resource_not_found", message: "Not found", type: "not_found_error" }, request_id: "browser-test" }, 404));
  });
}

async function openDiff(page: Page) {
  await mockExplore(page);
  await page.goto("/explore?q=valve");
  await expect(page.getByRole("heading", { name: "Valve checklist", exact: false })).toBeVisible();
  await page.getByRole("button", { name: "Show revision changes for Valve checklist", exact: false }).click();
  await expect(page.locator(".revision-diff")).toBeVisible();
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
}

test("revision diff columns stay within the viewport on desktop", async ({ page }) => {
  await openDiff(page);
  await expectNoHorizontalOverflow(page);
  const columns = page.locator(".revision-diff-columns");
  await expect(columns).toBeVisible();
  const template = await columns.evaluate((element) => getComputedStyle(element).gridTemplateColumns);
  expect(template.trim().split(" ").length).toBe(2);
});

test("revision diff columns stack without overflow on a narrow viewport", async ({ page }) => {
  await page.setViewportSize({ height: 844, width: 390 });
  await openDiff(page);
  await expectNoHorizontalOverflow(page);
  const columns = page.locator(".revision-diff-columns");
  const template = await columns.evaluate((element) => getComputedStyle(element).gridTemplateColumns);
  expect(template.trim().split(" ").length).toBe(1);
});
