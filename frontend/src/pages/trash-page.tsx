import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RotateCcw, Trash2 } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { toast } from "sonner";
import { call, client } from "@/api/client";
import { useInvalidateDocuments } from "@/api/queries";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { EmptyState, ErrorNote, PageHeader, Spinner } from "@/components/ui/misc";
import { formatDateTime } from "@/lib/utils";

/**
 * Documents that were put away — by hand, or because their pages went into new documents.
 * Their files are untouched; restoring brings them back as they were.
 */
export function TrashPage() {
  const qc = useQueryClient();
  const invalidate = useInvalidateDocuments();
  const trash = useQuery({
    queryKey: ["trash"],
    queryFn: () => call(() => client.GET("/api/v1/alterations/trash")),
  });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const refresh = () => {
    setSelected(new Set());
    void qc.invalidateQueries({ queryKey: ["trash"] });
    invalidate();
  };
  const restore = useMutation({
    mutationFn: (ids: string[]) => call(() => client.POST("/api/v1/alterations/restore", { body: { ids } })),
    onSuccess: (a) => {
      toast.success(a.summary);
      refresh();
    },
    onError: (e) => toast.error(e.message),
  });

  const items = trash.data ?? [];
  return (
    <div>
      <PageHeader
        title="Trash"
        description="Documents you put away, and those whose pages went into new documents. Original files are always kept; you can restore them at any time."
        actions={
          selected.size > 0 && (
            <Button onClick={() => restore.mutate([...selected])} disabled={restore.isPending}>
              <RotateCcw /> Restore {selected.size}
            </Button>
          )
        }
      />
      {trash.isPending ? (
        <Spinner />
      ) : trash.error ? (
        <ErrorNote error={trash.error} />
      ) : !items.length ? (
        <EmptyState icon={<Trash2 />} title="The trash is empty" />
      ) : (
        <ul className="divide-y rounded-lg border bg-card">
          {items.map((d) => (
            <li key={d.id} className="flex flex-wrap items-center gap-3 px-4 py-3 text-sm">
              <Checkbox
                checked={selected.has(d.id)}
                onCheckedChange={(v) => {
                  const next = new Set(selected);
                  if (v === true) next.add(d.id);
                  else next.delete(d.id);
                  setSelected(next);
                }}
                aria-label={`Select ${d.title}`}
              />
              <img
                src={`/api/v1/alterations/pages/${d.id}/1`}
                alt=""
                className="h-14 w-10 rounded border bg-white object-contain"
                onError={(e) => (e.currentTarget.style.visibility = "hidden")}
              />
              <div className="min-w-0 flex-1">
                <Link to={`/documents/${d.id}`} className="font-medium hover:underline">
                  {d.title}
                </Link>
                <div className="text-xs text-muted-foreground">
                  {d.page_count} page{d.page_count === 1 ? "" : "s"} · in the trash since {formatDateTime(d.deleted_at)}
                  {d.replaced_by.length > 0 && (
                    <>
                      {" · pages now in "}
                      {d.replaced_by.map((r, i) => (
                        <span key={r.id}>
                          <Link to={`/documents/${r.id}`} className="hover:underline">
                            {r.title}
                          </Link>
                          {i < d.replaced_by.length - 1 && ", "}
                        </span>
                      ))}
                    </>
                  )}
                </div>
              </div>
              <Button size="sm" variant="outline" onClick={() => restore.mutate([d.id])} disabled={restore.isPending}>
                <RotateCcw /> Restore
              </Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
