import { useQueryClient } from "@tanstack/react-query";
import { Download, PackageOpen, PackagePlus, Trash2 } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";
import { toast } from "sonner";
import { call, client } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input, Label } from "@/components/ui/input";
import { ErrorNote, Spinner } from "@/components/ui/misc";
import { formatBytes, formatDateTime } from "@/lib/utils";
import { ImportFlow, ImportProgress } from "./import-flow";
import { transferStatusKey, useTransferStatus } from "./transfer-api";

/** Settings → System: move the whole instance to another server. */
export function TransferCard() {
  const qc = useQueryClient();
  const status = useTransferStatus();
  const [exporting, setExporting] = useState(false);
  const [importing, setImporting] = useState(false);
  // An import started elsewhere (or before a reload): follow it until it ends.
  const [following, setFollowing] = useState(false);
  const s = status.data;
  const exportRunning = s?.operation === "export" && s.state === "running";
  const importRunning = s?.operation === "import" && s.state === "running";
  useEffect(() => {
    if (importRunning && !importing) setFollowing(true);
  }, [importRunning, importing]);

  const removeExport = async () => {
    try {
      qc.setQueryData(transferStatusKey, await call(() => client.DELETE("/api/v1/transfer/export")));
      toast.success("Export deleted from the server");
    } catch (err) {
      toast.error((err as Error).message);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Export &amp; import</CardTitle>
        <CardDescription>
          Move DocNest to another server: one password-protected ZIP with the database, the master key, all settings,
          the Proton Drive sign-in and the files not yet in Proton Drive. Your documents stay in Proton Drive.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {s?.export && !exportRunning && (
          <div className="flex flex-wrap items-center gap-3 rounded-md border p-3 text-sm">
            <div className="min-w-0 flex-1">
              <div className="truncate font-medium">{s.export.name}</div>
              <div className="text-xs text-muted-foreground">
                {formatBytes(s.export.size)}
                {s.finished_at && ` · created ${formatDateTime(s.finished_at)}`} · deleted from the server after 6 hours
              </div>
            </div>
            <Button asChild size="sm">
              <a href="/api/v1/transfer/export/download" download>
                <Download /> Download
              </a>
            </Button>
            <Button size="sm" variant="ghost" onClick={removeExport} aria-label="Delete export from the server">
              <Trash2 />
            </Button>
          </div>
        )}
        {s?.operation === "export" && s.state === "failed" && s.message && (
          <ErrorNote error={new Error(`Last export failed: ${s.message}`)} />
        )}
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => setExporting(true)} disabled={importRunning}>
            {exportRunning ? <Spinner /> : <PackagePlus />}
            {exportRunning ? s?.step || "Exporting…" : "Export everything…"}
          </Button>
          <Button variant="outline" onClick={() => setImporting(true)} disabled={exportRunning}>
            <PackageOpen /> Import an export…
          </Button>
        </div>
      </CardContent>
      <ExportDialog open={exporting} onClose={() => setExporting(false)} />
      <Dialog
        open={importing || following}
        onOpenChange={(o) => !o && !importRunning && (setImporting(false), setFollowing(false))}
      >
        <DialogContent className="max-w-lg" onInteractOutside={(e) => e.preventDefault()}>
          <DialogHeader>
            <DialogTitle>Import an export</DialogTitle>
            <DialogDescription>
              Replaces all data on this server with an export. Your accounts and sign-in here stay.
            </DialogDescription>
          </DialogHeader>
          {/* One element for the whole import: switching would start the form over when it ends. */}
          {following ? <ImportProgress /> : <ImportFlow needsSetupCode={false} onCancel={() => setImporting(false)} />}
        </DialogContent>
      </Dialog>
    </Card>
  );
}

function ExportDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const status = useTransferStatus();
  const [password, setPassword] = useState("");
  const [repeat, setRepeat] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [started, setStarted] = useState(false);
  const s = status.data;
  const running = s?.operation === "export" && s.state === "running";

  const close = () => {
    setPassword("");
    setRepeat("");
    setError(null);
    setStarted(false);
    onClose();
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (password !== repeat) return setError(new Error("The passwords do not match."));
    setBusy(true);
    setError(null);
    try {
      qc.setQueryData(transferStatusKey, await call(() => client.POST("/api/v1/transfer/export", { body: { password } })));
      setPassword("");
      setRepeat("");
      setStarted(true);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  let body;
  if (started && running) {
    body = (
      <div className="flex flex-col items-center gap-3 py-4 text-center">
        <Spinner className="size-6" />
        <p className="text-sm font-medium">{s?.step || "Exporting…"}</p>
        <p className="text-xs text-muted-foreground">You can close this dialog; the export continues.</p>
      </div>
    );
  } else if (started && s?.state === "failed") {
    body = <ErrorNote error={new Error(s.message || "The export failed.")} />;
  } else if (started && s?.export) {
    body = (
      <div className="flex flex-col gap-4">
        <p className="text-sm">
          The export is ready ({formatBytes(s.export.size)}). Keep it and its password safe: together they open your
          whole archive.
        </p>
        <Button asChild>
          <a href="/api/v1/transfer/export/download" download>
            <Download /> Download {s.export.name}
          </a>
        </Button>
        <p className="text-xs text-muted-foreground">
          On the new server: start DocNest, create your user (
          <code className="font-mono">docker compose exec app docnest createuser NAME</code>), sign in and set up your
          passkey, then import the file under Settings → System → Export &amp; import. Your new sign-in stays.
        </p>
      </div>
    );
  } else {
    body = (
      <form onSubmit={submit} className="flex flex-col gap-4">
        <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
          <li>Database: documents' metadata, OCR text, folders, tags, series, all settings</li>
          <li>Master key and the Proton Drive sign-in</li>
          <li>Files that are not in Proton Drive yet (new uploads, scans in progress)</li>
          <li>
            Not included: the documents in Proton Drive (they stay there), downloaded AI and Docling models, and the
            server's <code className="font-mono">.env</code> — the export lists its settings so the import can show what
            differs
          </li>
        </ul>
        <p className="text-sm">Processing pauses while the export runs.</p>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="export-password">Password for the export</Label>
          <Input
            id="export-password"
            type="password"
            autoComplete="new-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            At least 12 characters with three character classes, or a passphrase of 20+ characters. It cannot be
            recovered.
          </p>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="export-repeat">Repeat the password</Label>
          <Input
            id="export-repeat"
            type="password"
            autoComplete="new-password"
            required
            value={repeat}
            onChange={(e) => setRepeat(e.target.value)}
          />
        </div>
        <ErrorNote error={error} />
        <div className="flex justify-end gap-2">
          <Button type="button" variant="outline" onClick={close}>
            Cancel
          </Button>
          <Button type="submit" disabled={busy}>
            {busy && <Spinner className="text-primary-foreground" />}
            Create export
          </Button>
        </div>
      </form>
    );
  }

  return (
    <Dialog open={open} onOpenChange={(o) => !o && close()}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Export everything</DialogTitle>
          <DialogDescription>One password-protected ZIP to restore this instance on another server.</DialogDescription>
        </DialogHeader>
        {body}
      </DialogContent>
    </Dialog>
  );
}
