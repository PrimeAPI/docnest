import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, FolderInput, FolderPlus, Sparkles } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";
import { call, client, type Schemas } from "@/api/client";
import { keys, useInvalidateDocuments } from "@/api/queries";
import { Badge } from "@/components/ui/badge";
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
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/misc";
import { cn, formatDate } from "@/lib/utils";

type Proposal = Schemas["FilingProposalOut"];
type Group = Schemas["FilingGroupOut"];
type Doc = Schemas["FilingDocOut"];

/** "Suggest filing" in the bulk bar: groups the selection into subfolders for review. */
export function SuggestFilingButton({ ids, onDone }: { ids: string[]; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button size="sm" variant="outline" onClick={() => setOpen(true)} title="Suggest subfolders for the selection">
        <Sparkles /> Suggest filing
      </Button>
      {open && <SuggestFilingDialog ids={ids} onClose={() => setOpen(false)} onDone={onDone} />}
    </>
  );
}

type Edit = { include: boolean; skipped: Set<string> };
// A new folder shared by several cards (".../Verdienstabrechnungen/2025" and ".../2026") is renamed once for all.
type Renames = Record<string, string>;
const segmentKey = (group: Group, index: number) => `${group.anchor_id ?? ""}|${group.new.slice(0, index + 1).join("/")}`;
const namesOf = (group: Group, renames: Renames) => group.new.map((n, i) => renames[segmentKey(group, i)] ?? n);

