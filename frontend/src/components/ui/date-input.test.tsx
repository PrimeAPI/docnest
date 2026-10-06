import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { DateInput } from "./date-input";

describe("DateInput", () => {
  it("shows DD/MM/YYYY and emits ISO dates", () => {
    const onValueChange = vi.fn();
    const view = render(<DateInput aria-label="Date" value="2026-03-14" onValueChange={onValueChange} />);

    const input = view.container.querySelector("input") as HTMLInputElement;
    expect(input.value).toBe("14/03/2026");
    fireEvent.change(input, { target: { value: "01/12/2025" } });
    expect(onValueChange).toHaveBeenCalledWith("2025-12-01");
  });

  it("marks impossible dates as invalid instead of saving them", () => {
    const onValueChange = vi.fn();
    const view = render(<DateInput aria-label="Date" value="" onValueChange={onValueChange} />);

    const input = view.container.querySelector("input") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "31/02/2026" } });
    fireEvent.blur(input);
    expect(input.getAttribute("aria-invalid")).toBe("true");
    expect(onValueChange).not.toHaveBeenCalled();
  });
});
