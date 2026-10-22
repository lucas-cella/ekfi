#!/usr/bin/env python3
"""Ekfi — todos os KPIs do Wi-Fi conectado, direto no terminal (Linux).

Coleta dados de iw, nmcli, ethtool, sysfs, ping, dig e journalctl, sem
precisar de root nem de bibliotecas externas.
"""

import argparse
import curses
import json
import locale
import math
import os
import re
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime

VERSION = "2.0.0"
ENV = dict(os.environ, LC_ALL="C", LANG="C")
U64_MAX = 2**64 - 1


# ───────────────────────────── utilidades ─────────────────────────────

def sh(cmd, timeout=15):
    """Executa um comando e devolve stdout ('' em caso de erro)."""
    if shutil.which(cmd[0]) is None:
        return ""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=ENV).stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def to_num(s):
    try:
        f = float(s)
        return int(f) if f.is_integer() and "." not in str(s) else f
    except (TypeError, ValueError):
        return None


def lead_num(s):
    """Primeiro número de um texto como '2016 seconds' ou '-87 [-87, -73] dBm'."""
    m = re.match(r"\s*(-?\d+(?:\.\d+)?)", s or "")
    return to_num(m.group(1)) if m else None


def pct(part, total):
    if part is None or not total:
        return None
    return 100.0 * part / total


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def lin_score(v, best, worst):
    """Mapeia v para 0–100 linearmente entre worst (0) e best (100)."""
    if v is None:
        return None
    return clamp(100.0 * (v - worst) / (best - worst), 0, 100)


def fmt_bytes(n):
    if n is None:
        return None
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1000.0


def fmt_bps(n):
    if n is None:
        return None
    for unit in ("bit/s", "kbit/s", "Mbit/s", "Gbit/s"):
        if abs(n) < 1000 or unit == "Gbit/s":
            return f"{n:.0f} {unit}" if unit == "bit/s" else f"{n:.2f} {unit}"
        n /= 1000.0


def fmt_dur(s):
    if s is None:
        return None
    s = int(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = [f"{d}d"] if d else []
    if d or h:
        parts.append(f"{h}h")
    if d or h or m:
        parts.append(f"{m}min")
    parts.append(f"{s}s")
    return " ".join(parts)


def freq_band(f):
    if f is None:
        return None
    if f < 3000:
        return "2.4 GHz"
    if f < 5925:
        return "5 GHz"
    return "6 GHz"


def pct_to_dbm(p):
    """Inverso da conversão do NetworkManager (−100 dBm = 0 %, −40 dBm = 100 %)."""
    return -40 - (100 - p) * 0.6


# ───────────────────────────── cores / saída ─────────────────────────────

class C:
    on = sys.stdout.isatty()

    @classmethod
    def w(cls, code, s):
        return f"\033[{code}m{s}\033[0m" if cls.on else str(s)

    @classmethod
    def bold(cls, s): return cls.w("1", s)
    @classmethod
    def dim(cls, s): return cls.w("2", s)
    @classmethod
    def green(cls, s): return cls.w("32", s)
    @classmethod
    def yellow(cls, s): return cls.w("33", s)
    @classmethod
    def red(cls, s): return cls.w("31", s)
    @classmethod
    def cyan(cls, s): return cls.w("36", s)
    @classmethod
    def blue(cls, s): return cls.w("34", s)
    @classmethod
    def c256(cls, n, s): return cls.w(f"38;5;{n}", s)


GRADES = [(80, "Excelente", C.green), (60, "Bom", C.green), (40, "Regular", C.yellow),
          (20, "Ruim", C.red), (0, "Crítico", C.red)]


def grade(score):
    for limit, name, color in GRADES:
        if score >= limit:
            return name, color
    return GRADES[-1][1:]


def rate(v, good, ok, higher_better=True):
    """Classifica v em bom/atenção/ruim e devolve a função de cor."""
    if v is None:
        return C.dim
    if not higher_better:
        v, good, ok = -v, -good, -ok
    if v >= good:
        return C.green
    if v >= ok:
        return C.yellow
    return C.red


SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values):
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1
    return "".join(SPARK[int((v - lo) / span * (len(SPARK) - 1))] if v is not None else " " for v in values)


def bar(score, width=20):
    if score is None:
        return ""
    filled = int(round(score / 100 * width))
    return rate(score, 60, 40)("█" * filled) + C.dim("░" * (width - filled))


# ───────────────────────────── coletores ─────────────────────────────

def detect_iface():
    cur = None
    for line in sh(["iw", "dev"]).splitlines():
        line = line.strip()
        if line.startswith("Interface "):
            cur = line.split()[1]
        elif line.startswith("ssid ") and cur:
            return cur
    for line in sh(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "dev"]).splitlines():
        dev, typ, state = (line.split(":") + ["", ""])[:3]
        if typ == "wifi" and state.startswith("connected"):
            return dev
    return None


def phy_of(iface):
    try:
        return open(f"/sys/class/net/{iface}/phy80211/name").read().strip()
    except OSError:
        return "phy0"


def parse_kv(text):
    out = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip().lower()] = v.strip()
    return out


