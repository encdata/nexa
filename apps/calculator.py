import ast
import math
import re

import termuxgui as tg

from shell import palette


_FUNCTIONS = {
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "sqrt": math.sqrt, "ln": math.log, "log": math.log10,
    "abs": abs, "exp": math.exp, "floor": math.floor, "ceil": math.ceil,
    "factorial": math.factorial,
}


def _rewrite_postfix(expression):
    for pattern, replacement in ((r"(\d+(?:\.\d+)?|\([^()]*\))!", r"factorial(\1)"),
                                 (r"(\d+(?:\.\d+)?|\([^()]*\))%", r"(\1/100)")):
        while True:
            updated = re.sub(pattern, replacement, expression)
            if updated == expression:
                break
            expression = updated
    return expression.replace("^", "**")


def _evaluate(expression, degrees=True):
    if len(expression) > 160:
        raise ValueError("expression is too long")
    tree = ast.parse(_rewrite_postfix(expression), mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 96:
        raise ValueError("expression is too complex")

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id == "pi":
                return math.pi
            if node.id == "e":
                return math.e
            raise ValueError("unknown value")
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.Pow):
                if abs(right) > 1000:
                    raise ValueError("exponent is too large")
                return left ** right
            raise ValueError("unsupported operator")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            function = _FUNCTIONS.get(node.func.id)
            if function is None or len(node.args) != 1:
                raise ValueError("unknown function")
            value = visit(node.args[0])
            if node.func.id == "factorial":
                if not value.is_integer() or value < 0 or value > 170:
                    raise ValueError("factorial needs a whole number from 0 to 170")
                return math.factorial(int(value))
            if node.func.id in ("sin", "cos", "tan") and degrees:
                value = math.radians(value)
            result = function(value)
            if node.func.id in ("asin", "acos", "atan") and degrees:
                result = math.degrees(result)
            return result
        raise ValueError("unsupported expression")

    result = visit(tree)
    if not math.isfinite(result):
        raise ValueError("result is out of range")
    return float(result)


def _format_number(value):
    if value == 0:
        return "0"
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.12g}"


