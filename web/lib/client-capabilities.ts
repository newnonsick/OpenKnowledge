import { contractClient, contractData } from "@/lib/api-client";
import type { components } from "@/lib/generated/openapi";

type ClientCapabilities = components["schemas"]["ClientCapabilities"];

const cacheTtlMs = 45_000;

let cached: ClientCapabilities | null = null;
let cachedAt = 0;
let inFlight: Promise<ClientCapabilities> | null = null;
let generation = 0;

async function fetchCapabilities(signal?: AbortSignal): Promise<ClientCapabilities> {
  return contractData(contractClient.GET("/api/v1/capabilities", { signal }));
}

export async function loadClientCapabilities(signal?: AbortSignal): Promise<ClientCapabilities> {
  if (cached && Date.now() - cachedAt < cacheTtlMs) {
    return cached;
  }
  if (!inFlight) {
    const requestGeneration = generation;
    inFlight = fetchCapabilities(signal).then((capabilities) => {
      if (requestGeneration === generation) {
        cached = capabilities;
        cachedAt = Date.now();
      }
      return capabilities;
    }).finally(() => {
      if (requestGeneration === generation) inFlight = null;
    });
  }
  return inFlight;
}

export function invalidateClientCapabilities(): void {
  generation += 1;
  inFlight = null;
  cached = null;
  cachedAt = 0;
}
