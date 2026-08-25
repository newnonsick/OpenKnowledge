"use client";

import { useEffect, useState } from "react";

import { contractClient, contractData } from "@/lib/api-client";
import { useCurrentMember } from "@/components/auth/session-gate";
import { DashboardOperations, DashboardShell, DashboardSpace } from "@/components/dashboard-shell";

export function DashboardController() {
  const member = useCurrentMember();
  const [spaces, setSpaces] = useState<DashboardSpace[]>([]);
  const [spaceCount, setSpaceCount] = useState(0);
  const [spacesLoaded, setSpacesLoaded] = useState(false);
  const [loading, setLoading] = useState(true);
  const [ready, setReady] = useState(false);
  const [operations, setOperations] = useState<DashboardOperations | null>(null);

  useEffect(() => {
    let active = true;
    Promise.allSettled([
      contractData(contractClient.GET("/api/v1/spaces", { params: { query: { page: 1, page_size: 3 } } })),
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
        setSpaceCount(spacesResult.value.total_items);
        setSpacesLoaded(true);
      } else {
        setSpaceCount(0);
        setSpacesLoaded(false);
      }
      setReady(healthResult.status === "fulfilled" && healthResult.value.status === "ready");
      setOperations(operationsResult.status === "fulfilled" ? operationsResult.value : null);
      setLoading(false);
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
      loading={loading}
      operations={operations}
      ready={ready}
      spaceCount={spaceCount}
      spacesLoaded={spacesLoaded}
      spaces={spaces}
    />
  );
}
