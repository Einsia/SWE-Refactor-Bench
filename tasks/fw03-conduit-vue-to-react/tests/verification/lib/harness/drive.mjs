// Runs one scenario in a fresh browser context and reduces the result.
//
// "Settled" is defined without reference to either framework's scheduler: no
// in-flight XHR/fetch, and no DOM mutation for a quiet window. That holds for
// Vue's nextTick batching and for React's concurrent rendering alike, so the
// same definition can drive both states without a framework-specific hook.

import { snapshot, PROBE_SELECTORS } from "./normalize.mjs";
import { sealContext } from "./seal.mjs";

const TOKENS = {
  alice: "jwt.alice.fixed",
  bob: "jwt.bob.fixed",
  carol: "jwt.carol.fixed",
};

// Tokens are replaced by symbolic names in the recorded output so the contract
// is "the app sent the current user's token", not "the app sent this string".
function symbolise(value) {
  if (typeof value !== "string") return value;
  let out = value;
  for (const [name, token] of Object.entries(TOKENS)) {
    out = out.split(token).join(`<token:${name}>`);
  }
  return out.replace(/jwt\.([a-z0-9_-]+)\.fixed/gi, "<token:$1>");
}

export const initScript = (token) => {
  // Count in-flight requests, and seed the session if the scenario has one.
  window.__pending = 0;
  const proto = window.XMLHttpRequest && window.XMLHttpRequest.prototype;
  if (proto && proto.send) {
    const send = proto.send;
    proto.send = function patched(...args) {
      window.__pending += 1;
      this.addEventListener("loadend", () => { window.__pending -= 1; });
      return send.apply(this, args);
    };
  }
  if (window.fetch) {
    const f = window.fetch;
    window.fetch = function patched(...args) {
      window.__pending += 1;
      return f.apply(this, args).finally(() => { window.__pending -= 1; });
    };
  }
  if (token) {
    try { window.localStorage.setItem("id_token", token); } catch (e) { /* ignore */ }
  }
};

function domQuiet(page, quietMs, capMs) {
  return page.evaluate(
    ([quiet, cap]) => new Promise((resolve) => {
      let timer = null;
      const obs = new MutationObserver(() => {
        clearTimeout(timer);
        timer = setTimeout(finish, quiet);
      });
      const finish = () => { obs.disconnect(); clearTimeout(hard); resolve(true); };
      const hard = setTimeout(() => { obs.disconnect(); resolve(false); }, cap);
      obs.observe(document.documentElement, {
        subtree: true, childList: true, attributes: true, characterData: true,
      });
      timer = setTimeout(finish, quiet);
    }),
    [quietMs, capMs],
  ).catch(() => false);
}

export async function settle(page) {
  const deadline = Date.now() + 10000;
  for (let i = 0; i < 12 && Date.now() < deadline; i += 1) {
    await page
      .waitForFunction(() => window.__pending === 0, null, { timeout: 4000 })
      .catch(() => {});
    await domQuiet(page, 150, 3000);
    const pending = await page.evaluate(() => window.__pending).catch(() => 0);
    if (pending === 0) {
      // One more short quiet window catches a request fired by the render that
      // just finished (Article's meta -> profile fetch, for example).
      await page.waitForTimeout(120);
      const again = await page.evaluate(() => window.__pending).catch(() => 0);
      if (again === 0) break;
    }
  }
  await page.waitForTimeout(80);
}

async function applyStep(page, step, baseUrl) {
  if (step.goto !== undefined) {
    const target = step.goto.startsWith("#") ? `${baseUrl}/${step.goto}` : `${baseUrl}${step.goto}`;
    await page.goto(target, { waitUntil: "load", timeout: 30000 });
    return;
  }
  if (step.back) { await page.goBack({ waitUntil: "load" }).catch(() => {}); return; }
  // .first() makes a multi-match selector deterministic instead of an error.
  if (step.click !== undefined) {
    await page.locator(step.click).first().click({ timeout: 8000 });
    return;
  }
  if (step.fill !== undefined) {
    // fill() drives the element the way a user would, which is what React's
    // controlled inputs need; assigning .value directly would not notify React.
    await page.locator(step.fill).first().fill(step.value, { timeout: 8000 });
    return;
  }
  if (step.press !== undefined) {
    await page.locator(step.press).first().press(step.key, { timeout: 8000 });
    return;
  }
  throw new Error(`unknown step: ${JSON.stringify(step)}`);
}

/**
 * @param probes selectors to observe. Defaults to the standard list; a caller
 *        that wants to look somewhere else passes its own. The reduction is the
 *        same either way -- this decides where to point it, not what it records.
 * @returns observation object, or {error} if the scenario could not be driven
 *          (a missing click target is itself a finding, not a harness failure).
 */
export async function runScenario(browser, srv, scenario, probes = PROBE_SELECTORS) {
  srv.reset();
  const context = await browser.newContext({
    viewport: { width: 1280, height: 900 },
    // Fixed locale and timezone: any date the app formats itself must not vary.
    locale: "en-US",
    timezoneId: "UTC",
  });
  await sealContext(context, srv.url);
  const page = await context.newPage();
  const pageErrors = [];
  const stepErrors = [];
  page.on("pageerror", (e) => pageErrors.push(String((e && e.message) || e)));
  await page.addInitScript(initScript, TOKENS[scenario.token] || null);

  for (const [i, step] of scenario.steps.entries()) {
    try {
      await applyStep(page, step, srv.url);
      await settle(page);
    } catch (err) {
      stepErrors.push(`step ${i} ${JSON.stringify(step)}: ${String(err && err.message)}`);
      break;
    }
  }

  let observed = null;
  try {
    observed = await page.evaluate(snapshot, { probes });
  } catch (err) {
    stepErrors.push(`snapshot: ${String(err && err.message)}`);
  }
  await context.close();

  const requests = srv.log.map((r) => ({
    method: r.method,
    path: r.path,
    search: r.search,
    query: r.query,
    body: r.body,
    auth: symbolise(r.auth),
    status: r.status,
  }));

  if (observed) {
    observed.storage = Object.fromEntries(
      Object.entries(observed.storage).map(([k, v]) => [k, symbolise(v)]),
    );
  }

  return {
    id: scenario.id,
    group: scenario.group,
    token: scenario.token || null,
    stepErrors,
    pageErrors: pageErrors.map(symbolise),
    requests,
    observed,
  };
}
