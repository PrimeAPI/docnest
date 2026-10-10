import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, ChevronRight, FolderTree, PencilLine, Sparkles, Telescope, Undo2, Wand2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { call, client, type Schemas } from "@/api/client";
import { keys, useDocuments, useInvalidateDocuments } from "@/api/queries";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input, Textarea } from "@/components/ui/input";
import { Spinner } from "@/components/ui/misc";
import { DocItems, useViewMode, ViewSwitch } from "@/features/documents/document-views";
import { SuggestFilingDialog } from "@/features/folders/filing-suggestions";
import { StartReviewDialog } from "@/features/assist/start-review";
import { cn, formatDate } from "@/lib/utils";

type Task = Schemas["AssistTaskOut"];
type Group = Schemas["AssistGroupOut"];
type Operation = "filing" | "rename" | "custom" | "review";

const MAX_INSTRUCTION = 300;
const MAX_DOCUMENTS = 100;

/**
 * "Assistant": the AI model proposes changes for some documents — this one, the documents
 * of a folder, or a selection — and the user applies the ones they want. It never changes
 * anything by itself.
 */
export function AssistantButton({
  ids,
  folderId,
  scope,
  size = "sm",
  onDone,
}: {
  ids?: string[];
  folderId?: number;
  scope: string; // "this document", "the documents in “Work”", "3 selected documents"
  size?: "sm" | "default";
  onDone?: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button size={size} variant="outline" onClick={() => setOpen(true)} title="Ask the AI model for changes">
        <Wand2 /> Assistant
      </Button>
      {open && (
        <AssistantDialog
          ids={ids}
          folderId={folderId}
          scope={scope}
          onClose={() => setOpen(false)}
          onDone={onDone}
        />
      )}
    </>
  );
}

