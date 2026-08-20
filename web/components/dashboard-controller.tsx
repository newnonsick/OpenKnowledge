"use client";

import { useEffect, useState } from "react";

import { apiRequest } from "@/lib/api-client";
import { useCurrentMember } from "@/components/auth/session-gate";
import { DashboardShell, DashboardSpace } from "@/components/dashboard-shell";

type SpacesResponse = {
  items: Array<{
    created_at: string;
    id: string;
    name: string;
    role: string;
  }>;
};

type HealthResponse = {
  status: "not_ready" | "ready";
};

export function DashboardController() {
  const member = useCurrentMember();
  const [spaces, setSpaces] = useState<DashboardSpace[]>([]);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let active = true;
    Promise.allSettled([
      apiRequest<SpacesResponse>("/api/v1/spaces"),
      apiRequest<HealthResponse>("/healthz/ready", { retryAuthentication: false }),
    ]).then(([spacesResult, healthResult]) => {
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
      }}
      ready={ready}
      spaces={spaces}
    />
  );
}
