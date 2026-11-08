import argparse
import getpass
import json
import os
from datetime import UTC, datetime, timedelta

from client import AimHarderClient
from exceptions import (
    MESSAGE_BOX_IS_CLOSED,
    BookingFailed,
    BoxClosed,
    NoBookingGoal,
)
from logger import logger


def get_booking_goal_time(day: datetime, booking_goals):
    """Get the booking goal that satisfies the given day of the week"""
    try:
        return (
            booking_goals[str(day.weekday())]["time"],
            booking_goals[str(day.weekday())]["name"],
        )
    except KeyError:  # did not find a matching booking goal
        raise NoBookingGoal(
            f"There is no booking-goal for {day.strftime('%A, %Y-%m-%d')}."
        )


def get_class_to_book(classes: list[dict], target_time: str, class_name: str):
    """Return one unambiguous class matching the requested time and name."""
    if not classes:
        raise BoxClosed(MESSAGE_BOX_IS_CLOSED)

    # AimHarder time IDs commonly include a duration suffix, e.g. "1715_60".
    # Compare the time component, not a substring that could match another time.
    time_classes = [
        item
        for item in classes
        if isinstance(item, dict)
        and str(item.get("timeid", "")).split("_", 1)[0] == str(target_time)
    ]

    requested_name = str(class_name).strip().casefold()
    exact_matches = [
        item
        for item in time_classes
        if isinstance(item.get("className"), str)
        and item["className"].strip().casefold() == requested_name
    ]

    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        raise NoBookingGoal(
            f"Ambiguous class: multiple exact matches for "
            f"`{class_name}` at `{target_time}`."
        )

    partial_matches = [
        item
        for item in time_classes
        if isinstance(item.get("className"), str)
        and requested_name in item["className"].casefold()
    ]
    if len(partial_matches) == 1:
        return partial_matches[0]
    if len(partial_matches) > 1:
        raise NoBookingGoal(
            f"Ambiguous class: multiple partial matches for "
            f"`{class_name}` at `{target_time}`."
        )
    raise NoBookingGoal(
        f"No class with the text `{class_name}` in its name at time `{target_time}`"
    )


def main(
    email,
    password,
    booking_goals,
    box_name,
    box_id,
    days_in_advance,
    family_id=None,
    proxy=None,
):
    target_day = datetime.now(tz=UTC) + timedelta(days=days_in_advance)
    try:
        target_time, target_name = get_booking_goal_time(target_day, booking_goals)
    except NoBookingGoal as e:
        logger.info(str(e))
        return
    client = AimHarderClient(
        email=email, password=password, box_id=box_id, box_name=box_name, proxy=proxy
    )
    classes = client.get_classes(target_day, family_id)
    _class = get_class_to_book(classes, target_time, target_name)
    if _class["bookState"] == 1:
        logger.info("Class already booked. Nothing to do")
        return
    try:
        client.book_class(target_day, _class["id"], family_id)
    except BookingFailed as e:
        logger.error(str(e))
        return
    logger.info("Class booked successfully")


if __name__ == "__main__":
    """
    PASSWORD may be supplied through the environment; otherwise the CLI prompts
    securely for it.

    python src/fitbot_cli.py
     --email your.email@mail.com
     --box-name lahuellacrossfit
     --box-id 3984
     --booking-goal '{"0":{"time": "1815", "name": "Provenza"}}'
     --family-id 123456
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True, type=str)
    parser.add_argument("--booking-goals", required=True, type=json.loads)
    parser.add_argument("--box-name", required=True, type=str)
    parser.add_argument("--box-id", required=True, type=int)
    parser.add_argument("--days-in-advance", required=True, type=int, default=3)
    parser.add_argument(
        "--family-id",
        required=False,
        type=int,
        default=None,
        help="ID of the family member (optional)",
    )
    args = parser.parse_args()
    arguments = {key: value for key, value in args.__dict__.items() if value != ""}
    password = (
        os.environ.get("PASSWORD")
        or os.environ.get("password")
        or getpass.getpass("AimHarder password: ")
    )
    arguments["proxy"] = os.environ.get("PROXY") or os.environ.get("proxy") or None
    main(password=password, **arguments)
