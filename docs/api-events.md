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
# on **stdout** (default ``none`` puts the formatted exception on
# stderr, which is fine for humans but not capturable by ``$(...)``).
# The envelope's ``body`` is the raw response string, so ``fromjson``
# parses it; ``text`` format makes the scalar print without JSON quotes.
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

The script uses `--output-errors text` + `--query-errors` because
every events call may return a 412 whose body carries the next sync
token — extracting that body programmatically requires the envelope
path on stdout. See [`exit-codes.md`](exit-codes.md) and
[`sdk-deviations.md`](sdk-deviations.md) for the generic envelope
contract.

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
example above does not include that recovery path. (`--output-errors`
is therefore needed on the polling call too, not just on bootstrap.)

See also: [`exit-codes.md`](exit-codes.md),
[`sdk-deviations.md`](sdk-deviations.md).
