"""cf.linux.netcheck: which hop between this machine and the network fails, and what a change of
the VM's network mode leaves behind."""
from cf.linux import netcheck as N

ADDR = ("2: enp0s1    inet 192.168.64.5/24 brd 192.168.64.255 scope global dynamic noprefixroute enp0s1\\"
        "       valid_lft 85000sec preferred_lft 85000sec\n"
        "5: waydroid0    inet 192.168.240.1/24 brd 192.168.240.255 scope global waydroid0\\       valid_lft forever\n")
ROUTE = "default via 192.168.64.1 dev enp0s1 proto dhcp metric 100 \n"


def facts(**kw):
    f = {"addrs": [("enp0s1", "192.168.1.23", 24)], "route": ("192.168.1.1", "enp0s1"), "gateway_ok": True,
         "outside": {"223.5.5.5:443": True, "1.1.1.1:443": False},
         "dns": {"www.baidu.com": ["110.242.68.3"], "mirrors.tuna.tsinghua.edu.cn": ["101.6.15.130"]},
         "web": {u: (204, "") for u in N.PORTAL_URLS}, "proxies": {}}
    f.update(kw)
    return f


def levels(f):
    return [(lv, what) for lv, what, _ in N.diagnose(f)]


def test_ip_output_is_parsed():
    assert N.parse_addrs(ADDR) == [("enp0s1", "192.168.64.5", 24), ("waydroid0", "192.168.240.1", 24)]
    assert N.parse_default(ROUTE) == ("192.168.64.1", "enp0s1")
    assert N.parse_default("default dev ppp0 scope link\n") == ("", "ppp0")
    assert N.parse_default("") is None


def test_proxy_settings_are_collected_from_everywhere():
    found = N.proxy_settings(
        {"https_proxy": "http://192.168.64.1:7897", "ALL_PROXY": "socks5://192.168.64.1:7897", "no_proxy": "localhost",
         "HTTP_PROXY": "http://127.0.0.1:8080"},
        {"/etc/environment": 'PATH="/usr/bin"\nhttp_proxy=http://192.168.64.1:7897\n#https_proxy=http://10.0.0.9:1\n',
         "/etc/apt/apt.conf.d/95proxy": 'Acquire::http::Proxy "http://192.168.64.1:7897";\n',
         "/etc/apt/apt.conf.d/50unattended": '// see http://example.org/proxy for details\nUnattended "true";\n'},
        {"mode": "manual", "http": "192.168.1.8:7890"})
    assert found == {("192.168.64.1", 7897): ["the environment", "/etc/environment", "/etc/apt/apt.conf.d/95proxy"],
                     ("192.168.1.8", 7890): ["GNOME system proxy (http)"]}
    assert N.proxy_settings({}, {}, {"mode": "none", "http": "192.168.1.8:7890"}) == {}
    assert N.proxy_target("192.168.64.1:7897") == ("192.168.64.1", 7897)
    assert N.proxy_target("https://proxy.example") == ("proxy.example", 443)
    assert N.proxy_target("http://[bad") is None


def test_a_clean_path_has_no_findings():
    assert N.diagnose(facts()) == []
    assert any("192.168.1.23/24" in line for line in N.report(facts()))
    shared = N.report(facts(addrs=[("enp0s1", "192.168.64.5", 24)]))
    assert "UTM shared network" in shared[0]


def test_the_first_hop_that_fails_is_the_only_finding():
    dead = dict(outside={"223.5.5.5:443": False}, dns={n: None for n in N.NAMES}, web={u: None for u in N.PORTAL_URLS})
    got = levels(facts(addrs=[], route=None, gateway_ok=None, **dead))
    assert got == [("FAIL", "no IPv4 address: nobody answered the DHCP request")]
    got = levels(facts(route=None, gateway_ok=None, **dead))
    assert got == [("FAIL", "an address but no default route")]
    got = levels(facts(gateway_ok=False, **dead))
    assert got == [("FAIL", "the gateway 192.168.1.1 does not answer")]
    got = levels(facts(**dead))
    assert got == [("FAIL", "the gateway answers but nothing beyond it does")]
    # a cached or router-made answer for the names does not mean the network lets the device out
    got = levels(facts(**dict(dead, dns={"www.baidu.com": ["110.242.68.3"]})))
    assert got == [("FAIL", "the gateway answers but nothing beyond it does")]
    got = levels(facts(dns={n: None for n in N.NAMES}, web={u: None for u in N.PORTAL_URLS}))
    assert got == [("FAIL", "addresses are reachable but names do not resolve")]
    assert "resolvectl dns enp0s1" in N.diagnose(facts(dns={n: None for n in N.NAMES}))[0][2]


