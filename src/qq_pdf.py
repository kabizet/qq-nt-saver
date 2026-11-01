# -*- coding: utf-8 -*-
"""导出补丁模块（v2.1：在 v2.0 的 HTML/PDF 之上新增“群聊导出”）。

实现要点
--------
1. 不修改原程序逻辑：只追加按钮和方法，并复用原程序的
   get_db_paths / decrypt_db / extract_text_from_msg。
2. 消息体结构依据 NTQQ 数据库 40800 字段的 Protobuf 结构解析：
       MsgBody { repeated MsgContent content = 40800; }
       MsgContent: 45001 段ID / 45002 内容类型 / 45003 媒体子类型
                   45101 文本 / 47601+47602 QQ表情 / 45402 文件名 /
                   45406 raw MD5 / 45424 MD5 hex / 45812 本地缓存路径 /
                   45815 文本降级 / 80900 商城表情显示文本 ...
   内容类型 1=文本 2=图片 3=文件 4=语音 5=视频 6=QQ表情 7=引用 8=灰条
              9=红包 10=Ark 11=商城表情 16=合并转发 21=通话
3. 媒体按 md5/文件名 到 QQ 本地缓存（nt_data）里查真实文件；
   QQ 内置表情按 face index 映射到 EmojiSystermResource。
4. 渲染：生成自包含 HTML，再用 Edge/Chrome headless 打印成 PDF，
   中文、彩色 emoji、图片、表情包都能正常显示。
5. 群聊（v2.1 新增）：
   - 群消息在 group_msg_table，与 c2c_msg_table 同构（40030=群号、
     40033=发送者QQ、40050=时间、40013=方向、40800=消息体），
     另有 40090=发送者群名片、40093=发送者昵称，直接用于显示名。
   - 群列表从 group_msg_table 聚合（最近活跃排序），群名从同目录
     group_info.db 的 group_detail_info_ver1（60001=群号, 60007=群名）
     读取，读不到就用“未命名群 <群号>”。
   - 「加载群聊列表」按钮把群聊填进步骤3原有的列表框，选择群聊后
     TXT / HTML / PDF / 搜索过滤全部照常工作。
"""

import os
import re
import shutil
import subprocess
import tempfile
import tkinter as tk

from tkinter import ttk, messagebox, filedialog

import cdp_pdf


# --------------------------------------------------------------------------
# SQL（与原程序 export_chat 完全一致）
# --------------------------------------------------------------------------

MSG_SQL = """
    SELECT "40050", "40033", "40800", "40013"
    FROM c2c_msg_table
    WHERE "40030" = ?
    ORDER BY "40050" ASC
"""

IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}

MAGIC = (
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"BM", ".bmp"),
    (b"RIFF", ".webp"),
)

# 40800 内层字段号
F_SEG_ID = 45001
F_CTYPE = 45002
F_MEDIA_SUB = 45003
F_TEXT = 45101
F_FILENAME = 45402
F_FILEPATH = 45403
F_FILESIZE = 45405
F_MD5_RAW = 45406
F_DURATION = 45410
F_IMG_W = 45411
F_IMG_H = 45412
F_FILE_EXT = 45419
F_MD5_HEX = 45424
F_FILE_UUID = 45503
F_LOCAL_PATH = 45812
F_NTOS_PATH = 45814
F_TEXT_FALLBACK = 45815
F_VIDEO_THUMB_MD5 = 45862
F_FILE_COVER = 45954
F_FACE_INDEX = 47601
F_FACE_TEXT = 47602
F_REPLY_SUMMARY = 47413
F_STICKER = 45600
F_FILE_COVER = 45954
F_FWD_META = 47901
F_CALL_DESC = 48153
F_SYS_XML = 48214
F_MF_ID = 80810
F_MF_KEY = 80824
F_SYS_TEXT = 80900

CT_TEXT = 1
CT_IMAGE = 2
CT_FILE = 3
CT_PTT = 4
CT_VIDEO = 5
CT_FACE = 6
CT_REPLY = 7
CT_GRAYTIP = 8
CT_REDBAG = 9
CT_ARK = 10
CT_MARKETFACE = 11
CT_MARKDOWN = 14
CT_FORWARD = 16
CT_CALL = 21


# --------------------------------------------------------------------------
# 极简 protobuf 读取
# --------------------------------------------------------------------------

def _varint(buf, i):
    shift = 0
    val = 0
    n = len(buf)
    while i < n and shift < 64:
        c = buf[i]
        i += 1
        val |= (c & 0x7F) << shift
        if not (c & 0x80):
            return val, i
        shift += 7
    return None, i


def _fields(buf, base=0):
    """产出 (field, wire, value, payload_start, payload_end)。"""
    i = 0
    n = len(buf)
    while i < n:
        start = i
        key, i = _varint(buf, i)
        if key is None or i == start:
            return
        field = key >> 3
        wire = key & 7
        if field == 0:
            return
        if wire == 0:
            v, j = _varint(buf, i)
            if v is None:
                return
            yield field, wire, v, base + i, base + j
            i = j
        elif wire == 1:
            if i + 8 > n:
                return
            yield field, wire, buf[i:i + 8], base + i, base + i + 8
            i += 8
        elif wire == 2:
            ln, j = _varint(buf, i)
            if ln is None or ln < 0 or j + ln > n:
                return
            yield field, wire, buf[j:j + ln], base + j, base + j + ln
            i = j + ln
        elif wire == 5:
            if i + 4 > n:
                return
            yield field, wire, buf[i:i + 4], base + i, base + i + 4
            i += 4
        else:
            return


class Seg(object):
    """一条 MsgContent 段的字段容器。"""

    def __init__(self):
        self.v = {}

    def add(self, field, wire, value):
        self.v.setdefault(field, []).append((wire, value))

    def raw(self, field):
        for w, val in self.v.get(field, ()):
            if w == 2:
                return val
        return None

    def text(self, field):
        b = self.raw(field)
        if b is None:
            return None
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return b.decode("gbk", "replace")
            except Exception:
                return None

    def integer(self, field):
        for w, val in self.v.get(field, ()):
            if w == 0:
                return int(val)
        b = self.raw(field)
        if b is not None and len(b) <= 8:
            v = 0
            for i, ch in enumerate(b):
                v |= ch << (8 * i)
            return v
        return None

    def raw_bytes_all(self, field):
        return [val for w, val in self.v.get(field, ()) if w == 2]


def _segments(blob):
    """拆出顶层 40800 repeated MsgContent。"""
    segs = []
    if not blob:
        return segs
    try:
        for field, wire, value, _ps, _pe in _fields(blob):
            if field == 40800 and wire == 2:
                seg = Seg()
                for f2, w2, v2, _a, _b in _fields(value):
                    seg.add(f2, w2, v2)
                segs.append(seg)
    except Exception:
        pass
    return segs


# --------------------------------------------------------------------------
# 媒体索引
# --------------------------------------------------------------------------

