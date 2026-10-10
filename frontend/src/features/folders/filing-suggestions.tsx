import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, FolderInput, FolderPlus, RotateCcw, Sparkles, Undo2 } from "lucide-react";
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
import { Input, Select, Textarea } from "@/components/ui/input";
import { Spinner } from "@/components/ui/misc";
import { cn, formatDate } from "@/lib/utils";

type Proposal = Schemas["FilingProposalOut"];
type Group = Schemas["FilingGroupOut"];
type Doc = Schemas["FilingDocOut"];
type Options = Schemas["FilingOptionsOut"];

const MAX_INSTRUCTIONS = 300; // what the model is given; keep in step with ai.MAX_INSTRUCTIONS
const STAY = -1;

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

// A new folder shared by several cards (".../Verdienstabrechnungen/2025" and ".../2026") is renamed once for all.
type Renames = Record<string, string>;
const segmentKey = (group: Group, index: number) => `${group.anchor_id ?? ""}|${group.new.slice(0, index + 1).join("/")}`;
const namesOf = (group: Group, renames: Renames) => group.new.map((n, i) => renames[segmentKey(group, i)] ?? n);
const pathOf = (group: Group, renames: Renames) =>
  [group.anchor_path || "Top level", ...namesOf(group, renames)].join(" / ");

/** Documents only move deeper below their own folder (unfiled ones anywhere), as the server checks too. */
function canGo(doc: Doc, group: Group): boolean {
  const from = doc.folder_path;
  if (!from) return true;
  if (group.anchor_path === from) return group.new.length > 0;
  return group.anchor_path.startsWith(`${from} / `); // folder_paths joins with " / "
}

