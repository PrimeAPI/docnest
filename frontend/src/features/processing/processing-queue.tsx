import * as Popover from "@radix-ui/react-popover";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, ChevronUp, CircleAlert, Clock3, Loader2, Rows3, X } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { call, client, type Schemas } from "@/api/client";
import { keys, useInvalidateDocuments, useProcessingQueue } from "@/api/queries";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/misc";
import { cn, formatDateTime, formatDuration } from "@/lib/utils";

type QueueItem = Schemas["ProcessingQueueItem"];

const STAGES: Record<string, string> = {
  received: "Received",
  assemble: "Building PDF",
  validate: "Validating",
  enhance: "Improving scan",
  ocr: "Reading document",
  analyze: "AI analysis / detecting fields",
  store: "Storing",
  index: "Indexing",
  done: "Complete",
};

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
        <span className="truncate">
          {queued ? "Waiting" : (STAGES[item.stage] ?? item.stage)} · {item.backend === "docling" ? "Docling" : "OCRmyPDF"}
          {item.attempts > 1 ? ` · attempt ${item.attempts}` : ""}
        </span>
        <span className="shrink-0">
          {item.finished_at ? formatDateTime(item.finished_at) : queued ? formatDateTime(item.created_at) : "Now"}
        </span>
      </div>
      {failed && item.error && <p className="mt-1 line-clamp-2 pl-6 text-xs text-destructive">{item.error}</p>}
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

export function ProcessingQueueWidget() {
  const queue = useProcessingQueue();
  const running = queue.data?.running.length ?? 0;
  const queued = queue.data?.queued_total ?? 0;
  const active = running + queued;

  return (
    <div className="fixed bottom-3 left-3 z-40">
      <Popover.Root>
        <Popover.Trigger asChild>
          <Button
            variant="outline"
            className="gap-2 bg-card shadow-lg"
            aria-label={`Processing queue: ${running} running, ${queued} queued`}
          >
            <Rows3 className={cn("size-4", running > 0 && "text-primary")} />
            <span>{active > 0 ? `${running} running · ${queued} queued` : "Processing queue"}</span>
            <ChevronUp className="size-3.5 text-muted-foreground" />
          </Button>
        </Popover.Trigger>
        <Popover.Portal>
          <Popover.Content
            side="top"
            align="start"
            sideOffset={8}
            className="z-50 flex max-h-[min(70vh,680px)] w-[calc(100vw-1.5rem)] max-w-md flex-col overflow-hidden rounded-lg border bg-card shadow-2xl outline-none"
          >
            <div className="border-b px-4 py-3">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <h2 className="font-semibold">Document processing</h2>
                  <p className="text-xs text-muted-foreground">
                    Up to {queue.data?.concurrency ?? "—"} document{queue.data?.concurrency === 1 ? "" : "s"} at once
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
