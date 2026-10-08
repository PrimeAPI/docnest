import { useMutation, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, RotateCcw } from "lucide-react";
import { useEffect, useId, useState } from "react";
import { toast } from "sonner";
import { call, client, type Schemas } from "@/api/client";
import { keys, useInvalidateDocuments, useSystem } from "@/api/queries";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input, Label, Select } from "@/components/ui/input";
import { ErrorNote, Spinner } from "@/components/ui/misc";
import { cn } from "@/lib/utils";

export type EnhanceSettings = Schemas["EnhanceSettingsOut"];
type BoolKey = { [K in keyof EnhanceSettings]: EnhanceSettings[K] extends boolean ? K : never }[keyof EnhanceSettings];
type NumberKey = {
  [K in keyof EnhanceSettings]: EnhanceSettings[K] extends number ? K : never;
}[keyof EnhanceSettings];

export const DEFAULT_ENHANCEMENT: EnhanceSettings = {
  enabled: true,
  rotate: true,
  rotate_min_confidence: 3,
  deskew: true,
  deskew_min_angle: 0.2,
  deskew_max_angle: 8,
  crop: true,
  crop_max_fraction: 0.35,
  cleanup: true,
  cleanup_background: true,
  cleanup_contrast: true,
  cleanup_despeckle: true,
  cleanup_strength: "medium",
  remove_blank: true,
  blank_threshold: 0.01,
};

/** One line describing what the enhancement changed, e.g. "2 straightened · 1 blank page removed". */
export function describeEnhancement(summary: Record<string, unknown> | undefined): string {
  if (!summary || typeof summary.pages !== "number") return "";
  const n = (key: string) => (typeof summary[key] === "number" ? (summary[key] as number) : 0);
  const parts = [
    n("rotated") && `${n("rotated")} rotated`,
    n("deskewed") && `${n("deskewed")} straightened`,
    n("cropped") && `${n("cropped")} cropped`,
    n("cleaned") && `${n("cleaned")} cleaned up`,
    n("removed_blank") && `${n("removed_blank")} blank page${n("removed_blank") === 1 ? "" : "s"} removed`,
  ].filter(Boolean);
  if (parts.length) return parts.join(" · ");
  return n("scanned_pages") ? "No changes were needed" : "No scanned pages (nothing to enhance)";
}

