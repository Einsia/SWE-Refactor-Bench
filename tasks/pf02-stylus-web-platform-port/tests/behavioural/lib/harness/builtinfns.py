"""Probes for every built-in Stylus ships: 70 in JavaScript, 44 in `.styl`.

Each probe is a tiny stylesheet whose output reveals what the function returned.
Compiled twice -- pristine State A, and the submitted core in the realm -- and
compared byte for byte.  No expected values live here.

Three id prefixes, because they need different setup: `io:` reads files through
the mounted fixtures, `url:` additionally installs the `url()` embedding plugin
with per-probe options, and the rest compile with nothing but a filename.

Why probe individually when the corpus already exercises most of these: the
corpus tells you *a* case failed, this tells you *which function* is wrong.  The
built-in library is also where the port's two hardest constraints meet -- the
`.styl` half has to be fetched through `platform.readFile` (§1.4) and the JS half
has to run with no `Buffer` for `image-size` and no synchronous hash for
`cache` -- so a submission can be most of the way there and still have a
specific, findable hole.
"""
from __future__ import annotations

import functools

#: `(id, stylus source)`.  Sources are compiled with no options and no filename
#: unless the id starts with "io:", which the suite mounts fixtures for.
_JS_PROBES: tuple[tuple[str, str], ...] = (
    # --- colour construction and inspection ---------------------------------
    ("rgb", "a\n  color rgb(255, 0, 0)\n"),
    ("rgb-pct", "a\n  color rgb(100%, 50%, 25%)\n"),
    ("rgb-one-arg", "a\n  color rgb(#abc)\n"),
    ("rgba", "a\n  color rgba(255, 0, 0, 0.5)\n"),
    ("rgba-two-arg", "a\n  color rgba(#f00, 0.25)\n"),
    ("hsl", "a\n  color hsl(120, 50%, 50%)\n"),
    ("hsl-one-arg", "a\n  color hsl(#123456)\n"),
    ("hsla", "a\n  color hsla(120, 50%, 50%, 0.5)\n"),
    ("red", "a\n  width red(#123456)\n  height red(#ff0000)\n"),
    ("green", "a\n  width green(#123456)\n"),
    ("blue", "a\n  width blue(#123456)\n"),
    ("alpha", "a\n  width alpha(rgba(0,0,0,0.4))\n"),
    ("hue", "a\n  width hue(#f00)\n"),
    ("saturation", "a\n  width saturation(#f00)\n"),
    ("lightness", "a\n  width lightness(#123456)\n"),
    ("luminosity", "a\n  width luminosity(#888)\n"),
    ("contrast", "a\n  foo contrast(#000, #fff)\n"),
    ("blend", "a\n  color blend(rgba(#fff, 0.4), #123456)\n"),
    ("component", "a\n  width component(#123456, 'green')\n  height component(#123456, 'blue')\n"),
    ("transparentify", "a\n  color transparentify(#808080, #000)\n"),
    ("adjust", "a\n  color adjust(#f00, 'lightness', 30%)\n"),
    ("opposite-position", "a\n  foo opposite-position(top left)\n"),

    # --- units and arithmetic ----------------------------------------------
    ("unit-read", "a\n  content unit(15px)\n"),
    ("unit-set", "a\n  width unit(15, 'em')\n"),
    ("unit-replace", "a\n  width unit(15px, '%')\n"),
    ("convert", "a\n  width convert('1s')\n"),
    ("base-convert", "a\n  content base-convert(15, 2, 8)\n"),
    ("math-abs", "a\n  width math(-5, 'abs')\n"),
    ("math-prop", "a\n  width round(-math-prop('PI'), 5)\n  height -math-prop('E') > 2.7\n"),
    ("operate", "a\n  width operate('+', 5px, 10px)\n"),
    ("range", "a\n  foo range(1, 5)\n"),
    ("range-step", "a\n  foo range(1, 10, 3)\n"),
    ("percent-of", "a\n  width 10px * 2\n"),

    # --- strings ------------------------------------------------------------
    ("s-fmt", "a\n  content s('%s and %s', 'a', 'b')\n"),
    ("s-percent-d", "a\n  content s('%d', 4.6)\n"),
    ("unquote", "a\n  content unquote('\"hi\"')\n"),
    ("substr", "a\n  content substr('stylus', 2, 3)\n"),
    ("slice-str", "a\n  content slice('stylus', 1, 4)\n"),
    ("split", "a\n  foo split(',', 'a,b,c')\n"),
    ("replace", "a\n  content replace('a', 'b', 'aaa')\n"),
    ("replace-regex", "a\n  content replace('[0-9]+', 'N', 'a12b34')\n"),
    ("match", "a\n  foo match('^a(b)c$', 'abc')\n"),
    ("match-flags", "a\n  foo match('A', 'xax', 'i')\n"),
    # p() and warn() write to the console and return null; the probe is that
    # they are callable and that the null lands in the output as State A does.
    # Both return null, so the CSS says nothing: these two are judged on the
    # console lines they emit, which the suite compares separately.
    ("p", "a\n  foo type(p(1 2))\n  bar type(p('s', #f00, 5px))\n  baz type(p())\n"),
    ("warn", "a\n  foo type(warn('careful'))\n"),
    ("io:json-hash", "vars = json('import.json/vars.json', { hash: true })\na\n  foo type(vars)\n  color vars.color\n"),

    # --- lists --------------------------------------------------------------
    # Each of these shows the whole resulting list, not a count: probes that
    # only printed a length were mutually indistinguishable, so swapping `pop`
    # for `shift` would have passed all of them.
    ("length", "a\n  width length(4 5 6 7)\n"),
    ("push", "l = 1 2\na\n  foo push(l, 9)\n  bar l\n"),
    ("append", "l = 1 2\na\n  foo append(l, 9)\n  bar l\n"),
    ("pop", "l = 1 2 3\na\n  foo pop(l)\n  bar l\n"),
    ("shift", "l = 1 2 3\na\n  foo shift(l)\n  bar l\n"),
    ("unshift", "l = 2 3\na\n  foo unshift(l, 1)\n  bar l\n"),
    ("prepend", "l = 2 3\na\n  foo prepend(l, 1)\n  bar l\n"),
    ("remove", "a\n  foo remove({ a: 1, b: 2 }, 'a')\n"),
    ("list-separator", "l = 1, 2\na\n  content list-separator(l)\n"),
    ("list-separator-space", "l = 1 2\na\n  content list-separator(l)\n"),
    ("clone", "a\n  foo clone((1 2 3))\n"),
    ("lookup", "a\n  x = 5\n  width lookup('x')\n  height type(lookup('nope'))\n"),

    # --- types and reflection ----------------------------------------------
    ("type-of", "a\n  content type-of(12px)\n"),
    ("typeof", "a\n  content typeof('s')\n"),
    ("type-alias", "a\n  content type(#fff)\n"),
    ("selector", "a\n  b\n    content selector()\n"),
    ("selectors", "a\n  b\n    foo selectors()\n"),
    ("selector-exists", "a\n  foo selector-exists('a')\n  bar selector-exists('zz')\n"),
    ("current-media", "@media screen\n  a\n    content current-media()\n"),
    ("define-get", "define('zz', 9)\na\n  width zz\n"),
    ("define-global", "mixin()\n  define('gg', 7, true)\nb\n  mixin()\na\n  width gg\n"),
    ("add-property", "a\n  foo add-property('bar', 1)\n"),
    ("extend-fn", "a\n  foo extend({ x: 1 }, { y: 2 }).y\n"),
    ("merge", "a\n  foo type(merge({a: 1}, {b: 2}))\n"),
        
    # --- paths (the JS path built-ins, which the port must reimplement) -----
    ("basename-fn", "a\n  content basename('/a/b/c.styl')\n"),
    ("basename-ext", "a\n  content basename('/a/b/c.styl', '.styl')\n"),
    ("dirname-fn", "a\n  content dirname('/a/b/c.styl')\n"),
    ("extname-fn", "a\n  content extname('/a/b/c.styl')\n"),
    ("pathjoin", "a\n  content pathjoin('a', 'b', 'c.styl')\n"),
    ("selector-nested", "a\n  b\n    &:hover\n      content selector()\n"),

    # --- trigonometry, the JS half ------------------------------------------
    # `sin` and `cos` are also defined in index.styl and are probed there; these
    # four exist only in JavaScript.
    ("tan", "a\n  width round(tan(0), 4)\n  height round(tan(45deg), 4)\n"),
    ("asin", "a\n  width asin(1)\n  height asin(0)\n"),
    ("acos", "a\n  width acos(1)\n  height acos(0)\n"),
    ("atan", "a\n  width atan(1)\n  height atan(0)\n"),

    # `-prefix-classes` is exercised through the `+prefix-classes` mixin in the
    # `.styl` half; this only asserts the raw function is registered.
    ("prefix-classes-raw", "a\n  foo type(-prefix-classes)\n"),

    # `trace()` dumps the evaluator's frame stack.  It returns null and its text
    # names absolute filenames, so it is compared as "printed something" rather
    # than byte for byte -- see the suite.
    ("trace", "a\n  foo type(trace())\n"),
)

