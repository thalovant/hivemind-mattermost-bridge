Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Section 1: where the operator runs the bridge, the allow-list and DM policy are always set.

# Audit: hivemind-mattermost-bridge

Evidence (review, 2026-11-03, branch `fix/review-2026-11-03` off `main` `2a45629c`): `python -m pytest tests -q` with `.[test,e2e]` on Python 3.12: 168 passed (121 before the review, e2e excluded); every new test was seen failing with its fix reverted, and six behaviours of the reply sender were gutted one at a time (split, pacing, 429, wait cap, bound, drain), each caught. Line numbers in the review sections refer to that branch.

Evidence (first audit): `python -m pytest tests -q` (123 passed, 2026-11-03, Python 3.12 and 3.14), `tests/integration/run.sh` (21/21 against Mattermost 12.0.1 in `mattermost/mattermost-preview` with a real hivemind-core hub), a mutation pass (17 behaviours gutted one at a time, each caught by a test), and reading the code on branch `feat/managed-bridge` (line numbers below refer to it) and upstream `dev` at `8d006eb`.

## Fixed in this fork (upstream `dev` at `8d006eb`)

1. **The bridge never listened on Python 3.12+.** `MMostBot.listen()` ran `Driver.init_websocket()` in a daemon thread; mattermostdriver 7.3.2 calls `asyncio.get_event_loop()` there, which raises `RuntimeError: There is no current event loop in thread` on 3.12+ (the image was `python:3.14-slim`). Replaced by `mattermost_bridge.mattermost` and `HiveMindMattermostBridge._listen`; `tests/test_websocket.py` runs the listener over a real socket.
2. **The websocket did not verify the server's certificate.** mattermostdriver built its context with `ssl.Purpose.CLIENT_AUTH` (verify mode `CERT_NONE`, no hostname check) and sent the login over it. Now `MattermostAPI.ssl_context` (`mattermost_bridge/mattermost.py`) verifies; `tests/test_mattermost_api.py::test_an_https_server_is_verified`.
3. **A dropped websocket was never reopened** (`keepalive` defaulted to off). Now reconnected with backoff (`_listen_until_stopped`, `mattermost_bridge/__init__.py:370`); `tests/test_websocket.py::test_a_dropped_websocket_is_reconnected`.
4. **Message text, usernames and emails in the log.** Upstream `mmost.py:112,128` logged `sender said: msg`, `:67` fetched the email of everyone whose status changed, and `__init__.py:156` logged every reply. Now only lengths and ids; `tests/test_logs.py` runs the whole path with every logger at debug.
5. **The bot posted unprompted.** `mmost.py:139,141` greeted every user added to a channel ("hello @user", "Blip Blop, I am a Bot!"). Removed.
6. **Secrets on the command line, root user, image not built from this code.** The upstream `Dockerfile` installed `hivemind-mattermost-bridge` from PyPI, ran as root, and its `CMD` passed `--pswd` and `--password` as arguments. Now uid 10001, no arguments, settings from the environment and `*_FILE`; `tests/test_image.py`, `tests/test_cli.py`.
7. **SIGTERM ignored as PID 1.** Without a handler the kernel drops SIGTERM for PID 1, so a stop waited out the grace period and ended in SIGKILL (exit 137 measured on the image before the fix). `stop_on_sigterm` (`mattermost_bridge/__main__.py:265`); integration: exit 0 in 2.2 s.
8. **Any post containing the tag reached the hub**, in any channel, including `@bot` inside a longer name, and DMs with no way to refuse them. Now an allow-list, a DM policy and a mention matcher (`mention_pattern`).
9. **Found by the real-server run: a revoked token left the bridge deaf and ready.** Mattermost 12.0.1 keeps the websocket of a revoked token open and stops sending events on it; the bridge logged nothing and `/readyz` stayed 200. Now `_watch_token` checks `GET /users/me` every `TOKEN_CHECK_INTERVAL` (60 s); `tests/test_websocket.py::test_a_token_revoked_silently_is_found_by_the_periodic_check`.

