import { contractClient, contractData } from "@/lib/api-client";
import type { components } from "@/lib/generated/openapi";

type Space = components["schemas"]["SpaceSummary"];

const pageSize = 100;

export async function loadAccessibleSpaces(signal?: AbortSignal): Promise<Space[]> {
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
