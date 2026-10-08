import * as Popover from "@radix-ui/react-popover";
import { CheckCircle2, ChevronUp, CircleAlert, Clock3, Loader2, Rows3 } from "lucide-react";
import { Link } from "react-router";
import type { Schemas } from "@/api/client";
import { useProcessingQueue } from "@/api/queries";
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
  analyze: "Detecting fields",
  store: "Storing",
  index: "Indexing",
  done: "Complete",
};

function QueueRow({ item, queued = false }: { item: QueueItem; queued?: boolean }) {
  const running = item.state === "running";
  const failed = item.state === "failed";
  return (
    <Link
      to={`/documents/${item.document_id}`}
      className="block rounded-md border px-3 py-2 transition-colors hover:bg-muted"
    >
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
        <span className="min-w-0 flex-1 truncate text-sm font-medium">{item.title}</span>
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
  queued = false,
  empty,
}: {
  title: string;
  items: QueueItem[];
  queued?: boolean;
  empty: string;
}) {
  return (
    <section>
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</h3>
        <span className="text-xs tabular-nums text-muted-foreground">{items.length}</span>
      </div>
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
  const queued = queue.data?.queued.length ?? 0;
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
                  <QueueSection title="Queued" items={queue.data.queued} queued empty="No documents are waiting." />
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
