Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: CodeRabbit review bodies on PR #2: a reply's channel type is the bridge's own, not the hub's; split replies stop with the bridge; Retry-After HTTP date.

# Configuration reference

Settings are resolved by `resolve_options` in `mattermost_bridge/__main__.py`:
a flag wins over its environment variable, and the variable over its
`<VARIABLE>_FILE` (secrets only). A malformed value is a usage error (exit
status 2) naming the variable.

## Mattermost

| Variable (flag) | Meaning | Default |
| --- | --- | --- |
| `MATTERMOST_URL` (`--url`) | The server's base URL. `https://chat.example.com`, or with a path prefix `https://example.com/mattermost`. A bare host means HTTPS; `http://` is accepted for a local test server, with a warning at start unless it is a loopback address (`is_cleartext`): the token would travel unencrypted. Credentials, a query or a fragment are refused (`normalise_url`). Point it at the server itself: a websocket handshake redirected to another host is refused before the token is sent (`websocket_host`). | required |
| `MATTERMOST_TOKEN`, `MATTERMOST_TOKEN_FILE` (`--token`) | The bot account's access token. | required |
| `MATTERMOST_ALLOWED_CHANNELS` (`--allowed-channel`, repeatable) | Channel ids (26 lowercase letters and digits), separated by commas or spaces. When set, only these channels are answered (a listed group DM too, whatever the DM policy). A list that is given but names nothing is refused, so it can never mean "every channel". | any channel the bot is in |
| `MATTERMOST_DM_POLICY` (`--dm-policy`) | `auto`, `allow` or `deny`, for one-to-one (`D`) and group (`G`) direct messages that are not listed. `auto` is `deny` when channels are listed, `allow` otherwise. | `auto` |
| `MATTERMOST_REQUIRE_MENTION` (`--no-require-mention`) | `true`/`false` (also `1/0`, `yes/no`, `on/off`). | `true` |
| `MATTERMOST_SESSION_SCOPE` (`--session-scope`) | `person` or `chat`. | `person` |

The bot's id and `@username` come from `GET /api/v4/users/me` with the token
(`HiveMindMattermostBridge.identify`).

## HiveMind

| Variable (flag) | Meaning | Default |
| --- | --- | --- |
| `HIVEMIND_HOST` (`--host`) | Hub host; `ws://` is added when no scheme is given. | identity file |
| `HIVEMIND_PORT` (`--port`) | Hub port. | `5678` |
| `HIVEMIND_ACCESS_KEY`, `_FILE` (`--access-key`, `--key`) | Access key from `hivemind-core add-client`. | identity file |
| `HIVEMIND_PASSWORD`, `_FILE` (`--password`) | Its password. | identity file |
| `HIVEMIND_SITE_ID` (`--site-id`) | This bridge's site id. | `mattermost` |
| `HIVEMIND_LANG` (`--lang`) | Language tag on forwarded utterances. | `en-us` |
| `HIVEMIND_SELF_SIGNED` (`--self-signed`) | Accept a self-signed hub certificate. | `false` |

The hub client is built without a node name, so its Noise static key is
`~/.config/hivemind/unnamed-node_noise.key` (hivemind-bus-client's path for
an unnamed node). The handshake is retried at most 10 times
(`DEFAULT_HANDSHAKE_MAX_RETRIES`), then `start()` fails.

## Process

| Variable (flag) | Meaning | Default |
| --- | --- | --- |
| `BRIDGE_HEALTH_PORT` (`--health-port`) | Serve `/healthz` and `/readyz` here. | off |
| `REQUESTS_CA_BUNDLE` or `SSL_CERT_FILE` | A CA file to verify the Mattermost server with (REST and websocket), for a private CA. | certifi's bundle |

## What is forwarded

`HiveMindMattermostBridge.handle_posted`, for each `posted` websocket event
received after the server's `hello`:

1. Dropped: posts with no text, system posts (non-empty `type`), posts by the
   bot itself and posts with `from_bot`, `from_webhook` or `from_plugin` in
   their props.
2. `may_answer`: a listed channel id is answered whatever its type. Other
   direct (`D`) and group-direct (`G`) channels: dropped unless the DM
   policy is `allow`. Other channels: dropped when an allow-list is set.
