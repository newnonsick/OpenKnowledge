import { Boxes, Check, ShieldCheck, Sparkles } from "lucide-react";

import { LoginForm } from "@/components/auth/login-form";

export default function LoginPage() {
  return (
    <main className="auth-page">
      <section className="auth-story">
        <div className="auth-brand"><span><Boxes aria-hidden="true" size={20} /></span>Kinbase <small>HOME</small></div>
        <div className="auth-story-copy">
          <div className="auth-story-eyebrow"><Sparkles aria-hidden="true" size={15} /> One trusted place</div>
          <h2>Your family knowledge,<br /><span>kept beautifully close.</span></h2>
          <p>Search every space you can access while private projects stay private by default.</p>
          <div className="auth-promises">
            <span><Check aria-hidden="true" size={14} />Permission-aware search</span>
            <span><Check aria-hidden="true" size={14} />Personal API keys</span>
            <span><Check aria-hidden="true" size={14} />No social identity provider</span>
          </div>
        </div>
        <div className="auth-trust"><ShieldCheck aria-hidden="true" size={17} /><span><strong>Private by architecture</strong><small>Every request is verified against your current access.</small></span></div>
      </section>
      <section className="auth-form-side"><LoginForm /></section>
    </main>
  );
}