def test_a_login_page_in_the_way_is_recognised():
    web = dict.fromkeys(N.PORTAL_URLS, (302, "http://10.1.1.1/login?mac=aa"))
    assert N.portal(web) == "http://10.1.1.1/login?mac=aa"
    got = N.diagnose(facts(outside={"223.5.5.5:443": False}, web=web))
    assert len(got) == 1 and got[0][0] == "FAIL" and "http://10.1.1.1/login?mac=aa" in got[0][1]
    assert N.portal(dict.fromkeys(N.PORTAL_URLS, (200, ""))) == ""              # page replaced in place
    assert N.portal({N.PORTAL_URLS[0]: (204, ""), N.PORTAL_URLS[1]: None}) is None
    assert N.portal({N.PORTAL_URLS[0]: (204, ""), N.PORTAL_URLS[1]: (200, "")}) is None   # one honest answer is enough
    assert N.portal(dict.fromkeys(N.PORTAL_URLS)) is None


def test_proxies_dead_alive_and_fake_ip():
    dead = {("192.168.64.1", 7897): {"where": ["the environment", "/etc/environment"], "ok": False}}
    got = N.diagnose(facts(proxies=dead))
    assert [(lv, what) for lv, what, _ in got] == [
        ("FAIL", "proxy 192.168.64.1:7897 does not answer, and it is set in the environment, /etc/environment")]
    assert "95proxy" in got[0][2] and "Allow LAN" in got[0][2] and "{HTTP,HTTPS,FTP,ALL}_PROXY" in got[0][2]
    alive = {("192.168.64.1", 7897): {"where": ["the environment"], "ok": True}}
    assert levels(facts(proxies=alive)) == [("WARN", "proxy 192.168.64.1:7897 is set in the environment")]
    got = levels(facts(dns={"www.baidu.com": ["198.18.0.7"], "mirrors.tuna.tsinghua.edu.cn": ["198.18.0.9"]}, proxies=alive))
    assert [lv for lv, _ in got] == ["WARN", "WARN"] and "198.18.x.x" in got[0][1]


def test_gather_probes_without_touching_the_network(monkeypatch):
    calls = []

    def run(cmd, timeout=8.0):
        calls.append(list(cmd))
        if cmd[:2] == ["ip", "-4"] and "addr" in cmd:
            return (0, ADDR)
        if cmd[:2] == ["ip", "-4"] and "route" in cmd:
            return (0, ROUTE)
        if cmd[0] == "ping":
            return (127, "")                                                # not installed
        if cmd[:2] == ["ip", "neigh"]:
            return (0, "192.168.64.1 dev enp0s1 lladdr 5a:94:ef:e4:0c:ee REACHABLE\n")
        if cmd[0] == "nmcli":
            return (0, "enp0s1:connected\nlo:unmanaged\n")
        if cmd[0] == "gsettings":
            return (0, "'none'\n")
        return (127, "")

    f = N.gather(run, env={"http_proxy": "http://192.168.64.1:7897"}, physical=lambda dev: dev == "enp0s1",
                 connect=lambda h, p, timeout=3.0: h == "223.5.5.5", lookup=lambda n: ["110.242.68.3"],
                 get=lambda u: (204, ""), files={}, devices=lambda: ["enp0s1"],
                 knock=lambda gw: False, link=lambda dev: {"carrier": True}, via=lambda proxy, name: "never asked")
    assert f["addrs"] == [("enp0s1", "192.168.64.5", 24)]                   # waydroid's bridge is not an uplink
    assert f["route"] == ("192.168.64.1", "enp0s1") and f["gateway_ok"] is True   # no ping, no open port, but a neighbour
    assert f["links"] == {"enp0s1": {"carrier": True, "nm": "connected"}}
    assert f["outside"]["223.5.5.5:443"] is True and f["outside"]["1.1.1.1:443"] is False
    assert f["proxies"] == {("192.168.64.1", 7897): {"where": ["the environment"], "ok": False}}
    assert levels(f) == [("FAIL", "proxy 192.168.64.1:7897 does not answer, and it is set in the environment")]
    assert f["devs"] == ["enp0s1"]
    silent = lambda cmd, timeout=8.0: (127, "") if cmd[0] == "ping" else (1, "10.0.0.1 dev enp0s1  FAILED\n")
    assert N.gateway_answers("10.0.0.1", silent, lambda gw: False) is False
    assert N.gateway_answers("10.0.0.1", silent, lambda gw: True) is True          # a machine without ping: the knock decides


