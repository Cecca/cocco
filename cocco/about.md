# About COCCO

COCCO, the *Coding Challenges Companion*, is a platform for
programming challenges and exercises.

Students will find collection of challenges, each with its own test cases: submissions are compiled and checked against all test cases. To foster some healthy competition, submissions are then ranked in a challenge-specific way (e.g. by running time, memory, accuracy, etc..).

Besides challenges, the platform allows to host short exercises to be carried out live in class: students write short programs in the browser without the need to install any programming toolchain, while the teacher follows the class from a dashboard.

## How a submission is graded

Each submission runs inside [isolate](https://github.com/ioi/isolate), the
sandbox the IOI and ICPC judges use, against every active test case of its
challenge. Compilers and interpreters come from a pinned Nix flake, so a
program is graded by the same toolchain every time.

## Credits

Design and implementation: [Matteo Ceccarello](https://www.dei.unipd.it/~ceccarello/)

Icon: [Gracielle Perocillo](https://www.behance.net/gracielleperocillo)

Beta testing: the students of the 2026 edition of _Fondamenti di Informatica_ at the [Department of Information Engineering](https://www.dei.unipd.it/) of UNIPD.
