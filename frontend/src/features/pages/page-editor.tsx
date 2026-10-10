import { useMutation, useQueries } from "@tanstack/react-query";
import {
  ArrowLeft,
  ArrowRight,
  Combine,
  EyeOff,
  FilePlus2,
  Layers,
  Scissors,
  ShieldCheck,
  Undo2,
  X,
  ZoomIn,
} from "lucide-react";
import { type DragEvent, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { call, client, type Schemas } from "@/api/client";
import { useInvalidateDocuments } from "@/api/queries";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown";
import { Input } from "@/components/ui/input";
import { ErrorNote, Spinner } from "@/components/ui/misc";
import { cn } from "@/lib/utils";
import {
  effect,
  initialPlan,
  joinWithPrevious,
  leaveOut,
  mergeAll,
  movePages,
  nudge,
  type Output,
  type PageKey,
  type Plan,
  pageKey,
  parseKey,
  planFrom,
  splitAt,
  toRequest,
  unchangedSource,
} from "./page-plan";

type Alteration = Schemas["AlterationOut"];
type Twin = { text: string; other: PageKey };
type PageInfo = { mark: string; blank: boolean; color: string; title: string };
type Match = [string, number, string, number];

// One colour per source document, so a page shows where it comes from.
const COLORS = [
  "border-sky-500 bg-sky-500",
  "border-amber-500 bg-amber-500",
  "border-violet-500 bg-violet-500",
  "border-emerald-500 bg-emerald-500",
  "border-rose-500 bg-rose-500",
  "border-cyan-600 bg-cyan-600",
  "border-lime-600 bg-lime-600",
  "border-fuchsia-500 bg-fuchsia-500",
];

export function pageImage(doc: string, page: number) {
  return `/api/v1/alterations/pages/${doc}/${page}`;
}

export function undoAlteration(id: number, onDone?: () => void) {
  return call(() => client.POST("/api/v1/alterations/{alteration_id}/undo", { params: { path: { alteration_id: id } } }))
    .then(() => {
      toast.success("Undone: the documents are as before");
      onDone?.();
    })
    .catch((e: Error) => toast.error(e.message));
}

/**
 * The page editor: which pages make up which documents. Merge, split, take pages out,
 * put them in order — by hand, or starting from a suggestion of the assistant.
 * Applying never changes an original: new documents are made from copies of the pages,
 * and the documents they came from go to the trash, where they can be restored.
 */
export function PageEditor({
  sourceIds,
  suggestion,
  matches = [],
  origin,
  heading = "Edit pages",
  explanation,
  onApplied,
  onClose,
}: {
  sourceIds: string[];
  suggestion?: { pages: [string, number][]; title: string }[];
  matches?: Match[];
  origin?: { task: number; finding: string };
  heading?: string;
  explanation?: string;
  onApplied?: (alteration: Alteration) => void;
  onClose: () => void;
}) {
  const invalidate = useInvalidateDocuments();
  const results = useQueries({
    queries: sourceIds.map((id) => ({
      queryKey: ["pages", id],
      queryFn: () => call(() => client.GET("/api/v1/alterations/pages/{doc_id}", { params: { path: { doc_id: id } } })),
      staleTime: 60_000,
    })),
  });
  const loading = results.some((r) => r.isPending);
  const failed = results.find((r) => r.error)?.error;
  const docs = results.map((r) => r.data).filter((d): d is Schemas["PagesOut"] => !!d);
  const sources = useMemo(() => docs.map((d) => ({ id: d.id, pages: d.page_count })), [docs.map((d) => d.id).join()]); // eslint-disable-line react-hooks/exhaustive-deps

  const [plan, setPlan] = useState<Plan | null>(null);
  const [initial, setInitial] = useState<Plan | null>(null);
  const [selected, setSelected] = useState<PageKey[]>([]);
  useEffect(() => {
    if (loading || failed || plan) return;
    const start = suggestion ? planFrom(sources, suggestion) : initialPlan(sources);
    setPlan(start);
    setInitial(start);
  }, [loading, failed, plan, sources, suggestion]);

  const info = useMemo(() => {
    const m = new Map<PageKey, { mark: string; blank: boolean; color: string; title: string }>();
    docs.forEach((d, i) =>
      d.pages.forEach((p) =>
        m.set(pageKey(d.id, p.number), { mark: p.mark, blank: p.blank, color: COLORS[i % COLORS.length], title: d.title }),
      ),
    );
    return m;
  }, [docs]);
  const twins = useMemo(() => {
    const m = new Map<PageKey, Twin>();
    const title = (id: string) => docs.find((d) => d.id === id)?.title ?? "another document";
    for (const [a, p, b, q] of matches) {
      m.set(pageKey(b, q), { text: `Same as page ${p} of “${title(a)}”`, other: pageKey(a, p) });
      m.set(pageKey(a, p), { text: `Same as page ${q} of “${title(b)}”`, other: pageKey(b, q) });
    }
    return m;
  }, [matches, docs]);
  const [zoom, setZoom] = useState<PageKey | null>(null);

  const apply = useMutation({
    mutationFn: () =>
      call(() => client.POST("/api/v1/alterations/compose", { body: { ...toRequest(plan as Plan, sources), origin } })),
    onSuccess: (alteration) => {
      invalidate();
      toast.success(alteration.summary, {
        description: "The originals are untouched; the documents they replace are in the trash.",
        action: { label: "Undo", onClick: () => void undoAlteration(alteration.id, invalidate) },
        duration: 10_000,
      });
      onApplied?.(alteration);
      onClose();
    },
    onError: (e) => toast.error(e.message),
  });

  const result = plan ? effect(plan, sources) : null;
  const toggle = (k: PageKey) => setSelected((s) => (s.includes(k) ? s.filter((x) => x !== k) : [...s, k]));
  const update = (next: Plan) => {
    setPlan(next);
    setSelected([]);
  };

  return (
    <Dialog open onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="flex h-[92vh] max-w-6xl flex-col gap-0 overflow-hidden p-0">
        <DialogHeader className="border-b px-6 py-4 pr-12">
          <DialogTitle className="flex items-center gap-2">
            <Layers className="size-5" /> {heading}
          </DialogTitle>
          <DialogDescription>
            {explanation ??
              "Arrange the pages into documents. Click pages to select them; drag them to move; cut a document with the scissors."}
          </DialogDescription>
        </DialogHeader>

        {loading || !plan ? (
          <div className="flex flex-1 items-center justify-center">{failed ? <ErrorNote error={failed} /> : <Spinner />}</div>
        ) : (
          <>
            <Legend docs={docs} />
            <Toolbar
              plan={plan}
              selected={selected}
              onClear={() => setSelected([])}
              onMove={(to) => update(movePages(plan, selected, to))}
              onLeaveOut={() => update(leaveOut(plan, selected))}
              onMergeAll={() => update(mergeAll(plan))}
              onReset={() => initial && update(initial)}
              changed={plan !== initial}
            />
            <div className="min-h-0 flex-1 space-y-3 overflow-y-auto bg-muted/30 px-6 py-4">
              {plan.outputs.map((output, index) => (
                <OutputRow
                  key={output.key}
                  output={output}
                  index={index}
                  unchanged={unchangedSource(output, sources) !== null}
                  info={info}
                  twins={twins}
                  selected={selected}
                  onToggle={toggle}
                  onTitle={(title) =>
                    setPlan({ ...plan, outputs: plan.outputs.map((o) => (o.key === output.key ? { ...o, title } : o)) })
                  }
                  onSplit={(at) => update(splitAt(plan, output.key, at))}
                  onJoin={() => update(joinWithPrevious(plan, output.key))}
                  onNudge={(page, by) => update(nudge(plan, page, by))}
                  onDrop={(keys, at) => update(movePages(plan, keys, output.key, at))}
                  onLeaveOut={(keys) => update(leaveOut(plan, keys))}
                  onZoom={setZoom}
                />
              ))}
              <NewRowDropZone onDrop={(keys) => update(movePages(plan, keys, null))} />
              {plan.leftOut.length > 0 && (
                <LeftOut
                  pages={plan.leftOut}
                  info={info}
                  twins={twins}
                  selected={selected}
                  onToggle={toggle}
                  onPutBack={(keys) => update(movePages(plan, keys, null))}
                  onZoom={setZoom}
                />
              )}
            </div>
            {zoom && <Zoom page={zoom} twin={twins.get(zoom)} info={info} onClose={() => setZoom(null)} />}
            <DialogFooter className="items-center border-t px-6 py-3 sm:justify-between">
              {result && <EffectText effect={result} docs={docs} />}
              <div className="flex gap-2">
                <Button variant="outline" onClick={onClose}>
                  Cancel
                </Button>
                <Button disabled={!result?.changes || apply.isPending} onClick={() => apply.mutate()}>
                  {apply.isPending ? <Spinner /> : <ArrowRight />} Apply
                </Button>
              </div>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

function Legend({ docs }: { docs: Schemas["PagesOut"][] }) {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 border-b px-6 py-2 text-xs text-muted-foreground">
      {docs.map((d, i) => (
        <a key={d.id} href={`/documents/${d.id}`} target="_blank" rel="noreferrer" className="flex items-center gap-1.5 hover:text-foreground">
          <span className={cn("size-2.5 rounded-full", COLORS[i % COLORS.length].split(" ")[1])} />
          <span className="max-w-64 truncate">{d.title || "Untitled"}</span>
          <span>· {d.page_count} p.</span>
        </a>
      ))}
    </div>
  );
}

function Toolbar({
  plan,
  selected,
  onClear,
  onMove,
  onLeaveOut,
  onMergeAll,
  onReset,
  changed,
}: {
  plan: Plan;
  selected: PageKey[];
  onClear: () => void;
  onMove: (to: string | null) => void;
  onLeaveOut: () => void;
  onMergeAll: () => void;
  onReset: () => void;
  changed: boolean;
}) {
  return (
    <div className="flex min-h-12 flex-wrap items-center gap-2 border-b px-6 py-2">
      {selected.length ? (
        <>
          <span className="text-sm font-medium">
            {selected.length} page{selected.length === 1 ? "" : "s"} selected
          </span>
          <Button size="sm" variant="outline" onClick={() => onMove(null)}>
            <FilePlus2 /> New document
          </Button>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button size="sm" variant="outline">
                <ArrowRight /> Move to…
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent>
              {plan.outputs.map((o, i) => (
                <DropdownMenuItem key={o.key} onSelect={() => onMove(o.key)}>
                  Document {i + 1}
                  {o.title && ` — ${o.title}`}
                </DropdownMenuItem>
              ))}
              <DropdownMenuSeparator />
              <DropdownMenuItem onSelect={() => onMove(null)}>A new document</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          <Button size="sm" variant="outline" onClick={onLeaveOut}>
            <EyeOff /> Leave out
          </Button>
          <Button size="sm" variant="ghost" onClick={onClear}>
            <X /> Clear
          </Button>
        </>
      ) : (
        <>
          <span className="text-sm text-muted-foreground">Select pages to move or leave them out.</span>
          <div className="ml-auto flex gap-2">
            {plan.outputs.length > 1 && (
              <Button size="sm" variant="outline" onClick={onMergeAll}>
                <Combine /> Merge all into one
              </Button>
            )}
            {changed && (
              <Button size="sm" variant="ghost" onClick={onReset}>
                <Undo2 /> Start over
              </Button>
            )}
          </div>
        </>
      )}
    </div>
  );
}

const DRAG = "application/x-docnest-pages";

function dragged(e: DragEvent): PageKey[] {
  try {
    return JSON.parse(e.dataTransfer.getData(DRAG) || "[]");
  } catch {
    return [];
  }
}

function OutputRow({
  output,
  index,
  unchanged,
  info,
  twins,
  selected,
  onToggle,
  onTitle,
  onSplit,
  onJoin,
  onNudge,
  onDrop,
  onLeaveOut,
  onZoom,
}: {
  output: Output;
  index: number;
  unchanged: boolean;
  info: Map<PageKey, PageInfo>;
  twins: Map<PageKey, Twin>;
  selected: PageKey[];
  onToggle: (k: PageKey) => void;
  onTitle: (title: string) => void;
  onSplit: (at: number) => void;
  onJoin: () => void;
  onNudge: (page: PageKey, by: -1 | 1) => void;
  onDrop: (keys: PageKey[], at: number) => void;
  onLeaveOut: (keys: PageKey[]) => void;
  onZoom: (k: PageKey) => void;
}) {
  const [over, setOver] = useState<number | null>(null);
  const firstTitle = info.get(output.pages[0])?.title ?? "";
  return (
    <section
      className={cn("rounded-lg border bg-card p-3", over !== null && "ring-2 ring-primary/40")}
      onDragOver={(e) => {
        if (!e.dataTransfer.types.includes(DRAG)) return;
        e.preventDefault();
        if (over === null) setOver(output.pages.length);
      }}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node)) setOver(null);
      }}
      onDrop={(e) => {
        e.preventDefault();
        const keys = dragged(e);
        if (keys.length) onDrop(keys, over ?? output.pages.length);
        setOver(null);
      }}
    >
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="text-sm font-semibold">Document {index + 1}</span>
        {unchanged ? (
          <Badge variant="muted" title="All its pages, in order: this document stays as it is">
            Unchanged
          </Badge>
        ) : (
          <Badge variant="success">New · {output.pages.length} p.</Badge>
        )}
        {!unchanged && (
          <Input
            value={output.title}
            onChange={(e) => onTitle(e.target.value)}
            placeholder={`Title (empty: like “${firstTitle || "Untitled"}”, then the analysis)`}
            className="h-8 min-w-48 flex-1 text-sm"
            maxLength={200}
            aria-label={`Title of document ${index + 1}`}
          />
        )}
        <div className="ml-auto flex gap-1">
          {index > 0 && (
            <Button size="sm" variant="ghost" onClick={onJoin} title="Append to the document above">
              <Combine /> Join with above
            </Button>
          )}
          <Button size="sm" variant="ghost" onClick={() => onLeaveOut(output.pages)} title="Leave all its pages out">
            <EyeOff />
          </Button>
        </div>
      </div>
      <div className="flex flex-wrap items-start gap-y-3">
        {output.pages.map((k, i) => (
          <div key={k} className="flex items-start">
            {i > 0 && (
              <button
                type="button"
                className={cn(
                  "group mx-0.5 flex h-40 w-5 items-center justify-center rounded text-muted-foreground/40 hover:bg-muted hover:text-foreground",
                  over === i && "bg-primary/20",
                )}
                onClick={() => onSplit(i)}
                onDragOver={() => setOver(i)}
                title="Cut here: the pages after it become a document of their own"
                aria-label={`Cut document ${index + 1} before page ${i + 1}`}
              >
                <Scissors className="size-3.5 -rotate-90" />
              </button>
            )}
            <PageCard
              page={k}
              info={info.get(k)}
              twin={twins.get(k)}
              selected={selected.includes(k)}
              onToggle={() => onToggle(k)}
              dragKeys={selected.includes(k) ? selected : [k]}
              onLeft={i > 0 ? () => onNudge(k, -1) : undefined}
              onRight={i < output.pages.length - 1 ? () => onNudge(k, 1) : undefined}
              onDragOver={() => setOver(i)}
              onZoom={() => onZoom(k)}
            />
          </div>
        ))}
      </div>
    </section>
  );
}

