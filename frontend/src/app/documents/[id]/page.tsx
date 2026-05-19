"use client";

import { use, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  ArrowLeft,
  RefreshCw,
  RotateCcw,
  Loader2,
  AlertCircle,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { StatusBadge } from "@/components/status-badge";
import { api, APIError } from "@/lib/api";
import { formatBytes, formatDate } from "@/lib/format";
import type { DocStatus, DocumentDetail, Entity } from "@/lib/types";
import { toast } from "sonner";

const ACTIVE: DocStatus[] = ["queued", "routing", "extracting", "enriching"];

export default function DocumentDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const [doc, setDoc] = useState<DocumentDetail | null>(null);
  const [entities, setEntities] = useState<Entity[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [retrying, setRetrying] = useState(false);

  const load = useCallback(async () => {
    try {
      const d = await api.getDocument(id);
      setDoc(d);
      setError(null);
      if (d.status === "indexed" || d.status === "failed") {
        try {
          const e = await api.getEntities(id);
          setEntities(e);
        } catch {
          /* non-fatal */
        }
      }
    } catch (e) {
      setError(e instanceof APIError ? e.detail || e.message : String(e));
    } finally {
      setLoaded(true);
      setRefreshing(false);
    }
  }, [id]);

  useEffect(() => {
    // setState happens only after an await — safe; rule can't see across functions
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load();
  }, [load]);

  // Poll while the doc is mid-flight
  useEffect(() => {
    if (!doc) return;
    if (!(ACTIVE as string[]).includes(doc.status)) return;
    const t = setInterval(load, 2000);
    return () => clearInterval(t);
  }, [doc, load]);

  const onRetry = async () => {
    setRetrying(true);
    try {
      await api.retryDocument(id);
      toast.success("Re-queued for processing");
      await load();
    } catch (e) {
      const msg = e instanceof APIError ? e.detail || e.message : String(e);
      toast.error("Retry failed", { description: msg });
    } finally {
      setRetrying(false);
    }
  };

  if (!loaded && !doc) {
    return (
      <div className="p-8 flex items-center justify-center text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin mr-2" />
        Loading document…
      </div>
    );
  }

  if (error && !doc) {
    return (
      <div className="p-8 max-w-3xl mx-auto">
        <Link
          href="/documents"
          className="inline-flex items-center text-sm text-muted-foreground hover:text-foreground mb-4"
        >
          <ArrowLeft className="h-4 w-4 mr-1" /> Back
        </Link>
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>Failed to load document</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      </div>
    );
  }

  if (!doc) return null;

  const canRetry =
    doc.status === "failed" && Boolean(doc.error?.retry_payload);

  return (
    <div className="p-8 max-w-5xl mx-auto space-y-6">
      <div>
        <Link
          href="/documents"
          className="inline-flex items-center text-sm text-muted-foreground hover:text-foreground mb-4"
        >
          <ArrowLeft className="h-4 w-4 mr-1" /> Back to documents
        </Link>
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0 flex-1">
            <h1 className="text-2xl font-semibold break-all">{doc.filename}</h1>
            <div className="flex items-center gap-3 mt-2 text-sm text-muted-foreground">
              <StatusBadge status={doc.status} />
              <span className="font-mono">{doc.mime_type}</span>
              <span>{formatBytes(doc.size_bytes)}</span>
              <span>•</span>
              <span className="font-mono text-xs">{doc.document_id}</span>
            </div>
          </div>
          <div className="flex gap-2 shrink-0">
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                setRefreshing(true);
                load();
              }}
              disabled={refreshing}
            >
              <RefreshCw className={refreshing ? "animate-spin" : ""} />
              Refresh
            </Button>
            {canRetry && (
              <Button size="sm" onClick={onRetry} disabled={retrying}>
                {retrying ? (
                  <Loader2 className="animate-spin" />
                ) : (
                  <RotateCcw />
                )}
                Retry
              </Button>
            )}
          </div>
        </div>
      </div>

      {doc.error && (
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>Processing error</AlertTitle>
          <AlertDescription className="font-mono text-xs">
            {doc.error.reason ?? JSON.stringify(doc.error)}
          </AlertDescription>
        </Alert>
      )}

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Stat label="Handler" value={doc.handler ?? "—"} />
        <Stat label="Created" value={formatDate(doc.created_at)} />
        <Stat label="Indexed" value={formatDate(doc.indexed_at)} />
      </div>

      {doc.summary && (doc.summary.abstract || doc.summary.title) && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Summary</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3 text-sm">
            {doc.summary.title && (
              <p className="font-medium">{doc.summary.title}</p>
            )}
            {doc.summary.abstract && (
              <p className="text-muted-foreground leading-relaxed">
                {doc.summary.abstract}
              </p>
            )}
            {doc.summary.key_points && doc.summary.key_points.length > 0 && (
              <div>
                <p className="text-xs uppercase tracking-wide text-muted-foreground mb-1.5">
                  Key points
                </p>
                <ul className="list-disc list-inside space-y-1">
                  {doc.summary.key_points.map((kp, i) => (
                    <li key={i}>{kp}</li>
                  ))}
                </ul>
              </div>
            )}
            {doc.summary.topics && doc.summary.topics.length > 0 && (
              <div className="flex flex-wrap gap-1.5 pt-1">
                {doc.summary.topics.map((t, i) => (
                  <Badge key={i} variant="secondary">
                    {t}
                  </Badge>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              Entities{" "}
              <span className="text-muted-foreground font-normal">
                ({entities.length})
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent>
            {entities.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                {doc.status === "indexed"
                  ? "No entities extracted."
                  : "Will populate after indexing."}
              </p>
            ) : (
              <div className="space-y-3 max-h-80 overflow-y-auto">
                {groupBy(entities, (e) => e.label).map(([label, group]) => (
                  <div key={label}>
                    <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground mb-1.5">
                      {label}
                    </p>
                    <div className="flex flex-wrap gap-1.5">
                      {group.map((e, i) => (
                        <Badge key={i} variant="outline" className="font-normal">
                          {e.value}
                        </Badge>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">Routing decision</CardTitle>
          </CardHeader>
          <CardContent className="text-sm space-y-2">
            {doc.routing_decision ? (
              <>
                <div className="flex items-center justify-between">
                  <span className="text-muted-foreground">Policy</span>
                  <span className="font-mono text-xs">
                    {doc.routing_decision.policy ?? "default"}
                  </span>
                </div>
                <Separator />
                {Object.entries(doc.routing_decision.sink_counts ?? {}).length ===
                0 ? (
                  <p className="text-muted-foreground">No sinks matched.</p>
                ) : (
                  Object.entries(doc.routing_decision.sink_counts ?? {}).map(
                    ([sink, count]) => (
                      <div
                        key={sink}
                        className="flex items-center justify-between"
                      >
                        <span className="font-mono text-xs">{sink}</span>
                        <Badge variant="secondary">{count} chunk(s)</Badge>
                      </div>
                    )
                  )
                )}
              </>
            ) : (
              <p className="text-muted-foreground">
                Will populate after enrichment.
              </p>
            )}
          </CardContent>
        </Card>
      </div>

      {doc.metadata && Object.keys(doc.metadata).length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Metadata</CardTitle>
          </CardHeader>
          <CardContent>
            <pre className="text-xs font-mono bg-muted p-3 rounded-md overflow-x-auto max-h-96">
              {JSON.stringify(doc.metadata, null, 2)}
            </pre>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border bg-card p-4">
      <p className="text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </p>
      <p className="font-medium mt-1 truncate" title={value}>
        {value}
      </p>
    </div>
  );
}

function groupBy<T, K extends string>(
  arr: T[],
  key: (t: T) => K
): [K, T[]][] {
  const map = new Map<K, T[]>();
  for (const it of arr) {
    const k = key(it);
    const list = map.get(k) ?? [];
    list.push(it);
    map.set(k, list);
  }
  return Array.from(map.entries()).sort(([a], [b]) => a.localeCompare(b));
}
