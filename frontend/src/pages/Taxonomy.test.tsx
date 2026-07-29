import { describe, expect, it } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { Taxonomy } from "./Taxonomy";
import { LanguageProvider } from "../lib/i18n";
import { mockFetchWith } from "../test/mockFetch";
import type { CanonicalCategory, CategoryAuditLogEntry } from "../api";

function makeCategory(overrides: Partial<CanonicalCategory> = {}): CanonicalCategory {
  return {
    category_id: "cc_1",
    product_category: "wireless earbuds",
    canonical_label: "battery_life",
    normalized_label: "battery life",
    status: "proposed",
    alias_of: null,
    first_seen_aspect_raw: "battery_life",
    created_at: "2026-01-01T00:00:00+00:00",
    updated_at: "2026-01-01T00:00:00+00:00",
    ...overrides,
  };
}

function renderTaxonomy(initialEntry = "/taxonomy") {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <LanguageProvider>
        <Routes>
          <Route path="/taxonomy" element={<Taxonomy />} />
        </Routes>
      </LanguageProvider>
    </MemoryRouter>,
  );
}

async function browseTo(user: ReturnType<typeof userEvent.setup>, productCategory: string) {
  await user.type(screen.getByPlaceholderText("e.g. wireless earbuds"), productCategory);
  await user.click(screen.getByText("Browse"));
}

describe("Taxonomy -- browsing and listing", () => {
  it("prompts for a product category before loading anything", () => {
    renderTaxonomy();
    expect(screen.getByText("Enter a product category above to browse its taxonomy.")).toBeTruthy();
  });

  it("loads and lists categories for the entered product category", async () => {
    const user = userEvent.setup();
    mockFetchWith((url) => {
      if (url.includes("/categories?")) {
        return { body: [makeCategory({ canonical_label: "battery_life" }), makeCategory({ category_id: "cc_2", canonical_label: "chew_durability" })] };
      }
      return undefined;
    });
    renderTaxonomy();

    await browseTo(user, "wireless earbuds");

    await waitFor(() => expect(screen.getByText("Battery Life")).toBeTruthy());
    expect(screen.getByText("Chew Durability")).toBeTruthy();
  });

  it("shows a loading state while the list request is in flight", async () => {
    const user = userEvent.setup();
    let resolveFetch: (value: CanonicalCategory[]) => void = () => {};
    const pending = new Promise<CanonicalCategory[]>((resolve) => {
      resolveFetch = resolve;
    });
    mockFetchWith((url) => {
      if (url.includes("/categories?")) {
        return { body: pending as unknown as CanonicalCategory[] };
      }
      return undefined;
    });
    // Simpler: use a handler that returns a body wrapped so json() awaits our promise.
    renderTaxonomy();
    await browseTo(user, "wireless earbuds");
    expect(screen.getByText("Loading categories…")).toBeTruthy();
    resolveFetch([]);
  });

  it("shows an empty state when the API returns an empty list", async () => {
    const user = userEvent.setup();
    mockFetchWith((url) => (url.includes("/categories?") ? { body: [] } : undefined));
    renderTaxonomy();

    await browseTo(user, "wireless earbuds");

    await waitFor(() => expect(screen.getByText("No categories found.")).toBeTruthy());
  });

  it("shows a readable error state when the list request fails", async () => {
    const user = userEvent.setup();
    mockFetchWith((url) => (url.includes("/categories?") ? { status: 500, body: { detail: "boom" } } : undefined));
    renderTaxonomy();

    await browseTo(user, "wireless earbuds");

    await waitFor(() => expect(screen.getByText("boom")).toBeTruthy());
  });

  it("filters by status", async () => {
    const user = userEvent.setup();
    mockFetchWith((url) => {
      if (url.includes("/categories?")) {
        expect(url).toContain("product_category=wireless+earbuds");
        if (url.includes("status=approved")) return { body: [makeCategory({ status: "approved", canonical_label: "chew_durability" })] };
        return { body: [makeCategory({ status: "proposed" })] };
      }
      return undefined;
    });
    renderTaxonomy();
    await browseTo(user, "wireless earbuds");
    await waitFor(() => expect(screen.getByText("Battery Life")).toBeTruthy());

    await user.selectOptions(screen.getByDisplayValue("All statuses"), "Approved");

    await waitFor(() => expect(screen.getByText("Chew Durability")).toBeTruthy());
  });

  it("looks up an exact canonical label", async () => {
    const user = userEvent.setup();
    mockFetchWith((url) => {
      if (url.includes("canonical_label=battery")) return { body: [makeCategory({ canonical_label: "battery_life" })] };
      if (url.includes("/categories?")) return { body: [] };
      return undefined;
    });
    renderTaxonomy();
    await browseTo(user, "wireless earbuds");
    await user.type(screen.getByPlaceholderText(/Exact label/), "battery");

    await waitFor(() => expect(screen.getByText("Battery Life")).toBeTruthy());
  });
});

