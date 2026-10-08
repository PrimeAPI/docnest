import type { FolderOut } from "@/api/queries";

export type FolderNode = FolderOut & { children: FolderNode[]; depth: number; total: number };

const byName = (a: { name: string }, b: { name: string }) =>
  a.name.localeCompare(b.name, undefined, { sensitivity: "base", numeric: true });

/** Build the folder tree; `total` counts documents in the folder and all subfolders. */
export function buildTree(folders: FolderOut[]): FolderNode[] {
  const nodes = new Map<number, FolderNode>(
    folders.map((f) => [f.id, { ...f, children: [], depth: 0, total: f.document_count }]),
  );
  const roots: FolderNode[] = [];
  for (const node of nodes.values()) {
    const parent = node.parent_id != null ? nodes.get(node.parent_id) : undefined;
    if (parent) parent.children.push(node);
    else roots.push(node);
  }
  const finish = (list: FolderNode[], depth: number): number => {
    list.sort(byName);
    let sum = 0;
    for (const node of list) {
      node.depth = depth;
      node.total = node.document_count + finish(node.children, depth + 1);
      sum += node.total;
    }
    return sum;
  };
  finish(roots, 0);
  return roots;
}

/** Depth-first list of all nodes, e.g. for indented <select> options. */
export function flatten(tree: FolderNode[]): FolderNode[] {
  return tree.flatMap((node) => [node, ...flatten(node.children)]);
}

/** Ids of a folder and all its descendants. */
export function subtreeIds(folders: FolderOut[], id: number): Set<number> {
  const ids = new Set([id]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const f of folders) {
      if (f.parent_id != null && ids.has(f.parent_id) && !ids.has(f.id)) {
        ids.add(f.id);
        grew = true;
      }
    }
  }
  return ids;
}

/** The folder and its ancestors, root first. */
export function ancestry(folders: FolderOut[], id: number): FolderOut[] {
  const byId = new Map(folders.map((f) => [f.id, f]));
  const chain: FolderOut[] = [];
  let current = byId.get(id);
  while (current && chain.length < 50) {
    chain.unshift(current);
    current = current.parent_id != null ? byId.get(current.parent_id) : undefined;
  }
  return chain;
}

// --- Drag & drop ----------------------------------------------------------------

export const DOCS_MIME = "application/x-docnest-documents";
export const FOLDER_MIME = "application/x-docnest-folder";

export function dragDocuments(e: React.DragEvent, ids: string[]) {
  e.dataTransfer.setData(DOCS_MIME, JSON.stringify(ids));
  e.dataTransfer.setData("text/plain", `${ids.length} document${ids.length === 1 ? "" : "s"}`);
  e.dataTransfer.effectAllowed = "move";
}

export function draggedDocuments(e: React.DragEvent): string[] | null {
  const raw = e.dataTransfer.getData(DOCS_MIME);
  if (!raw) return null;
  try {
    const ids = JSON.parse(raw);
    return Array.isArray(ids) ? ids.filter((x): x is string => typeof x === "string") : null;
  } catch {
    return null;
  }
}
