import { useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, FileArchive, Upload } from "lucide-react";
import { type FormEvent, useState } from "react";
import { call, client } from "@/api/client";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input, Label } from "@/components/ui/input";
import { ErrorNote, Spinner } from "@/components/ui/misc";
import { formatBytes, formatDateTime } from "@/lib/utils";
import { type ImportSummary, transferStatusKey, uploadExport, useTransferStatus } from "./transfer-api";

type Step = "choose" | "uploading" | "review" | "running";

/**
 * Restore an export onto this server: upload, check the password, review, replace.
 * On a new instance (no users) the setup code is asked for as well.
 */
export function ImportFlow({ needsSetupCode, onCancel }: { needsSetupCode: boolean; onCancel?: () => void }) {
  const qc = useQueryClient();
  const [step, setStep] = useState<Step>("choose");
  const [file, setFile] = useState<File | null>(null);
  const [password, setPassword] = useState("");
  const [setupCode, setSetupCode] = useState("");
  const [progress, setProgress] = useState(0);
  const [summary, setSummary] = useState<ImportSummary | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const code = needsSetupCode ? setupCode.trim() : undefined;

  const check = async (e: FormEvent) => {
    e.preventDefault();
    if (!file) return;
    setError(null);
    setStep("uploading");
    setProgress(0);
    try {
      await uploadExport(file, { setupCode: code, onProgress: setProgress });
      const result = await call(() =>
        client.POST("/api/v1/transfer/import/inspect", { body: { password, setup_code: code } }),
      );
      setSummary(result);
      setConfirmed(false);
      setStep("review");
    } catch (err) {
      setError(err);
      setStep("choose");
    }
  };

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const status = await call(() =>
        client.POST("/api/v1/transfer/import/start", { body: { password, setup_code: code } }),
      );
      qc.setQueryData(transferStatusKey, status);
      setPassword("");
      setStep("running");
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  if (step === "running") return <ImportProgress />;

  if (step === "review" && summary) {
    return (
      <div className="flex flex-col gap-4">
        <div className="rounded-md border p-3 text-sm">
          <div className="flex items-center gap-2 font-medium">
            <FileArchive className="size-4" /> Export of {summary.base_url || "a DocNest instance"}
          </div>
          <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-muted-foreground">
            <dt>Created</dt>
            <dd>{summary.created_at ? formatDateTime(summary.created_at) : "unknown"}</dd>
            <dt>DocNest</dt>
            <dd>{summary.docnest_version}</dd>
            <dt>Contents</dt>
            <dd>
              {summary.documents} document{summary.documents === 1 ? "" : "s"}, {summary.users} user
              {summary.users === 1 ? "" : "s"} · {formatBytes(summary.size)}
            </dd>
          </dl>
        </div>
        {summary.warnings.length > 0 && <Warnings items={summary.warnings} />}
        <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm">
          {summary.keeps_accounts ? (
            <>
              All data on this server — documents, folders, settings, the Proton Drive sign-in — is replaced by the
              export. <strong>Your accounts here stay as they are</strong>, with password, passkeys and authenticator
              app; the export's accounts are not taken over.
            </>
          ) : (
            <>
              Everything on this server is replaced by the export, including its accounts: sign in with the account of
              the old server afterwards.
            </>
          )}{" "}
          <strong>Shut the old server down</strong> once this one runs: two servers with the same Proton Drive sign-in
          and mailbox get in each other's way.
        </div>
        <label className="flex items-start gap-2 text-sm">
          <Checkbox checked={confirmed} onCheckedChange={(v) => setConfirmed(v === true)} className="mt-0.5" />
          Replace everything on this server with the export
        </label>
        <ErrorNote error={error} />
        <div className="flex justify-end gap-2">
          <Button variant="outline" onClick={() => setStep("choose")} disabled={busy}>
            Back
          </Button>
          <Button variant="destructive" disabled={!confirmed || busy} onClick={start}>
            {busy && <Spinner className="text-white" />}
            Import and replace
          </Button>
        </div>
      </div>
    );
  }

  return (
    <form onSubmit={check} className="flex flex-col gap-4">
      {needsSetupCode && (
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="setup-code">Setup code</Label>
          <Input
            id="setup-code"
            required
            autoComplete="off"
            placeholder="XXXX-XXXX-XXXX-XXXX"
            value={setupCode}
            onChange={(e) => setSetupCode(e.target.value)}
          />
          <p className="text-xs text-muted-foreground">
            Shown on the server by <code className="font-mono">docker compose exec app docnest import-code</code>
          </p>
        </div>
      )}
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="import-file">Export file</Label>
        <Input
          id="import-file"
          type="file"
          accept=".zip,application/zip"
          required
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="import-password">Password of the export</Label>
        <Input
          id="import-password"
          type="password"
          autoComplete="off"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </div>
      {step === "uploading" && (
        <div className="flex flex-col gap-1.5 text-sm text-muted-foreground">
          <div className="h-2 overflow-hidden rounded-full bg-muted">
            <div className="h-full bg-primary transition-[width]" style={{ width: `${Math.round(progress * 100)}%` }} />
          </div>
          {progress < 1 ? `Uploading… ${Math.round(progress * 100)}%` : "Checking the export…"}
        </div>
      )}
      <ErrorNote error={error} />
      <div className="flex justify-end gap-2">
        {onCancel && (
          <Button type="button" variant="outline" onClick={onCancel} disabled={step === "uploading"}>
            Cancel
          </Button>
        )}
        <Button type="submit" disabled={!file || step === "uploading"}>
          {step === "uploading" ? <Spinner className="text-primary-foreground" /> : <Upload />}
          Upload and check
        </Button>
      </div>
    </form>
  );
}

