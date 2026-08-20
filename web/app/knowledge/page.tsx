import { SessionGate } from "@/components/auth/session-gate";
import { KnowledgeConsole } from "@/components/management-console";

export default function KnowledgePage() {
  return <SessionGate><KnowledgeConsole /></SessionGate>;
}
