// The observable contract: how a rendered page is reduced to comparable data.
//
// The reduction erases differences that are artefacts of *which* framework
// rendered the page, and preserves everything a user or an integrator could
// notice. Concretely, erased:
//
//   - comment nodes            (Vue marks empty v-if slots with them)
//   - scoped-style attributes  (data-v-xxxxxxxx)
//   - class ORDER              (compared as a set; binding order is internal)
//   - vue-router's automatic router-link-active / -exact-active classes
//   - collapsible whitespace   (template indentation vs JSX indentation)
//   - text-node SPLITTING      (adjacent text nodes are merged into one run)
//   - boolean attribute VALUES (disabled="disabled" vs disabled="")
//   - form-control value/checked ATTRIBUTES (captured as live properties)
//
// Preserved exactly: element identity and order, every other attribute and its
// value, non-breaking spaces, text content after collapsing, inline styles, and
// the live value of every form control.
//
// Text deserves a note, because it is where two frameworks differ most while
// showing the user the same thing. A compiled template emits one text node for
// `{{ a }} {{ b }}`; JSX emits three. A template writes `\n  conduit\n` where JSX
// writes `conduit`. Neither difference is observable, so text is compared as
// *runs*: consecutive text nodes are concatenated, whitespace is collapsed, and
// the ends are trimmed of ordinary spaces -- but not of non-breaking spaces,
// which are content. A run that is nothing but whitespace survives as a single
// space when it sits between two elements, and is dropped at the edges of an
// element's children; that is the same distinction Vue's `condense` mode and
// JSX's whitespace rules already make independently, so both sides agree.
//
// snapshot() is injected into the page, so it must be entirely self-contained.

export const PROBE_SELECTORS = [
  ".navbar .nav-link",
  ".navbar .navbar-brand",
  ".banner h1",
  ".banner p",
  ".feed-toggle .nav-link",
  ".feed-toggle .nav-link.active",
  ".article-preview",
  ".article-preview .preview-link h1",
  ".article-preview .preview-link p",
  ".article-preview .author",
  ".article-preview .date",
  ".article-preview .counter",
  ".article-preview .tag-list .tag-pill",
  ".article-preview img",
  ".sidebar .tag-list .tag-pill",
  ".pagination .page-item",
  ".pagination .page-item.active",
  ".pagination .page-link",
  ".article-page h1",
  ".article-page .article-content p",
  ".article-page .tag-list .tag-pill",
  ".article-meta",
  ".article-meta .author",
  ".card",
  ".card .card-text",
  ".card .comment-author",
  ".error-messages li",
  ".user-info h4",
  ".user-info p",
  "form",
  "form button",
  "input",
  "textarea",
  "a[href]",
  "#app > *",
];

/**
 * Runs in the page. Returns a flat, document-order node list plus auxiliary
 * observations.
 * @param {{probes: string[]}} args
 */