function PageCard({
  page,
  info,
  twin,
  selected,
  onToggle,
  dragKeys,
  onLeft,
  onRight,
  onDragOver,
  onZoom,
}: {
  page: PageKey;
  info?: PageInfo;
  twin?: Twin;
  selected: boolean;
  onToggle: () => void;
  dragKeys: PageKey[];
  onLeft?: () => void;
  onRight?: () => void;
  onDragOver?: () => void;
  onZoom?: () => void;
}) {
  const [doc, n] = parseKey(page);
  const color = info?.color.split(" ")[0] ?? "border-border";
  return (
    <div className="group/page flex w-28 flex-col items-center gap-1">
      <button
        type="button"
        draggable
        onDragStart={(e) => {
          e.dataTransfer.setData(DRAG, JSON.stringify(dragKeys));
          e.dataTransfer.effectAllowed = "move";
        }}
        onDragOver={onDragOver}
        onClick={onToggle}
        className={cn(
          "relative h-40 w-28 overflow-hidden rounded border-2 bg-white shadow-sm transition",
          color,
          selected && "ring-4 ring-primary/60",
          twin && "outline outline-2 outline-offset-2 outline-red-500",
        )}
        aria-pressed={selected}
        title={[info?.title, `page ${n}`, twin?.text].filter(Boolean).join(" · ")}
      >
        <img src={pageImage(doc, n)} alt={`Page ${n} of ${info?.title ?? "a document"}`} loading="lazy" className="h-full w-full object-contain" />
        {info?.mark && (
          <span className="absolute right-1 top-1 rounded bg-black/70 px-1 text-[10px] font-medium text-white">{info.mark}</span>
        )}
        {info?.blank && (
          <span className="absolute inset-x-1 top-1/2 rounded bg-amber-100 px-1 text-[10px] text-amber-900">empty</span>
        )}
        {twin && (
          <span className="absolute inset-x-0 bottom-0 bg-red-600/90 px-1 py-0.5 text-[10px] leading-tight text-white">
            {twin.text}
          </span>
        )}
      </button>
      <div className="flex w-full items-center justify-between text-[11px] text-muted-foreground">
        <button type="button" disabled={!onLeft} onClick={onLeft} className="rounded p-0.5 hover:bg-muted disabled:opacity-0" aria-label="Move left">
          <ArrowLeft className="size-3" />
        </button>
        <button type="button" onClick={onZoom} className="flex items-center gap-0.5 truncate rounded px-1 hover:bg-muted hover:text-foreground" title="Look closer">
          <ZoomIn className="size-3" /> p. {n}
        </button>
        <button type="button" disabled={!onRight} onClick={onRight} className="rounded p-0.5 hover:bg-muted disabled:opacity-0" aria-label="Move right">
          <ArrowRight className="size-3" />
        </button>
      </div>
    </div>
  );
}

