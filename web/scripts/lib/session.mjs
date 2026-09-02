import { readFileSync } from "node:fs";
import { createHmac } from "node:crypto";
import { chromium } from "playwright";

let cached = null;

export function credentials() {
  if (cached) {
    return cached;
  }
  cached = JSON.parse(readFileSync(new URL("../credentials.local.json", import.meta.url), "utf8"));
  return cached;
}

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

export function totp(secret = credentials().totp_secret, timeStep = 30) {
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 1000 / timeStep)));
  const digest = createHmac("sha1", base32Decode(secret)).update(counter).digest();
  const offset = digest[digest.length - 1] & 0xf;
  const binary = ((digest[offset] & 0x7f) << 24) | (digest[offset + 1] << 16) | (digest[offset + 2] << 8) | digest[offset + 3];
  return String(binary % 1_000_000).padStart(6, "0");
}

export async function launch() {
  return await chromium.launch();
}

export async function loginContext(browser, viewport) {
  const creds = credentials();
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  await page.goto("http://127.0.0.1:3000/login", { waitUntil: "domcontentloaded" });
  await page.waitForSelector("button[type=\"submit\"]", { state: "visible" });
  await page.waitForTimeout(600);
  await page.getByRole("textbox", { name: "Username" }).fill(creds.username);
  await page.getByRole("textbox", { name: "Password" }).fill(creds.password);
  await page.getByRole("textbox", { name: "Password" }).press("Enter");
  await page.getByLabel("Authentication code").waitFor({ state: "visible", timeout: 15000 });
  await page.getByLabel("Authentication code").fill(totp());
  await page.getByRole("button", { name: "Verify and sign in" }).click();
  await page.waitForURL("http://127.0.0.1:3000/", { timeout: 15000 });
  return { context, page };
}

export async function gatewayCookies(context) {
  const state = await context.storageState();
  const cookies = Object.fromEntries(state.cookies.map((cookie) => [cookie.name, cookie.value]));
  return {
    cookie: `__Host-openknowledge-access=${cookies["__Host-openknowledge-access"]}; openknowledge-csrf=${cookies["openknowledge-csrf"]}`,
    csrf: cookies["openknowledge-csrf"],
  };
}

export function gatewayFetch({ cookie, csrf }) {
  return async function api(method, path, payload) {
    const response = await fetch("http://127.0.0.1:8000" + path, {
      method,
      headers: {
        "content-type": "application/json",
        cookie,
        "Idempotency-Key": crypto.randomUUID(),
        "X-CSRF-Token": csrf,
        Origin: "http://localhost:3000",
      },
      body: payload === undefined ? undefined : JSON.stringify(payload),
    });
    if (!response.ok) {
      throw new Error(`${method} ${path} -> ${response.status} ${await response.text()}`);
    }
    return await response.json();
  };
}
