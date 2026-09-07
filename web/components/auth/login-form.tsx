"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { ArrowLeft, Eye, EyeOff, KeyRound, LoaderCircle, LockKeyhole, ShieldCheck } from "lucide-react";
import { useRouter } from "next/navigation";

import { ApiError, contractClient, contractData } from "@/lib/api-client";
import { resetCachedMember } from "@/components/auth/session-gate";

export function LoginForm() {
  const router = useRouter();
  const [step, setStep] = useState<"credentials" | "code">("credentials");
  const [factorMode, setFactorMode] = useState<"recovery" | "totp">("totp");
  const [showPassword, setShowPassword] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const codeInputRef = useRef<HTMLInputElement>(null);
  const errorRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    return () => setPassword("");
  }, []);

  useEffect(() => {
    if (step === "code") {
      codeInputRef.current?.focus();
    }
  }, [factorMode, step]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) {
      return;
    }
    setError(null);
    setNotice(null);
    setSubmitting(true);
    const fields = new FormData(event.currentTarget);
    try {
      const session = await contractData(contractClient.POST("/api/v1/auth/login", {
        body: {
          password: String(fields.get("password") || ""),
          recovery_code: factorMode === "recovery" ? String(fields.get("recovery_code") || "").trim() || null : null,
          totp_code: factorMode === "totp" ? String(fields.get("totp_code") || "").replace(/[\s-]/g, "") || null : null,
          username: String(fields.get("username") || ""),
        },
      }));
      resetCachedMember();
      setPassword("");
      if (session.requires_password_change) {
        router.replace("/first-use/password");
      } else if (session.requires_mfa_enrollment) {
        router.replace("/first-use/mfa");
      } else {
        router.replace("/");
      }
    } catch (requestError) {
      if (requestError instanceof ApiError && requestError.code === "mfa_code_required") {
        setStep("code");
        setFactorMode("totp");
        setError(null);
        setNotice("Password accepted. Enter the current code from your authenticator to finish signing in.");
      } else if (requestError instanceof ApiError && requestError.status === 429) {
        setError("Too many attempts. Please wait a moment before trying again.");
      } else if (requestError instanceof ApiError && requestError.status >= 500) {
        setError("The gateway is having trouble. Please wait a moment and try again.");
      } else if (requestError instanceof TypeError || (requestError instanceof ApiError && requestError.code === "request_failed")) {
        setError("Could not reach the gateway. Check your connection and try again.");
      } else {
        setError(step === "code"
          ? "The authentication code was not accepted. Check your authenticator or switch to a recovery code."
          : "The username or password is incorrect.");
      }
      requestAnimationFrame(() => errorRef.current?.focus());
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="auth-form" onSubmit={submit}>
      <div className="auth-form-heading">
        <span className="auth-form-icon"><LockKeyhole aria-hidden="true" size={18} /></span>
        <div><p>Private family gateway</p><h1>Welcome back</h1></div>
      </div>
      <div className="auth-step-pane" hidden={step !== "credentials"}>
        <p className="auth-form-intro">Sign in with the credentials created for you by your family administrator.</p>
        <label className="field-label" htmlFor="username">Username</label>
        <input autoCapitalize="none" autoComplete="username" className="text-field" disabled={submitting} id="username" name="username" onChange={(event) => setUsername(event.target.value)} required={step === "credentials"} value={username} />
        <label className="field-label" htmlFor="password">Password</label>
        <div className="password-field">
          <input autoComplete="current-password" disabled={submitting} id="password" name="password" onChange={(event) => setPassword(event.target.value)} required={step === "credentials"} type={showPassword ? "text" : "password"} value={password} />
          <button aria-label={showPassword ? "Hide password" : "Show password"} disabled={submitting} onClick={() => setShowPassword((value) => !value)} type="button">
            {showPassword ? <EyeOff aria-hidden="true" size={18} /> : <Eye aria-hidden="true" size={18} />}
          </button>
        </div>
      </div>
      <div className="auth-step-pane" hidden={step !== "code"}>
        <p className="auth-form-intro auth-form-intro-protected" role="status"><ShieldCheck aria-hidden="true" size={15} /> This account protects sign-in with two-factor authentication. Enter the current code to finish.</p>
        <input autoComplete="username" hidden id="login-username-persist" name="username" readOnly type="text" value={username} />
        <input autoComplete="current-password" hidden id="login-password-persist" name="password" readOnly type="password" value={password} />
        <div className="factor-tabs" role="group" aria-label="Second factor method">
          <button aria-pressed={factorMode === "totp"} disabled={submitting} onClick={() => { setFactorMode("totp"); setError(null); }} tabIndex={step === "code" ? 0 : -1} type="button"><ShieldCheck aria-hidden="true" size={15} /> Authenticator code</button>
          <button aria-pressed={factorMode === "recovery"} disabled={submitting} onClick={() => { setFactorMode("recovery"); setError(null); }} tabIndex={step === "code" ? 0 : -1} type="button"><KeyRound aria-hidden="true" size={15} /> Recovery code</button>
        </div>
        <label className="field-label" htmlFor={factorMode === "totp" ? "totp_code" : "recovery_code"}>
          {factorMode === "totp" ? "Authentication code" : "Recovery code"}
        </label>
        {factorMode === "totp" ? (
          <input autoComplete="one-time-code" className="text-field code-field" disabled={submitting} id="totp_code" inputMode="numeric" maxLength={8} name="totp_code" pattern="[0-9]{6,8}" ref={codeInputRef} required={step === "code" && factorMode === "totp"} />
        ) : (
          <input autoCapitalize="none" autoComplete="one-time-code" className="text-field code-field" disabled={submitting} id="recovery_code" maxLength={64} name="recovery_code" ref={codeInputRef} required={step === "code" && factorMode === "recovery"} />
        )}
      </div>

      {notice && !error ? <div className="auth-notice" role="status">{notice}</div> : null}
      {error ? <div className="auth-error" ref={errorRef} role="alert" tabIndex={-1}>{error}</div> : null}
      <button className="auth-submit" disabled={submitting} type="submit">
        {submitting ? <LoaderCircle aria-hidden="true" className="spin" size={17} /> : null}
        {submitting
          ? "Signing in…"
          : step === "code"
            ? "Verify and sign in"
            : "Sign in"}
      </button>
      {step === "code" ? (
        <button className="auth-back-link" disabled={submitting} onClick={() => { setStep("credentials"); setError(null); setNotice(null); }} type="button">
          <ArrowLeft aria-hidden="true" size={14} /> Back to sign in
        </button>
      ) : null}
      <p className="auth-footnote">Access is invitation-only. No social account is connected to this gateway.</p>
    </form>
  );
}
