"""排行榜封面 PDF 功能测试（配置项 rank_cover_pdf，默认关闭）。

锁定的行为契约：
1. `/picarank` 在开关**关闭**（默认）时不下载任何封面、不生成 PDF；
2. 开关**开启**时按榜单顺序下载全部封面，打包成一个 PDF 一并发送；
3. 单张封面下载失败只跳过该张，不影响其余封面与排行榜文本；
4. 封面文件名带序号，使打包器按自然顺序收集出的**页序与榜单一致**；
5. 封面 PDF 属于上传类消息，上传超时后不重发（沿用 v1.4.1 的修复语义）；
6. 封面环节的任何意外都只提示用户，不会影响排行榜文本本身。
"""

import asyncio
import io

import pytest
from PIL import Image

from conftest import import_plugin_main
from core.downloader import PicaDownloader
from core.packer import _collect_images_sorted

main = import_plugin_main()
PicaPlugin = main.PicaPlugin
MessageChain = main.MessageChain
Comp = main.Comp

# 会话标识占位符：UMO 在本测试中仅作字符串透传
UMO = "default:private:10000"


def _jpeg(width: int, height: int = 20) -> bytes:
    """生成一张合法 JPEG；宽度随榜单名次递增，用于验证 PDF 页序"""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, format="JPEG")
    return buf.getvalue()


# 第 i 名封面宽度 = 10 * i 像素（96dpi，PDF 中为 0.75 倍 pt）
_COVER_WIDTH_STEP = 10


# --------------------------------------------------------------------- 测试替身


def _comics(count: int = 3) -> list[dict]:
    return [
        {
            "_id": f"c{i}",
            "title": f"本子{i}",
            "thumb": {"fileServer": "https://fs.picacomic.com", "path": f"c{i}.jpg"},
        }
        for i in range(1, count + 1)
    ]


class _FakeClient:
    """同时充当 API 客户端与图片下载通道"""

    def __init__(self, comics: list = None, fail_ids=()):
        self.comics = _comics() if comics is None else comics
        self.fail_ids = set(fail_ids)
        self.user_id = None

    async def leaderboard(self, tt: str = "H24", token: str = None) -> list:
        return list(self.comics)

    def set_current_user(self, user_id: str) -> None:
        self.user_id = user_id

    async def _download(self, url, token=None, timeout=60, _retry=0):
        name = url.rsplit("/", 1)[-1]
        if any(fid in url for fid in self.fail_ids):
            raise RuntimeError("cover download failed")
        rank = int(name[1]) if name[:1] == "c" and name[1:2].isdigit() else 1
        return _jpeg(_COVER_WIDTH_STEP * rank)


class _FakeAuth:
    async def ensure_login(self, user_id: str) -> str:
        return "token"


class _FakeEvent:
    """命令事件桩：记录 yield 出去的结果"""

    def __init__(self, sender_id: str = "10000"):
        self._sender_id = sender_id
        self.unified_msg_origin = UMO
        self.bot = None
        self.results = []

    def get_sender_id(self) -> str:
        return self._sender_id

    def plain_result(self, text):
        self.results.append(("plain", text))
        return MessageChain([Comp.Plain(text)])

    def chain_result(self, chain):
        self.results.append(("chain", list(chain)))
        return MessageChain(list(chain))


def _component_text(comp) -> str:
    """取出 Plain 组件里的文本（conftest 的组件桩按位置/关键字两种方式存参）"""
    args = getattr(comp, "args", ())
    if args:
        return str(args[0])
    return str(getattr(comp, "kwargs", {}).get("text", ""))


class _ScriptedContext:
    """记录每次 send_message；可选地让「含文件的消息链」失败/超时"""

    def __init__(self, on_upload=None):
        self.calls = []
        self.on_upload = on_upload

    async def send_message(self, umo, chain):
        self.calls.append((umo, chain))
        is_upload = any(isinstance(c, Comp.File) for c in chain.chain)
        if is_upload and self.on_upload is not None:
            if isinstance(self.on_upload, BaseException):
                raise self.on_upload
            return self.on_upload
        return True

    @property
    def upload_calls(self):
        return [
            (umo, chain)
            for umo, chain in self.calls
            if any(isinstance(c, Comp.File) for c in chain.chain)
        ]

    @property
    def texts(self):
        return [
            _component_text(c)
            for _umo, chain in self.calls
            for c in chain.chain
            if isinstance(c, Comp.Plain)
        ]


