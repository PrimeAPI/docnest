import { useQuery } from "@tanstack/react-query";
import { call, client, type Schemas } from "@/api/client";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorNote, Spinner } from "@/components/ui/misc";
import { formatBytes, formatDuration } from "@/lib/utils";

type Stats = Schemas["Stats"];
type Counted = Schemas["Counted"];

const STAGES: Record<string, string> = {
  assemble: "Building PDF",
  validate: "Validating",
  enhance: "Improving scan",
  ocr: "Text recognition",
  analyze: "Analysis",
  store: "Storing",
  index: "Indexing",
};

const number = new Intl.NumberFormat();

/** Settings → Statistics: what the archive holds, how it grows and how fast documents are processed. */
export function StatsPanel() {
  const stats = useQuery({
    queryKey: ["stats"],
    queryFn: () => call(() => client.GET("/api/v1/stats")),
    refetchInterval: 60_000,
  });
  if (stats.error) return <ErrorNote error={stats.error} />;
  if (!stats.data) return <Spinner />;
  const s = stats.data;
  return (
    <div className="flex flex-col gap-6">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <Tile label="Documents" value={number.format(s.documents)} />
        <Tile label="Pages" value={number.format(s.pages)} />
        <Tile label="Stored" value={formatBytes(s.bytes)} />
        <Tile label="New per week" value={s.per_week.toLocaleString()} hint="average of the last 12 weeks" />
        <Tile label="Senders" value={number.format(s.correspondents)} />
        <Tile
          label="Open"
          value={number.format(s.todo)}
          hint={`${s.unread} unread${s.failed ? ` · ${s.failed} failed` : ""}${s.processing_now ? ` · ${s.processing_now} in progress` : ""}`}
        />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>New documents per month</CardTitle>
          <CardDescription>
            Uploads of the last 12 months
            {s.oldest_document_date && ` · the oldest document dates from ${new Date(s.oldest_document_date).toLocaleDateString()}`}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <MonthChart months={s.months} />
        </CardContent>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <BarListCard title="Document types" items={s.types} total={s.documents} />
        <BarListCard title="Top senders" items={s.correspondents_top} total={s.documents} />
      </div>

      <ProcessingCard p={s.processing} />
      <BarListCard title="Received from" items={s.sources} total={s.documents} />
    </div>
  );
}

function Tile({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-lg border bg-card p-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums">{value}</p>
      {hint && <p className="mt-0.5 truncate text-xs text-muted-foreground" title={hint}>{hint}</p>}
    </div>
  );
}

function monthLabel(key: string, style: "short" | "long" = "short"): string {
  const [year, month] = key.split("-").map(Number);
  return new Date(year, month - 1, 1).toLocaleDateString(undefined, { month: style, year: style === "long" ? "numeric" : undefined });
}

/** One series of vertical bars; hovering a month shows its numbers. */
function MonthChart({ months }: { months: Stats["months"] }) {
  const peak = Math.max(1, ...months.map((m) => m.documents));
  return (
    <figure>
      <div className="flex h-44 items-end gap-0.5 border-b border-border" role="img" aria-label="Documents uploaded per month">
        {months.map((m) => (
          <div key={m.month} className="group relative flex h-full flex-1 items-end justify-center">
            {/* The hit target is the whole column, larger than the bar. */}
            <div
              className="w-full max-w-10 rounded-t bg-primary/80 transition-colors group-hover:bg-primary"
              style={{ height: m.documents ? `${Math.max(2, (m.documents / peak) * 100)}%` : 0 }}
            />
            <div className="pointer-events-none absolute -top-1 left-1/2 z-10 hidden -translate-x-1/2 -translate-y-full whitespace-nowrap rounded-md border bg-popover px-2 py-1 text-xs shadow-md group-hover:block">
              <p className="font-medium">{monthLabel(m.month, "long")}</p>
              <p className="text-muted-foreground">
                {m.documents} document{m.documents === 1 ? "" : "s"} · {m.pages} page{m.pages === 1 ? "" : "s"}
              </p>
            </div>
          </div>
        ))}
      </div>
      <div className="mt-1 flex gap-0.5">
        {months.map((m, index) => (
          <span key={m.month} className="flex-1 text-center text-[10px] text-muted-foreground">
            {index % 2 === months.length % 2 ? "" : monthLabel(m.month)}
          </span>
        ))}
      </div>
      <table className="sr-only">
        <caption>Documents uploaded per month</caption>
        <tbody>
          {months.map((m) => (
            <tr key={m.month}>
              <th scope="row">{monthLabel(m.month, "long")}</th>
              <td>{m.documents} documents</td>
              <td>{m.pages} pages</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}

/** Horizontal bars, each labelled with its name and count (no legend needed). */
function BarList({ items, total, format = (n) => number.format(n) }: { items: Counted[]; total: number; format?: (n: number) => string }) {
  if (!items.length) return <p className="text-sm text-muted-foreground">Nothing yet.</p>;
  const peak = Math.max(1, ...items.map((i) => i.count));
  return (
    <ul className="flex flex-col gap-2">
      {items.map((item) => (
        <li key={item.name} title={`${item.name}: ${format(item.count)}${total ? ` (${Math.round((item.count / total) * 100)} %)` : ""}`}>
          <div className="flex items-baseline justify-between gap-3 text-sm">
            <span className="truncate">{item.name}</span>
            <span className="shrink-0 tabular-nums text-muted-foreground">{format(item.count)}</span>
          </div>
          <div className="mt-1 h-2 overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-primary/80" style={{ width: `${(item.count / peak) * 100}%` }} />
          </div>
        </li>
      ))}
    </ul>
  );
}

function BarListCard({ title, items, total }: { title: string; items: Counted[]; total: number }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <BarList items={items} total={total} />
      </CardContent>
    </Card>
  );
}

function ProcessingCard({ p }: { p: Stats["processing"] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Processing</CardTitle>
        <CardDescription>
          Documents uploaded in the last 90 days ({p.sample}). Times from the upload, including waiting in the queue.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <Tile label="Readable after" value={formatDuration(p.readable_median_seconds)} hint="median" />
          <Tile label="Fully processed after" value={formatDuration(p.done_median_seconds)} hint="median" />
          <Tile
            label="AI analysis"
            value={formatDuration(p.ai_average_seconds)}
            hint={p.ai_documents ? `average of ${p.ai_documents} documents` : "not used yet"}
          />
        </div>
        <div>
          <p className="mb-2 text-sm font-medium">Average time per step</p>
          <BarList
            items={(p.stages ?? []).map((stage) => ({ name: STAGES[stage.stage] ?? stage.stage, count: stage.average_seconds }))}
            total={0}
            format={formatDuration}
          />
        </div>
      </CardContent>
    </Card>
  );
}