def parse_bitrate(s, freq=None):
    """'65.0 MBit/s VHT-MCS 1 80MHz short GI VHT-NSS 1' → dict."""
    if not s:
        return None
    r = {"raw": s}
    m = re.search(r"([\d.]+) MBit/s", s)
    r["mbps"] = float(m.group(1)) if m else None
    if "EHT-MCS" in s:
        r["standard"], fam = "Wi-Fi 7 (802.11be)", "EHT"
    elif "HE-MCS" in s:
        r["standard"], fam = ("Wi-Fi 6E (802.11ax)" if freq and freq >= 5925 else "Wi-Fi 6 (802.11ax)"), "HE"
    elif "VHT-MCS" in s:
        r["standard"], fam = "Wi-Fi 5 (802.11ac)", "VHT"
    elif re.search(r"\bMCS \d+", s):
        r["standard"], fam = "Wi-Fi 4 (802.11n)", "HT"
    else:
        legacy = "802.11b/g" if freq and freq < 3000 else "802.11a"
        r["standard"], fam = f"Legado ({legacy})", "LEGACY"
    r["family"] = fam
    m = re.search(r"MCS (\d+)", s)
    r["mcs"] = int(m.group(1)) if m else None
    m = re.search(r"NSS (\d+)", s)
    r["nss"] = int(m.group(1)) if m else (r["mcs"] // 8 + 1 if fam == "HT" and r["mcs"] is not None else 1)
    if fam == "HT" and r["mcs"] is not None:
        r["mcs"] %= 8
    m = re.search(r"(\d+)MHz", s)
    r["width"] = int(m.group(1)) if m else (40 if "40MHz" in s else 20)
    m = re.search(r"HE-GI (\d)", s)
    if m:
        r["gi_us"] = {"0": 0.8, "1": 1.6, "2": 3.2}.get(m.group(1))
    else:
        r["gi_us"] = 0.4 if "short GI" in s else 0.8
    return r


def parse_dbm_chains(v):
    """'-87 [-87, -73] dBm' → (-87, [-87, -73])."""
    if not v:
        return None, []
    m = re.match(r"(-?\d+)", v)
    main = int(m.group(1)) if m else None
    chains = [int(x) for x in re.findall(r"-?\d+", v.split("[", 1)[1])] if "[" in v else []
    return main, chains


def get_station(iface):
    st = parse_kv(sh(["iw", "dev", iface, "station", "dump"]))
    link = parse_kv(sh(["iw", "dev", iface, "link"]))
    m = re.search(r"Connected to ([0-9a-f:]{17})", sh(["iw", "dev", iface, "link"]))
    st["_bssid"] = m.group(1) if m else None
    st["_ssid"] = link.get("ssid")
    st["_freq"] = to_num(link.get("freq", "").split()[0]) if link.get("freq") else None
    return st


def get_iw_info(iface):
    txt = sh(["iw", "dev", iface, "info"])
    r = {}
    m = re.search(r"channel (\d+) \((\d+) MHz\), width: (\d+) MHz(?:, center1: (\d+) MHz)?", txt)
    if m:
        r["channel"], r["freq"], r["width"] = int(m.group(1)), int(m.group(2)), int(m.group(3))
        r["center1"] = int(m.group(4)) if m.group(4) else r["freq"]
    m = re.search(r"txpower ([\d.]+) dBm", txt)
    r["txpower"] = float(m.group(1)) if m else None
    return r


def get_survey(iface):
    for block in sh(["iw", "dev", iface, "survey", "dump"]).split("Survey data from"):
        if "[in use]" in block:
            kv = parse_kv(block)
            g = lambda k: to_num((kv.get(k) or "").split()[0]) if kv.get(k) else None
            return {"noise": g("noise"), "active": g("channel active time"), "busy": g("channel busy time"),
                    "rx": g("channel receive time"), "tx": g("channel transmit time")}
    return {}


def get_ethtool(iface):
    stats = {}
    for line in sh(["ethtool", "-S", iface]).splitlines()[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            n = to_num(v.strip())
            stats[k.strip()] = None if n in (U64_MAX, None) or (isinstance(n, int) and n > 2**63) else n
    info = parse_kv(sh(["ethtool", "-i", iface]))
    return stats, info


def get_sysfs(iface):
    base = f"/sys/class/net/{iface}/statistics"
    out = {}
    try:
        for name in os.listdir(base):
            with open(os.path.join(base, name)) as f:
                out[name] = int(f.read().strip())
    except OSError:
        pass
    return out


def get_pci_name(iface):
    try:
        slot = os.path.basename(os.path.realpath(f"/sys/class/net/{iface}/device"))
    except OSError:
        return None
    line = sh(["lspci", "-s", slot]).strip()
    return line.split(": ", 1)[1] if ": " in line else None


def get_nm_device(iface):
    d = {"ipv4": [], "ipv6": [], "dns": []}
    for line in sh(["nmcli", "-t", "dev", "show", iface]).splitlines():
        k, _, v = line.partition(":")
        if k.startswith("IP4.ADDRESS"):
            d["ipv4"].append(v)
        elif k.startswith("IP6.ADDRESS"):
            d["ipv6"].append(v)
        elif k.startswith(("IP4.DNS", "IP6.DNS")):
            d["dns"].append(v)
        elif k == "IP4.GATEWAY":
            d["gateway"] = v or None
        elif k == "IP6.GATEWAY":
            d["gateway6"] = v or None
        elif k == "GENERAL.MTU":
            d["mtu"] = to_num(v)
        elif k == "GENERAL.HWADDR":
            d["mac"] = v.lower()
        elif k == "GENERAL.CONNECTION":
            d["connection"] = v
        elif k.startswith("DHCP4.OPTION"):
            ok, _, ov = v.partition(" = ")
            if ok.strip() == "dhcp_lease_time":
                d["dhcp_lease_s"] = to_num(ov)
            elif ok.strip() == "expiry":
                d["dhcp_expiry"] = to_num(ov)
            elif ok.strip() == "dhcp_server_identifier":
                d["dhcp_server"] = ov.strip()
    d["connectivity"] = sh(["nmcli", "networking", "connectivity"]).strip() or None
    if d.get("connection"):
        kv = parse_kv(sh(["nmcli", "-t", "-f", "802-11-wireless-security.key-mgmt,802-11-wireless-security.pmf,"
                          "802-11-wireless.mac-address-randomization,802-11-wireless.cloned-mac-address",
                          "connection", "show", d["connection"]]))
        d["key_mgmt"] = kv.get("802-11-wireless-security.key-mgmt")
        d["pmf_cfg"] = kv.get("802-11-wireless-security.pmf")
        d["cloned_mac"] = kv.get("802-11-wireless.cloned-mac-address")
    return d


def split_nmcli(line):
    return [p.replace("\\:", ":") for p in re.split(r"(?<!\\):", line)]


def get_neighbors(iface, rescan):
    fields = "IN-USE,SSID,BSSID,CHAN,FREQ,RATE,BANDWIDTH,SIGNAL,SECURITY,RSN-FLAGS"
    out = sh(["nmcli", "-t", "-f", fields, "dev", "wifi", "list", "ifname", iface,
              "--rescan", "yes" if rescan else "no"], timeout=30)
    aps = []
    for line in out.splitlines():
        p = split_nmcli(line)
        if len(p) < 10:
            continue
        sig = to_num(p[7])
        aps.append({
            "in_use": p[0] == "*", "ssid": p[1] or None, "bssid": p[2].lower(), "channel": to_num(p[3]),
            "freq": to_num(p[4].split()[0]) if p[4] else None, "rate_mbps": to_num(p[5].split()[0]) if p[5] else None,
            "width": to_num(p[6].split()[0]) if p[6] else 20, "signal_pct": sig,
            "signal_dbm_est": round(pct_to_dbm(sig)) if sig is not None else None,
            "security": p[8] or "Aberta", "rsn_flags": p[9],
        })
    return aps


def get_phy(phy):
    txt = sh(["iw", "phy", phy, "info"])
    r = {"freqs": {}}
    m = re.search(r"Available Antennas: TX (0x[0-9a-f]+) RX (0x[0-9a-f]+)", txt)
    if m:
        r["tx_chains"], r["rx_chains"] = bin(int(m.group(1), 16)).count("1"), bin(int(m.group(2), 16)).count("1")
    r["he"] = "HE Iftypes" in txt
    r["eht"] = "EHT Iftypes" in txt
    r["vht"] = "VHT Capabilities" in txt
    for m in re.finditer(r"\* (\d+)(?:\.\d+)? MHz \[(\d+)\](.*)", txt):
        tail = m.group(3)
        p = re.search(r"\(([\d.]+) dBm\)", tail)
        r["freqs"][int(m.group(1))] = {
            "channel": int(m.group(2)), "max_dbm": float(p.group(1)) if p else None,
            "disabled": "disabled" in tail, "radar": "radar" in tail, "no_ir": "no IR" in tail,
        }
    reg = sh(["iw", "reg", "get"])
    codes = re.findall(r"^country (\w+)", reg, re.M)
    real = [c for c in codes if c not in ("00", "99")]
    r["country"] = real[0] if real else (codes[0] if codes else None)
    return r


def lookup_vendor(mac):
    if not mac:
        return None
    if int(mac.split(":")[0], 16) & 0x02:
        return "MAC administrado localmente (aleatório/virtual)"
    prefix = mac.upper().replace(":", "-")[:8]
    for path in ("/usr/share/hwdata/oui.txt", "/usr/share/ieee-data/oui.txt", "/usr/share/misc/oui.txt"):
        try:
            with open(path, errors="ignore") as f:
                for line in f:
                    if line.startswith(prefix):
                        return line.split("(hex)", 1)[-1].strip()
        except OSError:
            continue
    return None


def ping(host, count, iface=None, v6=False):
    target = f"{host}%{iface}" if v6 and host.startswith("fe80") and iface else host
    cmd = ["ping", "-6" if v6 else "-4", "-n", "-c", str(count), "-i", "0.2", "-W", "2", target]
    out = sh(cmd, timeout=count * 0.2 + 10)
    times = [float(t) for t in re.findall(r"time=([\d.]+) ms", out)]
    m = re.search(r"([\d.]+)% packet loss", out)
    loss = float(m.group(1)) if m else 100.0
    r = {"host": host, "sent": count, "received": len(times), "loss_pct": loss}
    if times:
        r.update(min=min(times), avg=statistics.mean(times), max=max(times),
                 stdev=statistics.pstdev(times),
                 jitter=statistics.mean(abs(a - b) for a, b in zip(times, times[1:])) if len(times) > 1 else 0.0,
                 p95=sorted(times)[max(0, math.ceil(0.95 * len(times)) - 1)], series=times)
    return r


def dns_time(server, name="google.com"):
    if shutil.which("dig"):
        out = sh(["dig", f"@{server.split('%')[0]}", name, "+tries=1", "+time=2", "+noall", "+stats"], timeout=5)
        m = re.search(r"Query time: (\d+) msec", out)
        return {"server": server, "ms": int(m.group(1)) if m else None}
    return {"server": server, "ms": None}


def system_dns_time(name="example.org"):
    t0 = time.perf_counter()
    try:
        socket.getaddrinfo(name, 443)
        return (time.perf_counter() - t0) * 1000
    except OSError:
        return None


DISCONNECT_REASONS = {
    1: "não especificado", 2: "autenticação anterior inválida", 3: "deauth: estação saindo",
    4: "inatividade", 5: "AP sobrecarregado", 6: "frame classe 2 de estação não autenticada",
    7: "frame classe 3 de estação não associada", 8: "desassociação: estação saindo",
    14: "falha de MIC", 15: "timeout do 4-way handshake", 16: "timeout do group key handshake",
    23: "falha de autenticação 802.1X", 34: "muitos frames sem ACK (sinal ruim)",
}


def get_journal(iface):
    out = sh(["journalctl", "--since", "-24h", "-t", "wpa_supplicant", "-o", "short-unix", "--no-pager"], timeout=10)
    r = {"connects": 0, "disconnects": 0, "roams": 0, "beacon_loss_events": 0, "reasons": {}, "available": bool(out)}
    last_bssid = None
    for line in out.splitlines():
        if f"{iface}:" not in line:
            continue
        ts = to_num(line.split(" ", 1)[0])
        m = re.search(r"CTRL-EVENT-CONNECTED - Connection to ([0-9a-f:]{17})", line)
        if m:
            r["connects"] += 1
            if last_bssid and last_bssid != m.group(1):
                r["roams"] += 1
            last_bssid = m.group(1)
            r["last_connect_ts"] = ts
        m = re.search(r"CTRL-EVENT-DISCONNECTED .*reason=(\d+)", line)
        if m:
            r["disconnects"] += 1
            code = int(m.group(1))
            label = f"{code} – {DISCONNECT_REASONS.get(code, 'código IEEE 802.11')}"
            r["reasons"][label] = r["reasons"].get(label, 0) + 1
            r["last_disconnect_ts"] = ts
        if "CTRL-EVENT-BEACON-LOSS" in line:
            r["beacon_loss_events"] += 1
        m = re.search(r"\[PTK=(\S+) GTK=(\S+)\]", line)
        if m:
            r["ptk"], r["gtk"] = m.group(1), m.group(2)
    return r


def snapshot(iface):
    st = parse_kv(sh(["iw", "dev", iface, "station", "dump"]))
    sv = get_survey(iface)
    fs = get_sysfs(iface)
    g = lambda k: lead_num(st.get(k))
    return {"t": time.monotonic(), "rx_bytes": fs.get("rx_bytes"), "tx_bytes": fs.get("tx_bytes"),
            "rx_packets": fs.get("rx_packets"), "tx_packets": g("tx packets"), "tx_retries": g("tx retries"),
            "tx_failed": g("tx failed"), "beacon_rx": g("beacon rx"), "ch_active": sv.get("active"), "ch_busy": sv.get("busy")}


def delta(a, b, key):
    if a.get(key) is None or b.get(key) is None:
        return None
    return b[key] - a[key]


# ───────────────────────────── tabelas de referência ─────────────────────────────

# Taxa máxima por fluxo espacial (Mbit/s) no maior MCS e menor GI de cada padrão.
MAX_RATE_PER_SS = {
    "LEGACY": {20: 54},
    "HT": {20: 72.2, 40: 150},
    "VHT": {20: 86.7, 40: 200, 80: 433.3, 160: 866.7},
    "HE": {20: 143.4, 40: 286.8, 80: 600.5, 160: 1201},
    "EHT": {20: 172.1, 40: 344.1, 80: 720.6, 160: 1441.2, 320: 2882.4},
}

# SNR mínimo aproximado (dB) para cada MCS.
MCS_MIN_SNR = [5, 8, 11, 14, 17, 20, 23, 25, 29, 32, 35, 38, 41, 44]
MCS_MAX = {"HT": 7, "VHT": 9, "HE": 11, "EHT": 13}


def channel_span(freq, chan, width):
    """Faixa (MHz) ocupada por um AP dado canal primário e largura."""
    width = width or 20
    if freq < 3000:
        return (freq - 30, freq + 30) if width >= 40 else (freq - 11, freq + 11)
    if width <= 20:
        return (freq - 10, freq + 10)
    step = width // 5
    if freq < 5925:
        base = 149 if chan >= 149 else 36
        start = base + ((chan - base) // step) * step
        lo = 5000 + 5 * start - 10
    else:
        start = 1 + ((chan - 1) // step) * step
        lo = 5950 + 5 * start - 10
    return (lo, lo + width)


def span_allowed(span, freqs):
    """Todos os subcanais de 20 MHz da faixa existem e estão habilitados?"""
    if span[1] - span[0] <= 22:
        return True
    subs = range(int(span[0]) + 10, int(span[1]), 20)
    return all(f in freqs and not freqs[f]["disabled"] for f in subs)


def overlaps(a, b):
    return a[0] < b[1] and b[0] < a[1]


def power_sum_dbm(dbms):
    if not dbms:
        return None
    return 10 * math.log10(sum(10 ** (d / 10) for d in dbms))


# ───────────────────────────── testes de internet ─────────────────────────────

def internet_test(seconds):
    import urllib.request
    ua = {"User-Agent": f"ekfi/{VERSION}"}
    base = "https://speed.cloudflare.com"
    r = {}
    try:
        t0 = time.perf_counter()
        body = urllib.request.urlopen(urllib.request.Request(f"{base}/cdn-cgi/trace", headers=ua), timeout=10).read()
        r["trace_ms"] = (time.perf_counter() - t0) * 1000
        trace = dict(l.split("=", 1) for l in body.decode().splitlines() if "=" in l)
        r.update(public_ip=trace.get("ip"), colo=trace.get("colo"), country=trace.get("loc"),
                 http=trace.get("http"), tls=trace.get("tls"))
        lat = []
        for _ in range(5):
            t0 = time.perf_counter()
            urllib.request.urlopen(urllib.request.Request(f"{base}/__down?bytes=0", headers=ua), timeout=10).read()
            lat.append((time.perf_counter() - t0) * 1000)
        r["http_latency_ms"] = min(lat)

        # O endpoint recusa pedidos grandes, então baixa blocos de 25 MB até acabar o tempo.
        got, t0 = 0, time.perf_counter()
        while time.perf_counter() - t0 < seconds:
            resp = urllib.request.urlopen(urllib.request.Request(f"{base}/__down?bytes=25000000", headers=ua), timeout=15)
            while time.perf_counter() - t0 < seconds:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                got += len(chunk)
            resp.close()
        el = time.perf_counter() - t0
        r["download_bps"], r["download_bytes"] = got * 8 / el, got

        sent = [0]
        t_up = time.perf_counter()
        payload = b"0" * (256 * 1024)

        def gen():
            while time.perf_counter() - t_up < seconds and sent[0] < 200_000_000:
                sent[0] += len(payload)
                yield payload

        req = urllib.request.Request(f"{base}/__up", data=gen(), method="POST",
                                     headers={**ua, "Content-Type": "application/octet-stream"})
        urllib.request.urlopen(req, timeout=seconds + 15).read()
        el = time.perf_counter() - t_up
        r["upload_bps"], r["upload_bytes"] = sent[0] * 8 / el, sent[0]
    except Exception as e:  # noqa: BLE001 — qualquer falha de rede vira mensagem no relatório
        r["error"] = str(e)
    return r


# ───────────────────────────── coleta principal ─────────────────────────────

def collect(iface, args):
    phy = phy_of(iface)
    snap0 = snapshot(iface)

    # Testes lentos rodam em paralelo enquanto a janela de amostragem corre.
    nm = get_nm_device(iface)
    results, threads = {}, []

    def run(key, fn, *a):
        th = threading.Thread(target=lambda: results.__setitem__(key, fn(*a)), daemon=True)
        th.start()
        threads.append(th)

    if not args.no_ping:
        targets = []
        if nm.get("gateway"):
            targets.append(("Gateway", nm["gateway"], False))
        if nm.get("gateway6"):
            targets.append(("Gateway IPv6", nm["gateway6"], True))
        for t in args.targets:
            targets.append((t, t, ":" in t))
        for name, host, v6 in targets:
            run(("ping", name), ping, host, args.ping_count, iface, v6)
        for srv in nm["dns"]:
            run(("dns", srv), dns_time, srv)
        run("sysdns", system_dns_time)
    run("neighbors", get_neighbors, iface, args.rescan)
    run("journal", get_journal, iface)
    if args.speedtest:
        run("internet", internet_test, args.speedtest_seconds)

    st = get_station(iface)
    info = get_iw_info(iface)
    survey = get_survey(iface)
    eth, drv = get_ethtool(iface)
    sysfs = get_sysfs(iface)
    phyinfo = get_phy(phy)
    pci = get_pci_name(iface)

    min_window = args.window
    t_start = snap0["t"]
    for th in threads:
        th.join()
    remaining = min_window - (time.monotonic() - t_start)
    if remaining > 0:
        time.sleep(remaining)
    snap1 = snapshot(iface)
    window = snap1["t"] - snap0["t"]

    g = lambda k: lead_num(st.get(k))
    freq = info.get("freq") or st.get("_freq")
    rx = parse_bitrate(st.get("rx bitrate"), freq)
    tx = parse_bitrate(st.get("tx bitrate"), freq)
    link = rx or tx or {}

    d = {"timestamp": datetime.now().isoformat(timespec="seconds"), "iface": iface, "phy": phy}

    # Conexão
    neighbors = results.get("neighbors", [])
    me = next((a for a in neighbors if a["in_use"]), None) or next(
        (a for a in neighbors if a["bssid"] == (st.get("_bssid") or "")), None)
    journal = results.get("journal", {})
    d["conexao"] = {
        "ssid": st.get("_ssid"), "bssid": st.get("_bssid"), "fabricante_ap": lookup_vendor(st.get("_bssid")),
        "estado": "autorizado" if st.get("authorized") == "yes" else st.get("authorized"),
        "conectividade_nm": nm.get("connectivity"),
        "tempo_conectado_s": g("connected time"),
        "seguranca": me["security"] if me else None, "rsn_flags": me["rsn_flags"] if me else None,
        "key_mgmt": nm.get("key_mgmt"), "cifra_ptk": journal.get("ptk"), "cifra_gtk": journal.get("gtk"),
        "pmf_mfp": st.get("mfp"), "wmm": st.get("wmm/wme"), "tdls": st.get("tdls peer"),
        "preamble": st.get("preamble"), "short_slot": st.get("short slot time"),
        "dtim": g("dtim period"), "beacon_interval_tu": g("beacon interval"),
        "power_save": (sh(["iw", "dev", iface, "get", "power_save"]).split(":")[-1].strip() or None),
    }

    # Rádio
    pf = phyinfo["freqs"].get(freq, {})
    d["radio"] = {
        "padrao": link.get("standard"), "banda": freq_band(freq), "canal": info.get("channel"), "freq_mhz": freq,
        "largura_mhz": info.get("width"), "centro_mhz": info.get("center1"), "dfs": pf.get("radar"),
        "pais_regulatorio": phyinfo.get("country"), "tx_power_dbm": info.get("txpower"),
        "tx_power_max_reg_dbm": pf.get("max_dbm"),
        "cadeias_tx": phyinfo.get("tx_chains"), "cadeias_rx": phyinfo.get("rx_chains"),
        "suporte": ", ".join(n for n, ok in (("Wi-Fi 7", phyinfo.get("eht")), ("Wi-Fi 6", phyinfo.get("he")),
                                             ("Wi-Fi 5", phyinfo.get("vht"))) if ok) or "Wi-Fi 4 ou anterior",
        "driver": drv.get("driver"), "firmware": drv.get("firmware-version"), "chipset": pci,
    }

    # Sinal
    rssi, chains = parse_dbm_chains(st.get("signal"))
    rssi_avg, chains_avg = parse_dbm_chains(st.get("signal avg"))
    beacon_avg, _ = parse_dbm_chains(st.get("beacon signal avg"))
    noise = survey.get("noise")
    if noise is None and eth.get("noise") is not None:
        noise = eth["noise"] - 256 if eth["noise"] > 127 else eth["noise"]
    snr = rssi - noise if rssi is not None and noise is not None else None
    d["sinal"] = {
        "rssi_dbm": rssi, "rssi_medio_dbm": rssi_avg, "rssi_beacon_medio_dbm": beacon_avg,
        "rssi_por_cadeia_dbm": chains, "rssi_medio_por_cadeia_dbm": chains_avg,
        "desbalanceamento_cadeias_db": (max(chains) - min(chains)) if len(chains) > 1 else None,
        "ruido_dbm": noise, "snr_db": snr,
        "qualidade_pct": round(lin_score(rssi, -40, -100)) if rssi is not None else None,
        "sinal_nm_pct": me["signal_pct"] if me else None,
    }

    # Taxas / eficiência
    fam = link.get("family")
    width = link.get("width") or info.get("width") or 20
    nss_cap = min(filter(None, [phyinfo.get("tx_chains"), phyinfo.get("rx_chains")]), default=1)
    per_ss = MAX_RATE_PER_SS.get(fam or "", {}).get(width) if fam else None
    max_phy = per_ss * nss_cap if per_ss else None
    expected_mcs = None
    if snr is not None and fam in MCS_MAX:
        expected_mcs = max([i for i, need in enumerate(MCS_MIN_SNR[:MCS_MAX[fam] + 1]) if need <= snr], default=None)
    mcs_now = link.get("mcs")
    d["taxas"] = {
        "rx_mbps": rx["mbps"] if rx else None, "rx_detalhe": rx["raw"] if rx else None,
        "tx_mbps": tx["mbps"] if tx else None, "tx_detalhe": tx["raw"] if tx else None,
        "mcs": mcs_now, "nss": link.get("nss"), "gi_us": link.get("gi_us"),
        "phy_max_teorico_mbps": max_phy, "fluxos_espaciais_cliente": nss_cap,
        "eficiencia_link_pct": pct(link.get("mbps"), max_phy),
        "throughput_util_estimado_mbps": round(link["mbps"] * 0.65, 1) if link.get("mbps") else None,
        "snr_min_mcs_atual_db": MCS_MIN_SNR[mcs_now] if mcs_now is not None and mcs_now < len(MCS_MIN_SNR) else None,
        "margem_snr_db": snr - MCS_MIN_SNR[mcs_now] if snr is not None and mcs_now is not None and mcs_now < len(MCS_MIN_SNR) else None,
        "mcs_esperado_pelo_snr": expected_mcs,
    }

    # Utilização do canal
    a_d, b_d = delta(snap0, snap1, "ch_active"), delta(snap0, snap1, "ch_busy")
    if a_d and a_d > 0 and b_d is not None:
        active, busy, src = a_d, b_d, "janela atual"
    else:
        active, busy, src = survey.get("active"), survey.get("busy"), "último scan"
    d["canal"] = {
        "tempo_ativo_ms": active, "tempo_ocupado_ms": busy, "utilizacao_pct": pct(busy, active),
        "tempo_rx_ms": survey.get("rx"), "tempo_tx_ms": survey.get("tx"), "fonte": src,
    }

    # Tráfego e confiabilidade (desde a associação)
    conn = g("connected time")
    txp, rxp = g("tx packets"), g("rx packets")
    retries = g("tx retries")
    if not retries and eth.get("tx_retries"):
        retries = eth["tx_retries"]
    bi = g("beacon interval")
    expected_beacons = conn * 1000 / (bi * 1.024) if conn and bi else None
    w = lambda k: delta(snap0, snap1, k)
    d["trafego"] = {
        "rx_bytes": g("rx bytes"), "tx_bytes": g("tx bytes"), "rx_pacotes": rxp, "tx_pacotes": txp,
        "tamanho_medio_rx_bytes": (g("rx bytes") / rxp) if rxp else None,
        "tamanho_medio_tx_bytes": (g("tx bytes") / txp) if txp else None,
        "tx_retries": retries, "taxa_retry_pct": pct(retries, txp),
        "tx_falhas": g("tx failed"), "taxa_falha_tx_pct": pct(g("tx failed"), txp),
        "rx_drop_misc": g("rx drop misc"), "taxa_drop_rx_pct": pct(g("rx drop misc"), rxp),
        "rx_duplicados": eth.get("rx_duplicates"), "taxa_duplicados_pct": pct(eth.get("rx_duplicates"), rxp),
        "beacon_loss": g("beacon loss"), "beacons_recebidos": g("beacon rx"),
        "beacons_esperados": round(expected_beacons) if expected_beacons else None,
        "recepcao_beacons_pct": clamp(pct(g("beacon rx"), expected_beacons), 0, 100) if expected_beacons and g("beacon rx") is not None else None,
        "airtime_tx_pct": pct((g("tx duration") or 0) / 1e6, conn) if g("tx duration") else None,
        "airtime_rx_pct": pct((g("rx duration") or 0) / 1e6, conn) if g("rx duration") else None,
        "inatividade_ms": g("inactive time"),
        "janela_s": window,
        "download_atual_bps": w("rx_bytes") * 8 / window if w("rx_bytes") is not None and window else None,
        "upload_atual_bps": w("tx_bytes") * 8 / window if w("tx_bytes") is not None and window else None,
        "pacotes_tx_janela": w("tx_packets"), "retries_janela": w("tx_retries"),
        "taxa_retry_janela_pct": pct(w("tx_retries"), w("tx_packets")),
        "falhas_tx_janela": w("tx_failed"),
    }

    # Contadores do driver e da interface
    drv_keys = ["rx_dropped", "rx_fragments", "tx_filtered", "tx_retry_failed", "tx_handlers_drop",
                "d_phy_error", "d_rx_crc_err", "d_rx_crc_err_drop", "d_no_beacon", "d_rts_bad", "d_rts_good",
                "d_tx_msdu_dropped", "d_tx_ppdu_hw_queued", "d_fw_crash_count", "d_fw_warm_reset_count",
                "d_fw_cold_reset_count", "d_tx_power"]
    d["driver"] = {k: eth[k] for k in drv_keys if eth.get(k) is not None}
    if args.verbose:
        d["driver_completo"] = eth
    d["interface"] = {k: v for k, v in sysfs.items() if k.endswith(("errors", "dropped")) or k in ("collisions", "multicast")}
    d["interface"]["rx_pacotes_total"] = sysfs.get("rx_packets")
    d["interface"]["tx_pacotes_total"] = sysfs.get("tx_packets")

    # IP
    d["ip"] = {
        "mac_cliente": nm.get("mac"), "mac_aleatorio": bool(nm.get("mac") and int(nm["mac"][:2], 16) & 2),
        "ipv4": nm["ipv4"], "gateway": nm.get("gateway"), "ipv6": nm["ipv6"], "gateway6": nm.get("gateway6"),
        "dns": nm["dns"], "mtu": nm.get("mtu"), "dhcp_servidor": nm.get("dhcp_server"),
        "dhcp_lease_s": nm.get("dhcp_lease_s"),
        "dhcp_expira_em_s": (nm["dhcp_expiry"] - time.time()) if nm.get("dhcp_expiry") else None,
    }

    # Latência e DNS
    d["latencia"] = {k[1]: v for k, v in results.items() if isinstance(k, tuple) and k[0] == "ping"}
    d["dns"] = [v for k, v in results.items() if isinstance(k, tuple) and k[0] == "dns"]
    d["dns_sistema_ms"] = results.get("sysdns")

    # Estabilidade
    d["estabilidade"] = journal

    # Vizinhança
    own_span = (info["center1"] - info["width"] / 2, info["center1"] + info["width"] / 2) if info.get("center1") else None
    others = [a for a in neighbors if a["bssid"] != (st.get("_bssid") or "") and a["freq"]]
    band = freq_band(freq)
    same_band = [a for a in others if freq_band(a["freq"]) == band]
    cochannel = [a for a in same_band if a["channel"] == info.get("channel")]
    overlapping = [a for a in same_band if own_span and overlaps(own_span, channel_span(a["freq"], a["channel"], a["width"]))]
    same_ssid = [a for a in others if a["ssid"] and a["ssid"] == st.get("_ssid")]
    my_pct = me["signal_pct"] if me else None
    better = [a for a in same_ssid if my_pct is not None and a["signal_pct"] and a["signal_pct"] >= my_pct + 15]
    by_band = {}
    for a in neighbors:
        by_band[freq_band(a["freq"])] = by_band.get(freq_band(a["freq"]), 0) + 1

    # Recomendação de canal na banda atual
    candidates = []
    if freq:
        seen = set()
        for f, meta in sorted(phyinfo["freqs"].items()):
            if meta["disabled"] or freq_band(f) != band:
                continue
            if band == "2.4 GHz" and meta["channel"] not in (1, 6, 11):
                continue
            sp = channel_span(f, meta["channel"], info.get("width") or 20)
            if sp in seen or not span_allowed(sp, phyinfo["freqs"]):
                continue
            seen.add(sp)
            inter = [a["signal_dbm_est"] for a in others if a["signal_dbm_est"] is not None
                     and overlaps(sp, channel_span(a["freq"], a["channel"], a["width"]))]
            candidates.append({"canal": meta["channel"], "freq": f, "dfs": meta["radar"], "aps": len(inter),
                               "interferencia_dbm": power_sum_dbm(inter), "atual": overlaps(sp, own_span or (0, 0))})
        candidates.sort(key=lambda c: (c["interferencia_dbm"] if c["interferencia_dbm"] is not None else -999, c["dfs"]))
    current_cand = next((c for c in candidates if c["atual"]), None)

    d["vizinhanca"] = {
        "fonte": "scan novo" if args.rescan else "cache do NetworkManager",
        "aps_visiveis": len(neighbors), "ssids_unicos": len({a["ssid"] for a in neighbors if a["ssid"]}),
        "ocultos": sum(1 for a in neighbors if not a["ssid"]), "por_banda": by_band,
        "aps_mesma_banda": len(same_band), "co_canal": len(cochannel), "sobrepostos": len(overlapping),
        "interferencia_total_dbm": power_sum_dbm([a["signal_dbm_est"] for a in overlapping]),
        "interferente_mais_forte": max(overlapping, key=lambda a: a["signal_pct"] or 0, default=None),
        "aps_abertos": sum(1 for a in neighbors if a["security"] in ("Aberta", "--", "")),
        "aps_wep_tkip": sum(1 for a in neighbors if "WEP" in a["security"] or "tkip" in a["rsn_flags"]),
        "mesmo_ssid_outros_bssids": same_ssid, "bssids_melhores": better,
        "canais_recomendados": candidates[:3], "canal_atual_avaliacao": current_cand,
        "lista": sorted(neighbors, key=lambda a: -(a["signal_pct"] or 0)),
    }

    if args.speedtest:
        d["internet"] = results.get("internet", {})

    d["score"] = health_score(d)
    return d


def health_score(d):
    lat = d["latencia"].get("Gateway", {})
    comps = {
        "Sinal (RSSI)": (lin_score(d["sinal"]["rssi_medio_dbm"] or d["sinal"]["rssi_dbm"], -55, -90), 25),
        "SNR": (lin_score(d["sinal"]["snr_db"], 40, 10), 15),
        "Latência gateway": (lin_score(lat.get("avg"), 2, 100), 15),
        "Perda de pacotes": (lin_score(lat.get("loss_pct"), 0, 5) if lat else None, 15),
        "Jitter": (lin_score(lat.get("jitter"), 1, 30), 10),
        "Utilização do canal": (lin_score(d["canal"]["utilizacao_pct"], 10, 80), 10),
        "Eficiência do link": (lin_score(d["taxas"]["eficiencia_link_pct"], 60, 5), 5),
        "Retransmissões": (lin_score(d["trafego"]["taxa_retry_pct"], 2, 30) if d["trafego"]["taxa_retry_pct"] else None, 5),
    }
    used = {k: v for k, v in comps.items() if v[0] is not None}
    total_w = sum(w for _, w in used.values())
    score = sum(s * w for s, w in used.values()) / total_w if total_w else 0
    return {"valor": round(score), "classificacao": grade(score)[0], "componentes": {k: round(v[0]) for k, v in used.items()}}


# ───────────────────────────── logo pixel art ─────────────────────────────

LOGO_GLYPHS = {
    "E": ["#####", "#....", "#....", "####.", "#....", "#....", "#####"],
    "k": ["#....", "#....", "#..#.", "#.#..", "##...", "#.#..", "#..#."],
    "t": [".#...", ".#...", "####.", ".#...", ".#...", ".#..#", "..##."],
    "e": [".....", ".....", ".###.", "#...#", "#####", "#....", ".###."],
    "c": [".....", ".....", ".####", "#....", "#....", "#....", ".####"],
    "h": ["#....", "#....", "#.##.", "##..#", "#...#", "#...#", "#...#"],
}
LOGO_TEXT = "Ektech"
LOGO_SCALE = 2           # cada pixel da fonte vira um bloco 2x2…
LOGO_BOLD = 1            # …engrossado em mais 1 pixel: traço de 3 px
LOGO_GAP = 2             # espaço entre letras
LOGO_SHADOW_D = (1, 2)   # deslocamento da sombra (linhas, colunas)
LOGO_PAD = 2             # margem lateral da caixa preta
LOGO_WHITE = [255, 255, 254, 253, 252, 251, 250, 249]   # branco → cinza claro, de cima para baixo
LOGO_GRAY = 242
LOGO_BLACK = 16


def _logo_pixels():
    main = set()
    x = 0
    block = LOGO_SCALE + LOGO_BOLD
    for ch in LOGO_TEXT:
        glyph = LOGO_GLYPHS[ch]
        for r, line in enumerate(glyph):
            for c, px in enumerate(line):
                if px == "#":
                    main.update((r * LOGO_SCALE + dy, x + c * LOGO_SCALE + dx) for dy in range(block) for dx in range(block))
        x += len(glyph[0]) * LOGO_SCALE + LOGO_BOLD + LOGO_GAP
    sy, sx = LOGO_SHADOW_D
    shadow = {(r + sy, c + sx) for r, c in main} - main
    pts = main | shadow
    rows = max(r for r, _ in pts) + 1
    return main, shadow, max(c for _, c in pts) + 1, rows + rows % 2


LOGO_MAIN, LOGO_SHADOW_PX, LOGO_COLS, LOGO_ROWS = _logo_pixels()


def _logo_color(r, c, reveal):
    if reveal is not None and c >= reveal:
        return LOGO_BLACK
    if (r, c) in LOGO_MAIN:
        return LOGO_WHITE[r * len(LOGO_WHITE) // LOGO_ROWS]
    if (r, c) in LOGO_SHADOW_PX:
        return LOGO_GRAY
    return LOGO_BLACK


def logo_lines(reveal=None):
    """Logo 'Ektech' em pixel art monocromática sobre caixa preta.

    Usa meio-bloco (▀): cada célula do terminal mostra 2 pixels na vertical,
    o que deixa os pixels quadrados. reveal limita as colunas visíveis (animação).
    """
    def run(cells):
        out, prev, buf = "", None, ""
        for code, ch in cells:
            if code != prev and buf:
                out += C.w(prev, buf)
                buf = ""
            prev = code
            buf += ch
        return out + (C.w(prev, buf) if buf else "")

    width = LOGO_COLS + 2 * LOGO_PAD
    blank = run([(f"48;5;{LOGO_BLACK}", " ")] * width)
    lines = [blank]
    for r in range(0, LOGO_ROWS, 2):
        cells = [(f"48;5;{LOGO_BLACK}", " ")] * LOGO_PAD
        for c in range(LOGO_COLS):
            top, bottom = _logo_color(r, c, reveal), _logo_color(r + 1, c, reveal)
            cells.append((f"48;5;{top}", " ") if top == bottom else (f"38;5;{top};48;5;{bottom}", "▀"))
        cells += [(f"48;5;{LOGO_BLACK}", " ")] * LOGO_PAD
        lines.append(run(cells))
    lines.append(blank)
    return lines


# ───────────────────────────── páginas ─────────────────────────────

ANSI_RE = re.compile(r"\033\[([\d;]*)m")
LABEL_W = 30


def visible_len(s):
    return len(ANSI_RE.sub("", s))


class Out:
    """Acumula linhas (com cores ANSI) para imprimir ou desenhar no curses."""

    def __init__(self):
        self.lines = []

    def line(self, s=""):
        self.lines.append(s)

    def section(self, title):
        self.line()
        self.line(C.bold(C.cyan(f"━━ {title} " + "━" * max(0, 64 - len(title)))))

    def row(self, label, value, color=None, hint=None):
        if value is None or value == "" or value == []:
            value, color = "n/d", C.dim
        text = color(value) if color else str(value)
        self.line(f"  {label:<{LABEL_W}} {text}" + (f"  {C.dim(hint)}" if hint else ""))


def f1(v, unit="", nd=1):
    return None if v is None else f"{v:.{nd}f}{unit}"


def meter(label, score, text, color=None, width=30):
    """Barra no estilo htop: Sinal [||||||||      ] -58 dBm"""
    if score is None:
        return f"  {label:<20}[{' ' * width}] {C.dim('n/d')}"
    score = clamp(score, 0, 100)
    filled = int(round(score / 100 * width))
    col = color or rate(score, 60, 40)
    return f"  {label:<20}[{col('|' * filled)}{' ' * (width - filled)}] {col(text)}"


def chart(o, title, values, width, height=5, color=C.cyan, nd=0, unit="", zero=False):
    """Gráfico de área em blocos com o histórico recente."""
    vals = [v for v in values if v is not None]
    if not vals:
        o.line(f"  {C.bold(title)}  {C.dim('aguardando dados…')}")
        return
    series = values[-width:]
    lo, hi = min(vals), max(vals)
    o.line(f"  {C.bold(title)}  {color(f'{vals[-1]:.{nd}f}{unit}')}  {C.dim(f'média {statistics.mean(vals):.{nd}f}{unit}')}")
    base = 0 if zero else lo - (hi - lo or 1) * 0.15
    top = hi if hi > base else base + 1
    for r in range(height):
        row_base = (height - 1 - r) * 8
        s = ""
        for v in series:
            if v is None:
                s += " "
                continue
            cell = (v - base) / (top - base) * height * 8 - row_base
            s += "█" if cell >= 8 else (" ▁▂▃▄▅▆▇"[int(cell)] if cell > 0 else " ")
        axis = f"{hi:.{nd}f}" if r == 0 else f"{lo if not zero else 0:.{nd}f}" if r == height - 1 else ""
        o.line(f"  {axis:>8} │{color(s)}")
    o.line(f"  {'':>8} └{'─' * len(series)}")


def hist_point(d):
    lat = d["latencia"].get("Gateway", {})
    tr = d["trafego"]
    return {
        "rssi": d["sinal"]["rssi_dbm"], "snr": d["sinal"]["snr_db"], "lat": lat.get("avg"),
        "jitter": lat.get("jitter"), "loss": lat.get("loss_pct"), "util": d["canal"]["utilizacao_pct"],
        "dl": tr["download_atual_bps"] / 1e6 if tr["download_atual_bps"] is not None else None,
        "ul": tr["upload_atual_bps"] / 1e6 if tr["upload_atual_bps"] is not None else None,
        "rate": d["taxas"]["rx_mbps"], "score": d["score"]["valor"],
    }


def page_overview(o, d, opts):
    hist = opts.get("hist", [])
    s = d["score"]
    name, color = grade(s["valor"])
    r, g, t, ch = d["radio"], d["sinal"], d["taxas"], d["canal"]
    lat = d["latencia"].get("Gateway", {})
    tr = d["trafego"]
    full = opts.get("width", 80)
    logo = logo_lines()
    logo_w = visible_len(logo[0])
    info = ["", C.bold("Saúde do Wi-Fi"), color(C.bold(f"{s['valor']}/100 · {name}")), bar(s["valor"], 24), "",
            C.bold(d["conexao"]["ssid"] or "?"), C.dim(r["padrao"] or ""),
            C.dim(f"{r['banda']} · canal {r['canal']} · {r['largura_mhz']} MHz")]
    o.line()
    if full >= logo_w + 28:
        for line in side_by_side(["  " + l for l in logo], info, logo_w + 5):
            o.line(line)
    else:
        if full >= logo_w + 2:
            for line in logo:
                o.line("  " + line)
        for line in info[1:]:
            o.line("  " + line)
    o.line()
    o.line(meter("Sinal", lin_score(g["rssi_dbm"], -40, -100), f"{g['rssi_dbm']} dBm" if g["rssi_dbm"] is not None else "",
                 rate(g["rssi_dbm"], -67, -75)))
    o.line(meter("SNR", lin_score(g["snr_db"], 50, 0), f"{g['snr_db']} dB" if g["snr_db"] is not None else "", rate(g["snr_db"], 25, 15)))
    o.line(meter("Taxa do link", t["eficiencia_link_pct"], f"{f1(t['rx_mbps'], ' Mbit/s')} de {f1(t['phy_max_teorico_mbps'], '')}"
                 if t["rx_mbps"] else "", rate(t["eficiencia_link_pct"], 50, 20)))
    util = ch["utilizacao_pct"]
    o.line(meter("Canal livre", 100 - util if util is not None else None, f"{util:.1f} % ocupado" if util is not None else "",
                 rate(util, 30, 60, higher_better=False)))
    o.line(meter("Latência gateway", lin_score(lat.get("avg"), 0, 100), f1(lat.get("avg"), " ms") or "",
                 rate(lat.get("avg"), 10, 50, higher_better=False)))
    o.line(meter("Jitter", lin_score(lat.get("jitter"), 0, 50), f1(lat.get("jitter"), " ms") or "",
                 rate(lat.get("jitter"), 5, 20, higher_better=False)))
    o.line(meter("Entrega de pacotes", lin_score(lat.get("loss_pct"), 0, 10), f1(lat.get("loss_pct"), " % de perda", 0) or "",
                 rate(lat.get("loss_pct"), 0.01, 2, higher_better=False)))
    o.line(f"  {'Tráfego agora':<20}{C.green('↓ ' + (fmt_bps(tr['download_atual_bps']) or 'n/d'))}   "
           f"{C.cyan('↑ ' + (fmt_bps(tr['upload_atual_bps']) or 'n/d'))}")

    o.line(C.dim("  Barras: quanto mais cheia, melhor."))

    full = opts.get("width", 80)
    two_cols = full >= 100
    col_w = full // 2 if two_cols else full
    width = max(10, col_w - 14)
    o.section("Histórico ao vivo")
    pairs = [
        (("Sinal (dBm)", [h["rssi"] for h in hist], dict(color=C.green)),
         ("Latência gateway (ms)", [h["lat"] for h in hist], dict(color=C.yellow, nd=1, zero=True))),
        (("Download (Mbit/s)", [h["dl"] for h in hist], dict(color=C.green, nd=2, zero=True)),
         ("Upload (Mbit/s)", [h["ul"] for h in hist], dict(color=C.cyan, nd=2, zero=True))),
    ]
    for left, right in pairs:
        outs = []
        for title, values, kw in (left, right):
            part = Out()
            chart(part, title, values, width, height=4, **kw)
            outs.append(part.lines)
        if two_cols:
            for line in side_by_side(outs[0], outs[1], col_w):
                o.line(line)
        else:
            for lines in outs:
                o.lines.extend(lines + [""])
        o.line()
    chart(o, "Saúde (0–100)", [h["score"] for h in hist], max(10, full - 14), height=3, color=C.blue)
    page_score(o, d, opts)


def side_by_side(left, right, col_w):
    out = []
    for i in range(max(len(left), len(right))):
        a = left[i] if i < len(left) else ""
        b = right[i] if i < len(right) else ""
        out.append(a + " " * max(0, col_w - visible_len(a)) + b)
    return out


def page_score(o, d, opts):
    o.section("Composição da nota")
    for k, val in d["score"]["componentes"].items():
        o.line(f"  {k:<{LABEL_W}} {bar(val)} {val:>3}")


def page_connection(o, d, opts):
    c = d["conexao"]
    o.section("Conexão")
    o.row("SSID", c["ssid"])
    o.row("BSSID (AP)", c["bssid"], hint=c["fabricante_ap"])
    o.row("Estado", c["estado"])
    o.row("Conectividade (NM)", c["conectividade_nm"], C.green if c["conectividade_nm"] == "full" else C.yellow)
    o.row("Tempo conectado", fmt_dur(c["tempo_conectado_s"]))
    sec = c["seguranca"]
    o.row("Segurança", sec, C.red if sec in (None, "Aberta") or "WEP" in (sec or "") else
          C.green if "WPA3" in (sec or "") else C.yellow if "WPA1" in (sec or "") else C.green, hint=c["key_mgmt"])
    if c["cifra_ptk"]:
        o.row("Cifras PTK / GTK", f"{c['cifra_ptk']} / {c['cifra_gtk']}",
              C.red if "TKIP" in c["cifra_ptk"] + c["cifra_gtk"] else C.green)
    o.row("Proteção de frames (PMF/MFP)", c["pmf_mfp"], C.green if c["pmf_mfp"] == "yes" else C.yellow)
    o.row("WMM / QoS", c["wmm"])
    o.row("Economia de energia", c["power_save"], C.yellow if c["power_save"] == "on" else None,
          hint="ligado pode aumentar latência" if c["power_save"] == "on" else None)
    o.row("DTIM / Intervalo de beacon", f"{c['dtim']} / {c['beacon_interval_tu']} TU" if c["dtim"] else None)
    o.row("Preâmbulo / Short slot", f"{c['preamble']} / {c['short_slot']}" if c["preamble"] else None)

    r = d["radio"]
    o.section("Rádio")
    o.row("Padrão negociado", r["padrao"])
    o.row("Banda / Canal", f"{r['banda']} · canal {r['canal']} ({r['freq_mhz']} MHz)" if r["canal"] else None,
          hint="DFS (radar)" if r["dfs"] else None)
    o.row("Largura de canal", f"{r['largura_mhz']} MHz" if r["largura_mhz"] else None,
          hint=f"centro {r['centro_mhz']} MHz" if r["centro_mhz"] else None)
    o.row("Potência TX", f1(r["tx_power_dbm"], " dBm"),
          hint=f"máx. regulatório {r['tx_power_max_reg_dbm']} dBm ({r['pais_regulatorio']})" if r["tx_power_max_reg_dbm"] else None)
    o.row("Antenas TX/RX", f"{r['cadeias_tx']}x{r['cadeias_rx']}" if r["cadeias_tx"] else None)
    o.row("Capacidade da placa", r["suporte"])
    o.row("Chipset", r["chipset"])
    o.row("Driver / Firmware", f"{r['driver']} / {r['firmware']}" if r["driver"] else None)


def page_signal(o, d, opts):
    hist = opts.get("hist", [])
    g = d["sinal"]
    o.section("Sinal")
    o.row("RSSI atual", f1(g["rssi_dbm"], " dBm", 0), rate(g["rssi_dbm"], -67, -75),
          hint=sparkline([h["rssi"] for h in hist][-40:]) or None)
    o.row("RSSI médio", f1(g["rssi_medio_dbm"], " dBm", 0), rate(g["rssi_medio_dbm"], -67, -75))
    o.row("RSSI médio dos beacons", f1(g["rssi_beacon_medio_dbm"], " dBm", 0), rate(g["rssi_beacon_medio_dbm"], -67, -75))
    if g["rssi_por_cadeia_dbm"]:
        o.row("RSSI por cadeia (antena)", ", ".join(f"{v} dBm" for v in g["rssi_por_cadeia_dbm"]),
              hint=f"desbalanceamento {g['desbalanceamento_cadeias_db']} dB" if g["desbalanceamento_cadeias_db"] is not None else None)
    o.row("Ruído (noise floor)", f1(g["ruido_dbm"], " dBm", 0), rate(g["ruido_dbm"], -90, -85, higher_better=False))
    o.row("SNR", f1(g["snr_db"], " dB", 0), rate(g["snr_db"], 25, 15), hint=sparkline([h["snr"] for h in hist][-40:]) or None)
    o.row("Qualidade do sinal", f"{g['qualidade_pct']}%  {bar(g['qualidade_pct'])}" if g["qualidade_pct"] is not None else None)

    t = d["taxas"]
    o.section("Taxas e modulação")
    o.row("Taxa RX (AP → você)", f1(t["rx_mbps"], " Mbit/s"), hint=t["rx_detalhe"])
    o.row("Taxa TX (você → AP)", f1(t["tx_mbps"], " Mbit/s"), hint=t["tx_detalhe"] or "driver não informa")
    o.row("MCS / Fluxos espaciais", f"MCS {t['mcs']} · {t['nss']} SS" if t["mcs"] is not None else None)
    o.row("Intervalo de guarda", f1(t["gi_us"], " µs"))
    o.row("PHY máximo teórico", f1(t["phy_max_teorico_mbps"], " Mbit/s"),
          hint=f"{t['fluxos_espaciais_cliente']} SS nesta largura" if t["phy_max_teorico_mbps"] else None)
    o.row("Eficiência do link", f1(t["eficiencia_link_pct"], " %"), rate(t["eficiencia_link_pct"], 50, 20))
    o.row("Throughput útil estimado", f1(t["throughput_util_estimado_mbps"], " Mbit/s"), hint="≈65% da taxa PHY")
    o.row("Margem de SNR p/ MCS atual", f1(t["margem_snr_db"], " dB", 0), rate(t["margem_snr_db"], 5, 0),
          hint=f"MCS {t['mcs']} exige ~{t['snr_min_mcs_atual_db']} dB" if t["snr_min_mcs_atual_db"] else None)
    o.row("MCS sustentável pelo SNR", f"MCS {t['mcs_esperado_pelo_snr']}" if t["mcs_esperado_pelo_snr"] is not None else None)

    ch = d["canal"]
    o.section("Utilização do canal")
    o.row("Utilização (ocupado)", f1(ch["utilizacao_pct"], " %"), rate(ch["utilizacao_pct"], 30, 60, higher_better=False),
          hint=f"{ch['tempo_ocupado_ms']}/{ch['tempo_ativo_ms']} ms · {ch['fonte']}" if ch["tempo_ativo_ms"] else None)
    if ch["tempo_rx_ms"] is not None:
        o.row("Tempo RX / TX no canal", f"{ch['tempo_rx_ms']} / {ch['tempo_tx_ms']} ms")


def page_traffic(o, d, opts):
    hist = opts.get("hist", [])
    tr = d["trafego"]
    o.section("Tráfego e confiabilidade")
    win = f"janela de {tr['janela_s']:.1f} s"
    o.row("Download atual", fmt_bps(tr["download_atual_bps"]), hint=sparkline([h["dl"] for h in hist][-40:]) or win)
    o.row("Upload atual", fmt_bps(tr["upload_atual_bps"]), hint=sparkline([h["ul"] for h in hist][-40:]) or win)
    o.row("Recebido (desde associação)", f"{fmt_bytes(tr['rx_bytes'])} · {tr['rx_pacotes']} pacotes" if tr["rx_bytes"] is not None else None)
    o.row("Enviado (desde associação)", f"{fmt_bytes(tr['tx_bytes'])} · {tr['tx_pacotes']} pacotes" if tr["tx_bytes"] is not None else None)
    o.row("Tamanho médio RX / TX", f"{tr['tamanho_medio_rx_bytes']:.0f} / {tr['tamanho_medio_tx_bytes']:.0f} bytes"
          if tr["tamanho_medio_rx_bytes"] and tr["tamanho_medio_tx_bytes"] else None)
    o.row("Retransmissões TX", f"{tr['tx_retries']} ({f1(tr['taxa_retry_pct'], ' %', 2)})" if tr["tx_retries"] is not None else None,
          rate(tr["taxa_retry_pct"], 5, 15, higher_better=False))
    if tr["taxa_retry_janela_pct"] is not None:
        o.row("Retransmissões (janela)", f1(tr["taxa_retry_janela_pct"], " %", 2), rate(tr["taxa_retry_janela_pct"], 5, 15, higher_better=False))
    o.row("Falhas de TX", f"{tr['tx_falhas']} ({f1(tr['taxa_falha_tx_pct'], ' %', 3)})" if tr["tx_falhas"] is not None else None,
          rate(tr["taxa_falha_tx_pct"], 0.1, 1, higher_better=False))
    o.row("Descartes RX", f"{tr['rx_drop_misc']} ({f1(tr['taxa_drop_rx_pct'], ' %', 3)})" if tr["rx_drop_misc"] is not None else None,
          rate(tr["taxa_drop_rx_pct"], 0.1, 1, higher_better=False))
    if tr["rx_duplicados"] is not None:
        o.row("Frames RX duplicados", f"{tr['rx_duplicados']} ({f1(tr['taxa_duplicados_pct'], ' %', 3)})")
    o.row("Beacons perdidos (beacon loss)", tr["beacon_loss"], C.green if tr["beacon_loss"] == 0 else C.yellow)
    o.row("Recepção de beacons", f"{f1(tr['recepcao_beacons_pct'], ' %')} ({tr['beacons_recebidos']}/{tr['beacons_esperados']})"
          if tr["recepcao_beacons_pct"] is not None else None, rate(tr["recepcao_beacons_pct"], 90, 70),
          hint="power save pode pular beacons" if d["conexao"]["power_save"] == "on" else None)
    o.row("Airtime TX", f1(tr["airtime_tx_pct"], " %", 2))
    if tr["airtime_rx_pct"]:
        o.row("Airtime RX", f1(tr["airtime_rx_pct"], " %", 2))
    o.row("Inatividade da estação", f"{tr['inatividade_ms']} ms" if tr["inatividade_ms"] is not None else None)

    if d["driver"]:
        o.section("Contadores do driver")
        for k, v in d["driver"].items():
            bad = k in ("d_rx_crc_err", "d_phy_error", "tx_retry_failed", "d_tx_msdu_dropped", "d_fw_crash_count") and v
            o.row(k, v, C.yellow if bad else None)
        for k, v in sorted(d.get("driver_completo", {}).items()):
            if k not in d["driver"]:
                o.row(k, v, C.dim)

    i = d["interface"]
    o.section("Interface (desde que subiu)")
    o.row("Erros RX / TX", f"{i.get('rx_errors')} / {i.get('tx_errors')}", C.green if not (i.get("rx_errors") or i.get("tx_errors")) else C.yellow)
    o.row("Descartes RX / TX", f"{i.get('rx_dropped')} / {i.get('tx_dropped')}",
          hint=f"{f1(pct(i.get('rx_dropped'), i.get('rx_pacotes_total')), ' %', 2)} dos pacotes RX" if i.get("rx_pacotes_total") else None)


def page_network(o, d, opts):
    ip = d["ip"]
    o.section("Rede IP")
    o.row("MAC do cliente", ip["mac_cliente"], hint="aleatório (privacidade)" if ip["mac_aleatorio"] else None)
    o.row("IPv4", ", ".join(ip["ipv4"]))
    o.row("Gateway IPv4", ip["gateway"])
    o.row("IPv6", ", ".join(ip["ipv6"]))
    o.row("Gateway IPv6", ip["gateway6"])
    o.row("DNS", ", ".join(ip["dns"]))
    o.row("MTU", ip["mtu"])
    o.row("Servidor DHCP", ip["dhcp_servidor"])
    o.row("Lease DHCP", fmt_dur(ip["dhcp_lease_s"]),
          hint=f"expira em {fmt_dur(ip['dhcp_expira_em_s'])}" if ip["dhcp_expira_em_s"] and ip["dhcp_expira_em_s"] > 0 else None)

    o.section("Latência (ICMP)")
    if not d["latencia"]:
        o.row("Ping", None, hint="desativado (--no-ping)")
    for name, p in d["latencia"].items():
        if not p.get("received"):
            o.row(f"{name} ({p['host']})", "sem resposta", C.red)
            continue
        o.row(name, f"{p['avg']:.1f} ms", rate(p["avg"], 10, 50, higher_better=False),
              hint=f"{p['host']}  {sparkline(p.get('series', []))}")
        o.row("  mín / máx / p95", f"{p['min']:.1f} / {p['max']:.1f} / {p['p95']:.1f} ms")
        o.row("  jitter / desvio padrão", f"{p['jitter']:.1f} / {p['stdev']:.1f} ms", rate(p["jitter"], 5, 20, higher_better=False))
        o.row("  perda", f"{p['loss_pct']:.0f} % ({p['received']}/{p['sent']})", rate(p["loss_pct"], 0.01, 2, higher_better=False))
    if d["dns"] or d.get("dns_sistema_ms") is not None:
        o.section("DNS")
        for x in d["dns"]:
            o.row(f"Consulta via {x['server']}", f"{x['ms']} ms" if x["ms"] is not None else "falhou",
                  rate(x["ms"], 30, 100, higher_better=False) if x["ms"] is not None else C.red)
        o.row("Resolvedor do sistema", f1(d.get("dns_sistema_ms"), " ms"), rate(d.get("dns_sistema_ms"), 30, 100, higher_better=False))


NEIGHBOR_SORTS = {
    "sinal": lambda a: -(a["signal_pct"] or 0),
    "canal": lambda a: (a["channel"] or 0, -(a["signal_pct"] or 0)),
    "ssid": lambda a: ((a["ssid"] or "~").lower(), -(a["signal_pct"] or 0)),
}


def page_neighbors(o, d, opts):
    v = d["vizinhanca"]
    o.section(f"Vizinhança RF ({v['fonte']})")
    o.row("APs visíveis / SSIDs únicos", f"{v['aps_visiveis']} / {v['ssids_unicos']}",
          hint=f"{v['ocultos']} ocultos · " + ", ".join(f"{k}: {n}" for k, n in v["por_banda"].items() if k))
    o.row("APs no mesmo canal (co-canal)", v["co_canal"], rate(v["co_canal"], 1, 3, higher_better=False))
    o.row("APs sobrepostos ao seu canal", v["sobrepostos"], rate(v["sobrepostos"], 2, 5, higher_better=False))
    o.row("Interferência somada (≈)", f1(v["interferencia_total_dbm"], " dBm", 0))
    s_int = v["interferente_mais_forte"]
    if s_int:
        o.row("Interferente mais forte", f"{s_int['ssid'] or '<oculto>'} · canal {s_int['channel']} · ≈{s_int['signal_dbm_est']} dBm")
    o.row("Outros APs da mesma rede", len(v["mesmo_ssid_outros_bssids"]) or "0")
    for a in v["bssids_melhores"]:
        o.row("  ! AP melhor disponível", f"{a['bssid']} · canal {a['channel']} · {a['signal_pct']}%", C.yellow)
    o.row("Redes abertas / WEP-TKIP", f"{v['aps_abertos']} / {v['aps_wep_tkip']}")
    cur = v["canal_atual_avaliacao"]
    if v["canais_recomendados"]:
        rec = ", ".join(f"{c['canal']}{' (DFS)' if c['dfs'] else ''} [{c['aps']} APs]" for c in v["canais_recomendados"])
        o.row("Melhores canais nesta banda", rec, hint=f"atual: {cur['aps']} APs sobrepostos" if cur else None)
    if not opts.get("neighbors", True):
        return
    sort = opts.get("sort", "sinal")
    o.section(f"Redes vizinhas · ordenadas por {sort}")
    o.line(C.dim(f"  {'SSID':<24} {'BSSID':<17} {'CH':>4} {'LARG':>5} {'SINAL':>6} {'≈dBm':>5}  {'':<10} SEGURANÇA"))
    for a in sorted(v["lista"], key=NEIGHBOR_SORTS[sort]):
        sig = a["signal_pct"] or 0
        line = (f"{'▶ ' if a['in_use'] else '  '}{(a['ssid'] or '<oculto>')[:24]:<24} {a['bssid']:<17} {a['channel'] or '':>4} {a['width'] or '':>5} "
                f"{str(sig) + '%':>6} {a['signal_dbm_est'] or '':>5}  ")
        meter_s = rate(sig, 60, 35)("■" * round(sig / 10)) + " " * (10 - round(sig / 10))
        sec = a["security"]
        sec_s = C.red(sec) if sec == "Aberta" or "WEP" in sec else sec
        o.line((C.bold(C.green(line)) if a["in_use"] else line) + meter_s + " " + sec_s)


def page_stability(o, d, opts):
    e = d["estabilidade"]
    o.section("Estabilidade (últimas 24 h)")
    if not e.get("available"):
        o.row("Histórico", None, hint="journal do wpa_supplicant indisponível")
        return
    o.row("Conexões", e["connects"])
    o.row("Desconexões", e["disconnects"], rate(e["disconnects"], 0, 3, higher_better=False))
    o.row("Roamings (troca de AP)", e["roams"])
    o.row("Eventos de beacon loss", e["beacon_loss_events"], C.green if not e["beacon_loss_events"] else C.yellow)
    if e.get("last_disconnect_ts"):
        o.row("Última desconexão", datetime.fromtimestamp(e["last_disconnect_ts"]).strftime("%d/%m %H:%M:%S"))
    if e.get("last_connect_ts"):
        o.row("Última conexão", datetime.fromtimestamp(e["last_connect_ts"]).strftime("%d/%m %H:%M:%S"))
    for reason, n in sorted(e["reasons"].items(), key=lambda x: -x[1]):
        o.row("  motivo", f"{n}× {reason}")


def page_internet(o, d, opts):
    n = opts.get("internet") or d.get("internet")
    o.section("Internet (speed.cloudflare.com)")
    if opts.get("internet_running"):
        o.line(f"  {C.yellow(opts.get('spinner', '…'))} Executando teste de velocidade… acompanhe o tráfego ao vivo na Visão geral.")
    if not n:
        if not opts.get("internet_running"):
            o.line(f"  Pressione {C.bold('S')} ou {C.bold('F6')} para medir download, upload e latência HTTP.")
        return
    if n.get("error"):
        o.row("Erro", n["error"], C.red)
    o.row("Executado em", n.get("quando"))
    o.row("IP público", n.get("public_ip"), hint=f"PoP {n.get('colo')} · {n.get('country')}" if n.get("colo") else None)
    o.row("Latência HTTP", f1(n.get("http_latency_ms"), " ms"), rate(n.get("http_latency_ms"), 30, 100, higher_better=False))
    o.row("Download", fmt_bps(n.get("download_bps")), hint=fmt_bytes(n.get("download_bytes")))
    o.row("Upload", fmt_bps(n.get("upload_bps")), hint=fmt_bytes(n.get("upload_bytes")))
    o.row("Protocolo", f"{n.get('http')} · TLS {n.get('tls')}" if n.get("http") else None)


PAGES = [
    ("Visão geral", "Geral", page_overview),
    ("Conexão e rádio", "Conexão", page_connection),
    ("Sinal e taxas", "Sinal", page_signal),
    ("Tráfego", "Tráfego", page_traffic),
    ("Rede e latência", "Rede", page_network),
    ("Vizinhança", "Vizinhos", page_neighbors),
    ("Estabilidade", "Estab.", page_stability),
    ("Internet", "Internet", page_internet),
]


def render_report(d, opts):
    """Relatório completo, impresso de uma vez (modo --once)."""
    o = Out()
    if C.on:
        if opts["width"] >= LOGO_COLS + 2 * LOGO_PAD + 2:
            for line in logo_lines():
                o.line("  " + line)
            o.line()
    s = d["score"]
    name, color = grade(s["valor"])
    o.line(C.bold(f"Ekfi · {d['conexao']['ssid'] or '?'} · {d['iface']} · {d['timestamp'].replace('T', ' ')}"))
    o.line(f"  Saúde geral do Wi-Fi: {color(C.bold(f'{s['valor']}/100 – {name}'))}  {bar(s['valor'], 30)}")
    for _, _, fn in PAGES[1:]:
        if fn is page_internet and "internet" not in d:
            continue
        fn(o, d, opts)
    page_score(o, d, opts)
    o.line()
    return o.lines


# ───────────────────────────── monitor em segundo plano ─────────────────────────────

class Monitor:
    """Coleta métricas em loop numa thread e guarda o histórico."""

    def __init__(self, iface, args):
        self.iface, self.args = iface, args
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.data, self.hist, self.error = None, [], None
        self.interval = args.delay
        self.paused = self.collecting = self.rescan = False
        self.updated_at = None
        self.internet, self.internet_running = None, False

    def loop(self):
        while not self.stop.is_set():
            if self.paused:
                self.stop.wait(0.2)
                continue
            a = argparse.Namespace(**vars(self.args))
            a.rescan, self.rescan = self.rescan, False
            a.window = self.interval
            a.ping_count = clamp(int(self.interval / 0.2), 3, self.args.ping_count)
            a.speedtest = False
            self.collecting = True
            try:
                d = collect(self.iface, a)
            except Exception as e:  # noqa: BLE001 — a TUI continua viva mesmo se uma coleta falhar
                self.error = f"Falha na coleta: {e}"
                self.stop.wait(self.interval)
                continue
            finally:
                self.collecting = False
            with self.lock:
                self.data, self.error = d, None
                self.hist = (self.hist + [hist_point(d)])[-300:]
                self.updated_at = time.monotonic()

    def start_speedtest(self):
        if self.internet_running:
            return False
        self.internet_running = True

        def job():
            r = internet_test(self.args.speedtest_seconds)
            r["quando"] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            self.internet, self.internet_running = r, False

        threading.Thread(target=job, daemon=True).start()
        return True


# ───────────────────────────── interface interativa (curses) ─────────────────────────────

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SITE_URL = "https://distribuidor.ektech.com.br/"


def open_url(url):
    """Abre a URL no navegador padrão sem poluir a tela do curses."""
    for cmd in (["xdg-open", url], ["gio", "open", url]):
        if shutil.which(cmd[0]):
            try:
                subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                return True
            except OSError:
                continue
    return False
FOOTER_KEYS = [("F1", "Ajuda", curses.KEY_F1), ("F2", None, curses.KEY_F2), ("F4", "Site", curses.KEY_F4),
               ("F5", "Rescan", curses.KEY_F5),
               ("F6", "Speedtest", curses.KEY_F6), ("F7", "Int -", curses.KEY_F7), ("F8", "Int +", curses.KEY_F8),
               ("F9", "Ordenar", curses.KEY_F9), ("F10", "Sair", curses.KEY_F10)]
HELP_LINES = [
    ("", "Navegação"),
    ("←/→  Tab", "trocar de aba"),
    ("1 – 8", "ir direto para a aba"),
    ("↑/↓  PgUp/PgDn", "rolar o conteúdo"),
    ("Home / End", "início / fim"),
    ("", "Ações"),
    ("P  F2  Espaço", "pausar / retomar coleta"),
    ("R  F5", "novo scan de redes vizinhas"),
    ("S  F6", "teste de velocidade da internet"),
    ("-  +  F7  F8", "intervalo de atualização"),
    ("O  F9", "ordenar redes vizinhas"),
    ("F4", "abrir distribuidor.ektech.com.br"),
    ("?  F1", "mostrar / ocultar esta ajuda"),
    ("Q  F10", "sair"),
    ("", "Mouse"),
    ("clique", "menu, abas e teclas do rodapé"),
    ("roda", "rolar o conteúdo"),
]


class TUI:
    def __init__(self, scr, mon):
        self.scr, self.mon = scr, mon
        self.page = 0
        self.scroll = [0] * len(PAGES)
        self.help = False
        self.sort_keys = list(NEIGHBOR_SORTS)
        self.sort = 0
        self.msg, self.msg_until = None, 0
        self.clicks = []
        self.view_h = 10
        self.pairs = {}
        curses.curs_set(0)
        scr.keypad(True)
        scr.timeout(200)
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        curses.mouseinterval(0)

    # ── cores e escrita ──
    def pair(self, fg, bg=-1):
        key = (fg, bg)
        if key not in self.pairs:
            n = len(self.pairs) + 1
            if n >= curses.COLOR_PAIRS:
                return 0
            try:
                curses.init_pair(n, fg, bg)
            except curses.error:
                return 0
            self.pairs[key] = curses.color_pair(n)
        return self.pairs[key]

    @staticmethod
    def c256(n):
        if curses.COLORS >= 256:
            return n
        if n < 16:
            return n % 8
        if n >= 232:
            gray = n - 232
            if gray >= 14:
                return curses.COLOR_WHITE
            return 8 if gray >= 4 and curses.COLORS >= 16 else curses.COLOR_BLACK
        r, g, b = (n - 16) // 36, (n - 16) // 6 % 6, (n - 16) % 6
        return (r >= 3) * 1 + (g >= 3) * 2 + (b >= 3) * 4

    def put(self, y, x, s, attr=0, maxw=None):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        s = s[: max(0, min(maxw if maxw is not None else w, w - x))]
        try:
            self.scr.addstr(y, x, s, attr)
        except curses.error:
            pass

    def put_ansi(self, y, x, text, maxw):
        fg, bg, bold, dim, rev = None, None, False, False, False
        col, end = x, x + maxw
        for i, part in enumerate(ANSI_RE.split(text)):
            if i % 2 == 0:
                if part and col < end:
                    color = self.pair(-1 if fg is None else fg, -1 if bg is None else bg) if (fg, bg) != (None, None) else 0
                    attr = color | (curses.A_BOLD if bold else 0) | \
                           (curses.A_DIM if dim else 0) | (curses.A_REVERSE if rev else 0)
                    seg = part[: end - col]
                    self.put(y, col, seg, attr)
                    col += len(seg)
                continue
            codes = [int(c) for c in part.split(";") if c] or [0]
            j = 0
            while j < len(codes):
                c = codes[j]
                if c == 0:
                    fg, bg, bold, dim, rev = None, None, False, False, False
                elif c == 1:
                    bold = True
                elif c == 2:
                    dim = True
                elif c == 7:
                    rev = True
                elif 30 <= c <= 37:
                    fg = c - 30
                elif 90 <= c <= 97:
                    fg = c - 90 + (8 if curses.COLORS >= 16 else 0)
                elif c == 38 and j + 2 < len(codes) and codes[j + 1] == 5:
                    fg = self.c256(codes[j + 2])
                    j += 2
                elif c == 48 and j + 2 < len(codes) and codes[j + 1] == 5:
                    bg = self.c256(codes[j + 2])
                    j += 2
                elif 40 <= c <= 47:
                    bg = c - 40
                elif c == 39:
                    fg = None
                elif c == 49:
                    bg = None
                j += 1

    def flash(self, msg):
        self.msg, self.msg_until = msg, time.monotonic() + 3

    # ── telas ──
    def splash(self):
        t0 = time.monotonic()
        self.scr.timeout(60)
        black = self.pair(curses.COLOR_WHITE, curses.COLOR_BLACK)
        gray = self.pair(self.c256(244), curses.COLOR_BLACK)
        self.scr.bkgd(" ", black)
        try:
            while True:
                el = time.monotonic() - t0
                h, w = self.scr.getmaxyx()
                self.scr.erase()
                ready = self.mon.data is not None
                lines = logo_lines(reveal=int(el / 0.9 * LOGO_COLS) + 1)
                lw = visible_len(lines[0])
                if w >= lw and h >= len(lines) + 5:
                    y = max(0, (h - len(lines) - 5) // 2)
                    for i, line in enumerate(lines):
                        self.put_ansi(y + i, (w - lw) // 2, line, lw)
                    y += len(lines) + 1
                else:
                    y = h // 2 - 2
                    self.put(y, (w - 6) // 2, "EKTECH", black | curses.A_BOLD)
                    y += 2
                sub = f"Ekfi {VERSION} · monitor de Wi-Fi em tempo real"
                self.put(y, max(0, (w - len(sub)) // 2), sub, black | curses.A_BOLD)
                if ready:
                    status, attr = "Pronto! Pressione qualquer tecla", black
                else:
                    status, attr = f"{SPINNER[int(el * 12) % len(SPINNER)]} Coletando métricas de {self.mon.iface}…", gray
                self.put(y + 2, max(0, (w - len(status)) // 2), status, attr)
                self.scr.refresh()
                k = self.scr.getch()
                if k in (ord("q"), curses.KEY_F10):
                    return False
                if (ready and el > 2.5) or (k not in (-1, curses.KEY_RESIZE, curses.KEY_MOUSE) and el > 0.3):
                    return True
        finally:
            self.scr.bkgd(" ", 0)
            self.scr.timeout(200)

    def draw(self):
        scr = self.scr
        scr.erase()
        h, w = scr.getmaxyx()
        self.clicks = []
        if h < 12 or w < 60:
            self.put(0, 0, "Terminal muito pequeno (mín. 60x12).", curses.A_BOLD)
            scr.refresh()
            return
        with self.mon.lock:
            d, hist = self.mon.data, list(self.mon.hist)
        spin = SPINNER[int(time.monotonic() * 10) % len(SPINNER)]
        bar_attr = self.pair(curses.COLOR_BLACK, curses.COLOR_CYAN)

        # Cabeçalho
        self.put(0, 0, " " * w, bar_attr)
        self.put(0, 1, "▚ Ektech", bar_attr | curses.A_BOLD)
        left = f"  Ekfi {VERSION}"
        if d:
            left += f"  │  {d['conexao']['ssid'] or '?'}  │  {d['iface']}  │  {d['radio']['banda'] or ''} canal {d['radio']['canal']}"
        self.put(0, 9, left, bar_attr, w - 10)
        if d:
            s = d["score"]
            state = "❚❚ PAUSADO" if self.mon.paused else "● AO VIVO"
            right = f"Saúde {s['valor']}/100  {state}  {datetime.now().strftime('%H:%M:%S')} "
            self.put(0, max(0, w - len(right)), right, bar_attr | curses.A_BOLD)

        # Menu: lateral em telas largas, abas em telas estreitas
        if w >= 110:
            sw = 26
            y0, x0 = 1, sw + 1
            self.draw_sidebar(sw, h, d, spin)
        else:
            y0, x0 = 2, 0
            self.draw_tabs(w)
        cw, self.view_h = w - x0 - 1, h - y0 - 1

        # Conteúdo
        name, _, fn = PAGES[self.page]
        if d is None:
            lines = ["", f"  {spin} Coletando as primeiras métricas…"]
        else:
            o = Out()
            opts = {"width": cw, "hist": hist, "sort": self.sort_keys[self.sort], "neighbors": True,
                    "internet": self.mon.internet, "internet_running": self.mon.internet_running, "spinner": spin}
            try:
                fn(o, d, opts)
            except Exception as e:  # noqa: BLE001 — mostra o erro sem derrubar a interface
                o.line(C.red(f"  Erro ao montar a página: {e}"))
            lines = o.lines
        max_scroll = max(0, len(lines) - self.view_h)
        self.scroll[self.page] = clamp(self.scroll[self.page], 0, max_scroll)
        off = self.scroll[self.page]
        for i, line in enumerate(lines[off: off + self.view_h]):
            self.put_ansi(y0 + i, x0, line, cw)
        if max_scroll:
            thumb = y0 + int(off / max_scroll * (self.view_h - 1))
            for yy in range(y0, y0 + self.view_h):
                self.put(yy, w - 1, "┃" if yy == thumb else "│", curses.A_BOLD if yy == thumb else curses.A_DIM)

        self.draw_footer(h, w)
        if self.help:
            self.draw_help(h, w)
        scr.refresh()

    def draw_sidebar(self, sw, h, d, spin):
        hl = self.pair(curses.COLOR_BLACK, curses.COLOR_GREEN) | curses.A_BOLD
        self.put(2, 1, "NAVEGAÇÃO", curses.A_BOLD | curses.A_DIM)
        for i, (name, _, _) in enumerate(PAGES):
            y = 3 + i
            label = f" {i + 1}  {name}".ljust(sw - 2)
            self.put(y, 1, label, hl if i == self.page else 0)
            self.clicks.append((y, 1, sw - 1, ord(str(i + 1))))
        y = 4 + len(PAGES)
        self.put(y, 1, "STATUS", curses.A_BOLD | curses.A_DIM)
        mon = self.mon
        age = f"há {time.monotonic() - mon.updated_at:.0f} s" if mon.updated_at else "—"
        rows = [
            ("Interface", mon.iface, 0),
            ("Intervalo", f"{mon.interval:g} s", 0),
            ("Estado", "pausado" if mon.paused else "ao vivo", self.pair(curses.COLOR_YELLOW if mon.paused else curses.COLOR_GREEN)),
            ("Atualizado", age, 0),
            ("Coleta", f"{spin} ativa" if mon.collecting else "ociosa", curses.A_DIM),
            ("Ordenação", self.sort_keys[self.sort], 0),
        ]
        if mon.internet_running:
            rows.append(("Speedtest", f"{spin} rodando", self.pair(curses.COLOR_YELLOW)))
        for i, (k, v, attr) in enumerate(rows):
            self.put(y + 1 + i, 2, f"{k:<11}", curses.A_DIM)
            self.put(y + 1 + i, 13, str(v), attr, sw - 14)
        if d:
            s = d["score"]
            name, _ = grade(s["valor"])
            yy = y + 3 + len(rows)
            self.put(yy, 1, "SAÚDE", curses.A_BOLD | curses.A_DIM)
            self.put_ansi(yy + 1, 2, bar(s["valor"], sw - 9) + f" {s['valor']:>3}", sw - 2)
            self.put_ansi(yy + 2, 2, grade(s["valor"])[1](name), sw - 2)
        for yy in range(1, h - 1):
            self.put(yy, sw, "│", curses.A_DIM)

    def draw_tabs(self, w):
        x = 0
        hl = self.pair(curses.COLOR_BLACK, curses.COLOR_GREEN) | curses.A_BOLD
        for i, (_, short, _) in enumerate(PAGES):
            label = f" {i + 1} {short} "
            if x + len(label) > w:
                break
            self.put(1, x, label, hl if i == self.page else curses.A_DIM)
            self.clicks.append((1, x, x + len(label), ord(str(i + 1))))
            x += len(label) + 1

    def draw_footer(self, h, w):
        y, x = h - 1, 0
        lab = self.pair(curses.COLOR_BLACK, curses.COLOR_CYAN)
        for key, label, code in FOOTER_KEYS:
            if label is None:
                label = "Retomar" if self.mon.paused else "Pausar"
            seg_w = len(key) + len(label) + 1
            if x + seg_w > w:
                break
            self.put(y, x, key, curses.A_BOLD)
            self.put(y, x + len(key), label.ljust(len(label) + 1), lab)
            self.clicks.append((y, x, x + seg_w, code))
            x += seg_w
        msg = self.msg if self.msg and time.monotonic() < self.msg_until else self.mon.error
        if msg and x + 4 < w:
            self.put(y, x + 2, msg, self.pair(curses.COLOR_YELLOW) | curses.A_BOLD, w - x - 3)
        elif x < w - 1:
            self.put(y, x, " " * (w - x - 1), lab)

    def draw_help(self, h, w):
        bw = 56
        bh = len(HELP_LINES) + 4
        y0, x0 = max(0, (h - bh) // 2), max(0, (w - bw) // 2)
        box = self.pair(curses.COLOR_WHITE, curses.COLOR_BLUE)
        for i in range(bh):
            self.put(y0 + i, x0, " " * bw, box)
        self.put(y0, x0, "┌" + "─" * (bw - 2) + "┐", box)
        self.put(y0 + bh - 1, x0, "└" + "─" * (bw - 2) + "┘", box)
        for i in range(1, bh - 1):
            self.put(y0 + i, x0, "│", box)
            self.put(y0 + i, x0 + bw - 1, "│", box)
        self.put(y0, x0 + 3, " Ajuda · Ekfi by Ektech ", box | curses.A_BOLD)
        for i, (k, v) in enumerate(HELP_LINES):
            yy = y0 + 2 + i
            if not k:
                self.put(yy, x0 + 3, v.upper(), box | curses.A_BOLD)
            else:
                self.put(yy, x0 + 5, f"{k:<16}", box | curses.A_BOLD)
                self.put(yy, x0 + 22, v, box, bw - 24)
        self.put(y0 + bh - 1, x0 + bw - 22, " Esc/? para fechar ", box)

    # ── teclado e mouse ──
    def handle(self, k):
        page_count = len(PAGES)
        if k == curses.KEY_MOUSE:
            try:
                _, mx, my, _, b = curses.getmouse()
            except curses.error:
                return True
            if b & curses.BUTTON4_PRESSED:
                self.scroll[self.page] -= 3
            elif b & getattr(curses, "BUTTON5_PRESSED", 0):
                self.scroll[self.page] += 3
            elif b & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
                for y, x0, x1, code in self.clicks:
                    if my == y and x0 <= mx < x1:
                        return self.handle(code)
            return True
        if self.help and k in (27, ord("?"), curses.KEY_F1, ord("h")):
            self.help = False
        elif k in (ord("q"), ord("Q"), curses.KEY_F10):
            return False
        elif k in (ord("?"), ord("h"), curses.KEY_F1):
            self.help = True
        elif k in (curses.KEY_RIGHT, 9, ord("l")):
            self.page = (self.page + 1) % page_count
        elif k in (curses.KEY_LEFT, curses.KEY_BTAB):
            self.page = (self.page - 1) % page_count
        elif ord("1") <= k < ord("1") + page_count:
            self.page = k - ord("1")
        elif k in (curses.KEY_UP, ord("k")):
            self.scroll[self.page] -= 1
        elif k in (curses.KEY_DOWN, ord("j")):
            self.scroll[self.page] += 1
        elif k == curses.KEY_PPAGE:
            self.scroll[self.page] -= self.view_h - 1
        elif k == curses.KEY_NPAGE:
            self.scroll[self.page] += self.view_h - 1
        elif k in (curses.KEY_HOME, ord("g")):
            self.scroll[self.page] = 0
        elif k in (curses.KEY_END, ord("G")):
            self.scroll[self.page] = 10**6
        elif k in (ord("p"), ord("P"), ord(" "), curses.KEY_F2):
            self.mon.paused = not self.mon.paused
            self.flash("Coleta pausada" if self.mon.paused else "Coleta retomada")
        elif k in (ord("r"), ord("R"), curses.KEY_F5):
            self.mon.rescan = True
            self.flash("Novo scan de redes vizinhas na próxima coleta")
        elif k in (ord("s"), ord("S"), curses.KEY_F6):
            started = self.mon.start_speedtest()
            self.page = len(PAGES) - 1
            self.flash("Teste de velocidade iniciado" if started else "Teste de velocidade já em andamento")
        elif k in (ord("-"), curses.KEY_F7):
            self.mon.interval = max(1, self.mon.interval - 1)
            self.flash(f"Intervalo: {self.mon.interval:g} s")
        elif k in (ord("+"), ord("="), curses.KEY_F8):
            self.mon.interval = min(60, self.mon.interval + 1)
            self.flash(f"Intervalo: {self.mon.interval:g} s")
        elif k == curses.KEY_F4:
            ok = open_url(SITE_URL)
            self.flash(f"Abrindo {SITE_URL} no navegador…" if ok else f"Não foi possível abrir o navegador: {SITE_URL}")
        elif k in (ord("o"), ord("O"), curses.KEY_F9):
            self.sort = (self.sort + 1) % len(self.sort_keys)
            self.flash(f"Vizinhos ordenados por {self.sort_keys[self.sort]}")
        return True

    def run(self):
        if not self.splash():
            return
        while True:
            self.draw()
            k = self.scr.getch()
            if k != -1 and not self.handle(k):
                return


def run_tui(iface, args):
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    C.on = True
    mon = Monitor(iface, args)
    threading.Thread(target=mon.loop, daemon=True).start()
    try:
        curses.wrapper(lambda scr: TUI(scr, mon).run())
    except KeyboardInterrupt:
        pass
    finally:
        mon.stop.set()


# ───────────────────────────── main ─────────────────────────────

def main():
    ap = argparse.ArgumentParser(prog="ekfi", description="Ekfi by Ektech — monitor interativo dos KPIs do Wi-Fi conectado.")
    ap.add_argument("-i", "--iface", help="interface Wi-Fi (padrão: detecta automaticamente)")
    ap.add_argument("-d", "--delay", type=float, default=2.0, metavar="SEG", help="intervalo de atualização (padrão 2 s)")
    ap.add_argument("--once", action="store_true", help="imprime o relatório completo uma vez e sai")
    ap.add_argument("-j", "--json", action="store_true", help="imprime os dados em JSON e sai")
    ap.add_argument("-s", "--speedtest", action="store_true", help="inclui teste de velocidade (com --once/--json)")
    ap.add_argument("--speedtest-seconds", type=float, default=8, help="duração de cada sentido do speedtest")
    ap.add_argument("-r", "--rescan", action="store_true", help="força novo scan de redes vizinhas")
    ap.add_argument("-n", "--neighbors", action="store_true", help="lista as redes vizinhas (com --once)")
    ap.add_argument("-c", "--ping-count", type=int, default=20, help="máximo de pings por alvo (padrão 20)")
    ap.add_argument("-t", "--targets", default="1.1.1.1,8.8.8.8", help="alvos extras de ping, separados por vírgula")
    ap.add_argument("--no-ping", action="store_true", help="não executa ping/DNS")
    ap.add_argument("--window", type=float, default=1.0, help="janela mínima de amostragem em segundos (--once)")
    ap.add_argument("-v", "--verbose", action="store_true", help="mostra todos os contadores do driver")
    ap.add_argument("--no-color", action="store_true", help="desativa cores (--once)")
    ap.add_argument("--version", action="version", version=f"ekfi {VERSION}")
    args = ap.parse_args()
    args.targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    args.delay = max(1.0, args.delay)

    if not sys.platform.startswith("linux"):
        sys.exit("ekfi funciona apenas no Linux (usa iw/nmcli/sysfs).")
    if not shutil.which("iw"):
        sys.exit("Comando 'iw' não encontrado. Instale com: sudo dnf install iw")
    iface = args.iface or detect_iface()
    if not iface:
        sys.exit("Nenhuma interface Wi-Fi conectada encontrada.")

    interactive = not (args.once or args.json) and sys.stdin.isatty() and sys.stdout.isatty()
    if interactive:
        run_tui(iface, args)
        return

    if args.no_color or args.json:
        C.on = False
    d = collect(iface, args)
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2, default=str))
        return
    opts = {"width": shutil.get_terminal_size().columns, "neighbors": args.neighbors, "sort": "sinal"}
    print("\n".join(render_report(d, opts)))


if __name__ == "__main__":
    main()
