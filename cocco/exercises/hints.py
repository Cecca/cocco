"""A short explanation of a failure, for a student who has just met it.

A port of `errorHint` from the prototype's `comune.js`, translated to English
like the rest of the interface. It is keyed on the failure's name, which is
what a run reports as `error` and what a hidden test is allowed to show: a
Python exception's class name, taken from the traceback, or the name of the
signal that ended a compiled program (`SIGSEGV`). There is no line number: the full
stderr names it.
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
    # What a compiled program is ended by, as `kill -l` spells it.
    "SIGSEGV": "invalid memory access: an index past the end of an array, or a NULL or uninitialised pointer.",
    "SIGFPE": "integer division by zero.",
    "SIGABRT": "an assert failed, or abort() was called.",
    "SIGBUS": "invalid memory access: a misaligned or unmapped address.",
    "SIGILL": "the program tried to run an invalid instruction (a corrupted function pointer or return address).",
    "SIGXCPU": "the program used up its CPU time.",
}


def error_hint(type_name: str | None) -> str:
    """One sentence about a failure.

    A name with no entry above is named rather than explained, and no failure
    at all is the empty string, as in the prototype.
    """
    if not type_name:
        return ""
    hint = HINTS.get(type_name, f"{type_name} error.")
    return hint[0].upper() + hint[1:]
