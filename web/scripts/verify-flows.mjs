import { mkdirSync, writeFileSync } from "node:fs";
import { chromium } from "playwright";
import { exitCodeFor } from "./lib/audit-results.mjs";
import { credentials, totp } from "./lib/session.mjs";

const shots = "../gui-test-screenshots/verify";
mkdirSync(shots, { recursive: true });
const results = [];
const check = (name, ok, detail = "") => {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? " — " + detail : ""}`);
};

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
const page = await context.newPage();
const consoleErrors = [];
page.on("pageerror", (err) => consoleErrors.push("pageerror: " + String(err).slice(0, 200)));
page.on("console", (msg) => {
  if (msg.type() === "error" && !msg.text().includes("Applying inline style violates") && !msg.text().includes("status of 401")) {
    consoleErrors.push(msg.text().slice(0, 200));
  }
});

await page.goto("http://127.0.0.1:3000/login", { waitUntil: "domcontentloaded" });
await page.waitForTimeout(600);
await page.getByRole("textbox", { name: "Username" }).fill("no_such_audit_user");
await page.getByRole("textbox", { name: "Password" }).fill("definitely-wrong");
await page.getByRole("button", { name: "Sign in", exact: true }).click();
await page.waitForSelector(".auth-error", { timeout: 15000 });
const wrongError = await page.locator(".auth-error").textContent().catch(() => null);
check("login shows error for wrong password", Boolean(wrongError && wrongError.includes("incorrect")), wrongError ?? "no error shown");
await page.screenshot({ path: `${shots}/01-login-error.png` });

await page.waitForTimeout(2500);
await page.getByRole("textbox", { name: "Username" }).fill(credentials().username);
await page.getByRole("textbox", { name: "Password" }).fill(credentials().password);
await page.getByRole("button", { name: "Sign in", exact: true }).click();
await page.getByRole("textbox", { name: "Authentication code" }).fill(totp(), { timeout: 20000 });
await page.getByRole("button", { name: "Verify and sign in" }).click();
await page.waitForURL("http://127.0.0.1:3000/", { timeout: 15000 });
await page.waitForTimeout(1600);
check("admin lands on dashboard", page.url() === "http://127.0.0.1:3000/");

const sidebarGeom = await page.evaluate(() => {
  const sb = document.querySelector(".sidebar");
  const footer = document.querySelector(".sidebar-footer");
  const accountMenu = document.querySelector(".account-menu-trigger");
  return {
    viewportH: window.innerHeight,
    sidebarClientH: sb.clientHeight,
    footerBottom: Math.round(footer.getBoundingClientRect().bottom),
    accountMenuVisible: accountMenu.getBoundingClientRect().bottom <= window.innerHeight,
  };
});
check("sidebar fits viewport at 900px height", sidebarGeom.footerBottom <= sidebarGeom.viewportH && sidebarGeom.accountMenuVisible, JSON.stringify(sidebarGeom));
await page.screenshot({ path: `${shots}/02-dashboard.png` });

await page.getByRole("link", { name: "Explore" }).click();
await page.waitForURL("**/explore");
await page.waitForTimeout(900);
await page.getByRole("searchbox", { name: "Search query" }).fill("family recipes wifi");
await page.getByRole("button", { name: "Search knowledge" }).click();
await page.waitForSelector(".search-results-section .result-list, .search-results-section .result-empty", { timeout: 30000 });
await page.waitForTimeout(300);
const matchesHeading = await page.locator(".results-toolbar h2").textContent();
check("explore pluralizes matches", matchesHeading === null || !matchesHeading.includes("1 matches"), matchesHeading ?? "no heading");
await page.screenshot({ path: `${shots}/03-explore-results.png` });

await page.getByRole("link", { name: "Spaces" }).click();
await page.waitForURL("**/spaces");
await page.waitForTimeout(1600);
const idLineCount = await page.locator(".space-card > div > p").evaluateAll((els) => els.filter((el) => /^space-[0-9a-f]{20,}/.test(el.textContent.trim())).length);
check("space cards hide raw space IDs", idLineCount === 0, `${idLineCount} raw IDs visible`);
const manageBtn = page.getByRole("button", { name: "Manage access for Family Shared" });
if (await manageBtn.count()) {
  await manageBtn.click();
  await page.getByText("Current members").waitFor({ state: "visible", timeout: 15000 });
  const ownerRow = page.locator(".membership-row").filter({ has: page.locator(".role-owner") });
  const ownerRowRemove = await ownerRow.getByRole("button", { name: /Remove/ }).count();
  check("owner row has no remove button", ownerRowRemove === 0);
  const memberLabel = await page.getByText("Current members", { exact: true }).count();
  check("access panel labels member list", memberLabel === 1, `${memberLabel} labels found`);
  const strayPaginations = await page.locator(".space-access-dialog .pagination-summary").allTextContents();
  check("single-page member lists hide pagination", strayPaginations.filter((t) => t.includes("1–1 of 1")).length === 0, JSON.stringify(strayPaginations));
  await page.getByRole("button", { name: "Close access manager" }).click();
  await manageBtn.waitFor({ state: "visible" });
}
await page.screenshot({ path: `${shots}/04-spaces-access.png` });

const spaceSearch = page.getByRole("searchbox", { name: "Search spaces" });
await spaceSearch.fill("Family Shared");
await page.locator("form").filter({ has: spaceSearch }).getByRole("button", { name: "Apply" }).click();
await page.waitForTimeout(1200);
const familyCardCount = await page.getByText("1 accessible space").count();
check("sidebar keeps global space count under search filter", familyCardCount === 0, "filtered sidebar text found: " + familyCardCount);
const filteredPagination = await page.locator(".pagination-summary").allTextContents();
check("filtered single result hides pagination", filteredPagination.filter((t) => t.includes("1–1 of 1")).length === 0, JSON.stringify(filteredPagination));
await page.screenshot({ path: `${shots}/05-spaces-filtered.png` });
await page.getByRole("button", { name: "Clear" }).click();
await page.waitForTimeout(800);

await page.getByRole("link", { name: "Knowledge" }).click();
await page.waitForURL("**/knowledge");
await page.waitForTimeout(1600);
await page.getByLabel("Title").fill("Verify probe note");
await page.getByLabel("Knowledge content").fill("Verification of capture feedback. Reference VERIFY-77.");
await page.getByRole("button", { name: "Capture knowledge" }).click();
const success = await page.locator(".inline-success").textContent().catch(() => null);
check("capture shows success feedback", Boolean(success && success.includes("Verify probe note")), success ?? "none");
await page.screenshot({ path: `${shots}/06-knowledge-captured.png` });

const editKnowledgeButton = page.getByRole("button", { name: "Edit Verify probe note" });
await editKnowledgeButton.click();
await page.waitForTimeout(1200);
const knowledgeEditor = page.getByRole("dialog", { name: "Edit Verify probe note" });
check("knowledge editor opens as modal", await knowledgeEditor.getAttribute("aria-modal") === "true");
check("knowledge editor locks background scroll", await page.evaluate(() => document.body.style.overflow) === "hidden");
await page.getByLabel("Edit content").fill("Verification of capture feedback. Edited but not saved.");
await page.getByRole("button", { name: "Close knowledge editor" }).click();
await page.waitForTimeout(400);
const discardStrip = await page.getByRole("alertdialog", { name: "Discard unsaved changes" }).count();
check("editor asks before discarding unsaved changes", discardStrip === 1);
check("editor handoff keeps one active modal", await page.locator("[aria-modal='true']").count() === 1);
await page.screenshot({ path: `${shots}/07-knowledge-discard-guard.png` });
if (discardStrip) {
  await page.getByRole("button", { name: "Discard changes" }).click();
  await page.waitForTimeout(300);
  check("discard returns focus to edit trigger", await editKnowledgeButton.evaluate((element) => element === document.activeElement));
  check("closing knowledge editor restores background scroll", await page.evaluate(() => document.body.style.overflow) === "");
}
await editKnowledgeButton.click();
await page.waitForTimeout(1000);
await page.getByRole("button", { name: "Archive Verify probe note" }).click();
await page.waitForTimeout(400);
await page.getByRole("alertdialog", { name: "Archive Verify probe note" }).getByRole("button", { name: "Confirm archive" }).click();
await page.waitForTimeout(1500);

await page.getByRole("link", { name: "Sources" }).click();
await page.waitForURL("**/sources");
await page.waitForTimeout(1600);
const firstArchive = page.getByRole("button", { name: /^Archive .* source/ }).first();
if (await firstArchive.count()) {
  const rowTitle = await firstArchive.evaluate((el) => el.getAttribute("aria-label").replace("Archive ", ""));
  await firstArchive.click();
  await page.waitForTimeout(500);
  const archiveDialog = page.getByRole("alertdialog", { name: `Archive ${rowTitle}` });
  check("source archive confirmation is modal", await archiveDialog.getAttribute("aria-modal") === "true", rowTitle);
  await page.keyboard.press("Escape");
  await archiveDialog.waitFor({ state: "hidden" });
  check("Escape closes source confirmation", await archiveDialog.count() === 0);
  check("source confirmation restores trigger focus", await firstArchive.evaluate((element) => element === document.activeElement));
  await firstArchive.click();
  await archiveDialog.waitFor();
  await page.screenshot({ path: `${shots}/08-source-confirm-modal.png` });
  await page.getByRole("button", { name: "Keep source" }).click();
} else {
  check("sources empty state is clear", await page.locator(".console-empty").filter({ hasText: /No source/i }).count() === 1);
}

await page.getByRole("link", { name: "Activity" }).click();
await page.waitForURL("**/activity");
await page.waitForTimeout(1800);
await page.getByLabel("Filter audit outcome").selectOption("denied");
await page.waitForTimeout(1400);
const deniedRows = await page.locator(".audit-row").count();
const deniedPills = await page.locator(".audit-row .status-pill").allTextContents();
check("outcome filter applies without Apply click", deniedRows === 0 || deniedPills.every((t) => t === "denied"), `${deniedRows} rows`);
await page.getByRole("button", { name: "Clear" }).click();
await page.waitForTimeout(1200);
const jumpInput = page.getByRole("textbox", { name: /Jump to page/ });
if (await jumpInput.count()) {
  await jumpInput.fill("2");
  await jumpInput.press("Enter");
  await page.waitForTimeout(1500);
  const jumpSummary = await page.locator(".pagination-summary").textContent();
  check("jump input submits on Enter", jumpSummary.includes("26–"), jumpSummary ?? "none");
} else {
  const nextPage = page.getByRole("button", { name: "Next page" });
  if (await nextPage.count()) {
    const before = await page.locator(".pagination-summary").textContent();
    await nextPage.click();
    await page.waitForTimeout(1200);
    const after = await page.locator(".pagination-summary").textContent();
    check("activity next-page control advances the result window", before !== after, `${before} -> ${after}`);
  } else {
    check("single-page activity hides pagination", await page.locator(".pagination-summary").count() === 0);
  }
}
await page.screenshot({ path: `${shots}/09-activity-pagination.png` });

await page.getByRole("link", { name: "Settings" }).click();
await page.waitForURL("**/settings");
await page.waitForTimeout(1800);
const keyName = `Verify Probe ${Date.now().toString(36)}`;
await page.getByLabel("Key name").fill(keyName);
await page.getByRole("button", { name: "Create API key" }).click();
await page.waitForTimeout(1600);
const secretBanner = await page.getByText("Copy this key now").count();
check("API key secret banner shows once", secretBanner === 1);
const revokeRow = page.getByRole("article").filter({ hasText: keyName });
await revokeRow.getByRole("button", { name: `Revoke ${keyName}` }).click();
await page.waitForTimeout(400);
await page.getByRole("alertdialog", { name: `Revoke ${keyName}` }).getByRole("button", { name: "Confirm revoke API key" }).click();
await revokeRow.waitFor({ state: "hidden", timeout: 15000 });
check("API key revoked and removed from the active list", true);
await page.screenshot({ path: `${shots}/10-settings-key-revoked.png` });

const themeBefore = await page.evaluate(() => document.documentElement.dataset.theme);
await page.getByRole("button", { name: "Open account menu" }).click();
await page.getByRole("menuitem", { name: /theme/i }).click();
await page.waitForTimeout(400);
const themeAfter = await page.evaluate(() => document.documentElement.dataset.theme);
check("theme toggles", themeBefore !== themeAfter, `${themeBefore} -> ${themeAfter}`);
await page.screenshot({ path: `${shots}/11-dark-settings.png` });
await page.getByRole("menuitem", { name: /theme/i }).click();
await page.waitForTimeout(300);

await page.getByRole("menuitem", { name: "Sign out", exact: true }).click();
await page.waitForURL("http://127.0.0.1:3000/login", { timeout: 10000 });
check("sign out returns to login", page.url().endsWith("/login"));

check("no console/page errors during flows", consoleErrors.length === 0, JSON.stringify([...new Set(consoleErrors)].slice(0, 4)));

await context.close();
await browser.close();
writeFileSync("../reports/verify-flows.json", JSON.stringify(results, null, 2));
console.log("VERIFY DONE:", results.filter((r) => r.ok).length + "/" + results.length, "passed");
process.exit(exitCodeFor(results));
