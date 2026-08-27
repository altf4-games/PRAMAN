"use client";

/** A plain client-side Blob download — this is a real deployed page, not an
 * Artifact sandbox, so a download link works normally here. */
export function DownloadJsonButton({ data, filename }: { data: unknown; filename: string }) {
  function download() {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <button
      type="button"
      onClick={download}
      className="border border-ink px-3 py-1.5 font-mono text-xs uppercase tracking-wide hover:bg-paper-raised transition-colors"
    >
      Export JSON
    </button>
  );
}
