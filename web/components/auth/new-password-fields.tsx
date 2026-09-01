"use client";

import { Check, Eye, EyeOff } from "lucide-react";
import { useState } from "react";

import { passwordPolicy, passwordRuleStates } from "@/lib/password-policy";

type NewPasswordFieldsProps = {
  confirmation: string;
  disabled?: boolean;
  idPrefix: string;
  onConfirmationChange: (value: string) => void;
  onPasswordChange: (value: string) => void;
  password: string;
};

export function NewPasswordFields({
  confirmation,
  disabled = false,
  idPrefix,
  onConfirmationChange,
  onPasswordChange,
  password,
}: NewPasswordFieldsProps) {
  const [visible, setVisible] = useState(false);
  const passwordId = `${idPrefix}-new-password`;
  const confirmationId = `${idPrefix}-confirm-password`;

  return (
    <>
      <label className="field-label" htmlFor={passwordId}>New password</label>
      <div className="password-field">
        <input
          autoComplete="new-password"
          disabled={disabled}
          id={passwordId}
          maxLength={passwordPolicy.maxLength}
          minLength={passwordPolicy.minLength}
          onChange={(event) => onPasswordChange(event.target.value)}
          required
          type={visible ? "text" : "password"}
          value={password}
        />
        <button
          aria-label={visible ? "Hide passwords" : "Show passwords"}
          disabled={disabled}
          onClick={() => setVisible((value) => !value)}
          type="button"
        >
          {visible ? <EyeOff aria-hidden="true" size={18} /> : <Eye aria-hidden="true" size={18} />}
        </button>
      </div>

      <label className="field-label" htmlFor={confirmationId}>Confirm password</label>
      <input
        autoComplete="new-password"
        className="text-field"
        disabled={disabled}
        id={confirmationId}
        maxLength={passwordPolicy.maxLength}
        minLength={passwordPolicy.minLength}
        onChange={(event) => onConfirmationChange(event.target.value)}
        required
        type={visible ? "text" : "password"}
        value={confirmation}
      />

      <div className="password-rules">
        {passwordRuleStates(password, confirmation).map((rule) => (
          <span data-valid={password.length > 0 && rule.valid} key={rule.label}>
            <i><Check aria-hidden="true" size={11} /></i>{rule.label}
          </span>
        ))}
      </div>
    </>
  );
}
