"""英文界面文案（机翻）与语言切换的字符串表

只在 [ui] language = "en" 时由 /api/i18n 下发，网页/客户端按「中文原文 →
英文译文」的精确匹配替换静态文案。没有收录的句子保持中文（半覆盖好过乱翻），
动态生成的对话内容不参与替换。

插件端暂未翻译：面板仍用中文，后续要用的话直接在 plugin 里读 /api/i18n 即可。
"""
from __future__ import annotations

# 机翻表：键是界面上出现的中文原文（会先去首尾空白再比对）
EN: dict[str, str] = {
    # 输入区 / 通用按钮
    "发送": "Send",
    "终止": "Stop",
    "设置": "Settings",
    "保存": "Save",
    "保存设置": "Save settings",
    "取消": "Cancel",
    "关闭": "Close",
    "返回": "Back",
    "刷新": "Refresh",
    "删除": "Delete",
    "重试": "Retry",
    "分享": "Share",
    "复制": "Copy",
    "确定": "OK",
    "开启": "On",
    "打开": "Open",
    "新会话": "New chat",
    "删除会话": "Delete chat",
    "会话": "Chats",
    "回到最新": "Jump to latest",
    "回复": "Reply",
    "重新生成": "Regenerate",
    "@ 添加上下文，/ 使用命令": "@ add context, / for commands",
    "按回车加入队列，等这一轮结束自动发送": "Enter to queue; it sends after this turn",
    "终止当前操作（输入框里按回车 = 加入队列）": "Stop this turn (Enter in the box queues a message)",
    # 语音
    "语音": "Voice",
    "停止": "Stop",
    "识别中": "Transcribing",
    "语音输入": "Voice input",
    "语音播报回复": "Read replies aloud",
    "录音时静音外放": "Mute output while recording",
    "录音时静音外放（录完自动恢复）": "Mute output while recording (restored after)",
    "语音输入未启用或模型未下载：到设置 → 对话与语音里开启并下载模型":
        "Voice input is off or the model is missing. Enable and download it in Settings → Chat & Voice.",
    # 设置导航与分区
    "对话与语音": "Chat & Voice",
    "模型设置": "Model",
    "插件设置": "Plugin",
    "设备": "Device",
    "远程访问": "Remote access",
    "关于更新": "About & updates",
    "界面语言": "Language",
    "版本": "Version",
    "更新源": "Update sources",
    "检查更新": "Check for updates",
    "立即升级到最新版": "Upgrade to latest",
    "当前最新": "Up to date",
    "已是最新版本": "Already up to date",
    "有更新": "Update available",
    "同时探测 GitHub（关掉只用国内镜像）": "Also query GitHub (off = mirror only)",
    # 附件与上下文
    "附件": "Attachments",
    "添加附件": "Add attachments",
    "引用文件": "Reference files",
    "引用工作区文件": "Reference workspace files",
    "引用当前屏幕截图": "Reference current screenshot",
    # 权限
    "权限": "Permissions",
    "手动审批": "Ask every time",
    "智能审批": "Smart approval",
    "完全访问": "Full access",
    # 远程
    "连接": "Connect",
    "断开": "Disconnect",
    "局域网可访问": "Accessible on your LAN",
    # 模型
    "模型": "Model",
    "温度": "Temperature",
    "上下文": "Context",
    "人设": "Persona",
    "读取中": "Loading",
    "读取中…": "Loading…",
    "正在读取版本信息…": "Reading version info…",
    # 关于
    "用户数据": "User data",
    "安装目录": "Install directory",
    "安装器文件": "Installer files",
}


def strings() -> dict[str, str]:
    """返回一份拷贝，调用方随意改不会污染模块"""
    return dict(EN)