/** Follows an import until DocNest has restarted with the imported data. */
export function ImportProgress() {
  const status = useTransferStatus({ poll: true });
  const s = status.data;
  const failed = s?.operation === "import" && s.state === "failed";
  const done = s?.operation === "import" && s.state === "done" && !s.running;

  if (failed) {
    return (
      <div className="flex flex-col gap-3">
        <ErrorNote error={new Error(s.message || "The import failed.")} />
        <Button variant="outline" onClick={() => window.location.reload()}>
          Back
        </Button>
      </div>
    );
  }
  if (done && !status.isError) {
    return (
      <div className="flex flex-col gap-4">
        <div className="flex items-center gap-2 font-medium">
          <CheckCircle2 className="size-5 text-success" /> Import complete
        </div>
        {s.warnings.length > 0 && <Warnings items={s.warnings} />}
        {s.accounts_kept ? (
          <Button onClick={() => window.location.assign("/inbox")}>Open DocNest</Button>
        ) : (
          <>
            <p className="text-sm text-muted-foreground">
              Sign in with the account of the old server. Passkeys only work if the address is the same; otherwise use
              your authenticator app or a recovery code.
            </p>
            <Button onClick={() => window.location.assign("/login")}>Go to sign-in</Button>
          </>
        )}
      </div>
    );
  }
  return (
    <div className="flex flex-col items-center gap-3 py-4 text-center">
      <Spinner className="size-6" />
      <p className="text-sm font-medium">{status.isError ? "DocNest is restarting…" : s?.step || "Importing…"}</p>
      <p className="text-xs text-muted-foreground">Keep this page open. Large exports take a while.</p>
    </div>
  );
}

function Warnings({ items }: { items: string[] }) {
  return (
    <ul className="flex flex-col gap-2 rounded-md border border-warning/40 bg-warning/10 p-3 text-sm">
      {items.map((w) => (
        <li key={w} className="flex gap-2">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" />
          <span className="break-words">{w}</span>
        </li>
      ))}
    </ul>
  );
}
