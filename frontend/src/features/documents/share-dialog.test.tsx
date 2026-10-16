import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ShareDialog } from "./share-dialog";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), delete: vi.fn(), copy: vi.fn() }));
vi.mock("@/api/client", () => ({
  call: (run: () => unknown) => run(),
  client: { GET: api.get, POST: api.post, DELETE: api.delete },
}));
const share = {
  id: "random-share", protected: true, allow_original: true, allow_text: true,
  created_at: "2026-10-12T10:00:00Z", url: "https://example.com/share/random-share",
  pdf_url: "https://example.com/api/v1/shares/random-share/file",
};
let qc: QueryClient;
beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
  api.get.mockReset().mockResolvedValue([]);
  api.post.mockReset().mockResolvedValue(share);
  api.delete.mockReset().mockResolvedValue({ ok: true });
  api.copy.mockReset().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: api.copy } });
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});
afterEach(() => { cleanup(); qc.clear(); vi.unstubAllGlobals(); });
function show() {
  const view = render(<QueryClientProvider client={qc}><ShareDialog id="document-id" /></QueryClientProvider>);
  fireEvent.click(view.getByRole("button", { name: /^Share$/ }));
  return view;
}

it("creates a passphrase link, copies the raw PDF URL and revokes the link", async () => {
  const view = show();
  await view.findByText("No share links yet.");
  fireEvent.change(view.getByLabelText("Access"), { target: { value: "passphrase" } });
  expect((view.getByRole("button", { name: "Create share link" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(view.getByLabelText("Passphrase"), { target: { value: "a private phrase" } });
  fireEvent.click(view.getByLabelText("Allow viewing and downloading the original"));
  api.get.mockResolvedValue([share]);
  fireEvent.click(view.getByRole("button", { name: "Create share link" }));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/documents/{doc_id}/shares", {
    params: { path: { doc_id: "document-id" } },
    body: { passphrase: "a private phrase", allow_original: true, allow_text: true },
  }));
  const pdf = await view.findByLabelText("Direct enhanced-PDF link") as HTMLInputElement;
  expect(pdf.value).toBe(share.pdf_url);
  fireEvent.click(view.getByTitle("Copy PDF link"));
  await waitFor(() => expect(api.copy).toHaveBeenCalledWith(share.pdf_url));
  expect((view.getByLabelText("Passphrase") as HTMLInputElement).value).toBe("");
  api.get.mockResolvedValue([]);
  fireEvent.click(view.getByRole("button", { name: "Revoke" }));
  await view.findByText("No share links yet.");
  expect(api.delete).toHaveBeenCalledWith("/api/v1/documents/{doc_id}/shares/{share_id}", {
    params: { path: { doc_id: "document-id", share_id: share.id } },
  });
});

it("can create a link-only share with neither the original nor OCR text", async () => {
  const view = show();
  await view.findByText("No share links yet.");
  fireEvent.click(view.getByLabelText("Include OCR text"));
  fireEvent.click(view.getByRole("button", { name: "Create share link" }));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/documents/{doc_id}/shares", {
    params: { path: { doc_id: "document-id" } },
    body: { passphrase: "", allow_original: false, allow_text: false },
  }));
});
