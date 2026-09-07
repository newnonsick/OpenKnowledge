"use client";

import { FormEvent, useState } from "react";
import { KeyRound, LoaderCircle, ShieldCheck } from "lucide-react";

import { NewPasswordFields } from "@/components/auth/new-password-fields";
import { ApiError, contractClient, contractDataWithSessionRetry } from "@/lib/api-client";
import { isPasswordValid } from "@/lib/password-policy";

type PasswordSecurityPanelProps = {
  systemRole: "member" | "super_admin";
};

export function PasswordSecurityPanel({ systemRole }: PasswordSecurityPanelProps) {
  const [currentPassword, setCurrentPassword] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [factor, setFactor] = useState<"totp" | "recovery">("totp");
  const [factorValue, setFactorValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [changed, setChanged] = useState(false);
  const factorValid = systemRole === "member" || (
    factor === "totp" ? /^\d{6,8}$/.test(factorValue) : factorValue.trim().length > 0
  );
  const canSubmit = currentPassword.length > 0
    && isPasswordValid(password, confirmation)
    && factorValid
    && !busy;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit) {
      return;
    }
    setBusy(true);
    setError(null);
    setChanged(false);
    try {
      await contractDataWithSessionRetry(() => contractClient.POST("/api/v1/auth/password", {
        body: {
          confirmation,
          current_password: currentPassword,
          password,
          ...(systemRole === "super_admin" && factor === "totp" ? { current_totp_code: factorValue } : {}),
          ...(systemRole === "super_admin" && factor === "recovery" ? { recovery_code: factorValue.trim() } : {}),
        },
      }));
      setCurrentPassword("");
      setPassword("");
      setConfirmation("");
      setFactorValue("");
      setChanged(true);
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : "The password could not be changed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="password-security-panel">
      <div className="panel-heading"><div><span>Account credentials</span><h2>Password</h2></div><KeyRound aria-hidden="true" size={20} /></div>
      <form className="password-security-form" onSubmit={submit}>
        <p>Changing your password signs out other website sessions. This device stays signed in and personal API keys stay active.</p>
        <label htmlFor="security-password-current">Current password</label>
        <input
          autoComplete="current-password"
          disabled={busy}
          id="security-password-current"
          maxLength={128}
          onChange={(event) => setCurrentPassword(event.target.value)}
          required
          type="password"
          value={currentPassword}
        />
        <NewPasswordFields
          confirmation={confirmation}
          disabled={busy}
          idPrefix="security-password"
          onConfirmationChange={setConfirmation}
          onPasswordChange={setPassword}
          password={password}
        />
        {systemRole === "super_admin" ? (
          <>
            <div className="step-up-factor-tabs" role="group" aria-label="Authentication factor">
              <button aria-pressed={factor === "totp"} disabled={busy} onClick={() => { setFactor("totp"); setFactorValue(""); }} type="button"><ShieldCheck aria-hidden="true" size={14} />Authenticator</button>
              <button aria-pressed={factor === "recovery"} disabled={busy} onClick={() => { setFactor("recovery"); setFactorValue(""); }} type="button"><KeyRound aria-hidden="true" size={14} />Recovery code</button>
            </div>
            <label htmlFor={factor === "totp" ? "security-password-totp" : "security-password-recovery"}>{factor === "totp" ? "Authentication code" : "Recovery code"}</label>
            <input
              autoCapitalize={factor === "recovery" ? "none" : undefined}
              autoComplete="one-time-code"
              className="text-field code-field"
              disabled={busy}
              id={factor === "totp" ? "security-password-totp" : "security-password-recovery"}
              inputMode={factor === "totp" ? "numeric" : undefined}
              maxLength={factor === "totp" ? 8 : 64}
              onChange={(event) => setFactorValue(factor === "totp" ? event.target.value.replace(/\D/g, "") : event.target.value)}
              pattern={factor === "totp" ? "[0-9]{6,8}" : undefined}
              required
              value={factorValue}
            />
          </>
        ) : null}
        {error ? <p className="inline-error" role="alert">{error}</p> : null}
        {changed ? <p className="inline-success" role="status">Password changed. Other website sessions were signed out; this device remains signed in.</p> : null}
        <button className="primary-button" disabled={!canSubmit} type="submit">
          {busy ? <LoaderCircle aria-hidden="true" className="spin" size={16} /> : <KeyRound aria-hidden="true" size={16} />}
          {busy ? "Changing…" : "Change password"}
        </button>
      </form>
    </div>
  );
}
