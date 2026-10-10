import { CircleDot, FileText, LayoutGrid, List, Rows3, Star } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import type { DocumentListItem } from "@/api/queries";
import { Card } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { DocumentRow, StatusBadge, Thumbnail } from "@/features/documents/document-row";
import { dragDocuments } from "@/features/folders/tree";
import { cn, formatDate } from "@/lib/utils";

/** list: a table, title and facts only · compact: rows with a small preview · cards: big previews. */
export type ViewMode = "list" | "compact" | "cards";
/** Pages default to compact; dialogs, which only name documents, to list. Each remembers its own choice. */
export type ViewScope = "page" | "dialog";

const DEFAULTS: Record<ViewScope, ViewMode> = { page: "compact", dialog: "list" };
const MODES: { mode: ViewMode; label: string; icon: typeof List }[] = [
  { mode: "list", label: "List", icon: List },
  { mode: "compact", label: "Compact", icon: Rows3 },
  { mode: "cards", label: "Cards", icon: LayoutGrid },
];

function stored(scope: ViewScope): ViewMode {
  try {
    const value = localStorage.getItem(`docnest.view.${scope}`);
    if (value === "list" || value === "compact" || value === "cards") return value;
  } catch {
    // no storage (private window): the default
  }
  return DEFAULTS[scope];
}

export function useViewMode(scope: ViewScope): [ViewMode, (mode: ViewMode) => void] {
  const [mode, setMode] = useState<ViewMode>(() => stored(scope));
  const set = (next: ViewMode) => {
    setMode(next);
    try {
      localStorage.setItem(`docnest.view.${scope}`, next);
    } catch {
      // kept for this visit only
    }
  };
  return [mode, set];
}

export function ViewSwitch({ mode, onChange, className }: { mode: ViewMode; onChange: (m: ViewMode) => void; className?: string }) {
  return (
    <div role="radiogroup" aria-label="Display" className={cn("inline-flex shrink-0 rounded-md border bg-card p-0.5", className)}>
      {MODES.map(({ mode: m, label, icon: Icon }) => (
        <button
          key={m}
          type="button"
          role="radio"
          aria-checked={mode === m}
          title={label}
          onClick={() => onChange(m)}
          className={cn(
            "flex h-7 items-center gap-1 rounded px-2 text-xs text-muted-foreground transition-colors hover:text-foreground",
            mode === m && "bg-accent text-foreground",
          )}
        >
          <Icon className="size-4" />
          <span className="sr-only sm:not-sr-only">{label}</span>
        </button>
      ))}
    </div>
  );
}

type Selection = {
  selected?: string[];
  onSelect?: (id: string, checked: boolean) => void;
};

/** The documents of a page in the chosen view; selection and drag work the same in all three. */
export function DocumentCollection({
  docs,
  mode,
  selected,
  onSelect,
  showFolder = true,
  dim = false,
  className,
}: {
  docs: DocumentListItem[];
  mode: ViewMode;
  showFolder?: boolean;
  dim?: boolean;
  className?: string;
} & Selection) {
  const isSelected = (id: string) => selected?.includes(id) ?? false;
  const dragIds = (id: string) => (isSelected(id) && selected ? selected : [id]);
  const select = onSelect ? (id: string) => (v: boolean) => onSelect(id, v) : undefined;

  if (mode === "cards") {
    return (
      <div className={cn("grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 2xl:grid-cols-5", dim && "opacity-70", className)}>
        {docs.map((d) => (
          <DocumentCard key={d.id} doc={d} selected={isSelected(d.id)} onSelect={select?.(d.id)} dragIds={dragIds(d.id)} showFolder={showFolder} />
        ))}
      </div>
    );
  }
  if (mode === "list") {
    return (
      <Card className={cn("overflow-hidden", dim && "opacity-70", className)}>
        <table className="w-full table-fixed text-sm">
          <thead className="border-b bg-muted/40 text-left text-xs text-muted-foreground">
            <tr>
              {onSelect && <th className="w-9 px-3 py-2" aria-label="Select" />}
              <th className="px-2 py-2 font-medium">Title</th>
              <th className="hidden w-36 px-2 py-2 font-medium sm:table-cell">Sender</th>
              <th className="hidden w-28 px-2 py-2 font-medium 2xl:table-cell">Type</th>
              {showFolder && <th className="hidden w-32 px-2 py-2 font-medium xl:table-cell">Folder</th>}
              <th className="w-24 px-2 py-2 font-medium">Date</th>
              <th className="hidden w-24 px-2 py-2 font-medium lg:table-cell">Status</th>
            </tr>
          </thead>
          <tbody>
            {docs.map((d) => (
              <ListRow key={d.id} doc={d} selected={isSelected(d.id)} onSelect={select?.(d.id)} dragIds={dragIds(d.id)} showFolder={showFolder} />
            ))}
          </tbody>
        </table>
      </Card>
    );
  }
  return (
    <Card className={cn("overflow-hidden", dim && "opacity-70", className)}>
      {docs.map((d) => (
        <DocumentRow
          key={d.id}
          doc={d}
          showFolder={showFolder}
          selected={isSelected(d.id)}
          dragIds={isSelected(d.id) ? selected : undefined}
          onSelect={select?.(d.id)}
        />
      ))}
    </Card>
  );
}

