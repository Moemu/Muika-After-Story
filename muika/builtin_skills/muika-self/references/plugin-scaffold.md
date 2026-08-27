# Single-file plugin scaffold

Muika can create one Python file at `plugins/<name>.py`. The name must start with a
lowercase letter. It can contain lowercase letters, digits, and underscores.

## Minimal command plugin

Use a command plugin when a user must start an operation with a chat command. This
example adds the `.hello` command.

```python
from arclet.alconna import Alconna

from muika.plugin.command import on_alconna

hello = on_alconna(Alconna("hello"))


@hello.handle()
async def say_hello() -> str:
    return "Hello."
```

## Minimal function tool plugin

Use a function tool when Muika must call Python capability during model work. This
example gives Muika an `echo` tool. It does not add a user chat command.

```python
from pydantic import BaseModel, Field

from muika.plugin.func_call import on_function_call


class EchoParams(BaseModel):
    text: str = Field(..., description="Text to return.")


@on_function_call("Return the input text.", params=EchoParams)
async def echo(text: str) -> str:
    return text
```

## Lifecycle and state

Use lifecycle hooks to acquire and release runtime resources. Use `ctx.state` for
data that must remain available after a code reload.

```python
from muika.plugin.ctx import ctx

state = ctx.state


@ctx.load
def start() -> None:
    state["loads"] = state.get("loads", 0) + 1


@ctx.unload
def stop() -> None:
    state["last_stop"] = "complete"
```

Hooks must be synchronous functions. MAS runs load hooks in registration order. MAS
runs unload hooks in reverse registration order. Code is removed during reload. The
`ctx.state` object stays in memory.

A failed load can change live state before it stops. MAS does not copy or roll back
live objects in Phase 4.

Use `get_plugin_data_dir()` for durable files owned by one plugin. It prevents two
plugins from writing to the same data directory:

```python
from muika.plugin import get_plugin_data_dir

data_dir = get_plugin_data_dir()
```

Do not import configured blocked modules. Default blocked modules include
`subprocess`, `socket`, `ctypes`, `multiprocessing`, and `shutil`. Do not call `eval`,
`exec`, `compile`, `os.system`, `os.popen`, or process execution functions.

## Safe workflow

1. Read this scaffold.
2. Call `self_write` to validate and stage a new plugin.
3. Call `self_edit` to preview a change.
4. Check the preview before you continue.
5. Call `self_edit_confirm` to validate and stage the changed plugin.
6. Call `plugin_load(name)` to activate the staged candidate.
7. Call `self_revert` to discard staging or undo an active deployment.
8. Report the quarantine ID and failure cause after a validation failure.
9. Fix the source and stage a new candidate when possible.

The `.plugins quarantine` commands accept user chat messages only. They are not
function tools. Ask the user to send `.plugins quarantine` to list items. Ask the
user to send `.plugins quarantine restore <id>` only after the failure cause is gone.

MAS runs a candidate through load and unload in a child process. The candidate then
stays in staging. `plugin_load` replaces the formal file and reloads the plugin. If
formal reload fails, MAS restores the old file.
The child process isolates Core memory and registries. It is not an operating-system
security sandbox.
The structured L3 proposal tools are the only default tool path that writes Core
code. If the operator enables Python or shell execution, that is a separate trust
decision and can bypass structured controls.
