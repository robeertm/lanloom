"""Sample replies of the router's ubus API, built after the rpcd and uhttpd sources and the configuration files of the
releases. Two shapes per release where they differ:

* ``21.02`` - firewall3 (iptables), addresses as ``ipaddr`` + ``netmask``, radios with ``hwmode``, an include of the
  old firewall script, the internet link over PPPoE (with a password that must never be copied);
* ``23.05`` - firewall4 (nftables), addresses as ``ipaddr`` lists in CIDR form, radios with ``band``, bridge VLANs.

Sample values only: documentation addresses (192.0.2.0/24, 203.0.113.0/24) on the internet side, 192.168.x.0/24
inside, locally administered hardware addresses (02:4c:4c:...). The passwords and keys below are test values.
"""
import copy

READ_USER = "lanloom"
READ_PASSWORD = "read-password-for-tests-only"                     # secret-scan: fake
WRITE_USER = "lanloom-write"
WRITE_PASSWORD = "write-password-for-tests-only"                   # secret-scan: fake
RADIO_KEY = "wireless-key-never-copied"                             # secret-scan: fake
PPPOE_PASSWORD = "pppoe-password-never-copied"                     # secret-scan: fake
WG_PRIVATE = "kPrivateKeyMaterialThatMustNeverLeave000000="        # secret-scan: fake
WG_PSK = "kPresharedKeyMaterialThatMustNeverLeave00000="           # secret-scan: fake
SECRETS = (READ_PASSWORD, WRITE_PASSWORD, RADIO_KEY, PPPOE_PASSWORD, WG_PRIVATE, WG_PSK)

ROUTER_MAC = "02:4c:4c:00:00:01"
PRINTER_MAC = "02:4c:4c:00:00:40"
PHONE_MAC = "02:4c:4c:00:00:51"
LAPTOP_MAC = "02:4c:4c:00:00:52"
PLUG_MAC = "02:4c:4c:00:00:61"
OWN = "lanloom_0123456789ab"


def sec(kind, section=None, anon=False, **opts):
    """One section as it is stored (the fake renames anonymous ones like UCI does)."""
    d = {".type": kind, ".anonymous": anon}
    if section:
        d[".name"] = section
    d.update(opts)
    return d


def network(version="23.05"):
    old = version < "22.03"
    out = [sec("interface", "loopback", device="lo", proto="static", ipaddr="127.0.0.1", netmask="255.0.0.0"),
           sec("globals", "globals", ula_prefix="fd00:4c4c::/48")]
    if old:
        out += [sec("interface", "lan", type="bridge", ifname="eth0.1", proto="static", ipaddr="192.168.1.1",
                    netmask="255.255.255.0"),
                sec("interface", "iot", ifname="eth0.30", proto="static", ipaddr="192.168.30.1", netmask="255.255.255.0"),
                sec("interface", "guest", ifname="eth0.80", proto="static", ipaddr="192.168.80.1", netmask="255.255.255.0"),
                sec("interface", "wan", ifname="eth1", proto="pppoe", username="customer@example.net",
                    password=PPPOE_PASSWORD),
                sec("interface", "wan6", ifname="@wan", proto="dhcpv6"),
                sec("switch_vlan", anon=True, device="switch0", vlan="1", ports="0t 1 2 3 4")]
    else:
        out += [sec("device", anon=True, name="br-lan", type="bridge", ports=["lan1", "lan2", "lan3", "lan4"]),
                sec("bridge-vlan", anon=True, device="br-lan", vlan="1", ports=["lan1:u*", "lan2:u*", "lan3:t"]),
                sec("bridge-vlan", anon=True, device="br-lan", vlan="30", ports=["lan3:t", "lan4:u*"]),
                sec("interface", "lan", device="br-lan.1", proto="static", ipaddr=["192.168.1.1/24"]),
                sec("interface", "iot", device="br-lan.30", proto="static", ipaddr=["192.168.30.1/24"]),
                sec("interface", "guest", device="br-guest", proto="static", ipaddr=["192.168.80.1/24"]),
                sec("interface", "wan", device="wan", proto="dhcp"),
                sec("interface", "wan6", device="wan", proto="dhcpv6"),
                sec("interface", "wg0", proto="wireguard", private_key=WG_PRIVATE, listen_port="51820",
                    addresses=["10.9.0.1/24"]),
                sec("wireguard_wg0", anon=True, description="Phone", public_key="pUbLiC", preshared_key=WG_PSK,
                    allowed_ips=["10.9.0.2/32"]),
                sec("route", anon=True, interface="lan", target="10.70.0.0/16", gateway="192.168.1.254")]
    return out


