"""命令和工具共用的处理器参数绑定。"""

import inspect
from typing import Any, Callable, Mapping, get_type_hints


def resolve_arguments(
    handler: Callable[..., Any], arguments: Mapping[str, Any], dependencies: Mapping[type, Any]
) -> dict[str, Any]:
    """按类型依赖、同名参数、默认值的顺序绑定参数。

    :param handler: 待调用的处理器。
    :param arguments: 已解析的业务参数。
    :param dependencies: 当前调用可用的类型与实例，None 表示依赖不可用。
    :raises TypeError: 必需参数或声明的依赖不可用。
    """
    hints = get_type_hints(handler)
    kwargs: dict[str, Any] = {}
    for name, parameter in inspect.signature(handler).parameters.items():
        annotation = hints.get(name)
        if isinstance(annotation, type) and annotation in dependencies:
            value = dependencies[annotation]
            if value is None:
                raise TypeError(f"Dependency '{name}: {annotation.__name__}' is unavailable.")
            kwargs[name] = value
        elif name in arguments:
            kwargs[name] = arguments[name]
        elif parameter.default is not inspect.Parameter.empty:
            kwargs[name] = parameter.default
        else:
            raise TypeError(f"Cannot resolve parameter '{name}' of handler '{handler.__name__}'.")
    return kwargs