function NewRowDropZone({ onDrop }: { onDrop: (keys: PageKey[]) => void }) {
  const [over, setOver] = useState(false);
  return (
    <div
      className={cn(
        "flex h-14 items-center justify-center rounded-lg border-2 border-dashed text-sm text-muted-foreground",
        over && "border-primary bg-primary/5 text-foreground",
      )}
      onDragOver={(e) => {
        if (e.dataTransfer.types.includes(DRAG)) {
          e.preventDefault();
          setOver(true);
        }
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setOver(false);
        const keys = dragged(e);
        if (keys.length) onDrop(keys);
      }}
    >
      <FilePlus2 className="mr-2 size-4" /> Drop pages here for a new document
    </div>
  );
}

function LeftOut({
  pages,
  info,
  twins,
  selected,
  onToggle,
  onPutBack,
  onZoom,
}: {
  pages: PageKey[];
  info: Map<PageKey, PageInfo>;
  twins: Map<PageKey, Twin>;
  selected: PageKey[];
  onToggle: (k: PageKey) => void;
  onPutBack: (keys: PageKey[]) => void;
  onZoom: (k: PageKey) => void;
}) {
  return (
    <section className="rounded-lg border border-dashed bg-card/60 p-3">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <EyeOff className="size-4 text-muted-foreground" />
        <span className="text-sm font-semibold">Left out</span>
        <span className="text-xs text-muted-foreground">
          In no document afterwards — but still in the original, which stays in the trash.
        </span>
        <Button size="sm" variant="ghost" className="ml-auto" onClick={() => onPutBack(pages)}>
          Put all back
        </Button>
      </div>
      <div className="flex flex-wrap gap-3">
        {pages.map((k) => (
          <PageCard key={k} page={k} info={info.get(k)} twin={twins.get(k)} selected={selected.includes(k)} onToggle={() => onToggle(k)} dragKeys={selected.includes(k) ? selected : [k]} onZoom={() => onZoom(k)} />
        ))}
      </div>
    </section>
  );
}

