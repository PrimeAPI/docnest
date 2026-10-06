import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  Calendar,
  Check,
  ChevronLeft,
  Copy,
  FileText,
  Inbox,
  Columns2,
  SkipForward,
  Star,
  Tag,
  Type,
  User,
  X,
} from "lucide-react";
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { toast } from "sonner";
import { call, client, type Schemas } from "@/api/client";
import {
  type DocumentDetail,
  type DocumentPatch,
  keys,
  useBuckets,
  useCorrespondents,
  useDocument,
  useInvalidateDocuments,
  useSeriesList,
  useTags,
  useTypes,
} from "@/api/queries";
import { Button } from "@/components/ui/button";
import { DateInput } from "@/components/ui/date-input";
import { Input, Label, Select } from "@/components/ui/input";
import { EmptyState, ErrorNote, Kbd, Spinner } from "@/components/ui/misc";
import { TagChip } from "@/features/documents/document-row";
import { PdfViewer } from "@/features/documents/pdf-viewer";
import { cn } from "@/lib/utils";

type Status = "new" | "todo" | "done";

type Draft = {
  title: string;
  documentDate: string;
  sender: string;
  typeId: number | null;
  bucketId: number;
  tags: string[];
  seriesId: number | null;
  status: Status;
  important: boolean;
};

function draftFrom(doc: DocumentDetail, statusOnSave: Status): Draft {
  return {
    title: doc.title,
    documentDate: doc.document_date ?? "",
    sender: doc.correspondent?.name ?? "",
    typeId: doc.document_type?.id ?? null,
    bucketId: doc.bucket.id,
    tags: doc.tags.map((t) => t.name),
    seriesId: doc.series?.id ?? null,
    status: doc.status === "new" ? statusOnSave : (doc.status as Status),
    important: doc.is_important,
  };
}

// --- Parsing selected OCR text into a date ------------------------------------------

const MONTHS: Record<string, number> = {
  jan: 1, januar: 1, january: 1, feb: 2, februar: 2, february: 2, mär: 3, maerz: 3, märz: 3, mar: 3, march: 3,
  apr: 4, april: 4, mai: 5, may: 5, jun: 6, juni: 6, june: 6, jul: 7, juli: 7, july: 7, aug: 8, august: 8,
  sep: 9, sept: 9, september: 9, okt: 10, oktober: 10, oct: 10, october: 10, nov: 11, november: 11,
  dez: 12, dezember: 12, dec: 12, december: 12,
};

