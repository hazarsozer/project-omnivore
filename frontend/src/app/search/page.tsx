"use client";

import { useState } from "react";
import Link from "next/link";
import { Loader2, Search as SearchIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Slider } from "@/components/ui/slider";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Card, CardContent } from "@/components/ui/card";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { api, APIError } from "@/lib/api";
import type { SearchMode, SearchResult } from "@/lib/types";

const MODE_BLURB: Record<SearchMode, string> = {
  bm25: "Lexical match via Postgres tsvector (plainto_tsquery, ts_rank).",
  vector: "Semantic match via pgvector HNSW (768-dim BGE-base-en-v1.5, cosine).",
  hybrid: "BM25 + vector merged with Reciprocal Rank Fusion (k=60, pre_k=top_k×4).",
};

export default function SearchPage() {
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<SearchMode>("hybrid");
  const [topK, setTopK] = useState(10);
  const [results, setResults] = useState<SearchResult[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState<number | null>(null);

  const onSearch = async (e?: React.FormEvent) => {
    e?.preventDefault();
    if (!query.trim() || loading) return;
    setLoading(true);
    setError(null);
    setResults(null);
    const t0 = performance.now();
    try {
      const data = await api.search({ query, mode, top_k: topK });
      setResults(data);
      setElapsed(performance.now() - t0);
    } catch (err) {
      setError(err instanceof APIError ? err.detail || err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="p-8 max-w-5xl mx-auto space-y-6">
      <header>
        <h1 className="text-2xl font-semibold">Search</h1>
        <p className="text-muted-foreground text-sm mt-1">
          Query indexed chunks across all your documents.
        </p>
      </header>

      <form onSubmit={onSearch} className="space-y-4">
        <div className="flex gap-2">
          <Input
            placeholder="Ask anything… (e.g. 'document ingestion pipeline')"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="flex-1"
            autoFocus
          />
          <Button type="submit" disabled={!query.trim() || loading}>
            {loading ? <Loader2 className="animate-spin" /> : <SearchIcon />}
            Search
          </Button>
        </div>

        <div className="flex flex-wrap items-end gap-6">
          <div>
            <Label className="text-xs uppercase tracking-wide text-muted-foreground mb-2 block">
              Mode
            </Label>
            <Tabs value={mode} onValueChange={(v) => setMode(v as SearchMode)}>
              <TabsList>
                <TabsTrigger value="bm25">BM25</TabsTrigger>
                <TabsTrigger value="vector">Vector</TabsTrigger>
                <TabsTrigger value="hybrid">Hybrid (RRF)</TabsTrigger>
              </TabsList>
            </Tabs>
          </div>
          <div className="flex-1 min-w-48 max-w-xs">
            <Label className="text-xs uppercase tracking-wide text-muted-foreground mb-2 block">
              top_k = {topK}
            </Label>
            <Slider
              min={1}
              max={50}
              step={1}
              value={[topK]}
              onValueChange={(v) => setTopK(Array.isArray(v) ? v[0] : v)}
            />
          </div>
        </div>

        <p className="text-xs text-muted-foreground">{MODE_BLURB[mode]}</p>
      </form>

      {error && (
        <Alert variant="destructive">
          <AlertTitle>Search failed</AlertTitle>
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {results && (
        <section className="space-y-3">
          <div className="flex items-center justify-between text-sm text-muted-foreground">
            <span>
              {results.length} result{results.length === 1 ? "" : "s"}
            </span>
            {elapsed !== null && <span>{Math.round(elapsed)} ms</span>}
          </div>

          {results.length === 0 ? (
            <Card>
              <CardContent className="py-12 text-center text-muted-foreground">
                No matches. Try a different query or mode.
              </CardContent>
            </Card>
          ) : (
            results.map((r) => <ResultCard key={r.chunk_id} result={r} />)
          )}
        </section>
      )}
    </div>
  );
}

function ResultCard({ result }: { result: SearchResult }) {
  return (
    <Card>
      <CardContent className="p-4 space-y-2">
        <div className="flex items-center justify-between gap-3 text-xs">
          <div className="flex items-center gap-2 min-w-0 flex-wrap">
            {result.heading_path?.map((h, i) => (
              <span key={i} className="text-muted-foreground truncate">
                {i > 0 && <span className="mx-1 opacity-60">›</span>}
                {h}
              </span>
            ))}
            {(!result.heading_path || result.heading_path.length === 0) && (
              <span className="text-muted-foreground italic">no heading</span>
            )}
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <Badge variant="secondary" className="font-mono text-xs">
              {result.score.toFixed(4)}
            </Badge>
            <span className="text-muted-foreground">{result.token_count} tok</span>
          </div>
        </div>
        <p className="text-sm leading-relaxed whitespace-pre-wrap">
          {result.content}
        </p>
        <Link
          href={`/documents/${result.document_id}`}
          className="text-xs text-muted-foreground hover:text-foreground font-mono inline-block pt-1"
        >
          → {result.document_id.slice(0, 8)} · chunk {result.chunk_id.slice(0, 8)}
        </Link>
      </CardContent>
    </Card>
  );
}
