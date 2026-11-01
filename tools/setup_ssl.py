# -*- coding: utf-8 -*-
"""用 acme.sh 申请 Let's Encrypt 证书（webroot 模式）。"""
import os

import paramiko

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(os.environ.get("DEPLOY_HOST", ""), port=int(os.environ.get("DEPLOY_PORT", "22")), username=os.environ.get("DEPLOY_USER", "root"), password=os.environ.get("DEPLOY_PASS", ""), timeout=30)

DOMAIN = 'meido.meagames.cn'
WEBROOT = f'/www/wwwroot/{DOMAIN}'
CERTDIR = f'/www/server/panel/vhost/cert/{DOMAIN}'

def run(cmd, check=True):
    print('$ ' + cmd, flush=True)
    i, o, e = c.exec_command(cmd, timeout=300)
    rc = o.channel.recv_exit_status()
    out = o.read().decode('utf-8', 'replace')
    err = e.read().decode('utf-8', 'replace')
    if out.strip():
        print(out.strip())
    if err.strip():
        print('[err]', err.strip())
    if check and rc != 0:
        raise RuntimeError(f'rc={rc}: {cmd}')
    return rc, out

# 1. 安装 acme.sh
run('curl -s https://get.acme.sh | sh -s email=deploy@example.com', check=False)

# 2. 申请证书（webroot 模式，验证文件放站点 .well-known/acme-challenge/）
run(f'mkdir -p {WEBROOT}/.well-known/acme-challenge')
run(
    f'~/.acme.sh/acme.sh --issue -d {DOMAIN} -w {WEBROOT} --server letsencrypt '
    f'--keylength ec-256 --force',
    check=False,
)

# 3. 安装证书到宝塔 cert 目录
run(f'mkdir -p {CERTDIR}')
run(
    f'~/.acme.sh/acme.sh --install-cert -d {DOMAIN} '
    f'--ecc '
    f'--fullchain-file {CERTDIR}/fullchain.pem '
    f'--key-file {CERTDIR}/privkey.pem '
    f'--reloadcmd "nginx -s reload"',
    check=False,
)

run(f'ls -la {CERTDIR}')
c.close()
print('完成。')
