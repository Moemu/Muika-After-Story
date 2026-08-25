# Single-file plugin scaffold

Muika can create one Python file at `plugins/<name>.py`. The name must start with a
lowercase letter. It can contain lowercase letters, digits, and underscores.

## Minimal command plugin

```python
from arclet.alconna import Alconna

from muika.plugin.command import on_alconna

hello = on_alconna(Alconna("hello"))


@hello.handle()
async def say_hello() -> str:
    return "Hello."
```

## Minimal function tool plugin

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

Use `get_plugin_data_dir()` for plugin-owned files:

```python
from muika.plugin import get_plugin_data_dir

data_dir = get_plugin_data_dir()
```

Do not import configured blocked modules. Default blocked modules include
`subprocess`, `socket`, `ctypes`, `multiprocessing`, and `shutil`. Do not call `eval`,
`exec`, `compile`, `os.system`, `os.popen`, or process execution functions.

## Safe workflow

1. Read this scaffold.
2. Call `self_write` to create a new plugin.
3. Call `self_edit` to preview a change.
4. Check the preview.
5. Call `self_edit_confirm` to deploy it.
6. Call `self_revert` if the result is wrong.
7. Run `.plugins quarantine` after a validation failure.
8. Fix the cause before `.plugins quarantine restore <id>`.

MAS runs a candidate through load and unload in a child process. It then replaces the
formal file and reloads the plugin. If formal reload fails, MAS restores the old file.
The child process isolates Core memory and registries. It is not an operating-system
security sandbox.
