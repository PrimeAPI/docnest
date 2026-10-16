import { useMutation, useQuery } from "@tanstack/react-query";
import { Download, FileText, Lock } from "lucide-react";
import { useEffect, useState } from "react";
import { useParams, useSearchParams } from "react-router";
import { ApiError, call, client, ensureCsrf } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { ErrorNote, Spinner } from "@/components/ui/misc";
import { PdfViewer } from "@/features/documents/pdf-viewer";
import { cn, formatDate } from "@/lib/utils";

export function SharedDocumentPage() {
  const { shareId = "" } = useParams();
  return <SharedDocument key={shareId} id={shareId} />;
}

function SharedDocument({ id }: { id: string }) {
  const [search] = useSearchParams();
  const [passphrase, setPassphrase] = useState("");
  const [variant, setVariant] = useState<"archive" | "original">("archive");
  const [showText, setShowText] = useState(false);
  const doc = useQuery({
    queryKey: ["shared-document", id],
    queryFn: () => call(() => client.GET("/api/v1/shares/{share_id}", { params: { path: { share_id: id } } })),
    retry: false,
  });
  const unlock = useMutation({
    mutationFn: async () => {
      await ensureCsrf();
      return call(() => client.POST("/api/v1/shares/{share_id}/unlock", {
        params: { path: { share_id: id } }, body: { passphrase },
      }));
    },
    onSuccess: () => { setPassphrase(""); void doc.refetch(); },
  });
  const text = useQuery({
    queryKey: ["shared-document-text", id],
    queryFn: () => call(() => client.GET("/api/v1/shares/{share_id}/text", { params: { path: { share_id: id } } })),
    enabled: showText && !!doc.data?.allow_text && !doc.error,
    retry: false,
  });
  const base = `/api/v1/shares/${encodeURIComponent(id)}/file`;
  const requestedPdf = search.get("pdf");
  const redirectToPdf = !doc.error && !!doc.data && (requestedPdf === "archive" || (requestedPdf === "original" && doc.data.allow_original));
  useEffect(() => {
    if (redirectToPdf) {
      window.location.replace(`${base}?variant=${requestedPdf}&download=${search.get("download") === "true"}`);
    }
  }, [redirectToPdf, base, requestedPdf, search]);

  const locked = doc.error instanceof ApiError && doc.error.status === 403;
  if (doc.isPending || redirectToPdf) return <div className="flex min-h-screen items-center justify-center"><Spinner className="size-6" /></div>;
  if (locked) return (
    <main className="flex min-h-screen items-center justify-center bg-muted/30 p-6">
      <form className="flex w-full max-w-sm flex-col gap-4 rounded-xl border bg-card p-6 shadow-sm"
        onSubmit={(e) => { e.preventDefault(); unlock.mutate(); }}>
        <Lock className="size-6 text-muted-foreground" />
        <h1 className="text-xl font-semibold">Shared document</h1>
        <p className="text-sm text-muted-foreground">Enter the passphrase to view and download this document.</p>
        <Label htmlFor="passphrase">Passphrase</Label>
        <Input id="passphrase" type="password" autoComplete="current-password" autoFocus required maxLength={1024}
          value={passphrase} onChange={(e) => setPassphrase(e.target.value)} />
        <ErrorNote error={unlock.error} />
        <Button type="submit" disabled={unlock.isPending}>{unlock.isPending && <Spinner />} Open document</Button>
      </form>
    </main>
  );
  if (doc.error || !doc.data) return (
    <main className="mx-auto max-w-lg p-8">
      <h1 className="mb-4 text-xl font-semibold">Shared document unavailable</h1>
      <ErrorNote error={doc.error ?? "This link is no longer available."} />
    </main>
  );
  const d = doc.data;
  return (
    <main className="flex h-dvh flex-col bg-background">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b p-3 md:px-6">
        <div className="min-w-0">
          <p className="text-xs text-muted-foreground">DocNest · Shared document · Read only</p>
          <h1 className="break-words font-semibold">{d.title}</h1>
          <p className="text-xs text-muted-foreground">{d.page_count} page{d.page_count === 1 ? "" : "s"}{d.document_date && ` · ${formatDate(d.document_date)}`}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {d.allow_text && <Button variant="outline" size="sm" aria-pressed={showText} onClick={() => setShowText(!showText)}><FileText /> OCR text</Button>}
          <Button variant="outline" size="sm" asChild><a href={`${base}?variant=${variant}&download=true`}><Download /> Download {variant === "archive" ? "enhanced PDF" : "original"}</a></Button>
        </div>
      </header>
      <div className="flex items-center gap-2 border-b px-3 py-2 text-sm">
        <Button size="sm" variant={variant === "archive" ? "secondary" : "ghost"} aria-pressed={variant === "archive"} onClick={() => setVariant("archive")}>Enhanced</Button>
        {d.allow_original && <Button size="sm" variant={variant === "original" ? "secondary" : "ghost"} aria-pressed={variant === "original"} onClick={() => setVariant("original")}>Original</Button>}
      </div>
      <div className={cn("grid min-h-0 flex-1", showText && "grid-rows-2 lg:grid-cols-[1fr_24rem] lg:grid-rows-1")}>
        <div className="min-h-0 min-w-0"><PdfViewer url={`${base}?variant=${variant}`} /></div>
        {showText && <aside className="overflow-auto border-t p-4 lg:border-l lg:border-t-0">
          <h2 className="mb-3 text-sm font-semibold">OCR text</h2>
          {text.isPending && <Spinner />}
          <ErrorNote error={text.error} />
          {text.data && <pre className="whitespace-pre-wrap break-words font-mono text-xs leading-5">{text.data.text || "No text was recognized."}</pre>}
        </aside>}
      </div>
    </main>
  );
}
