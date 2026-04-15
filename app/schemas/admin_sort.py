from enum import Enum


class UsersSort(str, Enum):
    pubkey = "pubkey"
    times_calculated = "times_calculated"
    last_triggered = "last_triggered"
    last_updated = "last_updated"


class SortOrder(str, Enum):
    asc = "asc"
    desc = "desc"
