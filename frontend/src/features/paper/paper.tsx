import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Archive, ArrowDown, ArrowUp, Plus } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { call, client, type Schemas } from "@/api/client";
import { type DocumentDetail, keys, useInvalidateDocuments, useUpdateDocument } from "@/api/queries";
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
import { Input, Label, Select } from "@/components/ui/input";
import { Spinner } from "@/components/ui/misc";
import { cn, formatDate, formatDateTime } from "@/lib/utils";

export type PaperLocation = Schemas["LocationOut"];
export type PendingDocument = Schemas["PendingItemOut"];
export const SHEET_MM = 0.1;

export const paperKeys = {
  locations: ["paper", "locations"] as const,
  pending: ["paper", "pending"] as const,
  stack: (id: number) => ["paper", "stack", id] as const,
};

export function usePaperLocations() {
  return useQuery({
    queryKey: paperKeys.locations,
    queryFn: () => call(() => client.GET("/api/v1/paper/locations")),
  });
}

export function usePaperPending() {
  return useQuery({
    queryKey: paperKeys.pending,
    queryFn: () => call(() => client.GET("/api/v1/paper/pending")),
  });
}

export function usePaperStack(id: number | null) {
  return useQuery({
    queryKey: paperKeys.stack(id ?? 0),
    enabled: id !== null,
    queryFn: () =>
      call(() => client.GET("/api/v1/paper/locations/{location_id}/stack", { params: { path: { location_id: id ?? 0 } } })),
  });
}

/** Refresh everything that shows where paper lies. */
export function useInvalidatePaper() {
  const qc = useQueryClient();
  const invalidateDocuments = useInvalidateDocuments();
  return () => {
    qc.invalidateQueries({ queryKey: ["paper"] });
    qc.invalidateQueries({ queryKey: keys.overview });
    invalidateDocuments();
  };
}

export function formatThickness(sheets: number): string {
  const mm = sheets * SHEET_MM;
  return mm >= 10 ? `${(mm / 10).toFixed(1).replace(".", ",")} cm` : `${mm.toFixed(1).replace(".", ",")} mm`;
}

export function FillBar({ sheets, capacity, extra = 0 }: { sheets: number; capacity: number; extra?: number }) {
  const used = Math.min(100, (sheets / capacity) * 100);
  const added = Math.min(100 - used, (extra / capacity) * 100);
  const over = sheets + extra > capacity;
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted" title={`${sheets + extra} of ${capacity} sheets`}>
      <div className="flex h-full">
        <div className={cn("h-full", over ? "bg-destructive" : "bg-primary")} style={{ width: `${used}%` }} />
        {extra > 0 && (
          <div className={cn("h-full", over ? "bg-destructive/50" : "bg-primary/40")} style={{ width: `${added}%` }} />
        )}
      </div>
    </div>
  );
}

export function LocationSelect({
  value,
  onChange,
  locations,
  placeholder = "Choose a location…",
  id,
}: {
  value: number | null;
  onChange: (id: number | null) => void;
  locations: PaperLocation[];
  placeholder?: string;
  id?: string;
}) {
  return (
    <Select id={id} value={value ?? ""} onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}>
      <option value="">{placeholder}</option>
      {locations.map((l) => (
        <option key={l.id} value={l.id}>
          {l.path} ({l.sheets}/{l.capacity} sheets)
        </option>
      ))}
    </Select>
  );
}

/**
 * Side view of a binder: the paper stack fills it from the bottom, the newest sheets on top.
 * The document is the highlighted band; its height is computed from the sheets above and below it.
 */
