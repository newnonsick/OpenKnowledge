export const passwordPolicy = {
  maxLength: 128,
  minLength: 15,
} as const;

export type PasswordRuleState = {
  label: string;
  valid: boolean;
};

export function passwordRuleStates(
  password: string,
  confirmation: string,
): PasswordRuleState[] {
  return [
    { label: "At least 15 characters", valid: password.length >= passwordPolicy.minLength },
    { label: "At most 128 characters", valid: password.length <= passwordPolicy.maxLength },
    { label: "A lowercase letter", valid: /\p{Ll}/u.test(password) },
    { label: "An uppercase letter", valid: /\p{Lu}/u.test(password) },
    { label: "A number", valid: /\p{Nd}/u.test(password) },
    { label: "A special character", valid: /[!-/:-@[-`{-~]/.test(password) },
    { label: "Passwords match", valid: password.length > 0 && password === confirmation },
  ];
}

export function isPasswordValid(
  password: string,
  confirmation: string,
): boolean {
  return passwordRuleStates(password, confirmation).every((rule) => rule.valid);
}
