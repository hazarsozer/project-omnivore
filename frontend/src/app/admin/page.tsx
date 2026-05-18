"use client";

import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, XCircle, RefreshCw, Loader2 } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { api, APIError, API_BASE } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { Handler, Readiness } from "@/lib/types";

export default function AdminPage() {
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [readyError, setReadyError] = useState<string | null>(null);
  const [handlers, setHandlers] = useState<Handler[] | null>(null);
  const [handlersError, setHandlersError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const [r, h] = await Promise.allSettled([api.ready(), api.handlers()]);
    if (r.status === "fulfilled") {
      setReadiness(r.value);
      setReadyError(null);
    } else {
      setReadyError(
        r.reason instanceof APIError
          ? r.reason.detail || r.reason.message
          : String(r.reason)
      );
      setReadiness(null);
    }
    if (h.status === "fulfilled") {
      setHandlers(h.value);
      setHandlersError(null);
    } else {
      setHandlersError(
        h.reason instanceof APIError
          ? h.reason.detail || h.reason.message
          : String(h.reason)
      );
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    // setState happens only after an await — safe; rule can't see across functions
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load();
  }, [load]);

  return (
    <div className="p-8 max-w-5xl mx-auto space-y-6">
      <header className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Admin</h1>
          <p className="text-muted-foreground text-sm mt-1">
            Pipeline health and registered handlers — API at{" "}
            <code className="font-mono text-xs">{API_BASE}</code>
          </p>
        </div>
        <Button variant="outline" size="sm" onClick={load} disabled={loading}>
          <RefreshCw className={loading ? "animate-spin" : ""} />
          Refresh
        </Button>
      </header>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Readiness</CardTitle>
        </CardHeader>
        <CardContent>
          {readyError ? (
            <Alert variant="destructive">
              <AlertTitle>API unreachable</AlertTitle>
              <AlertDescription>{readyError}</AlertDescription>
            </Alert>
          ) : !readiness ? (
            <div className="text-muted-foreground text-sm">
              <Loader2 className="h-4 w-4 animate-spin inline mr-2" />
              Checking…
            </div>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              {Object.entries(readiness).map(([k, v]) => (
                <HealthChip key={k} name={k} value={v} />
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            Registered handlers{" "}
            {handlers && (
              <span className="text-muted-foreground font-normal">
                ({handlers.length})
              </span>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent>
          {handlersError ? (
            <Alert variant="destructive">
              <AlertTitle>Failed to load handlers</AlertTitle>
              <AlertDescription>{handlersError}</AlertDescription>
            </Alert>
          ) : !handlers ? (
            <div className="text-muted-foreground text-sm">
              <Loader2 className="h-4 w-4 animate-spin inline mr-2" />
              Loading…
            </div>
          ) : (
            <div className="rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Name</TableHead>
                    <TableHead>Version</TableHead>
                    <TableHead>Cost class</TableHead>
                    <TableHead>Accepts</TableHead>
                    <TableHead className="text-right">Timeout</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {handlers.map((h) => (
                    <TableRow key={h.name}>
                      <TableCell className="font-medium">{h.name}</TableCell>
                      <TableCell className="font-mono text-xs text-muted-foreground">
                        {h.version}
                      </TableCell>
                      <TableCell>
                        <CostBadge cost={h.cost_class} />
                      </TableCell>
                      <TableCell className="font-mono text-xs">
                        <div className="flex flex-wrap gap-1">
                          {h.accepts.slice(0, 3).map((a, i) => (
                            <Badge
                              key={i}
                              variant="outline"
                              className="font-mono text-xs font-normal"
                            >
                              {a}
                            </Badge>
                          ))}
                          {h.accepts.length > 3 && (
                            <span className="text-muted-foreground text-xs">
                              +{h.accepts.length - 3}
                            </span>
                          )}
                        </div>
                      </TableCell>
                      <TableCell className="text-right text-muted-foreground text-sm">
                        {h.timeout_seconds}s
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function HealthChip({ name, value }: { name: string; value: string }) {
  const ok = value === "ok";
  return (
    <div
      className={cn(
        "flex items-center justify-between rounded-md border p-3",
        ok
          ? "border-emerald-200 bg-emerald-50 dark:border-emerald-900 dark:bg-emerald-950/30"
          : "border-rose-200 bg-rose-50 dark:border-rose-900 dark:bg-rose-950/30"
      )}
    >
      <span className="font-medium capitalize">{name}</span>
      <span
        className={cn(
          "flex items-center gap-1.5 text-sm",
          ok
            ? "text-emerald-700 dark:text-emerald-300"
            : "text-rose-700 dark:text-rose-300"
        )}
      >
        {ok ? <CheckCircle2 className="h-4 w-4" /> : <XCircle className="h-4 w-4" />}
        {value}
      </span>
    </div>
  );
}

function CostBadge({ cost }: { cost: Handler["cost_class"] }) {
  const colors: Record<Handler["cost_class"], string> = {
    io: "bg-slate-100 text-slate-700 dark:bg-slate-900 dark:text-slate-300",
    cpu: "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300",
    gpu: "bg-violet-100 text-violet-700 dark:bg-violet-950 dark:text-violet-300",
  };
  return (
    <Badge variant="outline" className={cn("font-mono text-xs", colors[cost])}>
      {cost}
    </Badge>
  );
}
