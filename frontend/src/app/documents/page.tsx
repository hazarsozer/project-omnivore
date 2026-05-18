"use client";

import { useState } from "react";
import { UploadZone } from "@/components/upload-zone";
import { DocumentTable } from "@/components/document-table";

export default function DocumentsPage() {
  const [refreshKey, setRefreshKey] = useState(0);

  return (
    <div className="p-8 max-w-6xl mx-auto space-y-8">
      <header>
        <h1 className="text-2xl font-semibold">Documents</h1>
        <p className="text-muted-foreground text-sm mt-1">
          Upload files to extract chunks, tables, entities, and embeddings.
        </p>
      </header>

      <section>
        <UploadZone onUploaded={() => setRefreshKey((k) => k + 1)} />
      </section>

      <section>
        <h2 className="text-sm font-medium text-muted-foreground uppercase tracking-wide mb-3">
          Recent uploads
        </h2>
        <DocumentTable refreshKey={refreshKey} />
      </section>
    </div>
  );
}
