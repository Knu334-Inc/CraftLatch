"""SQS から許可要求を受信し、検証して要求ファイルを置く（非特権）。"""
import argparse
import os
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

from common import (
    REQUESTS_DIR,
    ValidationError,
    allow_to_request,
    atomic_write_text,
    load_config,
    parse_json_strict,
    validate_sqs_message,
    write_request,
)

ROTATED_FILE = Path("/etc/craftlatch/aws-credentials.rotated")
MAX_BODY_BYTES = 1024
MAX_RECEIVES = 5
FAILURE_LIMIT = 5


@dataclass
class PollResult:
    received: int = 0
    written: int = 0
    deleted: int = 0
    invalid: int = 0
    failure: str | None = None


def make_sqs_client(region: str):
    import boto3
    import botocore.config

    return boto3.client(
        "sqs",
        region_name=region,
        config=botocore.config.Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 1}),
    )


def _log(line: str) -> None:
    print(line, flush=True)


def run_poll(
    sqs,
    queue_url: str,
    requests_dir: Path,
    now_fn: Callable[[], int],
    monotonic_fn: Callable[[], float],
    deadline_s: float = 35.0,
) -> PollResult:
    from botocore.exceptions import BotoCoreError, ClientError

    result = PollResult()
    start = monotonic_fn()

    def expired() -> bool:
        # API 呼び出しの直前に毎回確認する
        if monotonic_fn() - start >= deadline_s:
            result.failure = "deadline"
            return True
        return False

    try:
        for _ in range(MAX_RECEIVES):
            if expired():
                return result
            resp = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=1)
            messages = resp.get("Messages") or []
            if not messages:
                break
            result.received += len(messages)
            for m in messages:
                mid = m.get("MessageId", "-")
                handle = m["ReceiptHandle"]
                raw = m.get("Body", "")
                valid = False
                try:
                    data = raw.encode("utf-8")
                    if len(data) > MAX_BODY_BYTES:
                        raise ValidationError("本文が大きすぎます")
                    grant = validate_sqs_message(parse_json_strict(data), now_fn())
                    valid = True
                except (ValidationError, UnicodeError) as e:
                    result.invalid += 1
                    _log(f"event=invalid-message message_id={mid} reason={str(e)!r}")
                if valid:
                    try:
                        write_request(requests_dir, allow_to_request(grant), now_fn())
                    except OSError as e:
                        _log(f"event=write-failed message_id={mid} error={type(e).__name__}")
                        continue
                    result.written += 1
                if expired():
                    return result
                sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=handle)
                result.deleted += 1
    except (BotoCoreError, ClientError) as e:
        result.failure = type(e).__name__
    return result


def update_failures(state_dir: Path, failure: str | None) -> int:
    path = Path(state_dir) / "poll-failures"
    if failure is None:
        count = 0
    else:
        try:
            prev = int(path.read_text().strip())
            if prev < 0:
                prev = 0
        except (OSError, ValueError):
            prev = 0
        count = prev + 1
    atomic_write_text(path, f"{count}\n", 0o644)
    return count


def key_age_warning(rotated_file: Path, today: date, warn_days: int) -> str | None:
    try:
        rotated = date.fromisoformat(Path(rotated_file).read_text().strip())
    except (OSError, ValueError):
        return "アクセスキーのローテーション日を判定できません"
    age = (today - rotated).days
    if age >= warn_days:
        return f"アクセスキーが {age} 日経過しています（警告しきい値 {warn_days} 日）"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--import-check", action="store_true")
    args = parser.parse_args(argv)
    if args.import_check:
        print("import-ok")
        return 0
    try:
        cfg = load_config()
    except ValidationError as e:
        _log(f"event=config-error reason={str(e)!r}")
        return 1
    state_dir = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/craftlatch"))
    warning = key_age_warning(ROTATED_FILE, date.today(), cfg.key_warn_days)
    if warning:
        _log(f"event=key-age warning={warning!r}")
    sqs = make_sqs_client(cfg.region)
    r = run_poll(sqs, cfg.queue_url, REQUESTS_DIR, lambda: int(time.time()), time.monotonic)
    count = update_failures(state_dir, r.failure)
    line = f"event=poll received={r.received} written={r.written} deleted={r.deleted} invalid={r.invalid}"
    if r.failure is None:
        line += " result=ok"
    else:
        line += f" result=error failure={r.failure} failures={count}"
    _log(line)
    return 1 if count >= FAILURE_LIMIT else 0


if __name__ == "__main__":
    sys.exit(main())
