"""Small hand-made replies of the UniFi Network family for the adapter tests (nothing recorded from a real site).

``classic()`` is what a view-only account gets, ``official()`` what the integration API with a key gives. Both
describe the same little network, and the classic objects carry the official ids in ``external_id`` like a real
controller does:

    zones     Internal (networks Default + Lab), External, Guest (network Guest)
    networks  Default 192.168.50.0/24, Guest 198.51.100.0/24 (vlan 30), Lab 203.0.113.0/24 (vlan 20, classic only),
              WAN 1 and a VPN server network (classic only)
    equipment gateway .1, "Switch hall" .2 (uplink: gateway port 1), "AP hall" .3 (uplink: switch port 3)
    clients   Office PC (wired, switch port 2), Lab PC + Lab NAS (wired, same switch port 4), Phone (wireless),
              Guest phone (wireless)
"""
import copy

PASS = "fake-passphrase-for-tests"          # secret-scan: fake
PSK = "fake-pre-shared-key-for-tests"          # secret-scan: fake

MAC_GW, MAC_SW, MAC_AP = ("02:4c:4c:00:00:%02x" % i for i in (1, 2, 3))
MAC_OFFICE, MAC_LAB1, MAC_LAB2, MAC_PHONE, MAC_GUEST = ("02:4c:4c:00:01:%02x" % i for i in (1, 2, 3, 4, 5))


def uid(kind: str, n: int) -> str:
    """Official id (a UUID-shaped text)."""
    return "00000000-0000-4000-%s-%012d" % (kind, n)


O_NET_LAN, O_NET_GUEST, O_NET_WAN = uid("8a01", 1), uid("8a01", 2), uid("8a01", 3)
O_ZONE_INT, O_ZONE_EXT, O_ZONE_GUEST = uid("8b01", 1), uid("8b01", 2), uid("8b01", 3)
O_POL_OUT, O_POL_GUEST_OUT, O_POL_GUEST_IN, O_POL_INVALID, O_POL_SCHED, O_POL_SSH = (uid("8c01", i) for i in range(1, 7))
O_DEV_GW, O_DEV_SW, O_DEV_AP = uid("8d01", 1), uid("8d01", 2), uid("8d01", 3)
O_WIFI = uid("8e01", 1)
SITE_ID, SITE_OTHER = uid("8f01", 1), uid("8f01", 2)

C_NET_LAN, C_NET_GUEST, C_NET_LAB, C_NET_WAN, C_NET_VPN = ("5f00000000000000000000%02x" % i for i in range(1, 6))
C_ZONE_INT, C_ZONE_EXT, C_ZONE_GUEST = ("5f00000000000000000001%02x" % i for i in range(1, 4))
C_PROFILE = "5f0000000000000000000201"


def classic_wrap(rows):
    return {"meta": {"rc": "ok"}, "data": rows}


