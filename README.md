# MoviePilot 插件合集

自己维护的 **MoviePilot V2 插件集合**，全部放在这一个仓库里：MP 里只要添加一次外部插件仓库，就能看到、安装、更新所有插件，不用再挨个记仓库地址。

插件只调用各自服务的官方接口；**凭据由 MP 或插件设置页保管，仓库里不含任何 token / Cookie / API Key**。

---

## 插件一览

| 插件 | 说明 | 版本 | 状态 |
|---|---|---|---|
| 豆瓣通知图片代理 | 改写通知里的豆瓣海报地址，解决企业微信/微信封面裂图；竖版封面自动转横版 | 1.6 | 维护中 |
| 115秒传搬运 | 监控本地目录，能秒传就传 115、发通知、可选删源；不能就次日重试 | 1.4 | 维护中 |
| 115秒传助手·整理通知 | MP 整理完成后把路径推给 115秒传助手 | 1.1 | ⚠️ 已弃用，建议卸载 |
| 115秒传监控 | 监控文件夹验证秒传，成功后把源文件移入回收目录并微信通知，不能则次日重试 | 1.0.0 | 维护中 |
| WorkBuddy每日签到 | 每天自动领 WorkBuddy 每日积分，令牌自续期，复用 MP 内置企业微信通知 | 1.0.0 | 维护中 |

> `115秒传搬运` 是 MP 插件版（复用 MP 已授权的 115 存储）；独立的 `115-rapid-upload` 是带 WebUI 的 Docker 项目，两者不是同一个东西。

---

## 快速开始

### 1. 在 MP 里添加一次仓库

外部插件仓库地址（**不要带 `.git` 后缀**，带后缀会让 MP 拼 raw 地址时拉不到清单）：

```
https://github.com/celestial520/mp-plugins
```

MP →【插件】→ 插件仓库／设置 → **添加外部插件仓库** → 粘贴上面的地址并保存 → 刷新插件列表。

### 2. 按需安装

| 想做什么 | 装哪个 |
|---|---|
| 通知里的豆瓣封面不显示 | 豆瓣通知图片代理 |
| 把本地文件自动秒传进 115 | 115秒传搬运 |
| 秒传成功后把源文件移到回收目录 | 115秒传监控 |
| 每天自动领 WorkBuddy 积分 | WorkBuddy每日签到 |

点安装 → 启用 → 按对应插件的 README 填配置项。各插件互相独立，可以只装需要的。

### 3. 手动安装（GitHub 拉不到时）

```bash
git clone https://github.com/celestial520/mp-plugins
```

把 `plugins.v2/<插件目录>` 整个复制到 MP 的 `app/plugins/` 下（绿联 NAS Docker 部署时通常是 config 映射目录里的 `plugins/`，容器内 `/app/app/plugins/` 或 `/config/plugins/`），重启 MP。

### 4. 依赖

| 插件 | 额外依赖 |
|---|---|
| 豆瓣通知图片代理 / 115秒传搬运 / 整理通知 / WorkBuddy每日签到 | 无，只用 MP 自带的 `requests` 和标准库 |
| 115秒传监控 | `p115client`（见该插件目录 `requirements.txt`；装过「115网盘STRM助手」等插件通常已自带） |

---

## 各插件说明

| 插件 | 详细文档 | 配置要点 |
|---|---|---|
| 豆瓣通知图片代理 | [README](plugins.v2/doubanimage/README.md) | 开关 + 可选豆瓣 Cookie；代理地址要公网可达 |
| 115秒传搬运 | [README](plugins.v2/u115rapidupload/README.md) | 监控目录、115 目标目录、触发方式、删源开关 |
| 115秒传助手·整理通知 | [README](plugins.v2/u115transfernotify/README.md) | 助手地址 / API Key / 路径映射（已弃用） |
| 115秒传监控 | [README](plugins.v2/p115instanttransfer/README.md) | 监控目录、回收目录、115 Cookie、目标 CID |
| WorkBuddy每日签到 | [README](plugins.v2/workbuddycheckin/README.md) | 长效令牌（用 `tools/export_token.bat` 导出） |

