Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Platform state: the operator keys on the termination message and renders the bridge without a network table; the https-only URL check is in the platform API's connector catalog change.

# Suggestions: hivemind-mattermost-bridge

## Resume the websocket after a gap

- **Problem:** AUDIT.md section 5: posts made while the websocket is down are never seen.
- **Proposal:** keep the `connection_id` from `hello` and the last `seq`, and reconnect with `?connection_id=...&sequence_number=...` so the server replays what was missed (check which server versions support the resume before relying on it).
- **Impact:** medium; a server restart no longer loses questions.

## Thread lookups off the websocket reader

- **Problem:** AUDIT.md section 4 (section 3, replies on the bus thread, was fixed in the review: replies have their own sender thread).
- **Proposal:** run `_in_bot_thread` lookups as tasks so a slow `GET /posts/{id}` does not hold later events.
- **Impact:** low.

## Learn the server's post limit

- **Problem:** AUDIT.md "Open, found in the review": long replies are split at 16383 characters (`MAX_POST_LENGTH`), which a server with an older database refuses.
- **Proposal:** read the limit at start, or on an HTTP 400 for a long post re-split at 4000 once.
- **Impact:** low; long answers and old databases are both rare.

## Require https:// for managed bridges (in review in the platform API, 2026-11-03)

- **Problem:** `http://` is accepted (with a warning, `is_cleartext`) so local test servers work; a managed bridge pointed at an `http://` server sends the bot's token and every message in the clear.
- **Proposal:** the platform API that takes the customer's server URL refuses `http://`; optionally a `MATTERMOST_ALLOW_HTTP` switch here that defaults to off outside loopback.
- **State:** the API check (https, port 443 only, a host name that resolves to public addresses only, checked on create and on every settings edit) is part of thalovant-api#467, in review. The switch here is not made.
- **Impact:** medium; a one-line validation where the URL enters the platform.

## Key on the termination message, not the exit status (done in the operator, 2026-11-03)

- **Problem:** a refused token exits 78 here; the Matrix and Twitch forks exit 3.
- **Done:** the operator reports `TokenRejected` when the bridge container's last termination message is `token_rejected`, whatever the exit status (`BridgeManagerMixin._bridge_token_rejected`, hivemind-k8s-operator#121).

## Per-person rate limit in the bridge

- **Problem:** AUDIT.md section 2.
- **Proposal:** a token bucket per (channel, person) in `forward_to_hivemind`, with a short "slow down" reply, configurable by flag and environment; useful where the hub has no quota of its own.
- **Impact:** medium for self-hosted use; the managed platform already limits per person on the hub.

## Localised fallback line

- **Problem:** AUDIT.md section 7.
- **Proposal:** a small per-language table keyed by `HIVEMIND_LANG`, or let the hub supply it.
- **Impact:** low.

## Reproducible images

- **Problem:** AUDIT.md section 11.
- **Proposal:** pin the base image by digest and build from a constraints file generated in CI; Renovate updates both (and the `mattermost-preview` digest in `ci.yml`).
- **Impact:** medium; makes a rollback mean something.

## Platform integration (done in the operator, 2026-11-03)

- **Proposal:** run the image as the managed bridge workload, like the Telegram and Discord forks: the network needs `MATTERMOST_URL` beside the token, channel ids that are 26-character strings (not numbers), and `MATTERMOST_DM_POLICY` passed through when a deployment wants DMs.
- **Done:** hivemind-k8s-operator#121 keeps no network table. The platform renders `env` (`MATTERMOST_URL`, `MATTERMOST_REQUIRE_MENTION`, `MATTERMOST_SESSION_SCOPE`, `MATTERMOST_DM_POLICY=deny`), `secretEnv` (`MATTERMOST_TOKEN`, mounted as `MATTERMOST_TOKEN_FILE`) and `allowedEnv` (`MATTERMOST_ALLOWED_CHANNELS`), and the operator applies them within guard rails. The Mattermost entry in the platform's connector catalog (channel ids `^[a-z0-9]{26}$`, at most 50; egress 443 only) is in review in thalovant-api#467, hidden until its first version is published.
