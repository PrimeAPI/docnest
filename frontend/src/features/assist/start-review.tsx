import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bot, Cpu, Moon, Play, ScanSearch, Telescope } from "lucide-react";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";
import { call, client } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input, Label, Select, Textarea } from "@/components/ui/input";
import { Spinner } from "@/components/ui/misc";
import { cn, formatBytes } from "@/lib/utils";

/** The next time it is `hh:mm` after `after`. */
export function nextTime(hhmm: string, after: Date): Date {
  const [h, m] = hhmm.split(":").map(Number);
  const at = new Date(after);
  at.setHours(h, m, 0, 0);
  if (at <= after) at.setDate(at.getDate() + 1);
  return at;
}

export function usePagesStatus() {
  return useQuery({
    queryKey: ["pages-status"],
    queryFn: () => call(() => client.GET("/api/v1/alterations/pages/status")),
    refetchInterval: (q) => ((q.state.data?.queued ?? 0) > 0 ? 5000 : false),
  });
}

export function PreparePagesNote() {
  const qc = useQueryClient();
  const status = usePagesStatus();
  const prepare = useMutation({
    mutationFn: () => call(() => client.POST("/api/v1/alterations/pages/prepare")),
    onSuccess: (s) => qc.setQueryData(["pages-status"], s),
    onError: (e) => toast.error(e.message),
  });
  const s = status.data;
  if (!s || s.ready >= s.documents) return null;
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md bg-sky-50 px-3 py-2 text-sm text-sky-900 dark:bg-sky-950 dark:text-sky-100">
      <ScanSearch className="size-4 shrink-0" />
      <span className="min-w-0 flex-1">
        {s.queued > 0
          ? `Preparing pages: ${s.ready} of ${s.documents} documents done.`
          : `${s.documents - s.ready} document(s) need page fingerprints. Reviews prepare these automatically; you can also prepare them now.`}
      </span>
      {s.queued === 0 && (
        <Button size="sm" variant="outline" onClick={() => prepare.mutate()} disabled={prepare.isPending}>
          Prepare all pages
        </Button>
      )}
    </div>
  );
}

/**
 * Start a review: the assistant looks through documents — these, or all of them — and
 * leaves a report. With the AI model it can take all night; without, minutes.
 */
