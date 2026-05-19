"use client";

import { useCallback, useRef, useState } from "react";
import { Upload, Loader2, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { api, APIError } from "@/lib/api";
import { formatBytes } from "@/lib/format";
import { toast } from "sonner";
import { cn } from "@/lib/utils";

interface UploadItem {
  id: string;
  file: File;
  progress: number;
  status: "uploading" | "done" | "error";
  documentId?: string;
  error?: string;
}

export function UploadZone({ onUploaded }: { onUploaded?: () => void }) {
  const [items, setItems] = useState<UploadItem[]>([]);
  const [dragOver, setDragOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const uploadFile = useCallback(
    async (file: File) => {
      const id = crypto.randomUUID();
      setItems((prev) => [
        ...prev,
        { id, file, progress: 0, status: "uploading" },
      ]);
      try {
        const result = await api.uploadDocument(file, (loaded, total) => {
          const pct = Math.round((loaded / total) * 100);
          setItems((prev) =>
            prev.map((it) => (it.id === id ? { ...it, progress: pct } : it))
          );
        });
        setItems((prev) =>
          prev.map((it) =>
            it.id === id
              ? { ...it, status: "done", progress: 100, documentId: result.document_id }
              : it
          )
        );
        if (result.status === "duplicate") {
          toast.info(`Duplicate of existing document`, { description: file.name });
        } else {
          toast.success(`Queued: ${file.name}`);
        }
        onUploaded?.();
      } catch (e) {
        const msg = e instanceof APIError ? e.detail || e.message : String(e);
        setItems((prev) =>
          prev.map((it) =>
            it.id === id ? { ...it, status: "error", error: msg } : it
          )
        );
        toast.error(`Upload failed: ${file.name}`, { description: msg });
      }
    },
    [onUploaded]
  );

  const onFiles = useCallback(
    (files: FileList | null) => {
      if (!files) return;
      Array.from(files).forEach(uploadFile);
    },
    [uploadFile]
  );

  return (
    <div className="space-y-3">
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          onFiles(e.dataTransfer.files);
        }}
        onClick={() => inputRef.current?.click()}
        className={cn(
          "rounded-lg border-2 border-dashed p-8 text-center cursor-pointer transition-colors",
          dragOver
            ? "border-primary bg-accent/50"
            : "border-muted-foreground/25 hover:border-muted-foreground/50 hover:bg-accent/20"
        )}
      >
        <Upload className="h-8 w-8 mx-auto mb-3 text-muted-foreground" />
        <p className="font-medium">Drop files to upload</p>
        <p className="text-sm text-muted-foreground mt-1">
          or click to browse — PDF, DOCX, TXT, MD, HTML, JSON, CSV, XLSX, audio, video, image
        </p>
        <input
          ref={inputRef}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            onFiles(e.target.files);
            e.target.value = "";
          }}
        />
      </div>

      {items.length > 0 && (
        <div className="space-y-2">
          {items.map((it) => (
            <div
              key={it.id}
              className="flex items-center gap-3 rounded-md border bg-card p-3 text-sm"
            >
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="truncate font-medium">{it.file.name}</span>
                  <span className="text-muted-foreground text-xs shrink-0">
                    {formatBytes(it.file.size)}
                  </span>
                </div>
                {it.status === "uploading" && (
                  <div className="mt-1.5 h-1 rounded-full bg-secondary overflow-hidden">
                    <div
                      className="h-full bg-primary transition-all"
                      style={{ width: `${it.progress}%` }}
                    />
                  </div>
                )}
                {it.status === "error" && (
                  <p className="text-xs text-rose-600 dark:text-rose-400 mt-1">
                    {it.error}
                  </p>
                )}
              </div>
              <div className="shrink-0 flex items-center gap-2">
                {it.status === "uploading" && (
                  <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
                )}
                {it.status === "done" && (
                  <span className="text-emerald-600 dark:text-emerald-400 text-xs font-medium">
                    Queued
                  </span>
                )}
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6"
                  onClick={() =>
                    setItems((prev) => prev.filter((x) => x.id !== it.id))
                  }
                >
                  <X className="h-3 w-3" />
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