def firewall(version="23.05"):
    old = version < "22.03"
    out = [sec("defaults", anon=True, syn_flood="1", input="REJECT", output="ACCEPT", forward="REJECT"),
           sec("zone", anon=True, name="lan", network=["lan"], input="ACCEPT", output="ACCEPT", forward="ACCEPT"),
           sec("zone", anon=True, name="wan", network=["wan", "wan6"], input="REJECT", output="ACCEPT", forward="REJECT",
               masq="1", mtu_fix="1"),
           sec("zone", anon=True, name="iot", network=["iot"], input="REJECT", output="ACCEPT", forward="REJECT"),
           sec("zone", anon=True, name="guest", network=["guest"], input="REJECT", output="ACCEPT", forward="REJECT"),
           sec("forwarding", anon=True, src="lan", dest="wan"),
           sec("forwarding", anon=True, src="iot", dest="wan"),
           sec("forwarding", anon=True, src="guest", dest="wan"),
           sec("rule", anon=True, name="Allow-DHCP-Renew", src="wan", proto="udp", dest_port="68", target="ACCEPT",
               family="ipv4"),
           sec("rule", anon=True, name="Allow-Ping", src="wan", proto="icmp", icmp_type="echo-request", family="ipv4",
               target="ACCEPT"),
           sec("rule", anon=True, name="IoT DNS", src="iot", dest_port="53", target="ACCEPT"),
           sec("rule", anon=True, name="IoT to the broker", src="iot", dest="lan", dest_ip="192.168.1.13", proto="tcp",
               dest_port="8883", target="ACCEPT"),
           sec("rule", anon=True, name="Guests may not reach home", src="guest", dest="lan", proto="all", target="REJECT"),
           sec("rule", anon=True, name="Guest DNS", src="guest", dest_port="53", proto=["tcp", "udp"], target="ACCEPT"),
           sec("rule", anon=True, name="Admin from outside", src="wan", proto="tcp", dest_port="22", target="ACCEPT"),
           sec("rule", anon=True, name="Old test", src="lan", dest="iot", proto="all", target="ACCEPT", enabled="0"),
           sec("rule", anon=True, name="Mark streaming", src="lan", dest="wan", proto="udp", dest_port="3478",
               target="MARK", set_mark="0x10"),
           sec("rule", anon=True, src="iot", dest="guest", proto="all", target="DROP"),
           sec("rule", OWN, name="[lanloom] IoT may not reach guests", src="iot", dest="guest", proto="all",
               target="DROP", enabled="1"),
           sec("redirect", anon=True, name="Web server", target="DNAT", src="wan", src_dport="8443", dest="lan",
               dest_ip="192.168.1.20", dest_port="443", proto="tcp")]
    if old:
        out.append(sec("include", anon=True, path="/etc/firewall.user"))
    return out


def dhcp(version="23.05"):
    return [sec("dnsmasq", anon=True, domainneeded="1", local="/lan/", domain="lan"),
            sec("dhcp", "lan", interface="lan", start="100", limit="150", leasetime="12h"),
            sec("dhcp", "wan", interface="wan", ignore="1"),
            sec("dhcp", "iot", interface="iot", start="50", limit="100", leasetime="12h"),
            sec("dhcp", "guest", interface="guest", start="10", limit="200", leasetime="1h"),
            sec("host", anon=True, name="printer", mac=PRINTER_MAC, ip="192.168.1.40")]


def wireless(version="23.05"):
    old = version < "22.03"
    r0 = dict(type="mac80211", channel="1", htmode="HT20", **({"hwmode": "11g"} if old else {"band": "2g"}))
    r1 = dict(type="mac80211", channel="36", htmode="VHT80", **({"hwmode": "11a"} if old else {"band": "5g"}))
    return [sec("wifi-device", "radio0", **r0), sec("wifi-device", "radio1", **r1),
            sec("wifi-iface", "default_radio0", device="radio0", network="lan", mode="ap", ssid="Home",
                encryption="psk2", key=RADIO_KEY),
            sec("wifi-iface", "default_radio1", device="radio1", network="lan", mode="ap", ssid="Home",
                encryption="sae-mixed", key=RADIO_KEY),
            sec("wifi-iface", anon=True, device="radio0", network="guest", mode="ap", ssid="Guests", encryption="none",
                isolate="1"),
            sec("wifi-iface", anon=True, device="radio0", network="iot", mode="ap", ssid="Things", encryption="psk2",
                hidden="1", key=RADIO_KEY),
            sec("wifi-iface", anon=True, device="radio1", network="lan", mode="sta", ssid="Uplink", encryption="psk2",
                key=RADIO_KEY)]


CONFIGS = {"network": network, "firewall": firewall, "dhcp": dhcp, "wireless": wireless}


def board(version="23.05"):
    target = "ath79/generic" if version < "22.03" else "mediatek/filogic"
    return {"kernel": "5.15.150", "hostname": "edge-router", "system": "ARMv8 Processor rev 4",
            "model": "Sample Router AX", "board_name": "sample,router-ax", "rootfs_type": "squashfs",
            "release": {"distribution": "OpenWrt", "version": version + ".5", "revision": "r24106-10cc5fcd00",
                        "target": target, "description": "OpenWrt %s.5 r24106-10cc5fcd00" % version}}


