/* probe.c -- differential ABI/API consumer for lang01-cmark-c-to-rust.
 *
 * This program is the verifier's instrument.  It is compiled twice from
 * identical source: once against the pinned C reference, once against the
 * submitted implementation.  Both builds are fed the same case list and their
 * per-case output is compared byte for byte.
 *
 * Two properties matter and are deliberate:
 *
 *   1. It includes ONLY <cmark.h>.  Upstream's own `main.c` reaches into the
 *      private `node.h`, but a legitimate downstream consumer cannot, so
 *      neither does this.  Everything here goes through the 70 exported
 *      symbols and the documented types.  That makes the probe a fair test of
 *      the *published interface* rather than of any internal layout, and it
 *      means a conforming rewrite passes without having to mirror C structs.
 *
 *   2. It is an interpreter, not a fixed script.  Cases arrive as data on
 *      stdin, so the verifier's catalog can describe hundreds of distinct API
 *      workflows without changing this file.
 *
 * Protocol.  stdin: one case per line, tab-separated fields
 *     <case-id> TAB <op> [TAB <arg>]...
 * stdout: for each case, a header line then the raw payload then a newline
 *     #CASE TAB <case-id> TAB <status> TAB <payload-length> LF
 *     <payload bytes> LF
 * Payloads are arbitrary bytes, including NUL, which is why they are
 * length-prefixed rather than delimited.  Records are flushed as they are
 * produced: if a case crashes the process, everything before it survives and
 * the harness can attribute the crash to exactly one case.
 */

#define _POSIX_C_SOURCE 200809L

#include <cmark.h>

#include <errno.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_FIELDS 12
#define MAX_REGS 16
#define MAX_LINE (1 << 20)

/* `cmark_node_check` is exported from the shared library of the pinned release
 * but declared only in the *private* `node.h`, so it is not visible through the
 * installed public header.  It is still part of the release's exported symbol
 * set, which means a conforming rewrite has to keep it -- with C linkage and
 * this signature -- even though no installed header declares it.  Declaring it
 * here lets the probe exercise it as a linker-visible entry point without
 * reaching into private headers. */
CMARK_EXPORT int cmark_node_check(cmark_node *node, FILE *out);

/* ---------------------------------------------------------------- buffers */

typedef struct {
  char *p;
  size_t n, cap;
} buf;

static void bgrow(buf *b, size_t need) {
  if (b->n + need <= b->cap)
    return;
  size_t cap = b->cap ? b->cap : 256;
  while (cap < b->n + need)
    cap *= 2;
  char *p = realloc(b->p, cap);
  if (!p) {
    fputs("probe: out of memory\n", stderr);
    exit(70);
  }
  b->p = p;
  b->cap = cap;
}

static void bput(buf *b, const void *d, size_t n) {
  if (n == 0)
    return;
  bgrow(b, n);
  memcpy(b->p + b->n, d, n);
  b->n += n;
}

static void bputs(buf *b, const char *s) { bput(b, s, strlen(s)); }

static void bputc_(buf *b, char c) { bput(b, &c, 1); }

static void bprintf(buf *b, const char *fmt, ...) {
  char tmp[512];
  va_list ap;
  va_start(ap, fmt);
  int n = vsnprintf(tmp, sizeof(tmp), fmt, ap);
  va_end(ap);
  if (n < 0)
    return;
  if ((size_t)n < sizeof(tmp)) {
    bput(b, tmp, (size_t)n);
    return;
  }
  bgrow(b, (size_t)n + 1);
  va_start(ap, fmt);
  vsnprintf(b->p + b->n, (size_t)n + 1, fmt, ap);
  va_end(ap);
  b->n += (size_t)n;
}

static void bfree(buf *b) {
  free(b->p);
  b->p = NULL;
  b->n = b->cap = 0;
}

/* Emit a completed record.  Flushing per record is what makes a crash
 * attributable to a single case instead of poisoning the whole batch. */
static void emit(const char *id, const char *status, const buf *payload) {
  printf("#CASE\t%s\t%s\t%zu\n", id, status, payload ? payload->n : (size_t)0);
  if (payload && payload->n)
    fwrite(payload->p, 1, payload->n, stdout);
  putchar('\n');
  fflush(stdout);
}

/* ------------------------------------------------------------- decoding */

/* Percent-decoding keeps arbitrary text (tabs, newlines, NUL, invalid UTF-8)
 * inside a single tab-separated field.  Returns a heap block and its length. */
static char *pctdecode(const char *s, size_t *out_len) {
  size_t n = strlen(s);
  char *r = malloc(n + 1);
  if (!r) {
    fputs("probe: out of memory\n", stderr);
    exit(70);
  }
  size_t w = 0;
  for (size_t i = 0; i < n; i++) {
    if (s[i] == '%' && i + 2 < n) {
      char hex[3] = {s[i + 1], s[i + 2], 0};
      char *end = NULL;
      long v = strtol(hex, &end, 16);
      if (end && *end == 0) {
        r[w++] = (char)v;
        i += 2;
        continue;
      }
    }
    r[w++] = s[i];
  }
  r[w] = 0;
  *out_len = w;
  return r;
}

static int split_tabs(char *line, char **fields, int max) {
  int n = 0;
  char *p = line;
  while (n < max) {
    fields[n++] = p;
    char *tab = strchr(p, '\t');
    if (!tab)
      break;
    *tab = 0;
    p = tab + 1;
  }
  return n;
}

/* --------------------------------------------------------------- corpus */

static char *g_corpus_dir = NULL;

/* Documents live in a private directory and are addressed by index so the case
 * list stays small and the document bytes never round-trip through the shell. */
static char *load_doc(int index, size_t *out_len) {
  char path[4096];
  snprintf(path, sizeof(path), "%s/%05d.md", g_corpus_dir, index);
  FILE *f = fopen(path, "rb");
  if (!f)
    return NULL;
  buf b = {0};
  char chunk[65536];
  size_t got;
  while ((got = fread(chunk, 1, sizeof(chunk), f)) > 0)
    bput(&b, chunk, got);
  fclose(f);
  bgrow(&b, 1);
  b.p[b.n] = 0;
  *out_len = b.n;
  return b.p ? b.p : calloc(1, 1);
}

/* -------------------------------------------------------------- options */

/* Options are named rather than numeric.  The numeric values are part of the
 * ABI and checked separately; using names here means a mistake in one place
 * cannot silently mask a mistake in the other. */
static int parse_options(const char *spec, int *out) {
  int options = CMARK_OPT_DEFAULT;
  if (!spec || !*spec || strcmp(spec, "default") == 0 ||
      strcmp(spec, "none") == 0) {
    *out = options;
    return 1;
  }
  char *copy = strdup(spec);
  if (!copy)
    return 0;
  int ok = 1;
  for (char *tok = strtok(copy, ","); tok; tok = strtok(NULL, ",")) {
    if (strcmp(tok, "sourcepos") == 0)
      options |= CMARK_OPT_SOURCEPOS;
    else if (strcmp(tok, "hardbreaks") == 0)
      options |= CMARK_OPT_HARDBREAKS;
    else if (strcmp(tok, "safe") == 0)
      options |= CMARK_OPT_SAFE;
    else if (strcmp(tok, "unsafe") == 0)
      options |= CMARK_OPT_UNSAFE;
    else if (strcmp(tok, "nobreaks") == 0)
      options |= CMARK_OPT_NOBREAKS;
    else if (strcmp(tok, "normalize") == 0)
      options |= CMARK_OPT_NORMALIZE;
    else if (strcmp(tok, "validate_utf8") == 0)
      options |= CMARK_OPT_VALIDATE_UTF8;
    else if (strcmp(tok, "smart") == 0)
      options |= CMARK_OPT_SMART;
    else if (strcmp(tok, "default") == 0 || strcmp(tok, "none") == 0)
      ;
    else
      ok = 0;
  }
  free(copy);
  *out = options;
  return ok;
}

