"""Pinned embedded SNMP module catalog for Alloy 1.20.1.

Source: grafana/alloy v1.20.1, internal/static/integrations/snmp_exporter/common/
snmp.yml.gz, SHA256 18329efe2a859659a3236015b1db053aa81aa499cd4f15a1b86f8fd91598b9a5.
This catalog contains names only; Alloy supplies the actual metric definitions.
"""

import re

DEFAULT_MODULES = ("system", "if_mib")
MAX_MODULES = 8
INVALID_MODULE = "__invalid_netbox_module_selection"
MODULES = (
    "apcups",
    "arista_sw",
    "cisco_device",
    "cisco_fc_fe",
    "cisco_imc",
    "cisco_wlc",
    "cyberpower",
    "dell",
    "dell_network",
    "dlink",
    "eap",
    "eltex_mes",
    "fieldserver",
    "hpe",
    "hrDevice",
    "hrSWInstalled",
    "hrSWRun",
    "hrSWRunPerf",
    "hrStorage",
    "hrSystem",
    "if_mib",
    "infrapower_pdu",
    "ip_mib",
    "juniper",
    "juniper_optics",
    "keepalived",
    "kemp_loadmaster",
    "liebert_pdu",
    "mikrotik",
    "nec_ix",
    "paloalto_fw",
    "powercom",
    "printer_mib",
    "raritan",
    "readynas",
    "servertech_sentry3",
    "servertech_sentry4",
    "sophos_xg",
    "synology",
    "system",
    "tplink-ddm",
    "ubiquiti_airfiber",
    "ubiquiti_airmax",
    "ubiquiti_airos",
    "ubiquiti_unifi",
    "ucd_la_table",
    "ucd_memory",
    "ucd_system_stats",
    "yamaha_rt",
)


class SnmpModulesError(ValueError):
    """Report invalid public module settings without reflecting the input."""


def parse_module_list(value: str) -> tuple[str, ...]:
    """Normalize a bounded list of modules loaded by the pinned Alloy binary."""
    if not isinstance(value, str) or len(value) > 1024:
        raise SnmpModulesError("snmp-modules must be a comma-separated module list")
    names = tuple(item.strip() for item in value.split(","))
    if not names or len(names) > MAX_MODULES or any(name not in MODULES for name in names):
        raise SnmpModulesError("snmp-modules requires 1 to 8 known embedded Alloy module names")
    return tuple(dict.fromkeys(names))


def module_list_regex() -> str:
    """Match a canonical discovery list without accepting arbitrary module names."""
    name = "(?:" + "|".join(re.escape(item) for item in MODULES) + ")"
    return "(" + name + "(?:," + name + "){0," + str(MAX_MODULES - 1) + "})"
