# Sprint 4: Network Verification Report

## 1. Topology

![CML topology](screenshots/topology.png)

## 2. Router R1 Verification

### 2.1 Interface status (`show ip interface brief`)

![show ip interface brief](screenshots/r1_show_ip_int_br.png)

### 2.2 Routing table (`show ip route`)

![show ip route](screenshots/r1_show_ip_route.png)

### 2.3 DHCP leases (`show ip dhcp binding`)

![show ip dhcp binding](screenshots/r1_show_ip_dhcp_binding.png)

## 3. Game Server Configuration

![Server IP](screenshots/server_ip_addr_show.png)
![Server ip route](screenshots/server_ip_route.png)

## 4. Cross-Subnet Connectivity

### Client 1 to the game server

![Client 1 ping](screenshots/client1_ping.png)

### Client 2 to the game server

![Client 2 ping](screenshots/client2_ping.png)

## 5. DNS Resolution

### Client 1 nslookup server.rassadina.edu

![Client 1 nslookup](screenshots/client1_nslookup.png)

### Client 2 nslookup server.rassadina.edu

![Client 2 nslookup](screenshots/client2_nslookup.png)

## 6. Wireshark DNS Capture

Capture file: [`sprint_4_dns_lookup.pcap`](sprint_4_dns_lookup.pcap), taken on the link between R1 and Client 1 while running `nslookup server.rassadina.edu`.

![Annotated DNS capture](screenshots/wireshark_dns_annotated.png)

- **Query:** Client 1 (192.168.10.2) sends a standard DNS query for the A record to R1 (192.168.10.1) on UDP port 53.
- **Response:** R1 answers with `server.rassadina.edu` = `192.168.20.100`.
- The second query/response pair is the IPv6 (AAAA) lookup that `nslookup` sends along with the IPv4 one. R1 only has an IPv4 host record, so that lookup returns "No such name", which is expected.
