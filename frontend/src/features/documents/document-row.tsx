import {
  AlertCircle,
  Check,
  CircleDot,
  FileText,
  Folder,
  Layers,
  Loader2,
  MoreHorizontal,
  RefreshCw,
  Star,
} from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router";
import { type DocumentListItem, useBulkAction } from "@/api/queries";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown";
import { AssistantButton } from "@/features/assist/assistant";
import { SuggestFilingButton } from "@/features/folders/filing-suggestions";
import { MoveButton, MoveToFolderDialog } from "@/features/folders/folder-ui";
import { dragDocuments } from "@/features/folders/tree";
import { PageEditor } from "@/features/pages/page-editor";
import { ReprocessDialog } from "@/features/processing/reprocess-dialog";
import { cn, colorClass, formatDate, formatDateTime } from "@/lib/utils";

/**
 * Select mode: once any document is selected, clicking a document toggles it instead of opening it.
 * Buttons keep their own action, and Ctrl/⌘-click still opens the document in a new tab.
 */
export function selectOnClick(selecting: boolean, selected: boolean, onSelect?: (checked: boolean) => void) {
  if (!selecting || !onSelect) return undefined;
  return (e: React.MouseEvent) => {
    if (e.ctrlKey || e.metaKey || e.shiftKey || (e.target as HTMLElement).closest("button")) return;
    e.preventDefault();
    e.stopPropagation();
    onSelect(!selected);
  };
}

export function Thumbnail({ doc, className }: { doc: DocumentListItem; className?: string }) {
  const [failed, setFailed] = useState(false);
  const ready = doc.processing_state === "done" || doc.page_count > 0;
  // The thumbnail appears (and improves) as processing advances: try again at every step.
  const version = `${doc.page_count}-${doc.processing_state}`;
  useEffect(() => setFailed(false), [version]);
  return (
    <div
      className={cn(
        "flex shrink-0 items-center justify-center overflow-hidden rounded border bg-muted text-muted-foreground",
        className,
      )}
    >
      {ready && !failed ? (
        <img
          src={`/api/v1/documents/${doc.id}/thumbnail?v=${version}`}
          alt=""
          loading="lazy"
          draggable={false}
          className="h-full w-full object-cover object-top"
          onError={() => setFailed(true)}
        />
      ) : (
        <FileText className="size-5" />
      )}
    </div>
  );
}

export function StatusBadge({
  doc,
}: {
  doc: Pick<DocumentListItem, "status" | "processing_state" | "page_count">;
}) {
  if (doc.processing_state === "failed")
    return (
      <Badge variant="danger">
        <AlertCircle /> Failed
      </Badge>
    );
  if (doc.processing_state === "pending" || doc.processing_state === "running")
    return (
      <Badge variant="muted" title={doc.page_count > 0 ? "Readable already; details follow" : undefined}>
        <Loader2 className="animate-spin" /> {doc.page_count > 0 ? "Analyzing" : "Processing"}
      </Badge>
    );
  if (doc.status === "todo") return <Badge variant="warning">Todo</Badge>;
  if (doc.status === "done") return <Badge variant="success">Done</Badge>;
  return <Badge>New</Badge>;
}

export function TagChip({ name, color }: { name: string; color?: string }) {
  return <span className={cn("rounded-full px-2 py-0.5 text-xs font-medium", colorClass(color))}>{name}</span>;
}

export function Highlighted({ text, highlights }: { text: string; highlights: number[][] }) {
  const parts: React.ReactNode[] = [];
  let pos = 0;
  highlights.forEach(([s, e], i) => {
    if (s > pos) parts.push(text.slice(pos, s));
    parts.push(<mark key={i}>{text.slice(s, e)}</mark>);
    pos = e;
  });
  parts.push(text.slice(pos));
  return <>{parts}</>;
}

