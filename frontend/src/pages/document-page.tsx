import { useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  Check,
  CircleDot,
  Download,
  FileText,
  History,
  Info,
  Layers,
  MoreHorizontal,
  RefreshCw,
  Star,
  Trash2,
  X,
} from "lucide-react";
import { type KeyboardEvent, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { call, client } from "@/api/client";
import {
  type DocumentDetail,
  useBuckets,
  useCorrespondents,
  useDocument,
  useInvalidateDocuments,
  useSeriesList,
  useTags,
  useTypes,
  useUpdateDocument,
} from "@/api/queries";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { DateInput } from "@/components/ui/date-input";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown";
import { Input, Label, Select } from "@/components/ui/input";
import { ErrorNote, Spinner } from "@/components/ui/misc";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { StatusBadge, TagChip } from "@/features/documents/document-row";
import { PdfViewer } from "@/features/documents/pdf-viewer";
import { cn, formatBytes, formatDate, formatDateTime } from "@/lib/utils";

export function DocumentPage() {
  const { id = "" } = useParams();
  const doc = useDocument(id);
  const navigate = useNavigate();

  if (doc.isPending) return <Spinner className="size-6" />;
  if (doc.error || !doc.data) return <ErrorNote error={doc.error ?? "Document not found"} />;
  const d = doc.data;
  const viewable = d.page_count > 0 || d.stored;

  return (
    <div className="-mx-4 -my-6 flex h-[calc(100vh-3.5rem)] flex-col md:-mx-8 lg:flex-row">
      <div className="flex min-h-[60vh] min-w-0 flex-1 flex-col border-b lg:border-b-0 lg:border-r">
        <div className="flex items-center gap-2 border-b px-3 py-2">
          <Button variant="ghost" size="icon-sm" onClick={() => navigate(-1)} title="Back">
            <ArrowLeft />
          </Button>
          <h1 className="truncate text-sm font-semibold">{d.title}</h1>
        </div>
        <div className="min-h-0 flex-1">
          {viewable ? (
            <PdfViewer url={`/api/v1/documents/${d.id}/file?variant=archive`} />
          ) : (
            <div className="flex h-full flex-col items-center justify-center gap-2 text-sm text-muted-foreground">
              <Spinner className="size-6" /> The document is still being processed…
            </div>
          )}
        </div>
      </div>
      <aside className="w-full shrink-0 overflow-y-auto bg-background lg:w-[400px]">
        <SidePanel doc={d} />
      </aside>
    </div>
  );
}

function SidePanel({ doc }: { doc: DocumentDetail }) {
  return (
    <div className="p-4">
      <Header doc={doc} />
      {doc.processing_state === "failed" && (
        <div className="mb-4 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">
          <strong>Processing failed:</strong> {doc.processing_error}
          <p className="mt-1 text-xs">The original file is safe. Use “Reprocess” to try again.</p>
        </div>
      )}
      {doc.series_suggestion && <SeriesSuggestion doc={doc} />}
      <Tabs defaultValue="details">
        <TabsList className="grid w-full grid-cols-3">
          <TabsTrigger value="details">
            <Info /> Details
          </TabsTrigger>
          <TabsTrigger value="text">
            <FileText /> Text
          </TabsTrigger>
          <TabsTrigger value="history">
            <History /> History
          </TabsTrigger>
        </TabsList>
        <TabsContent value="details">
          <Details doc={doc} />
        </TabsContent>
        <TabsContent value="text">
          <OcrText id={doc.id} />
        </TabsContent>
        <TabsContent value="history">
          <HistoryTab doc={doc} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function Header({ doc }: { doc: DocumentDetail }) {
  const update = useUpdateDocument(doc.id);
  const invalidate = useInvalidateDocuments();
  const navigate = useNavigate();
  const [confirmDelete, setConfirmDelete] = useState(false);

  const reprocess = async (stage: "ocr" | "analyze", backend?: "ocrmypdf" | "docling") => {
    try {
      await call(() =>
        client.POST("/api/v1/documents/{doc_id}/reprocess", {
          params: { path: { doc_id: doc.id } },
          body: { stage, backend },
        }),
      );
      toast.success("Reprocessing started");
      invalidate();
      update.reset();
    } catch (e) {
      toast.error((e as Error).message);
    }
  };

  const markUnread = async () => {
    await call(() => client.POST("/api/v1/documents/{doc_id}/unread", { params: { path: { doc_id: doc.id } } }));
    invalidate();
    toast.success("Marked as unread");
  };

  const remove = async () => {
    try {
      await call(() => client.DELETE("/api/v1/documents/{doc_id}", { params: { path: { doc_id: doc.id } } }));
      toast.success("Document deleted");
      invalidate();
      navigate("/documents", { replace: true });
    } catch (e) {
      toast.error((e as Error).message);
    }
  };

  const setStatus = (status: "new" | "todo" | "done") => update.mutate({ status });

  return (
    <div className="mb-4">
      <TitleEditor doc={doc} />
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <StatusBadge doc={doc} />
        {doc.series && (
          <Link to={`/series/${doc.series.id}`}>
            <Badge variant="outline">
              <Layers /> {doc.series.name} {doc.period_label && `· ${doc.period_label}`}
            </Badge>
          </Link>
        )}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <div className="inline-flex rounded-md border p-0.5">
          {(["new", "todo", "done"] as const).map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => setStatus(s)}
              className={cn(
                "rounded px-3 py-1 text-xs font-medium capitalize transition-colors cursor-pointer",
                doc.status === s ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted",
              )}
            >
              {s === "done" && <Check className="mr-1 inline size-3" />}
              {s}
            </button>
          ))}
        </div>
        <Button
          variant={doc.is_important ? "secondary" : "outline"}
          size="sm"
          onClick={() => update.mutate({ is_important: !doc.is_important })}
        >
          <Star className={cn(doc.is_important && "fill-amber-400 text-amber-400")} />
          Important
        </Button>
        <Button variant="outline" size="sm" asChild>
          <a href={`/api/v1/documents/${doc.id}/file?variant=archive&download=true`}>
            <Download /> Download
          </a>
        </Button>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" size="icon-sm" title="More actions">
              <MoreHorizontal />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent>
            <DropdownMenuItem asChild>
              <a href={`/api/v1/documents/${doc.id}/file?variant=original&download=true`}>
                <Download /> Download original scan
              </a>
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={markUnread}>
              <CircleDot /> Mark as unread
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => reprocess("analyze")}>
              <RefreshCw /> Re-run analysis
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => reprocess("ocr", "ocrmypdf")}>
              <RefreshCw /> Reprocess with OCRmyPDF
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => reprocess("ocr", "docling")}>
              <RefreshCw /> Reprocess with Docling
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem className="text-destructive" onSelect={() => setConfirmDelete(true)}>
              <Trash2 /> Delete document
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      <Dialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>Delete this document?</DialogTitle>
            <DialogDescription>
              The document, its text and its search data are removed, and the files are deleted from storage. This
              cannot be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmDelete(false)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={remove}>
              <Trash2 /> Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function TitleEditor({ doc }: { doc: DocumentDetail }) {
  const update = useUpdateDocument(doc.id);
  const [value, setValue] = useState(doc.title);
  useEffect(() => setValue(doc.title), [doc.title]);
  const save = () => {
    const v = value.trim();
    if (v && v !== doc.title) update.mutate({ title: v });
    else setValue(doc.title);
  };
  return (
    <div>
      <textarea
        value={value}
        rows={1}
        onChange={(e) => setValue(e.target.value)}
        onBlur={save}
        onKeyDown={(e: KeyboardEvent<HTMLTextAreaElement>) => {
          if (e.key === "Enter") {
            e.preventDefault();
            (e.target as HTMLTextAreaElement).blur();
          }
          if (e.key === "Escape") setValue(doc.title);
        }}
        className="w-full resize-none rounded-md border border-transparent bg-transparent px-1 py-0.5 text-lg font-semibold leading-snug hover:border-input focus:border-input focus:outline-none field-sizing-content"
        aria-label="Title"
      />
      <Provenance doc={doc} field="title" />
    </div>
  );
}

