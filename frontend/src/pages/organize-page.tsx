import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Combine, Pencil, Plus, Trash2, Wand2 } from "lucide-react";
import { type FormEvent, useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { call, client, type Schemas } from "@/api/client";
import { keys, useCorrespondents, useTags, useTypes } from "@/api/queries";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input, Label, Select } from "@/components/ui/input";
import { EmptyState, PageHeader, Spinner } from "@/components/ui/misc";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { TagChip } from "@/features/documents/document-row";
import { cn, dotClass, TAG_COLORS } from "@/lib/utils";

function useInvalidateTaxonomy() {
  const qc = useQueryClient();
  return () => {
    for (const k of [keys.tags, keys.folders, keys.types, keys.correspondents, keys.rules, keys.overview]) {
      qc.invalidateQueries({ queryKey: k });
    }
    qc.invalidateQueries({ queryKey: ["documents"] });
  };
}

function useAction<T>(fn: (arg: T) => Promise<unknown>, success?: string) {
  const invalidate = useInvalidateTaxonomy();
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      invalidate();
      if (success) toast.success(success);
    },
    onError: (e) => toast.error(e.message),
  });
}

export function OrganizePage() {
  return (
    <>
      <PageHeader title="Tags & more" description="Keep your structure tidy: tags, senders, types and rules. Folders live under Filing." />
      <Tabs defaultValue="tags">
        <TabsList className="flex-wrap">
          <TabsTrigger value="tags">Tags</TabsTrigger>
          <TabsTrigger value="senders">Senders</TabsTrigger>
          <TabsTrigger value="types">Types</TabsTrigger>
          <TabsTrigger value="rules">Rules</TabsTrigger>
        </TabsList>
        <TabsContent value="tags">
          <TagsTab />
        </TabsContent>
        <TabsContent value="senders">
          <SendersTab />
        </TabsContent>
        <TabsContent value="types">
          <TypesTab />
        </TabsContent>
        <TabsContent value="rules">
          <RulesTab />
        </TabsContent>
      </Tabs>
    </>
  );
}

// --- Tags ---------------------------------------------------------------------

function TagsTab() {
  const tags = useTags();
  const [name, setName] = useState("");
  const [editing, setEditing] = useState<Schemas["TagOut"] | null>(null);
  const [merging, setMerging] = useState<Schemas["TagOut"] | null>(null);
  const create = useAction((n: string) => call(() => client.POST("/api/v1/tags", { body: { name: n } })));
  const confirmTag = useAction((id: number) =>
    call(() => client.PATCH("/api/v1/tags/{tag_id}", { params: { path: { tag_id: id } }, body: { confirm: true } })),
  );
  const remove = useAction(
    (id: number) => call(() => client.DELETE("/api/v1/tags/{tag_id}", { params: { path: { tag_id: id } } })),
    "Tag deleted",
  );

  const suggested = tags.data?.filter((t) => t.is_suggested) ?? [];
  const regular = tags.data?.filter((t) => !t.is_suggested) ?? [];

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (name.trim()) create.mutate(name.trim(), { onSuccess: () => setName("") });
  };

  const row = (t: Schemas["TagOut"]) => (
    <div key={t.id} className="flex items-center gap-3 border-b px-4 py-2.5 last:border-b-0">
      <TagChip name={t.name} color={t.color} />
      <span className="flex-1 truncate text-xs text-muted-foreground">
        {t.aliases.length > 0 && `also: ${t.aliases.join(", ")}`}
      </span>
      <Link to={`/documents?tag=${t.id}`} className="text-xs text-muted-foreground hover:underline">
        {t.document_count} docs
      </Link>
      {t.is_suggested && (
        <Button size="sm" variant="outline" onClick={() => confirmTag.mutate(t.id)}>
          <Check /> Keep
        </Button>
      )}
      <Button size="icon-sm" variant="ghost" title="Edit" onClick={() => setEditing(t)}>
        <Pencil />
      </Button>
      <Button size="icon-sm" variant="ghost" title="Merge into…" onClick={() => setMerging(t)}>
        <Combine />
      </Button>
      <Button
        size="icon-sm"
        variant="ghost"
        title="Delete"
        onClick={() => confirm(`Delete tag “${t.name}”? It is removed from all documents.`) && remove.mutate(t.id)}
      >
        <Trash2 />
      </Button>
    </div>
  );

  if (tags.isPending) return <Spinner />;
  return (
    <div className="flex flex-col gap-6">
      <form onSubmit={submit} className="flex max-w-md gap-2">
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="New tag name" />
        <Button type="submit" disabled={create.isPending}>
          <Plus /> Add
        </Button>
      </form>
      {suggested.length > 0 && (
        <div>
          <h3 className="mb-2 flex items-center gap-2 text-sm font-semibold">
            <Wand2 className="size-4" /> Suggested by DocNest
          </h3>
          <p className="mb-2 text-sm text-muted-foreground">
            These tags were created automatically. Keep them, merge them into one of your tags, or delete them (they
            won't be suggested again).
          </p>
          <Card className="overflow-hidden">{suggested.map(row)}</Card>
        </div>
      )}
      <div>
        <h3 className="mb-2 text-sm font-semibold">Your tags</h3>
        {regular.length ? (
          <Card className="overflow-hidden">{regular.map(row)}</Card>
        ) : (
          <EmptyState title="No tags yet">Tags are added automatically or by you on a document.</EmptyState>
        )}
      </div>
      {editing && <TagDialog tag={editing} onClose={() => setEditing(null)} />}
      {merging && <MergeDialog tag={merging} tags={tags.data ?? []} onClose={() => setMerging(null)} />}
    </div>
  );
}