describe("Taxonomy -- status visual states", () => {
  it("distinguishes proposed, approved, and deprecated visually", async () => {
    const user = userEvent.setup();
    mockFetchWith((url) => {
      if (url.includes("/categories?")) {
        return {
          body: [
            makeCategory({ category_id: "cc_p", canonical_label: "proposed_one", status: "proposed" }),
            makeCategory({ category_id: "cc_a", canonical_label: "approved_one", status: "approved" }),
            makeCategory({ category_id: "cc_d", canonical_label: "deprecated_one", status: "deprecated" }),
          ],
        };
      }
      return undefined;
    });
    renderTaxonomy();
    await browseTo(user, "wireless earbuds");

    await waitFor(() => expect(screen.getByText("Proposed One")).toBeTruthy());
    expect(screen.getByText("Approved One")).toBeTruthy();
    expect(screen.getByText("Deprecated One")).toBeTruthy();
    expect(screen.getAllByText("Proposed").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Approved").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Deprecated").length).toBeGreaterThan(0);
  });

  it("shows an alias relationship for a merged category", async () => {
    const user = userEvent.setup();
    mockFetchWith((url) => {
      if (url.includes("/categories?")) {
        return { body: [makeCategory({ canonical_label: "floor_damage", status: "deprecated", alias_of: "cc_target" })] };
      }
      return undefined;
    });
    renderTaxonomy();
    await browseTo(user, "wireless earbuds");

    await waitFor(() => expect(screen.getByText("Floor Damage → cc_target")).toBeTruthy());
  });

  it("renders neutrally for an unknown future category_status without crashing", async () => {
    const user = userEvent.setup();
    mockFetchWith((url) => {
      if (url.includes("/categories?")) {
        return { body: [{ ...makeCategory(), status: "some_future_status" as CanonicalCategory["status"] }] };
      }
      return undefined;
    });
    renderTaxonomy();
    await browseTo(user, "wireless earbuds");

    await waitFor(() => expect(screen.getByText("Battery Life")).toBeTruthy());
    expect(screen.getByText("some_future_status")).toBeTruthy();
  });
});

describe("Taxonomy -- approve", () => {
  it("approves a proposed category and refreshes", async () => {
    const user = userEvent.setup();
    let approveCalled = false;
    mockFetchWith((url, method) => {
      if (url.includes("/categories?")) return { body: [makeCategory({ status: approveCalled ? "approved" : "proposed" })] };
      if (url.endsWith("/categories/cc_1")) return { body: makeCategory({ status: approveCalled ? "approved" : "proposed" }) };
      if (url.endsWith("/categories/cc_1/approve") && method === "POST") {
        approveCalled = true;
        return { body: makeCategory({ status: "approved" }) };
      }
      if (url.endsWith("/categories/cc_1/history")) return { body: [] };
      return undefined;
    });
    renderTaxonomy();
    await browseTo(user, "wireless earbuds");
    await waitFor(() => expect(screen.getByText("Battery Life")).toBeTruthy());
    await user.click(screen.getByText("Battery Life"));

    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeTruthy());
    await user.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(screen.queryByRole("button", { name: "Approve" })).toBeNull()); // approved -- no longer offered
  });

  it("surfaces a 409 from the backend as a readable message", async () => {
    const user = userEvent.setup();
    mockFetchWith((url, method) => {
      if (url.includes("/categories?")) return { body: [makeCategory({ status: "proposed" })] };
      if (url.endsWith("/categories/cc_1")) return { body: makeCategory({ status: "proposed" }) };
      if (url.endsWith("/categories/cc_1/approve") && method === "POST") {
        return { status: 409, body: { detail: "Category cc_1 is already deprecated" } };
      }
      if (url.endsWith("/categories/cc_1/history")) return { body: [] };
      return undefined;
    });
    renderTaxonomy();
    await browseTo(user, "wireless earbuds");
    await waitFor(() => expect(screen.getByText("Battery Life")).toBeTruthy());
    await user.click(screen.getByText("Battery Life"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeTruthy());

    await user.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(screen.getByText("Category cc_1 is already deprecated")).toBeTruthy());
  });

  it("disables the approve button while the request is pending", async () => {
    const user = userEvent.setup();
    let resolveApprove: (value: unknown) => void = () => {};
    mockFetchWith((url, method) => {
      if (url.includes("/categories?")) return { body: [makeCategory({ status: "proposed" })] };
      if (url.endsWith("/categories/cc_1")) return { body: makeCategory({ status: "proposed" }) };
      if (url.endsWith("/categories/cc_1/history")) return { body: [] };
      if (url.endsWith("/categories/cc_1/approve") && method === "POST") {
        return {
          body: new Promise((resolve) => {
            resolveApprove = resolve;
          }) as unknown,
        };
      }
      return undefined;
    });
    renderTaxonomy();
    await browseTo(user, "wireless earbuds");
    await waitFor(() => expect(screen.getByText("Battery Life")).toBeTruthy());
    await user.click(screen.getByText("Battery Life"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeTruthy());

    await user.click(screen.getByRole("button", { name: "Approve" }));

    expect(screen.getByRole("button", { name: "Approving…" })).toHaveProperty("disabled", true);
    resolveApprove(makeCategory({ status: "approved" }));
  });
});

