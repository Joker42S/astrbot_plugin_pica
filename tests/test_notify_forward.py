"""主动推送长文本的「合并转发」折叠测试。

背景：`_notify` 走 `context.send_message`，绕过了 AstrBot 的响应管线，
因此管线里那层处理（`ResultDecorateStage`：aiocqhttp 平台下纯文本超过
`platform_settings.forward_threshold` 时整条消息折叠成一个 Node，
即 QQ 的「合并转发」）不会生效；超长纯文本在部分协议端无法直接送达。
插件因此自行复刻了这层规则。

本文件锁定该行为：
1. 不超过阈值 → 仍按普通文本发送，且**不**去查机器人账号（行为不变）；
2. 超过阈值 → 折叠成 Node，节点里是完整原文（不截断）；
3. 阈值取自该会话的 `platform_settings.forward_threshold`，异常值回落默认 1500；
4. 取不到机器人账号（无平台实例 / 接口报错）时退化成普通文本，不抛异常；
5. 账号信息按平台缓存，只查一次；
6. 推送链路整体生效（榜单文本被折叠），且发送失败仍不影响调用方。
"""

import asyncio
import types
from datetime import datetime

from conftest import import_plugin_main

main = import_plugin_main()
PicaPlugin = main.PicaPlugin
MessageChain = main.MessageChain
Comp = main.Comp
PicaDownloader = main.PicaDownloader
RankHistoryStore = main.RankHistoryStore
RankSubscriptionStore = main.RankSubscriptionStore

UMO = "default:group:100"
MONDAY = datetime(2026, 8, 3, 9, 0)

LONG_TEXT = "长" * 1600
SHORT_TEXT = "短" * 20


# --------------------------------------------------------------------- 测试替身


class _FakeBot:
    """模拟 aiocqhttp 的 bot（`__getattr__` 动态暴露 OneBot API）"""

    def __init__(self, user_id=12345, nickname="测试bot", error=None):
        self.user_id = user_id
        self.nickname = nickname
        self.error = error
        self.calls = 0

    async def get_login_info(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"user_id": self.user_id, "nickname": self.nickname}


class _FakePlatform:
    def __init__(self, platform_id, bot):
        self._platform_id = platform_id
        self.bot = bot

    def meta(self):
        return types.SimpleNamespace(id=self._platform_id)


class _FakeContext:
    """记录发送内容；`get_config` 模拟会话级 platform_settings"""

    def __init__(self, threshold=1500, platforms=None, fail_send=False):
        self.calls = []
        self.threshold = threshold
        self.fail_send = fail_send
        self.platform_manager = types.SimpleNamespace(platform_insts=platforms or [])

    def get_config(self, umo=None):
        return {"platform_settings": {"forward_threshold": self.threshold}}

    async def send_message(self, umo, chain):
        self.calls.append((umo, chain))
        if self.fail_send:
            raise RuntimeError("send failed")
        return True

    @property
    def chains(self):
        return [chain for _umo, chain in self.calls]


class _NoConfigContext(_FakeContext):
    """没有 get_config 的上下文（取阈值应回落默认值）"""

    def get_config(self, umo=None):
        raise AttributeError("no get_config here")


class _FakeClient:
    def __init__(self):
        self.user_id = None

    async def leaderboard(self, tt="H24", token=None):
        return [
            {"_id": f"c{i}", "title": f"本子标题{i}" * 4, "totalLikes": 1}
            for i in range(3)
        ]

    def set_current_user(self, user_id):
        self.user_id = user_id


class _FakeAuth:
    async def ensure_login(self, user_id):
        return "tk"


# --------------------------------------------------------------------- 构造


def _make_plugin(tmp_path, context, config=None):
    plugin = PicaPlugin.__new__(PicaPlugin)
    plugin.config = config if config is not None else {}
    plugin.context = context
    plugin.data_dir = tmp_path
    plugin.auth = _FakeAuth()
    plugin.client = _FakeClient()
    plugin.downloader = PicaDownloader(plugin.client, tmp_path)
    plugin.rank_history = RankHistoryStore(tmp_path)
    plugin.subs = RankSubscriptionStore(tmp_path)
    plugin._rank_fired = {}
    plugin._cron_warned = {}
    plugin._bot_accounts = {}
    return plugin


