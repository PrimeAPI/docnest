import * as Popover from "@radix-ui/react-popover";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, ChevronRight, CircleAlert, Clock3, Loader2, Rows3, X } from "lucide-react";
import { type ReactNode, useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { call, client, type Schemas } from "@/api/client";
import { keys, useInvalidateDocuments, useProcessingQueue } from "@/api/queries";
import { Spinner } from "@/components/ui/misc";
import { cn, formatDateTime, formatDuration } from "@/lib/utils";

type QueueItem = Schemas["ProcessingQueueItem"];

const STEPS: Record<string, string> = {
  received: "Received",
  assemble: "Building the PDF",
  validate: "Checking the file",
  enhance: "Scan enhancement",
  ocr: "Text recognition",
  analyze: "Analysis",
  store: "Storing",
  index: "Indexing",
  done: "Complete",
};

const PHASES: Record<string, string> = { intake: "Intake", processing: "Processing", index: "Search index" };

function Chip({ children, tone = "muted" }: { children: ReactNode; tone?: "muted" | "primary" }) {
  return (
    <span
      className={cn(
        "shrink-0 rounded px-1.5 py-px text-[11px] font-medium",
        tone === "primary" ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground",
      )}
    >
      {children}
    </span>
  );
}

/** What the job is doing, waiting for, or how it ended — one line, no mixed-up labels. */
function QueueDetail({ item, queued }: { item: QueueItem; queued: boolean }) {
  const phase = PHASES[item.phase] ?? item.phase;
  const processor = item.backend === "docling" ? "Docling" : "OCRmyPDF";
  if (queued)
    return (
      <span className="truncate">
        Waiting for {item.phase === "intake" ? "intake" : item.phase === "processing" ? "processing" : "indexing"}
      </span>
    );
  if (item.state === "running")
    return (
      <span className="flex min-w-0 items-center gap-1.5">
        <Chip tone="primary">{phase}</Chip>
        <span className="truncate">
          {item.step > 0 && `${item.step}/${item.steps} · `}
          {STEPS[item.stage] ?? item.stage}
        </span>
        {item.phase === "processing" && <Chip>{processor}</Chip>}
      </span>
    );
  const ended = {
    done: item.phase === "intake" ? "Taken in" : item.phase === "processing" ? "Processed" : "Indexed",
    failed: "Failed",
    cancelled: "Cancelled",
  }[item.outcome];
  return (
    <span className="flex min-w-0 items-center gap-1.5">
      <Chip>{phase}</Chip>
      <span className="truncate">{ended ?? item.state}</span>
    </span>
  );
}

/** Cancel waiting jobs: the given ones, or every waiting one. Running jobs finish their step. */
function useCancel() {
  const qc = useQueryClient();
  const invalidate = useInvalidateDocuments();
  return useMutation({
    mutationFn: (jobIds?: number[]) =>
      call(() => client.POST("/api/v1/processing/queue/cancel", { body: { job_ids: jobIds ?? null } })),
    onSuccess: (data) => {
      toast.success(
        data.cancelled
          ? `${data.cancelled} job${data.cancelled === 1 ? "" : "s"} cancelled`
          : "Nothing to cancel — it had already started",
      );
      qc.invalidateQueries({ queryKey: keys.processingQueue });
      invalidate();
    },
    onError: (e) => toast.error(e.message),
  });
}

function QueueRow({ item, queued = false }: { item: QueueItem; queued?: boolean }) {
  const running = item.state === "running";
  const failed = item.state === "failed";
  const cancel = useCancel();
  return (
    <Link
      to={`/documents/${item.document_id}`}
      className="group relative block rounded-md border px-3 py-2 transition-colors hover:bg-muted"
    >
      {queued && (
        <button
          type="button"
          title="Cancel — the document stays as it is; reprocess it later if needed"
          aria-label={`Cancel processing of ${item.title}`}
          disabled={cancel.isPending}
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            cancel.mutate([item.id]);
          }}
          className="absolute right-1.5 top-1.5 cursor-pointer rounded p-1 text-muted-foreground hover:bg-background hover:text-destructive"
        >
          <X className="size-3.5" />
        </button>
      )}
      <div className="flex min-w-0 items-center gap-2">
        {running ? (
          <Loader2 className="size-4 shrink-0 animate-spin text-primary" />
        ) : failed ? (
          <CircleAlert className="size-4 shrink-0 text-destructive" />
        ) : item.state === "done" ? (
          <CheckCircle2 className="size-4 shrink-0 text-emerald-600" />
        ) : (
          <Clock3 className="size-4 shrink-0 text-muted-foreground" />
        )}
        <span className={cn("min-w-0 flex-1 truncate text-sm font-medium", queued && "pr-6")}>{item.title}</span>
        {item.duration_seconds !== null && (
          <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
            {formatDuration(item.duration_seconds)}
          </span>
        )}
      </div>
      <div className="mt-1 flex items-center justify-between gap-2 pl-6 text-xs text-muted-foreground">
        <QueueDetail item={item} queued={queued} />
        <span className="shrink-0">
          {item.finished_at ? formatDateTime(item.finished_at) : queued ? formatDateTime(item.created_at) : "Now"}
        </span>
      </div>
      {running && item.steps > 0 && (
        <div className="mt-1.5 ml-6 flex gap-0.5" aria-hidden>
          {Array.from({ length: item.steps }, (_, index) => (
            <span
              key={index}
              className={cn(
                "h-1 flex-1 rounded-full",
                index + 1 < item.step ? "bg-primary" : index + 1 === item.step ? "bg-primary/50" : "bg-muted",
              )}
            />
          ))}
        </div>
      )}
      {failed && item.outcome !== "cancelled" && item.error && (
        <p className="mt-1 line-clamp-2 pl-6 text-xs text-destructive">{item.error}</p>
      )}
    </Link>
  );
}

