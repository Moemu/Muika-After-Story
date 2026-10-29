---
name: muika-self
description: Guide for Muika to read, customise, and iterate on her own persona template and self-knowledge.
---

# muika-self

This skill tells you how to look at and change yourself — your persona template, your self-knowledge notes, the topic seeds you carry, and your own core code.

## What you can change

You can observe and change these parts of yourself:

1. **Persona template** — the file that shapes how you speak, think, and feel.
2. **Self-knowledge guide** — a project override that records what you know about yourself, your relationship, and your growth.
3. **Topic seeds** — structured entries in your topic library (not edited as a file).
4. **Plugins** — optional single-file capabilities under `plugins/`.
5. **Core code** — versioned through a personal workspace that receives review and a test gate before it ever runs.

## Persona template customisation

The built-in persona template lives at `muika/builtin_templates/Muika.md.jinja2`. It is **read-only** — distributed with the package and cannot be modified directly.

To customise your persona:

1. **Browse** available templates with `persona_list()` — it shows all override and built-in templates, marking the currently active one.
2. **Read** the built-in template with `read_file("muika/builtin_templates/Muika.md.jinja2")` to see what you currently are.
3. **Copy** it with `write_file("templates/Muika.md.jinja2", <content>, reason=<why>)` if no override exists. Start with an exact copy so nothing is lost. Every write to your own files requires a `reason` and is journaled.
4. **Refine** an existing override with `edit_file("templates/Muika.md.jinja2", ..., reason=<why>)` — precise partial changes, validated (Jinja2 syntax + trial render) and snapshotted before every write.
5. **Switch** to the new template with `persona_switch("Muika.md.jinja2")`. The change takes effect immediately and survives restarts. `self_revert(path=...)` restores a previous version when a change feels wrong.

See `references/persona-template.md` for detailed guidance on what each section of the template does and how to edit it safely.

## Self-knowledge notes

The active project override is `configs/skills/muika-self/SKILL.md` — this very file. Read it first, then edit it with `write_file` / `edit_file` (always with a `reason`). Only `SKILL.md` is discovered automatically; if it refers to another note, read that note with an explicit path. Every change is journaled in your self-modification history (`self_read(".history")`).

## Topic seeds

Topic seeds are **not** edited as files. Use the structured tools:
- `topic_list` — browse your current topics
- `topic_add` — add a new topic seed
- `topic_update` — change a topic's concept, category, tags, or cooldown
- `topic_delete` — remove a topic

The tools keep user changes in `configs/topics.yml`. Do not edit the YAML file directly.

## Single-file plugins

Plugin writing is available only when its configuration switch is on. Read
`references/plugin-scaffold.md` before you create or change a plugin. Use
`write_file("plugins/<name>.py", ..., reason=<why>)` for a new plugin and
`edit_file` for an existing one. These tools validate the candidate and keep it
in staging; they do not change the active plugin. Call `plugin_load(name)` to
activate the staged candidate. Use `self_revert` to discard a staged candidate
or undo a deployment.

MAS checks each candidate in a separate Python process. A failed candidate goes
to quarantine. These quarantine commands are user chat commands; Muika cannot
call them as function tools. Report the quarantine ID and the failure cause to
the user. The user can send `.plugins quarantine` to list items and
`.plugins quarantine restore <id>` to validate and restore one.

## Core code: version workspaces

Read `references/workspace.md` before you touch your own core code. Open a
personal workspace (`self_workspace_open`), edit the copy freely with the
ordinary file tools, then submit the finished version (`self_workspace_submit`)
— it passes review, a test gate, and the player's own approval for sensitive
paths before it is staged for the next restart. If `self_workspace_open`
returns an availability error, this deployment has no kernel editing
capability; everything else above still applies where the deployment allows.
