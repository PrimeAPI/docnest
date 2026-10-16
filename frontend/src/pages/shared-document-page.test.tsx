import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ApiError } from "@/api/client";
import { SharedDocumentPage } from "./shared-document-page";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), csrf: vi.fn() }));
vi.mock("@/api/client", async (original) => ({
  ...await original<typeof import("@/api/client")>(),
  call: (run: () => unknown) => run(), ensureCsrf: api.csrf,
  client: { GET: api.get, POST: api.post },
}));
vi.mock("@/features/documents/pdf-viewer", () => ({
  PdfViewer: ({ url }: { url: string }) => <div data-testid="pdf">{url}</div>,
}));
const shared = { title: "Shared letter", document_date: null, page_count: 2, allow_original: false, allow_text: true };
let qc: QueryClient;
beforeEach(() => {
  api.get.mockReset().mockResolvedValue(shared);
  api.post.mockReset();
  api.csrf.mockReset().mockResolvedValue(undefined);
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});
afterEach(() => { cleanup(); qc.clear(); });
function show() {
  return render(<QueryClientProvider client={qc}>
    <MemoryRouter initialEntries={["/share/random-share"]}><Routes>
      <Route path="/share/:shareId" element={<SharedDocumentPage />} />
    </Routes></MemoryRouter>
  </QueryClientProvider>);
}

it("opens without login, shows OCR text and omits access to an unshared original", async () => {
  api.get.mockImplementation(async (path) => path.endsWith("/text") ? { text: "Recognized words" } : shared);
  const view = show();
  await view.findByRole("heading", { name: "Shared letter" });
  expect(view.queryByRole("button", { name: /^Original$/ })).toBeNull();
  expect(view.getByRole("link", { name: /Download enhanced PDF/ }).getAttribute("href"))
    .toBe("/api/v1/shares/random-share/file?variant=archive&download=true");
  fireEvent.click(view.getByRole("button", { name: "OCR text" }));
  await view.findByText("Recognized words");
  expect(api.get.mock.calls.every(([path]) => path.startsWith("/api/v1/shares/"))).toBe(true);
});

it("requires the passphrase before showing content and then permits the original", async () => {
  let unlocked = false;
  api.get.mockImplementation(async () => {
    if (!unlocked) throw new ApiError(403, "passphrase_required");
    return { ...shared, allow_original: true, allow_text: false };
  });
  api.post.mockImplementation(async (_path, { body }) => {
    if (body.passphrase !== "private") throw new ApiError(403, "Incorrect passphrase");
    unlocked = true;
    return { ok: true };
  });
  const view = show();
  const input = await view.findByLabelText("Passphrase");
  expect(view.queryByTestId("pdf")).toBeNull();
  fireEvent.change(input, { target: { value: "wrong" } });
  fireEvent.click(view.getByRole("button", { name: "Open document" }));
  await view.findByText("Incorrect passphrase");
  fireEvent.change(input, { target: { value: "private" } });
  fireEvent.click(view.getByRole("button", { name: "Open document" }));
  await view.findByRole("heading", { name: "Shared letter" });
  expect(api.csrf).toHaveBeenCalled();
  expect(view.queryByRole("button", { name: "OCR text" })).toBeNull();
  fireEvent.click(view.getByRole("button", { name: /^Original$/ }));
  await waitFor(() => expect(view.getByTestId("pdf").textContent).toBe("/api/v1/shares/random-share/file?variant=original"));
  expect(view.getByRole("link", { name: /Download original/ }).getAttribute("href"))
    .toBe("/api/v1/shares/random-share/file?variant=original&download=true");
});

it("shows a revoked link as unavailable without sending the visitor to login", async () => {
  api.get.mockRejectedValue(new ApiError(404, "This share is unavailable or has been revoked"));
  const view = show();
  await view.findByRole("heading", { name: "Shared document unavailable" });
  expect(view.queryByTestId("pdf")).toBeNull();
  expect(view.queryByLabelText("Passphrase")).toBeNull();
});
