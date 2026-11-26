"""GitHub Actions 에서 도는 발행 스크립트 — 예약 시각이 된 게시물을 Instagram 에 올린다 (파이썬 표준 라이브러리만).

queue/<id>.json   예약 정보 {id, kind: reel|carousel, publish_at(UTC ISO), caption, media: [경로…], cover, first_comment}
queue/<id>/…      영상·사진 (발행이 끝나면 지운다)
published/<id>.json  결과 기록 {status: published|failed, permalink, media_id, published_at, error}

영상·사진 주소는 이 실행이 체크아웃한 커밋 번호(GITHUB_SHA)가 들어간 raw.githubusercontent.com 주소를 쓴다
(캐시 때문에 옛 파일이 넘어가지 않게). 실패하면 다음 실행에서 한 번 더 해 보고, 그래도 안 되면 failed 로 남긴다.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://graph.instagram.com"
ROOT = Path(__file__).resolve().parent.parent
QUEUE, DONE = ROOT / "queue", ROOT / "published"
TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
UID = os.environ.get("IG_USER_ID", "")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
SHA = os.environ.get("GITHUB_SHA", "")
MAX_TRIES = 2


def log(msg: str) -> None:
    print(msg, flush=True)


def call(method: str, path: str, **params) -> dict:
    params["access_token"] = TOKEN
    data = urllib.parse.urlencode(params).encode()
    url = f"{API}/{path}"
    if method == "GET":
        url, data = f"{url}?{data.decode()}", None
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"Instagram API {exc.code}: {body[:400]}") from exc


def raw(rel: str) -> str:
    return f"https://raw.githubusercontent.com/{REPO}/{SHA}/{urllib.parse.quote(rel)}"


def wait_ready(container: str, limit_s: int = 600) -> None:
    """영상·캐러셀은 Instagram 이 처리할 시간이 필요하다 (FINISHED 가 될 때까지)."""
    start = time.time()
    while time.time() - start < limit_s:
        st = call("GET", container, fields="status_code,status").get("status_code")
        if st == "FINISHED":
            return
        if st in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram 이 파일을 처리하지 못함: {st}")
        time.sleep(8)
    raise RuntimeError("Instagram 처리 시간이 너무 김")


def publish(entry: dict) -> dict:
    caption = entry.get("caption", "")
    if entry["kind"] == "reel":
        p = {"media_type": "REELS", "video_url": raw(entry["media"][0]), "caption": caption, "share_to_feed": "true"}
        if entry.get("cover"):
            p["cover_url"] = raw(entry["cover"])
        container = call("POST", f"{UID}/media", **p)["id"]
        log("  릴스 등록, 처리 기다리는 중…")
        wait_ready(container)
    else:
        children = []
        for i, m in enumerate(entry["media"][:10], 1):
            c = call("POST", f"{UID}/media", image_url=raw(m), is_carousel_item="true")["id"]
            children.append(c)
            log(f"  사진 {i}/{len(entry['media'])} 등록")
        for c in children:
            wait_ready(c, 180)
        container = call("POST", f"{UID}/media", media_type="CAROUSEL", children=",".join(children),
                         caption=caption)["id"]
        wait_ready(container, 300)
    media_id = call("POST", f"{UID}/media_publish", creation_id=container)["id"]
    info = call("GET", media_id, fields="id,permalink,timestamp")
    if entry.get("first_comment"):
        try:
            call("POST", f"{media_id}/comments", message=entry["first_comment"])
        except Exception as exc:  # noqa: BLE001 — 첫 댓글 실패로 발행을 실패 처리하지 않는다
            log(f"  첫 댓글 실패: {exc}")
    return {"media_id": media_id, "permalink": info.get("permalink", ""), "timestamp": info.get("timestamp", "")}


def main() -> int:
    waiting = list(QUEUE.glob("*.json"))
    if not waiting:
        log("예약 없음")                      # 비어 있으면 조용히 끝낸다 (15분마다 실패 메일이 가지 않게)
        return 0
    if not (TOKEN and UID and REPO and SHA):
        log("설정 부족: IG_ACCESS_TOKEN / IG_USER_ID 비밀값 또는 GitHub 환경 변수가 없습니다.")
        return 1
    DONE.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    due = []
    for f in sorted(QUEUE.glob("*.json")):
        e = json.loads(f.read_text(encoding="utf-8"))
        if datetime.fromisoformat(e["publish_at"].replace("Z", "+00:00")) <= now:
            due.append((f, e))
    log(f"예약 {len(list(QUEUE.glob('*.json')))}건 중 지금 올릴 것 {len(due)}건")
    for f, e in due[:1]:                       # 한 번에 한 건 (인스타가 몰아 올리기를 싫어함)
        log(f"발행: {e['id']} ({e['kind']})")
        rec = {"id": e["id"], "kind": e["kind"], "publish_at": e["publish_at"]}
        try:
            rec.update(publish(e), status="published",
                       published_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
            log(f"  완료: {rec.get('permalink')}")
        except Exception as exc:  # noqa: BLE001
            e["tries"] = int(e.get("tries", 0)) + 1
            log(f"  실패 ({e['tries']}/{MAX_TRIES}): {exc}")
            if e["tries"] < MAX_TRIES:
                f.write_text(json.dumps(e, ensure_ascii=False, indent=2), encoding="utf-8")
                continue
            rec.update(status="failed", error=str(exc)[:500],
                       failed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        (DONE / f"{e['id']}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        f.unlink()
        shutil.rmtree(QUEUE / e["id"], ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
