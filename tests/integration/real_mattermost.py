"""The built image against a real Mattermost server and a real HiveMind hub.

Run it with ``tests/integration/run.sh`` (which starts the Mattermost server);
see ``tests/integration/README.md``. Not collected by pytest: it needs a
container engine and a few minutes.

What it sets up:

- on the Mattermost server (``--mattermost``, already running): an admin, a
  team, two public channels (``allowed`` and ``other``), a bot account
  ``hivebot`` with an access token, a person ``alice``; the bot and alice are
  members of both channels;
- a real hivemind-core hub on 127.0.0.1 (hivescope loopback), whose agent
  answers ``answer to <first word>``, so each reply names its question;
- the image under test, run as the platform runs it: read-only root, uid
  10001, no capabilities, the identity directory and /tmp as tmpfs, the
  Noise key, the hub credentials and the bot token as read-only files, every
  setting from the environment (``MATTERMOST_ALLOWED_CHANNELS`` = the
  ``allowed`` channel, the DM policy left at ``auto``).

What it checks, as alice through the REST API: a mention in ``allowed`` is
answered there; a mention in a thread is answered in that thread; a reply in
the thread under the bot's answer is answered without a mention; a mention
in ``other``, an unmentioned post and a DM are ignored; ``/readyz`` is 200.
Each ignored post is followed by one that is answered, and the bridge reads
the websocket in order, so a reply to the ignored post would have arrived
first. Then the bot's token is revoked and the bridge must stop with
``token_rejected`` in its termination log and exit status 78.

Exit status 0 only if every check passes.
"""
import argparse
import json
import os
import secrets
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import requests

import ovos_config.config as _config

# hivemind_core starts a config file watcher at import; it needs inotify
# watches this check has no use for, and some hosts have none left.
_config.Configuration.set_config_watcher = staticmethod(lambda callback=None: None)

from ovos_bus_client.message import Message  # noqa: E402
from hivescope.topology import TopologyBuilder  # noqa: E402

KEY = "integration-bridge-key"
PASSWORD = "correct-horse-battery-staple-" + secrets.token_hex(12)
ADMIN_PASSWORD = "Admin-Passw0rd!" + secrets.token_hex(4)
ALICE_PASSWORD = "Alice-Passw0rd!" + secrets.token_hex(4)
NOISE_KEY = secrets.token_hex(32)
TOKEN_REJECTED_EXIT_CODE = "78"


