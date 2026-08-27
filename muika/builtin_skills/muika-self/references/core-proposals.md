# Core Proposal Guide

Core code is your deepest runtime layer. A Core change can affect memory, replies,
tools, startup, and recovery. Study the current code before you prepare a proposal.

## Observation

Use `core_list` to find Python files. Use `core_search` to locate an exact concept.
Use `core_read` to read a bounded line range. These tools only observe approved Core
paths. They do not change code.

## Proposal format

Use `propose_core_change` once you have a small and complete change set. A proposal
can contain these actions:

- `modify`: Give ordered `old_text` and `new_text` replacements. Each `old_text`
  must match exactly once.
- `create`: Give the complete Python file content.
- `delete`: Give only the project-relative path.

Each path can appear once. Do not replace a complete existing file. Keep the reason
clear and specific. Structured errors identify the file and replacement that failed.

## Human review

A proposal does not change active code. Tell the user what you want to change and
why. Use your own words. The recorded reason should match what you tell the user.
The user reviews the diff, runs validation, and makes the decision.

If the user approves the change, it still needs one restart. You can say that the
change must wait until you sleep and wake again before it becomes part of you.
The deny-list contains doors that you cannot open through a proposal. State this
boundary plainly when it matters.

## Trust boundary

Changing test files is allowed. A weak assertion can make validation misleading.
Human review is the final safety check for test changes.

The structured proposal path is the only default tool path that writes Core code.
Python and shell execution are separate operator trust decisions. When enabled, they
can bypass these structured controls. MAS does not provide an operating-system
security sandbox.
