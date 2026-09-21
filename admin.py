"""
Công cụ admin để duyệt yêu cầu rút USDT.

  python admin.py list [pending|paid|rejected]
  python admin.py paid ID 0xTXHASH        # đã chuyển USDT, ghi lại mã giao dịch
  python admin.py reject ID "lý do"       # từ chối, xu được hoàn lại cho người dùng

Cần ADMIN_TOKEN (và tuỳ chọn SERVER_URL) trong .env hoặc biến môi trường.
"""
import os
import sys

import requests

here = os.path.dirname(os.path.abspath(__file__))
env_file = os.path.join(here, ".env")
if os.path.exists(env_file):
    for line in open(env_file, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))

BASE = os.environ.get("SERVER_URL", "http://127.0.0.1:%s" % os.environ.get("PORT", 8000)).rstrip("/")
HEAD = {"X-Admin-Token": os.environ.get("ADMIN_TOKEN", "")}


def show(r):
    try:
        j = r.json()
    except ValueError:
        sys.exit("Lỗi %s: %s" % (r.status_code, r.text[:200]))
    if not j.get("ok"):
        sys.exit("Lỗi: %s" % (j.get("message") or j))
    return j


def main(argv):
    if len(argv) < 2 or argv[1] not in ("list", "paid", "reject"):
        sys.exit(__doc__)
    cmd = argv[1]
    if cmd == "list":
        status = argv[2] if len(argv) > 2 else "pending"
        j = show(requests.get(BASE + "/admin/withdrawals", params={"status": status}, headers=HEAD, timeout=15))
        if not j["withdrawals"]:
            print("Không có yêu cầu nào (%s)." % status)
        for w in j["withdrawals"]:
            flag = "  [!] ví dùng chung với %d tài khoản khác" % w["shared_with_other_accounts"] if w["shared_with_other_accounts"] else ""
            print("#%d  %s USDT (%d xu)  %s (%d)  %s%s" % (w["id"], w["usdt"], w["units"], w["user"], w["user_id"], w["address"], flag))
    elif cmd == "paid":
        if len(argv) < 4:
            sys.exit("Dùng: python admin.py paid ID 0xTXHASH")
        show(requests.post("%s/admin/withdrawals/%s/paid" % (BASE, argv[2]), json={"txhash": argv[3]}, headers=HEAD, timeout=15))
        print("Đã đánh dấu đã chuyển.")
    else:
        if len(argv) < 3:
            sys.exit('Dùng: python admin.py reject ID "lý do"')
        note = argv[3] if len(argv) > 3 else ""
        show(requests.post("%s/admin/withdrawals/%s/reject" % (BASE, argv[2]), json={"note": note}, headers=HEAD, timeout=15))
        print("Đã từ chối và hoàn xu.")


if __name__ == "__main__":
    main(sys.argv)
