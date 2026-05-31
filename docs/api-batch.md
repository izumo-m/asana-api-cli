# `batch-api`: parallel requests in a single HTTP call

Run up to 10 Asana API requests in parallel against `/batch`. The CLI
wraps `BatchAPIApi.create_batch_request` from `python-asana`.

See the [Asana reference](https://developers.asana.com/reference/createbatchrequest)
for the contract; this page focuses on `asana-api` invocation, body
shape, and worked examples of the response.

## Request body

`asana-api batch-api create-batch-request --body @<path>` accepts a JSON
object of the form:

```json
{"data": {"actions": [<action>, ...]}}
```

Each `<action>` describes one sub-request:

| Field           | Required | Description                                                                                                                                                              |
| --------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `relative_path` | yes      | Path of the target endpoint, e.g. `/tasks/12345`. **Query string in the path is not accepted** — put params in `data` (or pagination/fields in `options`) instead.       |
| `method`        | yes      | One of `get`, `post`, `put`, `delete`, `patch`, `head`.                                                                                                                  |
| `data`          | no       | For `GET`: query params **other than** pagination / field selection (those belong in `options`). For `POST` / `PUT` / `PATCH`: the contents of the body's `data` envelope. |
| `options`       | no       | Per-action `limit`, `offset`, `fields`, `expand` per Asana's documented schema. "Pretty" JSON output is not honored here — pass it on the parent request instead. Unknown keys are silently ignored by the server (not enforced by the CLI). |

The actions array must have **1 to 10 entries**. 0 actions or 11+
produce a parent `400 Bad Request`.

## Response shape

A successful parent call (`200 OK`) returns:

```json
{"data": [<result>, <result>, ...]}
```

Results are returned in the **same order** as the request's actions.
Each `<result>` has:

> The `body` field below carries whatever the **invoked inner endpoint
> normally returns**; `/batch` does not modify that payload. Refer to
> the per-endpoint Asana reference (e.g.
> [`/tasks/{gid}`](https://developers.asana.com/reference/gettask)) for
> the exact inner shapes.


| Field         | Description                                                                                                                                                                                 |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `status_code` | HTTP status code the invoked endpoint returned. `GET` / `PUT` / `DELETE` sub-actions return `200` on success; `POST` (resource creation) returns `201` with a `location` header attached.   |
| `headers`     | Per-result HTTP headers. The Asana reference documents this as a plain map; the `python-asana` SDK surfaces it as a `urllib3` `HTTPHeaderDict` repr — `{"headers": [...], "keys": [...], "as_tuples": [...], "empty": <bool>}`. Carries `location` on `201`; empty (`empty: true`) on plain `200`. Treat the inner shape as opaque and read `keys` / `as_tuples` rather than depending on its exact structure. |
| `body`        | The full JSON body the invoked endpoint returned. Success: `{"data": {...}}`. Failure: `{"errors": [...]}`.                                                                                 |

The batch endpoint returns `200` **even when some or all sub-actions
fail** ("parent 200 unless the request itself is malformed").

## CLI output modes

| Flag             | What stdout contains                                                                                                                       |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| *(default)*      | The outer `data` array unwrapped to a JSON list of `<result>` objects. The SDK routes `/batch` through its page iterator, but `/batch` is single-page — there is no real pagination, and pagination flags like `--page-limit` (global) / `--item-limit` (per-command) are inert here. |
| `--full-payload` | Single dict `{"data": [<result>, ...]}` from one HTTP call, matching the Asana-documented shape one-to-one.                                |

The default unwrap is convenient when piping into `jq` (`.[0].body.data.gid`),
but `--full-payload` preserves the outer `data` wrapper. Prefer
`--full-payload` for scripts that need to be readable against the Asana
reference.

## Examples

### 1. Two GETs in one call

`/tmp/who-and-workspaces.json`:

```json
{
  "data": {
    "actions": [
      {"relative_path": "/users/me", "method": "get",
       "options": {"fields": ["gid", "name", "resource_type"]}},
      {"relative_path": "/workspaces", "method": "get",
       "options": {"limit": 1, "fields": ["gid", "name"]}}
    ]
  }
}
```

```bash
asana-api batch-api create-batch-request \
  --body @/tmp/who-and-workspaces.json \
  --full-payload
```

Response (abbreviated):

```json
{
  "data": [
    {
      "status_code": 200,
      "headers": {"headers": [], "keys": [], "as_tuples": [], "empty": true},
      "body": {"data": {"gid": "1234567890", "name": "Alice", "resource_type": "user"}}
    },
    {
      "status_code": 200,
      "headers": {"headers": [], "keys": [], "as_tuples": [], "empty": true},
      "body": {"data": [{"gid": "9876543210", "name": "Acme Inc"}], "next_page": null}
    }
  ]
}
```

### 2. Bulk task creation (10 in one HTTP call)

```json
{
  "data": {
    "actions": [
      {"relative_path": "/tasks", "method": "post",
       "data": {"name": "Task 01", "projects": ["1234567890"]}},
      {"relative_path": "/tasks", "method": "post",
       "data": {"name": "Task 02", "projects": ["1234567890"]}}
    ]
  }
}
```

Successful response (abbreviated — Asana's actual response includes every
default task field; the example below shows only the keys most scripts
need):

```jsonc
{
  "data": [
    {
      "status_code": 201,
      "headers": {
        "headers": [{"key": "location", "value": "/api/1.0/tasks/1111111111"}],
        "keys": ["location"],
        "as_tuples": [["location", "/api/1.0/tasks/1111111111"]],
        "empty": false
      },
      "body": {"data": {"gid": "1111111111", "name": "Task 01", "resource_type": "task" /* …default task fields… */}}
    },
    {
      "status_code": 201,
      "headers": {
        "headers": [{"key": "location", "value": "/api/1.0/tasks/2222222222"}],
        "keys": ["location"],
        "as_tuples": [["location", "/api/1.0/tasks/2222222222"]],
        "empty": false
      },
      "body": {"data": {"gid": "2222222222", "name": "Task 02", "resource_type": "task" /* …default task fields… */}}
    }
  ]
}
```

To read the new resource's location across SDK versions, prefer
walking the `as_tuples` field (a list of `[key, value]` pairs)
rather than indexing into `keys` / `headers` directly.

> **Note**: actions run in parallel. You cannot reference the result of
> one action (e.g. the gid of a freshly-created task) in another action
> of the *same* batch. Chain dependent operations across separate batch
> calls.

### 3. Partial failure (parent 200, one sub-action 404)

```json
{
  "data": {
    "actions": [
      {"relative_path": "/users/me", "method": "get",
       "options": {"fields": ["gid", "name", "resource_type"]}},
      {"relative_path": "/tasks/9999999999999999", "method": "get"}
    ]
  }
}
```

Response — note the parent status is still `200`, exit code is `0`:

```json
{
  "data": [
    {
      "status_code": 200,
      "headers": {"headers": [], "keys": [], "as_tuples": [], "empty": true},
      "body": {"data": {"gid": "1234567890", "name": "Alice", "resource_type": "user"}}
    },
    {
      "status_code": 404,
      "headers": {"headers": [], "keys": [], "as_tuples": [], "empty": true},
      "body": {"errors": [{"message": "task: Not a recognized ID: 9999999999999999", "help": "For more information on API status codes and how to handle them, read the docs on errors: https://developers.asana.com/docs/errors"}]}
    }
  ]
}
```

Sub-action failures are visible **only** inside per-action
`status_code` and `body.errors[]`. The CLI exit code reflects only the
parent call's status, so scripts must walk the result array themselves:

```bash
asana-api batch-api create-batch-request --body @/tmp/req.json --full-payload \
  | jq -e 'all(.data[]; .status_code >= 200 and .status_code < 300)' >/dev/null \
  || echo "at least one sub-action failed" >&2
```

## Limits and restrictions

- **Max 10 actions** per request. 0 or 11+ → parent `400`.
- **No nested batch**: an action cannot target `/batch`.
- **Not batchable** (sub-action returns `400`): attachment upload,
  organization-export create / get / delete, any SCIM endpoint.
- **Rate limiting**: a batch with N actions counts as N requests
  against both the standard and concurrent rate limiters. The batch
  request itself incurs no extra cost. If any individual action would
  exceed a limit, the *entire* batch fails with `429`.

## Related

- [Asana reference: Batch API](https://developers.asana.com/reference/batch-api)
- [Asana reference: createBatchRequest](https://developers.asana.com/reference/createbatchrequest)
- [Architecture](architecture.md)
- [CLI ↔ SDK mapping](cli-sdk-mapping.md)
