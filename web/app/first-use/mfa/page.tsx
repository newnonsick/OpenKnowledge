import { Boxes, ShieldCheck, Smartphone } from "lucide-react";

import { MfaEnrollmentForm } from "@/components/auth/mfa-enrollment-form";

export default function MfaEnrollmentPage() {
  return (
    <main className="first-use-page">
      <div className="first-use-brand"><span><Boxes aria-hidden="true" size={18} /></span>OpenKnowledge</div>
      <section className="first-use-layout">
        <div className="first-use-context">
          <span className="first-use-kicker"><ShieldCheck aria-hidden="true" size={15} />Super admin protection</span>
          <h2>A second proof.<br /><span>A much safer home.</span></h2>
          <p>Administrative accounts require a rotating authentication code in addition to the password.</p>
          <div className="first-use-assurance"><Smartphone aria-hidden="true" size={18} /><span><strong>Works offline</strong><small>Your authenticator generates codes on your device.</small></span></div>
        </div>
        <MfaEnrollmentForm />
      </section>
    </main>
  );
}
