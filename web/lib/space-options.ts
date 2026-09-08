import { contractClient, contractData } from "@/lib/api-client";
import type { components } from "@/lib/generated/openapi";

type Space = components["schemas"]["SpaceSummary"];

const pageSize = 100;
const cacheTtlMs = 45_000;

let cachedSpaces: Space[] | null = null;
let cachedAt = 0;
let inFlight: Promise<Space[]> | null = null;
let generation = 0;

async function fetchAccessibleSpaces(signal?: AbortSignal): Promise<Space[]> {
  const firstPage = await contractData(contractClient.GET("/api/v1/spaces", {
    params: { query: { page: 1, page_size: pageSize } },
    signal,
  }));
  const remainingPages = Array.from({ length: Math.max(0, firstPage.total_pages - 1) }, (_, index) => index + 2);
  const pages = await Promise.all(remainingPages.map((page) => contractData(contractClient.GET("/api/v1/spaces", {
    params: { query: { page, page_size: pageSize } },
    signal,
  }))));
  const seen = new Set<string>();
  return [firstPage, ...pages].flatMap((response) => response.items).filter((space) => {
    if (seen.has(space.id)) {
      return false;
    }
    seen.add(space.id);
    return true;
  });
}

export async function loadAccessibleSpaces(signal?: AbortSignal): Promise<Space[]> {
  if (cachedSpaces && Date.now() - cachedAt < cacheTtlMs) {
    return cachedSpaces;
  }
  if (!inFlight) {
    const requestGeneration = generation;
    inFlight = fetchAccessibleSpaces(signal).then((spaces) => {
      if (requestGeneration === generation) {
        cachedSpaces = spaces;
        cachedAt = Date.now();
      }
      return spaces;
    }).finally(() => {
      if (requestGeneration === generation) inFlight = null;
    });
  }
  return inFlight;
}

export function invalidateAccessibleSpaces(): void {
  generation += 1;
  inFlight = null;
  cachedSpaces = null;
  cachedAt = 0;
}