def _make_plugin(tmp_path, config=None, comics: list = None, fail_ids=()):
    """绕过 __init__ 构造插件实例（不碰真实网络）"""
    plugin = PicaPlugin.__new__(PicaPlugin)
    plugin.config = config if config is not None else {}
    plugin.context = _ScriptedContext()
    plugin.data_dir = tmp_path
    plugin.auth = _FakeAuth()
    plugin.client = _FakeClient(comics, fail_ids)
    plugin.downloader = PicaDownloader(plugin.client, tmp_path)
    return plugin


def _run_rank(plugin, event, tt: str = "H24") -> list:
    async def _collect():
        return [item async for item in plugin.rank_command(event, tt)]

    return asyncio.run(_collect())


def _page_widths(pdf_path) -> list:
    """PDF 每页宽度（pt）；与封面像素宽度 0.75 倍对应，用于验证页序"""
    fitz = pytest.importorskip("fitz")
    with fitz.open(pdf_path) as doc:
        return [round(float(page.rect.width), 2) for page in doc]


# ------------------------------------------- download_covers：顺序 / 容错 / 命名


def test_download_covers_keeps_rank_order(tmp_path):
    downloader = PicaDownloader(_FakeClient(), tmp_path)

    covers = asyncio.run(
        downloader.download_covers(_comics(3), token="t", target_dir=tmp_path / "covers")
    )

    assert [p.name for p in covers] == [
        "001_本子1.jpg",
        "002_本子2.jpg",
        "003_本子3.jpg",
    ]
    # 关键契约：打包器的自然排序必须与榜单顺序一致（决定 PDF 页序）
    assert _collect_images_sorted(tmp_path / "covers") == covers


def test_download_covers_skips_failed_cover(tmp_path):
    downloader = PicaDownloader(_FakeClient(fail_ids=("c2",)), tmp_path)

    covers = asyncio.run(
        downloader.download_covers(_comics(3), token="t", target_dir=tmp_path / "covers")
    )

    assert [p.name for p in covers] == ["001_本子1.jpg", "003_本子3.jpg"]


def test_download_covers_without_thumb_returns_empty(tmp_path):
    downloader = PicaDownloader(_FakeClient(), tmp_path)

    covers = asyncio.run(
        downloader.download_covers(
            [{"_id": "x", "title": "无封面"}], target_dir=tmp_path / "covers"
        )
    )

    assert covers == []


def test_download_covers_sanitizes_title(tmp_path):
    comics = [
        {
            "_id": "c1",
            "title": 'bad/name:*?"<>|',
            "thumb": {"fileServer": "https://fs.picacomic.com", "path": "c1.jpg"},
        }
    ]
    downloader = PicaDownloader(_FakeClient(), tmp_path)

    covers = asyncio.run(
        downloader.download_covers(comics, target_dir=tmp_path / "covers")
    )

    assert len(covers) == 1
    name = covers[0].name
    assert name.startswith("001_bad-name")
    assert name.endswith(".jpg")
    # 非法字符全部被替换，文件名可安全落盘
    assert not set(name) & set('\\/:*?"<>|')


# ------------------------------------------------- /picarank：默认关闭时不打包


def test_rank_without_flag_sends_only_text(tmp_path):
    plugin = _make_plugin(tmp_path, {"page_size": 3})

    event = _FakeEvent()
    _run_rank(plugin, event)

    assert plugin.context.calls == []
    assert not (tmp_path / "packs").exists()
    assert list((tmp_path / "cache").glob("rank_*")) == []
    assert "🏆 哔咔排行榜" in event.results[-1][1]


def test_rank_flag_off_even_when_explicit_false(tmp_path):
    plugin = _make_plugin(tmp_path, {"rank_cover_pdf": False, "page_size": 3})

    _run_rank(plugin, _FakeEvent())

    assert plugin.context.calls == []
    assert not (tmp_path / "packs").exists()


