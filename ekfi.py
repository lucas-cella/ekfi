#!/usr/bin/env python3
"""Ekfi by Ektech — monitor interativo dos KPIs do Wi-Fi conectado (Linux, Windows e macOS).

Usa só a biblioteca padrão do Python. No Linux lê iw, nmcli, ethtool e sysfs;
no Windows, a Native Wi-Fi API (wlanapi.dll), a IP Helper API e o PowerShell;
no macOS, o CoreWLAN (se o pyobjc estiver instalado) ou o system_profiler.
"""

import argparse
import json
import locale
import math
import os
import re
import shutil
import socket
import statistics
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

try:
    import curses
except ImportError:  # Windows sem o pacote windows-curses
    curses = None

VERSION = "2.1.0"
IS_LINUX = sys.platform.startswith("linux")
IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
ENV = dict(os.environ, LC_ALL="C", LANG="C")
U64_MAX = 2**64 - 1


# ───────────────────────────── utilidades ─────────────────────────────

def sh(cmd, timeout=15, encoding=None):
    """Executa um comando e devolve stdout ('' em caso de erro)."""
    if shutil.which(cmd[0]) is None:
        return ""
    flags = 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=ENV, encoding=encoding,
                              errors="replace", creationflags=flags).stdout or ""
    except (subprocess.SubprocessError, OSError):
        return ""


def ttl_cache(seconds):
    """Guarda o resultado por alguns segundos (para comandos lentos ou que mudam pouco)."""
    def deco(fn):
        store, lock = {}, threading.Lock()

        def wrapper(*a):
            now = time.monotonic()
            with lock:
                hit = store.get(a)
                if hit and now - hit[0] < seconds:
                    return hit[1]
            val = fn(*a)
            with lock:
                store[a] = (now, val)
            return val
        return wrapper
    return deco


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


# ───────────────────────────── coletores do Linux ─────────────────────────────

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


@ttl_cache(3600)
def get_pci_name(iface):
    try:
        slot = os.path.basename(os.path.realpath(f"/sys/class/net/{iface}/device"))
    except OSError:
        return None
    line = sh(["lspci", "-s", slot]).strip()
    return line.split(": ", 1)[1] if ": " in line else None


@ttl_cache(10)
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


@ttl_cache(60)
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


@ttl_cache(30)
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
    diff = b[key] - a[key]
    if diff < 0 and a.get("wrap32"):  # contadores de 32 bits do Windows dão a volta em 4 GB
        diff += 2**32
    return diff


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


# ───────────────────────────── utilidades multiplataforma ─────────────────────────────

def dbm_to_pct(dbm):
    """Mesma escala do NetworkManager: −100 dBm = 0 %, −40 dBm = 100 %."""
    if dbm is None:
        return None
    return int(round(clamp(100 - (-40 - dbm) * 100 / 60, 0, 100)))


def chan_to_freq(ch, band=None):
    if ch is None:
        return None
    if band == "6":
        return 5950 + 5 * ch
    if band == "2.4" or (band is None and ch <= 14):
        return 2484 if ch == 14 else 2407 + 5 * ch
    return 5000 + 5 * ch


def freq_to_chan(f):
    if f is None:
        return None
    if f == 2484:
        return 14
    if f < 3000:
        return (f - 2407) // 5
    if f < 5925:
        return (f - 5000) // 5
    return (f - 5950) // 5


def is_dfs(freq):
    return bool(freq and 5250 < freq < 5730)


def default_freqs():
    """Canais padrão, para sistemas que não expõem a lista regulatória."""
    freqs = {}
    entry = lambda ch, radar=False: {"channel": ch, "max_dbm": None, "disabled": False, "radar": radar, "no_ir": False}
    for ch in range(1, 14):
        freqs[chan_to_freq(ch, "2.4")] = entry(ch)
    for ch in (36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128, 132, 136, 140, 144,
               149, 153, 157, 161, 165):
        freqs[chan_to_freq(ch, "5")] = entry(ch, 52 <= ch <= 144)
    for ch in range(1, 234, 4):
        freqs[chan_to_freq(ch, "6")] = entry(ch)
    return freqs


def std_from_phymode(mode, freq=None):
    """'802.11ax' / '802.11a/n/ac' → ('Wi-Fi 6 (802.11ax)', 'HE')."""
    tokens = {t[2:] if t.startswith("11") else t for t in re.split(r"[./\s]+", (mode or "").lower()) if t}
    if "be" in tokens:
        return "Wi-Fi 7 (802.11be)", "EHT"
    if "ax" in tokens:
        return ("Wi-Fi 6E (802.11ax)" if freq and freq >= 5925 else "Wi-Fi 6 (802.11ax)"), "HE"
    if "ac" in tokens:
        return "Wi-Fi 5 (802.11ac)", "VHT"
    if "n" in tokens:
        return "Wi-Fi 4 (802.11n)", "HT"
    if tokens & {"a", "b", "g"}:
        return f"Legado (802.11{'/'.join(sorted(tokens & {'a', 'b', 'g'}))})", "LEGACY"
    return None, None


def simple_rate(mbps, standard, family, width=None, mcs=None, nss=None):
    """Taxa para sistemas que não detalham MCS/NSS como o iw."""
    if not mbps:
        return None
    raw = f"{mbps:g} Mbit/s" + (f" · {standard}" if standard else "") + (f" · MCS {mcs}" if mcs is not None else "")
    return {"raw": raw, "mbps": float(mbps), "standard": standard, "family": family, "mcs": mcs,
            "nss": nss, "width": width, "gi_us": None}


RSN_CIPHERS = {1: "WEP40", 2: "TKIP", 4: "CCMP", 5: "WEP104", 8: "GCMP", 9: "GCMP-256", 10: "CCMP-256"}


def parse_ies(ies, capability=0):
    """Lê os Information Elements de um beacon: largura do canal e segurança."""
    width, rsn, wpa = 20, None, False
    akms, pairwise, group = set(), [], None
    i = 0
    while i + 2 <= len(ies):
        tag, ln = ies[i], ies[i + 1]
        body = ies[i + 2:i + 2 + ln]
        i += 2 + ln
        if tag == 61 and len(body) >= 2 and body[1] & 0x04:                 # HT Operation
            width = max(width, 40)
        elif tag == 192 and len(body) >= 3 and body[0]:                     # VHT Operation
            width = max(width, 160 if body[0] in (2, 3) or (body[2] and abs(body[2] - body[1]) == 8) else 80)
        elif tag == 255 and len(body) >= 7 and body[0] == 36:               # HE Operation (6 GHz)
            params = int.from_bytes(body[1:4], "little")
            off = 7 + (3 if params & (1 << 14) else 0) + (1 if params & (1 << 15) else 0)
            if params & (1 << 17) and len(body) >= off + 2:
                width = max(width, (20, 40, 80, 160)[body[off + 1] & 0x03])
        elif tag == 48 and len(body) >= 8:                                  # RSN
            rsn = True
            group = RSN_CIPHERS.get(body[5], str(body[5]))
            n = int.from_bytes(body[6:8], "little")
            pairwise = [RSN_CIPHERS.get(body[8 + 4 * k + 3], "?") for k in range(n) if len(body) >= 12 + 4 * k]
            j = 8 + 4 * n
            if len(body) >= j + 2:
                m = int.from_bytes(body[j:j + 2], "little")
                akms = {body[j + 2 + 4 * k + 3] for k in range(m) if len(body) >= j + 6 + 4 * k}
        elif tag == 221 and body[:4] == b"\x00\x50\xf2\x01":                # WPA (vendor)
            wpa = True
    parts = ["WPA1"] if wpa else []
    if rsn:
        if akms & {1, 3, 5, 11, 12, 13}:
            parts.append("WPA2-Enterprise" if not akms & {12, 13} else "WPA3-Enterprise")
        if akms & {2, 4, 6}:
            parts.append("WPA2")
        if akms & {8, 9, 24, 25}:
            parts.append("WPA3")
        if 18 in akms:
            parts.append("OWE")
        if not parts:
            parts.append("WPA2")
    security = " ".join(parts) or ("WEP" if capability & 0x10 else "Aberta")
    flags = " ".join([f"pair_{c.lower()}" for c in pairwise] + ([f"group_{group.lower()}"] if group else []))
    return {"width": width, "security": security, "rsn_flags": flags}


def _ping_result(host, count, times):
    r = {"host": host, "sent": count, "received": len(times), "loss_pct": 100.0 * (count - len(times)) / count}
    if times:
        r.update(min=min(times), avg=statistics.mean(times), max=max(times), stdev=statistics.pstdev(times),
                 jitter=statistics.mean(abs(a - b) for a, b in zip(times, times[1:])) if len(times) > 1 else 0.0,
                 p95=sorted(times)[max(0, math.ceil(0.95 * len(times)) - 1)], series=times)
    return r


