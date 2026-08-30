import { describe, expect, it } from "vitest";
import { ApiClientError } from "./api";

describe("ApiClientError", () => {
  it("exposes structured diagnostics", () => {
    const error = new ApiClientError(422, {
      code: "INPUT_COMPILATION_FAILED",
      message: "Inputs are invalid",
      diagnostics: [
        {
          code: "REFERENCE_POINTER_NOT_FOUND",
          specification_path: "/sections/0/fields/0/reference/pointer",
          reference_pointer: "/order/id",
          message: "Pointer does not exist",
          remediation_hint: "Correct the pointer",
        },
      ],
    });
    expect(error.message).toBe("Inputs are invalid");
    expect(error.apiError?.diagnostics?.[0]?.reference_pointer).toBe(
      "/order/id",
    );
  });
});