---

## 仓库结构

```
mp-plugins/
├── package.v2.json                    # MP V2 插件清单，新增插件要在这里登记
├── README.md                          # 本文件
├── tools/
│   ├── export_token.cjs               # WorkBuddy 长效令牌导出工具（Windows）
│   └── export_token.bat
└── plugins.v2/
    ├── doubanimage/                   # 豆瓣通知图片代理
    ├── u115rapidupload/               # 115秒传搬运
    ├── u115transfernotify/            # 115秒传助手·整理通知（已弃用）
    ├── p115instanttransfer/           # 115秒传监控（含 requirements.txt）
    └── workbuddycheckin/              # WorkBuddy每日签到
```

---

## 常见问题

**Q：以前那两个独立插件仓库呢？**
已经合并进本仓库并删除了源仓库。以后 MP 里只填 `mp-plugins` 这一个地址即可；已经装过的插件不受影响，更新时重新同步插件列表。

**Q：`115秒传搬运` 和 `115秒传监控` 装哪个？**
`搬运` 走 MP 内置的 115 开放平台授权、复用 MP 存储实例，功能更全（三种触发、真实上传、空文件夹清理）；`监控` 是自己填 115 Cookie、把秒传成功的文件移进回收目录的早期版本。新装建议用 `搬运`。

**Q：整理通知插件还能用吗？**
不能了。115秒传助手已移除外部触发接口，现在靠 inotify 文件变化事件自己发现文件，插件推送会失败。清单里保留该条目仅作记录，建议卸载。

**Q：插件之间会冲突吗？**
不会。它们监听不同事件、读写各自的插件数据；但两个 115 秒传类插件别监控同一个目录，否则会抢文件。

**Q：外部插件仓库拉取失败 / 列表不刷新 / 版本号一直不涨？**
按顺序排查：① 仓库地址必须是 `https://github.com/celestial520/mp-plugins`，**不要带 `.git`**；② 检查 NAS 能否访问 GitHub，走 `GITHUB_PROXY` 的话注意镜像会缓存 `package.v2.json`，可先在插件页刷新／重新同步，必要时换个镜像或重启 MP；③ 插件市场显示的是**清单版本**，已安装插件卡片显示的是**安装时记录的版本**——清单更新后必须在该插件上点「更新」才会替换 `app/plugins/` 里的 `__init__.py`；④ 更新完重启 MP 容器，设置页的新控件才会出现。实在拉不动就用「手动安装」把 `plugins.v2/workbuddycheckin` 复制进 MP 的 `app/plugins/` 再重启。

**Q：企业微信通知能不能用？**
能。以上插件都走 MP【设定 → 通知】里已配好的渠道（`CommandChain.post_message`），插件本身不需要填 corpid / Secret / AgentId。

---

## 新增 / 更新插件

1. 在 `plugins.v2/<小写目录名>/` 放 `__init__.py`（类继承 `_PluginBase`，实现 `get_form()` / `get_service()` / `get_state()` 等）。
2. 同目录放一份 README，格式参照 `115-rapid-upload`。
3. 在 `package.v2.json` 登记，key 用驼峰类名：

```json
{
  "MyPlugin": {
    "name": "插件显示名",
    "description": "一句话说明",
    "labels": "网盘,自动化",
    "version": "1.0",
    "author": "celestial520",
    "level": 1,
    "history": { "v1.0": "初始版本" },
    "author_url": "https://github.com/celestial520/mp-plugins",
    "repo_url": "https://github.com/celestial520/mp-plugins"
  }
}
```

4. 升级时递增 `version` 并补一条 `history`，MP 才能识别到新版本。

---

## 免责声明

这些插件只调用对应服务的官方接口，用于自己账号、自己文件的日常自动化，不涉及破解、刷分或绕过会员限制。接口与活动规则可能随官方调整而变化，插件会随之失效并给出提示。请遵守各平台服务条款，并自行保管好插件设置页里填写的令牌与 Cookie。
