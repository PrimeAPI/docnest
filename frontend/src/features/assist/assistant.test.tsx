import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AssistantButton } from "./assistant";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock("@/api/client", () => ({
  call: (run: () => unknown) => run(),
  client: { GET: api.get, POST: api.post },
}));

let qc: QueryClient;

beforeEach(() => {
  api.get.mockReset();
  api.post.mockReset().mockResolvedValue({ id: 9, state: "pending" });
  api.get.mockImplementation(async (path, options) => {
    if (path === "/api/v1/documents") {
      const q = options.params.query;
      const total = q.folder ? (q.subfolders ? 801 : 751) : q.id.length;
      const ids: string[] = q.id ?? Array.from({ length: Math.min(100, total - ((q.page ?? 1) - 1) * 100) },
        (_, i) => `doc-${((q.page ?? 1) - 1) * 100 + i + 1}`);
      return { total, items: ids.map((id) => ({ id, title: id, correspondent: null, document_date: null })) };
    }
    if (path === "/api/v1/assist/models") return [{ name: "small-model", current: true }];
    if (path === "/api/v1/alterations/pages/status") return { documents: 0, ready: 0, queued: 0 };
    if (path === "/api/v1/assist/tasks/{task_id}") return {
      id: 9, state: "done", operation: "rename", groups: [], done: 0, total: 0, error: "", note: "",
    };
    throw new Error(`Unexpected request: ${path}`);
  });
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
});

afterEach(() => { cleanup(); qc.clear(); });

function show(props: { ids?: string[]; folderId?: number; subfolders?: boolean }) {
  const view = render(<QueryClientProvider client={qc}><MemoryRouter>
    <AssistantButton {...props} scope="this folder" />
  </MemoryRouter></QueryClientProvider>);
  fireEvent.click(view.getByRole("button", { name: "Assistant" }));
  return view;
}

it("reviews the whole folder, with exclusions across preview pages and no default deadline", async () => {
  const view = show({ folderId: 42 });
  fireEvent.click(await view.findByRole("button", { name: "751 documents" }));
  fireEvent.click(view.getByRole("checkbox", { name: "Include doc-1" }));
  fireEvent.click(view.getByRole("button", { name: "Next page" }));
  fireEvent.click(await view.findByRole("checkbox", { name: "Include doc-101" }));
  fireEvent.click(view.getByRole("radio", { name: /^Look through/ }));
  fireEvent.click(view.getByRole("button", { name: /Next.*749/ }));
  expect(await view.findByText(/749 documents included/)).toBeTruthy();
  expect(view.getByRole("checkbox", { name: /Write the report by/ }).getAttribute("aria-checked")).toBe("false");
  fireEvent.click(view.getByRole("radio", { name: "Now" }));
  fireEvent.click(view.getByRole("button", { name: "Start now" }));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/assist/reviews", {
    body: expect.objectContaining({ scope: "folder", folder_id: 42, ids: [],
      excluded_ids: ["doc-1", "doc-101"], subfolders: false, until: null }),
  }));
  expect(api.get.mock.calls.some(([path]) => path === "/api/v1/documents/ids")).toBe(false);
});

it("sends the entire folder scope for consistent names, including the chosen subfolder setting", async () => {
  const view = show({ folderId: 42 });
  await view.findByRole("button", { name: "751 documents" });
  fireEvent.click(view.getByRole("checkbox", { name: "Include subfolders" }));
  await view.findByRole("button", { name: "801 documents" });
  fireEvent.click(view.getByRole("button", { name: /Ask the AI model.*801/ }));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/assist/tasks", {
    body: { folder_id: 42, subfolders: true, excluded_ids: [], operation: "rename", instruction: "" },
  }));
});

it("keeps more than 100 explicit IDs while requesting only a preview-sized GET", async () => {
  const ids = Array.from({ length: 245 }, (_, i) => `selected-${i}`);
  const view = show({ ids });
  await view.findByRole("button", { name: "245 documents" });
  fireEvent.click(view.getByRole("button", { name: /Ask the AI model.*245/ }));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/assist/tasks", {
    body: { ids, subfolders: false, operation: "rename", instruction: "" },
  }));
  for (const [path, options] of api.get.mock.calls) {
    if (path === "/api/v1/documents") expect(options.params.query.id.length).toBeLessThanOrEqual(100);
  }
});
