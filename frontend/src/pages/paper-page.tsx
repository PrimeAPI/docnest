import { useMutation } from "@tanstack/react-query";
import { Archive, ChevronDown, ChevronRight, Pencil, Trash2 } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { call, client } from "@/api/client";
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
import { Input, Label } from "@/components/ui/input";
import { EmptyState, ErrorNote, PageHeader, Spinner } from "@/components/ui/misc";
import {
  BinderPictogram,
  FillBar,
  formatThickness,
  NewLocationButton,
  type PaperLocation,
  PutAwayDialog,
  useInvalidatePaper,
  useLocationTree,
  usePaperLocations,
  usePaperPending,
  usePaperStack,
} from "@/features/paper/paper";
import { formatDate } from "@/lib/utils";

export function PaperPage() {
  const locations = usePaperLocations();
  const pending = usePaperPending();
  const tree = useLocationTree(locations.data);
  const [putAway, setPutAway] = useState(false);
  const waiting = pending.data?.documents.length ?? 0;

  return (
    <>
      <PageHeader
        title="Paper storage"
        description="Where the paper originals are. Put away everything scanned since last time in one go; each document then shows its binder and how deep in the stack it lies."
        actions={<NewLocationButton />}
      />
      <Card className="mb-6 flex flex-wrap items-center gap-3 p-4">
        <Archive className="size-5 text-muted-foreground" />
        <div className="min-w-0 flex-1">
          {pending.isPending ? (
            <Spinner />
          ) : waiting ? (
            <>
              <p className="font-medium">
                {waiting} paper original{waiting === 1 ? "" : "s"} waiting to be put away
              </p>
              <p className="text-sm text-muted-foreground">
                {pending.data?.sheets} sheets ≈ {formatThickness(pending.data?.sheets ?? 0)}
              </p>
            </>
          ) : (
            <p className="text-sm text-muted-foreground">Every paper original has a location.</p>
          )}
        </div>
        <Button onClick={() => setPutAway(true)} disabled={!waiting}>
          <Archive /> Put away…
        </Button>
        {waiting > 0 && (
          <Button variant="ghost" size="sm" asChild>
            <Link to="/documents?paper_pending=true">Show documents</Link>
          </Button>
        )}
      </Card>
      {locations.error ? (
        <ErrorNote error={locations.error} />
      ) : locations.isPending ? (
        <Spinner />
      ) : !locations.data.length ? (
        <EmptyState icon={<Archive />} title="No locations yet">
          Create one for each binder, box or shelf — e.g. “Cabinet” with “Binder 2026-1” inside.
        </EmptyState>
      ) : (
        <Card className="divide-y">
          {(tree.get(null) ?? []).map((l) => (
            <LocationRow key={l.id} location={l} tree={tree} depth={0} />
          ))}
        </Card>
      )}
      <PutAwayDialog open={putAway} onOpenChange={setPutAway} />
    </>
  );
}