def classic() -> dict:
    """Classic replies keyed by area (see ``model`` for the keys)."""
    policy_base = {"connection_state_type": "ALL", "connection_states": [], "create_allow_respond": True, "enabled": True,
                   "ip_version": "BOTH", "logging": False, "predefined": False, "protocol": "all",
                   "schedule": {"mode": "ALWAYS"}, "hits": 7, "last_hit": 1790000000000}

    def side(zone, **kw):
        return dict({"zone_id": zone, "matching_target": "ANY", "port_matching_type": "ANY"}, **kw)

    def pol(pid, name, index, src, dst, action="ALLOW", **kw):
        return dict(policy_base, _id=pid, name=name, index=index, source=side(src), destination=side(dst), action=action, **kw)

    return {
        "sysinfo": classic_wrap([{"version": "10.2.0", "name": "Console hall"}]),
        "networkconf": classic_wrap([
            {"_id": C_NET_LAN, "external_id": O_NET_LAN, "name": "Default", "purpose": "corporate", "ip_subnet": "192.168.50.1/24",
             "vlan_enabled": False, "dhcpd_enabled": True, "dhcpd_start": "192.168.50.100", "dhcpd_stop": "192.168.50.199",
             "firewall_zone_id": C_ZONE_INT, "internet_access_enabled": True, "enabled": True},
            {"_id": C_NET_GUEST, "external_id": O_NET_GUEST, "name": "Guest", "purpose": "guest", "ip_subnet": "198.51.100.1/24",
             "vlan_enabled": True, "vlan": "30", "dhcpd_enabled": True, "dhcpd_start": "198.51.100.10", "dhcpd_stop": "198.51.100.99",
             "firewall_zone_id": C_ZONE_GUEST, "network_isolation_enabled": True, "enabled": True},
            {"_id": C_NET_LAB, "name": "Lab", "purpose": "corporate", "ip_subnet": "203.0.113.1/24", "vlan_enabled": True,
             "vlan": 20, "dhcpd_enabled": False, "firewall_zone_id": C_ZONE_INT, "enabled": True},
            {"_id": C_NET_WAN, "external_id": O_NET_WAN, "name": "WAN 1", "purpose": "wan", "wan_type": "dhcp", "enabled": True},
            {"_id": "5f0000000000000000000005", "name": "VPN server", "purpose": "remote-user-vpn", "vpn_type": "tunnel-server",
             "enabled": True},
        ]),
        "zone": [
            {"_id": C_ZONE_INT, "external_id": O_ZONE_INT, "name": "Internal", "default_zone": True, "attr_no_edit": False,
             "network_ids": [C_NET_LAN, C_NET_LAB]},
            {"_id": C_ZONE_EXT, "external_id": O_ZONE_EXT, "name": "External", "default_zone": True, "attr_no_edit": True,
             "network_ids": []},
            {"_id": C_ZONE_GUEST, "external_id": O_ZONE_GUEST, "name": "Guest", "default_zone": False, "attr_no_edit": False,
             "network_ids": [C_NET_GUEST]},
        ],
        # the policy ids equal the official ones here, so the hit counters can be matched across both APIs
        "policies": [
            pol(O_POL_OUT, "Allow internal to external", 10, C_ZONE_INT, C_ZONE_EXT, hits=4242),
            pol(O_POL_GUEST_OUT, "Allow guest to external", 20, C_ZONE_GUEST, C_ZONE_EXT, hits=99),
            pol(O_POL_GUEST_IN, "Block guest to internal", 30, C_ZONE_GUEST, C_ZONE_INT, "BLOCK", hits=3, create_allow_respond=False),
            pol(O_POL_INVALID, "Drop invalid", 5, C_ZONE_INT, C_ZONE_GUEST, "BLOCK", connection_state_type="CUSTOM",
                connection_states=["INVALID"]),
            pol(O_POL_SCHED, "Evening block", 6, C_ZONE_GUEST, C_ZONE_EXT, "BLOCK", schedule={"mode": "EVERY_WEEK"}),
        ],
        "firewallgroup": classic_wrap([{"_id": "5f0000000000000000000301", "name": "Printers", "group_type": "address-group",
                                        "group_members": ["192.168.50.40"]}]),
        "firewallrule": classic_wrap([{"_id": "5f0000000000000000000401", "name": "Old rule", "action": "drop",
                                       "ruleset": "LAN_IN", "enabled": True, "rule_index": 2000}]),
        "portconf": classic_wrap([{"_id": C_PROFILE, "name": "Office ports", "native_networkconf_id": C_NET_LAN,
                                   "tagged_vlan_mgmt": "auto"}]),
        "portforward": classic_wrap([{"_id": "5f0000000000000000000501", "name": "Web", "proto": "tcp", "dst_port": "8080",
                                      "fwd": "192.168.50.20", "fwd_port": "80", "enabled": True, "src": "any", "log": False}]),
        "wlanconf": classic_wrap([
            {"_id": "5f0000000000000000000601", "external_id": O_WIFI, "name": "Home", "security": "wpapsk", "wpa_mode": "wpa2",
             "x_passphrase": PASS, "private_preshared_keys_enabled": True,          # secret-scan: fake
             "private_preshared_keys": [{"password": PSK, "networkconf_id": C_NET_LAN}],          # secret-scan: fake
             "networkconf_id": C_NET_LAN, "wlan_bands": ["2g", "5g"], "enabled": True, "hide_ssid": False},
        ]),
        "user": classic_wrap([
            {"mac": MAC_OFFICE, "name": "Office PC", "use_fixedip": True, "fixed_ip": "192.168.50.20", "network_id": C_NET_LAN},
            {"mac": "02:4c:4c:00:01:09", "name": "Printer", "use_fixedip": True, "fixed_ip": "192.168.50.40", "network_id": C_NET_LAN},
        ]),
        "device": classic_wrap([
            {"mac": MAC_GW, "type": "ugw", "name": "Gateway", "model": "GW1", "ip": "192.168.50.1", "state": 1, "version": "5.0.0",
             "uptime": 1000, "num_sta": 0},
            {"mac": MAC_SW, "type": "usw", "name": "Switch hall", "model": "SW8", "ip": "192.168.50.2", "state": 1, "version": "7.0.0",
             "uplink": {"type": "wire", "uplink_mac": MAC_GW, "uplink_remote_port": 1, "port_idx": 8},
             "lldp_table": [{"local_port_idx": 8, "chassis_id": MAC_GW}],
             "port_table": [
                 {"port_idx": 2, "name": "Port 2", "up": True, "speed": 1000, "media": "GE", "port_poe": True, "poe_enable": True,
                  "poe_power": "3.5", "portconf_id": C_PROFILE, "rx_bytes": 10, "tx_bytes": 20},
                 {"port_idx": 3, "name": "Port 3", "up": True, "speed": 1000, "media": "GE", "port_poe": True, "poe_enable": True},
                 {"port_idx": 4, "name": "Port 4", "up": True, "speed": 100, "media": "GE", "port_poe": False},
                 {"port_idx": 8, "name": "Uplink", "up": True, "speed": 1000, "media": "GE", "is_uplink": True},
             ]},
            {"mac": MAC_AP, "type": "uap", "name": "AP hall", "model": "AP1", "ip": "192.168.50.3", "state": 1, "version": "6.0.0",
             "uplink": {"type": "wire", "uplink_mac": MAC_SW, "uplink_remote_port": 3, "port_idx": 1}},
        ]),
        "sta": classic_wrap([
            {"mac": MAC_OFFICE, "name": "Office PC", "ip": "192.168.50.20", "is_wired": True, "sw_mac": MAC_SW, "sw_port": 2,
             "network_id": C_NET_LAN},
            {"mac": MAC_LAB1, "name": "Lab PC", "ip": "203.0.113.20", "is_wired": True, "sw_mac": MAC_SW, "sw_port": 4,
             "network_id": C_NET_LAB},
            {"mac": MAC_LAB2, "name": "Lab NAS", "ip": "203.0.113.21", "is_wired": True, "sw_mac": MAC_SW, "sw_port": 4,
             "network_id": C_NET_LAB},
            {"mac": MAC_PHONE, "name": "Phone", "ip": "192.168.50.30", "is_wired": False, "ap_mac": MAC_AP, "radio": "na",
             "essid": "Home", "network_id": C_NET_LAN},
            {"mac": MAC_GUEST, "name": "Guest phone", "ip": "198.51.100.10", "is_wired": False, "ap_mac": MAC_AP, "radio": "ng",
             "is_guest": True, "network_id": C_NET_GUEST},
        ]),
    }


