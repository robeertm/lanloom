"""Free text from the LAN (page titles, Server headers, SSDP and mDNS fields) never carries credentials onward."""
import pytest

from lanloom.scan import banners, mdns, ssdp
from lanloom.scan.scrub import HIDDEN, scrub

# bait: shaped like real secrets, built at run time so the repository holds no secret-looking literal
TOKEN = "x9Kq3TzW8vB1nRf5" + "Lh0YpCd7MsAe2GjUo4"          # 34 chars, letters and digits
URL_PW = "http://admin:" + "s3cret-pw" + "@192.0.2.10:8080/cam"


def test_bait_is_removed():
    assert scrub("stream " + URL_PW) == "stream http://admin:" + HIDDEN + "@192.0.2.10:8080/cam"
    assert "s3cret" not in scrub(URL_PW)
    assert TOKEN not in scrub("Camera token=" + TOKEN + " ready")
    assert scrub("Camera token=" + TOKEN + " ready") == "Camera token=" + HIDDEN + " ready"
    assert scrub("rtsp://u:" + "p@cam.lan/live") == "rtsp://u:" + HIDDEN + "@cam.lan/live"


@pytest.mark.parametrize("text", [
    "Living-Room-TV-4K", "FRITZ!Box 7590 AX", "nginx/1.25.3", "Apache/2.4.58 (Debian) OpenSSL/3.0.11",
    "uuid:2f402f80-da50-11e1-9b23-001788102201", "urn:schemas-upnp-org:device:MediaRenderer:1",
    "esp32-a4cf12b3", "Brother HL-L2350DW series", "http://192.0.2.1:49000/igddesc.xml", "user@example.org",
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "1234567890123456789012345678901234567890",
    "Samsung 7 Series (55) UE55RU7179UXZG", "https://example.org/a/b?c=d",
    "kitchen_light_2024_ceiling_lamp_left_side", "shelf-sensor-01-living-room-window-2",
])
def test_harmless_device_text_is_kept(text):
    assert scrub(text) == text


def test_none_and_empty():
    assert scrub(None) is None and scrub("") == ""


def test_scan_text_paths_use_the_filter():
    assert TOKEN not in (banners.clean_text("Admin " + TOKEN) or "")
    head = {"server": "Linux UPnP/1.0 key=" + TOKEN}
    assert TOKEN not in ssdp._clean(head["server"])
    desc = ("<root xmlns='urn:schemas-upnp-org:device-1-0'><device><friendlyName>Cam " + URL_PW +
            "</friendlyName></device></root>").encode()
    assert "s3cret" not in ssdp.parse_description(desc)["friendly_name"]


def test_mdns_names_and_txt_are_filtered():
    b = mdns.MdnsCollector()
    inst = "Printer " + TOKEN + "._ipp._tcp.local"
    b._record({"name": "_ipp._tcp.local", "type": mdns.T_PTR, "data": inst}, "192.168.1.5")
    b._record({"name": inst, "type": mdns.T_TXT, "data": {"adminurl": URL_PW, "ty": "Office printer"}}, "192.168.1.5")
    res = b.results()
    svc = res["192.168.1.5"]["services"][0]
    assert TOKEN not in svc["name"] and "s3cret" not in svc["txt"]["adminurl"]
    assert svc["txt"]["ty"] == "Office printer"
