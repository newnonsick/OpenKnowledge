"use client";

import { createContext, ReactNode, useContext, useEffect, useRef, useState } from "react";
import { Boxes, LoaderCircle } from "lucide-react";
import { useRouter } from "next/navigation";

import { ApiError, apiRequest, refreshSession } from "@/lib/api-client";

export type CurrentMember = {
  display_name: string;
  id: string;
  requires_password_change: boolean;
  status: string;
  system_role: "member" | "super_admin";
  username: string;
};

const SessionContext = createContext<CurrentMember | null>(null);
const activityWindow = 20 * 60 * 1000;
const refreshInterval = 10 * 60 * 1000;
const coordinationWindow = 30 * 1000;
const refreshTimestampKey = "aigw-last-session-refresh";

async function coordinatedRefresh(now: number): Promise<void> {
  const execute = async () => {
    const recent = Number(localStorage.getItem(refreshTimestampKey) || 0);
    if (now - recent < coordinationWindow) {
      return;
    }
    await refreshSession();
    localStorage.setItem(refreshTimestampKey, String(Date.now()));
  };
  if (navigator.locks) {
    await navigator.locks.request("aigw-session-refresh", execute);
  } else {
    await execute();
  }
}

export function useCurrentMember(): CurrentMember {
  const member = useContext(SessionContext);
  if (!member) {
    throw new Error("Session context is unavailable");
  }
  return member;
}

export function SessionGate({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [member, setMember] = useState<CurrentMember | null>(null);
  const [failed, setFailed] = useState(false);
  const lastActivity = useRef(Date.now());
  const lastRefresh = useRef(Date.now());
  const refreshActive = useRef(false);

  useEffect(() => {
    let active = true;
    apiRequest<CurrentMember>("/api/v1/me")
      .then((current) => {
        if (!active) {
          return;
        }
        if (current.requires_password_change) {
          router.replace("/first-use/password");
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
  }, [router]);

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
      if (refreshActive.current || now - lastActivity.current > activityWindow || now - lastRefresh.current < refreshInterval) {
        return;
      }
      refreshActive.current = true;
      try {
        await coordinatedRefresh(now);
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
