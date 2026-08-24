// Request routing for the mock Conduit API.
//
// handle() is pure with respect to the outside world: it reads and mutates the
// db object it is handed and returns {status, body}. The HTTP layer in
// server.mjs owns sockets, static files and the request log.
//
// Trailing slashes are tolerated because State A's ApiService builds some paths
// as `${resource}/${slug}` with an empty slug, producing `/api/tags/` and
// `/api/user/`. A strict router would 404 those; the real Conduit API does not.

import { TOKEN_TO_USER } from "./fixtures.mjs";
import {
  articleView, commentView, errors, listArticles, listComments,
  profileView, slugify, userView,
} from "./api-model.mjs";

const json = (status, body) => ({ status, body });
const notFound = (what) => json(404, errors({ [what]: ["not found"] }));
const unauthorized = () => json(401, errors({ authorization: ["required"] }));
const notAllowed = () => json(405, errors({ method: ["not allowed"] }));

/** Resolve `Authorization: Token <jwt>` to a username, or null. */
export function viewerFor(db, headers) {
  const raw = headers.authorization || headers.Authorization || "";
  const m = /^Token\s+(.+)$/.exec(String(raw).trim());
  if (!m) return null;
  const username = TOKEN_TO_USER[m[1]];
  return username && db.users[username] ? username : null;
}

const articleBySlug = (db, slug) =>
  db.articles.find((a) => a.slug === slug) || null;

/** Unwrap the {article: {...}} / {user: {...}} envelope the app always sends. */
function envelope(body, key) {
  const inner = body && typeof body === "object" ? body[key] : null;
  return inner && typeof inner === "object" ? inner : null;
}

export function handle(db, method, segments, query, body, headers) {
  const viewer = viewerFor(db, headers);
  const [s0, s1, s2, s3] = segments;

  if (s0 === "tags" && segments.length === 1) {
    return method === "GET" ? json(200, { tags: [...db.tags] }) : notAllowed();
  }
  if (s0 === "articles") return articlesRoute(db, viewer, method, segments, query, body);
  if (s0 === "profiles" && s1) return profilesRoute(db, viewer, method, segments);
  if (s0 === "users") return usersRoute(db, method, segments, body);
  if (s0 === "user" && segments.length === 1) return userRoute(db, viewer, method, body);
  return notFound("endpoint");
}
function articlesRoute(db, viewer, method, segments, query, body) {
  const [, s1, s2, s3] = segments;

  // GET /articles, GET /articles/feed
  if (segments.length === 1 && method === "GET") {
    return json(200, listArticles(db, viewer, query));
  }
  if (segments.length === 1 && method === "POST") {
    if (!viewer) return unauthorized();
    const input = envelope(body, "article");
    if (!input) return json(422, errors({ article: ["is required"] }));
    const problems = {};
    if (!input.title) problems.title = ["can't be blank"];
    if (!input.description) problems.description = ["can't be blank"];
    if (!input.body) problems.body = ["can't be blank"];
    if (Object.keys(problems).length) return json(422, errors(problems));
    const slug = slugify(input.title);
    if (articleBySlug(db, slug)) return json(422, errors({ title: ["must be unique"] }));
    const created = {
      slug,
      title: input.title,
      description: input.description,
      body: input.body,
      tagList: Array.isArray(input.tagList) ? [...input.tagList] : [],
      createdAt: "2019-10-14T12:00:00.000Z",
      updatedAt: "2019-10-14T12:00:00.000Z",
      author: viewer,
    };
    db.articles.push(created);
    return json(200, { article: articleView(db, viewer, created) });
  }
  if (segments.length === 2 && s1 === "feed" && method === "GET") {
    if (!viewer) return unauthorized();
    return json(200, listArticles(db, viewer, query, { feed: true }));
  }
  // /articles/:slug
  if (segments.length === 2) {
    const article = articleBySlug(db, s1);
    if (method === "GET") {
      return article
        ? json(200, { article: articleView(db, viewer, article) })
        : notFound("article");
    }
    if (method === "PUT") {
      if (!viewer) return unauthorized();
      if (!article) return notFound("article");
      if (article.author !== viewer) return json(403, errors({ article: ["forbidden"] }));
      const input = envelope(body, "article") || {};
      for (const key of ["title", "description", "body"]) {
        if (typeof input[key] === "string") article[key] = input[key];
      }
      if (Array.isArray(input.tagList)) article.tagList = [...input.tagList];
      article.updatedAt = "2019-10-14T12:30:00.000Z";
      return json(200, { article: articleView(db, viewer, article) });
    }
    if (method === "DELETE") {
      if (!viewer) return unauthorized();
      if (!article) return notFound("article");
      if (article.author !== viewer) return json(403, errors({ article: ["forbidden"] }));
      db.articles = db.articles.filter((a) => a.slug !== s1);
      db.comments = db.comments.filter((c) => c.slug !== s1);
      return json(200, {});
    }
    return notAllowed();
  }
  // /articles/:slug/comments[/:id], /articles/:slug/favorite
  if (segments.length >= 3 && s2 === "comments") return commentsRoute(db, viewer, method, s1, s3, body);
  if (segments.length === 3 && s2 === "favorite") return favoriteRoute(db, viewer, method, s1);
  return notFound("endpoint");
}