export function DocumentRow({
  doc,
  selected,
  onSelect,
  dragIds,
  showFolder = true,
  selecting = false,
}: {
  doc: DocumentListItem;
  selected?: boolean;
  onSelect?: (checked: boolean) => void;
  /** Select mode: a click on the row selects it instead of opening it. */
  selecting?: boolean;
  /** Documents dragged along when this row is dragged (defaults to just this one). */
  dragIds?: string[];
  showFolder?: boolean;
}) {
  const bulk = useBulkAction();
  const act = (action: Parameters<typeof bulk.mutate>[0]["action"]) => bulk.mutate({ ids: [doc.id], action });
  const [moving, setMoving] = useState(false);

  return (
    <div
      draggable
      onDragStart={(e) => dragDocuments(e, dragIds?.length ? dragIds : [doc.id])}
      onClickCapture={selectOnClick(selecting, selected ?? false, onSelect)}
      className={cn(
        "group flex items-start gap-3 border-b px-3 py-3 transition-colors last:border-b-0 hover:bg-muted/50",
        selecting && "cursor-pointer select-none",
        selected && "bg-accent/50",
      )}
    >
      {onSelect && (
        <div className="pt-1">
          <Checkbox checked={selected} onCheckedChange={(v) => onSelect(v === true)} aria-label="Select" />
        </div>
      )}
      <Link to={`/documents/${doc.id}`} className="shrink-0">
        <Thumbnail doc={doc} className="h-16 w-12" />
      </Link>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          {!doc.is_read && <CircleDot className="size-3 shrink-0 text-primary" aria-label="Unread" />}
          <Link
            to={`/documents/${doc.id}`}
            className={cn("truncate hover:underline", doc.is_read ? "font-medium" : "font-semibold")}
          >
            {doc.title}
          </Link>
          {doc.is_important && <Star className="size-4 shrink-0 fill-amber-400 text-amber-400" aria-label="Important" />}
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
          <StatusBadge doc={doc} />
          {doc.correspondent && <span className="font-medium text-foreground/80">{doc.correspondent.name}</span>}
          {doc.document_type && <span>· {doc.document_type.name}</span>}
          {showFolder && doc.folder && (
            <span className="inline-flex items-center gap-1" title={doc.folder.path}>
              · <Folder className="size-3" /> {doc.folder.name}
            </span>
          )}
          {doc.document_date && <span>· {formatDate(doc.document_date)}</span>}
          {doc.series && (
            <span className="inline-flex items-center gap-1">
              · <Layers className="size-3" /> {doc.series.name}
              {doc.period_label && ` (${doc.period_label})`}
            </span>
          )}
          {doc.tags.map((t) => (
            <TagChip key={t.id} name={t.name} color={t.color} />
          ))}
        </div>
        {doc.snippet && doc.snippet.text && (
          <p className="mt-1.5 line-clamp-2 text-sm text-muted-foreground">
            <Highlighted text={doc.snippet.text} highlights={doc.snippet.highlights} />
          </p>
        )}
        {doc.processing_state === "failed" && (
          <p className="mt-1 text-xs text-destructive">{doc.processing_error}</p>
        )}
      </div>
      <div className="flex shrink-0 flex-col items-end gap-1">
        <span className="text-xs text-muted-foreground" title={formatDateTime(doc.uploaded_at)}>
          {formatDateTime(doc.uploaded_at)}
        </span>
        <div className="flex items-center gap-0.5 opacity-100 md:opacity-0 md:group-hover:opacity-100 md:focus-within:opacity-100">
          {doc.status !== "done" && (
            <Button variant="ghost" size="icon-sm" title="Mark done" onClick={() => act("status_done")}>
              <Check />
            </Button>
          )}
          <Button
            variant="ghost"
            size="icon-sm"
            title={doc.is_important ? "Not important" : "Important"}
            onClick={() => act(doc.is_important ? "unimportant" : "important")}
          >
            <Star className={cn(doc.is_important && "fill-amber-400 text-amber-400")} />
          </Button>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="icon-sm" title="More">
                <MoreHorizontal />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent>
              <DropdownMenuItem onSelect={() => setMoving(true)}>Move to folder…</DropdownMenuItem>
              <DropdownMenuItem onSelect={() => act("status_todo")}>Mark as todo</DropdownMenuItem>
              <DropdownMenuItem onSelect={() => act("status_new")}>Mark as new</DropdownMenuItem>
              <DropdownMenuItem onSelect={() => act(doc.is_read ? "mark_unread" : "mark_read")}>
                Mark as {doc.is_read ? "unread" : "read"}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          <MoveToFolderDialog ids={[doc.id]} open={moving} onOpenChange={setMoving} />
        </div>
      </div>
    </div>
  );
}

export function BulkBar({ ids, onClear }: { ids: string[]; onClear: () => void }) {
  const bulk = useBulkAction();
  const [reprocessOpen, setReprocessOpen] = useState(false);
  const [pagesOpen, setPagesOpen] = useState(false);
  if (!ids.length) return null;
  const run = (action: Parameters<typeof bulk.mutate>[0]["action"]) =>
    bulk.mutate({ ids, action }, { onSuccess: onClear });
  return (
    <div className="sticky top-0 z-10 mb-3 flex flex-wrap items-center gap-2 rounded-lg border bg-card px-3 py-2 shadow-sm">
      <span className="text-sm font-medium">{ids.length} selected</span>
      <div className="ml-auto flex flex-wrap gap-1">
        <Button size="sm" variant="outline" onClick={() => run("status_done")}>
          <Check /> Done
        </Button>
        <Button size="sm" variant="outline" onClick={() => run("status_todo")}>
          Todo
        </Button>
        <Button size="sm" variant="outline" onClick={() => run("mark_read")}>
          Mark read
        </Button>
        <Button size="sm" variant="outline" onClick={() => run("important")}>
          <Star /> Important
        </Button>
        <MoveButton ids={ids} onMoved={onClear} />
        <SuggestFilingButton ids={ids} onDone={onClear} />
        <AssistantButton ids={ids} scope={`${ids.length} selected document${ids.length === 1 ? "" : "s"}`} onDone={onClear} />
        <Button
          size="sm"
          variant="outline"
          onClick={() => setPagesOpen(true)}
          disabled={ids.length > 50}
          title="Merge these documents, or move pages between them — the originals stay untouched"
        >
          <Layers /> Pages
        </Button>
        <Button size="sm" variant="outline" onClick={() => setReprocessOpen(true)}>
          <RefreshCw /> Reprocess…
        </Button>
        <Button size="sm" variant="ghost" onClick={onClear}>
          Clear
        </Button>
      </div>
      <ReprocessDialog target={{ ids }} open={reprocessOpen} onOpenChange={setReprocessOpen} onDone={onClear} />
      {pagesOpen && (
        <PageEditor
          sourceIds={ids}
          heading={`Pages of ${ids.length} documents`}
          explanation="Hide or reorder pages in the same document, split off additional parts, or merge processed pages. The summary shows which documents stay and which go to recoverable trash. Originals are never changed."
          onApplied={onClear}
          onClose={() => setPagesOpen(false)}
        />
      )}
    </div>
  );
}
