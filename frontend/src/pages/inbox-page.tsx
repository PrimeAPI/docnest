import { AlertCircle, CheckCheck, CheckSquare, CircleDot, Inbox, Layers, ListChecks, Loader2, Star } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router";
import { useBulkAction, useDocuments, useOverview } from "@/api/queries";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, PageHeader, Spinner } from "@/components/ui/misc";
import { BulkBar } from "@/features/documents/document-row";
import { DocumentCollection, useViewMode, ViewSwitch } from "@/features/documents/document-views";
import { cn } from "@/lib/utils";

function StatCard({
  label,
  value,
  icon: Icon,
  to,
  tone,
}: {
  label: string;
  value?: number;
  icon: typeof Inbox;
  to: string;
  tone?: string;
}) {
  return (
    <Link to={to}>
      <Card className="flex items-center gap-3 p-4 transition-colors hover:bg-muted/50">
        <div className={cn("rounded-md bg-accent p-2 text-accent-foreground", tone)}>
          <Icon className="size-5" />
        </div>
        <div>
          <div className="text-2xl font-semibold leading-none">{value ?? "–"}</div>
          <div className="mt-1 text-xs text-muted-foreground">{label}</div>
        </div>
      </Card>
    </Link>
  );
}

export function InboxPage() {
  const overview = useOverview();
  const o = overview.data;
  const inbox = useDocuments({ status: ["new"], sort: "-uploaded", page_size: 50 }, { refetchInterval: 10_000 });
  const failed = useDocuments({ processing: ["failed"], page_size: 20 });
  const suggestions = useDocuments({ sort: "-uploaded", page_size: 50 });
  const bulk = useBulkAction();
  const [selected, setSelected] = useState<string[]>([]);
  const [view, setView] = useViewMode("page");
  const navigate = useNavigate();

  const items = inbox.data?.items ?? [];
  const withSuggestion = (suggestions.data?.items ?? []).filter((d) => d.has_series_suggestion);

  return (
    <>
      <PageHeader
        title="Inbox"
        description="New documents that have not been handled yet."
        actions={
          <>
            <ViewSwitch mode={view} onChange={setView} />
            {items.length > 0 && (
              <>
              <Button
                variant="outline"
                onClick={() => bulk.mutate({ ids: items.map((d) => d.id), action: "mark_read" })}
              >
                <CheckCheck /> Mark all read
              </Button>
              <Button onClick={() => navigate("/inbox/review")}>
                <ListChecks /> Review inbox ({items.length})
              </Button>
              </>
            )}
          </>
        }
      />
      <div className="mb-6 grid grid-cols-2 gap-3 lg:grid-cols-5">
        <StatCard label="New" value={o?.new} icon={Inbox} to="/inbox" />
        <StatCard label="Unread" value={o?.unread} icon={CircleDot} to="/documents?unread=true" />
        <StatCard label="Todo" value={o?.todo} icon={CheckSquare} to="/todos" tone="bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-200" />
        <StatCard label="Important (open)" value={o?.important} icon={Star} to="/documents?important=true&status=new&status=todo" tone="bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-200" />
        <StatCard label="Processing" value={o?.processing} icon={Loader2} to="/documents?processing=pending&processing=running" tone="bg-muted text-muted-foreground" />
      </div>

      {(failed.data?.total ?? 0) > 0 && (
        <section className="mb-6">
          <h2 className="mb-2 flex items-center gap-2 text-sm font-medium text-destructive">
            <AlertCircle className="size-4" /> Processing failed for {failed.data?.total} document(s)
          </h2>
          <DocumentCollection docs={failed.data?.items ?? []} mode={view} className="border-destructive/40" />
        </section>
      )}

      {withSuggestion.length > 0 && (
        <section className="mb-6">
          <h2 className="mb-2 flex items-center gap-2 text-sm font-medium">
            <Layers className="size-4" /> Possible series — open to confirm
          </h2>
          <DocumentCollection docs={withSuggestion} mode={view} />
        </section>
      )}

      <BulkBar ids={selected} onClear={() => setSelected([])} />
      {inbox.isPending ? (
        <Spinner />
      ) : items.length === 0 ? (
        <EmptyState icon={<Inbox />} title="Inbox zero">
          New scans appear here automatically. Mark documents as done or todo to move them out of the inbox.
        </EmptyState>
      ) : (
        <DocumentCollection docs={items} mode={view} selected={selected} onSelect={(id, v) => setSelected((s) => (v ? [...s, id] : s.filter((x) => x !== id)))} />
      )}
    </>
  );
}
