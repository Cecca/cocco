---
title: Loops and lists
description: A worked example of every part of the format
language: python
---

Each exercise reads its input from the keyboard and prints its answer.
Do not print prompts: `input()`, never `input("n? ")`.

# Sum of a line

The input is one line of integers separated by spaces. Print their sum.

For example, on

```
3 4 5
```

the program prints

```
12
```

```starter
line = input()
```

```solution
print(sum(int(x) for x in input().split()))
```

```input
3 4 5
```

```input
-2 2
```

```input hidden
7
```

```input hidden
-1 -2 -3
```

```hint
`line.split()` turns the line into a list of strings.
```

```hint
Convert each string with `int` before adding it up.
```

# Average

The input is one line of numbers separated by spaces. Print their average.

compare: numbers 0.001

```solution
numbers = [float(x) for x in input().split()]
print(sum(numbers) / len(numbers))
```

```input
1 2 3 4
```

```input
2.5 3.5
```

```input hidden
1 1 2
```

# Distinct words

The input is one line of words. Print each distinct word once, one per line,
in any order.

compare: order

```solution
for word in set(input().split()):
    print(word)
```

```input
the cat and the dog
```

```input hidden
a a a
```

# Safe division

The input is two integers on one line. Print the first divided by the second,
rounded down. Dividing by zero must end the program with a
`ZeroDivisionError`: do not catch it.

```solution
a, b = map(int, input().split())
print(a // b)
```

```input
17 5
```

```input
-7 2
```

```input hidden
1 0
```

```error
ZeroDivisionError
```

```hint
`//` divides rounding down.
```
