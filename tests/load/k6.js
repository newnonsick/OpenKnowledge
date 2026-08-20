import http from "k6/http";
import { check } from "k6";
import { Trend } from "k6/metrics";

const managementLatency = new Trend("management_latency", true);
const retrievalLatency = new Trend("retrieval_latency", true);
const streamingLatency = new Trend("streaming_latency", true);
const ingestionLatency = new Trend("ingestion_latency", true);
const baseUrl = __ENV.GATEWAY_BASE_URL;
const apiKey = __ENV.GATEWAY_API_KEY;
const spaceId = __ENV.GATEWAY_SPACE_ID || "global";
const headers = { Authorization: `Bearer ${apiKey}` };
const jsonHeaders = { ...headers, "Content-Type": "application/json" };

export const options = {
  scenarios: {
    management: { executor: "constant-vus", exec: "management", vus: 4, duration: "2m" },
    retrieval: { executor: "constant-vus", exec: "retrieval", vus: 4, duration: "2m" },
    streaming: { executor: "constant-vus", exec: "streaming", vus: 2, duration: "2m" },
    ingestion: { executor: "constant-arrival-rate", exec: "ingestion", rate: 2, timeUnit: "1m", duration: "2m", preAllocatedVUs: 1, maxVUs: 2 },
  },
  thresholds: {
    management_latency: ["p(95)<500"],
    retrieval_latency: ["p(95)<1000"],
    streaming_latency: ["p(95)<30000"],
    ingestion_latency: ["p(95)<1000"],
    http_req_failed: ["rate<0.01"],
  },
};

export function setup() {
  if (!baseUrl || !apiKey) {
    throw new Error("GATEWAY_BASE_URL and GATEWAY_API_KEY are required");
  }
}

export function management() {
  const response = http.get(`${baseUrl}/api/v1/spaces?limit=100`, { headers });
  managementLatency.add(response.timings.duration);
  check(response, { "management status 200": (value) => value.status === 200 });
}

export function retrieval() {
  const response = http.post(`${baseUrl}/api/v1/retrieval/search`, JSON.stringify({ query: "family procedures", semantic_policy: "prefer", limit: 10 }), { headers: jsonHeaders });
  retrievalLatency.add(response.timings.duration);
  check(response, { "retrieval status 200": (value) => value.status === 200 });
}

export function streaming() {
  const response = http.post(`${baseUrl}/v1/chat/completions`, JSON.stringify({ model: "default", stream: true, messages: [{ role: "user", content: "Reply with OK" }] }), { headers: jsonHeaders, timeout: "30s" });
  streamingLatency.add(response.timings.duration);
  check(response, { "streaming status 200": (value) => value.status === 200 });
}

export function ingestion() {
  const key = `${__VU}-${__ITER}-${Date.now()}`;
  const response = http.post(`${baseUrl}/api/v1/sources/upload`, { space_id: spaceId, display_name: `Load probe ${key}`, file: http.file(`load probe ${key}`, `${key}.txt`, "text/plain") }, { headers: { ...headers, "Idempotency-Key": key } });
  ingestionLatency.add(response.timings.duration);
  check(response, { "ingestion status 202": (value) => value.status === 202 });
}
