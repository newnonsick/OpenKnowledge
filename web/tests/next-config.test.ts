import { describe, expect, it } from "vitest";

import nextConfig from "@/next.config";

describe("next configuration", () => {
  it("proxies gateway health probes to the API", async () => {
    if (!nextConfig.rewrites) {
      throw new Error("Gateway rewrites are required");
    }
    const rewrites = await nextConfig.rewrites();
    const rules = Array.isArray(rewrites) ? rewrites : [];

    expect(rules).toContainEqual(
      expect.objectContaining({
        destination: "http://127.0.0.1:8000/healthz/:path*",
        source: "/healthz/:path*",
      }),
    );
  });
});
