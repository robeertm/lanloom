"""Sample replies of a home router's TR-064 services, its host list and its mesh topology (made up, built from the
documented field names; addresses from documentation ranges, hardware addresses of the locally administered range)."""
import json
from xml.sax.saxutils import escape

USER = "lanloom"
PASSWORD = "correct horse battery"
REALM = "F!Box SOAP-Auth"
SID = "0123456789abcdef"


def mac(n):
    return "02:4C:4C:00:00:%02X" % n


BOX, REPEATER, NAS, PRINTER, TABLET, PHONE, OLD = (mac(n) for n in (1, 2, 0x10, 0x11, 0x20, 0x21, 0x30))
BOX_IP = "192.168.178.1"
SOFTWARE = "154.07.57"
FIRMWARE = "7.57"

# (mac, ip, name, interface, port, active)
HOSTS = [
    (REPEATER, "192.168.178.2", "Repeater upstairs", "802.11", 0, True),
    (NAS, "192.168.178.20", "NAS", "Ethernet", 2, True),
    (PRINTER, "192.168.178.31", "Office printer", "Ethernet", 3, True),
    (TABLET, "192.168.178.52", "Kitchen tablet", "802.11", 0, True),
    (PHONE, "192.168.178.53", "Phone", "802.11", 0, True),
    (OLD, "192.168.178.60", "Old laptop", "802.11", 0, False),
]


def envelope(service: str, action: str, out: dict) -> bytes:
    inner = "".join("<%s>%s</%s>" % (k, escape(str(v)), k) for k, v in out.items())
    return ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:%sResponse xmlns:u="%s">%s</u:%sResponse>'
            '</s:Body></s:Envelope>' % (action, service, inner, action)).encode("utf-8")


def fault(code: int, desc: str = "error") -> bytes:
    return ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><s:Fault><faultcode>s:Client</faultcode>'
            '<faultstring>UPnPError</faultstring><detail><UPnPError xmlns="urn:dslforum-org:control-1-0"><errorCode>%d</errorCode>'
            '<errorDescription>%s</errorDescription></UPnPError></detail></s:Fault></s:Body></s:Envelope>' % (code, desc)).encode("utf-8")


def hostlist_xml(hosts=HOSTS) -> bytes:
    items = []
    for i, (m, ip, name, iface, port, active) in enumerate(hosts):
        items.append("<Item><Index>%d</Index><IPAddress>%s</IPAddress><MACAddress>%s</MACAddress><Active>%d</Active>"
                     "<HostName>%s</HostName><InterfaceType>%s</InterfaceType><X_AVM-DE_Port>%d</X_AVM-DE_Port>"
                     "<X_AVM-DE_Speed>0</X_AVM-DE_Speed><X_AVM-DE_UpdateAvailable>0</X_AVM-DE_UpdateAvailable>"
                     "<X_AVM-DE_UpdateSuccessful>unknown</X_AVM-DE_UpdateSuccessful><X_AVM-DE_InfoURL/><X_AVM-DE_Model/>"
                     "<X_AVM-DE_URL/><X_AVM-DE_Guest>0</X_AVM-DE_Guest><X_AVM-DE_RequestClient>0</X_AVM-DE_RequestClient>"
                     "<X_AVM-DE_VPN>0</X_AVM-DE_VPN><X_AVM-DE_WANAccess>granted</X_AVM-DE_WANAccess>"
                     "<X_AVM-DE_Disallow>0</X_AVM-DE_Disallow><X_AVM-DE_IsMeshable>0</X_AVM-DE_IsMeshable><Priority>0</Priority>"
                     "<FriendlyName>%s</FriendlyName><FriendlyNameIsWriteable>1</FriendlyNameIsWriteable></Item>"
                     % (i, ip, m, 1 if active else 0, escape(name), iface, port, escape(name)))
    return ('<?xml version="1.0" encoding="utf-8"?><List>%s</List>' % "".join(items)).encode("utf-8")


def _node(uid, name, mac_, ip, role="unknown", meshed=False, model="", fw="", ifaces=()):
    return {"uid": uid, "device_name": name, "device_model": model, "device_firmware_version": fw, "device_mac_address": mac_,
            "ip_address": ip, "is_meshed": meshed, "mesh_role": role, "meshd_version": "3.19" if meshed else "",
            "node_interfaces": [dict(i) for i in ifaces]}


def _iface(uid, name, typ, mac_, links=()):
    return {"uid": uid, "name": name, "type": typ, "mac_address": mac_, "blocking_state": "UNKNOWN", "node_links": list(links)}


