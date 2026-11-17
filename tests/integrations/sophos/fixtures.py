"""Replies of the firewall's XML API, shaped like the published API reference and a home firewall's export (names
and addresses are made up; hardware addresses are sample values). ``REPLY`` answers the combined ``<Get>`` of every
entity the read half asks for."""

API_VERSION = "2000.2"

ZONES = """
<Zone transactionid=""><Name>LAN</Name><Type>LAN</Type><Description>Inside</Description>
  <ApplianceAccess><AdminServices><HTTPS>HTTPS</HTTPS><SSH>SSH</SSH></AdminServices>
  <NetworkServices><DNS>DNS</DNS><Ping>Ping</Ping></NetworkServices></ApplianceAccess></Zone>
<Zone transactionid=""><Name>WAN</Name><Type>WAN</Type><Description/>
  <ApplianceAccess><AdminServices><HTTPS>HTTPS</HTTPS></AdminServices><NetworkServices><Ping>Ping</Ping></NetworkServices></ApplianceAccess></Zone>
<Zone transactionid=""><Name>DMZ</Name><Type>DMZ</Type><Description/><ApplianceAccess><NetworkServices><DNS>DNS</DNS></NetworkServices></ApplianceAccess></Zone>
<Zone transactionid=""><Name>VPN</Name><Type>VPN</Type><Description/><ApplianceAccess/></Zone>
<Zone transactionid=""><Name>IoT</Name><Type>LAN</Type><Description>Things</Description><ApplianceAccess><NetworkServices><DNS>DNS</DNS></NetworkServices></ApplianceAccess></Zone>
"""

INTERFACES = """
<Interface transactionid=""><IPv4Configuration>Enable</IPv4Configuration><IPv6Configuration>Disable</IPv6Configuration>
  <Hardware>Port1</Hardware><Name>Port1</Name><NetworkZone>LAN</NetworkZone><IPv4Assignment>Static</IPv4Assignment>
  <IPAddress>192.168.10.1</IPAddress><Netmask>255.255.255.0</Netmask><MTU>1500</MTU><MSS><OverrideMSS>Disable</OverrideMSS></MSS>
  <Status>Connected, 1000 Mbps - Full Duplex</Status><MACAddress>Default</MACAddress></Interface>
<Interface transactionid=""><IPv4Configuration>Enable</IPv4Configuration><Hardware>Port2</Hardware><Name>Port2</Name>
  <NetworkZone>WAN</NetworkZone><IPv4Assignment>DHCP</IPv4Assignment><Status>Connected, 2500 Mbps - Full Duplex</Status></Interface>
<Interface transactionid=""><IPv4Configuration>Enable</IPv4Configuration><Hardware>Port3</Hardware><Name>Port3</Name>
  <NetworkZone>DMZ</NetworkZone><IPv4Assignment>Static</IPv4Assignment><IPAddress>172.16.30.1</IPAddress><Netmask>255.255.255.0</Netmask>
  <Status>Connected, 1000 Mbps - Full Duplex</Status></Interface>
<Interface transactionid=""><IPv4Configuration>Disable</IPv4Configuration><Hardware>Port4</Hardware><Name>Port4</Name>
  <NetworkZone>None</NetworkZone><Status>Disconnected</Status></Interface>
"""

VLANS = """
<VLAN transactionid=""><Zone>IoT</Zone><Interface>Port1</Interface><Hardware>Port1.20</Hardware><Name>Port1.20</Name>
  <VLANID>20</VLANID><IPv4Configuration>Enable</IPv4Configuration><IPv4Assignment>Static</IPv4Assignment>
  <IPAddress>192.168.20.1</IPAddress><Netmask>255.255.255.0</Netmask></VLAN>
"""

