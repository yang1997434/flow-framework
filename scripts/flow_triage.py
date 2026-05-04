#!/usr/bin/env python3
"""flow triage — heuristic complexity classifier.

Usage:
  flow_triage.py "task description"

Note: this is a heuristic-only fallback. The real Triage decision should be made
by Claude (Haiku model) via the /flow:start slash command. This script gives a
default answer when the LLM is unavailable.
"""
from __future__ import annotations

import argparse
import re
import sys


# Trivial signals
TRIVIAL_PATTERNS = [
    r"\btypo\b", r"\bfix typo\b", r"\b错别字\b",
    r"\bone[- ]?line\b", r"\bsingle line\b", r"\b单行\b",
    r"\brename\b.*\bvariable\b",
    r"\bformat\b", r"\bgofmt\b", r"\bprettier\b",
]

# Simple signals
SIMPLE_PATTERNS = [
    r"\bfix\b.*\bbug\b",
    r"\badd\b.*\b(field|param|flag|option)\b",
    r"\b加个\b", r"\b改一下\b",
]

# Complex signals
COMPLEX_PATTERNS = [
    r"\brefactor\b", r"\b重构\b",
    r"\barchitecture\b", r"\b架构\b",
    r"\bmigrate\b", r"\b迁移\b",
    r"\brewrite\b", r"\b重写\b",
    r"\bredesign\b", r"\b重新设计\b",
    r"\bcross[- ]layer\b", r"\b跨层\b",
    r"\b多个模块\b", r"\bmultiple modules\b",
]

# UI signals (affects task type)
UI_PATTERNS = [
    r"\b(component|page|view|UI|UX)\b",
    r"\b组件\b", r"\b页面\b", r"\b前端\b",
    r"\b(react|vue|svelte|next\.js)\b",
]

# Backend signals
BACKEND_PATTERNS = [
    r"\b(API|endpoint|route|handler)\b",
    r"\b(server|backend|database|DB)\b",
    r"\b接口\b", r"\b后端\b", r"\b服务\b",
]

# Research signals
RESEARCH_PATTERNS = [
    r"\bresearch\b", r"\b调研\b",
    r"\bcompare\b.*\b(library|framework|tool)s?\b",
    r"\b对比\b",
]


def classify(description: str) -> tuple[str, str]:
    """Return (complexity, type)."""
    text = description.lower()

    # Complexity
    if any(re.search(p, text, re.IGNORECASE) for p in COMPLEX_PATTERNS):
        complexity = "complex"
    elif any(re.search(p, text, re.IGNORECASE) for p in TRIVIAL_PATTERNS):
        complexity = "trivial"
    elif any(re.search(p, text, re.IGNORECASE) for p in SIMPLE_PATTERNS):
        complexity = "simple"
    else:
        complexity = "moderate"  # default

    # Type
    if any(re.search(p, text, re.IGNORECASE) for p in RESEARCH_PATTERNS):
        task_type = "research"
    elif any(re.search(p, text, re.IGNORECASE) for p in UI_PATTERNS):
        task_type = "frontend"
    elif any(re.search(p, text, re.IGNORECASE) for p in BACKEND_PATTERNS):
        task_type = "backend"
    else:
        task_type = "backend"  # default

    return complexity, task_type


def main():
    parser = argparse.ArgumentParser(description="Heuristic Triage classifier")
    parser.add_argument("description", help="Task description")
    args = parser.parse_args()

    complexity, task_type = classify(args.description)
    print(f"complexity: {complexity}")
    print(f"type: {task_type}")
    print(f"(heuristic — Claude should override via /flow:start with Haiku judgment)")


if __name__ == "__main__":
    main()
