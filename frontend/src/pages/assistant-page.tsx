import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowRight,
  Bot,
  Check,
  ChevronRight,
  Copy,
  Cpu,
  FileWarning,
  Layers,
  ListChecks,
  Moon,
  NotebookPen,
  PencilLine,
  RotateCcw,
  Scale,
  Scissors,
  Square,
  Telescope,
  Trash2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { call, client, type Schemas } from "@/api/client";
import { useInvalidateDocuments } from "@/api/queries";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, ErrorNote, PageHeader, Spinner } from "@/components/ui/misc";
import { applyChanges, ChangeCard, initialTicks, type Key } from "@/features/assist/assistant";
import { PreparePagesNote, StartReviewDialog } from "@/features/assist/start-review";
import { PageEditor, pageImage, undoAlteration } from "@/features/pages/page-editor";
import { cn, formatDate, formatDateTime } from "@/lib/utils";

type Summary = Schemas["ReviewSummary"];
type Review = Schemas["ReviewOut"];
type Finding = Schemas["FindingOut"];

export const reviewsKey = ["reviews"] as const;

export function useReviews() {
  return useQuery({
    queryKey: reviewsKey,
    queryFn: () => call(() => client.GET("/api/v1/assist/reviews")),
    refetchInterval: (q) => (q.state.data?.some((r) => ["pending", "running"].includes(r.state)) ? 10_000 : 60_000),
  });
}

/** Reports the user has not opened yet: the sidebar shows their number. */
export function useUnreadReviews(): number {
  const reviews = useReviews();
  return (reviews.data ?? []).filter((r) => r.state === "done" && !r.read).length;
}

const time = (iso?: string | null) =>
  iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";

export function AssistantPage() {
  const { id } = useParams();
  const reviews = useReviews();
  const navigate = useNavigate();
  const [starting, setStarting] = useState(false);
  const list = reviews.data ?? [];
  const selected = id ? Number(id) : list[0]?.id;

  return (
    <div>
      <PageHeader
        title="Assistant"
        description="Let the assistant look through your documents — a folder, a selection or everything — and decide in the morning what to do with its suggestions."
        actions={
          <Button onClick={() => setStarting(true)}>
            <Telescope /> Look through everything
          </Button>
        }
      />
      <div className="mb-4">
        <PreparePagesNote />
      </div>
      {reviews.isPending ? (
        <Spinner />
      ) : reviews.error ? (
        <ErrorNote error={reviews.error} />
      ) : !list.length ? (
        <EmptyState icon={<Telescope />} title="No reviews yet">
          Select documents or open a folder, choose <strong>Assistant → Look through</strong>, or look through everything
          here. It can run overnight.
        </EmptyState>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[18rem_1fr]">
          <ul className="flex flex-col gap-2">
            {list.map((r) => (
              <li key={r.id}>
                <ReviewListItem review={r} active={r.id === selected} onClick={() => navigate(`/assistant/${r.id}`)} />
              </li>
            ))}
          </ul>
          <div className="min-w-0">{selected ? <ReviewDetail key={selected} id={selected} /> : null}</div>
        </div>
      )}
      {starting && <StartReviewDialog scopeLabel="all documents" onClose={() => setStarting(false)} />}
    </div>
  );
}

function StateBadge({ review }: { review: Summary }) {
  if (review.state === "pending")
    return (
      <Badge variant="muted">
        <Moon /> {review.start ? `Starts ${time(review.start)}` : "Waiting"}
      </Badge>
    );
  if (review.state === "running")
    return (
      <Badge variant="default">
        <Spinner className="size-3" /> Working
      </Badge>
    );
  if (review.state === "failed") return <Badge variant="danger">Failed</Badge>;
  if (review.state === "cancelled") return <Badge variant="muted">Cancelled</Badge>;
  return review.open_findings ? (
    <Badge variant={review.read ? "warning" : "success"}>{review.open_findings} to decide</Badge>
  ) : (
    <Badge variant="muted">
      <Check /> All decided
    </Badge>
  );
}

function ReviewListItem({ review: r, active, onClick }: { review: Summary; active: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex w-full flex-col gap-1 rounded-lg border bg-card p-3 text-left text-sm transition-colors hover:bg-muted/50",
        active && "border-primary ring-1 ring-primary",
      )}
    >
      <div className="flex items-center gap-2">
        {!r.read && r.state === "done" && <span className="size-2 rounded-full bg-primary" aria-label="New" />}
        <span className="flex-1 truncate font-medium">{r.instruction || (r.ai ? "Look through" : "Pages and duplicates")}</span>
      </div>
      <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
        <StateBadge review={r} />
        <span>
          {r.documents} documents · {formatDateTime(r.created_at)}
        </span>
      </div>
    </button>
  );
}

