import { Boxes, LockKeyhole, ShieldCheck } from "lucide-react";

import { PasswordChangeForm } from "@/components/auth/password-change-form";

export default function PasswordChangePage() {
  return (
    <main className="first-use-page">
      <div className="first-use-brand"><span><Boxes aria-hidden="true" size={18} /></span>Kinbase</div>
      <section className="first-use-layout">
        <div className="first-use-context">
          <span className="first-use-kicker"><ShieldCheck aria-hidden="true" size={15} />Required security setup</span>
          <h2>One careful minute.<br /><span>Then it is all yours.</span></h2>
          <p>Your temporary credential expires automatically and cannot be recovered after this step.</p>
          <div className="first-use-assurance"><LockKeyhole aria-hidden="true" size={18} /><span><strong>Passwords are never stored directly</strong><small>The gateway keeps only an Argon2id password hash.</small></span></div>
        </div>
        <PasswordChangeForm />
      </section>
    </main>
  );
}