def test_a_proxy_of_the_shared_network_is_named_when_the_path_stops_earlier():
    proxies = {("192.168.64.1", 7897): {"where": ["the environment", "/etc/environment"], "ok": False}}
    got = levels(facts(addrs=[], route=None, gateway_ok=None, devs=["enp0s1"], links={"enp0s1": {"carrier": True}},
                       proxies=proxies))
    assert got == [("FAIL", "no IPv4 address: nobody answered the DHCP request"),
                   ("WARN", "proxy 192.168.64.1:7897 (set in the environment, /etc/environment) is the host on UTM's "
                            "shared network; this machine is not on that network now")]
    # on the shared network the same setting is judged by whether it answers, once
    shared = facts(addrs=[("enp0s1", "192.168.64.5", 24)], route=("192.168.64.1", "enp0s1"), proxies=proxies)
    assert [lv for lv, _ in levels(shared)] == ["FAIL"]
    other = {("proxy.example", 3128): {"where": ["the environment"], "ok": False}}
    assert len(levels(facts(addrs=[], route=None, gateway_ok=None, proxies=other))) == 1


def test_knock_takes_a_refusal_for_an_answer():
    import socket
    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        closed = srv.getsockname()[1]                                       # bound, not listening: connections are refused
        assert N.knock("127.0.0.1", (closed,), 1.0) is True
        srv.listen(1)
        assert N.knock("127.0.0.1", (closed,), 1.0) is True
    assert N.knock("192.0.2.1", (9,), 0.3) is False                        # TEST-NET-1: nobody there


def test_no_address_tells_a_dead_link_from_an_unanswered_dhcp_request():
    none = dict(addrs=[], route=None, gateway_ok=None, devs=["enp0s1"])
    got = N.diagnose(facts(links={"enp0s1": {"carrier": False}}, **none))
    assert got[0][1] == "no IPv4 address and no link on enp0s1: the VM's network cable is unplugged"
    got = N.diagnose(facts(links={"enp0s1": {"carrier": True, "nm": "connecting (getting IP configuration)"}}, **none))
    assert got[0][1] == "no IPv4 address: nobody answered the DHCP request"
    lines = N.report(facts(links={"enp0s1": {"carrier": True, "nm": "connecting (getting IP configuration)"}}, **none))
    assert "          enp0s1: link up, NetworkManager: connecting (getting IP configuration)" in lines


def test_cli_prints_facts_and_verdict(monkeypatch, capsys):
    monkeypatch.setattr(N, "gather", lambda: facts(addrs=[], route=None, gateway_ok=None))
    assert N.main([]) == 1
    out = capsys.readouterr().out
    assert "address   none" in out and "!! no IPv4 address" in out and "Bridged Interface" in out
    assert "nmcli device reapply <dev>" in out
    monkeypatch.setattr(N, "gather", lambda: facts(addrs=[], route=None, gateway_ok=None, devs=["enp0s1"]))
    assert N.main([]) == 1 and "nmcli device reapply enp0s1" in capsys.readouterr().out
    monkeypatch.setattr(N, "gather", lambda: facts())
    assert N.main([]) == 0 and "ok:" in capsys.readouterr().out
    monkeypatch.setattr(N, "gather", lambda: facts(dns={"www.baidu.com": ["198.18.0.7"]}))
    assert N.main([]) == 0 and " ! www.baidu.com resolve" in capsys.readouterr().out     # a warning is not a failure