describe("Taxonomy -- rename", () => {
  it("renames a category successfully", async () => {
    const user = userEvent.setup();
    let renamed = false;
    mockFetchWith((url, method) => {
      if (url.includes("/categories?")) return { body: [makeCategory({ status: "approved", canonical_label: renamed ? "battery_duration" : "battery_life" })] };
      if (url.endsWith("/categories/cc_1")) return { body: makeCategory({ status: "approved", canonical_label: renamed ? "battery_duration" : "battery_life" }) };
      if (url.endsWith("/categories/cc_1/rename") && method === "POST") {
        renamed = true;
        return { body: makeCategory({ status: "approved", canonical_label: "battery_duration" }) };
      }
      if (url.endsWith("/categories/cc_1/history")) return { body: [] };
      return undefined;
    });
    renderTaxonomy();
    await browseTo(user, "wireless earbuds");
    await waitFor(() => expect(screen.getByText("Battery Life")).toBeTruthy());
    await user.click(screen.getByText("Battery Life"));

    const input = await screen.findByDisplayValue("battery_life"); // prefilled with the RAW stored label, not the formatted one
    await user.clear(input);
    await user.type(input, "battery_duration");
    await user.click(screen.getByText("Rename"));

    await waitFor(() => expect(screen.getAllByText("Battery Duration").length).toBeGreaterThan(0));
  });

  it("rejects empty input locally before calling the API", async () => {
    const user = userEvent.setup();
    let renameCalls = 0;
    mockFetchWith((url, method) => {
      if (url.includes("/categories?")) return { body: [makeCategory({ status: "approved" })] };
      if (url.endsWith("/categories/cc_1")) return { body: makeCategory({ status: "approved" }) };
      if (url.endsWith("/categories/cc_1/rename") && method === "POST") {
        renameCalls += 1;
        return { body: makeCategory({ status: "approved" }) };
      }
      if (url.endsWith("/categories/cc_1/history")) return { body: [] };
      return undefined;
    });
    renderTaxonomy();
    await browseTo(user, "wireless earbuds");
    await waitFor(() => expect(screen.getByText("Battery Life")).toBeTruthy());
    await user.click(screen.getByText("Battery Life"));
    const input = await screen.findByDisplayValue("battery_life");
    await user.clear(input);

    expect(screen.getByRole("button", { name: "Rename" })).toHaveProperty("disabled", true);
    expect(renameCalls).toBe(0);
  });

  it("shows a duplicate-label 409 clearly", async () => {
    const user = userEvent.setup();
    mockFetchWith((url, method) => {
      if (url.includes("/categories?")) return { body: [makeCategory({ status: "approved" })] };
      if (url.endsWith("/categories/cc_1")) return { body: makeCategory({ status: "approved" }) };
      if (url.endsWith("/categories/cc_1/rename") && method === "POST") {
        return { status: 409, body: { detail: "A category with the normalized label 'chew durability' already exists" } };
      }
      if (url.endsWith("/categories/cc_1/history")) return { body: [] };
      return undefined;
    });
    renderTaxonomy();
    await browseTo(user, "wireless earbuds");
    await waitFor(() => expect(screen.getByText("Battery Life")).toBeTruthy());
    await user.click(screen.getByText("Battery Life"));
    const input = await screen.findByDisplayValue("battery_life");
    await user.clear(input);
    await user.type(input, "chew_durability");
    await user.click(screen.getByText("Rename"));

    await waitFor(() => expect(screen.getByText(/already exists/)).toBeTruthy());
  });
});

