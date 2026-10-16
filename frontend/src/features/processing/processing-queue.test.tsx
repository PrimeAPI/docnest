import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { type Schemas } from "@/api/client";
import { ProcessingQueueButton } from "./processing-queue";

const mocks = vi.hoisted(() => ({ post: vi.fn(), queue: vi.fn(), invalidate: vi.fn() }));
vi.mock("@/api/client", () => ({
  call: (run: () => unknown) => run(),
  client: { POST: mocks.post },
}));
vi.mock("@/api/queries", () => ({
  keys: { processingQueue: ["processing-queue"] },
  useProcessingQueue: mocks.queue,
  useInvalidateDocuments: () => mocks.invalidate,
}));

function item(overrides: Partial<Schemas["ProcessingQueueItem"]> = {}): Schemas["ProcessingQueueItem"] {
  return {
    id: 1, document_id: "doc-1", title: "Loyalty card", state: "running", phase: "processing",
    stage: "ocr", step: 2, steps: 5, outcome: "", backend: "docling", attempts: 1, error: "",
    created_at: "2026-10-13T00:00:00Z", run_after: "2026-10-13T00:00:00Z", started_at: "2026-10-13T00:00:00Z",
    finished_at: null, duration_seconds: 23, cancellable: true, cancel_requested: false, ...overrides,
  };
}

let qc: QueryClient;
beforeEach(() => {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  mocks.post.mockReset().mockResolvedValue({ cancelled: 1 });
  mocks.invalidate.mockReset();
});
afterEach(() => { cleanup(); qc.clear(); });

function show(running: Schemas["ProcessingQueueItem"][], queued: Schemas["ProcessingQueueItem"][] = []) {
  mocks.queue.mockReturnValue({
    data: { running, queued, queued_total: queued.length, history: [], concurrency: 1 },
    dataUpdatedAt: Date.now(),
  });
  const view = render(
    <QueryClientProvider client={qc}><MemoryRouter><ProcessingQueueButton /></MemoryRouter></QueryClientProvider>,
  );
  fireEvent.click(view.getByRole("button", { name: /Processing queue:/ }));
  return view;
}

it("cancels active recognition from the popout and refreshes document and queue state", async () => {
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  const view = show([item()]);
  expect(view.getByText("2/5 · Text recognition")).toBeTruthy();
  fireEvent.click(view.getByRole("button", { name: "Cancel processing of Loyalty card" }));
  await waitFor(() => expect(mocks.post).toHaveBeenCalledWith("/api/v1/processing/queue/cancel", {
    body: { job_ids: [1] },
  }));
  await waitFor(() => expect(mocks.invalidate).toHaveBeenCalled());
  expect(invalidate).toHaveBeenCalledWith({ queryKey: ["processing-queue"] });
});

it("protects both running and queued intake", () => {
  const view = show(
    [item({ phase: "intake", stage: "validate", cancellable: false })],
    [item({ id: 2, state: "queued", phase: "intake", stage: "assemble", cancellable: false })],
  );
  expect(view.queryByRole("button", { name: /Cancel processing of/ })).toBeNull();
  expect(view.queryByRole("button", { name: "Cancel all" })).toBeNull();
  expect(view.getByText(/Intake must finish first/)).toBeTruthy();
});

it("shows stopping while the supervisor is terminating processing", () => {
  const view = show([item({ cancel_requested: true, cancellable: false })]);
  expect(view.getByText("Stopping processing…")).toBeTruthy();
  expect(view.queryByRole("button", { name: /Cancel processing of/ })).toBeNull();
});

it("can cancel all post-intake processing with explicit confirmation", async () => {
  const view = show([item()]);
  fireEvent.click(view.getByRole("button", { name: "Cancel all" }));
  expect(mocks.post).not.toHaveBeenCalled();
  fireEvent.click(view.getByRole("button", { name: "Cancel all processing?" }));
  await waitFor(() => expect(mocks.post).toHaveBeenCalledWith("/api/v1/processing/queue/cancel", {
    body: { job_ids: null },
  }));
});

it("carries the measured time forward between polls instead of standing still", () => {
  vi.useFakeTimers();
  try {
    // The server measured 4h 32m 2s when it answered; the queue is polled only every few seconds.
    const view = show([item({ duration_seconds: 16_322 })]);
    expect(view.getByText("4h 32m 2s")).toBeTruthy();
    act(() => void vi.advanceTimersByTime(3_000));
    expect(view.getByText("4h 32m 5s")).toBeTruthy();
  } finally {
    vi.useRealTimers();
  }
});

it("shows what a finished job took, and says when a job is on another attempt", () => {
  const view = show([item({ attempts: 3 })], []);
  expect(view.getByText("Attempt 3")).toBeTruthy();
});

it("measures a finished job from its own start and end", () => {
  mocks.queue.mockReturnValue({
    dataUpdatedAt: Date.now(),
    data: {
      running: [],
      queued: [],
      queued_total: 0,
      concurrency: 1,
      history: [
        item({
          state: "done",
          outcome: "done",
          cancellable: false,
          started_at: "2026-10-13T00:00:00Z",
          finished_at: "2026-10-13T00:02:05Z",
          duration_seconds: 125,
        }),
      ],
    },
  });
  const view = render(
    <QueryClientProvider client={qc}><MemoryRouter><ProcessingQueueButton /></MemoryRouter></QueryClientProvider>,
  );
  fireEvent.click(view.getByRole("button", { name: /Processing queue:/ }));
  expect(view.getByText("2m 5s")).toBeTruthy();
});

it("says why a queued job waits and when it is tried again", () => {
  const later = new Date(Date.now() + 10 * 60_000).toISOString();
  const view = show([], [
    item({ id: 3, state: "queued", run_after: later, error: "Ollama is not reachable at http://ollama:11434" }),
  ]);
  expect(view.getByText(/Retrying at .* · Ollama is not reachable/)).toBeTruthy();
  expect(view.queryByText("Waiting for processing")).toBeNull();
});