function EffectText({
  effect: e,
  docs,
}: {
  effect: ReturnType<typeof effect>;
  docs: Schemas["PagesOut"][];
}) {
  const title = (id: string) => `“${docs.find((d) => d.id === id)?.title || "Untitled"}”`;
  if (!e.changes) return <p className="text-sm text-muted-foreground">Nothing changes yet.</p>;
  return (
    <p className="flex max-w-3xl items-start gap-2 text-sm text-muted-foreground">
      <ShieldCheck className="mt-0.5 size-4 shrink-0 text-emerald-600" />
      <span>
        {e.created > 0 && (
          <>
            Makes <strong className="text-foreground">{e.created} new document{e.created === 1 ? "" : "s"}</strong>.{" "}
          </>
        )}
        {e.trashed.map(title).join(", ")} {e.trashed.length === 1 ? "goes" : "go"} to the trash, where{" "}
        {e.trashed.length === 1 ? "it" : "they"} can be restored.
        {e.dropped > 0 && ` ${e.dropped} page${e.dropped === 1 ? " is" : "s are"} left out.`} Original files are never changed.
      </span>
    </p>
  );
}

/** A closer look at a page — and at the page it is the same as, side by side. */
function Zoom({
  page,
  twin,
  info,
  onClose,
}: {
  page: PageKey;
  twin?: Twin;
  info: Map<PageKey, PageInfo>;
  onClose: () => void;
}) {
  const shown = twin ? [page, twin.other] : [page];
  return (
    <Dialog open onOpenChange={(v) => !v && onClose()}>
      <DialogContent className={cn("flex max-h-[95vh] flex-col gap-3", twin ? "max-w-6xl" : "max-w-3xl")}>
        <DialogHeader>
          <DialogTitle>{twin ? "Are these the same page?" : "Page"}</DialogTitle>
          {twin && <DialogDescription>{twin.text}. Compare them before you leave one out.</DialogDescription>}
        </DialogHeader>
        <div className={cn("grid min-h-0 flex-1 gap-3 overflow-y-auto", twin && "grid-cols-2")}>
          {shown.map((k) => {
            const [doc, n] = parseKey(k);
            return (
              <figure key={k} className="flex flex-col gap-1">
                <figcaption className="truncate text-xs text-muted-foreground">
                  {info.get(k)?.title ?? "Document"} · page {n}
                </figcaption>
                <ZoomImage src={`${pageImage(doc, n)}?large=true`} fallback={pageImage(doc, n)} />
              </figure>
            );
          })}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function ZoomImage({ src, fallback }: { src: string; fallback: string }) {
  const [loaded, setLoaded] = useState(false);
  const [failed, setFailed] = useState(false);
  return (
    <div className="relative rounded border bg-white">
      {!loaded && <img src={fallback} alt="" className="w-full" />}
      {!failed && (
        <img
          src={src}
          alt=""
          className={cn("w-full", !loaded && "absolute inset-0 opacity-0")}
          onLoad={() => setLoaded(true)}
          onError={() => setFailed(true)}
        />
      )}
      {!loaded && !failed && (
        <span className="absolute right-2 top-2">
          <Spinner />
        </span>
      )}
    </div>
  );
}