class MediaIndex(object):
    """把 nt_data 下的图片 / 表情缓存建索引，供消息体反查真实文件。"""

    def __init__(self, nt_qq_dir):
        self.nt_qq = nt_qq_dir
        self.by_key = {}
        self.tmp_dir = None
        self._built = False
        self.face_root = None
        self.market_root = None
        if nt_qq_dir:
            self.face_root = os.path.join(nt_qq_dir, "nt_data", "Emoji",
                                          "BaseEmojiSyastems", "EmojiSystermResource")
            self.market_root = os.path.join(nt_qq_dir, "nt_data", "Emoji", "marketface")

    # -- 索引 -------------------------------------------------------------
    def build(self):
        if self._built:
            return
        self._built = True
        if not self.nt_qq:
            return
        roots = [
            os.path.join(self.nt_qq, "nt_data", "Pic"),
            os.path.join(self.nt_qq, "nt_data", "Emoji"),
            os.path.join(self.nt_qq, "nt_data", "File"),
            os.path.join(self.nt_qq, "nt_data", "Video"),
        ]
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, _dn, fns in os.walk(root):
                low = dirpath.lower()
                for fn in fns:
                    full = os.path.join(dirpath, fn)
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        continue
                    if size <= 0:
                        continue
                    stem = os.path.splitext(fn)[0].lower()
                    base = stem
                    if "_" in stem and len(stem.split("_")[0]) == 32:
                        base = stem.split("_")[0]
                    score = self._score(low, fn, size)
                    for k in {stem, base}:
                        cur = self.by_key.get(k)
                        if cur is None or score > cur[0]:
                            self.by_key[k] = (score, full)

    @staticmethod
    def _score(dirpath_low, filename, size):
        """越大越优先。

        关键：QQ 的 Ori 常是 8000px 原图（1~2MB），塞进 PDF 会让文件爆到上百 MB、
        渲染极慢甚至失败。所以优先用 Thumb（含 _720 / _0 变体），Ori 只在没有
        缩略图时才用，并且对超大原图重罚。
        """
        s = 0.0
        stem = os.path.splitext(filename)[0].lower()
        is_thumb = "thumb" in dirpath_low
        is_ori = dirpath_low.endswith("ori") or "\\ori" in dirpath_low

        if is_thumb:
            s += 100.0
            if stem.endswith("_720"):
                s += 40.0
            elif stem.endswith("_0"):
                s += 30.0
            else:
                s += 20.0
        elif is_ori:
            s += 40.0
        if "emoji" in dirpath_low or "marketface" in dirpath_low:
            s += 25.0

        kb = size / 1024.0
        if is_thumb:
            s += min(kb / 100.0, 6.0)          # 缩略图里挑稍大的
        else:
            if size > 1500 * 1024:
                s -= 80.0                       # 超大原图：坚决降权
            elif size > 800 * 1024:
                s -= 30.0
            else:
                s += min(kb / 400.0, 4.0)
        if size > 1200 * 1024:
            s -= 45.0                           # 超大缩略图（如 2.9MB 的 png）也压一压
        return s

    # -- 查询 -------------------------------------------------------------
    def find(self, token):
        if not token:
            return None
        self.build()
        t = str(token).strip().lower().replace("/", "\\")
        if os.path.isabs(t) and os.path.isfile(t):
            self.build()
            return t
        name = os.path.basename(t)
        for cand in (name, os.path.splitext(name)[0]):
            if cand in self.by_key:
                return self.by_key[cand][1]
        stem = os.path.splitext(name)[0]
        if "_" in stem:
            head = stem.split("_")[0]
            if head in self.by_key:
                return self.by_key[head][1]
        return None

    def find_face(self, index):
        """QQ 内置表情：EmojiSystermResource\\<idx>\\png\\<idx>.png"""
        if index is None or self.face_root is None:
            return None
        try:
            idx = int(index)
        except (TypeError, ValueError):
            return None
        for sub in ("png", "apng"):
            p = os.path.join(self.face_root, str(idx), sub, "%d.png" % idx)
            if os.path.isfile(p):
                return p
        return None

    def find_market(self, seg):
        """商城表情：marketface\\<packageId>\\<emojiId>"""
        if self.market_root is None or not os.path.isdir(self.market_root):
            return None
        # 优先用段内出现的路径/文件名
        for field in (F_LOCAL_PATH, F_FILEPATH, F_NTOS_PATH, F_FILE_COVER, F_FILENAME):
            s = seg.text(field)
            if not s:
                continue
            low = s.replace("/", "\\").lower()
            if "marketface" in low and os.path.isfile(s):
                return s

        # 收集段里所有数字（含 45600 sticker 内嵌消息里的 pkgid/emojiid）
        nums = set()
        names = set()
        for field in (F_FACE_INDEX, F_SEG_ID, F_CTYPE, F_MEDIA_SUB, F_FILESIZE, F_MF_ID):
            v = seg.integer(field)
            if v:
                nums.add(int(v))
        for field in (F_MF_KEY,):
            s = seg.text(field)
            if s:
                names.add(s.strip())
        blob = seg.raw(F_STICKER)
        if blob:
            try:
                for f2, w2, v2, _a, _b in _fields(blob):
                    if w2 == 0:
                        nums.add(int(v2))
                    elif w2 == 2 and 1 <= len(v2) <= 8:
                        acc = 0
                        for i, ch in enumerate(v2):
                            acc |= ch << (8 * i)
                        nums.add(acc)
                    elif w2 == 2 and len(v2) < 40:
                        try:
                            s = v2.decode("ascii")
                            if s.isdigit():
                                nums.add(int(s))
                        except Exception:
                            pass
            except Exception:
                pass

        try:
            pkgs = [d for d in os.listdir(self.market_root)
                    if os.path.isdir(os.path.join(self.market_root, d))]
        except OSError:
            return None
        for pkg in pkgs:
            pdir = os.path.join(self.market_root, pkg)
            try:
                files = set(os.listdir(pdir))
            except OSError:
                continue
            for n in nums:
                for cand in (str(n), "%d_aio.png" % n):
                    if cand in files:
                        return os.path.join(pdir, cand)
            for s in names:
                for cand in (s, "%s_aio.png" % s):
                    if cand in files:
                        return os.path.join(pdir, cand)
        return None

    # -- 浏览器可解码路径 ---------------------------------------------------
    def browser_path(self, path):
        if not path or not os.path.isfile(path):
            return None
        ext = os.path.splitext(path)[1].lower()
        if ext in IMG_EXT:
            return path
        try:
            with open(path, "rb") as f:
                head = f.read(16)
        except OSError:
            return None
        for magic, want in MAGIC:
            if head.startswith(magic):
                if self.tmp_dir is None:
                    self.tmp_dir = tempfile.mkdtemp(prefix="qqpdf_media_")
                dst = os.path.join(self.tmp_dir, os.path.basename(path) + want)
                try:
                    shutil.copyfile(path, dst)
                except OSError:
                    return None
                return dst
        return None


# --------------------------------------------------------------------------
# 消息体 -> 片段
# --------------------------------------------------------------------------

class Part(object):
    __slots__ = ("kind", "text", "path", "style", "meta")

    def __init__(self, kind, text=None, path=None, style="pic", meta=None):
        self.kind = kind      # text / image / sticker / face / tip
        self.text = text
        self.path = path
        self.style = style    # pic / sticker / face
        self.meta = meta or {}


def _media_candidates(seg):
    """按可靠度产出可用于查文件的候选键。"""
    out = []
    p = seg.text(F_LOCAL_PATH)
    if p:
        out.append(p)
    p = seg.text(F_FILEPATH)
    if p:
        out.append(p)
    p = seg.text(F_FILENAME)
    if p:
        out.append(p)
    b = seg.raw(F_MD5_HEX)
    if b:
        try:
            s = b.decode("ascii").strip()
        except Exception:
            s = ""
        s = s or b.hex()
        out.append(s)
    b = seg.raw(F_MD5_RAW)
    if b:
        out.append(b.hex())
    b = seg.raw(F_VIDEO_THUMB_MD5)
    if b:
        out.append(b.hex())
    return out


def _resolve_image(seg, media):
    if media is None:
        return None
    for key in _media_candidates(seg):
        path = media.find(key)
        if path:
            return media.browser_path(path)
    return None


def _fallback_text(blob, fallback_fn):
    """原程序的文本提取，用于非结构化/旧版本消息。"""
    if fallback_fn:
        try:
            return fallback_fn(blob)
        except Exception:
            return ""
    return ""


_XML_TXT = re.compile(r'\b(?:txt|summary|prompt|content)\s*=\s*"([^"]{1,200})"')
_HTML_TAG = re.compile(r"<[^>]+>")


