import type { Metadata, Viewport } from "next";
import { headers } from "next/headers";
import { connection } from "next/server";
import localFont from "next/font/local";
import type { ReactNode } from "react";

import "./globals.css";

const uiFont = localFont({
  src: "./fonts/inter-latin-var.woff2",
  variable: "--next-font-ui",
  weight: "100 900",
  display: "swap",
});

const displayFont = localFont({
  src: "./fonts/fraunces-latin-var.woff2",
  variable: "--next-font-display",
  weight: "300 700",
  display: "swap",
});

export const metadata: Metadata = {
  title: "OpenKnowledge — Family knowledge gateway",
  description: "A private, shared knowledge console for your family.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
};

const themeBootstrap = `(function(){try{var stored=localStorage.getItem("openknowledge-theme");var theme=stored==="dark"||stored==="light"?stored:window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light";document.documentElement.setAttribute("data-theme",theme);}catch(e){}})();`;

export default async function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  await connection();
  const nonce = (await headers()).get("x-nonce") ?? undefined;
  return (
    <html lang="en" className={`${uiFont.variable} ${displayFont.variable}`} data-theme="light" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeBootstrap }} nonce={nonce} />
      </head>
      <body>
        {children}
      </body>
    </html>
  );
}
