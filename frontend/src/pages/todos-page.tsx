import { CheckSquare } from "lucide-react";
import { useState } from "react";
import { useDocuments } from "@/api/queries";
import { Card } from "@/components/ui/card";
import { EmptyState, PageHeader, Spinner } from "@/components/ui/misc";
import { BulkBar, DocumentRow } from "@/features/documents/document-row";

export function TodosPage() {
  const todos = useDocuments({ status: ["todo"], sort: "-date", page_size: 100 });
  const [selected, setSelected] = useState<string[]>([]);
  const items = [...(todos.data?.items ?? [])].sort((a, b) => Number(b.is_important) - Number(a.is_important));
  return (
    <>
      <PageHeader title="Todos" description="Documents that still need an action — important ones first." />
      <BulkBar ids={selected} onClear={() => setSelected([])} />
      {todos.isPending ? (
        <Spinner />
      ) : items.length === 0 ? (
        <EmptyState icon={<CheckSquare />} title="Nothing to do">
          Mark a document as “Todo” when you still need to reply, pay or file something.
        </EmptyState>
      ) : (
        <Card className="overflow-hidden">
          {items.map((d) => (
            <DocumentRow
              key={d.id}
              doc={d}
              selected={selected.includes(d.id)}
              onSelect={(v) => setSelected((s) => (v ? [...s, d.id] : s.filter((x) => x !== d.id)))}
            />
          ))}
        </Card>
      )}
    </>
  );
}
