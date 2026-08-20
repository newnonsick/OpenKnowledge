import { SessionGate } from "@/components/auth/session-gate";
import { ActivityConsole } from "@/components/management-console";

export default function ActivityPage() {
  return <SessionGate><ActivityConsole /></SessionGate>;
}
