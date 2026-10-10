import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ChevronLeft,
  ChevronRight,
  Folder,
  FolderOpen,
  FolderPlus,
  FolderTree,
  Inbox,
  Pencil,
  Search,
  Trash2,
} from "lucide-react";
import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { Link, NavLink, useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { call, client } from "@/api/client";
import { type FolderOut, keys, useDocuments, useFolders, useInvalidateDocuments } from "@/api/queries";
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
import { EmptyState, ErrorNote, PageHeader, Spinner } from "@/components/ui/misc";
import { BulkBar } from "@/features/documents/document-row";
import { DocumentCollection, useViewMode, ViewSwitch } from "@/features/documents/document-views";
import { createFolderPath, useMoveDocuments } from "@/features/folders/folder-ui";
import {
  ancestry,
  buildTree,
  DOCS_MIME,
  draggedDocuments,
  FOLDER_MIME,
  type FolderNode,
  flatten,
  subtreeIds,
} from "@/features/folders/tree";
import { cn, dotClass, iconClass, TAG_COLORS } from "@/lib/utils";

const PAGE_SIZE = 25;
const EXPANDED_KEY = "docnest-filing-expanded";

type Selection = { kind: "root" } | { kind: "unfiled" } | { kind: "folder"; id: number };

function useSelection(): Selection {
  const { folderId } = useParams();
  if (folderId === "unfiled") return { kind: "unfiled" };
  const id = Number(folderId);
  return folderId && Number.isFinite(id) ? { kind: "folder", id } : { kind: "root" };
}

function loadExpanded(): Set<number> {
  try {
    return new Set(JSON.parse(localStorage.getItem(EXPANDED_KEY) ?? "[]"));
  } catch {
    return new Set();
  }
}

type FolderUpdate = { id: number; name?: string; color?: string; parent_id?: number; move_to_root?: boolean };

function useFolderActions() {
  const qc = useQueryClient();
  const invalidate = useInvalidateDocuments();
  const onError = (e: Error) => toast.error(e.message);
  const done = () => {
    qc.invalidateQueries({ queryKey: keys.folders });
    invalidate();
  };
  const update = useMutation({
    mutationFn: ({ id, ...body }: FolderUpdate) =>
      call(() =>
        client.PATCH("/api/v1/folders/{folder_id}", {
          params: { path: { folder_id: id } },
          body: { move_to_root: false, ...body },
        }),
      ),
    onSuccess: done,
    onError,
  });
  const remove = useMutation({
    mutationFn: (id: number) =>
      call(() => client.DELETE("/api/v1/folders/{folder_id}", { params: { path: { folder_id: id } } })),
    onSuccess: done,
    onError,
  });
  return { update, remove };
}

type DialogState = null | { mode: "create"; parent: FolderNode | null } | { mode: "edit"; folder: FolderNode };

type DropTarget = {
  id: number | null; // null = top level (folders) / unfiled (documents)
  label: string;
  documents?: boolean;
  folders?: boolean;
};

/** Shared drop handling: documents are moved into the folder, folders are re-parented. */
function useDropTarget(target: DropTarget, folders: FolderOut[]) {
  const [over, setOver] = useState(0);
  const move = useMoveDocuments();
  const { update } = useFolderActions();
  const { documents = true, folders: acceptFolders = true } = target;
  const accepts = (e: React.DragEvent) =>
    (documents && e.dataTransfer.types.includes(DOCS_MIME)) ||
    (acceptFolders && e.dataTransfer.types.includes(FOLDER_MIME));
  return {
    over: over > 0,
    props: {
      onDragEnter: (e: React.DragEvent) => {
        if (!accepts(e)) return;
        e.preventDefault();
        setOver((n) => n + 1);
      },
      onDragOver: (e: React.DragEvent) => {
        if (!accepts(e)) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
      },
      onDragLeave: (e: React.DragEvent) => {
        if (accepts(e)) setOver((n) => Math.max(0, n - 1));
      },
      onDrop: (e: React.DragEvent) => {
        if (!accepts(e)) return;
        e.preventDefault();
        e.stopPropagation();
        setOver(0);
        const ids = documents ? draggedDocuments(e) : null;
        if (ids?.length) {
          move.mutate({ ids, folderId: target.id, label: target.label });
          return;
        }
        const folderId = acceptFolders ? Number(e.dataTransfer.getData(FOLDER_MIME)) : 0;
        if (!folderId) return;
        const dragged = folders.find((f) => f.id === folderId);
        if (!dragged || folderId === target.id || dragged.parent_id === target.id) return;
        if (target.id !== null && subtreeIds(folders, folderId).has(target.id)) {
          toast.error("A folder cannot be moved into itself");
          return;
        }
        update.mutate(
          target.id === null ? { id: folderId, move_to_root: true } : { id: folderId, parent_id: target.id },
          { onSuccess: () => toast.success(`Moved “${dragged.name}” to ${target.label}`) },
        );
      },
    },
  };
}

export function FilingPage() {
  const selection = useSelection();
  const folders = useFolders();
  const all = folders.data ?? [];
  const tree = useMemo(() => buildTree(all), [all]);
  const nodes = useMemo(() => new Map(flatten(tree).map((n) => [n.id, n])), [tree]);
  const current = selection.kind === "folder" ? nodes.get(selection.id) : undefined;
  const unfiled = useDocuments({ unfiled: true, page_size: 1 });
  const [dialog, setDialog] = useState<DialogState>(null);

  const [expanded, setExpanded] = useState<Set<number>>(loadExpanded);
  const toggle = useCallback(
    (id: number, open?: boolean) =>
      setExpanded((prev) => {
        const next = new Set(prev);
        if (open ?? !next.has(id)) next.add(id);
        else next.delete(id);
        try {
          localStorage.setItem(EXPANDED_KEY, JSON.stringify([...next]));
        } catch {
          /* storage unavailable */
        }
        return next;
      }),
    [],
  );
  // Keep the selected folder visible in the tree.
  const selectedId = selection.kind === "folder" ? selection.id : null;
  useEffect(() => {
    if (selectedId === null || !all.length) return;
    const parents = ancestry(all, selectedId).slice(0, -1);
    setExpanded((prev) =>
      parents.every((p) => prev.has(p.id)) ? prev : new Set([...prev, ...parents.map((p) => p.id)]),
    );
  }, [selectedId, all]);

  return (
    <>
      <PageHeader
        title="Filing"
        description="Your folders. Drag documents onto a folder to file them — or select several and use “Move to…”."
        actions={
          <Button variant="outline" onClick={() => setDialog({ mode: "create", parent: null })}>
            <FolderPlus /> New folder
          </Button>
        }
      />
      <div className="flex flex-col gap-6 lg:flex-row">
        <aside className="lg:w-72 lg:shrink-0">
          <Card className="p-2 lg:sticky lg:top-0">
            <TreeRootRow folders={all} />
            <UnfiledRow count={unfiled.data?.total} active={selection.kind === "unfiled"} folders={all} />
            <div className="my-1 border-t" />
            {folders.isPending && <Spinner className="m-2" />}
            {tree.map((node) => (
              <TreeNode
                key={node.id}
                node={node}
                folders={all}
                expanded={expanded}
                onToggle={toggle}
                activeId={selection.kind === "folder" ? selection.id : null}
              />
            ))}
            {folders.data && !tree.length && (
              <p className="px-2 py-3 text-sm text-muted-foreground">
                No folders yet. Create one, or let the scanner create them by sending a <code>bucket</code>.
              </p>
            )}
          </Card>
        </aside>
        <div className="min-w-0 flex-1">
          <ErrorNote error={folders.error} />
          {selection.kind === "folder" && !current && folders.data ? (
            <EmptyState icon={<Folder />} title="Folder not found">
              It may have been deleted. <Link to="/filing" className="underline">Back to all folders</Link>
            </EmptyState>
          ) : selection.kind === "folder" && current ? (
            <FolderView
              key={current.id}
              folder={current}
              folders={all}
              onCreate={() => setDialog({ mode: "create", parent: current })}
              onEdit={() => setDialog({ mode: "edit", folder: current })}
            />
          ) : selection.kind === "unfiled" ? (
            <>
              <Breadcrumb folders={all} trail={[]} last="Unfiled" />
              <p className="mb-4 text-sm text-muted-foreground">
                Documents that are not in any folder yet. Drag them onto a folder on the left.
              </p>
              <DocumentList query={{ unfiled: true }} empty="Everything is filed. Nice!" />
            </>
          ) : (
            <RootView tree={tree} folders={all} unfiledCount={unfiled.data?.total} />
          )}
        </div>
      </div>
      <FolderDialog state={dialog} folders={all} onClose={() => setDialog(null)} />
    </>
  );
}

// --- Tree -------------------------------------------------------------------------

function TreeRootRow({ folders }: { folders: FolderOut[] }) {
  const drop = useDropTarget({ id: null, label: "the top level", documents: false }, folders);
  return (
    <NavLink
      to="/filing"
      end
      {...drop.props}
      className={({ isActive }) =>
        cn(
          "flex items-center gap-2 rounded-md px-2 py-1.5 text-sm font-medium hover:bg-muted",
          isActive && "bg-accent text-accent-foreground",
          drop.over && "ring-2 ring-primary",
        )
      }
    >
      <FolderTree className="size-4 text-muted-foreground" />
      All folders
    </NavLink>
  );
}

function UnfiledRow({ count, active, folders }: { count?: number; active: boolean; folders: FolderOut[] }) {
  const drop = useDropTarget({ id: null, label: "Unfiled", folders: false }, folders);
  return (
    <Link
      to="/filing/unfiled"
      {...drop.props}
      className={cn(
        "flex items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-muted",
        active && "bg-accent text-accent-foreground",
        drop.over && "ring-2 ring-primary",
      )}
    >
      <Inbox className="size-4 text-muted-foreground" />
      <span className="flex-1">Unfiled</span>
      {!!count && (
        <span className="rounded-full bg-primary px-1.5 py-0.5 text-[10px] font-semibold leading-none text-primary-foreground">
          {count}
        </span>
      )}
    </Link>
  );
}

function TreeNode({
  node,
  folders,
  expanded,
  onToggle,
  activeId,
}: {
  node: FolderNode;
  folders: FolderOut[];
  expanded: Set<number>;
  onToggle: (id: number, open?: boolean) => void;
  activeId: number | null;
}) {
  const open = expanded.has(node.id);
  const drop = useDropTarget({ id: node.id, label: node.name }, folders);
  const active = activeId === node.id;
  const hasChildren = node.children.length > 0;
  // Hovering a closed folder while dragging opens it, so deeper folders become reachable.
  useEffect(() => {
    if (!drop.over || open || !hasChildren) return;
    const timer = setTimeout(() => onToggle(node.id, true), 600);
    return () => clearTimeout(timer);
  }, [drop.over, open, hasChildren, node.id, onToggle]);
  return (
    <div>
      <div
        {...drop.props}
        draggable
        onDragStart={(e) => {
          e.dataTransfer.setData(FOLDER_MIME, String(node.id));
          e.dataTransfer.setData("text/plain", node.path);
          e.dataTransfer.effectAllowed = "move";
        }}
        className={cn(
          "group flex items-center gap-1 rounded-md pr-2 text-sm hover:bg-muted",
          active && "bg-accent text-accent-foreground hover:bg-accent",
          drop.over && "ring-2 ring-primary",
        )}
        style={{ paddingLeft: `${node.depth * 0.9}rem` }}
      >
        <button
          type="button"
          aria-label={open ? "Collapse" : "Expand"}
          className={cn(
            "flex size-6 shrink-0 cursor-pointer items-center justify-center rounded text-muted-foreground hover:text-foreground",
            !hasChildren && "invisible",
          )}
          onClick={() => onToggle(node.id)}
        >
          <ChevronRight className={cn("size-3.5 transition-transform", open && "rotate-90")} />
        </button>
        <Link to={`/filing/${node.id}`} className="flex min-w-0 flex-1 items-center gap-2 py-1.5">
          {active || open ? (
            <FolderOpen className={cn("size-4 shrink-0", iconClass(node.color))} />
          ) : (
            <Folder className={cn("size-4 shrink-0", iconClass(node.color))} />
          )}
          <span className={cn("truncate", active && "font-medium")}>{node.name}</span>
        </Link>
        {node.total > 0 && <span className="text-xs text-muted-foreground">{node.total}</span>}
      </div>
      {open &&
        node.children.map((child) => (
          <TreeNode
            key={child.id}
            node={child}
            folders={folders}
            expanded={expanded}
            onToggle={onToggle}
            activeId={activeId}
          />
        ))}
    </div>
  );
}

// --- Main views -------------------------------------------------------------------

function Breadcrumb({ folders, trail, last, actions }: { folders: FolderOut[]; trail: FolderOut[]; last: string; actions?: ReactNode }) {
  const root = useDropTarget({ id: null, label: "the top level", documents: false }, folders);
  return (
    <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
      <nav className="flex min-w-0 flex-wrap items-center gap-1 text-lg font-semibold" aria-label="Breadcrumb">
        <Link
          to="/filing"
          {...root.props}
          className={cn("rounded px-1 text-muted-foreground hover:text-foreground", root.over && "ring-2 ring-primary")}
        >
          Filing
        </Link>
        {trail.map((f) => (
          <span key={f.id} className="flex items-center gap-1">
            <ChevronRight className="size-4 text-muted-foreground" />
            <BreadcrumbLink folder={f} folders={folders} />
          </span>
        ))}
        <ChevronRight className="size-4 text-muted-foreground" />
        <span className="truncate px-1">{last}</span>
      </nav>
      {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
    </div>
  );
}

function BreadcrumbLink({ folder, folders }: { folder: FolderOut; folders: FolderOut[] }) {
  const drop = useDropTarget({ id: folder.id, label: folder.name }, folders);
  return (
    <Link
      to={`/filing/${folder.id}`}
      {...drop.props}
      className={cn("rounded px-1 text-muted-foreground hover:text-foreground", drop.over && "ring-2 ring-primary")}
    >
      {folder.name}
    </Link>
  );
}

function FolderView({
  folder,
  folders,
  onCreate,
  onEdit,
}: {
  folder: FolderNode;
  folders: FolderOut[];
  onCreate: () => void;
  onEdit: () => void;
}) {
  const navigate = useNavigate();
  const { remove } = useFolderActions();
  const [withSubfolders, setWithSubfolders] = useState(false);
  const trail = ancestry(folders, folder.id).slice(0, -1);

  const deleteFolder = () => {
    if (folder.total > 0) {
      toast.error("Move the documents out of this folder (and its subfolders) first.");
      return;
    }
    const sub = folder.children.length ? " and its subfolders" : "";
    if (!confirm(`Delete folder “${folder.name}”${sub}?`)) return;
    remove.mutate(folder.id, {
      onSuccess: () => navigate(folder.parent_id ? `/filing/${folder.parent_id}` : "/filing"),
    });
  };

  return (
    <>
      <Breadcrumb
        folders={folders}
        trail={trail}
        last={folder.name}
        actions={
          <>
            <Button size="sm" variant="outline" onClick={onCreate}>
              <FolderPlus /> Subfolder
            </Button>
            <Button size="sm" variant="outline" onClick={onEdit}>
              <Pencil /> Edit
            </Button>
            <Button size="sm" variant="ghost" onClick={deleteFolder} title="Delete folder">
              <Trash2 />
            </Button>
          </>
        }
      />
      {folder.children.length > 0 && <FolderGrid nodes={folder.children} folders={folders} />}
      <div className="mb-2 mt-4 flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold">
          Documents{" "}
          <span className="font-normal text-muted-foreground">
            ({withSubfolders ? folder.total : folder.document_count})
          </span>
        </h3>
        {folder.children.length > 0 && (
          <label className="flex cursor-pointer items-center gap-2 text-sm">
            <Checkbox checked={withSubfolders} onCheckedChange={(v) => setWithSubfolders(v === true)} />
            Include subfolders
          </label>
        )}
      </div>
      <DocumentList
        query={{ folder: [folder.id], subfolders: withSubfolders }}
        showFolder={withSubfolders}
        empty={
          folder.children.length
            ? "No documents directly in this folder."
            : "This folder is empty. Drag documents here, or use “Move to…” on any document."
        }
      />
    </>
  );
}

function RootView({ tree, folders, unfiledCount }: { tree: FolderNode[]; folders: FolderOut[]; unfiledCount?: number }) {
  return (
    <>
      {tree.length > 0 ? (
        <FolderGrid nodes={tree} folders={folders} />
      ) : (
        <EmptyState icon={<FolderTree />} title="No folders yet">
          Create your first folder with “New folder”. Scanners create folders automatically from the <code>bucket</code>{" "}
          they send, e.g. <code>Private/Taxes/2024</code>.
        </EmptyState>
      )}
      <div className="mb-2 mt-6 flex items-center justify-between">
        <h3 className="text-sm font-semibold">
          Unfiled documents <span className="font-normal text-muted-foreground">({unfiledCount ?? "…"})</span>
        </h3>
        {!!unfiledCount && (
          <Link to="/filing/unfiled" className="text-sm text-primary hover:underline">
            Show all
          </Link>
        )}
      </div>
      <DocumentList query={{ unfiled: true }} empty="Everything is filed. Nice!" />
    </>
  );
}

function FolderGrid({ nodes, folders }: { nodes: FolderNode[]; folders: FolderOut[] }) {
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-4">
      {nodes.map((node) => (
        <FolderCard key={node.id} node={node} folders={folders} />
      ))}
    </div>
  );
}

function FolderCard({ node, folders }: { node: FolderNode; folders: FolderOut[] }) {
  const drop = useDropTarget({ id: node.id, label: node.name }, folders);
  return (
    <Link
      to={`/filing/${node.id}`}
      {...drop.props}
      draggable
      onDragStart={(e) => {
        e.dataTransfer.setData(FOLDER_MIME, String(node.id));
        e.dataTransfer.effectAllowed = "move";
      }}
      className={cn(
        "flex items-center gap-3 rounded-lg border bg-card px-3 py-3 transition-colors hover:bg-muted/60",
        drop.over && "border-primary bg-accent ring-2 ring-primary",
      )}
    >
      <Folder className={cn("size-6 shrink-0", iconClass(node.color))} />
      <div className="min-w-0">
        <div className="truncate text-sm font-medium">{node.name}</div>
        <div className="text-xs text-muted-foreground">
          {node.total} doc{node.total === 1 ? "" : "s"}
          {node.children.length > 0 && ` · ${node.children.length} folder${node.children.length === 1 ? "" : "s"}`}
        </div>
      </div>
    </Link>
  );
}

function DocumentList({
  query,
  empty,
  showFolder = false,
}: {
  query: { folder?: number[]; subfolders?: boolean; unfiled?: boolean };
  empty: string;
  showFolder?: boolean;
}) {
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<string[]>([]);
  // "Select all": every document of the list, also those on other pages.
  const [all, setAll] = useState<string[] | null>(null);
  const [view, setView] = useViewMode("page");
  // Quick search in this list: title, sender and the text of the pages.
  const [search, setSearch] = useState("");
  const [text, setText] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setText(search.trim()), 300);
    return () => clearTimeout(t);
  }, [search]);
  useEffect(() => setSearch(""), [JSON.stringify(query)]); // eslint-disable-line react-hooks/exhaustive-deps
  const listQuery = { ...query, ...(text ? { q: text, sort: "relevance" as const } : { sort: "-uploaded" as const }) };
  const docs = useDocuments({ ...listQuery, page, page_size: PAGE_SIZE });
  const key = JSON.stringify(listQuery);
  const total = docs.data?.total ?? 0;
  useEffect(() => {
    setPage(1);
    setSelected([]);
    setAll(null);
  }, [key]);
  useEffect(() => {
    // Documents were moved away or added: "all" no longer means the same documents.
    if (all && total !== all.length) {
      setAll(null);
      setSelected([]);
    }
  }, [all, total]);
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  // Documents that moved away disappear from the list; drop them from the selection.
  const visible = new Set(docs.data?.items.map((d) => d.id));
  const selection = all ? selected : selected.filter((id) => visible.has(id));
  const clear = () => {
    setSelected([]);
    setAll(null);
  };
  const selectAll = async (on: boolean) => {
    if (!on) return clear();
    try {
      const r = await call(() => client.GET("/api/v1/documents/ids", { params: { query: listQuery } }));
      setAll(r.ids);
      setSelected(r.ids);
      if (r.total > r.ids.length) toast.info(`Selected the first ${r.ids.length} of ${r.total} documents.`);
    } catch (e) {
      toast.error((e as Error).message);
    }
  };

  if (docs.isPending) return <Spinner />;
  if (!docs.data?.items.length && !text && !search) {
    return <p className="rounded-lg border border-dashed px-4 py-8 text-center text-sm text-muted-foreground">{empty}</p>;
  }
  const items = docs.data?.items ?? [];
  return (
    <>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="relative min-w-48 flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            type="search"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            onKeyDown={(e) => e.key === "Escape" && setSearch("")}
            placeholder={query.unfiled ? "Search unfiled documents…" : "Search in this folder — title, sender, text…"}
            aria-label="Search in this list"
            className="pl-8"
          />
        </div>
        <ViewSwitch mode={view} onChange={setView} />
      </div>
      <BulkBar ids={selection} onClear={clear} />
      {items.length > 0 && (
        <label className="mb-2 flex w-fit cursor-pointer items-center gap-2 px-3 text-sm text-muted-foreground">
          <Checkbox checked={all !== null && selection.length === all.length} onCheckedChange={(v) => selectAll(v === true)} />
          Select all {total} {text ? "found " : ""}document{total === 1 ? "" : "s"}
          {query.folder && !query.subfolders ? " in this folder" : ""}
        </label>
      )}
      {items.length ? (
        <DocumentCollection
          docs={items}
          mode={view}
          showFolder={showFolder}
          dim={docs.isFetching}
          selected={selection}
          onSelect={(id, v) => setSelected((s) => (v ? [...s, id] : s.filter((x) => x !== id)))}
        />
      ) : (
        <p className="rounded-lg border border-dashed px-4 py-8 text-center text-sm text-muted-foreground">
          Nothing matches “{search}”. Prefixes like “versich” work too.
        </p>
      )}
      {pages > 1 && (
        <div className="mt-4 flex items-center justify-between text-sm">
          <span className="text-muted-foreground">
            Page {page} of {pages}
          </span>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>
              <ChevronLeft /> Previous
            </Button>
            <Button variant="outline" size="sm" disabled={page >= pages} onClick={() => setPage(page + 1)}>
              Next <ChevronRight />
            </Button>
          </div>
        </div>
      )}
    </>
  );
}

