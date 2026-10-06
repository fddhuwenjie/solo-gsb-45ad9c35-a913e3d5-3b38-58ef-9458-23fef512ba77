"""工作单输入迁移（``POST /worksheet-migrate``）。

把现有的 2～5 步工作单从旧版**已发布**定义集迁移到新版定义集：

- **开始时固定实际指向**：旧/新两侧引用（定义集标识或活动别名）在任何
  步骤执行前各解析一次（与 :mod:`app.diagnose` 共用
  :func:`~app.diagnose.resolve_side`），随后两侧都只使用解析到的那一版
  定义；执行期间别名被切换不会造成单侧混版。整个过程只读，不修改定义集
  或别名。
- **先在旧定义下执行**：旧工作单在旧定义集下完整执行一遍；初始变量无法
  解析或任一步骤失败都返回 422（``migration_failed``），并指出变量或
  步骤。
- **改写每个初始变量**：单位表达式保持不变，把数值改写为“在新定义下
  与旧定义表示同一物理量”的精确十进制值——SI 口径精确分数、量纲与
  绝对温度属性三者一致。新定义下单位无法解析、量纲或绝对温度属性不
  兼容、改写值不是有限十进制（或超过精度上限）都返回 422 并指出变量。
- **改写后执行整张新工作单**：用改写后的变量在新定义集下再执行一遍，
  逐步核对两侧的内部精确量、量纲与绝对温度属性（显示值允许变化——
  步骤 ``output_unit`` 中的自定义符号在新定义下含义可能不同）。新工作
  单任一步出错或某步结果含义变化都返回 422 并指出步骤。
- 全部通过后返回可直接交给 ``POST /worksheet`` 的新工作单：步骤原样
  保留，``custom_unit_set_id`` 引用固定下来的新定义集标识。

任何失败都不返回迁移工作单；``migration_failed`` 的 message 中指出
出问题的变量名或步骤（下标与结果名）及底层原因。
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

from .converter import convert_fraction, is_absolute_temperature, to_decimal_str
from .diagnose import resolve_side
from .errors import ConversionError, migration_failed
from .evaluator import make_quantity
from .parser import ParsedExpression, canonical_dim, parse_expression
from .schemas import (
    MigrateSide,
    MigrateStepCheck,
    MigratedVariable,
    MigratedWorksheet,
    Variable,
    WorksheetMigrateRequest,
    WorksheetMigrateResponse,
    WorksheetRequest,
)
from .unit_sets import UnitSetStore, get_store
from .worksheet import WorksheetRun, collect_worksheet_run

#: 绝对温度变量在内部换算时使用的源单位项（开尔文），与 worksheet 一致。
_KELVIN_TERMS = (("K", 1),)


def _variable_error(name: str, reason: str) -> ConversionError:
    return migration_failed(f"初始变量 {name!r} 无法迁移: {reason}")


def _step_error(index: int, name: str, reason: str) -> ConversionError:
    return migration_failed(f"第 {index} 步 {name!r}: {reason}")


def _underlying(exc: ConversionError) -> str:
    return f"{exc.code}: {exc.message}"


def _ensure_old_run_ok(
    req: WorksheetMigrateRequest, run: WorksheetRun, old_scope
) -> None:
    """旧定义下的执行结果必须全部成功，否则抛出 422 并指出变量或步骤。"""

    if run.setup_error is not None:
        # 初始变量解析失败：逐个重解析以定位第一个失败的变量
        for name, var in req.variables.items():
            try:
                parse_expression(var.unit, old_scope)
            except ConversionError:
                raise _variable_error(
                    name,
                    f"单位 {var.unit!r} 在旧定义集下无法解析"
                    f"（{_underlying(run.setup_error)}）",
                ) from None
        raise migration_failed(
            "旧定义集下工作单初始变量解析失败"
            f"（{_underlying(run.setup_error)}）"
        ) from None
    for index, record in enumerate(run.steps):
        if record.error is not None:
            raise _step_error(
                index,
                req.steps[index].name,
                f"工作单在旧定义集下执行失败（{_underlying(record.error)}）",
            ) from None


def _rewrite_variables(
    req: WorksheetMigrateRequest, old_scope, new_scope
) -> dict[str, MigratedVariable]:
    """把每个初始变量改写为新定义下同一单位表达式、同一物理量的精确十进制值。

    单位表达式保持不变：新值 = 旧定义下的 SI 口径精确量在新定义解析出的
    同一单位下的数值。量纲或绝对温度属性不兼容、新定义下单位无法解析、
    改写值无法精确十进制表示都抛出 422 并指出变量。
    """

    rewritten: dict[str, MigratedVariable] = {}
    for name, var in req.variables.items():
        # 旧定义下的解析已在旧工作单执行时验证通过
        old_unit = parse_expression(var.unit, old_scope)
        try:
            new_unit = parse_expression(var.unit, new_scope)
        except ConversionError as exc:
            raise _variable_error(
                name,
                f"单位 {var.unit!r} 在新定义集下无法解析（{_underlying(exc)}）",
            ) from None
        if new_unit.dim != old_unit.dim:
            raise _variable_error(
                name,
                f"单位 {var.unit!r} 在新定义集下量纲不兼容: "
                f"旧量纲 {canonical_dim(old_unit.dim)!r}, "
                f"新量纲 {canonical_dim(new_unit.dim)!r}",
            )
        if is_absolute_temperature(new_unit) != is_absolute_temperature(old_unit):
            raise _variable_error(
                name, f"单位 {var.unit!r} 在新定义集下绝对温度属性发生变化"
            )
        old_quantity = make_quantity(var.value, old_unit)
        if old_quantity.absolute:
            # 绝对温度：以开尔文口径走仿射换算（与 /evaluate 一致）
            src = ParsedExpression(
                dim=old_quantity.dim, scale=Fraction(1), terms=_KELVIN_TERMS
            )
        else:
            # 线性量（含温差）：以 SI 基单位口径构造源表达式
            src = ParsedExpression(
                dim=old_quantity.dim, scale=Fraction(1), terms=()
            )
        new_value = convert_fraction(old_quantity.value_si, src, new_unit)
        try:
            new_value_str = to_decimal_str(new_value)
        except ConversionError:
            raise _variable_error(
                name,
                "改写值在新定义集下不是有限十进制或超过精度上限，"
                "无法精确表示",
            ) from None
        rewritten[name] = MigratedVariable(value=new_value_str, unit=var.unit)
    return rewritten


def _ensure_new_run_ok(req: WorksheetMigrateRequest, run: WorksheetRun) -> None:
    """改写后的工作单在新定义下必须全部成功，否则抛出 422 并指出步骤。"""

    if run.setup_error is not None:
        # 改写后的变量均已在新定义下验证过，正常不会到达这里
        raise migration_failed(
            "改写后的工作单在新定义集下初始变量解析失败"
            f"（{_underlying(run.setup_error)}）"
        ) from None
    for index, record in enumerate(run.steps):
        if record.error is not None:
            raise _step_error(
                index,
                req.steps[index].name,
                "改写后的工作单在新定义集下执行失败"
                f"（{_underlying(record.error)}）",
            ) from None


def _check_meaning(
    req: WorksheetMigrateRequest, old_run: WorksheetRun, new_run: WorksheetRun
) -> list[MigrateStepCheck]:
    """逐步核对两侧内部精确量、量纲与绝对温度属性；显示值允许变化。"""

    checks: list[MigrateStepCheck] = []
    for index, step in enumerate(req.steps):
        old_quantity = old_run.steps[index].quantity
        new_quantity = new_run.steps[index].quantity
        assert old_quantity is not None and new_quantity is not None
        differences: list[str] = []
        if old_quantity.value_si != new_quantity.value_si:
            differences.append("内部精确数值")
        if old_quantity.dim != new_quantity.dim:
            differences.append("量纲")
        if old_quantity.absolute != new_quantity.absolute:
            differences.append("绝对温度属性")
        if differences:
            raise _step_error(
                index,
                step.name,
                "结果含义在新定义集下发生变化（"
                + "、".join(differences)
                + "不同）",
            ) from None
        checks.append(
            MigrateStepCheck(
                index=index,
                name=step.name,
                old=old_run.steps[index].result,
                new=new_run.steps[index].result,
            )
        )
    return checks


def migrate_worksheet(
    req: WorksheetMigrateRequest, *, store: UnitSetStore | None = None
) -> WorksheetMigrateResponse:
    """执行工作单输入迁移并组装 200 响应；任何失败抛出 422（不返回迁移工作单）。"""

    chosen = store or get_store()

    # 1) 请求开始时分别固定两侧的实际指向（别名各解析一次），随后两侧
    #    各用各的独立 scope；整个迁移只读，不修改定义集或别名。任一侧
    #    引用不存在都在此抛出 422（不执行任何步骤）。
    old_resolved = resolve_side(req.old, chosen)
    new_resolved = resolve_side(req.new, chosen)

    # 2) 先在旧定义下执行整张工作单；初始变量或任一步失败即 422
    old_run = collect_worksheet_run(req, old_resolved.scope)
    _ensure_old_run_ok(req, old_run, old_resolved.scope)

    # 3) 把每个初始变量改写为新定义下同一单位表达式、同一物理量的精确值
    rewritten = _rewrite_variables(req, old_resolved.scope, new_resolved.scope)

    # 4) 用改写后的变量在新定义下执行整张工作单（步骤与原工作单一致）
    new_req = WorksheetRequest(
        variables={
            name: Variable(value=Decimal(migrated.value), unit=migrated.unit)
            for name, migrated in rewritten.items()
        },
        steps=req.steps,
        custom_unit_set_id=new_resolved.resolved_id,
    )
    new_run = collect_worksheet_run(new_req, new_resolved.scope)
    _ensure_new_run_ok(req, new_run)

    # 5) 逐步核对内部精确量、量纲与绝对温度属性（显示值允许变化）
    checks = _check_meaning(req, old_run, new_run)

    # 6) 全部通过：返回可直接交给 /worksheet 的新工作单
    return WorksheetMigrateResponse(
        old=MigrateSide(reference=req.old, resolved_id=old_resolved.resolved_id),
        new=MigrateSide(reference=req.new, resolved_id=new_resolved.resolved_id),
        worksheet=MigratedWorksheet(
            variables=rewritten,
            steps=req.steps,
            custom_unit_set_id=new_resolved.resolved_id,
        ),
        steps=checks,
    )