def _seg_digest(seg):
    """把段落里几个关键字段摘成一行，便于排查。"""
    bits = []
    for name, field in (("ctype", F_CTYPE), ("sub", F_MEDIA_SUB),
                        ("name", F_FILENAME), ("local", F_LOCAL_PATH),
                        ("uuid", F_FILE_UUID), ("size", F_FILESIZE),
                        ("w", F_IMG_W), ("h", F_IMG_H),
                        ("mfId", F_MF_ID), ("mfKey", F_MF_KEY),
                        ("sysText", F_SYS_TEXT)):
        if field in (F_CTYPE, F_MEDIA_SUB, F_FILESIZE, F_IMG_W, F_IMG_H, F_MF_ID):
            v = seg.integer(field)
        else:
            v = seg.text(field)
        if v not in (None, "", 0):
            bits.append("%s=%s" % (name, str(v)[:60]))
    return " ".join(bits) or "(空)"


def _xml_text(s):
    """系统灰条/卡片里的 XML，尽量抽出可读文字。"""
    if not s:
        return None
    try:
        m = _XML_TXT.search(s)
        if m:
            t = m.group(1).strip()
            return t or None
        t = _HTML_TAG.sub(" ", s).strip()
        t = re.sub(r"\s+", " ", t)
        return t[:300] or None
    except Exception:
        return None


def parse_message(blob, media_index, fallback_fn=None, diag=None):
    """把一条消息体解析成有序 Part 列表（严格按 MsgContent 语义）。

    diag: 传入 list 时，把"该有媒体却没定位到"的段落信息收集进去，便于出报告。
    """
    if not blob:
        return [Part("text", text="[空消息]")]

    segs = _segments(blob)
    if not segs:
        # 旧格式：退回原程序的文本提取 + 关键词提示
        txt = _fallback_text(blob, fallback_fn) or "[二进制消息]"
        return [Part("text", text=txt)]

    parts = []
    seen = set()

    for seg in segs:
        ctype = seg.integer(F_CTYPE)

        if ctype in (CT_TEXT, None):
            t = seg.text(F_TEXT)
            if t is None:
                t = seg.text(F_SYS_TEXT)
            if t:
                parts.append(Part("text", text=t))
                continue
            if ctype == CT_TEXT:
                continue

        if ctype in (CT_IMAGE, CT_VIDEO, CT_MARKETFACE):
            path = _resolve_image(seg, media_index)
            if ctype == CT_MARKETFACE and not path and media_index is not None:
                mp = media_index.find_market(seg)
                path = media_index.browser_path(mp) if mp else None
                if not path:
                    # 再试一次：用文件名/md5 在 marketface 索引里碰运气
                    path = _resolve_image(seg, media_index)

            if path and path not in seen:
                seen.add(path)
                if ctype == CT_MARKETFACE:
                    style = "sticker"
                else:
                    low = path.lower()
                    style = "sticker" if ("emoji" in low or low.endswith(".gif")) else "pic"
                parts.append(Part("image", path=path, style=style))
                continue

            if ctype == CT_MARKETFACE:
                t = seg.text(F_SYS_TEXT) or "[动画表情]"
            elif ctype == CT_VIDEO:
                t = "[视频]"
            else:
                t = "[图片]"
            if ctype == CT_VIDEO:
                # 视频本来就不抽帧，不算"定位失败"
                parts.append(Part("tip", text=t, meta={"unsupported": True}))
            else:
                parts.append(Part("tip", text=t, meta={"missing": True}))
                if diag is not None:
                    cands = [str(c)[:100] for c in _media_candidates(seg)][:6]
                    if ctype == CT_MARKETFACE:
                        verdict = ("本地 marketface 缓存里没有该表情；消息体只有序号/哈希，"
                                   "没有可直接使用的文件路径"
                                   if not cands else
                                   "已用消息里的文件名/哈希查过本地 marketface 缓存，未命中")
                    else:
                        verdict = ("消息体里没有文件名/md5，无法定位"
                                   if not cands else
                                   "已按 md5/文件名 查过本机图片缓存，未命中"
                                   "（多为原图未下载或缓存已清理）")
                    diag.append({
                        "ctype": ctype,
                        "cands": cands,
                        "fields": _seg_digest(seg),
                        "verdict": verdict,
                        "head": blob[:48].hex(),
                    })
            continue

        if ctype == CT_FACE:
            idx = seg.integer(F_FACE_INDEX)
            face_text = seg.text(F_FACE_TEXT)
            fp = media_index.find_face(idx) if media_index else None
            if fp and fp not in seen:
                seen.add(fp)
                parts.append(Part("image", path=fp, style="face"))
                continue
            label = face_text or "🙂"
            parts.append(Part("text", text=label, meta={"missing": True, "face": True}))
            if diag is not None:
                diag.append({
                    "ctype": CT_FACE,
                    "cands": ["faceIndex=%s" % idx, "faceText=%s" % face_text],
                    "fields": _seg_digest(seg),
                    "verdict": "faceIndex 在本机 EmojiSystermResource 里没有对应图片（未缓存）",
                    "head": blob[:48].hex(),
                })
            continue

        if ctype == CT_FILE:
            name = seg.text(F_FILENAME) or "文件"
            parts.append(Part("tip", text="[文件] %s" % name))
            continue

        if ctype == CT_PTT:
            parts.append(Part("tip", text="[语音]"))
            continue

        if ctype == CT_REPLY:
            summary = seg.text(F_REPLY_SUMMARY)
            inner = seg.raw(47710)
            if not summary and inner:
                for f2, w2, v2, _a, _b in _fields(inner):
                    if f2 == F_TEXT and w2 == 2:
                        try:
                            summary = v2.decode("utf-8")
                        except Exception:
                            summary = None
                        break
            if summary:
                parts.append(Part("text", text="「%s」" % summary))
            continue

        if ctype == CT_GRAYTIP:
            t = (seg.text(F_TEXT) or seg.text(F_SYS_TEXT)
                 or _xml_text(seg.text(F_SYS_XML)))
            if t:
                parts.append(Part("tip", text=t))
            continue

        if ctype == CT_REDBAG:
            t = seg.text(F_SYS_TEXT) or "[红包]"
            parts.append(Part("tip", text=t))
            continue

        if ctype == CT_ARK:
            t = seg.text(F_SYS_TEXT) or seg.text(F_REPLY_SUMMARY) or "[卡片消息]"
            parts.append(Part("tip", text=t))
            continue

        if ctype == CT_FORWARD:
            parts.append(Part("tip", text="[聊天记录]"))
            continue

        if ctype == CT_CALL:
            t = seg.text(F_CALL_DESC) or "[通话]"
            parts.append(Part("tip", text=t))
            continue

        if ctype == CT_MARKDOWN:
            t = seg.text(F_TEXT)
            if t:
                parts.append(Part("text", text=t))
            continue

        # 未知类型：尽力捞文本（45101 → 80900 → 48214 里的 XML 文字）
        t = seg.text(F_TEXT) or seg.text(F_SYS_TEXT) or _xml_text(seg.text(F_SYS_XML))
        if t:
            parts.append(Part("text", text=t))

    if not parts:
        txt = _fallback_text(blob, fallback_fn) or "[空消息]"
        return [Part("text", text=txt)]
    return parts


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

_CSS = """
@page { size: A4; margin: 12mm 10mm; }
* { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
html, body { margin: 0; padding: 0; background: #eeeeee; }
body { font-family: "Microsoft YaHei", "Segoe UI", "Segoe UI Emoji", "SimSun", sans-serif;
       font-size: 10.5pt; color: #1a1a1a; }
.page { max-width: 560px; margin: 0 auto; background: #ffffff;
        padding: 14px 12px 22px 12px; }
@media print {
  html, body { background: #ffffff; }
  .page { max-width: 560px; padding: 0; margin: 0 auto; }
}
.header { border-bottom: 2px solid #4a86e8; padding-bottom: 8px; margin-bottom: 12px; }
.header h1 { font-size: 14pt; margin: 0 0 4px 0; color: #1f3864; }
.header .sub { font-size: 8pt; color: #777; }
.day { margin: 14px 0 5px 0; text-align: center; }
.day span { background: #ececec; color: #666; font-size: 8pt;
            padding: 2px 10px; border-radius: 9px; }
.row { display: flex; margin: 6px 0; page-break-inside: avoid; break-inside: avoid;
       justify-content: flex-start; }
.row.me { justify-content: flex-end; }
.card { max-width: 84%; }
.meta { font-size: 8pt; color: #8a8a8a; margin: 0 0 2px 2px; }
.row.me .meta { text-align: right; margin-right: 2px; }
.bubble { background: #ffffff; border: 1px solid #e2e2e2; border-radius: 10px;
          padding: 6px 10px; line-height: 1.5; white-space: pre-wrap;
          word-break: break-word; overflow-wrap: anywhere; }
.row.me .bubble { background: #d8ebff; border-color: #c3ddf7; }
.bubble img { display: block; margin: 3px 0; border-radius: 6px; }
.bubble img.pic { max-width: 235px; max-height: 235px; }
.bubble img.sticker { max-width: 104px; max-height: 104px; }
.bubble img.face { width: 22px; height: 22px; display: inline-block;
                   vertical-align: -4px; margin: 0 1px; }
.tip { color: #7a7a7a; font-style: italic; }
.footer { margin-top: 16px; border-top: 1px solid #e6e6e6; padding-top: 6px;
          font-size: 8pt; color: #999; }
"""


