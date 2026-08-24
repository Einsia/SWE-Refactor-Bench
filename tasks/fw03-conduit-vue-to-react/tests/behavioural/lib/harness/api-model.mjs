// View builders and query logic for the mock Conduit API.
//
// Split out from the routing layer so both are readable. Everything is pure:
// (db, viewer, ...) -> plain JSON-serialisable objects.

/** Total favourites for a slug: real users plus the synthetic padding. */
export function favoritesCount(db, slug) {
  let n = db.extraFavoriteCounts[slug] || 0;
  for (const list of Object.values(db.favorites)) {
    if (list.includes(slug)) n += 1;
  }
  return n;
}

export function isFavorited(db, viewer, slug) {
  if (!viewer) return false;
  return (db.favorites[viewer] || []).includes(slug);
}

export function isFollowing(db, viewer, username) {
  if (!viewer) return false;
  return (db.follows[viewer] || []).includes(username);
}

export function profileView(db, viewer, username) {
  const u = db.users[username];
  if (!u) return null;
  return {
    username: u.username,
    bio: u.bio,
    image: u.image,
    following: isFollowing(db, viewer, username),
  };
}

export function articleView(db, viewer, a) {
  return {
    slug: a.slug,
    title: a.title,
    description: a.description,
    body: a.body,
    tagList: [...a.tagList],
    createdAt: a.createdAt,
    updatedAt: a.updatedAt,
    favorited: isFavorited(db, viewer, a.slug),
    favoritesCount: favoritesCount(db, a.slug),
    author: profileView(db, viewer, a.author),
  };
}

export function commentView(db, viewer, c) {
  return {
    id: c.id,
    createdAt: c.createdAt,
    updatedAt: c.createdAt,
    body: c.body,
    author: profileView(db, viewer, c.author),
  };
}

export function userView(u) {
  return {
    email: u.email,
    username: u.username,
    bio: u.bio,
    image: u.image,
    token: u.token,
  };
}

/**
 * Newest first, which is what the real API does and what the fixtures' strictly
 * increasing createdAt makes unambiguous. Ties break on slug so the order is
 * total.
 */
export function sortedArticles(db) {
  return [...db.articles].sort((x, y) => {
    if (x.createdAt !== y.createdAt) return x.createdAt < y.createdAt ? 1 : -1;
    return x.slug < y.slug ? -1 : 1;
  });
}

function intParam(value, fallback) {
  if (value === undefined || value === "") return fallback;
  const n = Number.parseInt(value, 10);
  return Number.isNaN(n) ? fallback : n;
}

/**
 * Apply the list filters and window. `limit` defaults to 20 like the real API;
 * the app always sends an explicit limit, but the default keeps the mock honest
 * if a submission stops sending one.
 */
export function listArticles(db, viewer, query, opts = {}) {
  let items = sortedArticles(db);
  if (opts.feed) {
    const followed = db.follows[viewer] || [];
    items = items.filter((a) => followed.includes(a.author));
  }
  if (query.tag) items = items.filter((a) => a.tagList.includes(query.tag));
  if (query.author) items = items.filter((a) => a.author === query.author);
  if (query.favorited) {
    const favs = db.favorites[query.favorited] || [];
    items = items.filter((a) => favs.includes(a.slug));
  }
  const articlesCount = items.length;
  const offset = Math.max(0, intParam(query.offset, 0));
  const limit = Math.max(0, intParam(query.limit, 20));
  const page = items.slice(offset, offset + limit);
  return {
    articles: page.map((a) => articleView(db, viewer, a)),
    articlesCount,
  };
}

export function listComments(db, viewer, slug) {
  const items = db.comments
    .filter((c) => c.slug === slug)
    .sort((x, y) => x.id - y.id);
  return { comments: items.map((c) => commentView(db, viewer, c)) };
}

/** RealWorld's 422 envelope: {"errors": {"field": ["message", ...]}}. */
export function errors(map) {
  return { errors: map };
}

/** Slugify a title the way the reference API does. */
export function slugify(title) {
  return String(title)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}
