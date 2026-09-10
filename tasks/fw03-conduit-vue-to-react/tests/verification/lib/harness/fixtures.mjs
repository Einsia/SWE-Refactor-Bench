// Deterministic seed data for the mock RealWorld ("Conduit") API.
//
// Every value here is fixed. Nothing derives from the clock, the filesystem or
// a random source, so the same request sequence produces the same responses in
// the golden capture and at grading time.
//
// Shape follows the RealWorld API spec: users carry a token, profiles carry a
// per-viewer `following` flag, articles carry a per-viewer `favorited` flag.
// Those two per-viewer fields are computed by api.mjs from FOLLOWS/FAVORITES,
// never stored pre-resolved.

export const USERS = {
  alice: {
    email: "alice@conduit.test",
    username: "alice",
    bio: "I write about dragons and distributed systems.",
    image: "/static/rwv-logo.png",
    password: "alice-secret",
    token: "jwt.alice.fixed",
  },
  bob: {
    email: "bob@conduit.test",
    username: "bob",
    bio: "",
    // Null image: both frameworks must omit the src attribute entirely rather
    // than render src="null".
    image: null,
    password: "bob-secret",
    token: "jwt.bob.fixed",
  },
  carol: {
    email: "carol@conduit.test",
    username: "carol",
    bio: "Occasional poet.",
    image: "/static/carol.jpg",
    password: "carol-secret",
    token: "jwt.carol.fixed",
  },
};

// Who follows whom. alice follows bob and carol, so alice's feed is their
// articles; bob follows nobody, so bob's feed is empty (an important empty
// state).
export const FOLLOWS = { alice: ["bob", "carol"], bob: [], carol: ["alice"] };

// "dragons" lands on five of the nine slots, so #/tag/dragons holds 14 of the 25
// articles and paginates; "poetry" holds three and does not. One slot is empty
// so at least one article renders an empty tag list.
const TAG_POOL = [
  ["dragons", "training"],
  ["dragons"],
  ["baby", "dragons"],
  ["welcome", "introduction"],
  ["javascript", "frameworks"],
  ["javascript", "dragons"],
  [],
  ["poetry", "dragons"],
  ["distributed", "systems", "javascript"],
];

const TITLES = [
  "How to train your dragon",
  "Dragon breeding for beginners",
  "The baby dragon problem",
  "Welcome to Conduit",
  "Frameworks come and frameworks go",
  "A short note on JavaScript",
  "An article with no tags at all",
  "Ode to a lost semicolon",
  "Consensus is hard, actually",
];

// Rich enough to pin down the markdown renderer: headings, emphasis, inline and
// fenced code, both list kinds, a nested list, a blockquote, a table, a rule, an
// image, a raw-HTML passthrough, and characters that must be escaped.
export const MARKDOWN_BODY = [
  "# Heading one",
  "",
  "Some **bold** and *italic* text, `inline code`, and a [link](https://example.com).",
  "",
  "## Heading two",
  "",
  "- item one",
  "- item two",
  "  - nested item",
  "",
  "1. first",
  "2. second",
  "",
  "> a blockquote",
  "",
  "```js",
  "const x = 1;",
  "console.log(x < 2 && x > 0);",
  "```",
  "",
  "| a | b |",
  "| - | - |",
  "| 1 | 2 |",
  "",
  "Raw <em>html</em> passes straight through.",
  "",
  "Ampersand & less-than < greater-than > quote \" apostrophe '.",
  "",
  "---",
  "",
  "![the logo](/static/rwv-logo.png)",
].join("\n");

const AUTHOR_CYCLE = ["alice", "bob", "carol"];