OBJECTS = """
<IPHost transactionid=""><Name>nas</Name><IPFamily>IPv4</IPFamily><HostType>IP</HostType><IPAddress>192.168.10.20</IPAddress></IPHost>
<IPHost transactionid=""><Name>web-server</Name><IPFamily>IPv4</IPFamily><HostType>IP</HostType><IPAddress>172.16.30.10</IPAddress></IPHost>
<IPHost transactionid=""><Name>lan-net</Name><IPFamily>IPv4</IPFamily><HostType>Network</HostType><IPAddress>192.168.10.0</IPAddress><Subnet>255.255.255.0</Subnet></IPHost>
<IPHost transactionid=""><Name>guest-range</Name><IPFamily>IPv4</IPFamily><HostType>IPRange</HostType><StartIPAddress>192.168.10.200</StartIPAddress><EndIPAddress>192.168.10.220</EndIPAddress></IPHost>
<IPHostGroup transactionid=""><Name>servers</Name><Description/><HostList><Host>nas</Host><Host>web-server</Host></HostList><IPFamily>IPv4</IPFamily></IPHostGroup>
<FQDNHost transactionid=""><Name>updates</Name><FQDN>updates.example.org</FQDN></FQDNHost>
<Services transactionid=""><Name>HTTPS</Name><Type>TCPorUDP</Type><ServiceDetails><ServiceDetail><SourcePort>1:65535</SourcePort><DestinationPort>443</DestinationPort><Protocol>TCP</Protocol></ServiceDetail></ServiceDetails></Services>
<Services transactionid=""><Name>HTTP</Name><Type>TCPorUDP</Type><ServiceDetails><ServiceDetail><SourcePort>1:65535</SourcePort><DestinationPort>80</DestinationPort><Protocol>TCP</Protocol></ServiceDetail></ServiceDetails></Services>
<Services transactionid=""><Name>DNS</Name><Type>TCPorUDP</Type><ServiceDetails><ServiceDetail><SourcePort>1:65535</SourcePort><DestinationPort>53</DestinationPort><Protocol>TCP</Protocol></ServiceDetail><ServiceDetail><SourcePort>1:65535</SourcePort><DestinationPort>53</DestinationPort><Protocol>UDP</Protocol></ServiceDetail></ServiceDetails></Services>
<Services transactionid=""><Name>MQTT-TLS</Name><Type>TCPorUDP</Type><ServiceDetails><ServiceDetail><SourcePort>1:65535</SourcePort><DestinationPort>8883</DestinationPort><Protocol>TCP</Protocol></ServiceDetail></ServiceDetails></Services>
<Services transactionid=""><Name>Ping</Name><Type>ICMPorICMPv6</Type><ServiceDetails><ServiceDetail><ICMPType>Echo Request</ICMPType><ICMPCode>Any Code</ICMPCode></ServiceDetail></ServiceDetails></Services>
<ServiceGroup transactionid=""><Name>Web</Name><Description/><ServiceList><Service>HTTP</Service><Service>HTTPS</Service></ServiceList></ServiceGroup>
"""

