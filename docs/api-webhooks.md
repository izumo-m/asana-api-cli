# `webhooks` API: subscribing to changes

A walkthrough for the `asana-api webhooks` group: register a webhook so
that Asana pushes change events to an HTTP server you run, watch events
arrive, and remove the webhook again.

The CLI only manages the **subscription** (create / list / get / update /
delete). The server that receives the events is yours; a minimal one is
included below.

See Asana's [webhooks guide](https://developers.asana.com/docs/webhooks-guide)
for the resources and actions you can subscribe to and the filters allowed
on each, the [webhooks reference](https://developers.asana.com/reference/webhooks)
for the webhook object, and the [events reference](https://developers.asana.com/reference/events)
for the event payload. If you cannot expose a public URL, poll instead:
see [`api-events.md`](api-events.md).

## How it works

- A webhook connects a **resource** (the Asana object to watch, given by
  its gid) to a **target** (the URL of your receiver).
- **Handshake.** While `webhooks create-webhook` is running, Asana sends
  a `POST` with an `X-Hook-Secret` header to the target. The receiver
  must answer `200` or `204` and echo the same header back. Only then
  does `create-webhook` succeed, so the receiver has to be running and
  reachable *before* you create the webhook.
- **Delivery.** From then on Asana sends `POST` requests with a JSON body
  `{"events": [...]}`. Each one carries an `X-Hook-Signature` header: the
  hex HMAC-SHA256 of the raw request body, keyed with the secret from the
  handshake. The receiver uses it to check that the request really comes
  from Asana.

## Before you start

- An access token, set up as in [`usage.md`](usage.md#authentication).
- The gid of a workspace you can create projects in.
- Python 3 for the sample receiver.
- A way to give the receiver a public URL. The walkthrough uses a
  [Cloudflare Quick Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/)
  (`cloudflared`, no account needed); any tunnel or public host works.

The commands are written for bash and use three terminals. On PowerShell,
see [`usage.md`](usage.md#windows-and-powershell) for passing JSON bodies.

## Walkthrough

### 1. Start the receiver (terminal 1)

Save this as `receiver.py`. It completes the handshake, verifies the
signature of every delivery, and prints each event as one JSON line.

```python
import hashlib
import hmac
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

secrets = {}  # URL path -> X-Hook-Secret from that webhook's handshake


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, secret=None):
        self.send_response(status)
        if secret is not None:
            self.send_header("X-Hook-Secret", secret)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # lets you check reachability with curl
        self.reply(200)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))

        # Handshake (sent while create-webhook runs): echo the secret back
        # and remember it for this URL path.
        handshake = self.headers.get("X-Hook-Secret")
        if handshake is not None:
            secrets[self.path] = handshake
            self.reply(200, secret=handshake)
            return

        # Delivery: reject anything not signed with the remembered secret.
        secret = secrets.get(self.path)
        signature = self.headers.get("X-Hook-Signature")
        if secret is None or signature is None:
            self.reply(401)
            return
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected.encode(), signature.encode()):
            self.reply(401)
            return

        # A heartbeat delivery has no events; nothing is printed for it.
        for event in json.loads(body or b"{}").get("events", []):
            print(json.dumps(event), flush=True)
        self.reply(200)


ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
```

Run it and leave it running:

```bash
python3 receiver.py 8765     # Windows: python receiver.py 8765
```

### 2. Give it a public URL (terminal 2)

Start the tunnel and leave it running too:

```bash
cloudflared tunnel --url http://127.0.0.1:8765
# prints a line containing https://<random>.trycloudflare.com
```

### 3. Create the webhook (terminal 3)

```bash
TARGET=https://<random>.trycloudflare.com   # the URL from step 2
WORKSPACE=<workspace gid>

# Wait until this prints 200: a new tunnel hostname can take a little
# while to become reachable, and the handshake fails until it is.
curl -s -o /dev/null -w '%{http_code}\n' "$TARGET/"

WEBHOOK=$(asana-api webhooks create-webhook --body - --query '.gid' --output text <<EOF
{"data": {
  "resource": "$WORKSPACE",
  "target": "$TARGET/workspace",
  "filters": [
    {"resource_type": "project", "action": "added"},
    {"resource_type": "project", "action": "deleted"}
  ]
}}
EOF
)
echo "webhook gid: $WEBHOOK"
```

If the gid printed is empty, the create failed: read the error above it
and see [Troubleshooting](#troubleshooting). On success, terminal 1 shows
the handshake as a `"POST /workspace HTTP/1.1" 200` log line.

Two things about the body:

- `filters` says which events to deliver. It is **required** here because
  a workspace is a higher-level resource (as are teams, portfolios, and
  goals). Such webhooks also never receive task, subtask, or story
  events; watch a project or task directly for those (see
  [Variations](#variations)). For the values `resource_type` and `action`
  accept, see the guide's
  [Actions](https://developers.asana.com/docs/webhooks-guide#actions),
  [Resources and actions](https://developers.asana.com/docs/webhooks-guide#resources-and-actions),
  and — for higher-level resources —
  [Filtering: Higher-level Webhooks](https://developers.asana.com/docs/webhooks-guide#filtering-higher-level-webhooks).
- The path in `target` (`/workspace`) is yours to choose. Giving each
  webhook its own path lets one receiver keep their secrets apart.

### 4. Trigger events and watch terminal 1

```bash
PROJECT=$(asana-api projects create-project --query '.gid' --output text \
  --body "{\"data\": {\"name\": \"webhook-demo\", \"workspace\": \"$WORKSPACE\"}}")
asana-api projects delete-project --project "$PROJECT" --output none
```

Delivery is not instant; give it a little while. Terminal 1 then prints
one line per event (abbreviated here):

```jsonc
{"action": "added", "resource": {"gid": "1200000000000001", "resource_type": "project"}, "created_at": "2026-01-01T00:00:00.000Z"}
{"action": "deleted", "resource": {"gid": "1200000000000001", "resource_type": "project"}, "created_at": "2026-01-01T00:00:05.000Z"}
```

Events are compact: they identify what changed, not its contents. Fetch
the details with the matching command, e.g.
`asana-api projects get-project --project <gid>`.

Asana also sends deliveries with no events (heartbeats) to check that the
receiver is alive. The receiver answers them with `200` and prints
nothing.

### 5. Inspect the webhook

```bash
# One webhook, including its delivery health.
asana-api webhooks get-webhook --webhook "$WEBHOOK" \
  --opt-fields active,target,last_success_at,last_failure_at,last_failure_content

# The webhooks your token created in a workspace. --workspace can be
# omitted when ASANA_DEFAULT_WORKSPACE is set; --resource <gid> narrows
# the list to one watched resource.
asana-api webhooks get-webhooks --workspace "$WORKSPACE" \
  --opt-fields resource.name,target,active --output table
```

### 6. Clean up

```bash
asana-api webhooks delete-webhook --webhook "$WEBHOOK" --output none
```

Then stop the tunnel and the receiver with Ctrl-C.

## Variations

### Watch a single project or task

Resources below the higher-level ones need no `filters`; without them
every event on the resource is delivered.

```bash
PROJECT_WEBHOOK=$(asana-api webhooks create-webhook --body - --query '.gid' --output text <<EOF
{"data": {"resource": "<project gid>", "target": "$TARGET/project"}}
EOF
)
```

### Change the filters of an existing webhook

`update-webhook` **replaces** the whole filter set. This narrows the
project webhook above to tasks being completed or reopened:

```bash
asana-api webhooks update-webhook --webhook "$PROJECT_WEBHOOK" \
  --body '{"data": {"filters": [{"resource_type": "task", "action": "changed", "fields": ["completed"]}]}}'
```

Only the filters can be updated. To change the target, delete the webhook
and create a new one.

### Filter examples

Each entry below is one element of `filters`. An event is delivered when
it matches **any** of the listed filters.

On a project (or task) webhook:

```jsonc
// A comment was added / edited
{"resource_type": "story", "resource_subtype": "comment_added", "action": "added"}
{"resource_type": "story", "resource_subtype": "comment_added", "action": "changed"}

// Any story was added (comments and system-generated activity alike)
{"resource_type": "story", "action": "added"}

// A task was added to the project
{"resource_type": "task", "action": "added"}

// A task was completed or reopened
{"resource_type": "task", "action": "changed", "fields": ["completed"]}

// A task's due date or assignee changed
{"resource_type": "task", "action": "changed", "fields": ["due_on", "due_at", "assignee"]}

// A task was deleted / removed from the project
{"resource_type": "task", "action": "deleted"}
{"resource_type": "task", "action": "removed"}

// Only milestones, when they change
{"resource_type": "task", "resource_subtype": "milestone", "action": "changed"}

// A section was added / renamed
{"resource_type": "section", "action": "added"}
{"resource_type": "section", "action": "changed"}
```

On a workspace webhook:

```jsonc
// A project was created / deleted
{"resource_type": "project", "action": "added"}
{"resource_type": "project", "action": "deleted"}

// A member joined / left the workspace
{"resource_type": "workspace_membership", "action": "added"}
{"resource_type": "workspace_membership", "action": "removed"}
```

Notes:

- `fields` only applies to `"action": "changed"`, and is not supported on
  higher-level resources.
- `resource_type` and `resource_subtype` take the values the resources
  themselves report. To find them, fetch a real one, e.g.
  `asana-api stories get-stories-for-task --task <gid> --opt-fields type,resource_subtype`.
- A filter selects *which* field changed, not its new value. After a
  `completed` event, read the current state with
  `asana-api tasks get-task --task <gid> --opt-fields completed`.
- A comment event does not contain the text; fetch it with
  `asana-api stories get-story --story <gid>`.

### Keep the body in a file

`--body` also accepts `@path/to/file.json`:

```bash
asana-api webhooks create-webhook --body @webhook.json
```

## Notes for a real receiver

The sample is meant for trying things out. A receiver you rely on should
also:

- **Persist the secret per target URL.** The sample keeps secrets in
  memory, so after a restart it rejects every delivery. If the secret is
  lost, delete the webhook and create it again.
- **Accept a handshake only when you expect one.** The sample lets any
  request with an `X-Hook-Secret` header set the secret for a path. Use a
  path that cannot be guessed, or accept the handshake only while you are
  creating the webhook.
- **Keep the target URL stable.** A Quick Tunnel gets a new hostname each
  time `cloudflared` starts, which leaves existing webhooks pointing at a
  dead URL.

## Troubleshooting

- **`create-webhook` fails.** The error printed on stderr includes
  Asana's response; read it first. If it points at the target, the
  handshake did not complete. Check that the receiver and the tunnel are
  both running, that `curl -i "$TARGET/"` returns `200`, and that the
  target is a public URL (`localhost` is rejected).
- **The tunnel URL answers `404`.** An existing `~/.cloudflared/config.yml`
  can take over the Quick Tunnel. Start it with
  `cloudflared --config /dev/null tunnel --url http://127.0.0.1:8765`.
- **No events arrive.** Look at `active`, `last_failure_at`, and
  `last_failure_content` with `get-webhook`, check that terminal 1 logs
  incoming `POST` requests (`401` means the signature did not verify,
  e.g. after a receiver restart), and confirm that `filters` match what
  you are changing — remember that workspace-level webhooks never see
  task events.
- **Scripting around failures.** Errors follow the usual exception
  envelope contract; see [`usage.md`](usage.md#error-handling).

See also: [`api-events.md`](api-events.md), [`usage.md`](usage.md),
[`api-groups.md`](api-groups.md).
