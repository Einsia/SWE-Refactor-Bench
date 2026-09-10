// The behavioural corpus: what gets exercised, and how.
//
// A scenario is a token (or none), then an ordered list of steps. After every
// step the driver waits for the network to go quiet and the DOM to settle, then
// the final state is reduced by normalize.snapshot().
//
// Steps are data, not code, so the exact same corpus drives the State A golden
// capture and the State B grading run.
//
//   token   pre-seed localStorage with a user's JWT before the first load,
//           which is how a returning visitor arrives
//   goto    full page load at a hash route (a cold boot, not a SPA transition)
//   click   dispatch a real click; SPA transitions go through this
//   fill    set an input's value and fire the events both frameworks listen for
//   press   keyboard event on a focused input
//   back    history.back()

const S = (id, group, steps, opts = {}) => ({ id, group, steps, ...opts });
const goto = (hash) => ({ goto: hash });
const click = (sel) => ({ click: sel });
const fill = (sel, value) => ({ fill: sel, value });
const press = (sel, key) => ({ press: sel, key });
const back = () => ({ back: true });

// Selectors used more than once, named so a template change is a one-line edit.
// Indexed on the <li>, not the <a>: every .nav-link is an only child, so
// :nth-of-type on the link itself would always resolve to the first one.
const NAV = (n) => `.navbar .nav-item:nth-of-type(${n}) .nav-link`;
const FEED_TAB = (n) => `.feed-toggle .nav-item:nth-of-type(${n}) .nav-link`;
const PROFILE_TAB = (n) => `.articles-toggle .nav-item:nth-of-type(${n}) .nav-link`;
const PREVIEW_LINK = ".article-preview .preview-link";
const PAGE = (n) => `.pagination li[data-test="page-link-${n}"]`;
const EMAIL = 'input[placeholder="Email"]';
const PASSWORD = 'input[placeholder="Password"]';
const USERNAME = 'input[placeholder="Username"]';
const SUBMIT = "form button";
const COMMENT_BOX = ".comment-form textarea";
const COMMENT_SUBMIT = ".comment-form button";
const TRASH = ".card .mod-options .ion-trash-a";
const LOGOUT = ".settings-page .btn-outline-danger";
const ED_TITLE = 'input[placeholder="Article Title"]';
const ED_DESC = 'input[placeholder="What\'s this article about?"]';
const ED_BODY = 'textarea[placeholder="Write your article (in markdown)"]';
const ED_TAGS = 'input[placeholder="Enter tags"]';
const ED_SUBMIT = ".editor-page form > button";
const ED_TAG_X = ".editor-page .tag-list .tag-pill .ion-close-round";
const ACTION_BTN = ".profile-page .action-btn";
// ArticleMeta renders twice on an article page (banner and article-actions), so
// click targets are scoped to the banner copy. The action buttons are told apart
// by class, which is also what encodes their state.
const BANNER_META = ".article-page .banner .article-meta";
const FOLLOW_BTN = `${BANNER_META} button.btn-outline-secondary`;
const FAV_ADD = `${BANNER_META} button.btn-outline-primary`;
const FAV_REMOVE = `${BANNER_META} button.btn-primary`;
const DELETE_BTN = `${BANNER_META} button.btn-outline-danger`;
const PREVIEW_FAV = ".article-preview .article-meta button";

