import { ChevronLeft, ChevronRight, FileSearch, SlidersHorizontal, X } from "lucide-react";
import { type ReactNode, useMemo, useState } from "react";
import { useSearchParams } from "react-router";
import {
  type DocumentQuery,
  useBuckets,
  useCorrespondents,
  useDocuments,
  useSeriesList,
  useTags,
  useTypes,
} from "@/api/queries";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input, Label, Select } from "@/components/ui/input";
import { EmptyState, ErrorNote, PageHeader, Spinner } from "@/components/ui/misc";
import { BulkBar, DocumentRow } from "@/features/documents/document-row";
import { cn, dotClass } from "@/lib/utils";

const PAGE_SIZE = 25;

function numbers(params: URLSearchParams, key: string): number[] {
  return params
    .getAll(key)
    .map(Number)
    .filter((n) => Number.isFinite(n));
}

function parseQuery(params: URLSearchParams): DocumentQuery {
  const q: DocumentQuery = { page: Number(params.get("page") ?? 1) || 1, page_size: PAGE_SIZE };
  const text = params.get("q");
  if (text) q.q = text;
  for (const key of ["bucket", "document_type", "correspondent", "tag"] as const) {
    const v = numbers(params, key);
    if (v.length) q[key] = v;
  }
  const series = params.get("series");
  if (series) q.series = Number(series);
  const status = params.getAll("status") as DocumentQuery["status"];
  if (status?.length) q.status = status;
  const processing = params.getAll("processing") as DocumentQuery["processing"];
  if (processing?.length) q.processing = processing;
  for (const key of ["important", "unread"] as const) {
    const v = params.get(key);
    if (v === "true" || v === "false") q[key] = v === "true";
  }
  for (const key of ["uploaded_from", "uploaded_to", "date_from", "date_to"] as const) {
    const v = params.get(key);
    if (v) q[key] = v;
  }
  const sort = params.get("sort") as DocumentQuery["sort"];
  q.sort = sort ?? (text ? "relevance" : "-uploaded");
  return q;
}

