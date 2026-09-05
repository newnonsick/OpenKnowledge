import { chromium } from "playwright";
const base = "http://127.0.0.1:3000";
const outDir = "../gui-test-screenshots/verify";
import fs from "fs";
fs.mkdirSync(outDir, { recursive: true });
const readyFixture = { request_id: "verify", status: "ready" };
const memberFixture = { display_name: "Mai Arun", id: "member-1", mfa_enabled: false, requires_password_change: false, status: "active", system_role: "super_admin", username: "mai" };
const spacesFixture = { items: [{ created_at: "2026-08-21T12:00:00Z", id: "global", name: "Family Shared", personal: false, revision: 1, role: "editor" }, { created_at: "2026-08-20T12:00:00Z", id: "travel", name: "Travel plans", personal: false, revision: 1, role: "owner" }], page: 1, page_size: 100, total_items: 2, total_pages: 1 };
const operationsFixture = { ingestion: { cancelled: 0, cancellation_requested: 0, failed: 0, queued: 1, retry_wait: 0, running: 1, succeeded: 8 }, observed_at: "2026-08-22T13:00:00Z", retrieval: { embedding_generation_active: true }, scope: "accessible_spaces", settings_revision: 4, spaces: 2, storage: { referenced_bytes: 524288 } };
function listPage(items, pageSize=25) { return { items, page: 1, page_size: pageSize, total_items: items.length, total_pages: items.length ? 1 : 0 }; }
function json(body, status=200) { return { body: JSON.stringify(body), contentType: "application/json", status }; }
async function mock(page) {
  await page.route("**/healthz/ready", r => r.fulfill(json(readyFixture)));
  await page.route("**/api/v1/**", r => {
    const p = new URL(r.request().url()).pathname;
    if (p === "/api/v1/me") return r.fulfill(json(memberFixture));
    if (p === "/api/v1/spaces") return r.fulfill(json(spacesFixture));
    if (p === "/api/v1/operations/summary") return r.fulfill(json(operationsFixture));
    if (p === "/api/v1/knowledge") return r.fulfill(json(listPage([{ content: "Passport renewal instructions.", created_at: "2026-08-20T12:00:00Z", id: "k1", revision: 2, source_id: "s1", space_id: "global", tags: ["travel"], title: "Travel checklist", updated_at: "2026-08-21T12:00:00Z" }])));
    if (p === "/api/v1/sources") return r.fulfill(json(listPage([{ display_name: "Procedures", id: "s1", original_filename: "proc.pdf", revision: 3, size_bytes: 524288, space_id: "global", status: "active", updated_at: "2026-08-20T12:00:00Z" }])));
    if (p === "/api/v1/ingestion-jobs") return r.fulfill(json(listPage([{ attempt_count: 1, created_at: "2026-08-20T12:00:00Z", document_id: "s1", id: "j1", last_error_code: null, max_attempts: 5, progress: 100, space_id: "global", state: "succeeded", updated_at: "2026-08-20T12:00:00Z" }])));
    if (p === "/api/v1/audit-events") return r.fulfill(json(listPage([{ action: "knowledge.create", actor_kind: "member", actor_member_id: "m1", id: "a1", occurred_at: "2026-08-20T12:00:00Z", outcome: "success", request_id: "r1", resource_id: "k1", resource_type: "knowledge" }])));
    if (p === "/api/v1/api-keys") return r.fulfill(json(listPage([{ created_at: "2026-08-20T12:00:00Z", id: "key1", name: "Laptop", public_id: "pk_live_123", scopes: ["knowledge:read"], status: "active" }])));
    if (p === "/api/v1/sessions") return r.fulfill(json(listPage([{ created_at: "2026-08-20T12:00:00Z", current: true, id: "sess1", last_activity_at: "2026-08-21T12:00:00Z", status: "active" }])));
    if (p === "/api/v1/settings/history") return r.fulfill(json(listPage([{ base_revision: 3, id: "rev4", revision: 4, state: "active", values: { retrieval: { lexical_weight: 1, limit: 20, vector_weight: 1 } } }])));
    if (p === "/api/v1/settings") return r.fulfill(json({ base_revision: 3, id: "rev4", revision: 4, state: "active", values: { retrieval: { lexical_weight: 1, limit: 20, vector_weight: 1 } } }));
    if (p === "/api/v1/ai-actions") return r.fulfill(json(listPage([])));
    if (p === "/api/v1/ai-tools") return r.fulfill(json({ items: [{ description: "Archive space.", name: "spaces.archive.v1", risk: "high" }] }));
    if (p === "/api/v1/members") return r.fulfill(json(listPage([{ display_name: "Nana", id: "m2", mfa_enabled: false, requires_password_change: false, status: "active", system_role: "member", username: "nana" }])));
    if (p === "/api/v1/admin/spaces") return r.fulfill(json(listPage([{ created_at: "2026-08-20T12:00:00Z", id: "private", name: "Private", owner_display_name: "Nana", owner_member_id: "m2", owner_username: "nana", revision: 4 }])));
    if (p.match(/^\/api\/v1\/spaces\/[^/]+\/members$/)) return r.fulfill(json(listPage([{ display_name: "Mai", member_id: "m1", role: "owner", status: "active", username: "mai" }])));
    if (p.match(/^\/api\/v1\/spaces\/[^/]+\/member-candidates$/)) return r.fulfill(json(listPage([])));
    return r.fulfill(json({ error: { code: "x", message: "nf", type: "t" }, request_id: "v" }, 404));
  });
}
const routes = ["/", "/explore", "/spaces", "/knowledge", "/sources", "/ingestion", "/ai-actions", "/people", "/activity", "/settings", "/login"];
const browser = await chromium.launch();
for (const vp of [{w:1440,h:1000},{w:390,h:844}]) {
  const ctx = await browser.newContext({ viewport: { width: vp.w, height: vp.h } });
  const page = await ctx.newPage();
  await mock(page);
  for (const route of routes) {
    await page.goto(base + route, { waitUntil: "networkidle" });
    await page.waitForTimeout(600);
    const name = (route === "/" ? "dashboard" : route.slice(1)) + "-" + vp.w + ".png";
    await page.screenshot({ path: outDir + "/" + name, fullPage: true, animations: "disabled" });
    const overflow = await page.evaluate(() => {
      const de = document.documentElement;
      const els = Array.from(document.querySelectorAll("main *")).filter(e => {
        const s = getComputedStyle(e);
        return s.display !== "none" && s.visibility !== "hidden" && s.overflowX === "visible" && e.scrollWidth > e.clientWidth + 1;
      }).map(e => e.tagName + "." + e.className);
      return { doc: { cw: de.clientWidth, sw: de.scrollWidth }, els };
    });
    console.log(vp.w + " " + route + " doc=" + overflow.doc.cw + "/" + overflow.doc.sw + " overflow=" + JSON.stringify(overflow.els.slice(0,3)));
  }
  await ctx.close();
}
await browser.close();
console.log("done");