export function StartReviewDialog({
  ids,
  folderId,
  subfolders = false,
  excludedIds = [],
  documentCount,
  scopeLabel,
  onClose,
}: {
  ids?: string[]; // none: every document
  folderId?: number; // resolved completely by the server, never a paginated ID selection
  subfolders?: boolean;
  excludedIds?: string[];
  documentCount?: number;
  scopeLabel: string;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const models = useQuery({
    queryKey: ["assist-models"],
    queryFn: () => call(() => client.GET("/api/v1/assist/models")),
  });
  const [ai, setAi] = useState(true);
  const [instruction, setInstruction] = useState("");
  const [model, setModel] = useState("");
  const [think, setThink] = useState(false);
  const [explore, setExplore] = useState(true);
  const [when, setWhen] = useState<"now" | "tonight">("tonight");
  const [startAt, setStartAt] = useState("01:00");
  const [stopBy, setStopBy] = useState(false);
  const [until, setUntil] = useState("07:00");
  const [context, setContext] = useState(8192);

  const list = models.data ?? [];
  useEffect(() => {
    if (!model && list.length) setModel((list.find((m) => m.current) ?? list[0]).name);
  }, [list, model]);
  const chosen = list.find((m) => m.name === model);
  useEffect(() => {
    if (!chosen?.thinking) setThink(false);
  }, [chosen]);

  const now = new Date();
  const start = when === "now" ? now : nextTime(startAt, now);
  const end = ai && stopBy ? nextTime(until, start) : null;
  const hours = end ? Math.round(((end.getTime() - start.getTime()) / 3_600_000) * 10) / 10 : null;

  const submit = useMutation({
    mutationFn: () =>
      call(() =>
        client.POST("/api/v1/assist/reviews", {
          body: {
            ids: ids ?? [],
            scope: folderId !== undefined ? "folder" : ids ? "selection" : "all",
            folder_id: folderId,
            subfolders,
            excluded_ids: folderId !== undefined ? excludedIds : [],
            instruction,
            model: ai ? model : "",
            ai,
            think: ai && think,
            explore: ai && explore,
            context,
            start: when === "tonight" ? start.toISOString() : null,
            until: end ? end.toISOString() : null,
          },
        }),
      ),
    onSuccess: (r) => {
      void qc.invalidateQueries({ queryKey: ["reviews"] });
      toast.success(when === "now" ? "The assistant is looking through the documents" : `Starts at ${startAt}`, {
        description: "The report will be on the Assistant page.",
      });
      onClose();
      navigate(`/assistant/${r.id}`);
    },
    onError: (e) => toast.error(e.message),
  });

  return (
    <Dialog open onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Telescope className="size-5" /> Look through {scopeLabel}
          </DialogTitle>
          <DialogDescription>
            The assistant compares every page, reads the documents and argues about what it finds. In the morning there
            is a report with suggestions; nothing changes until you apply one.
          </DialogDescription>
        </DialogHeader>
        <p className="text-sm text-muted-foreground">
          {documentCount !== undefined ? `${documentCount} documents included. ` : "The complete selected scope is included. "}
          Larger reviews take longer; documents are read one at a time, not in one huge model request.
        </p>

        <div className="grid gap-2 sm:grid-cols-2" role="radiogroup" aria-label="How thorough">
          <ModeCard
            active={ai}
            onClick={() => setAi(true)}
            icon={<Bot className="size-4" />}
            title="Thorough, with the AI model"
            text="Reads every document, checks names and argues about each finding. Hours — best overnight."
          />
          <ModeCard
            active={!ai}
            onClick={() => setAi(false)}
            icon={<Cpu className="size-4" />}
            title="Quick, pages only"
            text="Pages scanned twice, duplicates, split scans, missing pages and gaps. Minutes, no AI."
          />
        </div>

        <PreparePagesNote />

        {ai && (
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="review-task">What should it do? (optional)</Label>
              <Textarea
                id="review-task"
                rows={2}
                maxLength={1000}
                value={instruction}
                onChange={(e) => setInstruction(e.target.value)}
                placeholder="e.g. “Ordne diesen Ordner: einheitliche Namen, Duplikate und fehlende Seiten finden”"
              />
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="review-model">AI model</Label>
                {models.isPending ? (
                  <Spinner />
                ) : list.length ? (
                  <Select id="review-model" value={model} onChange={(e) => setModel(e.target.value)}>
                    {list.map((m) => (
                      <option key={m.name} value={m.name}>
                        {m.name}
                        {m.parameter_size && ` · ${m.parameter_size}`}
                        {m.size ? ` · ${formatBytes(m.size)}` : ""}
                        {m.current ? " (Settings)" : ""}
                      </option>
                    ))}
                  </Select>
                ) : (
                  <p className="text-sm text-destructive">No AI model is installed (Settings → AI).</p>
                )}
                <p className="text-xs text-muted-foreground">
                  Choose an installed model that fits your server. Each question uses a small part of the archive.
                </p>
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="review-context">Context</Label>
                <Select id="review-context" value={context} onChange={(e) => setContext(Number(e.target.value))}>
                  <option value={4096}>4,096 tokens — least memory</option>
                  <option value={8192}>8,192 tokens — a page or two at once</option>
                  <option value={16384}>16,384 tokens — long letters, more memory</option>
                </Select>
                <p className="text-xs text-muted-foreground">More context needs more memory on the server.</p>
              </div>
            </div>
            <div className="flex flex-col gap-2 text-sm">
              <label className={cn("flex items-start gap-2", !chosen?.thinking && "opacity-50")}>
                <Checkbox checked={think} disabled={!chosen?.thinking} onCheckedChange={(v) => setThink(v === true)} />
                <span>
                  Think before answering
                  <span className="block text-xs text-muted-foreground">
                    {chosen?.thinking
                      ? "The model reasons first: much slower, more careful judgments."
                      : "This model cannot reason first; a “thinking” variant can."}
                  </span>
                </span>
              </label>
              <label className="flex items-start gap-2">
                <Checkbox checked={explore} onCheckedChange={(v) => setExplore(v === true)} />
                <span>
                  Use the time left to work through the task step by step
                  <span className="block text-xs text-muted-foreground">
                    It reads, compares and searches documents itself and proposes what it finds — each proposal argued
                    about before it goes into the report.
                  </span>
                </span>
              </label>
            </div>
          </div>
        )}

        <div className="flex flex-col gap-2 rounded-md border p-3 text-sm">
          <div className="flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-2">
              <input type="radio" checked={when === "now"} onChange={() => setWhen("now")} /> Now
            </label>
            <label className="flex items-center gap-2">
              <input type="radio" checked={when === "tonight"} onChange={() => setWhen("tonight")} />
              <Moon className="size-4" /> At
              <Input
                type="time"
                value={startAt}
                onChange={(e) => setStartAt(e.target.value)}
                className="h-8 w-28"
                disabled={when !== "tonight"}
                aria-label="Start at"
              />
            </label>
          </div>
          {ai && (
            <label className="flex flex-wrap items-center gap-2">
              <Checkbox checked={stopBy} onCheckedChange={(v) => setStopBy(v === true)} />
              Write the report by
              <Input
                type="time"
                value={until}
                onChange={(e) => setUntil(e.target.value)}
                className="h-8 w-28"
                disabled={!stopBy}
                aria-label="Stop by"
              />
              {hours !== null && <span className="text-xs text-muted-foreground">({hours} hours)</span>}
            </label>
          )}
          {(!ai || !stopBy) && <p className="text-xs text-muted-foreground">Run until finished. You can close the browser and return later, or stop and get a partial report from the Assistant page.</p>}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={submit.isPending || (ai && !model)} onClick={() => submit.mutate()}>
            {submit.isPending ? <Spinner /> : when === "now" ? <Play /> : <Moon />}
            {when === "now" ? "Start now" : `Start at ${startAt}`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ModeCard({
  active,
  onClick,
  icon,
  title,
  text,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  title: string;
  text: string;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={active}
      onClick={onClick}
      className={cn(
        "flex flex-col gap-1 rounded-lg border p-3 text-left text-sm transition-colors hover:bg-muted/50",
        active && "border-primary bg-accent/60 ring-1 ring-primary",
      )}
    >
      <span className="flex items-center gap-2 font-medium">
        {icon} {title}
      </span>
      <span className="text-xs text-muted-foreground">{text}</span>
    </button>
  );
}
