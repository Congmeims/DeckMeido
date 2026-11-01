#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""创建 meido.meagames.cn 的 nginx vhost 配置（HTTP80 + SSL 预留）。"""
import os

import paramiko

HOST = os.environ.get("DEPLOY_HOST", "")
PORT = int(os.environ.get("DEPLOY_PORT", "22"))
USER = "root"
PASS = os.environ.get("DEPLOY_PASS", "")
DOMAIN = "meido.meagames.cn"
WEBROOT = f"/www/wwwroot/{DOMAIN}"

WELL_KNOWN = '''set $well_known '';
if ( $uri ~ "^/.well-known/" ) {
  set_by_lua_block $well_known { 
    local m,err = ngx.re.match(ngx.var.uri,"/.well-known/(.*)","isjo")
    if m then
      local filename = ngx.var.document_root .. m[0]
      if not ngx.re.find(m[1],"\\\\./","isjo") then
        local is_exists = io.open(filename, "r")
        if not is_exists then
            filename = "/www/wwwroot/java_node_ssl" ..  m[0]
        end
        if is_exists then is_exists:close() end
        local fp = io.open(filename,'r')
        if fp then
          local file_body = fp:read("*a")
          fp:close()
          if file_body then
            ngx.header['content-type'] = 'text/plain'
            return file_body
          end
        end
      end
    end
    return ""
  }
}

if ( $well_known != "" ) {
  return 200 $well_known;
}
'''

NGINX_CONF = f'''server
{{
    listen 80;
    server_name {DOMAIN};
    index index.html index.htm;
    root {WEBROOT};

    include /www/server/panel/vhost/nginx/extension/{DOMAIN}/*.conf;

    #CERT-APPLY-CHECK--START
    # 用于SSL证书申请时的文件验证相关配置 -- 请勿删除
    include /www/server/panel/vhost/nginx/well-known/{DOMAIN}.conf;
    #CERT-APPLY-CHECK--END

    # 镜像站静态文件，禁止缓存校验和/版本指针
    location /deckmeido {{
        autoindex off;
        add_header Cache-Control "no-cache";
    }}

    # 禁止访问敏感文件
    location ~ ^/(\\.user.ini|\\.htaccess|\\.git|\\.env|\\.svn|\\.project|LICENSE|README.md)
    {{
        return 404;
    }}

    # 一键申请SSL证书验证目录相关设置
    location ~ \\.well-known{{
        allow all;
    }}

    # 禁止在证书验证目录放入敏感文件
    if ( $uri ~ "^/\\.well-known/.*\\.(php|jsp|py|js|css|lua|ts|go|zip|tar\\.gz|rar|7z|sql|bak)$" ) {{
        return 403;
    }}

    location ~ .*\\.(gif|jpg|jpeg|png|bmp|swf|webp)$
    {{
        expires      30d;
        error_log /dev/null;
        access_log /dev/null;
    }}

    location ~ .*\\.(js|css)?$
    {{
        expires      12h;
        error_log /dev/null;
        access_log /dev/null;
    }}

    access_log /www/wwwlogs/{DOMAIN}.log;
    error_log /www/wwwlogs/{DOMAIN}.error.log;
}}
'''


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, port=PORT, username=USER, password=PASS, timeout=30)

    def run(cmd, check=True):
        print(f"$ {cmd}", flush=True)
        _i, o, e = c.exec_command(cmd, timeout=120)
        rc = o.channel.recv_exit_status()
        out = o.read().decode("utf-8", "replace")
        err = e.read().decode("utf-8", "replace")
        if out.strip():
            print(out.strip())
        if err.strip():
            print(f"[err] {err.strip()}")
        if check and rc != 0:
            raise RuntimeError(f"rc={rc}: {cmd}")
        return rc, out

    # 写 well-known 配置
    run(f"mkdir -p /www/server/panel/vhost/nginx/well-known /www/server/panel/vhost/nginx/extension/{DOMAIN}")
    sftp = c.open_sftp()
    with sftp.open(f"/www/server/panel/vhost/nginx/well-known/{DOMAIN}.conf", "w") as f:
        f.write(WELL_KNOWN)
    with sftp.open(f"/www/server/panel/vhost/nginx/{DOMAIN}.conf", "w") as f:
        f.write(NGINX_CONF)
    sftp.close()

    # 测试配置并 reload
    rc, out = run("nginx -t", check=False)
    run("nginx -s reload", check=False)

    c.close()
    print("完成。")


if __name__ == "__main__":
    main()
