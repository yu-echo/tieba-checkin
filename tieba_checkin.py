#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""百度贴吧自动签到

接口来源：手机端客户端协议，取自实测可用的开源实现并经核对（见 README 鸣谢）。
不是猜的——三个端点与签名算法如下：

    GET  https://tieba.baidu.com/dc/common/tbs          取 tbs（BDUSS 走 Cookie）
    POST https://c.tieba.baidu.com/c/f/forum/like       关注的贴吧列表（分页）
    POST https://c.tieba.baidu.com/c/c/forum/sign       单个贴吧签到

签名：sign = MD5( 按 key 升序拼接 "k=v" + "tiebaclient!!!" ).upper()

业务码（不是 HTTP 状态码）：
    error_code == "0"      签到成功（user_info.user_sign_rank 是签到排名）
    error_code == "160002" 今日已签到（幂等，不重复领）
    error_code == "340006" 该贴吧被屏蔽

设计要点：
  * 幂等：先取列表再逐个签，已签到的按「已签到」处理，不算失败
  * 节流：贴吧之间随机 1.0-2.5s，每 10 个额外 5-10s；失败的等 15s 刷新 tbs 后重试一轮
  * 脱敏：BDUSS 全程不进日志；响应体打印前抹掉凭据字段
  * 日志脱敏：**默认不打印贴吧名**，只给「序号 + 短指纹」
    （如 `[123/717 fp=a1b2c3d4]`）。失败时同样不打名字，只给指纹，
    便于跨运行关联同一个贴吧，又不会把关注列表写进日志。
    本机调试要看名字就设 `TIEBA_LOG_NAMES=1`。
    → 这样即使仓库公开，Actions 日志也不会泄露你的关注画像。
  * 尾部：推送正文与日志都带统一尾部（来源 / 运行记录 / Token 认证日期）
    —— BDUSS 是不透明凭据、没有签发时间，所以**不显示有效期**，不伪造
  * 失败会 sys.exit(1)，让工作流真正报红，不被绿勾掩盖