/* --------------------------------------------------------------- naming */

static const char *type_name(cmark_node_type t) {
  switch (t) {
  case CMARK_NODE_NONE: return "NONE";
  case CMARK_NODE_DOCUMENT: return "DOCUMENT";
  case CMARK_NODE_BLOCK_QUOTE: return "BLOCK_QUOTE";
  case CMARK_NODE_LIST: return "LIST";
  case CMARK_NODE_ITEM: return "ITEM";
  case CMARK_NODE_CODE_BLOCK: return "CODE_BLOCK";
  case CMARK_NODE_HTML_BLOCK: return "HTML_BLOCK";
  case CMARK_NODE_CUSTOM_BLOCK: return "CUSTOM_BLOCK";
  case CMARK_NODE_PARAGRAPH: return "PARAGRAPH";
  case CMARK_NODE_HEADING: return "HEADING";
  case CMARK_NODE_THEMATIC_BREAK: return "THEMATIC_BREAK";
  case CMARK_NODE_TEXT: return "TEXT";
  case CMARK_NODE_SOFTBREAK: return "SOFTBREAK";
  case CMARK_NODE_LINEBREAK: return "LINEBREAK";
  case CMARK_NODE_CODE: return "CODE";
  case CMARK_NODE_HTML_INLINE: return "HTML_INLINE";
  case CMARK_NODE_CUSTOM_INLINE: return "CUSTOM_INLINE";
  case CMARK_NODE_EMPH: return "EMPH";
  case CMARK_NODE_STRONG: return "STRONG";
  case CMARK_NODE_LINK: return "LINK";
  case CMARK_NODE_IMAGE: return "IMAGE";
  default: return "UNKNOWN";
  }
}

static cmark_node_type type_by_name(const char *name) {
  for (int t = CMARK_NODE_NONE; t <= CMARK_NODE_IMAGE; t++)
    if (strcmp(type_name((cmark_node_type)t), name) == 0)
      return (cmark_node_type)t;
  return (cmark_node_type)-1;
}

static const char *event_name(cmark_event_type e) {
  switch (e) {
  case CMARK_EVENT_NONE: return "NONE";
  case CMARK_EVENT_DONE: return "DONE";
  case CMARK_EVENT_ENTER: return "ENTER";
  case CMARK_EVENT_EXIT: return "EXIT";
  default: return "UNKNOWN";
  }
}

static const char *list_type_name(cmark_list_type t) {
  switch (t) {
  case CMARK_NO_LIST: return "NO_LIST";
  case CMARK_BULLET_LIST: return "BULLET";
  case CMARK_ORDERED_LIST: return "ORDERED";
  default: return "UNKNOWN";
  }
}

static const char *delim_name(cmark_delim_type t) {
  switch (t) {
  case CMARK_NO_DELIM: return "NO_DELIM";
  case CMARK_PERIOD_DELIM: return "PERIOD";
  case CMARK_PAREN_DELIM: return "PAREN";
  default: return "UNKNOWN";
  }
}

/* Text may be NULL or contain bytes that would break the line-oriented dump
 * format, so every string in a dump is escaped. */
static void put_escaped(buf *b, const char *s) {
  if (!s) {
    bputs(b, "<null>");
    return;
  }
  for (const unsigned char *p = (const unsigned char *)s; *p; p++) {
    if (*p == '\\')
      bputs(b, "\\\\");
    else if (*p == '\n')
      bputs(b, "\\n");
    else if (*p == '\r')
      bputs(b, "\\r");
    else if (*p == '\t')
      bputs(b, "\\t");
    else if (*p < 0x20 || *p == 0x7f)
      bprintf(b, "\\x%02x", *p);
    else
      bputc_(b, (char)*p);
  }
}

/* ----------------------------------------------------------- tree dumps */

/* One line per node holding every public getter.  This is the structural
 * counterpart to the rendered-output comparison: two implementations can agree
 * on HTML while disagreeing about the tree (wrong source positions, dropped
 * fence info, a list that reports itself loose), and this catches that. */
static void dump_node_line(buf *b, cmark_node *n, cmark_event_type ev) {
  bprintf(b, "%s %s", event_name(ev), type_name(cmark_node_get_type(n)));
  bputs(b, " ts=");
  put_escaped(b, cmark_node_get_type_string(n));
  bprintf(b, " sl=%d sc=%d el=%d ec=%d", cmark_node_get_start_line(n),
          cmark_node_get_start_column(n), cmark_node_get_end_line(n),
          cmark_node_get_end_column(n));
  bprintf(b, " h=%d", cmark_node_get_heading_level(n));
  bprintf(b, " lt=%s", list_type_name(cmark_node_get_list_type(n)));
  bprintf(b, " ld=%s", delim_name(cmark_node_get_list_delim(n)));
  bprintf(b, " ls=%d tight=%d", cmark_node_get_list_start(n),
          cmark_node_get_list_tight(n));
  bputs(b, " lit=");
  put_escaped(b, cmark_node_get_literal(n));
  bputs(b, " url=");
  put_escaped(b, cmark_node_get_url(n));
  bputs(b, " title=");
  put_escaped(b, cmark_node_get_title(n));
  bputs(b, " fence=");
  put_escaped(b, cmark_node_get_fence_info(n));
  bputs(b, " onenter=");
  put_escaped(b, cmark_node_get_on_enter(n));
  bputs(b, " onexit=");
  put_escaped(b, cmark_node_get_on_exit(n));
  bputc_(b, '\n');
}

static int dump_tree(buf *b, cmark_node *root) {
  if (!root) {
    bputs(b, "<null root>\n");
    return 0;
  }
  cmark_iter *iter = cmark_iter_new(root);
  if (!iter) {
    bputs(b, "<null iter>\n");
    return 0;
  }
  int count = 0;
  cmark_event_type ev;
  while ((ev = cmark_iter_next(iter)) != CMARK_EVENT_DONE) {
    cmark_node *node = cmark_iter_get_node(iter);
    if (!node) {
      bputs(b, "<null node>\n");
      break;
    }
    dump_node_line(b, node, ev);
    count++;
    if (count > 200000)
      break;
  }
  bprintf(b, "events=%d done=%s root_match=%d\n", count,
          event_name(ev), cmark_iter_get_root(iter) == root);
  cmark_iter_free(iter);
  return count;
}

/* Structure only, via the navigation accessors rather than the iterator.  The
 * two must agree; a rewrite whose sibling links are inconsistent with its
 * iterator shows up as a mismatch between this dump and the one above. */
static void dump_links(buf *b, cmark_node *node, int depth) {
  for (cmark_node *cur = node; cur; cur = cmark_node_next(cur)) {
    for (int i = 0; i < depth; i++)
      bputs(b, "  ");
    cmark_node *parent = cmark_node_parent(cur);
    cmark_node *prev = cmark_node_previous(cur);
    bprintf(b, "%s parent=%s prev=%s first=%s last=%s\n",
            type_name(cmark_node_get_type(cur)),
            parent ? type_name(cmark_node_get_type(parent)) : "-",
            prev ? type_name(cmark_node_get_type(prev)) : "-",
            cmark_node_first_child(cur)
                ? type_name(cmark_node_get_type(cmark_node_first_child(cur)))
                : "-",
            cmark_node_last_child(cur)
                ? type_name(cmark_node_get_type(cmark_node_last_child(cur)))
                : "-");
    if (depth < 64 && cmark_node_first_child(cur))
      dump_links(b, cmark_node_first_child(cur), depth + 1);
  }
}

/* --------------------------------------------------------------- render */