describe("Taxonomy -- merge", () => {
  it("excludes the source itself and deprecated categories from the target list", async () => {
    const user = userEvent.setup();
    mockFetchWith((url) => {
      if (url.includes("/categories?")) {
        return {
          body: [
            makeCategory({ category_id: "cc_1", canonical_label: "battery_life", status: "approved" }),
            makeCategory({ category_id: "cc_2", canonical_label: "battery_duration", status: "approved" }),
            makeCategory({ category_id: "cc_3", canonical_label: "old_one", status: "deprecated" }),
          ],
        };
      }
      if (url.endsWith("/categories/cc_1")) return { body: makeCategory({ category_id: "cc_1", canonical_label: "battery_life", status: "approved" }) };
      if (url.endsWith("/categories/cc_1/history")) return { body: [] };
      return undefined;
    });
    renderTaxonomy();
    await browseTo(user, "wireless earbuds");
    await waitFor(() => expect(screen.getByText("Battery Life")).toBeTruthy());
    await user.click(screen.getAllByText("Battery Life")[0]);

    const targetSelect = await screen.findByDisplayValue("Select an active category");
    const options = within(targetSelect).getAllByRole("option").map((option) => option.textContent);
    expect(options).toContain("Battery Duration");
    expect(options).not.toContain("Battery Life"); // self-merge excluded
    expect(options).not.toContain("Old One"); // deprecated excluded
  });

  it("merges successfully after confirmation", async () => {
    const user = userEvent.setup();
    let merged = false;
    mockFetchWith((url, method) => {
      if (url.includes("/categories?")) {
        return {
          body: merged
            ? [makeCategory({ category_id: "cc_2", canonical_label: "battery_duration", status: "approved" })]
            : [
                makeCategory({ category_id: "cc_1", canonical_label: "battery_life", status: "approved" }),
                makeCategory({ category_id: "cc_2", canonical_label: "battery_duration", status: "approved" }),
              ],
        };
      }
      if (url.endsWith("/categories/cc_1")) return { body: makeCategory({ category_id: "cc_1", status: merged ? "deprecated" : "approved", alias_of: merged ? "cc_2" : null }) };
      if (url.endsWith("/categories/cc_1/merge/cc_2") && method === "POST") {
        merged = true;
        return { body: makeCategory({ category_id: "cc_1", status: "deprecated", alias_of: "cc_2" }) };
      }
      if (url.endsWith("/categories/cc_1/history")) return { body: [] };
      return undefined;
    });
    renderTaxonomy();
    await browseTo(user, "wireless earbuds");
    await waitFor(() => expect(screen.getByText("Battery Life")).toBeTruthy());
    await user.click(screen.getAllByText("Battery Life")[0]);

    const targetSelect = await screen.findByDisplayValue("Select an active category");
    await user.selectOptions(targetSelect, "Battery Duration");
    await user.click(screen.getByRole("button", { name: "Merge" }));
    await waitFor(() => expect(screen.getByText(/Merging will deprecate/)).toBeTruthy());
    await user.click(screen.getByRole("button", { name: "Confirm merge" }));

    await waitFor(() => expect(screen.getByText("cc_2")).toBeTruthy()); // alias_of now shown
  });

  it("surfaces a backend merge validation error (e.g. a cycle) after confirming", async () => {
    const user = userEvent.setup();
    mockFetchWith((url, method) => {
      if (url.includes("/categories?")) {
        return {
          body: [
            makeCategory({ category_id: "cc_1", canonical_label: "battery_life", status: "approved" }),
            makeCategory({ category_id: "cc_2", canonical_label: "battery_duration", status: "approved" }),
          ],
        };
      }
      if (url.endsWith("/categories/cc_1")) return { body: makeCategory({ category_id: "cc_1", status: "approved" }) };
      if (url.endsWith("/categories/cc_1/merge/cc_2") && method === "POST") {
        return { status: 409, body: { detail: "Target cc_2 is itself an alias of cc_1; merge into the root of that chain instead" } };
      }
      if (url.endsWith("/categories/cc_1/history")) return { body: [] };
      return undefined;
    });
    renderTaxonomy();
    await browseTo(user, "wireless earbuds");
    await waitFor(() => expect(screen.getByText("Battery Life")).toBeTruthy());
    await user.click(screen.getAllByText("Battery Life")[0]);
    const targetSelect = await screen.findByDisplayValue("Select an active category");
    await user.selectOptions(targetSelect, "Battery Duration");
    await user.click(screen.getByRole("button", { name: "Merge" }));
    await user.click(screen.getByRole("button", { name: "Confirm merge" }));

    await waitFor(() => expect(screen.getByText(/merge into the root of that chain instead/)).toBeTruthy());
  });
});

