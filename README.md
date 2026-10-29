# FileWeb

一个依赖 Python 标准库的轻量文件归档管理服务，包含目录浏览、文件夹创建、多文件上传、下载、删除、移动、同名覆盖确认、文本/图片/PDF/HTML 新窗口预览，以及受限的 Codex CLI 文件助手。

浏览器通过前端登录页建立会话；勾选“在此设备保持登录”后，登录状态会在浏览器关闭及服务重启后继续保留，直到用户在右上角账户面板中选择“切换登录账户”。右上角头像也可用于修改密码和上传头像。HTML 预览默认支持到 128 MiB，并以流式方式加载；普通文本预览仍限制为 4 MiB。

助手入口采用可拖拽悬浮球：点击后从右侧展开，移动端自动变为底部面板；可用最小化按钮、遮罩或 `Esc` 随时收起。助手面板支持多对话切换、对话重命名/删除、工作目录选择和连续上下文；输出通过 SSE 流式显示，浏览器本地会保留对话记录。

## 本地运行

```bash
mkdir -p /tmp/fileweb-data
FILEWEB_ROOT=/tmp/fileweb-data FILEWEB_PASSWORD=your-password python3 server.py
```

访问 `http://127.0.0.1:8787`，默认用户名 `admin`，密码取自 `FILEWEB_PASSWORD`；未设置该变量时回退为 `admin`，只适合本机开发使用。

常用环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `FILEWEB_ROOT` | `/srv/fileweb/data` | 归档根目录，所有文件操作都被限制在此目录内 |
| `FILEWEB_HOST` | `127.0.0.1` | 监听地址，默认只监听本机 |
| `FILEWEB_PORT` | `8787` | 监听端口 |
| `FILEWEB_USER` | `admin` | 登录用户名 |
| `FILEWEB_PASSWORD` | `admin` | 登录密码；生产环境请显式设置或改用 `FILEWEB_PASSWORD_FILE` |
| `FILEWEB_PASSWORD_FILE` | `/etc/fileweb/admin-password` | 密码文件，存在时优先于 `FILEWEB_PASSWORD` |
| `FILEWEB_PUBLIC_URL` | 空 | 对外基地址，留空时分享链接按请求的 Host 生成 |

## 安全边界

- 所有文件操作都限制在 `FILEWEB_ROOT`，拒绝 `..`、反斜杠和符号链接。
- 上传大小默认 100 MiB，预览大小默认 4 MiB，默认不覆盖同名文件。
- 删除目录会明确二次确认；移动默认不覆盖目标项目；同名上传先返回冲突并由界面确认后才覆盖。
- 查看文件会打开受保护的新预览页面，不会覆盖当前管理界面。
- 点击文件行的“分享”会生成 1-30 天有效的只读链接。访问分享链接不需要登录，只能查看指定文件，不能浏览目录或访问管理 API。
- HTML 默认启用隔离的交互预览，内嵌按钮、图表操作和下载导出打开即可使用；点击顶部「静态预览」可切回无脚本模式。普通预览和分享预览均支持此切换，`mode=static` 明确关闭脚本，`mode=interactive` 启用交互；空、未知或重复的模式参数仍按静态模式处理。
- 交互 HTML 在浏览器分配的独立沙箱源（opaque origin）中运行，iframe 和原文件响应 CSP 均不授予 `allow-same-origin`。脚本不能读取管理页面、Cookie 或本地存储；管理 API 请求、外部资源和表单提交仍受限制。允许用户触发的提示框、下载和新窗口，新窗口继续继承沙箱；此方案无需新增域名或端口，不直接注入管理页面。
- 交互文档需内嵌脚本和样式，图片支持 data/blob；外部 CDN、`fetch`、XHR 和 WebSocket 仍被阻止。跨刷新保存主题、部分剪贴板功能等依赖浏览器存储或权限的功能可能不可用。原文件初次加载仍按现有登录或分享授权校验，沙箱内的脚本无法读取这些凭据。
- agent 默认只读；勾选写入后仅获得工作目录内的 Codex `workspace-write` 能力。部署服务使用专用系统用户，不能修改操作系统。
- 浏览器使用 HttpOnly session cookie 和逐会话 CSRF token；长期登录只在服务器保存令牌摘要，不保存密码或可直接使用的登录令牌。Basic Auth 仍保留用于兼容命令行客户端，健康检查只返回状态。

## 远端部署

`./deploy.sh <host>` 会把应用上传到目标主机，创建 `fileweb` 系统用户和 systemd 服务，并按 `fileweb.service` 中的 `FILEWEB_USER`（默认 `admin`）提供登录账户。首次部署会在目标机生成随机密码文件，后续升级不会覆盖已有密码。服务监听 8787 端口：

```bash
./deploy.sh <host>
```

`<host>` 写可 SSH 登录的主机名或地址。可选设置环境变量 `FILEWEB_FIREWALL_CIDR`，脚本会额外添加一条只放行指定网段的防火墙规则；不设置则不改动防火墙。

密码保存在远端 `/etc/fileweb/admin-password`，不写入应用代码或部署包。助手默认只读，界面中的“允许助手修改当前归档”复选框会为该次任务启用受限写入；服务用户只能访问 `/srv/fileweb/data` 和自己的运行目录。

部署脚本只替换 `/srv/fileweb/app`，不会删除或覆盖 `/srv/fileweb/data` 中的归档文档；数据目录独立于应用和账户配置。升级时会先停止服务，再对完整目录结构和文件内容计算指纹，升级重启后再次校验。
