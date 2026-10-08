import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Folder, FolderInput, FolderPlus, Inbox } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import { call, client } from "@/api/client";
import { type FolderOut, keys, useFolders, useInvalidateDocuments } from "@/api/queries";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input, Select } from "@/components/ui/input";
import { Spinner } from "@/components/ui/misc";
import { iconClass } from "@/lib/utils";
import { buildTree, flatten } from "./tree";

/** Native select over the folder tree, with an "Unfiled" option (value null). */
export function FolderSelect({
  value,
  onChange,
  id,
  className,
}: {
  value: number | null;
  onChange: (id: number | null) => void;
  id?: string;
  className?: string;
}) {
  const folders = useFolders();
  const options = useMemo(() => flatten(buildTree(folders.data ?? [])), [folders.data]);
  return (
    <Select
      id={id}
      className={className}
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}
    >
      <option value="">— Unfiled —</option>
      {options.map((f) => (
        <option key={f.id} value={f.id}>
          {"   ".repeat(f.depth)}
          {f.name}
        </option>
      ))}
    </Select>
  );
}

/** Move documents into a folder (null = unfile). */
export function useMoveDocuments() {
  const invalidate = useInvalidateDocuments();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ ids, folderId }: { ids: string[]; folderId: number | null; label?: string }) =>
      call(() => client.POST("/api/v1/documents/bulk", { body: { ids, action: "move", folder_id: folderId } })),
    onSuccess: (r, { label }) => {
      invalidate();
      qc.invalidateQueries({ queryKey: ["document"] });
      toast.success(`Moved ${r.updated} document${r.updated === 1 ? "" : "s"} to ${label ?? "folder"}`);
    },
    onError: (e) => toast.error(e.message),
  });
}

/** Create "A/B/C" below `parentId`, reusing existing folders (case-insensitive). Returns the last one. */
export async function createFolderPath(
  folders: FolderOut[],
  path: string,
  parentId: number | null = null,
): Promise<FolderOut> {
  const names = path
    .split("/")
    .map((n) => n.trim())
    .filter(Boolean);
  if (!names.length) throw new Error("Folder name is required");
  let parent = parentId;
  let current: FolderOut | undefined;
  for (const name of names) {
    current = folders.find((f) => f.parent_id === parent && f.name.toLowerCase() === name.toLowerCase());
    if (!current) {
      current = await call(() => client.POST("/api/v1/folders", { body: { name, parent_id: parent } }));
      folders = [...folders, current];
    }
    parent = current.id;
  }
  return current!;
}

export function MoveToFolderDialog({
  ids,
  open,
  onOpenChange,
  onMoved,
}: {
  ids: string[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onMoved?: () => void;
}) {
  const folders = useFolders();
  const qc = useQueryClient();
  const move = useMoveDocuments();
  const [search, setSearch] = useState("");
  const [creating, setCreating] = useState(false);

  const all = useMemo(() => flatten(buildTree(folders.data ?? [])), [folders.data]);
  const needle = search.trim().toLowerCase();
  const visible = needle ? all.filter((f) => f.path.toLowerCase().includes(needle)) : all;
  const exact = all.some((f) => f.path.toLowerCase().replaceAll(" / ", "/") === needle.replace(/\s*\/\s*/g, "/"));

  const finish = (folderId: number | null, label: string) =>
    move.mutate(
      { ids, folderId, label },
      {
        onSuccess: () => {
          onOpenChange(false);
          setSearch("");
          onMoved?.();
        },
      },
    );

  const createAndMove = async () => {
    setCreating(true);
    try {
      const folder = await createFolderPath(folders.data ?? [], search);
      qc.invalidateQueries({ queryKey: keys.folders });
      finish(folder.id, folder.name);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setCreating(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        onOpenChange(v);
        if (!v) setSearch("");
      }}
    >
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Move to folder</DialogTitle>
          <DialogDescription>
            {ids.length} document{ids.length === 1 ? "" : "s"}. Type to search or to create a new folder — use “/” for
            subfolders.
          </DialogDescription>
        </DialogHeader>
        <Input
          autoFocus
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search folders…"
          onKeyDown={(e) => {
            if (e.key !== "Enter") return;
            e.preventDefault();
            if (visible.length === 1) finish(visible[0].id, visible[0].name);
            else if (needle && !exact && !visible.length) void createAndMove();
          }}
        />
        <div className="-mx-2 max-h-80 overflow-y-auto">
          {folders.isPending && <Spinner className="m-2" />}
          {!needle && (
            <FolderOption icon={<Inbox />} label="Unfiled" onClick={() => finish(null, "Unfiled")} />
          )}
          {visible.map((f) => (
            <FolderOption
              key={f.id}
              label={needle ? f.path : f.name}
              depth={needle ? 0 : f.depth}
              color={f.color}
              count={f.document_count}
              onClick={() => finish(f.id, f.name)}
            />
          ))}
          {needle && !exact && (
            <FolderOption
              icon={creating ? <Spinner /> : <FolderPlus />}
              label={`Create “${search.trim()}” and move`}
              onClick={() => void createAndMove()}
            />
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function FolderOption({
  label,
  depth = 0,
  color,
  count,
  icon,
  onClick,
}: {
  label: string;
  depth?: number;
  color?: string;
  count?: number;
  icon?: React.ReactNode;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex w-full cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm hover:bg-muted [&_svg]:size-4 [&_svg]:shrink-0"
      style={{ paddingLeft: `${0.5 + depth * 1.25}rem` }}
    >
      {icon ?? <Folder className={iconClass(color)} />}
      <span className="flex-1 truncate">{label}</span>
      {count !== undefined && count > 0 && <span className="text-xs text-muted-foreground">{count}</span>}
    </button>
  );
}

export function MoveButton({ ids, onMoved, size = "sm" }: { ids: string[]; onMoved?: () => void; size?: "sm" | "default" }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button size={size} variant="outline" onClick={() => setOpen(true)} disabled={!ids.length}>
        <FolderInput /> Move to…
      </Button>
      <MoveToFolderDialog ids={ids} open={open} onOpenChange={setOpen} onMoved={onMoved} />
    </>
  );
}
