"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { FileText, Search, Settings, Database, LogOut } from "lucide-react";
import { cn } from "@/lib/utils";
import { clearAuth } from "@/lib/auth";

const ITEMS = [
  { href: "/documents", label: "Documents", icon: FileText },
  { href: "/search", label: "Search", icon: Search },
  { href: "/admin", label: "Admin", icon: Settings },
];

export function SidebarNav() {
  const pathname = usePathname();
  const router = useRouter();

  const handleSignOut = () => {
    clearAuth();
    router.replace("/login");
  };

  return (
    <aside className="w-60 shrink-0 border-r bg-card flex flex-col">
      <div className="flex items-center gap-2 p-4 border-b">
        <Database className="h-5 w-5 text-primary" />
        <span className="font-semibold text-lg">Omnivore</span>
      </div>
      <nav className="flex-1 p-2 space-y-1">
        {ITEMS.map(({ href, label, icon: Icon }) => {
          const active = pathname === href || pathname.startsWith(`${href}/`);
          return (
            <Link
              key={href}
              href={href}
              className={cn(
                "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                active
                  ? "bg-accent text-accent-foreground"
                  : "text-muted-foreground hover:bg-accent/50 hover:text-foreground"
              )}
            >
              <Icon className="h-4 w-4" />
              {label}
            </Link>
          );
        })}
      </nav>
      <div className="p-4 border-t space-y-3">
        <div className="text-xs text-muted-foreground">
          <div>Phase 5 complete</div>
          <div className="font-mono mt-1 opacity-60">v0.1.0</div>
        </div>
        <button
          onClick={handleSignOut}
          className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-accent/50 hover:text-foreground"
        >
          <LogOut className="h-4 w-4" />
          Sign Out
        </button>
      </div>
    </aside>
  );
}