function LocationRow({
  location,
  tree,
  depth,
}: {
  location: PaperLocation;
  tree: Map<number | null, PaperLocation[]>;
  depth: number;
}) {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const invalidate = useInvalidatePaper();
  const children = tree.get(location.id) ?? [];
  const remove = useMutation({
    mutationFn: () =>
      call(() =>
        client.DELETE("/api/v1/paper/locations/{location_id}", { params: { path: { location_id: location.id } } }),
      ),
    onSuccess: () => {
      toast.success(`Deleted ${location.path}`);
      invalidate();
    },
    onError: (e) => toast.error(e.message),
  });
  return (
    <>
      <div className="flex items-center gap-2 px-3 py-2" style={{ paddingLeft: `${12 + depth * 20}px` }}>
        <button
          type="button"
          className="cursor-pointer rounded p-0.5 text-muted-foreground hover:bg-muted disabled:opacity-30"
          onClick={() => setOpen((v) => !v)}
          disabled={!location.document_count}
          aria-label={open ? "Hide documents" : "Show documents"}
        >
          {open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
        </button>
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline gap-2">
            <span className="truncate font-medium">{location.name}</span>
            <span className="text-xs text-muted-foreground">
              {location.document_count} document{location.document_count === 1 ? "" : "s"} · {location.sheets}/
              {location.capacity} sheets
            </span>
          </div>
          {location.document_count > 0 && (
            <div className="mt-1 max-w-sm">
              <FillBar sheets={location.sheets} capacity={location.capacity} />
            </div>
          )}
        </div>
        <NewLocationButton parent={location} />
        <Button size="icon-sm" variant="ghost" onClick={() => setEditing(true)} title="Edit">
          <Pencil />
        </Button>
        <Button
          size="icon-sm"
          variant="ghost"
          title="Delete"
          onClick={() => {
            if (window.confirm(`Delete ${location.path}?`)) remove.mutate();
          }}
        >
          <Trash2 />
        </Button>
      </div>
      {open && <Stack location={location} depth={depth} />}
      {children.map((c) => (
        <LocationRow key={c.id} location={c} tree={tree} depth={depth + 1} />
      ))}
      <EditLocationDialog location={location} open={editing} onOpenChange={setEditing} />
    </>
  );
}

function Stack({ location, depth }: { location: PaperLocation; depth: number }) {
  const stack = usePaperStack(location.id);
  if (stack.isPending) return <Spinner />;
  if (stack.error) return <ErrorNote error={stack.error} />;
  const docs = stack.data.documents;
  const total = docs.reduce((sum, d) => sum + d.sheets, 0);
  return (
    <div className="flex gap-4 bg-muted/30 px-3 py-3" style={{ paddingLeft: `${40 + depth * 20}px` }}>
      <BinderPictogram capacity={location.capacity} total={total} below={0} sheets={0} className="hidden sm:block" />
      <ol className="min-w-0 flex-1 text-sm">
        <li className="pb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">Top</li>
        {docs.map((d) => (
          <li key={d.id} className="flex items-center gap-3 border-t py-1">
            <span className="w-24 shrink-0 text-xs tabular-nums text-muted-foreground" title="Depth from the top">
              {d.sheets_above === 0 ? "top" : `−${formatThickness(d.sheets_above)}`}
            </span>
            <Link to={`/documents/${d.id}`} className="min-w-0 flex-1 truncate hover:underline">
              {d.title}
              {d.correspondent && <span className="text-muted-foreground"> · {d.correspondent}</span>}
            </Link>
            <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
              {formatDate(d.uploaded_at)} · {d.sheets} sh.
            </span>
          </li>
        ))}
        <li className="border-t pt-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">Bottom</li>
      </ol>
    </div>
  );
}

function EditLocationDialog({
  location,
  open,
  onOpenChange,
}: {
  location: PaperLocation;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [name, setName] = useState(location.name);
  const [capacity, setCapacity] = useState(String(location.capacity));
  const invalidate = useInvalidatePaper();
  const save = useMutation({
    mutationFn: () =>
      call(() =>
        client.PATCH("/api/v1/paper/locations/{location_id}", {
          params: { path: { location_id: location.id } },
          body: { name: name.trim(), capacity: Number(capacity) || location.capacity, move_to_root: false },
        }),
      ),
    onSuccess: () => {
      invalidate();
      onOpenChange(false);
    },
    onError: (e) => toast.error(e.message),
  });
  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (v) {
          setName(location.name);
          setCapacity(String(location.capacity));
        }
        onOpenChange(v);
      }}
    >
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>Edit {location.path}</DialogTitle>
          <DialogDescription>Capacity is in sheets; an 8 cm binder holds about 500.</DialogDescription>
        </DialogHeader>
        <form
          className="grid gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (name.trim()) save.mutate();
          }}
        >
          <div className="grid gap-1">
            <Label htmlFor={`edit-name-${location.id}`}>Name</Label>
            <Input id={`edit-name-${location.id}`} value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="grid gap-1">
            <Label htmlFor={`edit-capacity-${location.id}`}>Capacity (sheets)</Label>
            <Input
              id={`edit-capacity-${location.id}`}
              type="number"
              min={1}
              value={capacity}
              onChange={(e) => setCapacity(e.target.value)}
            />
          </div>
          <DialogFooter>
            <Button type="submit" disabled={!name.trim() || save.isPending}>
              Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
