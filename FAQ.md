Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: The managed deployment mounts both identity keys; the operator keys a rejected token on the termination message.

# FAQ: hivemind-mattermost-bridge

## Why does the bot ignore some posts in a channel?

Outside a one-to-one DM, `HiveMindMattermostBridge.addressed_text` forwards only posts that mention `@botname` or that reply in a thread the bot started, and removes the mention first. `MATTERMOST_REQUIRE_MENTION=false` forwards every post the bot can read. Posts in channels not in `MATTERMOST_ALLOWED_CHANNELS`, posts by bots, webhooks and plugins, and system posts are never forwarded (`handle_posted`).

## Why does the bot not answer my DM?

`MATTERMOST_DM_POLICY` decides one-to-one and group DMs. Its default, `auto`, refuses them when `MATTERMOST_ALLOWED_CHANNELS` is set, so a restricted bot cannot be reached around its channel list. Set `allow` to accept DMs as well. A group DM also needs the mention.

## Where does the reply go?

To the question's channel, and into its thread when the question was in one (`root_id` in the utterance context). Outside one-to-one DMs it starts with the asker's `@username` (`HiveMindMattermostBridge.speak`). A reply to a channel the bridge does not serve (off the allow-list, or a DM the policy refuses) is not posted (`may_answer`).

## Can the bot be made to ping @channel or @all?

No. The reply is the hub's text, and an assistant asked to "say @channel ..." will write it, so `defuse_reply` (`mattermost_bridge/mattermost.py`) breaks every mention in it with an invisible U+2060 word joiner before posting: `@channel`, `@all`, `@here`, groups and people are shown but not notified. The only mention the bot makes is the asker's `@username` in front. Markdown images are escaped too, because every reader's client would fetch the image URL.

## Why did a long answer arrive as several posts?

Mattermost takes at most 16383 characters per post (its default `MaxPostSize`). A longer reply is split at a paragraph, line or space (`split_post`), up to 4 posts (`MAX_REPLY_POSTS`), and the rest is cut with ` [...]`. A server configured for less than 16383 answers HTTP 400 and the reply is dropped (logged by status).

## Does it log email login or password, like upstream?

No. Upstream's `--mail`/`--pswd` login is gone; the bridge uses a bot access token (`MATTERMOST_TOKEN`/`_FILE`), and never logs it, nor any question or reply (`tests/test_logs.py`). The websocket library's frame logging is pinned off (`_WEBSOCKET_LOG`).

## What does exit status 78 mean?

The Mattermost server answered HTTP 401: the token is wrong, revoked, or the bot is disabled. The bridge writes `token_rejected` to `/dev/termination-log` and stops (`mattermost_bridge/__main__.py`, `main`). Network failures are retried instead. A token revoked while the bridge runs is found within a minute: the server keeps the websocket open but stops sending on it, so the bridge asks `GET /users/me` every 60 s (`_watch_token`). The operator reads the termination message, not the status, so `token_rejected` is reported as `TokenRejected` whatever the exit status (hivemind-k8s-operator#121).

## What does `/readyz` check?

That the HiveMind session is live (the client's `handshake_event`) and that the Mattermost websocket is open and has received `hello` (`HiveMindMattermostBridge.ready`). `/healthz` only says the process answers.

## Why must the Noise key stay the same?

The hub pins the bridge's Noise static key (`~/.config/hivemind/unnamed-node_noise.key`) on first contact and refuses a different one; `tests/integration/run.sh` saw exactly that when a run started a second container with a new key. Mount the kept key there. The RSA identity beside it (`unnamed-node.pem`) is not refused when it changes, but the hub warns on each new one. Where the operator runs the bridge, both keys come read-only from the bridge's credentials Secret (hivemind-k8s-operator#122).

## Which channel ids go in `MATTERMOST_ALLOWED_CHANNELS`?

The 26-character channel ids (channel name → View Info), not channel names. A name, an uppercase id or an empty list stops the bridge with a usage error.

## Why is the default branch `main` and not `dev`?

This is the Thalovant fork. Upstream's release workflows were removed, so nothing here publishes to PyPI or to upstream's registry; images go to `ghcr.io/thalovant/hivemind-mattermost-bridge`.
