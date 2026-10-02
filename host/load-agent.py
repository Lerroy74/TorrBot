#!/usr/bin/env python3
"""Агент нагрузки torrbot — работает на самом сервере (вне Docker), от root.

Каждые 5 секунд пишет в ~/torrbot/data/host-load.json:
  * диск с медиатекой (MEDIA_DIR из .env): % занятости, запись/чтение МБ/с, отклик мс;
  * сеть (интерфейс маршрута по умолчанию): Мбит/с приём/отдача;
  * iowait процессора;
  * какие фильмы сейчас открыты по сети (Samba) и с какого адреса;
  * SMART диска (раз в 6 часов; нужен smartmontools).
Бот читает файл и решает, тормозить ли закачки и кого предупредить (см. bot/guard.py).

Ручной запуск:  sudo torrbot-load-agent --once     — один замер на экран
Установка:      sudo sh ~/torrbot/host/install.sh
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time

VIDEO = (".mkv", ".avi", ".mp4", ".m4v", ".mov", ".wmv", ".ts", ".m2ts", ".mpg", ".mpeg", ".webm", ".flv")
SMART_EVERY = 6 * 3600
MB = 1024 * 1024


def getenv(env_file: str, name: str, default: str = "") -> str:
    try:
        with open(env_file, encoding="utf-8") as f:
            val = default
            for line in f:
                m = re.match(rf"^\s*{name}\s*=\s*(.*?)\s*$", line)
                if m:
                    val = m.group(1).strip().strip('"').strip("'")
            return val
    except OSError:
        return default


def run(cmd: list[str], timeout: int = 10) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def disk_of(path: str) -> tuple[str | None, str | None]:
    """(устройство раздела /dev/sdb1, имя диска sdb) для папки."""
    src = run(["findmnt", "-no", "SOURCE", "--target", path]).strip()
    if not src.startswith("/dev/"):
        return None, None
    parent = run(["lsblk", "-no", "PKNAME", src]).strip().splitlines()
    name = parent[0].strip() if parent and parent[0].strip() else re.sub(r"\d+$", "", os.path.basename(src))
    return src, name


def diskstats(dev: str) -> list[int] | None:
    with open("/proc/diskstats") as f:
        for line in f:
            p = line.split()
            if len(p) >= 14 and p[2] == dev:
                return [int(x) for x in p[3:14]]
    return None


def cpu_times() -> list[int]:
    with open("/proc/stat") as f:
        return [int(x) for x in f.readline().split()[1:]]


def default_iface() -> str | None:
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                p = line.split()
                if len(p) > 2 and p[1] == "00000000":
                    return p[0]
    except OSError:
        pass
    return None


def netbytes(iface: str) -> tuple[int, int] | None:
    with open("/proc/net/dev") as f:
        for line in f:
            if ":" in line and line.split(":")[0].strip() == iface:
                p = line.split(":")[1].split()
                return int(p[0]), int(p[8])
    return None


_TIME = re.compile(r"\s+\w{3} \w{3}\s+\d+ \d\d:\d\d:\d\d \d{4}\s*$")


def smb_open(media: str) -> list[dict]:
    """Открытые по Samba видеофайлы медиатеки: [{"name": "movies/…/x.mkv", "client": "192.168.1.40"}]."""
    if not shutil.which("smbstatus"):
        return []
    machines = {}
    for line in run(["smbstatus", "-p"]).splitlines():
        p = line.split()
        if len(p) >= 4 and p[0].isdigit():
            m = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
            if m:
                machines[p[0]] = m.group(1)
    out, seen = [], set()
    media = media.rstrip("/")
    for line in run(["smbstatus", "-L"]).splitlines():
        i = line.find(media)
        if i < 0 or not line.split() or not line.split()[0].isdigit():
            continue
        name = _TIME.sub("", line[i + len(media):]).strip()
        if not name.lower().endswith(VIDEO):
            continue
        client = machines.get(line.split()[0], "")
        if (name, client) not in seen:
            seen.add((name, client))
            out.append({"name": name, "client": client})
    return out


def smart(dev_path: str) -> dict:
    if not shutil.which("smartctl"):
        return {"error": "smartctl не установлен (sudo apt install smartmontools)"}
    data = None
    for extra in ([], ["-d", "sat"]):
        txt = run(["smartctl", "-H", "-A", "-j", *extra, dev_path], timeout=30)
        try:
            d = json.loads(txt)
        except ValueError:
            continue
        if "smart_status" in d or (d.get("ata_smart_attributes") or {}).get("table"):
            data = d
            break
    if data is None:
        return {"error": "диск не отдаёт SMART (бывает через USB-бокс)"}
    attrs = {a.get("id"): ((a.get("raw") or {}).get("value") or 0)
             for a in (data.get("ata_smart_attributes") or {}).get("table") or []}
    passed = (data.get("smart_status") or {}).get("passed")
    return {"health": "PASSED" if passed else ("FAILED" if passed is False else "?"),
            "temp": (data.get("temperature") or {}).get("current"),
            "realloc": attrs.get(5, 0), "pending": attrs.get(197, 0), "uncorrect": attrs.get(198, 0),
            "checked": int(time.time())}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=os.environ.get("AGENT_ENV", "/home/lerroy/torrbot/.env"))
    ap.add_argument("--out", default=os.environ.get("AGENT_OUT", ""))
    ap.add_argument("--every", type=float, default=5.0)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.env)), "data", "host-load.json")
    media = getenv(a.env, "MEDIA_DIR", "/mnt/media")
    part, dev = disk_of(media)
    iface = default_iface()
    prev = (time.time(), diskstats(dev) if dev else None, cpu_times(), netbytes(iface) if iface else None)
    smart_state, smart_at = {}, 0.0
    if a.once:
        time.sleep(a.every)
    while True:
        if not a.once:
            time.sleep(a.every)
        now = time.time()
        if not dev or diskstats(dev) is None:          # диск переподключили (USB → SATA) — найти заново
            part, dev = disk_of(media)
        if iface is None or netbytes(iface) is None:
            iface = default_iface()
        ds = diskstats(dev) if dev else None
        cpu = cpu_times()
        nb = netbytes(iface) if iface else None
        dt = max(0.5, now - prev[0])
        rec: dict = {"ts": int(now), "interval": round(dt, 1)}
        if ds and prev[1]:
            d = [x - y for x, y in zip(ds, prev[1])]
            ios = d[0] + d[4]
            rec["disk"] = {"dev": dev, "util": round(min(100.0, d[9] / (dt * 10)), 1),
                           "read_mb": round(d[2] * 512 / dt / MB, 2), "write_mb": round(d[6] * 512 / dt / MB, 2),
                           "await_ms": round((d[3] + d[7]) / ios, 1) if ios else 0.0}
        dc = [x - y for x, y in zip(cpu, prev[2])]
        rec["iowait"] = round(100 * dc[4] / max(1, sum(dc)), 1) if len(dc) > 4 else 0.0
        if nb and prev[3]:
            rec["net"] = {"iface": iface, "rx_mbit": round((nb[0] - prev[3][0]) * 8 / dt / 1e6, 1),
                          "tx_mbit": round((nb[1] - prev[3][1]) * 8 / dt / 1e6, 1)}
        files = smb_open(media)
        rec["smb"] = {"open": len(files), "items": files}
        if part and now - smart_at >= SMART_EVERY:
            smart_state, smart_at = smart(part), now
        rec["smart"] = smart_state
        prev = (now, ds, cpu, nb)
        if a.once:
            print(json.dumps(rec, ensure_ascii=False, indent=1))
            return
        try:
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out + ".tmp", "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False)
            os.chmod(out + ".tmp", 0o644)
            os.replace(out + ".tmp", out)
        except OSError as e:
            print(f"не смог записать {out}: {e}", flush=True)


if __name__ == "__main__":
    main()
