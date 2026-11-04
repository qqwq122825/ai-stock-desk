"""自动发现 strategies/ 下所有 Strategy 子类。"""
import importlib
import inspect
import logging
import os
import pkgutil

from .base import Signal, Strategy  # noqa

log = logging.getLogger("strategies")


def discover():
    found = {}
    pkg_dir = os.path.dirname(__file__)
    for m in pkgutil.iter_modules([pkg_dir]):
        if m.name in ("base",) or m.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module("strategies." + m.name)
            mod = importlib.reload(mod)
        except Exception as e:
            log.error("加载策略模块 %s 失败: %s", m.name, e)
            continue
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if issubclass(obj, Strategy) and obj is not Strategy and obj.name != "base":
                found[obj.name] = obj
    return found