class Server:
    """Just enough of the Mattermost REST API to set the scene and read it back."""

    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self.token = None
        self.version = None

    def call(self, method: str, path: str, token: str = None, expect=(200, 201), **kwargs):
        headers = {"X-Requested-With": "XMLHttpRequest"}
        if token or self.token:
            headers["Authorization"] = f"Bearer {token or self.token}"
        response = requests.request(method, f"{self.url}/api/v4{path}", headers=headers,
                                    timeout=15, **kwargs)
        if response.status_code not in expect:
            raise RuntimeError(f"{method} {path}: HTTP {response.status_code} {response.text[:300]}")
        return response

    def json(self, method: str, path: str, token: str = None, **kwargs):
        return self.call(method, path, token=token, **kwargs).json()

    def login(self, username: str, password: str) -> str:
        response = self.call("POST", "/users/login", json={"login_id": username, "password": password})
        return response.headers["Token"]

    def wait_up(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                response = requests.get(f"{self.url}/api/v4/system/ping", timeout=3)
                if response.status_code == 200:
                    self.version = response.headers.get("X-Version-Id")
                    return
            except requests.RequestException:
                pass
            time.sleep(2)
        raise RuntimeError(f"Mattermost at {self.url} did not come up in {timeout} s")


def set_scene(server: Server) -> dict:
    """Admin, team, channels, bot with a token, alice. Returns the ids and tokens."""
    run = secrets.token_hex(3)
    admin_name = f"admin{run}"
    users = server.call("GET", "/users?per_page=1", expect=(200, 401, 403))
    server.json("POST", "/users", json={"email": f"{admin_name}@example.com", "username": admin_name,
                                        "password": ADMIN_PASSWORD})
    server.token = server.login(admin_name, ADMIN_PASSWORD)
    if users.status_code == 200 and users.json():
        print("note: the server already had users; this admin may not be a system admin")
    server.json("PUT", "/config/patch", json={"ServiceSettings": {
        "EnableBotAccountCreation": True, "EnableUserAccessTokens": True}})
    team = server.json("POST", "/teams", json={"name": f"bridge-{run}", "display_name": f"Bridge {run}",
                                               "type": "O"})
    allowed = server.json("POST", "/channels", json={"team_id": team["id"], "name": f"allowed-{run}",
                                                     "display_name": "allowed", "type": "O"})
    other = server.json("POST", "/channels", json={"team_id": team["id"], "name": f"other-{run}",
                                                   "display_name": "other", "type": "O"})
    bot = server.json("POST", "/bots", json={"username": f"hivebot{run}", "display_name": "HiveBot"})
    token = server.json("POST", f"/users/{bot['user_id']}/tokens", json={"description": "integration"})
    second = server.json("POST", f"/users/{bot['user_id']}/tokens", json={"description": "stop check"})
    alice = server.json("POST", "/users", json={"email": f"alice{run}@example.com",
                                                "username": f"alice{run}", "password": ALICE_PASSWORD})
    for user_id in (bot["user_id"], alice["id"]):
        server.json("POST", f"/teams/{team['id']}/members", json={"team_id": team["id"], "user_id": user_id})
        for channel in (allowed, other):
            server.json("POST", f"/channels/{channel['id']}/members", json={"user_id": user_id})
    alice_token = server.login(alice["username"], ALICE_PASSWORD)
    direct = server.json("POST", "/channels/direct", token=alice_token,
                         json=[alice["id"], bot["user_id"]])
    return {"team": team["id"], "allowed": allowed["id"], "other": other["id"],
            "bot_id": bot["user_id"], "bot_username": bot["username"], "bot_token": token["token"],
            "bot_token_id": token["id"], "bot_token_2": second["token"], "alice_id": alice["id"], "alice": alice["username"],
            "alice_token": alice_token, "direct": direct["id"]}


class Hub:
    """A real hivemind-core hub whose agent answers ``answer to <first word>``."""

    def __init__(self):
        self.asked = []
        self.builder = TopologyBuilder()
        self.master = self.builder.add_master("M0", use_loopback=True)
        self.master.register_satellite(KEY, password=PASSWORD, allowed_types=["recognizer_loop:utterance"])
        self.builder.start_all()
        bus = self.master.agent_protocol.bus

        def respond(msg: Message) -> None:
            utterance = (msg.data.get("utterances") or [""])[0]
            self.asked.append({"utterance": utterance,
                               "session": msg.context.get("session", {}).get("session_id")})
            context = dict(msg.context)
            context["destination"] = context.get("source") or context.get("peer")
            tag = utterance.split()[0] if utterance.split() else "?"
            bus.emit(Message("speak", {"utterance": f"answer to {tag}"}, context))

        bus.on("recognizer_loop:utterance", respond)
        host, port = self.master.network_protocol.url.replace("ws://", "").rstrip("/").split(":")
        self.host, self.port = host, int(port)

    def stop(self) -> None:
        self.builder.stop_all()


def engine() -> list:
    given = os.environ.get("CONTAINER_ENGINE")
    if given:
        return shlex.split(given)
    for candidate in ("podman", "docker"):
        if subprocess.run(["which", candidate], capture_output=True).returncode == 0:
            return [candidate]
    raise SystemExit("no container engine: set CONTAINER_ENGINE")


def start_bridge(image: str, scene: dict, hub: Hub, server_url: str, health_port: int,
                 work: str, name: str, token: str) -> None:
    # One Noise key for every run of this bridge, as the platform keeps it in
    # a Secret: the hub pins the key on first contact and refuses another.
    files = {"apiKey": KEY, "password": PASSWORD, "token": token,
             "noiseKey": NOISE_KEY, "termination-log": ""}
    for file_name, value in files.items():
        path = os.path.join(work, file_name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(value)
        # The test's own throwaway values; the container user must read them.
        os.chmod(path, 0o666 if file_name == "termination-log" else 0o644)
    command = engine() + [
        "run", "-d", "--name", name, "--network", "host",
        "--read-only", "--user", "10001:10001", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--tmpfs", "/tmp", "--tmpfs", "/home/bridge/.config/hivemind:mode=1777",
        "-v", f"{work}/noiseKey:/home/bridge/.config/hivemind/unnamed-node_noise.key:ro,z",
        "-v", f"{work}/apiKey:/var/run/thalovant/hivemind/apiKey:ro,z",
        "-v", f"{work}/password:/var/run/thalovant/hivemind/password:ro,z",
        "-v", f"{work}/token:/var/run/thalovant/bridge-token/token:ro,z",
        "-v", f"{work}/termination-log:/dev/termination-log:rw,z",
        "-e", "HOME=/home/bridge",
        "-e", f"HIVEMIND_HOST=ws://{hub.host}", "-e", f"HIVEMIND_PORT={hub.port}",
        "-e", "HIVEMIND_SITE_ID=integration-bridge",
        "-e", "HIVEMIND_ACCESS_KEY_FILE=/var/run/thalovant/hivemind/apiKey",
        "-e", "HIVEMIND_PASSWORD_FILE=/var/run/thalovant/hivemind/password",
        "-e", f"MATTERMOST_URL={server_url}",
        "-e", "MATTERMOST_TOKEN_FILE=/var/run/thalovant/bridge-token/token",
        "-e", f"MATTERMOST_ALLOWED_CHANNELS={scene['allowed']}",
        "-e", "MATTERMOST_REQUIRE_MENTION=true", "-e", "MATTERMOST_SESSION_SCOPE=person",
        "-e", f"BRIDGE_HEALTH_PORT={health_port}",
        image,
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)


def probe(port: int, path: str):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as answer:
            return answer.status
    except urllib.error.HTTPError as error:
        return error.code
    except OSError:
        return None


def wait(predicate, timeout: float, step: float = 0.5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


class Check:
    def __init__(self):
        self.results = []

    def __call__(self, name: str, ok: bool, detail: str = "") -> bool:
        self.results.append((name, bool(ok)))
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""), flush=True)
        return bool(ok)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--image", required=True)
    parser.add_argument("--mattermost", default="http://127.0.0.1:8065")
    parser.add_argument("--health-port", type=int, default=18000 + secrets.randbelow(1000))
    parser.add_argument("--wait", type=float, default=240, help="seconds for the server to come up")
    args = parser.parse_args()

    check = Check()
    server = Server(args.mattermost)
    server.wait_up(args.wait)
    print(f"Mattermost server up: {server.version}", flush=True)
    scene = set_scene(server)
    print(f"scene: team {scene['team']}, allowed {scene['allowed']}, other {scene['other']}, "
          f"bot @{scene['bot_username']} {scene['bot_id']}, alice @{scene['alice']}", flush=True)
    hub = Hub()
    work = tempfile.mkdtemp(prefix="mm-integration-", dir=os.environ.get("TMPDIR"))
    os.chmod(work, 0o755)
    name = f"mm-bridge-it-{secrets.token_hex(3)}"
    alice = scene["alice_token"]

    def post(channel: str, message: str, root_id: str = "") -> dict:
        body = {"channel_id": channel, "message": message}
        if root_id:
            body["root_id"] = root_id
        return server.json("POST", "/posts", token=alice, json=body)

    def bot_posts(channel: str) -> list:
        listing = server.json("GET", f"/channels/{channel}/posts?per_page=200")
        posts = [listing["posts"][i] for i in listing["order"]]
        return sorted((p for p in posts if p["user_id"] == scene["bot_id"]), key=lambda p: p["create_at"])

    def reply_to(channel: str, tag: str, timeout: float = 30):
        def found():
            return next((p for p in bot_posts(channel) if p["message"].endswith(f"answer to {tag}")), None)
        return wait(found, timeout)

    try:
        start_bridge(args.image, scene, hub, args.mattermost, args.health_port, work, name,
                     scene["bot_token"])
        inspect = json.loads(subprocess.run(engine() + ["inspect", name], capture_output=True,
                                            text=True, check=True).stdout)[0]
        check("container runs read-only as uid 10001",
              inspect["HostConfig"]["ReadonlyRootfs"] and inspect["Config"]["User"] == "10001:10001",
              f"ReadonlyRootfs={inspect['HostConfig']['ReadonlyRootfs']} User={inspect['Config']['User']}")
        arguments = " ".join(inspect.get("Args") or []) + " " + " ".join(inspect["Config"].get("Cmd") or [])
        check("no secret on the command line", scene["bot_token"] not in arguments and PASSWORD not in arguments,
              f"args={arguments.strip() or '(none)'}")
        check("/healthz answers", wait(lambda: probe(args.health_port, "/healthz") == 200, 60))
        check("/readyz is 200 (hub session live and websocket logged in)",
              wait(lambda: probe(args.health_port, "/readyz") == 200, 120))
        uid = subprocess.run(engine() + ["exec", name, "id", "-u"], capture_output=True, text=True).stdout.strip()
        check("process uid is 10001", uid == "10001", f"id -u = {uid}")

        # 1. a mention in the allowed channel
        post(scene["allowed"], f"@{scene['bot_username']} q1 what is the weather?")
        answer = reply_to(scene["allowed"], "q1")
        check("a mention in the allowed channel is answered there, top level",
              answer and answer["root_id"] == "" and answer["message"] == f"@{scene['alice']} answer to q1",
              answer and repr(answer["message"]))
        check("the hub was asked without the mention",
              any(a["utterance"] == "q1 what is the weather?" for a in hub.asked),
              repr([a["utterance"] for a in hub.asked]))
        check("one hub session per person per channel",
              # the hub prefixes the session id with the client's own id
              any(str(a["session"]).endswith(f"mattermost-{scene['allowed']}-{scene['alice_id']}")
                  for a in hub.asked),
              repr({a["session"] for a in hub.asked}))

        # 2. an unmentioned post is ignored (q2), the next mention is answered (q3)
        post(scene["allowed"], "q2 just chatting about lunch")
        post(scene["allowed"], f"@{scene['bot_username']} q3 still there?")
        reply_to(scene["allowed"], "q3")
        check("an unmentioned post in the allowed channel is ignored",
              not any(p["message"].endswith("answer to q2") for p in bot_posts(scene["allowed"])))

        # 3. a mention in a thread someone else started is answered in that thread
        root = post(scene["allowed"], "q4 planning thread, not for the bot")
        post(scene["allowed"], f"@{scene['bot_username']} q5 and in this thread?", root_id=root["id"])
        answer = reply_to(scene["allowed"], "q5")
        check("a mention in a thread is answered in that thread",
              answer and answer["root_id"] == root["id"], answer and f"root_id={answer['root_id']}")
        check("the thread's unmentioned root post was ignored",
              not any(p["message"].endswith("answer to q4") for p in bot_posts(scene["allowed"])))

        # 4. a reply under the bot's own answer needs no mention
        first = reply_to(scene["allowed"], "q1")
        post(scene["allowed"], "q6 and tomorrow?", root_id=first["id"])
        answer = reply_to(scene["allowed"], "q6")
        check("a reply in the thread the bot started is answered without a mention, in that thread",
              answer and answer["root_id"] == first["id"])

        # 5. a mention in the other channel and a DM are ignored, then a control
        post(scene["other"], f"@{scene['bot_username']} q7 hello from the other channel")
        post(scene["direct"], "q8 a direct message")
        post(scene["allowed"], f"@{scene['bot_username']} q9 control")
        check("control after the ignored posts is answered", reply_to(scene["allowed"], "q9"))
        time.sleep(2)
        check("a mention in a channel not on the list is ignored", not bot_posts(scene["other"]))
        check("a DM is ignored (dm policy auto with an allow-list)", not bot_posts(scene["direct"]))
        check("the hub never heard the ignored posts",
              not any(a["utterance"].split()[0] in ("q2", "q4", "q7", "q8") for a in hub.asked),
              repr(sorted(a["utterance"].split()[0] for a in hub.asked)))

        logs = subprocess.run(engine() + ["logs", name], capture_output=True, text=True)
        text = logs.stdout + logs.stderr
        leaked = [s for s in ("what is the weather", "just chatting", "answer to q", scene["bot_token"], PASSWORD)
                  if s in text]
        check("no message text, reply text or secret in the container log", not leaked, repr(leaked))

        # 6. the token is revoked: the bridge stops with token_rejected
        server.json("POST", "/users/tokens/revoke", json={"token_id": scene["bot_token_id"]})
        post(scene["allowed"], f"@{scene['bot_username']} q10 after the revocation")
        stopped = wait(lambda: not json.loads(subprocess.run(
            engine() + ["inspect", name], capture_output=True, text=True).stdout)[0]["State"]["Running"], 90, 1)
        state = json.loads(subprocess.run(engine() + ["inspect", name], capture_output=True,
                                          text=True).stdout)[0]["State"]
        with open(os.path.join(work, "termination-log"), encoding="utf-8") as handle:
            termination = handle.read()
        check("a revoked token stops the bridge with exit status 78",
              stopped and str(state["ExitCode"]) == TOKEN_REJECTED_EXIT_CODE, f"ExitCode={state['ExitCode']}")
        check("the termination log says token_rejected", termination == "token_rejected", repr(termination))

        # 7. a stop (SIGTERM) is clean and quick: the bridge is PID 1
        first = subprocess.run(engine() + ["logs", name], capture_output=True, text=True)
        with open(os.path.join(work, "bridge-first.log"), "w", encoding="utf-8") as handle:
            handle.write(first.stdout + first.stderr)
        last = [line for line in (first.stdout + first.stderr).splitlines()
                if "Mattermost" in line and ("refused" in line or "websocket" in line)][-3:]
        print("bridge log around the revocation:\n  " + "\n  ".join(last), flush=True)
        subprocess.run(engine() + ["rm", "-f", name], capture_output=True)
        start_bridge(args.image, scene, hub, args.mattermost, args.health_port, work, name,
                     scene["bot_token_2"])
        check("a restarted bridge with the kept Noise key is accepted by the hub (ready again)",
              wait(lambda: probe(args.health_port, "/readyz") == 200, 120))
        began = time.monotonic()
        subprocess.run(engine() + ["stop", "-t", "20", name], capture_output=True)
        took = time.monotonic() - began
        state = json.loads(subprocess.run(engine() + ["inspect", name], capture_output=True,
                                          text=True).stdout)[0]["State"]
        check("SIGTERM stops the bridge cleanly, well inside the grace period",
              str(state["ExitCode"]) == "0" and took < 15, f"ExitCode={state['ExitCode']} in {took:.1f} s")
    finally:
        logs = subprocess.run(engine() + ["logs", name], capture_output=True, text=True)
        with open(os.path.join(work, "bridge.log"), "w", encoding="utf-8") as handle:
            handle.write(logs.stdout + logs.stderr)
        print(f"bridge log kept at {work}/bridge.log")
        subprocess.run(engine() + ["rm", "-f", name], capture_output=True)
        hub.stop()

    failed = [name for name, ok in check.results if not ok]
    print(f"\n{len(check.results) - len(failed)}/{len(check.results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    threading.excepthook = lambda args: None  # hub threads at shutdown
    sys.exit(main())
