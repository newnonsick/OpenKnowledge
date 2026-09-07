"use client";

import { KeySquare } from "lucide-react";
import { QRCodeSVG } from "qrcode.react";

import { CopyButton } from "@/components/copy-button";

export function MfaEnrollmentSecret({ provisioningUri, secret }: { provisioningUri: string; secret: string }) {
  return (
    <div className="enrollment-provisioning">
      <div className="enrollment-qr">
        <QRCodeSVG marginSize={4} role="img" size={148} title="Authenticator setup QR code" value={provisioningUri} />
      </div>
      <div className="enrollment-secret">
        <span><KeySquare aria-hidden="true" size={16} />Manual setup key</span>
        <span className="enrollment-secret-value"><code aria-label="Manual setup key">{secret}</code><CopyButton label="Copy manual setup key" value={secret} /></span>
      </div>
    </div>
  );
}
