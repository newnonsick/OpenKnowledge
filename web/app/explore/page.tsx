import { SessionGate } from "@/components/auth/session-gate";
import { ExploreConsole } from "@/components/management-console";

export default function ExplorePage() {
  return <SessionGate><ExploreConsole /></SessionGate>;
}