function QueueSection({
  title,
  items,
  total,
  queued = false,
  empty,
}: {
  title: string;
  items: QueueItem[];
  total?: number;
  queued?: boolean;
  empty: string;
}) {
  const cancel = useCancel();
  const [confirming, setConfirming] = useState(false);
  const count = total ?? items.length;
  return (
    <section>
      <div className="mb-2 flex items-center justify-between gap-2">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</h3>
        <div className="flex items-center gap-2">
          {queued && count > 0 && (
            <button
              type="button"
              disabled={cancel.isPending}
              onClick={() => {
                if (confirming) {
                  cancel.mutate(undefined);
                  setConfirming(false);
                } else setConfirming(true);
              }}
              onBlur={() => setConfirming(false)}
              className={cn(
                "cursor-pointer rounded px-1.5 py-0.5 text-xs",
                confirming ? "bg-destructive text-white" : "text-muted-foreground hover:text-destructive",
              )}
            >
              {confirming ? `Really cancel ${count}?` : "Cancel all"}
            </button>
          )}
          <span className="text-xs tabular-nums text-muted-foreground">{count}</span>
        </div>
      </div>
      {queued && count > items.length && (
        <p className="mb-2 text-xs text-muted-foreground">Showing the next {items.length} of {count}.</p>
      )}
      {items.length ? (
        <div className="space-y-2">
          {items.map((item) => (
            <QueueRow key={item.id} item={item} queued={queued} />
          ))}
        </div>
      ) : (
        <p className="rounded-md border border-dashed px-3 py-2 text-xs text-muted-foreground">{empty}</p>
      )}
    </section>
  );
}

/** The last entry of the sidebar: shows what is being processed and opens the queue beside it. */
export function ProcessingQueueButton() {
  const queue = useProcessingQueue();
  const running = queue.data?.running.length ?? 0;
  const queued = queue.data?.queued_total ?? 0;
  const active = running + queued;

  return (
    <div className="border-t px-3 py-2">
      <Popover.Root>
        <Popover.Trigger asChild>
          <button
            type="button"
            className="flex w-full cursor-pointer items-center gap-3 rounded-md px-3 py-2 text-left text-sm font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground data-[state=open]:bg-accent data-[state=open]:text-accent-foreground"
            aria-label={`Processing queue: ${running} running, ${queued} queued`}
          >
            {running > 0 ? (
              <Loader2 className="size-4 animate-spin text-primary" />
            ) : (
              <Rows3 className="size-4" />
            )}
            <span className="min-w-0 flex-1">
              <span className="block">Processing queue</span>
              {active > 0 && (
                <span className="block text-xs font-normal tabular-nums">
                  {running} running · {queued} queued
                </span>
              )}
            </span>
            <ChevronRight className="size-3.5" />
          </button>
        </Popover.Trigger>
        <Popover.Portal>
          <Popover.Content
            side="right"
            align="end"
            sideOffset={12}
            collisionPadding={12}
            className="z-50 flex max-h-[min(70vh,680px)] w-[calc(100vw-1.5rem)] max-w-md flex-col overflow-hidden rounded-lg border bg-card shadow-2xl outline-none"
          >
            <div className="border-b px-4 py-3">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <h2 className="font-semibold">Document processing</h2>
                  <p className="text-xs text-muted-foreground">
                    Processing {queue.data?.running.filter((i) => i.phase === "processing").length ?? 0} of{" "}
                    {queue.data?.concurrency ?? "—"} at once · intake runs alongside
                  </p>
                </div>
                {queue.isFetching && <Loader2 className="size-4 animate-spin text-muted-foreground" />}
              </div>
            </div>
            <div className="space-y-5 overflow-y-auto p-4">
              {queue.isPending ? (
                <div className="flex justify-center py-8">
                  <Spinner />
                </div>
              ) : queue.error ? (
                <p className="text-sm text-destructive">Could not load the processing queue.</p>
              ) : queue.data ? (
                <>
                  <QueueSection title="Processing now" items={queue.data.running} empty="Nothing is being processed." />
                  <QueueSection
                    title="Queued"
                    items={queue.data.queued}
                    total={queue.data.queued_total}
                    queued
                    empty="No documents are waiting."
                  />
                  <QueueSection title="Recent" items={queue.data.history} empty="No recent processing history." />
                </>
              ) : null}
            </div>
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
    </div>
  );
}
