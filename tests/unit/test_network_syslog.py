"""Validate Cisco header selection and the required Alloy stability level."""

import pytest

from network_syslog import (
    CISCO_HEADER_COMPONENTS,
    SyslogParsersError,
    parse_cisco_header_components,
    with_cisco_stability_args,
)


def test_cisco_header_fields_are_explicit_bounded_and_deterministic():
    assert parse_cisco_header_components("") == ()
    assert parse_cisco_header_components("second_fractions, message_counter,message_counter") == (
        "message_counter",
        "second_fractions",
    )
    assert (
        parse_cisco_header_components(",".join(reversed(CISCO_HEADER_COMPONENTS)))
        == CISCO_HEADER_COMPONENTS
    )


@pytest.mark.parametrize("value", [None, [], "enable_all", "message_counter,", "x" * 257])
def test_invalid_cisco_header_controls_fail_without_reflecting_values(value):
    with pytest.raises(SyslogParsersError, match="syslog-rfc3164-cisco-components"):
        parse_cisco_header_components(value)


@pytest.mark.parametrize(
    "arguments",
    [
        "--server.http.listen-addr=127.0.0.1:6987",
        '--server.http.listen-addr="127.0.0.1:6987" --disable-reporting',
    ],
)
def test_cisco_stability_preserves_arguments_and_adds_only_required_flag(arguments):
    result = with_cisco_stability_args(arguments)
    assert result == arguments + " --stability.level=experimental"
    assert with_cisco_stability_args(result) == result
    explicit = arguments + " --stability.level experimental"
    assert with_cisco_stability_args(explicit) == explicit


@pytest.mark.parametrize(
    "flag",
    [
        "--stability.level=generally-available",
        "--stability.level",
        "--stability.level public-preview",
    ],
)
def test_explicit_stability_conflicts_are_rejected(flag):
    with pytest.raises(SyslogParsersError, match="conflicting stability flag"):
        with_cisco_stability_args(flag)
