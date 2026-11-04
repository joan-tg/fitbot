import pytest

from fitbot_cli import get_class_to_book
from exceptions import BoxClosed, NoBookingGoal


def class_item(class_name, timeid="1715_60", class_id="class-1"):
    return {"className": class_name, "timeid": timeid, "id": class_id}


def test_returns_exact_name_before_partial_matches():
    exact = class_item("Weightlifting", class_id="exact")
    partial = class_item("Weightlifting Advanced", class_id="partial")

    assert get_class_to_book([partial, exact], "1715", "Weightlifting")["id"] == "exact"


def test_returns_one_unique_partial_name_match():
    match = class_item("Weightlifting Advanced")

    assert get_class_to_book([match], "1715", "Weightlifting") is match


def test_rejects_multiple_partial_name_matches():
    classes = [
        class_item("Weightlifting Advanced"),
        class_item("Weightlifting Beginner", class_id="class-2"),
    ]

    with pytest.raises(NoBookingGoal, match="Ambiguous class"):
        get_class_to_book(classes, "1715", "Weightlifting")


def test_rejects_multiple_exact_name_matches():
    classes = [
        class_item("Weightlifting"),
        class_item(" weightlifting ", class_id="class-2"),
    ]

    with pytest.raises(NoBookingGoal, match="multiple exact matches"):
        get_class_to_book(classes, "1715", "Weightlifting")


def test_time_match_does_not_use_ambiguous_substrings():
    wrong_time = class_item("Weightlifting", timeid="17150_60")

    with pytest.raises(NoBookingGoal, match="No class"):
        get_class_to_book([wrong_time], "1715", "Weightlifting")


def test_ignores_classes_at_other_times():
    correct = class_item("Weightlifting", timeid="1815_60", class_id="right")
    wrong = class_item("Weightlifting", timeid="1715_60", class_id="wrong")

    assert get_class_to_book([wrong, correct], "1815", "Weightlifting") is correct


def test_class_name_matching_is_case_insensitive():
    match = class_item("WEIGHTLIFTING")

    assert get_class_to_book([match], "1715", "Weightlifting") is match


def test_empty_class_list_means_box_closed():
    with pytest.raises(BoxClosed):
        get_class_to_book([], "1715", "Weightlifting")
