---
title: Warm-up in C
description: A worked example of a C set
language: c
---

Read from standard input with `scanf` and print with `printf`. Do not print
prompts.

# Sum

Read two integers and print their sum.

```starter
#include <stdio.h>

int main(void) {
    int a, b;
    scanf("%d %d", &a, &b);
    /* print the sum */
    return 0;
}
```

```solution
#include <stdio.h>

int main(void) {
    int a, b;
    scanf("%d %d", &a, &b);
    printf("%d\n", a + b);
    return 0;
}
```

```input
2 3
```

```input hidden
-4 10
```

# Maximum

The first line holds `n`, the second `n` integers. Print the largest.

```solution
#include <stdio.h>

int main(void) {
    int n, x, max;
    scanf("%d", &n);
    scanf("%d", &max);
    for (int i = 1; i < n; i++) {
        scanf("%d", &x);
        if (x > max) {
            max = x;
        }
    }
    printf("%d\n", max);
    return 0;
}
```

```input
3
4 9 2
```

```input hidden
1
-5
```

```input hidden
4
-3 -1 -7 -1
```

```hint
Start from the first number, not from 0: every number may be negative.
```

# Square root

Read a number and print its square root with three decimals.

compare: numbers 0.001

```solution
#include <math.h>
#include <stdio.h>

int main(void) {
    double x;
    scanf("%lf", &x);
    printf("%.3f\n", sqrt(x));
    return 0;
}
```

```input
2
```

```input hidden
0
```

# Remainder

Read two integers and print the remainder of the first divided by the second.
Dividing by zero kills the program with `SIGFPE`.

```solution
#include <stdio.h>

int main(void) {
    int a, b;
    scanf("%d %d", &a, &b);
    printf("%d\n", a % b);
    return 0;
}
```

```input
17 5
```

```input
1 0
```

```error
SIGFPE
```
