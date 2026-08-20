import type { Metadata } from "next";
import { connection } from "next/server";
import type { ReactNode } from "react";

import "./globals.css";

export const metadata: Metadata = {
  title: "Kinbase — Family knowledge gateway",
  description: "A private, shared knowledge console for your family.",
};

export default async function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  await connection();
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
