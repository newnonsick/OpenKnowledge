import { writeFileSync } from "node:fs";
import { launch, loginContext, gatewayCookies, gatewayFetch } from "./lib/session.mjs";

const browser = await launch();
const { context } = await loginContext(browser, { width: 1280, height: 800 });
const api = gatewayFetch(await gatewayCookies(context));

const spaces = await api("GET", "/api/v1/spaces?page=1&page_size=100");
const existing = spaces.items.find((space) => space.name === "Pagination Demo");
const space = existing ?? await api("POST", "/api/v1/spaces", { name: "Pagination Demo" });
console.log("space:", space.id, space.name);

const knowledge = await api("GET", `/api/v1/knowledge?page=1&page_size=1&space_id=${space.id}`);
console.log("existing knowledge in space:", knowledge.total_items);

const start = knowledge.total_items + 1;
for (let i = start; i <= 34; i += 1) {
  await api("POST", "/api/v1/knowledge", {
    space_id: space.id,
    title: `Pagination demo entry ${String(i).padStart(2, "0")}`,
    content: `Entry ${i}: The quick brown fox keeps family recipes, wifi passwords, and trip plans safe. Reference number REF-${1000 + i}.`,
    tags: ["demo", `batch-${Math.ceil(i / 10)}`],
  });
}
const after = await api("GET", "/api/v1/knowledge?page=1&page_size=1");
console.log("total knowledge now:", after.total_items);

writeFileSync("../reports/seed-summary.json", JSON.stringify({ spaceId: space.id, knowledgeTotal: after.total_items }, null, 2));
await browser.close();
console.log("SEED DONE");
