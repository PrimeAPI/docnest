import { useQueryClient } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { toast } from "sonner";
import { call, client, type Schemas } from "@/api/client";
import { keys, useInvalidateDocuments, useSystem } from "@/api/queries";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label, Select } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { useAiSettings } from "./ai-model";
import { DEFAULT_ENHANCEMENT, type EnhanceSettings, EnhancementForm, Toggle } from "./enhancement";

type Step = "enhance" | "ocr" | "analyze";
type Backend = "" | "ocrmypdf" | "docling";
// "" = as in Settings, "-" = rules only, else an installed model.
type ModelChoice = string;
const RULES_ONLY = "-";

export type ReprocessTarget = { ids: string[] } | { all: true; count?: number };

/**
 * The one reprocess dialog: for a document, a selection, or every document.
 * Pick the steps to run again and, per step, its processor or model.
 */
export function ReprocessDialog({
  target,
  open,
  onOpenChange,
  onDone,
}: {
  target: ReprocessTarget;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone?: () => void;
}) {
  const system = useSystem();
  const ai = useAiSettings({ enabled: open });
  const qc = useQueryClient();
  const invalidate = useInvalidateDocuments();
  const [steps, setSteps] = useState<Set<Step>>(new Set(["analyze"]));
  const [backend, setBackend] = useState<Backend>("");
  const [model, setModel] = useState<ModelChoice>("");
  const [custom, setCustom] = useState(false);
  const [settings, setSettings] = useState<EnhanceSettings | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) {
      setSteps(new Set(["analyze"]));
      setBackend("");
      setModel("");
      setCustom(false);
      setSettings(null);
    }
  }, [open]);

  const all = "all" in target;
  const ids = all ? [] : target.ids;
  const count = all ? target.count : ids.length;
  const many = all || ids.length > 1;
  const enhance = steps.has("enhance");
  const ocr = enhance || steps.has("ocr");
  const analyze = steps.has("analyze");
  const current = settings ?? system.data?.scan_enhancement ?? DEFAULT_ENHANCEMENT;
  const aiReady = ai.data?.reachable && ai.data.configured;
  const systemModel = ai.data?.model;

  const toggle = (step: Step, on: boolean) =>
    setSteps((previous) => {
      const next = new Set(previous);
      if (on) next.add(step);
      else next.delete(step);
      return next;
    });

  const submit = async () => {
    const chosen: Step[] = (["enhance", "ocr", "analyze"] as const).filter(
      (s) => (s === "ocr" ? ocr : steps.has(s)),
    );
    const body: Schemas["ReprocessIn"] = {
      steps: chosen,
      backend: ocr && backend ? backend : undefined,
      enhancement: enhance && custom ? current : undefined,
      ai_model: analyze && model ? (model === RULES_ONLY ? "" : model) : undefined,
    };
    setBusy(true);
    try {
      let queued: number;
      if (all) {
        queued = (await call(() => client.POST("/api/v1/documents/reprocess-all", { body }))).updated;
      } else if (many) {
        queued = (
          await call(() => client.POST("/api/v1/documents/bulk", { body: { ids, action: "reprocess", reprocess: body } }))
        ).updated;
      } else {
        await call(() =>
          client.POST("/api/v1/documents/{doc_id}/reprocess", { params: { path: { doc_id: ids[0] } }, body }),
        );
        queued = 1;
      }
      const skipped = !all && many ? ids.length - queued : 0;
      toast.success(
        many
          ? `${queued} document${queued === 1 ? "" : "s"} queued` + (skipped ? ` (${skipped} already in the queue)` : "")
          : "Reprocessing started",
      );
      invalidate();
      qc.invalidateQueries({ queryKey: keys.processingQueue });
      onOpenChange(false);
      onDone?.();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const title = all
    ? `Reprocess all ${count ?? ""} documents`.replace("  ", " ")
    : many
      ? `Reprocess ${ids.length} documents`
      : "Reprocess document";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>
            Choose what runs again. Documents stay readable meanwhile, and fields you set yourself are kept.
            {many && " Documents already in the queue are skipped."}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <StepBox
            checked={enhance}
            onChange={(on) => toggle("enhance", on)}
            label="Scan enhancement"
            hint="Straighten, crop, clean up and drop blank pages, starting again from the untouched original."
          >
            <Toggle
              label="Use different enhancement settings for this run"
              hint="Only for this run; the system settings stay unchanged."
              checked={custom}
              onChange={setCustom}
            />
            {custom && (
              <div className="mt-3 rounded-md border p-4">
                <EnhancementForm value={current} onChange={(c) => setSettings({ ...current, ...c })} />
              </div>
            )}
          </StepBox>

          <StepBox
            checked={ocr}
            disabled={enhance}
            onChange={(on) => toggle("ocr", on)}
            label="Text recognition"
            hint={
              enhance
                ? "Runs with the enhancement: the pages change, so their text is read again."
                : "Reads the current version of the pages again (the enhancement stays as it is)."
            }
          >
            <div className="grid gap-1.5">
              <Label htmlFor="reprocess-backend">Processor</Label>
              <Select id="reprocess-backend" value={backend} onChange={(e) => setBackend(e.target.value as Backend)}>
                <option value="">{many ? "Keep each document's processor" : "Keep the current processor"}</option>
                <option value="ocrmypdf">OCRmyPDF / Tesseract — searchable PDF/A</option>
                <option value="docling">Docling — layout, tables and reading order</option>
              </Select>
            </div>
          </StepBox>

          <StepBox
            checked={analyze}
            onChange={(on) => toggle("analyze", on)}
            label="Analysis"
            hint="Sender, title, date, type, tags and series from the recognised text."
          >
            <div className="grid gap-1.5">
              <Label htmlFor="reprocess-model">Read by</Label>
              <Select id="reprocess-model" value={model} onChange={(e) => setModel(e.target.value)}>
                <option value="">
                  As in Settings ({systemModel ? `AI model ${systemModel}` : "rules only"})
                </option>
                <option value={RULES_ONLY}>Rules only — no AI model</option>
                {aiReady &&
                  ai.data?.models.map((m) => (
                    <option key={m.name} value={m.name}>
                      AI model {m.name}
                      {m.vision ? "" : " (text only)"}
                      {m.thinking ? " (reasoning, slow)" : ""}
                    </option>
                  ))}
              </Select>
              {!aiReady && ai.data && (
                <p className="text-xs text-muted-foreground">
                  AI models appear here once Ollama is set up (Settings → System → AI analysis).
                </p>
              )}
            </div>
          </StepBox>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={busy || !(ocr || analyze) || (!all && !ids.length)}>
            <RefreshCw /> Reprocess
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function StepBox({
  checked,
  disabled,
  onChange,
  label,
  hint,
  children,
}: {
  checked: boolean;
  disabled?: boolean;
  onChange: (on: boolean) => void;
  label: string;
  hint: string;
  children: ReactNode;
}) {
  return (
    <div className={cn("rounded-md border p-3", checked && "border-primary/40 bg-primary/5")}>
      <Toggle label={label} hint={hint} checked={checked} disabled={disabled} onChange={onChange} />
      {checked && <div className="mt-3 pl-6">{children}</div>}
    </div>
  );
}