type ItemProps = {
  doc: DocumentListItem;
  selected: boolean;
  onSelect?: (checked: boolean) => void;
  dragIds: string[];
  showFolder: boolean;
};

function ListRow({ doc, selected, onSelect, dragIds, showFolder }: ItemProps) {
  return (
    <tr
      draggable
      onDragStart={(e) => dragDocuments(e, dragIds)}
      className={cn("border-b last:border-b-0 hover:bg-muted/50", selected && "bg-accent/50")}
    >
      {onSelect && (
        <td className="px-3 py-1.5 align-middle">
          <Checkbox checked={selected} onCheckedChange={(v) => onSelect(v === true)} aria-label="Select" />
        </td>
      )}
      <td className="px-2 py-1.5">
        <span className="flex min-w-0 items-center gap-1.5">
          {!doc.is_read && <CircleDot className="size-3 shrink-0 text-primary" aria-label="Unread" />}
          <Link
            to={`/documents/${doc.id}`}
            className={cn("truncate hover:underline", doc.is_read ? "font-medium" : "font-semibold")}
            title={doc.title}
          >
            {doc.title}
          </Link>
          {doc.is_important && <Star className="size-3.5 shrink-0 fill-amber-400 text-amber-400" aria-label="Important" />}
        </span>
        {/* On narrow screens the sender moves under the title. */}
        {doc.correspondent && <span className="block truncate text-xs text-muted-foreground sm:hidden">{doc.correspondent.name}</span>}
      </td>
      <td className="hidden truncate px-2 py-1.5 text-muted-foreground sm:table-cell">{doc.correspondent?.name}</td>
      <td className="hidden truncate px-2 py-1.5 text-muted-foreground 2xl:table-cell">{doc.document_type?.name}</td>
      {showFolder && (
        <td className="hidden truncate px-2 py-1.5 text-muted-foreground xl:table-cell" title={doc.folder?.path}>
          {doc.folder?.name}
        </td>
      )}
      <td className="whitespace-nowrap px-2 py-1.5 text-muted-foreground">{doc.document_date ? formatDate(doc.document_date) : ""}</td>
      <td className="hidden px-2 py-1.5 lg:table-cell">
        <StatusBadge doc={doc} />
      </td>
    </tr>
  );
}

function DocumentCard({ doc, selected, onSelect, dragIds, showFolder }: ItemProps) {
  return (
    <div
      draggable
      onDragStart={(e) => dragDocuments(e, dragIds)}
      className={cn(
        "group relative flex min-w-0 flex-col overflow-hidden rounded-lg border bg-card transition-colors hover:border-primary/50",
        selected && "border-primary ring-2 ring-primary/40",
      )}
    >
      <Link to={`/documents/${doc.id}`} className="block">
        <Thumbnail doc={doc} className="aspect-[3/4] w-full rounded-none border-0 border-b" />
      </Link>
      {onSelect && (
        <div
          className={cn(
            "absolute left-2 top-2 rounded bg-card/90 p-1 shadow-sm",
            !selected && "md:opacity-0 md:group-hover:opacity-100 md:focus-within:opacity-100",
          )}
        >
          <Checkbox checked={selected} onCheckedChange={(v) => onSelect(v === true)} aria-label="Select" />
        </div>
      )}
      {doc.is_important && (
        <Star className="absolute right-2 top-2 size-5 fill-amber-400 text-amber-400 drop-shadow" aria-label="Important" />
      )}
      <div className="flex min-w-0 flex-1 flex-col gap-1 p-2.5">
        <Link
          to={`/documents/${doc.id}`}
          className={cn("line-clamp-2 text-sm leading-snug hover:underline", doc.is_read ? "font-medium" : "font-semibold")}
          title={doc.title}
        >
          {!doc.is_read && <CircleDot className="mr-1 inline size-3 text-primary" aria-label="Unread" />}
          {doc.title}
        </Link>
        <span className="truncate text-xs text-muted-foreground">
          {[doc.correspondent?.name, doc.document_date && formatDate(doc.document_date)].filter(Boolean).join(" · ")}
        </span>
        {showFolder && doc.folder && (
          <span className="truncate text-xs text-muted-foreground" title={doc.folder.path}>
            {doc.folder.name}
          </span>
        )}
        <div className="mt-auto pt-1">
          <StatusBadge doc={doc} />
        </div>
      </div>
    </div>
  );
}

