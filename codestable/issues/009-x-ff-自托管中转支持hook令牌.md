---
kind: issue
title: "自托管中转支持 hook 令牌：云端会话开场自动带规矩在 LAX 上重新可用"
type: ff
status: closed
created: 2026-11-25
---

# 自托管中转支持 hook 令牌

11-13 把中转搬到自托管的 LAX 以后，云端会话开场自动带上电脑上规矩的 hook 一直不通。11-25 实测：旧域名上的 hook 回「电脑离线，开场没带上规矩」，因为电脑连的是 LAX；LAX 上同一路径回 404。原因是自托管的 compose 文件没有把 HOOK_TOKEN_FILE 传给中转，LAX 上也没生成过 hook 令牌。中转代码本来就支持，缺的是自托管这套部署脚本。

- 改动：
  - `relay/selfhost/compose.yaml`：把 HOOK_TOKEN_FILE 传给中转。
  - `relay/selfhost/install.sh`：加 --hook on|off。on 在 secrets/hook-token 不存在时生成它，不打印；这个文件在，就在 .env 里设上 HOOK_TOKEN_FILE；off 删掉它；不给就沿用。
  - `relay/selfhost/config.mjs`：hook 令牌设了，就要至少 32 个字符，并且不能和主人口令、设备令牌、代理令牌相同。它是放在云端环境请求头里的，不能和别的密钥撞。
  - `relay/test/selfhost.test.mjs`：三条新测试。配置校验；compose 里有这个变量；用假的 docker 和 curl 把安装脚本跑一遍：令牌生成了、没打印、只有主人能读、再跑一次保留、--hook off 移除。三条在旧代码上都失败。
  - docs 目录的规则文档、自托管说明：写明新选项和设置办法。
- 验证：relay 的类型检查通过，单元测试 61 条通过，CI 的 e2e 和 windows 通过。LAX 上的部署和端到端验证见下面。
- codestable：无影响。现状规格没写自托管怎么开 hook；已同步 docs 目录的规则文档和自托管说明。

## 部署（LAX，2026-11-25）

- 合并后（提交 3158f75）在 LAX 上后台升级，命令带 --cimd on --hook on。脚本和代码都固定在这个提交，不取 main，因为 GitHub 的原始文件有几分钟缓存，刚合并就取 main 可能拿到旧脚本。
- 升级后核对：升级日志里只写了令牌文件的位置，没有令牌；.env 里有 HOOK_TOKEN_FILE；令牌文件 43 字节、权限 600；容器里这个变量已设，设备令牌的变量为空，CIMD 还开着；回退用的镜像 winhand-relay:previous 在。
- 从外面验证：不带令牌打 hook 路径回 401（部署前是 404），错误令牌回 401。
- 在服务器上用令牌文件做真实请求（令牌不打印）：规矩、技能名单、本机工具三段都回 200，没有「电脑离线」的提示，分别 8333、2459、8860 个字符。云端 → 中转（验令牌）→ 主人的电脑这条链通了。
- 还要主人做：在 claude.ai 的云环境里加一条 Network secret（域名、请求头名 X-Winhand-Hook-Token、值从服务器上的令牌文件读），再给要用的私有仓库跑 winhand bridge init --write。这个仓库是公开的，不装这个 hook：生成的配置里写着中转的域名。