function Toggle({
  label,
  hint,
  checked,
  disabled,
  onChange,
  className,
}: {
  label: string;
  hint?: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (value: boolean) => void;
  className?: string;
}) {
  const id = useId();
  return (
    <div className={cn("flex items-start gap-2", className)}>
      <Checkbox
        id={id}
        checked={checked}
        disabled={disabled}
        onCheckedChange={(v) => onChange(v === true)}
        className="mt-0.5"
      />
      <div className="grid gap-0.5">
        <Label htmlFor={id} className={cn("cursor-pointer", disabled && "opacity-60")}>
          {label}
        </Label>
        {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
      </div>
    </div>
  );
}

function NumberField({
  label,
  hint,
  value,
  min,
  max,
  step,
  unit,
  disabled,
  onChange,
}: {
  label: string;
  hint?: string;
  value: number;
  min: number;
  max: number;
  step: number;
  unit?: string;
  disabled?: boolean;
  onChange: (value: number) => void;
}) {
  const id = useId();
  const [text, setText] = useState(String(value));
  useEffect(() => setText(String(value)), [value]);
  const commit = () => {
    const parsed = Number(text.replace(",", "."));
    if (Number.isFinite(parsed) && parsed >= min && parsed <= max) {
      if (parsed !== value) onChange(parsed);
    } else {
      setText(String(value));
    }
  };
  return (
    <div className="grid gap-1">
      <Label htmlFor={id} className={cn(disabled && "opacity-60")}>
        {label}
      </Label>
      <div className="flex items-center gap-2">
        <Input
          id={id}
          type="number"
          inputMode="decimal"
          min={min}
          max={max}
          step={step}
          value={text}
          disabled={disabled}
          className="h-8 w-28"
          onChange={(e) => setText(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") e.currentTarget.blur();
          }}
        />
        {unit && <span className="text-xs text-muted-foreground">{unit}</span>}
      </div>
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

/** All scan enhancement options. Used for the system settings and for one-off reprocessing. */
export function EnhancementForm({
  value,
  onChange,
  disabled,
}: {
  value: EnhanceSettings;
  onChange: (changes: Partial<EnhanceSettings>) => void;
  disabled?: boolean;
}) {
  const off = disabled || !value.enabled;
  const bool = (key: BoolKey) => ({
    checked: value[key],
    onChange: (v: boolean) => onChange({ [key]: v } as Partial<EnhanceSettings>),
  });
  const num = (key: NumberKey) => ({
    value: value[key],
    onChange: (v: number) => onChange({ [key]: v } as Partial<EnhanceSettings>),
  });
  return (
    <div className="flex flex-col gap-5">
      <Toggle
        label="Enhance scanned pages"
        hint="The original is always kept unchanged. Only pages that are a scanned image are touched; pages with real text stay as they are."
        disabled={disabled}
        {...bool("enabled")}
      />
      <fieldset className="grid gap-5 border-l-2 pl-4" disabled={off}>
        <div className="grid gap-3">
          <Toggle label="Turn sideways and upside-down pages upright" disabled={off} {...bool("rotate")} />
          {value.rotate && (
            <div className="pl-6">
              <NumberField
                label="Minimum confidence"
                hint="Tesseract's orientation confidence. Higher = only turn pages it is very sure about."
                min={0}
                max={30}
                step={0.5}
                disabled={off}
                {...num("rotate_min_confidence")}
              />
            </div>
          )}
        </div>
        <div className="grid gap-3">
          <Toggle label="Straighten crooked pages" disabled={off} {...bool("deskew")} />
          {value.deskew && (
            <div className="grid gap-3 pl-6 sm:grid-cols-2">
              <NumberField
                label="Ignore skew below"
                unit="°"
                min={0}
                max={5}
                step={0.05}
                disabled={off}
                {...num("deskew_min_angle")}
              />
              <NumberField
                label="Ignore estimates above"
                unit="°"
                min={0.5}
                max={30}
                step={0.5}
                disabled={off}
                {...num("deskew_max_angle")}
              />
            </div>
          )}
        </div>
        <div className="grid gap-3">
          <Toggle
            label="Crop to the paper"
            hint="Removes scanner background where the scan is longer than the sheet, and the shadow at the paper edge."
            disabled={off}
            {...bool("crop")}
          />
          {value.crop && (
            <div className="pl-6">
              <NumberField
                label="Cut at most"
                hint="Share of the page height/width that may be removed. Larger cuts are not trusted."
                unit="%"
                min={5}
                max={60}
                step={1}
                disabled={off}
                value={Math.round(value.crop_max_fraction * 100)}
                onChange={(v) => onChange({ crop_max_fraction: v / 100 })}
              />
            </div>
          )}
        </div>
        <div className="grid gap-3">
          <Toggle label="Clean up the image" disabled={off} {...bool("cleanup")} />
          {value.cleanup && (
            <div className="grid gap-3 pl-6">
              <Toggle
                label="Whiten the paper"
                hint="Grey or yellowed paper and uneven lighting become white."
                disabled={off}
                {...bool("cleanup_background")}
              />
              <Toggle
                label="Strengthen faint text"
                hint="A mild contrast stretch for pale scans."
                disabled={off}
                {...bool("cleanup_contrast")}
              />
              <Toggle
                label="Remove dust and specks"
                hint="Only tiny isolated dots; punctuation and accents next to text stay."
                disabled={off}
                {...bool("cleanup_despeckle")}
              />
              <div className="grid gap-1">
                <Label htmlFor="cleanup-strength" className={cn(off && "opacity-60")}>
                  Strength
                </Label>
                <Select
                  id="cleanup-strength"
                  className="h-8 w-40"
                  value={value.cleanup_strength}
                  disabled={off}
                  onChange={(e) => onChange({ cleanup_strength: e.target.value as EnhanceSettings["cleanup_strength"] })}
                >
                  <option value="low">Low</option>
                  <option value="medium">Medium</option>
                  <option value="high">High</option>
                </Select>
              </div>
            </div>
          )}
        </div>
        <div className="grid gap-3">
          <Toggle
            label="Remove blank pages"
            hint="Empty backsides from duplex scans. If every page looks blank, all are kept."
            disabled={off}
            {...bool("remove_blank")}
          />
          {value.remove_blank && (
            <div className="pl-6">
              <NumberField
                label="A page counts as blank below"
                hint="Share of the page covered by ink. 0.01 % keeps a page with a single short line."
                unit="% ink"
                min={0}
                max={2}
                step={0.005}
                disabled={off}
                {...num("blank_threshold")}
              />
            </div>
          )}
        </div>
      </fieldset>
    </div>
  );
}

export function EnhancementCard() {
  const system = useSystem();
  const qc = useQueryClient();
  const update = useMutation({
    mutationFn: (changes: Partial<EnhanceSettings>) =>
      call(() => client.PUT("/api/v1/settings/enhancement", { body: changes })),
    onSuccess: (data) => {
      qc.setQueryData(keys.system, (current: Schemas["SystemStatus"] | undefined) =>
        current ? { ...current, scan_enhancement: data } : current,
      );
      toast.success("Scan enhancement updated");
    },
    onError: (e) => toast.error(e.message),
  });
  const value = system.data?.scan_enhancement;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Scan enhancement</CardTitle>
        <CardDescription>
          Runs before OCR / Docling. Documents show the enhanced version; the untouched original can always be viewed
          and downloaded. Changes apply to new documents; reprocess existing ones to apply them there.
        </CardDescription>
      </CardHeader>
      <CardContent className="max-w-xl">
        {value ? (
          <div className="flex flex-col gap-4">
            <EnhancementForm value={value} disabled={update.isPending} onChange={(c) => update.mutate(c)} />
            <div>
              <Button
                variant="outline"
                size="sm"
                disabled={update.isPending}
                onClick={() => update.mutate(DEFAULT_ENHANCEMENT)}
              >
                <RotateCcw /> Restore defaults
              </Button>
            </div>
          </div>
        ) : system.error ? (
          <ErrorNote error={system.error} />
        ) : (
          <Spinner />
        )}
      </CardContent>
    </Card>
  );
}

/** Reprocess one or many documents from the original, optionally with one-off enhancement settings. */
export function ReprocessDialog({
  ids,
  open,
  onOpenChange,
  onDone,
}: {
  ids: string[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onDone?: () => void;
}) {
  const system = useSystem();
  const invalidate = useInvalidateDocuments();
  const [backend, setBackend] = useState<"" | "ocrmypdf" | "docling">("");
  const [custom, setCustom] = useState(false);
  const [settings, setSettings] = useState<EnhanceSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const defaults = system.data?.scan_enhancement;

  useEffect(() => {
    if (open) {
      setBackend("");
      setCustom(false);
      setSettings(null);
    }
  }, [open]);

  const current = settings ?? defaults ?? DEFAULT_ENHANCEMENT;
  const many = ids.length > 1;

  const submit = async () => {
    const reprocess = {
      stage: "ocr" as const,
      backend: backend || undefined,
      enhancement: custom ? current : undefined,
    };
    setBusy(true);
    try {
      if (many) {
        const result = await call(() =>
          client.POST("/api/v1/documents/bulk", { body: { ids, action: "reprocess", reprocess } }),
        );
        const skipped = ids.length - result.updated;
        toast.success(
          `Reprocessing ${result.updated} document${result.updated === 1 ? "" : "s"}` +
            (skipped ? ` (${skipped} already in the queue)` : ""),
        );
      } else {
        await call(() =>
          client.POST("/api/v1/documents/{doc_id}/reprocess", {
            params: { path: { doc_id: ids[0] } },
            body: reprocess,
          }),
        );
        toast.success("Reprocessing started");
      }
      invalidate();
      onOpenChange(false);
      onDone?.();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{many ? `Reprocess ${ids.length} documents` : "Reprocess from the original"}</DialogTitle>
          <DialogDescription>
            Scan enhancement, text recognition and analysis run again from the untouched original. Fields you set
            yourself are kept.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-5">
          <div className="grid gap-1.5">
            <Label htmlFor="reprocess-backend">Processor</Label>
            <Select
              id="reprocess-backend"
              value={backend}
              onChange={(e) => setBackend(e.target.value as typeof backend)}
            >
              <option value="">{many ? "Keep each document's processor" : "Keep the current processor"}</option>
              <option value="ocrmypdf">OCRmyPDF / Tesseract</option>
              <option value="docling">Docling</option>
            </Select>
          </div>
          <Toggle
            label="Use different scan enhancement settings for this run"
            hint="Only for this reprocessing; the system settings stay unchanged."
            checked={custom}
            onChange={setCustom}
          />
          {custom && (
            <div className="rounded-md border p-4">
              <EnhancementForm value={current} onChange={(c) => setSettings({ ...current, ...c })} />
            </div>
          )}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={busy || !ids.length}>
            <RefreshCw /> Reprocess
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
