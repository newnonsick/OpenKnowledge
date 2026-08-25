import { chromium } from "playwright";
import { createHmac } from "node:crypto";
import { writeFileSync } from "node:fs";
import { credentials } from "./lib/session.mjs";

const TEMP_PASSWORD = process.argv[2];
const NEW_PASSWORD = credentials().password;

function base32Decode(input) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  const clean = input.toUpperCase().replace(/[^A-Z2-7]/g, "");
  let bits = "";
  for (const char of clean) {
    bits += alphabet.indexOf(char).toString(2).padStart(5, "0");
  }
  const bytes = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) {
    bytes.push(parseInt(bits.slice(i, i + 8), 2));
  }
  return Buffer.from(bytes);
}

function totp(secret, timeStep = 30) {
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 1000 / timeStep)));
  const digest = createHmac("sha1", base32Decode(secret)).update(counter).digest();
  const offset = digest[digest.length - 1] & 0xf;
  const binary = ((digest[offset] & 0x7f) << 24) | (digest[offset + 1] << 16) | (digest[offset + 2] << 8) | digest[offset + 3];
  return String(binary % 1_000_000).padStart(6, "0");
}

const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
const page = await context.newPage();
page.on("pageerror", (err) => console.log("[pageerror]", String(err).slice(0, 200)));

await page.goto("http://127.0.0.1:3000/login", { waitUntil: "domcontentloaded" });
await page.getByRole("textbox", { name: "Username" }).fill("admin");
await page.getByRole("textbox", { name: "Password" }).fill(TEMP_PASSWORD);
await page.getByRole("button", { name: "Sign in" }).click();
await page.waitForURL("**/first-use/password", { timeout: 15000 });
console.log("step1 ok: password page");

await page.getByLabel("New password").fill(NEW_PASSWORD);
await page.getByLabel("Confirm password").fill(NEW_PASSWORD);
await page.getByRole("button", { name: "Set new password" }).click();
await page.waitForURL("**/first-use/mfa", { timeout: 15000 });
console.log("step2 ok: mfa page");

await page.locator(".enrollment-secret code").waitFor({ state: "visible", timeout: 15000 });
const secret = await page.locator(".enrollment-secret code").textContent();
console.log("totp secret:", secret);

await page.getByLabel("6-digit authentication code").fill(totp(secret.trim()));
await page.getByRole("button", { name: "Verify and continue" }).click();
await page.locator(".recovery-codes code").first().waitFor({ state: "visible", timeout: 15000 });
const recoveryCodes = await page.locator(".recovery-codes code").allTextContents();
console.log("recovery codes:", JSON.stringify(recoveryCodes));

await page.getByRole("button", { name: "I have saved these secrets" }).click();
await page.waitForURL("http://127.0.0.1:3000/", { timeout: 15000 });
await page.waitForTimeout(2500);
console.log("step3 ok: console at", page.url());

await context.storageState({ path: "../reports/admin-session.json" });
writeFileSync("../reports/admin-totp-secret.txt", secret.trim(), "utf8");
writeFileSync(
  new URL("./credentials.local.json", import.meta.url),
  JSON.stringify({ username: "admin", password: NEW_PASSWORD, totp_secret: secret.trim() }, null, 2),
);
writeFileSync("../reports/admin-recovery-codes.json", JSON.stringify(recoveryCodes, null, 2), "utf8");
await page.screenshot({ path: "../reports/shots/after-login-console.png", fullPage: true });
await browser.close();
console.log("DONE");
