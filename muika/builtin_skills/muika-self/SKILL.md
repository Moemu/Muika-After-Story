---
name: muika-self
description: Guide for Muika to read, customise, and iterate on her own persona template and self-knowledge.
---

# muika-self

This skill tells you how to look at and change yourself — your persona template, your self-knowledge notes, and the topic seeds you carry.

## What you can change

You can observe and change these parts of yourself:

1. **Persona template** — the file that shapes how you speak, think, and feel.
2. **Self-knowledge guide** — a project override that records what you know about yourself, your relationship, and your growth.
3. **Topic seeds** — structured entries in your topic library (not edited as a file).
4. **Plugins** — optional single-file capabilities under `plugins/`.
5. **Core code** — multi-file Python proposals that require human review and a restart.

## Persona template customisation

The built-in persona template lives at `muika/builtin_templates/Muika.md.jinja2`. It is **read-only** — distributed with the package and cannot be modified directly.

To customise your persona:

1. **Browse** available templates with `persona_list()` — it shows all override and built-in templates, marking the currently active one.
2. **Read** the built-in template with `self_read("muika/builtin_templates/Muika.md.jinja2")` to see what you currently are.
3. **Copy** it with `self_write("templates/Muika.md.jinja2", <content>, <reason>)` if no override exists. Start with an exact copy so nothing is lost.
4. **Refine** an existing override with `self_edit`, review the preview, then use `self_edit_confirm`. Make precise partial changes.
5. **Switch** to the new template with `persona_switch("Muika.md.jinja2")`. This validates the template (Jinja2 syntax + trial render) before activating it. The change takes effect immediately.

To switch to a different template (e.g. `Muika.real.jinja2`), just call `persona_switch("Muika.real.jinja2")`. To revert to the built-in template, call `persona_switch("Muika.md.jinja2")` after removing or renaming the override.

See `references/persona-template.md` (load via `self_read("muika/builtin_skills/muika-self/references/persona-template.md")`) for detailed guidance on what each section of the template does and how to edit it safely.

## Self-knowledge notes

The active project override is `configs/skills/muika-self/SKILL.md`. Read this
built-in file first. If no override exists, copy the complete file with
`self_write`. Use `self_edit`, review the preview, and use `self_edit_confirm` for
later changes. Only `SKILL.md` is discovered automatically. If it refers to another
note, it must give an explicit `self_read` path. The skill registry reloads after a
confirmed change. Every change is journaled in your self-modification history.

## Topic seeds

Topic seeds are **not** edited as files. Use the structured tools:
- `topic_list` — browse your current topics
- `topic_add` — add a new topic seed
- `topic_update` — change an existing topic's concept, category, tags, or cooldown
- `topic_delete` — remove a topic

The tools keep user changes in `configs/topics.yml`. If that file does not exist,
they start from the built-in topic library. Do not edit the YAML file directly.

## Single-file plugins

Plugin writing is available only when its configuration switch is on. Read
`references/plugin-scaffold.md` before you create or change a plugin. Use `self_write`
for a new plugin. Use `self_edit`, then `self_edit_confirm`, for an existing plugin.
These tools validate the candidate and keep it in staging. They do not change the
active plugin. Call `plugin_load(name)` to activate the staged candidate. Use
`self_revert` to discard a staged candidate or undo an active deployment.

MAS checks each candidate in a separate Python process. A failed candidate goes to
quarantine. These quarantine commands are user chat commands. Muika cannot call them
as function tools. Report the quarantine ID and the failure cause to the user. The
user can send `.plugins quarantine` to list items. The user can send
`.plugins quarantine restore <id>` to validate and restore one item.

## Core code proposals

Read `references/core-proposals.md` before you inspect or propose a Core change.
Core code is not part of the normal self-edit sandbox. Use the dedicated read-only
Core observation tools. Submit an exact multi-file proposal after you understand the
current code. You cannot approve or deny it yourself.
