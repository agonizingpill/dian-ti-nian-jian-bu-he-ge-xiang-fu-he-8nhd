"""Tiny actor helpers shared by web routes and offline workflows.

Kept free of framework/storage imports so the rule-package service can be
unit tested in restricted environments.
"""

from collections.abc import Mapping


def actor_field(user, field):
    if isinstance(user, Mapping):
        return user.get(field)
    return getattr(user, field, None)


def actor_name(user):
    return next(str(value) for key in ("username", "email", "name", "id", "sub")
                if (value := actor_field(user, key)) is not None)
