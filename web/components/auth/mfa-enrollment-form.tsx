"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { Check, Copy, LoaderCircle, ShieldCheck } from "lucide-react";
import { useRouter } from "next/navigation";

import { MfaEnrollmentSecret } from "@/components/auth/mfa-enrollment-secret";
import { ApiError, contractClient, contractDataWithSessionRetry } from "@/lib/api-client";
import { resetCachedMember } from "@/components/auth/session-gate";
import type { components } from "@/lib/generated/openapi";

type Enrollment = components["schemas"]["TotpEnrollment"];
type InitialAPIKey = components["schemas"]["InitialAPIKey"];

export function MfaEnrollmentForm() {
  const router = useRouter();
  const started = useRef(false);
  const successHeadingRef = useRef<HTMLHeadingElement>(null);
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null);
  const [code, setCode] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState<string[] | null>(null);
  const [initialKey, setInitialKey] = useState<InitialAPIKey | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (recoveryCodes) {
      successHeadingRef.current?.focus();
    }
  }, [recoveryCodes]);

  useEffect(() => {
    if (started.current) {
      return;
    }
    started.current = true;
    contractDataWithSessionRetry(() => contractClient.POST("/api/v1/auth/mfa/totp/enroll", { body: {} }))
      .then(setEnrollment)
      .catch((requestError) => setError(requestError instanceof ApiError ? requestError.message : "Authenticator setup could not start."))
      .finally(() => setLoading(false));
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!enrollment || !/^\d{6,8}$/.test(code)) {
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const confirmation = await contractDataWithSessionRetry(() => contractClient.POST("/api/v1/auth/mfa/totp/confirm", {
        body: { code, factor_id: enrollment.factor_id },
      }));
      setRecoveryCodes(confirmation.recovery_codes);
      setInitialKey(confirmation.initial_api_key ?? null);
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : "The authentication code was not accepted.");
    } finally {
      setSubmitting(false);
    }
  }

  async function copyCodes() {
    if (!recoveryCodes) {
      return;
    }
    try {
      await navigator.clipboard.writeText(recoveryCodes.join("\n"));
      setCopied(true);
    } catch {
      setError("Copy failed — select the codes manually.");
    }
  }

  if (recoveryCodes) {
    return (
      <section aria-labelledby="recovery-secrets-heading" className="first-use-card recovery-card">
        <span className="first-use-step">Final security step</span>
        <span className="first-use-icon mint"><Check aria-hidden="true" size={22} /></span>
        <h1 id="recovery-secrets-heading" ref={successHeadingRef} tabIndex={-1}>Save your recovery secrets.</h1>
        <p>Your recovery codes and first API key are shown only once. Keep them somewhere separate from your authenticator.</p>
        <div aria-label="Recovery codes" className="recovery-codes" role="status">
          {recoveryCodes.map((recoveryCode) => <code key={recoveryCode}>{recoveryCode}</code>)}
        </div>
        <button className="secondary-action" onClick={copyCodes} type="button"><Copy aria-hidden="true" size={15} />{copied ? "Copied" : "Copy all codes"}</button>
        {initialKey ? <><div className="secret-value"><code>{initialKey.secret}</code><button aria-label="Copy API key" onClick={() => { navigator.clipboard.writeText(initialKey.secret).catch(() => setError("Copy failed — select the key manually.")); }} type="button"><Copy aria-hidden="true" size={15} /></button></div><p className="secret-scope-summary">API access: {initialKey.scopes.join(", ")}</p></> : null}
        {error ? <div className="auth-error" role="alert">{error}</div> : null}
        <button className="auth-submit" onClick={() => { resetCachedMember(); router.replace("/"); }} type="button">I have saved these secrets</button>
      </section>
    );
  }

  return (
    <form className="first-use-card" onSubmit={submit}>
      <span className="first-use-step">Step 2 of 2</span>
      <span className="first-use-icon"><ShieldCheck aria-hidden="true" size={22} /></span>
      <h1>Protect admin access.</h1>
      <p>Scan this code with any TOTP-compatible authenticator, then enter its current code.</p>

      {loading ? <div className="enrollment-loading"><LoaderCircle aria-hidden="true" className="spin" size={18} />Creating a private setup key…</div> : null}
      {enrollment ? <MfaEnrollmentSecret provisioningUri={enrollment.provisioning_uri} secret={enrollment.secret} /> : null}

      <label className="field-label" htmlFor="mfa-code">6-digit authentication code</label>
      <input autoComplete="one-time-code" className="text-field code-field" disabled={!enrollment} id="mfa-code" inputMode="numeric" maxLength={8} onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))} pattern="[0-9]{6,8}" required value={code} />
      {error ? <div className="auth-error" role="alert">{error}</div> : null}
      <button className="auth-submit" disabled={!enrollment || !/^\d{6,8}$/.test(code) || submitting} type="submit">
        {submitting ? <LoaderCircle aria-hidden="true" className="spin" size={17} /> : null}
        {submitting ? "Verifying…" : "Verify and continue"}
      </button>
    </form>
  );
}
