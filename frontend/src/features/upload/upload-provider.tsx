import { FileUp, Upload } from "lucide-react";
import { createContext, type ReactNode, useCallback, useContext, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { ensureCsrf } from "@/api/client";
import { useBuckets, useInvalidateDocuments } from "@/api/queries";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label, Select } from "@/components/ui/input";
import { cn } from "@/lib/utils";

type Options = { bucketId: number | null; todo: boolean; important: boolean };

const STORAGE_KEY = "docnest-upload-options";

function loadOptions(): Options {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return { bucketId: null, todo: false, important: false, ...JSON.parse(raw) };
  } catch {
    /* storage unavailable */
  }
  return { bucketId: null, todo: false, important: false };
}

function saveOptions(options: Options) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(options));
  } catch {
    /* storage unavailable */
  }
}

function csrfToken(): string {
  const match = document.cookie.match(/(?:^|;\s*)(?:__Host-)?docnest_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

function isPdf(file: File) {
  return file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
}

type UploadContextValue = { openDialog: () => void; uploadFiles: (files: File[]) => void };
const UploadContext = createContext<UploadContextValue | null>(null);

export function useUpload() {
  const ctx = useContext(UploadContext);
  if (!ctx) throw new Error("useUpload outside UploadProvider");
  return ctx;
}

/**
 * Drag & drop PDFs anywhere in the app to upload them into the processing
 * pipeline. Files go through the same durable, encrypted intake as scanner uploads.
 */
export function UploadProvider({ children }: { children: ReactNode }) {
  const buckets = useBuckets();
  const invalidate = useInvalidateDocuments();
  const navigate = useNavigate();
  const [options, setOptionsState] = useState<Options>(loadOptions);
  const [dragging, setDragging] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);
  const dragDepth = useRef(0);
  const input = useRef<HTMLInputElement>(null);

  const setOptions = (next: Options) => {
    setOptionsState(next);
    saveOptions(next);
  };

  const bucket =
    buckets.data?.find((b) => b.id === options.bucketId) ??
    buckets.data?.find((b) => b.slug === "private") ??
    buckets.data?.[0];

  const uploadOne = useCallback(
    async (file: File) => {
      const id = toast.loading(`Uploading ${file.name}…`);
      try {
        await ensureCsrf();
        const form = new FormData();
        form.append("file", file);
        if (bucket) form.append("bucket_id", String(bucket.id));
        form.append("todo", String(options.todo));
        form.append("important", String(options.important));
        const response = await fetch("/api/v1/documents/upload", {
          method: "POST",
          body: form,
          credentials: "same-origin",
          headers: { "X-CSRFToken": csrfToken() },
        });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.detail ?? `Upload failed (${response.status})`);
        toast.success(body.duplicate ? `${file.name} is already in DocNest` : `${file.name} uploaded — processing…`, {
          id,
          action: { label: "Open", onClick: () => navigate(`/documents/${body.id}`) },
        });
      } catch (err) {
        toast.error(`${file.name}: ${(err as Error).message}`, { id });
      }
    },
    [bucket, options.todo, options.important, navigate],
  );

  const uploadFiles = useCallback(
    async (files: File[]) => {
      const pdfs = files.filter(isPdf);
      const skipped = files.length - pdfs.length;
      if (skipped) toast.error(`${skipped} file(s) skipped — only PDF documents are supported.`);
      for (const file of pdfs) await uploadOne(file);
      if (pdfs.length) invalidate();
    },
    [uploadOne, invalidate],
  );

  // Window-wide drag & drop
  useEffect(() => {
    const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer?.types ?? []).includes("Files");
    const onEnter = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      dragDepth.current += 1;
      setDragging(true);
    };
    const onOver = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    };
    const onLeave = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      dragDepth.current = Math.max(0, dragDepth.current - 1);
      if (dragDepth.current === 0) setDragging(false);
    };
    const onDrop = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      dragDepth.current = 0;
      setDragging(false);
      setDialogOpen(false);
      void uploadFiles(Array.from(e.dataTransfer?.files ?? []));
    };
    window.addEventListener("dragenter", onEnter);
    window.addEventListener("dragover", onOver);
    window.addEventListener("dragleave", onLeave);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragenter", onEnter);
      window.removeEventListener("dragover", onOver);
      window.removeEventListener("dragleave", onLeave);
      window.removeEventListener("drop", onDrop);
    };
  }, [uploadFiles]);

  return (
    <UploadContext.Provider value={{ openDialog: () => setDialogOpen(true), uploadFiles }}>
      {children}
      <input
        ref={input}
        type="file"
        accept="application/pdf,.pdf"
        multiple
        hidden
        data-testid="upload-input"
        onChange={(e) => {
          const files = Array.from(e.target.files ?? []);
          e.target.value = "";
          setDialogOpen(false);
          void uploadFiles(files);
        }}
      />
      {dragging && (
        <div className="pointer-events-none fixed inset-0 z-[60] flex items-center justify-center bg-primary/10 p-6 backdrop-blur-[2px]">
          <div className="flex flex-col items-center gap-3 rounded-xl border-2 border-dashed border-primary bg-card px-12 py-10 text-center shadow-xl">
            <FileUp className="size-10 text-primary" />
            <div className="text-lg font-semibold">Drop PDFs to upload</div>
            <div className="text-sm text-muted-foreground">
              Into <span className="font-medium text-foreground">{bucket?.name ?? "Private"}</span>
              {options.todo && " · as Todo"}
              {options.important && " · important"}
            </div>
          </div>
        </div>
      )}
      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Upload documents</DialogTitle>
            <DialogDescription>
              Drop PDFs anywhere in DocNest, or choose files. They are processed like scanned documents (OCR,
              analysis, storage).
            </DialogDescription>
          </DialogHeader>
          <button
            type="button"
            onClick={() => input.current?.click()}
            className={cn(
              "flex cursor-pointer flex-col items-center gap-2 rounded-lg border-2 border-dashed px-6 py-8 text-sm text-muted-foreground transition-colors hover:border-primary hover:bg-accent/40",
            )}
          >
            <Upload className="size-7 text-primary" />
            <span>
              <span className="font-medium text-foreground">Choose PDF files</span> or drop them here
            </span>
          </button>
          <div className="grid gap-3">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="upload-bucket">Bucket</Label>
              <Select
                id="upload-bucket"
                value={bucket?.id ?? ""}
                onChange={(e) => setOptions({ ...options, bucketId: Number(e.target.value) })}
              >
                {buckets.data?.map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.name}
                  </option>
                ))}
              </Select>
            </div>
            <label className="flex items-center gap-2 text-sm">
              <Checkbox checked={options.todo} onCheckedChange={(v) => setOptions({ ...options, todo: v === true })} />
              Mark as Todo
            </label>
            <label className="flex items-center gap-2 text-sm">
              <Checkbox
                checked={options.important}
                onCheckedChange={(v) => setOptions({ ...options, important: v === true })}
              />
              Mark as important
            </label>
            <p className="text-xs text-muted-foreground">These settings are remembered for drag & drop uploads.</p>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </UploadContext.Provider>
  );
}