def _fake_proxy(reply, tunnel_to=None):
    """An http proxy on a loopback port that answers one CONNECT with `reply`, then either hangs
    up or (tunnel_to = a port) passes bytes both ways.  Returns its port."""
    import socket
    import threading
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def pump(a, b):
        try:
            while True:
                data = a.recv(65536)
                if not data:
                    break
                b.sendall(data)
        except OSError:
            pass
        finally:
            for x in (a, b):
                try:
                    x.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def serve():
        conn, _ = srv.accept()
        srv.close()
        head = b""
        while b"\r\n\r\n" not in head:
            head += conn.recv(4096)
        conn.sendall(reply)
        if tunnel_to is None:
            conn.close()
            return
        far = socket.create_connection(("127.0.0.1", tunnel_to))
        threading.Thread(target=pump, args=(conn, far), daemon=True).start()
        pump(far, conn)

    threading.Thread(target=serve, daemon=True).start()
    return srv.getsockname()[1]


def test_a_proxy_that_refuses_or_drops_the_tunnel_is_told_from_one_that_fetches(tmp_path):
    import shutil
    import socket
    import ssl
    import subprocess
    import threading
    import pytest
    # what apt shows as "502 Bad Gateway [IP: 192.168.64.1 7897]"
    port = _fake_proxy(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
    assert N.through_proxy(("127.0.0.1", port), "github.com", 3.0) == "502 Bad Gateway"
    # Clash with a dead node: "200 Connection established" at once, then nothing behind it
    port = _fake_proxy(b"HTTP/1.1 200 Connection established\r\n\r\n")
    assert N.through_proxy(("127.0.0.1", port), "github.com", 3.0) == "tunnel closed before the far side answered"
    with socket.socket() as dead:
        dead.bind(("127.0.0.1", 0))
        assert N.through_proxy(dead.getsockname(), "github.com", 2.0) == "Connection refused"
    if not shutil.which("openssl"):
        pytest.skip("needs openssl for a TLS server to tunnel to")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=github.com",
                    "-keyout", str(tmp_path / "k.pem"), "-out", str(tmp_path / "c.pem")], check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(tmp_path / "c.pem"), str(tmp_path / "k.pem"))
    tls = socket.socket()
    tls.bind(("127.0.0.1", 0))
    tls.listen(1)

    def answer():
        conn, _ = tls.accept()
        try:
            with ctx.wrap_socket(conn, server_side=True) as c:
                c.recv(1)
        except OSError:
            pass

    threading.Thread(target=answer, daemon=True).start()
    port = _fake_proxy(b"HTTP/1.1 200 Connection established\r\n\r\n", tunnel_to=tls.getsockname()[1])
    assert N.through_proxy(("127.0.0.1", port), "github.com", 5.0) == ""
    tls.close()


def test_a_proxy_that_takes_connections_but_fetches_nothing_is_a_failure():
    where = ["the environment", "/etc/environment"]
    dead = {("192.168.64.1", 7897): {"where": where, "ok": True,
                                     "via": {"www.baidu.com": "502 Bad Gateway", "github.com": "502 Bad Gateway"}}}
    shared = dict(addrs=[("enp0s1", "192.168.64.5", 24)], route=("192.168.64.1", "enp0s1"))
    got = N.diagnose(facts(proxies=dead, **shared))
    assert [(lv, what) for lv, what, _ in got] == [
        ("FAIL", "proxy 192.168.64.1:7897 takes connections but fetches nothing through them (www.baidu.com: 502 Bad Gateway; "
                 "github.com: 502 Bad Gateway), and it is set in the environment, /etc/environment")]
    assert "curl -x http://127.0.0.1:7897" in got[0][2] and "502 Bad Gateway [IP: 192.168.64.1 7897]" in got[0][2]
    assert any("www.baidu.com through it: 502 Bad Gateway" in line for line in N.report(facts(proxies=dead, **shared)))
    half = {("192.168.64.1", 7897): {"where": where, "ok": True,
                                     "via": {"www.baidu.com": "", "github.com": "tunnel closed before the far side answered"}}}
    got = levels(facts(proxies=half, **shared))
    assert got == [("FAIL", "proxy 192.168.64.1:7897 reaches www.baidu.com but not github.com (tunnel closed before the far "
                            "side answered), and it is set in the environment, /etc/environment")]
    fine = {("192.168.64.1", 7897): {"where": where, "ok": True, "via": {"www.baidu.com": "", "github.com": ""}}}
    assert levels(facts(proxies=fine, **shared)) == [("WARN", "proxy 192.168.64.1:7897 is set in the environment, /etc/environment")]
    assert any("github.com through it: ok" in line for line in N.report(facts(proxies=fine, **shared)))


