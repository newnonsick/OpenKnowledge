"use client";

import { createContext, ReactNode, useContext, useEffect, useRef, useState } from "react";
import { Boxes, LoaderCircle } from "lucide-react";
import { usePathname, useRouter } from "next/navigation";

import { ApiError, contractClient, contractData, refreshSession } from "@/lib/api-client";
import type { components } from "@/lib/generated/openapi";

export type CurrentMember = components["schemas"]["CurrentMember"];

const SessionContext = createContext<CurrentMember | null>(null);
const activityWindow = 20 * 60 * 1000;
const refreshInterval = 10 * 60 * 1000;

export function useCurrentMember(): CurrentMember {
  const member = useContext(SessionContext);
  if (!member) {
    throw new Error("Session context is unavailable");
  }
  return member;
}

export function SessionGate({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [member, setMember] = useState<CurrentMember | null>(null);
  const [failed, setFailed] = useState(false);
  const lastActivity = useRef(Date.now());
  const lastRefresh = useRef(Date.now());
  const refreshActive = useRef(false);

  useEffect(() => {
    let active = true;
    contractData(contractClient.GET("/api/v1/me"))
      .then((current) => {
        if (!active) {
          return;
        }
        if (current.requires_password_change) {
          router.replace("/first-use/password");
          return;
        }
        if (current.system_role !== "super_admin" && (pathname.startsWith("/people") || pathname.startsWith("/activity"))) {
          router.replace("/");
          return;
        }
        setMember(current);
      })
      .catch(() => {
        if (active) {
          setFailed(true);
          router.replace("/login");
        }
      });
    return () => {
      active = false;
    };
  }, [pathname, router]);

  useEffect(() => {
    const recordActivity = () => {
      if (document.visibilityState === "visible") {
        lastActivity.current = Date.now();
      }
    };
    const events: Array<keyof WindowEventMap> = ["focus", "keydown", "pointerdown"];
    events.forEach((event) => window.addEventListener(event, recordActivity, { passive: true }));
    document.addEventListener("visibilitychange", recordActivity);
    const timer = window.setInterval(async () => {
      const now = Date.now();
      if (
        document.visibilityState !== "visible" ||
        !navigator.locks ||
        refreshActive.current ||
        now - lastActivity.current > activityWindow ||
        now - lastRefresh.current < refreshInterval
      ) {
        return;
      }
      refreshActive.current = true;
      try {
        await refreshSession();
        lastRefresh.current = Date.now();
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) {
          router.replace("/login");
        }
      } finally {
        refreshActive.current = false;
      }
    }, 60 * 1000);
    return () => {
      events.forEach((event) => window.removeEventListener(event, recordActivity));
      document.removeEventListener("visibilitychange", recordActivity);
      window.clearInterval(timer);
    };
  }, [router]);

  if (!member) {
    return (
      <main className="session-loading" aria-live="polite">
        <span><Boxes aria-hidden="true" size={20} /></span>
        <LoaderCircle aria-hidden="true" className="spin" size={20} />
        <p>{failed ? "Returning to sign in…" : "Opening your private console…"}</p>
      </main>
    );
  }

  return <SessionContext.Provider value={member}>{children}</SessionContext.Provider>;
}
