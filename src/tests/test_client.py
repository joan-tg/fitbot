import datetime
from contextlib import nullcontext as does_not_raise
from http import HTTPStatus
from unittest.mock import patch

import pytest
from requests import Session
from requests.exceptions import HTTPError, Timeout

from client import AimHarderClient
from exceptions import (
    MESSAGE_BOOKING_FAILED_NO_CREDIT,
    MESSAGE_BOOKING_FAILED_PENDING_PAYMENT,
    MESSAGE_TOO_SOON_TO_BOOK,
    AimHarderUnavailable,
    BookingFailed,
    BookingUnconfirmed,
    IncorrectCredentials,
    TooManyWrongAttempts,
)

LOGIN_OK = {
    "info": {
        "version": "1.0",
        "copyright": "Copyright © 2026 Aimharder Global. All rights reserved.",
    }
}


def login_error(message):
    return {"error": {"code": 401, "message": message}, "info": LOGIN_OK["info"]}


class TestAimHarderClient:
    @pytest.mark.parametrize(
        "status_code, response, expectation",
        (
            (HTTPStatus.OK, LOGIN_OK, does_not_raise()),
            (
                HTTPStatus.UNAUTHORIZED,
                login_error(TooManyWrongAttempts.key_phrase),
                pytest.raises(TooManyWrongAttempts),
            ),
            (
                HTTPStatus.UNAUTHORIZED,
                login_error(IncorrectCredentials.key_phrase),
                pytest.raises(IncorrectCredentials),
            ),
            (
                HTTPStatus.INTERNAL_SERVER_ERROR,
                login_error("LOGIN_ERROR_SOMETHING_ELSE"),
                pytest.raises(AimHarderUnavailable),
            ),
        ),
    )
    def test_init(self, status_code, response, expectation):
        with expectation, patch("requests.Session.post") as m_post:
            m_post.return_value.status_code = status_code
            m_post.return_value.json.return_value = response
            m_post.return_value.raise_for_status.side_effect = HTTPError
            AimHarderClient(email="foo", password="bar", box_id=1, box_name="foo")

    def test__login(self):
        with patch("requests.Session.post") as m_post:
            m_post.return_value.status_code = HTTPStatus.OK
            m_post.return_value.json.return_value = LOGIN_OK
            assert isinstance(
                AimHarderClient._login(email="foo", password="bar"), Session
            )

    def test__login_request_body(self):
        with patch("requests.Session.post") as m_post:
            m_post.return_value.status_code = HTTPStatus.OK
            m_post.return_value.json.return_value = LOGIN_OK
            AimHarderClient._login(email="foo", password="bar")

        assert m_post.call_args.kwargs["json"] == {
            "username": "foo",
            "password": "bar",
            "iniframe": 0,
        }
        assert m_post.call_args.kwargs["timeout"] == AimHarderClient.REQUEST_TIMEOUT

    @pytest.mark.parametrize(
        "response, expected_classes",
        (
            (
                {"bookings": []},
                [],
            ),
            (
                {"bookings": [{"id": 123, "timeid": "1100_60", "className": "foo"}]},
                [{"id": 123, "timeid": "1100_60", "className": "foo"}],
            ),
        ),
    )
    def test_get_classes(self, response, expected_classes):
        # mock login
        with patch("requests.Session.post") as m_post:
            m_post.return_value.status_code = HTTPStatus.OK
            client = AimHarderClient(
                email="foo", password="bar", box_id=1, box_name="foo"
            )

        with patch("requests.Session.get") as m_get:
            m_get.return_value.json.return_value = response
            if expected_classes is None:
                with pytest.raises(ValueError, match="invalid classes response"):
                    client.get_classes(
                        datetime.datetime(2022, 3, 2, tzinfo=datetime.UTC)
                    )
            else:
                assert (
                    client.get_classes(
                        datetime.datetime(2022, 3, 2, tzinfo=datetime.UTC)
                    )
                    == expected_classes
                )

    @pytest.mark.parametrize(
        "response, status_code, expectation",
        (
            (
                None,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                pytest.raises(BookingUnconfirmed, match="not confirmed"),
            ),
            (
                {},
                HTTPStatus.OK,
                does_not_raise(),
            ),
            (
                {"bookState": 1},
                HTTPStatus.OK,
                does_not_raise(),
            ),
            (
                {"bookState": -2},
                HTTPStatus.OK,
                pytest.raises(BookingFailed, match=MESSAGE_BOOKING_FAILED_NO_CREDIT),
            ),
            (
                {"bookState": -5},
                HTTPStatus.OK,
                pytest.raises(
                    BookingFailed, match=MESSAGE_BOOKING_FAILED_PENDING_PAYMENT
                ),
            ),
            (
                {"bookState": -12},
                HTTPStatus.OK,
                pytest.raises(BookingFailed, match=MESSAGE_TOO_SOON_TO_BOOK),
            ),
        ),
    )
    def test_book_class(self, response, status_code, expectation):
        # mock login
        with patch("requests.Session.post") as m_post:
            m_post.return_value.status_code = HTTPStatus.OK
            client = AimHarderClient(
                email="foo", password="bar", box_id=1, box_name="foo"
            )

        with (
            patch("requests.Session.post") as m_post,
            patch("requests.Session.get") as m_get,
        ):
            m_post.return_value.json.return_value = response
            m_post.return_value.status_code = status_code
            m_get.return_value.json.return_value = {
                "bookings": [
                    {"id": "123", "bookState": 0 if status_code != HTTPStatus.OK else 1}
                ]
            }
            with expectation:
                client.book_class(
                    datetime.datetime(2022, 3, 2, tzinfo=datetime.UTC), "123"
                )

    def test_login_timeout_is_classified_as_pre_booking_outage(self):
        with patch("requests.Session.post", side_effect=Timeout):
            with pytest.raises(AimHarderUnavailable, match="login request failed"):
                AimHarderClient(email="foo", password="bar", box_id=1, box_name="foo")

    def test_login_server_error_is_classified_as_pre_booking_outage(self):
        with patch("requests.Session.post") as m_post:
            m_post.return_value.status_code = HTTPStatus.SERVICE_UNAVAILABLE
            with pytest.raises(AimHarderUnavailable, match="HTTP 503"):
                AimHarderClient(email="foo", password="bar", box_id=1, box_name="foo")

    def test_get_classes_timeout_is_classified_as_pre_booking_outage(self):
        with patch("requests.Session.post") as m_post:
            m_post.return_value.status_code = HTTPStatus.OK
            client = AimHarderClient(
                email="foo", password="bar", box_id=1, box_name="foo"
            )

        with patch("requests.Session.get", side_effect=Timeout):
            with pytest.raises(AimHarderUnavailable, match="classes request failed"):
                client.get_classes(
                    datetime.datetime(2022, 3, 2, tzinfo=datetime.UTC)
                )

    def test_get_classes_invalid_json_is_classified_as_pre_booking_outage(self):
        with patch("requests.Session.post") as m_post:
            m_post.return_value.status_code = HTTPStatus.OK
            client = AimHarderClient(
                email="foo", password="bar", box_id=1, box_name="foo"
            )

        with patch("requests.Session.get") as m_get:
            m_get.return_value.json.side_effect = ValueError("invalid JSON")
            with pytest.raises(AimHarderUnavailable, match="classes request failed"):
                client.get_classes(
                    datetime.datetime(2022, 3, 2, tzinfo=datetime.UTC)
                )

    def test_booking_post_timeout_verifies_before_reporting_uncertain(self):
        with patch("requests.Session.post") as m_post:
            m_post.return_value.status_code = HTTPStatus.OK
            client = AimHarderClient(
                email="foo", password="bar", box_id=1, box_name="foo"
            )

        with (
            patch("requests.Session.post", side_effect=[Timeout]),
            patch("requests.Session.get") as m_get,
        ):
            m_get.return_value.json.return_value = {
                "bookings": [{"id": "123", "bookState": 1}]
            }
            assert (
                client.book_class(
                    datetime.datetime(2022, 3, 2, tzinfo=datetime.UTC), "123"
                )
                is True
            )

    def test_booking_post_timeout_without_verification_stays_unconfirmed(self):
        with patch("requests.Session.post") as m_post:
            m_post.return_value.status_code = HTTPStatus.OK
            client = AimHarderClient(
                email="foo", password="bar", box_id=1, box_name="foo"
            )

        with (
            patch("requests.Session.post", side_effect=[Timeout]),
            patch("requests.Session.get", side_effect=Timeout),
        ):
            with pytest.raises(BookingUnconfirmed, match="outcome is unknown"):
                client.book_class(
                    datetime.datetime(2022, 3, 2, tzinfo=datetime.UTC), "123"
                )
