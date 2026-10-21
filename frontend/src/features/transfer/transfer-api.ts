import { useQuery } from "@tanstack/react-query";
import { type Schemas, call, client, csrfToken, ensureCsrf } from "@/api/client";

export type TransferStatus = Schemas["TransferStatusOut"];
export type ImportSummary = Schemas["ImportSummaryOut"];

export const transferStatusKey = ["transfer-status"] as const;

/** Export/import status. Polls quickly while one runs, and keeps polling while the server restarts. */
export function useTransferStatus({ poll = false }: { poll?: boolean } = {}) {
  return useQuery({
    queryKey: transferStatusKey,
    queryFn: () => call(() => client.GET("/api/v1/transfer/status")),
    retry: poll ? true : 1,
    retryDelay: 2000,
    refetchInterval: (query) => {
      const s = query.state.data;
      if (poll || query.state.status === "error") return 1500;
      return s?.running || s?.state === "running" ? 1500 : false;
    },
  });
}

/** Upload an export file as the raw request body, reporting progress (0–1). */
export async function uploadExport(
  file: File,
  { setupCode, onProgress }: { setupCode?: string; onProgress: (fraction: number) => void },
): Promise<void> {
  await ensureCsrf();
  await new Promise<void>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/v1/transfer/import/upload");
    xhr.withCredentials = true;
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("X-CSRFToken", csrfToken());
    if (setupCode) xhr.setRequestHeader("X-DocNest-Setup-Code", setupCode);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) return resolve();
      let detail = `Upload failed (${xhr.status})`;
      try {
        detail = JSON.parse(xhr.responseText).detail ?? detail;
      } catch {
        if (xhr.status === 413) detail = "The file is too large for the server or its reverse proxy (see docs/operations.md).";
      }
      reject(new Error(detail));
    };
    xhr.onerror = () => reject(new Error("The upload was interrupted — check the connection and try again."));
    xhr.send(file);
  });
}
