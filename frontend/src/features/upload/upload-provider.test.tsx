import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { UploadProvider, useUpload } from "./upload-provider";

const api = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock("@/api/client", () => ({
  call: (run: () => unknown) => run(),
  client: { GET: api.get },
  ensureCsrf: () => Promise.resolve(),
}));

const folders = [{ id: 7, name: "Taxes", path: "Private / Taxes", parent_id: null, color: "slate" }];
const locations = [
  { id: 3, name: "Binder 3", parent_id: 1, path: "Cabinet / Binder 3", capacity: 500, sheets: 10 },
];

let fetchMock: ReturnType<typeof vi.fn>;
let qc: QueryClient;

beforeEach(() => {
  localStorage.clear();
  api.get.mockReset().mockImplementation((path: string) => {
    if (path === "/api/v1/paper/locations") return Promise.resolve(locations);
    if (path === "/api/v1/folders") return Promise.resolve(folders);
    return Promise.resolve([]);
  });
  fetchMock = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ id: "doc-1", duplicate: false }) });
  vi.stubGlobal("fetch", fetchMock);
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});
afterEach(() => {
  cleanup();
  qc.clear();
  vi.unstubAllGlobals();
});

/** Mounts the dialog plus a button that uploads one PDF with the chosen options. */
function Harness() {
  const { openDialog, uploadFiles } = useUpload();
  return (
    <>
      <button type="button" onClick={openDialog}>
        Open upload
      </button>
      <button
        type="button"
        data-testid="send"
        onClick={() => uploadFiles([new File(["%PDF-1.7"], "scan.pdf", { type: "application/pdf" })])}
      >
        Send
      </button>
    </>
  );
}

/** Opens the dialog and waits for the paper locations to arrive in the select. */
async function show() {
  const view = render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <UploadProvider>
          <Harness />
        </UploadProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
  fireEvent.click(view.getByRole("button", { name: "Open upload" }));
  await view.findByRole("option", { name: /Cabinet \/ Binder 3/ });
  return view;
}

function sentForm(): FormData {
  expect(fetchMock).toHaveBeenCalledWith("/api/v1/documents/upload", expect.anything());
  return fetchMock.mock.calls[0][1].body as FormData;
}

it("files the paper original in the chosen location", async () => {
  const view = await show();
  const select = view.getByLabelText("Paper original") as HTMLSelectElement;
  expect(select.value).toBe(""); // no paper original by default
  fireEvent.change(select, { target: { value: "3" } });

  fireEvent.click(view.getByTestId("send"));
  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  expect(sentForm().get("paper_location_id")).toBe("3");
});

it("sends no location when the document has no paper original", async () => {
  const view = await show();
  fireEvent.click(view.getByTestId("send"));
  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  expect(sentForm().has("paper_location_id")).toBe(false);
});

it("remembers the location for drag & drop uploads", async () => {
  const first = await show();
  fireEvent.change(first.getByLabelText("Paper original"), { target: { value: "3" } });
  cleanup();

  const again = await show();
  const select = again.getByLabelText("Paper original") as HTMLSelectElement;
  expect(select.value).toBe("3");
});

it("forgets a location that no longer exists", async () => {
  localStorage.setItem("docnest-upload-options", JSON.stringify({ paperLocationId: 999 }));
  const view = await show();
  const select = view.getByLabelText("Paper original") as HTMLSelectElement;
  expect(select.value).toBe("");
  fireEvent.click(view.getByTestId("send"));
  await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  expect(sentForm().has("paper_location_id")).toBe(false);
});