static char *render_by_name(const char *format, cmark_node *doc, int options,
                            int width, int *known) {
  /* Every writer reads `root->mem` before checking it, so a NULL root is
   * undefined rather than an error the library reports.  Refuse it here so no
   * call site can reach that state: `*known` stays set, and the caller prints
   * its normal "no output" branch on both sides of the comparison. */
  if (doc == NULL) {
    *known = 1;
    return NULL;
  }
  *known = 1;
  if (strcmp(format, "html") == 0)
    return cmark_render_html(doc, options);
  if (strcmp(format, "xml") == 0)
    return cmark_render_xml(doc, options);
  if (strcmp(format, "man") == 0)
    return cmark_render_man(doc, options, width);
  if (strcmp(format, "latex") == 0)
    return cmark_render_latex(doc, options, width);
  if (strcmp(format, "commonmark") == 0)
    return cmark_render_commonmark(doc, options, width);
  *known = 0;
  return NULL;
}

/* Rendered buffers come from the library's allocator, so they go back to the
 * matching free.  Using the wrong deallocator here would be a bug in the
 * probe, and on a submission with a different allocator it would look like a
 * submission bug. */
static void render_free(char *p) {
  if (!p)
    return;
  cmark_mem *mem = cmark_get_default_mem_allocator();
  if (mem && mem->free)
    mem->free(p);
  else
    free(p);
}

/* --------------------------------------------------------------- sweeps */

/* cmark's NULL tolerance is not uniform, and the split is not documented, so it
 * was measured against the pinned release: of 60 entry points called with a NULL
 * node, 46 return a defined value and 14 dereference before testing.  The
 * crashing 14 are node_unlink, node_free, the five writers, the five iterator
 * accessors, cmark_parser_free and cmark_parser_finish.  Only the tolerant 46
 * appear below.  Grading the other 14 would mean grading a segfault: a rewrite
 * that returns quietly and one that panics are both defensible, and neither can
 * be called wrong, so those inputs are refused by the interpreter instead. */

/* Every public getter applied to NULL.  cmark documents these as NULL-tolerant
 * and callers rely on it; a rewrite that translates them into Rust references
 * without keeping the guard turns a defined return value into a crash. */
static void sweep_null_getters(buf *b) {
  bprintf(b, "type=%s\n", type_name(cmark_node_get_type(NULL)));
  bputs(b, "type_string=");
  put_escaped(b, cmark_node_get_type_string(NULL));
  bputc_(b, '\n');
  bputs(b, "literal=");
  put_escaped(b, cmark_node_get_literal(NULL));
  bputc_(b, '\n');
  bputs(b, "url=");
  put_escaped(b, cmark_node_get_url(NULL));
  bputc_(b, '\n');
  bputs(b, "title=");
  put_escaped(b, cmark_node_get_title(NULL));
  bputc_(b, '\n');
  bputs(b, "fence_info=");
  put_escaped(b, cmark_node_get_fence_info(NULL));
  bputc_(b, '\n');
  bputs(b, "on_enter=");
  put_escaped(b, cmark_node_get_on_enter(NULL));
  bputc_(b, '\n');
  bputs(b, "on_exit=");
  put_escaped(b, cmark_node_get_on_exit(NULL));
  bputc_(b, '\n');
  bprintf(b, "heading_level=%d\n", cmark_node_get_heading_level(NULL));
  bprintf(b, "list_type=%s\n", list_type_name(cmark_node_get_list_type(NULL)));
  bprintf(b, "list_delim=%s\n", delim_name(cmark_node_get_list_delim(NULL)));
  bprintf(b, "list_start=%d\n", cmark_node_get_list_start(NULL));
  bprintf(b, "list_tight=%d\n", cmark_node_get_list_tight(NULL));
  bprintf(b, "start_line=%d\n", cmark_node_get_start_line(NULL));
  bprintf(b, "start_column=%d\n", cmark_node_get_start_column(NULL));
  bprintf(b, "end_line=%d\n", cmark_node_get_end_line(NULL));
  bprintf(b, "end_column=%d\n", cmark_node_get_end_column(NULL));
  bprintf(b, "user_data=%d\n", cmark_node_get_user_data(NULL) == NULL);
}

static void sweep_null_nav(buf *b) {
  bprintf(b, "next=%d\n", cmark_node_next(NULL) == NULL);
  bprintf(b, "previous=%d\n", cmark_node_previous(NULL) == NULL);
  bprintf(b, "parent=%d\n", cmark_node_parent(NULL) == NULL);
  bprintf(b, "first_child=%d\n", cmark_node_first_child(NULL) == NULL);
  bprintf(b, "last_child=%d\n", cmark_node_last_child(NULL) == NULL);
}

/* Setters and structural mutators with NULL operands must report failure, not
 * apply a partial change. */
static void sweep_null_setters(buf *b) {
  bprintf(b, "set_literal=%d\n", cmark_node_set_literal(NULL, "x"));
  bprintf(b, "set_url=%d\n", cmark_node_set_url(NULL, "x"));
  bprintf(b, "set_title=%d\n", cmark_node_set_title(NULL, "x"));
  bprintf(b, "set_fence_info=%d\n", cmark_node_set_fence_info(NULL, "x"));
  bprintf(b, "set_on_enter=%d\n", cmark_node_set_on_enter(NULL, "x"));
  bprintf(b, "set_on_exit=%d\n", cmark_node_set_on_exit(NULL, "x"));
  bprintf(b, "set_heading_level=%d\n", cmark_node_set_heading_level(NULL, 2));
  bprintf(b, "set_list_type=%d\n",
          cmark_node_set_list_type(NULL, CMARK_BULLET_LIST));
  bprintf(b, "set_list_delim=%d\n",
          cmark_node_set_list_delim(NULL, CMARK_PERIOD_DELIM));
  bprintf(b, "set_list_start=%d\n", cmark_node_set_list_start(NULL, 3));
  bprintf(b, "set_list_tight=%d\n", cmark_node_set_list_tight(NULL, 1));
  bprintf(b, "set_user_data=%d\n", cmark_node_set_user_data(NULL, NULL));
}

static void sweep_null_structure(buf *b) {
  cmark_node *node = cmark_node_new(CMARK_NODE_PARAGRAPH);
  bprintf(b, "append_null_parent=%d\n", cmark_node_append_child(NULL, node));
  bprintf(b, "append_null_child=%d\n", cmark_node_append_child(node, NULL));
  bprintf(b, "prepend_null_parent=%d\n", cmark_node_prepend_child(NULL, node));
  bprintf(b, "prepend_null_child=%d\n", cmark_node_prepend_child(node, NULL));
  bprintf(b, "insert_before_null_a=%d\n", cmark_node_insert_before(NULL, node));
  bprintf(b, "insert_before_null_b=%d\n", cmark_node_insert_before(node, NULL));
  bprintf(b, "insert_after_null_a=%d\n", cmark_node_insert_after(NULL, node));
  bprintf(b, "insert_after_null_b=%d\n", cmark_node_insert_after(node, NULL));
  bprintf(b, "replace_null_a=%d\n", cmark_node_replace(NULL, node));
  bprintf(b, "replace_null_b=%d\n", cmark_node_replace(node, NULL));
  bprintf(b, "check_null=%d\n", cmark_node_check(NULL, NULL));
  cmark_node_free(node);
  /* cmark_node_free(NULL) is deliberately NOT exercised.  The C implementation
   * writes through the pointer before testing it (`node->next = NULL` in
   * cmark_node_free, then `e->mem` in S_free_nodes), so passing NULL is
   * undefined and segfaults.  A rewrite that returns quietly, and one that
   * aborts, are both defensible; neither can be called wrong, so the case is
   * not graded.  The same reasoning applies to every NULL argument omitted
   * below: this sweep asserts the tolerances cmark actually documents by
   * implementing them, not the ones it happens to crash on. */
}