function Provenance({ doc, field }: { doc: DocumentDetail; field: string }) {
  const source = (doc.field_sources as Record<string, string>)[field] ?? "auto";
  if (source === "user") return null;
  return (
    <span className="ml-1 text-[10px] uppercase tracking-wide text-muted-foreground">
      {source === "scanner" ? "from scanner" : "auto-detected"}
    </span>
  );
}

function Field({ label, children, doc, field }: { label: string; children: React.ReactNode; doc: DocumentDetail; field?: string }) {
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-baseline">
        <Label className="text-xs text-muted-foreground">{label}</Label>
        {field && <Provenance doc={doc} field={field} />}
      </div>
      {children}
    </div>
  );
}

function Details({ doc }: { doc: DocumentDetail }) {
  const update = useUpdateDocument(doc.id);
  const buckets = useBuckets();
  const types = useTypes();
  const correspondents = useCorrespondents();
  const series = useSeriesList();
  const [correspondentText, setCorrespondentText] = useState(doc.correspondent?.name ?? "");
  useEffect(() => setCorrespondentText(doc.correspondent?.name ?? ""), [doc.correspondent?.name]);

  const saveCorrespondent = () => {
    const v = correspondentText.trim();
    if (v === (doc.correspondent?.name ?? "")) return;
    if (!v) return update.mutate({ clear_correspondent: true });
    const existing = correspondents.data?.find((c) => c.name.toLowerCase() === v.toLowerCase());
    update.mutate(existing ? { correspondent_id: existing.id } : { correspondent_name: v });
  };

  const extracted = doc.extracted as Record<string, unknown>;
  const references = (extracted.references ?? {}) as Record<string, string>;
  const ibans = (extracted.ibans as string[]) ?? [];
  const plates = (extracted.license_plates as string[]) ?? [];
  const hasDetectedData =
    extracted.total_amount != null || Object.keys(references).length > 0 || ibans.length > 0 || plates.length > 0;

  return (
    <div className="flex flex-col gap-4">
      <Field label="Sender" doc={doc} field="correspondent">
        <Input
          list="correspondent-options"
          value={correspondentText}
          onChange={(e) => setCorrespondentText(e.target.value)}
          onBlur={saveCorrespondent}
          onKeyDown={(e) => e.key === "Enter" && (e.target as HTMLInputElement).blur()}
          placeholder="Unknown"
        />
        <datalist id="correspondent-options">
          {correspondents.data?.map((c) => <option key={c.id} value={c.name} />)}
        </datalist>
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Document date" doc={doc} field="document_date">
          <DateInput
            value={doc.document_date ?? ""}
            onValueChange={(value) =>
              update.mutate(value ? { document_date: value } : { clear_document_date: true })
            }
          />
        </Field>
        <Field label="Type" doc={doc} field="document_type">
          <Select
            value={doc.document_type?.id ?? ""}
            onChange={(e) => update.mutate({ document_type_id: Number(e.target.value) })}
          >
            {!doc.document_type && <option value="">—</option>}
            {types.data?.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </Select>
        </Field>
      </div>
      <Field label="Bucket" doc={doc} field="bucket">
        <Select value={doc.bucket.id} onChange={(e) => update.mutate({ bucket_id: Number(e.target.value) })}>
          {buckets.data?.map((b) => (
            <option key={b.id} value={b.id}>
              {b.name}
            </option>
          ))}
        </Select>
      </Field>
      <Field label="Tags" doc={doc} field="tags">
        <TagEditor doc={doc} />
      </Field>
      <Field label="Series" doc={doc} field="series">
        <Select
          value={doc.series?.id ?? ""}
          onChange={(e) =>
            update.mutate(e.target.value ? { series_id: Number(e.target.value) } : { clear_series: true })
          }
        >
          <option value="">Not part of a series</option>
          {series.data?.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </Select>
      </Field>

      <Card className="p-3 text-sm">
        <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">Detected data</h4>
        {hasDetectedData ? (
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
            {extracted.total_amount != null ? (
              <>
                <dt className="text-muted-foreground">Amount</dt>
                <dd className="font-medium tabular-nums">{String(extracted.total_amount)}</dd>
              </>
            ) : null}
            {Object.entries(references).map(([k, v]) => (
              <ExtractedRow key={k} label={k} value={v} />
            ))}
            {ibans.map((iban) => (
              <ExtractedRow key={iban} label="IBAN" value={iban} />
            ))}
            {plates.map((plate) => (
              <ExtractedRow key={plate} label="Plate" value={plate} />
            ))}
          </dl>
        ) : (
          <p className="text-xs text-muted-foreground">
            Recognized amounts, references, IBANs and licence plates will appear here.
          </p>
        )}
      </Card>

      <Card className="p-3 text-sm">
        <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">File details</h4>
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
          <dt className="text-muted-foreground">Pages</dt>
          <dd>{doc.page_count || "—"}</dd>
          <dt className="text-muted-foreground">Size</dt>
          <dd>{formatBytes(doc.size)}</dd>
          <dt className="text-muted-foreground">Scanned</dt>
          <dd>{formatDateTime(doc.uploaded_at)}</dd>
          {doc.received_from && (
            <>
              <dt className="text-muted-foreground">Scanner</dt>
              <dd>{doc.received_from}</dd>
            </>
          )}
          <dt className="text-muted-foreground">Stored</dt>
          <dd>{doc.stored ? "In permanent storage" : "Waiting for storage"}</dd>
        </dl>
      </Card>
    </div>
  );
}

const REFERENCE_LABELS: Record<string, string> = {
  rechnungnr: "Invoice no.",
  rechnungsnr: "Invoice no.",
  rechnungsnummer: "Invoice no.",
  rechnungnummer: "Invoice no.",
  kundennummer: "Customer no.",
  kundennr: "Customer no.",
  vertragsnummer: "Contract no.",
  vertragsnr: "Contract no.",
  aktenzeichen: "File ref.",
  personalnummer: "Personnel no.",
  steuernummer: "Tax no.",
};

function ExtractedRow({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="text-muted-foreground">{REFERENCE_LABELS[label] ?? label}</dt>
      <dd className="break-all font-mono text-xs leading-5">{value}</dd>
    </>
  );
}

function TagEditor({ doc }: { doc: DocumentDetail }) {
  const update = useUpdateDocument(doc.id);
  const tags = useTags();
  const [input, setInput] = useState("");
  const current = doc.tags.map((t) => t.id);

  const add = () => {
    const name = input.trim();
    if (!name) return;
    const existing = tags.data?.find((t) => t.name.toLowerCase() === name.toLowerCase());
    update.mutate(
      existing ? { tag_ids: [...new Set([...current, existing.id])] } : { tag_ids: current, tag_names: [name] },
    );
    setInput("");
  };

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap gap-1.5">
        {doc.tags.map((t) => (
          <span key={t.id} className="inline-flex items-center gap-1">
            <TagChip name={t.name} color={t.color} />
            <button
              type="button"
              className="rounded-full text-muted-foreground hover:text-foreground cursor-pointer"
              title={`Remove ${t.name}`}
              onClick={() => update.mutate({ tag_ids: current.filter((x) => x !== t.id) })}
            >
              <X className="size-3" />
            </button>
          </span>
        ))}
        {!doc.tags.length && <span className="text-sm text-muted-foreground">No tags</span>}
      </div>
      <Input
        list="tag-options"
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            add();
          }
        }}
        onBlur={add}
        placeholder="Add tag…"
        className="h-8"
      />
      <datalist id="tag-options">
        {tags.data?.filter((t) => !current.includes(t.id)).map((t) => <option key={t.id} value={t.name} />)}
      </datalist>
    </div>
  );
}