#: Probes that need files, mounted under the fixtures the corpus already ships.
_IO_PROBES: tuple[tuple[str, str], ...] = (
    ("io:image-size-png", "a\n  foo image-size('tux.png')\n"),
    ("io:image-size-w", "a\n  width image-size('tux.png')[1]\n"),
    ("io:image-size-gif", "a\n  foo image-size('gif')\n"),
    ("io:image-size-jpeg", "a\n  foo image-size('flowers.jpeg')\n"),
    ("io:image-size-jpg", "a\n  foo image-size('flowers_p.jpg')\n  bar image-size('flowers_p.jpg')[0]\n"),
    ("io:image-size-svg", "a\n  foo image-size('tiger.svg')\n"),
    # `json()` without `hash` declares variables in the calling scope and
    # returns null, so the probe has to read the variables back to see anything.
    ("io:json-file", "json('import.json/vars.json')\na\n  color color\n  pad spacing\n  gut gutter\n  awe awesome\n"),
    ("io:json-nested", "json('import.json/vars.json')\na\n  foo animate-special-out\n  bar queries-small\n"),
    ("io:json-prefixed",
     "a\n  json('import.json/local-vars.json', true, 'p-')\n"
     "  color p-params-color\n  width 10px * p-a\n  background p-bg\n"),
    ("io:json-leave-strings",
     "i = json('import.json/icons.json', { hash: true, 'leave-strings': true })\na\n  content i.menu\n"),
    ("io:json-optional",
     "o = json('import.json/optional.json', { hash: true, optional: true })\na\n  foo typeof(o) == 'null'\n"),
)

