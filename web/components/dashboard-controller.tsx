"use client";

import { useEffect, useState } from "react";

import { contractClient, contractData } from "@/lib/api-client";
import { useCurrentMember } from "@/components/auth/session-gate";
import { DashboardOperations, DashboardShell, DashboardSpace } from "@/components/dashboard-shell";

export function DashboardController() {
  const member = useCurrentMember();
  const [spaces, setSpaces] = useState<DashboardSpace[]>([]);
  const [ready, setReady] = useState(false);
  const [operations, setOperations] = useState<DashboardOperations | null>(null);

  useEffect(() => {
    let active = true;
    Promise.allSettled([
      contractData(contractClient.GET("/api/v1/spaces")),
      contractData(contractClient.GET("/healthz/ready")),
      contractData(contractClient.GET("/api/v1/operations/summary")),
    ]).then(([spacesResult, healthResult, operationsResult]) => {
      if (!active) {
        return;
      }
      if (spacesResult.status === "fulfilled") {
        setSpaces(spacesResult.value.items.map((space) => ({
          createdAt: space.created_at,
          id: space.id,
          name: space.name,
          role: space.role,
        })));
      }
      setReady(healthResult.status === "fulfilled" && healthResult.value.status === "ready");
      setOperations(operationsResult.status === "fulfilled" ? operationsResult.value : null);
    });
    return () => {
      active = false;
    };
  }, []);

  return (
    <DashboardShell
      member={{
        displayName: member.display_name,
        role: member.system_role === "super_admin" ? "Super admin" : "Member",
        systemRole: member.system_role,
      }}
      operations={operations}
      ready={ready}
      spaces={spaces}
    />
  );
}
