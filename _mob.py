import json, urllib.request, asyncio
import websockets

TARGET = "192.168.124.14:18765"


def pick():
    lst = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=8))
    for t in lst:
        if t.get("type") == "page" and TARGET in t.get("url", ""):
            return t["webSocketDebuggerUrl"]
    raise SystemExit("没找到 target")


JS = r"""
(() => {
  const info = (sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect(), c = getComputedStyle(el);
    return { sel,
      box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
      transform: c.transform, bg: c.backgroundColor, bf: c.backdropFilter,
      pos: c.position, z: c.zIndex, disp: c.display };
  };
  return {
    bodyCls: document.body.className,
    vw: window.innerWidth,
    sidebar: info("#sidebar"),
    rightbar: info("#rightbar"),
    main: info("#main"),
    header: info("header"),
    setPanel: info("#settingsPanel"),
  };
})()
"""


async def main():
    url = pick()
    async with websockets.connect(url, max_size=8 << 20) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                                  "params": {"expression": JS, "returnByValue": True}}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("id") == 1:
                print(json.dumps(msg.get("result", {}).get("result", {}).get("value"),
                                 ensure_ascii=False, indent=1))
                return


asyncio.run(main())