def _esc(s):
    import html as _h
    return _h.escape(s if s is not None else "", quote=False)


def _uri(path):
    """本地路径 -> file:/// URL（百分号编码，兼容中文/空格/括号）。"""
    try:
        from pathlib import Path
        return Path(path).as_uri()
    except Exception:
        return "file:///" + path.replace("\\", "/").replace(" ", "%20")


def _now():
    import datetime as _dt
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _is_mine(sender, friend_qq, my_qq, flag):
    """判断是否本人发送。

    优先用发送者与好友号/自己QQ号的比较；两者都判不出来时才参考 40013，
    避免不同 NTQQ 版本里 40013 语义变化导致整份记录方向错乱。
    """
    s = "" if sender is None else str(sender).strip()
    f = "" if friend_qq is None else str(friend_qq).strip()
    me = "" if my_qq is None else str(my_qq).strip()
    if me and s == me:
        return True
    if f and s == f:
        return False
    return flag == 1


# --------------------------------------------------------------------------
# 群聊（v2.1）
# --------------------------------------------------------------------------

GROUP_TABLE = "group_msg_table"


def _table_exists(conn, table):
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,))
        return cur.fetchone() is not None
    except Exception:
        return False


def _columns_of(conn, table):
    try:
        cur = conn.cursor()
        cur.execute('PRAGMA table_info("%s")' % table)
        # table_info 每行: (序号, 列名, 类型, ...) —— 取第 1 列“列名”
        return set(str(r[1]) for r in cur.fetchall())
    except Exception:
        return set()


_LEGACY_CARD = re.compile(r"^(.*)\(\d{5,}\)$")


def _clean_name(s):
    if not s:
        return ""
    s = str(s).strip()
    m = _LEGACY_CARD.match(s)      # 旧版迁移数据形如 "name(12345)"
    if m and m.group(1).strip():
        s = m.group(1).strip()
    return s


def _group_sender_display(card, nick, sender, member_lookup=None, group=None):
    """群消息发送者显示名：群名片 → 昵称 → 成员表 → QQ号。"""
    for s in (card, nick):
        s = _clean_name(s)
        if s:
            return s
    if member_lookup is not None:
        s = member_lookup(group, sender)
        if s:
            return s
    try:
        s = str(sender).strip()
    except Exception:
        s = ""
    return s if s and s != "0" else "未知"


def _make_member_lookup(decrypt_fn, msg_db):
    """group_info.db 的 group_member3（60001=群号, 1002=成员QQ,
    20002=群名片, 64031=昵称）→ 返回 lookup(群号, QQ号)。

    老消息的 40090/40093 常为空，用成员表能把名字补上；
    表读不到时返回返回 None 的查找函数，不影响导出。
    """
    def _none(g, u):
        return ""

    try:
        gi = os.path.join(os.path.dirname(os.path.abspath(msg_db)), "group_info.db")
        if not os.path.isfile(gi):
            return _none
        conn = decrypt_fn(gi)
        if not conn:
            return _none
        try:
            cols = _columns_of(conn, "group_member3")
            if not ("60001" in cols and "1002" in cols and "20002" in cols):
                return _none
            nick_col = "64031" if "64031" in cols else None
            sel = '"60001", "1002", "20002"' + (', "%s"' % nick_col if nick_col else "")
            cur = conn.cursor()
            cur.execute("SELECT %s FROM group_member3" % sel)
            card_map = {}
            nick_map = {}
            for row in cur.fetchall():
                g, uin, card = row[0], row[1], row[2]
                nk = row[3] if nick_col else None
                try:
                    gk = str(int(str(g).strip()))
                    uk = str(int(str(uin).strip()))
                except Exception:
                    continue
                c = _clean_name(card)
                if c:
                    card_map[(gk, uk)] = c
                n = _clean_name(nk)
                if n:
                    nick_map.setdefault(uk, n)
            if not card_map and not nick_map:
                return _none

            def lookup(g, u):
                try:
                    gk = str(int(str(g).strip()))
                    uk = str(int(str(u).strip()))
                except Exception:
                    return ""
                return card_map.get((gk, uk)) or nick_map.get(uk) or ""

            return lookup
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return _none


def _group_msg_rows(conn, code, member_lookup=None):
    """查一个群的全部消息，返回 [(ts, sender, blob, flag, sender_name)]。

    群表与 c2c 表同构；40090(群名片)/40093(昵称) 列存在时一并取出用于显示。
    """
    cols = _columns_of(conn, GROUP_TABLE)
    base = ["40050", "40033", "40800", "40013"]
    extra = [c for c in ("40090", "40093") if c in cols]
    sel = ", ".join('"%s"' % c for c in base + extra)
    cur = conn.cursor()
    cur.execute('SELECT %s FROM "%s" WHERE "40030" = ? ORDER BY "40050" ASC'
                % (sel, GROUP_TABLE), (code,))
    rows = []
    for r in cur.fetchall():
        ts, sender, blob, flag = r[0], r[1], r[2], r[3]
        if not ts:
            continue          # 40050 为空的占位行：无时间、无内容（群表特有）
        card = r[4] if len(r) > 4 else None
        nick = r[5] if len(r) > 5 else None
        rows.append((ts, sender, blob, flag,
                     _group_sender_display(card, nick, sender,
                                           member_lookup, code)))
    return rows


def _group_key(code):
    """群号统一成十进制字符串，用于群名映射。"""
    try:
        return str(int(str(code).strip()))
    except Exception:
        return str(code).strip()


def _load_group_names(decrypt_fn, msg_db):
    """从 nt_db/group_info.db 读 {群号: 群名}。

    group_detail_info_ver1: 60001=群号, 60007=群名（本机已实测）；
    旧版没有该表时退回 group_list 的同名列。读不到返回空 dict。
    """
    try:
        gi = os.path.join(os.path.dirname(os.path.abspath(msg_db)), "group_info.db")
        if not os.path.isfile(gi):
            return {}
        conn = decrypt_fn(gi)
        if not conn:
            return {}
        try:
            names = {}
            for table in ("group_detail_info_ver1", "group_list"):
                if not _table_exists(conn, table):
                    continue
                cols = _columns_of(conn, table)
                if "60001" not in cols or "60007" not in cols:
                    continue
                cur = conn.cursor()
                cur.execute('SELECT "60001", "60007" FROM "%s"' % table)
                for code, name in cur.fetchall():
                    if code is None:
                        continue
                    s = str(name).strip() if name is not None else ""
                    if s:
                        names[_group_key(code)] = s
                if names:
                    break
            return names
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return {}


