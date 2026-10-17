"""排行榜「新上榜」提示测试（配置项 rank_new_highlight，默认开启）。

锁定的行为契约：
1. 每次 `/picarank` 都按榜单类型保存快照到持久化目录（rank_history/）；
2. 下一次发送时对比快照，额外发一条高亮消息（在榜单文本之后）；
3. 榜单为空时不覆盖快照 —— 否则下次会把整榜都当成「新上榜」；
4. 只有用户看得见的名次（page_size 以内）才会被高亮，但全量榜单仍进快照；
5. `rank_new_highlight=false` 时不再提示，快照仍会更新（保留对比基准）；
6. 快照文件损坏不影响排行榜命令本身。
"""

import asyncio
import json

from conftest import import_plugin_main

main = import_plugin_main()
PicaPlugin = main.PicaPlugin
MessageChain = main.MessageChain
Comp = main.Comp
# 与插件内部保持同一份模块对象（conftest 载入的是 astrbot_plugin_pica.core.*）
PicaDownloader = main.PicaDownloader
RankHistoryStore = main.RankHistoryStore
RankSubscriptionStore = main.RankSubscriptionStore

UMO = "default:group:100"


def _comics(*ids):
    return [{"_id": cid, "title": f"本子{cid}", "totalLikes": 5} for cid in ids]


class _FakeClient:
    """只实现榜单拉取（封面 PDF 默认关闭，用不到图片下载）"""

    def __init__(self, board=None):
        self.board = _comics("a", "b") if board is None else board
        self.fetches = []
        self.user_id = None

    async def leaderboard(self, tt="H24", token=None):
        self.fetches.append(tt)
        return list(self.board)

    def set_current_user(self, user_id):
        self.user_id = user_id


class _FakeAuth:
    async def ensure_login(self, user_id):
        return "token"


class _FakeEvent:
    def __init__(self, sender_id="10000"):
        self._sender_id = sender_id
        self.unified_msg_origin = UMO
        self.bot = None
        self.results = []

    def get_sender_id(self):
        return self._sender_id

    def plain_result(self, text):
        self.results.append(text)
        return MessageChain([Comp.Plain(text)])

    def chain_result(self, chain):
        self.results.append("")
        return MessageChain(list(chain))


class _FakeContext:
    def __init__(self):
        self.calls = []

    async def send_message(self, umo, chain):
        self.calls.append((umo, chain))
        return True


def _make_plugin(tmp_path, config=None, board=None):
    plugin = PicaPlugin.__new__(PicaPlugin)
    plugin.config = config if config is not None else {}
    plugin.context = _FakeContext()
    plugin.data_dir = tmp_path
    plugin.auth = _FakeAuth()
    plugin.client = _FakeClient(board)
    plugin.downloader = PicaDownloader(plugin.client, tmp_path)
    plugin.rank_history = RankHistoryStore(tmp_path)
    plugin.subs = RankSubscriptionStore(tmp_path)
    return plugin


def _run_rank(plugin, event, tt="H24"):
    async def _collect():
        return [item async for item in plugin.rank_command(event, tt)]

    return asyncio.run(_collect())


def _snapshot(tmp_path, tt="H24"):
    path = tmp_path / "rank_history" / f"{tt}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _ids(snapshot):
    return [item["_id"] for item in snapshot["items"]]


def _joined(results):
    return "\n".join(results)


# ------------------------------------------------------------------ 快照落盘


def test_first_run_saves_snapshot_without_highlight(tmp_path):
    plugin = _make_plugin(tmp_path)

    event = _FakeEvent()
    _run_rank(plugin, event)

    assert _ids(_snapshot(tmp_path)) == ["a", "b"]
    assert "新上榜" not in _joined(event.results)
    assert "🏆 哔咔排行榜" in event.results[-1]


def test_empty_board_does_not_overwrite_snapshot(tmp_path):
    plugin = _make_plugin(tmp_path)
    _run_rank(plugin, _FakeEvent())

    plugin.client.board = []
    _run_rank(plugin, _FakeEvent())

    # 空榜很可能是接口抖动，快照必须保持不变
    assert _ids(_snapshot(tmp_path)) == ["a", "b"]


def test_corrupt_snapshot_does_not_break_rank_command(tmp_path):
    (tmp_path / "rank_history").mkdir()
    (tmp_path / "rank_history" / "H24.json").write_text("{ 坏文件", encoding="utf-8")
    plugin = _make_plugin(tmp_path)

    event = _FakeEvent()
    _run_rank(plugin, event)

    assert "🏆 哔咔排行榜" in event.results[-1]
    assert "新上榜" not in _joined(event.results)
    assert _ids(_snapshot(tmp_path)) == ["a", "b"]


# ------------------------------------------------------------------ 新上榜提示


def test_second_run_highlights_new_entry_after_board(tmp_path):
    plugin = _make_plugin(tmp_path)
    _run_rank(plugin, _FakeEvent())

    plugin.client.board = _comics("a", "c", "b")
    event = _FakeEvent()
    _run_rank(plugin, event)

    highlights = [text for text in event.results if "新上榜" in text]
    assert len(highlights) == 1
    assert "本子c" in highlights[0]
    assert "#2" in highlights[0]
    assert "与上次榜单" in highlights[0]
    # 高亮必须排在榜单文本之后，用户先看到榜单再看新上榜
    assert event.results[-1] == highlights[0]


def test_no_highlight_when_board_unchanged(tmp_path):
    plugin = _make_plugin(tmp_path)
    _run_rank(plugin, _FakeEvent())

    event = _FakeEvent()
    _run_rank(plugin, event)

    assert "新上榜" not in _joined(event.results)


def test_new_entry_beyond_page_size_is_not_highlighted(tmp_path):
    plugin = _make_plugin(tmp_path, {"page_size": 2})
    _run_rank(plugin, _FakeEvent())

    plugin.client.board = _comics("a", "b", "c")
    event = _FakeEvent()
    _run_rank(plugin, event)

    assert "新上榜" not in _joined(event.results)
    # 全量榜单仍然进快照，避免 c 升到前列时又被算成新上榜
    assert _ids(_snapshot(tmp_path)) == ["a", "b", "c"]


def test_highlight_is_isolated_per_rank_type(tmp_path):
    plugin = _make_plugin(tmp_path)
    _run_rank(plugin, _FakeEvent(), tt="H24")

    event = _FakeEvent()
    _run_rank(plugin, event, tt="D7")

    assert "新上榜" not in _joined(event.results)
    assert _ids(_snapshot(tmp_path, "D7")) == ["a", "b"]


def test_invalid_rank_type_falls_back_to_h24(tmp_path):
    plugin = _make_plugin(tmp_path)

    event = _FakeEvent()
    _run_rank(plugin, event, tt="whatever")

    assert plugin.client.fetches == ["H24"]
    assert _ids(_snapshot(tmp_path, "H24")) == ["a", "b"]


def test_highlight_can_be_disabled_but_snapshot_still_updates(tmp_path):
    plugin = _make_plugin(tmp_path, {"rank_new_highlight": False})
    _run_rank(plugin, _FakeEvent())

    plugin.client.board = _comics("a", "c", "b")
    event = _FakeEvent()
    _run_rank(plugin, event)

    assert "新上榜" not in _joined(event.results)
    assert _ids(_snapshot(tmp_path)) == ["a", "c", "b"]
