import { SessionGate } from "@/components/auth/session-gate";
import { AiActionsConsole } from "@/components/management-console";

export default function AiActionsPage() {
  return <SessionGate><AiActionsConsole /></SessionGate>;
}