// --- Create / edit dialog -------------------------------------------------------------

function FolderDialog({
  state,
  folders,
  onClose,
}: {
  state: DialogState;
  folders: FolderOut[];
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const { update } = useFolderActions();
  const [name, setName] = useState("");
  const [color, setColor] = useState("slate");
  const [parentId, setParentId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!state) return;
    if (state.mode === "edit") {
      setName(state.folder.name);
      setColor(state.folder.color);
      setParentId(state.folder.parent_id);
    } else {
      setName("");
      setColor("slate");
      setParentId(state.parent?.id ?? null);
    }
  }, [state]);

  const options = useMemo(() => {
    const excluded = state?.mode === "edit" ? subtreeIds(folders, state.folder.id) : new Set<number>();
    return flatten(buildTree(folders)).filter((f) => !excluded.has(f.id));
  }, [folders, state]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!state || !name.trim()) return;
    if (state.mode === "edit") {
      const f = state.folder;
      const moved = parentId !== f.parent_id;
      update.mutate(
        {
          id: f.id,
          name: name.trim(),
          color,
          ...(moved ? (parentId === null ? { move_to_root: true } : { parent_id: parentId }) : {}),
        },
        { onSuccess: onClose },
      );
      return;
    }
    setBusy(true);
    try {
      // "A/B" creates nested folders in one go.
      const created = await createFolderPath(folders, name, parentId);
      if (color !== "slate") {
        await call(() =>
          client.PATCH("/api/v1/folders/{folder_id}", { params: { path: { folder_id: created.id } }, body: { color, move_to_root: false } }),
        );
      }
      qc.invalidateQueries({ queryKey: keys.folders });
      onClose();
      navigate(`/filing/${created.id}`);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={!!state} onOpenChange={(v) => !v && onClose()}>
      <DialogContent>
        <form onSubmit={submit} className="grid gap-4">
          <DialogHeader>
            <DialogTitle>{state?.mode === "edit" ? "Edit folder" : "New folder"}</DialogTitle>
            <DialogDescription>
              {state?.mode === "edit"
                ? "Rename, recolor or move the folder. Its documents and subfolders move with it."
                : "Tip: “Taxes/2024” creates a folder with a subfolder."}
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-1.5">
            <Label htmlFor="folder-name">Name</Label>
            <Input id="folder-name" autoFocus value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="folder-parent">Inside</Label>
            <Select
              id="folder-parent"
              value={parentId ?? ""}
              onChange={(e) => setParentId(e.target.value ? Number(e.target.value) : null)}
            >
              <option value="">— Top level —</option>
              {options.map((f) => (
                <option key={f.id} value={f.id}>
                  {"   ".repeat(f.depth)}
                  {f.name}
                </option>
              ))}
            </Select>
          </div>
          <div className="grid gap-1.5">
            <Label>Color</Label>
            <div className="flex flex-wrap gap-1.5">
              {TAG_COLORS.map((c) => (
                <button
                  key={c}
                  type="button"
                  title={c}
                  onClick={() => setColor(c)}
                  className={cn(
                    "size-6 cursor-pointer rounded-full ring-offset-2 ring-offset-card",
                    dotClass(c),
                    color === c && "ring-2 ring-ring",
                  )}
                />
              ))}
            </div>
          </div>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={!name.trim() || busy || update.isPending}>
              {state?.mode === "edit" ? "Save" : "Create"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
