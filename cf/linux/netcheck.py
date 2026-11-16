"""Why can this machine not reach the network - or what stands between it and the network?

Walks the path outwards and stops at the first hop that fails: address, default route, gateway,
anything beyond the gateway, a login page in the way, name resolution, a proxy in fake-IP mode,
proxy settings that point at an address which no longer answers.  The last one is what a change
of the VM's network mode leaves behind: `http://192.168.64.1:7897` is the host on UTM's shared
network and nothing at all on a bridged one.

    python3 -m cf.linux.netcheck          # facts, then the verdict and what to do; exit 0 = clean

Needs no config and nothing outside the standard library; proxy settings are never used for the
probes themselves.
"""
from __future__ import annotations

import http.client
import ipaddress
import os
import re
import socket
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from .netstat import is_fake_ip

OUTSIDE = (("223.5.5.5", 443), ("114.114.114.114", 53), ("119.29.29.29", 53), ("1.1.1.1", 443))
NAMES = ("www.baidu.com", "mirrors.tuna.tsinghua.edu.cn")
# each answers 204 with an empty body; anything else on these URLs was put there by the network
PORTAL_URLS = ("http://connect.rom.miui.com/generate_204",
               "http://connectivity-check.ubuntu.com/",
               "http://connectivitycheck.gstatic.com/generate_204")
PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy")
PROXY_FILES = ("/etc/environment", "/etc/apt/apt.conf", "/etc/profile.d", "/etc/apt/apt.conf.d")
UTM_SHARED = ipaddress.ip_network("192.168.64.0/24")
UNSET = "unset {http,https,ftp,all}_proxy {HTTP,HTTPS,FTP,ALL}_PROXY"

_ADDR = re.compile(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)")
_VIA = re.compile(r"\bvia\s+(\S+)")
_DEV = re.compile(r"\bdev\s+(\S+)")
_URL = re.compile(r"""(?:https?|socks[45]?h?)://[^\s"';]+""", re.I)
_ASSIGN = re.compile(r"""^\s*(?:export\s+)?([A-Za-z_]+)\s*=\s*["']?([^"'\s]+)""")


def parse_addrs(text: str) -> list[tuple[str, str, int]]:
    """(device, address, prefix length) of `ip -4 -o addr show scope global`."""
    out = []
    for line in text.splitlines():
        m = _ADDR.match(line)
        if m:
            out.append((m.group(1), m.group(2), int(m.group(3))))
    return out


def parse_default(text: str) -> Optional[tuple[str, str]]:
    """(gateway, device) of the first line of `ip -4 route show default`."""
    for line in text.splitlines():
        via, dev = _VIA.search(line), _DEV.search(line)
        if line.startswith("default") and dev:
            return (via.group(1) if via else "", dev.group(1))
    return None


def proxy_target(url: str) -> Optional[tuple[str, int]]:
    """host and port a proxy setting names ("http://192.168.64.1:7897", "192.168.64.1:7897")."""
    try:
        u = urlsplit(url if "://" in url else "http://" + url)
        if not u.hostname:
            return None
        return (u.hostname, u.port or {"https": 443, "socks5": 1080, "socks4": 1080}.get(u.scheme, 80))
    except ValueError:
        return None


def proxy_settings(env: Mapping[str, str], files: Mapping[str, str], gnome: Mapping[str, str]) -> dict:
    """{(host, port): [where it is set, ...]} over the environment, the given files' text and
    GNOME's system proxy ({"mode": "manual", "http": "host:port", ...})."""
    found: dict = {}

    def add(target, where):
        if target and target[0] not in ("localhost", "127.0.0.1", "::1"):
            found.setdefault(target, [])
            if where not in found[target]:
                found[target].append(where)

    for k, v in env.items():
        if k.lower() in PROXY_VARS and v:
            add(proxy_target(v), "the environment")
    for path, text in files.items():
        for line in text.splitlines():
            if line.lstrip().startswith(("#", "//")):
                continue
            m = _ASSIGN.match(line)
            if m and m.group(1).lower() in PROXY_VARS:
                add(proxy_target(m.group(2)), path)
            elif "proxy" in line.lower():
                for url in _URL.findall(line):
                    add(proxy_target(url), path)
    if gnome.get("mode") == "manual":
        for k, v in gnome.items():
            if k != "mode" and v:
                add(proxy_target(v), f"GNOME system proxy ({k})")
    return found


