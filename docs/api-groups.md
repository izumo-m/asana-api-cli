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

<!--
Verb selection cheat sheet (when editing the **Short description** column).
The opening verb must not under- or over-claim the underlying `*Api` class's
CRUD coverage — SDK methods are the ground truth.

| Coverage             | Suggested opening                           |
| -------------------- | ------------------------------------------- |
| Full CRUD            | `Manage <thing>`                            |
| Read-only            | `Read <thing>` / `List <thing>` / `Look up` |
| Create + read        | `Trigger and <verb> <thing>` (e.g. exports) |
| Read + update        | list the verbs (`Update workspace …`)       |
| Specialized (1 verb) | use the verb (`Trigger`, `Execute`)         |
-->

| CLI group                     | Asana reference                                                                                       | Short description (`--help`)               |
| ----------------------------- | ----------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| `access-requests`             | [Access Requests](https://developers.asana.com/reference/access-requests)                             | Manage private-object access requests      |
| `agents`                      | [Agents](https://developers.asana.com/reference/agents)                                               | Read workspace AI agents (AI Teammates)    |
| `ai-studio-usage-api`         | [AI Studio Usage API](https://developers.asana.com/reference/ai-studio-usage-api)                     | Read AI Studio credit usage and seats      |
| `allocations`                 | [Allocations](https://developers.asana.com/reference/allocations)                                     | Manage user allocations across projects    |
| `attachments`                 | [Attachments](https://developers.asana.com/reference/attachments)                                     | Upload, list, and remove file attachments  |
| `audit-log-api`               | [Audit Log API](https://developers.asana.com/reference/audit-log-api)                                 | Read domain audit log events               |
| `batch-api`                   | [Batch API](https://developers.asana.com/reference/batch-api) ([usage](api-batch.md))                 | Execute multiple API requests in parallel  |
| `budgets`                     | [Budgets](https://developers.asana.com/reference/budgets)                                             | Manage project and portfolio budgets       |
| `custom-field-settings`       | [Custom Field Settings](https://developers.asana.com/reference/custom-field-settings)                 | List custom fields attached to objects     |
| `custom-fields`               | [Custom Fields](https://developers.asana.com/reference/custom-fields)                                 | Manage workspace custom fields             |
| `custom-types`                | [Custom Types](https://developers.asana.com/reference/custom-types)                                   | Read workspace custom object types         |
| `events`                      | [Events](https://developers.asana.com/reference/events) ([usage](api-events.md))                      | Poll resource change events (sync token)   |
| `exports`                     | [Exports](https://developers.asana.com/reference/exports)                                             | Initiate graph or resource exports         |
| `goal-relationships`          | [Goal Relationships](https://developers.asana.com/reference/goal-relationships)                       | Manage links between goals                 |
| `goals`                       | [Goals](https://developers.asana.com/reference/goals)                                                 | Manage organizational goals and metrics    |
| `jobs`                        | [Jobs](https://developers.asana.com/reference/jobs)                                                   | Check status of async background jobs      |
| `memberships`                 | [Memberships](https://developers.asana.com/reference/memberships)                                     | Manage memberships across object types     |
| `ooo-entries`                 | [Out-of-Office Entries](https://developers.asana.com/reference/ooo-entries)                           | Manage out-of-office (OOO) entries         |
| `organization-exports`        | [Organization Exports](https://developers.asana.com/reference/organization-exports)                   | Trigger and download org-wide exports      |
| `portfolio-memberships`       | [Portfolio Memberships](https://developers.asana.com/reference/portfolio-memberships)                 | Read who has access to portfolios          |
| `portfolios`                  | [Portfolios](https://developers.asana.com/reference/portfolios)                                       | Manage portfolios (project collections)    |
| `project-briefs`              | [Project Briefs](https://developers.asana.com/reference/project-briefs)                               | Manage project briefs                      |
| `project-memberships`         | [Project Memberships](https://developers.asana.com/reference/project-memberships)                     | Read who has access to projects            |
| `project-portfolio-settings`  | [Project Portfolio Settings](https://developers.asana.com/reference/project-portfolio-settings)       | Read/update project-portfolio settings     |
| `project-statuses`            | [Project Statuses](https://developers.asana.com/reference/project-statuses)                           | Post project statuses (deprecated)         |
| `project-templates`           | [Project Templates](https://developers.asana.com/reference/project-templates)                         | Instantiate or remove project templates    |
| `projects`                    | [Projects](https://developers.asana.com/reference/projects)                                           | Manage projects, members, and followers    |
| `rates`                       | [Rates](https://developers.asana.com/reference/rates)                                                 | Manage per-user billing rates on projects  |
| `reactions`                   | [Reactions](https://developers.asana.com/reference/reactions)                                         | Read emoji reactions on stories            |
| `roles`                       | [Roles](https://developers.asana.com/reference/roles)                                                 | Manage user roles within a workspace       |
| `rules`                       | [Rules](https://developers.asana.com/reference/rules)                                                 | Trigger Asana rule via incoming webhook    |
| `sections`                    | [Sections](https://developers.asana.com/reference/sections)                                           | Manage project sections (board/list)       |
| `status-updates`              | [Status Updates](https://developers.asana.com/reference/status-updates)                               | Post status updates on any object          |
| `stories`                     | [Stories](https://developers.asana.com/reference/stories)                                             | Manage stories (comments + activity)       |
| `tags`                        | [Tags](https://developers.asana.com/reference/tags)                                                   | Manage tags applied to tasks               |
| `task-templates`              | [Task Templates](https://developers.asana.com/reference/task-templates)                               | Instantiate or remove task templates       |
| `tasks`                       | [Tasks](https://developers.asana.com/reference/tasks)                                                 | Manage tasks, subtasks, and dependencies   |
| `team-memberships`            | [Team Memberships](https://developers.asana.com/reference/team-memberships)                           | Read who belongs to teams                  |
| `teams`                       | [Teams](https://developers.asana.com/reference/teams)                                                 | Manage teams within organizations          |
| `time-periods`                | [Time Periods](https://developers.asana.com/reference/time-periods)                                   | Read time periods (for goals, reporting)   |
| `time-tracking-categories`    | [Time Tracking Categories](https://developers.asana.com/reference/time-tracking-categories)           | Manage time-tracking categories            |
| `time-tracking-entries`       | [Time Tracking Entries](https://developers.asana.com/reference/time-tracking-entries)                 | Manage time-tracking entries on tasks      |
| `timesheet-approval-statuses` | [Timesheet Approval Statuses](https://developers.asana.com/reference/timesheet-approval-statuses)     | Manage weekly timesheet approval statuses  |
| `typeahead`                   | [Typeahead](https://developers.asana.com/reference/typeahead)                                         | Type-ahead lookup of workspace resources   |
| `user-task-lists`             | [User Task Lists](https://developers.asana.com/reference/user-task-lists)                             | Read a user's My Tasks list                |
| `users`                       | [Users](https://developers.asana.com/reference/users)                                                 | Read/update users (`me` = authenticated)   |
| `webhooks`                    | [Webhooks](https://developers.asana.com/reference/webhooks)                                           | Manage webhook subscriptions (real-time)   |
| `workspace-memberships`       | [Workspace Memberships](https://developers.asana.com/reference/workspace-memberships)                 | Read workspace members (admin/guest flags) |
| `workspaces`                  | [Workspaces](https://developers.asana.com/reference/workspaces)                                       | Update workspace and manage its users      |

## Sources

- [`developers.asana.com/llms.txt`](https://developers.asana.com/llms.txt) — AI-friendly Markdown index of the developer reference.
- Each row's "Asana reference" link (`/reference/<group>` page body) — paraphrased into the **Short description** column.
