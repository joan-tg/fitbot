from datetime import datetime
from http import HTTPStatus

import requests
from requests import Session

from constants import (
    LOGIN_ENDPOINT,
    book_endpoint,
    classes_endpoint,
)
from exceptions import (
    MESSAGE_BOOKING_FAILED_NO_CREDIT,
    MESSAGE_BOOKING_FAILED_PENDING_PAYMENT,
    MESSAGE_BOOKING_FAILED_UNKNOWN,
    MESSAGE_TOO_SOON_TO_BOOK,
    BookingFailed,
    BookingUnconfirmed,
    IncorrectCredentials,
    TooManyWrongAttempts,
)
from logger import logger


class AimHarderClient:
    REQUEST_TIMEOUT = 10

    def __init__(
        self,
        email: str,
        password: str,
        box_id: int,
        box_name: str,
        proxy: str | None = None,
    ):
        self.session = self._login(email, password, proxy)
        self.box_id = box_id
        self.box_name = box_name

    @staticmethod
    def _login(email: str, password: str, proxy: str | None = None) -> Session:
        session = Session()
        session.proxies = {"https": proxy} if proxy else {}
        logger.info("Using proxy: %s", "yes" if proxy else "no")
        response = session.post(
            LOGIN_ENDPOINT,
            json={"username": email, "password": password, "iniframe": 0},
            timeout=AimHarderClient.REQUEST_TIMEOUT,
        )
        if response.status_code != HTTPStatus.OK:
            try:
                message = response.json().get("error", {}).get("message", "")
            except (ValueError, AttributeError):
                message = ""
            if TooManyWrongAttempts.key_phrase in message:
                raise TooManyWrongAttempts
            if IncorrectCredentials.key_phrase in message:
                raise IncorrectCredentials
            response.raise_for_status()
        logger.info("Logged successfully")
        return session

    def get_classes(self, target_day: datetime, family_id: str | None = None):
        response = self.session.get(
            classes_endpoint(self.box_name),
            params={
                "box": self.box_id,
                "day": target_day.strftime("%Y%m%d"),
                "familyId": family_id,
            },
            timeout=self.REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(
            payload.get("bookings"), list
        ):
            raise ValueError("AimHarder returned an invalid classes response")
        return payload["bookings"]

    def _is_class_booked(self, target_day, class_id, family_id=None):
        classes = self.get_classes(target_day, family_id)
        return any(
            str(item.get("id")) == str(class_id) and item.get("bookState") == 1
            for item in classes
        )

    def book_class(
        self, target_day: datetime, class_id: str, family_id: str | None = None
    ) -> bool:
        try:
            response = self.session.post(
                book_endpoint(self.box_name),
                data={
                    "id": class_id,
                    "day": target_day.strftime("%Y%m%d"),
                    "insist": 0,
                    "familyId": family_id,
                },
                timeout=self.REQUEST_TIMEOUT,
            )
        except requests.RequestException as error:
            # The server may have accepted the request before the connection failed.
            try:
                if self._is_class_booked(target_day, class_id, family_id):
                    return True
            except (requests.RequestException, ValueError):
                pass
            raise BookingUnconfirmed(
                "Booking request outcome is unknown; verification failed"
            ) from error

        if response.status_code == HTTPStatus.OK:
            try:
                result = response.json()
            except ValueError:
                result = {}
            state = result.get("bookState") if isinstance(result, dict) else None
            known_errors = {
                -2: MESSAGE_BOOKING_FAILED_NO_CREDIT,
                -5: MESSAGE_BOOKING_FAILED_PENDING_PAYMENT,
                -12: MESSAGE_TOO_SOON_TO_BOOK,
            }
            if state in known_errors:
                raise BookingFailed(known_errors[state])
        else:
            # For server errors, verify before treating the request as failed:
            # AimHarder may have processed it despite a lost/invalid response.
            pass

        try:
            if self._is_class_booked(target_day, class_id, family_id):
                return True
        except (requests.RequestException, ValueError) as error:
            raise BookingUnconfirmed(
                "Booking request was sent but its result could not be verified"
            ) from error

        if response.status_code != HTTPStatus.OK:
            raise BookingUnconfirmed(
                f"AimHarder returned HTTP {response.status_code}; booking not confirmed"
            )
        raise BookingUnconfirmed(
            "AimHarder accepted the request but the class is not marked as booked"
        )