RULES = """
<FirewallRule transactionid=""><Name>LAN to internet</Name><Description/><IPFamily>IPv4</IPFamily><Status>Enable</Status>
  <Position>Top</Position><PolicyType>Network</PolicyType>
  <NetworkPolicy><Action>Accept</Action><LogTraffic>Disable</LogTraffic><SkipLocalDestined>Disable</SkipLocalDestined>
    <SourceZones><Zone>LAN</Zone></SourceZones><DestinationZones><Zone>WAN</Zone></DestinationZones><Schedule>All The Time</Schedule>
    <WebFilter>Allow All</WebFilter><IntrusionPrevention>generalpolicy</IntrusionPrevention></NetworkPolicy></FirewallRule>
<FirewallRule transactionid=""><Name>Things may use MQTT</Name><Description/><IPFamily>IPv4</IPFamily><Status>Enable</Status>
  <Position>After</Position><After><Name>LAN to internet</Name></After><PolicyType>Network</PolicyType>
  <NetworkPolicy><Action>Accept</Action><LogTraffic>Enable</LogTraffic>
    <SourceZones><Zone>IoT</Zone></SourceZones><DestinationZones><Zone>LAN</Zone></DestinationZones><Schedule>All The Time</Schedule>
    <DestinationNetworks><Network>nas</Network></DestinationNetworks><Services><Service>MQTT-TLS</Service></Services></NetworkPolicy></FirewallRule>
<FirewallRule transactionid=""><Name>Things to internet (web)</Name><Description/><IPFamily>IPv4</IPFamily><Status>Enable</Status>
  <Position>After</Position><PolicyType>Network</PolicyType>
  <NetworkPolicy><Action>Accept</Action><SourceZones><Zone>IoT</Zone></SourceZones><DestinationZones><Zone>WAN</Zone></DestinationZones>
    <Schedule>All The Time</Schedule><Services><Service>Web</Service><Service>DNS</Service></Services></NetworkPolicy></FirewallRule>
<FirewallRule transactionid=""><Name>Inside to DMZ</Name><Description/><IPFamily>IPv4</IPFamily><Status>Enable</Status>
  <Position>After</Position><PolicyType>Network</PolicyType>
  <NetworkPolicy><Action>Accept</Action><SourceZones><Zone>LAN</Zone><Zone>VPN</Zone></SourceZones><DestinationZones><Zone>DMZ</Zone></DestinationZones>
    <Schedule>Work hours</Schedule></NetworkPolicy></FirewallRule>
<FirewallRule transactionid=""><Name>Old test rule</Name><Description/><IPFamily>IPv4</IPFamily><Status>Disable</Status>
  <Position>After</Position><PolicyType>Network</PolicyType>
  <NetworkPolicy><Action>Accept</Action><SourceZones/><DestinationZones/><Schedule>All The Time</Schedule></NetworkPolicy></FirewallRule>
<FirewallRule transactionid=""><Name>Remote desk from outside</Name><Description/><IPFamily>IPv4</IPFamily><Status>Enable</Status>
  <Position>After</Position><PolicyType>Network</PolicyType>
  <NetworkPolicy><Action>Accept</Action><SourceZones><Zone>WAN</Zone></SourceZones><DestinationZones><Zone>LAN</Zone></DestinationZones>
    <Schedule>All The Time</Schedule></NetworkPolicy></FirewallRule>
<FirewallRule transactionid=""><Name>Staff browsing</Name><Description/><IPFamily>IPv4</IPFamily><Status>Enable</Status>
  <Position>After</Position><PolicyType>User</PolicyType>
  <UserPolicy><Action>Accept</Action><MatchIdentity>Enable</MatchIdentity><Identity><Member>Staff</Member></Identity>
    <SourceZones><Zone>LAN</Zone></SourceZones><DestinationZones><Zone>WAN</Zone></DestinationZones><Schedule>All The Time</Schedule>
    <Services><Service>Web</Service></Services></UserPolicy></FirewallRule>
<FirewallRule transactionid=""><Name>Drop things to DMZ</Name><Description/><IPFamily>IPv4</IPFamily><Status>Enable</Status>
  <Position>Bottom</Position><PolicyType>Network</PolicyType>
  <NetworkPolicy><Action>Drop</Action><SourceZones><Zone>IoT</Zone></SourceZones><DestinationZones><Zone>DMZ</Zone></DestinationZones>
    <Schedule>All The Time</Schedule></NetworkPolicy></FirewallRule>
<FirewallRule transactionid=""><Name>Mail protection</Name><Description/><IPFamily>IPv4</IPFamily><Status>Enable</Status>
  <Position>Bottom</Position><PolicyType>BusinessApplication</PolicyType><BusinessApplicationPolicy/></FirewallRule>
<FirewallRuleGroup transactionid=""><Name>Things</Name><Description/><SecurityPolicyList><SecurityPolicy>Things may use MQTT</SecurityPolicy>
  <SecurityPolicy>Things to internet (web)</SecurityPolicy><SecurityPolicy>Drop things to DMZ</SecurityPolicy></SecurityPolicyList></FirewallRuleGroup>
"""

