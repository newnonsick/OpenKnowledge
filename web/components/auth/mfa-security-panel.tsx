"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { Check, LoaderCircle, ShieldCheck } from "lucide-react";

import { MfaEnrollmentSecret } from "@/components/auth/mfa-enrollment-secret";
import { CopyButton } from "@/components/copy-button";
import { ApiError, contractClient, contractDataWithSessionRetry } from "@/lib/api-client";
import type { components } from "@/lib/generated/openapi";

type Enrollment = components["schemas"]["TotpEnrollment"];

export function MfaSecurityPanel({ enabled, onEnabled }: { enabled: boolean; onEnabled: () => void }) {
  const [protectedState, setProtectedState] = useState(enabled);
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null);
  const [code, setCode] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const passwordRef = useRef("");

  useEffect(() => {
    if (enabled) {
      setProtectedState(true);
    }
  }, [enabled]);

  const start = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) {
      return;
    }
    const form = event.currentTarget;
    const currentPassword = String(new FormData(form).get("current_password") || "");
    if (!currentPassword) {
      return;
    }
    setBusy(true);
    setError(null);
    passwordRef.current = currentPassword;
    try {
      const response = await contractDataWithSessionRetry(() => contractClient.POST("/api/v1/auth/mfa/totp/enroll", {
        body: { current_password: currentPassword },
      }));
      form.reset();
      setEnrollment(response);
    } catch (requestError) {
      passwordRef.current = "";
      setError(requestError instanceof ApiError ? requestError.message : "MFA setup could not start. Try again.");
    } finally {
      setBusy(false);
    }
  };

  const confirm = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!enrollment || !/^\d{6,8}$/.test(code) || busy) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const response = await contractDataWithSessionRetry(() => contractClient.POST("/api/v1/auth/mfa/totp/confirm", {
        body: {
          code,
          current_password: passwordRef.current,
          factor_id: enrollment.factor_id,
        },
      }));
      passwordRef.current = "";
      setRecoveryCodes(response.recovery_codes);
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : "The authentication code was not accepted.");
    } finally {
      setBusy(false);
    }
  };

  const cancel = () => {
    passwordRef.current = "";
    setEnrollment(null);
    setCode("");
    setError(null);
  };

  const acknowledge = () => {
    passwordRef.current = "";
    setRecoveryCodes(null);
    setEnrollment(null);
    setCode("");
    setProtectedState(true);
    onEnabled();
  };

  return (
    <div className="mfa-security-panel">
      <div className="panel-heading"><div><span>Account protection</span><h2>Multi-factor authentication</h2></div><ShieldCheck aria-hidden="true" size={20} /></div>
      {protectedState ? (
        <div className="mfa-enabled-state"><span><Check aria-hidden="true" size={18} /></span><div><strong>Authenticator enabled</strong><p>Your account has a confirmed TOTP factor and is eligible for super-admin promotion.</p></div></div>
      ) : recoveryCodes ? (
        <div className="mfa-recovery-state">
          <span className="security-kicker"><ShieldCheck aria-hidden="true" size={15} />Setup complete</span>
          <h3>Save your recovery codes</h3>
          <p>Each code works once. Store them separately from your authenticator before leaving this screen.</p>
          <div aria-label="Recovery codes" className="recovery-codes">{recoveryCodes.map((recoveryCode) => <code key={recoveryCode}>{recoveryCode}</code>)}</div>
          <CopyButton label="Copy all recovery codes" value={recoveryCodes.join("\n")} />
          <button className="primary-button" onClick={acknowledge} type="button">I saved my recovery codes</button>
        </div>
      ) : enrollment ? (
        <form className="mfa-setup-form" onSubmit={confirm}>
          <p>Scan this code with a TOTP-compatible authenticator, then enter the current code.</p>
          <MfaEnrollmentSecret provisioningUri={enrollment.provisioning_uri} secret={enrollment.secret} />
          <label htmlFor="security-mfa-code">Authentication code</label>
          <input autoComplete="one-time-code" className="code-field" id="security-mfa-code" inputMode="numeric" maxLength={8} onChange={(event) => setCode(event.target.value.replace(/\D/g, ""))} pattern="[0-9]{6,8}" required value={code} />
          {error ? <p className="inline-error" role="alert">{error}</p> : null}
          <div className="mfa-security-actions"><button className="secondary-button" disabled={busy} onClick={cancel} type="button">Cancel setup</button><button className="primary-button" disabled={busy || !/^\d{6,8}$/.test(code)} type="submit">{busy ? <LoaderCircle aria-hidden="true" className="spin" size={16} /> : null}Confirm MFA</button></div>
        </form>
      ) : (
        <form className="mfa-start-form" onSubmit={start}>
          <p>Set up an authenticator before this account needs elevated access. Your current password verifies that the request is yours.</p>
          <label htmlFor="security-current-password">Current password</label>
          <input autoComplete="current-password" id="security-current-password" name="current_password" required type="password" />
          {error ? <p className="inline-error" role="alert">{error}</p> : null}
          <button className="primary-button" disabled={busy} type="submit">{busy ? <LoaderCircle aria-hidden="true" className="spin" size={16} /> : <ShieldCheck aria-hidden="true" size={16} />}Start MFA setup</button>
        </form>
      )}
    </div>
  );
}