function TagDialog({ tag, onClose }: { tag: Schemas["TagOut"]; onClose: () => void }) {
  const [name, setName] = useState(tag.name);
  const [color, setColor] = useState(tag.color);
  const [aliases, setAliases] = useState(tag.aliases.join(", "));
  const save = useAction(
    () =>
      call(() =>
        client.PATCH("/api/v1/tags/{tag_id}", {
          params: { path: { tag_id: tag.id } },
          body: {
            name,
            color,
            confirm: true,
            aliases: aliases
              .split(",")
              .map((a) => a.trim())
              .filter(Boolean),
          },
        }),
      ),
    "Tag saved",
  );
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <form
          className="flex flex-col gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate(undefined, { onSuccess: onClose });
          }}
        >
          <DialogHeader>
            <DialogTitle>Edit tag</DialogTitle>
            <DialogDescription>Aliases are other words for the same thing; they are matched automatically.</DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="edit-name">Name</Label>
            <Input id="edit-name" value={name} onChange={(e) => setName(e.target.value)} required />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="edit-aliases">Aliases (comma-separated)</Label>
            <Input id="edit-aliases" value={aliases} onChange={(e) => setAliases(e.target.value)} placeholder="KFZ, Auto, PKW" />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label>Color</Label>
            <div className="flex flex-wrap gap-2">
              {TAG_COLORS.map((c) => (
                <button
                  key={c}
                  type="button"
                  title={c}
                  onClick={() => setColor(c)}
                  className={cn(
                    "size-6 rounded-full ring-offset-2 ring-offset-card cursor-pointer",
                    dotClass(c),
                    color === c && "ring-2 ring-ring",
                  )}
                />
              ))}
            </div>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit">Save</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function MergeDialog({ tag, tags, onClose }: { tag: Schemas["TagOut"]; tags: Schemas["TagOut"][]; onClose: () => void }) {
  const [target, setTarget] = useState<number | "">("");
  const merge = useAction(
    (targetId: number) =>
      call(() =>
        client.POST("/api/v1/tags/{tag_id}/merge", { params: { path: { tag_id: tag.id } }, body: { target_id: targetId } }),
      ),
    "Tags merged",
  );
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Merge “{tag.name}”</DialogTitle>
          <DialogDescription>
            All documents get the target tag instead; “{tag.name}” becomes an alias so it is recognized in the future.
          </DialogDescription>
        </DialogHeader>
        <Select value={target} onChange={(e) => setTarget(Number(e.target.value))}>
          <option value="">Choose target tag…</option>
          {tags
            .filter((t) => t.id !== tag.id)
            .map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
        </Select>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={!target} onClick={() => target && merge.mutate(target, { onSuccess: onClose })}>
            <Combine /> Merge
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// --- Senders ------------------------------------------------------------------

function SendersTab() {
  const correspondents = useCorrespondents();
  const [editing, setEditing] = useState<Schemas["CorrespondentOut"] | null>(null);
  const [merging, setMerging] = useState<Schemas["CorrespondentOut"] | null>(null);
  const [filter, setFilter] = useState("");
  const remove = useAction((id: number) =>
    call(() => client.DELETE("/api/v1/correspondents/{corr_id}", { params: { path: { corr_id: id } } })),
  );
  if (correspondents.isPending) return <Spinner />;
  const list = (correspondents.data ?? []).filter((c) => c.name.toLowerCase().includes(filter.toLowerCase()));
  return (
    <div className="flex flex-col gap-4">
      <Input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter senders…" className="max-w-md" />
      {!list.length ? (
        <EmptyState title="No senders yet">Senders are detected automatically from letterheads.</EmptyState>
      ) : (
        <Card className="overflow-hidden">
          {list.map((c) => (
            <div key={c.id} className="flex items-center gap-3 border-b px-4 py-2.5 last:border-b-0">
              <span className="font-medium">{c.name}</span>
              <span className="flex-1 truncate text-xs text-muted-foreground">
                {c.aliases.length > 0 && `also: ${c.aliases.join(", ")}`}
              </span>
              <Link to={`/documents?correspondent=${c.id}`} className="text-xs text-muted-foreground hover:underline">
                {c.document_count} docs
              </Link>
              <Button size="icon-sm" variant="ghost" title="Edit" onClick={() => setEditing(c)}>
                <Pencil />
              </Button>
              <Button size="icon-sm" variant="ghost" title="Merge into…" onClick={() => setMerging(c)}>
                <Combine />
              </Button>
              <Button
                size="icon-sm"
                variant="ghost"
                title="Delete"
                onClick={() => confirm(`Delete sender “${c.name}”?`) && remove.mutate(c.id)}
              >
                <Trash2 />
              </Button>
            </div>
          ))}
        </Card>
      )}
      {editing && <SenderDialog sender={editing} onClose={() => setEditing(null)} />}
      {merging && (
        <SenderMergeDialog sender={merging} senders={correspondents.data ?? []} onClose={() => setMerging(null)} />
      )}
    </div>
  );
}

function SenderDialog({ sender, onClose }: { sender: Schemas["CorrespondentOut"]; onClose: () => void }) {
  const [name, setName] = useState(sender.name);
  const [aliases, setAliases] = useState(sender.aliases.join(", "));
  const save = useAction(
    () =>
      call(() =>
        client.PATCH("/api/v1/correspondents/{corr_id}", {
          params: { path: { corr_id: sender.id } },
          body: {
            name,
            aliases: aliases
              .split(",")
              .map((a) => a.trim())
              .filter(Boolean),
          },
        }),
      ),
    "Sender saved",
  );
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <form
          className="flex flex-col gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            save.mutate(undefined, { onSuccess: onClose });
          }}
        >
          <DialogHeader>
            <DialogTitle>Edit sender</DialogTitle>
            <DialogDescription>
              Aliases are alternative spellings found in letters; documents containing them get this sender.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="edit-name">Name</Label>
            <Input id="edit-name" value={name} onChange={(e) => setName(e.target.value)} required />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="edit-aliases">Aliases (comma-separated)</Label>
            <Input id="edit-aliases" value={aliases} onChange={(e) => setAliases(e.target.value)} />
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit">Save</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function SenderMergeDialog({
  sender,
  senders,
  onClose,
}: {
  sender: Schemas["CorrespondentOut"];
  senders: Schemas["CorrespondentOut"][];
  onClose: () => void;
}) {
  const [target, setTarget] = useState<number | "">("");
  const merge = useAction(
    (targetId: number) =>
      call(() =>
        client.POST("/api/v1/correspondents/{corr_id}/merge", {
          params: { path: { corr_id: sender.id } },
          body: { target_id: targetId },
        }),
      ),
    "Senders merged",
  );
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Merge “{sender.name}”</DialogTitle>
          <DialogDescription>All documents and series move to the target sender.</DialogDescription>
        </DialogHeader>
        <Select value={target} onChange={(e) => setTarget(Number(e.target.value))}>
          <option value="">Choose target…</option>
          {senders
            .filter((s) => s.id !== sender.id)
            .map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
        </Select>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={!target} onClick={() => target && merge.mutate(target, { onSuccess: onClose })}>
            <Combine /> Merge
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// --- Types -------------------------------------------------------------------------

function TypesTab() {
  const types = useTypes();
  const [typeName, setTypeName] = useState("");
  const addType = useAction((name: string) => call(() => client.POST("/api/v1/document-types", { body: { name } })));
  const delType = useAction((id: number) =>
    call(() => client.DELETE("/api/v1/document-types/{type_id}", { params: { path: { type_id: id } } })),
  );
  const renameType = useAction((t: { id: number; name: string }) =>
    call(() =>
      client.PATCH("/api/v1/document-types/{type_id}", { params: { path: { type_id: t.id } }, body: { name: t.name } }),
    ),
  );

  return (
    <div className="max-w-2xl">
      <h3 className="mb-1 text-sm font-semibold">Document types</h3>
      <p className="mb-3 text-sm text-muted-foreground">The scanner can send a type slug, or “auto”.</p>
      <Card className="overflow-hidden">
        {types.data?.map((t) => (
          <div key={t.id} className="flex items-center gap-3 border-b px-4 py-2.5 last:border-b-0">
            <span className="font-medium">{t.name}</span>
            <code className="text-xs text-muted-foreground">{t.slug}</code>
            <span className="ml-auto text-xs text-muted-foreground">{t.document_count} docs</span>
            <Button
              size="icon-sm"
              variant="ghost"
              title="Rename"
              onClick={() => {
                const name = prompt("New name", t.name);
                if (name) renameType.mutate({ id: t.id, name });
              }}
            >
              <Pencil />
            </Button>
            <Button
              size="icon-sm"
              variant="ghost"
              title="Delete"
              onClick={() => confirm(`Delete type “${t.name}”?`) && delType.mutate(t.id)}
            >
              <Trash2 />
            </Button>
          </div>
        ))}
      </Card>
      <form
        className="mt-3 flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (typeName.trim()) addType.mutate(typeName.trim(), { onSuccess: () => setTypeName("") });
        }}
      >
        <Input value={typeName} onChange={(e) => setTypeName(e.target.value)} placeholder="New type" />
        <Button type="submit" variant="outline">
          <Plus /> Add
        </Button>
      </form>
    </div>
  );
}