function SeriesSuggestion({ doc }: { doc: DocumentDetail }) {
  const update = useUpdateDocument(doc.id);
  return (
    <div className="mb-4 rounded-md border bg-accent/50 p-3 text-sm">
      <div className="flex items-center gap-2 font-medium">
        <Layers className="size-4" /> Part of “{doc.series_suggestion?.name}”?
      </div>
      <p className="mt-1 text-xs text-muted-foreground">This looks like another document of an existing series.</p>
      <div className="mt-2 flex gap-2">
        <Button size="sm" onClick={() => update.mutate({ accept_series_suggestion: true })}>
          <Check /> Yes, add it
        </Button>
        <Button size="sm" variant="outline" onClick={() => update.mutate({ reject_series_suggestion: true })}>
          No
        </Button>
      </div>
    </div>
  );
}

function OcrText({ id }: { id: string }) {
  const text = useQuery({
    queryKey: ["document-text", id],
    queryFn: () => call(() => client.GET("/api/v1/documents/{doc_id}/text", { params: { path: { doc_id: id } } })),
  });
  if (text.isPending) return <Spinner />;
  if (text.error) return <ErrorNote error={text.error} />;
  const value = text.data.text;
  if (!value.trim()) return <p className="text-sm text-muted-foreground">No text was recognized.</p>;
  return (
    <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap rounded-md bg-muted p-3 font-mono text-xs leading-5">
      {value}
    </pre>
  );
}

