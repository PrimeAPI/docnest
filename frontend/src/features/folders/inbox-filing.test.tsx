import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { InboxFilingSection } from "./inbox-filing";

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn() }));
vi.mock("@/api/client", () => ({
  call: (run: () => unknown) => run(),
  client: { GET: api.get, POST: api.post, PUT: api.put },
}));
const doc = (id: string, title: string) => ({
  id, title, document_date: "2026-01-05", correspondent: "HUK-Coburg", document_type: "Invoice", folder_path: "",
});
const overview = {
  enabled: true,
  planning: false,
  waiting: 0,
  groups: [
    {
      anchor_id: 7, anchor_path: "Privat / Versicherungen", new: ["Kfz"], named_by: "ai",
      reasons: ["sender HUK-Coburg — chosen by the AI model"],
      documents: [doc("a", "Versicherungsschein Kfz"), doc("b", "Beitragsrechnung Kfz 2026")],
    },
  ],
};
let qc: QueryClient;
beforeEach(() => {
  api.get.mockReset().mockImplementation((path: string) => Promise.resolve(path === "/api/v1/filing/inbox" ? overview : []));
  api.post.mockReset().mockResolvedValue({ moved: 2, created: 1, dismissed: 0 });
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});
afterEach(() => { cleanup(); qc.clear(); });
function show() {
  return render(
    <QueryClientProvider client={qc}><MemoryRouter><InboxFilingSection /></MemoryRouter></QueryClientProvider>,
  );
}

it("shows a group by its target and approves it with a renamed new folder", async () => {
  const view = show();
  await view.findByText("Versicherungsschein Kfz");
  expect(view.getByText("Versicherungen")).toBeTruthy();
  fireEvent.change(view.getByLabelText("New folder name"), { target: { value: "Auto" } });
  fireEvent.click(view.getByRole("button", { name: /Approve \(2\)/ }));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/filing/inbox/decide", {
    body: { ids: ["a", "b"], action: "accept", target: { anchor_id: 7, new: ["Auto"] } },
  }));
});

it("dismisses a whole group, leaving its documents where they are", async () => {
  const view = show();
  await view.findByText("Versicherungsschein Kfz");
  fireEvent.click(view.getByRole("button", { name: /Dismiss/ }));
  await waitFor(() => expect(api.post).toHaveBeenCalledWith("/api/v1/filing/inbox/decide", {
    body: { ids: ["a", "b"], action: "dismiss" },
  }));
});
