import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronRight, FolderInput, FolderPlus, Loader2, Sparkles, X } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { call, client, type Schemas } from "@/api/client";
import { type DocumentDetail, useInvalidateDocuments } from "@/api/queries";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { FolderSelect } from "@/features/folders/folder-ui";
import { formatDate } from "@/lib/utils";

type Group = Schemas["InboxFilingGroupOut"];
type Target = { anchor_id: number | null; new: string[] };

const inboxFilingKey = ["inbox-filing"] as const;

export function useInboxFiling() {
  return useQuery({
    queryKey: inboxFilingKey,
    queryFn: () => call(() => client.GET("/api/v1/filing/inbox")),
    refetchInterval: (query) => {
      const data = query.state.data;
      return data?.enabled && (data.planning || data.waiting) ? 5_000 : 15_000;
    },
  });
}

/** Approve (move, creating folders) or dismiss the suggestions of some documents. */
export function useDecideFiling() {
  const qc = useQueryClient();
  const invalidate = useInvalidateDocuments();
  return useMutation({
    mutationFn: (body: { ids: string[]; action: "accept" | "dismiss"; target?: Target }) =>
      call(() => client.POST("/api/v1/filing/inbox/decide", { body })),
    onSuccess: (r, body) => {
      if (body.action === "accept") {
        const made = r.created ? `, ${r.created} folder${r.created === 1 ? "" : "s"} created` : "";
        toast.success(`Filed ${r.moved} document${r.moved === 1 ? "" : "s"}${made}`);
      }
      qc.invalidateQueries({ queryKey: inboxFilingKey });
      invalidate();
    },
    onError: (err) => toast.error((err as Error).message),
  });
}

