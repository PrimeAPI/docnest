import { Download, Sparkles } from "lucide-react";
import { useEffect, useState } from "react";
import { cn } from "@/lib/utils";
import { PdfViewer } from "./pdf-viewer";

type Variant = "archive" | "original";

/** The PDF viewer with a switch between the enhanced version (default) and the untouched original. */
export function DocumentViewer({ id, enhanced }: { id: string; enhanced?: boolean }) {
  const [variant, setVariant] = useState<Variant>("archive");
  useEffect(() => setVariant("archive"), [id]);

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b bg-card px-2 py-1 text-xs">
        <div className="inline-flex rounded-md border p-0.5" role="radiogroup" aria-label="Version">
          {(
            [
              ["archive", "Enhanced"],
              ["original", "Original"],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              role="radio"
              aria-checked={variant === value}
              onClick={() => setVariant(value)}
              className={cn(
                "cursor-pointer rounded px-2.5 py-0.5 font-medium transition-colors",
                variant === value ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted",
              )}
            >
              {value === "archive" && <Sparkles className="mr-1 inline size-3" />}
              {label}
            </button>
          ))}
        </div>
        <span className="truncate text-muted-foreground">
          {variant === "original"
            ? "The file exactly as it was scanned or uploaded"
            : enhanced === false
              ? "Nothing needed enhancing: same as the original"
              : "Straightened, cropped and cleaned up"}
        </span>
        <a
          className="ml-auto inline-flex items-center gap-1 rounded px-2 py-0.5 text-muted-foreground hover:bg-muted"
          href={`/api/v1/documents/${id}/file?variant=${variant}&download=true`}
          title={variant === "original" ? "Download the original" : "Download the enhanced version"}
        >
          <Download className="size-3" /> {variant === "original" ? "Original" : "Enhanced"}
        </a>
      </div>
      <div className="min-h-0 flex-1">
        <PdfViewer key={variant} url={`/api/v1/documents/${id}/file?variant=${variant}`} />
      </div>
    </div>
  );
}