def test_only_proxies_named_as_http_are_asked_to_fetch():
    http = set()
    N.proxy_settings({"all_proxy": "socks5://10.0.0.1:1080", "http_proxy": "http://10.0.0.2:7897"}, {},
                     {"mode": "manual", "socks": "10.0.0.3:1080", "http": "10.0.0.4:8080"}, http)
    assert http == {("10.0.0.2", 7897), ("10.0.0.4", 8080)}
    asked = []

    def run(cmd, timeout=8.0):
        return (0, ADDR) if "addr" in cmd else (0, ROUTE) if "route" in cmd else (0, "'none'\n") if cmd[0] == "gsettings" else (1, "")

    f = N.gather(run, env={"all_proxy": "socks5://10.0.0.1:1080", "http_proxy": "http://10.0.0.2:7897"},
                 physical=lambda dev: dev == "enp0s1", connect=lambda h, p, timeout=3.0: True, lookup=lambda n: ["110.242.68.3"],
                 get=lambda u: (204, ""), files={}, devices=lambda: ["enp0s1"], knock=lambda gw: True,
                 link=lambda dev: {"carrier": True}, via=lambda proxy, name: asked.append((proxy, name)) or "502 Bad Gateway")
    assert sorted(asked) == [(("10.0.0.2", 7897), "github.com"), (("10.0.0.2", 7897), "www.baidu.com")]
    assert f["proxies"][("10.0.0.2", 7897)]["via"] == {"www.baidu.com": "502 Bad Gateway", "github.com": "502 Bad Gateway"}
    assert "via" not in f["proxies"][("10.0.0.1", 1080)]


def test_without_the_ip_tool_the_kernel_tables_are_read(tmp_path):
    table = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
             "enp0s1\t0040A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n"
             "enp0s1\t00000000\t0140A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n")
    assert N.parse_proc_route(table) == ("192.168.64.1", "enp0s1")
    assert N.parse_proc_route(table.splitlines()[0] + "\n" + table.splitlines()[1] + "\n") is None
    assert N.parse_proc_route("Iface\tDestination\n" "ppp0\t00000000\t00000000\t0001\t0\t0\t0\t00000000\t0\t0\t0\n") == ("", "ppp0")
    (tmp_path / "route").write_text(table)
    route, addrs = N.kernel_tables(str(tmp_path / "route"))
    assert route == ("192.168.64.1", "enp0s1")
    assert all(not ip.startswith("127.") and 0 < plen <= 32 for _, ip, plen in addrs)       # this machine's own, loopback left out

    def run(cmd, timeout=8.0):
        return (0, "'none'\n") if cmd[0] == "gsettings" else (127, "")                      # neither ip nor ping nor nmcli

    f = N.gather(run, env={}, physical=lambda dev: True, connect=lambda h, p, timeout=3.0: True,
                 lookup=lambda n: ["110.242.68.3"], get=lambda u: (204, ""), files={}, devices=lambda: ["enp0s1"],
                 knock=lambda gw: True, link=lambda dev: {"carrier": True},
                 kernel=lambda: (("192.168.64.1", "enp0s1"), [("enp0s1", "192.168.64.5", 24), ("docker0", "172.17.0.1", 16)]))
    assert f["route"] == ("192.168.64.1", "enp0s1") and ("enp0s1", "192.168.64.5", 24) in f["addrs"]
    assert f["gateway_ok"] is True and N.diagnose(f) == []
