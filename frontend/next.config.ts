import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Produces .next/standalone -- a minimal server.js plus only the
  // node_modules actually traced as used, instead of the full
  // node_modules tree. That's what the desktop packaging build bundles
  // (see packaging/README.md); dev mode (`next dev`) is unaffected.
  output: "standalone",
};

export default nextConfig;
