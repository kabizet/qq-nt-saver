# -*- coding: utf-8 -*-
"""QQ聊天记录导出工具 v2.1 启动器。

原始程序（v1.9）的主体逻辑以编译后的 code object 形式随包分发，
这里用 exec 在独立命名空间中还原它（不触发其 __main__ 分支），
随后打上补丁：v2.0 的 HTML/PDF 导出 + v2.1 的群聊导出，
再调用原始 main()。原有功能完全走原代码，不做任何改写。

另外提供一个隐藏自检入口，便于验证打包后的导出链路：
    QQ聊天记录导出工具_v2.1.exe --pdf-selftest <nt_msg.db> <密钥> <输出> ^
        [pdf|html|both] [--group [群号]] [--qq 我的QQ号]

不带 --group 时自检好友单聊（c2c_msg_table），带 --group 时自检群聊
（group_msg_table），群号省略时自动取消息最多的群。
"""

import marshal
import os
import sys


def _base_dir():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def _load_original():
    path = os.path.join(_base_dir(), "orig_entry.bin")
    with open(path, "rb") as f:
        code = marshal.loads(f.read())
    ns = {
        "__name__": "qqchatexporter_original",
        "__file__": "QQChatExporter.py",
        "__builtins__": __builtins__,
    }
    exec(code, ns)
    return ns


class _Stub(object):
    def config(self, **kw):
        pass

    def pack(self, *a, **k):
        pass

    def update(self, *a, **k):
        pass


class _SelftestApp(object):
    """无界面自检用的最小宿主对象。"""

    def __init__(self, db_path, key, my_qq="0"):
        self._db = db_path
        self.status_label = _Stub()
        self.root = _Stub()
        self.selected_friend = ("0", "自检好友 - 0")
        self._group_mode = False
        self._selected_is_group = False

        outer = self

        class _Var(object):
            def __init__(self, v):
                self.v = v

            def get(self):
                return self.v

        self.qq_number = _Var(my_qq)
        self.db_key = _Var(key)

    def get_db_paths(self):
        return (self._db, None)


def _pick_peer(conn, table, wanted=None):
    """选自检对象。返回 (code_str, msg_count) 或 None。

    wanted 为空时取消息最多的会话，样本更有代表性。
    """
    import qq_pdf

    if not qq_pdf._table_exists(conn, table):
        return None
    cur = conn.cursor()
    if wanted is not None:
        cur.execute('SELECT COUNT(*) FROM "%s" WHERE "40030" = ?' % table,
                    (wanted,))
        return (str(wanted), cur.fetchone()[0])
    cur.execute('SELECT "40030", COUNT(*) FROM "%s" WHERE "40030" IS NOT NULL '
                'GROUP BY "40030" ORDER BY COUNT(*) DESC LIMIT 1' % table)
    row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return (str(row[0]), row[1])


def _selftest(argv):
    db, key, out = argv[0], argv[1], argv[2]
    mode = "pdf"
    group = False
    wanted = None
    my_qq = "0"
    i = 0
    rest = argv[3:]
    while i < len(rest):
        a = rest[i]
        if a in ("pdf", "html", "both"):
            mode = a
        elif a == "--group":
            group = True
            if i + 1 < len(rest) and not rest[i + 1].startswith("-"):
                wanted = rest[i + 1]
                i += 1
        elif a == "--qq":
            if i + 1 < len(rest):
                my_qq = rest[i + 1]
                i += 1
        i += 1

    ns = _load_original()
    App = ns["QQChatExporter"]

    # 与真实运行一致：先打补丁（含 decrypt_db 的“读最新快照”补丁），再自检
    import qq_pdf
    qq_pdf.install(App)

    app = _SelftestApp(db, key, my_qq)
    app.clean_db_file = App.clean_db_file.__get__(app, _SelftestApp)
    app.decrypt_db = App.decrypt_db.__get__(app, _SelftestApp)
    app.extract_text_from_msg = App.extract_text_from_msg.__get__(app, _SelftestApp)

    table = qq_pdf.GROUP_TABLE if group else "c2c_msg_table"
    conn = app.decrypt_db(db)
    if conn is None:
        print("DECRYPT_FAILED")
        return 2
    try:
        pick = _pick_peer(conn, table, wanted)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if pick is None:
        print("NO_PEER table=%s" % table)
        return 3
    peer, cnt = pick
    if not cnt:
        print("NO_MESSAGES table=%s code=%s" % (table, peer))
        return 3

    app.selected_friend = (peer, ("自检群聊 - %s" if group else "自检好友 - %s") % peer)
    app._group_mode = group
    app._selected_is_group = group

    print("SELFTEST mode=%s table=%s peer=%s msgs=%d" % (mode, table, peer, cnt))

    stats = None
    if mode in ("html", "both"):
        html_out = out if mode == "html" else (os.path.splitext(out)[0] + ".html")
        stats = qq_pdf._do_export_html(app, html_out)
        print("HTML_OK %s %s" % (html_out, stats))
    if mode in ("pdf", "both"):
        pdf_out = out if mode == "pdf" else (os.path.splitext(out)[0] + ".pdf")
        stats = qq_pdf._do_export(app, pdf_out)
        print("PDF_OK %s %s" % (pdf_out, stats))
    return 0


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--pdf-selftest":
        return _selftest(sys.argv[2:])

    ns = _load_original()
    app_cls = ns["QQChatExporter"]
    run = ns["main"]

    try:
        import qq_pdf
        qq_pdf.install(app_cls)
    except Exception:
        # 补丁失败也不影响原功能，只是没有新按钮
        import traceback
        traceback.print_exc()

    run()


if __name__ == "__main__":
    sys.exit(main() or 0)
