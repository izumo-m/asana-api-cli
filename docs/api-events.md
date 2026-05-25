# `events` API: polling for changes

A working shell pattern for using `asana-api events get-events` to keep
up with changes to a resource (task, project, or goal).

See the [Asana reference](https://developers.asana.com/reference/events)
for the resource-coverage details and the event payload shape.

## Example

```bash
RES=<task or project gid>

# Bootstrap: the first call always returns 412 with a fresh sync token
# in the response body. ``--output-errors text`` opts into an envelope
# (default is ``none`` which would let the 412 ApiException propagate
# uncaught). The envelope's ``body`` is the raw response string, so
# ``fromjson`` parses it; ``text`` format makes the scalar print
# without JSON quotes.
SYNC=$(asana-api events get-events --resource "$RES" \
  --full-payload \
  --output-errors text \
  --query-errors '.body | fromjson | .sync')
[ -z "$SYNC" ] && exit 1

# Poll: send the token, print events, rotate, sleep, repeat.
# Exit 0 = normal response; exit 3 = 412 (token expired, new token in envelope).
while true; do
  RESP=$(asana-api events get-events --resource "$RES" --sync "$SYNC" \
    --full-payload \
    --output-errors text \
    --query-errors '.body | fromjson | .sync')
  case $? in
    0) echo "$RESP"
       SYNC=$(echo "$RESP" | jq -r '.sync')
       [ -z "$SYNC" ] && exit 1
       ;;
    3) # 412: sync token expired — envelope yields a fresh token
       SYNC=$RESP
       [ -z "$SYNC" ] && exit 1
       ;;
    *) exit 1 ;;
  esac
  sleep 5
done
```

`--output-errors` / `--query-errors` write to **stdout** (not stderr) so
the variable assignment captures them cleanly. The exception is *also*
echoed to **stderr** (Python's top-level format without the traceback)
so that an unexpected error — e.g. a 500 instead of the expected 412 —
stays visible to the user even though `--query-errors` would have
stripped it from stdout. Add `2>/dev/null` to suppress the echo if the
loop is noisy in your environment. See
[`sdk-deviations.md`](sdk-deviations.md) for the envelope schema and
the reason `--output-errors` mirrors `--output`.

## Why `--full-payload` is required

Without `--full-payload`, the `python-asana` SDK's `EventIterator`
absorbs the bootstrap 412 silently, fetches the new sync token
internally, and starts iterating — the token is never exposed to the
caller, so a shell script can't capture and persist it. The same
absorption happens on every subsequent rotation.

`--full-payload` switches the SDK to non-iterator mode (returns the
raw `{data, sync, has_more}` payload), which lets the 412 bubble up
to the CLI's exception handler, where `--output-errors` /
`--query-errors` then expose the envelope — including the rotated sync
— on stdout.

So `--full-payload` belongs on **every** events call, not just the
bootstrap.

## Sync tokens expire after ~24 h

When a token expires, the next poll returns 412 with a fresh token in
the body — same shape as bootstrap. A production loop should treat
exit `3` from a steady-state poll as "re-run the bootstrap step"; the
example above does not include that recovery path. Note that catching
exit `3` requires `--output-errors` on the polling call too (default
`none` would surface a Python traceback and exit 1 instead).

See also: [`exit-codes.md`](exit-codes.md),
[`sdk-deviations.md`](sdk-deviations.md).
