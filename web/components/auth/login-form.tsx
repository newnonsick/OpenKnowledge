"use client";

import { FormEvent, useState } from "react";
import { Eye, EyeOff, LoaderCircle, LockKeyhole, ShieldCheck } from "lucide-react";
import { useRouter } from "next/navigation";

import { ApiError, apiRequest } from "@/lib/api-client";

type LoginResponse = {
  access_expires_at: string;
  member_id: string;
  requires_mfa_enrollment: boolean;
  requires_password_change: boolean;
  system_role: "member" | "super_admin";
};

export function LoginForm() {
  const router = useRouter();
  const [showPassword, setShowPassword] = useState(false);
  const [showTotp, setShowTotp] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);
    const fields = new FormData(event.currentTarget);
    const totpCode = String(fields.get("totp_code") || "").trim();
    try {
      const session = await apiRequest<LoginResponse>("/api/v1/auth/login", {
        body: {
          password: String(fields.get("password") || ""),
          totp_code: totpCode || null,
          username: String(fields.get("username") || ""),
        },
        method: "POST",
        retryAuthentication: false,
      });
      if (session.requires_password_change) {
        router.replace("/first-use/password");
      } else if (session.requires_mfa_enrollment) {
        router.replace("/first-use/mfa");
      } else {
        router.replace("/");
      }
    } catch (requestError) {
      if (requestError instanceof ApiError && requestError.status === 429) {
        setError("Too many attempts. Please wait a moment before trying again.");
      } else {
        setError("The username, password, or authentication code is incorrect.");
      }
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
      <p className="auth-form-intro">Sign in with the credentials created for you by your family administrator.</p>

      <label className="field-label" htmlFor="username">Username</label>
      <input autoCapitalize="none" autoComplete="username" className="text-field" id="username" name="username" required />

      <label className="field-label" htmlFor="password">Password</label>
      <div className="password-field">
        <input autoComplete="current-password" id="password" name="password" required type={showPassword ? "text" : "password"} />
        <button aria-label={showPassword ? "Hide password" : "Show password"} onClick={() => setShowPassword((value) => !value)} type="button">
          {showPassword ? <EyeOff aria-hidden="true" size={18} /> : <Eye aria-hidden="true" size={18} />}
        </button>
      </div>

      <button className="totp-reveal" onClick={() => setShowTotp((value) => !value)} type="button">
        <ShieldCheck aria-hidden="true" size={16} />
        {showTotp ? "Hide authentication code" : "Use an authentication code"}
      </button>
      {showTotp ? (
        <div className="totp-field-wrap">
          <label className="field-label" htmlFor="totp_code">Authentication code</label>
          <input autoComplete="one-time-code" className="text-field code-field" id="totp_code" inputMode="numeric" maxLength={8} name="totp_code" pattern="[0-9]{6,8}" />
        </div>
      ) : null}

      {error ? <div className="auth-error" role="alert">{error}</div> : null}
      <button className="auth-submit" disabled={submitting} type="submit">
        {submitting ? <LoaderCircle aria-hidden="true" className="spin" size={17} /> : null}
        {submitting ? "Signing in…" : "Sign in"}
      </button>
      <p className="auth-footnote">Access is invitation-only. No social account is connected to this gateway.</p>
    </form>
  );
}