#: The `url()` embedding plugin.  Its own family because each probe needs the
#: function installed with different options, and because this is where the port
#: loses the most: upstream reads the file with `fs.readFileSync` and encodes it
#: with `Buffer.prototype.toString('base64')`, and the port has neither.
#:
#: `(id, source, url options, define name)`.  The define name matters: the lexer
#: treats everything inside `url(` as url characters, comma included, so the
#: second `encoding` argument is unreachable under that name and the utf8 branch
#: can only be probed through an alias.
_URL_PROBES: tuple[tuple[str, str, dict, str], ...] = (
    ("url:embed-svg", "a\n  background url('circle.svg')\n", {}, "url"),
    ("url:over-limit", "a\n  background url('tux.png')\n", {}, "url"),
    ("url:limit-false", "a\n  background url('tux.png')\n", {"limit": False}, "url"),
    ("url:limit-raised", "a\n  background url('circle.svg')\n", {"limit": 100}, "url"),
    ("url:limit-exact", "a\n  background url('circle.svg')\n", {"limit": 186}, "url"),
    ("url:hash-kept", "a\n  background url('circle.svg#frag')\n", {}, "url"),
    ("url:no-mime", "a\n  background url('gif')\n", {}, "url"),
    ("url:custom-mime", "a\n  background url('gif')\n", {"mimes": {"": "image/gif"}}, "url"),
    ("url:absolute", "a\n  background url('https://x.com/a.svg')\n", {}, "url"),
    ("url:protocol-relative", "a\n  background url('//x.com/a.svg')\n", {}, "url"),
    ("url:missing", "a\n  background url('nope.svg')\n", {}, "url"),
    ("url:query-string", "a\n  background url('circle.svg?v=2')\n", {}, "url"),
    ("url:utf8", "a\n  background embed('circle.svg', 'utf8')\n", {}, "embed"),
    ("url:utf8-upper", "a\n  background embed('circle.svg', 'UTF8')\n", {}, "embed"),
    ("url:b64-alias", "a\n  background embed('circle.svg')\n", {}, "embed"),
    ("url:jpeg-embed", "a\n  background url('flowers_p.jpg')\n", {"limit": False}, "url"),
)

