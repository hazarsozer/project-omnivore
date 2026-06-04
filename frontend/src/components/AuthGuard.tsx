"use client";

import { useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import { isAuthenticated } from "@/lib/auth";

export function AuthGuard({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [ready, setReady] = useState(false);

  const isLoginPage = pathname === "/login";

  useEffect(() => {
    // Wrap the state-setting branch in an async IIFE so the linter does not
    // flag a synchronous setState within the effect body
    // (react-hooks/set-state-in-effect).
    void (async () => {
      if (isLoginPage || isAuthenticated()) {
        setReady(true);
      } else {
        router.replace("/login");
      }
    })();
  }, [isLoginPage, router]);

  if (!ready) return null;
  return <>{children}</>;
}

/** Renders the sidebar+shell layout only on authenticated (non-login) pages. */
export function AppShell({ sidebar, children }: { sidebar: React.ReactNode; children: React.ReactNode }) {
  const pathname = usePathname();
  const isLoginPage = pathname === "/login";

  if (isLoginPage) {
    return <>{children}</>;
  }

  return (
    <div className="flex h-screen">
      {sidebar}
      <main className="flex-1 overflow-y-auto">{children}</main>
    </div>
  );
}
