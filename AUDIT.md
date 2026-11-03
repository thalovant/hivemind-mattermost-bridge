Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: First audit of the Thalovant fork: upstream defects fixed, open findings with evidence.

# Audit: hivemind-mattermost-bridge

Evidence: `python -m pytest tests -q` (123 passed, 2026-11-03, Python 3.12 and 3.14), `tests/integration/run.sh` (21/21 against Mattermost 12.0.1 in `mattermost/mattermost-preview` with a real hivemind-core hub), a mutation pass (17 behaviours gutted one at a time, each caught by a test), and reading the code on branch `feat/managed-bridge` (line numbers below refer to it) and upstream `dev` at `8d006eb`.

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

## 1. Every channel is answered unless an allow-list is set

- **Where:** `mattermost_bridge/__init__.py:165` (`allowed_channels`), `:167` (`auto` allows DMs without a list).
- **Finding:** with no `MATTERMOST_ALLOWED_CHANNELS`, every channel the bot is in and every DM reaches the hub.
- **Impact:** anyone who can add the bot to a channel or DM it spends the hub's capacity. A managed deployment always sets the list.

## 2. No per-person rate limit in the bridge

- **Where:** `HiveMindMattermostBridge.forward_to_hivemind` (`mattermost_bridge/__init__.py:564`).
- **Finding:** every addressed post is forwarded at once; limits are left to the hub.

## 3. A reply holds the hub's bus thread for up to 10 s

- **Where:** `speak` -> `MattermostAPI.create_post`, `timeout=self.timeout` (`mattermost_bridge/mattermost.py:228`, `DEFAULT_TIMEOUT = 10`).
- **Finding:** the REST call is blocking on the bus client's thread; a slow server delays every other reply.

## 4. A thread lookup pauses the websocket reader

- **Where:** `_in_bot_thread` (`mattermost_bridge/__init__.py:537`), awaited inline from `handle_posted` (`:505`).
- **Finding:** an unmentioned reply in a thread the bridge has not seen costs one `GET /posts/{id}` (up to 10 s), during which later events wait. Bounded to the first post of each thread (cache of 1024, `:561`); after a restart the cache is empty.

## 5. Events while the websocket is down are lost

- **Where:** `_listen_until_stopped` (`mattermost_bridge/__init__.py:370`).
- **Finding:** a reconnect starts a new connection; Mattermost's `connection_id`/`sequence_number` resume is not used, so a post made during the gap is never seen.

## 6. A reply over Mattermost's post limit is dropped

- **Where:** `speak` (`mattermost_bridge/__init__.py:614`), error log at `:635`.
- **Finding:** replies are posted verbatim; one longer than the server's limit (16383 characters by default) gets HTTP 400 and is logged by status and dropped.

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

## 11. Prerelease dependencies, base image by tag

- **Where:** `requirements.txt:1` (`hivemind-bus-client>=1.0.16a1`), `Dockerfile:1` (`FROM python:3.14-slim`).
- **Finding:** each build resolves the newest alphas and can pull a different base; images are not reproducible across builds.