#: `.styl` built-ins from `lib/functions/index.styl`.  These only work if §1.4 is
#: satisfied, so a submission that inlined or skipped the library fails all of
#: them together -- which is the signal.
_STYL_PROBES: tuple[tuple[str, str], ...] = (
    ("abs", "a\n  width abs(-5px)\n"),
    ("ceil", "a\n  width ceil(4.2)\n  height ceil(-4.2)\n"),
    ("floor", "a\n  width floor(4.8)\n  height floor(-4.8)\n"),
    ("round", "a\n  width round(4.5)\n  height round(-4.5)\n  top round(4.4)\n"),
    ("round-precision", "a\n  width round(4.567, 2)\n"),
    ("min", "a\n  width min(3, 5)\n  height min(5, 3)\n"),
    ("max", "a\n  width max(3, 5)\n  height max(5, 3)\n"),
    ("even", "a\n  foo even(4)\n  bar even(5)\n"),
    ("odd", "a\n  foo odd(3)\n  bar odd(4)\n"),
    ("sum", "a\n  width sum(1 2 3)\n"),
    ("avg", "a\n  width avg(2 4 7)\n"),
    ("sin", "a\n  width sin(0)\n  height round(sin(30deg), 4)\n"),
    ("cos", "a\n  width cos(0)\n  height round(cos(60deg), 4)\n"),
    ("percentage", "a\n  width percentage(0.5)\n  height percentage(0.125)\n"),
    ("percent-to-decimal", "a\n  width percent-to-decimal(50%)\n"),
    ("remove-unit", "a\n  width remove-unit(5px)\n  height type(remove-unit(5px))\n"),
    ("degrees-to-radians", "a\n  width round(degrees-to-radians(90), 5)\n"),
    ("radians-to-degrees", "a\n  width radians-to-degrees(-math-prop('PI'))\n"),
    ("lighten", "a\n  color lighten(#800, 20%)\n"),
    ("darken", "a\n  color darken(#f00, 20%)\n"),
    ("desaturate", "a\n  color desaturate(#f00, 20%)\n"),
    ("saturate", "a\n  color saturate(#800, 20%)\n"),
    ("fade-in", "a\n  color fade-in(rgba(#f00, 0.2), 20%)\n"),
    ("fade-out", "a\n  color fade-out(#f00, 20%)\n"),
    ("spin", "a\n  color spin(#f00, 60)\n"),
    ("complement", "a\n  color complement(#123456)\n"),
    ("invert", "a\n  color invert(#123456)\n"),
    ("grayscale", "a\n  color grayscale(#123456)\n"),
    ("mix", "a\n  color mix(#f00, #00f)\n"),
    ("mix-pct", "a\n  color mix(#f00, #00f, 25%)\n"),
    ("tint", "a\n  color tint(#f00, 30%)\n"),
    ("shade", "a\n  color shade(#f00, 30%)\n"),
    ("light", "a\n  foo light(#fff)\n  bar light(#111)\n"),
    ("dark", "a\n  foo dark(#000)\n  bar dark(#eee)\n"),
    ("index", "a\n  width index(a b c, c)\n  height type(index(a b c, z))\n"),
    ("join", "a\n  content join(', ', 1 2 3)\n"),
    ("last", "a\n  foo last(1 2 3)\n"),
    ("keys", "a\n  foo keys({ a: 1, b: 2 })\n"),
    ("values", "a\n  foo values({ a: 1, b: 2 })\n"),
    ("string-fn", "a\n  content -string(5px)\n"),
    ("require-color", "a\n  foo require-color(#f00) is a 'null'\n  bar type(#f00)\n"),
    ("require-unit", "a\n  foo require-unit(5px) is a 'null'\n  bar type(5px)\n"),
    ("require-string", "a\n  foo require-string('s') is a 'null'\n  bar type('s')\n"),
    ("prefix-classes-styl", "+prefix-classes('p-')\n  .b\n    color red\n"),
    ("cache-fn", "m($w)\n  +cache('w' + $w)\n    width $w\na\n  m(5px)\nb\n  m(5px)\n"),
)


