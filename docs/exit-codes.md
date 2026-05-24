# Exit codes

| Exit | Meaning |
|---|---|
| `0` | success |
| `1` | uncaught exception from the SDK call path (default `--output-errors=raw`: Python prints the traceback on stderr and exits 1, the SDK-parity baseline) |
| `2` | user input invalid (e.g. Click usage error, invalid jq) |
| `3` | SDK call exception rendered as an envelope on stdout (requires `--output-errors {json\|text\|csv\|table}`) |
| anything else | unclassified (no contract) |

## Shell pattern

The envelope path puts machine-readable output on **stdout** (not stderr),
so scripts can branch on `$?` and consume stdout uniformly — stderr is
free to carry library noise (urllib3 retries, Python warnings) without
contaminating the parse target.

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

Without `--output-errors`, the default `raw` mode lets the exception
propagate — Python prints the traceback on stderr and exits 1. Use raw
mode for one-off interactive use; opt into an envelope format when
scripting.
