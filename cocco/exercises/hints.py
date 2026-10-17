"""A short explanation of a Python exception, for a student who has just met it.

A port of `errorHint` from the prototype's `comune.js`, translated to English
like the rest of the interface. It is keyed on the exception's class name, which
is what the harness reports and what a hidden test is allowed to show.
"""

HINTS = {
    "SyntaxError": "syntax error (check brackets, colons and quotes).",
    "IndentationError": "wrong indentation (check the spaces at the start of the line).",
    "TabError": "indentation that mixes tabs and spaces.",
    "NameError": "name not defined (a misspelt variable or function, or one used before it was created).",
    "TypeError": "a value of the wrong type for the operation (for example adding a number and a string).",
    "ValueError": "invalid value (for example int() applied to text that is not a number).",
    "ZeroDivisionError": "division by zero.",
    "IndexError": "index out of range for the list or string.",
    "KeyError": "key not in the dictionary.",
    "AttributeError": "no such method or attribute for this type of value.",
    "RecursionError": "recursion too deep (is a base case missing?).",
    "EOFError": "the program asks for more lines of input than there are.",
    "UnboundLocalError": "variable used before it was given a value.",
    "ModuleNotFoundError": "module not available here.",
    "OverflowError": "number too large.",
    "MemoryError": "out of memory.",
}


def error_hint(type_name: str | None, line: int | None = None) -> str:
    """One sentence about an exception, prefixed with its line when known.

    An exception with no entry above is named rather than explained, and no
    exception at all is the empty string, as in the prototype.
    """
    if not type_name:
        return ""
    hint = HINTS.get(type_name, f"{type_name} error.")
    if line:
        return f"Line {line}: {hint}"
    return hint[0].upper() + hint[1:]
