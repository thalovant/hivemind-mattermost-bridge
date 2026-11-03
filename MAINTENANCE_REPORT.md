Last Edit: Claude Opus 5.5 - 2026-11-03 - Motive: Thalovant fork set up: bot-token client, managed image, CI, real-server integration, docs set.

# Maintenance report: hivemind-mattermost-bridge

## 2026-11-03: Thalovant fork

**Branches:** `main` made from upstream `dev` (`8d006eb`) and set as the default; the `dev` mirror deleted. Upstream's other branches (`master`, `release-*`, feature branches) are left as they were.

**Changes** (each its own commit on `feat/managed-bridge`, oldest first)

1. Mattermost client rewritten on the REST v4 API and the websocket with a bot access token (`mattermost_bridge/mattermost.py`), replacing mattermostdriver: the old listener crashed on Python 3.12+, did not verify the websocket's TLS certificate and never reconnected (AUDIT.md "Fixed" 1-3). Channel allow-list, DM policy (`auto`/`allow`/`deny`), mention gating with bot threads, per-person sessions (default), replies in the question's thread, posts by bots/webhooks/plugins ignored, `/healthz` and `/readyz`, settings from the environment and `*_FILE`, `token_rejected` + exit 78 on a refused token, no message text or token in any log. `--mail`, `--pswd`, `--tag`, `MMostBot` and `examples/echobot.py` removed. Tests rewritten against a local Mattermost stand-in; the e2e suite runs the real bridge with a real loopback hub.
2. Image: built from this checkout, uid 10001, `HOME=/home/bridge`, entrypoint without arguments, read-only root; `wait-for-creds.sh` removed; SIGTERM handled (PID 1).
3. CI: upstream workflows (alpha and stable release to PyPI, image push to upstream's registry, gh-automations test and label workflows) removed; `.github/workflows/ci.yml` added like the Telegram fork's (tests on 3.12 and 3.14 with the e2e extra, ruff `F,E9`, image smoke test including a read-only start, and on `main` only: push `:sha-<commit>`, cosign keyless sign and verify pinned to `ci.yml@refs/heads/main`, then `:latest`), plus an `integration` job running `tests/integration/run.sh` against `mattermost-preview` pinned by digest; publishing waits for it.
4. `tests/integration/`: the built image against a real Mattermost server and a real hub, scripted.
5. Token check every 60 s (`_watch_token`): the real-server run showed that a revoked token leaves the websocket open and silent, so the bridge went deaf while ready (two earlier passes had only won a race between the next post and the revocation).
6. Docs set: README, `docs/` (index, setup, configuration, operator-setup, examples), QUICK_FACTS, FAQ, AUDIT, SUGGESTIONS, this report; AGENTS.md gets the fork section and repo rules for the new design.

**Rationale:** run the bridge as a managed workload, the same contract as the Telegram and Discord forks: no secrets in process arguments, nothing anyone wrote in the logs, nothing forwarded that was not addressed to the bot, probes a platform can use, a refused token reported as such.

**Verification**

- `python -m pytest tests -q` with `.[test,e2e]`: 123 passed on Python 3.12 and on 3.14 locally; CI `test (3.12)` and `test (3.14)` green on the pull request. Local runs used a wrapper that disables ovos-config's file watcher, because this host had no inotify watches left; CI needs none.
- Mutation pass: 17 behaviours gutted one at a time (periodic token check, allow-list, DM policy, mention stripping, bot-thread lookup, hello before reading, token check after a failed login, readiness on a dropped socket, thread in replies, bot posts skipped, hub-down drop, per-person sessions, TLS verification, websocket logger level, strict booleans, empty allow-list, termination message): each made a test fail.
- `ruff check --isolated --select F,E9 .`: clean.
- Image built locally with podman: `--help` answers; runs as uid 10001; refuses to start without the URL or the token and names the variable, on a read-only root; on a read-only root with tmpfs at `/tmp` and `~/.config/hivemind` it builds the bridge and resolves the Noise key to `/home/bridge/.config/hivemind/unnamed-node_noise.key`. Against a loopback hub and a stand-in server, with the pod's mounts (read-only Noise key, credentials and token files) it answered a mention, wrote `token_rejected` and exited 78 on a wrong token, and logged none of the question, the answer or the token.
- `tests/integration/run.sh` against Mattermost 12.0.1 (`mattermost/mattermost-preview`) and a real hivemind-core hub: 21/21 checks passed (read-only, uid 10001, no secret in the arguments, `/healthz`, `/readyz`, mention answered top level, thread mention answered in the thread, reply under the bot's answer answered without a mention, unmentioned post, other channel and DM ignored, hub never heard the ignored posts, one session per person per channel, nothing sensitive logged, revoked token -> exit 78 + `token_rejected`, restart with the kept Noise key accepted, SIGTERM -> exit 0 in about 2 s).
- CI on the pull request: `test (3.12)`, `test (3.14)` and `integration` (21/21 with docker on the GitHub runner, Mattermost 12.0.1 pinned by digest) passed on the first run; `smoke` failed because `docker run ... --help | grep -q` broke the pipe under `pipefail` (grep stops reading at the first match). Fixed by writing the help to a file.
- Not verified: a production Mattermost server behind a reverse proxy, a deployment by the platform, the publish steps (they run only on `main`).

**Transparency report**

- **AI model:** Claude Opus 5.5 (`claude-opus-5-5`), in Claude Code.
- **Actions taken:** wrote the Mattermost client, the bridge changes, the CLI, the image changes, the CI workflow, the tests (unit, e2e, integration) and these documents; ran the test suites, the mutation pass, the local image checks and the real-server integration run.
- **Oversight:** directed by the repository owner through a session lead (fork, fix, follow the Telegram fork's contract, prove it against a real Mattermost server); no human code review yet.
