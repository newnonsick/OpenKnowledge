import type { NextConfig } from "next";

const gatewayUrl = new URL(process.env.GATEWAY_INTERNAL_URL || "http://127.0.0.1:8000");
if (!["http:", "https:"].includes(gatewayUrl.protocol)) {
  throw new Error("GATEWAY_INTERNAL_URL must use HTTP or HTTPS");
}

const nextConfig: NextConfig = {
  poweredByHeader: false,
  reactStrictMode: true,
  async headers() {
    const headers = [
      { key: "Cache-Control", value: "no-store" },
      { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
      { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=(), payment=(), usb=()" },
      { key: "Referrer-Policy", value: "no-referrer" },
      { key: "X-Content-Type-Options", value: "nosniff" },
      { key: "X-Frame-Options", value: "DENY" },
    ];
    if (process.env.NODE_ENV === "production") {
      headers.push({ key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains; preload" });
    }
    return [{ source: "/:path*", headers }];
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${gatewayUrl.origin}/api/:path*` }];
  },
};

export default nextConfig;