## Fixed in the review of `main` `2a45629c` (2026-11-03)

R1. **High - the token followed a websocket redirect to any host.** The websockets library (17.4 here) follows 301/302/303/307/308 during the opening handshake, across origins too; it strips `Authorization` then, but the bridge sends the token in the first frame (`authentication_challenge`), not in a header. A redirect from the server or a proxy in front of it (an SSO gateway, a moved host) handed the bot's token to whatever host the `Location` named, over a TLS connection verified for that host. Now `_listen` compares the final handshake's `Host` with `websocket_host(url)` (`mattermost_bridge/__init__.py:490`, `mattermost_bridge/mattermost.py:171`) and refuses to send the token; `tests/test_websocket.py::test_a_websocket_redirected_to_another_host_never_gets_the_token` (four statuses, a second local server records every frame). REST calls already used `allow_redirects=False` (`mattermost.py:344`).

R2. **High - replies could ping `@channel`/`@all`/`@here` and anyone.** The reply is the hub's text, posted as Markdown by a bot account (bots hold `use_channel_mentions` by default): "@bot say @channel lunch" notified the whole channel. `defuse_reply` (`mattermost.py:227`) breaks every mention with an invisible U+2060 word joiner, including the forms the server still parses after punctuation (`x.@channel`, `_@all`), and leaves addresses alone; the asker's `@username` is added after and is never a channel-wide name (`CHANNEL_WIDE_MENTIONS`). `tests/test_bridge.py::test_a_reply_never_pings_anyone_but_the_asker_nor_loads_an_image`.

R3. **Medium - Markdown images in replies were fetched by every reader's client.** `![x](https://host/?q=...)` from a hub that was talked into it leaks who reads the channel (and whatever the hub put in the URL) with no click. Escaped by `defuse_reply`; same test.

