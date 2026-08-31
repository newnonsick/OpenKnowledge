import { describe, expect, it } from "vitest";

import { exitCodeFor } from "../scripts/lib/audit-results.mjs";

describe("audit result status", () => {
  it("returns a failure exit code when any check fails", () => {
    expect(exitCodeFor([{ ok: true }, { ok: false }])).toBe(1);
  });

  it("returns a success exit code when every check passes", () => {
    expect(exitCodeFor([{ ok: true }, { ok: true }])).toBe(0);
  });
});