function ReviewDetail({ id }: { id: number }) {
  const qc = useQueryClient();
  const review = useQuery({
    queryKey: ["review", id],
    queryFn: () => call(() => client.GET("/api/v1/assist/reviews/{review_id}", { params: { path: { review_id: id } } })),
    refetchInterval: (q) => (["pending", "running"].includes(q.state.data?.state ?? "") ? 3000 : false),
  });
  const r = review.data;
  useEffect(() => {
    if (r && r.state === "done" && !r.read) {
      void call(() => client.POST("/api/v1/assist/reviews/{review_id}/read", { params: { path: { review_id: id } } })).then(
        () => qc.invalidateQueries({ queryKey: reviewsKey }),
      );
    }
  }, [r, id, qc]);
  const stop = useMutation({
    mutationFn: () => call(() => client.POST("/api/v1/assist/reviews/{review_id}/stop", { params: { path: { review_id: id } } })),
    onSuccess: () => {
      toast.success("Stopping: the report is written with what there is");
      void review.refetch();
      void qc.invalidateQueries({ queryKey: reviewsKey });
    },
  });
  const navigate = useNavigate();
  const remove = useMutation({
    mutationFn: () => call(() => client.DELETE("/api/v1/assist/reviews/{review_id}", { params: { path: { review_id: id } } })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: reviewsKey });
      navigate("/assistant");
    },
    onError: (e) => toast.error(e.message),
  });

  if (review.isPending) return <Spinner />;
  if (review.error || !r) return <ErrorNote error={review.error} />;
  const busy = r.state === "pending" || r.state === "running";

  return (
    <div className="flex flex-col gap-4">
      <Card className="p-4">
        <div className="flex flex-wrap items-start gap-3">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-lg font-semibold">{r.instruction || (r.ai ? "Look through" : "Pages and duplicates")}</h2>
              <StateBadge review={r} />
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              {r.documents} documents · {r.ai ? <><Bot className="inline size-3" /> {r.model || "the model from Settings"}{r.think && ", thinking first"}</> : <><Cpu className="inline size-3" /> without AI</>}
              {r.started_at && ` · started ${formatDateTime(r.started_at)}`}
              {r.finished_at && ` · finished ${formatDateTime(r.finished_at)}`}
              {busy && r.until && ` · report by ${time(r.until)}`}
            </p>
          </div>
          {busy ? (
            <Button variant="outline" size="sm" onClick={() => stop.mutate()} disabled={stop.isPending}>
              <Square /> {r.state === "pending" ? "Cancel" : "Stop and report"}
            </Button>
          ) : (
            <Button variant="ghost" size="sm" onClick={() => remove.mutate()} title="Delete this report (changes you applied stay)">
              <Trash2 />
            </Button>
          )}
        </div>
        {r.state === "running" && <Progress review={r} />}
        {r.state === "failed" && <p className="mt-3 text-sm text-destructive">{r.error}</p>}
        {r.stopped && <p className="mt-3 text-sm text-amber-700 dark:text-amber-300">{r.stopped}: the report covers what was done until then.</p>}
        {r.summary && <p className="mt-3 text-sm leading-relaxed">{r.summary}</p>}
        {r.next_steps.length > 0 && (
          <ol className="mt-2 list-decimal space-y-0.5 pl-5 text-sm">
            {r.next_steps.map((s) => (
              <li key={s}>{s}</li>
            ))}
          </ol>
        )}
      </Card>
      {(r.state === "done" || (r.state === "failed" && r.findings.length > 0)) && <Findings review={r} onChanged={() => void review.refetch()} />}
      <Journal review={r} open={busy || r.state === "failed"} />
    </div>
  );
}