def official() -> dict:
    """Replies of the integration API, keyed like ``model`` expects (``info``, ``o_*``)."""
    def flt(kind, **kw):
        return dict({"type": kind}, **kw)

    def pol(pid, name, index, src, dst, action="ALLOW", origin="USER_DEFINED", **kw):
        return dict({"id": pid, "enabled": True, "name": name, "index": index,
                     "action": {"type": action, "allowReturnTraffic": action == "ALLOW"},
                     "source": {"firewallZoneId": src}, "destination": {"firewallZoneId": dst},
                     "ipProtocolScope": {"ipVersion": "IPV4_AND_IPV6", "protocolFilter": None},
                     "connectionStateFilter": [], "loggingEnabled": False, "metadata": {"origin": origin}}, **kw)

    ssh = pol(O_POL_SSH, "Allow SSH to office PC", 40, O_ZONE_GUEST, O_ZONE_INT,
              ipProtocolScope={"ipVersion": "IPV4", "protocolFilter": {"type": "NAMED_PROTOCOL", "protocol": {"name": "TCP"}}})
    ssh["destination"]["trafficFilter"] = flt("PORT", portFilter={"type": "PORTS", "items": [{"type": "PORT_NUMBER", "value": 22}]})
    ssh["source"]["trafficFilter"] = flt("NETWORK", networkFilter={"networkIds": [O_NET_GUEST], "matchOpposite": False})
    return {
        "info": {"applicationVersion": "11.0.1"},
        "o_networks": [
            {"id": O_NET_LAN, "name": "Default", "enabled": True, "vlanId": 1, "zoneId": O_ZONE_INT, "management": "GATEWAY",
             "metadata": {"origin": "SYSTEM_DEFINED"},
             "ipv4Configuration": {"hostIpAddress": "192.168.50.1", "prefixLength": 24,
                                   "dhcpConfiguration": {"mode": "SERVER", "ipAddressRange": {"start": "192.168.50.100", "end": "192.168.50.199"}}}},
            {"id": O_NET_GUEST, "name": "Guest", "enabled": True, "vlanId": 30, "zoneId": O_ZONE_GUEST, "isolationEnabled": True,
             "metadata": {"origin": "USER_DEFINED"},
             "ipv4Configuration": {"hostIpAddress": "198.51.100.1", "prefixLength": 24,
                                   "dhcpConfiguration": {"mode": "SERVER", "ipAddressRange": {"start": "198.51.100.10", "end": "198.51.100.99"}}}},
        ],
        "o_zones": [
            {"id": O_ZONE_INT, "name": "Internal", "networkIds": [O_NET_LAN], "metadata": {"origin": "SYSTEM_DEFINED"}},
            {"id": O_ZONE_EXT, "name": "External", "networkIds": [], "metadata": {"origin": "SYSTEM_DEFINED"}},
            {"id": O_ZONE_GUEST, "name": "Guest", "networkIds": [O_NET_GUEST], "metadata": {"origin": "USER_DEFINED"}},
        ],
        "o_policies": [
            pol(O_POL_OUT, "Allow internal to external", 10, O_ZONE_INT, O_ZONE_EXT, origin="SYSTEM_DEFINED"),
            pol(O_POL_GUEST_OUT, "Allow guest to external", 20, O_ZONE_GUEST, O_ZONE_EXT),
            pol(O_POL_GUEST_IN, "Block guest to internal", 30, O_ZONE_GUEST, O_ZONE_INT, "BLOCK"),
            pol(O_POL_INVALID, "Drop invalid", 5, O_ZONE_INT, O_ZONE_GUEST, "BLOCK", connectionStateFilter=["INVALID"]),
            pol(O_POL_SCHED, "Evening block", 6, O_ZONE_GUEST, O_ZONE_EXT, "BLOCK", schedule={"mode": "EVERY_WEEK"}),
            ssh,
        ],
        "o_wifi": [
            {"id": O_WIFI, "name": "Home", "enabled": True, "network": {"networkId": O_NET_LAN}, "hideName": False,
             "broadcastingFrequenciesGHz": [2.4, 5], "clientIsolationEnabled": False,
             "securityConfiguration": {"type": "WPA2_PERSONAL", "passphrase": PASS}},          # secret-scan: fake
        ],
        "o_devices": [
            {"id": O_DEV_GW, "macAddress": MAC_GW, "ipAddress": "192.168.50.1", "name": "Gateway", "model": "GW1", "state": "ONLINE",
             "firmwareVersion": "5.0.0", "features": {"gateway": {}}},
            {"id": O_DEV_SW, "macAddress": MAC_SW, "ipAddress": "192.168.50.2", "name": "Switch hall", "model": "SW8",
             "state": "ONLINE", "firmwareVersion": "7.0.0", "uplink": {"deviceId": O_DEV_GW}, "features": {"switching": {}},
             "interfaces": {"ports": [
                 {"idx": 2, "state": "UP", "connector": "RJ45", "maxSpeedMbps": 1000, "speedMbps": 1000, "poe": {"enabled": True}},
                 {"idx": 3, "state": "UP", "connector": "RJ45", "maxSpeedMbps": 1000, "speedMbps": 1000, "poe": {"enabled": True}},
                 {"idx": 4, "state": "DOWN", "connector": "RJ45", "maxSpeedMbps": 1000, "speedMbps": 0}]}},
            {"id": O_DEV_AP, "macAddress": MAC_AP, "ipAddress": "192.168.50.3", "name": "AP hall", "model": "AP1", "state": "ONLINE",
             "firmwareVersion": "6.0.0", "uplink": {"deviceId": O_DEV_SW}, "features": {"accessPoint": {}}},
        ],
        "o_clients": [
            {"type": "WIRED", "id": uid("9001", 1), "name": "Office PC", "ipAddress": "192.168.50.20", "macAddress": MAC_OFFICE,
             "uplinkDeviceId": O_DEV_SW, "access": {"type": "DEFAULT"}},
            {"type": "WIRELESS", "id": uid("9001", 2), "name": "Phone", "ipAddress": "192.168.50.30", "macAddress": MAC_PHONE,
             "uplinkDeviceId": O_DEV_AP, "access": {"type": "DEFAULT"}},
            {"type": "WIRELESS", "id": uid("9001", 3), "name": "Guest phone", "ipAddress": "198.51.100.10", "macAddress": MAC_GUEST,
             "uplinkDeviceId": O_DEV_AP, "access": {"type": "GUEST"}},
            {"type": "VPN", "id": uid("9001", 4), "name": "Remote laptop", "ipAddress": "192.168.50.200"},
        ],
        "o_wans": [{"id": O_NET_WAN, "name": "WAN 1"}],
        "o_vpn": [{"id": uid("9002", 1), "name": "Remote access", "type": "TUNNEL", "enabled": True}],
        "o_acl": [{"id": uid("9003", 1), "name": "Block lab to printer", "enabled": True}],
        "o_dns": [{"id": uid("9004", 1), "domain": "files.example.test", "enabled": True}],
    }


def both() -> dict:
    out = classic()
    out.update(official())
    return out


def only(raw: dict, *keys) -> dict:
    return {k: copy.deepcopy(v) for k, v in raw.items() if k in keys}