所有凭据走环境变量注入，脚本本身零密钥。
"""

import hashlib
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ==================== 常量：真实接口 ====================

SIGN_KEY = "tiebaclient!!!"
TBS_URL = "https://tieba.baidu.com/dc/common/tbs"
LIKE_URL = "https://c.tieba.baidu.com/c/f/forum/like"
SIGN_URL = "https://c.tieba.baidu.com/c/c/forum/sign"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/95.0.4638.69 Safari/537.36"
)

CLIENT_ID = "wappc_1534235498291_488"
CLIENT_VERSION = "9.7.8.0"

BJ_TZ = timezone(timedelta(hours=8))
TIMEOUT = 15

# 节流参数（可用环境变量覆盖）。
# 关注的贴吧越多，这两项越决定总耗时：717 个吧在 1.0-2.5s 档位下要跑约 40 分钟。
# 默认取 0.3-0.8s，请求本身的往返已经构成自然节流。
MIN_DELAY = float(os.environ.get("TIEBA_MIN_DELAY", "0.3"))
MAX_DELAY = float(os.environ.get("TIEBA_MAX_DELAY", "0.8"))
# 每 N 个贴吧额外休息一次
REST_EVERY = int(os.environ.get("TIEBA_REST_EVERY", "30"))
REST_MIN = float(os.environ.get("TIEBA_REST_MIN", "2"))
REST_MAX = float(os.environ.get("TIEBA_REST_MAX", "4"))

# 是否在日志里打印贴吧名。默认关闭——公开仓库的 Actions 日志
# 对所有登录用户可见，打名字等于公开你的关注列表。本机调试时设 1 打开。
SHOW_NAMES = os.environ.get("TIEBA_LOG_NAMES", "").strip().lower() in ("1", "true", "yes", "on")


def now_bj() -> str:
    return datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now_bj()}] {msg}", flush=True)


# ==================== 脱敏 ====================

_SENSITIVE_KEYS = {
    "bduss", "stoken", "tbs", "token", "cookie", "password",
    "secret", "authorization", "session", "ptoken",
}


def sanitize(value):
    """递归脱敏，日志里不出现任何凭据字段。"""
    if isinstance(value, dict):
        return {
            k: ("<已脱敏>" if str(k).lower() in _SENSITIVE_KEYS else sanitize(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    return value


def mask_cred(value: str) -> str:
    """只报长度，不报内容。"""
    if not value:
        return "<未配置>"
    return f"***(len={len(value)})"


def short_fp(name: str) -> str:
    """贴吧名的短指纹。稳定（同一个吧每次一样），但不可反推名字。

    用途：跨运行关联同一个贴吧、定位问题，同时不把名字写进日志。
    """
    if not name:
        return "--------"
    return hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]


def tag(name: str, idx: int, total: int) -> str:
    """条目标识。默认「序号 + 短指纹」，只有显式开启才打名字。"""
    if SHOW_NAMES and name:
        return f"【{name}】({idx}/{total})"
    return f"[{idx}/{total} fp={short_fp(name)}]"


# ==================== HTTP ====================

def http_json(url, data=None, cookie=None):
    """POST/GET 取 JSON。返回 (dict|None, http_status)。不抛异常。"""
    headers = {"User-Agent": USER_AGENT}
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if cookie:
        headers["Cookie"] = cookie

    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            return json.loads(raw), resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            return json.loads(raw), e.code
        except json.JSONDecodeError:
            return None, e.code
    except Exception as e:
        log(f"  请求异常：{type(e).__name__}: {e}")
        return None, 0


def request_with_retry(url, data=None, cookie=None, retry=3):
    """带指数退避的请求。"""
    for i in range(retry):
        result, status = http_json(url, data, cookie)
        if result is not None:
            return result
        if i < retry - 1:
            wait = 1.5 * (2 ** i) + random.uniform(0, 1)
            time.sleep(wait)
    return None


# ==================== 签名 ====================

def sign(data: dict) -> str:
    """贴吧客户端签名：MD5(升序拼接 k=v + SIGN_KEY).upper()"""
    raw = "".join(f"{k}={data[k]}" for k in sorted(data)) + SIGN_KEY
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def signed(data: dict) -> dict:
    out = dict(data)
    out["sign"] = sign(out)
    return out


# ==================== 业务 ====================

class TiebaClient:
    def __init__(self, bduss: str):
        self.bduss = bduss
        self.cookie = f"BDUSS={bduss}"

    def get_tbs(self):
        """取 tbs。BDUSS 是否有效由后续签到请求自然验证。"""
        result = request_with_retry(TBS_URL, cookie=self.cookie)
        if not result:
            return None
        return result.get("tbs", "")

    def get_favorites(self):
        """分页取关注的贴吧列表。"""
        forums, page_no = [], 1
        while True:
            data = signed({
                "BDUSS": self.bduss,
                "_client_type": "2",
                "_client_id": CLIENT_ID,
                "_client_version": CLIENT_VERSION,
                "_phone_imei": "000000000000000",
                "from": "1008621y",
                "page_no": str(page_no),
                "page_size": "200",
                "model": "MI+5",
                "net_type": "1",
                "timestamp": str(int(time.time())),
                "vcode_tag": "11",
            })
            result = request_with_retry(LIKE_URL, data)
            if not result:
                log("  获取贴吧列表失败，停止翻页")
                break

            forum_list = result.get("forum_list") or {}
            for key in ("non-gconforum", "gconforum"):
                items = forum_list.get(key, [])
                if isinstance(items, list):
                    forums.extend(items)
                elif isinstance(items, dict):
                    forums.append(items)

            if result.get("has_more") != "1":
                break
            page_no += 1
            time.sleep(random.uniform(1, 2))

        log(f"共获取到 {len(forums)} 个关注的贴吧")
        return forums

    def sign_forum(self, fid: str, name: str, tbs: str) -> dict:
        """单个贴吧签到。返回 {status, rank, message}"""
        data = signed({
            "BDUSS": self.bduss,
            "_client_type": "2",
            "_client_version": CLIENT_VERSION,
            "_phone_imei": "000000000000000",
            "model": "MI+5",
            "net_type": "1",
            "fid": fid,
            "kw": name,
            "tbs": tbs,
            "timestamp": str(int(time.time())),
        })
        result = request_with_retry(SIGN_URL, data)
        if not result:
            return {"status": "error", "rank": None, "message": "网络请求失败"}

        code = str(result.get("error_code", ""))
        msg = result.get("error_msg", "")

        if code == "0":
            rank = (result.get("user_info") or {}).get("user_sign_rank")
            return {"status": "success", "rank": int(rank) if rank else None,
                    "message": "签到成功"}
        if code == "160002":
            return {"status": "exist", "rank": None, "message": msg or "今日已签到"}
        if code == "340006":
            return {"status": "shield", "rank": None, "message": "贴吧已被屏蔽"}
        return {"status": "error", "rank": None,
                "message": f"{msg or '未知错误'}(code={code})"}


# ==================== 状态文件（首次认证日期） ====================

def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_state(path: str, state: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # 只读环境下放弃持久化，下次运行会重新记录


def auth_date(bduss: str, path: str) -> str:
    """BDUSS 是不透明凭据，没有签发时间。

    取「该凭据首次在本流水线认证成功的日期」，写进状态文件跨运行保留。
    只存 SHA-256 短指纹，不落明文。
    """
    fp = hashlib.sha256(bduss.encode("utf-8")).hexdigest()[:12]
    state = load_state(path)
    creds = state.get("credentials") or {}
    if creds.get(fp):
        return creds[fp]
    today = datetime.now(BJ_TZ).strftime("%Y-%m-%d")
    creds[fp] = today
    state["credentials"] = creds
    save_state(path, state)
    return today


# ==================== 推送 ====================

def build_footer(bduss: str, state_path: str) -> str:
    """统一尾部：来源 + 运行记录 + Token 认证日期。

    BDUSS 解析不出有效期，按规则**不伪造**，所以这里没有「凭证有效期至」一行。
    """
    lines = []
    if os.environ.get("GITHUB_ACTIONS") == "true":
        wf = os.environ.get("GITHUB_WORKFLOW") or ""
        lines.append(f"来源：GitHub Actions{' · ' + wf if wf else ''}")
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        if repo and run_id:
            base = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
            lines.append(f"运行记录：{base}/{repo}/actions/runs/{run_id}")
    else:
        lines.append("来源：本地运行")

    if bduss:
        lines.append(f"Token 认证日期：{auth_date(bduss, state_path)}")
    return "\n".join(lines)


def push_notify(title: str, content: str, bduss: str, state_path: str) -> bool:
    """PushPlus 推送。token 走 POST body，不进 URL（URL 会进日志）。"""
    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    body = f"{content}\n\n{'-' * 22}\n{build_footer(bduss, state_path)}"

    # 尾部同时 log 一份，否则只能在推送里看到、CI 日志里没法验证
    log(f"推送正文:\n{body}")

    if not token:
        log("[推送] 未配置 PUSHPLUS_TOKEN，跳过推送")
        return False

    result, status = http_json(
        "https://www.pushplus.plus/send",
        data={"token": token, "title": title, "content": body, "template": "txt"},
    )
    ok = status == 200 and isinstance(result, dict) and result.get("code") == 200
    log(f"[推送{'成功' if ok else '失败'}] {title}"
        + ("" if ok else f" resp={sanitize(result)}"))
    return ok


# ==================== 主流程 ====================

def main() -> int:
    log("=== 百度贴吧自动签到 ===")

    bduss = os.environ.get("TIEBA_BDUSS", "").strip()
    state_path = os.environ.get("TIEBA_STATE_FILE", ".tieba-state.json")

    if not bduss:
        push_notify("❌ 贴吧签到失败", "未配置 TIEBA_BDUSS，请在仓库 Secrets 中添加。",
                    "", state_path)
        return 1

    log(f"凭据检查：BDUSS={mask_cred(bduss)}")
    client = TiebaClient(bduss)

    tbs = client.get_tbs()
    if not tbs:
        push_notify("❌ 贴吧签到失败",
                    "获取 tbs 失败，通常意味着 BDUSS 已失效。请重新从浏览器 F12 取一次。",
                    bduss, state_path)
        return 1
    log("tbs 获取成功")

    forums = client.get_favorites()
    if not forums:
        push_notify("⚠️ 贴吧签到", "未获取到关注的贴吧（可能 BDUSS 失效或未关注任何贴吧）。",
                    bduss, state_path)
        return 1

    total = len(forums)
    stats = {"success": 0, "exist": 0, "shield": 0, "error": 0}
    failed = []

    est = total * ((MIN_DELAY + MAX_DELAY) / 2 + 1.5) / 60
    log(f"开始第 1 轮签到，共 {total} 个贴吧（预计约 {est:.0f} 分钟）")

    for idx, forum in enumerate(forums):
        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
        if (idx + 1) % REST_EVERY == 0:
            rest = random.uniform(REST_MIN, REST_MAX)
            log(f"  已签到 {idx + 1}/{total}，休息 {rest:.1f}s")
            time.sleep(rest)

        name = forum.get("name", "")
        res = client.sign_forum(forum.get("id", ""), name, tbs)
        stats[res["status"]] += 1

        mark = tag(name, idx + 1, total)
        if res["status"] == "success":
            rank_str = f"，第 {res['rank']} 个签到" if res["rank"] else ""
            log(f"  {mark} 签到成功{rank_str}")
        elif res["status"] == "exist":
            log(f"  {mark} 已签到")
        elif res["status"] == "shield":
            log(f"  {mark} 被屏蔽")
        else:
            # 失败也只给指纹，不打名字
            log(f"  {mark} 失败：{res['message']}")
            failed.append(forum)

    # 第二轮：刷新 tbs 后重试失败的
    final_failed = []
    if failed:
        log(f"第 1 轮结束，{len(failed)} 个失败，等待 15s 后刷新 tbs 重试")
        time.sleep(15)
        new_tbs = client.get_tbs()
        if new_tbs:
            tbs = new_tbs
            log("  已刷新 tbs")

        for idx, forum in enumerate(failed):
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
            name = forum.get("name", "")
            res = client.sign_forum(forum.get("id", ""), name, tbs)
            mark = tag(name, idx + 1, len(failed))
            if res["status"] == "success":
                stats["error"] -= 1
                stats["success"] += 1
                log(f"  重试 {mark} 成功")
            elif res["status"] == "exist":
                stats["error"] -= 1
                stats["exist"] += 1
                log(f"  重试 {mark} 已签到")
            elif res["status"] == "shield":
                stats["error"] -= 1
                stats["shield"] += 1
                log(f"  重试 {mark} 被屏蔽")
            else:
                final_failed.append(short_fp(name))
                log(f"  重试 {mark} 仍失败：{res['message']}")

    lines = [
        f"贴吧总数：{total}",
        f"签到成功：{stats['success']}",
        f"已经签到：{stats['exist']}",
        f"被屏蔽的：{stats['shield']}",
        f"签到失败：{stats['error']}",
    ]
    if final_failed:
        # 只给指纹，不给名字（想定位就本机设 TIEBA_LOG_NAMES=1 重跑）
        lines.append(f"重试失败的条目指纹：{', '.join(final_failed)}")
        lines.append("（指纹是贴吧名的短哈希，本机设 TIEBA_LOG_NAMES=1 可显示名字）")
    summary = "\n".join(lines)
    log("========== 签到汇总 ==========\n" + summary + "\n==============================")

    # 已签到 / 被屏蔽 都属于正常结果，不算失败
    if stats["error"] > 0:
        push_notify("❌ 贴吧签到异常", summary + "\n\n请查看运行日志。", bduss, state_path)
        return 1

    push_notify("✅ 贴吧签到完成", summary, bduss, state_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