function AssistantDialog({
  ids,
  folderId,
  scope,
  onClose,
  onDone,
}: {
  ids?: string[];
  folderId?: number;
  scope: string;
  onClose: () => void;
  onDone?: () => void;
}) {
  // A folder means the documents lying directly in it, like "Select all in this folder".
  const folderIds = useQuery({
    queryKey: ["documents", "ids", folderId],
    enabled: folderId !== undefined && !ids,
    queryFn: () =>
      call(() =>
        client.GET("/api/v1/documents/ids", { params: { query: { folder: [folderId ?? 0], sort: "-uploaded" } } }),
      ),
  });
  const all = useMemo(() => (ids ?? folderIds.data?.ids ?? []).slice(0, MAX_DOCUMENTS), [ids, folderIds.data]);
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  const chosen = all.filter((id) => !excluded.has(id));

  const [operation, setOperation] = useState<Operation>("rename");
  const [instruction, setInstruction] = useState("");
  const [taskId, setTaskId] = useState<number | null>(null);
  const [filing, setFiling] = useState(false);
  const [reviewing, setReviewing] = useState(false);

  const start = useMutation({
    mutationFn: () =>
      call(() =>
        client.POST("/api/v1/assist/tasks", {
          body: { ids: chosen, operation: operation as "rename" | "custom", instruction },
        }),
      ),
    onSuccess: (t) => setTaskId(t.id),
    onError: (e) => toast.error(e.message),
  });
  const task = useQuery({
    queryKey: ["assist-task", taskId],
    enabled: taskId !== null,
    queryFn: () => call(() => client.GET("/api/v1/assist/tasks/{task_id}", { params: { path: { task_id: taskId ?? 0 } } })),
    refetchInterval: (q) => (["pending", "running"].includes(q.state.data?.state ?? "pending") ? 1000 : false),
  });
  const cancel = useMutation({
    mutationFn: () =>
      call(() => client.POST("/api/v1/assist/tasks/{task_id}/cancel", { params: { path: { task_id: taskId ?? 0 } } })),
    onSuccess: () => setTaskId(null),
  });

  if (reviewing) {
    return <StartReviewDialog ids={chosen} scopeLabel={scope} onClose={onClose} />;
  }
  if (filing) {
    return (
      <SuggestFilingDialog
        ids={chosen}
        onClose={onClose}
        onDone={() => {
          onDone?.();
          onClose();
        }}
      />
    );
  }

  const next = () => (operation === "filing" ? setFiling(true) : operation === "review" ? setReviewing(true) : start.mutate());
  const t = task.data;
  const phase = taskId === null ? "ask" : t && ["done", "failed", "cancelled"].includes(t.state) ? "review" : "working";
  const loading = folderId !== undefined && !ids && folderIds.isPending;
  const canStart = chosen.length > 0 && (operation !== "custom" || instruction.trim().length > 0);

  return (
    <Dialog open onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="flex h-[90vh] max-w-3xl flex-col gap-0 overflow-hidden p-0">
        <DialogHeader className="border-b px-6 py-4 pr-12">
          <DialogTitle className="flex items-center gap-2">
            <Wand2 className="size-5" /> Assistant
          </DialogTitle>
          <DialogDescription>
            The AI model suggests changes for {scope}. Nothing changes until you apply them.
          </DialogDescription>
        </DialogHeader>

        <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden px-6 py-4">
          {loading ? (
            <Spinner />
          ) : phase === "ask" ? (
            <div className="flex flex-col gap-5">
              <DocumentPicker all={all} excluded={excluded} onChange={setExcluded} />
              <OperationPicker
                operation={operation}
                onOperation={setOperation}
                instruction={instruction}
                onInstruction={setInstruction}
                onSubmit={() => canStart && next()}
              />
            </div>
          ) : phase === "working" ? (
            <Working task={t} />
          ) : t ? (
            <Review task={t} onApplied={() => (onDone?.(), onClose())} />
          ) : null}
        </div>

        <DialogFooter className="border-t px-6 py-3">
          {phase === "ask" && (
            <>
              <Button variant="outline" onClick={onClose}>
                Cancel
              </Button>
              <Button
                disabled={!canStart || start.isPending}
                onClick={next}
              >
                <Sparkles />{" "}
                {operation === "filing" ? "Suggest filing" : operation === "review" ? "Next" : "Ask the AI model"} (
                {chosen.length})
              </Button>
            </>
          )}
          {phase === "working" && (
            <Button variant="outline" onClick={() => cancel.mutate()}>
              Stop
            </Button>
          )}
          {phase === "review" && (
            <Button variant="outline" onClick={() => setTaskId(null)}>
              <Undo2 /> Change the request
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function DocumentPicker({
  all,
  excluded,
  onChange,
}: {
  all: string[];
  excluded: Set<string>;
  onChange: (s: Set<string>) => void;
}) {
  const [open, setOpen] = useState(all.length <= 5);
  const [view, setView] = useViewMode("dialog");
  const docs = useDocuments({ id: all, page_size: 100, sort: "-date" }, undefined);
  const items = docs.data?.items ?? [];
  const chosen = all.length - excluded.size;
  if (!all.length) {
    return <p className="text-sm text-muted-foreground">There are no documents here.</p>;
  }
  return (
    <section className="rounded-lg border p-3 text-sm">
      <div className="flex items-center gap-2">
        <button type="button" className="flex flex-1 items-center gap-1 text-left font-medium" onClick={() => setOpen(!open)}>
          <ChevronRight className={cn("size-4 transition-transform", open && "rotate-90")} />
          {chosen === all.length ? `${all.length} document${all.length === 1 ? "" : "s"}` : `${chosen} of ${all.length} documents`}
        </button>
        {open && <ViewSwitch mode={view} onChange={setView} />}
      </div>
      {open &&
        (docs.isPending ? (
          <Spinner />
        ) : (
          <DocItems
            className="mt-2"
            mode={view}
            items={items.map((d) => ({
              id: d.id,
              title: d.title,
              detail: [d.correspondent?.name, d.document_date && formatDate(d.document_date)].filter(Boolean).join(" · "),
              lead: (
                <Checkbox
                  checked={!excluded.has(d.id)}
                  onCheckedChange={(v) => {
                    const next = new Set(excluded);
                    if (v === true) next.delete(d.id);
                    else next.add(d.id);
                    onChange(next);
                  }}
                  aria-label={`Include ${d.title}`}
                />
              ),
            }))}
          />
        ))}
    </section>
  );
}

const OPERATIONS: { value: Operation; title: string; text: string; icon: typeof Wand2 }[] = [
  {
    value: "rename",
    title: "Consistent names",
    text: "Documents that belong together get names in one pattern, e.g. “Verdienstabrechnung 2026-03”.",
    icon: PencilLine,
  },
  {
    value: "filing",
    title: "Suggest filing",
    text: "Subfolders for the documents, below the folder they are in.",
    icon: FolderTree,
  },
  {
    value: "custom",
    title: "Custom",
    text: "Tags, sender, type or title, as you describe it.",
    icon: Sparkles,
  },
  {
    value: "review",
    title: "Look through",
    text: "Duplicates, split scans, missing pages, names — for hours if you like. A report with suggestions.",
    icon: Telescope,
  },
];

function OperationPicker({
  operation,
  onOperation,
  instruction,
  onInstruction,
  onSubmit,
}: {
  operation: Operation;
  onOperation: (o: Operation) => void;
  instruction: string;
  onInstruction: (v: string) => void;
  onSubmit: () => void;
}) {
  return (
    <section className="flex flex-col gap-3">
      <div role="radiogroup" aria-label="What should be done" className="grid gap-2 sm:grid-cols-2">
        {OPERATIONS.map(({ value, title, text, icon: Icon }) => (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={operation === value}
            onClick={() => onOperation(value)}
            className={cn(
              "flex flex-col gap-1 rounded-lg border p-3 text-left text-sm transition-colors hover:bg-muted/50",
              operation === value && "border-primary bg-accent/60 ring-1 ring-primary",
            )}
          >
            <span className="flex items-center gap-2 font-medium">
              <Icon className="size-4" /> {title}
            </span>
            <span className="text-xs text-muted-foreground">{text}</span>
          </button>
        ))}
      </div>
      {operation !== "filing" && operation !== "review" && (
        <div className="flex flex-col gap-1">
          <Textarea
            rows={2}
            maxLength={MAX_INSTRUCTION}
            value={instruction}
            onChange={(e) => onInstruction(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && (e.metaKey || e.ctrlKey) && onSubmit()}
            placeholder={
              operation === "rename"
                ? "Optional: a pattern like “Verdienstabrechnung YYYY/MM”, or a wish like “English names”"
                : "e.g. “Die Stadtwerke-Rechnungen bekommen das Tag Energie” or “Absender der Lohnzettel ist ACME GmbH”"
            }
            aria-label="Your request"
          />
          <p className="text-xs text-muted-foreground">
            {operation === "rename"
              ? "Placeholders: YYYY year, MM month, DD day, MMMM month name — filled from each document's date."
              : "The AI model is small: name the detail (tag, sender, type, title), the value, and which documents — by sender, type or title words."}
          </p>
        </div>
      )}
      {operation === "filing" && (
        <p className="text-xs text-muted-foreground">
          Opens the filing suggestions for these documents, where you can add instructions too.
        </p>
      )}
      {operation === "review" && (
        <p className="text-xs text-muted-foreground">
          Next you choose the model, how long it may take and what it should look for.
        </p>
      )}
    </section>
  );
}

function Working({ task }: { task?: Task }) {
  const total = task?.total ?? 0;
  const done = task?.done ?? 0;
  return (
    <div className="flex flex-col items-center gap-3 py-10 text-sm text-muted-foreground">
      <Spinner />
      <p>
        {task?.state === "pending" ? "Waiting for the AI model…" : "The AI model is working…"}
        {total > 0 && ` ${done} of ${total} request${total === 1 ? "" : "s"} answered.`}
      </p>
      {total > 0 && (
        <div className="h-1.5 w-64 overflow-hidden rounded-full bg-muted">
          <div className="h-full bg-primary transition-all" style={{ width: `${(done / total) * 100}%` }} />
        </div>
      )}
      <p className="text-xs">It is a small model on your server; this can take a while.</p>
    </div>
  );
}

export type Key = `${number}:${string}`;
export const keyOf = (group: number, doc: string): Key => `${group}:${doc}`;

/** Apply the ticked changes: one update per document, with everything ticked for it. */
export async function applyChanges(
  groups: Group[],
  ticked: Set<Key>,
  edits: Record<Key, string>,
  origin?: { task: number; finding: string },
): Promise<number> {
  const patches = new Map<string, Partial<Schemas["DocumentPatch"]>>();
  groups.forEach((g, gi) =>
    g.items.forEach((item) => {
      const key = keyOf(gi, item.document.id);
      if (!ticked.has(key)) return;
      const patch: Partial<Schemas["DocumentPatch"]> = patches.get(item.document.id) ?? {};
      const value = edits[key] ?? item.new;
      if (g.field === "title") patch.title = String(value).trim();
      else if (g.field === "sender") patch.correspondent_name = String(value).trim();
      else if (g.field === "document_type") patch.document_type_id = g.document_type_id ?? undefined;
      else {
        patch.tag_ids = item.document.tag_ids;
        patch.tag_names = [...(patch.tag_names ?? []), ...(Array.isArray(value) ? value : [value])];
      }
      patches.set(item.document.id, patch);
    }),
  );
  let applied = 0;
  for (const [id, body] of patches) {
    await call(() =>
      client.PATCH("/api/v1/documents/{doc_id}", {
        params: { path: { doc_id: id } },
        body: { ...body, origin } as Schemas["DocumentPatch"], // the server fills in the rest
      }),
    );
    applied += 1;
  }
  return applied;
}

export function initialTicks(groups: Group[]): Set<Key> {
  return new Set(groups.flatMap((g, gi) => g.items.filter((i) => i.checked).map((i) => keyOf(gi, i.document.id))));
}

function Review({ task, onApplied }: { task: Task; onApplied: () => void }) {
  const qc = useQueryClient();
  const invalidate = useInvalidateDocuments();
  const [ticked, setTicked] = useState<Set<Key>>(new Set());
  const [edits, setEdits] = useState<Record<Key, string>>({});
  useEffect(() => {
    setTicked(initialTicks(task.groups));
    setEdits({});
  }, [task]);

  const count = ticked.size;
  const apply = useMutation({
    mutationFn: () => applyChanges(task.groups, ticked, edits),
    onSuccess: (n) => {
      toast.success(`Changed ${n} document${n === 1 ? "" : "s"}`);
      invalidate();
      qc.invalidateQueries({ queryKey: keys.tags });
      qc.invalidateQueries({ queryKey: keys.correspondents });
      onApplied();
    },
    onError: (e) => {
      toast.error(e.message);
      invalidate();
    },
  });

  if (task.state === "failed") return <p className="text-sm text-destructive">{task.error}</p>;
  if (task.state === "cancelled") return <p className="text-sm text-muted-foreground">Stopped.</p>;
  return (
    <div className="flex flex-col gap-3">
      {task.note && (
        <p className="rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100">
          {task.note}
        </p>
      )}
      {!task.groups.length && !task.note && (
        <p className="py-6 text-center text-sm text-muted-foreground">Nothing to change: the names already fit.</p>
      )}
      {task.groups.map((g, gi) => (
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
      {task.groups.length > 0 && (
        <div className="sticky bottom-0 flex justify-end bg-card pt-2">
          <Button disabled={!count || apply.isPending} onClick={() => apply.mutate()}>
            {apply.isPending ? <Spinner /> : <ArrowRight />} Apply {count} change{count === 1 ? "" : "s"}
          </Button>
        </div>
      )}
    </div>
  );
}

export function ChangeCard({
  group,
  index,
  ticked,
  onTick,
  edits,
  onEdit,
}: {
  group: Group;
  index: number;
  ticked: Set<Key>;
  onTick: (key: Key, on: boolean) => void;
  edits: Record<Key, string>;
  onEdit: (key: Key, value: string) => void;
}) {
  const [showAll, setShowAll] = useState(false);
  const proposed = group.items.filter((i) => i.checked || ticked.has(keyOf(index, i.document.id)));
  const others = group.items.filter((i) => !proposed.includes(i));
  const shown = showAll ? group.items : proposed;
  const on = group.items.filter((i) => ticked.has(keyOf(index, i.document.id))).length;
  return (
    <Card className="min-w-0 p-3">
      <div className="flex items-center gap-2 text-sm font-medium">
        <Checkbox
          checked={on === 0 ? false : on === proposed.length ? true : "indeterminate"}
          onCheckedChange={(v) => proposed.forEach((i) => onTick(keyOf(index, i.document.id), v === true))}
          aria-label={`All of ${group.label}`}
        />
        <span className="min-w-0 flex-1 truncate">{group.label}</span>
        <span className="shrink-0 text-xs font-normal text-muted-foreground">{on} ticked</span>
      </div>
      {shown.length > 0 && (
        <ul className="mt-2 flex flex-col divide-y">
          {shown.map((item) => {
            const key = keyOf(index, item.document.id);
            const value = edits[key] ?? (Array.isArray(item.new) ? item.new.join(", ") : item.new);
            return (
              <li key={key} className="flex flex-col gap-1 py-1.5 text-sm sm:flex-row sm:items-center sm:gap-3">
                <span className="flex min-w-0 flex-1 items-center gap-2">
                  <Checkbox
                    checked={ticked.has(key)}
                    onCheckedChange={(v) => onTick(key, v === true)}
                    aria-label={`Change ${item.document.title}`}
                  />
                  <span className="flex min-w-0 flex-col">
                    <a
                      href={`/documents/${item.document.id}`}
                      target="_blank"
                      rel="noreferrer"
                      className="truncate hover:underline"
                    >
                      {item.document.title}
                    </a>
                    {group.field !== "title" && (
                      <span className="truncate text-xs text-muted-foreground">
                        {group.field === "tags" ? "Tags" : group.field === "sender" ? "Sender" : "Type"}:{" "}
                        {item.old || "—"}
                      </span>
                    )}
                  </span>
                </span>
                {group.field === "title" || group.field === "sender" ? (
                  <span className="flex min-w-0 items-center gap-2 pl-6 sm:w-72 sm:pl-0">
                    <ArrowRight className="size-3.5 shrink-0 text-muted-foreground" />
                    <Input
                      value={value}
                      onChange={(e) => onEdit(key, e.target.value)}
                      className="h-8 min-w-0 flex-1 text-sm"
                      aria-label={`New ${group.field} for ${item.document.title}`}
                    />
                  </span>
                ) : (
                  <span className="flex items-center gap-2 pl-6 text-sm sm:pl-0">
                    <ArrowRight className="size-3.5 text-muted-foreground" /> {group.field === "tags" ? `+ ${value}` : value}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {others.length > 0 && (
        <button
          type="button"
          className="mt-2 flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          onClick={() => setShowAll(!showAll)}
        >
          <ChevronRight className={cn("size-3.5 transition-transform", showAll && "rotate-90")} />
          {showAll ? "Show only the proposed documents" : `${others.length} other selected document${others.length === 1 ? "" : "s"} — tick to include`}
        </button>
      )}
    </Card>
  );
}