function HistoryTab({ doc }: { doc: DocumentDetail }) {
  return (
    <div className="flex flex-col gap-2 text-sm">
      <p className="text-xs text-muted-foreground">
        Processing stage: <span className="font-medium text-foreground">{doc.processing_stage}</span> · OCR backend:{" "}
        <span className="font-medium text-foreground">{doc.ocr_backend}</span>
      </p>
      {doc.events.length === 0 && <p className="text-muted-foreground">No processing events yet.</p>}
      {doc.events.map((e, i) => (
        <div key={i} className="flex items-start gap-2 rounded-md border px-3 py-2">
          <span
            className={cn(
              "mt-1.5 size-2 shrink-0 rounded-full",
              e.outcome === "ok" ? "bg-emerald-500" : e.outcome === "warning" ? "bg-amber-500" : "bg-red-500",
            )}
          />
          <div className="min-w-0 flex-1">
            <div className="flex justify-between gap-2">
              <span className="font-medium capitalize">{e.stage}</span>
              <span className="text-xs text-muted-foreground">{formatDateTime(e.created_at)}</span>
            </div>
            <div className="text-xs text-muted-foreground">
              {e.outcome} · {(e.duration_ms / 1000).toFixed(1)} s
            </div>
            {e.message && <div className="mt-1 break-words text-xs text-destructive">{e.message}</div>}
          </div>
        </div>
      ))}
      <p className="mt-2 text-xs text-muted-foreground">Document date: {formatDate(doc.document_date)}</p>
    </div>
  );
}
