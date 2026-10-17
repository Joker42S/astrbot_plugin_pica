"""排行榜订阅的权限门槛（sub_admin_only）测试。

锁定的行为契约：
1. 默认只允许 `admin_ids` 中的用户添加 / 取消订阅；
2. `/picasub list` 是只读的，对所有人开放；
3. 未配置 `admin_ids` 时给出明确提示，而不是静默放行；
4. `sub_admin_only=false` 时恢复「任意群员可订阅」的旧行为；
5. 订阅状态里会带上每次推送的条数，便于解释「命令 10 条 / 推送 30 条」。
"""

import asyncio

from conftest import import_plugin_main

main = import_plugin_main()
PicaPlugin = main.PicaPlugin
PicaDownloader = main.PicaDownloader
RankHistoryStore = main.RankHistoryStore
RankSubscriptionStore = main.RankSubscriptionStore

UMO = "default:group:100"
ADMIN = "10000"
MEMBER = "20000"


class _FakeClient:
    async def leaderboard(self, tt="H24", token=None):
        return []

    def set_current_user(self, user_id):
        pass


class _FakeAuth:
    async def ensure_login(self, user_id):
        raise main.PicaError("未绑定")

    async def ensure_default_login(self):
        return "token"


class _FakeContext:
    async def send_message(self, umo, chain):
        return True

    def get_config(self, umo=None):
        return {"platform_settings": {"forward_threshold": 1500}}


class _FakeEvent:
    def __init__(self, sender_id=MEMBER, umo=UMO):
        self._sender_id = sender_id
        self.unified_msg_origin = umo
        self.results = []

    def get_sender_id(self):
        return self._sender_id

    def plain_result(self, text):
        self.results.append(text)
        return main.MessageChain([main.Comp.Plain(text)])


def _make_plugin(tmp_path, **config):
    plugin = PicaPlugin.__new__(PicaPlugin)
    plugin.config = {"page_size": 10, **config}
    plugin.context = _FakeContext()
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


async def _collect(plugin, event, arg):
    return [item async for item in plugin.subscribe_command(event, arg)]


def _sub(plugin, event, arg=None):
    return asyncio.run(_collect(plugin, event, arg))


def _reply(event):
    return "\n".join(event.results)


# ------------------------------------------------------------------ 默认收紧


def test_member_cannot_subscribe_by_default(tmp_path):
    plugin = _make_plugin(tmp_path, admin_ids=ADMIN)
    event = _FakeEvent(MEMBER)

    _sub(plugin, event, "H24")

    assert "只有管理员" in _reply(event)
    assert plugin.subs.targets(UMO) == []


def test_admin_can_subscribe(tmp_path):
    plugin = _make_plugin(tmp_path, admin_ids=ADMIN)
    event = _FakeEvent(ADMIN)

    _sub(plugin, event, "H24")

    assert plugin.subs.targets(UMO) == ["H24"]
    assert "已为本群订阅" in _reply(event)


def test_member_cannot_unsubscribe(tmp_path):
    plugin = _make_plugin(tmp_path, admin_ids=ADMIN)
    plugin.subs.add(UMO, "H24", ADMIN)
    event = _FakeEvent(MEMBER)

    _sub(plugin, event, "off")

    assert "只有管理员" in _reply(event)
    assert plugin.subs.targets(UMO) == ["H24"]


def test_admin_can_unsubscribe(tmp_path):
    plugin = _make_plugin(tmp_path, admin_ids=ADMIN)
    plugin.subs.add(UMO, "H24", ADMIN)
    event = _FakeEvent(ADMIN)

    _sub(plugin, event, "off")

    assert plugin.subs.targets(UMO) == []


# ------------------------------------------------------------------ 只读不受限


def test_list_is_open_to_everyone(tmp_path):
    plugin = _make_plugin(tmp_path, admin_ids=ADMIN)
    event = _FakeEvent(MEMBER)

    _sub(plugin, event, "list")

    assert "本群排行榜订阅" in _reply(event)


# ------------------------------------------------------------------ 配置边界


def test_empty_admin_ids_gives_clear_hint(tmp_path):
    plugin = _make_plugin(tmp_path, admin_ids="")
    event = _FakeEvent(MEMBER)

    _sub(plugin, event, "H24")

    assert "未配置 admin_ids" in _reply(event)
    assert plugin.subs.targets(UMO) == []


def test_open_mode_allows_member(tmp_path):
    plugin = _make_plugin(tmp_path, admin_ids="", sub_admin_only=False)
    event = _FakeEvent(MEMBER)

    _sub(plugin, event, "H24")

    assert plugin.subs.targets(UMO) == ["H24"]


# ------------------------------------------------------------------ 状态文本


def test_status_text_shows_push_count(tmp_path):
    plugin = _make_plugin(tmp_path, admin_ids=ADMIN, page_size=10, rank_push_size=30)
    plugin.subs.add(UMO, "H24", ADMIN)
    event = _FakeEvent(ADMIN)

    _sub(plugin, event, "list")

    assert "推送 30 条" in _reply(event)
