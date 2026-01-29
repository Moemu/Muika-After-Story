"""
模型所需第三方库依赖检查
"""

import importlib

MODEL_DEPENDENCY_MAP = {
    "Azure": ["azure-ai-inference>=1.0.0b7"],
    "Dashscope": ["dashscope>=1.22.1"],
    "Gemini": ["google-genai>=1.8.0"],
    "Ollama": ["ollama>=0.4.7"],
    "Openai": ["openai>=1.64.0"],
}


def get_missing_dependencies(dependencies: list[str]) -> list[str]:
    """
    获取遗失的依赖
    """
    missing = []
    for dep in dependencies:
        try:
            importlib.import_module(dep)
        except ImportError:
            missing.append(dep)
    return missing