function SuggestFilingDialog({ ids, onClose, onDone }: { ids: string[]; onClose: () => void; onDone: () => void }) {
  const qc = useQueryClient();
  const invalidateDocuments = useInvalidateDocuments();
  const [proposalId, setProposalId] = useState<number | null>(null);
  const [edits, setEdits] = useState<Edit[]>([]);
  const [renames, setRenames] = useState<Renames>({});

  const start = useMutation({
    mutationFn: () => call(() => client.POST("/api/v1/filing/suggestions", { body: { ids } })),
    onSuccess: (p) => setProposalId(p.id),
    onError: (e) => toast.error(e.message),
  });
  // Start once when the dialog opens; the selection is fixed while it is open.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => start.mutate(), []);

  const proposal = useQuery({
    queryKey: ["filing-suggestion", proposalId],
    enabled: proposalId !== null,
    queryFn: () =>
      call(() =>
        client.GET("/api/v1/filing/suggestions/{proposal_id}", { params: { path: { proposal_id: proposalId ?? 0 } } }),
      ),
    refetchInterval: (q) => (q.state.data?.state === "pending" ? 1500 : false),
  });
  const data: Proposal | undefined = proposal.data;
  const ready = data?.state === "done";

  useEffect(() => {
    if (ready && data) setEdits(data.groups.map(() => ({ include: true, skipped: new Set() })));
  }, [ready, data]);

  const moves = useMemo(() => {
    if (!data || !ready) return [];
    return data.groups
      .map((g, i) => ({ g, e: edits[i] }))
      .filter(({ e }) => e?.include)
      .map(({ g, e }) => ({
        ids: g.documents.map((d) => d.id).filter((id) => !e.skipped.has(id)),
        anchor_id: g.anchor_id,
        new: namesOf(g, renames).map((n) => n.trim()),
      }))
      .filter((m) => m.ids.length > 0);
  }, [data, ready, edits, renames]);
  const count = moves.reduce((n, m) => n + m.ids.length, 0);
  const invalidName = moves.some((m) => m.new.some((n) => !n || n.includes("/")));

  const apply = useMutation({
    mutationFn: () => call(() => client.POST("/api/v1/filing/apply", { body: { moves } })),
    onSuccess: (r) => {
      const folders = r.created ? `, created ${r.created} folder${r.created === 1 ? "" : "s"}` : "";
      toast.success(`Filed ${r.moved} document${r.moved === 1 ? "" : "s"}${folders}`);
      qc.invalidateQueries({ queryKey: keys.folders });
      invalidateDocuments();
      onDone();
      onClose();
    },
    onError: (e) => toast.error(e.message),
  });

  const update = (index: number, change: (e: Edit) => Edit) =>
    setEdits((all) => all.map((e, i) => (i === index ? change(e) : e)));

  return (
    <Dialog open onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>Suggested filing</DialogTitle>
          <DialogDescription>
            Documents that belong together, and the subfolder each group would go into. Documents only move deeper into
            their own folder. Nothing changes until you apply.
          </DialogDescription>
        </DialogHeader>

        {start.isError || proposal.isError ? (
          <p className="text-sm text-destructive">Could not get suggestions.</p>
        ) : data?.state === "failed" ? (
          <p className="text-sm text-destructive">The suggestion failed: {data.error}</p>
        ) : !ready ? (
          <div className="flex items-center gap-3 py-8 text-sm text-muted-foreground">
            <Spinner />
            <span>
              Looking at {ids.length} document{ids.length === 1 ? "" : "s"} and your folders… With an AI model, naming
              new folders can take a minute.
            </span>
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {data.note && <p className="rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100">{data.note}</p>}
            {!data.groups.length && (
              <p className="py-6 text-center text-sm text-muted-foreground">
                No suggestions: these documents have too little in common, or nothing that fits a subfolder.
              </p>
            )}
            {data.groups.map((group, index) =>
              edits[index] ? (
                <GroupCard
                  key={`${group.anchor_id}-${group.new.join("/")}`}
                  group={group}
                  edit={edits[index]}
                  names={namesOf(group, renames)}
                  onChange={(change) => update(index, change)}
                  onRename={(i, name) => setRenames((r) => ({ ...r, [segmentKey(group, i)]: name }))}
                />
              ) : null,
            )}
            {data.unassigned.length > 0 && <Unassigned docs={data.unassigned} />}
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={!ready || !count || invalidName || apply.isPending} onClick={() => apply.mutate()}>
            <FolderInput /> File {count} document{count === 1 ? "" : "s"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function GroupCard({
  group,
  edit,
  names,
  onChange,
  onRename,
}: {
  group: Group;
  edit: Edit;
  names: string[];
  onChange: (change: (e: Edit) => Edit) => void;
  onRename: (index: number, name: string) => void;
}) {
  const isNew = group.new.length > 0;
  const chosen = group.documents.filter((d) => !edit.skipped.has(d.id)).length;
  return (
    <Card className={cn("p-3", !edit.include && "opacity-60")}>
      <div className="flex items-start gap-3">
        <Checkbox
          className="mt-1"
          checked={edit.include}
          onCheckedChange={(v) => onChange((e) => ({ ...e, include: v === true }))}
          aria-label="Use this suggestion"
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1 text-sm">
            {isNew ? <FolderPlus className="size-4 text-muted-foreground" /> : <FolderInput className="size-4 text-muted-foreground" />}
            <span className="text-muted-foreground">{group.anchor_path || "Top level"}</span>
            {names.map((name, i) => (
              // Path segments have no identity of their own: the position is the key.
              <span key={i} className="flex items-center gap-1">
                <ChevronRight className="size-3.5 text-muted-foreground" />
                <Input
                  value={name}
                  disabled={!edit.include}
                  onChange={(ev) => onRename(i, ev.target.value)}
                  className="h-7 w-auto min-w-24 max-w-56 px-2 py-0 font-medium"
                  size={Math.max(6, name.length + 1)}
                  aria-label="New folder name"
                />
              </span>
            ))}
            {isNew ? <Badge variant="success">new</Badge> : <Badge variant="muted">existing</Badge>}
          </div>
          {group.reason && <p className="mt-1 text-xs text-muted-foreground">{group.reason}</p>}
          <ul className="mt-2 flex flex-col gap-1">
            {group.documents.map((d) => (
              <li key={d.id} className="flex items-center gap-2 text-sm">
                <Checkbox
                  checked={edit.include && !edit.skipped.has(d.id)}
                  disabled={!edit.include}
                  onCheckedChange={(v) =>
                    onChange((e) => {
                      const skipped = new Set(e.skipped);
                      if (v === true) skipped.delete(d.id);
                      else skipped.add(d.id);
                      return { ...e, skipped };
                    })
                  }
                  aria-label={`Move ${d.title}`}
                />
                <DocLine doc={d} />
              </li>
            ))}
          </ul>
          {edit.include && chosen < group.documents.length && (
            <p className="mt-1 text-xs text-muted-foreground">
              {group.documents.length - chosen} unticked document{group.documents.length - chosen === 1 ? "" : "s"} stay
              where they are.
            </p>
          )}
        </div>
      </div>
    </Card>
  );
}

function DocLine({ doc }: { doc: Doc }) {
  return (
    <span className="flex min-w-0 flex-1 items-baseline gap-2">
      <a href={`/documents/${doc.id}`} target="_blank" rel="noreferrer" className="truncate hover:underline">
        {doc.title}
      </a>
      <span className="shrink-0 text-xs text-muted-foreground">
        {[doc.correspondent, doc.document_date && formatDate(doc.document_date)].filter(Boolean).join(" · ")}
      </span>
    </span>
  );
}

function Unassigned({ docs }: { docs: Doc[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-lg border border-dashed p-3 text-sm">
      <button type="button" className="flex w-full items-center gap-1 text-left text-muted-foreground" onClick={() => setOpen(!open)}>
        <ChevronRight className={cn("size-4 transition-transform", open && "rotate-90")} />
        {docs.length} document{docs.length === 1 ? "" : "s"} without a suggestion stay where they are
      </button>
      {open && (
        <ul className="mt-2 flex flex-col gap-1 pl-5">
          {docs.map((d) => (
            <li key={d.id} className="flex">
              <DocLine doc={d} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
