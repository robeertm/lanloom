"""Container principles as a test: no root, read-only root file system, capabilities dropped, no privileged mode.

Checked where containers are described: the Dockerfile, compose.yaml, the compose file the installer writes, and
the containers the one-click updater creates on its own (trial start and self-test). The only deliberate exception
is the optional updater sidecar, which needs the container runtime socket (documented in SECURITY.md)."""
import ast
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
HARDENED = ("lanloom", "executor")           # services that must carry every rule
AGENT = "lanloom-agent"                    # optional host agent of another host: shares the host process view, still hardened
EXCEPTION = "updater"                         # runtime socket, root inside; still read-only and no-new-privileges


def dockerfile_lines():
    text = (ROOT / "Dockerfile").read_text()
    text = re.sub(r"\\\n", " ", text)
    return [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]


def test_image_runs_as_an_unprivileged_numeric_user():
    lines = dockerfile_lines()
    users = [l.split(None, 1)[1] for l in lines if l.upper().startswith("USER ")]
    assert users, "no USER instruction"
    uid, _, gid = users[-1].partition(":")
    assert uid.isdigit() and int(uid) >= 1000, users[-1]          # numeric, so a runtime cannot map it to root
    assert not gid or (gid.isdigit() and int(gid) >= 1000)
    after = lines[[i for i, l in enumerate(lines) if l.upper().startswith("USER ")][-1]:]
    assert not any(l.upper().startswith("RUN ") for l in after), "nothing may run as root after the switch"


def test_image_has_no_privilege_tools_or_remote_downloads():
    text = " ".join(dockerfile_lines())
    for bad in ("setcap", "chmod u+s", "chmod +s", "sudo", "--privileged", "curl ", "wget ", "ADD http"):
        assert bad not in text, bad
    assert re.search(r"^FROM python:3\.\d+-slim-\w+", "\n".join(dockerfile_lines()), re.M)
    assert "HEALTHCHECK" in text


def compose_files():
    out = {"compose.yaml": yaml.safe_load((ROOT / "compose.yaml").read_text())}
    script = (ROOT / "deploy" / "install.sh").read_text()
    m = re.search(r"cat > compose\.yaml <<EOF\n(.*?)\nEOF\n", script, re.S)
    assert m, "installer compose block not found"
    out["install.sh"] = yaml.safe_load(m.group(1).replace("${IMAGE}", "img").replace("${PORT}", "7380")
                                       .replace("${UPDATE_CHECK}", "1"))
    agent = ROOT / "deploy" / "agent-compose.yaml"
    out["deploy/agent-compose.yaml"] = yaml.safe_load(agent.read_text())
    for doc in sorted((ROOT / "docs").rglob("*.md")):
        for block in re.findall(r"```ya?ml\n(.*?)```", doc.read_text(), re.S):
            if "services:" in block:
                out[doc.relative_to(ROOT).as_posix()] = yaml.safe_load(block)
    return out


def no_new_privileges(svc):
    return any(str(o).replace(" ", "") in ("no-new-privileges:true", "no-new-privileges") for o in svc.get("security_opt") or [])


def test_compose_services_follow_the_rules():
    for name, doc in compose_files().items():
        for sname, svc in (doc.get("services") or {}).items():
            where = "%s: %s" % (name, sname)
            assert not svc.get("privileged"), where
            assert not svc.get("cap_add"), where
            assert not svc.get("devices"), where
            assert svc.get("ipc") != "host", where
            if sname == AGENT:
                assert svc.get("pid") == "host" and svc.get("read_only") is True and svc.get("cap_drop") == ["ALL"], where
                assert no_new_privileges(svc), where
            else:
                assert svc.get("pid") != "host", where
            if name in ("compose.yaml", "install.sh") and sname in HARDENED + (EXCEPTION,):
                assert svc.get("read_only") is True, where
                assert no_new_privileges(svc), where
            if sname in HARDENED:
                assert svc.get("cap_drop") == ["ALL"], where
                assert str(svc.get("user", "")) not in ("0", "0:0", "root"), where
            if sname == EXCEPTION:
                assert svc.get("profiles") == ["updater"], where           # opt-in only
                assert any("/var/run/docker.sock" in v for v in svc.get("volumes") or []), where


def test_only_the_updater_runs_as_root():
    for name in ("compose.yaml", "install.sh"):
        services = compose_files()[name]["services"]
        root = sorted(s for s, svc in services.items() if str(svc.get("user", "")) in ("0", "0:0", "root"))
        assert root == [EXCEPTION], (name, root)


def dict_literals(path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
            yield keys, node


def test_containers_the_updater_creates_are_hardened():
    """Self-test and trial start of an update candidate: read-only, capabilities dropped, no-new-privileges."""
    seen = 0
    for keys, node in dict_literals(ROOT / "updater" / "engine.py"):
        if "ReadonlyRootfs" in keys:
            seen += 1
            vals = {k.value: v for k, v in zip(node.keys, node.values) if isinstance(k, ast.Constant)}
            assert ast.literal_eval(vals["CapDrop"]) == ["ALL"], node.lineno
            assert "no-new-privileges:true" in ast.literal_eval(vals["SecurityOpt"]), node.lineno
    assert seen >= 2
    for path in sorted((ROOT / "updater").glob("*.py")) + sorted((ROOT / "lanloom").rglob("*.py")):
        for keys, node in dict_literals(path):
            assert "Privileged" not in keys and "CapAdd" not in keys, (path, node.lineno)


def test_image_removes_every_set_user_id_bit_after_the_last_package_step():
    """B10: no mount, su or passwd in the image can gain rights, whatever options the container runs with - that keeps
    local flaws of those tools out of reach (see the exceptions in security/vulnerability-policy.json)."""
    lines = dockerfile_lines()
    runs = [i for i, l in enumerate(lines) if l.upper().startswith("RUN ")]
    strip = [i for i in runs if re.search(r"find / -xdev -perm /6000 -type f -exec chmod a-s \{\} \+", lines[i])]
    assert strip, "the set-user-ID strip step is missing"
    last_pkg = max(i for i in runs if re.search(r"apt-get|pip install|useradd|groupadd", lines[i]))
    assert strip[-1] >= last_pkg, "packages installed after the strip step could bring set-user-ID files back"
    text = (ROOT / "lanloom" / "main.py").read_text()
    assert "pins.setid_files()" in text                                # checked again by the self-test in the image
