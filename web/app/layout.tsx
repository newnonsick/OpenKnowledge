import type { Metadata, Viewport } from "next";
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
  title: "Kinbase — Family knowledge gateway",
  description: "A private, shared knowledge console for your family.",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
};

const themeBootstrap = `(function(){try{var stored=localStorage.getItem("aigw-theme");var dark=stored?stored==="dark":window.matchMedia("(prefers-color-scheme: dark)").matches;if(dark){document.documentElement.dataset.theme="dark";}}catch(e){}})();`;

export default async function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  await connection();
  return (
    <html lang="en" className={`${uiFont.variable} ${displayFont.variable}`}>
      <body>
        <script dangerouslySetInnerHTML={{ __html: themeBootstrap }} />
        {children}
      </body>
    </html>
  );
}