def build_html(friend_name, my_qq, rows, media_index, fallback_fn, progress=None,
               friend_qq=None, group_names=None, is_group=False):
    """rows: [(ts, sender, blob, flag)] -> (html_text, stats)

    群聊时 group_names 提供与 rows 等长的发送者显示名列表（“我”除外）。
    """
    import datetime as _dt

    short = (friend_name.split(" - ")[0] or friend_name).strip()
    stats = {"msgs": len(rows), "img": 0, "sticker": 0, "face": 0, "unresolved": 0,
             "diag": []}

    if is_group:
        title_line = "群聊「%s」的聊天记录" % _esc(short)
    else:
        title_line = "与 %s 的聊天记录" % _esc(short)

    out = ['<!doctype html><html><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           "<title>%s</title>" % _esc(short),
           "<style>%s</style></head><body>" % _CSS,
           '<div class="page">',
           '<div class="header"><h1>%s</h1>' % title_line,
           '<div class="sub">共 %d 条消息 ｜ 导出时间 %s ｜ QQ %s</div>'
           % (len(rows), _now(), _esc(my_qq or "")),
           '</div><!--pagesplit-->']

    last_day = None
    for i, (ts, sender, blob, flag) in enumerate(rows):
        try:
            dt = _dt.datetime.fromtimestamp(ts)
        except Exception:
            dt = _dt.datetime(1970, 1, 1)
        day = dt.strftime("%Y-%m-%d")
        if day != last_day:
            last_day = day
            out.append('<div class="day"><span>%s</span></div>' % day)

        mine = _is_mine(sender, friend_qq, my_qq, flag)
        if group_names is not None:
            name = "我" if mine else (group_names[i] if i < len(group_names) else short)
        else:
            name = "我" if mine else short

        item_diag = []
        parts = parse_message(blob, media_index, fallback_fn, item_diag)
        for d in item_diag:
            d["time"] = dt.strftime("%Y-%m-%d %H:%M:%S")
            d["who"] = name
            stats["diag"].append(d)

        body = []
        prev_text = False
        for p in parts:
            if p.meta.get("missing"):
                stats["unresolved"] += 1
            if p.kind == "text":
                # 多段文本同段显示时补个分隔，避免“甲乙”粘在一起
                if prev_text and body:
                    body.append(" ")
                body.append(_esc(p.text))
                prev_text = True
            elif p.kind == "image":
                body.append('<img class="%s" src="%s">' % (p.style, _uri(p.path)))
                if p.style == "face":
                    stats["face"] += 1
                elif p.style == "sticker":
                    stats["sticker"] += 1
                else:
                    stats["img"] += 1
                prev_text = False
            elif p.kind == "tip":
                body.append('<span class="tip">%s</span>' % _esc(p.text))
                prev_text = False
            else:
                stats["unresolved"] += 1

        out.append('<div class="row %s"><div class="card">' % ("me" if mine else "other"))
        out.append('<div class="meta">%s ｜ %s</div>' % (dt.strftime("%H:%M:%S"), _esc(name)))
        out.append('<div class="bubble">%s</div>' % "".join(body))
        out.append("</div></div>")

        if progress and (i + 1) % 150 == 0:
            progress(i + 1, len(rows))

    out.append('<div class="footer">由 QQ聊天记录导出工具 生成 ｜ 共 %d 条消息</div>' % len(rows))
    out.append("</div></body></html>")
    return "".join(out), stats


# --------------------------------------------------------------------------
# HTML -> PDF（大文档分块并行打印 + 合并；小文档单次打印）
# --------------------------------------------------------------------------

_BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 超过这个行数就分块并行打印（实测 2.5 万行单次打印要 7 分多钟）
PARALLEL_MIN_ROWS = 1200
# 每个分块至少承载的行数，避免分出过多小片
PARALLEL_CHUNK_ROWS = 700
PARALLEL_MAX_WORKERS = 6

_PAGE_SPLIT_MARK = "<!--pagesplit-->"
_DAY_SPLIT_MARK = '<div class="day">'
_FOOT_MARK = '<div class="footer">'


def find_browser():
    for p in _BROWSERS:
        if os.path.isfile(p):
            return p
    for name in ("msedge.exe", "chrome.exe"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _candidate_browsers():
    out = []
    for p in _BROWSERS:
        if os.path.isfile(p) and p not in out:
            out.append(p)
    if not out:
        for name in ("msedge.exe", "chrome.exe"):
            w = shutil.which(name)
            if w and w not in out:
                out.append(w)
    return out


def _legacy_print(browser, hp, out_pdf, work):
    """老办法：--print-to-pdf。保留作为 CDP 失败时的兜底。"""
    profile = os.path.join(work, "profile_legacy")
    os.makedirs(profile, exist_ok=True)
    tmp_pdf = os.path.join(work, "legacy.pdf")
    cmd = [
        browser, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check",
        "--disable-background-mode", "--disable-extensions",
        "--disable-crash-reporter", "--disable-breakpad",
        "--user-data-dir=" + profile,
        "--no-pdf-header-footer", "--run-all-compositor-stages-before-draw",
        "--virtual-time-budget=20000", "--print-to-pdf=" + tmp_pdf, _uri(hp),
    ]
    kw = {"creationflags": _NO_WINDOW} if _NO_WINDOW else {}
    log_path = os.path.join(work, "legacy.log")
    with open(log_path, "wb") as logf:
        try:
            proc = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                                  stdout=logf, stderr=logf, timeout=300, **kw)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc = "timeout"
    if os.path.exists(tmp_pdf) and os.path.getsize(tmp_pdf) > 0:
        shutil.move(tmp_pdf, out_pdf)
        return True, "ok"
    try:
        tail = open(log_path, "rb").read().decode("utf-8", "replace")[-400:]
    except Exception:
        tail = ""
    return False, "返回码 %s %s" % (rc, tail.strip()[:200])


def _print_one(browser, hp, out_pdf, work, timeout=240):
    """打印单个 HTML：先 CDP，失败退 --print-to-pdf。返回 (ok, 详情)。"""
    ok, info = cdp_pdf.print_to_pdf(browser, hp, out_pdf, work, timeout=timeout)
    if ok and os.path.exists(out_pdf) and os.path.getsize(out_pdf) > 0:
        return True, info
    info2 = info
    ok, info = _legacy_print(browser, hp, out_pdf, work)
    if ok and os.path.exists(out_pdf) and os.path.getsize(out_pdf) > 0:
        return True, "legacy"
    return False, "%s / %s" % (info2, info)


def _split_html_for_print(html_text):
    """按“天”把整页 HTML 切成可独立渲染的块。

    返回 (带页眉的开头, 无页眉的开头, 天块列表, 含页脚的结尾)。
    按结构定位（页眉 div → 第一个日期行），不依赖生成时埋标记。
    """
    i0 = html_text.index(_DAY_SPLIT_MARK)
    i1 = html_text.rindex(_FOOT_MARK)
    prologue = html_text[:i0]
    hend = prologue.index('<div class="header">')
    plain_prologue = prologue[:hend]
    blocks = [_DAY_SPLIT_MARK + b for b in html_text[i0:i1].split(_DAY_SPLIT_MARK) if b]
    tail = html_text[i1:]
    return prologue, plain_prologue, blocks, tail


def _merge_pdfs(part_paths, out_pdf):
    from pypdf import PdfWriter
    writer = PdfWriter()
    for p in part_paths:
        writer.append(p)
    parent = os.path.dirname(os.path.abspath(out_pdf))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    tmp = out_pdf + ".合并中.pdf"
    with open(tmp, "wb") as f:
        writer.write(f)
    os.replace(tmp, out_pdf)