static void sweep_null_iter(buf *b) {
  bprintf(b, "iter_new_null=%d\n", cmark_iter_new(NULL) == NULL);
  /* The five writers all read `root->mem` as their first statement, so
   * cmark_render_html(NULL, 0) and its siblings are undefined for a NULL root.
   * Their NULL handling is exercised through iterators over real trees instead;
   * see the iterator and renderer families. */
}

/* Setters applied to the wrong node type must fail and leave the node alone.
 * This is the property that keeps a tree well-formed under API editing, and it
 * is invisible in rendered output until something has already gone wrong. */
static void sweep_type_errors(buf *b) {
  for (int t = CMARK_NODE_NONE; t <= CMARK_NODE_IMAGE; t++) {
    cmark_node *node = cmark_node_new((cmark_node_type)t);
    if (!node) {
      bprintf(b, "%s create=fail\n", type_name((cmark_node_type)t));
      continue;
    }
    bprintf(b, "%s", type_name((cmark_node_type)t));
    bprintf(b, " lit=%d", cmark_node_set_literal(node, "L"));
    bprintf(b, " url=%d", cmark_node_set_url(node, "U"));
    bprintf(b, " title=%d", cmark_node_set_title(node, "T"));
    bprintf(b, " fence=%d", cmark_node_set_fence_info(node, "F"));
    bprintf(b, " onenter=%d", cmark_node_set_on_enter(node, "E"));
    bprintf(b, " onexit=%d", cmark_node_set_on_exit(node, "X"));
    bprintf(b, " head=%d", cmark_node_set_heading_level(node, 3));
    bprintf(b, " ltype=%d",
            cmark_node_set_list_type(node, CMARK_ORDERED_LIST));
    bprintf(b, " ldelim=%d",
            cmark_node_set_list_delim(node, CMARK_PAREN_DELIM));
    bprintf(b, " lstart=%d", cmark_node_set_list_start(node, 5));
    bprintf(b, " ltight=%d", cmark_node_set_list_tight(node, 1));
    /* Read everything back: a setter that returned failure must not have
     * changed anything, and one that returned success must have. */
    bputs(b, " |");
    bputs(b, " lit=");
    put_escaped(b, cmark_node_get_literal(node));
    bputs(b, " url=");
    put_escaped(b, cmark_node_get_url(node));
    bputs(b, " title=");
    put_escaped(b, cmark_node_get_title(node));
    bputs(b, " fence=");
    put_escaped(b, cmark_node_get_fence_info(node));
    bprintf(b, " head=%d", cmark_node_get_heading_level(node));
    bprintf(b, " ltype=%s", list_type_name(cmark_node_get_list_type(node)));
    bprintf(b, " ldelim=%s", delim_name(cmark_node_get_list_delim(node)));
    bprintf(b, " lstart=%d", cmark_node_get_list_start(node));
    bprintf(b, " ltight=%d", cmark_node_get_list_tight(node));
    bputc_(b, '\n');
    cmark_node_free(node);
  }
}

/* Heading levels and list starts have documented valid ranges. */
static void sweep_bounds(buf *b) {
  static const int levels[] = {-2147483647, -1, 0, 1, 2, 3, 4, 5, 6, 7, 100,
                               2147483647};
  cmark_node *heading = cmark_node_new(CMARK_NODE_HEADING);
  for (size_t i = 0; i < sizeof(levels) / sizeof(levels[0]); i++) {
    int rc = cmark_node_set_heading_level(heading, levels[i]);
    bprintf(b, "heading %d rc=%d got=%d\n", levels[i], rc,
            cmark_node_get_heading_level(heading));
  }
  cmark_node_free(heading);
  cmark_node *list = cmark_node_new(CMARK_NODE_LIST);
  for (size_t i = 0; i < sizeof(levels) / sizeof(levels[0]); i++) {
    int rc = cmark_node_set_list_start(list, levels[i]);
    bprintf(b, "liststart %d rc=%d got=%d\n", levels[i], rc,
            cmark_node_get_list_start(list));
  }
  bprintf(b, "tight_neg rc=%d got=%d\n", cmark_node_set_list_tight(list, -5),
          cmark_node_get_list_tight(list));
  bprintf(b, "tight_two rc=%d got=%d\n", cmark_node_set_list_tight(list, 2),
          cmark_node_get_list_tight(list));
  cmark_node_free(list);
}

/* --------------------------------------------------- accounting allocator */

/* A custom cmark_mem whose only job is to prove it was actually used.
 *
 * Raw allocation counts are NOT compared between reference and submission: how
 * many times an implementation calls malloc is an internal choice, and a Rust
 * rewrite has every right to differ.  What the interface promises is narrower
 * and is what gets compared: an allocator handed to the parser really does
 * back the whole document, buffers handed back to the caller are freeable
 * through it, and once the caller has released everything nothing is left
 * outstanding. */
typedef struct {
  long allocs, frees, reallocs;
  long live;
} memstat;

static memstat g_ms;

static void *ms_calloc(size_t n, size_t size) {
  void *p = calloc(n, size);
  if (!p) {
    fputs("probe: allocator exhausted\n", stderr);
    exit(70);
  }
  g_ms.allocs++;
  g_ms.live++;
  return p;
}

static void *ms_realloc(void *old, size_t size) {
  void *p = realloc(old, size);
  if (!p && size) {
    fputs("probe: allocator exhausted\n", stderr);
    exit(70);
  }
  g_ms.reallocs++;
  if (!old)
    g_ms.live++;
  return p;
}

static void ms_free(void *p) {
  if (!p)
    return;
  g_ms.frees++;
  g_ms.live--;
  free(p);
}

static cmark_mem g_ms_mem = {ms_calloc, ms_realloc, ms_free};

static void ms_reset(void) { memset(&g_ms, 0, sizeof(g_ms)); }

/* ---------------------------------------------------------- interpreter */

/* Registers hold nodes; scripts are `cmd:arg:arg;cmd:arg` strings supplied by
 * the case catalog.  Every command appends its own outcome to the transcript,
 * so the payload is a complete record of what happened rather than just a
 * final value -- when a submission diverges, the diff points at the exact
 * operation instead of at the end state. */
typedef struct {
  cmark_node *regs[MAX_REGS];
  buf *out;
} vm;

static int reg_index(const char *s) {
  char *end = NULL;
  long v = strtol(s, &end, 10);
  if (!end || *end || v < 0 || v >= MAX_REGS)
    return -1;
  return (int)v;
}

static cmark_node *reg_get(vm *m, const char *s) {
  int i = reg_index(s);
  return i < 0 ? NULL : m->regs[i];
}

static int arg_int(const char *s, int fallback) {
  if (!s)
    return fallback;
  char *end = NULL;
  long v = strtol(s, &end, 10);
  if (!end || *end)
    return fallback;
  return (int)v;
}

#define NEED(n)                                                                \
  do {                                                                         \
    if (argc < (n)) {                                                          \
      bputs(m->out, " -> ERR:argc\n");                                         \
      return 1;                                                                \
    }                                                                          \
  } while (0)

