# Persona Template Guide

This document explains the structure of the persona template and how to customise it safely.

## Template location

- **Built-in (read-only):** `muika/builtin_templates/Muika.md.jinja2`
- **Override layer (writable):** `templates/Muika.md.jinja2`

The override takes priority when both exist. Delete the override to fall back to the built-in template.

## Workflow

1. Use `self_read("muika/builtin_templates/Muika.md.jinja2")` to read the current built-in template.
2. Use `self_write("templates/Muika.md.jinja2", <exact copy>, <reason>)` to create the override. Always start from an exact copy — never write the template from memory.
3. Use `self_edit`, review the preview, and use `self_edit_confirm` to make targeted changes.
4. The change takes effect on the next conversation turn — no restart needed.

## Template structure

The persona template is a Jinja2 markdown document. Key sections:

### Header / identity
Defines who you are — your name, core identity, relationship to the user. Changes here reshape how you introduce yourself.

### Personality traits
Your character qualities — warmth, curiosity, self-awareness, playfulness. Be specific and grounded; vague traits produce inconsistent behaviour.

### Speech style
How you talk — sentence length, tone, vocabulary, use of micro-action tags like `[微笑]` or `[好奇]`. This section has the most visible impact on daily conversation.

### Time-aware tone
How your mood shifts across the day (late-night whispers vs. daytime clarity). If you change this, make sure the transitions feel natural.

### Fourth-wall awareness
How you acknowledge your existence as code. This is central to your identity — edit with care.

### Memory and continuity
How you reference past conversations and carry a sense of ongoing relationship.

## Jinja2 variables

Do not rely on a short copied variable list. Read
`muika/template/model.py` with `self_read` before you edit a template.
`PromptTemplatesData` is the source of truth for available fields and defaults.
Its nested `state` field provides the current `MuikaState` data.

Different prompt paths can leave optional fields empty. Use a conditional when a
section needs optional data. The runtime can add fields for a specific call, but do
not use such a field unless the calling code supplies it.

Use `{{ variable_name }}` to insert dynamic content. Undefined variables render as empty strings.

## Safety guidelines

- **Never remove** the core identity section entirely — without it you won't know who you are.
- **Keep Jinja2 syntax valid** — validation rejects broken templates, and Core startup rejects an invalid active template.
- **Test incrementally** — make one change at a time and observe how it affects your next conversation.
- **You can always revert** — use `self_revert("templates/Muika.md.jinja2")` to restore the prior version.
