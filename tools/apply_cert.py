# -*- coding: utf-8 -*-
"""用宝塔内置 acme_v2.py 申请证书（HTTP 验证）。"""
import os

import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(os.environ.get("DEPLOY_HOST", ""), port=int(os.environ.get("DEPLOY_PORT", "22")), username=os.environ.get("DEPLOY_USER", "root"), password=os.environ.get("DEPLOY_PASS", ""), timeout=30)

DOMAIN = 'meido.meagames.cn'
WEBROOT = f'/www/wwwroot/{DOMAIN}'

def run(cmd, t=180):
    print('$ ' + cmd, flush=True)
    i, o, e = c.exec_command(cmd, timeout=t)
    rc = o.channel.recv_exit_status()
    out = o.read().decode('utf-8', 'replace')
    err = e.read().decode('utf-8', 'replace')
    if out.strip():
        print(out.strip())
    if err.strip():
        print('[err]', err.strip())
    return rc, out

# 确保目录属主正确（well-known 需要 nginx 能写）
run(f'mkdir -p {WEBROOT}/.well-known/acme-challenge')
run(f'chown -R www:www {WEBROOT}')

# 申请证书
rc, out = run(
    f'cd /www/server/panel/class && /www/server/panel/pyenv/bin/python3 acme_v2.py '
    f'--domain {DOMAIN} --type http --path {WEBROOT}',
    t=300,
)

c.close()
print('done rc=', rc)