def _print_parallel(html_text, out_pdf, browsers, workers, progress=None):
    """分块并行打印整个文档。成功返回 True；任何一块失败返回 False。"""
    prologue, plain, blocks, tail = _split_html_for_print(html_text)
    nchunks = max(1, min(workers, len(blocks)))
    per = (len(blocks) + nchunks - 1) // nchunks
    chunks = [blocks[i:i + per] for i in range(0, len(blocks), per)]
    if not chunks:
        return False

    work = tempfile.mkdtemp(prefix="qqpdf_split_")
    part_paths = []
    jobs = []
    try:
        for idx, ch in enumerate(chunks):
            hp = os.path.join(work, "chunk_%02d.html" % idx)
            with open(hp, "w", encoding="utf-8") as f:
                f.write((prologue if idx == 0 else plain)
                        + "".join(ch)
                        + (tail if idx == len(chunks) - 1 else "</div></body></html>"))
            part = os.path.join(work, "part_%02d.pdf" % idx)
            part_paths.append(part)
            jobs.append((browsers[idx % len(browsers)], hp, part,
                         os.path.join(work, "w%02d" % idx)))

        errors = []
        done = [0]

        import threading
        from concurrent.futures import ThreadPoolExecutor

        def _job(i, args):
            b, hp, part, wdir = args
            os.makedirs(wdir, exist_ok=True)
            ok, info = _print_one(b, hp, part, wdir,
                                  timeout=max(600, 240 * len(chunks)))
            done[0] += 1
            if progress:
                progress(done[0], len(chunks))
            if not ok:
                errors.append("分块%d: %s" % (i, info))
            return ok

        with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
            list(ex.map(lambda t: _job(t[0], t[1]), enumerate(jobs)))

        if len(errors) < len(jobs) and all(
                os.path.exists(p) and os.path.getsize(p) > 0 for p in part_paths):
            if os.path.exists(out_pdf):
                try:
                    os.remove(out_pdf)
                except OSError:
                    pass
            _merge_pdfs(part_paths, out_pdf)
            return True
        if errors:
            raise RuntimeError("并行打印有分块失败：%s" % "; ".join(errors[:3]))
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


def html_to_pdf(html_text, out_pdf, progress=None):
    browsers = _candidate_browsers()
    if not browsers:
        raise RuntimeError("未找到 Edge 或 Chrome，无法生成 PDF。")

    rows = html_text.count('class="row')
    workers = min(PARALLEL_MAX_WORKERS, max(2, (os.cpu_count() or 4)))
    if rows >= PARALLEL_MIN_ROWS:
        try:
            import pypdf  # noqa: F401  有合并库才走并行
            if progress:
                progress(0, 1)
            if _print_parallel(html_text, out_pdf, browsers, workers, progress):
                return out_pdf
        except Exception:
            if progress:
                progress(0, 1)
            # 并行失败（如 pypdf 缺失/浏览器异常）→ 落回单次整页打印
            pass

    work = tempfile.mkdtemp(prefix="qqpdf_")
    try:
        hp = os.path.join(work, "chat.html")
        with open(hp, "w", encoding="utf-8") as f:
            f.write(html_text)

        parent = os.path.dirname(os.path.abspath(out_pdf))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        if os.path.exists(out_pdf):
            try:
                os.remove(out_pdf)
            except OSError:
                pass

        errors = []
        # 1) 首选 CDP Page.printToPDF：不依赖浏览器的 --print-to-pdf
        #    （某些机器上那条路会被 Edge 后台模式/单例吞掉：返回 0 且零输出）
        for b in browsers:
            ok, info = cdp_pdf.print_to_pdf(b, hp, out_pdf, work, timeout=600)
            if ok and os.path.exists(out_pdf) and os.path.getsize(out_pdf) > 0:
                return out_pdf
            errors.append("CDP %s: %s" % (os.path.basename(b), info))

        # 2) 兜底：--print-to-pdf
        for b in browsers:
            ok, info = _legacy_print(b, hp, out_pdf, work)
            if ok and os.path.exists(out_pdf) and os.path.getsize(out_pdf) > 0:
                return out_pdf
            errors.append("print-to-pdf %s: %s" % (os.path.basename(b), info))

        # 3) 全失败：落日志 + 明确结论
        log_path = os.path.splitext(os.path.abspath(out_pdf))[0] + ".浏览器日志.txt"
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("html 大小: %.2f MB\n消息数: %d\n\n%s\n"
                        % (len(html_text.encode("utf-8")) / 1048576.0,
                           rows, "\n".join(errors)))
        except Exception:
            pass
        raise RuntimeError(
            "浏览器两种打印方式都失败（详情见同目录 *.浏览器日志.txt）：\n%s\n\n"
            "可以先用「导出为HTML」，再用浏览器打开 Ctrl+P 另存为 PDF。"
            % "\n".join(errors[:4]))
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------
# 与原程序对接
# --------------------------------------------------------------------------

def _nt_qq_dir(msg_db):
    try:
        nt_db = os.path.dirname(os.path.abspath(msg_db))
        return os.path.dirname(nt_db)
    except Exception:
        return None


_BAD_NAME = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _safe_name(s, default="聊天记录"):
    """把好友昵称变成合法文件名（去掉 Windows 不允许的字符）。"""
    s = _BAD_NAME.sub("_", (s or "").strip()).strip(" .")
    return s[:60] or default


def export_pdf(self):
    if not getattr(self, "selected_friend", None):
        messagebox.showwarning("提示", "请先选择一个好友或群聊")
        return

    friend_qq, friend_name = self.selected_friend
    prefix = "群聊记录" if getattr(self, "_selected_is_group", False) else "聊天记录"
    default_filename = "%s_%s.pdf" % (prefix, _safe_name(friend_name.split(" - ")[0]))
    filepath = filedialog.asksaveasfilename(
        defaultextension=".pdf",
        filetypes=[("PDF 文件", "*.pdf"), ("所有文件", "*.*")],
        initialfile=default_filename,
    )
    if not filepath:
        return

    self.status_label.config(text="正在生成 PDF...", foreground="orange")
    try:
        self.root.update()
    except Exception:
        pass

    try:
        stats = _do_export(self, filepath)
    except Exception as e:
        try:
            self.status_label.config(text="导出失败", foreground="red")
        except Exception:
            pass
        messagebox.showerror("错误", "PDF 导出失败：%s" % e)
        return

    msg = ("PDF 已导出到：\n%s\n\n共 %d 条消息\n图片 %d 张 ｜ 表情 %d 个 ｜ 小表情 %d 个\n未能定位的媒体 %d 处"
           % (filepath, stats["msgs"], stats["img"], stats["sticker"],
              stats["face"], stats["unresolved"]))
    if stats.get("diag_file"):
        msg += "\n\n未定位明细已写到：\n%s" % stats["diag_file"]
    try:
        self.status_label.config(text="导出成功！共 %d 条消息" % stats["msgs"], foreground="green")
    except Exception:
        pass
    messagebox.showinfo("成功", msg)


def export_html(self):
    """导出为单文件 HTML（可直接用浏览器打开/打印，也是 PDF 的中间产物）。"""
    if not getattr(self, "selected_friend", None):
        messagebox.showwarning("提示", "请先选择一个好友或群聊")
        return

    friend_qq, friend_name = self.selected_friend
    prefix = "群聊记录" if getattr(self, "_selected_is_group", False) else "聊天记录"
    default_filename = "%s_%s.html" % (prefix, _safe_name(friend_name.split(" - ")[0]))
    filepath = filedialog.asksaveasfilename(
        defaultextension=".html",
        filetypes=[("网页文件", "*.html"), ("所有文件", "*.*")],
        initialfile=default_filename,
    )
    if not filepath:
        return

    self.status_label.config(text="正在生成 HTML...", foreground="orange")
    try:
        self.root.update()
    except Exception:
        pass

    try:
        stats = _do_export_html(self, filepath)
    except Exception as e:
        try:
            self.status_label.config(text="导出失败", foreground="red")
        except Exception:
            pass
        messagebox.showerror("错误", "HTML 导出失败：%s" % e)
        return

    try:
        self.status_label.config(text="导出成功！共 %d 条消息" % stats["msgs"], foreground="green")
    except Exception:
        pass
    detail = ""
    if stats.get("diag_file"):
        detail = "\n\n未定位明细已写到：\n%s" % stats["diag_file"]
    messagebox.showinfo(
        "成功",
        "HTML 已导出到：\n%s\n\n共 %d 条消息\n图片 %d 张 ｜ 表情 %d 个 ｜ 小表情 %d 个\n"
        "未能定位的媒体 %d 处%s\n\n可直接用浏览器打开查看（也可用浏览器「打印 → 另存为 PDF」）。"
        % (filepath, stats["msgs"], stats["img"], stats["sticker"],
           stats["face"], stats["unresolved"], detail))


