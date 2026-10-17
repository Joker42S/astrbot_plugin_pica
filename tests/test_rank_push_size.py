"""定时推送条数与命令条数解耦（rank_push_size）测试。

锁定的行为契约：
1. `rank_push_size` 只影响 `/picasub` 定时推送，不影响 `/picarank` 手动查看；
2. `rank_push_size` 为 0 / 负数 / 非法值时跟随 `page_size`；
3. 截断发生在展示层，快照仍写全量榜单（避免调整条数后误报新上榜）。
"""

import asyncio

from conftest import import_plugin_main

main = import_plugin_main()
PicaPlugin = main.PicaPlugin
PicaDownloader = main.PicaDownloader
RankHistoryStore = main.RankHistoryStore
RankSubscriptionStore = main.RankSubscriptionStore

UMO = "default:group:100"


def _comics(n):
    return [
        {"_id": f"c{i:03d}", "title": f"标题{i}", "totalLikes": i}
        for i in range(1, n + 1)
    ]


class _FakeClient:
    def __init__(self, board):
        self.board = board
        self.fetches = 0
        self.user_id = None

    async def leaderboard(self, tt="H24", token=None):
        self.fetches += 1
        return list(self.board)

    def set_current_user(self, user_id):
        self.user_id = user_id


class _FakeAuth:
    async def ensure_login(self, user_id):
        raise main.PicaError(f"用户 {user_id} 未绑定账号")

    async def ensure_default_login(self):
        return "token"


def _plain_text(comp):
    args = getattr(comp, "args", ())
    return str(args[0]) if args else str(getattr(comp, "kwargs", {}).get("text", ""))


class _FakeContext:
    def __init__(self):
        self.calls = []

    def get_config(self, umo=None):
        return {"platform_settings": {"forward_threshold": 1500}}

    async def send_message(self, umo, chain):
        self.calls.append((umo, chain))
        return True

    def texts_for(self, umo):
        out = []
        for target, chain in self.calls:
            if target != umo:
                continue
            for comp in chain.chain:
                if isinstance(comp, main.Comp.Plain):
                    out.append(_plain_text(comp))
        return out


def _make_plugin(tmp_path, config, board_size=40):
    plugin = PicaPlugin.__new__(PicaPlugin)
    plugin.config = config
    plugin.context = _FakeContext()
    plugin.data_dir = tmp_path
    plugin.auth = _FakeAuth()
    plugin.client = _FakeClient(_comics(board_size))
    plugin.downloader = PicaDownloader(plugin.client, tmp_path)
    plugin.rank_history = RankHistoryStore(tmp_path)
    plugin.subs = RankSubscriptionStore(tmp_path)
    plugin._rank_fired = {}
    plugin._cron_warned = {}
    plugin._bot_accounts = {}
    return plugin


def _payload(plugin, limit=None):
    if limit is None:
        return asyncio.run(plugin._rank_payload("H24", "token"))
    return asyncio.run(plugin._rank_payload("H24", "token", limit=limit))


# ------------------------------------------------------------------ _push_size


def test_push_size_follows_page_size_when_zero(tmp_path):
    plugin = _make_plugin(tmp_path, {"page_size": 10, "rank_push_size": 0})
    assert plugin._push_size() == 10


def test_push_size_follows_page_size_when_missing(tmp_path):
    plugin = _make_plugin(tmp_path, {"page_size": 15})
    assert plugin._push_size() == 15


def test_push_size_overrides_page_size(tmp_path):
    plugin = _make_plugin(tmp_path, {"page_size": 10, "rank_push_size": 30})
    assert plugin._push_size() == 30


def test_push_size_ignores_invalid_values(tmp_path):
    for bad in (-3, "abc", None):
        plugin = _make_plugin(tmp_path, {"page_size": 8, "rank_push_size": bad})
        assert plugin._push_size() == 8


# ------------------------------------------------------------------ _rank_payload


def test_command_payload_uses_page_size(tmp_path):
    plugin = _make_plugin(tmp_path, {"page_size": 10, "rank_push_size": 30})
    comics, _ = _payload(plugin)
    assert len(comics) == 10


def test_push_payload_uses_rank_push_size(tmp_path):
    plugin = _make_plugin(tmp_path, {"page_size": 10, "rank_push_size": 30})
    comics, _ = _payload(plugin, limit=plugin._push_size())
    assert len(comics) == 30


def test_limit_does_not_shrink_snapshot(tmp_path):
    plugin = _make_plugin(tmp_path, {"page_size": 10, "rank_push_size": 30})
    _payload(plugin, limit=30)
    saved = plugin.rank_history.load("H24")
    assert len(saved["items"]) == 40  # 快照仍是全量


def test_push_sends_more_entries_than_command(tmp_path):
    plugin = _make_plugin(tmp_path, {"page_size": 10, "rank_push_size": 30})
    plugin.subs.add(UMO, "H24", "10000")
    asyncio.run(plugin._push_rank_to_groups("H24", [UMO]))
    texts = plugin.context.texts_for(UMO)
    assert texts, "推送应至少发出一条文本"
    assert texts[0].count("ID:") == 30
