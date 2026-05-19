"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { Loader2, RefreshCw } from "lucide-react";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { StatusBadge } from "@/components/status-badge";
import { api, APIError } from "@/lib/api";
import { formatRelative, truncate } from "@/lib/format";
import type { DocStatus, DocumentListItem } from "@/lib/types";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";

const ACTIVE_STATUSES: DocStatus[] = ["queued", "routing", "extracting", "enriching"];
const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "all", label: "All statuses" },
  { value: "queued", label: "Queued" },
  { value: "routing", label: "Routing" },
  { value: "extracting", label: "Extracting" },
  { value: "enriching", label: "Enriching" },
  { value: "indexed", label: "Indexed" },
  { value: "failed", label: "Failed" },
  { value: "duplicate", label: "Duplicate" },
];

export function DocumentTable({
  refreshKey,
}: {
  refreshKey?: number;
}) {
  const [items, setItems] = useState<DocumentListItem[]>([]);
  const [status, setStatus] = useState<string>("all");
  const [loaded, setLoaded] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (statusFilter: string) => {
    try {
      const data = await api.listDocuments(
        statusFilter === "all" ? {} : { status: statusFilter }
      );
      setItems(data);
      setError(null);
    } catch (e) {
      setError(e instanceof APIError ? e.detail || e.message : String(e));
    } finally {
      setLoaded(true);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    // setState happens only after an await — safe; rule can't see across functions
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load(status);
  }, [status, refreshKey, load]);

  // Auto-refresh while there are active jobs in the result set
  useEffect(() => {
    const hasActive = items.some((d) =>
      (ACTIVE_STATUSES as string[]).includes(d.status)
    );
    if (!hasActive) return;
    const t = setInterval(() => load(status), 3000);
    return () => clearInterval(t);
  }, [items, status, load]);

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <Select value={status} onValueChange={(v) => setStatus(v ?? "all")}>
          <SelectTrigger className="w-48">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {STATUS_OPTIONS.map((opt) => (
              <SelectItem key={opt.value} value={opt.value}>
                {opt.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            setRefreshing(true);
            load(status);
          }}
          disabled={refreshing}
        >
          <RefreshCw className={refreshing ? "animate-spin" : ""} />
          Refresh
        </Button>
      </div>

      {error && (
        <Alert variant="destructive">
          <AlertTitle>Failed to load documents</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Filename</TableHead>
              <TableHead>MIME</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Created</TableHead>
              <TableHead className="text-right w-20">ID</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {!loaded && items.length === 0 ? (
              <TableRow>
                <TableCell colSpan={5} className="text-center text-muted-foreground py-12">
                  <Loader2 className="h-5 w-5 animate-spin inline-block mr-2" />
                  Loading…
                </TableCell>
              </TableRow>
            ) : items.length === 0 ? (
              <TableRow>
                <TableCell colSpan={5} className="text-center text-muted-foreground py-12">
                  No documents yet — upload one above.
                </TableCell>
              </TableRow>
            ) : (
              items.map((doc) => (
                <TableRow key={doc.document_id} className="cursor-pointer">
                  <TableCell>
                    <Link
                      href={`/documents/${doc.document_id}`}
                      className="font-medium hover:underline"
                    >
                      {truncate(doc.filename, 60)}
                    </Link>
                  </TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">
                    {doc.mime_type}
                  </TableCell>
                  <TableCell>
                    <StatusBadge status={doc.status} />
                  </TableCell>
                  <TableCell className="text-muted-foreground text-sm">
                    {formatRelative(doc.created_at)}
                  </TableCell>
                  <TableCell className="text-right font-mono text-xs text-muted-foreground">
                    {doc.document_id.slice(0, 8)}
                  </TableCell>
                </TableRow>
              ))
            )}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
