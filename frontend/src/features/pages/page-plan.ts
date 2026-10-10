/**
 * What the page editor edits: which pages make up which documents afterwards.
 *
 * A page is "documentId:physicalArchivePage". The first single-source output keeps its
 * document identity; visibility/order edits are view-only. Additional split parts and
 * multi-source outputs are new documents. Only sources with no retained output are trashed.
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

export interface PageSource {
  id: string;
  pages: number;
  visible?: number[];
}

const shownPages = (source: PageSource) =>
  source.visible?.map((n) => pageKey(source.id, n)) ?? allPages(source.id, source.pages);

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
export function initialPlan(sources: PageSource[]): Plan {
  const outputs = sources.map((s) => ({ key: newKey(), title: "", pages: shownPages(s) }));
  const shown = new Set(outputs.flatMap((o) => o.pages));
  return { outputs, leftOut: sources.flatMap((s) => allPages(s.id, s.pages)).filter((k) => !shown.has(k)) };
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
export function unchangedSource(output: Output, sources: PageSource[]): string | null {
  if (!output.pages.length) return null;
  const doc = parseKey(output.pages[0])[0];
  const source = sources.find((s) => s.id === doc);
  if (!source) return null;
  const baseline = shownPages(source);
  if (output.pages.length !== baseline.length) return null;
  return output.pages.every((k, i) => k === baseline[i]) ? doc : null;
}

/** The first single-source output keeps its identity; further split parts are new documents. */
export function retainedSource(output: Output, plan: Plan, sources: PageSource[]): string | null {
  if (!output.pages.length) return null;
  const id = parseKey(output.pages[0])[0];
  if (!sources.some((s) => s.id === id) || output.pages.some((p) => parseKey(p)[0] !== id)) return null;
  return plan.outputs.find((o) => o.pages.length > 0 && o.pages.every((p) => parseKey(p)[0] === id))?.key === output.key ? id : null;
}

/** Restore hidden pages to their existing document instead of making another document. */
export function putBack(plan: Plan, keys: PageKey[]): Plan {
  let next = plan;
  const ids = [...new Set(keys.map((k) => parseKey(k)[0]))];
  for (const id of ids) {
    const output = next.outputs.find((o) => o.pages.every((p) => parseKey(p)[0] === id));
    next = movePages(next, keys.filter((k) => parseKey(k)[0] === id), output?.key ?? null);
  }
  return next;
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
  kept: string[]; // source identities retained, including view-only updates
  updated: string[]; // same document, different Enhanced presentation
  trashed: string[]; // sources that go to the trash (restorable)
  dropped: number; // pages excluded from outputs, still retained in source files
  changes: boolean;
}

export function effect(plan: Plan, sources: PageSource[]): Effect {
  const kept = plan.outputs.map((o) => retainedSource(o, plan, sources)).filter((d): d is string => d !== null);
  const updated = plan.outputs.filter((o) => retainedSource(o, plan, sources) !== null && unchangedSource(o, sources) === null)
    .map((o) => parseKey(o.pages[0])[0]);
  const trashed = sources.map((s) => s.id).filter((id) => !kept.includes(id));
  const created = plan.outputs.filter((o) => retainedSource(o, plan, sources) === null).length;
  return { created, kept, updated, trashed, dropped: plan.leftOut.length, changes: created > 0 || trashed.length > 0 || updated.length > 0 };
}

/** The request body of POST /alterations/compose. */
export function toRequest(plan: Plan, sources: PageSource[]) {
  return {
    sources: sources.map((s) => s.id),
    expected_views: Object.fromEntries(sources.filter((s) => s.visible !== undefined).map((s) => [s.id, s.visible!])),
    outputs: plan.outputs.map((o) => ({
      title: o.title.trim(),
      pages: o.pages.map((k) => {
        const [document, page] = parseKey(k);
        return { document, page };
      }),
    })),
  };
}