function commentsRoute(db, viewer, method, slug, idPart, body) {
  const article = articleBySlug(db, slug);
  if (!article) return notFound("article");
  if (idPart === undefined) {
    if (method === "GET") return json(200, listComments(db, viewer, slug));
    if (method === "POST") {
      if (!viewer) return unauthorized();
      const input = envelope(body, "comment");
      if (!input || !input.body) return json(422, errors({ body: ["can't be blank"] }));
      const created = {
        id: db.nextCommentId,
        slug,
        author: viewer,
        body: input.body,
        createdAt: "2019-10-14T12:00:00.000Z",
      };
      db.nextCommentId += 1;
      db.comments.push(created);
      return json(200, { comment: commentView(db, viewer, created) });
    }
    return notAllowed();
  }
  if (method === "DELETE") {
    if (!viewer) return unauthorized();
    const id = Number.parseInt(idPart, 10);
    const comment = db.comments.find((c) => c.id === id && c.slug === slug);
    if (!comment) return notFound("comment");
    if (comment.author !== viewer) return json(403, errors({ comment: ["forbidden"] }));
    db.comments = db.comments.filter((c) => c.id !== id);
    return json(200, {});
  }
  return notAllowed();
}

function favoriteRoute(db, viewer, method, slug) {
  if (!viewer) return unauthorized();
  const article = articleBySlug(db, slug);
  if (!article) return notFound("article");
  const list = db.favorites[viewer] || (db.favorites[viewer] = []);
  if (method === "POST") {
    if (!list.includes(slug)) list.push(slug);
  } else if (method === "DELETE") {
    db.favorites[viewer] = list.filter((s) => s !== slug);
  } else {
    return notAllowed();
  }
  return json(200, { article: articleView(db, viewer, article) });
}

function profilesRoute(db, viewer, method, segments) {
  const [, username, s2] = segments;
  if (!db.users[username]) return notFound("profile");
  if (segments.length === 2 && method === "GET") {
    return json(200, { profile: profileView(db, viewer, username) });
  }
  if (segments.length === 3 && s2 === "follow") {
    if (!viewer) return unauthorized();
    const list = db.follows[viewer] || (db.follows[viewer] = []);
    if (method === "POST") {
      if (!list.includes(username)) list.push(username);
    } else if (method === "DELETE") {
      db.follows[viewer] = list.filter((u) => u !== username);
    } else {
      return notAllowed();
    }
    return json(200, { profile: profileView(db, viewer, username) });
  }
  return notFound("endpoint");
}

function usersRoute(db, method, segments, body) {
  // POST /users        -> register
  // POST /users/login  -> login
  const [, s1] = segments;
  if (segments.length === 2 && s1 === "login" && method === "POST") {
    const input = envelope(body, "user") || {};
    const user = Object.values(db.users).find((u) => u.email === input.email);
    if (!user || user.password !== input.password) {
      return json(422, errors({ "email or password": ["is invalid"] }));
    }
    return json(200, { user: userView(user) });
  }
  if (segments.length === 1 && method === "POST") {
    const input = envelope(body, "user") || {};
    const problems = {};
    if (!input.username) problems.username = ["can't be blank"];
    else if (db.users[input.username]) problems.username = ["has already been taken"];
    if (!input.email) problems.email = ["can't be blank"];
    else if (Object.values(db.users).some((u) => u.email === input.email)) {
      problems.email = ["has already been taken"];
    }
    if (!input.password) problems.password = ["can't be blank"];
    else if (String(input.password).length < 8) {
      problems.password = ["is too short (minimum is 8 characters)"];
    }
    if (Object.keys(problems).length) return json(422, errors(problems));
    const created = {
      email: input.email,
      username: input.username,
      bio: "",
      image: null,
      password: input.password,
      token: `jwt.${input.username}.fixed`,
    };
    db.users[input.username] = created;
    db.follows[input.username] = [];
    db.favorites[input.username] = [];
    return json(200, { user: userView(created) });
  }
  return notFound("endpoint");
}

function userRoute(db, viewer, method, body) {
  if (!viewer) return unauthorized();
  const user = db.users[viewer];
  if (method === "GET") return json(200, { user: userView(user) });
  if (method === "PUT") {
    const input = envelope(body, "user") || {};
    for (const key of ["email", "username", "bio", "image", "password"]) {
      if (input[key] !== undefined && input[key] !== null) user[key] = input[key];
    }
    return json(200, { user: userView(user) });
  }
  return notAllowed();
}
