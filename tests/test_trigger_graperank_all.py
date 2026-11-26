"""Re-running a list of Observers: who is chosen."""
import pytest

from scripts import trigger_graperank_all as mod

A, B, C, D = "a" * 64, "b" * 64, "c" * 64, "d" * 64


def test_a_file_lists_pubkeys_once_in_order(tmp_path):
    path = tmp_path / "observers.txt"
    path.write_text(f"{B}\n\n# the largest\n{A}  # House\n{B}\n")
    assert mod.read_observers(str(path)) == [B, A]


def test_a_line_that_is_not_a_pubkey_stops_the_read(tmp_path):
    path = tmp_path / "observers.txt"
    path.write_text(f"{A}\n{'A' * 64}\n")
    with pytest.raises(ValueError):
        mod.read_observers(str(path))


def test_without_a_list_everyone_runs():
    assert mod.choose([A, B, C], None, [C]) == [A, B, C]


def test_a_list_runs_only_its_observers_and_those_on_a_plan():
    assert mod.choose([A, B, C, D], [B], [D, B]) == [B, D]


def test_a_listed_key_that_is_not_an_observer_is_left_out():
    assert mod.choose([A, B], [C, A], []) == [A]
