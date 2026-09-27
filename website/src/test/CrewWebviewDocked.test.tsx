/**
 * The docked opt-in: a template that carries `<!--kirocrew:docked-->` is shown
 * IN the drawer card, as a small frame of its own document, instead of only
 * after Expand.
 *
 * The rules the existing containment suite pins for the expanded frame hold for
 * this one too, and are asserted here per frame: the exact sandbox string, no
 * markup from crew data in the React tree, and the single-use URL (a docked
 * frame stays mounted through expand and collapse and is never re-minted). A
 * template that does NOT opt in keeps the zero-mint native summary.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const DOCKED_URL = "/sandbox-doc/docked/1.mac";
const EXPANDED_URL = "/sandbox-doc/expanded/2.mac";
const PANEL_HTML = '<section id="xy-stage"></section><div id="kp-root"></div>';
const SLUG = "xiaoya-crew";
const CREW = "xiaoya-crew";

vi.mock("../hooks/useTheme", () => ({
  useTheme: () => ({ theme: "dark", colorTheme: "default", themeVersion: 0 }),
}));

vi.mock("../lib/widgetSrcdoc", () => ({
  THEME_VAR_NAMES: [] as string[],
  readThemeVars: () => ({}) as Record<string, string>,
  buildSrcdoc: (opts: { html: string }) => opts.html,
}));

const mintSpy = vi.fn();
const panelSpy = vi.fn();
vi.mock("../api/client", () => ({
  api: {
    sandboxDocUrl: (html: string) => mintSpy(html),
    memberPanel: (slug: string, member: string) => panelSpy(slug, member),
  },
  ApiError: class extends Error {},
}));

import CrewWebview, {
  CREW_WEBVIEW_SANDBOX,
  DOCKED_VIEW_MARK,
} from "../pages/members/CrewWebview";

function body(dockedHeight: number | null | undefined) {
  return {
    panel: {
      template: "xiaoya",
      title: "PR #123",
      crew: CREW,
      published_at: "2026-09-27T05:16:00",
      data: {
        xiaoya: { state: "working", mood: "busy", line: "在跑测试" },
        waiting_on_you: ["合并 PR #123"],
      },
      docked_height: dockedHeight,
    },
    html: PANEL_HTML,
  };
}

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  const view = render(
    <QueryClientProvider client={client}>
      <CrewWebview slug={SLUG} member={CREW} />
    </QueryClientProvider>,
  );
  return { ...view, client };
}

const q = (id: string) => document.querySelector(`[data-testid="${id}"]`);

async function dockedFrame(): Promise<HTMLIFrameElement> {
  let frame: HTMLIFrameElement | null = null;
  await waitFor(() => {
    frame = q("crew-webview-docked-frame") as HTMLIFrameElement | null;
    expect(frame).not.toBeNull();
  });
  return frame as unknown as HTMLIFrameElement;
}

async function waitExpanded(want: "true" | "false") {
  await waitFor(() => {
    expect(q("crew-webview")?.getAttribute("data-expanded")).toBe(want);
  });
}

describe("crew webview docked opt-in", () => {
  beforeEach(() => {
    mintSpy.mockReset();
    mintSpy.mockImplementation((html: string) =>
      Promise.resolve({
        url: html.startsWith(DOCKED_VIEW_MARK) ? DOCKED_URL : EXPANDED_URL,
      }),
    );
    panelSpy.mockReset();
  });

  it("renders the template's own docked frame in the card", async () => {
    panelSpy.mockImplementation(() => Promise.resolve(body(180)));
    mount();
    const frame = await dockedFrame();
    expect(frame.getAttribute("src")).toBe(DOCKED_URL);
    expect(frame.getAttribute("sandbox")).toBe(CREW_WEBVIEW_SANDBOX);
    expect((q("crew-webview-docked") as HTMLElement).style.height).toBe("180px");
    // The docked copy is told it is docked; nothing else is prepended.
    expect(mintSpy).toHaveBeenCalledTimes(1);
    expect(mintSpy.mock.calls[0][0]).toBe(DOCKED_VIEW_MARK + PANEL_HTML);
  });

  it("labels the reserved slot while the docked mint is in flight", async () => {
    mintSpy.mockImplementation(() => new Promise(() => {}));
    panelSpy.mockImplementation(() => Promise.resolve(body(180)));
    mount();
    await waitFor(() => expect(q("crew-webview-docked-pending")).not.toBeNull());
    expect(q("crew-webview-docked-pending")?.textContent).toBeTruthy();
    expect(document.querySelector("iframe")).toBeNull();
    expect(q("crew-webview-docked-failed")).toBeNull();
  });

  it("clamps a height the server did not clamp", async () => {
    panelSpy.mockImplementation(() => Promise.resolve(body(5000)));
    mount();
    await dockedFrame();
    expect((q("crew-webview-docked") as HTMLElement).style.height).toBe("320px");
  });

  it("keeps the zero-mint native summary for a template that did not opt in", async () => {
    for (const h of [null, undefined, 0, Number.NaN]) {
      mintSpy.mockClear();
      panelSpy.mockImplementation(() => Promise.resolve(body(h)));
      const { unmount } = mount();
      await waitFor(() => expect(q("crew-webview-summary")).not.toBeNull());
      expect(q("crew-webview-docked")).toBeNull();
      expect(document.querySelector("iframe")).toBeNull();
      expect(mintSpy).not.toHaveBeenCalled();
      expect(q("crew-webview-docked-failed")).toBeNull();
      unmount();
    }
  });

  it("never re-mints the docked frame across expand and collapse", async () => {
    panelSpy.mockImplementation(() => Promise.resolve(body(180)));
    mount();
    const docked = await dockedFrame();

    fireEvent.click(q("crew-webview-expand") as Element);
    await waitExpanded("true");
    await waitFor(() => expect(mintSpy).toHaveBeenCalledTimes(2));
    // Hidden with its card, NOT unmounted: its URL is spent.
    expect(q("crew-webview-docked-frame")).toBe(docked);
    expect(q("crew-webview-summary")?.className).toBe("hidden");
    const frames = Array.from(document.querySelectorAll("iframe"));
    expect(frames.map((f) => f.getAttribute("sandbox"))).toEqual([
      CREW_WEBVIEW_SANDBOX,
      CREW_WEBVIEW_SANDBOX,
    ]);
    // The expanded copy is the plain document, not the docked one.
    expect(mintSpy.mock.calls[1][0]).toBe(PANEL_HTML);

    fireEvent.click(q("crew-webview-collapse") as Element);
    await waitExpanded("false");
    expect(q("crew-webview-docked-frame")).toBe(docked);
    expect(docked.getAttribute("src")).toBe(DOCKED_URL);
    expect(mintSpy).toHaveBeenCalledTimes(2);
  });

  it("falls back to the native summary when the docked mint fails", async () => {
    mintSpy.mockImplementation(() => Promise.reject(new Error("mint down")));
    panelSpy.mockImplementation(() => Promise.resolve(body(180)));
    mount();
    await waitFor(() => expect(mintSpy).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(q("crew-webview-docked")).toBeNull());
    expect(q("crew-webview-summary")).not.toBeNull();
    // Through the shared error surface, with a retry that mints again.
    const notice = q("crew-webview-docked-error") as HTMLElement;
    expect(notice.getAttribute("role")).toBe("alert");
    expect(notice.querySelector("button, a")).not.toBeNull();
    mintSpy.mockImplementation((html: string) =>
      Promise.resolve({
        url: html.startsWith(DOCKED_VIEW_MARK) ? DOCKED_URL : EXPANDED_URL,
      }),
    );
    fireEvent.click(q("crew-webview-docked-retry") as Element);
    const frame = await dockedFrame();
    expect(frame.getAttribute("src")).toBe(DOCKED_URL);
    expect(mintSpy).toHaveBeenCalledTimes(2);
    expect(q("crew-webview-docked-failed")).toBeNull();
    expect(q("crew-webview-expand")).not.toBeNull();
  });

  it("keeps the last docked document and says so when a refresh fails", async () => {
    panelSpy.mockImplementation(() => Promise.resolve(body(180)));
    const { client } = mount();
    const frame = await dockedFrame();
    // A newer publish re-mints the docked copy; that mint fails.
    mintSpy.mockImplementation(() => Promise.reject(new Error("mint down")));
    panelSpy.mockImplementation(() =>
      Promise.resolve({ ...body(180), html: PANEL_HTML + "<p></p>" }),
    );
    await client.invalidateQueries();
    await waitFor(() => expect(q("crew-webview-docked-error")).not.toBeNull());
    // The document that loaded stays on screen, and the notice says so.
    expect(q("crew-webview-docked-frame")).toBe(frame);
    expect(frame.getAttribute("src")).toBe(DOCKED_URL);
  });

  it("never shows one crew's docked document under another crew sharing its slug", async () => {
    panelSpy.mockImplementation(() => Promise.resolve(body(180)));
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    const tree = (member: string) => (
      <QueryClientProvider client={client}>
        <CrewWebview slug={SLUG} member={member} />
      </QueryClientProvider>
    );
    const { rerender } = render(tree(CREW));
    const first = await dockedFrame();
    // A second crew on the SAME slug, already in the cache (so there is no
    // loading window to reset the frame), whose docked mint fails.
    client.setQueryData(["member-panel", SLUG, "Xiaoya-Crew"], {
      ...body(180),
      html: PANEL_HTML + "<p></p>",
    });
    mintSpy.mockImplementation(() => Promise.reject(new Error("mint down")));
    rerender(tree("Xiaoya-Crew"));
    await waitFor(() => expect(q("crew-webview-docked-error")).not.toBeNull());
    expect(q("crew-webview-docked-frame")).toBeNull();
    expect(document.body.contains(first)).toBe(false);
  });

  it("builds the docked path without dangerouslySetInnerHTML", () => {
    const src = readFileSync(
      resolve(__dirname, "../pages/members/CrewWebview.tsx"),
      "utf8",
    );
    expect(src).not.toContain("dangerouslySetInnerHTML=");
  });
});
