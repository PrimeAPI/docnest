import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowRight, Bot, Layers, PencilLine, RotateCcw, Trash2, Undo2, User } from "lucide-react";
import { Link } from "react-router";
import { toast } from "sonner";
import { call, client, type Schemas } from "@/api/client";
import { useInvalidateDocuments } from "@/api/queries";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ErrorNote, Spinner } from "@/components/ui/misc";
import { cn, formatDateTime } from "@/lib/utils";

type Entry = Schemas["AlterationOut"];

export const historyKey = (id: string) => ["alterations", id] as const;

export function useHistory(id: string, enabled = true) {
  return useQuery({
    queryKey: historyKey(id),
    enabled,
    queryFn: () => call(() => client.GET("/api/v1/alterations/document/{doc_id}", { params: { path: { doc_id: id } } })),
    retry: false,
  });
}

const ICONS = { compose: Layers, trash: Trash2, restore: RotateCcw, edit: PencilLine } as const;

/** Every change to a document — its details, its pages, the trash — newest first, with undo. */
export function AlterationHistory({ id }: { id: string }) {
  const history = useHistory(id);
  const invalidate = useInvalidateDocuments();
  const undo = useMutation({
    mutationFn: (entry: Entry) =>
      call(() => client.POST("/api/v1/alterations/{alteration_id}/undo", { params: { path: { alteration_id: entry.id } } })),
    onSuccess: () => {
      toast.success("Undone");
      invalidate();
      void history.refetch();
    },
    onError: (e) => toast.error(e.message),
  });
  if (history.isPending) return <Spinner />;
  if (history.error) return <ErrorNote error={history.error} />;
  const entries = history.data?.entries ?? [];
  if (!entries.length)
    return <p className="text-xs text-muted-foreground">No changes yet. Every edit and page change will be listed here.</p>;
  return (
    <ol className="flex flex-col gap-2">
      {entries.map((e) => (
        <HistoryEntry key={e.id} entry={e} self={id} onUndo={() => undo.mutate(e)} undoing={undo.isPending} />
      ))}
    </ol>
  );
}

function HistoryEntry({
  entry: e,
  self,
  onUndo,
  undoing,
}: {
  entry: Entry;
  self: string;
  onUndo: () => void;
  undoing: boolean;
}) {
  const Icon = ICONS[e.kind as keyof typeof ICONS] ?? PencilLine;
  const others = (refs: Entry["sources"]) => refs.filter((r) => r.id !== self);
  return (
    <li className={cn("rounded-md border px-3 py-2 text-sm", e.undone_at && "opacity-60")}>
      <div className="flex items-start gap-2">
        <Icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-medium">{e.summary}</span>
            {e.actor === "assistant" ? (
              <Badge variant="outline" title="A suggestion of the assistant that you applied">
                <Bot /> Assistant
              </Badge>
            ) : (
              <Badge variant="muted">
                <User /> You
              </Badge>
            )}
            {e.undone_at && <Badge variant="warning">Undone</Badge>}
          </div>
          <div className="text-xs text-muted-foreground">
            {formatDateTime(e.created_at)}
            {e.task && (
              <>
                {" · "}
                <Link to={`/assistant/${e.task}`} className="hover:underline">
                  from a review{e.finding && ` (${e.finding})`}
                </Link>
              </>
            )}
          </div>
          {e.changes.length > 0 && (
            <dl className="mt-1.5 grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 text-xs">
              {e.changes.map((c) => (
                <div key={c.field} className="contents">
                  <dt className="text-muted-foreground">{c.field}</dt>
                  <dd className="min-w-0 break-words">
                    <span className="text-muted-foreground line-through">{c.old || "—"}</span>{" "}
                    <ArrowRight className="inline size-3" /> {c.new || "—"}
                  </dd>
                </div>
              ))}
            </dl>
          )}
          {e.kind === "compose" && (
            <div className="mt-1.5 flex flex-col gap-0.5 text-xs">
              {others(e.sources).length > 0 && <DocList label="From" refs={others(e.sources)} />}
              {others(e.results).length > 0 && <DocList label="Made" refs={others(e.results)} />}
            </div>
          )}
        </div>
        {e.can_undo && (
          <Button size="sm" variant="ghost" onClick={onUndo} disabled={undoing} title="Undo: as it was before">
            <Undo2 /> Undo
          </Button>
        )}
      </div>
    </li>
  );
}

function DocList({ label, refs }: { label: string; refs: Entry["sources"] }) {
  return (
    <div className="flex flex-wrap gap-x-1">
      <span className="text-muted-foreground">{label}:</span>
      {refs.map((r, i) => (
        <span key={r.id}>
          <Link to={`/documents/${r.id}`} className="hover:underline">
            {r.title}
          </Link>
          {r.trashed && <span className="text-muted-foreground"> (in the trash)</span>}
          {i < refs.length - 1 && ","}
        </span>
      ))}
    </div>
  );
}

/** Shown instead of a document that is in the trash: why, what replaced it, and how to get it back. */
export function TrashedDocument({ id }: { id: string }) {
  const history = useHistory(id);
  const invalidate = useInvalidateDocuments();
  const restore = useMutation({
    mutationFn: () => call(() => client.POST("/api/v1/alterations/restore", { body: { ids: [id] } })),
    onSuccess: () => {
      toast.success("Restored");
      invalidate();
      void history.refetch();
      window.location.reload();
    },
    onError: (e) => toast.error(e.message),
  });
  if (history.isPending) return <Spinner className="size-6" />;
  const h = history.data;
  if (!h) return null;
  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-4 py-6">
      <div className="rounded-lg border bg-card p-5">
        <div className="flex items-start gap-3">
          <Trash2 className="mt-1 size-5 text-muted-foreground" />
          <div className="min-w-0 flex-1">
            <h1 className="text-lg font-semibold">{h.title || "Untitled"} is in the trash</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              Its original file is kept as it was{h.deleted_at && ` (since ${formatDateTime(h.deleted_at)})`}.
              {h.replaced_by.length > 0 && " Its pages are now in:"}
            </p>
            {h.replaced_by.length > 0 && (
              <ul className="mt-2 flex flex-col gap-1 text-sm">
                {h.replaced_by.map((r) => (
                  <li key={r.id}>
                    <Link to={`/documents/${r.id}`} className="font-medium hover:underline">
                      {r.title}
                    </Link>
                    {r.trashed && <span className="text-muted-foreground"> (also in the trash)</span>}
                  </li>
                ))}
              </ul>
            )}
            <div className="mt-4 flex flex-wrap gap-2">
              <Button onClick={() => restore.mutate()} disabled={restore.isPending}>
                <RotateCcw /> Restore
              </Button>
              <Button variant="outline" asChild>
                <a href={`/api/v1/documents/${id}/file?variant=original&download=true`}>Download the original</a>
              </Button>
              <Button variant="ghost" asChild>
                <Link to="/trash">Open the trash</Link>
              </Button>
            </div>
          </div>
        </div>
      </div>
      <section>
        <h2 className="mb-2 text-sm font-semibold">History</h2>
        <AlterationHistory id={id} />
      </section>
    </div>
  );
}
