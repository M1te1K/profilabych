"""Small AST arithmetic evaluator; no arbitrary code execution."""

import ast
import operator
from collections.abc import Callable
from decimal import Decimal, DecimalException, localcontext

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from profilabych.tools.registry import ToolContext, ToolError

_OPERATIONS: dict[type[ast.operator], Callable[[Decimal, Decimal], Decimal]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_MAX_VALUE = Decimal("1e15")


class CalculatorArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expression: str = Field(min_length=1, max_length=256)


def calculate(expression: str) -> str:
    """Evaluate only numeric literals, + - * / and unary signs."""
    if not expression.strip() or len(expression) > 256:
        raise ToolError("invalid_expression", "Выражение должно содержать от 1 до 256 символов.")
    expression = expression.strip()
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        raise ToolError("invalid_expression", "Некорректное арифметическое выражение.") from None
    if sum(1 for _ in ast.walk(tree)) > 64:
        raise ToolError("expression_too_complex", "Выражение слишком сложное.")

    def visit(node: ast.AST, depth: int = 0) -> Decimal:
        if depth > 16:
            raise ToolError("expression_too_complex", "Слишком большая вложенность выражения.")
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            source = ast.get_source_segment(expression, node)
            assert source is not None
            value = Decimal(source)
        elif isinstance(node, ast.BinOp) and type(node.op) in _OPERATIONS:
            left, right = visit(node.left, depth + 1), visit(node.right, depth + 1)
            if isinstance(node.op, ast.Div) and right == 0:
                raise ToolError("division_by_zero", "Деление на ноль запрещено.")
            value = _OPERATIONS[type(node.op)](left, right)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand, depth + 1)
            if isinstance(node.op, ast.USub):
                value = -value
        else:
            raise ToolError(
                "invalid_expression", "Разрешены только числа, скобки и операции + - * /."
            )
        if not value.is_finite() or abs(value) > _MAX_VALUE:
            raise ToolError(
                "number_out_of_range", "Числа и результаты должны быть не больше 10^15 по модулю."
            )
        return value

    try:
        with localcontext() as context:
            context.prec = 28
            value = visit(tree.body)
            if not value:
                return "0"
            # Bound expansion of tiny scientific literals as well as huge ones.
            if value and value.adjusted() < -28:
                raise ToolError(
                    "number_out_of_range", "Результат слишком мал для поддерживаемой точности."
                )
            rendered = format(value, "f")
            return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    except DecimalException:
        raise ToolError("invalid_expression", "Некорректное или слишком большое число.") from None


async def run_calculator(
    arguments: CalculatorArguments, context: ToolContext
) -> dict[str, JsonValue]:
    return {"result": calculate(arguments.expression)}
