import { describe, expect, it } from "vitest";
import {
  effect,
  initialPlan,
  joinWithPrevious,
  leaveOut,
  mergeAll,
  movePages,
  nudge,
  planFrom,
  splitAt,
  toRequest,
  unchangedSource,
} from "./page-plan";

const sources = [
  { id: "a", pages: 2 },
  { id: "b", pages: 1 },
];

describe("page plan", () => {
  it("starts with every document as it is, which changes nothing", () => {
    const plan = initialPlan(sources);
    expect(plan.outputs.map((o) => o.pages)).toEqual([["a:1", "a:2"], ["b:1"]]);
    expect(effect(plan, sources)).toMatchObject({ created: 0, trashed: [], changes: false });
  });

  it("merges: one new document, both sources to the trash", () => {
    const plan = mergeAll(initialPlan(sources));
    expect(plan.outputs[0].pages).toEqual(["a:1", "a:2", "b:1"]);
    expect(effect(plan, sources)).toMatchObject({ created: 1, kept: [], trashed: ["a", "b"] });
    expect(toRequest(plan, sources).outputs[0].pages[2]).toEqual({ document: "b", page: 1 });
  });

  it("splits and joins again", () => {
    const start = initialPlan(sources);
    expect(splitAt(start, "nope", 1)).toBe(start);
    let plan = splitAt(start, start.outputs[0].key, 1);
    expect(plan.outputs.map((o) => o.pages)).toEqual([["a:1"], ["a:2"], ["b:1"]]);
    expect(effect(plan, sources)).toMatchObject({ created: 2, kept: ["b"], trashed: ["a"] });
    plan = joinWithPrevious(plan, plan.outputs[1].key);
    expect(unchangedSource(plan.outputs[0], sources)).toBe("a");
  });

  it("leaves pages out and moves them, keeping order", () => {
    const start = initialPlan(sources);
    let plan = leaveOut(start, ["a:2"]);
    expect(plan.leftOut).toEqual(["a:2"]);
    expect(effect(plan, sources)).toMatchObject({ created: 1, dropped: 1, trashed: ["a"] });
    plan = movePages(plan, ["a:2"], plan.outputs[1].key, 0);
    expect(plan.outputs[1].pages).toEqual(["a:2", "b:1"]);
    plan = movePages(plan, ["b:1"], null);
    expect(plan.outputs.map((o) => o.pages)).toEqual([["a:1"], ["a:2"], ["b:1"]]);
  });

  it("reorders within a document", () => {
    const plan = nudge(initialPlan(sources), "a:2", -1);
    expect(plan.outputs[0].pages).toEqual(["a:2", "a:1"]);
    expect(effect(plan, sources).trashed).toEqual(["a"]);
  });

  it("takes a suggestion's outputs and leaves the rest out", () => {
    const plan = planFrom(sources, [{ pages: [["a", 1]], title: "" }]);
    expect(plan.leftOut).toEqual(["a:2", "b:1"]);
  });
});
