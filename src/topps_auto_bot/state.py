from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from .models import AddToCartState, DomainError, SubmissionState, UTC, utc_now_iso
from .purchase import SubmissionRecord

RUN_STATE_SCHEMA_VERSION = 2
FLOW_FULL_BASELINE = "full_baseline"
FLOW_LEGACY_PREPARED = "legacy_prepared"
_TOP_KEYS = {"schema_version", "flow_type", "add_to_cart", "add_to_cart_attempts", "purchase", "updated_at_utc"}
_ADD_KEYS = {"schema_version", "task_id", "state", "created_at_utc", "updated_at_utc", "target", "note", "attempt_seq"}
_TARGET_KEYS = {"title", "variant", "quantity"}
_PENDING_ADD_STATES = (AddToCartState.IN_FLIGHT, AddToCartState.UNKNOWN)


class RunStateStatus(StrEnum):
    MISSING = "MISSING"
    OK = "OK"
    CORRUPT = "CORRUPT"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    IO_ERROR = "IO_ERROR"


class RunStateBlocked(RuntimeError):
    pass


class RunStateStoreError(RuntimeError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise DomainError("运行状态 JSON 包含重复字段")
    return obj


@dataclass(frozen=True)
class AddToCartRecord:
    schema_version: int
    task_id: str
    state: AddToCartState
    created_at_utc: str
    updated_at_utc: str
    target_title: str
    target_variant: str
    target_quantity: int
    note: str | None = None
    attempt_seq: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "state": str(self.state),
            "created_at_utc": self.created_at_utc,
            "updated_at_utc": self.updated_at_utc,
            "target": {
                "title": self.target_title,
                "variant": self.target_variant,
                "quantity": self.target_quantity,
            },
            "note": self.note,
            "attempt_seq": self.attempt_seq,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AddToCartRecord":
        if set(payload) - _ADD_KEYS:
            raise DomainError("加购记录包含不允许的字段")
        target = payload.get("target")
        if not isinstance(target, Mapping) or set(target) - _TARGET_KEYS:
            raise DomainError("加购记录目标字段无效")
        try:
            state = AddToCartState(str(payload["state"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise DomainError("加购记录必要字段无效") from exc
        version = payload.get("schema_version")
        if type(version) is not int or version != RUN_STATE_SCHEMA_VERSION:
            raise DomainError("加购记录版本不受支持")
        quantity = target.get("quantity")
        if type(quantity) is not int or quantity <= 0:
            raise DomainError("加购记录数量无效")
        attempt_seq = payload.get("attempt_seq", 1)
        if isinstance(attempt_seq, bool) or not isinstance(attempt_seq, int) or attempt_seq <= 0:
            raise DomainError("加购尝试序号无效")
        for key in ("task_id", "created_at_utc", "updated_at_utc"):
            if not isinstance(payload.get(key), str) or not payload[key]:
                raise DomainError(f"加购记录字段 {key} 无效")
        for key in ("title", "variant"):
            if not isinstance(target.get(key), str) or not target[key].strip():
                raise DomainError(f"加购记录字段 {key} 无效")
        note = payload.get("note")
        if note is not None and not isinstance(note, str):
            raise DomainError("加购记录 note 必须是文本或 null")
        return cls(
            schema_version=version,
            task_id=payload["task_id"],
            state=state,
            created_at_utc=payload["created_at_utc"],
            updated_at_utc=payload["updated_at_utc"],
            target_title=target["title"],
            target_variant=target["variant"],
            target_quantity=quantity,
            note=note,
            attempt_seq=attempt_seq,
        )


@dataclass(frozen=True)
class RunStateSnapshot:
    status: RunStateStatus
    flow_type: str | None = None
    add_to_cart: AddToCartRecord | None = None
    add_to_cart_attempts: tuple[AddToCartRecord, ...] = ()
    purchase: SubmissionRecord | None = None
    error: str | None = None

    @property
    def blocking(self) -> bool:
        if self.status in (
            RunStateStatus.CORRUPT,
            RunStateStatus.UNSUPPORTED_VERSION,
            RunStateStatus.IO_ERROR,
        ):
            return True
        if self.add_to_cart is not None and self.add_to_cart.state in _PENDING_ADD_STATES:
            return True
        if self.purchase is not None and self.purchase.state in (
            SubmissionState.SUBMITTING,
            SubmissionState.UNKNOWN,
        ):
            return True
        return False


class RunStateStore:
    """Combined schema-2 run state while retaining schema-1 purchase records."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> RunStateSnapshot:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return RunStateSnapshot(RunStateStatus.MISSING)
        except UnicodeDecodeError:
            return RunStateSnapshot(RunStateStatus.CORRUPT, error="运行状态不是有效 UTF-8")
        except OSError as exc:
            return RunStateSnapshot(RunStateStatus.IO_ERROR, error=str(exc))
        try:
            payload = json.loads(text, object_pairs_hook=_unique_object)
        except (ValueError, RecursionError) as exc:
            return RunStateSnapshot(RunStateStatus.CORRUPT, error=f"运行状态不是有效 JSON：{exc}")
        if not isinstance(payload, Mapping):
            return RunStateSnapshot(RunStateStatus.CORRUPT, error="运行状态根节点必须是对象")
        version = payload.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int):
            return RunStateSnapshot(RunStateStatus.CORRUPT, error="运行状态缺少有效版本")
        if version > RUN_STATE_SCHEMA_VERSION:
            return RunStateSnapshot(
                RunStateStatus.UNSUPPORTED_VERSION,
                error=f"运行状态版本 {version} 高于当前支持版本",
            )
        if version == 1:
            try:
                purchase = SubmissionRecord.from_dict(payload)
            except (ValueError, TypeError, OverflowError) as exc:
                return RunStateSnapshot(
                    RunStateStatus.CORRUPT,
                    error=f"旧购买记录无法读取：{exc}",
                )
            return RunStateSnapshot(
                RunStateStatus.OK,
                flow_type=FLOW_LEGACY_PREPARED,
                purchase=purchase,
            )
        if version != RUN_STATE_SCHEMA_VERSION:
            return RunStateSnapshot(
                RunStateStatus.UNSUPPORTED_VERSION,
                error=f"不支持运行状态版本 {version}",
            )
        if set(payload) - _TOP_KEYS:
            return RunStateSnapshot(RunStateStatus.CORRUPT, error="运行状态包含不允许的字段")
        if not (_TOP_KEYS - {"add_to_cart_attempts"}) <= payload.keys():
            return RunStateSnapshot(RunStateStatus.CORRUPT, error="运行状态缺少必要字段")
        updated = payload["updated_at_utc"]
        if not isinstance(updated, str) or not updated:
            return RunStateSnapshot(RunStateStatus.CORRUPT, error="运行状态更新时间无效")
        flow_type = payload.get("flow_type")
        if flow_type not in (FLOW_FULL_BASELINE, FLOW_LEGACY_PREPARED):
            return RunStateSnapshot(RunStateStatus.CORRUPT, error="运行状态流程类型无效")
        add_to_cart = None
        purchase = None
        add_payload = payload["add_to_cart"]
        purchase_payload = payload["purchase"]
        try:
            if add_payload is not None:
                if not isinstance(add_payload, Mapping):
                    raise DomainError("加购记录必须是对象或 null")
                add_to_cart = AddToCartRecord.from_dict(add_payload)
            if "add_to_cart_attempts" not in payload:
                # The legacy schema-2 writer stored only the first attempt.
                if add_to_cart is not None and add_to_cart.attempt_seq != 1:
                    raise DomainError("多次加购的运行状态缺少尝试历史")
                attempts = (add_to_cart,) if add_to_cart is not None else ()
            else:
                attempts_payload = payload["add_to_cart_attempts"]
                if not isinstance(attempts_payload, list) or not all(
                    isinstance(item, Mapping) for item in attempts_payload
                ):
                    raise DomainError("加购尝试记录必须是数组")
                attempts = tuple(AddToCartRecord.from_dict(item) for item in attempts_payload)
                if (attempts[-1] if attempts else None) != add_to_cart:
                    raise DomainError("当前加购记录与最新尝试不一致")
                if any(attempt.state in _PENDING_ADD_STATES for attempt in attempts[:-1]):
                    raise DomainError("加购历史包含未决的旧尝试")
                if len({(attempt.task_id, attempt.attempt_seq) for attempt in attempts}) != len(attempts):
                    raise DomainError("加购历史包含重复的尝试序号")
            if purchase_payload is not None:
                if not isinstance(purchase_payload, Mapping):
                    raise DomainError("购买记录必须是对象或 null")
                purchase = SubmissionRecord.from_dict(purchase_payload)
        except (ValueError, TypeError, OverflowError) as exc:
            return RunStateSnapshot(RunStateStatus.CORRUPT, error=str(exc))
        return RunStateSnapshot(
            RunStateStatus.OK,
            flow_type=flow_type,
            add_to_cart=add_to_cart,
            add_to_cart_attempts=attempts,
            purchase=purchase,
        )

    def _snapshot_dict(
        self,
        *,
        add_to_cart: AddToCartRecord | None,
        purchase: SubmissionRecord | None,
        flow_type: str = FLOW_FULL_BASELINE,
        now: datetime,
        add_to_cart_attempts: tuple[AddToCartRecord, ...] = (),
    ) -> dict[str, Any]:
        return {
            "schema_version": RUN_STATE_SCHEMA_VERSION,
            "flow_type": flow_type,
            "add_to_cart": add_to_cart.to_dict() if add_to_cart is not None else None,
            "add_to_cart_attempts": [record.to_dict() for record in add_to_cart_attempts],
            "purchase": purchase.to_dict() if purchase is not None else None,
            "updated_at_utc": utc_now_iso(now),
        }

    def _write(
        self,
        *,
        add_to_cart: AddToCartRecord | None,
        purchase: SubmissionRecord | None,
        flow_type: str,
        now: datetime,
        add_to_cart_attempts: tuple[AddToCartRecord, ...] = (),
    ) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=str(self._path.parent),
            text=True,
        )
        os.close(descriptor)
        temp_path = Path(raw_path)
        try:
            with open(temp_path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    self._snapshot_dict(
                        add_to_cart=add_to_cart,
                        purchase=purchase,
                        flow_type=flow_type,
                        now=now,
                        add_to_cart_attempts=add_to_cart_attempts,
                    ),
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                temp_path.chmod(0o600)
            except OSError:
                pass
            os.replace(temp_path, self._path)
            try:
                self._path.chmod(0o600)
            except OSError:
                pass
        except Exception as exc:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise RunStateStoreError(f"运行状态保存失败：{exc}") from exc

    def _loaded_ok(self) -> RunStateSnapshot:
        snapshot = self.load()
        if snapshot.status not in (RunStateStatus.MISSING, RunStateStatus.OK):
            raise RunStateBlocked("运行状态不可用：" + (snapshot.error or snapshot.status))
        if snapshot.blocking:
            raise RunStateBlocked("存在未决加购或购买提交，必须先核对")
        return snapshot

    def begin_add_to_cart(self, record: AddToCartRecord, *, now: datetime) -> None:
        snapshot = self._loaded_ok()
        if any(
            attempt.task_id == record.task_id and attempt.attempt_seq == record.attempt_seq
            for attempt in snapshot.add_to_cart_attempts
        ):
            raise RunStateBlocked("加购尝试序号已存在")
        self._write(
            add_to_cart=record,
            purchase=snapshot.purchase,
            flow_type=FLOW_FULL_BASELINE,
            now=now,
            add_to_cart_attempts=snapshot.add_to_cart_attempts + (record,),
        )

    def update_add_to_cart(self, record: AddToCartRecord, *, now: datetime) -> AddToCartRecord:
        snapshot = self.load()
        if snapshot.status is not RunStateStatus.OK:
            raise RunStateStoreError("没有可更新的运行状态")
        current = snapshot.add_to_cart
        if current is None or (current.task_id, current.attempt_seq) != (record.task_id, record.attempt_seq):
            raise RunStateStoreError("加购记录任务或尝试序号不匹配")
        if current.state is not AddToCartState.IN_FLIGHT:
            raise RunStateStoreError("只能自动更新当前在途加购")
        updated = replace(record, updated_at_utc=utc_now_iso(now))
        self._write(
            add_to_cart=updated,
            purchase=snapshot.purchase,
            flow_type=FLOW_FULL_BASELINE,
            now=now,
            add_to_cart_attempts=snapshot.add_to_cart_attempts[:-1] + (updated,),
        )
        return updated

    def begin_purchase(self, record: SubmissionRecord, *, now: datetime) -> None:
        snapshot = self._loaded_ok()
        if snapshot.add_to_cart is None or snapshot.add_to_cart.state is not AddToCartState.CONFIRMED:
            raise RunStateBlocked("没有已确认的本次加购，不能进入购买提交")
        self._write(
            add_to_cart=snapshot.add_to_cart,
            purchase=record,
            flow_type=FLOW_FULL_BASELINE,
            now=now,
            add_to_cart_attempts=snapshot.add_to_cart_attempts,
        )

    def update_purchase(self, record: SubmissionRecord, *, now: datetime) -> SubmissionRecord:
        snapshot = self.load()
        if snapshot.status is not RunStateStatus.OK:
            raise RunStateStoreError("没有可更新的运行状态")
        if snapshot.purchase is None or snapshot.purchase.task_id != record.task_id:
            raise RunStateStoreError("购买记录任务不匹配")
        updated = replace(record, updated_at_utc=utc_now_iso(now))
        self._write(
            add_to_cart=snapshot.add_to_cart,
            purchase=updated,
            flow_type=snapshot.flow_type or FLOW_FULL_BASELINE,
            now=now,
            add_to_cart_attempts=snapshot.add_to_cart_attempts,
        )
        return updated

    def resolve_add_to_cart(
        self,
        *,
        state: AddToCartState,
        note: str,
        now: datetime,
    ) -> AddToCartRecord:
        if state not in (AddToCartState.CONFIRMED, AddToCartState.FAILED, AddToCartState.NOT_SENT):
            raise RunStateStoreError("人工加购结论必须是成功、失败或未形成")
        snapshot = self.load()
        if snapshot.status is not RunStateStatus.OK or snapshot.add_to_cart is None:
            raise RunStateStoreError("没有可解除的加购记录")
        if snapshot.add_to_cart.state not in _PENDING_ADD_STATES:
            raise RunStateStoreError("加购记录当前未处于阻断状态")
        updated = replace(snapshot.add_to_cart, state=state, note=note, updated_at_utc=utc_now_iso(now))
        attempts = tuple(
            updated
            if attempt.task_id == updated.task_id and attempt.attempt_seq == updated.attempt_seq
            else attempt
            for attempt in snapshot.add_to_cart_attempts
        )
        self._write(
            add_to_cart=updated,
            purchase=snapshot.purchase,
            flow_type=snapshot.flow_type or FLOW_FULL_BASELINE,
            now=now,
            add_to_cart_attempts=attempts,
        )
        return updated

    def resolve_purchase(
        self,
        *,
        state: SubmissionState,
        note: str,
        now: datetime,
    ) -> SubmissionRecord:
        if state not in (
            SubmissionState.SUCCEEDED,
            SubmissionState.DECLINED,
            SubmissionState.NOT_SUBMITTED,
        ):
            raise RunStateStoreError("人工付款结论必须是成功、拒绝或未形成订单")
        snapshot = self.load()
        if snapshot.status is not RunStateStatus.OK or snapshot.purchase is None:
            raise RunStateStoreError("没有可解除的购买记录")
        if snapshot.purchase.state not in (SubmissionState.SUBMITTING, SubmissionState.UNKNOWN):
            raise RunStateStoreError("购买记录当前未处于阻断状态")
        updated = replace(
            snapshot.purchase,
            state=state,
            outcome=state.value,
            note=note,
            updated_at_utc=utc_now_iso(now),
        )
        self._write(
            add_to_cart=snapshot.add_to_cart,
            purchase=updated,
            flow_type=snapshot.flow_type or FLOW_LEGACY_PREPARED,
            now=now,
            add_to_cart_attempts=snapshot.add_to_cart_attempts,
        )
        return updated


def build_add_to_cart_record(
    *,
    task_id: str,
    target,
    now: datetime,
    note: str | None = None,
    attempt_seq: int = 1,
) -> AddToCartRecord:
    return AddToCartRecord(
        schema_version=RUN_STATE_SCHEMA_VERSION,
        task_id=task_id,
        state=AddToCartState.IN_FLIGHT,
        created_at_utc=utc_now_iso(now),
        updated_at_utc=utc_now_iso(now),
        target_title=target.title,
        target_variant=target.variant,
        target_quantity=target.quantity,
        note=note,
        attempt_seq=attempt_seq,
    )
