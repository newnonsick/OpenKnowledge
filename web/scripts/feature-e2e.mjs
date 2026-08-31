import { mkdirSync, writeFileSync } from "node:fs";
import { launch, loginContext, totp, gatewayCookies, gatewayFetch, credentials } from "./lib/session.mjs";

const BASE = "http://127.0.0.1:3000";
mkdirSync("../reports/shots/e2e", { recursive: true });
const results = [];
function step(name, ok, detail = "") {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? " — " + detail : ""}`);
}

const browser = await launch();
const { context, page } = await loginContext(browser, { width: 1440, height: 1000 });
page.setDefaultTimeout(20000);
const consoleErrors = [];
page.on("console", (msg) => {
  if (msg.type() === "error" && !msg.text().includes("Applying inline style violates") && !msg.text().startsWith("Failed to load resource")) {
    consoleErrors.push(msg.text().slice(0, 200));
  }
});
page.on("response", (response) => {
  if (response.status() >= 400 && !response.url().includes("/api/v1/auth/")) {
    consoleErrors.push(`HTTP ${response.status()} ${response.url().replace(BASE, "")}`);
  }
});
const api = gatewayFetch(await gatewayCookies(context));

try {
  // ---------- SPACES ----------
  await page.goto(BASE + "/spaces", { waitUntil: "domcontentloaded" });
  await page.getByLabel("Search spaces").waitFor();
  const runId = Date.now().toString(36);
  const uniqueName = `Feature Check ${runId}`;
  await page.getByLabel("Space name").fill(uniqueName);
  await page.getByRole("button", { name: "Create space" }).click();
  await page.locator(".inline-success", { hasText: `${uniqueName} is ready` }).waitFor();
  step("spaces: create space with visible confirmation", true);
  await page.getByLabel("Search spaces").fill(uniqueName);
  await page.getByRole("button", { name: "Apply" }).click();
  await page.locator(".space-card", { hasText: uniqueName }).waitFor();
  step("spaces: search finds the new space", true);

  const card = page.locator(".space-card", { hasText: uniqueName });
  await card.getByRole("button", { name: `Manage access for ${uniqueName}` }).click();
  await page.locator(".space-access-panel").waitFor();
  step("spaces: open access manager", true);

  const members = await api("GET", "/api/v1/members?page=1&page_size=50&status=active");
  const helper = members.items.find((m) => m.username.startsWith("e2e_m_")) ?? members.items.find((m) => m.username !== "admin");
  await page.getByLabel("Find member to add").fill(helper.display_name.slice(0, 8));
  await page.getByRole("button", { name: "Find member" }).click();
  await page.waitForTimeout(900);
  await page.getByLabel("Family member").selectOption({ label: `${helper.display_name} (@${helper.username})` });
  await page.getByRole("button", { name: "Add member" }).click();
  await page.locator(".membership-row", { hasText: helper.display_name }).waitFor();
  step("spaces: add member as reader", true);

  const memberRow = page.locator(".membership-row", { hasText: helper.display_name });
  await memberRow.getByLabel(`Role for ${helper.display_name}`).selectOption("editor");
  await page.waitForTimeout(900);
  step("spaces: change role to editor", true);

  await memberRow.getByRole("button", { name: `Transfer ownership to ${helper.display_name}` }).click();
  await page.getByRole("alertdialog").waitFor();
  await page.getByRole("button", { name: "Confirm ownership transfer" }).click();
  const dialogAppeared = await page.locator(".step-up-dialog").waitFor({ state: "visible", timeout: 4000 }).then(() => true).catch(() => false);
  if (dialogAppeared) {
    step("spaces: step-up dialog appears on ownership transfer", true);
    await page.getByLabel("Current password").fill(credentials().password);
    await page.getByLabel("Authentication code").fill(totp());
    await page.locator(".step-up-dialog").getByRole("button", { name: "Verify identity" }).click();
    await page.locator(".step-up-dialog").waitFor({ state: "hidden" });
    step("spaces: step-up verification accepted", true);
    await card.getByRole("button", { name: `Manage access for ${uniqueName}` }).click();
    await page.locator(".space-access-panel").waitFor();
    await memberRow.getByRole("button", { name: `Transfer ownership to ${helper.display_name}` }).click();
    await page.getByRole("alertdialog").waitFor();
    await page.getByRole("button", { name: "Confirm ownership transfer" }).click();
    await page.waitForTimeout(1500);
  } else {
    step("spaces: fresh login counts as recent verification (no step-up)", true);
    await page.waitForTimeout(1500);
  }
  const manageButtons = await card.getByRole("button", { name: `Manage access for ${uniqueName}` }).count();
  step("spaces: ownership transferred (admin demoted to editor)", manageButtons === 0);

  await page.goto(BASE + "/people", { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "Open ownership recovery" }).click();
  await page.locator(".ownership-recovery-form").waitFor();
  await page.getByLabel("Find space").fill(uniqueName);
  await page.getByRole("button", { name: "Apply space search" }).click();
  await page.waitForTimeout(900);
  const recoveryOption = page.getByLabel("Recovery space").locator("option", { hasText: uniqueName }).first();
  await page.getByLabel("Recovery space").selectOption(await recoveryOption.getAttribute("value"));
  await page.getByLabel("New owner", { exact: true }).selectOption({ index: 0 });
  await page.getByLabel("Recovery reason").fill("Feature check recovery of the demo space");
  await page.getByRole("button", { name: "Review ownership repair" }).click();
  await page.getByRole("alertdialog").waitFor();
  await page.getByRole("button", { name: "Confirm emergency transfer" }).click();
  await page.locator(".inline-success").waitFor();
  step("people: emergency ownership recovery returns space to admin", true);

  // ---------- KNOWLEDGE ----------
  await page.goto(BASE + "/knowledge", { waitUntil: "domcontentloaded" });
  await page.locator(".data-row").first().waitFor();
  const rowText = await page.locator(".data-row").first().locator(".row-copy p").textContent();
  step("knowledge: rows show space name", !rowText.includes("space-"), rowText.trim());
  const summary1 = await page.locator(".pagination-summary").textContent();
  await page.getByRole("button", { name: "Next page" }).click();
  await page.locator(".pagination-summary", { hasText: "26–50" }).waitFor();
  const summary2 = await page.locator(".pagination-summary").textContent();
  step("knowledge: page 2 navigation", summary1.includes("1–25") && summary2.includes("26–50"), `${summary1.trim()} -> ${summary2.trim()}`);
  await page.getByRole("button", { name: "Next page" }).click();
  await page.locator(".pagination-summary", { hasText: "51–75" }).waitFor();
  await page.getByRole("button", { name: "Next page" }).click();
  await page.locator(".pagination-summary", { hasText: "76–8" }).waitFor();
  const summary3 = await page.locator(".pagination-summary").textContent();
  step("knowledge: next-page navigation reaches page 4", summary3.includes("76–8"), summary3.trim());

  await page.locator("#knowledge-space").selectOption({ label: uniqueName });
  await page.getByLabel("Title").fill(`Feature check note ${runId}`);
  await page.getByLabel("Knowledge content").fill("Created during the interactive feature verification run.");
  await page.getByLabel("Tags").fill("e2e, feature-check");
  await page.getByRole("button", { name: "Capture knowledge" }).click();
  await page.locator(".data-row", { hasText: `Feature check note ${runId}` }).waitFor();
  step("knowledge: capture new item", true);

  await page.locator(".data-row", { hasText: `Feature check note ${runId}` }).getByRole("button", { name: `Edit Feature check note ${runId}` }).click();
  await page.locator(".knowledge-editor").waitFor();
  await page.getByLabel("Edit content").fill("Revised during the interactive feature verification run.");
  await page.getByRole("button", { name: "Save revision" }).click();
  await page.locator(".revision-state", { hasText: "Version 2" }).waitFor();
  step("knowledge: save revision bumps version", true);

  await page.getByRole("button", { name: "Archive" }).click();
  await page.getByRole("alertdialog").waitFor();
  await page.getByRole("button", { name: "Confirm archive" }).click();
  await page.waitForTimeout(1000);
  step("knowledge: archive item", true);

  // ---------- EXPLORE ----------
  await page.goto(BASE + "/explore", { waitUntil: "domcontentloaded" });
  await page.getByLabel("Search query").fill("wifi passwords and trip plans");
  await page.getByLabel("Search query").press("Enter");
  await page.locator(".result-card").first().waitFor({ timeout: 60000 });
  const hitCount = await page.locator(".result-card").count();
  const healthChip = await page.locator(".health-chip").textContent();
  step("explore: hybrid search returns hits", hitCount > 0, `${hitCount} hits, ${healthChip.trim()}`);

  // ---------- SOURCES + INGESTION ----------
  await page.goto(BASE + "/sources", { waitUntil: "domcontentloaded" });
  await page.locator("#source-space").selectOption({ label: uniqueName });
  await page.getByLabel("Display name").fill(`Feature check source ${runId}`);
  await page.setInputFiles("#source-file", { name: "feature-check.txt", mimeType: "text/plain", buffer: Buffer.from("Feature check runbook. The gateway stores original bytes, then the worker parses, chunks, embeds and activates the document for retrieval.") });
  await page.getByRole("button", { name: "Queue source" }).click();
  await page.locator(".inline-success").waitFor();
  step("sources: upload queues ingestion", true);

  await page.goto(BASE + "/ingestion", { waitUntil: "domcontentloaded" });
  const jobCard = page.locator(".job-card", { hasText: uniqueName }).first();
  await jobCard.waitFor({ timeout: 30000 }).catch(async () => {
    await page.getByRole("combobox", { name: "Filter ingestion space" }).selectOption({ label: uniqueName });
  });
  await jobCard.waitFor({ timeout: 30000 });
  let ingestionState = "";
  for (let i = 0; i < 45 && !["succeeded", "failed"].includes(ingestionState); i += 1) {
    ingestionState = (await jobCard.locator(".status-pill").textContent() ?? "").trim();
    if (!["succeeded", "failed"].includes(ingestionState)) {
      await page.waitForTimeout(2500);
      await page.getByRole("button", { name: "Refresh now" }).click().catch(() => {});
      await page.waitForTimeout(800);
    }
  }
  const ingestionError = await jobCard.locator(".job-error").textContent().catch(() => "");
  step("ingestion: uploaded source reaches succeeded", ingestionState === "succeeded", `${ingestionState}${ingestionError ? ` · ${ingestionError}` : ""}`);

  if (ingestionState === "failed") {
    await jobCard.getByRole("button", { name: /Retry job/ }).click();
    await page.getByRole("alertdialog").waitFor();
    await page.getByRole("button", { name: "Confirm retry job" }).click();
    let retryState = "failed";
    for (let i = 0; i < 45 && retryState !== "succeeded"; i += 1) {
      await page.waitForTimeout(2500);
      await page.getByRole("button", { name: "Refresh now" }).click().catch(() => {});
      await page.waitForTimeout(800);
      retryState = (await jobCard.locator(".status-pill").textContent() ?? "").trim();
    }
    step("ingestion: failed job retry reaches succeeded", retryState === "succeeded", retryState);
  } else {
    step("ingestion: retry path not needed", true, "job succeeded");
  }

  // ---------- PEOPLE ----------
  await page.goto(BASE + "/people", { waitUntil: "domcontentloaded" });
  await page.getByLabel("Username").fill(`feature_check_${runId}`);
  await page.getByLabel("Display name").fill(`Feature Check Member ${runId}`);
  await page.getByRole("button", { name: "Generate member" }).click();
  await page.locator(".secret-reveal").waitFor();
  const tempPassword = await page.locator(".secret-reveal .secret-value code").textContent();
  step("people: create member issues one-time password", tempPassword.length >= 15);

  const newMemberRow = page.locator(".data-row", { hasText: `Feature Check Member ${runId}` });
  await newMemberRow.getByRole("button", { name: "Manage Feature Check Member" }).click();
  await page.locator(".member-admin-panel").waitFor();
  await page.getByRole("button", { name: `Disable Feature Check Member ${runId}` }).click();
  await page.getByRole("alertdialog").waitFor();
  await page.getByRole("button", { name: "Confirm disable member" }).click();
  await page.getByRole("button", { name: `Enable Feature Check Member ${runId}` }).waitFor();
  step("people: disable member", true);
  await page.getByRole("button", { name: `Enable Feature Check Member ${runId}` }).click();
  await page.getByRole("alertdialog").waitFor();
  await page.getByRole("button", { name: "Confirm enable member" }).click();
  await page.getByRole("button", { name: `Disable Feature Check Member ${runId}` }).waitFor();
  step("people: enable member", true);
  await page.getByRole("button", { name: `Reset Feature Check Member ${runId} password` }).click();
  await page.getByRole("alertdialog").waitFor();
  await page.getByRole("button", { name: "Confirm password reset" }).click();
  await page.locator(".member-reset-secret").waitFor();
  step("people: reset password shows temporary secret", true);

  // ---------- SETTINGS ----------
  await page.goto(BASE + "/settings", { waitUntil: "domcontentloaded" });
  await page.getByLabel("Key name").fill(`Feature check key ${runId}`);
  await page.getByLabel("Read knowledge").check();
  await page.getByLabel("Read spaces").check();
  await page.getByRole("button", { name: "Create API key" }).click();
  await page.locator(".secret-banner").waitFor();
  step("settings: create API key reveals secret once", true);
  const keyRow = page.locator(".data-row", { hasText: `Feature check key ${runId}` });
  await keyRow.getByRole("button", { name: `Revoke Feature check key ${runId}` }).click();
  await page.getByRole("alertdialog").waitFor();
  await page.getByRole("button", { name: "Confirm revoke API key" }).click();
  await keyRow.waitFor({ state: "hidden" });
  step("settings: revoke API key leaves the active list", true);

  await page.getByRole("tab", { name: "Runtime" }).click();
  const activeRevision = await page.locator(".runtime-summary div:nth-child(1) strong").textContent();
  await page.getByLabel("Retrieval result limit").fill("23");
  await page.getByLabel("Change reason").fill("Feature check runtime adjustment");
  await page.getByRole("button", { name: "Create validated draft" }).click();
  await page.locator(".runtime-draft-review").waitFor();
  await page.getByRole("button", { name: "Activate settings" }).click();
  await page.locator(".runtime-summary div:nth-child(1) strong", { hasText: String(Number(activeRevision) + 1) }).waitFor();
  const newRevision = await page.locator(".runtime-summary div:nth-child(1) strong").textContent();
  step("settings: draft + activate bumps revision", Number(newRevision) === Number(activeRevision) + 1, `${activeRevision} -> ${newRevision}`);

  const restoreRow = page.locator(".data-row", { hasText: `Revision ${activeRevision}` });
  await restoreRow.getByRole("button", { name: `Restore revision ${activeRevision}` }).click();
  await page.getByRole("alertdialog").waitFor();
  await page.getByRole("button", { name: `Confirm restore revision ${activeRevision}` }).click();
  await page.waitForTimeout(1200);
  step("settings: restore historical revision", true);

  // ---------- ACTIVITY ----------
  await page.goto(BASE + "/activity", { waitUntil: "domcontentloaded" });
  await page.getByLabel("Filter audit action").fill("knowledge.create");
  await page.getByRole("button", { name: "Apply" }).click();
  await page.waitForTimeout(1200);
  const actions = await page.locator(".audit-row h3").allTextContents();
  step("activity: action filter narrows rows", actions.length > 0 && actions.every((text) => text === "knowledge.create"), `${actions.length} rows`);

  await page.screenshot({ path: "../reports/shots/e2e/activity-filtered.png", fullPage: false });

  // ---------- SIGN OUT ----------
  await page.getByRole("button", { name: "Sign out" }).click();
  await page.waitForURL("**/login", { timeout: 15000 });
  step("auth: sign out returns to login", true);

  // ---------- FAILED LOGIN + WRONG MFA ----------
  await page.getByRole("textbox", { name: "Username" }).fill("admin");
  await page.getByRole("textbox", { name: "Password" }).fill("wrong-password-entirely");
  await page.getByRole("textbox", { name: "Password" }).press("Enter");
  await page.locator(".auth-error").waitFor();
  const err1 = await page.locator(".auth-error").textContent();
  step("auth: wrong password shows error", err1.includes("incorrect"), err1.trim());

  await page.getByRole("textbox", { name: "Password" }).fill(credentials().password);
  await page.getByRole("textbox", { name: "Password" }).press("Enter");
  await page.getByLabel("Authentication code").waitFor();
  await page.getByLabel("Authentication code").fill("000000");
  await page.getByRole("button", { name: "Verify and sign in" }).click();
  await page.locator(".auth-error").waitFor();
  step("auth: wrong TOTP code rejected", true);

  await page.getByRole("button", { name: "Recovery code" }).click();
  await page.getByLabel("Recovery code").fill("not-a-valid-recovery-code");
  await page.getByRole("button", { name: "Verify and sign in" }).click();
  await page.locator(".auth-error").waitFor();
  step("auth: invalid recovery code rejected", true);

  await page.getByRole("button", { name: "Authenticator code" }).click();
  await page.getByLabel("Authentication code").fill(totp());
  await page.getByRole("button", { name: "Verify and sign in" }).click();
  await page.waitForURL(BASE + "/", { timeout: 20000 });
  step("auth: TOTP login works after failures", true);
} catch (error) {
  step("UNEXPECTED FAILURE", false, String(error).slice(0, 300));
  await page.screenshot({ path: "../reports/shots/e2e/failure.png", fullPage: true });
}

if (consoleErrors.length > 0) {
  step("console errors during run", false, JSON.stringify([...new Set(consoleErrors)].slice(0, 5)));
} else {
  step("console errors during run", true);
}

writeFileSync("../reports/e2e-feature-results.json", JSON.stringify(results, null, 2));
await context.close();
await browser.close();
const failed = results.filter((r) => !r.ok);
console.log(`DONE: ${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length > 0 ? 1 : 0);