function SuggestFilingDialog({ ids, onClose, onDone }: { ids: string[]; onClose: () => void; onDone: () => void }) {
  const qc = useQueryClient();
  const invalidateDocuments = useInvalidateDocuments();
  const [proposalId, setProposalId] = useState<number | null>(null);
  const [options, setOptions] = useState<Options | null>(null);
  const [remember, setRemember] = useState(false);
  // Where each document goes: the index of a suggested folder, or STAY.
  const [assigned, setAssigned] = useState<Record<string, number>>({});
  const [renames, setRenames] = useState<Renames>({});

  const preferences = useQuery({
    queryKey: ["filing-preferences"],
    queryFn: () => call(() => client.GET("/api/v1/filing/preferences")),
    staleTime: 0,
  });

  const start = useMutation({
    mutationFn: (o: Options) =>
      call(() => client.POST("/api/v1/filing/suggestions", { body: { ids, ...o, remember } })),
    onSuccess: (p) => {
      setProposalId(p.id);
      setRenames({});
      if (remember) qc.invalidateQueries({ queryKey: ["filing-preferences"] });
    },
    onError: (e) => toast.error(e.message),
  });
  // Start once the saved defaults are known; the selection is fixed while the dialog is open.
  useEffect(() => {
    if (options || !(preferences.data || preferences.isError)) return;
    const initial = preferences.data ?? { instructions: "", year_folders: true, new_folders: true };
    setOptions(initial);
    start.mutate(initial);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preferences.data, preferences.isError]);

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
  const ready = data?.state === "done" && !start.isPending;

  useEffect(() => {
    if (data?.state !== "done") return;
    const next: Record<string, number> = {};
    data.groups.forEach((g, i) => g.documents.forEach((d) => (next[d.id] = i)));
    data.unassigned.forEach((d) => (next[d.id] = STAY));
    setAssigned(next);
  }, [data]);

  const allDocs = useMemo(
    () => (data ? [...data.groups.flatMap((g) => g.documents), ...data.unassigned] : []),
    [data],
  );
  const docsIn = (index: number) => allDocs.filter((d) => (assigned[d.id] ?? STAY) === index);

  const moves = useMemo(() => {
    if (!data || !ready) return [];
    return data.groups
      .map((g, i) => ({
        ids: allDocs.filter((d) => assigned[d.id] === i).map((d) => d.id),
        anchor_id: g.anchor_id,
        new: namesOf(g, renames).map((n) => n.trim()),
      }))
      .filter((m) => m.ids.length > 0);
  }, [data, ready, allDocs, assigned, renames]);
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

  const moveDoc = (id: string, to: number) => setAssigned((a) => ({ ...a, [id]: to }));
  const targets = data?.groups.map((g) => pathOf(g, renames)) ?? [];
  const working = start.isPending || data?.state === "pending" || (!data && !start.isError);

  return (
    <Dialog open onOpenChange={(v) => !v && onClose()}>
      {/* One scrolling area between a fixed header and footer: no nested or sideways scrollbars. */}
      <DialogContent className="flex h-[90vh] max-w-4xl flex-col gap-0 overflow-hidden p-0">
        <DialogHeader className="border-b px-6 py-4 pr-12">
          <DialogTitle>Suggested filing</DialogTitle>
          <DialogDescription>
            Documents that belong together, and the subfolder each group would go into. Documents only move deeper into
            their own folder. Nothing changes until you apply.
          </DialogDescription>
        </DialogHeader>

        <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden px-6 py-4">
          {options && (
            <InstructionsBox
              options={options}
              onChange={setOptions}
              remember={remember}
              onRemember={setRemember}
              busy={working}
              onRun={() => start.mutate(options)}
            />
          )}

          <div className="mt-4 flex flex-col gap-3">
            {start.isError || proposal.isError ? (
              <p className="text-sm text-destructive">Could not get suggestions.</p>
            ) : data?.state === "failed" ? (
              <p className="text-sm text-destructive">The suggestion failed: {data.error}</p>
            ) : !ready || !data ? (
              <div className="flex items-center gap-3 py-8 text-sm text-muted-foreground">
                <Spinner />
                <span>
                  Looking at {ids.length} document{ids.length === 1 ? "" : "s"} and your folders… With an AI model this
                  can take a minute.
                </span>
              </div>
            ) : (
              <>
                {data.understood.length > 0 && (
                  <div className="rounded-md border px-3 py-2 text-sm">
                    <span className="text-muted-foreground">Your instructions, as the AI model understood them:</span>
                    <ul className="mt-1 list-disc pl-5">
                      {data.understood.map((line) => (
                        <li key={line}>{line}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {data.note && (
                  <p className="rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100">
                    {data.note}
                  </p>
                )}
                {!data.groups.length && (
                  <p className="py-6 text-center text-sm text-muted-foreground">
                    No suggestions: these documents have too little in common, or nothing that fits a subfolder.
                  </p>
                )}
                {data.groups.map((group, index) => (
                  <GroupCard
                    key={`${group.anchor_id}-${group.new.join("/")}`}
                    group={group}
                    index={index}
                    docs={docsIn(index)}
                    names={namesOf(group, renames)}
                    targets={targets}
                    groups={data.groups}
                    onMove={moveDoc}
                    onRename={(i, name) => setRenames((r) => ({ ...r, [segmentKey(group, i)]: name }))}
                  />
                ))}
                <StaySection docs={docsIn(STAY)} targets={targets} groups={data.groups} onMove={moveDoc} />
              </>
            )}
          </div>
        </div>

        <DialogFooter className="border-t px-6 py-3">
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

function InstructionsBox({
  options,
  onChange,
  remember,
  onRemember,
  busy,
  onRun,
}: {
  options: Options;
  onChange: (o: Options) => void;
  remember: boolean;
  onRemember: (v: boolean) => void;
  busy: boolean;
  onRun: () => void;
}) {
  const [open, setOpen] = useState(Boolean(options.instructions) || !options.year_folders || !options.new_folders);
  const set = <K extends keyof Options>(key: K, value: Options[K]) => onChange({ ...options, [key]: value });
  return (
    <div className="rounded-lg border bg-muted/30 p-3 text-sm">
      <div className="flex items-center gap-2">
        <button type="button" className="flex min-w-0 flex-1 items-center gap-1 text-left font-medium" onClick={() => setOpen(!open)}>
          <ChevronRight className={cn("size-4 shrink-0 transition-transform", open && "rotate-90")} />
          <span className="truncate">
            Instructions
            {!open && options.instructions && (
              <span className="font-normal text-muted-foreground"> — {options.instructions}</span>
            )}
          </span>
        </button>
        <Button size="sm" variant="outline" disabled={busy} onClick={onRun}>
          <RotateCcw /> Suggest again
        </Button>
      </div>
      {open && (
        <div className="mt-3 flex flex-col gap-3">
          <div className="flex flex-col gap-1">
            <Textarea
              rows={2}
              maxLength={MAX_INSTRUCTIONS}
              value={options.instructions}
              placeholder={"e.g. Put everything from Stadtwerke and Vodafone into “Wohnung”. Use English folder names."}
              onChange={(e) => set("instructions", e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !busy) onRun();
              }}
            />
            <p className="text-xs text-muted-foreground">
              The AI model turns them into rules: which documents go into which folder, or stay, and how new
              folders are named. It is a small model: short sentences naming senders, document types or title words
              and the folder work best, e.g. “Stadtwerke nach Wohnung/Nebenkosten”. {options.instructions.length}/
              {MAX_INSTRUCTIONS}
            </p>
          </div>
          <div className="flex flex-wrap gap-x-5 gap-y-2">
            <label className="flex cursor-pointer items-center gap-2">
              <Checkbox checked={options.year_folders} onCheckedChange={(v) => set("year_folders", v === true)} />
              Year folders
            </label>
            <label className="flex cursor-pointer items-center gap-2">
              <Checkbox checked={options.new_folders} onCheckedChange={(v) => set("new_folders", v === true)} />
              New folders (otherwise only existing ones)
            </label>
            <label className="flex cursor-pointer items-center gap-2">
              <Checkbox checked={remember} onCheckedChange={(v) => onRemember(v === true)} />
              Remember as my default
            </label>
          </div>
        </div>
      )}
    </div>
  );
}

function GroupCard({
  group,
  index,
  docs,
  names,
  targets,
  groups,
  onMove,
  onRename,
}: {
  group: Group;
  index: number;
  docs: Doc[];
  names: string[];
  targets: string[];
  groups: Group[];
  onMove: (id: string, to: number) => void;
  onRename: (index: number, name: string) => void;
}) {
  const isNew = group.new.length > 0;
  return (
    <Card className={cn("min-w-0 p-3", !docs.length && "opacity-60")}>
      <div className="flex flex-wrap items-center gap-1 text-sm">
        {isNew ? (
          <FolderPlus className="size-4 shrink-0 text-muted-foreground" />
        ) : (
          <FolderInput className="size-4 shrink-0 text-muted-foreground" />
        )}
        <span className="min-w-0 break-words text-muted-foreground">{group.anchor_path || "Top level"}</span>
        {names.map((name, i) => (
          // Path segments have no identity of their own: the position is the key.
          <span key={i} className="flex min-w-0 items-center gap-1">
            <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" />
            <Input
              value={name}
              onChange={(ev) => onRename(i, ev.target.value)}
              className="h-7 w-auto min-w-0 max-w-56 px-2 py-0 font-medium"
              size={Math.max(6, Math.min(name.length + 1, 28))}
              aria-label="New folder name"
            />
          </span>
        ))}
        {isNew ? <Badge variant="success">new</Badge> : <Badge variant="muted">existing</Badge>}
        <span className="ml-auto flex items-center gap-2 text-xs text-muted-foreground">
          {docs.length} document{docs.length === 1 ? "" : "s"}
          {docs.length > 0 && (
            <Button
              size="sm"
              variant="ghost"
              className="h-7 px-2"
              title="Leave all these documents where they are"
              onClick={() => docs.forEach((d) => onMove(d.id, STAY))}
            >
              <Undo2 /> Leave all
            </Button>
          )}
        </span>
      </div>
      {group.reason && <p className="mt-1 text-xs text-muted-foreground">{group.reason}</p>}
      {docs.length ? (
        <ul className="mt-2 flex flex-col divide-y">
          {docs.map((d) => (
            <DocRow key={d.id} doc={d} at={index} targets={targets} groups={groups} onMove={onMove} />
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-xs text-muted-foreground">Nothing goes here. Pick this folder on a document to add it.</p>
      )}
    </Card>
  );
}

function StaySection({
  docs,
  targets,
  groups,
  onMove,
}: {
  docs: Doc[];
  targets: string[];
  groups: Group[];
  onMove: (id: string, to: number) => void;
}) {
  const [open, setOpen] = useState(false);
  if (!docs.length) return null;
  return (
    <div className="rounded-lg border border-dashed p-3 text-sm">
      <button type="button" className="flex w-full items-center gap-1 text-left text-muted-foreground" onClick={() => setOpen(!open)}>
        <ChevronRight className={cn("size-4 transition-transform", open && "rotate-90")} />
        {docs.length} document{docs.length === 1 ? "" : "s"} stay where they are
      </button>
      {open && (
        <ul className="mt-2 flex flex-col divide-y">
          {docs.map((d) => (
            <DocRow key={d.id} doc={d} at={STAY} targets={targets} groups={groups} onMove={onMove} />
          ))}
        </ul>
      )}
    </div>
  );
}

function DocRow({
  doc,
  at,
  targets,
  groups,
  onMove,
}: {
  doc: Doc;
  at: number;
  targets: string[];
  groups: Group[];
  onMove: (id: string, to: number) => void;
}) {
  return (
    <li className="flex flex-col gap-1 py-1.5 sm:flex-row sm:items-center sm:gap-3">
      <span className="flex min-w-0 flex-1 items-baseline gap-2">
        <a href={`/documents/${doc.id}`} target="_blank" rel="noreferrer" className="truncate hover:underline">
          {doc.title}
        </a>
        <span className="shrink-0 text-xs text-muted-foreground">
          {[doc.correspondent, doc.document_date && formatDate(doc.document_date)].filter(Boolean).join(" · ")}
        </span>
      </span>
      <Select
        value={at}
        onChange={(e) => onMove(doc.id, Number(e.target.value))}
        className="h-8 w-full shrink-0 text-xs sm:w-64"
        aria-label={`Where ${doc.title} goes`}
      >
        <option value={STAY}>Leave where it is</option>
        {targets.map((path, i) =>
          canGo(doc, groups[i]) ? (
            <option key={i} value={i}>
              {path}
            </option>
          ) : null,
        )}
      </Select>
    </li>
  );
}
