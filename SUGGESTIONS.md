Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Review: two suggestions done (reply sender, split), three added (https-only URL, server post limit, operator keys on the termination message).

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

## Require https:// for managed bridges

- **Problem:** `http://` is accepted (with a warning, `is_cleartext`) so local test servers work; a managed bridge pointed at an `http://` server sends the bot's token and every message in the clear.
- **Proposal:** the platform API that takes the customer's server URL refuses `http://`; optionally a `MATTERMOST_ALLOW_HTTP` switch here that defaults to off outside loopback.
- **Impact:** medium; a one-line validation where the URL enters the platform.

## Key on the termination message, not the exit status

- **Problem:** a refused token exits 78 here; other chat-bridge forks may use another non-zero status.
- **Proposal:** the operator treats `token_rejected` in the container's termination message as the signal, whatever the status.
- **Impact:** low; avoids a per-network special case.

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

## Platform integration

- **Proposal:** run the image as the managed bridge workload, like the Telegram and Discord forks: the network needs `MATTERMOST_URL` beside the token, channel ids that are 26-character strings (not numbers), and `MATTERMOST_DM_POLICY` passed through when a deployment wants DMs.
- **Impact:** high; this is what the fork is for.
