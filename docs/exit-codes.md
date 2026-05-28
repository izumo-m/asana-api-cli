# Exit codes

| Exit | Meaning |
|---|---|
| `0` | success |
| `1` | SDK call exception, no envelope (default `--output-errors=none`: the exception is echoed to stderr without traceback frames) |
| `2` | user input invalid (e.g. Click usage error, invalid jq) |
| `3` | SDK call exception rendered as an envelope on stdout (requires `--output-errors {json\|text\|csv\|table}`) |
| anything else | unclassified (no contract) |

## Shell pattern

The envelope path puts machine-readable output on **stdout** (not stderr),
so scripts can branch on `$?` and consume stdout uniformly. On the
exception paths (exit `1` and exit `3`), stderr additionally carries a
human-readable echo of the exception (Python's top-level format
without traceback frames — for `ApiException` this is multi-line and
includes status / reason / headers / body), so unexpected error
shapes stay diagnosable even when the script does not parse stderr;
silence it with `2>/dev/null` if a pure stdout contract is preferred.

```bash
out=$(asana-api events get-events --resource $rid --sync $sync \
  --full-payload --output-errors json)
case $? in
  0) # success: $out is the payload
     echo "$out" | jq -c '.data[]'
     ;;
  3) # API / connection error: $out is the envelope; body is a string,
     # so `fromjson` parses it
     new_sync=$(echo "$out" | jq -r '.body | fromjson | .sync // empty')
     ;;
  *) echo "fatal" >&2; exit 1 ;;
esac
```

Without `--output-errors`, the default `none` mode catches the exception
and writes the formatted exception (no traceback frames) to stderr
before exiting 1. For `ApiException`, that output is multi-line and
already includes status, reason, response headers, and response body
— so the 412 sync-token body in events polling is readable from
stderr without opting into an envelope format. Scripts that need to
parse the body into structured fields should still use
`--output-errors {json|text|csv|table}` for the stdout envelope.