// --- In dialogs ---------------------------------------------------------------------------

/** A document as a dialog names it: enough to recognise it, plus the dialog's own control. */
export type DocItem = {
  id: string;
  title: string;
  detail?: string; // "ACME GmbH · 28.01.2026"
  control?: React.ReactNode; // a checkbox or a picker
  lead?: React.ReactNode; // shown before the title in list and compact view (a checkbox)
};

function ThumbById({ id, className }: { id: string; className?: string }) {
  const [failed, setFailed] = useState(false);
  return (
    <div className={cn("flex shrink-0 items-center justify-center overflow-hidden rounded border bg-muted text-muted-foreground", className)}>
      {failed ? (
        <FileText className="size-4" />
      ) : (
        <img
          src={`/api/v1/documents/${id}/thumbnail`}
          alt=""
          loading="lazy"
          draggable={false}
          className="h-full w-full object-cover object-top"
          onError={() => setFailed(true)}
        />
      )}
    </div>
  );
}

/** Documents listed inside a dialog, in the chosen view; the order is the caller's. */
export function DocItems({ items, mode, className }: { items: DocItem[]; mode: ViewMode; className?: string }) {
  if (mode === "cards") {
    return (
      <div className={cn("grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4", className)}>
        {items.map((d) => (
          <div key={d.id} className="relative flex min-w-0 flex-col overflow-hidden rounded-md border bg-card">
            <a href={`/documents/${d.id}`} target="_blank" rel="noreferrer">
              <ThumbById id={d.id} className="aspect-[3/4] w-full rounded-none border-0 border-b" />
            </a>
            {d.lead && <div className="absolute left-1.5 top-1.5 rounded bg-card/90 p-1 shadow-sm">{d.lead}</div>}
            <div className="flex min-w-0 flex-1 flex-col gap-1 p-2">
              <a href={`/documents/${d.id}`} target="_blank" rel="noreferrer" className="line-clamp-2 text-xs font-medium hover:underline" title={d.title}>
                {d.title}
              </a>
              {d.detail && <span className="truncate text-xs text-muted-foreground">{d.detail}</span>}
              {d.control && <div className="mt-auto pt-1 [&>*]:!w-full">{d.control}</div>}
            </div>
          </div>
        ))}
      </div>
    );
  }
  const compact = mode === "compact";
  return (
    <ul className={cn("flex flex-col divide-y", className)}>
      {items.map((d) => (
        <li key={d.id} className={cn("flex flex-col gap-1 py-1.5 sm:flex-row sm:items-center sm:gap-3", compact && "py-2")}>
          <span className="flex min-w-0 flex-1 items-center gap-2">
            {d.lead}
            {compact && (
              <a href={`/documents/${d.id}`} target="_blank" rel="noreferrer" className="shrink-0">
                <ThumbById id={d.id} className="h-12 w-9" />
              </a>
            )}
            <span className={cn("flex min-w-0 flex-1 gap-x-2", compact ? "flex-col" : "items-baseline")}>
              <a href={`/documents/${d.id}`} target="_blank" rel="noreferrer" className="truncate hover:underline" title={d.title}>
                {d.title}
              </a>
              {d.detail && <span className="shrink-0 truncate text-xs text-muted-foreground">{d.detail}</span>}
            </span>
          </span>
          {d.control}
        </li>
      ))}
    </ul>
  );
}