export function BinderPictogram({
  capacity,
  total,
  below,
  sheets,
  className,
}: {
  capacity: number;
  total: number;
  below: number;
  sheets: number;
  className?: string;
}) {
  const height = 168;
  const inner = { x: 14, y: 10, w: 52, h: height - 20 };
  const full = Math.max(capacity, total);
  const scale = inner.h / full;
  const stackH = total * scale;
  const docH = Math.max(3, sheets * scale);
  const docY = inner.y + inner.h - below * scale - docH;
  const stackY = inner.y + inner.h - stackH;
  return (
    <svg
      viewBox={`0 0 80 ${height}`}
      className={cn("h-44 w-auto shrink-0", className)}
      role="img"
      aria-label={`Binder filled with ${total} of ${capacity} sheets; the document lies ${total - below - sheets} sheets below the top`}
    >
      {/* binder */}
      <rect x="4" y="2" width="72" height={height - 4} rx="5" className="fill-muted stroke-border" strokeWidth="2" />
      <rect x={inner.x} y={inner.y} width={inner.w} height={inner.h} rx="2" className="fill-background" />
      {/* stack of paper */}
      <rect x={inner.x} y={stackY} width={inner.w} height={stackH} className="fill-primary/15" />
      {Array.from({ length: Math.min(40, Math.floor(stackH / 5)) }, (_, i) => (
        <line
          key={i}
          x1={inner.x + 2}
          x2={inner.x + inner.w - 2}
          y1={inner.y + inner.h - (i + 1) * 5}
          y2={inner.y + inner.h - (i + 1) * 5}
          className="stroke-primary/20"
          strokeWidth="0.6"
        />
      ))}
      {/* this document */}
      <rect x={inner.x - 6} y={docY} width={inner.w + 12} height={docH} rx="1" className="fill-amber-500" />
      {/* top of the stack */}
      <line x1={inner.x} x2={inner.x + inner.w} y1={stackY} y2={stackY} className="stroke-primary" strokeWidth="1.5" />
    </svg>
  );
}

