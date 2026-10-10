import { CheckSquare } from "lucide-react";
import { useState } from "react";
import { useDocuments } from "@/api/queries";
import { EmptyState, PageHeader, Spinner } from "@/components/ui/misc";
import { BulkBar } from "@/features/documents/document-row";
import { DocumentCollection, useViewMode, ViewSwitch } from "@/features/documents/document-views";

export function TodosPage() {
  const todos = useDocuments({ status: ["todo"], sort: "-date", page_size: 100 });
  const [selected, setSelected] = useState<string[]>([]);
  const [view, setView] = useViewMode("page");
  const items = [...(todos.data?.items ?? [])].sort((a, b) => Number(b.is_important) - Number(a.is_important));
  return (
    <>
      <PageHeader
        title="Todos"
        description="Documents that still need an action — important ones first."
        actions={<ViewSwitch mode={view} onChange={setView} />}
      />
      <BulkBar ids={selected} onClear={() => setSelected([])} />
      {todos.isPending ? (
        <Spinner />
      ) : items.length === 0 ? (
        <EmptyState icon={<CheckSquare />} title="Nothing to do">
          Mark a document as “Todo” when you still need to reply, pay or file something.
        </EmptyState>
      ) : (
        <DocumentCollection docs={items} mode={view} selected={selected} onSelect={(id, v) => setSelected((s) => (v ? [...s, id] : s.filter((x) => x !== id)))} />
      )}
    </>
  );
}
