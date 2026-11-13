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
    assert found == {("192.168.64.1", 7897): ["$https_proxy", "$ALL_PROXY", "/etc/environment", "/etc/apt/apt.conf.d/95proxy"],
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
    dead = {("192.168.64.1", 7897): {"where": ["$http_proxy", "/etc/environment"], "ok": False}}
    got = N.diagnose(facts(proxies=dead))
    assert [(lv, what) for lv, what, _ in got] == [
        ("FAIL", "proxy 192.168.64.1:7897 does not answer, and it is set in $http_proxy, /etc/environment")]
    assert "95proxy" in got[0][2] and "Allow LAN" in got[0][2]
    alive = {("192.168.64.1", 7897): {"where": ["$http_proxy"], "ok": True}}
    assert levels(facts(proxies=alive)) == [("WARN", "proxy 192.168.64.1:7897 is set in $http_proxy")]
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
            return (1, "")
        if cmd[:2] == ["ip", "neigh"]:
            return (0, "192.168.64.1 dev enp0s1 lladdr 5a:94:ef:e4:0c:ee REACHABLE\n")
        if cmd[0] == "gsettings":
            return (0, "'none'\n")
        return (127, "")

    f = N.gather(run, env={"http_proxy": "http://192.168.64.1:7897"}, physical=lambda dev: dev == "enp0s1",
                 connect=lambda h, p, timeout=3.0: h == "223.5.5.5", lookup=lambda n: ["110.242.68.3"],
                 get=lambda u: (204, ""), files={}, devices=lambda: ["enp0s1"])
    assert f["addrs"] == [("enp0s1", "192.168.64.5", 24)]                   # waydroid's bridge is not an uplink
    assert f["route"] == ("192.168.64.1", "enp0s1") and f["gateway_ok"] is True   # no ping answer, but it is a neighbour
    assert f["outside"]["223.5.5.5:443"] is True and f["outside"]["1.1.1.1:443"] is False
    assert f["proxies"] == {("192.168.64.1", 7897): {"where": ["$http_proxy"], "ok": False}}
    assert levels(f) == [("FAIL", "proxy 192.168.64.1:7897 does not answer, and it is set in $http_proxy")]
    assert f["devs"] == ["enp0s1"]
    assert N.gateway_answers("10.0.0.1", lambda cmd, timeout=8.0: (1, "10.0.0.1 dev enp0s1  FAILED\n")) is False


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
