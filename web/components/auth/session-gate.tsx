"use client";

import { createContext, FormEvent, ReactNode, useContext, useEffect, useId, useRef, useState } from "react";
import { Boxes, KeyRound, LoaderCircle, ShieldCheck, X } from "lucide-react";
import { usePathname, useRouter } from "next/navigation";

import { ModalDialog } from "@/components/confirmation-dialog";
import { ApiError, apiRequest, contractClient, contractData, noteMeaningfulActivity, refreshSession } from "@/lib/api-client";
import { focusModalReturnTarget, modalReturnTargetFor } from "@/lib/focus-management";
import type { components } from "@/lib/generated/openapi";

export type CurrentMember = components["schemas"]["CurrentMember"];

const SessionContext = createContext<CurrentMember | null>(null);
const activityWindow = 20 * 60 * 1000;
const refreshInterval = 10 * 60 * 1000;

let cachedMember: CurrentMember | null = null;

export function resetCachedMember(): void {
  cachedMember = null;
}

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
  const [member, setMember] = useState<CurrentMember | null>(cachedMember);
  const [failed, setFailed] = useState(false);
  const [stepUpOpen, setStepUpOpen] = useState(false);
  const [stepUpReturnTarget, setStepUpReturnTarget] = useState<HTMLElement | null>(null);
  const [stepUpFactor, setStepUpFactor] = useState<"recovery" | "totp">("totp");
  const [stepUpError, setStepUpError] = useState<string | null>(null);
  const [stepUpSubmitting, setStepUpSubmitting] = useState(false);
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
        cachedMember = current;
        setMember(current);
      })
      .catch(() => {
        cachedMember = null;
        if (active) {
          setFailed(true);
          setMember(null);
          router.replace("/login");
        }
      });
    return () => {
      active = false;
    };
  }, [pathname, router]);

  useEffect(() => {
    const current = member;
    if (!current) {
      return;
    }
    if (current.requires_password_change && !pathname.startsWith("/first-use/")) {
      router.replace("/first-use/password");
      return;
    }
    if (current.system_role !== "super_admin" && (pathname.startsWith("/people") || pathname.startsWith("/activity"))) {
      router.replace("/");
    }
  }, [member, pathname, router]);

  const routeBlocked = member !== null && (
    (member.requires_password_change && !pathname.startsWith("/first-use/")) ||
    (member.system_role !== "super_admin" && (pathname.startsWith("/people") || pathname.startsWith("/activity")))
  );

  useEffect(() => {
    const recordActivity = () => {
      if (document.visibilityState === "visible") {
        lastActivity.current = Date.now();
        noteMeaningfulActivity();
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

  useEffect(() => {
    const requireStepUp = (event: Event) => {
      queueMicrotask(() => {
        setStepUpReturnTarget(modalReturnTargetFor(event));
        setStepUpError(null);
        setStepUpOpen(true);
      });
    };
    window.addEventListener("aigw-step-up-required", requireStepUp);
    return () => window.removeEventListener("aigw-step-up-required", requireStepUp);
  }, []);

  const stepUpTitleId = useId();
  const stepUpDescriptionId = useId();

  function closeStepUp() {
    setStepUpOpen(false);
  }

  useEffect(() => {
    if (stepUpOpen || !stepUpReturnTarget) {
      return;
    }
    let focusTimer = 0;
    let attempts = 0;
    const restoreFocus = () => {
      attempts += 1;
      if (!focusModalReturnTarget(stepUpReturnTarget) && attempts < 20) {
        focusTimer = window.setTimeout(restoreFocus, 16);
      }
    };
    focusTimer = window.setTimeout(restoreFocus, 0);
    return () => window.clearTimeout(focusTimer);
  }, [stepUpOpen, stepUpReturnTarget]);

  async function submitStepUp(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    setStepUpSubmitting(true);
    setStepUpError(null);
    try {
      await apiRequest("/api/v1/auth/step-up", {
        body: {
          password: String(fields.get("password") || ""),
          recovery_code: member?.system_role === "super_admin" && stepUpFactor === "recovery" ? String(fields.get("recovery_code") || "") : null,
          totp_code: member?.system_role === "super_admin" && stepUpFactor === "totp" ? String(fields.get("totp_code") || "") : null,
        },
        method: "POST",
        retryAuthentication: false,
      });
      closeStepUp();
    } catch (error) {
      setStepUpError(error instanceof ApiError && error.code === "recent_authentication_required"
        ? "Verify your identity, then run that action again."
        : error instanceof ApiError ? error.message : "Identity verification failed.");
    } finally {
      setStepUpSubmitting(false);
    }
  }

  if (!member || routeBlocked) {
    return (
      <main className="session-loading" aria-live="polite">
        <span><Boxes aria-hidden="true" size={20} /></span>
        <LoaderCircle aria-hidden="true" className="spin" size={20} />
        <p>{failed ? "Returning to sign in…" : "Opening your private console…"}</p>
      </main>
    );
  }

  return (
    <SessionContext.Provider value={member}>
      {children}
      <ModalDialog ariaDescribedBy={stepUpDescriptionId} ariaLabelledBy={stepUpTitleId} className="step-up-dialog" onClose={() => { if (!stepUpSubmitting) closeStepUp(); }} open={stepUpOpen} returnFocusTarget={stepUpReturnTarget}>
            <div className="step-up-heading"><span><KeyRound aria-hidden="true" size={18} /></span><div><small>Security check</small><h2 id={stepUpTitleId}>Verify your identity</h2></div><button aria-label="Close identity verification" disabled={stepUpSubmitting} onClick={closeStepUp} type="button"><X aria-hidden="true" size={18} /></button></div>
            <p id={stepUpDescriptionId}>For extra security, this action needs a fresh identity check. Nothing was saved yet — verify below, then run the action again.</p>
            <form onSubmit={submitStepUp}>
              <label className="field-label" htmlFor="step-up-password">Current password</label>
              <input autoComplete="current-password" className="text-field" id="step-up-password" name="password" required type="password" />
              {member.system_role === "super_admin" ? (
                <>
                  <div className="step-up-factor-tabs" role="group" aria-label="Authentication factor">
                    <button aria-pressed={stepUpFactor === "totp"} onClick={() => setStepUpFactor("totp")} type="button"><ShieldCheck aria-hidden="true" size={14} />Authenticator</button>
                    <button aria-pressed={stepUpFactor === "recovery"} onClick={() => setStepUpFactor("recovery")} type="button"><KeyRound aria-hidden="true" size={14} />Recovery code</button>
                  </div>
                  <label className="field-label" htmlFor={stepUpFactor === "totp" ? "step-up-totp" : "step-up-recovery"}>{stepUpFactor === "totp" ? "Authentication code" : "Recovery code"}</label>
                  <input autoComplete="one-time-code" className="text-field code-field" id={stepUpFactor === "totp" ? "step-up-totp" : "step-up-recovery"} inputMode={stepUpFactor === "totp" ? "numeric" : undefined} maxLength={stepUpFactor === "totp" ? 8 : 64} name={stepUpFactor === "totp" ? "totp_code" : "recovery_code"} required />
                </>
              ) : null}
              {stepUpError ? <div className="auth-error" role="alert">{stepUpError}</div> : null}
              <div className="step-up-actions"><button className="secondary-button" disabled={stepUpSubmitting} onClick={closeStepUp} type="button">Cancel</button><button className="primary-button" disabled={stepUpSubmitting} type="submit">{stepUpSubmitting ? "Verifying…" : "Verify identity"}</button></div>
            </form>
      </ModalDialog>
    </SessionContext.Provider>
  );
}
