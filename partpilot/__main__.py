"""python -m partpilot 的命令列入口；業務操作交給 Service。"""
import argparse
import getpass
import json
from pathlib import Path
import sys

from .auth import init_users
from .config import load_config
from .service import Service
from .answers import render


class ChineseParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, "錯誤：命令列參數無效：" + message + "\n")


def parser():
    root = ChineseParser(description="PartPilot 公司內部物料查詢與管理")
    commands = root.add_subparsers(dest="command", required=True, title="子指令")
    commands.add_parser("init-users", help="以 PARTPILOT_INIT_PASSWORD 初始化帳號")
    clean = commands.add_parser("clean", help="admin 執行資料清理")
    clean.add_argument("--user", required=True)
    ask = commands.add_parser("ask", help="依角色權限進行多輪自然語言查詢")
    ask.add_argument("--user", required=True)
    ask.add_argument("--conv", help="接續自己的對話 ID")
    ask.add_argument("--json", action="store_true", help="輸出完整 JSON 結果")
    ask.add_argument("--question", dest="question_option", help="問題（亦可使用位置參數）")
    ask.add_argument("question", nargs="?", help="問題；省略時進入互動模式")
    review = commands.add_parser("review", help="component 列出或核准待確認資料")
    review.add_argument("--user", required=True)
    decision = review.add_mutually_exclusive_group()
    decision.add_argument("--approve", metavar="ID")
    decision.add_argument("--reject", metavar="ID")
    review.add_argument("--choice", type=int, metavar="N", help="候選索引（從 0 開始）")
    audit = commands.add_parser("audit", help="admin 查看稽核與 token 用量彙總")
    audit.add_argument("--user", required=True)
    audit.add_argument("--limit", type=int, default=200)
    serve = commands.add_parser("serve", help="啟動內部網頁")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=18765)
    return root


def _json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        cfg = load_config()
        service = Service(cfg)
        if args.command == "init-users":
            count = init_users(str(Path(cfg.state_dir) / "users.json"), cfg.init_password)
            print("已建立 {} 個帳號".format(count))
            return 0
        if args.command == "serve":
            from .web import serve
            serve(service, args.host, args.port)
            return 0
        password = cfg.password
        if not password and cfg.mode == "demo":
            password = cfg.init_password
        password = password or getpass.getpass("密碼：")
        user = service.login(args.user, password)
        if user is None:
            raise PermissionError("登入失敗：帳號或密碼錯誤")
        if args.command == "clean":
            _json(service.run_clean(user))
        elif args.command == "review":
            if args.approve or args.reject:
                _json(service.decide(user, args.approve or args.reject,
                                     "approve" if args.approve else "reject", args.choice))
            else:
                _json({"review": service.review_items(user), "approvals": service.own_approvals(user)})
        elif args.command == "audit":
            if args.limit < 0:
                raise ValueError("limit 不可小於零")
            rows = service.audit_view(user, args.limit)
            _json({"audit": rows, "usage": service.usage_summary(user)})
        elif args.command == "ask":
            conv_id = args.conv

            def ask(question):
                nonlocal conv_id
                result = service.agent(user).ask(question, conv_id)
                conv_id = result["conv_id"]
                if args.json:
                    _json(result)
                else:
                    print(render(result["summary"], result["raw_results"]))
                print("conv_id={} tokens={}".format(conv_id, json.dumps(result["usage"], ensure_ascii=False)), file=sys.stderr)

            question = args.question_option if args.question_option is not None else args.question
            if question is not None:
                ask(question)
            else:
                print("輸入問題；輸入 exit 或 quit 結束，/new 開新對話。", file=sys.stderr)
                while True:
                    try:
                        question = input("問題：" if sys.stdin.isatty() else "")
                    except EOFError:
                        break
                    if question.strip().lower() in {"exit", "quit"}:
                        break
                    if question.strip() == "/new":
                        conv_id = None
                    elif question.strip():
                        ask(question)
        return 0
    except KeyboardInterrupt:
        print("已中止操作", file=sys.stderr)
        return 130
    except Exception as exc:
        print("錯誤：" + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