def load_groups(self):
    """「加载群聊列表」：把群聊填进步骤3原有的列表框（与好友共用）。

    群消息来自 nt_msg.db 的 group_msg_table，按最近活跃排序；
    群名从同目录 group_info.db 读取，读不到显示“未命名群 <群号>”。
    选择群聊后，TXT / HTML / PDF / 搜索过滤全部照常工作。
    """
    if not hasattr(self, "friend_listbox"):
        return
    self.status_label.config(text="正在加载群聊列表...", foreground="orange")
    try:
        self.root.update()
    except Exception:
        pass

    conn = None
    try:
        msg_db, _profile_db = self.get_db_paths()
        if not msg_db:
            messagebox.showinfo(
                "未找到消息数据库",
                "请先完成步骤1/2，确认 nt_msg.db 位置后再加载群聊。")
            return
        conn = self.decrypt_db(msg_db)
        if not conn:
            self.status_label.config(text="解密失败", foreground="red")
            messagebox.showerror("错误", "解密消息库失败，请检查密钥")
            return

        if not _table_exists(conn, GROUP_TABLE):
            messagebox.showinfo("未找到群消息表",
                                "这个 nt_msg.db 里没有 group_msg_table，无法导出群聊。")
            return

        cur = conn.cursor()
        cur.execute('SELECT "40030", COUNT(*), MAX("40050") FROM "%s" '
                    'WHERE "40030" IS NOT NULL '
                    'GROUP BY "40030" ORDER BY MAX("40050") DESC' % GROUP_TABLE)
        stat_rows = cur.fetchall()
        if not stat_rows:
            messagebox.showinfo("没有群聊", "群消息表是空的。")
            return

        names = _load_group_names(self.decrypt_db, msg_db)

        groups = []
        for code, cnt, _last in stat_rows:
            key = _group_key(code)
            name = names.get(key)
            if name:
                display = "%s - %s（%d条）" % (name, key, cnt)
            else:
                display = "未命名群 %s - %s（%d条）" % (key, key, cnt)
            groups.append((code, display))

        self._group_mode = True
        self.friends = groups
        self.filtered_friends = list(groups)
        try:
            self.search_var.set("")
        except Exception:
            pass
        lb = self.friend_listbox
        lb.delete(0, tk.END)
        for _c, display in groups:
            lb.insert(tk.END, display)
        self.status_label.config(text="加载完成，共 %d 个群聊" % len(groups),
                                 foreground="green")
    except Exception as e:
        try:
            self.status_label.config(text="加载失败", foreground="red")
        except Exception:
            pass
        messagebox.showerror("错误", "加载群聊失败：%s" % e)
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def export_chat(self):
    """TXT 导出：好友走原程序逻辑；群聊用同样的版式从 group_msg_table 导出。"""
    if not getattr(self, "_selected_is_group", False):
        return self._orig_export_chat()

    if not getattr(self, "selected_friend", None):
        messagebox.showwarning("提示", "请先选择一个群聊")
        return

    import datetime as _dt

    friend_qq, friend_name = self.selected_friend
    short = (friend_name.split(" - ")[0] or friend_name).strip() or ("群" + str(friend_qq))
    default_filename = "聊天记录_%s.txt" % _safe_name(short)
    filepath = filedialog.asksaveasfilename(
        defaultextension=".txt",
        filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        initialfile=default_filename,
    )
    if not filepath:
        return

    self.status_label.config(text="正在导出聊天记录...", foreground="orange")
    try:
        self.root.update()
    except Exception:
        pass

    conn = None
    try:
        msg_db, _profile_db = self.get_db_paths()
        if not msg_db:
            raise RuntimeError("未找到消息数据库")
        conn = self.decrypt_db(msg_db)
        if not conn:
            raise RuntimeError("数据库解密失败，请检查密钥")
        lookup = _make_member_lookup(self.decrypt_db, msg_db)
        rows = _group_msg_rows(conn, friend_qq, lookup)
        try:
            my_qq = (self.qq_number.get() or "").strip()
        except Exception:
            my_qq = ""
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("=== 群聊「%s」的聊天记录 ===\n\n" % short)
            f.write("共 %d 条消息\n\n" % len(rows))
            for ts, sender, blob, flag, name in rows:
                mine = _is_mine(sender, None, my_qq, flag)
                try:
                    time_str = _dt.datetime.fromtimestamp(ts).strftime(
                        "%Y-%m-%d %H:%M:%S")
                except Exception:
                    time_str = "1970-01-01 08:00:00"
                text = self.extract_text_from_msg(blob)
                f.write("[%s] %s: %s\n" % (time_str, "我" if mine else name, text))
        self.status_label.config(text="导出成功！共 %d 条消息" % len(rows),
                                 foreground="green")
        messagebox.showinfo("成功", "聊天记录已导出到：\n%s\n\n共 %d 条消息"
                            % (filepath, len(rows)))
    except Exception as e:
        try:
            self.status_label.config(text="导出失败", foreground="red")
        except Exception:
            pass
        messagebox.showerror("错误", "导出失败：%s" % e)
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _generate(self):
    """读库 -> 解析 -> 生成 HTML。返回 (html_text, stats)。"""
    friend_qq, friend_name = self.selected_friend
    is_group = bool(getattr(self, "_selected_is_group", False))

    msg_db, _profile_db = self.get_db_paths()
    if not msg_db:
        raise RuntimeError("未找到消息数据库")

    # decrypt_db 已被 install() 包了一层：内部会复制 db+wal 到临时目录再解密，
    # 所以这里拿到的就是最新数据（含 WAL 里还没落盘的最近消息）。
    conn = self.decrypt_db(msg_db)
    if not conn:
        raise RuntimeError("数据库解密失败，请检查密钥")
    try:
        if is_group:
            if not _table_exists(conn, GROUP_TABLE):
                raise RuntimeError("这个消息库里没有群消息表（group_msg_table）")
            lookup = _make_member_lookup(self.decrypt_db, msg_db)
            g = _group_msg_rows(conn, friend_qq, lookup)
            rows = [(ts, s, b, f) for (ts, s, b, f, _n) in g]
            group_names = [n for (_ts, _s, _b, _f, n) in g]
        else:
            cur = conn.cursor()
            cur.execute(MSG_SQL, (friend_qq,))
            rows = cur.fetchall()
            group_names = None
    finally:
        try:
            conn.close()
        except Exception:
            pass

    try:
        my_qq = (self.qq_number.get() or "").strip()
    except Exception:
        my_qq = ""

    media_index = MediaIndex(_nt_qq_dir(msg_db))

    def progress(done, total):
        try:
            self.status_label.config(text="正在解析消息... %d/%d" % (done, total))
            self.root.update()
        except Exception:
            pass

    return build_html(friend_name, my_qq, rows, media_index,
                      self.extract_text_from_msg, progress,
                      friend_qq=friend_qq, group_names=group_names,
                      is_group=is_group)


# 最近一次 _open_msg_db 的诊断信息（供 UI 状态栏/自检报告读取）
_LAST_OPEN_DIAG = {}


def _copy_with_retry(src, dst, retries=3, delay=0.4):
    """复制文件，失败重试。返回 (是否成功, 错误信息)。"""
    import time as _t
    last_err = None
    for i in range(retries):
        try:
            shutil.copyfile(src, dst)
            return True, None
        except OSError as e:
            last_err = e
            _t.sleep(delay)
    return False, last_err


