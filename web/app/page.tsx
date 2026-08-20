import { SessionGate } from "@/components/auth/session-gate";
import { DashboardController } from "@/components/dashboard-controller";

export default function HomePage() {
  return <SessionGate><DashboardController /></SessionGate>;
}
