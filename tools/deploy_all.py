# 凭据一律从环境变量读，别写回代码里：仓库是公开的
import os

HOST = os.environ.get("DEPLOY_HOST", "")
PORT = int(os.environ.get("DEPLOY_PORT", "22"))
USER = os.environ.get("DEPLOY_USER", "root")
PASS = os.environ.get("DEPLOY_PASS", "")
DOMAIN = "meido.meagames.cn"
WEBROOT = f"/www/wwwroot/{DOMAIN}"
VER = os.environ.get("DEPLOY_VER", "v0.3.0")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_TAR = os.path.join(ROOT, "_src.tar.gz")


def log(msg):
    print(f"==> {msg}", flush=True)


class SSH:
    def __init__(self):
        self.c = paramiko.SSHClient()
        self.c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.c.connect(HOST, port=PORT, username=USER, password=PASS, timeout=30)

    def run(self, cmd, check=True):
        log(f"$ {cmd}")
        _in, out, err = self.c.exec_command(cmd, timeout=600)
        rc = out.channel.recv_exit_status()
        o = out.read().decode("utf-8", "replace")
        e = err.read().decode("utf-8", "replace")
        if o.strip():
            print(o.strip())
        if e.strip():
            print(f"[stderr] {e.strip()}")
        if check and rc != 0:
            raise RuntimeError(f"命令失败 rc={rc}: {cmd}")
        return rc, o, e

    def put(self, local, remote):
        log(f"上传 {local} -> {remote}")
        sftp = self.c.open_sftp()
        sftp.put(local, remote)
        sftp.close()


def main():
    if not os.path.isfile(SRC_TAR):
        log(f"缺少 {SRC_TAR}，请先打包源码")
        sys.exit(1)

    ssh = SSH()

    # 1. 上传源码包
    ssh.run("mkdir -p /root/deckmeido_build")
    ssh.put(SRC_TAR, "/root/deckmeido_build/_src.tar.gz")

    # 2. 解压到干净目录（tar 用相对路径打包，解压直接平铺成 repo）
    ssh.run("rm -rf /root/deckmeido_build/repo && mkdir -p /root/deckmeido_build/repo")
    ssh.run("tar -xzf /root/deckmeido_build/_src.tar.gz -C /root/deckmeido_build/repo")

    # 3. 服务器上打包（需要 python3 / zip；缺则先装）
    ssh.run("command -v python3 || apt-get install -y python3", check=False)
    ssh.run("command -v zip || apt-get install -y zip", check=False)
    ssh.run(
        f"cd /root/deckmeido_build/repo && bash tools/make_release.sh {VER}",
        check=False,
    )

    # 4. 部署产物到 webroot
    ssh.run(f"mkdir -p {WEBROOT} {WEBROOT}/deckmeido")
    ssh.run(f"chown -R www:www {WEBROOT} 2>/dev/null || true")
    dist = "/root/deckmeido_build/repo/dist"
    ssh.run(f"ls -la {dist}", check=False)
    ssh.run(f"cp -f {dist}/latest.json {dist}/SHA256SUMS {WEBROOT}/deckmeido/ 2>/dev/null || true")
    ssh.run(f"cp -f {dist}/deckmeido-*.tar.gz {dist}/deckmeido-*.tar.gz.sha256 {WEBROOT}/deckmeido/ 2>/dev/null || true")
    ssh.run(f"cp -f {dist}/deckmeido-plugin-*.zip {dist}/deckmeido-plugin-*.zip.sha256 {WEBROOT}/deckmeido/ 2>/dev/null || true")
    ssh.run(f"cp -f /root/deckmeido_build/repo/install.sh {WEBROOT}/deckmeido/install.sh")

    # 5. 上传官网 index.html
    ssh.put(os.path.join(ROOT, "website", "index.html"), f"{WEBROOT}/index.html")

    # 6. 校验
    log("===== 部署结果 =====")
    ssh.run(f"ls -la {WEBROOT} {WEBROOT}/deckmeido")

    ssh.c.close()
    log("完成。")


if __name__ == "__main__":
    main()