describe("Taxonomy -- deprecate", () => {
  it("requires confirmation before deprecating", async () => {
    const user = userEvent.setup();
    let deprecateCalls = 0;
    mockFetchWith((url, method) => {
      if (url.includes("/categories?")) return { body: [makeCategory({ status: "approved" })] };
      if (url.endsWith("/categories/cc_1")) return { body: makeCategory({ status: "approved" }) };
      if (url.endsWith("/categories/cc_1/deprecate") && method === "POST") {
        deprecateCalls += 1;
        return { body: makeCategory({ status: "deprecated" }) };
      }
      if (url.endsWith("/categories/cc_1/history")) return { body: [] };
      return undefined;
    });
    renderTaxonomy();
    await browseTo(user, "wireless earbuds");
    await waitFor(() => expect(screen.getByText("Battery Life")).toBeTruthy());
    await user.click(screen.getByText("Battery Life"));

    await user.click(screen.getByRole("button", { name: "Deprecate" }));
    expect(deprecateCalls).toBe(0); // confirmation shown, not yet called
    expect(screen.getByText(/no longer be selectable/)).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Confirm deprecate" }));
    await waitFor(() => expect(deprecateCalls).toBe(1));
  });
});

describe("Taxonomy -- history", () => {
  function makeAuditEntry(overrides: Partial<CategoryAuditLogEntry> = {}): CategoryAuditLogEntry {
    return {
      id: 1,
      category_id: "cc_1",
      action: "approve",
      detail: { from_status: "proposed", to_status: "approved" },
      created_at: "2026-01-01T00:00:00+00:00",
      ...overrides,
    };
  }

  it("renders history entries in chronological order", async () => {
    const user = userEvent.setup();
    mockFetchWith((url) => {
      if (url.includes("/categories?")) return { body: [makeCategory({ status: "deprecated" })] };
      if (url.endsWith("/categories/cc_1")) return { body: makeCategory({ status: "deprecated" }) };
      if (url.endsWith("/categories/cc_1/history")) {
        return {
          body: [
            makeAuditEntry({ id: 1, action: "approve", created_at: "2026-01-01T00:00:00+00:00" }),
            makeAuditEntry({ id: 2, action: "rename", created_at: "2026-01-02T00:00:00+00:00", detail: { old_label: "a", new_label: "b" } }),
            makeAuditEntry({ id: 3, action: "deprecate", created_at: "2026-01-03T00:00:00+00:00" }),
          ],
        };
      }
      return undefined;
    });
    renderTaxonomy();
    await browseTo(user, "wireless earbuds");
    await waitFor(() => expect(screen.getByText("Battery Life")).toBeTruthy());
    await user.click(screen.getByText("Battery Life"));

    const historyItems = await screen.findAllByRole("listitem");
    const texts = historyItems.map((item) => item.textContent);
    expect(texts[0]).toContain("Approved");
    expect(texts[1]).toContain("Renamed");
    expect(texts[2]).toContain("Deprecated");
  });

  it("never fabricates an actor -- renders history without any actor field", async () => {
    const user = userEvent.setup();
    mockFetchWith((url) => {
      if (url.includes("/categories?")) return { body: [makeCategory()] };
      if (url.endsWith("/categories/cc_1")) return { body: makeCategory() };
      if (url.endsWith("/categories/cc_1/history")) return { body: [makeAuditEntry()] };
      return undefined;
    });
    const { container } = renderTaxonomy();
    await browseTo(user, "wireless earbuds");
    await waitFor(() => expect(screen.getByText("Battery Life")).toBeTruthy());
    await user.click(screen.getByText("Battery Life"));

    await screen.findAllByRole("listitem");
    expect(container.textContent?.toLowerCase()).not.toContain("actor");
  });

  it("shows an empty state when a category has no history", async () => {
    const user = userEvent.setup();
    mockFetchWith((url) => {
      if (url.includes("/categories?")) return { body: [makeCategory()] };
      if (url.endsWith("/categories/cc_1")) return { body: makeCategory() };
      if (url.endsWith("/categories/cc_1/history")) return { body: [] };
      return undefined;
    });
    renderTaxonomy();
    await browseTo(user, "wireless earbuds");
    await waitFor(() => expect(screen.getByText("Battery Life")).toBeTruthy());
    await user.click(screen.getByText("Battery Life"));

    await waitFor(() => expect(screen.getByText("No history yet.")).toBeTruthy());
  });
});