def info():
    return {"localtime": 1800000000, "uptime": 864000, "load": [12000, 9000, 8000],
            "memory": {"total": 1000000000, "free": 600000000, "shared": 1000, "buffered": 1000}}


def iface_dump(version="23.05"):
    lan_dev = "br-lan" if version < "22.03" else "br-lan.1"
    return {"interface": [
        {"interface": "lan", "up": True, "proto": "static", "device": lan_dev, "l3_device": lan_dev,
         "ipv4-address": [{"address": "192.168.1.1", "mask": 24}], "route": []},
        {"interface": "iot", "up": True, "proto": "static", "l3_device": "br-lan.30" if version >= "22.03" else "eth0.30",
         "ipv4-address": [{"address": "192.168.30.1", "mask": 24}], "route": []},
        {"interface": "guest", "up": True, "proto": "static", "l3_device": "br-guest",
         "ipv4-address": [{"address": "192.168.80.1", "mask": 24}], "route": []},
        {"interface": "wan", "up": True, "proto": "dhcp" if version >= "22.03" else "pppoe", "l3_device": "wan",
         "ipv4-address": [{"address": "203.0.113.10", "mask": 24}],
         "route": [{"target": "0.0.0.0", "mask": 0, "nexthop": "203.0.113.1"}]},
        {"interface": "wan6", "up": False, "proto": "dhcpv6", "l3_device": "wan", "ipv4-address": []},
        {"interface": "loopback", "up": True, "proto": "static", "l3_device": "lo",
         "ipv4-address": [{"address": "127.0.0.1", "mask": 8}]}]}


def device_status(version="23.05"):
    lan_dev = "br-lan" if version < "22.03" else "br-lan.1"
    return {lan_dev: {"up": True, "macaddr": ROUTER_MAC, "type": "bridge"},
            "wan": {"up": True, "macaddr": "02:4c:4c:00:00:02"}, "lo": {"up": True, "macaddr": "00:00:00:00:00:00"}}


def hints():
    return {PRINTER_MAC: {"name": "printer", "ipaddrs": ["192.168.1.40"]},
            LAPTOP_MAC: {"name": "laptop", "ipaddrs": ["192.168.1.120"]},
            PLUG_MAC: {"ipaddrs": ["192.168.30.61"]}}


def leases():
    return {"dhcp_leases": [{"hostname": "phone", "ipaddr": "192.168.1.151", "macaddr": PHONE_MAC, "expires": 3600},
                            {"hostname": "smart-plug", "ipaddr": "192.168.30.61", "macaddr": PLUG_MAC, "expires": 3000}],
            "dhcp6_leases": []}


AIR = {"phy0-ap0": ("Home", 2412, [PHONE_MAC]), "phy1-ap0": ("Home", 5180, []), "phy0-ap1": ("Guests", 2412, []),
        "phy0-ap2": ("Things", 2412, [PLUG_MAC]), "phy1-sta0": ("Uplink", 5180, [])}


def iwinfo_info(dev):
    ssid, freq, _ = AIR[dev]
    return {"phy": dev.split("-")[0], "ssid": ssid, "bssid": "02:4c:4c:00:01:%02x" % (len(dev) + freq % 50),
            "mode": "Client" if "sta" in dev else "Master", "channel": 1 if freq < 3000 else 36, "frequency": freq,
            "txpower": 20, "quality": 70, "quality_max": 70, "signal": -40, "noise": -95, "bitrate": 0,
            "encryption": {"enabled": True, "wpa": [2], "authentication": ["psk"], "ciphers": ["ccmp"]},
            "hwmodes": ["b", "g", "n", "ax"] if freq < 3000 else ["a", "n", "ac", "ax"], "htmode": "HE20"}


def iwinfo_assoc(dev):
    return {"results": [{"mac": m, "signal": -52, "noise": -95, "inactive": 10, "connected_time": 500,
                         "rx": {"rate": 144400, "packets": 1000}, "tx": {"rate": 144400, "packets": 900}}
                        for m in AIR[dev][2]]}


def acls(write=False):
    """The access list rpcd returns at sign-in for the read login (and for the write login)."""
    out = {"access-group": {"lanloom-read": ["read"]},
           "ubus": {"system": ["board", "info"], "uci": ["get"], "network.interface": ["dump"],
                    "network.device": ["status"], "luci-rpc": ["getHostHints", "getDHCPLeases"],
                    "iwinfo": ["devices", "info", "assoclist"], "session": ["access", "login"]},
           "uci": {"network": ["read"], "firewall": ["read"], "dhcp": ["read"], "wireless": ["read"]}}
    if write:
        out["access-group"] = {"lanloom-write": ["read", "write"]}
        out["ubus"]["uci"] = ["get", "add", "set", "delete", "changes", "revert", "apply", "confirm", "rollback"]
        out["uci"]["firewall"] = ["read", "write"]
    return out


def raw_sections(version="23.05"):
    return {k: copy.deepcopy(f(version)) for k, f in CONFIGS.items()}
