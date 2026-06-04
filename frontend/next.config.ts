import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Pin the workspace root to this directory. Without this, Next infers the
  // parent (~/Dev) as the root because a stray lockfile exists there, emitting
  // a "inferred workspace root" warning on build/start.
  turbopack: { root: __dirname },
};

export default nextConfig;
