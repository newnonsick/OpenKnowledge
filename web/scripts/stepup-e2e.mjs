import { mkdirSync, writeFileSync } from "node:fs";
import { launch, loginContext, totp, credentials } from "./lib/session.mjs";

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

try {
  const spaceName = `Step-up Check ${Date.now().toString(36)}`;
  await page.goto(BASE + "/spaces", { waitUntil: "domcontentloaded" });
  await page.getByLabel("Space name").fill(spaceName);
  await page.getByRole("button", { name: "Create space" }).click();
  await page.locator(".space-card", { hasText: spaceName }).waitFor();
  console.log("setup: space created, waiting 10.5 minutes for step-up staleness…");
  await page.waitForTimeout(630000);

  const card = page.locator(".space-card", { hasText: spaceName });
  await card.getByRole("button", { name: `Manage access for ${spaceName}` }).click();
  await page.locator(".space-access-panel").waitFor();
  await page.getByLabel("Find member to add").fill("E2E");
  await page.getByRole("button", { name: "Find member" }).click();
  await page.waitForTimeout(900);
  await page.getByLabel("Family member").selectOption({ index: 0 });
  const chosenCandidate = await page.getByLabel("Family member").locator("option").first().textContent();
  await page.getByRole("button", { name: "Add member" }).click();
  const memberName = chosenCandidate.split(" (@")[0];
  await page.locator(".membership-row", { hasText: memberName }).waitFor();
  const transferButton = page.getByRole("button", { name: `Transfer ownership to ${memberName}` });
  await transferButton.click();
  await page.locator(".confirmation-strip").waitFor();
  await page.getByRole("button", { name: "Confirm ownership transfer" }).click();
  await page.locator(".step-up-dialog").waitFor({ timeout: 8000 });
  await page.screenshot({ path: "../reports/shots/e2e/step-up-dialog.png" });
  step("step-up: dialog appears after staleness window", true);

  await page.getByLabel("Current password").fill(credentials().password);
  await page.getByLabel("Authentication code").fill(totp());
  await page.locator(".step-up-dialog").getByRole("button", { name: "Verify identity" }).click();
  await page.locator(".step-up-dialog").waitFor({ state: "hidden" });
  step("step-up: verification accepted, dialog closes", true);

  await transferButton.click();
  await page.locator(".confirmation-strip").waitFor();
  await page.getByRole("button", { name: "Confirm ownership transfer" }).click();
  await page.locator(".space-access-panel").waitFor({ state: "hidden", timeout: 8000 });
  step("step-up: retried action succeeds", true);
} catch (error) {
  step("UNEXPECTED FAILURE", false, String(error).slice(0, 300));
  await page.screenshot({ path: "../reports/shots/e2e/stepup-failure.png", fullPage: true });
}

writeFileSync("../reports/e2e-stepup-results.json", JSON.stringify(results, null, 2));
await context.close();
await browser.close();
const failed = results.filter((r) => !r.ok);
console.log(`DONE: ${results.length - failed.length}/${results.length} passed`);
process.exit(failed.length > 0 ? 1 : 0);
