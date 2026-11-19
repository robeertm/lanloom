"""Replies of a sample router as the REST API gives them: every value is a string, ids are ``.id``, lists are plain
JSON arrays. One router with a bridge that filters by VLAN, a management network, a camera VLAN, an internet line over
DHCP on ether1 and a backup line over PPPoE, a switch neighbour, two wireless clients and a few wired ones.
Addresses are documentation or private test ranges; hardware addresses are locally administered or made up."""
import copy

USER = "lanloom"
PASSWORD = "Sup3r-secret-pw-for-tests"
VERSION = "7.16.1"

ROUTER_MAC = "02:00:5E:10:00:02"
SWITCH_MAC = "CC:2D:E0:00:00:01"
NAS_MAC = "02:00:5E:30:00:01"
PHONE_MAC = "AA:BB:5E:30:00:02"
CAM_MAC = "02:00:5E:30:00:03"
LAPTOP_MAC = "02:00:5E:30:00:04"
TABLET_MAC = "AA:BB:5E:30:00:05"
DESK_MAC = "02:00:5E:30:00:06"
OFFLINE_MAC = "02:00:5E:30:00:07"
MULTICAST_MAC = "01:00:5E:00:00:01"


def raw_data() -> dict:
    """The collected replies (keys of ``read.LIGHT`` and ``read.FULL``) of the sample router."""
    return {
        "resource": {"board-name": "hEX S", "version": "%s (stable)" % VERSION, "uptime": "1w2d3h4m5s", "cpu-load": "3",
                     "total-memory": "536870912", "free-memory": "402653184", "architecture-name": "mmips"},
        "identity": {"name": "edge-router"},
        "routerboard": {"model": "hEX S", "current-firmware": "7.16.1", "serial-number": "ABC123456"},
        "interfaces": [
            {".id": "*1", "name": "ether1", "type": "ether", "mac-address": "02:00:5E:10:00:01", "running": "true",
             "disabled": "false", "rx-byte": "123456789", "tx-byte": "987654321"},
            {".id": "*2", "name": "ether2", "type": "ether", "mac-address": ROUTER_MAC, "running": "true",
             "disabled": "false", "comment": "to switch", "rx-byte": "1000", "tx-byte": "2000"},
            {".id": "*3", "name": "ether3", "type": "ether", "mac-address": "02:00:5E:10:00:03", "running": "true",
             "disabled": "false"},
            {".id": "*4", "name": "ether4", "type": "ether", "mac-address": "02:00:5E:10:00:04", "running": "false",
             "disabled": "false"},
            {".id": "*5", "name": "ether5", "type": "ether", "mac-address": "02:00:5E:10:00:05", "running": "false",
             "disabled": "true"},
            {".id": "*6", "name": "sfp1", "type": "ether", "mac-address": "02:00:5E:10:00:06", "running": "false",
             "disabled": "false"},
            {".id": "*7", "name": "bridge", "type": "bridge", "mac-address": ROUTER_MAC, "running": "true", "disabled": "false"},
            {".id": "*8", "name": "vlan20-cams", "type": "vlan", "mac-address": ROUTER_MAC, "running": "true", "disabled": "false"},
            {".id": "*9", "name": "pppoe-backup", "type": "pppoe-out", "running": "false", "disabled": "false"},
            {".id": "*A", "name": "lte1", "type": "lte", "mac-address": "02:00:5E:10:00:0A", "running": "true", "disabled": "false"},
        ],
        "ethernet": [
            {"name": "ether1", "speed": "1Gbps", "full-duplex": "true"},
            {"name": "ether2", "speed": "2.5Gbps"},
            {"name": "ether3", "speed": "100Mbps"},
            {"name": "ether4", "speed": "10Mbps"},
        ],
        "poe": [
            {"name": "ether3", "poe-out": "auto-on", "poe-out-status": "powered-on"},
            {"name": "ether4", "poe-out": "off", "poe-out-status": "off"},
        ],
        "bridges": [{".id": "*7", "name": "bridge", "vlan-filtering": "true", "pvid": "1", "protocol-mode": "rstp"}],
        "bridge_ports": [
            {".id": "*10", "interface": "ether2", "bridge": "bridge", "pvid": "1"},
            {".id": "*11", "interface": "ether3", "bridge": "bridge", "pvid": "20"},
            {".id": "*12", "interface": "ether4", "bridge": "bridge", "pvid": "1"},
        ],
        "bridge_vlans": [
            {".id": "*20", "bridge": "bridge", "vlan-ids": "20", "tagged": "bridge,ether2", "untagged": "ether3", "dynamic": "false"},
            {".id": "*21", "bridge": "bridge", "vlan-ids": "30-31", "tagged": "ether2", "disabled": "true"},
        ],
        "vlans": [{".id": "*8", "name": "vlan20-cams", "vlan-id": "20", "interface": "bridge"}],
        "addresses": [
            {".id": "*30", "address": "10.77.0.1/24", "network": "10.77.0.0", "interface": "bridge", "disabled": "false"},
            {".id": "*31", "address": "10.77.20.1/24", "network": "10.77.20.0", "interface": "vlan20-cams", "disabled": "false",
             "comment": "Cameras"},
            {".id": "*32", "address": "203.0.113.5/24", "network": "203.0.113.0", "interface": "ether1", "dynamic": "true"},
            {".id": "*33", "address": "198.51.100.77/32", "network": "198.51.100.77", "interface": "pppoe-backup", "dynamic": "true"},
            {".id": "*34", "address": "10.77.99.1/24", "network": "10.77.99.0", "interface": "bridge", "disabled": "true"},
        ],
        "pools": [{".id": "*40", "name": "dhcp-lan", "ranges": "10.77.0.100-10.77.0.200"}],
        "dhcp_servers": [{".id": "*41", "name": "dhcp1", "interface": "bridge", "address-pool": "dhcp-lan", "disabled": "false"}],
        "dhcp_clients": [{".id": "*42", "interface": "ether1", "status": "bound", "address": "203.0.113.5/24", "disabled": "false"}],
        "pppoe_clients": [{".id": "*43", "name": "pppoe-backup", "interface": "ether1", "running": "false", "disabled": "false",
                           "user": "line-user", "password": "pppoe-secret-pw"}],
        "routes": [
            {".id": "*50", "dst-address": "0.0.0.0/0", "gateway": "203.0.113.1", "immediate-gw": "203.0.113.1%ether1",
             "active": "true", "distance": "1", "dynamic": "true"},
            {".id": "*51", "dst-address": "0.0.0.0/0", "gateway": "pppoe-backup", "immediate-gw": "pppoe-backup",
             "active": "false", "inactive": "true", "distance": "10"},
            {".id": "*52", "dst-address": "10.77.0.0/24", "gateway": "bridge", "active": "true", "distance": "0"},
        ],
        "leases": [
            {".id": "*60", "mac-address": NAS_MAC, "address": "10.77.0.50", "active-address": "10.77.0.50", "host-name": "nas",
             "dynamic": "false", "status": "bound", "disabled": "false", "server": "dhcp1"},
            {".id": "*61", "mac-address": PHONE_MAC, "address": "10.77.0.101", "active-address": "10.77.0.101",
             "host-name": "phone", "dynamic": "true", "status": "bound", "disabled": "false"},
            {".id": "*62", "mac-address": LAPTOP_MAC, "address": "10.77.0.102", "host-name": "laptop", "dynamic": "true",
             "status": "bound", "disabled": "false"},
            {".id": "*63", "mac-address": OFFLINE_MAC, "address": "10.77.0.103", "host-name": "old-printer", "dynamic": "true",
             "status": "waiting", "disabled": "false"},
            {".id": "*64", "mac-address": "02:00:5E:30:00:08", "address": "10.77.0.104", "host-name": "disabled-lease",
             "dynamic": "false", "status": "waiting", "disabled": "true"},
        ],
        "arp": [
            {".id": "*70", "address": "10.77.0.50", "mac-address": NAS_MAC, "interface": "bridge", "status": "reachable", "complete": "true"},
            {".id": "*71", "address": "10.77.0.101", "mac-address": PHONE_MAC, "interface": "bridge", "status": "reachable", "complete": "true"},
            {".id": "*72", "address": "10.77.0.102", "mac-address": LAPTOP_MAC, "interface": "bridge", "status": "stale", "complete": "true"},
            {".id": "*73", "address": "10.77.20.10", "mac-address": CAM_MAC, "interface": "vlan20-cams", "status": "reachable", "complete": "true"},
            {".id": "*74", "address": "203.0.113.1", "mac-address": "02:00:5E:FF:00:01", "interface": "ether1", "status": "reachable", "complete": "true"},
            {".id": "*75", "address": "10.77.0.199", "mac-address": "02:00:5E:30:00:09", "interface": "bridge", "status": "failed", "complete": "false"},
        ],
        "bridge_hosts": [
            {".id": "*80", "mac-address": NAS_MAC, "on-interface": "ether2", "bridge": "bridge", "vid": "1", "local": "false"},
            {".id": "*81", "mac-address": DESK_MAC, "on-interface": "ether2", "bridge": "bridge", "vid": "1", "local": "false"},
            {".id": "*82", "mac-address": CAM_MAC, "on-interface": "ether3", "bridge": "bridge", "vid": "20", "local": "false"},
            {".id": "*83", "mac-address": ROUTER_MAC, "on-interface": "bridge", "bridge": "bridge", "vid": "1", "local": "true"},
            {".id": "*84", "mac-address": MULTICAST_MAC, "on-interface": "ether2", "bridge": "bridge", "vid": "1", "local": "false"},
            {".id": "*85", "mac-address": PHONE_MAC, "on-interface": "ether4", "bridge": "bridge", "vid": "1", "local": "false"},
        ],
        "neighbors": [
            {".id": "*90", "interface": "ether2", "address4": "10.77.0.2", "mac-address": SWITCH_MAC, "identity": "core-switch",
             "platform": "MikroTik", "board": "CRS305-1G-4S+", "version": "7.15.2 (stable)"},
            {".id": "*91", "interface": "ether2,bridge", "address4": "10.77.0.2", "mac-address": SWITCH_MAC, "identity": "core-switch",
             "platform": "MikroTik", "board": "CRS305-1G-4S+", "version": "7.15.2 (stable)"},
            {".id": "*92", "interface": "ether4", "address4": "10.77.0.3", "mac-address": "CC:2D:E0:00:00:02", "identity": "hall-ap",
             "platform": "MikroTik", "board": "cAP ax", "version": "7.14 (stable)"},
        ],
        "wifi_reg": [
            {".id": "*A0", "mac-address": PHONE_MAC, "interface": "radio1", "ssid": "Home", "band": "5ghz-ax", "signal": "-55"},
        ],
        "wireless_reg": [
            {".id": "*A1", "mac-address": TABLET_MAC, "interface": "wlan1", "ssid": "Guest", "band": "2ghz-n", "signal": "-60"},
        ],
        "filter": [
            {".id": "*B0", "chain": "input", "action": "accept", "connection-state": "established,related", "comment": "established"},
            {".id": "*B1", "chain": "input", "action": "drop", "in-interface": "ether1", "comment": "drop from wan"},
            {".id": "*B2", "chain": "forward", "action": "accept", "disabled": "true", "comment": "temporary open"},
            {".id": "*B3", "chain": "forward", "action": "drop", "connection-state": "invalid"},
        ],
        "nat": [
            {".id": "*C0", "chain": "srcnat", "action": "masquerade", "out-interface": "ether1"},
            {".id": "*C1", "chain": "dstnat", "action": "dst-nat", "protocol": "tcp", "dst-port": "8443", "to-addresses": "10.77.0.50",
             "to-ports": "443", "comment": "nas web"},
            {".id": "*C2", "chain": "dstnat", "action": "dst-nat", "protocol": "udp", "dst-port": "51000", "to-addresses": "10.77.0.51",
             "src-address-list": "friends", "disabled": "true"},
            {".id": "*C3", "chain": "dstnat", "action": "redirect", "protocol": "udp", "dst-port": "53", "to-ports": "53"},
        ],
    }


def raw_copy() -> dict:
    return copy.deepcopy(raw_data())


GROUPS = [
    {".id": "*1", "name": "read", "policy": "local,telnet,ssh,read,test,winbox,password,web,sniff,sensitive,api,romon,rest-api,!ftp,!reboot,!write,!policy"},
    {".id": "*2", "name": "full", "policy": "local,telnet,ssh,ftp,reboot,read,write,policy,test,winbox,password,web,sniff,sensitive,api,romon,rest-api"},
    {".id": "*3", "name": "viewonly", "policy": "read,api,rest-api,!write,!policy,!reboot,!ftp,!test,!password,!sniff,!romon,!sensitive"},
]


def users(group="viewonly") -> list:
    return [{".id": "*0", "name": "admin", "group": "full"}, {".id": "*1", "name": USER, "group": group}]