export const SCENARIOS = [
  // ---- shell: the app frame, independent of any one page -----------------
  S("shell-boot-anon", "shell", [goto("#/")]),
  S("shell-boot-authed", "shell", [goto("#/")], { token: "alice" }),
  S("shell-brand-click", "shell", [goto("#/login"), click(".navbar-brand")]),
  S("shell-unknown-route", "shell", [goto("#/no/such/place")]),
  S("shell-root-no-hash", "shell", [goto("/")]),
  S("shell-deep-link-reload", "shell", [goto("#/@alice/favorites")], { token: "alice" }),
  S("shell-back-after-nav", "shell", [goto("#/"), click(PREVIEW_LINK), back()]),
  S("shell-nav-signin", "shell", [goto("#/"), click(NAV(2))]),
  S("shell-nav-signup", "shell", [goto("#/"), click(NAV(3))]),
  S("shell-nav-settings-authed", "shell", [goto("#/"), click(NAV(3))], { token: "alice" }),
  S("shell-nav-username-authed", "shell", [goto("#/"), click(NAV(4))], { token: "alice" }),

  // ---- home: global feed, personal feed, tag feed, pagination ------------
  S("home-global-anon", "home", [goto("#/")]),
  S("home-global-authed", "home", [goto("#/")], { token: "alice" }),
  S("home-page-2", "home", [goto("#/"), click(PAGE(2))]),
  S("home-page-3", "home", [goto("#/"), click(PAGE(3))]),
  S("home-page-2-then-1", "home", [goto("#/"), click(PAGE(2)), click(PAGE(1))]),
  S("home-page-click-current", "home", [goto("#/"), click(PAGE(1))]),
  S("home-my-feed-click", "home", [goto("#/"), click(FEED_TAB(1))],
    { token: "alice" }),
  S("home-my-feed-direct", "home", [goto("#/my-feed")], { token: "alice" }),
  S("home-my-feed-empty", "home", [goto("#/my-feed")], { token: "bob" }),
  S("home-my-feed-anon", "home", [goto("#/my-feed")]),
  S("home-my-feed-then-global", "home", [
    goto("#/my-feed"), click(FEED_TAB(2)),
  ], { token: "alice" }),
  S("home-tag-dragons", "home", [goto("#/tag/dragons")]),
  S("home-tag-dragons-page-2", "home", [goto("#/tag/dragons"), click(PAGE(2))]),
  S("home-tag-poetry", "home", [goto("#/tag/poetry")]),
  S("home-tag-unknown", "home", [goto("#/tag/no-such-tag")]),
  S("home-tag-authed", "home", [goto("#/tag/javascript")], { token: "alice" }),
  S("home-sidebar-tag-click", "home", [goto("#/"), click(".sidebar .tag-pill")]),
  S("home-preview-favorite-click", "home", [goto("#/"), click(PREVIEW_FAV)],
    { token: "alice" }),
  S("home-preview-favorite-anon", "home", [goto("#/"), click(PREVIEW_FAV)]),
  S("home-author-link-click", "home", [goto("#/"), click(".article-preview .author")]),

  // ---- article: read, markdown, comments, favourite, follow, delete ------
  S("article-anon", "article", [goto("#/articles/article-1")]),
  S("article-authed-author", "article", [goto("#/articles/article-1")], { token: "alice" }),
  S("article-authed-other", "article", [goto("#/articles/article-1")], { token: "bob" }),
  S("article-markdown", "article", [goto("#/articles/article-4")]),
  S("article-no-tags", "article", [goto("#/articles/article-7")]),
  S("article-no-comments", "article", [goto("#/articles/article-2")]),
  S("article-missing", "article", [goto("#/articles/does-not-exist")]),
  S("article-from-home-nav", "article", [goto("#/"), click(PREVIEW_LINK)]),
  S("article-favorite-add", "article", [goto("#/articles/article-3"), click(FAV_ADD)],
    { token: "bob" }),
  S("article-favorite-remove", "article", [goto("#/articles/article-1"), click(FAV_REMOVE)],
    { token: "bob" }),
  S("article-favorite-twice", "article", [
    goto("#/articles/article-3"), click(FAV_ADD), click(FAV_REMOVE),
  ], { token: "bob" }),
  S("article-follow-click", "article", [goto("#/articles/article-1"), click(FOLLOW_BTN)],
    { token: "bob" }),
  S("article-favorite-anon", "article", [goto("#/articles/article-3"), click(FAV_ADD)]),
  S("article-comment-post", "article", [
    goto("#/articles/article-2"),
    fill(COMMENT_BOX, "A brand new comment from the harness."),
    click(COMMENT_SUBMIT),
  ], { token: "bob" }),
  S("article-comment-empty-post", "article", [
    goto("#/articles/article-2"), click(COMMENT_SUBMIT),
  ], { token: "bob" }),
  S("article-comment-delete", "article", [goto("#/articles/article-1"), click(TRASH)],
    { token: "bob" }),
  S("article-delete", "article", [goto("#/articles/article-7"), click(DELETE_BTN)],
    { token: "alice" }),
  S("article-edit-link-click", "article", [
    goto("#/articles/article-1"), click(`${BANNER_META} a.btn-outline-secondary`),
  ], { token: "alice" }),

  // ---- auth: sign in, sign up, sign out, and their failure modes ---------
  S("login-page", "auth", [goto("#/login")]),
  S("login-page-authed", "auth", [goto("#/login")], { token: "alice" }),
  S("login-success", "auth", [
    goto("#/login"), fill(EMAIL, "alice@conduit.test"),
    fill(PASSWORD, "alice-secret"), click(SUBMIT),
  ]),
  S("login-bad-password", "auth", [
    goto("#/login"), fill(EMAIL, "alice@conduit.test"),
    fill(PASSWORD, "wrong"), click(SUBMIT),
  ]),
  S("login-blank-submit", "auth", [goto("#/login"), click(SUBMIT)]),
  S("login-then-home", "auth", [
    goto("#/login"), fill(EMAIL, "bob@conduit.test"),
    fill(PASSWORD, "bob-secret"), click(SUBMIT), click(NAV(1)),
  ]),
  S("register-page", "auth", [goto("#/register")]),
  S("register-success", "auth", [
    goto("#/register"), fill(USERNAME, "dave"), fill(EMAIL, "dave@conduit.test"),
    fill(PASSWORD, "dave-secret"), click(SUBMIT),
  ]),
  S("register-taken", "auth", [
    goto("#/register"), fill(USERNAME, "alice"), fill(EMAIL, "alice@conduit.test"),
    fill(PASSWORD, "long-enough"), click(SUBMIT),
  ]),
  S("register-short-password", "auth", [
    goto("#/register"), fill(USERNAME, "erin"), fill(EMAIL, "erin@conduit.test"),
    fill(PASSWORD, "short"), click(SUBMIT),
  ]),
  S("register-blank-submit", "auth", [goto("#/register"), click(SUBMIT)]),
  S("register-link-from-login", "auth", [
    goto("#/login"), click(".auth-page p a"),
  ]),
  S("logout-from-settings", "auth", [goto("#/settings"), click(LOGOUT)], { token: "alice" }),

  // ---- profile: own, other, tabs, pagination, follow ---------------------
  S("profile-anon", "profile", [goto("#/@alice")]),
  S("profile-self", "profile", [goto("#/@alice")], { token: "alice" }),
  S("profile-other", "profile", [goto("#/@alice")], { token: "bob" }),
  S("profile-followed-other", "profile", [goto("#/@bob")], { token: "alice" }),
  S("profile-empty-bio", "profile", [goto("#/@bob")]),
  S("profile-missing", "profile", [goto("#/@nobody")]),
  S("profile-page-2", "profile", [goto("#/@alice"), click(PAGE(2))]),
  S("profile-favorited", "profile", [goto("#/@alice/favorites")]),
  S("profile-favorited-empty", "profile", [goto("#/@carol/favorites")]),
  S("profile-tab-to-favorited", "profile", [
    goto("#/@alice"), click(PROFILE_TAB(2)),
  ]),
  S("profile-tab-back-to-articles", "profile", [
    goto("#/@alice/favorites"),
    click(PROFILE_TAB(1)),
  ]),
  S("profile-follow-click", "profile", [goto("#/@alice"), click(ACTION_BTN)], { token: "bob" }),
  S("profile-unfollow-click", "profile", [goto("#/@bob"), click(ACTION_BTN)], { token: "alice" }),
  S("profile-settings-link", "profile", [goto("#/@alice"), click(ACTION_BTN)],
    { token: "alice" }),
  S("settings-page", "profile", [goto("#/settings")], { token: "alice" }),
  S("settings-anon", "profile", [goto("#/settings")]),
  S("settings-update", "profile", [
    goto("#/settings"), fill('textarea[placeholder="Short bio about you"]', "New bio text."),
    click(".settings-page form button"),
  ], { token: "alice" }),

  // ---- editor: create, edit, tag handling, validation --------------------
  S("editor-new-authed", "editor", [goto("#/editor")], { token: "alice" }),
  S("editor-new-anon", "editor", [goto("#/editor")]),
  S("editor-new-from-nav", "editor", [goto("#/"), click(NAV(2))],
    { token: "alice" }),
  S("editor-edit-existing", "editor", [goto("#/editor/article-1")], { token: "alice" }),
  S("editor-edit-markdown", "editor", [goto("#/editor/article-4")], { token: "alice" }),
  S("editor-add-tag", "editor", [
    goto("#/editor"), fill(ED_TAGS, "harness"), press(ED_TAGS, "Enter"),
  ], { token: "alice" }),
  S("editor-add-two-tags", "editor", [
    goto("#/editor"), fill(ED_TAGS, "one"), press(ED_TAGS, "Enter"),
    fill(ED_TAGS, "two"), press(ED_TAGS, "Enter"),
  ], { token: "alice" }),
  S("editor-remove-tag", "editor", [
    goto("#/editor/article-1"), click(ED_TAG_X),
  ], { token: "alice" }),
  S("editor-publish", "editor", [
    goto("#/editor"), fill(ED_TITLE, "A harness written article"),
    fill(ED_DESC, "Written by the harness."), fill(ED_BODY, "Body **text** here."),
    fill(ED_TAGS, "harness"), press(ED_TAGS, "Enter"), click(ED_SUBMIT),
  ], { token: "alice" }),
  S("editor-publish-blank", "editor", [goto("#/editor"), click(ED_SUBMIT)],
    { token: "alice" }),
  S("editor-publish-no-body", "editor", [
    goto("#/editor"), fill(ED_TITLE, "Only a title"), click(ED_SUBMIT),
  ], { token: "alice" }),
  S("editor-update-existing", "editor", [
    goto("#/editor/article-1"), fill(ED_TITLE, "How to retrain your dragon"),
    click(ED_SUBMIT),
  ], { token: "alice" }),
  S("editor-edit-then-new", "editor", [
    goto("#/editor/article-1"), click(NAV(2)),
  ], { token: "alice" }),

  // ---- quirks: behaviours that are accidents of State A, but still real --
  S("quirk-stale-auth-after-logout", "quirks", [
    goto("#/settings"), click(LOGOUT), click(NAV(1)),
  ], { token: "alice" }),
  S("quirk-tag-links-collapse", "quirks", [goto("#/")]),
  S("quirk-article-tag-links", "quirks", [goto("#/articles/article-1")]),
  S("quirk-pagination-empty-list", "quirks", [goto("#/tag/no-such-tag")]),
  S("quirk-error-list-always-present", "quirks", [goto("#/editor")], { token: "alice" }),
  S("quirk-preview-ispreview-attr", "quirks", [goto("#/")]),
  S("quirk-footer-target", "quirks", [goto("#/")]),
  S("quirk-settings-binds-store", "quirks", [
    goto("#/settings"), fill('input[placeholder="Your username"]', "alice-renamed"),
    click(NAV(1)),
  ], { token: "alice" }),
];

export const SCENARIOS_BY_ID = Object.fromEntries(SCENARIOS.map((s) => [s.id, s]));
export const GROUPS = [...new Set(SCENARIOS.map((s) => s.group))];

if (SCENARIOS.length !== Object.keys(SCENARIOS_BY_ID).length) {
  throw new Error("duplicate scenario id");
}
