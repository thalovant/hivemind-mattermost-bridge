Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: The image against a real Mattermost server and a real hub, scripted.

# Integration: the image against a real Mattermost server

`run.sh` starts `mattermost/mattermost-preview` (Mattermost with its own
database) in a container, then `real_mattermost.py`:

1. sets the scene through the REST API: an admin, a team, two public
   channels (`allowed`, `other`), a bot account with two access tokens, a
   person `alice`; the bot and alice in both channels; a DM between them;
2. starts a real hivemind-core hub on 127.0.0.1 (hivescope loopback) whose
   agent answers `answer to <first word of the question>`;
3. runs the bridge image the way the platform does: `--read-only`,
   `--user 10001:10001`, `--cap-drop ALL`, tmpfs at `/tmp` and
   `/home/bridge/.config/hivemind`, the Noise key, hub credentials and bot
   token as read-only files, every setting from the environment
   (`MATTERMOST_ALLOWED_CHANNELS` = `allowed`, DM policy left at `auto`);
4. checks, posting as alice:

| Check | How |
| --- | --- |
| runs read-only as uid 10001, no secret in its arguments | container inspect, `id -u` |
| `/healthz` 200, `/readyz` 200 | from the host |
| a mention in `allowed` is answered there, top level, `@alice ` first | the bot's posts in `allowed` |
| the hub is asked without the mention, one session per person per channel | the hub's agent |
| an unmentioned post is ignored | followed by a mention that is answered |
| a mention in someone else's thread is answered in that thread | `root_id` of the reply |
| a reply in the thread under the bot's answer needs no mention | `root_id` of the reply |
| a mention in `other` and a DM are ignored | followed by a control that is answered |
| no question, answer or secret in the container log | container log |
| a revoked token stops the bridge: exit 78, `token_rejected` in the termination log (the server keeps the websocket open and goes quiet, so the bridge's 60 s token check finds it) | container state, the mounted termination log |
| SIGTERM stops it cleanly (exit 0) well inside the grace period | `stop -t 20` |

The bridge reads the websocket in order, so when the control's reply has
arrived, a reply to an ignored post would already be there.

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install --pre -e ".[e2e]"
tests/integration/run.sh                 # builds the image from this checkout
tests/integration/run.sh my/image:tag    # or tests a built one
```

| Variable | Meaning |
| --- | --- |
| `CONTAINER_ENGINE` | `podman` or `docker` (default: whichever is installed); may carry flags, e.g. `podman --cgroup-manager=cgroupfs` where systemd scopes fail |
| `PYTHON` | a Python with this package and its `e2e` extra (default `.venv/bin/python`) |
| `MATTERMOST_IMAGE` | default `docker.io/mattermost/mattermost-preview:latest`; CI pins a digest |
| `MATTERMOST_PORT` | host port for the server, default 18065 |
| `KEEP_MATTERMOST=1` | leave the server running |

It needs `--network host` (the bridge reaches the server and the hub on
127.0.0.1), and takes a minute or two, most of it the server starting. Exit
status 0 only when every check passes. The bridge's log is kept in a
temporary directory printed at the end.