class CalculatorApp:
    app_id = "calculator"
    name = "Calculator"
    icon = "calc"
    preload_ui = True
    build_threaded = True

    def build(self, activity, parent, context=None):
        root = tg.LinearLayout(activity, parent)
        root.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)
        root.setbackgroundcolor(palette.window_content)

        calc_page = tg.LinearLayout(activity, root)
        calc_page.setdimensions(tg.View.MATCH_PARENT, tg.View.MATCH_PARENT)

        key_actions = {}
        inverse_mode = False
        degree_mode = True
        expression = ""
        just_evaluated = False
        history = []

        def button(parent_view, label, height, action, color=palette.surface):
            view = tg.TextView(activity, label, parent_view)
            view.setdimensions(0, height)
            view.setlinearlayoutparams(1)
            view.setmargin(3, "left")
            view.setmargin(3, "right")
            view.setmargin(3, "top")
            view.setmargin(3, "bottom")
            view.setbackgroundcolor(color)
            view.settextcolor(palette.text if color != palette.accent else palette.background)
            view.settextsize(15)
            view.setgravity(1, 1)
            view.sendclickevent(True)
            key_actions[view.id] = action
            return view

        expression_view = tg.TextView(activity, "", calc_page)
        expression_view.setdimensions(tg.View.MATCH_PARENT, 30)
        expression_view.setlinearlayoutparams(0)
        expression_view.setmargin(12, "left")
        expression_view.setmargin(12, "right")
        expression_view.settextsize(14)
        expression_view.settextcolor(palette.muted)
        expression_view.setgravity(2, 1)
        result_view = tg.TextView(activity, "0", calc_page)
        result_view.setdimensions(tg.View.MATCH_PARENT, 68)
        result_view.setlinearlayoutparams(0)
        result_view.setmargin(12, "left")
        result_view.setmargin(12, "right")
        result_view.settextsize(38)
        result_view.settextcolor(palette.text)
        result_view.setgravity(2, 1)

        keypad_scroll = tg.NestedScrollView(activity, calc_page, fillviewport=True, nobar=True)
        keypad_scroll.setheight(0)
        keypad_scroll.setlinearlayoutparams(1)
        keypad = tg.LinearLayout(activity, keypad_scroll)
        keypad.setdimensions(tg.View.MATCH_PARENT, tg.View.WRAP_CONTENT)
        mode_button = None
        inverse_button = None
        function_buttons = {}
        scientific_rows = [
            [("2nd", "inverse"), ("DEG", "angle"), ("(", "("), (")", ")"), ("DEL", "delete")],
            [("sin", "sin"), ("cos", "cos"), ("tan", "tan"), ("√", "sqrt"), ("AC", "clear")],
            [("ln", "ln"), ("log", "log"), ("π", "pi"), ("e", "e"), ("x²", "square")],
            [("7", "7"), ("8", "8"), ("9", "9"), ("÷", "/"), ("^", "^")],
            [("4", "4"), ("5", "5"), ("6", "6"), ("×", "*"), ("n!", "!")],
            [("1", "1"), ("2", "2"), ("3", "3"), ("−", "-"), ("+", "+")],
            [("0", "0"), (".", "."), ("%", "%"), ("=", "equals"), ("abs", "abs")],
        ]
        for row_values in scientific_rows:
            row = tg.LinearLayout(activity, keypad, vertical=False)
            row.setdimensions(tg.View.MATCH_PARENT, 52)
            row.setlinearlayoutparams(0)
            for label, action in row_values:
                color = palette.accent if action == "equals" else palette.surface_light if action in (
                    "/", "*", "-", "+", "^", "clear", "delete") else palette.surface
                view = button(row, label, 48, action, color)
                if action == "angle":
                    mode_button = view
                elif action == "inverse":
                    inverse_button = view
                elif action in ("sin", "cos", "tan"):
                    key_actions[view.id] = ("function", action)
                    function_buttons[action] = view

        def render_expression():
            shown = expression.replace("pi", "π").replace("**", "^")
            expression_view.settext(shown[-42:])
            if expression:
                result_view.settext(shown[-18:])
            else:
                result_view.settext("0")

        def append_value(value):
            nonlocal expression, just_evaluated
            if just_evaluated:
                if value in ("+", "-", "*", "/", "^", "%", "!"):
                    just_evaluated = False
                else:
                    expression = ""
                    just_evaluated = False
            if value in ("+", "-", "*", "/", "^"):
                if not expression:
                    if value == "-":
                        expression = "-"
                    else:
                        return
                elif expression[-1] in "+-*/^.(":
                    if value == "-" and expression[-1] in "^(":
                        expression += value
                    else:
                        expression = expression[:-1] + value
                else:
                    expression += value
            elif value == ".":
                token = re.split(r"[+*/^()\-]", expression)[-1]
                if "." in token:
                    return
                expression += "0." if not token else "."
            elif value == "(":
                if expression and (expression[-1].isdigit() or expression[-1] == "." or expression[-1] == ")"):
                    expression += "*"
                expression += "("
            elif value == ")":
                if expression.count("(") > expression.count(")") and expression and expression[-1] not in "+-*/^(.":
                    expression += ")"
            elif value in ("pi", "e"):
                if expression and (expression[-1].isdigit() or expression[-1] == ")"):
                    expression += "*"
                expression += value
            elif value in ("sin", "cos", "tan", "asin", "acos", "atan", "sqrt", "ln", "log", "abs"):
                if expression and (expression[-1].isdigit() or expression[-1] == ")"):
                    expression += "*"
                expression += value + "("
            elif value == "square":
                if expression and expression[-1] not in "+-*/^(.":
                    expression += "^2"
            elif value == "!":
                if expression and expression[-1] not in "+-*/^(.":
                    expression += "!"
            elif value == "%":
                if expression and expression[-1] not in "+-*/^(.":
                    expression += "%"
            else:
                if expression.endswith(")"):
                    expression += "*"
                expression += value
            render_expression()

        def calculate():
            nonlocal expression, just_evaluated
            if not expression:
                return
            try:
                answer = _format_number(_evaluate(expression, degree_mode))
            except (ArithmeticError, OverflowError, SyntaxError, ValueError, TypeError):
                result_view.settext("error")
                return
            history.append(f"{expression.replace('pi', 'π')} = {answer}")
            history[:] = history[-20:]
            expression_view.settext(history[-1][-42:])
            result_view.settext(answer)
            expression = answer
            just_evaluated = True

        def handle_event(event):
            nonlocal degree_mode, inverse_mode, expression, just_evaluated
            if event.type != tg.Event.click:
                return False
            action = key_actions.get(event.id)
            if action == "inverse":
                inverse_mode = not inverse_mode
                inverse_button.settext("2nd on" if inverse_mode else "2nd")
                for name, view in function_buttons.items():
                    view.settext({"sin": "asin", "cos": "acos", "tan": "atan"}[name]
                                 if inverse_mode else name)
            elif action == "angle":
                degree_mode = not degree_mode
                mode_button.settext("DEG" if degree_mode else "RAD")
            elif action == "clear":
                expression = ""
                just_evaluated = False
                expression_view.settext("")
                result_view.settext("0")
            elif action == "delete":
                expression = expression[:-1]
                just_evaluated = False
                render_expression()
            elif action == "equals":
                calculate()
            elif isinstance(action, tuple) and action[0] == "function":
                name = action[1]
                if inverse_mode:
                    name = {"sin": "asin", "cos": "acos", "tan": "atan"}[name]
                append_value(name)
            elif action in ("sin", "cos", "tan", "asin", "acos", "atan", "sqrt", "ln", "log", "abs",
                            "pi", "e", "square", "!", "%", ".", "+", "-", "*", "/", "^", "(", ")") or action.isdigit():
                append_value(action)
            else:
                return False
            return True

        class CalculatorSession:
            def handle_event(self, event):
                return handle_event(event)

        return CalculatorSession()


APP = CalculatorApp()
