"""The 1.3.0 additions: stats columns, gateway timeouts, login, port collisions."""

import socket

from ipaapi.cli import (
    build_mapping,
    parse_fc_spec,
    parse_fdr_spec,
    parse_id_spec,
    parse_pvalue_spec,
)
from ipaapi.client import looks_like_gateway_timeout, looks_like_outage
from ipaapi.errors import GatewayTimeoutError, ServiceUnavailableError
from ipaapi.models import MeasurementType

COLUMNS = ["Gene", "Common_name", "Ctrl", "Trt", "Fold_change", "P_value", "Q_value"]


def _mapping(**kw):
    return build_mapping(
        COLUMNS, [parse_id_spec("1:hugo")], parse_fc_spec("4:logratio"),
        observation_name="Sample", **kw,
    )


# -- --pvalue / --fdr ------------------------------------------------------


def test_fold_change_alone_is_unchanged():
    # The regression that matters: adding these flags must not alter what an
    # existing submission sends.
    measurements = _mapping().observations[0].measurements
    assert [m.column for m in measurements] == ["Fold_change"]
    assert measurements[0].type is MeasurementType.LOG_RATIO


def test_measurement_order_is_fixed():
    # The wire format declares slots once and fills them positionally, so the
    # order has to be deterministic rather than dependent on flag order.
    measurements = _mapping(
        pvalue_spec=parse_pvalue_spec("5"), fdr_spec=parse_fdr_spec("6")
    ).observations[0].measurements
    assert [m.column for m in measurements] == ["Fold_change", "P_value", "Q_value"]
    assert [m.type for m in measurements] == [
        MeasurementType.LOG_RATIO,
        MeasurementType.P_VALUE,
        MeasurementType.FALSE_DISCOVERY,
    ]


def test_cutoffs_are_optional_and_parsed():
    assert parse_pvalue_spec("5") == (5, None)
    assert parse_pvalue_spec("5:0.05") == (5, 0.05)
    assert parse_fdr_spec("6:10") == (6, 10.0)


def test_a_column_cannot_be_claimed_twice():
    for kw in (
        {"pvalue_spec": (4, None)},                       # collides with --FC
        {"pvalue_spec": (1, None)},                       # collides with --ID
        {"pvalue_spec": (5, None), "fdr_spec": (5, None)},  # with each other
    ):
        try:
            _mapping(**kw)
        except Exception as exc:
            assert "already" in str(exc)
        else:
            raise AssertionError(f"expected a collision error for {kw}")


def test_bad_cutoff_is_rejected():
    try:
        parse_pvalue_spec("5:notanumber")
    except Exception as exc:
        assert "not a number" in str(exc)
    else:
        raise AssertionError("expected an error")


# -- gateway timeouts ------------------------------------------------------


TIMEOUT_BODY = (
    "<html><head><title>504 Gateway Time-out</title></head><body>"
    "<h1>504 Gateway Time-out</h1>The server didn't respond in time.</body></html>"
)


def test_a_504_in_the_body_is_recognised():
    # IPA sends these with HTTP 200, so the status-code check never sees them.
    assert looks_like_gateway_timeout(200, TIMEOUT_BODY)


def test_a_504_status_is_recognised_too():
    assert looks_like_gateway_timeout(504, "")


def test_a_maintenance_page_is_not_a_timeout():
    page = "The page you are looking for is currently unavailable."
    assert looks_like_outage(200, page)
    assert not looks_like_gateway_timeout(200, page)


def test_a_timeout_is_still_a_service_error_for_existing_handlers():
    # Callers written against ServiceUnavailableError must keep working.
    assert issubclass(GatewayTimeoutError, ServiceUnavailableError)


def test_the_timeout_message_names_the_name_length_cause():
    from ipaapi.client import _raise_submission_error

    try:
        _raise_submission_error("x", 200, TIMEOUT_BODY)
    except GatewayTimeoutError as exc:
        text = str(exc)
        assert "observation name" in text
        assert "1.2.0" in text
        assert "may have reached IPA" in text
    else:
        raise AssertionError("expected GatewayTimeoutError")


# -- port collisions -------------------------------------------------------


def test_the_port_holder_is_identified():
    from ipaapi.auth import port_holder

    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        holder = port_holder(port)
        # lsof and ss are both absent in some environments; only assert the
        # shape when something was actually found.
        if holder is not None:
            assert holder["pid"].isdigit()
            assert holder["command"]
    finally:
        sock.close()


def test_an_unheld_port_reports_nothing():
    from ipaapi.auth import port_holder

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    assert port_holder(port) is None


def test_the_collision_message_explains_the_fixed_port():
    from ipaapi.auth import _port_collision_message

    text = _port_collision_message("127.0.0.1", 8000, "Address already in use")
    assert "cannot simply be changed" in text
    assert "8000" in text
