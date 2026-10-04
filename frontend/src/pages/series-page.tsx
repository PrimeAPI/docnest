import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeft, Check, Layers, Pencil, Trash2 } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { toast } from "sonner";
import { call, client } from "@/api/client";
import { keys, useSeriesList } from "@/api/queries";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, Select } from "@/components/ui/input";
import { EmptyState, ErrorNote, PageHeader, Spinner } from "@/components/ui/misc";
import { formatDate } from "@/lib/utils";

const CADENCE: Record<string, string> = {
  monthly: "Monthly",
  quarterly: "Quarterly",
  yearly: "Yearly",
  irregular: "Irregular",
};

export function SeriesPage() {
  const series = useSeriesList();
  return (
    <>
      <PageHeader
        title="Series"
        description="Recurring documents like payslips, bills or statements are grouped automatically."
      />
      {series.isPending ? (
        <Spinner />
      ) : !series.data?.length ? (
        <EmptyState icon={<Layers />} title="No series yet">
          When two or more similar documents arrive from the same sender (e.g. monthly payslips), DocNest groups them.
        </EmptyState>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {series.data.map((s) => (
            <Link key={s.id} to={`/series/${s.id}`}>
              <Card className="h-full p-4 transition-colors hover:bg-muted/50">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="truncate font-semibold">{s.name}</div>
                    <div className="mt-0.5 truncate text-sm text-muted-foreground">
                      {[s.correspondent, s.document_type].filter(Boolean).join(" · ") || "—"}
                    </div>
                  </div>
                  {s.is_suggested && <Badge variant="warning">New</Badge>}
                </div>
                <div className="mt-3 flex items-center gap-3 text-xs text-muted-foreground">
                  <span>{s.document_count} documents</span>
                  <span>{CADENCE[s.cadence]}</span>
                  {s.latest_date && <span>latest {formatDate(s.latest_date)}</span>}
                </div>
              </Card>
            </Link>
          ))}
        </div>
      )}
    </>
  );
}

export function SeriesDetailPage() {
  const id = Number(useParams().id);
  const qc = useQueryClient();
  const navigate = useNavigate();
  const detail = useQuery({
    queryKey: keys.seriesDetail(id),
    queryFn: () => call(() => client.GET("/api/v1/series/{series_id}", { params: { path: { series_id: id } } })),
  });
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState("");

  const patch = useMutation({
    mutationFn: (body: { name?: string; cadence?: string; confirm?: boolean }) =>
      call(() =>
        client.PATCH("/api/v1/series/{series_id}", {
          params: { path: { series_id: id } },
          body: { confirm: false, ...body },
        }),
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["series"] });
      qc.invalidateQueries({ queryKey: ["documents"] });
      setEditing(false);
    },
    onError: (e) => toast.error(e.message),
  });

  const remove = async () => {
    if (!confirm("Remove this series? The documents are kept.")) return;
    await call(() => client.DELETE("/api/v1/series/{series_id}", { params: { path: { series_id: id } } }));
    qc.invalidateQueries({ queryKey: ["series"] });
    navigate("/series");
  };

  if (detail.isPending) return <Spinner />;
  if (detail.error || !detail.data) return <ErrorNote error={detail.error} />;
  const s = detail.data;

  return (
    <>
      <Link to="/series" className="mb-3 inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" /> All series
      </Link>
      <PageHeader
        title={
          editing ? (
            <form
              className="flex gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                patch.mutate({ name });
              }}
            >
              <Input value={name} onChange={(e) => setName(e.target.value)} autoFocus className="h-9 text-lg" />
              <Button type="submit" size="sm">
                Save
              </Button>
            </form>
          ) : (
            s.name
          )
        }
        description={[s.correspondent, s.document_type, `${s.document_count} documents`].filter(Boolean).join(" · ")}
        actions={
          <>
            {s.is_suggested && (
              <Button onClick={() => patch.mutate({ confirm: true })}>
                <Check /> Confirm series
              </Button>
            )}
            <Select value={s.cadence} onChange={(e) => patch.mutate({ cadence: e.target.value })} className="w-36">
              {Object.entries(CADENCE).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </Select>
            <Button
              variant="outline"
              size="icon"
              title="Rename"
              onClick={() => {
                setName(s.name);
                setEditing(true);
              }}
            >
              <Pencil />
            </Button>
            <Button variant="outline" size="icon" title="Remove series" onClick={remove}>
              <Trash2 />
            </Button>
          </>
        }
      />
      {s.is_suggested && (
        <p className="mb-4 text-sm text-muted-foreground">
          This series was detected automatically. Confirm it, rename it, or remove documents that do not belong.
        </p>
      )}
      {s.missing_periods.length > 0 && (
        <div className="mb-4 flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/50 dark:text-amber-100">
          <AlertTriangle className="mt-0.5 size-4 shrink-0" />
          <span>Missing: {s.missing_periods.join(", ")}</span>
        </div>
      )}
      <Card className="overflow-hidden">
        <ol className="relative">
          {s.members.map((m) => (
            <li key={m.id} className="flex items-center gap-4 border-b px-4 py-3 last:border-b-0 hover:bg-muted/50">
              <span className="w-32 shrink-0 text-sm font-medium">{m.period_label || formatDate(m.document_date)}</span>
              <Link to={`/documents/${m.id}`} className="min-w-0 flex-1 truncate hover:underline">
                {m.title}
              </Link>
              <span className="text-xs text-muted-foreground">{formatDate(m.document_date)}</span>
              {m.status === "todo" && <Badge variant="warning">Todo</Badge>}
            </li>
          ))}
        </ol>
      </Card>
    </>
  );
}