function Progress({ review: r }: { review: Review }) {
  const pct = r.total ? Math.min(100, Math.round((r.done / r.total) * 100)) : 0;
  return (
    <div className="mt-3 flex flex-col gap-1.5 text-sm">
      <div className="flex justify-between gap-2 text-xs text-muted-foreground">
        <span className="truncate">{r.step || "Starting…"}</span>
        <span className="shrink-0">
          {r.done} of ~{r.total} questions
        </span>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-muted">
        <div className="h-full bg-primary transition-all" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

function Journal({ review: r, open: initiallyOpen }: { review: Review; open: boolean }) {
  const [open, setOpen] = useState(initiallyOpen);
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (open) end.current?.scrollIntoView({ block: "nearest" });
  }, [open, r.journal.length]);
  if (!r.journal.length) return null;
  return (
    <section className="rounded-lg border bg-card">
      <button type="button" aria-expanded={open} className="flex w-full items-center gap-2 px-4 py-2 text-sm font-medium" onClick={() => setOpen(!open)}>
        <ChevronRight className={cn("size-4 transition-transform", open && "rotate-90")} />
        <NotebookPen className="size-4" /> What the assistant did ({r.journal.length})
      </button>
      {open && (
        <div className="max-h-96 overflow-y-auto border-t px-4 py-2 font-mono text-xs">
          {r.journal.map((j, i) => (
            <div key={i} className="flex gap-3 py-0.5">
              <span className="shrink-0 text-muted-foreground">{time(j.at)}</span>
              <span className="min-w-0 break-words">{j.text}</span>
            </div>
          ))}
          <div ref={end} />
        </div>
      )}
    </section>
  );
}

// --- Findings ------------------------------------------------------------------------------------

const PAGES = new Set(["duplicate", "duplicate_pages", "split", "split_inside", "empty_pages", "missing_pages"]);
const KIND_ICONS: Record<string, typeof Copy> = {
  duplicate: Copy,
  duplicate_pages: Copy,
  split: Layers,
  split_inside: Scissors,
  empty_pages: FileWarning,
  missing_pages: FileWarning,
  names: PencilLine,
  fix: PencilLine,
};

type View = "open" | "decided" | "set-aside";

function Findings({ review, onChanged }: { review: Review; onChanged: () => void }) {
  const [view, setView] = useState<View>("open");
  const [category, setCategory] = useState<"all" | "pages" | "details" | "notes">("all");
  const category_of = (f: Finding) =>
    PAGES.has(f.kind) ? "pages" : f.changes.length ? "details" : "notes";
  const groups = useMemo(() => {
    const by: Record<View, Finding[]> = { open: [], decided: [], "set-aside": [] };
    for (const f of review.findings) {
      if (f.decision) by.decided.push(f);
      else if (f.verdict === "drop") by["set-aside"].push(f);
      else by.open.push(f);
    }
    return by;
  }, [review.findings]);
  const shown = groups[view].filter((f) => category === "all" || category_of(f) === category);
  const counts = (c: typeof category) => groups[view].filter((f) => c === "all" || category_of(f) === c).length;

  if (!review.findings.length)
    return <EmptyState icon={<ListChecks />} title="Nothing to do">The assistant found nothing that needs you.</EmptyState>;
  return (
    <section className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        {(
          [
            ["open", "To decide"],
            ["decided", "Decided"],
            ["set-aside", "Set aside by the assistant"],
          ] as const
        ).map(([v, label]) => (
          <Button key={v} size="sm" variant={view === v ? "default" : "outline"} onClick={() => setView(v)}>
            {label} ({groups[v].length})
          </Button>
        ))}
        <span className="mx-1 h-5 w-px bg-border" />
        {(
          [
            ["all", "All"],
            ["pages", "Pages & duplicates"],
            ["details", "Names & details"],
            ["notes", "Notes"],
          ] as const
        ).map(([c, label]) => (
          <button
            key={c}
            type="button"
            onClick={() => setCategory(c)}
            className={cn(
              "rounded-full px-2.5 py-1 text-xs",
              category === c ? "bg-accent font-medium text-accent-foreground" : "text-muted-foreground hover:bg-muted",
            )}
          >
            {label} {counts(c) > 0 && `· ${counts(c)}`}
          </button>
        ))}
      </div>
      {view === "set-aside" && (
        <p className="text-xs text-muted-foreground">
          The code found these, but arguing about them the assistant decided against them. Look if you disagree.
        </p>
      )}
      {!shown.length && <p className="py-6 text-center text-sm text-muted-foreground">Nothing here.</p>}
      {shown.map((f) => (
        <FindingCard key={f.id} review={review} finding={f} onChanged={onChanged} />
      ))}
    </section>
  );
}