def _win_icmp(host, count, interval=0.2, timeout_ms=2000):
    """Ping IPv4 pela API IcmpSendEcho do Windows (não precisa de administrador)."""
    import ctypes
    from ctypes import wintypes

    class IP_OPTION_INFORMATION(ctypes.Structure):
        _fields_ = [("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte),
                    ("OptionsSize", ctypes.c_ubyte), ("OptionsData", ctypes.c_void_p)]

    class ICMP_ECHO_REPLY(ctypes.Structure):
        _fields_ = [("Address", ctypes.c_uint32), ("Status", ctypes.c_uint32), ("RoundTripTime", ctypes.c_uint32),
                    ("DataSize", ctypes.c_ushort), ("Reserved", ctypes.c_ushort), ("Data", ctypes.c_void_p),
                    ("Options", IP_OPTION_INFORMATION)]

    iphlp = ctypes.windll.iphlpapi
    iphlp.IcmpCreateFile.restype = wintypes.HANDLE
    iphlp.IcmpSendEcho.argtypes = [wintypes.HANDLE, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_ushort,
                                   ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
    iphlp.IcmpSendEcho.restype = ctypes.c_uint32
    iphlp.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
    addr = struct.unpack("<I", socket.inet_aton(socket.gethostbyname(host)))[0]
    payload = b"ekfi" * 8
    reply = ctypes.create_string_buffer(ctypes.sizeof(ICMP_ECHO_REPLY) + len(payload) + 8)
    handle = iphlp.IcmpCreateFile()
    times = []
    try:
        for n in range(count):
            t0 = time.perf_counter()
            if iphlp.IcmpSendEcho(handle, addr, payload, len(payload), None, reply, len(reply), timeout_ms):
                if ICMP_ECHO_REPLY.from_buffer(reply).Status == 0:
                    times.append((time.perf_counter() - t0) * 1000)
            if n < count - 1:
                time.sleep(max(0.0, interval - (time.perf_counter() - t0)))
    finally:
        iphlp.IcmpCloseHandle(handle)
    return times


def ping(host, count, iface=None, v6=False):
    target = host
    if v6 and host.lower().startswith("fe80") and "%" not in host and iface and not IS_WINDOWS:
        target = f"{host}%{iface}"
    if IS_WINDOWS:
        if not v6:
            try:
                return _ping_result(host, count, _win_icmp(host, count))
            except (OSError, AttributeError, ValueError):
                pass
        out = sh(["ping", "-n", str(count), "-w", "2000", "-6" if v6 else "-4", target], timeout=count * 1.5 + 10)
        # Saída do ping do Windows é traduzida; cada resposta tem exatamente um "=Nms" ou "<1ms".
        times = []
        for line in out.splitlines():
            found = re.findall(r"[=<]\s*(\d+)\s*ms", line)
            if len(found) == 1:
                times.append(float(found[0]))
        return _ping_result(host, count, times)
    if IS_MAC:
        if v6:
            count = min(count, 3)  # o ping6 do macOS só aceita intervalo < 1 s como root
            cmd = ["ping6", "-n", "-c", str(count), target]
        else:
            cmd = ["ping", "-n", "-c", str(count), "-i", "0.2", "-W", "2000", target]
    else:
        cmd = ["ping", "-6" if v6 else "-4", "-n", "-c", str(count), "-i", "0.2", "-W", "2", target]
    out = sh(cmd, timeout=count * 1.2 + 10)
    return _ping_result(host, count, [float(t) for t in re.findall(r"time=([\d.]+) ms", out)])


def dns_time(server, iface=None, name="google.com", timeout=2.0):
    """Mede uma consulta DNS (registro A) direto no servidor, sem depender do dig."""
    host = server
    if host.lower().startswith("fe80") and "%" not in host and iface and not IS_WINDOWS:
        host = f"{server}%{iface}"
    query_id = os.urandom(2)
    packet = (query_id + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
              + b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\x00\x00\x01\x00\x01")
    try:
        family, _, _, _, addr = socket.getaddrinfo(host, 53, type=socket.SOCK_DGRAM)[0]
        with socket.socket(family, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            t0 = time.perf_counter()
            s.sendto(packet, addr)
            while s.recvfrom(1500)[0][:2] != query_id:
                pass
            return {"server": server, "ms": round((time.perf_counter() - t0) * 1000, 1)}
    except OSError:
        return {"server": server, "ms": None}


def open_url(url):
    """Abre a URL no navegador padrão sem poluir a tela do terminal."""
    if IS_WINDOWS:
        try:
            os.startfile(url)
            return True
        except OSError:
            return False
    for cmd in (["open", url],) if IS_MAC else (["xdg-open", url], ["gio", "open", url]):
        if shutil.which(cmd[0]):
            try:
                subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
                return True
            except OSError:
                continue
    return False


# ───────────────────────────── backend: Linux ─────────────────────────────

class LinuxBackend:
    name = "Linux"
    ping_gateway6 = True
    driver_keys = ["rx_dropped", "rx_fragments", "tx_filtered", "tx_retry_failed", "tx_handlers_drop",
                   "d_phy_error", "d_rx_crc_err", "d_rx_crc_err_drop", "d_no_beacon", "d_rts_bad", "d_rts_good",
                   "d_tx_msdu_dropped", "d_tx_ppdu_hw_queued", "d_fw_crash_count", "d_fw_warm_reset_count",
                   "d_fw_cold_reset_count", "d_tx_power"]

    def check(self):
        if not shutil.which("iw"):
            return "Comando 'iw' não encontrado. Instale com: sudo dnf install iw  (ou: sudo apt install iw)"
        return None

    def detect_iface(self):
        return detect_iface()

    def resolve_iface(self, name):
        return name

    def label(self, iface):
        return iface

    def link(self, iface):
        st = get_station(iface)
        info = get_iw_info(iface)
        freq = info.get("freq") or st.get("_freq")
        g = lambda k: lead_num(st.get(k))
        rssi, chains = parse_dbm_chains(st.get("signal"))
        rssi_avg, chains_avg = parse_dbm_chains(st.get("signal avg"))
        beacon_avg, _ = parse_dbm_chains(st.get("beacon signal avg"))
        return {
            "ssid": st.get("_ssid"), "bssid": st.get("_bssid"), "freq": freq, "channel": info.get("channel"),
            "width": info.get("width"), "center1": info.get("center1"), "txpower": info.get("txpower"),
            "rx": parse_bitrate(st.get("rx bitrate"), freq), "tx": parse_bitrate(st.get("tx bitrate"), freq),
            "rssi": rssi, "rssi_avg": rssi_avg, "beacon_rssi": beacon_avg, "chains": chains, "chains_avg": chains_avg,
            "connected_s": g("connected time"),
            "state": "autorizado" if st.get("authorized") == "yes" else st.get("authorized"),
            "mfp": st.get("mfp"), "wmm": st.get("wmm/wme"), "tdls": st.get("tdls peer"), "preamble": st.get("preamble"),
            "short_slot": st.get("short slot time"), "dtim": g("dtim period"), "beacon_int": g("beacon interval"),
            "power_save": sh(["iw", "dev", iface, "get", "power_save"]).split(":")[-1].strip() or None,
            "counters": {
                "rx_bytes": g("rx bytes"), "tx_bytes": g("tx bytes"), "rx_packets": g("rx packets"),
                "tx_packets": g("tx packets"), "tx_retries": g("tx retries"), "tx_failed": g("tx failed"),
                "rx_drop": g("rx drop misc"), "beacon_loss": g("beacon loss"), "beacon_rx": g("beacon rx"),
                "tx_duration_us": g("tx duration"), "rx_duration_us": g("rx duration"), "inactive_ms": g("inactive time"),
            },
            "counters_scope": "desde a associação",
        }

    def survey(self, iface):
        return get_survey(iface)

    def driver(self, iface):
        stats, info = get_ethtool(iface)
        return stats, {"driver": info.get("driver"), "firmware": info.get("firmware-version"), "chipset": get_pci_name(iface)}

    def if_stats(self, iface):
        return get_sysfs(iface)

    def net(self, iface):
        return get_nm_device(iface)

    def neighbors(self, iface, rescan):
        return get_neighbors(iface, rescan)

    def phy(self, iface):
        return get_phy(phy_of(iface))

    def journal(self, iface):
        return get_journal(iface)

    def snapshot(self, iface):
        return snapshot(iface)


# ───────────────────────────── backend: Windows ─────────────────────────────

WIN_LOCATION_HINT = ("o Windows exige acesso à localização: Configurações > Privacidade > Localização > "
                     "permitir que aplicativos da área de trabalho acessem sua localização")


class WindowsBackend:
    """Native Wi-Fi API (wlanapi.dll) + IP Helper + PowerShell — independente do idioma do Windows."""

    name = "Windows"
    ping_gateway6 = False
    driver_keys = None
    PHY_TYPES = {4: "802.11a", 5: "802.11b", 6: "802.11g", 7: "802.11n", 8: "802.11ac", 9: "802.11ad",
                 10: "802.11ax", 11: "802.11be"}
    AUTH = {1: "Aberta", 2: "WEP (chave compartilhada)", 3: "WPA-Enterprise", 4: "WPA-Personal", 5: "WPA-None",
            6: "WPA2-Enterprise", 7: "WPA2-Personal", 8: "WPA3-Enterprise 192", 9: "WPA3-Personal (SAE)",
            10: "OWE (Enhanced Open)", 11: "WPA3-Enterprise"}
    CIPHER = {0: "Nenhuma", 1: "WEP40", 2: "TKIP", 4: "CCMP", 5: "WEP104", 6: "BIP", 8: "GCMP", 9: "GCMP-256",
              10: "CCMP-256", 0x100: "Grupo", 0x101: "WEP"}
    OP_CONNECTION, OP_CHANNEL, OP_STATISTICS, OP_RSSI = 7, 8, 0x10000101, 0x10000102

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct, self.wt = ctypes, wintypes
        c = ctypes
        self.lock = threading.RLock()
        self.guids, self.desc = {}, {}
        self._bss_cache = {}

        class GUID(c.Structure):
            _fields_ = [("Data1", c.c_uint32), ("Data2", c.c_ushort), ("Data3", c.c_ushort), ("Data4", c.c_ubyte * 8)]

        class DOT11_SSID(c.Structure):
            _fields_ = [("uSSIDLength", c.c_uint32), ("ucSSID", c.c_ubyte * 32)]

        class WLAN_INTERFACE_INFO(c.Structure):
            _fields_ = [("InterfaceGuid", GUID), ("strInterfaceDescription", c.c_wchar * 256), ("isState", c.c_uint)]

        class WLAN_INTERFACE_INFO_LIST(c.Structure):
            _fields_ = [("dwNumberOfItems", c.c_uint32), ("dwIndex", c.c_uint32), ("InterfaceInfo", WLAN_INTERFACE_INFO * 1)]

        class WLAN_ASSOCIATION_ATTRIBUTES(c.Structure):
            _fields_ = [("dot11Ssid", DOT11_SSID), ("dot11BssType", c.c_uint), ("dot11Bssid", c.c_ubyte * 6),
                        ("dot11PhyType", c.c_uint), ("uDot11PhyIndex", c.c_uint32), ("wlanSignalQuality", c.c_uint32),
                        ("ulRxRate", c.c_uint32), ("ulTxRate", c.c_uint32)]

        class WLAN_SECURITY_ATTRIBUTES(c.Structure):
            _fields_ = [("bSecurityEnabled", c.c_int), ("bOneXEnabled", c.c_int),
                        ("dot11AuthAlgorithm", c.c_uint), ("dot11CipherAlgorithm", c.c_uint)]

        class WLAN_CONNECTION_ATTRIBUTES(c.Structure):
            _fields_ = [("isState", c.c_uint), ("wlanConnectionMode", c.c_uint), ("strProfileName", c.c_wchar * 256),
                        ("wlanAssociationAttributes", WLAN_ASSOCIATION_ATTRIBUTES),
                        ("wlanSecurityAttributes", WLAN_SECURITY_ATTRIBUTES)]

        class WLAN_RATE_SET(c.Structure):
            _fields_ = [("uRateSetLength", c.c_uint32), ("usRateSet", c.c_ushort * 126)]

        class WLAN_BSS_ENTRY(c.Structure):
            _fields_ = [("dot11Ssid", DOT11_SSID), ("uPhyId", c.c_uint32), ("dot11Bssid", c.c_ubyte * 6),
                        ("dot11BssType", c.c_uint), ("dot11BssPhyType", c.c_uint), ("lRssi", c.c_int32),
                        ("uLinkQuality", c.c_uint32), ("bInRegDomain", c.c_ubyte), ("usBeaconPeriod", c.c_ushort),
                        ("ullTimestamp", c.c_uint64), ("ullHostTimestamp", c.c_uint64),
                        ("usCapabilityInformation", c.c_ushort), ("ulChCenterFrequency", c.c_uint32),
                        ("wlanRateSet", WLAN_RATE_SET), ("ulIeOffset", c.c_uint32), ("ulIeSize", c.c_uint32)]

        class WLAN_BSS_LIST(c.Structure):
            _fields_ = [("dwTotalSize", c.c_uint32), ("dwNumberOfItems", c.c_uint32), ("wlanBssEntries", WLAN_BSS_ENTRY * 1)]

        mac_names = ["tx_frames", "rx_frames", "wep_excluded", "tkip_mic_failures", "tkip_replays", "tkip_icv_errors",
                     "ccmp_replays", "ccmp_decrypt_errors", "wep_undecryptable", "wep_icv_errors", "decrypt_success",
                     "decrypt_failure"]
        phy_names = ["tx_frames", "tx_multicast", "tx_failed", "tx_retries", "tx_multiple_retries",
                     "tx_lifetime_exceeded", "tx_fragments", "rts_success", "rts_failure", "ack_failure", "rx_frames",
                     "rx_multicast", "rx_promiscuous", "rx_lifetime_exceeded", "rx_duplicates", "rx_fragments",
                     "rx_promiscuous_fragments", "fcs_errors"]

        class MAC_STATS(c.Structure):
            _fields_ = [(n, c.c_uint64) for n in mac_names]

        class PHY_STATS(c.Structure):
            _fields_ = [(n, c.c_uint64) for n in phy_names]

        class WLAN_STATISTICS(c.Structure):
            _fields_ = [("four_way_handshake_failures", c.c_uint64), ("tkip_countermeasures", c.c_uint64),
                        ("reserved", c.c_uint64), ("ucast", MAC_STATS), ("mcast", MAC_STATS),
                        ("dwNumberOfPhys", c.c_uint32), ("PhyCounters", PHY_STATS * 1)]

        class MIB_IFROW(c.Structure):
            _fields_ = [("wszName", c.c_wchar * 256), ("dwIndex", c.c_uint32), ("dwType", c.c_uint32), ("dwMtu", c.c_uint32),
                        ("dwSpeed", c.c_uint32), ("dwPhysAddrLen", c.c_uint32), ("bPhysAddr", c.c_ubyte * 8),
                        ("dwAdminStatus", c.c_uint32), ("dwOperStatus", c.c_uint32), ("dwLastChange", c.c_uint32),
                        ("dwInOctets", c.c_uint32), ("dwInUcastPkts", c.c_uint32), ("dwInNUcastPkts", c.c_uint32),
                        ("dwInDiscards", c.c_uint32), ("dwInErrors", c.c_uint32), ("dwInUnknownProtos", c.c_uint32),
                        ("dwOutOctets", c.c_uint32), ("dwOutUcastPkts", c.c_uint32), ("dwOutNUcastPkts", c.c_uint32),
                        ("dwOutDiscards", c.c_uint32), ("dwOutErrors", c.c_uint32), ("dwOutQLen", c.c_uint32),
                        ("dwDescrLen", c.c_uint32), ("bDescr", c.c_ubyte * 256)]

        self.S = argparse.Namespace(
            GUID=GUID, DOT11_SSID=DOT11_SSID, WLAN_INTERFACE_INFO=WLAN_INTERFACE_INFO,
            WLAN_INTERFACE_INFO_LIST=WLAN_INTERFACE_INFO_LIST, WLAN_CONNECTION_ATTRIBUTES=WLAN_CONNECTION_ATTRIBUTES,
            WLAN_BSS_ENTRY=WLAN_BSS_ENTRY, WLAN_BSS_LIST=WLAN_BSS_LIST, PHY_STATS=PHY_STATS,
            WLAN_STATISTICS=WLAN_STATISTICS, MIB_IFROW=MIB_IFROW)
        H, P = wintypes.HANDLE, c.c_void_p
        w = self.wlan = c.windll.wlanapi
        w.WlanOpenHandle.argtypes = [c.c_uint32, P, c.POINTER(c.c_uint32), c.POINTER(H)]
        w.WlanEnumInterfaces.argtypes = [H, P, c.POINTER(c.POINTER(WLAN_INTERFACE_INFO_LIST))]
        w.WlanQueryInterface.argtypes = [H, c.POINTER(GUID), c.c_uint, P, c.POINTER(c.c_uint32), c.POINTER(P), c.POINTER(c.c_uint)]
        w.WlanGetNetworkBssList.argtypes = [H, c.POINTER(GUID), c.POINTER(DOT11_SSID), c.c_uint, c.c_int, P,
                                            c.POINTER(c.POINTER(WLAN_BSS_LIST))]
        w.WlanScan.argtypes = [H, c.POINTER(GUID), P, P, P]
        w.WlanFreeMemory.argtypes = [P]
        self.iphlp = c.windll.iphlpapi
        self.iphlp.GetIfTable.argtypes = [P, c.POINTER(c.c_uint32), c.c_int]
        version, self.h = c.c_uint32(), H()
        rc = w.WlanOpenHandle(2, None, c.byref(version), c.byref(self.h))
        if rc != 0:
            raise OSError(f"WlanOpenHandle falhou (código {rc}): o serviço WLAN AutoConfig está ativo?")

    # ── helpers da API ──
    def _guid_str(self, g):
        b = bytes(g.Data4)
        return f"{{{g.Data1:08X}-{g.Data2:04X}-{g.Data3:04X}-{b[:2].hex().upper()}-{b[2:].hex().upper()}}}"

    def _interfaces(self):
        c = self.ct
        lst = c.POINTER(self.S.WLAN_INTERFACE_INFO_LIST)()
        with self.lock:
            if self.wlan.WlanEnumInterfaces(self.h, None, c.byref(lst)) != 0:
                return []
            try:
                base = c.addressof(lst.contents.InterfaceInfo)
                out = []
                for i in range(lst.contents.dwNumberOfItems):
                    info = self.S.WLAN_INTERFACE_INFO.from_address(base + i * c.sizeof(self.S.WLAN_INTERFACE_INFO))
                    guid = self.S.GUID.from_buffer_copy(info.InterfaceGuid)
                    key = self._guid_str(guid)
                    self.guids[key], self.desc[key] = guid, info.strInterfaceDescription
                    out.append((key, info.strInterfaceDescription, info.isState))
                return out
            finally:
                self.wlan.WlanFreeMemory(lst)

    def _query(self, iface, opcode, ctype=None):
        """Devolve (bytes, código de erro)."""
        c = self.ct
        size, data, vt = c.c_uint32(), c.c_void_p(), c.c_uint()
        with self.lock:
            rc = self.wlan.WlanQueryInterface(self.h, c.byref(self.guids[iface]), opcode, None,
                                              c.byref(size), c.byref(data), c.byref(vt))
            if rc != 0 or not data.value:
                return None, rc
            try:
                raw = c.string_at(data.value, size.value)
            finally:
                self.wlan.WlanFreeMemory(data)
        if ctype is not None:
            if len(raw) < c.sizeof(ctype):
                return None, rc
            return ctype.from_buffer_copy(raw[:c.sizeof(ctype)]), 0
        return raw, 0

    def _bss(self, iface, rescan=False):
        cached = self._bss_cache.get(iface)
        if cached and not rescan and time.monotonic() - cached[0] < 1.5:
            return cached[1], cached[2]
        c = self.ct
        if rescan:
            with self.lock:
                self.wlan.WlanScan(self.h, c.byref(self.guids[iface]), None, None, None)
            time.sleep(4)  # o scan é assíncrono; a lista é atualizada em poucos segundos
        lst = c.POINTER(self.S.WLAN_BSS_LIST)()
        entries = []
        with self.lock:
            rc = self.wlan.WlanGetNetworkBssList(self.h, c.byref(self.guids[iface]), None, 3, 0, None, c.byref(lst))
            if rc == 0:
                try:
                    base = c.addressof(lst.contents.wlanBssEntries)
                    size = c.sizeof(self.S.WLAN_BSS_ENTRY)
                    for i in range(lst.contents.dwNumberOfItems):
                        addr = base + i * size
                        e = self.S.WLAN_BSS_ENTRY.from_address(addr)
                        ies = c.string_at(addr + e.ulIeOffset, e.ulIeSize) if e.ulIeSize else b""
                        freq = e.ulChCenterFrequency // 1000
                        info = parse_ies(ies, e.usCapabilityInformation)
                        entries.append({
                            "ssid": bytes(e.dot11Ssid.ucSSID[:e.dot11Ssid.uSSIDLength]).decode("utf-8", "replace") or None,
                            "bssid": ":".join(f"{b:02x}" for b in e.dot11Bssid), "freq": freq,
                            "channel": freq_to_chan(freq), "rssi": e.lRssi, "beacon_period": e.usBeaconPeriod,
                            "phy": self.PHY_TYPES.get(e.dot11BssPhyType), **info,
                        })
                finally:
                    self.wlan.WlanFreeMemory(lst)
        self._bss_cache[iface] = (time.monotonic(), entries, rc)
        return entries, rc

    def _statistics(self, iface):
        raw, _ = self._query(iface, self.OP_STATISTICS)
        c = self.ct
        if not raw or len(raw) < c.sizeof(self.S.WLAN_STATISTICS):
            return {}
        st = self.S.WLAN_STATISTICS.from_buffer_copy(raw[:c.sizeof(self.S.WLAN_STATISTICS)])
        out = {"four_way_handshake_failures": st.four_way_handshake_failures,
               "decrypt_failure": st.ucast.decrypt_failure, "ccmp_decrypt_errors": st.ucast.ccmp_decrypt_errors}
        off = self.S.WLAN_STATISTICS.PhyCounters.offset
        size = c.sizeof(self.S.PHY_STATS)
        for i in range(st.dwNumberOfPhys):
            chunk = raw[off + i * size: off + (i + 1) * size]
            if len(chunk) < size:
                break
            phy = self.S.PHY_STATS.from_buffer_copy(chunk)
            for name, _ in self.S.PHY_STATS._fields_:
                out[name] = out.get(name, 0) + getattr(phy, name)
        return out

    def _ifrow(self, iface):
        """Linha da tabela de interfaces (bytes, erros, descartes) que corresponde ao adaptador Wi-Fi."""
        c = self.ct
        size = c.c_uint32(0)
        self.iphlp.GetIfTable(None, c.byref(size), 0)
        buf = c.create_string_buffer(size.value)
        if self.iphlp.GetIfTable(buf, c.byref(size), 0) != 0:
            return None
        n = c.c_uint32.from_buffer(buf).value
        row_size = c.sizeof(self.S.MIB_IFROW)
        off = 4  # MIB_IFTABLE: DWORD dwNumEntries seguido das linhas
        desc = (self.desc.get(iface) or "").strip()
        best = None
        for i in range(n):
            row = self.S.MIB_IFROW.from_buffer_copy(buf, off + i * row_size)
            if row.dwType != 71:  # IF_TYPE_IEEE80211
                continue
            name = bytes(row.bDescr[:row.dwDescrLen]).split(b"\0")[0].decode("mbcs" if IS_WINDOWS else "latin-1", "replace").strip()
            score = (name == desc, row.dwOperStatus >= 4, row.dwInOctets)
            if best is None or score > best[0]:
                best = (score, row)
        return best[1] if best else None

    # ── interface pública ──
    def check(self):
        return None

    def detect_iface(self):
        ifaces = self._interfaces()
        connected = [k for k, _, state in ifaces if state == 1]
        return (connected or [k for k, _, _ in ifaces] or [None])[0]

    def resolve_iface(self, name):
        for key, desc, _ in self._interfaces():
            if name.lower() in (key.lower(), desc.lower()) or name.lower() in desc.lower():
                return key
        net_names = {self.net(k).get("name", "").lower(): k for k, _, _ in self._interfaces()}
        return net_names.get(name.lower())

    def label(self, iface):
        return self.net(iface).get("name") or self.desc.get(iface) or iface

    def link(self, iface):
        r = {"counters": {}, "counters_scope": "desde que o adaptador iniciou"}
        ca, rc = self._query(iface, self.OP_CONNECTION, self.S.WLAN_CONNECTION_ATTRIBUTES)
        if ca is None:
            if rc == 5:
                r["ssid_hint"] = WIN_LOCATION_HINT
            r["state"] = "desconectado"
            return r
        aa, sa = ca.wlanAssociationAttributes, ca.wlanSecurityAttributes
        bssid = ":".join(f"{b:02x}" for b in aa.dot11Bssid)
        chan, _ = self._query(iface, self.OP_CHANNEL, self.ct.c_uint32)
        rssi, _ = self._query(iface, self.OP_RSSI, self.ct.c_int32)
        entries, bss_rc = self._bss(iface)
        own = next((e for e in entries if e["bssid"] == bssid), {})
        channel = chan.value if chan is not None else own.get("channel")
        freq = own.get("freq") or chan_to_freq(channel)
        width = own.get("width")
        std, fam = std_from_phymode(self.PHY_TYPES.get(aa.dot11PhyType), freq)
        center = None
        if freq and width:
            lo, hi = channel_span(freq, channel or freq_to_chan(freq), width)
            center = (lo + hi) // 2
        stats = self._statistics(iface)
        ssid = bytes(aa.dot11Ssid.ucSSID[:aa.dot11Ssid.uSSIDLength]).decode("utf-8", "replace")
        r.update({
            "ssid": ssid or None, "bssid": bssid if any(aa.dot11Bssid) else None, "freq": freq, "channel": channel,
            "width": width, "center1": center, "standard": std,
            "rx": simple_rate(aa.ulRxRate / 1000, std, fam, width), "tx": simple_rate(aa.ulTxRate / 1000, std, fam, width),
            "rssi": rssi.value if rssi is not None else own.get("rssi"), "quality": aa.wlanSignalQuality,
            "state": "conectado" if ca.isState == 1 else f"estado {ca.isState}",
            "security": self.AUTH.get(sa.dot11AuthAlgorithm, f"algoritmo {sa.dot11AuthAlgorithm}"),
            "key_mgmt": "802.1X" if sa.bOneXEnabled else ("PSK/SAE" if sa.bSecurityEnabled else None),
            "cipher_pairwise": self.CIPHER.get(sa.dot11CipherAlgorithm, str(sa.dot11CipherAlgorithm)),
            "beacon_int": own.get("beacon_period"), "profile": ca.strProfileName,
        })
        if bss_rc == 5 or not ssid:
            r["ssid_hint"] = WIN_LOCATION_HINT
        r["counters"] = {"tx_packets": stats.get("tx_frames"), "rx_packets": stats.get("rx_frames"),
                         "tx_retries": stats.get("tx_retries"), "tx_failed": stats.get("tx_failed"),
                         "rx_duplicates": stats.get("rx_duplicates")}
        row = self._ifrow(iface)
        if row is not None:
            r["counters"].update(rx_bytes=row.dwInOctets, tx_bytes=row.dwOutOctets)
        return r

    def survey(self, iface):
        return {}

    def driver(self, iface):
        stats = self._statistics(iface)
        keys = {"fcs_errors": "erros_fcs", "ack_failure": "falhas_ack", "tx_multiple_retries": "retries_multiplos",
                "rts_success": "rts_sucesso", "rts_failure": "rts_falha", "tx_lifetime_exceeded": "tx_tempo_excedido",
                "rx_fragments": "rx_fragmentos", "four_way_handshake_failures": "falhas_4way_handshake",
                "decrypt_failure": "falhas_decriptacao", "ccmp_decrypt_errors": "erros_ccmp"}
        net = self.net(iface)
        drv = " ".join(x for x in (net.get("driver"), net.get("driver_version")) if x) or None
        return {v: stats[k] for k, v in keys.items() if k in stats}, {"driver": drv, "firmware": None,
                                                                       "chipset": self.desc.get(iface)}

    def if_stats(self, iface):
        row = self._ifrow(iface)
        if row is None:
            return {}
        return {"rx_errors": row.dwInErrors, "tx_errors": row.dwOutErrors, "rx_dropped": row.dwInDiscards,
                "tx_dropped": row.dwOutDiscards, "rx_packets": row.dwInUcastPkts + row.dwInNUcastPkts,
                "tx_packets": row.dwOutUcastPkts + row.dwOutNUcastPkts}

    @ttl_cache(15)
    def net(self, iface):
        row = self._ifrow(iface)
        d = {"ipv4": [], "ipv6": [], "dns": []}
        if row is None:
            return d
        d["mac"] = ":".join(f"{b:02x}" for b in row.bPhysAddr[:row.dwPhysAddrLen])
        d["mtu"] = row.dwMtu
        script = f"""
$ErrorActionPreference = 'SilentlyContinue'
$i = {row.dwIndex}
$ad = Get-NetAdapter -InterfaceIndex $i
$c = Get-NetIPConfiguration -InterfaceIndex $i
$cfg = Get-CimInstance Win32_NetworkAdapterConfiguration -Filter "InterfaceIndex=$i"
$prof = Get-NetConnectionProfile -InterfaceIndex $i
$o = [ordered]@{{
  name = $ad.Name; driver = $ad.DriverProvider; driver_version = $ad.DriverVersion
  ipv4 = @(Get-NetIPAddress -InterfaceIndex $i -AddressFamily IPv4 | ForEach-Object {{ "$($_.IPAddress)/$($_.PrefixLength)" }})
  ipv6 = @(Get-NetIPAddress -InterfaceIndex $i -AddressFamily IPv6 | ForEach-Object {{ "$($_.IPAddress)/$($_.PrefixLength)" }})
  gateway = @($c.IPv4DefaultGateway | ForEach-Object {{ $_.NextHop }})
  gateway6 = @($c.IPv6DefaultGateway | ForEach-Object {{ $_.NextHop }})
  dns = @($c.DNSServer | ForEach-Object {{ $_.ServerAddresses }})
  dhcp_server = $cfg.DHCPServer
  lease = if ($cfg.DHCPLeaseExpires) {{ ($cfg.DHCPLeaseExpires - $cfg.DHCPLeaseObtained).TotalSeconds }} else {{ $null }}
  expires = if ($cfg.DHCPLeaseExpires) {{ ($cfg.DHCPLeaseExpires - (Get-Date)).TotalSeconds }} else {{ $null }}
  connectivity = [string]$prof.IPv4Connectivity
}}
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$o | ConvertTo-Json -Compress
"""
        import base64
        enc = base64.b64encode(script.encode("utf-16-le")).decode()
        out = sh(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc], timeout=30, encoding="utf-8")
        try:
            ps = json.loads(out[out.index("{"):])
        except ValueError:
            return d
        as_list = lambda v: [x for x in (v if isinstance(v, list) else [v]) if x]
        d.update(name=ps.get("name"), driver=ps.get("driver"), driver_version=ps.get("driver_version"),
                 ipv4=as_list(ps.get("ipv4")), ipv6=as_list(ps.get("ipv6")),
                 dns=[s for s in as_list(ps.get("dns")) if not s.lower().startswith("fec0:")],
                 dhcp_server=ps.get("dhcp_server"), dhcp_lease_s=ps.get("lease"),
                 connectivity={"Internet": "full", "LocalNetwork": "limited", "Subnet": "limited",
                               "NoTraffic": "none"}.get(ps.get("connectivity"), ps.get("connectivity") or None))
        d["gateway"] = (as_list(ps.get("gateway")) or [None])[0]
        d["gateway6"] = (as_list(ps.get("gateway6")) or [None])[0]
        if ps.get("expires") is not None:
            d["dhcp_expiry"] = time.time() + ps["expires"]
        return d

    def neighbors(self, iface, rescan):
        ca, _ = self._query(iface, self.OP_CONNECTION, self.S.WLAN_CONNECTION_ATTRIBUTES)
        own = ":".join(f"{b:02x}" for b in ca.wlanAssociationAttributes.dot11Bssid) if ca else None
        entries, _ = self._bss(iface, rescan)
        return [{"in_use": e["bssid"] == own, "ssid": e["ssid"], "bssid": e["bssid"], "channel": e["channel"],
                 "freq": e["freq"], "rate_mbps": None, "width": e["width"], "signal_pct": dbm_to_pct(e["rssi"]),
                 "signal_dbm_est": e["rssi"], "security": e["security"], "rsn_flags": e["rsn_flags"]} for e in entries]

    def phy(self, iface):
        return {"freqs": default_freqs(), "country": None}

    @ttl_cache(60)
    def journal(self, iface):
        query = ("*[System[(EventID=8001 or EventID=8002 or EventID=8003) and "
                 "TimeCreated[timediff(@SystemTime) <= 86400000]]]")
        out = sh(["wevtutil", "qe", "Microsoft-Windows-WLAN-AutoConfig/Operational", f"/q:{query}", "/f:xml"],
                 timeout=20, encoding="utf-8")
        r = {"connects": 0, "disconnects": 0, "falhas": 0, "roams": None, "beacon_loss_events": None, "reasons": {},
             "available": bool(out.strip()), "hint": "sem acesso ao log de eventos WLAN-AutoConfig"}
        for ev in re.findall(r"<Event .*?</Event>", out, re.S):
            m = re.search(r"<EventID[^>]*>(\d+)</EventID>", ev)
            t = re.search(r"SystemTime=['\"]([^'\"]+)['\"]", ev)
            if not m:
                continue
            ts = _iso_ts(t.group(1)) if t else None
            data = dict(re.findall(r"<Data Name=['\"](\w+)['\"]>(.*?)</Data>", ev, re.S))
            eid = int(m.group(1))
            if eid == 8001:
                r["connects"] += 1
                r["last_connect_ts"] = ts
            elif eid == 8002:
                r["falhas"] += 1
            elif eid == 8003:
                r["disconnects"] += 1
                r["last_disconnect_ts"] = ts
                reason = (data.get("Reason") or data.get("ReasonCode") or "não informado").strip()
                r["reasons"][reason] = r["reasons"].get(reason, 0) + 1
        return r

    def snapshot(self, iface):
        stats = self._statistics(iface)
        row = self._ifrow(iface)
        return {"t": time.monotonic(), "rx_bytes": row.dwInOctets if row else None,
                "tx_bytes": row.dwOutOctets if row else None, "tx_packets": stats.get("tx_frames"),
                "tx_retries": stats.get("tx_retries"), "tx_failed": stats.get("tx_failed"),
                "beacon_rx": None, "ch_active": None, "ch_busy": None, "wrap32": True}


def _iso_ts(s):
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?", s or "")
    if not m:
        return None
    dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.timestamp() + float("0." + (m.group(2) or "0"))


# ───────────────────────────── backend: macOS ─────────────────────────────

MAC_LOCATION_HINT = ("o macOS oculta SSID/BSSID sem permissão: Ajustes do Sistema > Privacidade e Segurança > "
                     "Serviços de Localização > ative para o Terminal")


class MacBackend:
    """CoreWLAN (se o pyobjc estiver instalado) com system_profiler, netstat e ifconfig."""

    name = "macOS"
    ping_gateway6 = True
    driver_keys = None
    CW_PHY = {1: "802.11a", 2: "802.11b", 3: "802.11g", 4: "802.11n", 5: "802.11ac", 6: "802.11ax", 7: "802.11be"}
    CW_WIDTH = {1: 20, 2: 40, 3: 80, 4: 160}
    CW_BAND = {1: "2.4", 2: "5", 3: "6"}
    CW_SEC = {0: "Aberta", 1: "WEP", 2: "WPA1", 3: "WPA1 WPA2", 4: "WPA2", 5: "WPA2", 6: "WEP dinâmico",
              7: "WPA1-Enterprise", 8: "WPA1 WPA2-Enterprise", 9: "WPA2-Enterprise", 10: "WPA2-Enterprise",
              11: "WPA3", 12: "WPA3-Enterprise", 13: "WPA2 WPA3"}
    CW_SEC_CHECK = [(11, "WPA3"), (12, "WPA3-Enterprise"), (4, "WPA2"), (9, "WPA2-Enterprise"), (2, "WPA1"), (1, "WEP")]

    def __init__(self):
        self.cw = None
        self._sp_cache = (0, {})
        try:
            from CoreWLAN import CWWiFiClient  # opcional: pip install pyobjc-framework-CoreWLAN
            self.cw = CWWiFiClient.sharedWiFiClient()
        except Exception:  # noqa: BLE001 — sem pyobjc, usa o system_profiler
            self.cw = None

    def _cw_iface(self, iface):
        if not self.cw:
            return None
        return self.cw.interfaceWithName_(iface) or self.cw.interface()

    def _sp(self, iface, max_age):
        """Dados do system_profiler (lento: 1–5 s), guardados por max_age segundos."""
        ts, data = self._sp_cache
        if time.monotonic() - ts > max_age:
            try:
                root = json.loads(sh(["system_profiler", "SPAirPortDataType", "-json"], timeout=30) or "{}")
                ifaces = root["SPAirPortDataType"][0]["spairport_airport_interfaces"]
                data = next((i for i in ifaces if i.get("_name") == iface), ifaces[0] if ifaces else {})
            except (ValueError, KeyError, IndexError):
                data = {}
            self._sp_cache = (time.monotonic(), data)
        return data

    @staticmethod
    def _sp_channel(s):
        """'149 (5GHz, 80MHz)' → (149, '5', 80)."""
        m = re.match(r"\s*(\d+)\s*(?:\(\s*([\d.]+)\s*GHz\s*,\s*(\d+)\s*MHz)?", str(s or ""))
        if not m:
            return None, None, None
        band = {"2": "2.4", "2.4": "2.4", "5": "5", "6": "6"}.get(m.group(2)) if m.group(2) else None
        return int(m.group(1)), band, int(m.group(3)) if m.group(3) else None

    @staticmethod
    def _sp_signal(s):
        nums = [int(x) for x in re.findall(r"-?\d+", str(s or ""))]
        return (nums + [None, None])[:2]

    @staticmethod
    def _sp_security(s):
        s = str(s or "").replace("spairport_security_mode_", "")
        if not s or s == "none":
            return "Aberta" if s == "none" else None
        return " ".join(w.upper() if w.startswith(("wpa", "wep")) else w.capitalize() for w in s.split("_"))

    def check(self):
        return None

    def detect_iface(self):
        if self.cw:
            i = self.cw.interface()
            if i is not None and i.interfaceName():
                return str(i.interfaceName())
        out = sh(["networksetup", "-listallhardwareports"])
        m = re.search(r"Hardware Port: (?:Wi-Fi|AirPort)\s*\nDevice: (\S+)", out)
        return m.group(1) if m else None

    def resolve_iface(self, name):
        return name

    def label(self, iface):
        return iface

    def link(self, iface):
        r = {"counters_scope": "desde que a interface subiu"}
        i = self._cw_iface(iface)
        if i is not None:
            ch = i.wlanChannel()
            chn = int(ch.channelNumber()) if ch is not None else None
            band = self.CW_BAND.get(int(ch.channelBand())) if ch is not None else None
            width = self.CW_WIDTH.get(int(ch.channelWidth())) if ch is not None else None
            freq = chan_to_freq(chn, band)
            std, fam = std_from_phymode(self.CW_PHY.get(int(i.activePHYMode())), freq)
            mw = i.transmitPower()
            r.update(ssid=str(i.ssid()) if i.ssid() else None, bssid=str(i.bssid()).lower() if i.bssid() else None,
                     channel=chn, freq=freq, width=width, standard=std, rssi=int(i.rssiValue()) or None,
                     noise=int(i.noiseMeasurement()) or None,
                     tx=simple_rate(float(i.transmitRate() or 0), std, fam, width),
                     txpower=round(10 * math.log10(mw), 1) if mw else None,
                     security=self.CW_SEC.get(int(i.security())), state="conectado" if i.ssid() or chn else "desconectado")
        else:
            cur = self._sp(iface, 3).get("spairport_current_network_information") or {}
            chn, band, width = self._sp_channel(cur.get("spairport_network_channel"))
            freq = chan_to_freq(chn, band)
            std, fam = std_from_phymode(cur.get("spairport_network_phymode"), freq)
            rssi, noise = self._sp_signal(cur.get("spairport_signal_noise"))
            ssid = cur.get("_name")
            r.update(ssid=None if not ssid or "redacted" in ssid else ssid, bssid=None, channel=chn, freq=freq,
                     width=width, standard=std, rssi=rssi, noise=noise,
                     tx=simple_rate(cur.get("spairport_network_rate"), std, fam, width, mcs=cur.get("spairport_network_mcs")),
                     security=self._sp_security(cur.get("spairport_security_mode")),
                     state="conectado" if cur else "desconectado")
        if r.get("freq") and r.get("width"):
            lo, hi = channel_span(r["freq"], r["channel"], r["width"])
            r["center1"] = (lo + hi) // 2
        if r.get("state") == "conectado" and not r.get("ssid"):
            r["ssid_hint"] = MAC_LOCATION_HINT
        snap = self._netstat(iface)
        r["counters"] = {k: snap.get(k) for k in ("rx_bytes", "tx_bytes", "rx_packets", "tx_packets")}
        return r

    def _netstat(self, iface):
        out = sh(["netstat", "-I", iface, "-b", "-n"])
        for line in out.splitlines()[1:]:
            parts = line.split()
            if parts and parts[0] == iface and "<Link#" in line and len(parts) >= 7:
                ipk, ierr, ib, opk, oerr, ob, coll = (to_num(x) for x in parts[-7:])
                return {"rx_packets": ipk, "rx_errors": ierr, "rx_bytes": ib, "tx_packets": opk,
                        "tx_errors": oerr, "tx_bytes": ob, "collisions": coll}
        return {}

    def survey(self, iface):
        return {}

    def driver(self, iface):
        sp = self._sp(iface, 300)
        return {}, {"driver": None, "firmware": sp.get("spairport_wireless_firmware_version"),
                    "chipset": sp.get("spairport_wireless_card_type")}

    def if_stats(self, iface):
        s = self._netstat(iface)
        return {k: s[k] for k in ("rx_errors", "tx_errors", "collisions", "rx_packets", "tx_packets") if k in s}

    @ttl_cache(10)
    def net(self, iface):
        d = {"ipv4": [], "ipv6": [], "dns": []}
        out = sh(["ifconfig", iface])
        d["ipv4"] = [f"{a}/{bin(int(m, 16)).count('1')}" for a, m in re.findall(r"inet (\S+) netmask (0x[0-9a-f]+)", out)]
        d["ipv6"] = [f"{a}/{p}" for a, p in re.findall(r"inet6 (\S+) prefixlen (\d+)", out)]
        m = re.search(r"mtu (\d+)", out)
        d["mtu"] = int(m.group(1)) if m else None
        m = re.search(r"ether ([0-9a-f:]{17})", out)
        d["mac"] = m.group(1) if m else None
        for fam, key in (("-inet", "gateway"), ("-inet6", "gateway6")):
            rt = sh(["route", "-n", "get", fam, "default"])
            g, i = re.search(r"gateway: (\S+)", rt), re.search(r"interface: (\S+)", rt)
            if g and (not i or i.group(1) == iface):
                d[key] = g.group(1)
        d["dns"] = list(dict.fromkeys(re.findall(r"nameserver\[\d+\] : (\S+)", sh(["scutil", "--dns"]))))[:4]
        pkt = sh(["ipconfig", "getpacket", iface])
        m = re.search(r"lease_time \(uint32\): (0x[0-9a-f]+)", pkt)
        d["dhcp_lease_s"] = int(m.group(1), 16) if m else None
        m = re.search(r"server_identifier \(ip\): (\S+)", pkt)
        d["dhcp_server"] = m.group(1) if m else None
        return d

    def neighbors(self, iface, rescan):
        own = self.link(iface)
        i = self._cw_iface(iface)
        out = []
        if i is not None:
            nets = None
            if rescan:
                res = i.scanForNetworksWithName_error_(None, None)
                nets = res[0] if isinstance(res, tuple) else res
            nets = nets or i.cachedScanResults() or []
            for n in nets:
                ch = n.wlanChannel()
                chn = int(ch.channelNumber()) if ch is not None else None
                freq = chan_to_freq(chn, self.CW_BAND.get(int(ch.channelBand())) if ch is not None else None)
                sec = [name for code, name in self.CW_SEC_CHECK if n.supportsSecurity_(code)]
                rssi = int(n.rssiValue())
                bssid = str(n.bssid()).lower() if n.bssid() else ""
                ssid = str(n.ssid()) if n.ssid() else None
                in_use = bssid == own.get("bssid") if bssid and own.get("bssid") else \
                    bool(ssid) and ssid == own.get("ssid") and chn == own.get("channel")
                out.append({"in_use": in_use, "ssid": ssid, "bssid": bssid, "channel": chn, "freq": freq,
                            "rate_mbps": None, "width": self.CW_WIDTH.get(int(ch.channelWidth())) if ch is not None else 20,
                            "signal_pct": dbm_to_pct(rssi), "signal_dbm_est": rssi,
                            "security": " ".join(sec) or "Aberta", "rsn_flags": ""})
            return out
        sp = self._sp(iface, 3)
        cur = sp.get("spairport_current_network_information")
        for n, in_use in [(cur, True)] * bool(cur) + [(x, False) for x in sp.get("spairport_airport_other_local_wireless_networks") or []]:
            chn, band, width = self._sp_channel(n.get("spairport_network_channel"))
            rssi, _ = self._sp_signal(n.get("spairport_signal_noise"))
            name = n.get("_name")
            out.append({"in_use": in_use, "ssid": None if not name or "redacted" in name else name, "bssid": "",
                        "channel": chn, "freq": chan_to_freq(chn, band), "rate_mbps": None, "width": width or 20,
                        "signal_pct": dbm_to_pct(rssi), "signal_dbm_est": rssi,
                        "security": self._sp_security(n.get("spairport_security_mode")) or "?", "rsn_flags": ""})
        return out

    def phy(self, iface):
        sp = self._sp(iface, 300)
        modes = str(sp.get("spairport_supported_phymodes") or "").lower()
        i = self._cw_iface(iface)
        country = str(i.countryCode()) if i is not None and i.countryCode() else sp.get("spairport_wireless_country_code")
        return {"freqs": default_freqs(), "country": country,
                "eht": "be" in modes if modes else None, "he": "ax" in modes if modes else None,
                "vht": "ac" in modes if modes else None}

    def journal(self, iface):
        return {"available": False, "hint": "histórico de conexões não disponível no macOS"}

    def snapshot(self, iface):
        s = self._netstat(iface)
        return {"t": time.monotonic(), "rx_bytes": s.get("rx_bytes"), "tx_bytes": s.get("tx_bytes"),
                "tx_packets": s.get("tx_packets"), "tx_retries": None, "tx_failed": None, "beacon_rx": None,
                "ch_active": None, "ch_busy": None}


def make_backend():
    if IS_LINUX:
        return LinuxBackend()
    if IS_WINDOWS:
        return WindowsBackend()
    if IS_MAC:
        return MacBackend()
    raise OSError(f"Sistema não suportado: {sys.platform}")


BACKEND = None


# ───────────────────────────── coleta principal ─────────────────────────────

def collect(iface, args):
    be = BACKEND
    snap0 = be.snapshot(iface)

    # Testes lentos rodam em paralelo enquanto a janela de amostragem corre.
    net = be.net(iface)
    results, threads = {}, []

    def run(key, fn, *a):
        th = threading.Thread(target=lambda: results.__setitem__(key, fn(*a)), daemon=True)
        th.start()
        threads.append(th)

    if not args.no_ping:
        targets = []
        if net.get("gateway"):
            targets.append(("Gateway", net["gateway"], False))
        if net.get("gateway6") and be.ping_gateway6:
            targets.append(("Gateway IPv6", net["gateway6"], True))
        for t in args.targets:
            targets.append((t, t, ":" in t))
        for name, host, v6 in targets:
            run(("ping", name), ping, host, args.ping_count, iface, v6)
        for srv in net["dns"]:
            run(("dns", srv), dns_time, srv, iface)
        run("sysdns", system_dns_time)
    run("neighbors", be.neighbors, iface, args.rescan)
    run("journal", be.journal, iface)
    if args.speedtest:
        run("internet", internet_test, args.speedtest_seconds)

    link = be.link(iface)
    survey = be.survey(iface)
    eth, drv = be.driver(iface)
    sysfs = be.if_stats(iface)
    phyinfo = be.phy(iface)

    t_start = snap0["t"]
    for th in threads:
        th.join()
    remaining = args.window - (time.monotonic() - t_start)
    if remaining > 0:
        time.sleep(remaining)
    snap1 = be.snapshot(iface)
    window = snap1["t"] - snap0["t"]

    freq = link.get("freq")
    rx, tx = link.get("rx"), link.get("tx")
    rate_info = rx or tx or {}
    cnt = link.get("counters") or {}

    d = {"timestamp": datetime.now().isoformat(timespec="seconds"), "iface": be.label(iface), "plataforma": be.name}

    # Conexão
    neighbors = results.get("neighbors", [])
    me = next((a for a in neighbors if a["in_use"]), None) or next(
        (a for a in neighbors if link.get("bssid") and a["bssid"] == link["bssid"]), None)
    journal = results.get("journal", {})
    connected_s = link.get("connected_s")
    if connected_s is None and journal.get("last_connect_ts") and \
            (journal.get("last_disconnect_ts") or 0) <= journal["last_connect_ts"]:
        connected_s = time.time() - journal["last_connect_ts"]
    d["conexao"] = {
        "ssid": link.get("ssid"), "ssid_aviso": link.get("ssid_hint"), "bssid": link.get("bssid"),
        "fabricante_ap": lookup_vendor(link.get("bssid")), "estado": link.get("state"),
        "conectividade": net.get("connectivity"), "tempo_conectado_s": connected_s,
        "seguranca": link.get("security") or (me["security"] if me else None),
        "rsn_flags": me["rsn_flags"] if me else None,
        "key_mgmt": link.get("key_mgmt") or net.get("key_mgmt"),
        "cifra_ptk": link.get("cipher_pairwise") or journal.get("ptk"),
        "cifra_gtk": link.get("cipher_group") or journal.get("gtk"),
        "pmf_mfp": link.get("mfp"), "wmm": link.get("wmm"), "tdls": link.get("tdls"),
        "preamble": link.get("preamble"), "short_slot": link.get("short_slot"),
        "dtim": link.get("dtim"), "beacon_interval_tu": link.get("beacon_int"), "power_save": link.get("power_save"),
    }

    # Rádio
    pf = phyinfo["freqs"].get(freq, {}) if freq else {}
    caps = [n for n, k in (("Wi-Fi 7", "eht"), ("Wi-Fi 6", "he"), ("Wi-Fi 5", "vht")) if phyinfo.get(k)]
    known_caps = any(k in phyinfo for k in ("eht", "he", "vht"))
    d["radio"] = {
        "padrao": rate_info.get("standard") or link.get("standard"), "banda": freq_band(freq),
        "canal": link.get("channel"), "freq_mhz": freq, "largura_mhz": link.get("width"),
        "centro_mhz": link.get("center1"), "dfs": pf.get("radar") if pf else is_dfs(freq),
        "pais_regulatorio": phyinfo.get("country"), "tx_power_dbm": link.get("txpower"),
        "tx_power_max_reg_dbm": pf.get("max_dbm"),
        "cadeias_tx": phyinfo.get("tx_chains"), "cadeias_rx": phyinfo.get("rx_chains"),
        "suporte": ", ".join(caps) or ("Wi-Fi 4 ou anterior" if known_caps else None),
        "driver": drv.get("driver"), "firmware": drv.get("firmware"), "chipset": drv.get("chipset"),
    }

    # Sinal
    rssi, chains = link.get("rssi"), link.get("chains") or []
    noise = survey.get("noise") if survey.get("noise") is not None else link.get("noise")
    if noise is None and eth.get("noise") is not None:
        noise = eth["noise"] - 256 if eth["noise"] > 127 else eth["noise"]
    snr = rssi - noise if rssi is not None and noise is not None else None
    d["sinal"] = {
        "rssi_dbm": rssi, "rssi_medio_dbm": link.get("rssi_avg"), "rssi_beacon_medio_dbm": link.get("beacon_rssi"),
        "rssi_por_cadeia_dbm": chains, "rssi_medio_por_cadeia_dbm": link.get("chains_avg") or [],
        "desbalanceamento_cadeias_db": (max(chains) - min(chains)) if len(chains) > 1 else None,
        "ruido_dbm": noise, "snr_db": snr,
        "qualidade_pct": round(lin_score(rssi, -40, -100)) if rssi is not None else link.get("quality"),
        "sinal_nm_pct": me["signal_pct"] if me else link.get("quality"),
    }

    # Taxas / eficiência
    fam = rate_info.get("family")
    width = rate_info.get("width") or link.get("width") or 20
    mbps = rate_info.get("mbps")
    per_ss = MAX_RATE_PER_SS.get(fam or "", {}).get(width) if fam else None
    chains_known = [c for c in (phyinfo.get("tx_chains"), phyinfo.get("rx_chains")) if c]
    if chains_known:
        nss_cap = min(chains_known)
    else:  # sistema não informa as antenas: deduz o mínimo de fluxos que explica a taxa atual
        nss_cap = max(rate_info.get("nss") or 1, math.ceil(mbps / per_ss - 0.01) if mbps and per_ss else 1)
    max_phy = per_ss * nss_cap if per_ss else None
    nss_estimated = not chains_known
    expected_mcs = None
    if snr is not None and fam in MCS_MAX:
        expected_mcs = max([i for i, need in enumerate(MCS_MIN_SNR[:MCS_MAX[fam] + 1]) if need <= snr], default=None)
    mcs_now = rate_info.get("mcs")
    need = MCS_MIN_SNR[mcs_now] if mcs_now is not None and mcs_now < len(MCS_MIN_SNR) else None
    d["taxas"] = {
        "rx_mbps": rx["mbps"] if rx else None, "rx_detalhe": rx["raw"] if rx else None,
        "tx_mbps": tx["mbps"] if tx else None, "tx_detalhe": tx["raw"] if tx else None,
        "mcs": mcs_now, "nss": rate_info.get("nss"), "gi_us": rate_info.get("gi_us"),
        "phy_max_teorico_mbps": max_phy, "fluxos_espaciais_cliente": nss_cap, "fluxos_estimados": nss_estimated,
        # sem saber as antenas, a "eficiência" seria sempre ~100 %: melhor não mostrar
        "eficiencia_link_pct": None if nss_estimated else pct(mbps, max_phy),
        "throughput_util_estimado_mbps": round(mbps * 0.65, 1) if mbps else None,
        "snr_min_mcs_atual_db": need,
        "margem_snr_db": snr - need if snr is not None and need is not None else None,
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

    # Tráfego e confiabilidade
    txp, rxp = cnt.get("tx_packets"), cnt.get("rx_packets")
    retries = cnt.get("tx_retries")
    if not retries and eth.get("tx_retries"):
        retries = eth["tx_retries"]
    dup = cnt.get("rx_duplicates", eth.get("rx_duplicates"))
    bi = link.get("beacon_int")
    beacon_rx = cnt.get("beacon_rx")
    expected_beacons = connected_s * 1000 / (bi * 1.024) if connected_s and bi and beacon_rx is not None else None
    w = lambda k: delta(snap0, snap1, k)
    tx_dur, rx_dur = cnt.get("tx_duration_us"), cnt.get("rx_duration_us")
    d["trafego"] = {
        "escopo": link.get("counters_scope"),
        "rx_bytes": cnt.get("rx_bytes"), "tx_bytes": cnt.get("tx_bytes"), "rx_pacotes": rxp, "tx_pacotes": txp,
        "tamanho_medio_rx_bytes": (cnt["rx_bytes"] / rxp) if rxp and cnt.get("rx_bytes") is not None else None,
        "tamanho_medio_tx_bytes": (cnt["tx_bytes"] / txp) if txp and cnt.get("tx_bytes") is not None else None,
        "tx_retries": retries, "taxa_retry_pct": pct(retries, txp),
        "tx_falhas": cnt.get("tx_failed"), "taxa_falha_tx_pct": pct(cnt.get("tx_failed"), txp),
        "rx_drop_misc": cnt.get("rx_drop"), "taxa_drop_rx_pct": pct(cnt.get("rx_drop"), rxp),
        "rx_duplicados": dup, "taxa_duplicados_pct": pct(dup, rxp),
        "beacon_loss": cnt.get("beacon_loss"), "beacons_recebidos": beacon_rx,
        "beacons_esperados": round(expected_beacons) if expected_beacons else None,
        "recepcao_beacons_pct": clamp(pct(beacon_rx, expected_beacons), 0, 100) if expected_beacons else None,
        "airtime_tx_pct": pct(tx_dur / 1e6, connected_s) if tx_dur and connected_s else None,
        "airtime_rx_pct": pct(rx_dur / 1e6, connected_s) if rx_dur and connected_s else None,
        "inatividade_ms": cnt.get("inactive_ms"),
        "janela_s": window,
        "download_atual_bps": w("rx_bytes") * 8 / window if w("rx_bytes") is not None and window else None,
        "upload_atual_bps": w("tx_bytes") * 8 / window if w("tx_bytes") is not None and window else None,
        "pacotes_tx_janela": w("tx_packets"), "retries_janela": w("tx_retries"),
        "taxa_retry_janela_pct": pct(w("tx_retries"), w("tx_packets")),
        "falhas_tx_janela": w("tx_failed"),
    }

    # Contadores do driver e da interface
    keys = be.driver_keys
    d["driver"] = {k: eth[k] for k in keys if eth.get(k) is not None} if keys else \
        {k: v for k, v in eth.items() if v is not None}
    if args.verbose:
        d["driver_completo"] = eth
    d["interface"] = {k: v for k, v in sysfs.items() if k.endswith(("errors", "dropped")) or k in ("collisions", "multicast")}
    d["interface"]["rx_pacotes_total"] = sysfs.get("rx_packets")
    d["interface"]["tx_pacotes_total"] = sysfs.get("tx_packets")

    # IP
    d["ip"] = {
        "mac_cliente": net.get("mac"), "mac_aleatorio": bool(net.get("mac") and int(net["mac"][:2], 16) & 2),
        "ipv4": net["ipv4"], "gateway": net.get("gateway"), "ipv6": net["ipv6"], "gateway6": net.get("gateway6"),
        "dns": net["dns"], "mtu": net.get("mtu"), "dhcp_servidor": net.get("dhcp_server"),
        "dhcp_lease_s": net.get("dhcp_lease_s"),
        "dhcp_expira_em_s": (net["dhcp_expiry"] - time.time()) if net.get("dhcp_expiry") else None,
    }

    # Latência e DNS
    d["latencia"] = {k[1]: v for k, v in results.items() if isinstance(k, tuple) and k[0] == "ping"}
    d["dns"] = [v for k, v in results.items() if isinstance(k, tuple) and k[0] == "dns"]
    d["dns_sistema_ms"] = results.get("sysdns")

    # Estabilidade
    d["estabilidade"] = journal

    # Vizinhança
    own_span = None
    if link.get("center1") and link.get("width"):
        own_span = (link["center1"] - link["width"] / 2, link["center1"] + link["width"] / 2)
    elif freq:
        own_span = channel_span(freq, link.get("channel") or freq_to_chan(freq), link.get("width") or 20)
    others = [a for a in neighbors if not a["in_use"] and a["bssid"] != (link.get("bssid") or "-") and a["freq"]]
    band = freq_band(freq)
    span_of = lambda a: channel_span(a["freq"], a["channel"] or freq_to_chan(a["freq"]), a["width"])
    same_band = [a for a in others if freq_band(a["freq"]) == band]
    cochannel = [a for a in same_band if a["channel"] == link.get("channel")]
    overlapping = [a for a in same_band if own_span and overlaps(own_span, span_of(a))]
    same_ssid = [a for a in others if a["ssid"] and a["ssid"] == link.get("ssid")]
    my_pct = me["signal_pct"] if me else dbm_to_pct(rssi)
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
            sp = channel_span(f, meta["channel"], link.get("width") or 20)
            if sp in seen or not span_allowed(sp, phyinfo["freqs"]):
                continue
            seen.add(sp)
            inter = [a["signal_dbm_est"] for a in others if a["signal_dbm_est"] is not None and overlaps(sp, span_of(a))]
            candidates.append({"canal": meta["channel"], "freq": f, "dfs": meta["radar"], "aps": len(inter),
                               "interferencia_dbm": power_sum_dbm(inter), "atual": overlaps(sp, own_span or (0, 0))})
        candidates.sort(key=lambda c: (c["interferencia_dbm"] if c["interferencia_dbm"] is not None else -999, c["dfs"]))
    current_cand = next((c for c in candidates if c["atual"]), None)

    d["vizinhanca"] = {
        "fonte": "scan novo" if args.rescan else "cache do sistema",
        "aps_visiveis": len(neighbors), "ssids_unicos": len({a["ssid"] for a in neighbors if a["ssid"]}),
        "ocultos": sum(1 for a in neighbors if not a["ssid"]), "por_banda": by_band,
        "aps_mesma_banda": len(same_band), "co_canal": len(cochannel), "sobrepostos": len(overlapping),
        "interferencia_total_dbm": power_sum_dbm([a["signal_dbm_est"] for a in overlapping if a["signal_dbm_est"] is not None]),
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
    o.row("SSID", c["ssid"], hint=c.get("ssid_aviso"))
    o.row("BSSID (AP)", c["bssid"], hint=c["fabricante_ap"])
    o.row("Estado", c["estado"])
    o.row("Conectividade", c["conectividade"], C.green if c["conectividade"] == "full" else C.yellow)
    o.row("Tempo conectado", fmt_dur(c["tempo_conectado_s"]))
    sec = c["seguranca"]
    o.row("Segurança", sec, C.red if sec in (None, "Aberta") or "WEP" in (sec or "") else
          C.green if "WPA3" in (sec or "") else C.yellow if "WPA1" in (sec or "") else C.green, hint=c["key_mgmt"])
    if c["cifra_ptk"]:
        ciphers = " / ".join(x for x in (c["cifra_ptk"], c["cifra_gtk"]) if x)
        o.row("Cifras PTK / GTK" if c["cifra_gtk"] else "Cifra", ciphers, C.red if "TKIP" in ciphers else C.green)
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
    o.row("MCS / Fluxos espaciais", f"MCS {t['mcs']}" + (f" · {t['nss']} SS" if t["nss"] else "") if t["mcs"] is not None else None)
    o.row("Intervalo de guarda", f1(t["gi_us"], " µs"))
    o.row("PHY máximo teórico", f1(t["phy_max_teorico_mbps"], " Mbit/s"),
          hint=(f"≥ {t['fluxos_espaciais_cliente']} SS (estimado pela taxa)" if t.get("fluxos_estimados") else
                f"{t['fluxos_espaciais_cliente']} SS nesta largura") if t["phy_max_teorico_mbps"] else None)
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
    pk = lambda n: f" · {n} pacotes" if n is not None else ""
    o.row("Recebido", f"{fmt_bytes(tr['rx_bytes'])}{pk(tr['rx_pacotes'])}" if tr["rx_bytes"] is not None else None, hint=tr.get("escopo"))
    o.row("Enviado", f"{fmt_bytes(tr['tx_bytes'])}{pk(tr['tx_pacotes'])}" if tr["tx_bytes"] is not None else None, hint=tr.get("escopo"))
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
            bad = k in ("d_rx_crc_err", "d_phy_error", "tx_retry_failed", "d_tx_msdu_dropped", "d_fw_crash_count",
                        "erros_fcs", "falhas_ack", "falhas_decriptacao", "falhas_4way_handshake") and v
            o.row(k, v, C.yellow if bad else None)
        for k, v in sorted(d.get("driver_completo", {}).items()):
            if k not in d["driver"]:
                o.row(k, v, C.dim)

    i = d["interface"]
    o.section("Interface (desde que subiu)")
    pair = lambda a, b: f"{i.get(a)} / {i.get(b)}" if i.get(a) is not None or i.get(b) is not None else None
    o.row("Erros RX / TX", pair("rx_errors", "tx_errors"), C.green if not (i.get("rx_errors") or i.get("tx_errors")) else C.yellow)
    o.row("Descartes RX / TX", pair("rx_dropped", "tx_dropped"),
          hint=f"{f1(pct(i.get('rx_dropped'), i.get('rx_pacotes_total')), ' %', 2)} dos pacotes RX"
          if i.get("rx_pacotes_total") and i.get("rx_dropped") is not None else None)
    if i.get("collisions"):
        o.row("Colisões", i["collisions"], C.yellow)


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
        sig_s = f"{a['signal_pct']}%" if a["signal_pct"] is not None else "—"
        line = (f"{'▶ ' if a['in_use'] else '  '}{(a['ssid'] or '<oculto>')[:24]:<24} {a['bssid']:<17} {a['channel'] or '':>4} {a['width'] or '':>5} "
                f"{sig_s:>6} {a['signal_dbm_est'] or '':>5}  ")
        meter_s = rate(sig, 60, 35)("■" * round(sig / 10)) + " " * (10 - round(sig / 10))
        sec = a["security"]
        sec_s = C.red(sec) if sec == "Aberta" or "WEP" in sec else sec
        o.line((C.bold(C.green(line)) if a["in_use"] else line) + meter_s + " " + sec_s)


def page_stability(o, d, opts):
    e = d["estabilidade"]
    o.section("Estabilidade (últimas 24 h)")
    if not e.get("available"):
        o.row("Histórico", None, hint=e.get("hint") or "journal do wpa_supplicant indisponível")
        return
    o.row("Conexões", e["connects"])
    o.row("Desconexões", e["disconnects"], rate(e["disconnects"], 0, 3, higher_better=False))
    if "falhas" in e:
        o.row("Falhas de conexão", e["falhas"], rate(e["falhas"], 0, 3, higher_better=False))
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
    score_txt = f"{s['valor']}/100 – {name}"
    o.line(f"  Saúde geral do Wi-Fi: {color(C.bold(score_txt))}  {bar(s['valor'], 30)}")
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
SITE_URL = "https://aescolhacertaparasuarede.com.br/"


def footer_keys():
    return [("F1", "Ajuda", curses.KEY_F1), ("F2", None, curses.KEY_F2), ("F4", "Site", curses.KEY_F4),
            ("F5", "Rescan", curses.KEY_F5), ("F6", "Speedtest", curses.KEY_F6), ("F7", "Int -", curses.KEY_F7),
            ("F8", "Int +", curses.KEY_F8), ("F9", "Ordenar", curses.KEY_F9), ("F10", "Sair", curses.KEY_F10)]
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
                    status, attr = f"{SPINNER[int(el * 12) % len(SPINNER)]} Coletando métricas de {BACKEND.label(self.mon.iface)}…", gray
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
            ("Sistema", BACKEND.name, 0),
            ("Interface", BACKEND.label(mon.iface), 0),
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
        for key, label, code in footer_keys():
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

    if IS_WINDOWS:
        os.system("")  # ativa as cores ANSI no console do Windows
        for stream in (sys.stdout, sys.stderr):
            stream.reconfigure(encoding="utf-8", errors="replace")

    global BACKEND
    try:
        BACKEND = make_backend()
    except OSError as e:
        sys.exit(f"ekfi: {e}")
    problem = BACKEND.check()
    if problem:
        sys.exit(problem)
    iface = BACKEND.resolve_iface(args.iface) if args.iface else BACKEND.detect_iface()
    if not iface:
        sys.exit("Nenhuma interface Wi-Fi encontrada." if not args.iface else f"Interface '{args.iface}' não encontrada.")

    interactive = not (args.once or args.json) and sys.stdin.isatty() and sys.stdout.isatty()
    if interactive and curses is None:
        print("O modo interativo precisa do curses. No Windows, instale com:  pip install windows-curses\n"
              "Mostrando o relatório completo (--once).\n", file=sys.stderr)
        interactive = False
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
