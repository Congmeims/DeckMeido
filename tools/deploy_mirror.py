#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上传 dist/ 产物 + 官网首页到服务器（对应 tools/deploy_mirror.sh 的 Windows 版）。"""
import os
import stat
import sys

import paramiko

HOST = os.environ.get("DEPLOY_HOST", "")
PORT = int(os.environ.get("DEPLOY_PORT", "22"))
USER = "root"
PASS = os.environ.get("DEPLOY_PASS", "")
DOMAIN = "meido.meagames.cn"
WEBROOT = f"/www/wwwroot/{DOMAIN}"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, port=PORT, username=USER, password=PASS, timeout=30)

    def run(cmd):
        print(f"$ {cmd}", flush=True)
        _i, out, err = c.exec_command(cmd, timeout=300)
        rc = out.channel.recv_exit_status()
        o = out.read().decode("utf-8", "replace").strip()
        e = err.read().decode("utf-8", "replace").strip()
        if o:
            print(o)
        if e:
            print(f"[stderr] {e}")
        return rc

    run(f"mkdir -p {WEBROOT}/deckmeido")

    files = [
        (os.path.join(DIST, "latest.json"), f"{WEBROOT}/deckmeido/latest.json"),
        (os.path.join(DIST, "SHA256SUMS"), f"{WEBROOT}/deckmeido/SHA256SUMS"),
        (os.path.join(DIST, "install.sh"), f"{WEBROOT}/deckmeido/install.sh"),
        (os.path.join(ROOT, "website", "index.html"), f"{WEBROOT}/index.html"),
        (os.path.join(ROOT, "website", "logo.png"), f"{WEBROOT}/logo.png"),
    ]
    for fn in os.listdir(DIST):
        if fn.startswith("deckmeido-"):
            files.append((os.path.join(DIST, fn), f"{WEBROOT}/deckmeido/{fn}"))

    sftp = c.open_sftp()
    for local, remote in files:
        print(f"上传 {os.path.basename(local)}", flush=True)
        sftp.put(local, remote)
        sftp.chmod(remote, 0o644)
    sftp.close()

    run(f"chown -R www:www {WEBROOT}")
    run(f"chmod 755 {WEBROOT}/deckmeido/install.sh")
    run(f"ls -la {WEBROOT} {WEBROOT}/deckmeido")
    c.close()
    print("完成。")


if __name__ == "__main__":
    main()
