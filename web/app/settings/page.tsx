import { SessionGate } from "@/components/auth/session-gate";
import { SettingsConsole } from "@/components/management-console";

export default function SettingsPage() {
  return <SessionGate><SettingsConsole /></SessionGate>;
}
