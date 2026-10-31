"""HTML レスポンスの生成。JavaScript は含めない。"""
import html

HEADERS = {
    "content-type": "text/html; charset=utf-8",
    "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'",
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
}
DELETE_COOKIE = "__Host-craftlatch=; Max-Age=0; Path=/; Secure; HttpOnly; SameSite=Lax"

_PAGES = {
    "ok": (200, "登録しました", "IP アドレス {ip} を登録しました。通常 1 分程度 + サーバー起動時間をお待ちください。"),
    "relogin": (400, "期限切れ・処理済みです", "期限切れまたは処理済みです。最初からログインし直してください。"),
    "forbidden": (403, "権限がありません", "このアカウントにはサーバーへ接続する権限がありません。"),
    "not_ipv4": (400, "IPv4 以外からのアクセスです", "IPv4 以外からのアクセスです。IPv4 で接続して再度お試しください。"),
    "account": (500, "アカウント設定に問題があります", "アカウント設定に問題があります。管理者に連絡してください。"),
    "temporary": (503, "一時的なエラー", "一時的なエラーが発生しました。しばらくしてから再試行してください。"),
}


def _html(title: str, message: str) -> str:
    return (
        '<!doctype html><html lang="ja"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>CraftLatch</title></head><body><h1>{html.escape(title)}</h1>"
        f"<p>{html.escape(message)}</p></body></html>"
    )


def render(kind: str, ip: str | None = None) -> dict:
    status, title, message = _PAGES[kind]
    return {
        "statusCode": status,
        "headers": dict(HEADERS),
        "cookies": [DELETE_COOKIE],
        "body": _html(title, message.format(ip=ip or "")),
    }


def not_found() -> dict:
    return {"statusCode": 404, "headers": dict(HEADERS), "body": _html("見つかりません", "ページが見つかりません。")}
