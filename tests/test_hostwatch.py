"""v8.3.3: сторож сервера host/host-watch.sh — причина обрыва, перезапуск xray, сообщение задним числом.
Скрипт запускается по-настоящему (sh), а curl/ping/docker/systemctl/ip подменены в PATH."""
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "host" / "host-watch.sh"

CURL = r"""#!/bin/sh
# подставной curl: поведение задаётся файлами в $FAKE
url=""; proxy=""; text=""
while [ $# -gt 0 ]; do
  case "$1" in
    -x) proxy=$2; shift;;
    --data-urlencode) text=$2; shift;;
    -m|-o|-w|-d) shift;;
    http*) url=$1;;
  esac; shift
done
case "$url" in
  *api.telegram.org*)
    [ -f "$FAKE/tg_down" ] && exit 7
    echo "$text" >> "$FAKE/sent"; printf '{"ok":true}'; exit 0;;
esac
if [ -n "$proxy" ]; then cat "$FAKE/tunnel" 2>/dev/null || printf 204; exit 0; fi
case "$url" in
  *ya.ru*) [ -f "$FAKE/inet_down" ] && { printf 000; exit 7; }; printf 302;;
  *vps.example*) [ -f "$FAKE/vps_down" ] && { printf 000; exit 7; }; printf 302;;
  *) printf 200;;
esac
"""
STUBS = {
    "curl": CURL,
    "ping": '#!/bin/sh\n[ -f "$FAKE/router_down" ] && exit 1\nexit 0\n',
    "docker": '#!/bin/sh\necho running\n',
    "ip": '#!/bin/sh\necho "default via 192.168.1.1 dev eth0"\n',
    "systemctl": '#!/bin/sh\necho "$@" >> "$FAKE/systemctl"\n[ -f "$FAKE/restart_fails" ] && exit 1\nexit 0\n',
}


@pytest.fixture
def hw(tmp_path):
    fake, binp, state = tmp_path / "fake", tmp_path / "bin", tmp_path / "state"
    for d in (fake, binp):
        d.mkdir()
    for name, body in STUBS.items():
        f = binp / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    (tmp_path / ".env").write_text('BOT_TOKEN=1:x\nADMIN_IDS=5\nTG_PROXY=socks5://10.0.0.1:1080\nTZ=Asia/Yekaterinburg\n')
    (tmp_path / "xray.json").write_text('{"outbounds":[{"settings":{"vnext":[{"address": "vps.example", "port": 443}]}}]}')
    env = dict(os.environ, PATH=f"{binp}:{os.environ['PATH']}", FAKE=str(fake), WATCH_ENV=str(tmp_path / ".env"),
               WATCH_STATE=str(state), WATCH_XRAY_CONF=str(tmp_path / "xray.json"))

    class HW:
        def flag(self, name, on=True):
            (fake / name).touch() if on else (fake / name).unlink(missing_ok=True)

        def tunnel(self, code):
            (fake / "tunnel").write_text(code)

        def run(self, *args):
            r = subprocess.run(["sh", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30)
            return r.stdout + r.stderr

        def sent(self):
            f = fake / "sent"
            return f.read_text().splitlines() if f.exists() else []

        def restarts(self):
            f = fake / "systemctl"
            return f.read_text().count("restart xray") if f.exists() else 0

        def log(self):
            f = state / "history.log"
            return f.read_text() if f.exists() else ""

        state_dir = state
    return HW()


def test_ok_is_silent(hw):
    hw.run(); hw.run()
    assert hw.sent() == [] and hw.restarts() == 0


def test_internet_down_reason_and_no_restart(hw):
    hw.tunnel("000"); hw.flag("inet_down")
    hw.run()
    assert hw.sent() == []                                   # одна неудача — ещё не тревога
    hw.run()
    assert len(hw.sent()) == 1 and "🔴" in hw.sent()[0] and "интернета нет совсем" in hw.sent()[0]
    hw.run()
    assert len(hw.sent()) == 1 and hw.restarts() == 0        # повторов нет, xray не трогаем
    hw.tunnel("204"); hw.flag("inet_down", False)
    hw.run()
    assert "🟢" in hw.sent()[-1] and "Причина обрыва: интернета нет совсем" in hw.sent()[-1]
    hw.run()
    assert len(hw.sent()) == 2 and not list(hw.state_dir.glob("tunnel.*"))


def test_vps_down_and_router_down(hw):
    hw.tunnel("000"); hw.flag("vps_down")
    hw.run(); hw.run()
    assert "VPS (vps.example) напрямую не отвечает" in hw.sent()[0] and hw.restarts() == 0
    hw.flag("router_down")
    assert "роутер (192.168.1.1) не отвечает" in hw.run("--why")


def test_xray_stuck_restarts_once(hw):
    hw.tunnel("000")
    hw.run()
    assert hw.restarts() == 0
    hw.run()
    assert hw.restarts() == 1 and "Перезапустил xray" in hw.sent()[0] and "через xray запросы не идут" in hw.sent()[0]
    hw.run(); hw.run()
    assert hw.restarts() == 1                                # один раз за обрыв
    hw.tunnel("204"); hw.run()
    assert "🟢" in hw.sent()[-1] and "Перезапустил xray" in hw.sent()[-1]
    hw.tunnel("000"); hw.run(); hw.run()
    assert hw.restarts() == 2                                # новый обрыв — снова можно
    assert "перезапустил xray" in hw.log() and "tunnel ok после" in hw.log()


def test_restart_can_be_disabled(hw, tmp_path):
    with open(tmp_path / ".env", "a") as f:
        f.write("WATCH_RESTART_XRAY=0\n")
    hw.tunnel("000"); hw.run(); hw.run(); hw.run()
    assert hw.restarts() == 0 and "🔴" in hw.sent()[0]


def test_alert_undelivered_then_told_afterwards(hw):
    """Telegram сам идёт через туннель: «🔴» не дошло, после восстановления — «🟡 был обрыв» с причиной."""
    hw.tunnel("000"); hw.flag("vps_down"); hw.flag("tg_down")
    out = hw.run() + hw.run() + hw.run()
    assert hw.sent() == [] and "не отправилось" in out
    hw.tunnel("204"); hw.flag("vps_down", False); hw.flag("tg_down", False)
    hw.run()
    assert len(hw.sent()) == 1
    m = hw.sent()[0]
    assert "🟡" in m and "был обрыв с" in m and "VPS (vps.example) напрямую не отвечает" in m
    hw.run()
    assert len(hw.sent()) == 1


def test_short_blip_is_silent(hw):
    hw.tunnel("000"); hw.run()
    hw.tunnel("204"); hw.run()
    assert hw.sent() == [] and "неудача 1" in hw.log()
