"""Setup scripts for another computer: rendering, syntax and dry runs with stubbed commands."""
import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from lanloom.ai.manager import AI
from lanloom.store import Store

HOST = "192.168.1.5:7380"


@pytest.fixture()
def ai(tmp_path):
    return AI(core=None, store=Store(":memory:"), vault=None,
              cfg=SimpleNamespace(demo=False, data_dir=tmp_path))


@pytest.mark.parametrize("os_name", ["linux", "mac", "windows"])
def test_render(ai, os_name):
    code = ai.new_pairing()["code"]
    name, text = ai.installer(os_name, code, HOST, "http")
    assert "http://" + HOST in text and code in text
    import re
    assert not re.search(r"__[A-Z_]+__", text)


def test_wrong_code(ai):
    ai.new_pairing()
    with pytest.raises(PermissionError):
        ai.installer("linux", "AAAA-BBBB", HOST, "http")


def test_expired_code(tmp_path):
    t = [1000.0]
    a = AI(core=None, store=Store(":memory:"), vault=None,
           cfg=SimpleNamespace(demo=False, data_dir=tmp_path), clock=lambda: t[0])
    code = a.new_pairing()["code"]
    t[0] += 10 ** 6
    with pytest.raises(PermissionError):
        a.installer("linux", code, HOST, "http")


@pytest.mark.parametrize("host", ["evil$(id)", "a b", "x;rm", '"q"'])
def test_host_header_injection(ai, host):
    code = ai.new_pairing()["code"]
    with pytest.raises(ValueError):
        ai.installer("linux", code, host, "http")


def _render(ai, os_name, tmp_path):
    code = ai.new_pairing()["code"]
    name, text = ai.installer(os_name, code, HOST, "http")
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p, code


def test_sh_syntax(ai, tmp_path):
    p, _ = _render(ai, "linux", tmp_path)
    assert subprocess.run(["sh", "-n", str(p)], capture_output=True).returncode == 0


@pytest.mark.skipif(not shutil.which("pwsh"), reason="pwsh not installed")
def test_ps_parses(ai, tmp_path):
    p, _ = _render(ai, "windows", tmp_path)
    cmd = ("$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile('%s',[ref]$null,[ref]$e);"
           "if($e.Count){$e|Out-String|Write-Host;exit 1}" % p)
    r = subprocess.run(["pwsh", "-NoProfile", "-Command", cmd], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


STUBS = {
    "curl": r'''#!/bin/sh
echo "curl $*" >> "$STUB_LOG"
out=""; prev=""; url=""
for a in "$@"; do
  [ "$prev" = "-o" ] && out="$a"
  prev="$a"; url="$a"
done
case "$url" in
  */pair/hello) body='{"ok":true,"model":"test-model","download_gb":1.0,"speed":"'"${STUB_SPEED:-good}"'","notes":[],"port":11434}'; code="${STUB_HELLO_CODE:-200}" ;;
  */pair/status) body='{"state":"done","error":null,"result":{"ms":1500,"model":"test-model"}}'; code=200 ;;
  */api/ai/pair) body='{"id":"0123456789abcdef","state":"queued"}'; code=200 ;;
  */api/version) body='{"version":"0.0.0"}'; code=200 ;;
  *) body='{}'; code=404 ;;
esac
[ "$code" = 403 ] && body='{"error":"ai.err.code"}'
[ -n "$out" ] && printf '%s' "$body" > "$out"
printf '%s' "$code"
''',
    "ollama": '#!/bin/sh\necho "ollama $*" >> "$STUB_LOG"\nexit 0\n',
    "systemctl": '#!/bin/sh\necho "systemctl $*" >> "$STUB_LOG"\nexit 0\n',
    "sudo": '#!/bin/sh\necho "sudo $*" >> "$STUB_LOG"\nexec "$@"\n',
    "uname": '#!/bin/sh\ncase "$1" in -m) echo x86_64;; *) echo Linux;; esac\n',
    "nproc": '#!/bin/sh\necho 8\n',
    "id": '#!/bin/sh\necho 1000\n',
    "sleep": '#!/bin/sh\nexit 0\n',
    "launchctl": '#!/bin/sh\necho "launchctl $*" >> "$STUB_LOG"\nexit 0\n',
}


def _dry_run(ai, tmp_path, **env_extra):
    script, code = _render(ai, "linux", tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in STUBS.items():
        f = bindir / name
        f.write_text(body)
        f.chmod(0o755)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       16384000 kB\n")
    log = tmp_path / "log"
    log.write_text("")
    env = {
        "PATH": str(bindir) + ":" + os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path),
        "STUB_LOG": str(log),
        "LANLOOM_AI_MEMINFO": str(meminfo),
        "LANLOOM_AI_SYSTEMD_DIR": str(tmp_path / "systemd"),
    }
    env.update(env_extra)
    r = subprocess.run(["sh", str(script), "--yes"], env=env, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=30)
    return r, log.read_text(), code


def test_dry_run_ok(ai, tmp_path):
    r, log, code = _dry_run(ai, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ready" in r.stdout
    assert "ollama pull test-model" in log
    assert ("-d {\"code\":\"%s\",\"port\":11434,\"model\":\"test-model\"} http://%s/api/ai/pair\n" % (code, HOST)) in log
    assert "systemctl daemon-reload" in log and "systemctl restart ollama" in log
    conf = tmp_path / "systemd" / "ollama.service.d" / "lanloom.conf"
    assert "OLLAMA_HOST=0.0.0.0:11434" in conf.read_text()


def test_dry_run_bad_code(ai, tmp_path):
    r, log, _ = _dry_run(ai, tmp_path, STUB_HELLO_CODE="403")
    assert r.returncode == 2
    assert "wrong or expired" in r.stderr
    assert "ollama pull" not in log


def test_dry_run_unsuitable(ai, tmp_path):
    r, log, _ = _dry_run(ai, tmp_path, STUB_SPEED="unsuitable")
    assert r.returncode == 3
    assert "ollama pull" not in log