def _link(uid, typ, a, b, ia, ib, state="CONNECTED"):
    return {"uid": uid, "type": typ, "state": state, "node_1_uid": a, "node_2_uid": b, "node_interface_1_uid": ia,
            "node_interface_2_uid": ib, "max_data_rate_rx": 866000, "max_data_rate_tx": 866000, "cur_data_rate_rx": 433000,
            "cur_data_rate_tx": 433000}


def mesh_doc(repeater_up: bool = True, order_swapped: bool = False) -> dict:
    """The mesh of the sample home: the box (master) with a repeater on the 5 GHz radio, a NAS on LAN 2, a printer on
    LAN 3, a tablet on the box' 2.4 GHz radio and a phone on the repeater's 2.4 GHz radio. ``order_swapped`` lists
    the two ends of every link the other way round (the parent must not depend on the order)."""
    def lk(uid, typ, a, b, ia, ib, state="CONNECTED"):
        return _link(uid, typ, b, a, ib, ia, state) if order_swapped else _link(uid, typ, a, b, ia, ib, state)

    nodes = [
        _node("n-box", "fritz.box", BOX, BOX_IP, "master", True, "FRITZ!Box 7590", FIRMWARE, [
            _iface("i-box-lan2", "LAN:2", "LAN", BOX, [lk("l-nas", "LAN", "n-box", "n-nas", "i-box-lan2", "i-nas")]),
            _iface("i-box-lan3", "LAN:3", "LAN", BOX, [lk("l-printer", "LAN", "n-box", "n-printer", "i-box-lan3", "i-printer")]),
            _iface("i-box-5g", "AP:5G:0", "WLAN", BOX, [lk("l-rep", "WLAN", "n-box", "n-rep", "i-box-5g", "i-rep-sta",
                                                         "CONNECTED" if repeater_up else "DISCONNECTED")]),
            _iface("i-box-2g", "AP:2G:0", "WLAN", BOX, [lk("l-tablet", "WLAN", "n-box", "n-tablet", "i-box-2g", "i-tablet")])]),
        _node("n-rep", "Repeater upstairs", REPEATER, "192.168.178.2", "slave", True, "FRITZ!Repeater 6000", "7.57", [
            _iface("i-rep-sta", "STA:5G:0", "WLAN", REPEATER),
            _iface("i-rep-2g", "AP:2G:0", "WLAN", REPEATER, [lk("l-phone", "WLAN", "n-rep", "n-phone", "i-rep-2g", "i-phone")])]),
        _node("n-nas", "NAS", NAS, "192.168.178.20", ifaces=[_iface("i-nas", "LAN:1", "LAN", NAS)]),
        _node("n-printer", "Office printer", PRINTER, "192.168.178.31", ifaces=[_iface("i-printer", "LAN:1", "LAN", PRINTER)]),
        _node("n-tablet", "Kitchen tablet", TABLET, "192.168.178.52", ifaces=[_iface("i-tablet", "STA:2G:0", "WLAN", TABLET)]),
        _node("n-phone", "Phone", PHONE, "192.168.178.53", ifaces=[_iface("i-phone", "STA:2G:0", "WLAN", PHONE)]),
    ]
    return {"schema_version": "1.9", "nodes": nodes}


def mesh_json(**kw) -> bytes:
    return json.dumps(mesh_doc(**kw)).encode("utf-8")


# TR-064 answers by (control path, action)
def services() -> dict:
    return {
        ("/upnp/control/deviceinfo", "GetInfo"): {"NewManufacturerName": "AVM", "NewModelName": "FRITZ!Box 7590",
            "NewSerialNumber": "SERIAL-DO-NOT-KEEP", "NewSoftwareVersion": SOFTWARE, "NewHardwareVersion": "FRITZ!Box 7590",
            "NewUpTime": "864000", "NewDeviceLog": "log line that must never be kept 203.0.113.9"},
        ("/upnp/control/lanhostconfigmgm", "GetInfo"): {"NewDHCPServerEnable": "1", "NewMinAddress": "192.168.178.20",
            "NewMaxAddress": "192.168.178.200", "NewIPRouters": BOX_IP, "NewSubnetMask": "255.255.255.0", "NewDNSServers": BOX_IP},
        ("/upnp/control/lanethernetifcfg", "GetInfo"): {"NewEnable": "1", "NewStatus": "Up", "NewMACAddress": BOX},
        ("/upnp/control/wanipconnection1", "GetStatusInfo"): {"NewConnectionStatus": "Connected", "NewLastConnectionError": "ERROR_NONE",
            "NewUptime": "3600"},
        ("/upnp/control/wanpppconn1", "GetStatusInfo"): {"NewConnectionStatus": "Unconfigured", "NewLastConnectionError": "ERROR_NONE",
            "NewUptime": "0"},
    }