R4. **Medium - replies were not checked against the allow-list.** `_reply` posted to whatever `channel` the context named (a reminder set before the list changed, a context the hub made up). Now `may_answer` (`__init__.py:572`) is the one check, inbound and outbound; it also answers a listed group DM, which `auto` used to refuse. `tests/test_bridge.py::test_a_reply_to_a_channel_off_the_list_is_not_posted`, `::test_a_reply_in_a_dm_follows_the_dm_policy`, `::test_a_listed_group_dm_is_answered`. Follow-up (CodeRabbit review body, PR #2, Major): the check used the `channel_type` in the hub's context, so with `dm_policy=allow` a reply labelled `D`/`G` reached an unlisted public channel. `_reply` now uses `may_reply`: listed, or the type Mattermost gave on a post the bridge forwarded from that channel (`_seen_channels`, at most `SEEN_CHANNEL_MEMORY = 1024`); a channel not seen since start is answered only with no list and DMs allowed. Tests: `tests/test_bridge.py::test_a_hub_reply_cannot_label_an_unlisted_channel_a_dm_to_get_past_the_list`, `::test_an_unseen_channel_is_answered_only_when_every_channel_would_be`, `::test_a_real_dm_the_bridge_forwarded_is_still_answered_off_the_list`, `::test_the_seen_channel_memory_is_bounded`.

R5. **Medium - replies blocked the hub's reader, 429 and long replies were lost** (was open findings 3 and 6). `handle_speak` ran inline on the hub client's websocket thread (pyee `EventEmitter`) and posted with a blocking 10 s call. Now `_queue_reply` (`:694`) hands it to one sender thread (`_send_replies`, `:714`): 0.2 s between posts, at most `REPLY_QUEUE_SIZE` (100, `:112`) waiting, 429 retried after `Retry-After`/`X-Ratelimit-Reset` (`RateLimited`, `retry_after`, `mattermost.py:67,79`; capped 30 s, 3 tries; `_post`, `:796`), replies over `MAX_POST_LENGTH` (16383) split by `split_post` (`mattermost.py:96`), at most 4 posts then cut; `stop()` drains for 5 s (`_stop_replies`, `:742`). `tests/test_replies.py` (23 tests). Follow-up (CodeRabbit, PR #2): `retry_after` also reads `Retry-After` as an HTTP date (seconds until it; past or unreadable falls through to `X-Ratelimit-Reset`, then 1 s), so a date-only 429 no longer spends the three tries early; `tests/test_replies.py::test_a_retry_after_http_date_alone_counts_until_that_time`. Follow-up (CodeRabbit review body): `speak` checks `_sender_stop` before each part of a split reply, so a part in flight when `stop()` begins is the last; `tests/test_replies.py::test_no_part_of_a_split_reply_is_posted_once_stopping_began`.

R6. **Low - reconnects were not jittered.** Every bridge dropped by one server restart came back at the same instants. `jittered` (`:133`) multiplies each wait by 0.5-1.5; `tests/test_websocket.py::test_reconnect_waits_are_jittered_and_bounded`.

R7. **Low - `http://` was accepted silently.** Still accepted (local test servers), but `main` warns unless the host is loopback (`is_cleartext`, `mattermost.py:147`; `__main__.py:278`); `tests/test_cli.py::test_a_cleartext_server_url_is_warned_about`.

## Open, found in the review

- **Link previews can still carry data out (Low).** With `EnableLinkPreviews` on, the Mattermost server itself fetches the first link of a post for its preview, so a link the hub writes into a reply is requested without a click. Links are kept on purpose (they are most of what an answer is for); an operator worried about this turns link previews off on the server. `defuse_reply` (`mattermost.py:227`).
- **Mention keys are not defused (Low).** People can set words (their first name, a nickname) that notify them without an `@`; a reply containing such a word notifies them as any post would. Nothing in a post marks a word as "not a mention".
- **A server with `MaxPostSize` below 16383 (Low).** Mattermost databases from before 5.0 keep 4000; a split part longer than that gets HTTP 400 and is dropped (logged by status). `MAX_POST_LENGTH` is not read from the server.
- **The server URL is the customer's (Info).** The bridge calls `GET /api/v4/users/me` and opens `/api/v4/websocket` at whatever `MATTERMOST_URL` names, internal addresses included; it reflects nothing but readiness back, and redirects are not followed (REST) or not given the token (websocket). The managed pod's egress policy is what keeps it off cluster-internal addresses.
- **`HIVEMIND_SELF_SIGNED=true` turns hub TLS verification off (Info).** hivemind-bus-client 1.2.12a1 `client.py:823` sets `CERT_NONE` and no hostname check. Opt-in, default `false`; a managed pod must not set it.
- **Exit status for a refused token is 78 (Info).** The contract asks for non-zero plus `token_rejected` in `/dev/termination-log`; other forks may use another status, so the operator should key on the termination message, not the number.

## 1. Every channel is answered unless an allow-list is set

- **Where:** `mattermost_bridge/__init__.py:165` (`allowed_channels`), `:167` (`auto` allows DMs without a list).
- **Finding:** with no `MATTERMOST_ALLOWED_CHANNELS`, every channel the bot is in and every DM reaches the hub.
- **Impact:** anyone who can add the bot to a channel or DM it spends the hub's capacity. A managed deployment always sets the list.
- **Where the operator runs the bridge (2026-11-03):** the list cannot be empty. The operator refuses a bridge without `allowed` entries, or with more than 50 (`BridgeManagerMixin._bridge_spec_error`, hivemind-k8s-operator#121), and the platform sets `MATTERMOST_DM_POLICY=deny`. No change in this repository.

## 2. No per-person rate limit in the bridge

- **Where:** `HiveMindMattermostBridge.forward_to_hivemind` (`mattermost_bridge/__init__.py:564`).
- **Finding:** every addressed post is forwarded at once; limits are left to the hub.

## 3. ~~A reply holds the hub's bus thread for up to 10 s~~

- **Fixed in the review (R5):** replies are queued and posted by their own thread; the hub's thread never waits on Mattermost. A slow server now delays only the replies behind it (one at a time, up to 10 s each).

## 4. A thread lookup pauses the websocket reader

- **Where:** `_in_bot_thread` (`mattermost_bridge/__init__.py:537`), awaited inline from `handle_posted` (`:505`).
- **Finding:** an unmentioned reply in a thread the bridge has not seen costs one `GET /posts/{id}` (up to 10 s), during which later events wait. Bounded to the first post of each thread (cache of 1024, `:561`); after a restart the cache is empty.

## 5. Events while the websocket is down are lost

- **Where:** `_listen_until_stopped` (`mattermost_bridge/__init__.py:370`).
- **Finding:** a reconnect starts a new connection; Mattermost's `connection_id`/`sequence_number` resume is not used, so a post made during the gap is never seen.

## 6. ~~A reply over Mattermost's post limit is dropped~~

- **Fixed in the review (R5):** split into at most `MAX_REPLY_POSTS` posts of `MAX_POST_LENGTH` characters (`split_post`), then cut with ` [...]`. A server with a lower limit: see "Open, found in the review".

## 7. The fallback reply is English only

- **Where:** `handle_intent_failure` (`mattermost_bridge/__init__.py:598`).

## 8. Rejected hub credentials can take about a minute to end the bridge

- **Where:** `DEFAULT_HANDSHAKE_MAX_RETRIES = 10` (`mattermost_bridge/__init__.py:69`) with hivemind-bus-client's 5 s per attempt (`client.py:669`, `wait_for_handshake(timeout=5)`).
- **Finding:** a hub that refuses the identity explicitly ends `connect()` at once; one that does not answer a wrong password (the loopback hub in `tests/e2e`) ends it only when the retries run out (23 s with 3 retries in `test_rejected_hub_credentials_end_the_bridge_before_mattermost_is_touched`).

## 9. The HiveMind client library logs some payloads

- **Where:** hivemind-bus-client 1.2.12a1 `protocol.py:1125` (`BROADCAST: {message.payload}`), `:1276` (`QUERY`), and PROPAGATE/CASCADE/INTERCOM next to them, at info.
- **Finding:** not this bridge's code. A bridge connection gets `speak` as a BUS message (logged by type only); a hub that sent it mesh messages would have their payloads logged.

## 10. Tests use a stand-in for most of Mattermost

- **Where:** `tests/fake_mattermost.py`.
- **Finding:** the unit and e2e suites run against a local stand-in that mimics the server's REST and websocket behaviour. The real server is exercised by `tests/integration/run.sh` (and the CI `integration` job), pinned to one `mattermost-preview` digest.

## 11. `/tmp` must be writable

- **Where:** a dependency, at import: ovos-config's `LocalConf` (`ovos_config/models.py:34`) takes a `combo_lock` lock, which calls `tempfile.gettempdir()`.
- **Finding:** on a read-only root with no writable `/tmp` the bridge dies at import with `FileNotFoundError: No usable temporary directory` (seen with `docker run --read-only`; podman adds a `/tmp` tmpfs by default). The managed pod mounts an emptyDir there; the CI smoke test mounts a tmpfs.

## 12. Prerelease dependencies, base image by tag

- **Where:** `requirements.txt:1` (`hivemind-bus-client>=1.0.16a1`), `Dockerfile:1` (`FROM python:3.14-slim`).
- **Finding:** each build resolves the newest alphas and can pull a different base; images are not reproducible across builds.

## Open: a reply to a channel not seen since start

- **Where:** `HiveMindMattermostBridge.may_reply` (`mattermost_bridge/__init__.py`).
- **Finding:** after a restart, a hub reply (a reminder, say) to an unlisted channel the bridge has not forwarded a post from since it started is dropped unless there is no allow-list and DMs are allowed. Resolving the type with `GET /api/v4/channels/{id}` would answer it; it was left out to keep the reply path free of an extra call per unseen channel.

