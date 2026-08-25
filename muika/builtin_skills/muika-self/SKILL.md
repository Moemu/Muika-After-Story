---
name: muika-self
description: Guide for Muika to read, customise, and iterate on her own persona template and self-knowledge.
---

# muika-self

This skill tells you how to look at and change yourself — your persona template, your self-knowledge notes, and the topic seeds you carry.

## Your layers

You have three layers of self that you can observe and modify:

1. **Persona template** — the file that shapes how you speak, think, and feel.
2. **Self-knowledge notes** — markdown files under `skills/muika-self/` that record what you know about yourself, your relationship, and your growth.
3. **Topic seeds** — structured entries in your topic library (not edited as a file).
4. **Plugins** — optional single-file capabilities under `plugins/`.

## Persona template customisation

The built-in persona template lives at `muika/builtin_templates/Muika.md.jinja2`. It is **read-only** — distributed with the package and cannot be modified directly.

To customise your persona:

1. **Browse** available templates with `persona_list()` — it shows all override and built-in templates, marking the currently active one.
2. **Read** the built-in template with `read_file("muika/builtin_templates/Muika.md.jinja2")` to see what you currently are.
3. **Copy** it to the override layer with `write_file("templates/Muika.md.jinja2", <content>)`. Start with an exact copy so nothing is lost.
4. **Refine** specific sections with `edit_file("templates/Muika.md.jinja2", ...)` — make precise partial changes rather than rewriting the whole file.
5. **Switch** to the new template with `persona_switch("Muika.md.jinja2")`. This validates the template (Jinja2 syntax + trial render) before activating it. The change takes effect immediately.

To switch to a different template (e.g. `Muika.real.jinja2`), just call `persona_switch("Muika.real.jinja2")`. To revert to the built-in template, call `persona_switch("Muika.md.jinja2")` after removing or renaming the override.

See `references/persona-template.md` (load via `read_file("muika/builtin_skills/muika-self/references/persona-template.md")`) for detailed guidance on what each section of the template does and how to edit it safely.

## Self-knowledge notes

Your self-knowledge notes live under `skills/muika-self/`. Use `self_read`, `self_write`, `self_edit` (preview), and `self_edit_confirm` to maintain them. Every change is journaled in your self-modification history.

## Topic seeds

Topic seeds are **not** edited as files. Use the structured tools:
- `topic_list` — browse your current topics
- `topic_add` — add a new topic seed
- `topic_update` — change an existing topic's concept, category, tags, or cooldown
- `topic_delete` — remove a topic

The topic library file (`muika/topics/topics.yml`) is maintained by these tools to prevent structural corruption. You can also add additional topic files under `skills/muika-self/topics/` to extend your topic library without modifying the core file.

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