NAT = """
<NATRule transactionid=""><Name>Web server</Name><Description/><Status>Enable</Status><Position>Top</Position>
  <LinkedFirewallrule>None</LinkedFirewallrule><TranslatedDestination>web-server</TranslatedDestination><TranslatedService>Original</TranslatedService>
  <OriginalSourceNetworks><Network>Any</Network></OriginalSourceNetworks><OriginalDestinationNetworks><Network>#Port2</Network></OriginalDestinationNetworks>
  <OriginalServices><Service>HTTPS</Service></OriginalServices><TranslatedSource>Original</TranslatedSource></NATRule>
<NATRule transactionid=""><Name>Default SNAT IPv4</Name><Description/><Status>Enable</Status><Position>Bottom</Position>
  <LinkedFirewallrule>None</LinkedFirewallrule><TranslatedDestination>Original</TranslatedDestination><TranslatedService>Original</TranslatedService>
  <TranslatedSource>MASQ</TranslatedSource></NATRule>
"""

DHCP = """
<DHCPServer transactionid=""><Name>LAN DHCP</Name><Interface>Port1 - 192.168.10.1</Interface><Status>Enable</Status>
  <IPLease><IP>192.168.10.100-192.168.10.199</IP></IPLease><SubnetMask>255.255.255.0</SubnetMask>
  <StaticIPMapping><MACIPMapping><HostName>printer</HostName><MACAddress>02:00:5e:10:00:01</MACAddress><IPAddress>192.168.10.30</IPAddress></MACIPMapping></StaticIPMapping></DHCPServer>
<DHCPServer transactionid=""><Name>IoT DHCP</Name><Interface>Port1.20 - 192.168.20.1</Interface><Status>Enable</Status>
  <IPLease><IP>192.168.20.100-192.168.20.199</IP></IPLease></DHCPServer>
"""

OTHER = """
<UnicastRoute transactionid=""><DestinationIP>192.168.70.0</DestinationIP><Netmask>255.255.255.0</Netmask><Gateway>192.168.10.2</Gateway><Interface>Port1</Interface><Distance>0</Distance><Status>Enable</Status></UnicastRoute>
<VPNIPSecConnection transactionid=""><Name>Office tunnel</Name><Description/><ConnectionType>SiteToSite</ConnectionType><Policy>IKEv2</Policy><Status>Active</Status></VPNIPSecConnection>
<SSLVPNPolicy transactionid=""><Name>Road warriors</Name><Description/></SSLVPNPolicy>
<Alias transactionid=""><Name>Port1:0</Name><Interface>Port1</Interface><IPFamily>IPv4</IPFamily><IPAddress>192.168.11.1</IPAddress><Netmask>255.255.255.0</Netmask></Alias>
<VPNIPSecConnection transactionid=""><Name>Spare</Name><Status>Disable</Status></VPNIPSecConnection>
"""

LOGIN_OK = "<Login><status>Authentication Successful</status></Login>"
LOGIN_FAIL = "<Login><status>Authentication Failure</status></Login>"


def wrap(body: str, login: str = LOGIN_OK, api: str = API_VERSION) -> bytes:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<Response APIVersion="%s" IPS_CAT_VER="1">%s%s</Response>' % (api, login, body)).encode("utf-8")


REPLY = wrap(ZONES + INTERFACES + VLANS + OBJECTS + RULES + NAT + DHCP + OTHER)
EMPTY_NAT = '<NATRule transactionid=""><Status code="529">No. of records Zero.</Status></NATRule>'
IP_REFUSED = b'<?xml version="1.0" encoding="UTF-8"?>\n<Response APIVersion="2000.2"><Status code="534">API operations are not allowed from the requester IP address.</Status></Response>'
API_OFF = b'<?xml version="1.0" encoding="UTF-8"?>\n<Response APIVersion="2000.2"><Status code="532">You need to enable the API Configuration.</Status></Response>'