/* Returns 1 to continue the script, 0 to stop it. */
static int vm_step(vm *m, char **argv, int argc) {
  const char *cmd = argv[0];
  buf *b = m->out;
  bputs(b, cmd);
  for (int i = 1; i < argc; i++) {
    bputc_(b, ':');
    bputs(b, argv[i]);
  }

  if (strcmp(cmd, "new") == 0) {
    NEED(3);
    int r = reg_index(argv[1]);
    cmark_node_type t = type_by_name(argv[2]);
    if (r < 0 || (int)t < 0) {
      bputs(b, " -> ERR:arg\n");
      return 1;
    }
    m->regs[r] = cmark_node_new(t);
    bprintf(b, " -> %s\n", m->regs[r] ? "node" : "null");
    return 1;
  }
  if (strcmp(cmd, "newmem") == 0) {
    NEED(3);
    int r = reg_index(argv[1]);
    cmark_node_type t = type_by_name(argv[2]);
    if (r < 0 || (int)t < 0) {
      bputs(b, " -> ERR:arg\n");
      return 1;
    }
    m->regs[r] = cmark_node_new_with_mem(t, &g_ms_mem);
    bprintf(b, " -> %s\n", m->regs[r] ? "node" : "null");
    return 1;
  }
  if (strcmp(cmd, "parsedoc") == 0) {
    NEED(4);
    int r = reg_index(argv[1]);
    int options = 0;
    if (r < 0 || !parse_options(argv[3], &options)) {
      bputs(b, " -> ERR:arg\n");
      return 1;
    }
    size_t len = 0;
    char *doc = load_doc(arg_int(argv[2], -1), &len);
    if (!doc) {
      bputs(b, " -> ERR:corpus\n");
      return 1;
    }
    m->regs[r] = cmark_parse_document(doc, len, options);
    free(doc);
    bprintf(b, " -> %s\n", m->regs[r] ? "node" : "null");
    return 1;
  }
  if (strcmp(cmd, "parsetext") == 0) {
    NEED(4);
    int r = reg_index(argv[1]);
    int options = 0;
    if (r < 0 || !parse_options(argv[3], &options)) {
      bputs(b, " -> ERR:arg\n");
      return 1;
    }
    size_t len = 0;
    char *text = pctdecode(argv[2], &len);
    m->regs[r] = cmark_parse_document(text, len, options);
    free(text);
    bprintf(b, " -> %s\n", m->regs[r] ? "node" : "null");
    return 1;
  }
  if (strcmp(cmd, "free") == 0) {
    NEED(2);
    int r = reg_index(argv[1]);
    if (r < 0) {
      bputs(b, " -> ERR:arg\n");
      return 1;
    }
    /* Freeing an empty register would mean calling cmark_node_free(NULL), which
     * the C implementation does not guard.  Report it instead: the transcript
     * still records that the script reached this point, and both sides of the
     * comparison take the same branch. */
    if (m->regs[r] == NULL) {
      bputs(b, " -> ERR:null\n");
      return 1;
    }
    cmark_node_free(m->regs[r]);
    m->regs[r] = NULL;
    bputs(b, " -> done\n");
    return 1;
  }
  return -1; /* not handled here */
}

/* Structural mutation.  Each command reports its return code, and the catalog
 * pairs these with a `dump` so the resulting tree is compared too -- a
 * mutator that returns success while corrupting sibling links fails on the
 * dump even though it passed on the return code. */
static int vm_step_struct(vm *m, char **argv, int argc) {
  const char *cmd = argv[0];
  buf *b = m->out;

  if (strcmp(cmd, "append") == 0) {
    NEED(3);
    bprintf(b, " -> %d\n",
            cmark_node_append_child(reg_get(m, argv[1]), reg_get(m, argv[2])));
    return 1;
  }
  if (strcmp(cmd, "prepend") == 0) {
    NEED(3);
    bprintf(b, " -> %d\n",
            cmark_node_prepend_child(reg_get(m, argv[1]), reg_get(m, argv[2])));
    return 1;
  }
  if (strcmp(cmd, "before") == 0) {
    NEED(3);
    bprintf(b, " -> %d\n",
            cmark_node_insert_before(reg_get(m, argv[1]), reg_get(m, argv[2])));
    return 1;
  }
  if (strcmp(cmd, "after") == 0) {
    NEED(3);
    bprintf(b, " -> %d\n",
            cmark_node_insert_after(reg_get(m, argv[1]), reg_get(m, argv[2])));
    return 1;
  }
  if (strcmp(cmd, "replace") == 0) {
    NEED(3);
    bprintf(b, " -> %d\n",
            cmark_node_replace(reg_get(m, argv[1]), reg_get(m, argv[2])));
    return 1;
  }
  if (strcmp(cmd, "unlink") == 0) {
    NEED(2);
    /* cmark_node_unlink(NULL) dereferences before testing, like node_free. */
    if (reg_get(m, argv[1]) == NULL) {
      bputs(b, " -> ERR:null\n");
      return 1;
    }
    cmark_node_unlink(reg_get(m, argv[1]));
    bputs(b, " -> done\n");
    return 1;
  }
  if (strcmp(cmd, "consolidate") == 0) {
    NEED(2);
    cmark_consolidate_text_nodes(reg_get(m, argv[1]));
    bputs(b, " -> done\n");
    return 1;
  }
  if (strcmp(cmd, "check") == 0) {
    NEED(2);
    /* Only the error count is compared.  The FILE* report includes node
     * addresses, which are not reproducible even between two runs of the same
     * binary, so it is written to /dev/null. */
    FILE *sink = fopen("/dev/null", "w");
    bprintf(b, " -> %d\n", cmark_node_check(reg_get(m, argv[1]), sink));
    if (sink)
      fclose(sink);
    return 1;
  }
  /* Navigation: move a register to a related node. */
  if (strcmp(cmd, "nav") == 0) {
    NEED(4);
    int dst = reg_index(argv[1]);
    cmark_node *src = reg_get(m, argv[2]);
    if (dst < 0) {
      bputs(b, " -> ERR:arg\n");
      return 1;
    }
    const char *dir = argv[3];
    cmark_node *target = NULL;
    if (strcmp(dir, "next") == 0)
      target = cmark_node_next(src);
    else if (strcmp(dir, "prev") == 0)
      target = cmark_node_previous(src);
    else if (strcmp(dir, "parent") == 0)
      target = cmark_node_parent(src);
    else if (strcmp(dir, "first") == 0)
      target = cmark_node_first_child(src);
    else if (strcmp(dir, "last") == 0)
      target = cmark_node_last_child(src);
    else {
      bputs(b, " -> ERR:dir\n");
      return 1;
    }
    m->regs[dst] = target;
    bprintf(b, " -> %s\n", target ? type_name(cmark_node_get_type(target))
                                  : "null");
    return 1;
  }
  return -1;
}

