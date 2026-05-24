# Command-group reference

Every `asana-api <group>` subcommand wraps an `*Api` class from the
[`python-asana`](https://github.com/Asana/python-asana) SDK, which in turn
mirrors a resource group in the
[Asana API reference](https://developers.asana.com/reference).

The **Short description** column below is what appears in
`asana-api --help` next to each group name. Descriptions are sourced from
Asana's own developer documentation (verbatim quotes are marked with
quotation marks); a few entries paraphrase the source where the canonical
text would be unhelpfully long for a CLI help table.

This file is **authoritative**: the in-code dict `_GROUP_DESCRIPTIONS`
(in [`src/asana_api_cli/cli.py`](../src/asana_api_cli/cli.py)) must list
the same set of CLI groups. A drift test in `tests/test_cli.py`
(`test_group_descriptions_match_docs`) enforces this.

| CLI group                     | Asana reference                                                                                       | Short description (`--help`)               |
| ----------------------------- | ----------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| `access-requests`             | [Access Requests](https://developers.asana.com/reference/access-requests)                             | Manage private-object access requests      |
| `allocations`                 | [Allocations](https://developers.asana.com/reference/allocations)                                     | Manage user allocations across projects    |
| `attachments`                 | [Attachments](https://developers.asana.com/reference/attachments)                                     | Upload, list, and remove file attachments  |
| `audit-log-api`               | [Audit Log API](https://developers.asana.com/reference/audit-log-api)                                 | Read domain audit log events               |
| `batch-api`                   | [Batch API](https://developers.asana.com/reference/batch-api)                                         | Execute multiple API requests in parallel  |
| `budgets`                     | [Budgets](https://developers.asana.com/reference/budgets)                                             | Manage project and portfolio budgets       |
| `custom-field-settings`       | [Custom Field Settings](https://developers.asana.com/reference/custom-field-settings)                 | List custom fields attached to objects     |
| `custom-fields`               | [Custom Fields](https://developers.asana.com/reference/custom-fields)                                 | Manage workspace custom fields             |
| `custom-types`                | [Custom Types](https://developers.asana.com/reference/custom-types)                                   | Read workspace custom object types         |
| `events`                      | [Events](https://developers.asana.com/reference/events) ([usage](api-events.md))                      | Poll resource change events (sync token)   |
| `exports`                     | [Exports](https://developers.asana.com/reference/exports)                                             | Initiate bulk exports of project resources |
| `goal-relationships`          | [Goal Relationships](https://developers.asana.com/reference/goal-relationships)                       | Manage links between goals                 |
| `goals`                       | [Goals](https://developers.asana.com/reference/goals)                                                 | Manage organizational goals and metrics    |
| `jobs`                        | [Jobs](https://developers.asana.com/reference/jobs)                                                   | Check status of async background jobs      |
| `memberships`                 | [Memberships](https://developers.asana.com/reference/memberships)                                     | Manage memberships across object types     |
| `organization-exports`        | [Organization Exports](https://developers.asana.com/reference/organization-exports)                   | Trigger and download org-wide exports      |
| `portfolio-memberships`       | [Portfolio Memberships](https://developers.asana.com/reference/portfolio-memberships)                 | Read who has access to portfolios          |
| `portfolios`                  | [Portfolios](https://developers.asana.com/reference/portfolios)                                       | Manage portfolios (project collections)    |
| `project-briefs`              | [Project Briefs](https://developers.asana.com/reference/project-briefs)                               | Manage project brief documents             |
| `project-memberships`         | [Project Memberships](https://developers.asana.com/reference/project-memberships)                     | Read who has access to projects            |
| `project-portfolio-settings`  | [Project Portfolio Settings](https://developers.asana.com/reference/project-portfolio-settings)       | Settings for projects within portfolios    |
| `project-statuses`            | [Project Statuses](https://developers.asana.com/reference/project-statuses)                           | Per-project status updates (deprecated)    |
| `project-templates`           | [Project Templates](https://developers.asana.com/reference/project-templates)                         | Manage and instantiate project templates   |
| `projects`                    | [Projects](https://developers.asana.com/reference/projects)                                           | Manage projects (CRUD + members, etc.)     |
| `rates`                       | [Rates](https://developers.asana.com/reference/rates)                                                 | Manage per-user billing rates on projects  |
| `reactions`                   | [Reactions](https://developers.asana.com/reference/reactions)                                         | Read emoji reactions on stories            |
| `roles`                       | [Roles](https://developers.asana.com/reference/roles)                                                 | Manage RBAC roles within a workspace       |
| `rules`                       | [Rules](https://developers.asana.com/reference/rules)                                                 | Trigger Asana rule via incoming webhook    |
| `sections`                    | [Sections](https://developers.asana.com/reference/sections)                                           | Manage project sections (board/list)       |
| `status-updates`              | [Status Updates](https://developers.asana.com/reference/status-updates)                               | Manage status updates on any object        |
| `stories`                     | [Stories](https://developers.asana.com/reference/stories)                                             | Manage stories (comments + activity)       |
| `tags`                        | [Tags](https://developers.asana.com/reference/tags)                                                   | Manage tags applied to tasks               |
| `task-templates`              | [Task Templates](https://developers.asana.com/reference/task-templates)                               | Manage and instantiate task templates      |
| `tasks`                       | [Tasks](https://developers.asana.com/reference/tasks)                                                 | Manage tasks (CRUD + lifecycle ops)        |
| `team-memberships`            | [Team Memberships](https://developers.asana.com/reference/team-memberships)                           | Read who belongs to teams                  |
| `teams`                       | [Teams](https://developers.asana.com/reference/teams)                                                 | Manage teams within organizations          |
| `time-periods`                | [Time Periods](https://developers.asana.com/reference/time-periods)                                   | Read time periods (for goals, reporting)   |
| `time-tracking-categories`    | [Time Tracking Categories](https://developers.asana.com/reference/time-tracking-categories)           | Manage time-tracking categories            |
| `time-tracking-entries`       | [Time Tracking Entries](https://developers.asana.com/reference/time-tracking-entries)                 | Manage time-tracking entries on tasks      |
| `timesheet-approval-statuses` | [Timesheet Approval Statuses](https://developers.asana.com/reference/timesheet-approval-statuses)     | Manage weekly timesheet approval states    |
| `typeahead`                   | [Typeahead](https://developers.asana.com/reference/typeahead)                                         | Auto-complete search for workspace objects |
| `user-task-lists`             | [User Task Lists](https://developers.asana.com/reference/user-task-lists)                             | Read a user's My Tasks list                |
| `users`                       | [Users](https://developers.asana.com/reference/users)                                                 | Manage user records (`me` = authenticated) |
| `webhooks`                    | [Webhooks](https://developers.asana.com/reference/webhooks)                                           | Manage webhook subscriptions (real-time)   |
| `workspace-memberships`       | [Workspace Memberships](https://developers.asana.com/reference/workspace-memberships)                 | Read workspace members (admin/guest flags) |
| `workspaces`                  | [Workspaces](https://developers.asana.com/reference/workspaces)                                       | Update workspace and manage its users      |

## Length constraint

Each **Short description** must fit in **≤ 45 characters**. Above that,
click's `make_default_short_help` (used to render the Commands table
on `asana-api --help`) truncates mid-sentence and appends `…`, which
routinely drops the key noun and produces dangling fragments like
`organization-wide…` or `tasks and…`. Keeping the canonical text
within 45 chars guarantees the same wording shows in both the root
listing and the group-level `--help` header (which uses the full
description string verbatim).

When tempted to write something richer than 45 chars allows, prefer:

- a stronger, shorter verb (`Trigger and download org-wide exports`
  vs `Request and retrieve organization-wide exports`)
- the Asana resource word over an explanatory paraphrase
  (`(deprecated)` parenthetical over "legacy ... prefer X")
- omitting redundant context the section already implies
  (`Read who has access to portfolios`, not "List the user records
  who have access to a given portfolio")

## Verb must match the actual operation set

The opening verb should not under- or over-claim the CRUD coverage of
the underlying `*Api` class. The SDK methods are the ground truth.
Quick guide:

| Coverage              | Suggested opening                            |
| --------------------- | -------------------------------------------- |
| Full CRUD             | `Manage <thing>`                             |
| Read-only             | `Read <thing>` / `List <thing>` / `Look up`  |
| Create + read         | `Trigger and <verb> <thing>` (e.g. exports)  |
| Read + update         | list the verbs (`Update workspace …`)        |
| Specialized (1 verb)  | use the verb (`Trigger`, `Execute`)          |

For example, `Webhooks` supports `create / get / update / delete`, so
`Subscribe to …` would under-claim (only the `create` action); the
description uses `Manage webhook subscriptions (real-time)` instead.
Conversely `Workspaces` cannot be created or deleted via the API
(only updated, plus user-management actions), so `Manage workspaces`
would over-claim; `Update workspace and manage its users` is honest.

A handy script for re-checking the CRUD coverage when bumping the
SDK:

```python
import asana
from asana_api_cli.cli import _enumerate_api_classes

for cls in _enumerate_api_classes():
    methods = [m for m in dir(cls) if not m.startswith("_") and not m.endswith("_with_http_info")]
    has = lambda kw: any(kw in m for m in methods)  # noqa: E731
    cats = "".join(
        c for c, present in zip(
            "CRUD",
            [has("create") or has("add") or has("instantiate"),
             any(m.startswith("get_") for m in methods),
             has("update"),
             has("delete") or has("remove")],
        ) if present
    )
    print(f"{cls.__name__[:-3]:35s} {cats}")
```

## Sourcing & re-verification

The descriptions above were sourced from
[developers.asana.com/llms.txt](https://developers.asana.com/llms.txt)
(an AI-friendly Markdown index of the developer reference) and from
the individual `*.md` page bodies linked in each row. When bumping the
`python-asana` SDK, optionally re-run the index fetch and update this
file plus `_GROUP_DESCRIPTIONS` for any new groups; see
[`docs/development.md`](development.md) §"Bumping the asana SDK"
for the soft procedure.
