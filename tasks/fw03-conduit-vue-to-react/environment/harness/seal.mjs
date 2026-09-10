// Make the browser context independent of the outside world.
//
// State A's index.html references four third-party origins: the ionicons
// stylesheet, Google Fonts, demo.productionready.io/main.css, and a polyfill.io
// script. Grading runs with no network, so all four fail. Two of those failures
// are harmless, but the missing ionicons stylesheet is not: Conduit's delete and
// remove-tag controls are bare `<i class="ion-trash-a">` elements whose only box
// comes from that stylesheet's icon-font rules. With no stylesheet they collapse
// to zero size, and a click on them is impossible.
//
// So rather than let reachability decide what the app looks like, every
// off-origin request is answered locally with a minimal stub. That has three
// properties worth stating:
//
//   1. It is deterministic. The same bytes are served on every run, on every
//      machine, with or without a network route.
//   2. It is framework-neutral. The stubs are keyed on resource type, not on
//      anything Vue- or React-specific, and the same sealing is applied when
//      capturing the golden and when grading a submission.
//   3. It does not touch the observable contract. normalize.mjs records markup
//      and live control state, never computed layout, so a stylesheet cannot
//      change what is compared. Its only effect is that elements have a box, so
//      the driver can click the same things a person could.
//
// It also means a submission cannot reach out to a real CDN even if one were
// reachable: everything it renders must come from its own bundle.

// A 1x1 transparent PNG, so an off-origin <img> resolves instead of firing
// onerror (which an app might handle by mutating the DOM).
const PIXEL_B64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk" +
  "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==";

// Stands in for the ionicons stylesheet. Real ionicons sizes its glyphs with an
// icon font; we only need every icon element to occupy a clickable box, which
// any explicit size achieves. The `content` rule keeps an empty <i> non-empty
// in layout terms without adding text (::before content is not a DOM node, so
// it never appears in a snapshot).
const ICON_CSS = `
[class*="ion-"], [class^="ion-"] {
  display: inline-block;
  width: 16px;
  height: 16px;
  min-width: 16px;
  min-height: 16px;
  line-height: 16px;
  vertical-align: middle;
}
[class*="ion-"]::before, [class^="ion-"]::before { content: "\\25A0"; }
`;

const EMPTY_CSS = "/* off-origin stylesheet stub */\n";
const EMPTY_JS = "/* off-origin script stub */\n";

function stubFor(url, resourceType) {
  const lower = url.toLowerCase();
  if (lower.includes("ionicons")) {
    return { contentType: "text/css", body: ICON_CSS };
  }
  if (resourceType === "stylesheet" || lower.endsWith(".css")) {
    return { contentType: "text/css", body: EMPTY_CSS };
  }
  if (resourceType === "script" || lower.endsWith(".js") || lower.endsWith(".mjs")) {
    return { contentType: "application/javascript", body: EMPTY_JS };
  }
  if (resourceType === "image") {
    return { contentType: "image/png", body: Buffer.from(PIXEL_B64, "base64") };
  }
  if (resourceType === "font") {
    return { contentType: "font/woff2", body: Buffer.alloc(0) };
  }
  return { contentType: "text/plain", body: "" };
}

/**
 * Answer every request that is not for `origin` from a local stub.
 *
 * @param {import('playwright').BrowserContext} context
 * @param {string} origin the harness origin, e.g. http://127.0.0.1:34567
 * @returns {Promise<string[]>} the list of off-origin URLs that were stubbed,
 *   which callers may record but must not score: what a bundle references is
 *   its own business as long as it does not need the reference to work.
 */
export async function sealContext(context, origin) {
  const stubbed = [];
  await context.route("**/*", async (route) => {
    const request = route.request();
    const url = request.url();
    if (url.startsWith(origin) || url.startsWith("data:") || url.startsWith("blob:")) {
      return route.continue();
    }
    stubbed.push(url);
    const stub = stubFor(url, request.resourceType());
    return route.fulfill({
      status: 200,
      contentType: stub.contentType,
      headers: { "cache-control": "no-store", "access-control-allow-origin": "*" },
      body: stub.body,
    });
  });
  return stubbed;
}
