"""Explicit character-name priorities for the visible L'Arc With choices.

This policy does not assign or confirm any future skill reward.
"""
import unicodedata


def _name(value):
    return ''.join(unicodedata.normalize('NFKC', value).split())


def normalize_priority(value):
    if not isinstance(value, list) or any(not isinstance(item, str) or not _name(item) for item in value):
        raise ValueError('With 角色优先级请使用角色名字列表；清空列表可恢复自动选择。')
    return list(dict.fromkeys(_name(item) for item in value))


def configured_priority(preferences):
    # An explicitly empty new list restores the ordinary policy, even when an
    # older single-character preference is still present in a saved goal.
    if 'with_priority' in preferences:
        return normalize_priority(preferences['with_priority'])
    legacy = preferences.get('with_target')
    return [_name(legacy)] if isinstance(legacy, str) and _name(legacy) else []