def tcp_ok(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve(name: str, timeout: float = 5.0) -> Optional[list[str]]:
    """IPv4 addresses of `name`, None when it does not resolve (in time: getaddrinfo cannot be told to give up)."""
    box: list = []

    def work():
        try:
            box.append(sorted({ai[4][0] for ai in socket.getaddrinfo(name, None, socket.AF_INET, socket.SOCK_STREAM)}))
        except OSError:
            box.append(None)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    return box[0] if box else None


def http_status(url: str, timeout: float = 5.0) -> Optional[tuple[int, str]]:
    """(status, Location) of a plain GET that follows nothing and uses no proxy; None = no answer."""
    u = urlsplit(url)
    try:
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
        try:
            conn.request("GET", u.path or "/", headers={"User-Agent": "cf-netcheck", "Connection": "close"})
            r = conn.getresponse()
            return (r.status, r.getheader("Location") or "")
        finally:
            conn.close()
    except (OSError, http.client.HTTPException):
        return None


def _run(cmd: Sequence[str], timeout: float = 8.0) -> tuple[int, str]:
    try:
        p = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
        return (p.returncode, p.stdout)
    except (OSError, subprocess.TimeoutExpired):
        return (127, "")


def _physical(dev: str) -> bool:
    """Could this interface be the way out?  Not the loopback, not the bridges docker / waydroid /
    lxc make, not the ports of such bridges, not a tunnel."""
    d = f"/sys/class/net/{dev}"
    if os.path.exists(f"{d}/device"):
        return True
    return dev != "lo" and os.path.isdir(d) and not any(os.path.exists(f"{d}/{x}") for x in ("bridge", "brport", "tun_flags"))


def _devices() -> list[str]:
    try:
        return sorted(d for d in os.listdir("/sys/class/net") if _physical(d))
    except OSError:
        return []


def _link(dev: str) -> dict:
    """{"carrier": is there a cable (None: the interface is down)}"""
    try:
        with open(f"/sys/class/net/{dev}/carrier") as f:
            return {"carrier": f.read().strip() == "1"}
    except OSError:
        return {"carrier": None}


def _read_proxy_files(paths: Sequence[str] = PROXY_FILES) -> dict[str, str]:
    out = {}
    for p in paths:
        names = [os.path.join(p, n) for n in sorted(os.listdir(p))] if os.path.isdir(p) else [p]
        for n in names:
            try:
                with open(n, encoding="utf-8", errors="replace") as f:
                    out[n] = f.read()
            except OSError:
                pass
    return out


def _gnome(run: Callable) -> dict[str, str]:
    rc, mode = run(["gsettings", "get", "org.gnome.system.proxy", "mode"])
    out = {"mode": mode.strip().strip("'")} if rc == 0 else {}
    if out.get("mode") == "manual":
        for kind in ("http", "https", "socks"):
            host = run(["gsettings", "get", f"org.gnome.system.proxy.{kind}", "host"])[1].strip().strip("'")
            port = run(["gsettings", "get", f"org.gnome.system.proxy.{kind}", "port"])[1].strip()
            if host and port not in ("", "0"):
                out[kind] = f"{host}:{port}"
    return out


def knock(host: str, ports: Sequence[int] = (53, 80), timeout: float = 1.5) -> bool:
    """Did the host react to a connection attempt?  A refusal is a reaction too."""
    for port in ports:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except ConnectionRefusedError:
            return True
        except OSError:
            pass
    return False


def gateway_answers(gw: str, run: Callable, knock: Callable = knock) -> bool:
    """A ping where there is one (a minimal Ubuntu has none), else a knock on its ports; for a
    gateway that ignores both, its hardware address in the neighbour table, which the attempts
    above have filled in when it is there at all."""
    if run(["ping", "-c", "2", "-W", "2", gw], timeout=8)[0] == 0 or knock(gw):
        return True
    neigh = run(["ip", "neigh", "show", gw])[1]
    return "lladdr" in neigh and not re.search(r"\b(FAILED|INCOMPLETE)\b", neigh)


def gather(run: Callable = _run, *, env: Optional[Mapping[str, str]] = None, physical: Callable = _physical,
           connect: Callable = tcp_ok, lookup: Callable = resolve, get: Callable = http_status,
           files: Optional[Mapping[str, str]] = None, devices: Callable = _devices,
           knock: Callable = knock, link: Optional[Callable] = None) -> dict:
    """Everything `diagnose` needs, each probe independent of the others."""
    env = os.environ if env is None else env
    route = parse_default(run(["ip", "-4", "route", "show", "default"])[1])
    addrs = [a for a in parse_addrs(run(["ip", "-4", "-o", "addr", "show", "scope", "global"])[1])
             if physical(a[0]) or (route and a[0] == route[1])]
    facts: dict = {"addrs": addrs, "route": route, "gateway_ok": None, "devs": list(devices())}
    if route and route[0]:
        facts["gateway_ok"] = gateway_answers(route[0], run, knock)
    facts["links"] = {d: (link or _link)(d) for d in facts["devs"]}
    for line in run(["nmcli", "-t", "-f", "DEVICE,STATE", "device"])[1].splitlines():
        d, _, state = line.partition(":")
        if d in facts["links"]:
            facts["links"][d]["nm"] = state
    with ThreadPoolExecutor(max_workers=12) as pool:
        outside = [pool.submit(connect, h, p) for h, p in OUTSIDE]
        names = [pool.submit(lookup, n) for n in NAMES]
        web = [pool.submit(get, u) for u in PORTAL_URLS]
        proxies = proxy_settings(env, _read_proxy_files() if files is None else files, _gnome(run))
        alive = {t: pool.submit(connect, t[0], t[1]) for t in proxies}
        facts["outside"] = {f"{h}:{p}": f.result() for (h, p), f in zip(OUTSIDE, outside)}
        facts["dns"] = {n: f.result() for n, f in zip(NAMES, names)}
        facts["web"] = {u: f.result() for u, f in zip(PORTAL_URLS, web)}
        facts["proxies"] = {t: {"where": w, "ok": alive[t].result()} for t, w in proxies.items()}
    return facts


def portal(web: Mapping[str, Optional[tuple[int, str]]]) -> Optional[str]:
    """The login page the network answers with instead of the pages asked for, "" when it only
    replaces their content, None when there is no such thing (or nothing answered at all)."""
    got = [r for r in web.values() if r]
    if not got or any(r[0] == 204 for r in got):
        return None
    return next((r[1] for r in got if r[1]), "")


def diagnose(f: dict) -> list[tuple[str, str, str]]:
    """[(level, finding, what to do)], level "FAIL" (traffic stops here; later hops are not
    judged) or "WARN" (traffic flows, but not the way the experiment assumes).  [] = clean."""
    found = _hops(f)
    if any(what.startswith("proxy ") for _, what, _ in found):
        return found
    # the path stops before a proxy could be judged: say what will be wrong with it once the path is mended
    on_shared = any(ipaddress.ip_address(ip) in UTM_SHARED for _, ip, _ in f["addrs"])
    for (host, port), p in sorted(f["proxies"].items()):
        try:
            host_is_shared = ipaddress.ip_address(host) in UTM_SHARED
        except ValueError:
            continue
        if host_is_shared and not on_shared:
            found.append(("WARN", f"proxy {host}:{port} (set in {', '.join(p['where'])}) is the host on UTM's shared "
                                  "network; this machine is not on that network now",
                          "on a bridged network nobody is at that address: everything that reads the setting will "
                          "fail even once the network works. Back on Shared Network it works again"))
    return found


def _hops(f: dict) -> list[tuple[str, str, str]]:
    addrs, route = f["addrs"], f["route"]
    dev = route[1] if route else (addrs[0][0] if addrs else next(iter(f.get("devs") or ()), "<dev>"))
    if not addrs:
        links = f.get("links") or {}
        if links and all(v.get("carrier") is not True for v in links.values()):
            return [("FAIL", "no IPv4 address and no link on " + ", ".join(sorted(links)) + ": the VM's network cable is unplugged",
                     "UTM: the VM's network device is missing or its bridged interface is not active on the Mac "
                     "(pick the one the Mac is online with, usually en0 = Wi-Fi), then start the VM again; "
                     f"inside the VM: sudo nmcli device connect {dev}")]
        return [("FAIL", "no IPv4 address: nobody answered the DHCP request",
                 "on a bridged network: the network does not hand this VM an address. Pick the interface the Mac is online "
                 "with (UTM > Network > Bridged Interface, usually en0 = Wi-Fi) instead of Automatic. A campus or "
                 "company Wi-Fi that signs in every device (802.1X / web login) or isolates its clients will not "
                 "accept the VM as a second device at all: go back to Shared Network. Retry: "
                 f"sudo nmcli device reapply {dev}   (or reboot the VM)")]
    if not route:
        return [("FAIL", "an address but no default route",
                 f"sudo nmcli device reapply {dev}   (or reboot the VM); a hand-written static address in "
                 "/etc/netplan or NetworkManager needs its gateway changed to the new network's")]
    if f["gateway_ok"] is False:
        return [("FAIL", f"the gateway {route[0]} does not answer",
                 "the address is left over from the previous network (wait for the lease or reboot the VM), or the "
                 "bridged interface chosen in UTM is not the one the Mac is online with")]
    out: list[tuple[str, str, str]] = []
    page = portal(f["web"])
    beyond = any(f["outside"].values())
    names = {n: a for n, a in f["dns"].items() if a}
    if page is not None:
        return [("FAIL", "the network answers with its own page instead of the one asked for"
                         + (f" ({page})" if page else "") + ": it wants this device signed in",
                 "open http://connect.rom.miui.com/generate_204 in the VM's Firefox and sign in (on a bridged "
                 "network the VM is a device of its own, the Mac's sign-in does not cover it); if the network "
                 "allows only one device per account, go back to Shared Network")]
    if not beyond and not any(f["web"].values()):
        return [("FAIL", "the gateway answers but nothing beyond it does",
                 "the network lets this device no further: not signed in / hardware address not registered "
                 "(campus and company networks), or the router's own access control. On a bridged network the VM "
                 "is a new device with its own hardware address; go back to Shared Network if it cannot be registered")]
    if not names:
        return [("FAIL", "addresses are reachable but names do not resolve",
                 "resolvectl status   shows the DNS server in use; a server of the previous network "
                 "(192.168.64.1) is gone after a change of network mode. Until the next reboot: "
                 f"sudo resolvectl dns {dev} 223.5.5.5")]
    fake = sorted(n for n, a in names.items() if any(is_fake_ip(x) for x in a))
    if fake:
        out.append(("WARN", f"{', '.join(fake)} resolve into 198.18.x.x: a proxy in fake-IP (TUN / enhanced) mode "
                            "on the host carries all of this machine's traffic",
                    "clients with protocols of their own (迅雷) may never find their sources and the latency "
                    "figures describe the proxy: switch the proxy's TUN mode off on the host, then "
                    "sudo resolvectl flush-caches   and restart the apps (manual §2.5)"))
    for (host, port), p in sorted(f["proxies"].items()):
        where = ", ".join(p["where"])
        if not p["ok"]:
            out.append(("FAIL", f"proxy {host}:{port} does not answer, and it is set in {where}",
                        "everything that reads the setting (apt, curl, git, pip, browsers on 'system proxy') "
                        "reaches nothing while the network itself is fine. Remove the setting (comment the lines in "
                        "/etc/environment and log in again; sudo rm /etc/apt/apt.conf.d/95proxy; "
                        "gsettings set org.gnome.system.proxy mode none; in this shell: " + UNSET + ") "
                        "or point it at the address the host has now (bridged: the Mac's own LAN address, "
                        "with Allow LAN on in the proxy)"))
        else:
            out.append(("WARN", f"proxy {host}:{port} is set in {where}",
                        "fine for apt / git. Apps started from this shell inherit it: before a run of the download "
                        "class, " + UNSET + ", or the figures describe the proxy"))
    return out


def report(f: dict) -> list[str]:
    lines = []
    for dev, ip, plen in f["addrs"]:
        shared = "  (UTM shared network: traffic leaves through macOS)" if ipaddress.ip_address(ip) in UTM_SHARED else ""
        lines.append(f"address   {dev} {ip}/{plen}{shared}")
    if not f["addrs"]:
        lines.append("address   none")
        for d, v in sorted((f.get("links") or {}).items()):
            cable = {True: "link up", False: "NO LINK", None: "interface down"}[v.get("carrier")]
            lines.append(f"          {d}: {cable}" + (f", NetworkManager: {v['nm']}" if v.get("nm") else ""))
    if f["route"]:
        state = {True: "answers", False: "NO ANSWER", None: "not probed"}[f["gateway_ok"]]
        lines.append(f"gateway   {f['route'][0] or '(on-link)'} via {f['route'][1]}: {state}")
    else:
        lines.append("gateway   no default route")
    lines.append("outside   " + ", ".join(f"{k} {'ok' if v else 'no'}" for k, v in f["outside"].items()))
    lines.append("names     " + ", ".join(f"{n} -> {' '.join(a[:2]) if a else 'NOT RESOLVED'}" for n, a in f["dns"].items()))
    lines.append("web       " + ", ".join(
        f"{urlsplit(u).hostname} {'no answer' if not r else str(r[0]) + (' -> ' + r[1] if r[1] else '')}"
        for u, r in f["web"].items()))
    for (host, port), p in sorted(f["proxies"].items()):
        lines.append(f"proxy     {host}:{port} {'answers' if p['ok'] else 'NO ANSWER'}  [{', '.join(p['where'])}]")
    if not f["proxies"]:
        lines.append("proxy     none set")
    return lines


def main(argv=None) -> int:
    import argparse
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args(argv)
    facts = gather()
    print("\n".join(report(facts)))
    found = diagnose(facts)
    print()
    if not found:
        print("ok: address, gateway, outside, names and web all answer; no proxy in the path")
        return 0
    for level, what, todo in found:
        print(f"{'!!' if level == 'FAIL' else ' !'} {what}\n   -> {todo}")
    return 1 if any(level == "FAIL" for level, _, _ in found) else 0


if __name__ == "__main__":
    sys.exit(main())
