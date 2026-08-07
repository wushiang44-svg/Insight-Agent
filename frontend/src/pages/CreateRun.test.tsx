import { describe, expect, it } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { CreateRun } from "./CreateRun";
import { LanguageProvider } from "../lib/i18n";
import { mockFetchWith } from "../test/mockFetch";
import type { AppConfig } from "../api";

function makeConfig(overrides: Partial<AppConfig> = {}): AppConfig {
  return {
    reddit_configured: false,
    reddit_browser_configured: true,
    reddit_profile_status: "healthy",
    reddit_last_success_at: "2026-08-01T00:00:00+00:00",
    reddit_last_challenge_at: null,
    reddit_consecutive_challenge_count: 0,
    amazon_configured: false,
    youtube_configured: false,
    deepseek_configured: true,
    ...overrides,
  };
}

async function renderCreateRun(config: AppConfig) {
  mockFetchWith((url) => {
    if (url.includes("/config")) return { body: config };
    return undefined;
  });

  const utils = render(
    <MemoryRouter>
      <LanguageProvider>
        <CreateRun />
      </LanguageProvider>
    </MemoryRouter>,
  );

  await waitFor(() => expect(screen.getByLabelText(/Product category/i)).toBeTruthy());
  return utils;
}

// ---------------------------------------------------------------------------
// Milestone 2 / B1: /config now surfaces reddit_profile_status, not just the
// binary reddit_browser_configured flag -- a user should see a proactive
// warning before starting a run, not only discover a challenged profile
// after a run comes back with 0 evidence.
// ---------------------------------------------------------------------------

describe("CreateRun -- Reddit profile status warnings (Milestone 2 / B1)", () => {
  it("shows no Reddit-specific warning when the profile is healthy", async () => {
    const { container } = await renderCreateRun(makeConfig({ reddit_profile_status: "healthy" }));
    expect(container.textContent).not.toContain("blocked by Reddit's bot detection");
    expect(container.textContent).not.toContain("isn't set up yet");
  });

  it("shows the not-configured warning when reddit_browser_configured is false, regardless of profile_status", async () => {
    const { container } = await renderCreateRun(
      makeConfig({ reddit_browser_configured: false, reddit_profile_status: "not_initialized" }),
    );
    // Pre-existing behavior (not part of this milestone): the effect that
    // loads /config auto-switches dataSource to "json_upload" when Reddit
    // isn't configured, so the Reddit radio has to be reselected to see its
    // own warning again.
    fireEvent.click(screen.getByLabelText(/Reddit \(browser automation/i));
    expect(container.textContent).toContain("isn't set up yet");
  });

  it("shows the challenged warning with the consecutive-count when configured but currently challenged", async () => {
    const { container } = await renderCreateRun(
      makeConfig({ reddit_profile_status: "challenged", reddit_consecutive_challenge_count: 3 }),
    );
    expect(container.textContent).toContain("blocked by Reddit's bot detection");
    expect(container.textContent).toContain("3");
  });

  it("shows the stale/not-yet-searched note when configured but status is unknown", async () => {
    const { container } = await renderCreateRun(makeConfig({ reddit_profile_status: "unknown" }));
    expect(container.textContent).toContain("hasn't completed a successful search yet");
  });

  it("hides the challenged warning once the user switches away from Reddit", async () => {
    const { container } = await renderCreateRun(
      makeConfig({ reddit_profile_status: "challenged", reddit_consecutive_challenge_count: 5, amazon_configured: true }),
    );
    expect(container.textContent).toContain("blocked by Reddit's bot detection"); // present while Reddit is selected

    fireEvent.click(screen.getByLabelText(/Amazon Reviews/i));

    expect(container.textContent).not.toContain("blocked by Reddit's bot detection");
  });
});
