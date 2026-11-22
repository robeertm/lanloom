"""Replies of the controller's Open API as the published specification describes them (field names and codes as
documented), for a small sample site: a gateway, two switches (one offline), two access points (one waiting to be
adopted), clients on wire and radio, three networks, four wireless networks and access rules.

Test values only: documentation addresses (RFC 5737), invented hardware addresses and names."""
import copy

CID = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"          # controller ID (test value)
CLIENT_ID = "lanloom-read"
CLIENT_SECRET = "s3cret-secret-value"            # test value only
SITE_ID = "6501a1b2c3d4e5f607182930"
VERSION = "5.15.24.18"

GW = "02-00-5E-20-00-01"
CORE = "02-00-5E-20-00-02"
DESK = "02-00-5E-20-00-03"
HALL = "02-00-5E-20-00-04"
NEWAP = "02-00-5E-20-00-05"

NET_LAN, NET_IOT, NET_GUEST = "net-lan-0001", "net-iot-0002", "net-guest-0003"


def wrap(result, code=0, msg="Success."):
    return {"errorCode": code, "msg": msg, "result": result}


def page(rows, n=1, size=100):
    return {"totalRows": len(rows), "currentPage": n, "currentSize": size, "data": rows[(n - 1) * size: n * size]}


INFO = {"controllerVer": VERSION, "apiVer": "3", "configured": True, "type": 0, "supportApp": True, "omadacId": CID}

SITES = [
    {"siteId": SITE_ID, "name": "Default", "region": "Germany", "timeZone": "Europe/Berlin", "scenario": "Home", "primary": True, "type": 0},
    {"siteId": "6501a1b2c3d4e5f607182931", "name": "Cabin", "region": "Germany", "timeZone": "Europe/Berlin", "scenario": "Home", "primary": False, "type": 0},
]


def _dev(mac, name, kind, ip, status=1, up=None, port=None, model="Sample", fw="1.2.3", uptime="10day(s) 3h 4m 5s"):
    return {"mac": mac, "name": name, "type": kind, "ip": ip, "status": status, "model": model, "modelName": model,
            "firmwareVersion": fw, "uptime": uptime, "cpuUtil": 4, "memUtil": 41, "uplinkDeviceMac": up or "", "uplinkDevicePort": port or "",
            "sn": "SN-NEVER-COPIED"}


DEVICES = [
    _dev(GW, "Edge gateway", "gateway", "192.168.10.1", model="GW-1"),
    _dev(CORE, "Core switch", "switch", "192.168.10.2", up=GW, port="5", model="SW-8P", fw="1.0.9"),
    _dev(DESK, "Desk switch", "switch", "192.168.10.3", status=0, up=CORE, port="8", model="SW-5", fw="1.0.9"),
    _dev(HALL, "Hall AP", "ap", "192.168.10.4", up=CORE, port="3", model="AP-650", fw="1.3.1"),
    _dev(NEWAP, "New AP", "ap", "192.168.10.5", status=2, model="AP-650", fw="1.3.1"),
]


def _cl(mac, name, ip, wired, **kw):
    base = {"mac": mac, "name": name, "ip": ip, "active": True, "wireless": not wired, "connectType": 2 if wired else 1,
            "guest": False, "blocked": False, "vendor": "Sample Vendor"}
    base.update(kw)
    return base


CLIENTS = [
    _cl("3C-00-5E-30-00-01", "NAS", "192.168.10.20", True, switchMac=CORE, connectDevType="switch", port=4, networkName="LAN",
        ipSetting={"useFixedAddr": True, "ip": "192.168.10.20"}),
    _cl("3C-00-5E-30-00-02", "Printer", "192.168.10.30", True, switchMac=CORE, connectDevType="switch", port=6, networkName="LAN"),
    _cl("3C-00-5E-30-00-03", "Camera", "192.168.20.40", True, switchMac=CORE, connectDevType="switch", port=6, networkName="IoT"),
    _cl("3C-00-5E-30-00-04", "Wired at gateway", "192.168.10.60", True, gatewayMac=GW, connectDevType="gateway", port=3, networkName="LAN"),
    _cl("02-00-5E-30-00-05", "Phone", "192.168.10.50", False, apMac=HALL, connectDevType="ap", ssid="Home", radioId=1, rssi=-58, signalLevel=80),
    _cl("3C-00-5E-30-00-06", "Guest laptop", "192.168.30.10", False, apMac=HALL, connectDevType="ap", ssid="Guests", radioId=0,
        connectType=0, guest=True, rssi=-104),
    _cl("3C-00-5E-30-00-07", "Blocked tablet", "192.168.10.70", False, apMac=HALL, connectDevType="ap", ssid="Home", radioId=3, blocked=True),
]

