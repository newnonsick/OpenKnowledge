import { SessionGate } from "@/components/auth/session-gate";
import { PeopleConsole } from "@/components/management-console";

export default function PeoplePage() {
  return <SessionGate><PeopleConsole /></SessionGate>;
}
