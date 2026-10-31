"""Bounded arithmetic/boolean expressions. No Python eval, attributes or I/O."""
import ast
import math
import operator

FUNCTIONS = {'abs': abs, 'min': min, 'max': max, 'sqrt': math.sqrt,
             'log': math.log, 'log10': math.log10}
RESERVED = set(FUNCTIONS) | {'coalesce', 'is_valid'}
BINARY = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
          ast.Div: operator.truediv, ast.Mod: operator.mod}
COMPARE = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt,
           ast.GtE: operator.ge, ast.Eq: operator.eq, ast.NotEq: operator.ne}


class Expression:
    def __init__(self, source, names):
        if isinstance(source, bool) or source is None or isinstance(source, (int, float)):
            source = repr(source)
        if not isinstance(source, str) or not 0 < len(source) <= 256:
            raise ValueError('Expressions must be numbers or at most 256 characters')
        try:
            self.tree = ast.parse(source, mode='eval').body
        except (SyntaxError, RecursionError) as exc:
            raise ValueError('Invalid expression: ' + source) from exc
        nodes = list(ast.walk(self.tree))
        if len(nodes) > 128:
            raise ValueError('Expression is too complex')
        allowed = (ast.Constant, ast.Name, ast.Load, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
                   ast.IfExp, ast.Call, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow,
                   ast.UAdd, ast.USub, ast.Not, ast.And, ast.Or, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq)
        for node in nodes:
            if not isinstance(node, allowed):
                raise ValueError('Unsupported expression syntax: ' + type(node).__name__)
            if isinstance(node, ast.Constant) and node.value is not None and not isinstance(node.value, (int, float, bool)):
                raise ValueError('Expressions cannot contain strings or containers')
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name) or node.func.id not in RESERVED or node.keywords or not 1 <= len(node.args) <= 8:
                    raise ValueError('Only bounded arithmetic functions are allowed')
                arity = len(node.args)
                if node.func.id in ('abs', 'sqrt', 'log', 'log10', 'is_valid') and arity != 1:
                    raise ValueError(node.func.id + ' takes one argument')
                if node.func.id in ('min', 'max', 'coalesce') and arity < 2:
                    raise ValueError(node.func.id + ' takes at least two arguments')
        self.names = {node.id for node in nodes if isinstance(node, ast.Name)} - RESERVED
        if self.names - set(names):
            raise ValueError('Unknown expression names: ' + ', '.join(sorted(self.names - set(names))))
        # Function identifiers must appear only as the callee of an allowed call.
        callees = {id(node.func) for node in nodes if isinstance(node, ast.Call)}
        if any(isinstance(node, ast.Name) and node.id in RESERVED and id(node) not in callees for node in nodes):
            raise ValueError('Function names cannot be used as values')
        self.source = source

    def evaluate(self, values):
        def run(node):
            if isinstance(node, ast.Constant):
                return node.value
            if isinstance(node, ast.Name):
                return values.get(node.id)
            if isinstance(node, ast.IfExp):
                condition = run(node.test)
                return None if condition is None else run(node.body if condition else node.orelse)
            if isinstance(node, ast.BoolOp):
                missing = False
                for child in node.values:
                    value = run(child)
                    missing |= value is None
                    if isinstance(node.op, ast.And) and value is not None and not value:
                        return False
                    if isinstance(node.op, ast.Or) and value is not None and bool(value):
                        return True
                return None if missing else isinstance(node.op, ast.And)
            if isinstance(node, ast.UnaryOp):
                value = run(node.operand)
                if value is None:
                    return None
                return not value if isinstance(node.op, ast.Not) else -value if isinstance(node.op, ast.USub) else +value
            if isinstance(node, ast.BinOp):
                left, right = run(node.left), run(node.right)
                if left is None or right is None:
                    return None
                if isinstance(node.op, ast.Pow):
                    if abs(right) > 16:
                        raise ValueError('Exponent must be between -16 and 16')
                    value = left ** right
                else:
                    value = BINARY[type(node.op)](left, right)
                if not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 1e300:
                    raise ValueError('Non-finite calculation')
                return value
            if isinstance(node, ast.Compare):
                left = run(node.left)
                for operation, child in zip(node.ops, node.comparators):
                    right = run(child)
                    if left is None or right is None:
                        return None
                    if not COMPARE[type(operation)](left, right):
                        return False
                    left = right
                return True
            if isinstance(node, ast.Call):
                args = [run(child) for child in node.args]
                if node.func.id == 'is_valid':
                    return args[0] is not None and math.isfinite(args[0])
                if node.func.id == 'coalesce':
                    return next((value for value in args if value is not None), None)
                if any(value is None for value in args):
                    return None
                return FUNCTIONS[node.func.id](*args)
            raise ValueError('Unsupported expression')
        try:
            value = run(self.tree)
            return value if value is None or isinstance(value, (int, float, bool)) and math.isfinite(value) and abs(value) <= 1e300 else None
        except (ArithmeticError, TypeError, ValueError, RecursionError):
            return None
