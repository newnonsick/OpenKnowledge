import { SessionGate } from "@/components/auth/session-gate";
import { SourcesConsole } from "@/components/management-console";

export default function SourcesPage() {
  return <SessionGate><SourcesConsole /></SessionGate>;
}