3. `addressed_text`: the bot's `@username` is removed (case-insensitive; not
   inside a longer name or an address; trailing `.`, `-`, `_` are
   punctuation). Outside a one-to-one DM, with mention gating on, the post
   must have mentioned the bot or be in a thread whose root the bot posted
   (looked up once with `GET /api/v4/posts/{root_id}`, then remembered; the
   bot's own top-level replies are remembered as they are posted).
4. Dropped while the HiveMind session is down.
5. Emitted as `recognizer_loop:utterance`.

## Reply routing

The utterance's context carries:

| Key | Value |
| --- | --- |
| `channel` | the channel id |
| `channel_type` | `O`, `P`, `D` or `G` |
| `root_id` | the thread the question was in, or `""` |
| `user` | `{"mattermost_username": ..., "mattermost_user_id": ...}` |
| `session.session_id` | `mattermost-<channel>-<user id>` (`person`) or `mattermost-<channel>` (`chat`) |

The hub echoes the context on `speak`. `handle_speak` only queues the reply
(the hub's thread never waits on Mattermost); the `mattermost-replies`
thread posts queued replies in order, `REPLY_INTERVAL` (0.2 s) apart, with
`speak()`:

1. Dropped: a reply without `channel`, or to a channel `may_reply` refuses:
   a listed channel is answered; any other is judged by `may_answer` (the
   same allow-list and DM policy as for questions) with the channel type
   Mattermost gave on a post the bridge forwarded from it, never the type in
   the hub's context (which could call a public channel a DM). A channel the
   bridge has not seen since it started is answered only when every channel
   would be: no allow-list and DMs allowed. A split reply posts no further
   part once the bridge is stopping.
2. `defuse_reply`: every mention in the hub's text is broken with an
   invisible U+2060 word joiner, so a reply never notifies `@channel`,
   `@all`, `@here`, a group or a person (`me@example.com` is left alone);
   Markdown images `![...](...)` are escaped, so no reader's client fetches
   a URL the hub wrote. Other Markdown is kept.
3. `@username ` of the asker in front, outside one-to-one DMs (never
   `channel`, `all` or `here`).
4. Longer than `MAX_POST_LENGTH` (16383 characters, Mattermost's default):
   split at a paragraph, line or space (`split_post`), at most
   `MAX_REPLY_POSTS` (4) posts, then cut with ` [...]`.
5. `POST /api/v4/posts` to `channel`, in `root_id` when set.

`hive.complete_intent_failure` gets "I don't know how to answer that". At
most `REPLY_QUEUE_SIZE` (100) replies wait; a reply past that is dropped and
logged.

## Failure handling

| Failure | What happens |
| --- | --- |
| Mattermost unreachable at start or while running | retried, 1 s doubling to 30 s, each wait jittered x0.5-1.5 (`jittered`); `/readyz` 503 meanwhile |
| Websocket closed or ping timeout | reconnected the same way |
| Websocket never confirmed with `hello` (30 s) | the token is checked with `GET /users/me`, then reconnected |
| HTTP 401 on any call | `TokenRejected`: the bridge stops, `token_rejected` goes to `/dev/termination-log`, exit status 78 |
| Token revoked while connected | the server keeps the websocket open and stops sending events on it; `GET /users/me` every 60 s (`TOKEN_CHECK_INTERVAL`) finds the 401, as above |
| A reply is rate limited (HTTP 429) | retried after `Retry-After` (seconds, or an HTTP date: the time until it) / `X-Ratelimit-Reset` (at most `RATE_LIMIT_MAX_WAIT`, 30 s), `RATE_LIMIT_ATTEMPTS` (3) tries, then dropped and logged |
| A reply fails otherwise (HTTP 4xx/5xx, network) | logged with the channel id and status, dropped |
| Websocket handshake redirected to another host | the token is not sent; logged, reconnected with backoff; `/readyz` 503 |
| Hub refuses the credentials or never completes the handshake | `start()` raises after the bounded retries; exit status 1 |
| SIGTERM | stops like Ctrl-C: websocket and hub session closed, queued replies given `REPLY_DRAIN_TIMEOUT` (5 s), exit status 0 |
