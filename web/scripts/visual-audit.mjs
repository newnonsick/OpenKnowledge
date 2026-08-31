import { mkdirSync, writeFileSync } from "node:fs";
import { exitCodeFor } from "./lib/audit-results.mjs";
import { launch, loginContext } from "./lib/session.mjs";

const VIEWPORTS = [
  { name: "desktop", width: 1440, height: 1000 },
  { name: "tablet", width: 834, height: 1112 },
  { name: "mobile", width: 375, height: 812 },
];

const PAGES = [
  { path: "/", name: "home" },
  { path: "/explore", name: "explore" },
  { path: "/spaces", name: "spaces" },
  { path: "/knowledge", name: "knowledge" },
  { path: "/sources", name: "sources" },
  { path: "/ingestion", name: "ingestion" },
  { path: "/ai-actions", name: "ai-actions" },
  { path: "/people", name: "people" },
  { path: "/activity", name: "activity" },
  { path: "/settings?section=api-keys", name: "settings-api-keys" },
  { path: "/settings?section=sessions", name: "settings-sessions" },
  { path: "/settings?section=runtime", name: "settings-runtime" },
  { path: "/first-use/password", name: "first-use-password" },
  { path: "/first-use/mfa", name: "first-use-mfa" },
  { path: "/login", name: "login" },
];

const loadingSelector = ".session-loading, .list-skeleton, .console-loading, .enrollment-loading, [aria-busy='true']";

mkdirSync("../reports/shots/audit", { recursive: true });

const browser = await launch();
const findings = [];
const measurements = [];

for (const viewport of VIEWPORTS) {
  let context, page;
  try {
    ({ context, page } = await loginContext(browser, { width: viewport.width, height: viewport.height }));
  } catch (loginError) {
    findings.push({ viewport: viewport.name, page: "login", issue: "login failed: " + String(loginError).slice(0, 160) });
    continue;
  }
  const consoleErrors = [];
  page.on("console", (msg) => {
    const expectedEnrollmentResponse = new URL(page.url()).pathname === "/first-use/mfa" && msg.text().includes("status of 401");
    if (msg.type() === "error" && !msg.text().includes("Applying inline style violates") && !expectedEnrollmentResponse) {
      consoleErrors.push(msg.text().slice(0, 250));
    }
  });
  page.on("pageerror", (err) => consoleErrors.push("pageerror: " + String(err).slice(0, 250)));

  for (const target of PAGES) {
    await page.goto(`http://127.0.0.1:3000${target.path}`, { waitUntil: "domcontentloaded" });
    await page.waitForFunction((selector) => !document.querySelector(selector), loadingSelector, { timeout: 30000 }).catch(() => {});
    await page.waitForTimeout(100);
    const unsettled = await page.locator(loadingSelector).count();
    if (unsettled > 0) {
      findings.push({ viewport: viewport.name, page: target.name, issue: `${unsettled} loading states remained visible` });
    }
    const overflow = await page.evaluate(() => {
      const doc = document.documentElement;
      const wide = [];
      doc.querySelectorAll("*").forEach((el) => {
        const box = el.getBoundingClientRect();
        if (box.right > doc.clientWidth + 1 && box.width > 8) {
          wide.push(`${el.tagName.toLowerCase()}.${String(el.className).split(" ")[0]}(${Math.round(box.right)})`);
        }
      });
      const componentOverflow = [...document.querySelectorAll("main *")].filter((el) => {
        const style = getComputedStyle(el);
        return style.display !== "none" && style.visibility !== "hidden" && style.overflowX === "visible" && el.scrollWidth > el.clientWidth + 1;
      }).map((el) => `${el.tagName.toLowerCase()}.${String(el.className).split(" ")[0]}`).slice(0, 8);
      return { scrollW: doc.scrollWidth, clientW: doc.clientWidth, componentOverflow, pageHeight: doc.scrollHeight, wide: wide.slice(0, 6), url: location.pathname + location.search };
    });
    if (overflow.url !== target.path) {
      findings.push({ viewport: viewport.name, page: target.name, issue: `redirected to ${overflow.url}` });
      continue;
    }
    if (overflow.scrollW > overflow.clientW + 1) {
      findings.push({
        viewport: viewport.name,
        page: target.name,
        issue: `horizontal overflow ${overflow.scrollW}>${overflow.clientW}`,
        elements: overflow.wide,
      });
    }
    if (overflow.componentOverflow.length > 0) {
      findings.push({ viewport: viewport.name, page: target.name, issue: "visible component overflow", elements: overflow.componentOverflow });
    }
    measurements.push({ viewport: viewport.name, page: target.name, pageHeight: overflow.pageHeight, clientWidth: overflow.clientW, scrollWidth: overflow.scrollW, componentOverflow: overflow.componentOverflow });
    await page.screenshot({ path: `../reports/shots/audit/${target.name}-${viewport.name}.png`, fullPage: true });
  }

  if (consoleErrors.length > 0) {
    findings.push({ viewport: viewport.name, page: "console", issue: "console errors", elements: [...new Set(consoleErrors)].slice(0, 8) });
  }
  await context.close();
}

await browser.close();
writeFileSync("../reports/audit-findings.json", JSON.stringify(findings, null, 2));
writeFileSync("../reports/audit-measurements.json", JSON.stringify(measurements, null, 2));
console.log(JSON.stringify(findings, null, 1));
console.log("AUDIT DONE, findings:", findings.length);
process.exit(exitCodeFor(findings.map(() => ({ ok: false }))));
