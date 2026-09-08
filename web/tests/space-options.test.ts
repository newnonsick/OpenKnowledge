import { beforeEach, expect, it, vi } from "vitest";
import { contractClient } from "@/lib/api-client";
import { invalidateAccessibleSpaces, loadAccessibleSpaces } from "@/lib/space-options";

vi.mock("@/lib/api-client", () => ({
  contractClient: { GET: vi.fn() },
  contractData: (request: unknown) => request,
}));

beforeEach(() => {
  vi.mocked(contractClient.GET).mockReset();
  invalidateAccessibleSpaces();
});

it("isolates a new session from an earlier session's pending space request", async () => {
  let resolveOld!: (value: unknown) => void;
  vi.mocked(contractClient.GET)
    .mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve; }) as never)
    .mockResolvedValueOnce({ items: [{ id: "new-session" }], total_pages: 1 } as never);
  const oldRequest = loadAccessibleSpaces();
  invalidateAccessibleSpaces();
  const newRequest = loadAccessibleSpaces();
  expect(contractClient.GET).toHaveBeenCalledTimes(2);
  expect(await newRequest).toEqual([{ id: "new-session" }]);
  resolveOld({ items: [{ id: "old-session" }], total_pages: 1 });
  await oldRequest;
  expect(await loadAccessibleSpaces()).toEqual([{ id: "new-session" }]);
});
