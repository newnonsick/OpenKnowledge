import { SessionGate } from "@/components/auth/session-gate";
import { SpacesConsole } from "@/components/management-console";

export default function SpacesPage() {
  return <SessionGate><SpacesConsole /></SessionGate>;
}
