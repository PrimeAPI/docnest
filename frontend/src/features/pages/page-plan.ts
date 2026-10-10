/**
 * What the page editor edits: which pages make up which documents afterwards.
 *
 * A page is "documentId:page". An output that holds all pages of one source, in order,
 * keeps that document unchanged; every other output becomes a new document, and every
 * source that is not kept unchanged goes to the trash — with its original file.
 */

export type PageKey = `${string}:${number}`;

export interface Output {
  key: string;
  title: string;
  pages: PageKey[];
}

export interface Plan {
  outputs: Output[];
  leftOut: PageKey[];
}

let counter = 0;
export const newKey = () => `o${++counter}`;

export const pageKey = (doc: string, page: number): PageKey => `${doc}:${page}`;

export function parseKey(key: PageKey): [string, number] {
  const at = key.lastIndexOf(":");
  return [key.slice(0, at), Number(key.slice(at + 1))];
}

export function allPages(doc: string, count: number): PageKey[] {
  return Array.from({ length: count }, (_, i) => pageKey(doc, i + 1));
}

/** Each source as it is. */
export function initialPlan(sources: { id: string; pages: number }[]): Plan {
  return { outputs: sources.map((s) => ({ key: newKey(), title: "", pages: allPages(s.id, s.pages) })), leftOut: [] };
}

/** A suggestion's outputs; pages of the sources it does not use are left out. */
export function planFrom(
  sources: { id: string; pages: number }[],
  outputs: { pages: [string, number][]; title: string }[],
): Plan {
  const used = new Set(outputs.flatMap((o) => o.pages.map(([d, n]) => pageKey(d, n))));
  return {
    outputs: outputs.map((o) => ({ key: newKey(), title: o.title, pages: o.pages.map(([d, n]) => pageKey(d, n)) })),
    leftOut: sources.flatMap((s) => allPages(s.id, s.pages)).filter((k) => !used.has(k)),
  };
}

/** The source an output keeps unchanged, if it does. */
export function unchangedSource(output: Output, sources: { id: string; pages: number }[]): string | null {
  if (!output.pages.length) return null;
  const doc = parseKey(output.pages[0])[0];
  const source = sources.find((s) => s.id === doc);
  if (!source || output.pages.length !== source.pages) return null;
  return output.pages.every((k, i) => k === pageKey(doc, i + 1)) ? doc : null;
}

function without(plan: Plan, keys: Set<PageKey>): Plan {
  return {
    outputs: plan.outputs.map((o) => ({ ...o, pages: o.pages.filter((k) => !keys.has(k)) })),
    leftOut: plan.leftOut.filter((k) => !keys.has(k)),
  };
}

const tidy = (plan: Plan): Plan => ({ ...plan, outputs: plan.outputs.filter((o) => o.pages.length) });

/** Move pages to an output (at `index`, default: the end), or to a new output when `to` is null. */
export function movePages(plan: Plan, keys: PageKey[], to: string | null, index?: number): Plan {
  const moving = new Set(keys);
  const ordered = [...plan.outputs.flatMap((o) => o.pages), ...plan.leftOut].filter((k) => moving.has(k));
  // Keep the order the user picked them in when they come from one place; else as they are.
  const pages = keys.length === ordered.length ? keys : ordered;
  const rest = without(plan, moving);
  if (to === null) return tidy({ ...rest, outputs: [...rest.outputs, { key: newKey(), title: "", pages }] });
  return tidy({
    ...rest,
    outputs: rest.outputs.map((o) => {
      if (o.key !== to) return o;
      // `index` counted in the output before the moved pages left it.
      const before = plan.outputs.find((x) => x.key === to)?.pages.slice(0, index ?? Infinity) ?? [];
      const at = before.filter((k) => !moving.has(k)).length;
      return { ...o, pages: [...o.pages.slice(0, at), ...pages, ...o.pages.slice(at)] };
    }),
  });
}

export function leaveOut(plan: Plan, keys: PageKey[]): Plan {
  const rest = without(plan, new Set(keys));
  return tidy({ ...rest, leftOut: [...rest.leftOut, ...keys] });
}

/** Cut an output in two before page `at` (an index into it). */
export function splitAt(plan: Plan, key: string, at: number): Plan {
  const i = plan.outputs.findIndex((o) => o.key === key);
  if (i < 0 || at <= 0 || at >= plan.outputs[i].pages.length) return plan;
  const o = plan.outputs[i];
  const outputs = [...plan.outputs];
  outputs.splice(i, 1, { ...o, pages: o.pages.slice(0, at) }, { key: newKey(), title: "", pages: o.pages.slice(at) });
  return { ...plan, outputs };
}

/** Append an output to the one before it. */
export function joinWithPrevious(plan: Plan, key: string): Plan {
  const i = plan.outputs.findIndex((o) => o.key === key);
  if (i <= 0) return plan;
  const outputs = [...plan.outputs];
  outputs.splice(i - 1, 2, { ...outputs[i - 1], pages: [...outputs[i - 1].pages, ...outputs[i].pages] });
  return { ...plan, outputs };
}

export function mergeAll(plan: Plan): Plan {
  if (plan.outputs.length < 2) return plan;
  return { ...plan, outputs: [{ key: newKey(), title: plan.outputs[0].title, pages: plan.outputs.flatMap((o) => o.pages) }] };
}

/** Move a page one place left or right within its output. */
export function nudge(plan: Plan, page: PageKey, by: -1 | 1): Plan {
  return {
    ...plan,
    outputs: plan.outputs.map((o) => {
      const i = o.pages.indexOf(page);
      const j = i + by;
      if (i < 0 || j < 0 || j >= o.pages.length) return o;
      const pages = [...o.pages];
      [pages[i], pages[j]] = [pages[j], pages[i]];
      return { ...o, pages };
    }),
  };
}

export interface Effect {
  created: number; // new documents
  kept: string[]; // sources that stay as they are
  trashed: string[]; // sources that go to the trash (restorable)
  dropped: number; // pages in no document afterwards (still in the trashed originals)
  changes: boolean;
}

export function effect(plan: Plan, sources: { id: string; pages: number }[]): Effect {
  const kept = plan.outputs.map((o) => unchangedSource(o, sources)).filter((d): d is string => d !== null);
  const trashed = sources.map((s) => s.id).filter((id) => !kept.includes(id));
  const created = plan.outputs.filter((o) => unchangedSource(o, sources) === null).length;
  return { created, kept, trashed, dropped: plan.leftOut.length, changes: trashed.length > 0 };
}

/** The request body of POST /alterations/compose. */
export function toRequest(plan: Plan, sources: { id: string }[]) {
  return {
    sources: sources.map((s) => s.id),
    outputs: plan.outputs.map((o) => ({
      title: o.title.trim(),
      pages: o.pages.map((k) => {
        const [document, page] = parseKey(k);
        return { document, page };
      }),
    })),
  };
}
