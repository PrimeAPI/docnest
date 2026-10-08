import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, Sparkles } from "lucide-react";
import { type FormEvent, useState } from "react";
import { toast } from "sonner";
import { call, client, type Schemas } from "@/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/input";
import { Spinner } from "@/components/ui/misc";
import { formatBytes } from "@/lib/utils";

type AiSettings = Schemas["AiSettingsOut"];
const aiKey = ["ai-settings"] as const;

/** Settings → System: which local AI model reads incoming documents, and downloading new ones. */
export function AiModelCard() {
  const qc = useQueryClient();
  const settings = useQuery({
    queryKey: aiKey,
    queryFn: () => call(() => client.GET("/api/v1/settings/ai")),
    refetchInterval: (query) => (query.state.data?.pull?.active ? 2_000 : false),
  });
  const choose = useMutation({
    mutationFn: (model: string) => call(() => client.PUT("/api/v1/settings/ai", { body: { model } })),
    onSuccess: (data) => {
      qc.setQueryData(aiKey, data);
      toast.success(data.model ? `New documents are now read by ${data.model}` : "AI analysis switched off");
    },
    onError: (e) => toast.error(e.message),
  });
  const pull = useMutation({
    mutationFn: (model: string) => call(() => client.POST("/api/v1/settings/ai/pull", { body: { model } })),
    onSuccess: (data) => {
      qc.setQueryData(aiKey, data);
      toast.success("Download started — this can take a while.");
    },
    onError: (e) => toast.error(e.message),
  });

  const s = settings.data;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Sparkles className="size-4" /> AI analysis
        </CardTitle>
        <CardDescription>
          A vision-language model running on your own server reads each document like a person would: the sender
          from the letterhead (never you, the recipient), a title, the date and the type. It runs after the document
          is already readable, so a slow model only delays the details, never the document.
        </CardDescription>
      </CardHeader>
      <CardContent className="max-w-xl">
        {!s ? (
          settings.error ? <p className="text-sm text-destructive">{String(settings.error)}</p> : <Spinner />
        ) : (
          <div className="flex flex-col gap-5">
            <Connection settings={s} />
            {s.reachable && (
              <ModelChoice settings={s} disabled={choose.isPending} onChoose={(m) => choose.mutate(m)} />
            )}
            {s.pull && <PullProgress pull={s.pull} />}
            {s.reachable && (
              <ModelDownload settings={s} busy={pull.isPending || !!s.pull?.active} onPull={(m) => pull.mutate(m)} />
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function Connection({ settings }: { settings: AiSettings }) {
  if (settings.reachable)
    return (
      <p className="text-xs text-muted-foreground">
        <Badge variant="success">Connected</Badge> Ollama at <span className="font-mono">{settings.url}</span>
      </p>
    );
  return (
    <div className="rounded-md border border-amber-300/60 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
      <p className="font-medium">{settings.configured ? "Ollama is not reachable" : "Not set up yet"}</p>
      <p className="mt-1 text-xs">{settings.error}</p>
      <p className="mt-1 text-xs">
        Start it with <span className="font-mono">docker compose --profile ai up -d</span> — see “AI analysis” in
        docs/operations.md.
      </p>
      {settings.model && (
        <p className="mt-1 text-xs">
          Documents wait for <span className="font-mono">{settings.model}</span> until Ollama is back, or until you
          switch AI analysis off.
        </p>
      )}
    </div>
  );
}

function ModelChoice({
  settings,
  disabled,
  onChoose,
}: {
  settings: AiSettings;
  disabled: boolean;
  onChoose: (model: string) => void;
}) {
  const chosen = settings.models.find((m) => m.name === settings.model);
  const missing = settings.model && !chosen;
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor="ai-model">Model</Label>
      <Select id="ai-model" value={settings.model} disabled={disabled} onChange={(e) => onChoose(e.target.value)}>
        <option value="">Off — rule-based detection only</option>
        {missing && <option value={settings.model}>{settings.model} (not installed)</option>}
        {settings.models.map((m) => (
          <option key={m.name} value={m.name}>
            {m.name} · {m.parameter_size || formatBytes(m.size)}
            {m.vision ? "" : " · text only"}
            {m.thinking ? " · reasoning (slow)" : ""}
          </option>
        ))}
      </Select>
      <p className="text-xs text-muted-foreground">
        Applies to new documents and to “Reanalyze”. Select documents and reprocess them to have existing ones
        read again; fields you set yourself are kept.
      </p>
      {chosen && !chosen.vision && (
        <p className="text-xs text-amber-700 dark:text-amber-300">
          This model cannot see the page and only gets the OCR text, including its recognition errors. A vision model
          is much better.
        </p>
      )}
      {chosen?.thinking && (
        <p className="text-xs text-amber-700 dark:text-amber-300">
          Reasoning models deliberate before answering, which can take many minutes per document on a CPU. Prefer an
          “-instruct” variant.
        </p>
      )}
      {missing && (
        <p className="text-xs text-amber-700 dark:text-amber-300">
          This model is not installed in Ollama: documents wait until it is downloaded.
        </p>
      )}
    </div>
  );
}

function ModelDownload({
  settings,
  busy,
  onPull,
}: {
  settings: AiSettings;
  busy: boolean;
  onPull: (model: string) => void;
}) {
  const [name, setName] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (name.trim()) onPull(name.trim());
  };
  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor="ai-pull">Download a model</Label>
      <ul className="flex flex-col gap-1.5 text-sm">
        {settings.suggestions.map((s) => (
          <li key={s.name} className="flex items-center justify-between gap-3">
            <div className="min-w-0">
              <span className="font-mono text-xs">{s.name}</span>
              <p className="text-xs text-muted-foreground">{s.description}</p>
            </div>
            {s.installed ? (
              <Badge variant="muted">Installed</Badge>
            ) : (
              <Button size="sm" variant="outline" disabled={busy} onClick={() => onPull(s.name)}>
                <Download /> Download
              </Button>
            )}
          </li>
        ))}
      </ul>
      <form onSubmit={submit} className="flex gap-2">
        <Input
          id="ai-pull"
          placeholder="Any model from ollama.com/library, e.g. mistral-small3.2"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <Button type="submit" variant="outline" disabled={busy || !name.trim()}>
          <Download /> Download
        </Button>
      </form>
    </div>
  );
}

function PullProgress({ pull }: { pull: NonNullable<AiSettings["pull"]> }) {
  const percent = pull.total > 0 ? Math.round((pull.completed / pull.total) * 100) : null;
  const failed = pull.status === "failed";
  const done = pull.status === "done";
  return (
    <div className="rounded-md border p-3 text-sm">
      <div className="flex items-center justify-between gap-2">
        <span>
          <span className="font-mono text-xs">{pull.model}</span>{" "}
          {failed ? "could not be downloaded" : done ? "is downloaded" : `— ${pull.status || "waiting"}`}
        </span>
        {pull.active && <Spinner className="size-4" />}
      </div>
      {pull.active && percent !== null && (
        <div className="mt-2 h-1.5 overflow-hidden rounded bg-muted">
          <div className="h-full bg-primary transition-all" style={{ width: `${percent}%` }} />
        </div>
      )}
      {pull.active && pull.total > 0 && (
        <p className="mt-1 text-xs text-muted-foreground">
          {formatBytes(pull.completed)} of {formatBytes(pull.total)}
        </p>
      )}
      {failed && <p className="mt-1 text-xs text-destructive">{pull.error}</p>}
      {done && <p className="mt-1 text-xs text-muted-foreground">Choose it above to use it.</p>}
    </div>
  );
}