/** "Privat › Versicherungen › Kfz (new)": where a suggestion files a document. */
export function FilingTarget({ anchorPath, names }: { anchorPath: string; names: string[] }) {
  const existing = anchorPath ? anchorPath.split(" / ") : [];
  return (
    <span className="flex min-w-0 flex-wrap items-center gap-1">
      {names.length ? (
        <FolderPlus className="size-4 shrink-0 text-muted-foreground" />
      ) : (
        <FolderInput className="size-4 shrink-0 text-muted-foreground" />
      )}
      {[...existing, ...names].map((name, i) => (
        // Path segments have no identity of their own: the position is the key.
        <span key={i} className="flex min-w-0 items-center gap-1">
          {i > 0 && <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" />}
          <span className={i >= existing.length ? "font-medium" : "text-muted-foreground"}>{name}</span>
          {i >= existing.length && <Badge variant="success">new</Badge>}
        </span>
      ))}
    </span>
  );
}

/** The inbox section: suggestions by target folder, to approve or dismiss together. */
export function InboxFilingSection() {
  const qc = useQueryClient();
  const filing = useInboxFiling();
  const decide = useDecideFiling();
  const settings = useMutation({
    mutationFn: (enabled: boolean) => call(() => client.PUT("/api/v1/filing/inbox/settings", { body: { enabled } })),
    onSuccess: (data) => qc.setQueryData(inboxFilingKey, data),
  });
  const planNow = useMutation({
    mutationFn: () => call(() => client.POST("/api/v1/filing/inbox/plan")),
    onSuccess: (data) => qc.setQueryData(inboxFilingKey, data),
  });
  const data = filing.data;
  if (!data) return null;
  const groups = data.groups;
  const all = groups.flatMap((g) => g.documents.map((d) => d.id));
  if (!data.enabled && !groups.length) {
    return (
      <p className="mb-6 flex items-center gap-2 text-xs text-muted-foreground">
        <Checkbox checked={false} onCheckedChange={() => settings.mutate(true)} id="inbox-filing-on" />
        <label htmlFor="inbox-filing-on">Suggest where new documents should be filed</label>
      </p>
    );
  }

  return (
    <section className="mb-6">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h2 className="flex items-center gap-2 text-sm font-medium">
          <FolderInput className="size-4" /> Filing suggestions
        </h2>
        {data.planning && data.waiting > 0 && (
          <span className="flex items-center gap-1 text-xs text-muted-foreground">
            <Loader2 className="size-3 animate-spin" /> Working out where {data.waiting} document
            {data.waiting === 1 ? " goes" : "s go"}…
          </span>
        )}
        {!data.planning && data.waiting > 0 && data.enabled && (
          <Button size="sm" variant="ghost" className="h-7" onClick={() => planNow.mutate()}>
            <Sparkles /> Suggest for {data.waiting} more
          </Button>
        )}
        <span className="ml-auto flex items-center gap-3">
          <label className="flex items-center gap-2 text-xs text-muted-foreground">
            <Checkbox checked={data.enabled} onCheckedChange={(v) => settings.mutate(v === true)} />
            Suggest automatically
          </label>
          {groups.length > 1 && (
            <Button size="sm" disabled={decide.isPending} onClick={() => decide.mutate({ ids: all, action: "accept" })}>
              <Check /> Approve all ({all.length})
            </Button>
          )}
        </span>
      </div>
      {groups.length === 0 ? (
        data.waiting === 0 && (
          <p className="text-xs text-muted-foreground">
            Nothing to file: new documents get a suggestion here once they are processed.
          </p>
        )
      ) : (
        <div className="grid gap-2 lg:grid-cols-2">
          {groups.map((g) => (
            <GroupCard key={`${g.anchor_id}:${g.new.join("/")}`} group={g} />
          ))}
        </div>
      )}
    </section>
  );
}

function GroupCard({ group }: { group: Group }) {
  const decide = useDecideFiling();
  const ids = group.documents.map((d) => d.id);
  const [names, setNames] = useState(group.new);
  const [elsewhere, setElsewhere] = useState(false);
  useEffect(() => setNames(group.new), [group.new]);
  const renamed = names.some((n, i) => n.trim() !== group.new[i]);

  const approve = () =>
    decide.mutate({
      ids,
      action: "accept",
      target: renamed ? { anchor_id: group.anchor_id, new: names.map((n) => n.trim()) } : undefined,
    });

  return (
    <Card className="min-w-0 p-3">
      <div className="flex flex-wrap items-center gap-1 text-sm">
        <FilingTarget anchorPath={group.anchor_path} names={[]} />
        {names.map((name, i) => (
          <span key={i} className="flex min-w-0 items-center gap-1">
            {(group.anchor_path || i > 0) && <ChevronRight className="size-3.5 shrink-0 text-muted-foreground" />}
            <Input
              value={name}
              onChange={(e) => setNames((all) => all.map((n, j) => (j === i ? e.target.value : n)))}
              className="h-7 w-auto min-w-0 max-w-56 px-2 py-0 font-medium"
              size={Math.max(6, Math.min(name.length + 1, 28))}
              aria-label="New folder name"
            />
          </span>
        ))}
        {names.length ? <Badge variant="success">new</Badge> : <Badge variant="muted">existing</Badge>}
        {group.named_by === "ai" && (
          <Badge variant="outline" title="The AI model chose this folder">
            <Sparkles /> AI
          </Badge>
        )}
      </div>
      {group.reasons.length > 0 && (
        <p className="mt-1 text-xs text-muted-foreground">{group.reasons.slice(0, 2).join("; ")}</p>
      )}
      <ul className="mt-2 space-y-1 text-sm">
        {group.documents.map((d) => (
          <li key={d.id} className="flex min-w-0 items-baseline gap-2">
            <Link to={`/documents/${d.id}`} className="min-w-0 truncate hover:underline">
              {d.title}
            </Link>
            <span className="shrink-0 text-xs text-muted-foreground">
              {[d.correspondent, d.document_date && formatDate(d.document_date)].filter(Boolean).join(" · ")}
            </span>
          </li>
        ))}
      </ul>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Button size="sm" disabled={decide.isPending || names.some((n) => !n.trim())} onClick={approve}>
          <Check /> Approve{ids.length > 1 ? ` (${ids.length})` : ""}
        </Button>
        {elsewhere ? (
          <FolderSelect
            className="h-8 w-56"
            value={null}
            onChange={(id) => id !== null && decide.mutate({ ids, action: "accept", target: { anchor_id: id, new: [] } })}
          />
        ) : (
          <Button size="sm" variant="outline" onClick={() => setElsewhere(true)}>
            <FolderInput /> Elsewhere…
          </Button>
        )}
        <Button
          size="sm"
          variant="ghost"
          disabled={decide.isPending}
          title="Leave these documents where they are"
          onClick={() => decide.mutate({ ids, action: "dismiss" })}
        >
          <X /> Dismiss
        </Button>
      </div>
    </Card>
  );
}

/** On a document: its suggested folder, to approve or dismiss. */
export function FilingSuggestionCard({ doc, onAccepted }: { doc: DocumentDetail; onAccepted?: () => void }) {
  const decide = useDecideFiling();
  const s = doc.filing_suggestion;
  if (!s) return null;
  return (
    <div className="mb-4 rounded-md border bg-accent/50 p-3 text-sm">
      <div className="flex items-center gap-2 font-medium">
        <FolderInput className="size-4" /> File it here?
        {s.named_by === "ai" && (
          <Badge variant="outline" title="The AI model chose this folder">
            <Sparkles /> AI
          </Badge>
        )}
      </div>
      <div className="mt-1">
        <FilingTarget anchorPath={s.anchor_path} names={s.new} />
      </div>
      {s.reason && <p className="mt-1 text-xs text-muted-foreground">{s.reason}</p>}
      <div className="mt-2 flex gap-2">
        <Button
          size="sm"
          disabled={decide.isPending}
          onClick={() => decide.mutate({ ids: [doc.id], action: "accept" }, { onSuccess: () => onAccepted?.() })}
        >
          <Check /> Move it there
        </Button>
        <Button
          size="sm"
          variant="outline"
          disabled={decide.isPending}
          onClick={() => decide.mutate({ ids: [doc.id], action: "dismiss" })}
        >
          No
        </Button>
      </div>
    </div>
  );
}