export function DocumentsPage() {
  const [params, setParams] = useSearchParams();
  const query = useMemo(() => parseQuery(params), [params]);
  const docs = useDocuments(query);
  const [showFilters, setShowFilters] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);

  const update = (fn: (p: URLSearchParams) => void) => {
    const next = new URLSearchParams(params);
    fn(next);
    if (!next.has("page_keep")) next.delete("page");
    next.delete("page_keep");
    setParams(next);
    setSelected([]);
  };

  const activeFilters = [...params.keys()].filter((k) => !["q", "page", "sort"].includes(k)).length;
  const total = docs.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const page = query.page ?? 1;

  return (
    <>
      <PageHeader
        title={query.q ? `Results for “${query.q}”` : "Documents"}
        description={docs.data ? `${total} document${total === 1 ? "" : "s"}` : " "}
        actions={
          <>
            <Select
              value={query.sort}
              onChange={(e) => update((p) => p.set("sort", e.target.value))}
              className="w-44"
              aria-label="Sort"
            >
              {query.q && <option value="relevance">Best match</option>}
              <option value="-uploaded">Newest scans</option>
              <option value="uploaded">Oldest scans</option>
              <option value="-date">Document date ↓</option>
              <option value="date">Document date ↑</option>
            </Select>
            <Button variant="outline" className="lg:hidden" onClick={() => setShowFilters((v) => !v)}>
              <SlidersHorizontal /> Filters {activeFilters > 0 && `(${activeFilters})`}
            </Button>
          </>
        }
      />
      <div className="flex gap-6">
        <aside className={cn("w-64 shrink-0", showFilters ? "block" : "hidden lg:block")}>
          <Filters params={params} update={update} />
        </aside>
        <div className="min-w-0 flex-1">
          <BulkBar ids={selected} onClear={() => setSelected([])} />
          <ErrorNote error={docs.error} />
          {docs.isPending ? (
            <Spinner />
          ) : !docs.data?.items.length ? (
            <EmptyState icon={<FileSearch />} title="No documents found">
              {query.q ? "Try fewer or shorter words — prefixes like “versich” also work." : "Adjust the filters."}
            </EmptyState>
          ) : (
            <>
              <Card className={cn("overflow-hidden", docs.isFetching && "opacity-70")}>
                {docs.data.items.map((d) => (
                  <DocumentRow
                    key={d.id}
                    doc={d}
                    selected={selected.includes(d.id)}
                    onSelect={(v) => setSelected((s) => (v ? [...s, d.id] : s.filter((x) => x !== d.id)))}
                  />
                ))}
              </Card>
              {pages > 1 && (
                <div className="mt-4 flex items-center justify-between text-sm">
                  <span className="text-muted-foreground">
                    Page {page} of {pages}
                  </span>
                  <div className="flex gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={page <= 1}
                      onClick={() => update((p) => (p.set("page", String(page - 1)), p.set("page_keep", "1")))}
                    >
                      <ChevronLeft /> Previous
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={page >= pages}
                      onClick={() => update((p) => (p.set("page", String(page + 1)), p.set("page_keep", "1")))}
                    >
                      Next <ChevronRight />
                    </Button>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="border-b py-3 first:pt-0 last:border-b-0">
      <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</h4>
      <div className="flex flex-col gap-1.5">{children}</div>
    </div>
  );
}

function CheckOption({
  label,
  checked,
  onChange,
  dot,
  count,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  dot?: string;
  count?: number;
}) {
  return (
    <label className="flex cursor-pointer items-center gap-2 text-sm">
      <Checkbox checked={checked} onCheckedChange={(v) => onChange(v === true)} />
      {dot && <span className={cn("size-2 rounded-full", dotClass(dot))} />}
      <span className="flex-1 truncate">{label}</span>
      {count !== undefined && <span className="text-xs text-muted-foreground">{count}</span>}
    </label>
  );
}

function Filters({ params, update }: { params: URLSearchParams; update: (fn: (p: URLSearchParams) => void) => void }) {
  const buckets = useBuckets();
  const types = useTypes();
  const tags = useTags();
  const correspondents = useCorrespondents();
  const series = useSeriesList();
  const [tagFilter, setTagFilter] = useState("");

  const toggle = (key: string, value: string) => (checked: boolean) =>
    update((p) => {
      const values = p.getAll(key).filter((v) => v !== value);
      p.delete(key);
      for (const v of checked ? [...values, value] : values) p.append(key, v);
    });
  const has = (key: string, value: string) => params.getAll(key).includes(value);
  const setOne = (key: string, value: string) =>
    update((p) => {
      if (value) p.set(key, value);
      else p.delete(key);
    });

  const sortedTags = (tags.data ?? [])
    .filter((t) => t.document_count > 0 || has("tag", String(t.id)))
    .filter((t) => !tagFilter || t.name.toLowerCase().includes(tagFilter.toLowerCase()))
    .sort((a, b) => b.document_count - a.document_count);

  const anyActive = [...params.keys()].some((k) => !["q", "sort"].includes(k));

  return (
    <Card className="p-4">
      {anyActive && (
        <Button
          variant="ghost"
          size="sm"
          className="-mt-1 mb-2 w-full justify-start text-muted-foreground"
          onClick={() =>
            update((p) => {
              for (const k of [...p.keys()]) if (!["q", "sort"].includes(k)) p.delete(k);
            })
          }
        >
          <X /> Clear filters
        </Button>
      )}
      <Section title="Status">
        <CheckOption label="New" checked={has("status", "new")} onChange={toggle("status", "new")} />
        <CheckOption label="Todo" checked={has("status", "todo")} onChange={toggle("status", "todo")} />
        <CheckOption label="Done" checked={has("status", "done")} onChange={toggle("status", "done")} />
        <CheckOption
          label="Important"
          checked={params.get("important") === "true"}
          onChange={(v) => setOne("important", v ? "true" : "")}
        />
        <CheckOption
          label="Unread"
          checked={params.get("unread") === "true"}
          onChange={(v) => setOne("unread", v ? "true" : "")}
        />
        <CheckOption
          label="Processing failed"
          checked={has("processing", "failed")}
          onChange={toggle("processing", "failed")}
        />
      </Section>
      <Section title="Bucket">
        {buckets.data?.map((b) => (
          <CheckOption
            key={b.id}
            label={b.name}
            dot={b.color}
            count={b.document_count}
            checked={has("bucket", String(b.id))}
            onChange={toggle("bucket", String(b.id))}
          />
        ))}
      </Section>
      <Section title="Type">
        {types.data
          ?.filter((t) => t.document_count > 0 || has("document_type", String(t.id)))
          .map((t) => (
            <CheckOption
              key={t.id}
              label={t.name}
              count={t.document_count}
              checked={has("document_type", String(t.id))}
              onChange={toggle("document_type", String(t.id))}
            />
          ))}
      </Section>
      <Section title="Tags">
        {(tags.data?.length ?? 0) > 8 && (
          <Input
            value={tagFilter}
            onChange={(e) => setTagFilter(e.target.value)}
            placeholder="Filter tags…"
            className="mb-1 h-8"
          />
        )}
        <div className="flex max-h-56 flex-col gap-1.5 overflow-y-auto">
          {sortedTags.map((t) => (
            <CheckOption
              key={t.id}
              label={t.name}
              dot={t.color}
              count={t.document_count}
              checked={has("tag", String(t.id))}
              onChange={toggle("tag", String(t.id))}
            />
          ))}
          {!sortedTags.length && <span className="text-xs text-muted-foreground">No tags yet</span>}
        </div>
      </Section>
      <Section title="Sender">
        <Select value={params.get("correspondent") ?? ""} onChange={(e) => setOne("correspondent", e.target.value)}>
          <option value="">Any sender</option>
          {correspondents.data
            ?.filter((c) => c.document_count > 0)
            .map((c) => (
              <option key={c.id} value={c.id}>
                {c.name} ({c.document_count})
              </option>
            ))}
        </Select>
      </Section>
      {(series.data?.length ?? 0) > 0 && (
        <Section title="Series">
          <Select value={params.get("series") ?? ""} onChange={(e) => setOne("series", e.target.value)}>
            <option value="">Any series</option>
            {series.data?.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </Select>
        </Section>
      )}
      <Section title="Document date">
        <DateRange from={params.get("date_from")} to={params.get("date_to")} onChange={(f, t) => update((p) => setRange(p, "date", f, t))} />
      </Section>
      <Section title="Scanned">
        <DateRange
          from={params.get("uploaded_from")}
          to={params.get("uploaded_to")}
          onChange={(f, t) => update((p) => setRange(p, "uploaded", f, t))}
        />
      </Section>
    </Card>
  );
}

function setRange(p: URLSearchParams, prefix: string, from: string, to: string) {
  if (from) p.set(`${prefix}_from`, from);
  else p.delete(`${prefix}_from`);
  if (to) p.set(`${prefix}_to`, to);
  else p.delete(`${prefix}_to`);
}

function DateRange({
  from,
  to,
  onChange,
}: {
  from: string | null;
  to: string | null;
  onChange: (from: string, to: string) => void;
}) {
  const year = new Date().getFullYear();
  return (
    <>
      <div className="grid grid-cols-2 gap-2">
        <div>
          <Label className="text-xs text-muted-foreground">From</Label>
          <Input type="date" className="h-8 px-2 text-xs" value={from ?? ""} onChange={(e) => onChange(e.target.value, to ?? "")} />
        </div>
        <div>
          <Label className="text-xs text-muted-foreground">To</Label>
          <Input type="date" className="h-8 px-2 text-xs" value={to ?? ""} onChange={(e) => onChange(from ?? "", e.target.value)} />
        </div>
      </div>
      <div className="flex flex-wrap gap-1">
        {[year, year - 1, year - 2].map((y) => (
          <button
            key={y}
            type="button"
            className="rounded border px-2 py-0.5 text-xs hover:bg-muted cursor-pointer"
            onClick={() => onChange(`${y}-01-01`, `${y}-12-31`)}
          >
            {y}
          </button>
        ))}
      </div>
    </>
  );
}