def test_rank_truncates_to_page_size(tmp_path):
    plugin = _make_plugin(tmp_path, {"rank_cover_pdf": True, "page_size": 2})

    _run_rank(plugin, _FakeEvent())

    pdfs = list((tmp_path / "packs").glob("*.pdf"))
    assert len(pdfs) == 1
    # 只打包 page_size 张：前 2 名封面宽度 10/20 像素 → 7.5/15pt
    assert _page_widths(pdfs[0]) == [7.5, 15.0]


# ------------------------------------------------- /picarank：开启后打包并发送


def test_rank_with_flag_builds_and_sends_pdf(tmp_path):
    plugin = _make_plugin(
        tmp_path, {"rank_cover_pdf": True, "page_size": 3}, comics=_comics(3)
    )

    event = _FakeEvent()
    _run_rank(plugin, event)

    pdfs = list((tmp_path / "packs").glob("*.pdf"))
    assert len(pdfs) == 1
    # 页序 = 榜单顺序（第 1 名封面最窄，第 3 名最宽）
    assert _page_widths(pdfs[0]) == [7.5, 15.0, 22.5]

    assert len(plugin.context.upload_calls) == 1
    _umo, chain = plugin.context.upload_calls[0]
    files = [c for c in chain.chain if isinstance(c, Comp.File)]
    assert files[0].kwargs["name"] == pdfs[0].name
    assert any("封面" in t for t in plugin.context.texts)
    # 排行榜文本仍然照常发出
    assert "🏆 哔咔排行榜" in event.results[-1][1]


def test_rank_rebuilds_pdf_and_cleans_covers(tmp_path):
    """重复调用：PDF 覆盖更新，且中间封面不会残留在缓存里"""
    plugin = _make_plugin(
        tmp_path, {"rank_cover_pdf": True, "page_size": 3}, comics=_comics(3)
    )
    _run_rank(plugin, _FakeEvent())
    assert list((tmp_path / "packs").glob("*.pdf"))

    # 第二次榜单变短（模拟榜单变化）
    plugin.client.comics = _comics(1)
    _run_rank(plugin, _FakeEvent())

    pdfs = list((tmp_path / "packs").glob("*.pdf"))
    assert len(pdfs) == 1, "同名 PDF 应覆盖而不是堆积"
    assert _page_widths(pdfs[0]) == [7.5], "PDF 必须按本次榜单重建"
    assert list((tmp_path / "cache").glob("rank_*")) == [], "临时封面目录应被清理"


def test_rank_reports_when_no_cover_available(tmp_path):
    plugin = _make_plugin(
        tmp_path,
        {"rank_cover_pdf": True, "page_size": 3},
        comics=[{"_id": "x", "title": "无封面"}],
    )

    event = _FakeEvent()
    _run_rank(plugin, event)

    assert plugin.context.upload_calls == []
    assert any("未获取到任何封面" in t for t in plugin.context.texts)
    assert not (tmp_path / "packs").exists()
    assert "🏆 哔咔排行榜" in event.results[-1][1]


def test_rank_cover_pdf_upload_timeout_is_not_retried(tmp_path):
    plugin = _make_plugin(
        tmp_path, {"rank_cover_pdf": True, "page_size": 3}, comics=_comics(3)
    )
    plugin.context = _ScriptedContext(
        on_upload=TimeoutError("WebSocket API call timeout")
    )

    _run_rank(plugin, _FakeEvent())

    assert len(plugin.context.upload_calls) == 1, "上传类消息绝不能被重发"
    assert any("超时" in t for t in plugin.context.texts)


def test_rank_cover_pdf_never_breaks_rank_text(tmp_path):
    """封面环节出任何意外都只提示，不影响排行榜文本"""
    plugin = _make_plugin(
        tmp_path, {"rank_cover_pdf": True, "page_size": 3}, comics=_comics(3)
    )

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("cover download exploded")

    plugin.downloader.download_covers = _boom

    event = _FakeEvent()
    _run_rank(plugin, event)

    assert "🏆 哔咔排行榜" in event.results[-1][1]
    assert any("失败" in t for t in plugin.context.texts)