NETWORKS = [
    {"id": NET_LAN, "name": "LAN", "purpose": 1, "vlanType": 0, "vlan": 1, "gatewaySubnet": "192.168.10.1/24", "primary": True,
     "dhcpSettingsVO": {"enable": True, "hostIP": "192.168.10.1", "dhcpPoolMask": 24, "ipaddrStart": "192.168.10.100", "ipaddrEnd": "192.168.10.199"}},
    {"id": NET_IOT, "name": "IoT", "purpose": 1, "vlanType": 0, "vlan": 20, "gatewaySubnet": "192.168.20.1/24", "primary": False,
     "dhcpSettingsVO": {"enable": True, "hostIP": "192.168.20.1", "dhcpPoolMask": 24, "ipaddrStart": "192.168.20.50", "ipaddrEnd": "192.168.20.150"}},
    {"id": NET_GUEST, "name": "Guest", "purpose": 0, "vlan": 30, "primary": False,
     "dhcpSettingsVO": {"enable": False, "hostIP": "192.168.30.1", "dhcpPoolMask": 24}},
]

PROFILES = [
    {"id": "prof-all", "name": "All", "flag": 0, "nativeNetworkId": NET_LAN, "poe": 2},
    {"id": "prof-iot", "name": "IoT ports", "flag": 2, "nativeNetworkId": NET_IOT, "poe": 2},
]

WLANS = [{"id": "wl-default", "wlanId": "wl-default", "name": "Default", "primary": True}]

SSIDS = [
    {"id": "ss-home", "ssidId": "ss-home", "name": "Home", "band": 3, "broadcast": True, "guestNetEnable": False, "security": 3,
     "vlanEnable": False, "vlanId": 0},
    {"id": "ss-iot", "ssidId": "ss-iot", "name": "IoT", "band": 1, "broadcast": True, "guestNetEnable": False, "security": 3,
     "vlanEnable": True, "vlanId": 20},
    {"id": "ss-guest", "ssidId": "ss-guest", "name": "Guests", "band": 7, "broadcast": True, "guestNetEnable": True, "security": 0,
     "vlanEnable": True, "vlanId": 30},
    {"id": "ss-hidden", "ssidId": "ss-hidden", "name": "Lab", "band": 2, "broadcast": False, "guestNetEnable": False, "security": 2,
     "vlanEnable": False, "vlanId": 0},
]

ACL_GATEWAY = [
    {"id": "acl-g1", "index": 1, "description": "Block IoT to LAN", "policy": 0, "status": True, "sourceType": 0, "sourceIds": [NET_IOT],
     "destinationType": 0, "destinationIds": [NET_LAN], "protocols": [256], "direction": {"lanToLan": True, "lanToWan": False},
     "stateMode": 0},
    {"id": "acl-g2", "index": 2, "description": "Allow all", "policy": 1, "status": False, "sourceType": 0, "sourceIds": [NET_LAN],
     "destinationType": 0, "destinationIds": [NET_GUEST], "protocols": [256], "direction": {"lanToLan": True, "lanToWan": False},
     "stateMode": 0},
]
ACL_SWITCH = [
    {"id": "acl-s1", "index": 1, "description": "", "policy": 0, "status": True, "sourceType": 0, "sourceIds": [NET_GUEST],
     "destinationType": 0, "destinationIds": [NET_LAN], "protocols": [256], "bindingType": 0},
]
ACL_EAP = []

WANS = [
    {"name": "WAN1", "port": 1, "mode": 0, "proto": "dhcp", "internetState": 1, "status": 1, "ip": "203.0.113.9", "latency": 12, "loss": 0.0},
    {"name": "LAN2", "port": 2, "mode": 1, "proto": "", "internetState": 0, "status": 1},
]


def _ports(linked):
    plist = [{"port": i, "name": "Port %d" % i, "status": 1, "profileId": "prof-all", "profileName": "All", "poeMode": 1, "lagPort": False}
             for i in range(1, 9)]
    plist[5]["profileId"], plist[5]["profileName"] = "prof-iot", "IoT ports"
    st = []
    for i in range(1, 9):
        up = i in linked
        entry = {"port": i, "name": "Port %d" % i, "type": 1, "disable": False,
                 "portStatus": {"linkStatus": 1 if up else 0, "linkSpeed": linked.get(i, 0), "poe": i == 3, "rx": 10, "tx": 20}}
        if i == 3:
            entry["downlink"] = {"mac": HALL, "name": "Hall AP", "type": "ap"}
        st.append(entry)
    return plist, st


_PL, _ST = _ports({3: 3, 4: 3, 5: 3, 6: 2})
SWITCH_INFO = {CORE: {"mac": CORE, "ip": "192.168.10.2", "portList": _PL, "model": "SW-8P"}}
SWITCH_STAT = {CORE: {"name": "Core switch", "portNum": 8, "ports": _ST, "supportPoe": True, "uplink": {"mac": GW, "port": 5, "name": "Edge gateway"}}}


def acl_page_for(scope):
    return {"gateway": ACL_GATEWAY, "switch": ACL_SWITCH, "eap": ACL_EAP}[scope]


def site_data():
    """A fresh deep copy of everything the fake controller serves (tests change it freely)."""
    return copy.deepcopy({"devices": DEVICES, "clients": CLIENTS, "networks": NETWORKS, "profiles": PROFILES, "wlans": WLANS,
                          "ssids": {"wl-default": SSIDS}, "acl_gateway": ACL_GATEWAY, "acl_switch": ACL_SWITCH, "acl_eap": ACL_EAP,
                          "wans": WANS, "switch_info": SWITCH_INFO, "switch_stat": SWITCH_STAT})