function certainty(f: Finding) {
  const variant = f.confidence === "high" ? "success" : f.confidence === "medium" ? "default" : "outline";
  return <Badge variant={variant}>{f.confidence === "high" ? "Clear" : f.confidence === "medium" ? "Likely" : "Possible"}</Badge>;
}

function FindingCard({ review, finding: f, onChanged }: { review: Review; finding: Finding; onChanged: () => void }) {
  const qc = useQueryClient();
  const invalidate = useInvalidateDocuments();
  const [editing, setEditing] = useState(false);
  const [debate, setDebate] = useState(false);
  const origin = { task: review.id, finding: f.id };
  const stale = !f.decision && f.documents.some((d) => d.trashed);
  const Icon = KIND_ICONS[f.kind] ?? NotebookPen;

  const decide = useMutation({
    mutationFn: (decision: "applied" | "dismissed" | "open") =>
      call(() =>
        client.POST("/api/v1/assist/reviews/{review_id}/findings/{finding_id}", {
          params: { path: { review_id: review.id, finding_id: f.id } },
          body: { decision },
        }),
      ),
    onSuccess: () => {
      onChanged();
      void qc.invalidateQueries({ queryKey: reviewsKey });
    },
  });
  const done = (message: string, alteration?: Schemas["AlterationOut"]) => {
    invalidate();
    decide.mutate("applied");
    toast.success(message, alteration && {
      description: "The originals are untouched.",
      action: { label: "Undo", onClick: () => void undoAlteration(alteration.id, () => (invalidate(), decide.mutate("open"))) },
      duration: 10_000,
    });
  };
  const compose = useMutation({
    mutationFn: () => {
      const c = f.compose as NonNullable<Finding["compose"]>;
      return c.outputs.length
        ? call(() =>
            client.POST("/api/v1/alterations/compose", {
              body: {
                sources: c.sources,
                outputs: c.outputs.map((o) => ({ title: o.title, pages: o.pages })),
                origin,
              },
            }),
          )
        : call(() => client.POST("/api/v1/alterations/trash", { body: { ids: c.sources, origin } }));
    },
    onSuccess: (a) => done(a.summary, a),
    onError: (e) => toast.error(e.message),
  });

  // For the editor: the suggestion's outputs, and every other document involved kept as it is.
  const editor = useMemo(() => {
    if (!f.compose) return null;
    const sources = new Set(f.compose.sources);
    const others = f.documents.filter((d) => !sources.has(d.id) && !d.trashed);
    return {
      sourceIds: [...f.compose.sources, ...others.map((d) => d.id)],
      suggestion: [
        ...f.compose.outputs.map((o) => ({ title: o.title, pages: o.pages.map((p) => [p.document, p.page] as [string, number]) })),
        ...others.map((d) => ({ title: "", pages: Array.from({ length: d.page_count }, (_, i) => [d.id, i + 1] as [string, number]) })),
      ],
    };
  }, [f]);

  return (
    <Card className={cn("p-4", f.decision && "opacity-70")}>
      <div className="flex items-start gap-3">
        <Icon className="mt-0.5 size-5 shrink-0 text-muted-foreground" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-semibold">{f.title}</h3>
            {certainty(f)}
            {f.source === "model" && (
              <Badge variant="outline">
                <Bot /> Proposed by the AI model
              </Badge>
            )}
            {f.verdict === "unsure" && <Badge variant="warning">The assistant is unsure</Badge>}
            {f.decision === "applied" && <Badge variant="success"><Check /> Applied</Badge>}
            {f.decision === "dismissed" && <Badge variant="muted"><X /> Dismissed</Badge>}
          </div>
          <p className="mt-1 text-sm">{f.text}</p>

          <ul className="mt-3 flex flex-wrap gap-3">
            {f.documents.map((d) => (
              <li key={d.id} className="flex w-60 gap-2 rounded-md border p-2 text-xs">
                <img
                  src={pageImage(d.id, 1)}
                  alt=""
                  loading="lazy"
                  className="h-16 w-12 shrink-0 rounded border bg-white object-contain"
                  onError={(e) => (e.currentTarget.style.visibility = "hidden")}
                />
                <div className="min-w-0">
                  <Link to={`/documents/${d.id}`} target="_blank" className="line-clamp-2 font-medium hover:underline">
                    {d.title}
                  </Link>
                  <div className="text-muted-foreground">
                    {[d.correspondent, d.document_date && formatDate(d.document_date), `${d.page_count} p.`]
                      .filter(Boolean)
                      .join(" · ")}
                  </div>
                  {d.what && <div className="mt-0.5 line-clamp-2 text-muted-foreground">{d.what}</div>}
                  {d.trashed && <div className="mt-0.5 text-amber-700 dark:text-amber-300">In the trash now</div>}
                </div>
              </li>
            ))}
          </ul>

          {f.debate.length > 0 && (
            <div className="mt-3">
              <button type="button" className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground" onClick={() => setDebate(!debate)}>
                <ChevronRight className={cn("size-3.5 transition-transform", debate && "rotate-90")} />
                <Scale className="size-3.5" /> How the assistant argued about it
              </button>
              {debate && (
                <dl className="mt-2 flex flex-col gap-1.5 border-l-2 pl-3 text-xs">
                  {f.debate.map((d, i) => (
                    <div key={i}>
                      <dt className="font-medium">{d.role === "against" ? "Against" : d.role === "for" ? "For" : "Verdict"}</dt>
                      <dd className="text-muted-foreground">{d.text}</dd>
                    </div>
                  ))}
                </dl>
              )}
            </div>
          )}

          {stale && (
            <p className="mt-3 flex items-center gap-2 text-xs text-amber-700 dark:text-amber-300">
              <AlertTriangle className="size-3.5" /> A document changed since the review; this suggestion may be out of date.
            </p>
          )}

          {!f.decision && f.changes.length > 0 && <Changes finding={f} origin={origin} onApplied={(n) => done(`Changed ${n} document${n === 1 ? "" : "s"}`)} />}

          <div className="mt-3 flex flex-wrap gap-2">
            {!f.decision && f.compose && !stale && (
              <>
                <Button size="sm" onClick={() => setEditing(true)}>
                  <Layers /> Review the pages…
                </Button>
                <Button size="sm" variant="outline" onClick={() => compose.mutate()} disabled={compose.isPending}>
                  {compose.isPending ? <Spinner /> : <ArrowRight />}
                  {f.compose.outputs.length ? "Apply as suggested" : "Move to the trash"}
                </Button>
              </>
            )}
            {!f.decision ? (
              <Button size="sm" variant="ghost" onClick={() => decide.mutate("dismissed")}>
                {f.compose || f.changes.length ? <><X /> Dismiss</> : <><Check /> Got it</>}
              </Button>
            ) : (
              <Button size="sm" variant="ghost" onClick={() => decide.mutate("open")}>
                <RotateCcw /> Reopen
              </Button>
            )}
          </div>
        </div>
      </div>
      {editing && editor && (
        <PageEditor
          sourceIds={editor.sourceIds}
          suggestion={editor.suggestion}
          matches={f.matches.map((m) => [m.a.document, m.a.page, m.b.document, m.b.page] as [string, number, string, number])}
          origin={origin}
          heading={f.title}
          explanation={`${f.text} Change anything before applying — pages marked red are the same as another page.`}
          onApplied={(a) => done(a.summary)}
          onClose={() => setEditing(false)}
        />
      )}
    </Card>
  );
}