function iso(y: number, m: number, d: number): string | null {
  if (y < 100) y += y < 70 ? 2000 : 1900;
  const date = new Date(Date.UTC(y, m - 1, d));
  if (date.getUTCFullYear() !== y || date.getUTCMonth() !== m - 1 || date.getUTCDate() !== d) return null;
  return `${y}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}

export function parseDate(text: string): string | null {
  const t = text.trim().toLowerCase();
  let m = t.match(/(\d{4})-(\d{1,2})-(\d{1,2})/);
  if (m) return iso(+m[1], +m[2], +m[3]);
  m = t.match(/(\d{1,2})[./](\d{1,2})[./](\d{2,4})/);
  if (m) return iso(+m[3], +m[2], +m[1]);
  m = t.match(/(\d{1,2})\.?\s+([a-zäöü]+)\.?\s+(\d{4})/);
  if (m && MONTHS[m[2]]) return iso(+m[3], MONTHS[m[2]], +m[1]);
  m = t.match(/([a-zäöü]+)\.?\s+(\d{1,2}),?\s+(\d{4})/);
  if (m && MONTHS[m[1]]) return iso(+m[3], MONTHS[m[1]], +m[2]);
  return null;
}

function clean(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

// --- Page ------------------------------------------------------------------------------

export function ReviewPage() {
  const [params] = useSearchParams();
  const qc = useQueryClient();
  const navigate = useNavigate();

  // The queue is fixed when review starts, so saved documents leaving the inbox don't shift positions.
  const queue = useQuery({
    queryKey: ["review-queue"],
    queryFn: () =>
      call(() =>
        client.GET("/api/v1/documents", {
          params: { query: { status: ["new"], sort: "uploaded", page_size: 100 } },
        }),
      ),
    staleTime: Number.POSITIVE_INFINITY,
    gcTime: 0,
    refetchOnWindowFocus: false,
  });
  const ids = useMemo(() => queue.data?.items.map((d) => d.id) ?? [], [queue.data]);
  const [index, setIndex] = useState(0);

  useEffect(() => {
    const start = params.get("start");
    if (start && ids.includes(start)) setIndex(ids.indexOf(start));
  }, [ids, params]);

  const currentId = ids[index];
  const nextId = ids[index + 1];

  // Load the next document in the background so "Save & next" feels instant.
  useEffect(() => {
    if (!nextId) return;
    qc.prefetchQuery({
      queryKey: keys.document(nextId),
      queryFn: () => call(() => client.GET("/api/v1/documents/{doc_id}", { params: { path: { doc_id: nextId } } })),
    });
    qc.prefetchQuery({
      queryKey: ["document-text", nextId],
      queryFn: () =>
        call(() => client.GET("/api/v1/documents/{doc_id}/text", { params: { path: { doc_id: nextId } } })),
    });
  }, [nextId, qc]);

  if (queue.isPending) return <Spinner className="size-6" />;
  if (queue.error) return <ErrorNote error={queue.error} />;
  if (!ids.length || index >= ids.length) {
    return (
      <div className="py-10">
        <EmptyState icon={<Inbox />} title={ids.length ? "All done — inbox reviewed" : "Your inbox is empty"}>
          {ids.length ? `You went through ${ids.length} document${ids.length === 1 ? "" : "s"}.` : null}
          <div className="mt-4 flex justify-center gap-2">
            <Button onClick={() => navigate("/inbox")}>Back to inbox</Button>
            {ids.length > 0 && (
              <Button variant="outline" onClick={() => qc.resetQueries({ queryKey: ["review-queue"] }).then(() => setIndex(0))}>
                Review new arrivals
              </Button>
            )}
          </div>
        </EmptyState>
      </div>
    );
  }

  return (
    <ReviewDocument
      key={currentId}
      id={currentId}
      position={index + 1}
      total={ids.length}
      onPrevious={index > 0 ? () => setIndex(index - 1) : undefined}
      onNext={() => setIndex(index + 1)}
    />
  );
}

function ReviewDocument({
  id,
  position,
  total,
  onPrevious,
  onNext,
}: {
  id: string;
  position: number;
  total: number;
  onPrevious?: () => void;
  onNext: () => void;
}) {
  const doc = useDocument(id);
  const [view, setView] = useState<"pdf" | "text" | "both">(() =>
    typeof window !== "undefined" && window.innerWidth >= 1400 ? "both" : "pdf",
  );

  return (
    <div className="-mx-4 -my-6 flex h-[calc(100vh-3.5rem)] flex-col md:-mx-8">
      <div className="flex flex-wrap items-center gap-2 border-b bg-card px-3 py-2">
        <Button variant="ghost" size="sm" asChild>
          <Link to="/inbox">
            <ArrowLeft /> Inbox
          </Link>
        </Button>
        <div className="flex items-center gap-2 text-sm">
          <span className="font-medium">
            Reviewing {position} of {total}
          </span>
          <div className="hidden h-1.5 w-32 overflow-hidden rounded-full bg-muted sm:block">
            <div className="h-full bg-primary transition-all" style={{ width: `${((position - 1) / total) * 100}%` }} />
          </div>
        </div>
        <div className="ml-auto flex items-center gap-1">
          <div className="mr-2 inline-flex rounded-md border p-0.5">
            {(
              [
                ["pdf", "Document", FileText],
                ["text", "Text", Type],
                ["both", "Both", Columns2],
              ] as const
            ).map(([value, label, Icon]) => (
              <button
                key={value}
                type="button"
                onClick={() => setView(value)}
                className={cn(
                  "inline-flex cursor-pointer items-center gap-1 rounded px-2.5 py-1 text-xs font-medium",
                  view === value ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted",
                )}
              >
                <Icon className="size-3.5" /> {label}
              </button>
            ))}
          </div>
          <Button variant="ghost" size="sm" disabled={!onPrevious} onClick={onPrevious} title="Previous (Alt+←)">
            <ChevronLeft /> Back
          </Button>
        </div>
      </div>
      {doc.isPending ? (
        <div className="p-6">
          <Spinner className="size-6" />
        </div>
      ) : doc.error || !doc.data ? (
        <div className="p-6">
          <ErrorNote error={doc.error ?? "Document not found"} />
        </div>
      ) : (
        <ReviewWorkspace doc={doc.data} view={view} onPrevious={onPrevious} onNext={onNext} />
      )}
    </div>
  );
}

function ReviewWorkspace({
  doc,
  view,
  onPrevious,
  onNext,
}: {
  doc: DocumentDetail;
  view: "pdf" | "text" | "both";
  onPrevious?: () => void;
  onNext: () => void;
}) {
  const [statusOnSave, setStatusOnSave] = useState<Status>(() => {
    try {
      const v = localStorage.getItem("docnest-review-status");
      if (v === "todo" || v === "done" || v === "new") return v;
    } catch {
      /* storage unavailable */
    }
    return "done";
  });
  const [initial, setInitial] = useState<Draft>(() => draftFrom(doc, statusOnSave));
  const [draft, setDraft] = useState<Draft>(initial);
  const [selection, setSelection] = useState("");
  const [saving, setSaving] = useState(false);
  const invalidate = useInvalidateDocuments();
  const qc = useQueryClient();
  const correspondents = useCorrespondents();
  const titleRef = useRef<HTMLInputElement>(null);
  const processing = doc.processing_state === "pending" || doc.processing_state === "running";

  // Keep the draft in sync while the document is still being processed (fields fill in).
  useEffect(() => {
    if (processing) return;
    if (JSON.stringify(draft) === JSON.stringify(initial)) {
      const fresh = draftFrom(doc, statusOnSave);
      setInitial(fresh);
      setDraft(fresh);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [processing]);

  useEffect(() => titleRef.current?.focus(), []);

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft((d) => ({ ...d, [key]: value }));
  // The status choice is a preference, not an edit — ignore it for "unsaved changes".
  const dirty = JSON.stringify({ ...draft, status: "" }) !== JSON.stringify({ ...initial, status: "" });

  const buildPatch = (): DocumentPatch => {
    const p: DocumentPatch = {};
    if (draft.title.trim() && draft.title.trim() !== doc.title) p.title = draft.title.trim();
    if (draft.documentDate !== (doc.document_date ?? "")) {
      if (draft.documentDate) p.document_date = draft.documentDate;
      else p.clear_document_date = true;
    }
    const sender = clean(draft.sender);
    if (sender !== (doc.correspondent?.name ?? "")) {
      if (!sender) p.clear_correspondent = true;
      else {
        const existing = correspondents.data?.find((c) => c.name.toLowerCase() === sender.toLowerCase());
        if (existing) p.correspondent_id = existing.id;
        else p.correspondent_name = sender;
      }
    }
    if (draft.typeId !== null && draft.typeId !== (doc.document_type?.id ?? null)) p.document_type_id = draft.typeId;
    if (draft.bucketId !== doc.bucket.id) p.bucket_id = draft.bucketId;
    const currentTags = doc.tags.map((t) => t.name);
    const tagsChanged =
      draft.tags.length !== currentTags.length || draft.tags.some((t) => !currentTags.includes(t));
    if (tagsChanged) {
      p.tag_ids = [];
      p.tag_names = draft.tags;
    }
    if (draft.seriesId !== (doc.series?.id ?? null)) {
      if (draft.seriesId === null) p.clear_series = true;
      else p.series_id = draft.seriesId;
    }
    if (draft.status !== doc.status) p.status = draft.status;
    if (draft.important !== doc.is_important) p.is_important = draft.important;
    return p;
  };

  const save = useCallback(async () => {
    if (saving) return;
    setSaving(true);
    try {
      const patch = buildPatch();
      if (Object.keys(patch).length) {
        await call(() =>
          client.PATCH("/api/v1/documents/{doc_id}", {
            params: { path: { doc_id: doc.id } },
            body: patch as Schemas["DocumentPatch"],
          }),
        );
        qc.removeQueries({ queryKey: keys.document(doc.id) });
        invalidate();
      }
      toast.success(`Saved “${clean(draft.title) || doc.title}”`, { duration: 2000 });
      onNext();
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setSaving(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draft, doc, saving, onNext]);

  const skip = useCallback(() => {
    if (dirty && !confirm("Discard your changes to this document?")) return;
    onNext();
  }, [dirty, onNext]);

  // Keyboard: Ctrl/⌘+Enter save & next · Alt+→ skip · Alt+← back
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        void save();
      } else if (e.altKey && e.key === "ArrowRight") {
        e.preventDefault();
        skip();
      } else if (e.altKey && e.key === "ArrowLeft" && onPrevious) {
        e.preventDefault();
        onPrevious();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [save, skip, onPrevious]);

  const chooseStatus = (s: Status) => {
    set("status", s);
    setStatusOnSave(s);
    try {
      localStorage.setItem("docnest-review-status", s);
    } catch {
      /* storage unavailable */
    }
  };

  const insert = (field: "title" | "sender" | "date" | "tag") => {
    const text = clean(selection);
    if (!text) return;
    if (field === "title") set("title", text.slice(0, 200));
    if (field === "sender") set("sender", text.slice(0, 150));
    if (field === "tag" && !draft.tags.some((t) => t.toLowerCase() === text.toLowerCase())) {
      set("tags", [...draft.tags, text.slice(0, 80)]);
    }
    if (field === "date") {
      const parsed = parseDate(text);
      if (parsed) set("documentDate", parsed);
      else return toast.error(`“${text.slice(0, 40)}” is not a date`);
    }
    window.getSelection()?.removeAllRanges();
    setSelection("");
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
      <div className="flex min-h-[50vh] min-w-0 flex-1 border-b lg:border-b-0 lg:border-r">
        {(view === "pdf" || view === "both") && (
          <div className={cn("min-w-0", view === "both" ? "w-1/2 border-r" : "w-full")}>
            {doc.page_count > 0 || doc.stored ? (
              <PdfViewer url={`/api/v1/documents/${doc.id}/file?variant=archive`} />
            ) : (
              <Processing />
            )}
          </div>
        )}
        {(view === "text" || view === "both") && (
          <div className={cn("min-w-0", view === "both" ? "w-1/2" : "w-full")}>
            <OcrPanel id={doc.id} processing={processing} selection={selection} onSelect={setSelection} onInsert={insert} />
          </div>
        )}
      </div>
      <aside className="flex w-full shrink-0 flex-col bg-background lg:w-[400px]">
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <ReviewForm doc={doc} draft={draft} set={set} titleRef={titleRef} />
        </div>
        <div className="border-t bg-card p-4">
          <Label className="mb-1.5 block text-xs text-muted-foreground">After saving, the document is</Label>
          <div className="mb-3 grid grid-cols-3 rounded-md border p-0.5">
            {(
              [
                ["done", "Done"],
                ["todo", "Todo"],
                ["new", "In inbox"],
              ] as const
            ).map(([value, label]) => (
              <button
                key={value}
                type="button"
                onClick={() => chooseStatus(value)}
                className={cn(
                  "cursor-pointer rounded px-2 py-1.5 text-xs font-medium",
                  draft.status === value ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted",
                )}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="flex gap-2">
            <Button variant="outline" onClick={skip} title="Skip without saving (Alt+→)">
              <SkipForward /> Skip
            </Button>
            <Button className="flex-1" onClick={() => void save()} disabled={saving}>
              {saving ? <Spinner className="text-primary-foreground" /> : <Check />}
              Save & next
            </Button>
          </div>
          <p className="mt-2 text-center text-[11px] text-muted-foreground">
            <Kbd>Ctrl</Kbd> + <Kbd>Enter</Kbd> save & next · <Kbd>Alt</Kbd> + <Kbd>→</Kbd> skip · <Kbd>Alt</Kbd> +{" "}
            <Kbd>←</Kbd> back
          </p>
        </div>
      </aside>
    </div>
  );
}

function Processing() {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-sm text-muted-foreground">
      <Spinner className="size-6" /> Still being processed (OCR)…
    </div>
  );
}

function OcrPanel({
  id,
  processing,
  selection,
  onSelect,
  onInsert,
}: {
  id: string;
  processing: boolean;
  selection: string;
  onSelect: (text: string) => void;
  onInsert: (field: "title" | "sender" | "date" | "tag") => void;
}) {
  const text = useQuery({
    queryKey: ["document-text", id],
    queryFn: () => call(() => client.GET("/api/v1/documents/{doc_id}/text", { params: { path: { doc_id: id } } })),
    refetchInterval: processing ? 3000 : false,
  });
  const panel = useRef<HTMLPreElement>(null);

  const capture = () => {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !panel.current) return onSelect("");
    const node = sel.anchorNode;
    if (node && panel.current.contains(node)) onSelect(sel.toString());
  };

  const short = clean(selection);
  const isDate = short ? parseDate(short) !== null : false;

  return (
    <div className="flex h-full flex-col">
      <div className="flex min-h-11 flex-wrap items-center gap-1.5 border-b bg-card px-3 py-1.5 text-xs">
        {short ? (
          <>
            <span className="mr-1 max-w-48 truncate text-muted-foreground" title={short}>
              “{short}” →
            </span>
            <InsertButton icon={<Type />} label="Title" onClick={() => onInsert("title")} />
            <InsertButton icon={<User />} label="Sender" onClick={() => onInsert("sender")} />
            <InsertButton icon={<Calendar />} label="Date" onClick={() => onInsert("date")} highlight={isDate} />
            <InsertButton icon={<Tag />} label="Tag" onClick={() => onInsert("tag")} />
            <InsertButton
              icon={<Copy />}
              label="Copy"
              onClick={() => navigator.clipboard.writeText(short).then(() => toast.success("Copied"))}
            />
          </>
        ) : (
          <span className="text-muted-foreground">
            Select text below to use it as title, sender, date or tag — or copy it.
          </span>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-auto bg-muted/40 p-4">
        {text.isPending ? (
          <Spinner />
        ) : text.error ? (
          <ErrorNote error={text.error} />
        ) : !text.data.text.trim() ? (
          <p className="text-sm text-muted-foreground">{processing ? "Text recognition is running…" : "No text was recognized."}</p>
        ) : (
          <pre
            ref={panel}
            data-testid="ocr-text"
            onMouseUp={capture}
            onKeyUp={capture}
            className="cursor-text select-text whitespace-pre-wrap rounded-md bg-card p-4 font-mono text-[13px] leading-6 shadow-sm selection:bg-primary/25"
          >
            {text.data.text}
          </pre>
        )}
      </div>
    </div>
  );
}

function InsertButton({
  icon,
  label,
  onClick,
  highlight,
}: {
  icon: ReactNode;
  label: string;
  onClick: () => void;
  highlight?: boolean;
}) {
  return (
    <button
      type="button"
      onMouseDown={(e) => e.preventDefault()} // keep the text selection
      onClick={onClick}
      className={cn(
        "inline-flex cursor-pointer items-center gap-1 rounded-md border px-2 py-1 font-medium hover:bg-muted [&_svg]:size-3.5",
        highlight && "border-primary text-primary",
      )}
    >
      {icon}
      {label}
    </button>
  );
}

function ReviewForm({
  doc,
  draft,
  set,
  titleRef,
}: {
  doc: DocumentDetail;
  draft: Draft;
  set: <K extends keyof Draft>(key: K, value: Draft[K]) => void;
  titleRef: React.RefObject<HTMLInputElement | null>;
}) {
  const buckets = useBuckets();
  const types = useTypes();
  const correspondents = useCorrespondents();
  const tags = useTags();
  const series = useSeriesList();
  const [tagInput, setTagInput] = useState("");

  const addTag = () => {
    const name = clean(tagInput);
    if (name && !draft.tags.some((t) => t.toLowerCase() === name.toLowerCase())) set("tags", [...draft.tags, name]);
    setTagInput("");
  };
  const colorOf = (name: string) => tags.data?.find((t) => t.name.toLowerCase() === name.toLowerCase())?.color;
  const extracted = doc.extracted as Record<string, unknown>;

  return (
    <div className="flex flex-col gap-4">
      {doc.processing_state === "failed" && (
        <div className="rounded-md border border-destructive/30 bg-destructive/10 p-2 text-xs text-destructive">
          Processing failed: {doc.processing_error}
        </div>
      )}
      <FormField label="Title" htmlFor="review-title">
        <Input id="review-title" ref={titleRef} value={draft.title} onChange={(e) => set("title", e.target.value)} />
      </FormField>
      <FormField label="Sender" htmlFor="review-sender">
        <Input
          id="review-sender"
          list="review-senders"
          value={draft.sender}
          placeholder="Unknown"
          onChange={(e) => set("sender", e.target.value)}
        />
        <datalist id="review-senders">
          {correspondents.data?.map((c) => <option key={c.id} value={c.name} />)}
        </datalist>
      </FormField>
      <div className="grid grid-cols-2 gap-3">
        <FormField label="Document date" htmlFor="review-date">
          <DateInput id="review-date" value={draft.documentDate} onValueChange={(value) => set("documentDate", value)} />
        </FormField>
        <FormField label="Type" htmlFor="review-type">
          <Select
            id="review-type"
            value={draft.typeId ?? ""}
            onChange={(e) => set("typeId", e.target.value ? Number(e.target.value) : null)}
          >
            {draft.typeId === null && <option value="">—</option>}
            {types.data?.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </Select>
        </FormField>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <FormField label="Bucket" htmlFor="review-bucket">
          <Select id="review-bucket" value={draft.bucketId} onChange={(e) => set("bucketId", Number(e.target.value))}>
            {buckets.data?.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
              </option>
            ))}
          </Select>
        </FormField>
        <FormField label="Series" htmlFor="review-series">
          <Select
            id="review-series"
            value={draft.seriesId ?? ""}
            onChange={(e) => set("seriesId", e.target.value ? Number(e.target.value) : null)}
          >
            <option value="">None</option>
            {series.data?.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </Select>
        </FormField>
      </div>
      <FormField label="Tags" htmlFor="review-tag">
        <div className="flex flex-wrap gap-1.5">
          {draft.tags.map((t) => (
            <span key={t} className="inline-flex items-center gap-1">
              <TagChip name={t} color={colorOf(t)} />
              <button
                type="button"
                title={`Remove ${t}`}
                className="cursor-pointer text-muted-foreground hover:text-foreground"
                onClick={() => set("tags", draft.tags.filter((x) => x !== t))}
              >
                <X className="size-3" />
              </button>
            </span>
          ))}
          {!draft.tags.length && <span className="text-sm text-muted-foreground">No tags</span>}
        </div>
        <Input
          id="review-tag"
          list="review-tag-options"
          className="h-8"
          placeholder="Add tag and press Enter…"
          value={tagInput}
          onChange={(e) => setTagInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !(e.ctrlKey || e.metaKey)) {
              e.preventDefault();
              addTag();
            }
          }}
          onBlur={addTag}
        />
        <datalist id="review-tag-options">
          {tags.data?.filter((t) => !draft.tags.includes(t.name)).map((t) => <option key={t.id} value={t.name} />)}
        </datalist>
      </FormField>
      <label className="flex cursor-pointer items-center gap-2 text-sm">
        <input
          type="checkbox"
          className="size-4 accent-[var(--primary)]"
          checked={draft.important}
          onChange={(e) => set("important", e.target.checked)}
        />
        <Star className={cn("size-4", draft.important && "fill-amber-400 text-amber-400")} /> Important
      </label>
      <div className="rounded-md border bg-muted/40 p-3 text-xs">
        <div className="mb-1 font-semibold uppercase tracking-wide text-muted-foreground">Detected data</div>
        {extracted.total_amount || Object.keys((extracted.references ?? {}) as object).length > 0 ? (
          <>
            {extracted.total_amount ? <div>Amount: {String(extracted.total_amount)}</div> : null}
            {Object.entries((extracted.references ?? {}) as Record<string, string>).map(([k, v]) => (
              <div key={k} className="truncate">
                {k}: <span className="font-mono">{v}</span>
              </div>
            ))}
          </>
        ) : (
          <p className="text-muted-foreground">Recognized amounts and references will appear here.</p>
        )}
      </div>
      <Link to={`/documents/${doc.id}`} className="text-xs text-muted-foreground hover:underline">
        Open full document page (history, downloads, more actions)
      </Link>
    </div>
  );
}

function FormField({ label, htmlFor, children }: { label: string; htmlFor: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={htmlFor} className="text-xs text-muted-foreground">
        {label}
      </Label>
      {children}
    </div>
  );
}