/** The document page card: does a paper original exist, where is it, and how deep in the stack. */
export function PaperCard({ doc }: { doc: DocumentDetail }) {
  const update = useUpdateDocument(doc.id);
  const locations = usePaperLocations();
  const invalidate = useInvalidatePaper();
  const paper = doc.paper;
  const pos = paper.position;
  const change = (body: Parameters<typeof update.mutate>[0]) =>
    update.mutate(body, { onSuccess: () => invalidate() });

  return (
    <Card className="p-3 text-sm">
      <h4 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        <Archive className="size-3.5" /> Paper original
      </h4>
      {!paper.has_paper ? (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-xs text-muted-foreground">No paper original (e.g. a digital PDF).</p>
          <Button size="sm" variant="outline" onClick={() => change({ has_paper: true })}>
            I have it on paper
          </Button>
        </div>
      ) : (
        <div className="flex flex-col gap-3">
          {pos && paper.location_path ? (
            <div className="flex gap-4">
              <BinderPictogram capacity={pos.capacity} total={pos.total_sheets} below={pos.sheets_below} sheets={pos.sheets} />
              <div className="flex min-w-0 flex-col gap-1.5">
                <p className="font-semibold">{paper.location_path}</p>
                <p>
                  Document <strong>{pos.index_from_top}</strong> of {pos.count} from the top
                  <span className="text-muted-foreground"> · {pos.sheets} sheet{pos.sheets === 1 ? "" : "s"}</span>
                </p>
                <p className="text-muted-foreground">
                  {pos.sheets_above === 0
                    ? "Lies on top of the stack."
                    : `About ${formatThickness(pos.sheets_above)} below the top (${pos.sheets_above} sheets above it).`}
                  {pos.sheets_below > 0 && ` ${formatThickness(pos.sheets_below)} from the bottom.`}
                </p>
                {pos.above && (
                  <p className="truncate text-xs">
                    <ArrowUp className="mr-1 inline size-3" />
                    Above it:{" "}
                    <Link className="underline-offset-2 hover:underline" to={`/documents/${pos.above.id}`}>
                      {pos.above.title}
                    </Link>
                  </p>
                )}
                {pos.below && (
                  <p className="truncate text-xs">
                    <ArrowDown className="mr-1 inline size-3" />
                    Below it:{" "}
                    <Link className="underline-offset-2 hover:underline" to={`/documents/${pos.below.id}`}>
                      {pos.below.title}
                    </Link>
                  </p>
                )}
                {paper.placed_at && (
                  <p className="text-xs text-muted-foreground">Put away {formatDateTime(paper.placed_at)}</p>
                )}
              </div>
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">
              Not put away yet. Use <Link className="underline" to="/paper">Paper → Put away</Link> to file everything
              scanned since last time, or choose a location here.
            </p>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <div className="min-w-48 flex-1">
              <LocationSelect
                locations={locations.data ?? []}
                value={paper.location_id ?? null}
                placeholder="Not put away"
                onChange={(id) => change(id === null ? { clear_paper_location: true } : { paper_location_id: id })}
              />
            </div>
            <Button size="sm" variant="ghost" onClick={() => change({ has_paper: false })}>
              No paper original
            </Button>
          </div>
        </div>
      )}
    </Card>
  );
}

/** "Put everything scanned since last time into this binder." */
export function PutAwayDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const pending = usePaperPending();
  const locations = usePaperLocations();
  const invalidate = useInvalidatePaper();
  const [target, setTarget] = useState<number | null>(null);
  const [newName, setNewName] = useState("");
  const [newParent, setNewParent] = useState<number | null>(null);
  const [newCapacity, setNewCapacity] = useState("500");
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  const [mode, setMode] = useState<"existing" | "new">("existing");
  // Which filing folder to put away from: "all", "none" (not filed) or a folder id.
  const [filing, setFiling] = useState<"all" | "none" | number>("all");

  const all = pending.data?.documents ?? [];
  const filings = useMemo(() => pendingFilings(all), [pending.data]);
  const docs = all.filter((d) => filing === "all" || (d.folder_id ?? "none") === filing);
  const list = locations.data ?? [];
  useEffect(() => {
    if (!open) return;
    setExcluded(new Set());
    setFiling("all");
    setNewName("");
    // Default: the most recently created location (usually the binder currently being filled).
    const latest = [...list].sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
    setTarget(latest?.id ?? null);
    setMode(list.length ? "existing" : "new");
  }, [open, locations.data]);

  useEffect(() => {
    // The chosen filing has been put away completely.
    if (filing !== "all" && !filings.some((f) => f.key === filing)) setFiling("all");
  }, [filings]);

  const chosen = docs.filter((d) => !excluded.has(d.id));
  const sheets = chosen.reduce((sum, d) => sum + d.sheets, 0);
  const location = list.find((l) => l.id === target);
  const capacity = mode === "new" ? Number(newCapacity) || 500 : location?.capacity ?? 0;
  const used = mode === "new" ? 0 : location?.sheets ?? 0;

  const place = useMutation({
    mutationFn: async () => {
      let locationId = target;
      if (mode === "new") {
        const created = await call(() =>
          client.POST("/api/v1/paper/locations", {
            body: { name: newName.trim(), parent_id: newParent, capacity: Number(newCapacity) || 500 },
          }),
        );
        locationId = created.id;
      }
      if (locationId === null) throw new Error("Choose a location");
      return call(() =>
        client.POST("/api/v1/paper/locations/{location_id}/place", {
          params: { path: { location_id: locationId } },
          body: { ids: chosen.map((d) => d.id) },
        }),
      );
    },
    onSuccess: (result) => {
      toast.success(`${result.placed} document${result.placed === 1 ? "" : "s"} put away in ${result.location.path}`);
      invalidate();
      onOpenChange(false);
    },
    onError: (e) => toast.error(e.message),
  });

  const ready = chosen.length > 0 && (mode === "new" ? newName.trim().length > 0 : target !== null);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Put away paper originals</DialogTitle>
          <DialogDescription>
            Everything scanned since last time goes into one location, in scan order with the newest on top. Untick
            documents you are not putting away now.
          </DialogDescription>
        </DialogHeader>
        {pending.isPending ? (
          <Spinner />
        ) : all.length === 0 ? (
          <p className="text-sm text-muted-foreground">Nothing to put away — every paper original has a location.</p>
        ) : (
          <div className="flex flex-col gap-4">
            {filings.length > 1 && (
              <div className="grid gap-1">
                <Label htmlFor="put-away-filing">From filing</Label>
                <Select
                  id="put-away-filing"
                  value={String(filing)}
                  onChange={(e) => {
                    const v = e.target.value;
                    setFiling(v === "all" || v === "none" ? v : Number(v));
                  }}
                >
                  <option value="all">Everything ({all.length})</option>
                  {filings.map((f) => (
                    <option key={f.key} value={String(f.key)}>
                      {f.label} ({f.count})
                    </option>
                  ))}
                </Select>
              </div>
            )}
            <div className="flex flex-col gap-2">
              <div className="inline-flex w-fit rounded-md border p-0.5 text-xs">
                {(["existing", "new"] as const).map((m) => (
                  <button
                    key={m}
                    type="button"
                    disabled={m === "existing" && !list.length}
                    onClick={() => setMode(m)}
                    className={cn(
                      "cursor-pointer rounded px-3 py-1 font-medium disabled:cursor-not-allowed disabled:opacity-50",
                      mode === m ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-muted",
                    )}
                  >
                    {m === "existing" ? "Existing location" : "New location"}
                  </button>
                ))}
              </div>
              {mode === "existing" ? (
                <LocationSelect locations={list} value={target} onChange={setTarget} />
              ) : (
                <div className="grid gap-2 sm:grid-cols-[1fr_1fr_7rem]">
                  <div className="grid gap-1">
                    <Label htmlFor="new-location-name">Name</Label>
                    <Input
                      id="new-location-name"
                      value={newName}
                      placeholder="e.g. Binder 2026-2"
                      onChange={(e) => setNewName(e.target.value)}
                    />
                  </div>
                  <div className="grid gap-1">
                    <Label htmlFor="new-location-parent">Inside</Label>
                    <LocationSelect
                      id="new-location-parent"
                      locations={list}
                      value={newParent}
                      onChange={setNewParent}
                      placeholder="— (top level)"
                    />
                  </div>
                  <div className="grid gap-1">
                    <Label htmlFor="new-location-capacity">Capacity</Label>
                    <Input
                      id="new-location-capacity"
                      type="number"
                      min={1}
                      value={newCapacity}
                      onChange={(e) => setNewCapacity(e.target.value)}
                    />
                  </div>
                </div>
              )}
              {(mode === "new" || location) && (
                <div className="grid gap-1 text-xs text-muted-foreground">
                  <FillBar sheets={used} capacity={capacity} extra={sheets} />
                  <span>
                    {used + sheets} of {capacity} sheets after putting away {sheets} sheet{sheets === 1 ? "" : "s"} (≈{" "}
                    {formatThickness(sheets)})
                    {used + sheets > capacity && <strong className="text-destructive"> — more than fits</strong>}
                  </span>
                </div>
              )}
            </div>
            <div className="rounded-md border">
              <div className="flex items-center gap-2 border-b px-3 py-2 text-xs font-medium text-muted-foreground">
                <Checkbox
                  checked={chosen.length === docs.length ? true : chosen.length === 0 ? false : "indeterminate"}
                  onCheckedChange={(v) =>
                    setExcluded((current) => {
                      const next = new Set(current);
                      for (const d of docs) {
                        if (v === true) next.delete(d.id);
                        else next.add(d.id);
                      }
                      return next;
                    })
                  }
                  aria-label="Select all"
                />
                <span className="flex-1">
                  {chosen.length} of {docs.length} documents
                </span>
                <span>Scanned · sheets</span>
              </div>
              <ul className="max-h-72 divide-y overflow-y-auto text-sm">
                {[...docs].reverse().map((d) => (
                  <li key={d.id} className="flex items-center gap-2 px-3 py-1.5">
                    <Checkbox
                      checked={!excluded.has(d.id)}
                      onCheckedChange={(v) =>
                        setExcluded((current) => {
                          const next = new Set(current);
                          if (v === true) next.delete(d.id);
                          else next.add(d.id);
                          return next;
                        })
                      }
                      aria-label={`Put away ${d.title}`}
                    />
                    <span className="min-w-0 flex-1 truncate">
                      {d.title}
                      {d.correspondent && <span className="text-muted-foreground"> · {d.correspondent}</span>}
                    </span>
                    <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                      {formatDate(d.uploaded_at)} · {d.sheets}
                    </span>
                  </li>
                ))}
              </ul>
              <p className="border-t px-3 py-1.5 text-xs text-muted-foreground">
                Listed as they will lie in the location: newest on top.
              </p>
            </div>
          </div>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={() => place.mutate()} disabled={!ready || place.isPending}>
            <Archive /> Put away {chosen.length || ""}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** The filing folders that have documents waiting to be put away, "No folder" last. */
function pendingFilings(docs: PendingDocument[]) {
  const byKey = new Map<number | "none", { key: number | "none"; label: string; count: number }>();
  for (const d of docs) {
    const key = d.folder_id ?? "none";
    const entry = byKey.get(key) ?? { key, label: d.folder ?? "No folder", count: 0 };
    entry.count += 1;
    byKey.set(key, entry);
  }
  return [...byKey.values()].sort((a, b) =>
    a.key === "none" ? 1 : b.key === "none" ? -1 : a.label.localeCompare(b.label),
  );
}

export function NewLocationButton({ parent }: { parent?: PaperLocation }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [capacity, setCapacity] = useState("500");
  const invalidate = useInvalidatePaper();
  const create = useMutation({
    mutationFn: () =>
      call(() =>
        client.POST("/api/v1/paper/locations", {
          body: { name: name.trim(), parent_id: parent?.id ?? null, capacity: Number(capacity) || 500 },
        }),
      ),
    onSuccess: (loc) => {
      toast.success(`Created ${loc.path}`);
      invalidate();
      setOpen(false);
      setName("");
    },
    onError: (e) => toast.error(e.message),
  });
  return (
    <>
      <Button size={parent ? "icon-sm" : "sm"} variant={parent ? "ghost" : "default"} onClick={() => setOpen(true)} title={parent ? `New location inside ${parent.name}` : undefined}>
        <Plus /> {parent ? null : "New location"}
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>{parent ? `New location in ${parent.path}` : "New location"}</DialogTitle>
            <DialogDescription>A cabinet, shelf, binder or box. Capacity is in sheets (an 8 cm binder ≈ 500).</DialogDescription>
          </DialogHeader>
          <form
            className="grid gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              if (name.trim()) create.mutate();
            }}
          >
            <div className="grid gap-1">
              <Label htmlFor="location-name">Name</Label>
              <Input id="location-name" autoFocus value={name} onChange={(e) => setName(e.target.value)} />
            </div>
            <div className="grid gap-1">
              <Label htmlFor="location-capacity">Capacity (sheets)</Label>
              <Input
                id="location-capacity"
                type="number"
                min={1}
                value={capacity}
                onChange={(e) => setCapacity(e.target.value)}
              />
            </div>
            <DialogFooter>
              <Button type="submit" disabled={!name.trim() || create.isPending}>
                Create
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  );
}

export function useLocationTree(locations: PaperLocation[] | undefined) {
  return useMemo(() => {
    const children = new Map<number | null, PaperLocation[]>();
    for (const l of locations ?? []) {
      const list = children.get(l.parent_id ?? null) ?? [];
      list.push(l);
      children.set(l.parent_id ?? null, list);
    }
    return children;
  }, [locations]);
}