function Changes({
  finding,
  origin,
  onApplied,
}: {
  finding: Finding;
  origin: { task: number; finding: string };
  onApplied: (n: number) => void;
}) {
  const [ticked, setTicked] = useState<Set<Key>>(() => initialTicks(finding.changes));
  const [edits, setEdits] = useState<Record<Key, string>>({});
  const apply = useMutation({
    mutationFn: () => applyChanges(finding.changes, ticked, edits, origin),
    onSuccess: onApplied,
    onError: (e) => toast.error(e.message),
  });
  return (
    <div className="mt-3 flex flex-col gap-2">
      {finding.changes.map((g, gi) => (
        <ChangeCard
          key={`${g.label}-${gi}`}
          group={g}
          index={gi}
          ticked={ticked}
          onTick={(key, on) =>
            setTicked((all) => {
              const next = new Set(all);
              if (on) next.add(key);
              else next.delete(key);
              return next;
            })
          }
          edits={edits}
          onEdit={(key, value) => setEdits((e) => ({ ...e, [key]: value }))}
        />
      ))}
      <div>
        <Button size="sm" disabled={!ticked.size || apply.isPending} onClick={() => apply.mutate()}>
          {apply.isPending ? <Spinner /> : <ArrowRight />} Apply {ticked.size} change{ticked.size === 1 ? "" : "s"}
        </Button>
      </div>
    </div>
  );
}