function seedArticles() {
  const out = [];
  // 25 articles: 9 alice, 8 bob, 8 carol. 10 per page on the home feed gives
  // three pages; 5 per page on a profile gives two pages for alice.
  for (let i = 0; i < 25; i += 1) {
    const author = AUTHOR_CYCLE[i % 3];
    const n = i + 1;
    const title = `${TITLES[i % TITLES.length]} ${n}`;
    // Fixed, strictly increasing timestamps spread across two months so the
    // date filter has both single- and double-digit days to format.
    const day = String((i % 28) + 1).padStart(2, "0");
    const month = i < 13 ? "09" : "10";
    out.push({
      slug: `article-${n}`,
      title,
      description: `Description for ${title.toLowerCase()}.`,
      body:
        n === 4
          ? MARKDOWN_BODY
          : `Body of ${title}.\n\nA second paragraph with a [link](https://example.com).`,
      tagList: TAG_POOL[i % TAG_POOL.length],
      createdAt: `2019-${month}-${day}T0${i % 10}:15:30.000Z`,
      updatedAt: `2019-${month}-${day}T0${i % 10}:15:30.000Z`,
      author,
    });
  }
  return out;
}

// username -> slugs that user has favorited. alice has favorited a spread of
// articles including some of her own; bob has favorited exactly one; carol none
// (an empty "Favorited Articles" tab).
export const FAVORITES = {
  alice: ["article-1", "article-2", "article-5", "article-8", "article-13",
          "article-21"],
  bob: ["article-1"],
  carol: [],
};

// Extra favourites from users who are not in USERS, purely to make
// favoritesCount differ from the length of the lists above.
export const EXTRA_FAVORITE_COUNTS = {
  "article-1": 3, "article-2": 1, "article-4": 7, "article-5": 0,
  "article-8": 2, "article-13": 11, "article-21": 1,
};

function seedComments() {
  return [
    { id: 1, slug: "article-1", author: "bob",
      body: "This is the first comment.",
      createdAt: "2019-10-01T10:00:00.000Z" },
    { id: 2, slug: "article-1", author: "alice",
      body: "A reply from the author, with **markdown** that is NOT rendered.",
      createdAt: "2019-10-02T11:30:00.000Z" },
    { id: 3, slug: "article-1", author: "carol",
      body: "Third comment.\nWith a newline.",
      createdAt: "2019-10-03T12:45:00.000Z" },
    { id: 4, slug: "article-4", author: "alice",
      body: "Only comment on the markdown article.",
      createdAt: "2019-09-15T08:00:00.000Z" },
  ];
}

/** The popular-tags list, in the fixed order the sidebar renders it. */
export const POPULAR_TAGS = [
  "dragons", "training", "javascript", "welcome", "introduction",
  "frameworks", "baby", "poetry", "distributed", "systems",
];

/** A fresh, mutable database. Each scenario gets its own so writes never leak. */
export function makeDb() {
  return {
    users: JSON.parse(JSON.stringify(USERS)),
    follows: JSON.parse(JSON.stringify(FOLLOWS)),
    favorites: JSON.parse(JSON.stringify(FAVORITES)),
    extraFavoriteCounts: { ...EXTRA_FAVORITE_COUNTS },
    articles: seedArticles(),
    comments: seedComments(),
    tags: [...POPULAR_TAGS],
    nextCommentId: 5,
  };
}

/**
 * Stamp a run-specific string through every surface the app renders from.
 *
 * Used only by the audit probe. The point is that the string cannot be known
 * ahead of time, so a page whose markup was recorded rather than rendered will
 * not contain it. Applied to a db that has already been built, so the default
 * fixtures — and therefore the behavioural golden — are untouched.
 */
export function stampNonce(db, nonce) {
  for (const article of db.articles) {
    article.title = `N${nonce} ${article.title}`;
    article.description = `D${nonce} ${article.description}`;
    article.body = `B${nonce}\n\n${article.body}`;
  }
  db.users.alice.bio = `BIO${nonce}`;
  db.users.carol.bio = `BIO${nonce} carol`;
  db.tags = db.tags.map((t, i) => (i === 0 ? `tag${nonce}` : t));
  for (const comment of db.comments) {
    comment.body = `C${nonce} ${comment.body}`;
  }
  return db;
}

/** Token -> username, for resolving `Authorization: Token <jwt>`. */
export const TOKEN_TO_USER = Object.fromEntries(
  Object.values(USERS).map((u) => [u.token, u.username]),
);