def _open_msg_db(decrypt_fn, msg_db, with_wal=True):
    """把库（含 -wal/-shm）复制到临时目录并生成 *_clean.db，再交给原程序解密。

    原程序的 clean_db_file() 命中已存在的 *_clean.db 就直接复用，
    会读到很旧的快照；而且它不处理 -wal，最近的聊天（还在 WAL 里）会丢。
    这里每次都重建，并带上 WAL。返回 (临时目录, 连接)。

    临时目录按源库路径复用：同一个库只保留一份副本（用户的 nt_msg.db 有一百多 MB，
    每次导出都新开一个目录会堆满 %TEMP%）。
    """
    import time as _t
    diag = {"src": msg_db, "wal_copied": False, "wal_size_src": 0,
            "wal_size_dst": 0, "wal_error": None, "checkpoint": None}
    tmp = _fresh_dir_for(msg_db)
    name = os.path.basename(msg_db)
    dst = os.path.join(tmp, name)
    try:
        shutil.copyfile(msg_db, dst)
        suffixes = ("-wal", "-shm") if with_wal else ()
        wal_src = msg_db + "-wal"
        wal_src_size = os.path.getsize(wal_src) if os.path.exists(wal_src) else 0
        diag["wal_size_src"] = wal_src_size
        for suf in suffixes:
            src = msg_db + suf
            if os.path.exists(src):
                ok, err = _copy_with_retry(src, dst + suf)
                if not ok:
                    diag["wal_error"] = "%s: %s" % (suf, err)
        clean = dst.replace(".db", "_clean.db")
        with open(dst, "rb") as f:
            data = f.read()
        with open(clean, "wb") as f:
            f.write(data[1024:])
        # WAL 大小：源有内容但副本没带上 → 告警
        wal_dst = clean + "-wal"
        wal_dst_size = os.path.getsize(wal_dst) if os.path.exists(wal_dst) else 0
        diag["wal_size_dst"] = wal_dst_size
        if wal_src_size > 0 and wal_dst_size == 0:
            # 源 WAL 有内容但没复制到临时副本
            for suf in suffixes:
                src = msg_db + suf
                if os.path.exists(src):
                    ok, err = _copy_with_retry(src, clean + suf, retries=5, delay=0.5)
                    if ok and os.path.exists(clean + "-wal"):
                        wal_dst_size = os.path.getsize(clean + "-wal")
                        diag["wal_size_dst"] = wal_dst_size
                        diag["wal_error"] = None
                        break
                    elif not ok:
                        diag["wal_error"] = "%s: %s" % (suf, err)
        else:
            for suf in suffixes:
                src = msg_db + suf
                if os.path.exists(src):
                    try:
                        shutil.copyfile(src, clean + suf)
                    except OSError:
                        pass
        diag["wal_copied"] = (wal_src_size > 0 and wal_dst_size > 0) or wal_src_size == 0
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    conn = decrypt_fn(dst)
    # 打开后强制把 WAL 合并进主库，确保后续查询读到最新页
    try:
        cur = conn.cursor()
        cur.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        row = cur.fetchone()
        diag["checkpoint"] = row[0] if row else None
    except Exception:
        pass
    _LAST_OPEN_DIAG.clear()
    _LAST_OPEN_DIAG.update(diag)
    return tmp, conn


_TMP_MAP = {}


def _fresh_dir_for(db_path):
    key = os.path.abspath(db_path).lower()
    old = _TMP_MAP.get(key)
    if old:
        shutil.rmtree(old, ignore_errors=True)
    d = tempfile.mkdtemp(prefix="qqpdf_db_")
    _TMP_MAP[key] = d
    return d


def _cleanup_all_tmp():
    for d in list(_TMP_MAP.values()):
        shutil.rmtree(d, ignore_errors=True)
    _TMP_MAP.clear()


def _install_fresh_db(App):
    """把原程序的 decrypt_db 包一层：任何调用都读“最新快照（含 WAL）”。

    TXT 导出、好友列表加载都会走 decrypt_db，这样三处数据一致，不会再出现
    “HTML 是最新的、TXT 还停在旧日期”的情况。
    """
    orig = App.decrypt_db
    if getattr(orig, "_qqpdf_fresh", False):
        return

    def decrypt_db(self, db_path):
        try:
            tmp, conn = _open_msg_db(lambda p: orig(self, p), db_path, True)
            if conn is not None:
                return conn
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass
        # 兜底：完全走原逻辑（例如路径不是 QQ 的 nt_msg.db）
        return orig(self, db_path)

    decrypt_db._qqpdf_fresh = True
    App.decrypt_db = decrypt_db


def _write_diag(stats, out_path):
    """把未定位的媒体写一份旁注文件，方便定位问题。返回文件路径或 None。"""
    diag = stats.get("diag") or []
    if not diag:
        return None
    path = os.path.splitext(os.path.abspath(out_path))[0] + ".未定位媒体.txt"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("未能定位的媒体明细（共 %d 处）\n" % len(diag))
            f.write("=" * 60 + "\n")
            for d in diag:
                f.write("时间: %s  发送者: %s  段类型(45002): %s\n"
                        % (d.get("time"), d.get("who"), d.get("ctype")))
                if d.get("verdict"):
                    f.write("  结论: %s\n" % d.get("verdict"))
                f.write("  字段: %s\n" % d.get("fields"))
                f.write("  候选: %s\n" % "; ".join(d.get("cands") or []))
                f.write("  消息头: %s\n\n" % d.get("head"))
        return path
    except Exception:
        return None


def _do_export(self, filepath):
    try:
        html_text, stats = _generate(self)

        def _pdf_progress(done, total):
            try:
                text = ("正在排版 PDF（并行 %d/%d）..." % (done, total)
                        if total > 1 else "正在排版 PDF...")
                self.status_label.config(text=text, foreground="orange")
                self.root.update()
            except Exception:
                pass

        html_to_pdf(html_text, filepath, _pdf_progress)
        stats["diag_file"] = _write_diag(stats, filepath)
        return stats
    finally:
        _cleanup_all_tmp()


def _do_export_html(self, filepath):
    try:
        html_text, stats = _generate(self)
        parent = os.path.dirname(os.path.abspath(filepath))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(html_text)
        stats["diag_file"] = _write_diag(stats, filepath)
        return stats
    finally:
        _cleanup_all_tmp()


def install(App):
    """给原始类打补丁：追加按钮/群聊支持，并挂上 export_pdf / load_groups。"""
    if getattr(App, "_pdf_installed", False):
        return
    App._pdf_installed = True

    orig_setup_ui = App.setup_ui

    def setup_ui(self):
        orig_setup_ui(self)
        try:
            panel = self.status_label.master
            self.html_button = ttk.Button(panel, text="导出为HTML",
                                          command=self.export_html)
            self.html_button.pack(side="right", padx=5, pady=5)
            self.pdf_button = ttk.Button(panel, text="导出为PDF",
                                         command=self.export_pdf)
            self.pdf_button.pack(side="right", padx=5, pady=5)
        except Exception:
            pass
        try:
            # 「加载群聊列表」放进步骤3的搜索行，位于「加载好友列表」左边
            frame3 = self.friend_listbox.master.master
            search_frame = frame3.winfo_children()[0]
            self.group_button = ttk.Button(search_frame, text="加载群聊列表",
                                           command=self.load_groups)
            self.group_button.pack(side="right", padx=(0, 5))
            frame3.config(text="步骤3：选择好友 / 群聊")
        except Exception:
            pass
        # 原来 600x500 时“步骤4”按钮会被窗口下沿裁掉一半，这里留够高度
        try:
            self.root.geometry("700x560")
        except Exception:
            pass

    App.setup_ui = setup_ui
    App.export_pdf = export_pdf
    App.export_html = export_html
    App.load_groups = load_groups

    # ---- v2.1：群聊模式跟踪与 TXT 分流 ----
    App._group_mode = False
    App._selected_is_group = False

    orig_load_friends = App.load_friends

    def load_friends(self):
        self._group_mode = False
        return orig_load_friends(self)

    App.load_friends = load_friends

    orig_on_friend_select = App.on_friend_select

    def on_friend_select(self, event):
        result = orig_on_friend_select(self, event)
        try:
            self._selected_is_group = bool(getattr(self, "_group_mode", False))
        except Exception:
            self._selected_is_group = False
        return result

    App.on_friend_select = on_friend_select

    App._orig_export_chat = App.export_chat
    App.export_chat = export_chat

    _install_fresh_db(App)
    try:
        import atexit
        atexit.register(_cleanup_all_tmp)
    except Exception:
        pass
