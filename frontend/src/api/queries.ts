import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { type Schemas, call, client } from "./client";

export type DocumentListItem = Schemas["DocumentPage"]["items"][number];
export type DocumentDetail = Schemas["DocumentDetail"];
export type DocumentPatch = Partial<Schemas["DocumentPatch"]>;
export type TagOut = Schemas["TagOut"];
export type FolderOut = Schemas["FolderOut"];
export type SessionState = Schemas["SessionStateOut"];

export const keys = {
  session: ["session"] as const,
  overview: ["overview"] as const,
  system: ["system"] as const,
  processingQueue: ["processing-queue"] as const,
  documents: (params: object) => ["documents", params] as const,
  document: (id: string) => ["document", id] as const,
  tags: ["tags"] as const,
  folders: ["folders"] as const,
  types: ["types"] as const,
  correspondents: ["correspondents"] as const,
  series: ["series"] as const,
  seriesDetail: (id: number) => ["series", id] as const,
  rules: ["rules"] as const,
  scanners: ["scanners"] as const,
  devices: ["devices"] as const,
  sessions: ["sessions"] as const,
  audit: ["audit"] as const,
};

export function useSession() {
  return useQuery({
    queryKey: keys.session,
    queryFn: () => call(() => client.GET("/api/v1/auth/session")),
    staleTime: 30_000,
  });
}

export function useOverview() {
  return useQuery({
    queryKey: keys.overview,
    queryFn: () => call(() => client.GET("/api/v1/overview")),
    refetchInterval: 15_000,
  });
}

export function useSystem() {
  return useQuery({
    queryKey: keys.system,
    queryFn: () => call(() => client.GET("/api/v1/system")),
    refetchInterval: 30_000,
  });
}

export function useProcessingQueue() {
  return useQuery({
    queryKey: keys.processingQueue,
    queryFn: () => call(() => client.GET("/api/v1/processing/queue")),
    refetchInterval: (query) => {
      const data = query.state.data;
      return data && (data.running.length > 0 || data.queued.length > 0) ? 2_000 : 15_000;
    },
  });
}

export type DocumentQuery = {
  q?: string;
  folder?: number[];
  subfolders?: boolean;
  unfiled?: boolean;
  document_type?: number[];
  correspondent?: number[];
  tag?: number[];
  series?: number;
  status?: ("new" | "todo" | "done")[];
  important?: boolean;
  unread?: boolean;
  processing?: ("pending" | "running" | "done" | "failed")[];
  uploaded_from?: string;
  uploaded_to?: string;
  date_from?: string;
  date_to?: string;
  paper_location?: number;
  paper_pending?: boolean;
  sort?: "relevance" | "uploaded" | "-uploaded" | "date" | "-date";
  page?: number;
  page_size?: number;
};

export function useDocuments(params: DocumentQuery, options?: { refetchInterval?: number }) {
  return useQuery({
    queryKey: keys.documents(params),
    queryFn: () => call(() => client.GET("/api/v1/documents", { params: { query: params } })),
    placeholderData: keepPreviousData,
    refetchInterval: options?.refetchInterval,
  });
}

export function useDocument(id: string) {
  return useQuery({
    queryKey: keys.document(id),
    queryFn: () => call(() => client.GET("/api/v1/documents/{doc_id}", { params: { path: { doc_id: id } } })),
    refetchInterval: (q) => {
      const state = q.state.data?.processing_state;
      return state === "pending" || state === "running" ? 3000 : false;
    },
  });
}

export function useInvalidateDocuments() {
  const qc = useQueryClient();
  return () => {
    qc.invalidateQueries({ queryKey: ["documents"] });
    qc.invalidateQueries({ queryKey: keys.overview });
    qc.invalidateQueries({ queryKey: keys.processingQueue });
    qc.invalidateQueries({ queryKey: ["series"] });
    qc.invalidateQueries({ queryKey: keys.tags });
    qc.invalidateQueries({ queryKey: keys.folders });
  };
}

export function useUpdateDocument(id: string) {
  const qc = useQueryClient();
  const invalidate = useInvalidateDocuments();
  return useMutation({
    mutationFn: (patch: DocumentPatch) =>
      call(() =>
        client.PATCH("/api/v1/documents/{doc_id}", {
          params: { path: { doc_id: id } },
          body: patch as Schemas["DocumentPatch"],
        }),
      ),
    // Optimistic update for simple flags so the UI reacts instantly.
    onMutate: async (patch) => {
      await qc.cancelQueries({ queryKey: keys.document(id) });
      const previous = qc.getQueryData<DocumentDetail>(keys.document(id));
      if (previous) {
        qc.setQueryData<DocumentDetail>(keys.document(id), {
          ...previous,
          ...(patch.status ? { status: patch.status } : {}),
          ...(patch.is_important !== undefined && patch.is_important !== null
            ? { is_important: patch.is_important }
            : {}),
          ...(patch.title ? { title: patch.title } : {}),
        });
      }
      return { previous };
    },
    onError: (e, _patch, context) => {
      if (context?.previous) qc.setQueryData(keys.document(id), context.previous);
      toast.error(e.message);
    },
    // Several edits can be in flight; always end with the server's latest state.
    onSettled: () => {
      if (qc.isMutating({ mutationKey: ["update-document", id] }) <= 1) {
        qc.invalidateQueries({ queryKey: keys.document(id) });
      }
      invalidate();
    },
    mutationKey: ["update-document", id],
  });
}

export function useBulkAction() {
  const invalidate = useInvalidateDocuments();
  return useMutation({
    mutationFn: (body: Schemas["BulkAction"]) => call(() => client.POST("/api/v1/documents/bulk", { body })),
    onSuccess: () => invalidate(),
    onError: (e) => toast.error(e.message),
  });
}

export function useTags() {
  return useQuery({ queryKey: keys.tags, queryFn: () => call(() => client.GET("/api/v1/tags")) });
}
export function useFolders() {
  return useQuery({ queryKey: keys.folders, queryFn: () => call(() => client.GET("/api/v1/folders")) });
}
export function useTypes() {
  return useQuery({ queryKey: keys.types, queryFn: () => call(() => client.GET("/api/v1/document-types")) });
}
export function useCorrespondents() {
  return useQuery({
    queryKey: keys.correspondents,
    queryFn: () => call(() => client.GET("/api/v1/correspondents")),
  });
}
export function useSeriesList() {
  return useQuery({ queryKey: keys.series, queryFn: () => call(() => client.GET("/api/v1/series")) });
}
