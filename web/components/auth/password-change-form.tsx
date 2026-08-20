"use client";

import { FormEvent, useState } from "react";
import { Check, Eye, EyeOff, KeyRound, LoaderCircle } from "lucide-react";
import { useRouter } from "next/navigation";

import { ApiError, apiRequest } from "@/lib/api-client";

type PasswordResponse = {
  requires_mfa_enrollment: boolean;
};

export function PasswordChangeForm() {
  const router = useRouter();
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [visible, setVisible] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const lengthValid = password.length >= 15 && password.length <= 128;
  const matchValid = password.length > 0 && password === confirmation;
  const canSubmit = lengthValid && matchValid && !submitting;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit) {
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const session = await apiRequest<PasswordResponse>("/api/v1/auth/password", {
        body: { confirmation, password },
        method: "POST",
        retryAuthentication: false,
      });
      router.replace(session.requires_mfa_enrollment ? "/first-use/mfa" : "/");
    } catch (requestError) {
      setError(requestError instanceof ApiError ? requestError.message : "The password could not be changed.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="first-use-card" onSubmit={submit}>
      <span className="first-use-step">Step 1 of 2</span>
      <span className="first-use-icon"><KeyRound aria-hidden="true" size={21} /></span>
      <h1>Make this account yours.</h1>
      <p>Replace the one-time password before accessing family knowledge.</p>

      <label className="field-label" htmlFor="new-password">New password</label>
      <div className="password-field">
        <input autoComplete="new-password" id="new-password" maxLength={128} minLength={15} onChange={(event) => setPassword(event.target.value)} required type={visible ? "text" : "password"} value={password} />
        <button aria-label={visible ? "Hide passwords" : "Show passwords"} onClick={() => setVisible((value) => !value)} type="button">
          {visible ? <EyeOff aria-hidden="true" size={18} /> : <Eye aria-hidden="true" size={18} />}
        </button>
      </div>

      <label className="field-label" htmlFor="confirm-password">Confirm password</label>
      <input autoComplete="new-password" className="text-field" id="confirm-password" maxLength={128} minLength={15} onChange={(event) => setConfirmation(event.target.value)} required type={visible ? "text" : "password"} value={confirmation} />

      <div className="password-rules">
        <span data-valid={lengthValid}><i><Check aria-hidden="true" size={12} /></i>At least 15 characters</span>
        <span data-valid={matchValid}><i><Check aria-hidden="true" size={12} /></i>Passwords match</span>
        <span data-valid="true"><i><Check aria-hidden="true" size={12} /></i>Checked against unsafe choices on save</span>
      </div>

      {error ? <div className="auth-error" role="alert">{error}</div> : null}
      <button className="auth-submit" disabled={!canSubmit} type="submit">
        {submitting ? <LoaderCircle aria-hidden="true" className="spin" size={17} /> : null}
        {submitting ? "Securing account…" : "Set new password"}
      </button>
    </form>
  );
}
