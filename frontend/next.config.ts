import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Produces .next/standalone -- a minimal server.js plus only the
  // node_modules actually traced as used, instead of the full
  // node_modules tree. That's what the desktop packaging build bundles
  // (see packaging/README.md); dev mode (`next dev`) is unaffected.
  // Opt-in only (DESKTOP_BUILD=1) -- platforms that build and host Next.js
  // themselves (Vercel, InsForge's deployments) expect their own default
  // output shape and fail with a stray ENOENT if "standalone" is forced on
  // every build.
  ...(process.env.DESKTOP_BUILD === "1" ? { output: "standalone" as const } : {}),
};

export default nextConfig;
