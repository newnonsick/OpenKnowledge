import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { AppSidebar } from "@/components/app-sidebar";

const mocks = vi.hoisted(() => ({ post: vi.fn(), replace: vi.fn(), reset: vi.fn() }));
vi.mock("next/navigation", () => ({ usePathname: () => "/", useRouter: () => ({ replace: mocks.replace }) }));
vi.mock("@/lib/api-client", () => ({ contractClient: { POST: mocks.post }, contractData: (request: unknown) => request }));
vi.mock("@/components/auth/session-gate", () => ({ resetCachedMember: mocks.reset }));

it("keeps a failed sign-out recoverable and redirects only after revocation succeeds", async () => {
  mocks.post.mockRejectedValueOnce(new TypeError("Offline")).mockResolvedValueOnce({});
  render(<AppSidebar closeRef={{ current: null }} drawerId="navigation" drawerRef={{ current: null }} manageGroups={[]} menuOpen={false} onMenuClose={() => {}} spaceCount={0} workspaceGroups={[]} member={{ displayName: "Test", role: "Member", systemRole: "member" }} />);
  fireEvent.click(screen.getByRole("button", { name: "Open account menu" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "Sign out" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Sign out failed");
  expect(mocks.replace).not.toHaveBeenCalled();
  expect(mocks.reset).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Retry sign out" }));
  await waitFor(() => expect(mocks.replace).toHaveBeenCalledWith("/login"));
  expect(mocks.reset).toHaveBeenCalledOnce();
});
