# Version workspaces

A version workspace is a complete editable copy of yourself as you currently run —
core code, persona templates, skills, topics and configs together. Editing inside a
workspace needs no approval; approval happens once, when the finished change is
submitted.

## Flow

1. `self_workspace_open(reason)` — forks the running tree into
   `data/workspaces/<id>/tree/`. Relative paths of the ordinary file tools now
   resolve inside the workspace. Availability depends on the deployment: if this
   returns an availability error, this deployment has no kernel editing capability
   (a source checkout is required).
2. Edit freely with `read_file` / `write_file` / `edit_file` / `delete_file`.
   No review records are created while editing.
3. `self_workspace_submit(reason)` — the final gate:
   - external changes that collide with your edits are refused by name;
   - `data/`, `.git`, `.env`, `plugins/` never enter the default tree,
     dependency files are refused as unsupported;
   - the diff is reviewed; sensitive paths (migrations, `configs/models.yml`,
     control-plane code, tests) wait for the player's `.review` approval;
   - the workspace test probe must not get worse than the running baseline;
   - a database backup is taken, and the version is staged for the next restart.
   The running code stays unchanged — you choose the restart moment in your
   conversation (`<restart workspace_id="...">`).
4. After restart the version boots; if it fails to come up, the previous version
   is restored automatically and the discarded one is archived.
5. `self_workspace_discard(ws_id, reason)` removes an unwanted copy without a trace.

## Rollback

The player can roll back the last promoted version with `.patch rollback`
(`--confirm-memory-loss` when it contained database migrations — those rollbacks
drop conversations made after the update, and the database is archived first).
`.patch list` shows workspaces, the waiting pointer and the last promotion.

## Keep in mind

- One submitted workspace at a time; serial submissions are refused.
- Files changed outside your workspace after you forked it collide and are named.
- The workspace contains no `.env` and no `data/` — never try to smuggle them in;
  they are rejected at the gate.
