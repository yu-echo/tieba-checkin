<!-- markdownlint-disable MD033 MD041 -->

<div align="center">
  <h1>🌐 贴吧自动签到</h1>
  <img alt="license" src="https://img.shields.io/github/license/yu-echo/tieba-checkin">
  <img alt="platform" src="https://img.shields.io/badge/platform-GitHub%20Actions-blueviolet">
  <img alt="python" src="https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white">
  <img alt="deps" src="https://img.shields.io/badge/dependencies-%E9%9B%B6-brightgreen">
  <img alt="commit" src="https://img.shields.io/github/commit-activity/m/yu-echo/tieba-checkin">
</div>

---

用 **GitHub Actions** 每天自动完成百度贴吧签到，结果推送到微信。

不需要服务器、不需要装依赖、不需要本地常驻——配两个 Secret 就能跑，跑完自动汇报。

---

## 功能介绍

### 🌿 日常签到

- 🎯 自动签到**所有关注的贴吧**（分页抓取，不限数量）
- 🔁 **幂等设计**：已签到的按「已签到」处理并跳过，一天跑多次也不会报错
- 📱 用**手机端接口**，签到经验更多
- 🛡️ **节流 + 重试**：贴吧之间随机间隔，每 30 个额外休息；失败的会刷新令牌后再重试一轮
- ⏱️ **耗时可见**：推送和日志都标明本次总耗时与「平均 N 秒/个」

### 📱 结果推送

- ✅ 签到完成 → 推送总数 / 成功 / 已签到 / 被屏蔽 / 失败
- ❌ 有失败 → 单独标题并列出失败的贴吧
- ⚠️ BDUSS 失效 → 明确提示该重新取了

### 🛡️ 安全

- 🔒 凭据只存 GitHub 加密 Secret，**不在代码、不在 git 历史、不在日志明文**
- 🙈 日志里 BDUSS 只显示长度（`***(len=192)`）
- 🧹 打印接口响应前先抹掉 `BDUSS` / `stoken` / `tbs` 等字段
- 🕶️ **日志不打印贴吧名**，只给「序号 + 短指纹」（见下）
- 🚨 失败会退出码 1，**工作流会真的报红**，不被绿勾掩盖

#### 为什么日志里看不到贴吧名

Actions 日志是公开的——公开仓库的情况下，**任何登录 GitHub 的账号都能下载查看**。
如果逐条打印贴吧名，等于把你关注的全部贴吧（兴趣画像）公开出去。

所以日志长这样：

```
  [123/717 fp=a1b2c3d4] 签到成功，第 4096 个签到
  [456/717 fp=e5f6a7b8] 已签到
  [717/717 fp=c9d0e1f2] 失败：网络请求失败
```

- **指纹**是贴吧名的 SHA-1 前 8 位：同一个吧每次都是同一个值，
  所以能跨运行关联、能定位问题，但**无法反推名字**。
- 正常条目和**失败条目都不打名字**，失败时同样只给指纹。
- 本机调试想看名字：设 `TIEBA_LOG_NAMES=1`（**不要**在 Actions 里打开）。

---

## 效果预览

微信收到的通知长这样：

```
✅ 贴吧签到完成

贴吧总数：42
签到成功：40
已经签到：2
被屏蔽的：0
签到失败：0
耗时：1 分 58 秒（平均 2.8 秒/个）
时间：2026-09-19 07:00:31

----------------------
来源：GitHub Actions · 贴吧自动签到
运行记录：https://github.com/yu-echo/tieba-checkin/actions/runs/123456
Token 认证日期：2026-09-19
```

- **来源**会自动识别：Actions 里显示 `GitHub Actions` 并附运行记录直达链接；
  在本机直接运行则显示 `本地运行`，不会谎报是 CI 发的。
- **没有「凭证有效期至」一行**，这不是漏了：`BDUSS` 是不透明凭据、里面没有签发时间，
  解析不出来就**不伪造**。它什么时候失效，只能等签到失败时才会知道。

日志侧看不到贴吧名（只给序号 + 指纹），原因见下面「安全」一节。

---

## 使用说明

### 第一步：获取 BDUSS

1. 浏览器打开 <https://tieba.baidu.com> 并登录（建议用无痕窗口）
2. 按 `F12` 打开开发者工具
3. 切到「应用程序 / Application」→「Cookie」→ `https://tieba.baidu.com`
4. 找到 `BDUSS`，复制它的**值**

### 第二步：配置 Secrets

在仓库 `Settings → Secrets and variables → Actions → New repository secret` 添加两条：

| Name | Value |
| --- | --- |
| `TIEBA_BDUSS` | 上一步复制的 BDUSS 值 |
| `PUSHPLUS_TOKEN` | [PushPlus](https://www.pushplus.plus/) 的 token（不配也能跑，只是不推送） |

### 第三步：开启并试跑

- 进 `Actions` 页，按提示启用 workflow
- 点 `Run workflow` 手动跑一次，确认日志里出现 `[推送成功]`

之后每天**北京时间 07:00** 自动执行。

---

## ⚠️ 需要知道的一件事

**`BDUSS` 没有刷新机制**，过期就得手动重来一遍「第一步」。

它不像 OAuth 的 refresh token 可以自动续期——百度这个 Cookie 在改密码、被强制下线、
或单纯时间久了之后会失效。表现是「获取 tbs 失败」或大面积签到失败，
这时重新取一次 BDUSS 更新到 Secret 即可。

仓库里已经内置了保活提交（防止 GitHub 停用长期无提交仓库的定时任务），
这部分不用你管；但**凭据过期这件事，脚本帮不了你**。

---

## 技术实现

接口来自贴吧手机端客户端协议：

| 用途 | 方法 | 端点 |
| --- | --- | --- |
| 取 tbs | GET | `https://tieba.baidu.com/dc/common/tbs` |
| 关注的贴吧列表 | POST | `https://c.tieba.baidu.com/c/f/forum/like` |
| 单个贴吧签到 | POST | `https://c.tieba.baidu.com/c/c/forum/sign` |

签名算法：

```python
sign = MD5("".join(f"{k}={v}" for k in sorted(data)) + "tiebaclient!!!").hexdigest().upper()
```

业务码（**不是 HTTP 状态码**）：

| error_code | 含义 | 处理 |
| --- | --- | --- |
| `0` | 签到成功 | 记成功，`user_info.user_sign_rank` 是排名 |
| `160002` | 今日已签到 | 正常，跳过 |
| `340006` | 该贴吧被屏蔽 | 正常，跳过 |
| 其他 | 失败 | 计入失败，进入第二轮重试 |

---

## 鸣谢

- 接口与签名实现参考 [LuoSue/TiebaSignIn-1](https://github.com/LuoSue/TiebaSignIn-1)（MIT, Copyright (c) 2020 srcrs）
- 推送使用 [PushPlus](https://www.pushplus.plus/)
- 部署形态与推送尾部规范沿用本账号其他签到仓库的既有约定
