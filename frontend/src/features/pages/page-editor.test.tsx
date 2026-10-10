import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { PageEditor } from "./page-editor";

const queries = vi.hoisted(() => ({
  data: {
    id: "source", title: "Letter", page_count: 2, visible_pages: [1, 2],
    pages: [{ number: 1, mark: "", blank: false }, { number: 2, mark: "", blank: false }],
  },
  fetching: false,
  post: vi.fn().mockResolvedValue({}),
}));

vi.mock("@tanstack/react-query", () => ({
  useQueries: () => [{ data: queries.data, isPending: false, isFetching: queries.fetching }],
  useMutation: ({ mutationFn }: { mutationFn: () => unknown }) => ({ mutate: mutationFn, isPending: false }),
}));
vi.mock("@/api/client", () => ({
  call: (run: () => unknown) => run(),
  client: { POST: queries.post },
}));
vi.mock("@/api/queries", () => ({ useInvalidateDocuments: () => vi.fn() }));

afterEach(cleanup);

it("keeps the editor's original page baseline when queries refresh in the background", () => {
  queries.data = { ...queries.data, visible_pages: [1, 2] };
  const props = { sourceIds: ["source"], onClose: vi.fn() };
  const view = render(<PageEditor {...props} />);
  fireEvent.click(view.getByRole("img", { name: "Page 2 of Letter" }));
  fireEvent.click(view.getByRole("button", { name: "Leave out" }));

  queries.data = { ...queries.data, visible_pages: [1] };
  view.rerender(<PageEditor {...props} />);
  const apply = view.getByRole("button", { name: "Apply" }) as HTMLButtonElement;
  expect(apply.disabled).toBe(false);
  fireEvent.click(apply);
  expect(queries.post).toHaveBeenCalledWith("/api/v1/alterations/compose", {
    body: {
      sources: ["source"], expected_views: { source: [1, 2] }, origin: undefined,
      outputs: [{ title: "", pages: [{ document: "source", page: 1 }] }],
    },
  });
});

it("waits for refreshed pages before initializing an editor reopened after undo", () => {
  queries.data = { ...queries.data, visible_pages: [1] };
  queries.fetching = true;
  const props = { sourceIds: ["source"], onClose: vi.fn() };
  const view = render(<PageEditor {...props} />);
  expect(view.queryAllByRole("img")).toHaveLength(0);

  queries.data = { ...queries.data, visible_pages: [1, 2] };
  queries.fetching = false;
  view.rerender(<PageEditor {...props} />);
  expect(view.getByRole("button", { name: "Cut document 1 before page 2" })).toBeTruthy();
  expect(view.queryByRole("button", { name: "Put all back" })).toBeNull();
});
