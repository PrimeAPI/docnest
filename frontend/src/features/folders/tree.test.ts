import { describe, expect, it } from "vitest";
import type { FolderOut } from "@/api/queries";
import { ancestry, buildTree, flatten, subtreeIds } from "./tree";

const folder = (id: number, name: string, parent_id: number | null, document_count = 0): FolderOut => ({
  id,
  name,
  parent_id,
  path: name,
  color: "slate",
  document_count,
});

const FOLDERS = [
  folder(1, "Private", null, 2),
  folder(2, "taxes", 1, 1),
  folder(3, "2024", 2, 4),
  folder(4, "Business", null),
  folder(5, "Car", 1),
];

describe("folder tree", () => {
  it("nests, sorts by name and sums documents of subfolders", () => {
    const tree = buildTree(FOLDERS);
    expect(tree.map((n) => n.name)).toEqual(["Business", "Private"]);
    const priv = tree[1];
    expect(priv.children.map((n) => n.name)).toEqual(["Car", "taxes"]);
    expect(priv.total).toBe(7);
    expect(flatten(tree).map((n) => [n.name, n.depth])).toEqual([
      ["Business", 0],
      ["Private", 0],
      ["Car", 1],
      ["taxes", 1],
      ["2024", 2],
    ]);
  });

  it("finds descendants and ancestors", () => {
    expect([...subtreeIds(FOLDERS, 1)].sort()).toEqual([1, 2, 3, 5]);
    expect(ancestry(FOLDERS, 3).map((f) => f.name)).toEqual(["Private", "taxes", "2024"]);
  });
});
