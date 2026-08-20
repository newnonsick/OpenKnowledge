import { SessionGate } from "@/components/auth/session-gate";
import { IngestionConsole } from "@/components/management-console";

export default function IngestionPage() {
  return <SessionGate><IngestionConsole /></SessionGate>;
}
