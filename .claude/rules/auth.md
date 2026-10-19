---
paths:
  - "cocco/auth.py"
  - "cocco/passwords.py"
  - "cocco/tokens.py"
  - "cocco/config.py"
  - "cocco/test_auth.py"
  - "cocco/test_auth_routes.py"
  - "cocco/test_passwords.py"
  - "cocco/test_password_routes.py"
  - "cocco/test_tokens.py"
  - "cocco/test_config.py"
  - "cocco/templates/login.jinja"
---

# Authentication (`cocco/auth.py`)

Login is **somebody else's login page**, and there are two of them: Google OIDC and GitHub OAuth2.
`GET /login` offers one button per *configured* provider; `GET /auth/{provider}` hands off to
Authlib and `GET /auth/{provider}/callback` comes back — **one parameterized pair of routes, not
one pair each**, because everything either provider does is the same sentence apart from the step
that turns a token into an address. There are no passwords and no sign-up step — **the account is
created the first time an accepted address comes back** — and it is keyed on the address alone, so
someone who signs in with Google today and GitHub tomorrow lands on the same account by
construction rather than by a rule anybody had to write. A new user has no enrollments and is
therefore read-only everywhere until course staff enroll them.

**A provider is enabled by its own two environment variables and by nothing else.**
`COCCO_GOOGLE_CLIENT_ID`/`_SECRET` and `COCCO_GITHUB_CLIENT_ID`/`_SECRET`, read by one
`_oauth_credentials` in `config.py`; either half alone is a typo, warned about, and that provider
stays off. There is no file spelling and therefore no `enabled = true` to disagree with the
credentials — `AuthConfig.google` / `.github` being non-None *is* "configured", `oauth.register`
runs only for those, and `oauth_client(name)` answers None for the rest, which is the 503 branch.

**What differs between the two is one function**, and that is the whole of `OAUTH_PROVIDERS`: a
`_OAuthProvider` carries the URL segment, the label every sentence and button prints, and the
`email` resolver. `_google_email` reads the ID token Authlib has already verified (`userinfo`,
plus the `email_verified` check matched against `True`/`"true"`, because the string `"false"` is
truthy). `_github_email` has to **ask** — GitHub has no ID token, so it calls `GET /user/emails`,
the only network request signing in makes on its own. The two failures are one exception,
`_NoProvedAddress`, carrying the sentence and the status, so the callback keeps a single `except`.
The `hd` hint is the one other branch on the provider name, in `get_auth_provider`.

**GitHub's address rule.** An account holds several addresses, so the **unverified are dropped
outright** — they prove nothing — and among the rest the first that passes `allowed_domains` wins,
the primary considered first. Deliberately not "the primary or nothing": a student's primary is
usually personal with the institutional address verified beside it, and refusing that would refuse
the people this is for. When none of them passes, the primary is returned anyway so that
`_complete_sign_in` prints its "not an institutional address" sentence, which names the domains —
a second, vaguer refusal for the same event would be a difference nobody could explain.

**The gate is `is_allowed_email`, on whatever address the provider vouched for.** A configured
domain admits itself and its subdomains, so `unipd.it` already covers `@studenti.unipd.it` and
`@phd.unipd.it`; the suffix test is on a dot boundary, so `notunipd.it` does not sneak in, and an
**empty allowlist admits nobody** — a deleted line in `cocco.toml` should lock the door, not open
it. Google's `hd` parameter is passed as a hint to the account chooser when `[auth] google_hd` is
set and is **never** the check: it is absent for consumer accounts, and it is not the identity.
A verified address is required before it is trusted at all, by both providers.

Every failure — a denied consent, a mismatched `state`, an unverified address, a domain that is
not ours, missing credentials, an unreachable provider — renders `login.jinja` with one sentence.
The callback is reached by a browser coming back from the provider, so a traceback there is a dead
end. An **unknown** provider segment is a bare **404** (`_oauth_provider`), `post_auth_dummy`'s
rule: a path this app does not serve should not admit that it exists.

`cocco/auth.py` never touches the network and does not know which provider asked: it is
`is_allowed_email`, `safe_next_path`, `nickname_from_email`, `oauth_redirect_uri` (one function
for both, so the two can only differ in the segment naming them) and `get_or_create_user`. Authlib
and the routes live in `main.py`, which is what keeps the interesting half unit-testable —
`test_auth_routes.py` replaces the whole of both providers with `_FakeOAuthClient`, three methods
including the `get` that is all of GitHub's API. The configuration is not here either: it is
`cocco/config.py`, because the judge reads the same file and a database URL is none of sign-in's
business. The dependency runs one way — `config.py` imports `is_allowed_email` and
`normalize_administrators` from here, and nothing here imports `config.py`.