def _build(plugin, umo, text):
    return asyncio.run(plugin._build_notify_chain(umo, text))


def _notify(plugin, umo, text):
    return asyncio.run(plugin._notify(umo, text))


def _node_of(chain):
    return next((c for c in chain.chain if isinstance(c, Comp.Node)), None)


def _plain_of(chain):
    return next((c for c in chain.chain if isinstance(c, Comp.Plain)), None)


def _node_text(node) -> str:
    return node.kwargs["content"][0].args[0]


# --------------------------------------------------------------- 短文本不变


def test_short_text_stays_plain_and_skips_identity_lookup(tmp_path):
    bot = _FakeBot()
    ctx = _FakeContext(platforms=[_FakePlatform("default", bot)])
    plugin = _make_plugin(tmp_path, ctx)

    chain = _build(plugin, UMO, SHORT_TEXT)

    assert _node_of(chain) is None
    assert _plain_of(chain).args[0] == SHORT_TEXT
    assert bot.calls == 0, "短文本不该去查机器人账号"


def test_text_exactly_at_threshold_stays_plain(tmp_path):
    ctx = _FakeContext(threshold=100)
    plugin = _make_plugin(tmp_path, ctx)

    chain = _build(plugin, UMO, "字" * 100)

    assert _node_of(chain) is None


# ----------------------------------------------------------------- 长文本折叠


def test_long_text_is_folded_into_forward_node(tmp_path):
    bot = _FakeBot(user_id=987654321, nickname="哔咔bot")
    ctx = _FakeContext(platforms=[_FakePlatform("default", bot)])
    plugin = _make_plugin(tmp_path, ctx)

    chain = _build(plugin, UMO, LONG_TEXT)

    node = _node_of(chain)
    assert node is not None
    assert len(chain.chain) == 1, "折叠后整条消息只应有一个 Node"
    assert node.kwargs["uin"] == "987654321"
    assert node.kwargs["name"] == "哔咔bot"
    assert _node_text(node) == LONG_TEXT, "节点里必须是完整原文，不能截断"


def test_fold_uses_session_threshold(tmp_path):
    bot = _FakeBot()
    ctx = _FakeContext(threshold=50, platforms=[_FakePlatform("default", bot)])
    plugin = _make_plugin(tmp_path, ctx)

    chain = _build(plugin, UMO, "字" * 51)

    assert _node_of(chain) is not None


def test_threshold_defaults_when_config_unavailable(tmp_path):
    bot = _FakeBot()
    ctx = _NoConfigContext(platforms=[_FakePlatform("default", bot)])
    plugin = _make_plugin(tmp_path, ctx)

    assert plugin._forward_threshold(UMO) == 1500
    assert _node_of(_build(plugin, UMO, "字" * 1500)) is None
    assert _node_of(_build(plugin, UMO, "字" * 1501)) is not None


def test_invalid_threshold_falls_back_to_default(tmp_path):
    ctx = _FakeContext(threshold="不是数字")
    plugin = _make_plugin(tmp_path, ctx)

    assert plugin._forward_threshold(UMO) == 1500


def test_nickname_falls_back_when_empty(tmp_path):
    bot = _FakeBot(nickname="")
    ctx = _FakeContext(platforms=[_FakePlatform("default", bot)])
    plugin = _make_plugin(tmp_path, ctx)

    node = _node_of(_build(plugin, UMO, LONG_TEXT))

    assert node.kwargs["name"] == "AstrBot"


# --------------------------------------------------------- 取不到账号时退化


def test_falls_back_to_plain_without_platform(tmp_path):
    ctx = _FakeContext(platforms=[])
    plugin = _make_plugin(tmp_path, ctx)

    chain = _build(plugin, UMO, LONG_TEXT)

    assert _node_of(chain) is None
    assert _plain_of(chain).args[0] == LONG_TEXT


def test_falls_back_to_plain_when_login_info_fails(tmp_path):
    bot = _FakeBot(error=RuntimeError("接口挂了"))
    ctx = _FakeContext(platforms=[_FakePlatform("default", bot)])
    plugin = _make_plugin(tmp_path, ctx)

    chain = _build(plugin, UMO, LONG_TEXT)

    assert _node_of(chain) is None
    assert _plain_of(chain).args[0] == LONG_TEXT


