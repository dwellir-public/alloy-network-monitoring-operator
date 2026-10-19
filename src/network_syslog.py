"""Optional syslog body parsers which preserve original messages and indexed labels."""

import json

PARSERS = ("opnsense_filterlog", "cisco_ios")
COMMON = (
    r"^[0-9]+,[^,]*,[^,]*,(?P<event_rule>[0-9]+),"
    r"(?P<event_interface>[^,]+),(?P<event_reason>[^,]+),"
    r"(?P<event_action>pass|block|match),(?P<event_direction>in|out),"
)
IP_DATA = (
    r"[0-9]+,(?P<event_src_ip>[^,]+),(?P<event_dst_ip>[^,]+)"
    r"(?:,(?P<event_transport>.*))?$"
)
OPNSENSE_V4 = COMMON + r"(?P<event_ip_version>4),(?:[^,]*,){7}(?P<event_protocol>[^,]+)," + IP_DATA
OPNSENSE_V6 = (
    COMMON + r"(?P<event_ip_version>6),(?:[^,]*,){3}(?P<event_protocol>[^,]+),[0-9]+," + IP_DATA
)
CISCO = (
    r"^.*%(?P<event_facility>[A-Z][A-Z0-9_]+)-(?P<event_severity>[0-7])-"
    r"(?P<event_mnemonic>[A-Z0-9_]+): ?(?P<event_message>.+)$"
)
METADATA = (
    "event_vendor",
    "event_type",
    "event_rule",
    "event_interface",
    "event_reason",
    "event_action",
    "event_direction",
    "event_ip_version",
    "event_protocol",
    "event_src_ip",
    "event_dst_ip",
    "event_src_port",
    "event_dst_port",
    "event_facility",
    "event_severity",
    "event_mnemonic",
    "event_state",
)


class SyslogParsersError(ValueError):
    """Describe invalid parser selection without reflecting its contents."""


def parse_syslog_parsers(value: object) -> tuple[str, ...]:
    """Accept an empty selection or a small list of supported parser profiles."""
    if not isinstance(value, str) or len(value) > 256:
        raise SyslogParsersError("syslog-parsers must be a comma-separated parser list")
    if not value.strip():
        return ()
    names = tuple(item.strip() for item in value.split(","))
    if len(names) > len(PARSERS) or any(name not in PARSERS for name in names):
        raise SyslogParsersError("syslog-parsers supports opnsense_filterlog and cisco_ios")
    return tuple(dict.fromkeys(names))


def _template(source: str, template: str) -> list[str]:
    return [
        "    stage.template {",
        f"      source = {json.dumps(source)}",
        f"      template = {json.dumps(template)}",
        "    }",
    ]


def _match(expression: str, *, application: str = "") -> list[str]:
    labels = 'log_source="remote_syslog"'
    if application:
        labels += f",syslog_app_name={json.dumps(application)}"
    selector = "{" + labels + "} |~ " + json.dumps(expression)
    return [
        "  stage.match {",
        f"    selector = {json.dumps(selector)}",
        "    stage.regex {",
        f"      expression = {json.dumps(expression)}",
        "    }",
    ]


def _metadata() -> list[str]:
    return [
        "    stage.structured_metadata {",
        "      values = {",
        *(f'        {key} = "",' for key in METADATA),
        "      }",
        "    }",
        "  }",
        "",
    ]


def _opnsense(expression: str) -> list[str]:
    lines = _match(expression, application="filterlog")
    lines += _template("event_vendor", "opnsense")
    lines += _template("event_type", "firewall")
    lines += _template(
        "event_port_data",
        '{{ if or (eq .event_protocol "tcp") (eq .event_protocol "udp") }}'
        "{{ .event_transport }}{{ end }}",
    )
    lines += [
        "    stage.regex {",
        '      source = "event_port_data"',
        '      expression = "^(?P<event_src_port>[0-9]+),(?P<event_dst_port>[0-9]+),.*$"',
        "    }",
    ]
    return lines + _metadata()


def _cisco() -> list[str]:
    lines = _match(CISCO)
    lines += _template("event_vendor", "cisco")
    lines += _template(
        "event_type",
        '{{ if and (or (eq .event_facility "LINK") (eq .event_facility "LINEPROTO")) '
        '(eq .event_mnemonic "UPDOWN") }}interface_state'
        '{{ else if contains "FLAP" .event_mnemonic }}interface_flap'
        '{{ else if or (eq .event_mnemonic "LOGIN_FAILED") '
        '(eq .event_mnemonic "LOGIN_FAILURE") }}authentication_failure'
        '{{ else if and (eq .event_facility "SYS") (eq .event_mnemonic "CONFIG_I") }}'
        "configuration_change"
        '{{ else if and (or (eq .event_facility "ENVMON") (eq .event_facility "FAN") '
        '(eq .event_facility "POWER") (eq .event_facility "PLATFORM_ENV")) '
        "(le (atoi .event_severity) 3) }}hardware_alarm{{ else }}message{{ end }}",
    )
    lines += [
        "    stage.regex {",
        '      source = "event_message"',
        '      expression = "^(?:Line protocol on )?Interface (?P<event_interface>[^,]+), '
        'changed state to (?P<event_state>up|down|administratively down)$"',
        "    }",
    ]
    return lines + _metadata()


def render_syslog_parser_stages(parsers: tuple[str, ...]) -> list[str]:
    """Render parser stages whose extracted fields become structured metadata."""
    lines: list[str] = []
    if "opnsense_filterlog" in parsers:
        lines += _opnsense(OPNSENSE_V4) + _opnsense(OPNSENSE_V6)
    if "cisco_ios" in parsers:
        lines += _cisco()
    return lines
