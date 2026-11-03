Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Rewritten for the bot-token client: settings, what is forwarded, reply routing.

# Configuration reference

Settings are resolved by `resolve_options` in `mattermost_bridge/__main__.py`:
a flag wins over its environment variable, and the variable over its
`<VARIABLE>_FILE` (secrets only). A malformed value is a usage error (exit
status 2) naming the variable.

## Mattermost

| Variable (flag) | Meaning | Default |
| --- | --- | --- |
| `MATTERMOST_URL` (`--url`) | The server's base URL. `https://chat.example.com`, or with a path prefix `https://example.com/mattermost`. A bare host means HTTPS; `http://` is accepted for a local test server. Credentials, a query or a fragment are refused (`normalise_url`). | required |
| `MATTERMOST_TOKEN`, `MATTERMOST_TOKEN_FILE` (`--token`) | The bot account's access token. | required |
| `MATTERMOST_ALLOWED_CHANNELS` (`--allowed-channel`, repeatable) | Channel ids (26 lowercase letters and digits), separated by commas or spaces. When set, only these channels are answered. A list that is given but names nothing is refused, so it can never mean "every channel". | any channel the bot is in |
| `MATTERMOST_DM_POLICY` (`--dm-policy`) | `auto`, `allow` or `deny`, for one-to-one (`D`) and group (`G`) direct messages. `auto` is `deny` when channels are listed, `allow` otherwise. | `auto` |
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
2. Direct (`D`) and group-direct (`G`) channels: dropped when the DM policy
   is `deny`. Other channels: dropped when not in the allow-list.
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

The hub echoes the context on `speak`; `speak()` posts the utterance with
`POST /api/v4/posts` to `channel`, in `root_id` when set, with `@username `
in front outside one-to-one DMs. `hive.complete_intent_failure` gets "I
don't know how to answer that". A reply without `channel` is dropped.

## Failure handling

| Failure | What happens |
| --- | --- |
| Mattermost unreachable at start or while running | retried, 1 s doubling to 30 s; `/readyz` 503 meanwhile |
| Websocket closed or ping timeout | reconnected the same way |
| Websocket never confirmed with `hello` (30 s) | the token is checked with `GET /users/me`, then reconnected |
| HTTP 401 on any call | `TokenRejected`: the bridge stops, `token_rejected` goes to `/dev/termination-log`, exit status 78 |
| Token revoked while connected | the server keeps the websocket open and stops sending events on it; `GET /users/me` every 60 s (`TOKEN_CHECK_INTERVAL`) finds the 401, as above |
| A reply fails otherwise (HTTP 4xx/5xx, network) | logged with the channel id and status, dropped |
| Hub refuses the credentials or never completes the handshake | `start()` raises after the bounded retries; exit status 1 |
| SIGTERM | stops like Ctrl-C: websocket and hub session closed, exit status 0 |
