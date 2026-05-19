import { Badge } from "@/components/ui/badge";
import type { DocStatus } from "@/lib/types";
import { cn } from "@/lib/utils";

const STYLES: Record<DocStatus, string> = {
  queued: "bg-slate-100 text-slate-700 border-slate-200 dark:bg-slate-900 dark:text-slate-300 dark:border-slate-800",
  routing: "bg-blue-100 text-blue-700 border-blue-200 dark:bg-blue-950 dark:text-blue-300 dark:border-blue-900",
  extracting: "bg-blue-100 text-blue-700 border-blue-200 dark:bg-blue-950 dark:text-blue-300 dark:border-blue-900",
  enriching: "bg-violet-100 text-violet-700 border-violet-200 dark:bg-violet-950 dark:text-violet-300 dark:border-violet-900",
  indexed: "bg-emerald-100 text-emerald-700 border-emerald-200 dark:bg-emerald-950 dark:text-emerald-300 dark:border-emerald-900",
  failed: "bg-rose-100 text-rose-700 border-rose-200 dark:bg-rose-950 dark:text-rose-300 dark:border-rose-900",
  duplicate: "bg-amber-100 text-amber-700 border-amber-200 dark:bg-amber-950 dark:text-amber-300 dark:border-amber-900",
};

export function StatusBadge({ status, className }: { status: DocStatus; className?: string }) {
  return (
    <Badge variant="outline" className={cn("font-mono text-xs", STYLES[status], className)}>
      {status}
    </Badge>
  );
}