The session is a signed cookie (Starlette's `SessionMiddleware`) holding nothing but `user_id`,
so a renamed or deleted account takes effect on the next request. `same_site` must stay `"lax"`:
the return from a provider is a top-level cross-site navigation, and under `"strict"` the cookie
carrying the `state` would not reach the callback, so every sign-in would fail as a state
mismatch. `_prune_stale_oauth_state` drops every `_state_*` entry rather than one provider's,
since only one flow can be in flight.

**`[auth] password_login` is the third real way in, and its default is the design.** An address
and a password, for the deployment neither provider can serve; off unless the file says otherwise,
and when it is off the feature *does not exist* — `_require_password_login` answers **404**, which
is `post_auth_dummy`'s rule, because hiding a form is presentation and never the check. A provider
proves that someone owns the mailbox; a password proves nothing about the address, and cocco has no mail
path anywhere in it, so it cannot send a verification link. Registration is therefore a **claim**,
and `claim_user_password` narrows it as far as it can be narrowed without email — a single
conditional INSERT whose `WHERE` is `first_login_at IS NULL`, with `ON CONFLICT DO NOTHING`, so the
rule is in the statement rather than in a check before it, the way `block_user` refuses an
administrator. `first_login_at` means exactly "this account has never been near the login page", so
a roster a teacher wrote out before term is claimable by its owner and an account somebody has
actually used never is. What remains — whoever types an unused institutional address first gets it
— is what a deployment accepts when it turns this on, and is why the default is false and why
`cocco.example.toml` spends a screen on it.

Three consequences. **`_complete_sign_in` stays the single funnel**, and registration is a
`claim_password=` keyword on it rather than a route with its own opinions: the allowlist is checked
*before* `get_or_create_user` writes anything, so an address outside `allowed_domains` never
becomes a row, and the blacklist refusal still runs afterwards. **Every way a sign-in can be wrong
is one sentence** — `_BAD_CREDENTIALS`, one module constant — because an unknown address, an
account with no password and a wrong password are the same event from outside; `verify_password`
takes `str | None` and turns None into False rather than short-circuiting, so they cost the same
work too, and a route that returned early for an address nobody has would be a way to read a class
list off the login page. Registration is the opposite and deliberately so: it is a form somebody is
filling in, so it says which field is wrong. And **scrypt runs in a threadpool** — 32 MiB of
sequential work on the event loop would stop every polled fragment in the app for its duration.

`cocco/passwords.py` is `tokens.py`'s deliberate opposite, and that file's `hash_token` already
says why: a token is 256 bits of `secrets`, so SHA-256 is right for it; a password is something a
person chose, so it needs a salt per password and a work factor. Stdlib `hashlib.scrypt`, no new
dependency, and `maxmem` passed explicitly because its 32 MiB default is exactly what `N=2**15,
r=8` needs plus overhead. The encoding `scrypt$n$r$p$salt$key` carries its own parameters, so
raising them later leaves every stored password working and `needs_rehash` upgrades each one the
next time its owner types it. **The hash lives in `user_password`, not on `user`**, for one reason
above the others: `_USER_COLUMNS` is read on every single request, and a credential has no business
travelling with the identity every route already holds. Keyed on `user.id` for `api_token`'s
reason — the account always exists — and no row means no password, which is what a
single-sign-on-only account looks like.

There are two places a password is set and they are not variations on one route. The **owner** sets
theirs at `/users/{id}/password`, behind `ProfileOwner` and never `ProfileReader` — that page
already draws the line between oversight and impersonation, and setting somebody's password is the
far side of it — and the current one is required whenever there is one to replace, because a
session left open on a shared machine is the ordinary way an account is taken. A **site
administrator** issues one from `/admin`, generated rather than chosen and shown exactly once,
`f-tokens.jinja`'s discipline; that is the whole answer to a forgotten password and the reason no
SMTP setting exists anywhere. It leaves `first_login_at` alone, so it never re-opens an account to
somebody else's claim. There is **no rate limiting** anywhere in this app, here included; that
belongs at the reverse proxy and `cocco.example.toml` says so.

**`[dev] dummy_auth` is a sign-in, not a bypass, and that is the whole of its design.** It puts a
text box on `/login`; `post_auth_dummy` takes the address typed into it and mints an ordinary
session, so everything downstream — the cookie, `get_user`, the block — is the **one** code path
Google's callback also lands on. It replaced a shortcut that signed every request in as an address
named in the file (`[dev] dummy_user`, now removed and warned about in both its spellings), which
made `get_user` two functions and meant seeing what a student sees was an edit and a restart — on
an app whose every permission is per course, so hopping between the seeded cast is the commonest
thing one does while developing it. Setting it on a public deployment is equivalent to publishing
every account's password.

Three consequences follow. The box **reuses the real rules**: `_complete_sign_in` holds the
allowlist check, `get_or_create_user` and the blacklist refusal, and the Google callback calls the
same function — a rule the browser enforced and the box did not would be a difference nobody could
explain, and the box is the configuration all of this gets developed in. `post_auth_dummy`
answers **404** when the switch is off, not 403, so a deployment does not admit the route exists;
hiding the form is presentation and never the check. And `/logout` now goes somewhere: it lands on
the box, which is how you become somebody else.

The seeded cast is `matteo.ceccarello@unipd.it` and `alice@unipd.it` (`teacher` on every course)
and `bob@unipd.it` and `charlie@unipd.it` (students); `_login_page` passes those names to the
template so the box can print them and they cannot drift from `_populate_dummy_data`. Matteo and
Alice differ in one thing only: he is a **site administrator**, a row in `administrator` rather
than a role anywhere, which is why a fresh checkout can create a course. `create_db_and_tables`
seeds him whenever dummy auth is on — a file every checkout shares should not name one person as
the administrator of all of them, so `cocco.dev.toml` deliberately lists none and this is what
keeps the checkout usable — and seeds only him, so typing `alice@unipd.it` still shows what plain
course staff may do instead of quietly promoting her.

`main.DUMMY_AUTH` is derived from `CONFIG.dev` **once, at import, and stays a module-level name** —
`post_auth_dummy`, `_login_page` and `create_db_and_tables` read it rather than `CONFIG.dev`,
because that is the seam six test files flip. A frozen dataclass field has no equivalent, so this
is not a tidy-up waiting to happen.