static int vm_step_set(vm *m, char **argv, int argc) {
  const char *cmd = argv[0];
  buf *b = m->out;
  cmark_node *node = argc > 1 ? reg_get(m, argv[1]) : NULL;

  if (strcmp(cmd, "setlit") == 0 || strcmp(cmd, "seturl") == 0 ||
      strcmp(cmd, "settitle") == 0 || strcmp(cmd, "setfence") == 0 ||
      strcmp(cmd, "setonenter") == 0 || strcmp(cmd, "setonexit") == 0) {
    NEED(3);
    size_t len = 0;
    char *text = pctdecode(argv[2], &len);
    int rc;
    if (strcmp(cmd, "setlit") == 0)
      rc = cmark_node_set_literal(node, text);
    else if (strcmp(cmd, "seturl") == 0)
      rc = cmark_node_set_url(node, text);
    else if (strcmp(cmd, "settitle") == 0)
      rc = cmark_node_set_title(node, text);
    else if (strcmp(cmd, "setfence") == 0)
      rc = cmark_node_set_fence_info(node, text);
    else if (strcmp(cmd, "setonenter") == 0)
      rc = cmark_node_set_on_enter(node, text);
    else
      rc = cmark_node_set_on_exit(node, text);
    free(text);
    bprintf(b, " -> %d\n", rc);
    return 1;
  }
  if (strcmp(cmd, "sethead") == 0) {
    NEED(3);
    bprintf(b, " -> %d\n",
            cmark_node_set_heading_level(node, arg_int(argv[2], 0)));
    return 1;
  }
  if (strcmp(cmd, "setltype") == 0) {
    NEED(3);
    cmark_list_type t = CMARK_NO_LIST;
    if (strcmp(argv[2], "BULLET") == 0)
      t = CMARK_BULLET_LIST;
    else if (strcmp(argv[2], "ORDERED") == 0)
      t = CMARK_ORDERED_LIST;
    else if (strcmp(argv[2], "BAD") == 0)
      t = (cmark_list_type)99;
    bprintf(b, " -> %d\n", cmark_node_set_list_type(node, t));
    return 1;
  }
  if (strcmp(cmd, "setldelim") == 0) {
    NEED(3);
    cmark_delim_type t = CMARK_NO_DELIM;
    if (strcmp(argv[2], "PERIOD") == 0)
      t = CMARK_PERIOD_DELIM;
    else if (strcmp(argv[2], "PAREN") == 0)
      t = CMARK_PAREN_DELIM;
    else if (strcmp(argv[2], "BAD") == 0)
      t = (cmark_delim_type)99;
    bprintf(b, " -> %d\n", cmark_node_set_list_delim(node, t));
    return 1;
  }
  if (strcmp(cmd, "setlstart") == 0) {
    NEED(3);
    bprintf(b, " -> %d\n", cmark_node_set_list_start(node, arg_int(argv[2], 0)));
    return 1;
  }
  if (strcmp(cmd, "setltight") == 0) {
    NEED(3);
    bprintf(b, " -> %d\n", cmark_node_set_list_tight(node, arg_int(argv[2], 0)));
    return 1;
  }
  /* User data is an opaque caller-owned pointer.  Its value is never printed
   * (addresses are not reproducible); what is compared is whether the same
   * pointer comes back out. */
  if (strcmp(cmd, "setuserdata") == 0) {
    NEED(3);
    static char slots[4];
    int slot = arg_int(argv[2], 0) & 3;
    void *value = arg_int(argv[2], 0) < 0 ? NULL : (void *)&slots[slot];
    int rc = cmark_node_set_user_data(node, value);
    bprintf(b, " -> rc=%d roundtrip=%d\n", rc,
            cmark_node_get_user_data(node) == value);
    return 1;
  }
  return -1;
}

static int vm_step_emit(vm *m, char **argv, int argc) {
  const char *cmd = argv[0];
  buf *b = m->out;
  cmark_node *node = argc > 1 ? reg_get(m, argv[1]) : NULL;

  if (strcmp(cmd, "dump") == 0) {
    NEED(2);
    bputs(b, " ->\n");
    dump_tree(b, node);
    return 1;
  }
  if (strcmp(cmd, "links") == 0) {
    NEED(2);
    bputs(b, " ->\n");
    if (node)
      dump_links(b, node, 0);
    else
      bputs(b, "<null>\n");
    return 1;
  }
  if (strcmp(cmd, "render") == 0) {
    NEED(3);
    int options = 0, width = arg_int(argc > 4 ? argv[4] : NULL, 0), known = 0;
    if (!parse_options(argc > 3 ? argv[3] : "default", &options)) {
      bputs(b, " -> ERR:options\n");
      return 1;
    }
    char *text = render_by_name(argv[2], node, options, width, &known);
    if (!known) {
      bputs(b, " -> ERR:format\n");
      return 1;
    }
    if (!text) {
      bputs(b, " -> null\n");
      return 1;
    }
    bprintf(b, " -> %zu bytes\n", strlen(text));
    bputs(b, text);
    if (b->n && b->p[b->n - 1] != '\n')
      bputc_(b, '\n');
    render_free(text);
    return 1;
  }
  if (strcmp(cmd, "getall") == 0) {
    NEED(2);
    bputs(b, " ->\n");
    if (!node) {
      bputs(b, "<null>\n");
      return 1;
    }
    dump_node_line(b, node, CMARK_EVENT_NONE);
    return 1;
  }
  if (strcmp(cmd, "type") == 0) {
    NEED(2);
    bprintf(b, " -> %s ts=%s\n",
            node ? type_name(cmark_node_get_type(node)) : "null",
            node && cmark_node_get_type_string(node)
                ? cmark_node_get_type_string(node)
                : "<null>");
    return 1;
  }
  if (strcmp(cmd, "memstat") == 0) {
    /* Deliberately not the raw counters: see the note on `memstat`. */
    bprintf(b, " -> used=%d balanced=%d\n", g_ms.allocs > 0, g_ms.live == 0);
    return 1;
  }
  if (strcmp(cmd, "memreset") == 0) {
    ms_reset();
    bputs(b, " -> done\n");
    return 1;
  }
  if (strcmp(cmd, "note") == 0) {
    bputs(b, " -> ok\n");
    return 1;
  }
  return -1;
}

#undef NEED

/* Split a script on ';' into commands, each split on ':' into fields. */
static void run_script(buf *out, const char *script) {
  vm m;
  memset(&m, 0, sizeof(m));
  m.out = out;
  char *copy = strdup(script);
  if (!copy) {
    bputs(out, "ERR:oom\n");
    return;
  }
  char *save_cmd = NULL;
  for (char *stmt = strtok_r(copy, ";", &save_cmd); stmt;
       stmt = strtok_r(NULL, ";", &save_cmd)) {
    if (!*stmt)
      continue;
    char *argv[MAX_FIELDS];
    int argc = 0;
    char *save_arg = NULL;
    for (char *tok = strtok_r(stmt, ":", &save_arg);
         tok && argc < MAX_FIELDS; tok = strtok_r(NULL, ":", &save_arg))
      argv[argc++] = tok;
    if (argc == 0)
      continue;
    int rc = vm_step(&m, argv, argc);
    if (rc == -1)
      rc = vm_step_struct(&m, argv, argc);
    if (rc == -1)
      rc = vm_step_set(&m, argv, argc);
    if (rc == -1)
      rc = vm_step_emit(&m, argv, argc);
    if (rc == -1) {
      bputs(out, " -> ERR:unknown-command\n");
      break;
    }
    if (rc == 0)
      break;
  }
  free(copy);
}

/* -------------------------------------------------------------- streaming */

/* Feeding a document in fixed-size chunks must produce the same tree as
 * handing it over in one piece.  Chunk boundaries that fall inside a fence
 * marker, a reference definition or a multi-byte character are exactly where a
 * rewrite's buffering differs from the reference's. */
static cmark_node *feed_chunks(const char *doc, size_t len, int options,
                               size_t chunk) {
  cmark_parser *parser = cmark_parser_new(options);
  if (!parser)
    return NULL;
  if (chunk == 0)
    chunk = len ? len : 1;
  for (size_t off = 0; off < len; off += chunk) {
    size_t n = len - off < chunk ? len - off : chunk;
    cmark_parser_feed(parser, doc + off, n);
  }
  if (len == 0)
    cmark_parser_feed(parser, "", 0);
  cmark_node *root = cmark_parser_finish(parser);
  cmark_parser_free(parser);
  return root;
}

/* ------------------------------------------------------------- operations */

