import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The UI is a client app talking to the local Python API (api/main.py).
  // End-to-end tests run their own dev server in a separate folder.
  distDir: process.env.NEXT_DIST_DIR || ".next",
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  devIndicators: false,
};

export default nextConfig;