export function snapshot(args) {
  const BOOLEAN_ATTRS = new Set([
    "disabled", "checked", "readonly", "required", "selected", "multiple",
    "hidden", "autofocus", "novalidate", "open", "async", "defer", "loop",
    "muted", "controls", "default", "reversed", "ismap", "playsinline",
  ]);
  const DROP_TAGS = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "LINK", "TEMPLATE"]);
  const CONTROL_TAGS = new Set(["INPUT", "TEXTAREA", "SELECT"]);
  const DROP_CLASSES = new Set(["router-link-active", "router-link-exact-active"]);
  // A textarea's value is not part of the observable tree here. Frameworks
  // disagree about where it lives -- Vue binds the value property and leaves the
  // element childless; React hands the initial value to the DOM as the element's
  // children, so the same filled textarea has a text child under one and not the
  // other. Neither is visible to a user, and the value itself is recorded in
  // `controls` and asserted from there. Same reason `class`, `data-v-*` and
  // `data-reactroot` are dropped: a framework fossil is not behaviour.
  const OPAQUE_TAGS = new Set(["TEXTAREA"]);

  const collapse = (s) => s.replace(/[ \t\n\r\f\v]+/g, " ");
  // Not String.prototype.trim: that also strips U+00A0, and `&nbsp;` is content
  // the templates use deliberately for spacing inside buttons and links.
  const trimSpace = (s) => s.replace(/^[ \t\n\r\f\v]+|[ \t\n\r\f\v]+$/g, "");

  function normStyle(value) {
    const parts = String(value)
      .split(";")
      .map((p) => p.trim())
      .filter(Boolean)
      .map((p) => {
        const i = p.indexOf(":");
        if (i < 0) return p.toLowerCase();
        return `${p.slice(0, i).trim().toLowerCase()}: ${p.slice(i + 1).trim()}`;
      });
    parts.sort();
    return parts.join("; ");
  }

  function attrsOf(el) {
    const out = {};
    const isControl = CONTROL_TAGS.has(el.tagName);
    for (const a of Array.from(el.attributes)) {
      const name = a.name.toLowerCase();
      if (name === "class") continue;
      if (name.startsWith("data-v-")) continue;
      if (name === "data-reactroot" || name === "data-reactid") continue;
      if (isControl && (name === "value" || name === "checked")) continue;
      if (BOOLEAN_ATTRS.has(name)) { out[name] = ""; continue; }
      if (name === "style") { out[name] = normStyle(a.value); continue; }
      out[name] = a.value;
    }
    return out;
  }

  function classesOf(el) {
    const list = Array.from(el.classList).filter((c) => !DROP_CLASSES.has(c));
    list.sort();
    return list;
  }

  const root = document.querySelector("#app");
  const nodes = [];
  const controls = [];

  function walk(el, path) {
    // Two passes. The first flattens the child list and merges adjacent text
    // nodes, because how many nodes a framework split a string across is not
    // something anyone can see. The second emits, which is where a text run
    // finally has the context -- what sits on either side of it -- to decide
    // whether its whitespace was content or indentation.
    const items = [];
    for (const child of Array.from(el.childNodes)) {
      if (child.nodeType === 8) continue; // comment
      if (child.nodeType === 3) {
        const last = items[items.length - 1];
        if (last && last.kind === "text") last.text += child.nodeValue;
        else items.push({ kind: "text", text: child.nodeValue });
        continue;
      }
      if (child.nodeType !== 1) continue;
      if (DROP_TAGS.has(child.tagName)) continue;
      items.push({ kind: "element", el: child });
    }

    const tagCounts = Object.create(null);
    let textIndex = 0;
    for (let i = 0; i < items.length; i += 1) {
      const item = items[i];

      if (item.kind === "text") {
        // trimSpace leaves U+00A0 alone, so an nbsp-only run survives as
        // content -- which is what these templates use it for.
        let text = trimSpace(collapse(item.text));
        if (text === "") {
          // Whitespace between two elements is a space the user sees. The same
          // whitespace at the start or end of an element's children is template
          // indentation and nothing else.
          if (i === 0 || i === items.length - 1) continue;
          text = " ";
        }
        textIndex += 1;
        nodes.push({ path: `${path}/#text[${textIndex}]`, kind: "text", text });
        continue;
      }

      const child = item.el;
      const tag = child.tagName.toLowerCase();
      tagCounts[tag] = (tagCounts[tag] || 0) + 1;
      const childPath = `${path}/${tag}[${tagCounts[tag]}]`;
      nodes.push({
        path: childPath,
        kind: "element",
        tag,
        attrs: attrsOf(child),
        classes: classesOf(child),
      });
      if (CONTROL_TAGS.has(child.tagName)) {
        controls.push({
          path: childPath,
          tag,
          type: (child.getAttribute("type") || "").toLowerCase(),
          value: typeof child.value === "string" ? child.value : null,
          checked: typeof child.checked === "boolean" ? child.checked : null,
          disabled: !!child.disabled,
          placeholder: child.getAttribute("placeholder"),
        });
      }
      if (OPAQUE_TAGS.has(child.tagName)) continue;
      walk(child, childPath);
    }
  }

  if (root) {
    nodes.push({
      path: "#app",
      kind: "element",
      tag: "div",
      attrs: attrsOf(root),
      classes: classesOf(root),
    });
    walk(root, "#app");
  }

  // A probe's text is built from the same normalised runs the node walk emits,
  // not from textContent. Two reasons, and both are about not measuring things
  // that are not behaviour:
  //
  //   * textContent includes a textarea's value under React and not under Vue,
  //     for the reason OPAQUE_TAGS explains;
  //   * a template's own indentation survives into textContent. Vue leaves the
  //     whitespace around an interpolation alone when the text node has content,
  //     so `<a>\n  {{ x }}\n</a>` reaches the DOM as "\n  alice\n" -- and JSX
  //     strips exactly that whitespace, so the identical React element reaches
  //     the DOM as "alice". Comparing raw textContent would score a port on how
  //     the retired framework's source happened to be laid out, which no user
  //     can see and which the stated text rule already discards.
  function probeText(el) {
    // The probed element can itself be the opaque one, and then it has no text
    // to report -- same rule as for an opaque descendant.
    if (OPAQUE_TAGS.has(el.tagName)) return "";
    const runs = [];
    (function collect(node) {
      const items = [];
      for (const child of Array.from(node.childNodes)) {
        if (child.nodeType === 8) continue;
        if (child.nodeType === 3) {
          const last = items[items.length - 1];
          if (last && last.kind === "text") last.text += child.nodeValue;
          else items.push({ kind: "text", text: child.nodeValue });
          continue;
        }
        if (child.nodeType !== 1) continue;
        if (DROP_TAGS.has(child.tagName)) continue;
        items.push({ kind: "element", el: child });
      }
      for (let i = 0; i < items.length; i += 1) {
        const item = items[i];
        if (item.kind === "text") {
          let text = trimSpace(collapse(item.text));
          if (text === "") {
            if (i === 0 || i === items.length - 1) continue;
            text = " ";
          }
          runs.push(text);
          continue;
        }
        if (OPAQUE_TAGS.has(item.el.tagName)) continue;
        collect(item.el);
      }
    })(el);
    return runs.join("");
  }

  const probes = {};
  for (const sel of args.probes) {
    let found = [];
    try { found = Array.from(document.querySelectorAll(sel)); } catch { found = []; }
    probes[sel] = {
      count: found.length,
      texts: found.map((e) => trimSpace(collapse(probeText(e)))),
      hrefs: found.map((e) => e.getAttribute("href")),
    };
  }

  const storage = {};
  for (let i = 0; i < localStorage.length; i += 1) {
    const key = localStorage.key(i);
    storage[key] = localStorage.getItem(key);
  }
  const storageKeys = Object.keys(storage).sort();

  return {
    hasApp: !!root,
    url: location.pathname + location.search + location.hash,
    title: document.title,
    nodeCount: nodes.length,
    nodes,
    controls,
    probes,
    storage,
    storageKeys,
    bodyClasses: Array.from(document.body.classList).sort(),
  };
}
