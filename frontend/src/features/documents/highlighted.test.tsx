import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Highlighted } from "./document-row";

describe("Highlighted", () => {
  it("wraps highlight ranges in <mark> and escapes text", () => {
    const { container } = render(
      <p>
        <Highlighted text="Ihre <b>Fahrzeugversicherung</b> 2026" highlights={[[8, 28]]} />
      </p>,
    );
    expect(container.querySelector("mark")?.textContent).toBe("Fahrzeugversicherung");
    expect(container.querySelector("b")).toBeNull();
  });
});
