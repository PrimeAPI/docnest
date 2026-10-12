import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { AiModelCard } from "./ai-model";

const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn() }));
vi.mock("@/api/client", () => ({
  call: (run: () => unknown) => run(),
  client: { GET: api.get, PUT: api.put },
}));

const initial = {
  configured: false, url: "", api_key_configured: false, pages: 1, max_pages: 500,
  reachable: false, error: "No Ollama server is configured", model: "previous-model",
  models: [], suggestions: [], pull: null,
};
let qc: QueryClient;

beforeEach(() => {
  api.get.mockReset().mockResolvedValue(initial);
  api.put.mockReset().mockImplementation(async (_path, { body }) => ({ ...initial, ...body }));
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
});
afterEach(() => { cleanup(); qc.clear(); });

function show() {
  return render(<QueryClientProvider client={qc}><AiModelCard /></QueryClientProvider>);
}

it("lets an unconfigured server be connected and limits document pages without changing the model", async () => {
  const view = show();
  const url = await view.findByLabelText("Ollama server URL");
  fireEvent.change(url, { target: { value: "http://192.168.1.50:11434" } });
  fireEvent.change(view.getByLabelText("Maximum pages per document"), { target: { value: "3" } });
  fireEvent.click(view.getByRole("button", { name: "Save AI settings" }));
  await waitFor(() => expect(api.put).toHaveBeenCalledWith("/api/v1/settings/ai", {
    body: { url: "http://192.168.1.50:11434", pages: 3 },
  }));
  await waitFor(() => expect((view.getByLabelText("Maximum pages per document") as HTMLInputElement).value).toBe("3"));
  expect((view.getByLabelText("Model") as HTMLSelectElement).value).toBe("previous-model");
});

it("keeps AI switch-off available when Ollama is unreachable and rejects an invalid page limit", async () => {
  const view = show();
  const pages = await view.findByLabelText("Maximum pages per document");
  fireEvent.change(pages, { target: { value: "0" } });
  expect((view.getByRole("button", { name: "Save AI settings" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(view.getByLabelText("Model"), { target: { value: "" } });
  await waitFor(() => expect(api.put).toHaveBeenCalledWith("/api/v1/settings/ai", { body: { model: "" } }));
});

it("saves a replacement key and empties the password field after saving", async () => {
  api.get.mockResolvedValue({ ...initial, url: "https://remote-server", api_key_configured: true });
  api.put.mockImplementation(async (_path, { body }) => ({
    ...initial, url: "https://remote-server", pages: body.pages, api_key_configured: true,
  }));
  const view = show();
  const key = await view.findByLabelText("Ollama API key (optional)");
  expect((key as HTMLInputElement).type).toBe("password");
  expect((key as HTMLInputElement).value).toBe("");
  fireEvent.change(key, { target: { value: "replacement-secret" } });
  fireEvent.click(view.getByRole("button", { name: "Save AI settings" }));
  await waitFor(() => expect(api.put).toHaveBeenCalledWith("/api/v1/settings/ai", {
    body: { url: "https://remote-server", pages: 1, api_key: "replacement-secret" },
  }));
  await waitFor(() => expect((view.getByLabelText("Ollama API key (optional)") as HTMLInputElement).value).toBe(""));
});

it("keeps a saved key when editing pages and supports explicit removal", async () => {
  api.get.mockResolvedValue({ ...initial, api_key_configured: true });
  api.put.mockImplementation(async (_path, { body }) => ({
    ...initial, pages: body.pages, api_key_configured: body.api_key !== "",
  }));
  const view = show();
  fireEvent.change(await view.findByLabelText("Maximum pages per document"), { target: { value: "2" } });
  fireEvent.click(view.getByRole("button", { name: "Save AI settings" }));
  await waitFor(() => expect(api.put).toHaveBeenCalledWith("/api/v1/settings/ai", {
    body: { url: "", pages: 2 },
  }));
  fireEvent.click(await view.findByLabelText("Remove API key"));
  fireEvent.click(view.getByRole("button", { name: "Save AI settings" }));
  await waitFor(() => expect(api.put).toHaveBeenCalledWith("/api/v1/settings/ai", {
    body: { url: "", pages: 2, api_key: "" },
  }));
  await waitFor(() => expect(view.queryByLabelText("Remove API key")).toBeNull());
});