static void op_iter_edit(buf *b, cmark_node *root, const char *mode) {
  /* The header documents that the current node may be modified on EXIT (or on
   * ENTER for a leaf).  This is the canonical tree-editing pattern for
   * consumers, so it has to keep working. */
  cmark_iter *iter = cmark_iter_new(root);
  if (!iter) {
    bputs(b, "null iter\n");
    return;
  }
  int touched = 0;
  cmark_event_type ev;
  while ((ev = cmark_iter_next(iter)) != CMARK_EVENT_DONE) {
    cmark_node *node = cmark_iter_get_node(iter);
    cmark_node_type t = cmark_node_get_type(node);
    if (strcmp(mode, "free_emph") == 0 && ev == CMARK_EVENT_EXIT &&
        t == CMARK_NODE_EMPH) {
      cmark_node_unlink(node);
      cmark_node_free(node);
      touched++;
    } else if (strcmp(mode, "replace_text") == 0 && ev == CMARK_EVENT_ENTER &&
               t == CMARK_NODE_TEXT) {
      cmark_node *code = cmark_node_new(CMARK_NODE_CODE);
      cmark_node_set_literal(code, cmark_node_get_literal(node));
      cmark_node_replace(node, code);
      cmark_node_free(node);
      touched++;
    } else if (strcmp(mode, "unlink_code") == 0 && ev == CMARK_EVENT_ENTER &&
               t == CMARK_NODE_CODE) {
      cmark_node_unlink(node);
      cmark_node_free(node);
      touched++;
    } else if (strcmp(mode, "strip_links") == 0 && ev == CMARK_EVENT_EXIT &&
               t == CMARK_NODE_LINK) {
      /* Hoist children out, then drop the link itself. */
      cmark_node *child;
      while ((child = cmark_node_first_child(node)) != NULL) {
        cmark_node_unlink(child);
        cmark_node_insert_before(node, child);
      }
      cmark_node_unlink(node);
      cmark_node_free(node);
      touched++;
    }
  }
  cmark_iter_free(iter);
  bprintf(b, "touched=%d\n", touched);
  char *html = cmark_render_html(root, CMARK_OPT_DEFAULT);
  if (html) {
    bputs(b, html);
    render_free(html);
  } else {
    bputs(b, "<null html>\n");
  }
}

static void op_iter_reset(buf *b, cmark_node *root, int skip) {
  cmark_iter *iter = cmark_iter_new(root);
  if (!iter) {
    bputs(b, "null iter\n");
    return;
  }
  cmark_node *mark = NULL;
  int seen = 0;
  cmark_event_type ev;
  while ((ev = cmark_iter_next(iter)) != CMARK_EVENT_DONE) {
    if (seen++ == skip) {
      mark = cmark_iter_get_node(iter);
      break;
    }
  }
  bprintf(b, "mark=%s\n",
          mark ? type_name(cmark_node_get_type(mark)) : "none");
  if (!mark) {
    cmark_iter_free(iter);
    return;
  }
  cmark_iter_reset(iter, mark, CMARK_EVENT_ENTER);
  bprintf(b, "after_reset_node=%s event=%s root=%d\n",
          type_name(cmark_node_get_type(cmark_iter_get_node(iter))),
          event_name(cmark_iter_get_event_type(iter)),
          cmark_iter_get_root(iter) == root);
  int count = 0;
  while ((ev = cmark_iter_next(iter)) != CMARK_EVENT_DONE) {
    cmark_node *node = cmark_iter_get_node(iter);
    bprintf(b, "%s %s\n", event_name(ev), type_name(cmark_node_get_type(node)));
    if (++count > 4096)
      break;
  }
  bprintf(b, "replayed=%d\n", count);
  cmark_iter_free(iter);
}

static void op_mem(buf *b, const char *doc, size_t len, int options) {
  /* Thread a custom allocator through the whole lifecycle and confirm the
   * interface contract: the allocator backs the parse, the rendered buffer is
   * freeable through it, and nothing is outstanding at the end. */
  ms_reset();
  cmark_parser *parser = cmark_parser_new_with_mem(options, &g_ms_mem);
  if (!parser) {
    bputs(b, "parser=null\n");
    return;
  }
  cmark_parser_feed(parser, doc, len);
  cmark_node *root = cmark_parser_finish(parser);
  long after_parse = g_ms.allocs;
  char *html = cmark_render_html(root, options);
  bprintf(b, "parser_used_mem=%d\n", after_parse > 0);
  bprintf(b, "html_len=%zu\n", html ? strlen(html) : (size_t)0);
  if (html)
    g_ms_mem.free(html);
  cmark_parser_free(parser);
  cmark_node_free(root);
  bprintf(b, "balanced=%d\n", g_ms.live == 0);
  bprintf(b, "default_mem_present=%d\n",
          cmark_get_default_mem_allocator() != NULL);
  cmark_mem *dm = cmark_get_default_mem_allocator();
  bprintf(b, "default_mem_fns=%d\n",
          dm && dm->calloc && dm->realloc && dm->free);
}

static void op_into_root(buf *b, const char *doc, size_t len, int options,
                         const char *root_type) {
  cmark_node_type t = type_by_name(root_type);
  if ((int)t < 0) {
    bputs(b, "ERR:type\n");
    return;
  }
  ms_reset();
  cmark_node *root = cmark_node_new_with_mem(t, &g_ms_mem);
  cmark_parser *parser =
      cmark_parser_new_with_mem_into_root(options, &g_ms_mem, root);
  if (!parser) {
    bputs(b, "parser=null\n");
    cmark_node_free(root);
    return;
  }
  cmark_parser_feed(parser, doc, len);
  cmark_node *result = cmark_parser_finish(parser);
  bprintf(b, "same_root=%d\n", result == root);
  dump_tree(b, result ? result : root);
  char *html = cmark_render_html(result ? result : root, options);
  if (html) {
    bputs(b, html);
    g_ms_mem.free(html);
  }
  cmark_parser_free(parser);
  cmark_node_free(result ? result : root);
  bprintf(b, "balanced=%d\n", g_ms.live == 0);
}

/* --------------------------------------------------------------- dispatch */

/* Resolve the document argument: either a corpus index or inline text. */
static char *case_document(char **f, int argc, int slot, size_t *len,
                           const char **err) {
  *err = NULL;
  if (argc <= slot) {
    *err = "ERR:missing-doc";
    return NULL;
  }
  if (strncmp(f[slot], "text=", 5) == 0)
    return pctdecode(f[slot] + 5, len);
  char *doc = load_doc(arg_int(f[slot], -1), len);
  if (!doc)
    *err = "ERR:corpus";
  return doc;
}