def test_falls_back_to_plain_when_login_info_has_no_user_id(tmp_path):
    bot = _FakeBot(user_id="")
    ctx = _FakeContext(platforms=[_FakePlatform("default", bot)])
    plugin = _make_plugin(tmp_path, ctx)

    assert _node_of(_build(plugin, UMO, LONG_TEXT)) is None


def test_platform_lookup_ignores_other_platforms(tmp_path):
    bot = _FakeBot()
    ctx = _FakeContext(platforms=[_FakePlatform("other", bot)])
    plugin = _make_plugin(tmp_path, ctx)

    assert _node_of(_build(plugin, UMO, LONG_TEXT)) is None
    assert bot.calls == 0


# --------------------------------------------------------------------- 缓存


def test_bot_identity_is_cached_per_platform(tmp_path):
    bot = _FakeBot()
    ctx = _FakeContext(platforms=[_FakePlatform("default", bot)])
    plugin = _make_plugin(tmp_path, ctx)

    _build(plugin, UMO, LONG_TEXT)
    _build(plugin, "default:group:200", LONG_TEXT)

    assert bot.calls == 1


# ------------------------------------------------------------------- 发送行为


def test_notify_sends_folded_chain(tmp_path):
    bot = _FakeBot()
    ctx = _FakeContext(platforms=[_FakePlatform("default", bot)])
    plugin = _make_plugin(tmp_path, ctx)

    _notify(plugin, UMO, LONG_TEXT)

    assert len(ctx.chains) == 1
    assert _node_of(ctx.chains[0]) is not None


def test_notify_swallows_send_errors(tmp_path):
    bot = _FakeBot()
    ctx = _FakeContext(platforms=[_FakePlatform("default", bot)], fail_send=True)
    plugin = _make_plugin(tmp_path, ctx)

    _notify(plugin, UMO, LONG_TEXT)  # 不应抛异常

    assert len(ctx.chains) == 1


class _RecordingLogger:
    def __init__(self):
        self.records = []

    def _add(self, level, msg):
        self.records.append((level, str(msg)))

    def debug(self, msg="", *args, **kwargs):
        self._add("debug", msg)

    def info(self, msg="", *args, **kwargs):
        self._add("info", msg)

    def warning(self, msg="", *args, **kwargs):
        self._add("warning", msg)

    def error(self, msg="", *args, **kwargs):
        self._add("error", msg)

    def levels(self):
        return [level for level, _msg in self.records]


def test_folded_send_failure_is_warned(tmp_path, monkeypatch):
    """折叠后发送失败意味着用户什么都收不到，必须留下告警"""
    recorder = _RecordingLogger()
    monkeypatch.setattr(main, "logger", recorder)
    bot = _FakeBot()
    ctx = _FakeContext(platforms=[_FakePlatform("default", bot)], fail_send=True)
    plugin = _make_plugin(tmp_path, ctx)

    _notify(plugin, UMO, LONG_TEXT)

    assert "warning" in recorder.levels()
    assert any("合并转发" in msg for _level, msg in recorder.records)


def test_plain_send_failure_stays_debug(tmp_path, monkeypatch):
    """普通文本的失败仍是「尽力而为」，不刷 warning"""
    recorder = _RecordingLogger()
    monkeypatch.setattr(main, "logger", recorder)
    ctx = _FakeContext(fail_send=True)
    plugin = _make_plugin(tmp_path, ctx)

    _notify(plugin, UMO, SHORT_TEXT)

    assert recorder.levels() == ["debug"]


def test_subscription_push_folds_long_leaderboard(tmp_path):
    """推送链路整体生效：超长榜单文本被折叠，短提示仍是普通文本"""
    bot = _FakeBot()
    ctx = _FakeContext(threshold=100, platforms=[_FakePlatform("default", bot)])
    plugin = _make_plugin(tmp_path, ctx, {"rank_cron_h24": "0 9 * * *"})
    plugin.subs.add(UMO, "H24", "u1")

    pushed = asyncio.run(plugin._run_rank_subscriptions(MONDAY))

    assert pushed == 1
    chains = ctx.chains
    assert len(chains) == 1
    node = _node_of(chains[0])
    assert node is not None
    assert "哔咔排行榜" in _node_text(node)