@functools.lru_cache(maxsize=1)
def probes() -> tuple[tuple[str, str], ...]:
    return _JS_PROBES + _IO_PROBES + tuple((pid, src) for pid, src, _, _ in _URL_PROBES) + _STYL_PROBES


@functools.lru_cache(maxsize=1)
def probe_ids() -> tuple[str, ...]:
    return tuple(pid for pid, _ in probes())


@functools.lru_cache(maxsize=1)
def source_for() -> dict[str, str]:
    return {pid: src for pid, src in probes()}


@functools.lru_cache(maxsize=1)
def _url_config() -> dict[str, tuple[dict, str]]:
    return {pid: (opts, name) for pid, _, opts, name in _URL_PROBES}


def needs_io(pid: str) -> bool:
    """Whether the probe reads files, and so needs the fixture mounts."""
    return pid.startswith(("io:", "url:"))


def url_config(pid: str) -> tuple[dict, str] | None:
    """`(url() options, define name)` for a url probe, else None."""
    return _url_config().get(pid)


# --- groups ------------------------------------------------------------------
#
# Named here rather than rediscovered by prefix in the suite, so that adding a
# probe to a tuple above puts it in exactly one family.


@functools.lru_cache(maxsize=1)
def js_ids() -> tuple[str, ...]:
    """Built-ins implemented in JavaScript under `lib/functions/`."""
    return tuple(pid for pid, _ in _JS_PROBES)


@functools.lru_cache(maxsize=1)
def io_ids() -> tuple[str, ...]:
    """Built-ins that read files: `image-size()` on real binaries, `json()`."""
    return tuple(pid for pid, _ in _IO_PROBES)


@functools.lru_cache(maxsize=1)
def url_ids() -> tuple[str, ...]:
    """The `url()` embedding plugin, one probe per branch."""
    return tuple(pid for pid, _, _, _ in _URL_PROBES)


@functools.lru_cache(maxsize=1)
def styl_ids() -> tuple[str, ...]:
    """Built-ins defined in `index.styl`, reachable only if §1.4 holds."""
    return tuple(pid for pid, _ in _STYL_PROBES)


#: Probes whose CSS reveals nothing -- they return null and print instead, so the
#: console line is the answer and is compared exactly.
CONSOLE_ONLY_IDS: tuple[str, ...] = ("p", "warn")

#: `trace()` also only prints, but its text is a frame dump naming absolute
#: filenames, which the oracle and the realm necessarily disagree about.  Checked
#: for "printed something" instead of compared.
PRINTS_UNCOMPARABLE_IDS: tuple[str, ...] = ("trace",)

#: The one built-in no web port can implement: `use()` loads a JavaScript plugin
#: from disk and calls it, which needs a module loader the capability object does
#: not provide.  Named here so the omission is a recorded decision rather than an
#: oversight; §1.1 leaves it to the Node adapter.
UNPORTABLE_IDS: tuple[str, ...] = ("use",)