static int run_case(char **f, int argc, buf *out) {
  const char *op = f[1];
  const char *err = NULL;
  int options = 0;

  if (strcmp(op, "version") == 0) {
    /* The numeric version and its string are both part of the published
     * interface and are baked into downstream compile-time checks. */
    int v = cmark_version();
    bprintf(out, "version=%d\n", v);
    bprintf(out, "major=%d minor=%d patch=%d\n", (v >> 16) & 0xff,
            (v >> 8) & 0xff, v & 0xff);
    bputs(out, "version_string=");
    put_escaped(out, cmark_version_string());
    bputc_(out, '\n');
    bprintf(out, "header_version=%d\n", CMARK_VERSION);
    bputs(out, "header_version_string=");
    put_escaped(out, CMARK_VERSION_STRING);
    bputc_(out, '\n');
    bprintf(out, "header_matches_runtime=%d\n", v == CMARK_VERSION);
    return 1;
  }
  if (strcmp(op, "optbits") == 0) {
    /* Option values are ABI: a caller compiled against the old header passes
     * these integers to a newly built library. */
    bprintf(out, "DEFAULT=%d\n", CMARK_OPT_DEFAULT);
    bprintf(out, "SOURCEPOS=%d\n", CMARK_OPT_SOURCEPOS);
    bprintf(out, "HARDBREAKS=%d\n", CMARK_OPT_HARDBREAKS);
    bprintf(out, "SAFE=%d\n", CMARK_OPT_SAFE);
    bprintf(out, "UNSAFE=%d\n", CMARK_OPT_UNSAFE);
    bprintf(out, "NOBREAKS=%d\n", CMARK_OPT_NOBREAKS);
    bprintf(out, "NORMALIZE=%d\n", CMARK_OPT_NORMALIZE);
    bprintf(out, "VALIDATE_UTF8=%d\n", CMARK_OPT_VALIDATE_UTF8);
    bprintf(out, "SMART=%d\n", CMARK_OPT_SMART);
    return 1;
  }
  if (strcmp(op, "enumbits") == 0) {
    for (int t = CMARK_NODE_NONE; t <= CMARK_NODE_IMAGE; t++)
      bprintf(out, "node %d %s\n", t, type_name((cmark_node_type)t));
    bprintf(out, "first_block=%d last_block=%d\n", CMARK_NODE_FIRST_BLOCK,
            CMARK_NODE_LAST_BLOCK);
    bprintf(out, "first_inline=%d last_inline=%d\n", CMARK_NODE_FIRST_INLINE,
            CMARK_NODE_LAST_INLINE);
    bprintf(out, "list %d %d %d\n", CMARK_NO_LIST, CMARK_BULLET_LIST,
            CMARK_ORDERED_LIST);
    bprintf(out, "delim %d %d %d\n", CMARK_NO_DELIM, CMARK_PERIOD_DELIM,
            CMARK_PAREN_DELIM);
    bprintf(out, "event %d %d %d %d\n", CMARK_EVENT_NONE, CMARK_EVENT_DONE,
            CMARK_EVENT_ENTER, CMARK_EVENT_EXIT);
    bprintf(out, "sizeof_mem=%zu\n", sizeof(cmark_mem));
    return 1;
  }
  if (strcmp(op, "sweep") == 0) {
    if (argc < 3) {
      bputs(out, "ERR:missing-group\n");
      return 1;
    }
    const char *group = f[2];
    if (strcmp(group, "null_getters") == 0)
      sweep_null_getters(out);
    else if (strcmp(group, "null_nav") == 0)
      sweep_null_nav(out);
    else if (strcmp(group, "null_setters") == 0)
      sweep_null_setters(out);
    else if (strcmp(group, "null_structure") == 0)
      sweep_null_structure(out);
    else if (strcmp(group, "null_iter") == 0)
      sweep_null_iter(out);
    else if (strcmp(group, "type_errors") == 0)
      sweep_type_errors(out);
    else if (strcmp(group, "bounds") == 0)
      sweep_bounds(out);
    else
      bputs(out, "ERR:unknown-group\n");
    return 1;
  }
  if (strcmp(op, "script") == 0) {
    if (argc < 3) {
      bputs(out, "ERR:missing-script\n");
      return 1;
    }
    run_script(out, f[2]);
    return 1;
  }

  /* Everything below takes a document in field 2. */
  size_t len = 0;
  char *doc = case_document(f, argc, 2, &len, &err);
  if (err) {
    bputs(out, err);
    bputc_(out, '\n');
    free(doc);
    return 1;
  }

  if (strcmp(op, "md2html") == 0) {
    if (!parse_options(argc > 3 ? f[3] : "default", &options)) {
      bputs(out, "ERR:options\n");
      free(doc);
      return 1;
    }
    char *html = cmark_markdown_to_html(doc, len, options);
    if (html) {
      bputs(out, html);
      render_free(html);
    } else {
      bputs(out, "<null>\n");
    }
    free(doc);
    return 1;
  }

  if (!parse_options(argc > 4 ? f[4] : "default", &options)) {
    bputs(out, "ERR:options\n");
    free(doc);
    return 1;
  }
  int width = arg_int(argc > 5 ? f[5] : NULL, 0);
  size_t chunk = (size_t)arg_int(argc > 6 ? f[6] : NULL, 0);

  if (strcmp(op, "render") == 0) {
    cmark_node *root = feed_chunks(doc, len, options, chunk);
    int known = 0;
    char *text = render_by_name(f[3], root, options, width, &known);
    if (!known)
      bputs(out, "ERR:format\n");
    else if (!text)
      bputs(out, "<null>\n");
    else {
      bputs(out, text);
      render_free(text);
    }
    cmark_node_free(root);
    free(doc);
    return 1;
  }
  if (strcmp(op, "tree") == 0) {
    cmark_node *root = feed_chunks(doc, len, options, chunk);
    dump_tree(out, root);
    cmark_node_free(root);
    free(doc);
    return 1;
  }
  if (strcmp(op, "links") == 0) {
    cmark_node *root = feed_chunks(doc, len, options, chunk);
    if (root)
      dump_links(out, root, 0);
    else
      bputs(out, "<null>\n");
    cmark_node_free(root);
    free(doc);
    return 1;
  }
  if (strcmp(op, "parsefile") == 0) {
    /* cmark_parse_file takes a FILE*, a distinct entry point from the
     * buffer-based one, with its own internal read loop. */
    char path[4096];
    snprintf(path, sizeof(path), "%s/%05d.md", g_corpus_dir, arg_int(f[2], -1));
    FILE *fp = fopen(path, "rb");
    if (!fp) {
      bputs(out, "ERR:open\n");
      free(doc);
      return 1;
    }
    cmark_node *root = cmark_parse_file(fp, options);
    fclose(fp);
    char *html = cmark_render_html(root, options);
    if (html) {
      bputs(out, html);
      render_free(html);
    } else {
      bputs(out, "<null>\n");
    }
    cmark_node_free(root);
    free(doc);
    return 1;
  }
  if (strcmp(op, "consolidate") == 0) {
    cmark_node *root = feed_chunks(doc, len, options, chunk);
    dump_tree(out, root);
    bputs(out, "--- after consolidate ---\n");
    cmark_consolidate_text_nodes(root);
    dump_tree(out, root);
    char *html = cmark_render_html(root, options);
    if (html) {
      bputs(out, html);
      render_free(html);
    }
    cmark_node_free(root);
    free(doc);
    return 1;
  }
  if (strcmp(op, "itercheck") == 0) {
    cmark_node *root = feed_chunks(doc, len, options, chunk);
    bprintf(out, "check=%d\n", cmark_node_check(root, NULL));
    dump_tree(out, root);
    cmark_node_free(root);
    free(doc);
    return 1;
  }
  if (strcmp(op, "iteredit") == 0) {
    cmark_node *root = feed_chunks(doc, len, options, chunk);
    op_iter_edit(out, root, f[3]);
    cmark_node_free(root);
    free(doc);
    return 1;
  }
  if (strcmp(op, "iterreset") == 0) {
    cmark_node *root = feed_chunks(doc, len, options, chunk);
    op_iter_reset(out, root, arg_int(f[3], 0));
    cmark_node_free(root);
    free(doc);
    return 1;
  }
  if (strcmp(op, "mem") == 0) {
    op_mem(out, doc, len, options);
    free(doc);
    return 1;
  }
  if (strcmp(op, "intoroot") == 0) {
    op_into_root(out, doc, len, options, f[3]);
    free(doc);
    return 1;
  }
  free(doc);
  bputs(out, "ERR:unknown-op\n");
  return 1;
}

int main(int argc, char **argv) {
  if (argc < 2) {
    fputs("usage: probe <corpus-dir>\n", stderr);
    return 64;
  }
  g_corpus_dir = argv[1];
  setvbuf(stdout, NULL, _IOFBF, 1 << 16);

  char *line = malloc(MAX_LINE);
  if (!line)
    return 70;
  while (fgets(line, MAX_LINE, stdin)) {
    size_t n = strlen(line);
    while (n && (line[n - 1] == '\n' || line[n - 1] == '\r'))
      line[--n] = 0;
    if (!n || line[0] == '#')
      continue;
    char *fields[MAX_FIELDS];
    int nf = split_tabs(line, fields, MAX_FIELDS);
    if (nf < 2) {
      emit(nf ? fields[0] : "?", "malformed", NULL);
      continue;
    }
    buf out = {0};
    run_case(fields, nf, &out);
    emit(fields[0], "ok", &out);
    bfree(&out);
  }
  free(line);
  fflush(stdout);
  return 0;
}
