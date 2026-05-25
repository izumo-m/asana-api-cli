# Exit codes

| Exit | Meaning |
|---|---|
| `0` | success |
| `1` | uncaught exception from the SDK call path (default `--output-errors=none`: Python prints the traceback on stderr and exits 1, the SDK-parity baseline) |
| `2` | user input invalid (e.g. Click usage error, invalid jq) |
| `3` | SDK call exception rendered as an envelope on stdout (requires `--output-errors {json\|text\|csv\|table}`) |
| anything else | unclassified (no contract) |

## Shell pattern

The envelope path puts machine-readable output on **stdout** (not stderr),
so scripts can branch on `$?` and consume stdout uniformly. Stderr
carries a human-readable echo of the exception (Python's top-level
format without the traceback) so unexpected error shapes stay
diagnosable even when the script does not parse stderr; silence it
with `2>/dev/null` if a pure stdout contract is preferred.

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

Without `--output-errors`, the default `none` mode lets the exception
propagate — Python prints the traceback on stderr and exits 1. Use
`none` for one-off interactive use; opt into an envelope format when
scripting.
