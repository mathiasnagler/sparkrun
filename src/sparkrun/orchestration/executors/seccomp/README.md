# Docker io_uring profile

`default.json` and `LICENSE` are unmodified copies from
[moby/profiles](https://github.com/moby/profiles/tree/61eaf32614c7c71b60bd8927d3e6a4ffc8ff1f31),
commit `61eaf32614c7c71b60bd8927d3e6a4ffc8ff1f31` (2026-08-28), Apache-2.0.
The source profile is `seccomp/default.json` in that repository.

`../_seccomp.py` adds exactly one unconditional `SCMP_ACT_ALLOW` rule for
`io_uring_enter`, `io_uring_register`, and `io_uring_setup`. All other rules,
architecture mappings, capability conditions, and the default deny action are
retained. Custom user profiles are loaded as supplied, without that amendment.

To update: select and review an upstream commit, replace both upstream files,
update this pin and the source digest in `tests/test_docker_seccomp.py`, then run
the seccomp tests and installed-wheel tests. Review upstream rule changes as
security changes; do not fetch a moving upstream profile at application startup.