// --- Rules -------------------------------------------------------------------------

function RulesTab() {
  const rules = useQuery({ queryKey: keys.rules, queryFn: () => call(() => client.GET("/api/v1/rules")) });
  const correspondents = useCorrespondents();
  const types = useTypes();
  const tags = useTags();
  const [phrase, setPhrase] = useState("");
  const [corr, setCorr] = useState<number | "">("");
  const [type, setType] = useState<number | "">("");
  const [tag, setTag] = useState<number | "">("");
  const create = useAction(
    () =>
      call(() =>
        client.POST("/api/v1/rules", {
          body: {
            phrase,
            correspondent_id: corr || null,
            document_type_id: type || null,
            tag_ids: tag ? [tag] : [],
          },
        }),
      ),
    "Rule added",
  );
  const remove = useAction((id: number) =>
    call(() => client.DELETE("/api/v1/rules/{rule_id}", { params: { path: { rule_id: id } } })),
  );

  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-muted-foreground">
        Rules are applied to every new document before anything else: if the text contains the phrase, the sender,
        type or tag is set.
      </p>
      <Card className="p-4">
        <form
          className="grid gap-3 md:grid-cols-[2fr_1fr_1fr_1fr_auto] md:items-end"
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate(undefined, {
              onSuccess: () => {
                setPhrase("");
                setCorr("");
                setType("");
                setTag("");
              },
            });
          }}
        >
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="rule-phrase">If text contains</Label>
            <Input id="rule-phrase" value={phrase} onChange={(e) => setPhrase(e.target.value)} placeholder="e.g. Versicherungsschein" required />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="rule-sender">Sender</Label>
            <Select id="rule-sender" value={corr} onChange={(e) => setCorr(e.target.value ? Number(e.target.value) : "")}>
              <option value="">—</option>
              {correspondents.data?.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </Select>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="rule-type">Type</Label>
            <Select id="rule-type" value={type} onChange={(e) => setType(e.target.value ? Number(e.target.value) : "")}>
              <option value="">—</option>
              {types.data?.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </Select>
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="rule-tag">Tag</Label>
            <Select id="rule-tag" value={tag} onChange={(e) => setTag(e.target.value ? Number(e.target.value) : "")}>
              <option value="">—</option>
              {tags.data?.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </Select>
          </div>
          <Button type="submit">
            <Plus /> Add rule
          </Button>
        </form>
      </Card>
      {rules.data?.length ? (
        <Card className="overflow-hidden">
          {rules.data.map((r) => (
            <div key={r.id} className="flex flex-wrap items-center gap-2 border-b px-4 py-2.5 text-sm last:border-b-0">
              <span className="text-muted-foreground">contains</span>
              <code className="rounded bg-muted px-1.5 py-0.5">{r.phrase}</code>
              <span className="text-muted-foreground">→</span>
              {r.correspondent && <Badge variant="outline">sender: {r.correspondent}</Badge>}
              {r.document_type && <Badge variant="outline">type: {r.document_type}</Badge>}
              {r.tags.map((t) => (
                <Badge key={t}>{t}</Badge>
              ))}
              <Button size="icon-sm" variant="ghost" className="ml-auto" onClick={() => remove.mutate(r.id)}>
                <Trash2 />
              </Button>
            </div>
          ))}
        </Card>
      ) : (
        <EmptyState title="No rules yet">Most documents are recognized without rules — add one when something is consistently misfiled.</EmptyState>
      )}
    </div>
  );
}
